"""EventBridge Scheduler helpers for per-topic dynamic scheduling (Phase 3).

Topics are created/updated/deleted at runtime through the Admin API rather
than declared in Terraform, so there is no fixed list of topics Terraform
could create one schedule per. Instead, `admin_api_handler` calls into this
module whenever a topic is written or removed, and this module manages the
two EventBridge Scheduler schedules (hourly research tick, daily authoring
cycle) that keep that topic running unattended.

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
    return f"bloggerbear-{os.environ['ENVIRONMENT_NAME']}-{topic_id}-{suffix}"


def _upsert_schedule(
    client,
    *,
    name: str,
    schedule_expression: str,
    target_arn: str,
    topic_id: str,
    timezone: str = DEFAULT_TIMEZONE,
) -> None:
    kwargs = {
        "Name": name,
        "GroupName": _SCHEDULE_GROUP_NAME,
        "ScheduleExpression": schedule_expression,
        "ScheduleExpressionTimezone": timezone,
        "FlexibleTimeWindow": {"Mode": "OFF"},
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
) -> None:
    """Create (or update, if they already exist) a topic's two schedules.

    `daily_timezone` is the zone the daily cadence's `cron(...)` is read in
    (so "9 AM Australia/Sydney" follows daylight saving); the hourly research
    schedule is a `rate(...)` and has no wall-clock time to interpret.

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
    )
    _upsert_schedule(
        client,
        name=_schedule_name(topic_id, "daily-cycle"),
        schedule_expression=daily_cadence,
        target_arn=os.environ["STATE_MACHINE_ARN"],
        topic_id=topic_id,
        timezone=daily_timezone,
    )


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
