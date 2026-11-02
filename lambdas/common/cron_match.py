"""Reading a topic's cadence the way EventBridge Scheduler does, so the dispatcher can decide the
same minutes the per-topic schedules fire at (docs/enhancements/model-ranch-prompt-assets-evals.md
section 4.8).

The dispatcher runs every minute and asks, for each topic, whether this minute is one of its
turns. The expressions it reads are the ones `stagger.effective_schedules` writes to the Scheduler
today: a `rate(...)`, or a `cron(...)` whose minute the stagger set, or a cron an operator wrote
and the stagger passed through. This module accepts exactly those shapes, and nothing else:

- minutes: a whole number (`5`), `*/N`, or a list (`0,30`);
- hours: `*`, a whole number, `*/N`, `a/N`, a list (`9,21`), a range (`9-17`, or `22-2` wrapping
  midnight), a range with a step (`9-17/2`);
- day of month: `*`, `?`, a whole number, or `1/N` (every N days from the 1st);
- month: `*`;
- day of week: `*`, `?`, a list of names (`MON,WED,FRI`) or a range of names (`MON-FRI`);
- year: `*`;
- `rate(N minutes|hours|days)`.

Anything else (`at(...)`, a month, a year, a `#` or `L` or `W`, a wrapped weekday range, both day
fields set at once, a number out of range) is refused with CadenceError. The caller logs the topic
and skips it; nothing here guesses what an unreadable cadence meant.

Turns. A rate is a heartbeat: for research every minute is a turn and `research_schedule.is_due`
decides whether the tick does work (the dispatcher proposal's rule); a daily rate is refused, since
the daily cycle runs once a day at a time of day. A cron's research turns are the minutes it names,
matched over the window since the dispatcher last ran, `(last_tick, now]`, so one failed dispatcher
run costs nothing. A cron's daily turn has *passed* when today's clock, as an `(hour, minute)`
pair in the topic's zone, has reached the first turn the cron names for today; the dispatcher then
starts the cycle once (its claim, `last_daily_dispatched_on`, stops a second start). Comparing
clock pairs is what makes daylight saving behave: a 02:30 turn on the spring-forward morning,
when 02:30 does not exist, fires at 03:00 (the first minute that is past it); on the fall-back
morning 02:30 comes round twice, and the claim stops the second one. A daily cron with several
hours (`9,21`, `*/4`) therefore dispatches once a day, at the first; the Scheduler fired each.

Topic-agnostic, and nothing here touches AWS or the clock: every function takes the time it is
asked about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

_RATE = re.compile(r"^rate\(\s*(\d+)\s+(minutes?|hours?|days?)\s*\)$")
_CRON = re.compile(r"^cron\(\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*\)$")
_WHOLE = re.compile(r"^\d+$")
_STEP = re.compile(r"^(\*|\d+)/(\d+)$")
_RANGE = re.compile(r"^(\d+)-(\d+)(?:/(\d+))?$")
_NAME_RANGE = re.compile(r"^([A-Za-z]{3})-([A-Za-z]{3})$")

# The Scheduler's names, in its order (SUN first), each mapped to Python's weekday (Monday 0).
_WEEKDAYS = {"SUN": 6, "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5}
_WEEKDAY_ORDER = tuple(_WEEKDAYS)
_ANY = ("*", "?")

# How far back research turns are looked for when the dispatcher has been away: every clock
# minute of a day has been seen once by then, and a turn older than that is one is_due decides
# anyway. Keeps a stale or absent last_tick from costing a long walk.
MAX_WINDOW = timedelta(days=1)


class CadenceError(ValueError):
    """The expression is not one this module reads. `expression` is what was refused (the dispatcher
    names it in its log line) and `why` says why, in plain words."""

    def __init__(self, expression: str, why: str):
        super().__init__(f"{expression!r}: {why}")
        self.expression = expression
        self.why = why


@dataclass(frozen=True)
class Cadence:
    """A parsed cadence. A rate has `rate` set and empty field sets; a cron has `rate` None and the
    minutes and hours it names, with `days_of_month` and `weekdays` None when any day will do.
    `weekdays` are Python's (Monday 0)."""

    expression: str
    rate: timedelta | None
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int] | None
    weekdays: frozenset[int] | None

    @property
    def is_rate(self) -> bool:
        return self.rate is not None

    def names_day(self, day: date) -> bool:
        """Whether the cron's day fields admit this date (a rate names every day)."""
        if self.days_of_month is not None and day.day not in self.days_of_month:
            return False
        return self.weekdays is None or day.weekday() in self.weekdays

    def matches(self, local: datetime) -> bool:
        """Whether this local minute is one of a cron's turns. A rate is never asked: it has none."""
        if self.is_rate:
            return False
        return local.minute in self.minutes and local.hour in self.hours and self.names_day(local.date())

    def turns_on(self, day: date) -> list[tuple[int, int]]:
        """A cron's turns on this date as (hour, minute) pairs, earliest first; [] when the day is
        not one it names."""
        if self.is_rate or not self.names_day(day):
            return []
        return sorted((hour, minute) for hour in self.hours for minute in self.minutes)

    def turn_passed(self, local_now: datetime) -> bool:
        """The daily rule: whether today's clock, in the topic's zone, has reached the first turn
        the cron names for today. Clock pairs, not instants, so a turn inside the spring-forward
        gap passes at the first minute after it. Refuses a rate: a daily cycle has no heartbeat."""
        if self.is_rate:
            raise CadenceError(self.expression, "a rate; a daily cadence must be a cron")
        turns = self.turns_on(local_now.date())
        return bool(turns) and (local_now.hour, local_now.minute) >= turns[0]


# --- parsing --------------------------------------------------------------------------------------


def _refuse(expression: str, why: str) -> CadenceError:
    return CadenceError(expression, why)


def _whole(part: str, low: int, high: int, what: str, expression: str) -> int:
    if not _WHOLE.match(part):
        raise _refuse(expression, f"{what} {part!r} is not a whole number")
    value = int(part)
    if not low <= value <= high:
        raise _refuse(expression, f"{what} {value} is outside {low} to {high}")
    return value


def _step(part: str, high: int, what: str, expression: str) -> int:
    """A `/N` step: from 1 to the field's own top, so `*/0` and `*/60` are refused."""
    return _whole(part, 1, high, f"{what} step", expression)


def _minutes(field: str, expression: str) -> frozenset[int]:
    step = _STEP.match(field)
    if step:
        if step.group(1) != "*":
            raise _refuse(expression, f"minute {field!r}: a minute step starts from '*' only")
        every = _step(step.group(2), 59, "minute", expression)
        return frozenset(range(0, 60, every))
    return frozenset(_whole(part, 0, 59, "minute", expression) for part in field.split(","))


def _hours(field: str, expression: str) -> frozenset[int]:
    if field == "*":
        return frozenset(range(24))
    step = _STEP.match(field)
    if step:
        first = 0 if step.group(1) == "*" else _whole(step.group(1), 0, 23, "hour", expression)
        every = _step(step.group(2), 23, "hour", expression)
        return frozenset(range(first, 24, every))
    span = _RANGE.match(field)
    if span:
        low = _whole(span.group(1), 0, 23, "hour", expression)
        high = _whole(span.group(2), 0, 23, "hour", expression)
        every = _step(span.group(3), 23, "hour", expression) if span.group(3) else 1
        # 22-2 wraps midnight: 22, 23, 0, 1, 2. The length of the span is the same either way.
        length = (high - low) % 24 + 1
        return frozenset((low + offset) % 24 for offset in range(0, length, every))
    return frozenset(_whole(part, 0, 23, "hour", expression) for part in field.split(","))


def _days_of_month(field: str, expression: str) -> frozenset[int] | None:
    if field in _ANY:
        return None
    step = _STEP.match(field)
    if step:
        if step.group(1) != "1":
            raise _refuse(expression, f"day of month {field!r}: a day step starts from 1 only")
        every = _step(step.group(2), 31, "day of month", expression)
        return frozenset(range(1, 32, every))
    return frozenset({_whole(field, 1, 31, "day of month", expression)})


def _weekday_name(name: str, expression: str) -> str:
    """`name` as the Scheduler spells it (SUN ... SAT), whatever its case."""
    if name.upper() not in _WEEKDAYS:
        raise _refuse(expression, f"day of week {name!r} is not one of {', '.join(_WEEKDAY_ORDER)}")
    return name.upper()


def _weekdays(field: str, expression: str) -> frozenset[int] | None:
    if field in _ANY:
        return None
    span = _NAME_RANGE.match(field)
    if span:
        first, last = (_WEEKDAY_ORDER.index(_weekday_name(name, expression)) for name in span.groups())
        # SUN-SAT is the Scheduler's order; a range that runs backwards would have to wrap the
        # week, and no cadence written today does, so it is refused rather than guessed.
        if first > last:
            raise _refuse(expression, f"day of week {field!r} runs backwards; a weekday range does not wrap")
        return frozenset(_WEEKDAYS[_WEEKDAY_ORDER[index]] for index in range(first, last + 1))
    return frozenset(_WEEKDAYS[_weekday_name(name, expression)] for name in field.split(","))


def parse(expression: str, *, allow_rate: bool = True) -> Cadence:
    """The cadence `expression` names, or CadenceError when it is not one of the shapes above.
    `allow_rate=False` is the daily cadence's reading: a rate is refused there and then."""
    text = (expression or "").strip() if isinstance(expression, str) else ""
    if not text:
        raise CadenceError(text, "an empty cadence")

    rate = _RATE.match(text)
    if rate:
        if not allow_rate:
            raise _refuse(text, "a rate; a daily cadence must be a cron")
        count, unit = int(rate.group(1)), rate.group(2)
        if count < 1:
            raise _refuse(text, "a rate of 0")
        if unit.startswith("minute"):
            every = timedelta(minutes=count)
        elif unit.startswith("hour"):
            every = timedelta(hours=count)
        else:
            every = timedelta(days=count)
        return Cadence(text, every, frozenset(), frozenset(), None, None)

    cron = _CRON.match(text)
    if not cron:
        raise _refuse(text, "not a rate(...) or a six-field cron(...)")
    minute, hour, day_of_month, month, day_of_week, year = cron.groups()
    if month != "*":
        raise _refuse(text, f"month {month!r}: only '*' is read")
    if year != "*":
        raise _refuse(text, f"year {year!r}: only '*' is read")
    if day_of_month not in _ANY and day_of_week not in _ANY:
        raise _refuse(text, "both day fields are set; the Scheduler takes one of them as '?'")
    return Cadence(
        text,
        None,
        _minutes(minute, text),
        _hours(hour, text),
        _days_of_month(day_of_month, text),
        _weekdays(day_of_week, text),
    )


# --- turns ----------------------------------------------------------------------------------------


def _minute_of(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def research_turn(
    cadence: Cadence, last_tick: datetime | None, now: datetime, zone: tzinfo = UTC
) -> bool:
    """Whether the research cadence has a turn in `(last_tick, now]`, read as clock minutes in
    `zone` (the Scheduler reads a research cron in UTC). A rate is always a turn: is_due decides.
    With no `last_tick`, or one that is not before `now`, only this minute is looked at; a window
    longer than MAX_WINDOW is cut to its last day."""
    if cadence.is_rate:
        return True
    last = _minute_of(now)
    if last_tick is None or last_tick >= now:
        first = last
    else:
        first = _minute_of(max(last_tick, now - MAX_WINDOW)) + timedelta(minutes=1)
    moment = first
    while moment <= last:
        if cadence.matches(moment.astimezone(zone)):
            return True
        moment += timedelta(minutes=1)
    return False


def daily_turn_passed(cadence: Cadence, now: datetime, zone: tzinfo) -> bool:
    """Whether the daily cadence's first turn for today, in the topic's zone, has passed by `now`
    (Cadence.turn_passed on the local clock)."""
    return cadence.turn_passed(now.astimezone(zone))
