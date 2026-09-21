from __future__ import annotations

from unittest.mock import Mock, patch

from common.adapters.hacker_news import HackerNewsAdapter


def _mock_json_response(payload) -> Mock:
    response = Mock()
    response.json = Mock(return_value=payload)
    response.raise_for_status = Mock()
    return response


def _story_item(story_id, title, score, url=None, by="someone"):
    return {
        "id": story_id,
        "type": "story",
        "title": title,
        "score": score,
        "by": by,
        **({"url": url} if url else {}),
    }


def test_fetch_state_parses_stories_via_ids_then_items():
    ids_response = _mock_json_response([1, 2])
    item_responses = [
        _mock_json_response(_story_item(1, "Story One", 100, url="https://example.com/one")),
        _mock_json_response(_story_item(2, "Story Two", 50)),
    ]

    adapter = HackerNewsAdapter()
    with patch(
        "common.adapters.hacker_news.requests.get",
        side_effect=[ids_response, *item_responses],
    ) as mock_get:
        state = adapter.fetch_state({"adapter_config": {}})

    assert mock_get.call_count == 3
    assert mock_get.call_args_list[0].args[0] == "https://hacker-news.firebaseio.com/v0/topstories.json"
    assert mock_get.call_args_list[1].args[0] == "https://hacker-news.firebaseio.com/v0/item/1.json"

    assert "fetched_at" in state
    assert len(state["stories"]) == 2

    first, second = state["stories"]
    assert first == {
        "id": 1,
        "title": "Story One",
        "url": "https://example.com/one",
        "score": 100,
        "by": "someone",
    }
    # No `url` field on the raw item -> falls back to the HN discussion link.
    assert second["url"] == "https://news.ycombinator.com/item?id=2"


def test_fetch_state_respects_configured_limit():
    ids_response = _mock_json_response([1, 2, 3])
    item_response = _mock_json_response(_story_item(1, "Story One", 10))

    adapter = HackerNewsAdapter()
    with patch(
        "common.adapters.hacker_news.requests.get",
        side_effect=[ids_response, item_response],
    ) as mock_get:
        state = adapter.fetch_state({"adapter_config": {"limit": 1}})

    assert mock_get.call_count == 2  # topstories + exactly 1 item, not 3
    assert len(state["stories"]) == 1


def test_fetch_state_skips_non_story_items():
    ids_response = _mock_json_response([1, 2])
    item_responses = [
        _mock_json_response({"id": 1, "type": "job", "title": "A job posting"}),
        _mock_json_response(_story_item(2, "Real Story", 5)),
    ]

    adapter = HackerNewsAdapter()
    with patch(
        "common.adapters.hacker_news.requests.get",
        side_effect=[ids_response, *item_responses],
    ):
        state = adapter.fetch_state({"adapter_config": {}})

    assert [s["title"] for s in state["stories"]] == ["Real Story"]


def test_material_diff_true_on_first_observation():
    adapter = HackerNewsAdapter()
    new_state = {"stories": [{"id": 1, "title": "a", "url": "u", "score": 1, "by": "x"}]}

    changed, summary = adapter.material_diff(None, new_state)

    assert changed is True
    assert "initial observation" in summary


def test_material_diff_false_on_pure_reorder():
    story_a = {"id": 1, "title": "a", "url": "ua", "score": 10, "by": "x"}
    story_b = {"id": 2, "title": "b", "url": "ub", "score": 20, "by": "y"}
    old_state = {"stories": [story_a, story_b]}
    new_state = {"stories": [story_b, story_a]}

    adapter = HackerNewsAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is False
    assert summary == "no new information"


def test_material_diff_a_story_already_reported_is_not_new_when_it_returns():
    story_a = {"id": 1, "title": "a", "url": "ua", "score": 10, "by": "x"}
    story_b = {"id": 2, "title": "b", "url": "ub", "score": 20, "by": "y"}
    # the seen-set round-trips through JSON, so its keys are strings
    old_state = {"stories": [story_a], "_seen": {"1": "2026-09-20", "2": "2026-09-20"}}
    new_state = {"stories": [story_a, story_b]}  # story 2 left the list earlier and is back

    assert HackerNewsAdapter().material_diff(old_state, new_state) == (False, "no new information")


def test_material_diff_a_story_leaving_the_list_is_not_news_but_a_new_one_is():
    story_a = {"id": 1, "title": "a", "url": "ua", "score": 10, "by": "x"}
    story_b = {"id": 2, "title": "b", "url": "ub", "score": 20, "by": "y"}
    story_c = {"id": 3, "title": "c", "url": "uc", "score": 5, "by": "z"}
    adapter = HackerNewsAdapter()

    assert adapter.material_diff({"stories": [story_a, story_b]}, {"stories": [story_a]})[0] is False
    changed, summary = adapter.material_diff(
        {"stories": [story_a, story_b]}, {"stories": [story_a, story_c]}
    )
    assert changed is True and "entered: c" in summary and "left: b" in summary


def test_material_diff_true_when_story_set_changes():
    old_state = {"stories": [{"id": 1, "title": "a", "url": "ua", "score": 10, "by": "x"}]}
    new_state = {"stories": [{"id": 2, "title": "b", "url": "ub", "score": 5, "by": "y"}]}

    adapter = HackerNewsAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is True
    assert "entered: b" in summary
    assert "left: a" in summary


def test_material_diff_true_on_large_score_jump():
    old_state = {"stories": [{"id": 1, "title": "a", "url": "ua", "score": 100, "by": "x"}]}
    new_state = {"stories": [{"id": 1, "title": "a", "url": "ua", "score": 500, "by": "x"}]}

    adapter = HackerNewsAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is True
    assert "score jumps" in summary


def test_material_diff_false_on_small_score_wobble():
    old_state = {"stories": [{"id": 1, "title": "a", "url": "ua", "score": 1000, "by": "x"}]}
    new_state = {"stories": [{"id": 1, "title": "a", "url": "ua", "score": 1010, "by": "x"}]}

    adapter = HackerNewsAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is False


def test_source_refs_one_per_story():
    new_state = {
        "fetched_at": "2026-09-13T00:00:00+00:00",
        "stories": [
            {"id": 1, "title": "a", "url": "https://example.com/a", "score": 1, "by": "x"},
            {"id": 2, "title": "b", "url": "https://example.com/b", "score": 2, "by": "y"},
        ],
    }

    adapter = HackerNewsAdapter()
    refs = adapter.source_refs(new_state)

    assert refs == [
        {"url": "https://example.com/a", "title": "a", "accessed_at": "2026-09-13T00:00:00+00:00"},
        {"url": "https://example.com/b", "title": "b", "accessed_at": "2026-09-13T00:00:00+00:00"},
    ]


# --- topic relevance filtering -----------------------------------------------------

NOISY_FRONT_PAGE = [
    _story_item(1, "Critical ransomware campaign hits hospitals", 300),
    _story_item(2, "I built a backyard pizza oven", 250),
    _story_item(3, "What we know about the UFO sightings", 200),
    _story_item(4, "New zero-day in a popular VPN", 150),
]


def _fetch_with_config(adapter_config, items=NOISY_FRONT_PAGE):
    responses = [
        _mock_json_response([item["id"] for item in items]),
        *[_mock_json_response(item) for item in items],
    ]
    with patch("common.adapters.hacker_news.requests.get", side_effect=responses) as mock_get:
        state = HackerNewsAdapter().fetch_state({"adapter_config": adapter_config})
    return state, mock_get


def test_keywords_drop_off_topic_stories_before_they_reach_a_summary():
    state, _ = _fetch_with_config({"keywords": ["ransomware", "zero-day", "vulnerability"]})

    assert [s["title"] for s in state["stories"]] == [
        "Critical ransomware campaign hits hospitals",
        "New zero-day in a popular VPN",
    ]
    assert state["off_topic_dropped"] == 2


def test_without_keywords_nothing_is_filtered_and_no_counter_is_added():
    state, _ = _fetch_with_config({})

    assert len(state["stories"]) == 4
    assert "off_topic_dropped" not in state


def test_a_keyword_filter_widens_the_candidate_pool_unless_limit_is_set():
    ids = list(range(1, 101))
    ids_response = _mock_json_response(ids)
    items = [_mock_json_response(_story_item(i, f"Story {i}", 1)) for i in range(1, 61)]

    with patch(
        "common.adapters.hacker_news.requests.get", side_effect=[ids_response, *items]
    ) as mock_get:
        HackerNewsAdapter().fetch_state({"adapter_config": {"keywords": ["story"]}})
    assert mock_get.call_count == 1 + 60  # 60 candidates examined, not the usual 25

    ids_response = _mock_json_response(ids)
    items = [_mock_json_response(_story_item(i, f"Story {i}", 1)) for i in range(1, 11)]
    with patch(
        "common.adapters.hacker_news.requests.get", side_effect=[ids_response, *items]
    ) as mock_get:
        HackerNewsAdapter().fetch_state({"adapter_config": {"keywords": ["story"], "limit": 10}})
    assert mock_get.call_count == 1 + 10


def test_kept_stories_are_capped_when_filtering():
    items = [_story_item(i, f"Security story {i}", 1) for i in range(1, 41)]

    state, _ = _fetch_with_config({"keywords": ["security"], "limit": 40}, items=items)

    assert len(state["stories"]) == 25


def test_nothing_relevant_is_never_material_so_no_model_call_is_wasted():
    adapter = HackerNewsAdapter()
    empty = {"stories": [], "off_topic_dropped": 25, "fetched_at": "2026-09-20T00:00:00+00:00"}
    old = {"stories": [{"id": 1, "title": "Old", "score": 1, "url": "u"}]}

    assert adapter.material_diff(None, empty) == (False, "no relevant stories to report")
    assert adapter.material_diff(old, empty) == (False, "no relevant stories to report")
