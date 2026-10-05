"""firewall_review: the deep dive into what the firewall (AWS WAF) has been doing. Production only
(docs/enhancements/alexa-plus.md, section 4.4).

**Only where it may be.** The firewall's logs are not one environment's: the CloudFront firewall
in front of both sites logs to one shared group. So the tool exists only where the deployment says
this assistant may report account-wide data (production) **and** has been given log groups to
read; dev's assistant never has it, and its role has no right to any WAF log group either
(infra/modules/ops-assistant/firewall.tf). Every configured group must also be named for this
environment or be the shared one, or the tool is not registered at all.

**Fixed queries, counts out.** The model never writes a query: the Logs Insights queries are the
ones below, over a window the tool clamps. What comes back is counts per action and per rule, a
daily baseline, and the most-blocked paths. Never an address, a header, a query string or a raw
log line. A path is text an attacker chose, so it goes under `untrusted`, cut short, and is never
spoken.

**A deep dive, never part of a briefing.** WAF logs are the largest and most hostile logs in the
account. ops_agent/policy.py offers this tool only on a follow-up ("what's happening with the
firewall?"), never on a first question.

**Unusual is decided here.** A spike is blocks in the window above twice the median of the
previous seven days (scaled to the window) and at least SPIKE_MIN_BLOCKS. That is a finding, with
nothing to run: what to look at is the edge dashboard.

    OPS_WAF_LOG_GROUPS  comma-separated "<region>:<log group name>" entries
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from statistics import median

import boto3
from botocore.config import Config

from common.naming import NAME_PREFIX
from common.security_events import untrusted_text
from ops_mcp.account import ENVIRONMENT_ENV, account_wide_data
from ops_mcp.suggestions import finding

LOG_GROUPS_ENV = "OPS_WAF_LOG_GROUPS"
# The firewall's log groups are named from this deployment's prefix (common/naming.py), like
# everything else: "aws-waf-logs-bloggerbear-shared" in the original deployment.
SHARED_LOG_GROUP = f"aws-waf-logs-{NAME_PREFIX}-shared"
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

FIREWALL_DEFAULT_HOURS = 24
FIREWALL_MAX_HOURS = 72
BASELINE_DAYS = 7
SPIKE_TIMES = 2
SPIKE_MIN_BLOCKS = 50
TOP_RULES = 5
TOP_PATHS = 5
PATH_MAX_CHARS = 80
# Logs Insights runs a query in the background; the tool waits this long for all of them, then
# answers with what has finished. Well inside the function's 30 seconds and the agent's patience.
WAIT_SECONDS = 15
POLL_SECONDS = 0.5

# The queries. Fixed text: nothing a caller or the model says is ever put into one.
QUERY_ACTIONS = "stats count(*) as requests by action"
QUERY_RULES = (
    'filter action = "BLOCK" | stats count(*) as blocks by terminatingRuleId '
    f"| sort blocks desc | limit {TOP_RULES}"
)
QUERY_PATHS = (
    'filter action = "BLOCK" | stats count(*) as blocks by httpRequest.uri '
    f"| sort blocks desc | limit {TOP_PATHS}"
)
QUERY_BASELINE = 'filter action = "BLOCK" | stats count(*) as blocks by bin(1d)'

NOT_AVAILABLE = "The firewall review isn't available from this environment."

_LOGS_CONFIG = Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 2, "mode": "standard"})
_clients: dict[str, object] = {}


def _logs(region: str):
    if region not in _clients:
        _clients[region] = boto3.client("logs", region_name=region, config=_LOGS_CONFIG)
    return _clients[region]


def log_groups() -> list[tuple[str, str]] | None:
    """The configured (region, log group) pairs, or None if anything about them is wrong: no
    environment, an entry that does not parse, or a group that is neither this environment's nor
    the shared one. None means the tool is not registered."""
    environment = os.environ.get(ENVIRONMENT_ENV, "")
    if not re.fullmatch(r"[a-z][a-z0-9]{1,31}", environment):
        return None
    own = re.compile(
        rf"^aws-waf-logs-{re.escape(NAME_PREFIX)}-{re.escape(environment)}-[a-z0-9-]{{1,64}}$"
    )
    entries = [entry.strip() for entry in os.environ.get(LOG_GROUPS_ENV, "").split(",") if entry.strip()]
    if not entries:
        return None
    pairs = []
    for entry in entries:
        region, _, name = entry.partition(":")
        if not _REGION.match(region) or not (own.match(name) or name == SHARED_LOG_GROUP):
            return None
        pairs.append((region, name))
    return pairs


def available() -> bool:
    """Whether firewall_review is registered here: account-wide data allowed, and log groups that
    pass log_groups()."""
    return account_wide_data() and log_groups() is not None


def _clamp_hours(hours) -> int:
    try:
        value = int(hours)
    except (TypeError, ValueError):
        value = FIREWALL_DEFAULT_HOURS
    return max(1, min(FIREWALL_MAX_HOURS, value))


def _run_queries(
    jobs: list[tuple[tuple[str, str], str, str, str, datetime, datetime]],
    *,
    client: Callable[[str], object] = _logs,
    wait_seconds: float = WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[tuple[str, str], list[dict] | None]:
    """Start every (key, region, group, query, start, end) query at once, then wait for them. Each
    result is a list of rows ({field: value}), or None for a query that failed or did not finish."""
    started: dict[tuple[str, str], tuple[str, str]] = {}
    results: dict[tuple[str, str], list[dict] | None] = {}
    for key, region, group, query, start, end in jobs:
        try:
            response = client(region).start_query(
                logGroupName=group,
                startTime=int(start.timestamp()),
                endTime=int(end.timestamp()),
                queryString=query,
                limit=100,
            )
            started[key] = (region, response["queryId"])
        except Exception as exc:  # noqa: BLE001 - one group that cannot be read is reported as such
            print(f"ops_firewall: start failed error={type(exc).__name__}")
            results[key] = None
    deadline = time.monotonic() + wait_seconds
    while started and time.monotonic() < deadline:
        for key, (region, query_id) in list(started.items()):
            try:
                response = client(region).get_query_results(queryId=query_id)
            except Exception as exc:  # noqa: BLE001
                print(f"ops_firewall: results failed error={type(exc).__name__}")
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
    for key in started:
        results[key] = None  # still running: what finished is reported, and this is said
    return results


def _count(value) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _rule_label(rule: str) -> str:
    """A rule's id as words, letters and digits only: rule ids are ours (Terraform names them),
    but nothing from a log line reaches speech without being reduced like this."""
    return re.sub(r"[^A-Za-z0-9]+", " ", str(rule or "")).strip()[:60] or "default action"


def firewall_review(
    hours: int = FIREWALL_DEFAULT_HOURS,
    *,
    now: datetime | None = None,
    run: Callable[[list], dict] | None = None,
) -> dict:
    """What the firewall allowed, blocked and counted in the last `hours` (1 to 72), per log group:
    blocks by rule, the most-blocked paths, and whether blocks are unusual against the last seven
    days. Counts only; paths are marked untrusted."""
    pairs = log_groups() if account_wide_data() else None
    if pairs is None:
        return {"spoken": NOT_AVAILABLE, "findings": [], "groups": [], "available": False}
    now = (now or datetime.now(UTC)).astimezone(UTC)
    hours = _clamp_hours(hours)
    window_start = now - timedelta(hours=hours)
    baseline_start = window_start - timedelta(days=BASELINE_DAYS)

    jobs = []
    for region, group in pairs:
        jobs += [
            ((group, "actions"), region, group, QUERY_ACTIONS, window_start, now),
            ((group, "rules"), region, group, QUERY_RULES, window_start, now),
            ((group, "paths"), region, group, QUERY_PATHS, window_start, now),
            ((group, "baseline"), region, group, QUERY_BASELINE, baseline_start, window_start),
        ]
    results = (run or _run_queries)(jobs)

    groups, findings, incomplete = [], [], False
    total = {"ALLOW": 0, "BLOCK": 0, "COUNT": 0}
    for region, group in pairs:
        actions = results.get((group, "actions"))
        rules = results.get((group, "rules"))
        paths = results.get((group, "paths"))
        baseline = results.get((group, "baseline"))
        incomplete = incomplete or any(part is None for part in (actions, rules, paths, baseline))
        by_action = {"ALLOW": 0, "BLOCK": 0, "COUNT": 0}
        for row in actions or []:
            action = str(row.get("action") or "").upper()
            if action in by_action:
                by_action[action] += _count(row.get("requests"))
        for action, count in by_action.items():
            total[action] += count
        daily = [_count(row.get("blocks")) for row in baseline or []]
        # Days with no blocks have no row; the median is over the whole week.
        daily += [0] * max(0, BASELINE_DAYS - len(daily))
        typical = median(daily) * hours / 24 if baseline is not None else None
        label = _group_label(group)
        row = {
            "log_group": group,
            "label": label,
            "region": region,
            "allowed": by_action["ALLOW"],
            "blocked": by_action["BLOCK"],
            "counted": by_action["COUNT"],
            "typical_blocked": round(typical, 1) if typical is not None else None,
            "rules": [
                {"rule": _rule_label(rule.get("terminatingRuleId")), "blocks": _count(rule.get("blocks"))}
                for rule in rules or []
            ],
            "untrusted": {
                "paths": [
                    {
                        "path": untrusted_text(path.get("httpRequest.uri"), PATH_MAX_CHARS),
                        "blocks": _count(path.get("blocks")),
                    }
                    for path in paths or []
                ]
            },
        }
        groups.append(row)
        blocked = by_action["BLOCK"]
        if typical is not None and blocked >= SPIKE_MIN_BLOCKS and blocked > SPIKE_TIMES * typical:
            findings.append(
                finding(
                    "firewall_spike",
                    f"The {label} firewall blocked {by_action['BLOCK']} requests in {hours} hours, "
                    f"against about {round(typical)} in a typical {hours} hours",
                    group,
                    log_group=group,
                )
            )

    return {
        "spoken": _spoken(total, hours, findings, incomplete),
        "findings": findings,
        "groups": groups,
        "hours": hours,
        "complete": not incomplete,
        "available": True,
        "as_of": now.isoformat(),
    }


_GROUP_LABELS = {"admin": "admin API", "public-api": "public API"}


def _group_label(group: str) -> str:
    """A log group as words: "CloudFront" for the shared one, else what follows the environment
    in its name ("admin API", "public API")."""
    if group == SHARED_LOG_GROUP:
        return "CloudFront"
    rest = group.removeprefix(f"aws-waf-logs-{NAME_PREFIX}-{os.environ.get(ENVIRONMENT_ENV, '')}-")
    return _GROUP_LABELS.get(rest, rest.replace("-", " "))


def _spoken(total: dict, hours: int, findings: list, incomplete: bool) -> str:
    seen = sum(total.values())
    if not seen and not incomplete:
        return f"The firewall logged no requests in the last {hours} hours."
    words = (
        f"In the last {hours} hours the firewall allowed {total['ALLOW']} requests and blocked "
        f"{total['BLOCK']}."
    )
    if findings:
        words += f" Blocks are unusually high on {len(findings)} of them; the detail is on screen."
    else:
        words += " That's within the usual range."
    if incomplete:
        words += " Some of the logs didn't answer in time, so the numbers may be low."
    return words
