from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

import pytest

from common import web_search
from common.web_search import (
    AgentCoreProvider,
    GdeltProvider,
    WebSearchProvider,
    natural_query,
    search_web,
)


def _article(title, url, domain="example.com", seendate="20260920T121500Z"):
    return {"title": title, "url": url, "domain": domain, "seendate": seendate, "language": "English"}


def _gdelt(articles):
    return patch("common.web_search.get_json_with_backoff", return_value={"articles": articles})


def test_gdelt_maps_articles_to_the_common_result_shape():
    with _gdelt([_article("Bitcoin rallies", "https://a.com/1", domain="a.com")]):
        results = GdeltProvider().search("bitcoin", max_results=5, max_age_hours=24)

    assert results == [
        {
            "title": "Bitcoin rallies",
            "url": "https://a.com/1",
            "source": "a.com",
            "published_at": "2026-09-20T12:15:00+00:00",
            "snippet": None,
        }
    ]


def test_gdelt_request_asks_for_recent_english_articles_and_overfetches():
    with _gdelt([]) as mock_get:
        GdeltProvider().search("bitcoin OR ethereum", max_results=15, max_age_hours=24)

    params = mock_get.call_args.kwargs["params"]
    assert params["query"] == "bitcoin OR ethereum sourcelang:english"
    assert params["timespan"] == "24h"
    assert params["maxrecords"] == 45  # 3x, so filtering still leaves enough
    assert params["mode"] == "artlist" and params["format"] == "json"
    assert params["sort"] == "datedesc"


def test_gdelt_does_not_double_append_a_language_filter():
    with _gdelt([]) as mock_get:
        GdeltProvider().search("bitcoin sourcelang:spanish", max_results=5, max_age_hours=24)

    assert mock_get.call_args.kwargs["params"]["query"] == "bitcoin sourcelang:spanish"


def test_gdelt_tolerates_missing_articles_key_and_bad_dates():
    with patch("common.web_search.get_json_with_backoff", return_value={}):
        assert GdeltProvider().search("x", max_results=5, max_age_hours=24) == []

    with _gdelt([_article("T", "https://a.com/1", seendate="garbage")]):
        results = GdeltProvider().search("x", max_results=5, max_age_hours=24)
    assert results[0]["published_at"] is None


def test_search_web_dedupes_by_url_ignoring_scheme_case_slash_and_fragment():
    with _gdelt(
        [
            _article("Story one", "http://Example.com/a/"),
            _article("Story one renamed", "https://example.com/a#comments"),
            _article("Story two", "https://example.com/b"),
        ]
    ):
        results = search_web("q", max_results=10)

    assert [r["title"] for r in results] == ["Story one", "Story two"]


def test_search_web_dedupes_syndicated_copies_by_normalized_title():
    with _gdelt(
        [
            _article("Bitcoin Hits $90K!", "https://a.com/1", domain="a.com"),
            _article("bitcoin hits 90k", "https://b.com/9", domain="b.com"),
        ]
    ):
        results = search_web("q", max_results=10)

    assert len(results) == 1 and results[0]["source"] == "a.com"


def test_search_web_title_keywords_drop_tangential_results():
    with _gdelt(
        [
            _article("New Interpol tool helps police find hidden wealth", "https://a.com/1"),
            _article("Ethereum staking hits a record", "https://a.com/2"),
            _article("Islamic Coin (ISLM) trading lower", "https://a.com/3"),
        ]
    ):
        results = search_web("q", max_results=10, title_keywords=["ethereum", "COIN"])

    assert [r["url"] for r in results] == ["https://a.com/2", "https://a.com/3"]


def test_search_web_caps_at_max_results_keeping_newest_first_order():
    with _gdelt([_article(f"Story {i}", f"https://a.com/{i}") for i in range(20)]):
        results = search_web("q", max_results=5)

    assert [r["title"] for r in results] == [f"Story {i}" for i in range(5)]


def test_search_web_skips_results_missing_a_title_or_url():
    with _gdelt([_article("", "https://a.com/1"), _article("Has title", ""), _article("OK", "https://a.com/2")]):
        results = search_web("q", max_results=10)

    assert [r["title"] for r in results] == ["OK"]


def test_search_web_empty_result_is_an_empty_list_not_an_error():
    with _gdelt([]):
        assert search_web("q") == []


def test_unknown_provider_raises_with_the_known_names():
    with pytest.raises(ValueError, match="unknown web search provider 'nope'.*gdelt"):
        search_web("q", provider="nope")


def test_provider_can_be_selected_by_env_var_and_new_providers_plug_in(monkeypatch):
    class FakeProvider(WebSearchProvider):
        def search(self, query, *, max_results, max_age_hours):
            return [
                {
                    "title": f"{query} news",
                    "url": "https://fake.test/1",
                    "source": "fake.test",
                    "published_at": None,
                    "snippet": "a snippet",
                }
            ]

    monkeypatch.setitem(web_search.PROVIDERS, "fake", FakeProvider)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "fake")

    results = search_web("solana")

    assert results[0]["title"] == "solana news"
    assert results[0]["snippet"] == "a snippet"


def test_explicit_provider_argument_beats_the_env_var(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "nope")

    with _gdelt([_article("T", "https://a.com/1")]):
        assert len(search_web("q", provider="gdelt")) == 1


def test_search_web_title_keywords_match_whole_words_only():
    with _gdelt(
        [
            _article("We worked together on a new method", "https://a.com/1"),
            _article("Spot ETH ETF flows turn positive", "https://a.com/2"),
            _article("Cryptocurrency exchange freezes withdrawals", "https://a.com/3"),
        ]
    ):
        results = search_web("q", max_results=10, title_keywords=["eth", "crypto*"])

    assert [r["url"] for r in results] == ["https://a.com/2", "https://a.com/3"]


# --- the time budget (`deadline`) ---------------------------------------------------------------


def test_without_a_deadline_gdelt_keeps_its_normal_timeout_and_no_budget():
    with _gdelt([]) as mock_get:
        GdeltProvider().search("x", max_results=5, max_age_hours=24)

    assert mock_get.call_args.kwargs["timeout"] == 30.0
    assert mock_get.call_args.kwargs["deadline"] is None


def test_a_deadline_caps_the_request_timeout_and_is_passed_on_to_the_retries():
    with _gdelt([]) as mock_get, patch("common.web_search.time.monotonic", return_value=100.0):
        GdeltProvider().search("x", max_results=5, max_age_hours=24, deadline=108.0)

    assert mock_get.call_args.kwargs["timeout"] == 8.0
    assert mock_get.call_args.kwargs["deadline"] == 108.0


def test_a_deadline_already_past_raises_without_a_request():
    with (
        _gdelt([]) as mock_get,
        patch("common.web_search.time.monotonic", return_value=100.0),
        pytest.raises(TimeoutError),
    ):
        GdeltProvider().search("x", max_results=5, max_age_hours=24, deadline=99.0)

    mock_get.assert_not_called()


def test_search_web_passes_a_deadline_through_to_the_provider():
    with _gdelt([]) as mock_get, patch("common.web_search.time.monotonic", return_value=0.0):
        search_web("x", deadline=5.0)

    assert mock_get.call_args.kwargs["deadline"] == 5.0


def test_a_provider_without_a_deadline_parameter_still_works_when_none_is_given(monkeypatch):
    class OldProvider(WebSearchProvider):
        def search(self, query, *, max_results, max_age_hours):
            return []

    monkeypatch.setitem(web_search.PROVIDERS, "old", OldProvider)

    assert search_web("x", provider="old") == []


# --- AgentCore web search (the fallback) --------------------------------------------------------

GATEWAY = "https://bb-web-search-abc.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp"


@pytest.fixture
def agentcore(monkeypatch):
    monkeypatch.setenv("AGENTCORE_WEB_SEARCH_URL", GATEWAY)
    monkeypatch.setenv("AGENTCORE_WEB_SEARCH_REGION", "ap-northeast-1")
    monkeypatch.setenv("AGENTCORE_WEB_SEARCH_TOOL", "web-search___WebSearch")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


class _Reply:
    def __init__(self, payload, content_type="application/json", status=200):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise web_search.requests.HTTPError(f"{self.status_code} Client Error")


def _tool_result(rows, structured=False):
    body = {"id": "x", "results": rows}
    result = {"isError": False, "content": [{"type": "text", "text": json.dumps(body)}]}
    if structured:
        result["structuredContent"] = body
    return {"jsonrpc": "2.0", "id": "search", "result": result}


ROW = {
    "title": "World of Warcraft: Forever is coming in November",
    "url": "https://www.polygon.com/wow-forever",
    "publishedDate": "2026-09-26",
    "text": "Blizzard's new server type...",
}


def _post(reply):
    return patch("common.web_search.requests.post", return_value=reply)


def test_agentcore_signs_one_mcp_tool_call_with_the_age_window(agentcore):
    with _post(_Reply(_tool_result([ROW]))) as mock_post:
        AgentCoreProvider().search('"World of Warcraft" Forever', max_results=5, max_age_hours=24)

    url, kwargs = mock_post.call_args.args[0], mock_post.call_args.kwargs
    assert url == GATEWAY
    assert kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert "ap-northeast-1/bedrock-agentcore/aws4_request" in kwargs["headers"]["Authorization"]
    body = json.loads(kwargs["data"])
    assert body["method"] == "tools/call" and body["params"]["name"] == "web-search___WebSearch"
    args = body["params"]["arguments"]
    assert args["query"] == '"World of Warcraft" Forever' and args["maxResults"] == 15
    window = args["filters"]["publishedDateFilter"]
    start = datetime.fromisoformat(window["from"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(window["to"].replace("Z", "+00:00"))
    assert (end - start).total_seconds() == 24 * 3600


def test_agentcore_results_take_the_common_shape_with_snippets(agentcore):
    with _post(_Reply(_tool_result([ROW]))):
        (result,) = AgentCoreProvider().search("q", max_results=5, max_age_hours=24)

    assert result == {
        "title": "World of Warcraft: Forever is coming in November",
        "url": "https://www.polygon.com/wow-forever",
        "source": "polygon.com",
        "published_at": "2026-09-26T00:00:00+00:00",
        "snippet": "Blizzard's new server type...",
    }


def test_agentcore_reads_structured_content_and_event_streams(agentcore):
    with _post(_Reply(_tool_result([ROW], structured=True))):
        assert len(AgentCoreProvider().search("q", max_results=5, max_age_hours=24)) == 1

    stream = "event: message\ndata: " + json.dumps(_tool_result([ROW, ROW])) + "\n\n"
    with _post(_Reply(stream, content_type="text/event-stream")):
        assert len(AgentCoreProvider().search("q", max_results=5, max_age_hours=24)) == 2


@pytest.mark.parametrize(
    "reply, match",
    [
        (_Reply({"jsonrpc": "2.0", "error": {"code": -32602, "message": "bad"}}), "failed"),
        (_Reply({"result": {"isError": True, "content": [{"type": "text", "text": "quota"}]}}), "quota"),
        (_Reply({}, status=403), "403"),
    ],
)
def test_agentcore_failures_raise(agentcore, reply, match):
    with _post(reply), pytest.raises(Exception, match=match):
        AgentCoreProvider().search("q", max_results=5, max_age_hours=24)


def test_agentcore_unconfigured_raises_without_a_request(monkeypatch):
    monkeypatch.delenv("AGENTCORE_WEB_SEARCH_URL", raising=False)
    with _post(_Reply({})) as mock_post, pytest.raises(RuntimeError, match="not configured"):
        AgentCoreProvider().search("q", max_results=5, max_age_hours=24)
    mock_post.assert_not_called()


def test_agentcore_respects_a_deadline(agentcore):
    with (
        _post(_Reply(_tool_result([]))) as mock_post,
        patch("common.web_search.time.monotonic", return_value=10.0),
    ):
        AgentCoreProvider().search("q", max_results=5, max_age_hours=24, deadline=13.0)
        assert mock_post.call_args.kwargs["timeout"] == 3.0
        with pytest.raises(TimeoutError):
            AgentCoreProvider().search("q", max_results=5, max_age_hours=24, deadline=9.0)


def test_natural_query_drops_gdelt_syntax_and_fits_200_characters():
    assert natural_query('("AI" OR Nvidia) sourcelang:english') == '"AI" Nvidia'
    long = " OR ".join(f'"phrase number {n}"' for n in range(40))
    short = natural_query(long)
    assert 0 < len(short) <= 200 and short.count('"') % 2 == 0 and " OR " not in short


# --- the fallback ---------------------------------------------------------------------------------


def test_a_failed_gdelt_search_falls_back_to_agentcore(agentcore, capsys):
    with (
        patch("common.web_search.get_json_with_backoff", side_effect=RuntimeError("429 Too Many Requests")),
        _post(_Reply(_tool_result([ROW]))),
    ):
        results = search_web("wow", max_results=5)

    assert [r["url"] for r in results] == [ROW["url"]]
    assert "trying the AgentCore web search instead" in capsys.readouterr().out


def test_with_a_fallback_gdelt_gets_a_short_budget(agentcore):
    with _gdelt([]) as mock_get, patch("common.web_search.time.monotonic", return_value=100.0):
        search_web("x")

    assert mock_get.call_args.kwargs["deadline"] == 100.0 + web_search.PRIMARY_BUDGET_WITH_FALLBACK_SECONDS


def test_the_fallback_keeps_part_of_a_callers_deadline_for_itself(agentcore):
    with _gdelt([]) as mock_get, patch("common.web_search.time.monotonic", return_value=100.0):
        search_web("x", deadline=115.0)

    assert mock_get.call_args.kwargs["deadline"] == 115.0 - web_search.FALLBACK_RESERVE_SECONDS


def test_a_gdelt_search_that_works_never_calls_agentcore(agentcore):
    with _gdelt([_article("Story", "https://a.com/1")]), _post(_Reply({})) as mock_post:
        assert len(search_web("x")) == 1
    mock_post.assert_not_called()


def test_without_the_gateway_a_gdelt_failure_is_raised_as_before(monkeypatch):
    monkeypatch.delenv("AGENTCORE_WEB_SEARCH_URL", raising=False)
    with (
        patch("common.web_search.get_json_with_backoff", side_effect=RuntimeError("429")),
        pytest.raises(RuntimeError, match="429"),
    ):
        search_web("x")


def test_a_topic_can_ask_for_agentcore_directly(agentcore):
    with _gdelt([]) as mock_get, _post(_Reply(_tool_result([ROW]))):
        assert len(search_web("x", provider="agentcore")) == 1
    mock_get.assert_not_called()


# --- web search usage is counted in the weekly Stats ------------------------------------------


@pytest.fixture
def recorders():
    with (
        patch("common.web_search.record_web_search_query") as query,
        patch("common.web_search.record_web_search_fallback") as fallback,
    ):
        yield query, fallback


def test_a_query_the_gateway_answered_is_counted_once(agentcore, recorders):
    query, fallback = recorders
    with _post(_Reply(_tool_result([ROW]))):
        AgentCoreProvider().search("q", max_results=5, max_age_hours=24)

    query.assert_called_once_with()
    fallback.assert_not_called()


def test_a_tool_error_inside_a_2xx_is_still_counted(agentcore, recorders):
    query, _ = recorders
    reply = _Reply({"result": {"isError": True, "content": [{"type": "text", "text": "quota"}]}})
    with _post(reply), pytest.raises(RuntimeError):
        AgentCoreProvider().search("q", max_results=5, max_age_hours=24)

    query.assert_called_once_with()


def test_a_request_the_gateway_refused_is_not_counted(agentcore, recorders):
    query, _ = recorders
    with _post(_Reply({}, status=403)), pytest.raises(Exception, match="403"):
        AgentCoreProvider().search("q", max_results=5, max_age_hours=24)

    query.assert_not_called()


def test_an_unconfigured_gateway_is_not_counted(monkeypatch, recorders):
    query, _ = recorders
    monkeypatch.delenv("AGENTCORE_WEB_SEARCH_URL", raising=False)
    with pytest.raises(RuntimeError):
        AgentCoreProvider().search("q", max_results=5, max_age_hours=24)

    query.assert_not_called()


def test_a_gdelt_fallback_is_counted_along_with_its_query(agentcore, recorders):
    query, fallback = recorders
    with (
        patch("common.web_search.get_json_with_backoff", side_effect=RuntimeError("429")),
        _post(_Reply(_tool_result([ROW]))),
    ):
        search_web("wow")

    fallback.assert_called_once_with()
    query.assert_called_once_with()


def test_a_working_gdelt_search_counts_nothing(agentcore, recorders):
    query, fallback = recorders
    with _gdelt([_article("Story", "https://a.com/1")]), _post(_Reply({})):
        search_web("x")

    query.assert_not_called()
    fallback.assert_not_called()


def test_a_stats_write_failure_never_breaks_the_search(agentcore, monkeypatch, capsys):
    # the real recorders, with no Stats table configured: the write fails and is only logged
    monkeypatch.delenv("STATS_CURRENT_TABLE", raising=False)
    with (
        patch("common.web_search.get_json_with_backoff", side_effect=RuntimeError("429")),
        _post(_Reply(_tool_result([ROW]))),
    ):
        results = search_web("wow")

    assert len(results) == 1
    assert "stats_tracking: could not record" in capsys.readouterr().out
