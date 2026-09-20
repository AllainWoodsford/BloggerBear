from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from common.editorial_goals import (
    ARTICLE_STYLES,
    EDITORIAL_MANDATES,
    GOAL_POOL,
    OFF_ADAPTER_DOMAIN_GOALS,
    EditorialGoal,
    goal_for_adapter_config,
    goal_for_date,
    parse_goal,
    resolve_goal_for_topic,
)

DAY = date(2026, 9, 20)
YEAR = [DAY + timedelta(days=offset) for offset in range(730)]


def test_the_pool_holds_every_goal_once():
    assert sorted(GOAL_POOL, key=lambda g: g.value) == sorted(EditorialGoal, key=lambda g: g.value)
    assert len(GOAL_POOL) == 4


def test_goal_is_a_pure_function_of_the_date():
    # stable for the whole day: hourly ticks and the daily cycle must always agree
    assert goal_for_date(DAY) is goal_for_date(date(2026, 9, 20))
    assert [goal_for_date(d) for d in YEAR] == [goal_for_date(d) for d in YEAR]


def test_every_goal_comes_up_and_the_draw_is_roughly_uniform():
    counts = Counter(goal_for_date(d) for d in YEAR)

    assert set(counts) == set(EditorialGoal)
    for goal in EditorialGoal:
        assert 0.20 < counts[goal] / len(YEAR) < 0.30  # ~25% each


def test_the_draw_is_random_not_a_repeating_cycle():
    goals = [goal_for_date(d) for d in YEAR]

    assert any(a is b for a, b in zip(goals, goals[1:], strict=False))  # repeats happen
    for period in (2, 3, 4):  # and it isn't a cycle of any small period
        assert goals[:60] != goals[period : period + 60]


def test_market_news_is_the_only_off_adapter_domain_goal():
    assert OFF_ADAPTER_DOMAIN_GOALS == {EditorialGoal.MARKET_NEWS}
    assert "do not bring them in" in EDITORIAL_MANDATES[EditorialGoal.MARKET_NEWS]


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
