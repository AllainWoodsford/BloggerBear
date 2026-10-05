"""Reading CloudWatch Logs, for the tools that look at logs (log_review, api_errors, firewall_review).

Two things live here: which log groups this assistant may read at all (`check_group`), and running a
set of fixed Logs Insights queries together and waiting for them (`run_queries`).

**Which log groups. The owner's rule is environment, project and ManagedBy, held three ways:**

1. IAM (infra/modules/ops-assistant/logs.tf): logs:StartQuery is allowed only on
   /aws/lambda/<prefix>-<env>-* and /aws/apigateway/<prefix>-<env>-*, for each environment
   this assistant may read, and only when the group carries the project's default tags
   (ManagedBy, Project) and that Environment. The Deny in isolation.tf refuses every other
   Environment.
2. Here, by name, before anything is asked of AWS: a group must be a Lambda's or an API's access
   log, named <prefix>-<env>-... (common/naming.py), with <env> one this assistant may read
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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from common.naming import NAME_PREFIX
from ops_mcp import architecture, samples

QUERY_LIMIT = 1000
# Logs Insights runs a query in the background; a tool waits this long for all of them, then
# answers with what has finished. Well inside the function's 30 seconds and API Gateway's 29.
WAIT_SECONDS = 15
POLL_SECONDS = 0.5
TAG_CACHE_SECONDS = 300

# A log group this assistant could ever read: a Lambda's own log, or an API's access log, of one
# of the project's environments. The environment is the same word architecture.py and the alarms
# tool use: a lowercase letter, then letters and digits (no hyphen, so "dev" never matches "dev-x").
# The prefix is this deployment's (common/naming.py), never written out.
GROUP_PATTERN = re.compile(
    rf"^/aws/(?P<service>lambda|apigateway)/{re.escape(NAME_PREFIX)}-"
    r"(?P<env>[a-z][a-z0-9]{1,31})-[a-z0-9][a-z0-9-]{0,100}$"
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
    jobs: list[tuple[tuple[str, str], str, str | tuple[str, ...], str, datetime, datetime]],
    *,
    client: Callable[[str], object] = client,
    wait_seconds: float = WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "ops_logs",
) -> dict[tuple[str, str], list[dict] | None]:
    """Start every (key, region, group, query, start, end) query at once, then wait for them. `group`
    is one log group's name, or a tuple of names for one query over several (Logs Insights takes up
    to 50, and @log in each row says which it came from). Each
    result is a list of rows ({field: value}), or None for a query that failed or did not finish.
    A query still running at the deadline is stopped, so it costs no more."""
    started: dict[tuple[str, str], tuple[str, str]] = {}
    results: dict[tuple[str, str], list[dict] | None] = {}
    for key, region, group, query, start, end in jobs:
        try:
            several = isinstance(group, list | tuple)
            where = {"logGroupNames": list(group)} if several else {"logGroupName": group}
            response = client(region).start_query(
                **where,
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


# --- the time window a tool reads ------------------------------------------------------------------

DEFAULT_HOURS = 24
MAX_HOURS = 168  # one window is at most a week: Logs Insights bills by what it scans
LOOKBACK_DAYS = 30  # and starts no further back than this


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    asked: bool  # whether the operator gave the times (start/end) rather than a number of hours
    clamped: bool  # whether what they gave was moved to fit the limits

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    def words(self) -> str:
        """The window for speech: "the last 24 hours", or "between 01:00 and 03:00 UTC on 5 October"."""
        if not self.asked:
            hours = round(self.hours)
            return f"the last {hours} hour{'s' if hours != 1 else ''}"
        same_day = self.start.date() == self.end.date()
        if same_day:
            return (
                f"between {self.start:%H:%M} and {self.end:%H:%M} UTC on "
                f"{self.start.day} {self.start:%B}"
            )
        return (
            f"between {self.start.day} {self.start:%B} {self.start:%H:%M} and "
            f"{self.end.day} {self.end:%B} {self.end:%H:%M} UTC"
        )

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "clamped": self.clamped}


def _when(value) -> datetime | None:
    """A timestamp the model passed on from the operator: ISO 8601, with or without a zone (UTC if
    none), or None if it is missing or unreadable."""
    if not isinstance(value, str) or not value.strip() or len(value) > 40:
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def window(hours=None, start=None, end=None, *, now: datetime | None = None) -> Window:
    """The window a tool reads. With `start` (and optionally `end`): those times, kept inside the
    limits (no later than now, no earlier than LOOKBACK_DAYS ago, at most MAX_HOURS long; an end
    before the start is swapped). Otherwise the last `hours` (1 to MAX_HOURS, DEFAULT_HOURS if
    unreadable)."""
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    earliest = now - timedelta(days=LOOKBACK_DAYS)
    begin, finish = _when(start), _when(end)
    if begin is None and finish is None:
        try:
            value = int(hours) if hours is not None else DEFAULT_HOURS
        except (TypeError, ValueError):
            value = DEFAULT_HOURS
        kept = max(1, min(MAX_HOURS, value))
        return Window(now - timedelta(hours=kept), now, asked=False, clamped=kept != value)
    clamped = False
    if begin is None:
        begin, clamped = finish - timedelta(hours=DEFAULT_HOURS), False
    if finish is None:
        finish = min(now, begin + timedelta(hours=DEFAULT_HOURS))
    if finish < begin:
        begin, finish = finish, begin
    if finish > now:
        finish, clamped = now, True
    if begin < earliest:
        begin, clamped = earliest, True
    if finish - begin > timedelta(hours=MAX_HOURS):
        begin, clamped = finish - timedelta(hours=MAX_HOURS), True
    if finish <= begin:
        begin, clamped = finish - timedelta(hours=1), True
    return Window(begin, finish, asked=True, clamped=clamped)


def group_of(log_field) -> str:
    """A log group's name from Logs Insights' @log field ("<account>:<group name>")."""
    text = str(log_field or "")
    return text.split(":", 1)[1] if ":" in text and not text.startswith("/") else text
