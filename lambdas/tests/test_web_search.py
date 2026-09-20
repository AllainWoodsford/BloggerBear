from __future__ import annotations

from unittest.mock import patch

import pytest

from common import web_search
from common.web_search import GdeltProvider, WebSearchProvider, search_web


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
