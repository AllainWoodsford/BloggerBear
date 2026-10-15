"""Staggering a topic's two schedules, so no two topics call their sources at the same minute.

Every topic has a *slot* (`stagger_slot`: 1, 2, 3, ...) on its Topics row, and the pipeline has
one setting, `stagger_minutes` (the `pipeline` config row; DEFAULT_STAGGER_MINUTES when unset,
0 turns staggering off). From those, and the number of topics, a topic's two EventBridge
Scheduler schedules are written with a minute of their own:

- the **research heartbeat** of the topic in slot k runs at `k * stagger_minutes` past the hour
  it would run anyway: with 5 minutes, slot 1 at :05, slot 2 at :10, slot 3 at :15, so a topic
  whose cadence is "every 4 hours" runs at 00:05, 04:05, 08:05 ... and the next topic at 00:10,
  04:10, 08:10 ...;
- the **daily cycle** (candidate ideas, the draft, the reviews) of every topic is first delayed
  by `topics * stagger_minutes`, so it starts after the last research heartbeat of its hour has
  had its turn, and then staggered the same way: with 4 topics and 5 minutes, 9:00 becomes 9:20
  for slot 1, 9:25 for slot 2, 9:30 for slot 3 and 9:35 for slot 4.

The stagger decides the **minute**; the topic's own cadence still decides everything else (the
hour pattern, the day, the time zone the daily cron is read in). A `rate(N hours)` heartbeat is
written as the cron it amounts to (`cron(5 */4 * * ? *)`), because a rate has no minute to set.
A cadence whose minute field is not a single number (`*/15`, `0,30`) cannot be staggered and is
written as it is.

What was actually written is recorded on the Topics row (`research_schedule`, `daily_schedule`)
so it can be read in DynamoDB and shown by the admin CLI (`topics schedule`), and the whole
set is rewritten by `topics reschedule`. The dispatcher design
(docs/enhancements/dispatcher-sqs-enhancement.md) reads the same slot as its spacing.

Topic-agnostic: nothing here knows about any adapter or domain.
"""

from __future__ import annotations

import re
from decimal import Decimal

DEFAULT_STAGGER_MINUTES = 5
MAX_STAGGER_MINUTES = 30
# 288 five-minute slots are a day; more topics than that is the dispatcher's job, not a cron's.
MAX_STAGGER_SLOT = 288

_MINUTES_IN_DAY = 24 * 60

_RATE = re.compile(r"^rate\(\s*(\d+)\s+(minutes?|hours?|days?)\s*\)$")
_CRON = re.compile(r"^cron\(\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*\)$")
_WHOLE = re.compile(r"^\d+$")
_STEP = re.compile(r"^(\*|\d+)/(\d+)$")
_RANGE = re.compile(r"^(\d+)-(\d+)$")


def _as_whole(value, low: int, high: int) -> int | None:
    """`value` as a whole number in [low, high], else None. Accepts the Decimal DynamoDB hands
    back; rejects bools, floats, strings and out-of-range."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    if value != int(value):
        return None
    number = int(value)
    return number if low <= number <= high else None


# --- the pipeline setting -----------------------------------------------------------------------


def stagger_error(value) -> str | None:
    """A message if `value` isn't a valid stored `stagger_minutes`, else None. None (unset) is
    valid and means the default; 0 turns staggering off."""
    if value is None or _as_whole(value, 0, MAX_STAGGER_MINUTES) is not None:
        return None
    return f"must be a whole number of minutes from 0 to {MAX_STAGGER_MINUTES}, or null for the default"


def resolve_stagger_minutes(pipeline_config: dict | None) -> int:
    """The minutes between one topic's turn and the next. A stored value that is no longer valid
    is skipped, never trusted."""
    minutes = _as_whole((pipeline_config or {}).get("stagger_minutes"), 0, MAX_STAGGER_MINUTES)
    return DEFAULT_STAGGER_MINUTES if minutes is None else minutes


# --- a topic's slot -------------------------------------------------------------------------------


def slot_error(value) -> str | None:
    """A message if `value` isn't a valid `stagger_slot`, else None. None (unset) is valid and
    means "give it the next free one"."""
    if value is None or _as_whole(value, 1, MAX_STAGGER_SLOT) is not None:
        return None
    return f"must be a whole number from 1 to {MAX_STAGGER_SLOT}, or null for the next free slot"


def topic_slot(topic: dict) -> int | None:
    """The topic's stored slot, or None when it has none (or one that is no longer valid)."""
    return _as_whole(topic.get("stagger_slot"), 1, MAX_STAGGER_SLOT)


def used_slots(topics, *, exclude: str | None = None) -> dict[int, str]:
    """slot -> topic_id for every topic with a valid slot, leaving `exclude` (a topic id) out."""
    used: dict[int, str] = {}
    for topic in topics:
        if topic.get("topic_id") == exclude:
            continue
        slot = topic_slot(topic)
        if slot is not None and slot not in used:
            used[slot] = topic.get("topic_id") or ""
    return used


def first_free_slot(used) -> int:
    """The smallest slot not in `used` (a collection of slots): 1 when none is, and a gap left
    by a deleted topic is reused before a new slot is opened."""
    taken = set(used)
    slot = 1
    while slot in taken:
        slot += 1
    return min(slot, MAX_STAGGER_SLOT)


def next_free_slot(topics, *, exclude: str | None = None) -> int:
    """The smallest slot no other topic has, leaving `exclude` (a topic id) out of the count."""
    return first_free_slot(used_slots(topics, exclude=exclude))


# --- offsets --------------------------------------------------------------------------------------


def research_offset_minutes(slot: int | None, stagger_minutes: int) -> int:
    """How many minutes past its hour the topic's research heartbeat runs: slot 1 at :05."""
    if slot is None or stagger_minutes <= 0:
        return 0
    return (slot * stagger_minutes) % _MINUTES_IN_DAY


def daily_offset_minutes(slot: int | None, stagger_minutes: int, topic_count: int) -> int:
    """How many minutes after its hour the topic's daily cycle runs: every topic's is delayed by
    `topic_count * stagger_minutes` (past the hour's research heartbeats), then slot 1 goes
    first, slot 2 one stagger later, and so on."""
    if slot is None or stagger_minutes <= 0:
        return 0
    return ((max(topic_count, 1) + slot - 1) * stagger_minutes) % _MINUTES_IN_DAY


def _shift_hours(field: str, carry: int) -> str:
    """The cron hour field moved `carry` hours later, for the shapes a topic's cadence uses:
    `*` (unchanged), `9`, `*/4`, `1/4`, `9,21`, `9-17`. Anything else is left as it is."""
    if carry == 0 or field == "*":
        return field
    if _WHOLE.match(field):
        return str((int(field) + carry) % 24)
    step = _STEP.match(field)
    if step:
        start, every = step.group(1), int(step.group(2))
        if every <= 0:
            return field
        first = 0 if start == "*" else int(start)
        return f"{(first + carry) % every}/{every}"
    span = _RANGE.match(field)
    if span:
        low, high = int(span.group(1)) + carry, int(span.group(2)) + carry
        return f"{low}-{high}" if high < 24 else field
    if all(_WHOLE.match(part) for part in field.split(",")):
        return ",".join(str((int(part) + carry) % 24) for part in field.split(","))
    return field


def staggered_expression(expression: str, offset_minutes: int) -> tuple[str, bool]:
    """`expression` (an EventBridge Scheduler `rate(...)` or `cron(...)`) with its minute set from
    `offset_minutes`: (the expression to write, whether the offset was applied).

    - `rate(N hours)` becomes the cron it amounts to, at that minute: `rate(1 hour)` at offset 5
      is `cron(5 * * * ? *)`, `rate(4 hours)` is `cron(5 */4 * * ? *)`. `rate(N days)` likewise,
      at midnight UTC. A rate in minutes is left alone: it has no hour to be a minute past.
    - `cron(M H ...)` with a single-number minute gets the offset's minute in place of M, and
      its hour moved on when the offset is an hour or more. Any other minute field (`*/15`,
      `0,30`) is left alone.
    - `at(...)`, and anything that doesn't parse, is left alone.
    """
    expression = (expression or "").strip()
    if offset_minutes <= 0:
        return expression, False
    offset_minutes %= _MINUTES_IN_DAY
    minute, carry = offset_minutes % 60, offset_minutes // 60

    rate = _RATE.match(expression)
    if rate:
        count, unit = int(rate.group(1)), rate.group(2)
        if count <= 0 or unit.startswith("minute"):
            return expression, False
        if unit.startswith("hour") and count < 24:
            hours = "*" if count == 1 else _shift_hours(f"*/{count}", carry)
            return f"cron({minute} {hours} * * ? *)", True
        days = count if unit.startswith("day") else max(count // 24, 1)
        day_of_month = "*" if days == 1 else f"1/{days}"
        return f"cron({minute} {carry} {day_of_month} * ? *)", True

    cron = _CRON.match(expression)
    if cron:
        fields = list(cron.groups())
        if not _WHOLE.match(fields[0]):
            return expression, False
        fields[0] = str(minute)
        fields[1] = _shift_hours(fields[1], carry)
        return f"cron({' '.join(fields)})", True

    return expression, False


def effective_schedules(
    *,
    research_cadence: str,
    daily_cadence: str,
    slot: int | None,
    stagger_minutes: int,
    topic_count: int,
) -> dict:
    """What to write to EventBridge Scheduler for one topic, and why:

    {"research": expr, "daily": expr, "research_offset_minutes": n, "daily_offset_minutes": n,
     "staggered": whether either expression was changed}
    """
    research_offset = research_offset_minutes(slot, stagger_minutes)
    daily_offset = daily_offset_minutes(slot, stagger_minutes, topic_count)
    research, research_applied = staggered_expression(research_cadence, research_offset)
    daily, daily_applied = staggered_expression(daily_cadence, daily_offset)
    return {
        "research": research,
        "daily": daily,
        "research_offset_minutes": research_offset if research_applied else 0,
        "daily_offset_minutes": daily_offset if daily_applied else 0,
        "staggered": research_applied or daily_applied,
    }


# --- for people -----------------------------------------------------------------------------------


def describe_expression(expression: str) -> str:
    """A schedule expression in plain words, for the admin CLI: "every 4 hours at :05 past",
    "daily at 09:20", "every hour at :10 past". An expression this doesn't recognise is returned
    as it is, so nothing is ever hidden."""
    expression = (expression or "").strip()
    rate = _RATE.match(expression)
    if rate:
        count, unit = int(rate.group(1)), rate.group(2).rstrip("s")
        return f"every {unit}" if count == 1 else f"every {count} {unit}s"

    cron = _CRON.match(expression)
    if not cron:
        return expression
    minute, hour, day_of_month, month, day_of_week, year = cron.groups()
    if not _WHOLE.match(minute):
        return expression
    every_day = day_of_month in ("*", "?") and month == "*" and day_of_week in ("*", "?") and year == "*"
    when = f":{int(minute):02d} past"
    if hour == "*":
        text = f"every hour at {when}"
    elif _STEP.match(hour):
        start, every = _STEP.match(hour).groups()
        text = f"every {every} hours at {when}"
        if start not in ("*", "0"):
            text += f" (from {int(start):02d}:{int(minute):02d})"
    elif _WHOLE.match(hour):
        clock = f"{int(hour):02d}:{int(minute):02d}"
        text = f"daily at {clock}" if every_day else f"at {clock}"
    else:
        text = f"at {when} of hours {hour}"
    if not every_day:
        text += f" ({expression})"
    return text
