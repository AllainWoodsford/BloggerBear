from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from common.editorial_goals import (
    ARTICLE_STYLES,
    EDITORIAL_MANDATES,
    GOAL_ROTATION,
    EditorialGoal,
    goal_for_adapter_config,
    goal_for_date,
    parse_goal,
    resolve_goal_for_topic,
)

DAY = date(2026, 9, 20)


def test_rotation_advances_one_goal_per_day_and_repeats_every_three_days():
    goals = [goal_for_date(DAY + timedelta(days=offset)) for offset in range(6)]

    assert set(goals[:3]) == set(EditorialGoal)  # all three appear in any 3 consecutive days
    assert goals[:3] == goals[3:]
    for offset in range(3):
        current = GOAL_ROTATION.index(goals[offset])
        assert goals[offset + 1] is GOAL_ROTATION[(current + 1) % 3]


def test_rotation_is_even_over_time():
    counts = Counter(goal_for_date(DAY + timedelta(days=offset)) for offset in range(300))

    assert set(counts.values()) == {100}


def test_goal_is_a_pure_function_of_the_date():
    assert goal_for_date(DAY) is goal_for_date(date(2026, 9, 20))


def test_parse_goal_accepts_names_case_insensitively_and_rejects_the_rest():
    assert parse_goal("altcoin_deep_dive") is EditorialGoal.ALTCOIN_DEEP_DIVE
    assert parse_goal("  WEB_AGGREGATOR ") is EditorialGoal.WEB_AGGREGATOR
    assert parse_goal(EditorialGoal.TREND_INVENTOR) is EditorialGoal.TREND_INVENTOR
    assert parse_goal("nope") is None
    assert parse_goal(None) is None
    assert parse_goal(3) is None


def test_pinned_goal_beats_the_rotation_and_invalid_pins_are_ignored():
    rotation_goal = goal_for_date(DAY)
    other = next(goal for goal in EditorialGoal if goal is not rotation_goal)

    assert goal_for_adapter_config({"editorial_goal": other.value}, DAY) is other
    assert goal_for_adapter_config({"editorial_goal": "garbage"}, DAY) is rotation_goal
    assert goal_for_adapter_config({}, DAY) is rotation_goal
    assert goal_for_adapter_config(None, DAY) is rotation_goal


def test_only_crypto_topics_have_an_editorial_goal():
    crypto = {"topic_id": "c", "adapter": "crypto_feed", "adapter_config": {}}

    assert resolve_goal_for_topic(crypto, DAY) is goal_for_date(DAY)
    assert resolve_goal_for_topic({"topic_id": "g", "adapter": "github_trending"}, DAY) is None
    assert resolve_goal_for_topic({"topic_id": "w", "adapter": "web_search"}, DAY) is None
    assert resolve_goal_for_topic({"topic_id": "x"}, DAY) is None


def test_a_pinned_goal_applies_to_the_topic():
    crypto = {
        "topic_id": "c",
        "adapter": "crypto_feed",
        "adapter_config": {"editorial_goal": "TREND_INVENTOR"},
    }

    assert resolve_goal_for_topic(crypto, DAY) is EditorialGoal.TREND_INVENTOR


def test_every_goal_has_a_mandate_and_a_style():
    for goal in EditorialGoal:
        assert EDITORIAL_MANDATES[goal].strip()
        assert ARTICLE_STYLES[goal].strip()
