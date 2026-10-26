"""Running a set of fixed Logs Insights queries together and waiting for them (`run_queries`).

Moved here from ops_mcp/logs.py unchanged, so that code in the pipeline's Lambda package can run
Logs Insights queries without depending on the operator's assistant: the evals rollup
(docs/enhancements/model-ranch-prompt-assets-evals.md) will read its events this way. ops_mcp/logs.py
imports everything below and keeps answering to the same names, so the assistant's tools and the
evidence export call it as before. The defaults (the wait, the shared client's timeouts, the label)
are still the assistant's; another caller passes its own.

The runner is handed every query whole, a constant of the module that runs it; it never builds
one. It asks Logs Insights for at most QUERY_LIMIT rows a query, and stops any query still running
when the wait ends, so an unfinished query costs no more. Read-only: StartQuery, GetQueryResults and
StopQuery.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

QUERY_LIMIT = 1000
# Logs Insights runs a query in the background; an assistant tool waits this long for all of them,
# then answers with what has finished. Well inside its function's 30 seconds and API Gateway's 29.
WAIT_SECONDS = 15
POLL_SECONDS = 0.5

# One retry, two attempts in all: queries are started and polled together, and Logs throttles bursts.
_LOGS_CONFIG = Config(
    connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 2, "mode": "standard"}
)
# What Logs throttles with: a request rate (ThrottlingException), or the queries an account may run at
# once (LimitExceededException). The shared client's retry rides out one; a caller whose clients make
# one attempt each (vision_evidence) asks run_queries to (`recover_throttles`).
THROTTLES = frozenset({"ThrottlingException", "LimitExceededException"})
_clients: dict[str, object] = {}


def client(region: str):
    """CloudWatch Logs in a region, one client each, kept for the life of the function."""
    if region not in _clients:
        _clients[region] = boto3.client("logs", region_name=region, config=_LOGS_CONFIG)
    return _clients[region]


def _throttled(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in THROTTLES


def _start_query(client, region: str, group, query: str, start: datetime, end: datetime) -> str:
    several = isinstance(group, list | tuple)
    where = {"logGroupNames": list(group)} if several else {"logGroupName": group}
    response = client(region).start_query(
        **where,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
        limit=QUERY_LIMIT,
    )
    return response["queryId"]


def run_queries(
    jobs: list[tuple[tuple[str, str], str, str | tuple[str, ...], str, datetime, datetime]],
    *,
    client: Callable[[str], object] = client,
    wait_seconds: float = WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "ops_logs",
    recover_throttles: bool = False,
) -> dict[tuple[str, str], list[dict] | None]:
    """Start every (key, region, group, query, start, end) query at once, then wait for them. `group`
    is one log group's name, or a tuple of names for one query over several (Logs Insights takes up
    to 50, and @log in each row says which it came from). Each
    result is a list of rows ({field: value}), or None for a query that failed or did not finish.
    A query still running at the deadline is stopped, so it costs no more. `label` starts each line
    this prints; unset, it is the assistant's own, as it was before the runner moved here.

    `recover_throttles` is for a caller whose clients make one attempt each (vision_evidence's, so a
    request that stalls cannot run twice): a request Logs throttles (THROTTLES) is then not a
    failure. A start is tried once more, after one pause, through a new `client`; a poll counts as
    still running and is asked again next round, within the wait. The other tools leave it off and
    are answered as before: the shared client's own retry rides out a throttle for them."""
    started: dict[tuple[str, str], tuple[str, str]] = {}
    results: dict[tuple[str, str], list[dict] | None] = {}
    again = []
    for key, region, group, query, start, end in jobs:
        try:
            started[key] = (region, _start_query(client, region, group, query, start, end))
        except Exception as exc:  # noqa: BLE001 - one group that cannot be read is reported as such
            if recover_throttles and _throttled(exc):
                print(f"{label}: start throttled, tried once more")
                again.append((key, region, group, query, start, end))
                continue
            print(f"{label}: start failed error={type(exc).__name__}")
            results[key] = None
    if again:
        sleep(POLL_SECONDS)  # one pause, as between polls
        for key, region, group, query, start, end in again:
            try:
                started[key] = (region, _start_query(client, region, group, query, start, end))
            except Exception as exc:  # noqa: BLE001 - the one more try failed too: as any failure
                print(f"{label}: start failed error={type(exc).__name__}")
                results[key] = None
    deadline = time.monotonic() + wait_seconds
    while started and time.monotonic() < deadline:
        for key, (region, query_id) in list(started.items()):
            try:
                response = client(region).get_query_results(queryId=query_id)
            except Exception as exc:  # noqa: BLE001
                if recover_throttles and _throttled(exc):
                    print(f"{label}: results throttled, asked again")  # still running, as far as known
                    continue
                print(f"{label}: results failed error={type(exc).__name__}")
                results[key] = None
                del started[key]
                continue
            status = response.get("status")
            if status == "Complete":
                rows = response.get("results", [])
                results[key] = [{field.get("field"): field.get("value") for field in row} for row in rows]
                del started[key]
            elif status in ("Failed", "Cancelled", "Timeout", "Unknown"):
                results[key] = None
                del started[key]
        if started:
            sleep(POLL_SECONDS)
    for key, (region, query_id) in started.items():
        results[key] = None  # still running: what finished is reported, and this is said
        try:
            client(region).stop_query(queryId=query_id)
        except Exception:  # noqa: BLE001,S110 - it ends by itself; stopping it only saves money
            pass
    return results
