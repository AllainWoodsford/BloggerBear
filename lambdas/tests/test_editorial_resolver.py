from __future__ import annotations

import pytest

import research_tick_handler
from common.day_subject import MAX_DAY_SUBJECT_LABEL_CHARS
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


def test_a_rail_topic_without_goals_drafts_under_its_own_limits_not_the_global_sample():
    topic = {"topic_id": "rail-sydney", "name": "Rail access in Sydney", "adapter": "rail_access"}
    resolved = resolve_editorial_goals(topic)

    assert resolved == f"Adapter-Specific Standard Goal: {ADAPTER_DEFAULTS['rail_access']}"
    assert GLOBAL_DEFAULT_GOAL not in resolved and "randomly sampling" not in resolved
    # Its summary prompt's limits: proxies from imagery and OpenStreetMap; no ridership, cost,
    # cause, forecast or recommendation; nothing that is not in the findings.
    for limit in ("Sentinel-2 imagery and OpenStreetMap", "proxies", "ridership", "cost", "cause",
                  "forecast", "recommendation", "not in the findings"):  # fmt: skip
        assert limit in resolved, limit


def test_the_rail_default_carries_the_stops_proxies_the_theoretical_links_and_the_register():
    """The five themes every rail goal carries, in the adapter's own default: the stop measures
    are proxies (straight-line reach to a mapped bus, tram or ferry stop, minutes at a stated
    pace, never a journey time); every simulated link is theoretical, justified and qualified by
    what its line crosses, never a route or a recommendation; a constructive register with no
    cost, political or resource-allocation view; the transport efficiency of the mapped stops
    and the multimodal isolation weight named as what they measure; nothing not in the findings."""
    goal = ADAPTER_DEFAULTS["rail_access"]
    # rail_access.subject_mandate folds a city's name into exactly these opening words.
    assert goal.startswith("Report only what was measured: proxies from Sentinel-2 imagery and OpenStreetMap")
    for theme in (
        "bus, tram and ferry stops", "straight-line reach to a station or stop", "80 m a minute",
        "journey, travel or waiting time", "per 100 stops", "overlap with station reach",
        "multimodal isolation weight", "theoretical what-if on the mapped graph",
        "what its straight line crosses", "water", "never a route, alignment or recommendation",
        "what a theoretical link would change", "complaint", "blame",
        "political or resource-allocation view", "not in the findings",
    ):  # fmt: skip
        assert theme in goal, theme
    # Not a complaint: the register is set by what to say, and no agency or politician is named.
    for word in ("agency", "politician", "underfunded", "neglect", "failure"):
        assert word not in goal, word
    # It is also a day subject's mandate with " for <city>" folded in, which the daily cycle refuses
    # over MAX_GOAL_TEXT_CHARS: room for the longest site name, so under 900 characters.
    assert len(goal) < 900
    assert len(goal) + len(" for ") + MAX_DAY_SUBJECT_LABEL_CHARS <= MAX_GOAL_TEXT_CHARS


def test_a_satellite_vision_topic_without_goals_drafts_under_its_own_limits():
    resolved = resolve_editorial_goals({"topic_id": "anchorages", "adapter": "satellite_vision"})

    assert resolved == f"Adapter-Specific Standard Goal: {ADAPTER_DEFAULTS['satellite_vision']}"
    for limit in ("Sentinel-2 imagery", "Name no ship", "cause", "forecast", "not in the findings"):
        assert limit in resolved, limit


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
