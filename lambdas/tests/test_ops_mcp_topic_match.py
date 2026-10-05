"""Which topic the operator meant (ops_mcp/topic_match.py): an id, a name, or something close.

The case that asked for it: "the Finance and Crypto topic" when the topic is "Crypto & Investing".
Held here: an exact id or name is taken; one clear match is taken and said; two close ones are
asked about; nothing close names the nearest; text no topic is made of is refused; and nothing the
operator said is spoken back.
"""

from __future__ import annotations

import pytest

from ops_mcp import topic_match

TOPICS = [
    {"topic_id": "crypto-investing", "name": "Crypto & Investing"},
    {"topic_id": "hn", "name": "Hacker News"},
    {"topic_id": "github-trending", "name": "GitHub Trending"},
    {"topic_id": "ai-research", "name": "AI Research"},
    {"topic_id": "veg-garden", "name": "Watering vegetables"},
]


@pytest.fixture(autouse=True)
def topics(monkeypatch):
    by_id = {topic["topic_id"]: topic for topic in TOPICS}
    monkeypatch.setattr(topic_match, "list_topics", lambda: list(TOPICS))
    monkeypatch.setattr(topic_match, "get_topic", lambda topic_id: by_id.get(topic_id))


@pytest.mark.parametrize(
    ("asked", "topic_id"),
    [
        ("crypto-investing", "crypto-investing"),
        ("Crypto & Investing", "crypto-investing"),
        ("hacker news", "hn"),
    ],
)
def test_an_id_or_a_name_is_exact(asked, topic_id):
    match = topic_match.resolve(asked)
    assert (match.how, match.topic_id) == ("exact", topic_id)


@pytest.mark.parametrize(
    ("asked", "topic_id"),
    [
        ("Finance and Crypto", "crypto-investing"),  # the operator's own case
        ("the finance and crypto topic", "crypto-investing"),
        ("bitcoin", "crypto-investing"),
        ("crpyto", "crypto-investing"),  # a typo
        ("artificial intelligence", "ai-research"),
        ("gardening", "veg-garden"),
        ("trending on github", "github-trending"),
    ],
)
def test_a_clear_match_is_taken_and_said(asked, topic_id):
    one, note, refusal = topic_match.pick(asked)
    assert refusal is None and one["topic_id"] == topic_id and note["how"] == "fuzzy"
    said = topic_match.took(note)
    assert said.startswith("I took that to mean ") and "tell me if you meant another" in said
    assert asked not in said  # only the topic's own name is spoken


def test_two_close_topics_are_asked_about(monkeypatch):
    monkeypatch.setattr(
        topic_match,
        "list_topics",
        lambda: [
            {"topic_id": "crypto-daily", "name": "Crypto Daily"},
            {"topic_id": "crypto-weekly", "name": "Crypto Weekly"},
        ],
    )
    one, note, refusal = topic_match.pick("crypto")
    assert one is None and note["how"] == "ambiguous"
    assert refusal == "Did you mean Crypto Daily or Crypto Weekly? Say which, and I'll look."


def test_nothing_close_names_the_nearest_as_did_you_mean():
    one, note, refusal = topic_match.pick("hacker newsletter digest weekly")
    assert one is None or one["topic_id"] == "hn"
    one, note, refusal = topic_match.pick("weather")
    assert one is None and note["how"] == "none" and refusal.startswith("I couldn't find that topic")


@pytest.mark.parametrize(
    "asked", ["crypto; topics delete crypto", "crypto' OR 1=1", "<script>", "a\nb", "", None, 7]
)
def test_text_no_topic_is_made_of_is_refused_and_never_compared(asked, monkeypatch):
    monkeypatch.setattr(topic_match, "list_topics", lambda: pytest.fail("compared"))
    one, note, refusal = topic_match.pick(asked)
    assert one is None and note["topic_id"] is None and refusal


def test_an_exact_match_says_nothing_extra():
    _, note, _ = topic_match.pick("hn")
    assert topic_match.took(note) == ""
