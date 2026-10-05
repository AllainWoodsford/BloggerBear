"""Reading CloudWatch Logs, for the tools that look at logs (log_review, api_errors, firewall_review).

Two things live here: which log groups this assistant may read at all (`check_group`), and running a
set of fixed Logs Insights queries together and waiting for them (`run_queries`).

**Which log groups. The owner's rule is environment, project and ManagedBy, held three ways:**

1. IAM (infra/modules/ops-assistant/logs.tf): logs:StartQuery is allowed only on
   /aws/lambda/bloggerbear-<env>-* and /aws/apigateway/bloggerbear-<env>-*, for each environment
   this assistant may read, and only when the group carries the project's default tags
   (ManagedBy, Project) and that Environment. The Deny in isolation.tf refuses every other
   Environment.
2. Here, by name, before anything is asked of AWS: a group must be a Lambda's or an API's access
   log, named bloggerbear-<env>-..., with <env> one this assistant may read
   (samples.readable_environments: dev reads dev's; production reads production's and "shared";
   dev never reads production's or the shared ones, and production never reads dev's).
3. Here, by tags: the group's own tags are listed and compared with the default tags the function
   is given (OPS_DEFAULT_TAGS) and the readable environments. Anything that differs is refused.
   A group's tags are remembered for TAG_CACHE_SECONDS, so a briefing that reads five groups twice
   lists each once.

The names a tool asks for are built from the architecture catalogue (architecture.py), never from
what the model or the operator typed; this check is the net under that.

**Fixed queries.** Every query string is a constant in the module that runs it, with at most a
whole number or a value from a fixed list put into it. The model never writes a query, so it
cannot be steered into reading something else or into an expensive scan. The window is clamped by
each tool, and Logs Insights is asked for at most QUERY_LIMIT rows.

**Read-only.** StartQuery, GetQueryResults, StopQuery and ListTagsForResource. Nothing here writes.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ops_mcp import architecture, samples

QUERY_LIMIT = 100
# Logs Insights runs a query in the background; a tool waits this long for all of them, then
# answers with what has finished. Well inside the function's 30 seconds and API Gateway's 29.
WAIT_SECONDS = 15
POLL_SECONDS = 0.5
TAG_CACHE_SECONDS = 300

# A log group this assistant could ever read: a Lambda's own log, or an API's access log, of one
# of the project's environments. The environment is the same word architecture.py and the alarms
# tool use: a lowercase letter, then letters and digits (no hyphen, so "dev" never matches "dev-x").
GROUP_PATTERN = re.compile(
    r"^/aws/(?P<service>lambda|apigateway)/bloggerbear-(?P<env>[a-z][a-z0-9]{1,31})-[a-z0-9][a-z0-9-]{0,100}$"
)

_LOGS_CONFIG = Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 2, "mode": "standard"})
_clients: dict[str, object] = {}
_tags: dict[str, tuple[float, dict[str, str]]] = {}


class NotAllowed(Exception):
    """A log group this assistant may not read. The message is fixed text, for speech."""


def client(region: str):
    """CloudWatch Logs in a region, one client each, kept for the life of the function."""
    if region not in _clients:
        _clients[region] = boto3.client("logs", region_name=region, config=_LOGS_CONFIG)
    return _clients[region]


def home_region() -> str | None:
    return architecture.region() or os.environ.get("AWS_DEFAULT_REGION") or None


def group_arn(name: str, region: str) -> str:
    return f"arn:aws:logs:{region}:{samples._account_id()}:log-group:{name}"


def check_name(name: str) -> str:
    """The environment a group belongs to, if its name is one this assistant may read; otherwise
    NotAllowed. No AWS call."""
    env = architecture.environment()
    if env is None:
        raise NotAllowed("I don't know which environment I'm in, so I won't read any logs.")
    match = GROUP_PATTERN.match(name or "")
    if match is None:
        raise NotAllowed("That isn't a log group I can read.")
    if match.group("env") not in samples.readable_environments(env):
        raise NotAllowed("That log group belongs to an environment I'm not allowed to read.")
    return match.group("env")


def check_group(name: str, *, region: str | None = None, now: float | None = None) -> dict[str, str]:
    """The group's tags, if both its name and its tags allow reading it; otherwise NotAllowed."""
    env_from_name = check_name(name)
    region = region or home_region()
    if not region:
        raise NotAllowed("I don't know which region to read logs in.")
    now = time.monotonic() if now is None else now
    cached = _tags.get(name)
    if cached is not None and now - cached[0] < TAG_CACHE_SECONDS:
        tags = cached[1]
    else:
        try:
            tags = client(region).list_tags_for_resource(resourceArn=group_arn(name, region)).get("tags", {})
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                raise NotAllowed("That log group doesn't exist in this environment.") from exc
            raise NotAllowed(
                "AWS refused to let me read that log group's tags, so I won't read it."
            ) from exc
        except BotoCoreError as exc:
            raise NotAllowed("I couldn't reach CloudWatch Logs.") from exc
        _tags[name] = (now, tags)
    env = architecture.environment() or ""
    for key, value in samples.default_tags().items():
        if tags.get(key) != value:
            raise NotAllowed(f"That log group isn't tagged {key} = {value}, so I won't read it.")
    tagged = tags.get("Environment")
    if tagged not in samples.readable_environments(env) or tagged != env_from_name:
        raise NotAllowed("That log group is tagged for an environment I'm not allowed to read.")
    return {key: tags[key] for key in (*samples.REQUIRED_TAG_KEYS, "Environment")}


def readable(names: list[str], *, region: str | None = None) -> tuple[list[str], list[dict]]:
    """The groups of `names` this assistant may read, and for each refused one why (fixed text).
    A tool reads the first list and reports the second."""
    allowed, refused = [], []
    for name in dict.fromkeys(names):
        try:
            check_group(name, region=region)
        except NotAllowed as exc:
            refused.append({"log_group": name, "why": str(exc)})
        else:
            allowed.append(name)
    return allowed, refused


def run_queries(
    jobs: list[tuple[tuple[str, str], str, str, str, datetime, datetime]],
    *,
    client: Callable[[str], object] = client,
    wait_seconds: float = WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "ops_logs",
) -> dict[tuple[str, str], list[dict] | None]:
    """Start every (key, region, group, query, start, end) query at once, then wait for them. Each
    result is a list of rows ({field: value}), or None for a query that failed or did not finish.
    A query still running at the deadline is stopped, so it costs no more."""
    started: dict[tuple[str, str], tuple[str, str]] = {}
    results: dict[tuple[str, str], list[dict] | None] = {}
    for key, region, group, query, start, end in jobs:
        try:
            response = client(region).start_query(
                logGroupName=group,
                startTime=int(start.timestamp()),
                endTime=int(end.timestamp()),
                queryString=query,
                limit=QUERY_LIMIT,
            )
            started[key] = (region, response["queryId"])
        except Exception as exc:  # noqa: BLE001 - one group that cannot be read is reported as such
            print(f"{label}: start failed error={type(exc).__name__}")
            results[key] = None
    deadline = time.monotonic() + wait_seconds
    while started and time.monotonic() < deadline:
        for key, (region, query_id) in list(started.items()):
            try:
                response = client(region).get_query_results(queryId=query_id)
            except Exception as exc:  # noqa: BLE001
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


def count(value) -> int:
    """A count from a Logs Insights row (they are all strings), never negative."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0
