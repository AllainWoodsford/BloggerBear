"""security_events, alarms and spend: what the account says, beside the pipeline's own state. The
same rules as tools.py: read-only, and what is said aloud is written here.

**Nothing an attacker wrote is spoken, and nothing that identifies a client.** An incident row
holds a request path and matched text the blocked client wrote, and a hash of its address. What is
said, and what a finding says, is built from the row's category, severity and source, each checked
against the fixed lists in common/security_events.py, and from counts and ages. The path and the
matched text go under `untrusted` for the page; the client hash and the country are not returned
at all. Next steps come from the playbook in code, not from the row.

**None of these has a command.** Nothing in admin_cli closes an incident, quiets an alarm or cuts
a bill, so their findings say what to look at (suggestions.py).

**One environment each.** Dev and production share one AWS account, and two things here are the
account's, not an environment's: CloudWatch lists every alarm in it, and the Stats row holds the
whole account's bill. So `alarms` asks only for this environment's alarms, by name, and refuses
when it has not been told which environment it is for; and `spend` leaves the account's bill out
unless the deployment says this assistant may report it (production's will; dev's does not).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

import boto3

from common.costing import USD_TO_AUD_RATE
from common.dynamo import get_current_stats, list_security_incidents, list_stats_history_weeks
from common.naming import NAME_PREFIX
from common.security_events import (
    ADMIN_API_ACCESS,
    COMMENT_SCREENING,
    HIGH,
    LOW,
    MANUAL,
    MEDIUM,
    PLAYBOOK,
    SIGN_IN,
    SUMMARY_MAX_CHARS,
    TRENDS,
    WAF_ADMIN_API,
    WAF_OTHER,
    WAF_PUBLIC_API,
    untrusted_text,
)
from common.stats_tracking import (
    ARTICLES_CATEGORY,
    AWS_BILL_AS_OF,
    AWS_BILL_WEEK_COMPLETE,
    AWS_BILL_WEEK_USD,
    BEDROCK_CATEGORIES,
)
from ops_mcp.suggestions import ID_PATTERN, finding
from ops_mcp.tools import _age, _clamp_days, _join, _now, _parse

# --- security_events -----------------------------------------------------------------------------

SECURITY_DEFAULT_DAYS = 7
SECURITY_MAX_DAYS = 30
# One query, newest first. More open incidents than this and the answer says "at least".
SECURITY_MAX_INCIDENTS = 200
SECURITY_SPOKEN_LINES = 3

_SEVERITIES = (HIGH, MEDIUM, LOW)
_CATEGORY_SPOKEN = {
    "xss": "cross-site scripting attempts",
    "oversized-request": "oversized requests",
    "bad-bot": "requests from bad bots",
    "file-inclusion": "file inclusion attempts",
    "ssrf": "server-side request forgery attempts",
    "sqli": "SQL injection attempts",
    "rce": "remote code execution attempts",
    "rate-limit": "rate-limit blocks",
    "feedback-flood": "a flood of feedback",
    "admin-denied": "requests from outside the allowlist",
    "prompt-injection": "comments that tried to instruct the model",
    "comment-attack": "comments shaped like an attack",
    "sign-in-failures": "repeated failed sign-ins",
    "sign-in-lockout": "a user locked out after failed sign-ins",
    "feedback-drops": "many dropped feedback comments in a day",
    "admin-api-errors": "many errors in an hour",
    "manual-report": "something reported by hand",
    "other": "blocks by a rule I don't recognise",
}
_SOURCE_SPOKEN = {
    WAF_PUBLIC_API: "on the public API",
    WAF_ADMIN_API: "on the admin API",
    WAF_OTHER: "at the firewall",
    COMMENT_SCREENING: "in comment screening",
    SIGN_IN: "at the assistant's sign-in",
    ADMIN_API_ACCESS: "from the admin API",
    MANUAL: "by an operator",
}


def _incident_row(incident: dict, now: datetime) -> dict:
    """An incident as the tool returns it. Every field outside `untrusted` is one of a fixed list,
    a number or a timestamp we parsed: nothing is passed through as it was stored."""
    category = incident.get("category") if incident.get("category") in PLAYBOOK else "other"
    severity = incident.get("severity")
    if severity not in _SEVERITIES:
        severity = PLAYBOOK[category][0]
    source = incident.get("source") if incident.get("source") in _SOURCE_SPOKEN else WAF_OTHER
    event_id = incident.get("event_id")
    first, last = _parse(incident.get("first_seen")), _parse(incident.get("last_seen"))
    try:
        requests = int(incident.get("request_count") or 0)
    except (TypeError, ValueError):
        requests = 0
    written = incident.get("untrusted") if isinstance(incident.get("untrusted"), dict) else {}
    untrusted = {
        "path": untrusted_text(written.get("path")),
        "matched": untrusted_text(written.get("matched")),
    }
    if source == MANUAL:
        # What the operator wrote when they opened it. Their own words, but still text from a
        # table: for the page, never for speech, like the rest of `untrusted`.
        untrusted["summary"] = untrusted_text(incident.get("summary"), SUMMARY_MAX_CHARS)
    return {
        "event_id": event_id if isinstance(event_id, str) and ID_PATTERN.match(event_id) else None,
        "category": category,
        "severity": severity,
        "source": source,
        "requests": requests,
        "first_seen": first.isoformat() if first else None,
        "last_seen": last.isoformat() if last else None,
        "last_seen_ago": _age(last, now),
        "next_steps": PLAYBOOK[category][2],
        # The blocked client wrote these: for the page, never for speech.
        "untrusted": untrusted,
    }


# Categories that are a finding from medium up, not only at high. A medium incident about one
# client (a scanner at the admin API, a scraper over the rate limit) is routine and only counted;
# a trend that has passed its second threshold, or something an operator reported, is worth a card.
_FINDING_FROM_MEDIUM = frozenset({*TRENDS, "manual-report"})


def _is_finding(row: dict) -> bool:
    if row["severity"] == HIGH:
        return True
    return row["severity"] == MEDIUM and row["category"] in _FINDING_FROM_MEDIUM


def _incident_words(row: dict) -> str:
    return f"{_CATEGORY_SPOKEN[row['category']]} {_SOURCE_SPOKEN[row['source']]}"


def security_events(days: int = SECURITY_DEFAULT_DAYS, *, now: datetime | None = None) -> dict:
    """The open security incidents last seen in the last `days` (1 to 30): how many at each
    severity, and for each its category, request count and when it was first and last seen."""
    now = _now(now)
    days = _clamp_days(days, SECURITY_MAX_DAYS)
    since = now - timedelta(days=days)
    incidents = list_security_incidents("open", SECURITY_MAX_INCIDENTS)  # newest first
    more = len(incidents) >= SECURITY_MAX_INCIDENTS

    rows = [
        _incident_row(incident, now)
        for incident in incidents
        if (_parse(incident.get("last_seen")) or now) >= since
    ]
    rows.sort(key=lambda row: _SEVERITIES.index(row["severity"]))  # stable: newest first within each
    counts = {severity: sum(row["severity"] == severity for row in rows) for severity in _SEVERITIES}

    findings = [
        finding(
            "security_incident",
            f"A {row['severity']}-severity security incident is open: {_incident_words(row)}",
            row["event_id"],
            category=row["category"],
            source=row["source"],
            # Only for one opened by hand: its summary, marked as text nobody here wrote.
            untrusted={"summary": row["untrusted"]["summary"]} if row["untrusted"].get("summary") else None,
        )
        for row in rows
        if _is_finding(row)
    ]
    return {
        "spoken": _security_spoken(rows, counts, days, more),
        "findings": findings,
        "days": days,
        "open": len(rows),
        "more": more,
        "by_severity": counts,
        "incidents": rows,
        "as_of": now.isoformat(),
    }


def _security_spoken(rows: list[dict], counts: dict[str, int], days: int, more: bool) -> str:
    window = f"the last {days} days" if days != 1 else "the last day"
    if not rows:
        return f"No open security incidents in {window}."
    count = len(rows)
    split = _join([f"{counts[severity]} {severity}" for severity in _SEVERITIES if counts[severity]])
    opening = (
        f"{'At least ' if more else ''}{count} open security incident{'s' if count != 1 else ''} "
        f"in {window}: {split}."
    )
    high = [row for row in rows if row["severity"] == HIGH]
    if not high:
        return f"{opening} None is high severity."
    lines = [
        f"High: {_incident_words(row)}, {row['requests']:,} request{'s' if row['requests'] != 1 else ''}, "
        f"last seen {row['last_seen_ago']} ago."
        for row in high[:SECURITY_SPOKEN_LINES]
    ]
    rest = len(high) - len(lines)
    return f"{opening} {' '.join(lines)}{f' And {rest} more high.' if rest > 0 else ''}"


# --- alarms --------------------------------------------------------------------------------------

# What every alarm in this deployment starts with: its name prefix (common/naming.py, from the
# NAME_PREFIX variable Terraform sets on the function), "bloggerbear-" in the original deployment.
ALARM_PREFIX = f"{NAME_PREFIX}-"
# Which environment this assistant is for: "dev" or "production". The module sets it on the
# function (infra/modules/ops-assistant/main.tf), and common/scheduler.py reads the same variable.
ENVIRONMENT_ENV = "ENVIRONMENT_NAME"
# What an environment's name may look like. It becomes part of the prefix CloudWatch is asked for,
# so it is held to what the alarms' own names are built from: infra/modules/observability names
# every alarm "<prefix>-${var.environment_name}-<what>", and the ops-assistant module refuses
# an environment name that is not a lowercase letter followed by lowercase letters and digits.
# No hyphen on purpose: with one, an environment called "dev-old" would have its alarms answer to
# dev's prefix, "bloggerbear-dev-".
_ENVIRONMENT_NAME = re.compile(r"[a-z][a-z0-9]{1,31}")
ALARMS_NOT_CONFIGURED = (
    "I can't read the alarms: this assistant has not been told which environment it is for."
)
ALARM_SPOKEN_LINES = 5
ALARM_LABEL_MAX_CHARS = 80

_cloudwatch_client = None
_NOT_A_WORD = re.compile(r"[^A-Za-z0-9]+")


def _get_cloudwatch_client():
    """Lazy, like common/static_pages.py's clients. No region is named: boto3 takes it from the
    environment, which on Lambda is the function's own."""
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = boto3.client("cloudwatch")
    return _cloudwatch_client


def _alarm_label(name: str) -> str:
    """An alarm's name as words: "bloggerbear-prod-pipeline-dlq-messages" -> "prod pipeline dlq
    messages". Letters and digits only, so a name can carry nothing else into speech."""
    return _NOT_A_WORD.sub(" ", name[len(ALARM_PREFIX) :]).strip()[:ALARM_LABEL_MAX_CHARS] or "unnamed"


def alarm_prefix() -> str | None:
    """What every one of this environment's alarms starts with, "bloggerbear-dev-" in dev, or None
    when the function has not been told its environment (or was told something that is not a
    name). None is never widened to the bare prefix, "bloggerbear-": that is both environments'."""
    name = os.environ.get(ENVIRONMENT_ENV, "")
    return f"{ALARM_PREFIX}{name}-" if _ENVIRONMENT_NAME.fullmatch(name) else None


def alarms(*, now: datetime | None = None) -> dict:
    """This environment's CloudWatch alarms that are in ALARM right now (the ones whose names
    start with "<prefix>-<environment>-"), and since when each has been.

    The role cannot be held to one environment's alarms: a DescribeAlarms that lists by prefix is
    authorized against every alarm in the account. So the separation is here: the other
    environment's alarms are never asked for, and one that came back anyway would be dropped."""
    now = _now(now)
    prefix = alarm_prefix()
    if prefix is None:
        return {
            "spoken": ALARMS_NOT_CONFIGURED,
            "findings": [],
            "alarms": [],
            "available": False,
            "as_of": now.isoformat(),
        }
    pages = (
        _get_cloudwatch_client()
        .get_paginator("describe_alarms")
        .paginate(
            AlarmNamePrefix=prefix, StateValue="ALARM", AlarmTypes=["MetricAlarm", "CompositeAlarm"]
        )
    )
    firing = []
    for page in pages:
        for alarm in [*page.get("MetricAlarms", []), *page.get("CompositeAlarms", [])]:
            name = str(alarm.get("AlarmName") or "")
            # Asked for above; checked again here, so the answer never depends on the filter alone.
            if name.startswith(prefix) and alarm.get("StateValue") == "ALARM":
                firing.append(alarm)

    rows, findings = [], []
    for alarm in sorted(firing, key=lambda alarm: alarm["AlarmName"]):
        name = untrusted_text(alarm["AlarmName"], 255)
        label = _alarm_label(name)
        since = alarm.get("StateUpdatedTimestamp")
        since = since if isinstance(since, datetime) else None
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=now.tzinfo)
        lasted = _age(since, now)
        rows.append(
            {
                "name": name,
                "label": label,
                "since": since.isoformat() if since else None,
                "for": lasted,
                "description": untrusted_text(alarm.get("AlarmDescription")),
            }
        )
        findings.append(
            finding("alarm_firing", f"The {label} alarm has been firing for {lasted}", name, alarm=name)
        )

    return {
        "spoken": _alarms_spoken(rows),
        "findings": findings,
        "alarms": rows,
        "available": True,
        "as_of": now.isoformat(),
    }


def _alarms_spoken(rows: list[dict]) -> str:
    if not rows:
        return "No alarms are firing."
    count = len(rows)
    named = [f"{row['label']}, for {row['for']}" for row in rows[:ALARM_SPOKEN_LINES]]
    rest = count - len(named)
    more = f" And {rest} more." if rest > 0 else ""
    return f"{count} alarm{'s are' if count != 1 else ' is'} firing: {'; '.join(named)}.{more}"


# --- spend ---------------------------------------------------------------------------------------

SPEND_PERIODS = ("week", "month")
# A typical week is the median of up to this many of the last complete weeks.
SPEND_TYPICAL_WEEKS = 8
# "month" is this week so far and the three complete weeks before it: the Stats rows are weekly,
# so four weeks is the month they can give without splitting one.
SPEND_MONTH_WEEKS = 4
SPEND_UNUSUAL_TIMES = 2

_AI_CATEGORIES = (*BEDROCK_CATEGORIES, ARTICLES_CATEGORY)
_SPEND_SPOKEN = {"ai": "AI spend", "aws": "The whole AWS bill"}

# Whether this assistant may report things that are the whole account's and not its environment's.
# The module sets it from its account_wide_data variable (off by default; production will switch
# it on). Only the exact word "true" switches it on: unset, empty or anything else is off.
ACCOUNT_WIDE_ENV = "OPS_ACCOUNT_WIDE_DATA"
BILL_NOT_AVAILABLE = "The whole AWS bill is not available from this environment."


def account_wide_data() -> bool:
    """True where the deployment says this assistant may report the whole account's figures. The
    AWS bill is the one such figure today: Cost Explorer bills the account, so it covers
    production and dev together and cannot be split between them."""
    return os.environ.get(ACCOUNT_WIDE_ENV, "") == "true"


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _ai_aud(row: dict) -> float:
    """What a week's model calls cost, in AUD: every category the pipeline tracks, articles
    included (common/stats_tracking.py)."""
    return float(
        sum((_decimal(row.get(f"{category}_cost_aud") or 0) for category in _AI_CATEGORIES), Decimal("0"))
    )


def _aws_aud(row: dict) -> float | None:
    """A week's whole AWS bill in AUD, or None if Cost Explorer has not been read for it. The
    table holds it in USD per service; the rate is the fixed one the Stats page uses."""
    bill = row.get(AWS_BILL_WEEK_USD)
    if not isinstance(bill, dict):
        return None
    usd = sum((_decimal(value) for value in bill.values()), Decimal("0"))
    return float(usd * Decimal(str(USD_TO_AUD_RATE)))


def _typical(values: list[float]) -> float | None:
    return round(median(values), 2) if values else None


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def spend(period: str = "week", *, now: datetime | None = None) -> dict:
    """AI spend (the model calls this environment's pipeline made and counted itself) and, where
    the deployment allows it, the whole AWS bill, in AUD, for this `week` so far or the `month`
    (the last four weeks), and this week against a typical week: the median of the last complete
    weeks.

    The bill is the whole account's, both environments together. Without account_wide_data() it
    is not reported, not compared and not a finding: `aws` is None, and the spoken text says the
    bill is not available from this environment."""
    now = _now(now)
    if period not in SPEND_PERIODS:
        return {"spoken": "I can report spend for a week or a month.", "findings": [], "period": None}

    current = get_current_stats()
    this_week = current.get("week_start") or (now.date() - timedelta(days=now.weekday())).isoformat()
    # Every history row is a week that has ended. Newest first.
    weeks = sorted(
        (row for row in list_stats_history_weeks() if row["week_start"] < this_week),
        key=lambda row: row["week_start"],
        reverse=True,
    )
    compared = weeks[:SPEND_TYPICAL_WEEKS]
    in_period = [current, *weeks[: SPEND_MONTH_WEEKS - 1]] if period == "month" else [current]

    ai = {
        "period": round(sum(_ai_aud(row) for row in in_period), 2),
        "this_week": round(_ai_aud(current), 2),
        "typical_week": _typical([_ai_aud(row) for row in compared]),
    }
    # A week's bill is only whole once the poll has filled it in after the week ended; the copy the
    # rollover made stops a day short, so a typical week is taken from the whole ones alone.
    bills = [bill for row in in_period if (bill := _aws_aud(row)) is not None]
    whole = [
        bill for row in compared if row.get(AWS_BILL_WEEK_COMPLETE) and (bill := _aws_aud(row)) is not None
    ]
    this_bill = _aws_aud(current)
    aws = {
        "period": round(sum(bills), 2) if bills else None,
        "this_week": round(this_bill, 2) if this_bill is not None else None,
        "typical_week": _typical(whole),
        "read_at": untrusted_text(current.get(AWS_BILL_AS_OF), 40) or None,
    }
    # The Stats row may hold the account's bill whatever this flag says (it is the same row the
    # Stats page reads). Dropped here, before anything is compared or said, so nothing below can
    # let it out.
    bill_available = account_wide_data()
    if not bill_available:
        aws = None

    findings = []
    for what, figures in (("ai", ai), ("aws", aws)):
        if figures is None:
            continue
        figures["unusual"] = _unusual(figures)
        if figures["unusual"]:
            noticed = (
                f"{_SPEND_SPOKEN[what]} this week is more than twice a typical week: "
                f"{_money(figures['this_week'])} against {_money(figures['typical_week'])}"
            )
            findings.append(finding("spend_unusual", noticed, None, what=what))

    return {
        "spoken": _spend_spoken(period, ai, aws),
        "findings": findings,
        "period": period,
        "currency": "AUD",
        "week_start": this_week,
        "weeks_in_period": len(in_period),
        "weeks_compared": len(compared),
        "ai": ai,
        "aws": aws,
        "account_bill_available": bill_available,
        "as_of": now.isoformat(),
    }


def _unusual(figures: dict) -> bool:
    """More than twice a typical week, when there is a typical week and it is not nothing."""
    this_week, typical = figures["this_week"], figures["typical_week"]
    return bool(this_week is not None and typical and this_week > SPEND_UNUSUAL_TIMES * typical)


def _spend_spoken(period: str, ai: dict, aws: dict | None) -> str:
    """`aws` is None where the account's bill is not this assistant's to report: that is said, in
    place of the bill's sentences."""
    sentences = []
    if period == "month":
        known = aws is not None and aws["period"] is not None
        bill = f" and the whole AWS bill is {_money(aws['period'])}" if known else ""
        sentences.append(f"Over the last four weeks, AI spend is {_money(ai['period'])}{bill}.")
    for what, figures in (("ai", ai), ("aws", aws)):
        name = _SPEND_SPOKEN[what]
        if figures is None:
            sentences.append(BILL_NOT_AVAILABLE)
            continue
        if figures["this_week"] is None:
            sentences.append(f"{name} has not been read yet this week.")
            continue
        sentence = f"{name} this week is {_money(figures['this_week'])} so far"
        if figures["typical_week"] is None:
            sentences.append(f"{sentence}, with no complete week to compare it with yet.")
        else:
            sentences.append(f"{sentence}; a typical week is {_money(figures['typical_week'])}.")
            if figures["unusual"]:
                sentences.append("That is more than twice a typical week.")
    sentences.append("Amounts are in Australian dollars.")
    return " ".join(sentences)
