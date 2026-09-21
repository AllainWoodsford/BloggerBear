"""How often a topic's research tick actually does its work.

EventBridge Scheduler only learns a schedule when the Admin API writes it, so
editing a cadence in DynamoDB alone changes nothing. Instead the per-topic
`research_cadence` schedule stays a fixed *heartbeat* (hourly by default) and
each tick asks whether it is *due*: enough time since the topic was last
checked. The interval lives in DynamoDB, so it can be changed there -- per
topic, or one global default -- and takes effect on the next heartbeat.

Precedence: the topic's own `research_interval_hours`, else the `pipeline`
config row's, else DEFAULT_RESEARCH_INTERVAL_HOURS. Intervals are whole hours
because the heartbeat is hourly; an interval shorter than the heartbeat simply
runs at the heartbeat.

Topic-agnostic: nothing here knows about any adapter or domain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

DEFAULT_RESEARCH_INTERVAL_HOURS = 1
MAX_RESEARCH_INTERVAL_HOURS = 168  # a week

# Scheduler jitter and a tick's own runtime make the gap between two heartbeats a
# little under (or over) the nominal hour. Without slack, a 2-hour interval could
# see 1h59m59s between ticks and skip a whole heartbeat.
DUE_TOLERANCE = timedelta(minutes=5)


def _as_hours(value) -> int | None:
    """`value` as a whole number of hours in range, else None. Accepts the
    Decimal DynamoDB hands back; rejects bools, floats, strings and out-of-range."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    if value != int(value):
        return None
    hours = int(value)
    if 1 <= hours <= MAX_RESEARCH_INTERVAL_HOURS:
        return hours
    return None


def interval_error(value) -> str | None:
    """A message if `value` isn't a valid stored interval, else None. None (unset)
    is valid and means "inherit"."""
    if value is None or _as_hours(value) is not None:
        return None
    return f"must be a whole number of hours from 1 to {MAX_RESEARCH_INTERVAL_HOURS}, or null to inherit"


def resolve_interval_hours(topic: dict, pipeline_config: dict | None) -> int:
    """The interval in force for a topic. A stored value that is no longer valid is
    skipped (falling through to the next level), never trusted."""
    for source in (topic, pipeline_config or {}):
        hours = _as_hours(source.get("research_interval_hours"))
        if hours is not None:
            return hours
    return DEFAULT_RESEARCH_INTERVAL_HOURS


def _parse(timestamp) -> datetime | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)  # stored in UTC


def next_due_at(last_research_at, interval_hours: int) -> datetime | None:
    """When the next check is due, or None if the topic has never been checked."""
    last = _parse(last_research_at)
    return None if last is None else last + timedelta(hours=interval_hours) - DUE_TOLERANCE


def is_due(now: datetime, last_research_at, interval_hours: int) -> bool:
    """A topic never checked (or with an unreadable timestamp) is always due."""
    due_at = next_due_at(last_research_at, interval_hours)
    return due_at is None or now >= due_at
