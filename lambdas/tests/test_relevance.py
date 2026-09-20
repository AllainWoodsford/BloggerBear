from __future__ import annotations

import pytest

from common.adapters.crypto_feed import CRYPTO_TITLE_KEYWORDS
from common.relevance import (
    UNKNOWN_TOPIC,
    draft_relevance_boundary,
    ideation_relevance_rule,
    keywords_from_query,
    matches_keywords,
    normalize_keywords,
    research_relevance_rule,
    topic_label,
)

# --- topic_label ---------------------------------------------------------------


def test_topic_label_prefers_name_then_id_then_a_placeholder():
    assert topic_label({"topic_id": "sec", "name": "Security & Hacker News"}) == "Security & Hacker News"
    assert topic_label({"topic_id": "sec", "name": ""}) == "sec"
    assert topic_label({"topic_id": "sec", "name": None}) == "sec"
    assert topic_label({}) == UNKNOWN_TOPIC


# --- keyword matching ----------------------------------------------------------


def test_normalize_keywords_accepts_a_list_or_one_string_and_drops_junk():
    assert normalize_keywords(["a", " b ", "", "  ", 3, None]) == ["a", "b"]
    assert normalize_keywords("solo") == ["solo"]
    assert normalize_keywords(None) == []
    assert normalize_keywords({"not": "a list"}) == []


def test_no_keywords_means_no_filter():
    assert matches_keywords("anything at all", None)
    assert matches_keywords("anything at all", [])
    assert matches_keywords("anything at all", ["", "  "])


def test_keywords_match_whole_words_case_insensitively():
    assert matches_keywords("Critical RANSOMWARE campaign hits hospitals", ["ransomware"])
    assert matches_keywords("New zero-day exploited in the wild", ["zero-day"])
    assert not matches_keywords("A recipe for homemade pizza", ["ransomware", "exploit"])


def test_a_keyword_does_not_match_inside_a_longer_word():
    assert not matches_keywords("We work together on a method", ["eth"])
    assert not matches_keywords("The ethics of AI", ["eth"])
    assert matches_keywords("ETH climbs as ether demand grows", ["eth"])
    assert not matches_keywords("A coincidence", ["coin"])


def test_a_simple_plural_still_matches_and_a_trailing_star_is_a_prefix():
    assert matches_keywords("Three new exploits disclosed", ["exploit"])
    assert not matches_keywords("Exploitation trends", ["exploit"])
    assert matches_keywords("Exploitation trends", ["exploit*"])
    assert matches_keywords("Cryptocurrency exchange hacked", ["crypto*"])


def test_keywords_with_symbols_and_phrases_work():
    assert matches_keywords("Rewriting the parser in C++ this week", ["c++"])
    assert matches_keywords("A remote code execution flaw", ["remote code execution"])


def test_the_crypto_title_keywords_keep_matching_crypto_and_reject_lookalikes():
    assert matches_keywords("Cryptocurrency exchange freezes withdrawals", CRYPTO_TITLE_KEYWORDS)
    assert matches_keywords("Coinbase lists a new token", CRYPTO_TITLE_KEYWORDS)
    assert matches_keywords("Spot ETH ETF approved", CRYPTO_TITLE_KEYWORDS)
    assert not matches_keywords("A method to work together", CRYPTO_TITLE_KEYWORDS)
    assert not matches_keywords("New Interpol tool helps police", CRYPTO_TITLE_KEYWORDS)


# --- keywords_from_query ---------------------------------------------------------


def test_keywords_from_query_drops_operators_fields_negations_and_short_words():
    query = '(ransomware OR "zero-day" OR CVE) AND -pizza sourcecountry:US NOT be'

    assert keywords_from_query(query) == ["zero-day", "ransomware", "CVE"]


def test_keywords_from_query_handles_plain_and_empty_queries():
    assert keywords_from_query("cybersecurity breach") == ["cybersecurity", "breach"]
    assert keywords_from_query("") == []
    assert keywords_from_query("a b") == []


# --- guardrail prompts ----------------------------------------------------------


@pytest.mark.parametrize("name", ["Security & Hacker News", "Urban Beekeeping", "Unknown Topic"])
@pytest.mark.parametrize(
    "builder", [ideation_relevance_rule, draft_relevance_boundary, research_relevance_rule]
)
def test_every_guardrail_is_anchored_to_whichever_topic_is_active(builder, name):
    text = builder(name)

    assert f"'{name}'" in text


def test_the_guardrails_name_only_the_active_topic():
    text = (
        ideation_relevance_rule("Alpha")
        + draft_relevance_boundary("Alpha")
        + research_relevance_rule("Alpha")
    )

    assert "Beta" not in text
    assert "security" not in text.lower()  # no hardcoded topic knowledge


def test_ideation_rule_says_to_ignore_or_reframe_noise():
    text = ideation_relevance_rule("T")

    assert text.startswith("CRITICAL RELEVANCE RULE:")
    assert "completely ignore" in text and "aggressively reframe" in text
    assert "Do not wander off-topic." in text


def test_draft_boundary_forbids_literal_readings_of_noisy_inputs():
    text = draft_relevance_boundary("T")

    assert text.startswith("CRITICAL RELEVANCE BOUNDARY:")
    assert "cookbook" in text and "literal" in text
    assert "absolute thematic integrity" in text


def test_research_rule_allows_saying_nothing_is_relevant():
    text = research_relevance_rule("T")

    assert "If nothing in the data is relevant" in text
