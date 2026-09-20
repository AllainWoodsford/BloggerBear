from __future__ import annotations

import pytest

import research_tick_handler
from common.editorial_resolver import (
    ADAPTER_DEFAULTS,
    DEFAULT_ADAPTER,
    GLOBAL_DEFAULT_GOAL,
    MAX_GOAL_TEXT_CHARS,
    normalize_editorial_goals,
    resolve_editorial_goals,
    validate_editorial_goals,
)

FOCUS = "Identify unpatched zero-day exploits actively being observed in production environments."
EXCLUSIONS = "Ignore generalized marketing press releases or compliance frameworks."


def _topic(adapter="web_search", **goals):
    topic = {"topic_id": "t", "name": "T", "adapter": adapter}
    if goals:
        topic["editorial_goals"] = goals
    return topic


# --- the fallback hierarchy --------------------------------------------------------


def test_a_topic_specific_focus_wins_over_the_adapter_and_global_defaults():
    resolved = resolve_editorial_goals(_topic("crypto_feed", primary_focus=FOCUS))

    assert resolved == f"Topic-Specific Focus: {FOCUS}"
    assert ADAPTER_DEFAULTS["crypto_feed"] not in resolved
    assert GLOBAL_DEFAULT_GOAL not in resolved


@pytest.mark.parametrize("adapter", sorted(ADAPTER_DEFAULTS))
def test_with_no_topic_goal_the_adapter_default_applies(adapter):
    resolved = resolve_editorial_goals(_topic(adapter))

    assert resolved == f"Adapter-Specific Standard Goal: {ADAPTER_DEFAULTS[adapter]}"


@pytest.mark.parametrize("adapter", ["web_search", "hacker_news", "some_future_adapter"])
def test_an_adapter_without_a_default_falls_back_to_the_global_goal(adapter):
    resolved = resolve_editorial_goals(_topic(adapter))

    assert resolved == f"Global Default Goal: {GLOBAL_DEFAULT_GOAL}"


def test_a_topic_with_no_adapter_at_all_gets_the_global_goal():
    topic = {"topic_id": "t", "name": "T"}

    assert resolve_editorial_goals(topic) == f"Global Default Goal: {GLOBAL_DEFAULT_GOAL}"
    assert DEFAULT_ADAPTER == "web_search"


def test_exclusion_criteria_layer_on_top_of_whichever_goal_applies():
    with_focus = resolve_editorial_goals(_topic(primary_focus=FOCUS, exclusion_criteria=EXCLUSIONS))
    assert with_focus == f"Topic-Specific Focus: {FOCUS}\nStrict Constraints: {EXCLUSIONS}"

    on_adapter_default = resolve_editorial_goals(_topic("github_trending", exclusion_criteria=EXCLUSIONS))
    assert on_adapter_default.startswith("Adapter-Specific Standard Goal:")
    assert on_adapter_default.endswith(f"\nStrict Constraints: {EXCLUSIONS}")

    on_global = resolve_editorial_goals(_topic(exclusion_criteria=EXCLUSIONS))
    assert on_global.startswith("Global Default Goal:")
    assert on_global.endswith(f"\nStrict Constraints: {EXCLUSIONS}")


@pytest.mark.parametrize(
    "junk",
    [None, "a string", ["a"], 5, {"primary_focus": None}, {"primary_focus": "  "}, {"primary_focus": 3}],
)
def test_malformed_or_blank_goal_blocks_fall_through_safely(junk):
    topic = {"topic_id": "t", "adapter": "github_trending", "editorial_goals": junk}

    assert resolve_editorial_goals(topic) == (
        f"Adapter-Specific Standard Goal: {ADAPTER_DEFAULTS['github_trending']}"
    )


def test_focus_text_is_stripped():
    assert resolve_editorial_goals(_topic(primary_focus=f"  {FOCUS}\n")) == f"Topic-Specific Focus: {FOCUS}"


def test_the_global_default_is_independent_web_research_over_five_to_ten_items():
    assert "independent web research" in GLOBAL_DEFAULT_GOAL
    assert "5 to 10" in GLOBAL_DEFAULT_GOAL


def test_every_adapter_default_names_a_real_registered_adapter():
    assert set(ADAPTER_DEFAULTS) <= set(research_tick_handler.ADAPTER_REGISTRY)
    assert DEFAULT_ADAPTER in research_tick_handler.ADAPTER_REGISTRY


# --- validation --------------------------------------------------------------------


@pytest.mark.parametrize(
    "ok",
    [
        None,
        {},
        {"primary_focus": "x"},
        {"exclusion_criteria": "y"},
        {"primary_focus": "x", "exclusion_criteria": "y"},
    ],
)
def test_valid_blocks_pass_validation(ok):
    assert validate_editorial_goals(ok) is None


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        ("text", "must be an object"),
        (["a"], "must be an object"),
        ({"tone": "witty"}, "unknown field"),
        ({"primary_focus": ""}, "primary_focus"),
        ({"primary_focus": "   "}, "primary_focus"),
        ({"exclusion_criteria": 7}, "exclusion_criteria"),
        ({"primary_focus": "x" * (MAX_GOAL_TEXT_CHARS + 1)}, "at most"),
    ],
)
def test_invalid_blocks_are_rejected_with_a_specific_message(bad, fragment):
    assert fragment in validate_editorial_goals(bad)


def test_the_length_limit_is_on_the_stripped_text():
    assert validate_editorial_goals({"primary_focus": " " + "x" * MAX_GOAL_TEXT_CHARS + " "}) is None


def test_normalize_strips_text_and_tolerates_none():
    assert normalize_editorial_goals({"primary_focus": " a ", "exclusion_criteria": "b\n"}) == {
        "primary_focus": "a",
        "exclusion_criteria": "b",
    }
    assert normalize_editorial_goals(None) == {}
    assert normalize_editorial_goals({}) == {}
