"""Source attribution (common/attribution.py): every adapter declares where its data comes from,
and that declaration is what readers see credited.

These are the build-breaking checks: a new adapter with no source, a source that is not a plain
https link, or a source the About page does not name, fails here. The pages that show the credit
are tested where they live (test_static_pages.py, test_public_api_handler.py,
test_trending_digest_handler.py, test_frontend_attribution.py).
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from common import attribution
from common.adapters.registry import ADAPTER_REGISTRY

ROOT = Path(__file__).resolve().parents[2]
ABOUT_HTML = ROOT / "frontend" / "about.html"

_ADAPTERS = sorted(ADAPTER_REGISTRY.items())


# --- every adapter declares its sources -----------------------------------------------------------


@pytest.mark.parametrize(("key", "adapter_cls"), _ADAPTERS, ids=[key for key, _ in _ADAPTERS])
def test_every_registered_adapter_declares_at_least_one_source(key, adapter_cls):
    assert adapter_cls.sources, (
        f"adapter {key!r} declares no sources. Every adapter must say where its data comes from: "
        "set `sources` on the class (see common/adapters/base.py) and add it to about.html."
    )


@pytest.mark.parametrize(("key", "adapter_cls"), _ADAPTERS, ids=[key for key, _ in _ADAPTERS])
def test_every_declared_source_is_a_plain_sentence_with_an_https_link(key, adapter_cls):
    for source in adapter_cls.sources:
        assert set(source) == {"text", "label", "url"}, source
        assert all(isinstance(value, str) and value == value.strip() and value for value in source.values())
        # The label is the part of the sentence that gets linked.
        assert source["label"] in source["text"]
        url = urlsplit(source["url"])
        assert url.scheme == "https" and url.netloc, source["url"]
        # Plain data, never markup: nothing a page would have to trust.
        assert not re.search(r"[<>\"']", source["text"] + source["url"]), source
    # Nothing is dropped on the way to a page: what is declared is what is shown.
    assert attribution.sources_for_adapter(key) == [dict(source) for source in adapter_cls.sources]


def test_an_adapter_added_without_sources_is_caught():
    """The check above is only worth having if the base class's default really is "none"."""
    from common.adapters.base import Adapter

    assert Adapter.sources == ()


def test_the_crypto_adapter_uses_coingeckos_own_wording_and_link():
    """CoinGecko's API terms prescribe the message ("Powered by CoinGecko") and their attribution
    guide the accepted forms and links; the adapter must use theirs, not a wording of ours."""
    first = attribution.sources_for_adapter("crypto_feed")[0]
    assert first["text"] == "Powered by CoinGecko API"
    assert "Powered by CoinGecko" in first["text"]
    assert first["url"] in ("https://www.coingecko.com", "https://www.coingecko.com/en/api")


def test_the_crypto_adapter_also_credits_what_its_headlines_come_from():
    crypto = attribution.sources_for_adapter("crypto_feed")
    web = attribution.sources_for_adapter("web_search")
    assert web and all(source in crypto for source in web)
    assert [source["url"] for source in web] == ["https://www.gdeltproject.org/"]


def test_github_trending_is_credited_with_a_link_to_the_trending_page():
    assert attribution.sources_for_adapter("github_trending") == [
        {
            "text": "Data sourced from GitHub Trending",
            "label": "GitHub Trending",
            "url": "https://github.com/trending",
        }
    ]


# --- resolving a topic's or an article's credit -----------------------------------------------------


def test_a_topic_carries_its_adapters_sources_and_an_unknown_adapter_carries_none():
    assert attribution.sources_for_topic({"adapter": "hacker_news"}) == [
        {"text": "Data sourced from Hacker News", "label": "Hacker News", "url": "https://news.ycombinator.com/"}
    ]
    for topic in (None, {}, {"adapter": None}, {"adapter": "gone"}, {"adapter": ["crypto_feed"]}):
        assert attribution.sources_for_topic(topic) == []


def test_the_union_of_several_topics_lists_each_source_once_in_first_met_order():
    adapters = ["web_search", "crypto_feed", "web_search", "no_such_adapter"]
    topics = [{"adapter": adapter} for adapter in adapters]
    assert [source["label"] for source in attribution.sources_for_topics(topics)] == [
        "GDELT Project",
        "CoinGecko API",
    ]


def test_sources_returned_are_copies_so_a_caller_cannot_edit_the_declaration():
    attribution.sources_for_adapter("crypto_feed")[0]["text"] = "changed"
    assert attribution.sources_for_adapter("crypto_feed")[0]["text"] == "Powered by CoinGecko API"


def _no_lookup(*_args):
    raise AssertionError("a stored credit must be used as it is, without looking anything up")


def test_an_article_keeps_the_credit_stored_on_it():
    stored = [{"text": "Data from X", "label": "X", "url": "https://x.example/"}]
    article = {"topic_id": "crypto", "attribution": stored}
    assert attribution.sources_for_article(article, get_topic=_no_lookup, list_topics=_no_lookup) == stored
    # "Nothing to credit" was stored: that is the answer.
    article = {"topic_id": "crypto", "attribution": []}
    assert attribution.sources_for_article(article, get_topic=_no_lookup, list_topics=_no_lookup) == []


def test_an_older_article_falls_back_to_its_topics_adapter():
    topics = {"crypto": {"topic_id": "crypto", "adapter": "crypto_feed"}}
    found = attribution.sources_for_article(
        {"topic_id": "crypto"}, get_topic=topics.get, list_topics=_no_lookup
    )
    assert found == attribution.sources_for_adapter("crypto_feed")
    # Its topic has since been deleted: nothing to credit, and no error.
    gone = attribution.sources_for_article({"topic_id": "gone"}, get_topic=topics.get, list_topics=_no_lookup)
    assert gone == []


def test_an_older_digest_credits_every_current_topics_sources():
    topics = [{"topic_id": "a", "adapter": "github_trending"}, {"topic_id": "b", "adapter": "hacker_news"}]
    found = attribution.sources_for_article(
        {"topic_id": "digest"}, get_topic=_no_lookup, list_topics=lambda: topics
    )
    assert [source["label"] for source in found] == ["GitHub Trending", "Hacker News"]


@pytest.mark.parametrize(
    "bad",
    [
        {"text": "Click here", "label": "here", "url": "javascript:alert(1)"},
        {"text": "Plain http", "label": "http", "url": "http://x.example/"},
        {"text": "Spaced", "label": "Spaced", "url": "https://x.example/ onmouseover=alert(1)"},
        {"text": "Label elsewhere", "label": "missing", "url": "https://x.example/"},
        {"text": "No url", "label": "No"},
        {"text": "", "label": "", "url": "https://x.example/"},
        {"text": 5, "label": "5", "url": "https://x.example/"},
        {"text": "x" * 500, "label": "x", "url": "https://x.example/"},
        "a string",
        None,
    ],
)
def test_a_malformed_credit_is_dropped_not_shown(bad):
    good = {"text": "Data from X", "label": "X", "url": "https://x.example/"}
    assert attribution.clean_sources([bad, good]) == [good]


def test_cleaning_keeps_only_the_three_fields():
    stored = [{"text": "Data from X", "label": "X", "url": "https://x.example/", "note": "internal"}]
    assert attribution.clean_sources(stored) == [{"text": "Data from X", "label": "X", "url": "https://x.example/"}]
    assert attribution.clean_sources("not a list") == [] and attribution.clean_sources(None) == []


# --- the About page -----------------------------------------------------------------------------


def _about_data_sources() -> str:
    html = ABOUT_HTML.read_text(encoding="utf-8")
    section = re.search(r'<h2 id="data-sources">.*?(?=<h2)', html, re.S)
    assert section, 'about.html needs a "Data sources" section (<h2 id="data-sources">)'
    return section.group(0)


def test_the_about_page_names_every_source_the_adapters_declare():
    """about.html is static, so its list is written by hand; this is what keeps it honest. For
    every declared source it must show the same sentence, with the same words linked to the same
    address, opening safely."""
    section = _about_data_sources()
    credits = {}
    for item in re.findall(r"<em>(.*?)</em>", section, re.S):
        link = re.search(r'<a href="([^"]+)"([^>]*)>(.*?)</a>', item, re.S)
        assert link, item
        assert 'rel="noopener noreferrer"' in link.group(2), item
        sentence = " ".join(unescape(re.sub(r"<[^>]+>", "", item)).split())
        credits[sentence] = (unescape(link.group(1)), " ".join(unescape(link.group(3)).split()))

    declared = attribution.all_declared_sources()
    assert declared
    for source in declared:
        assert credits.get(source["text"]) == (source["url"], source["label"]), (
            f"about.html's Data sources list does not credit {source['text']!r} "
            f"with {source['label']!r} linked to {source['url']}"
        )
    # And it credits nothing the code no longer uses.
    assert set(credits) == {source["text"] for source in declared}


def test_the_about_page_does_not_claim_an_endorsement():
    """CoinGecko's terms forbid wording that suggests they endorse or partner with the site."""
    section = " ".join(_about_data_sources().split())
    assert "not endorsed by, partnered with or affiliated with" in section
