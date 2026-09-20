from __future__ import annotations

from unittest.mock import patch

import pytest

from common.adapters.web_search import WebSearchAdapter, configured_queries


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
