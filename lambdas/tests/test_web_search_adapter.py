from __future__ import annotations

from unittest.mock import patch

import pytest

from common.adapters.web_search import (
    WebSearchAdapter,
    configured_queries,
    default_query_for_topic,
)


def _result(n, published_at="2026-09-20T12:00:00+00:00"):
    return {
        "title": f"Story {n}",
        "url": f"https://a.com/{n}",
        "source": "a.com",
        "published_at": published_at,
        "snippet": None,
    }


def _topic(**adapter_config):
    return {"topic_id": "t", "adapter": "web_search", "adapter_config": adapter_config}


def test_configured_queries_accepts_list_or_single_query_and_drops_blanks():
    assert configured_queries({"queries": ["a", " ", "b"]}) == ["a", "b"]
    assert configured_queries({"query": "solo"}) == ["solo"]
    assert configured_queries({}) == []


def test_fetch_state_requires_a_query():
    with pytest.raises(ValueError, match="queries"):
        WebSearchAdapter().fetch_state(_topic())


def test_fetch_state_merges_queries_dedupes_and_sorts_newest_first():
    per_query = {
        "one": [_result(1, "2026-09-20T10:00:00+00:00"), _result(2, "2026-09-20T12:00:00+00:00")],
        "two": [_result(2, "2026-09-20T12:00:00+00:00"), _result(3, "2026-09-20T11:00:00+00:00")],
    }
    with patch(
        "common.adapters.web_search.search_web", side_effect=lambda q, **kw: per_query[q]
    ):
        state = WebSearchAdapter().fetch_state(_topic(queries=["one", "two"]))

    assert [r["url"] for r in state["results"]] == [
        "https://a.com/2",
        "https://a.com/3",
        "https://a.com/1",
    ]
    assert state["queries"] == ["one", "two"]
    assert state["fetched_at"]


def test_fetch_state_passes_config_through_and_clamps_max_results():
    with patch("common.adapters.web_search.search_web", return_value=[]) as mock_search:
        WebSearchAdapter().fetch_state(
            _topic(
                query="q",
                max_results=500,
                max_age_hours=6,
                title_keywords=["k"],
                provider="gdelt",
            )
        )

    mock_search.assert_called_once_with(
        "q", max_results=25, max_age_hours=6, title_keywords=["k"], provider="gdelt"
    )


def test_fetch_state_caps_merged_results_at_max_results():
    with patch(
        "common.adapters.web_search.search_web", return_value=[_result(i) for i in range(20)]
    ):
        state = WebSearchAdapter().fetch_state(_topic(query="q", max_results=4))

    assert len(state["results"]) == 4


def test_first_observation_is_always_material():
    changed, summary = WebSearchAdapter().material_diff(None, {"results": [_result(1)]})

    assert changed is True and "initial observation" in summary


def test_few_new_results_are_not_material_but_enough_are():
    adapter = WebSearchAdapter()
    old = {"results": [_result(1), _result(2)]}

    few = {"results": [_result(1), _result(2), _result(3), _result(4)]}  # 2 new < default 3
    assert adapter.material_diff(old, few) == (False, "no material change")

    enough = {"results": [_result(i) for i in range(1, 6)]}  # 3 new
    changed, summary = adapter.material_diff(old, enough)
    assert changed is True
    assert summary.startswith("3 new results: Story 3; Story 4; Story 5")


def test_threshold_comes_from_the_snapshot_because_material_diff_cannot_see_config():
    adapter = WebSearchAdapter()
    old = {"results": [_result(1)]}
    new = {"results": [_result(1), _result(2)], "min_new_results": 1}

    assert adapter.material_diff(old, new)[0] is True

    with patch("common.adapters.web_search.search_web", return_value=[]):
        state = adapter.fetch_state(_topic(query="q", min_new_results=7))
    assert state["min_new_results"] == 7


def test_source_refs_cover_every_result():
    refs = WebSearchAdapter().source_refs(
        {"results": [_result(1), _result(2)], "fetched_at": "2026-09-20T12:00:00+00:00"}
    )

    assert refs == [
        {"url": "https://a.com/1", "title": "Story 1", "accessed_at": "2026-09-20T12:00:00+00:00"},
        {"url": "https://a.com/2", "title": "Story 2", "accessed_at": "2026-09-20T12:00:00+00:00"},
    ]


def test_uses_the_generic_summary_prompt():
    assert WebSearchAdapter().build_summary_prompt({}, "diff", {"results": []}) is None


# --- relevance defaults -------------------------------------------------------------


def _keywords_passed(**adapter_config):
    with patch("common.adapters.web_search.search_web", return_value=[]) as mock_search:
        WebSearchAdapter().fetch_state(_topic(**adapter_config))
    return [call.kwargs["title_keywords"] for call in mock_search.call_args_list]


def test_without_title_keywords_each_query_filters_on_its_own_terms():
    passed = _keywords_passed(queries=["(ransomware OR \"zero-day\")", "supply chain attack"])

    assert passed == [["zero-day", "ransomware"], ["supply", "chain", "attack"]]


def test_explicit_title_keywords_override_the_derived_ones():
    assert _keywords_passed(query="ransomware", title_keywords=["breach"]) == [["breach"]]


def test_an_empty_title_keywords_list_turns_the_filter_off():
    assert _keywords_passed(query="ransomware", title_keywords=[]) == [None]


def test_a_query_with_no_usable_terms_gets_no_filter():
    assert _keywords_passed(query="a b") == [None]


def test_no_results_is_never_material_so_no_model_call_is_wasted():
    adapter = WebSearchAdapter()

    assert adapter.material_diff(None, {"results": []}) == (False, "no relevant results to report")
    old = {"results": [_result(1)]}
    assert adapter.material_diff(old, {"results": []}) == (False, "no relevant results to report")


# --- default query from the topic name -------------------------------------------------


def test_default_query_ors_the_meaningful_words_of_the_topic_name():
    query = default_query_for_topic({"name": "Cybersecurity & Infrastructure Threats"})

    assert query == "(Cybersecurity OR Infrastructure OR Threats)"


def test_default_query_drops_stopwords_short_words_and_repeats():
    assert default_query_for_topic({"name": "The Art of the Deal for AI AI"}) == "(Art OR Deal)"


def test_default_query_for_a_single_word_is_that_word_and_keeps_hyphenated_terms():
    assert default_query_for_topic({"name": "Ransomware"}) == "Ransomware"
    assert default_query_for_topic({"name": "Zero-day exploits"}) == "(Zero-day OR exploits)"


def test_default_query_falls_back_to_the_topic_id_when_there_is_no_name():
    assert default_query_for_topic({"topic_id": "security-trends"}) == "(security OR trends)"
    assert default_query_for_topic({"topic_id": "x", "name": " "}) is None
    assert default_query_for_topic({}) is None


def test_default_query_is_capped():
    name = " ".join(f"word{i}" for i in range(20))

    assert default_query_for_topic({"name": name}).count(" OR ") == 7


def test_fetch_state_without_queries_searches_on_the_topic_name():
    topic = {"topic_id": "sec", "name": "Cybersecurity Threats", "adapter_config": {}}

    with patch("common.adapters.web_search.search_web", return_value=[_result(1)]) as mock_search:
        state = WebSearchAdapter().fetch_state(topic)

    assert state["queries"] == ["(Cybersecurity OR Threats)"]
    # and each result must mention one of those words (the default relevance filter)
    assert mock_search.call_args.kwargs["title_keywords"] == ["Cybersecurity", "Threats"]


def test_a_configured_query_beats_the_name_fallback():
    topic = {
        "topic_id": "sec",
        "name": "Cybersecurity Threats",
        "adapter_config": {"query": "ransomware"},
    }

    with patch("common.adapters.web_search.search_web", return_value=[]) as mock_search:
        WebSearchAdapter().fetch_state(topic)

    assert mock_search.call_args.args[0] == "ransomware"
