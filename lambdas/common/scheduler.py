"""EventBridge Scheduler helpers for per-topic dynamic scheduling (Phase 3).

Topics are created/updated/deleted at runtime through the Admin API rather
than declared in Terraform, so there is no fixed list of topics Terraform
could create one schedule per. Instead, `admin_api_handler` calls into this
module whenever a topic is written or removed, and this module manages the
two EventBridge Scheduler schedules (hourly research tick, daily authoring
cycle) that keep that topic running unattended.

The expressions written here are the topic's *effective* ones: the Admin API
applies the topic's stagger slot first (common/stagger.py), so the minute each
schedule runs at is the slot's, not the cadence's. A paused topic keeps both
schedules, DISABLED (`set_topic_schedules_state`), so resuming it is a state
change and not a rebuild; the handlers also refuse to run a paused topic, so
the Topics row is the source of truth either way.

The two Lambda/Step-Functions targets, the IAM role EventBridge Scheduler
assumes to invoke them, and the environment name used for schedule naming
all come from environment variables set on the Lambda by `infra/`:
`RESEARCH_TICK_FUNCTION_ARN`, `STATE_MACHINE_ARN`, `SCHEDULER_INVOKE_ROLE_ARN`,
`ENVIRONMENT_NAME`. Never hardcoded here.
"""

from __future__ import annotations

import json
import os
import re

import boto3

from .naming import environment_prefix

_scheduler_client = None

_VALID_EXPRESSION_PREFIXES = ("rate(", "cron(", "at(")

_SCHEDULE_GROUP_NAME = "default"

# An IANA zone name ("UTC", "Australia/Sydney", "America/Argentina/Buenos_Aires").
# EventBridge Scheduler does the real check; this only rejects obvious junk so
# a typo fails the admin request rather than the AWS call.
_TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")

DEFAULT_TIMEZONE = "UTC"


def _get_scheduler_client():
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = boto3.client("scheduler")
    return _scheduler_client


def _validate_schedule_expression(expr: str) -> None:
    """Minimally validate an EventBridge Scheduler expression.

    EventBridge Scheduler accepts exactly three expression forms:
    `rate(...)`, `cron(...)`, and `at(...)`. Raises ValueError if `expr`
    doesn't start with one of those.
    """
    if not isinstance(expr, str) or not expr.startswith(_VALID_EXPRESSION_PREFIXES):
        raise ValueError(
            f"schedule expression {expr!r} must start with 'rate(', 'cron(', or 'at('"
        )


def validate_timezone(tz: str) -> None:
    """Raise ValueError unless `tz` looks like an IANA time-zone name."""
    if not isinstance(tz, str) or not _TIMEZONE_RE.match(tz):
        raise ValueError(f"timezone {tz!r} must be an IANA zone name such as 'Australia/Sydney'")


def _schedule_name(topic_id: str, suffix: str) -> str:
    """<prefix>-<env>-<topic_id>-<suffix>, e.g. "bloggerbear-dev-hacker-news-research-tick". The
    prefix is this deployment's (common/naming.py). EventBridge Scheduler allows 64 characters,
    which is what bounds how long a prefix Terraform accepts (var.unique_name_prefix)."""
    return f"{environment_prefix(os.environ['ENVIRONMENT_NAME'])}{topic_id}-{suffix}"


def topic_schedule_names(topic_id: str) -> list[str]:
    """The names of a topic's two schedules, research tick first: what delete_topic_schedules
    deletes, for an operator to be told before it happens."""
    return [_schedule_name(topic_id, suffix) for suffix in ("research-tick", "daily-cycle")]


def _schedule_state(enabled: bool) -> str:
    return "ENABLED" if enabled else "DISABLED"


def _upsert_schedule(
    client,
    *,
    name: str,
    schedule_expression: str,
    target_arn: str,
    topic_id: str,
    timezone: str = DEFAULT_TIMEZONE,
    enabled: bool = True,
) -> None:
    kwargs = {
        "Name": name,
        "GroupName": _SCHEDULE_GROUP_NAME,
        "ScheduleExpression": schedule_expression,
        "ScheduleExpressionTimezone": timezone,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": _schedule_state(enabled),
        "Target": {
            "Arn": target_arn,
            "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
            "Input": json.dumps({"topic_id": topic_id}),
        },
    }
    try:
        client.create_schedule(**kwargs)
    except client.exceptions.ConflictException:
        client.update_schedule(**kwargs)


def upsert_topic_schedules(
    topic_id: str,
    research_cadence: str,
    daily_cadence: str,
    daily_timezone: str = DEFAULT_TIMEZONE,
    *,
    enabled: bool = True,
) -> None:
    """Create (or update, if they already exist) a topic's two schedules.

    `daily_timezone` is the zone the daily cadence's `cron(...)` is read in
    (so "9 AM Australia/Sydney" follows daylight saving); the research
    schedule is a `rate(...)`, or the cron its stagger slot turned it into,
    and is read in UTC.

    `enabled` is False for a paused topic: both schedules are written
    DISABLED, so an edit to a paused topic does not quietly start it again.

    Validates both cadence expressions and the zone before making any AWS
    calls, so a bad value never leaves one schedule created and the other not.
    """
    _validate_schedule_expression(research_cadence)
    _validate_schedule_expression(daily_cadence)
    validate_timezone(daily_timezone)

    client = _get_scheduler_client()
    _upsert_schedule(
        client,
        name=_schedule_name(topic_id, "research-tick"),
        schedule_expression=research_cadence,
        target_arn=os.environ["RESEARCH_TICK_FUNCTION_ARN"],
        topic_id=topic_id,
        enabled=enabled,
    )
    _upsert_schedule(
        client,
        name=_schedule_name(topic_id, "daily-cycle"),
        schedule_expression=daily_cadence,
        target_arn=os.environ["STATE_MACHINE_ARN"],
        topic_id=topic_id,
        timezone=daily_timezone,
        enabled=enabled,
    )


def set_topic_schedules_state(topic_id: str, *, enabled: bool) -> list[str]:
    """Pause (DISABLED) or resume (ENABLED) a topic's two schedules as they are, without
    rewriting what they run or when. Returns the names of the schedules changed.

    EventBridge Scheduler has no "set the state" call: an update must carry the whole
    schedule again, so each is read first and written back with only its state changed.
    A schedule that does not exist (a topic made before its schedules could be, or one
    whose creation failed) is skipped: the Topics row's `paused` flag is what the handlers
    obey, and the schedules follow it when they next exist.
    """
    client = _get_scheduler_client()
    changed = []
    for suffix in ("research-tick", "daily-cycle"):
        name = _schedule_name(topic_id, suffix)
        try:
            current = client.get_schedule(Name=name, GroupName=_SCHEDULE_GROUP_NAME)
        except client.exceptions.ResourceNotFoundException:
            continue
        target = current["Target"]
        kwargs = {
            "Name": name,
            "GroupName": _SCHEDULE_GROUP_NAME,
            "ScheduleExpression": current["ScheduleExpression"],
            "FlexibleTimeWindow": current.get("FlexibleTimeWindow") or {"Mode": "OFF"},
            "State": _schedule_state(enabled),
            "Target": {
                "Arn": target["Arn"],
                "RoleArn": target["RoleArn"],
                **({"Input": target["Input"]} if target.get("Input") else {}),
            },
        }
        if current.get("ScheduleExpressionTimezone"):
            kwargs["ScheduleExpressionTimezone"] = current["ScheduleExpressionTimezone"]
        client.update_schedule(**kwargs)
        changed.append(name)
    return changed


def delete_topic_schedules(topic_id: str) -> None:
    """Delete a topic's two schedules, ignoring ones that don't exist.

    A topic created before this migration (or one whose schedules failed to
    create for some other reason) may not have schedules yet -- deleting it
    should not error.
    """
    client = _get_scheduler_client()
    for suffix in ("research-tick", "daily-cycle"):
        name = _schedule_name(topic_id, suffix)
        try:
            client.delete_schedule(Name=name, GroupName=_SCHEDULE_GROUP_NAME)
        except client.exceptions.ResourceNotFoundException:
            pass
