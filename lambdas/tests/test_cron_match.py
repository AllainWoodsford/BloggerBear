"""The cron matcher (common/cron_match.py): the shapes the stagger writes or passes through are
read, everything else is refused, and the turns it computes are the minutes EventBridge Scheduler
fires at, including round midnight and across daylight saving in Sydney and Los Angeles."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common import cron_match
from common.cron_match import CadenceError, parse

SYDNEY = ZoneInfo("Australia/Sydney")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")
EVERY_HOUR = frozenset(range(24))


def at(text: str) -> datetime:
    """A UTC instant from "2026-10-25T03:04"."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def local(text: str, zone) -> datetime:
    """A wall-clock time in `zone` from "2026-10-25T03:04" (an ambiguous or missing time takes
    zoneinfo's first reading)."""
    return datetime.fromisoformat(text).replace(tzinfo=zone)


# --- every shape the grammar reads -------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression, minutes, hours, days_of_month, weekdays",
    [
        # What the stagger writes: a single minute, and the hour shapes _shift_hours emits.
        ("cron(5 * * * ? *)", {5}, EVERY_HOUR, None, None),
        ("cron(25 9 * * ? *)", {25}, {9}, None, None),
        ("cron(5 */4 * * ? *)", {5}, {0, 4, 8, 12, 16, 20}, None, None),
        ("cron(5 1/4 * * ? *)", {5}, {1, 5, 9, 13, 17, 21}, None, None),
        ("cron(30 9,21 * * ? *)", {30}, {9, 21}, None, None),
        ("cron(0 9-17 * * ? *)", {0}, set(range(9, 18)), None, None),
        ("cron(5 0 1/2 * ? *)", {5}, {0}, {1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31}, None),
        # What an operator writes and the stagger passes through.
        ("cron(*/15 * * * ? *)", {0, 15, 30, 45}, EVERY_HOUR, None, None),
        ("cron(0,30 * * * ? *)", {0, 30}, EVERY_HOUR, None, None),
        ("cron(0 9-17/2 * * ? *)", {0}, {9, 11, 13, 15, 17}, None, None),
        ("cron(0 9-9 * * ? *)", {0}, {9}, None, None),
        ("cron(0 9 15 * ? *)", {0}, {9}, {15}, None),
        ("cron(0 9 ? * MON-FRI *)", {0}, {9}, None, {0, 1, 2, 3, 4}),
        ("cron(0 9 ? * MON,WED,FRI *)", {0}, {9}, None, {0, 2, 4}),
        ("cron(0 9 ? * sun *)", {0}, {9}, None, {6}),
        ("cron(0 9 * * * *)", {0}, {9}, None, None),
        ("  cron(0 9 * * ? *)  ", {0}, {9}, None, None),
    ],
)
def test_every_shape_the_stagger_writes_or_passes_through_is_read(
    expression, minutes, hours, days_of_month, weekdays
):
    cadence = parse(expression)

    assert cadence.rate is None and not cadence.is_rate
    assert cadence.minutes == frozenset(minutes)
    assert cadence.hours == frozenset(hours)
    assert cadence.days_of_month == (None if days_of_month is None else frozenset(days_of_month))
    assert cadence.weekdays == (None if weekdays is None else frozenset(weekdays))


def test_an_hour_range_wraps_midnight():
    assert parse("cron(25 22-2 * * ? *)").hours == {22, 23, 0, 1, 2}
    assert parse("cron(0 22-2/2 * * ? *)").hours == {22, 0, 2}
    assert parse("cron(0 23-0 * * ? *)").hours == {23, 0}


@pytest.mark.parametrize(
    "expression, every",
    [
        ("rate(1 minute)", timedelta(minutes=1)),
        ("rate(5 minutes)", timedelta(minutes=5)),
        ("rate(1 hour)", timedelta(hours=1)),
        ("rate(4 hours)", timedelta(hours=4)),
        ("rate(2 days)", timedelta(days=2)),
    ],
)
def test_a_rate_is_read_as_the_heartbeat_it_is(expression, every):
    cadence = parse(expression)

    assert cadence.is_rate and cadence.rate == every
    assert cadence.minutes == frozenset() and cadence.hours == frozenset()


# --- refusals ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "at(2026-10-25T09:00:00)",
        "cron(0 9 * * ? 2026)",  # a year
        "cron(0 9 * JAN ? *)",  # a month
        "cron(0 9 1 * MON *)",  # both day fields set
        "cron(0 9 ? * SAT-MON *)",  # a weekday range that wraps the week
        "cron(0 9 ? * MON-FRI/2 *)",
        "cron(0 9 L * ? *)",
        "cron(0 9 ? * 2#1 *)",
        "cron(0 9 15W * ? *)",
        "cron(60 9 * * ? *)",
        "cron(0 24 * * ? *)",
        "cron(0 9 32 * ? *)",
        "cron(0 9 0 * ? *)",
        "cron(*/0 9 * * ? *)",
        "cron(*/60 9 * * ? *)",
        "cron(5/15 9 * * ? *)",  # a minute step starts from * only
        "cron(0 9 2/5 * ? *)",  # a day step starts from 1 only
        "cron(0 */24 * * ? *)",
        "cron(0 9-25 * * ? *)",
        "cron(0 9 * * ? *",
        "cron(0 9 * * ?)",
        "cron(0 9 * * ? * *)",
        "rate(0 hours)",
        "rate(1 week)",
        "rate(90 seconds)",
        "every hour",
        "",
        None,
        9,
    ],
)
def test_anything_else_is_refused_not_guessed(expression):
    with pytest.raises(CadenceError):
        parse(expression)


def test_the_refusal_says_what_was_wrong_in_plain_words():
    with pytest.raises(CadenceError, match="hour 24 is outside 0 to 23"):
        parse("cron(0 24 * * ? *)")
    with pytest.raises(CadenceError, match="not a rate"):
        parse("at(2026-10-25T09:00:00)")


def test_a_daily_cadence_cannot_be_a_rate():
    with pytest.raises(CadenceError, match="a daily cadence must be a cron"):
        parse("rate(1 day)", allow_rate=False)
    with pytest.raises(CadenceError, match="a daily cadence must be a cron"):
        cron_match.daily_turn_passed(parse("rate(1 hour)"), at("2026-10-25T09:00"), UTC)
    assert parse("cron(0 9 * * ? *)", allow_rate=False).hours == {9}


# --- research turns: (last_tick, now] --------------------------------------------------------------------


def test_a_research_rate_makes_every_minute_a_turn_and_leaves_is_due_to_decide():
    cadence = parse("rate(1 hour)")

    assert cron_match.research_turn(cadence, at("2026-10-25T03:03"), at("2026-10-25T03:04"))
    assert cron_match.research_turn(cadence, None, at("2026-10-25T03:04:30"))
    assert not cadence.matches(at("2026-10-25T03:04"))


def test_a_research_cron_is_a_turn_only_at_the_minutes_it_names():
    cadence = parse("cron(5 */4 * * ? *)")

    assert not cron_match.research_turn(cadence, at("2026-10-25T04:03"), at("2026-10-25T04:04"))
    assert cron_match.research_turn(cadence, at("2026-10-25T04:04"), at("2026-10-25T04:05"))
    assert cron_match.research_turn(cadence, at("2026-10-25T04:05"), at("2026-10-25T04:06")) is False
    assert cron_match.research_turn(cadence, at("2026-10-25T05:05"), at("2026-10-25T05:06")) is False


def test_the_window_is_open_at_last_tick_and_closed_at_now_in_whole_minutes():
    """The dispatcher that ran at 04:05:03 saw 04:05; the one at 04:06:02 must not see it again."""
    cadence = parse("cron(5 * * * ? *)")

    assert cron_match.research_turn(cadence, at("2026-10-25T04:04:02"), at("2026-10-25T04:05:03"))
    assert not cron_match.research_turn(cadence, at("2026-10-25T04:05:03"), at("2026-10-25T04:06:02"))
    # Two runs inside one minute: the second has no new minute to look at.
    assert not cron_match.research_turn(cadence, at("2026-10-25T04:05:03"), at("2026-10-25T04:05:40"))


def test_a_missed_dispatcher_run_costs_nothing_the_turn_is_found_in_the_gap():
    cadence = parse("cron(5 */4 * * ? *)")

    assert cron_match.research_turn(cadence, at("2026-10-25T03:00"), at("2026-10-25T06:00"))
    assert not cron_match.research_turn(cadence, at("2026-10-25T04:06"), at("2026-10-25T06:00"))


def test_with_no_last_tick_only_this_minute_is_a_turn():
    cadence = parse("cron(5 * * * ? *)")

    assert cron_match.research_turn(cadence, None, at("2026-10-25T04:05:59"))
    assert not cron_match.research_turn(cadence, None, at("2026-10-25T04:06:00"))
    # A last_tick in the future (a clock put back) is read the same way.
    assert not cron_match.research_turn(cadence, at("2026-10-25T05:00"), at("2026-10-25T04:06"))


def test_a_window_longer_than_a_day_is_cut_to_its_last_day():
    """Every clock minute has come round once in a day; an older turn is one is_due decides."""
    cadence = parse("cron(0 9 15 * ? *)")  # the 15th at 09:00

    assert cron_match.research_turn(cadence, at("2026-10-14T00:00"), at("2026-10-15T12:00"))
    assert not cron_match.research_turn(cadence, at("2026-10-14T00:00"), at("2026-10-18T12:00"))
    assert cron_match.MAX_WINDOW == timedelta(days=1)


def test_research_turns_are_read_in_the_zone_given_utc_by_default():
    cadence = parse("cron(0 9 * * ? *)")

    assert cron_match.research_turn(cadence, at("2026-06-14T08:59"), at("2026-06-14T09:00"))
    assert not cron_match.research_turn(cadence, at("2026-06-14T08:59"), at("2026-06-14T09:00"), SYDNEY)
    assert cron_match.research_turn(cadence, at("2026-06-13T22:59"), at("2026-06-13T23:00"), SYDNEY)


def test_day_fields_hold_for_research_turns_too():
    weekdays = parse("cron(0 9 ? * MON-FRI *)")
    assert cron_match.research_turn(weekdays, at("2026-10-26T08:59"), at("2026-10-26T09:00"))  # a Monday
    assert not cron_match.research_turn(weekdays, at("2026-10-25T08:59"), at("2026-10-25T09:00"))  # a Sunday

    fifths = parse("cron(0 9 1/5 * ? *)")
    assert cron_match.research_turn(fifths, at("2026-10-06T08:59"), at("2026-10-06T09:00"))
    assert not cron_match.research_turn(fifths, at("2026-10-07T08:59"), at("2026-10-07T09:00"))


# --- the daily turn: passed once today's clock has reached it ---------------------------------------------


def test_the_daily_turn_passes_at_its_minute_and_stays_passed_for_the_day():
    cadence = parse("cron(25 9 * * ? *)")

    assert not cron_match.daily_turn_passed(cadence, at("2026-10-24T22:24"), SYDNEY)  # 09:24 AEDT
    assert cron_match.daily_turn_passed(cadence, at("2026-10-24T22:25"), SYDNEY)
    assert cron_match.daily_turn_passed(cadence, at("2026-10-25T12:59"), SYDNEY)  # 23:59 AEDT
    assert not cron_match.daily_turn_passed(cadence, at("2026-10-25T13:00"), SYDNEY)  # the next day, 00:00


def test_a_cadence_with_several_hours_dispatches_once_a_day_at_the_first():
    """The Scheduler fires 9 and 21; the dispatcher's daily rule fires 9 and leaves the claim to stop
    anything more. This is the documented change of behaviour."""
    two = parse("cron(0 9,21 * * ? *)")
    assert two.turns_on(date(2026, 10, 25)) == [(9, 0), (21, 0)]
    assert not cron_match.daily_turn_passed(two, local("2026-10-25T08:59", UTC), UTC)
    assert cron_match.daily_turn_passed(two, local("2026-10-25T09:00", UTC), UTC)
    assert cron_match.daily_turn_passed(two, local("2026-10-25T21:00", UTC), UTC)

    every_four = parse("cron(25 */4 * * ? *)")
    assert not cron_match.daily_turn_passed(every_four, local("2026-10-25T00:24", UTC), UTC)
    assert cron_match.daily_turn_passed(every_four, local("2026-10-25T00:25", UTC), UTC)


def test_a_daily_turn_wrapped_past_midnight_is_still_a_time_of_day():
    """The stagger carries a late daily hour round the clock (`22-2`); each day's first turn is 00:25."""
    cadence = parse("cron(25 22-2 * * ? *)")

    assert cadence.turns_on(date(2026, 10, 25)) == [(0, 25), (1, 25), (2, 25), (22, 25), (23, 25)]
    assert not cron_match.daily_turn_passed(cadence, local("2026-10-25T00:24", UTC), UTC)
    assert cron_match.daily_turn_passed(cadence, local("2026-10-25T00:25", UTC), UTC)
    assert cron_match.daily_turn_passed(cadence, local("2026-10-25T22:25", UTC), UTC)


def test_the_daily_turn_respects_the_day_fields():
    weekdays = parse("cron(0 9 ? * MON-FRI *)")
    assert cron_match.daily_turn_passed(weekdays, local("2026-10-26T09:00", UTC), UTC)  # Monday
    assert not cron_match.daily_turn_passed(weekdays, local("2026-10-25T09:00", UTC), UTC)  # Sunday
    assert weekdays.turns_on(date(2026, 10, 25)) == []

    named = parse("cron(0 9 ? * MON,WED *)")
    assert cron_match.daily_turn_passed(named, local("2026-10-28T09:00", UTC), UTC)  # Wednesday
    assert not cron_match.daily_turn_passed(named, local("2026-10-27T09:00", UTC), UTC)  # Tuesday

    fifths = parse("cron(0 9 1/5 * ? *)")
    assert cron_match.daily_turn_passed(fifths, local("2026-10-06T09:00", UTC), UTC)
    assert not cron_match.daily_turn_passed(fifths, local("2026-10-07T09:00", UTC), UTC)

    fifteenth = parse("cron(0 9 15 * ? *)")
    assert cron_match.daily_turn_passed(fifteenth, local("2026-10-15T09:00", UTC), UTC)
    assert not cron_match.daily_turn_passed(fifteenth, local("2026-10-16T09:00", UTC), UTC)


def test_matches_and_turns_on_are_the_same_rule_from_two_sides():
    cadence = parse("cron(0,30 9 ? * MON-FRI *)")

    assert cadence.matches(local("2026-10-26T09:30", UTC))
    assert not cadence.matches(local("2026-10-26T09:31", UTC))
    assert not cadence.matches(local("2026-10-25T09:30", UTC))
    assert cadence.turns_on(date(2026, 10, 26)) == [(9, 0), (9, 30)]
    assert cadence.names_day(date(2026, 10, 26)) and not cadence.names_day(date(2026, 10, 25))


# --- daylight saving -----------------------------------------------------------------------------------
#
# Sydney: clocks go forward on 2026-10-04 (02:00 AEST becomes 03:00 AEDT) and back on 2026-04-05
# (03:00 AEDT becomes 02:00 AEST). Los Angeles: forward on 2026-03-08 (02:00 to 03:00) and back on
# 2026-11-01 (02:00 to 01:00). The turn is a clock pair, so a spring-forward turn in the missing hour
# fires at the first minute past it, and a fall-back turn in the repeated hour passes both times
# (the dispatcher's claim on last_daily_dispatched_on stops the second start).


def test_sydney_spring_forward_a_turn_in_the_missing_hour_fires_at_three():
    cadence = parse("cron(30 2 * * ? *)")

    before = at("2026-10-03T15:59")  # 01:59 AEST
    first_after = at("2026-10-03T16:00")  # 03:00 AEDT: 02:00-02:59 never happened
    assert before.astimezone(SYDNEY).hour == 1 and first_after.astimezone(SYDNEY).hour == 3
    assert not cron_match.daily_turn_passed(cadence, before, SYDNEY)
    assert cron_match.daily_turn_passed(cadence, first_after, SYDNEY)
    # The day before and after, the same turn passes at 02:30 as usual.
    assert not cron_match.daily_turn_passed(cadence, at("2026-10-02T16:29"), SYDNEY)
    assert cron_match.daily_turn_passed(cadence, at("2026-10-02T16:30"), SYDNEY)
    assert cron_match.daily_turn_passed(cadence, at("2026-10-04T15:30"), SYDNEY)


def test_sydney_fall_back_a_turn_in_the_repeated_hour_passes_both_times():
    cadence = parse("cron(30 2 * * ? *)")

    first = at("2026-04-04T15:30")  # 02:30 AEDT
    second = at("2026-04-04T16:30")  # 02:30 AEST, an hour later
    assert first.astimezone(SYDNEY).replace(tzinfo=None) == second.astimezone(SYDNEY).replace(tzinfo=None)
    assert not cron_match.daily_turn_passed(cadence, at("2026-04-04T15:29"), SYDNEY)
    assert cron_match.daily_turn_passed(cadence, first, SYDNEY)
    assert cron_match.daily_turn_passed(cadence, second, SYDNEY)
    # Between the two, the clock reads 02:00-02:29 again and the turn is "not yet" again; the claim
    # the dispatcher took at the first 02:30 is what keeps the day consumed through all of this.
    assert not cron_match.daily_turn_passed(cadence, at("2026-04-04T16:29"), SYDNEY)  # 02:29 AEST


def test_los_angeles_spring_forward_a_turn_in_the_missing_hour_fires_at_three():
    cadence = parse("cron(30 2 * * ? *)")

    before = at("2026-03-08T09:59")  # 01:59 PST
    first_after = at("2026-03-08T10:00")  # 03:00 PDT
    assert before.astimezone(LOS_ANGELES).hour == 1 and first_after.astimezone(LOS_ANGELES).hour == 3
    assert not cron_match.daily_turn_passed(cadence, before, LOS_ANGELES)
    assert cron_match.daily_turn_passed(cadence, first_after, LOS_ANGELES)


def test_los_angeles_fall_back_a_turn_in_the_repeated_hour_passes_both_times():
    cadence = parse("cron(30 1 * * ? *)")

    first = at("2026-11-01T08:30")  # 01:30 PDT
    second = at("2026-11-01T09:30")  # 01:30 PST
    assert first.astimezone(LOS_ANGELES).hour == 1 and second.astimezone(LOS_ANGELES).hour == 1
    assert not cron_match.daily_turn_passed(cadence, at("2026-11-01T08:29"), LOS_ANGELES)
    assert cron_match.daily_turn_passed(cadence, first, LOS_ANGELES)
    assert cron_match.daily_turn_passed(cadence, second, LOS_ANGELES)


def test_a_research_cron_in_a_zone_with_daylight_saving_finds_its_turn_each_day():
    """A clock-time turn either side of Sydney's change is one turn a day, at the instant the clock
    says 09:00 (23:00 UTC in winter, 22:00 UTC in summer)."""
    cadence = parse("cron(0 9 * * ? *)")

    assert cron_match.research_turn(cadence, at("2026-10-02T22:59"), at("2026-10-02T23:00"), SYDNEY)
    assert cron_match.research_turn(cadence, at("2026-10-04T21:59"), at("2026-10-04T22:00"), SYDNEY)
    assert not cron_match.research_turn(cadence, at("2026-10-04T22:59"), at("2026-10-04T23:00"), SYDNEY)
