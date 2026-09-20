"""Topic-relevance guardrails, shared by every stage that touches a topic.

Community feeds (Hacker News, GitHub Trending) and web search return whatever
is popular or textually matches, not what is *on topic*. Left alone that noise
flows straight into articles -- a "Security" topic once produced pieces on
cookbooks and UFOs. Relevance is therefore enforced at three points, all keyed
to whichever topic is active (never a hardcoded topic id, so it holds for any
topic added later):

  1. Collection -- adapters can pre-filter with `adapter_config.keywords`, and
     web search filters titles with word-boundary keyword matching
     (`matches_keywords`, `keywords_from_query`).
  2. Research summary -- `research_relevance_rule` for the P1 prompt.
  3. Writing -- `ideation_relevance_rule` (P2) and `draft_relevance_boundary`
     (P3) in the daily cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

UNKNOWN_TOPIC = "Unknown Topic"


def topic_label(topic: dict) -> str:
    """The name the guardrails address the topic by: its name, else its id."""
    return topic.get("name") or topic.get("topic_id") or UNKNOWN_TOPIC


# --- keyword matching ---------------------------------------------------------


def normalize_keywords(raw) -> list[str]:
    """A keyword list from config: a list of strings (or one string), blanks dropped."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list | tuple):
        return []
    return [k.strip() for k in raw if isinstance(k, str) and k.strip()]


@lru_cache(maxsize=256)
def _compile(keywords: tuple[str, ...]) -> re.Pattern | None:
    parts = []
    for keyword in keywords:
        text = keyword.strip().lower()
        prefix = text.endswith("*")
        text = text.rstrip("*").strip()
        if not text:
            continue
        # Whole-word match (an optional plural "s" allowed) so "eth" doesn't
        # match "together" or "ethics"; a trailing "*" makes it a prefix match
        # ("crypto*" matches "cryptocurrency"). Lookarounds rather than \b so
        # keywords like "c++" still work.
        tail = "" if prefix else "s?(?!\\w)"
        parts.append(f"(?<!\\w){re.escape(text)}{tail}")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def matches_keywords(text: str | None, keywords: Iterable[str] | None) -> bool:
    """True if `text` mentions any keyword as a whole word. No keywords means
    no filter, so everything matches."""
    keyword_tuple = tuple(normalize_keywords(list(keywords or [])))
    pattern = _compile(keyword_tuple)
    if pattern is None:
        return True
    return pattern.search(text or "") is not None


_QUOTED = re.compile(r'"([^"]+)"')
_QUERY_OPERATORS = {"or", "and", "not"}


def keywords_from_query(query: str) -> list[str]:
    """The topical words of a search query, as a default relevance filter.

    Quoted phrases are kept whole; boolean operators, parentheses, `field:value`
    operators and `-negated` terms are dropped, as are very short words. A
    result must mention at least one of these to count as on-topic, which
    catches pages that matched the query only in passing.
    """
    phrases = [p.strip() for p in _QUOTED.findall(query)]
    remainder = _QUOTED.sub(" ", query).replace("(", " ").replace(")", " ")
    words = []
    for token in remainder.split():
        if ":" in token or token.startswith("-") or token.lower() in _QUERY_OPERATORS:
            continue
        token = token.strip(".,;")
        if len(token) >= 3:
            words.append(token)
    return list(dict.fromkeys([*phrases, *words]))


# --- prompt guardrails ----------------------------------------------------------


def research_relevance_rule(topic_name: str) -> str:
    return (
        f"RELEVANCE RULE: this digest covers '{topic_name}' and nothing else. The data may "
        "contain off-topic noise (pop culture, unrelated hobbies, general-interest posts, or "
        "items whose name only sounds technical). Summarize only what is genuinely relevant to "
        f"'{topic_name}': ignore the rest, or, where an off-topic item has a real angle on "
        f"'{topic_name}', describe only that angle. Never take an item at its surface meaning "
        "(a 'cookbook' repository is not a recipe). If nothing in the data is relevant, say so "
        "in one sentence rather than summarizing the noise."
    )


def ideation_relevance_rule(topic_name: str) -> str:
    return (
        "CRITICAL RELEVANCE RULE:\n"
        "You are a strict domain-specific writer. Every proposed angle MUST remain deeply "
        f"relevant to the core theme of '{topic_name}'. If the raw data findings contain "
        "fringe, accidental, or off-topic subjects (e.g., pop culture, unrelated hobbies, "
        "speculative fiction, or internet noise), you MUST either completely ignore those "
        "findings or aggressively reframe them strictly through the functional lens of "
        f"'{topic_name}'. Do not wander off-topic."
    )


def draft_relevance_boundary(topic_name: str) -> str:
    return (
        "CRITICAL RELEVANCE BOUNDARY:\n"
        "The primary mandate of this publication is to provide high-signal commentary on "
        f"'{topic_name}'. Maintain absolute thematic integrity. Under no circumstances should "
        "you dive into literal or surface-level interpretations of noisy data inputs (for "
        "example, interpreting a technical 'cookbook' repository as literal culinary recipes, "
        "or general interest forum posts as core domain facts). Every paragraph must deliver "
        f"value directly aligned with the expectation of a reader subscribing to '{topic_name}'."
    )
