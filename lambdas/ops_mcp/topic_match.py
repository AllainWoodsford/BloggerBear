"""Which topic the operator meant, from however they said it.

The tools take a `topic`, and the agent passes on what the operator said: "crypto-investing" (the
id), "Crypto & Investing" (the name), or "the finance and crypto topic" (what they remembered).
Only the first used to work; the others got "I can't find a topic with that id." This module is
the forgiving lookup every tool that takes a topic goes through (`pick`).

**How a topic is chosen**, in order:

1. The id exactly, or the name exactly (case and punctuation aside): `exact`.
2. Otherwise each topic is scored against the words asked: shared words (with a few synonyms:
   finance ~ investing ~ markets, crypto ~ cryptocurrency ~ bitcoin, ai ~ artificial intelligence,
   tech ~ technology), words that are near-misses of each other ("crpyto"), and how alike the whole
   strings are. One clear winner is `fuzzy`: the tool goes ahead with it, and says which topic it
   took ("I took that to mean Crypto & Investing"), so the operator can stop it if it is wrong.
3. Two or more close: `ambiguous`, nothing is read, and the answer asks which one ("Did you mean
   Crypto & Investing or Finance Weekly?").
4. Nothing close: `none`, with the nearest few named, as "did you mean".

**Nothing the operator said is spoken back.** What is said is the topic's own name (as
tools._topic_label shows it, cut short and cleaned) and its id. The words asked are only compared.

Reads the Topics table once per call (list_topics), and only when the words are not an exact id.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from common.dynamo import get_topic, list_topics
from ops_mcp.suggestions import ID_PATTERN

ASKED_MAX_CHARS = 120
ACCEPT = 0.5  # a score this good is a match
CLEAR_LEAD = 0.15  # and it must beat the next one by this much to be taken without asking
CANDIDATES_MAX = 3

# Words that say nothing about which topic: "the crypto topic", "news about finance".
_FILLER = frozenset(
    {"the", "a", "an", "and", "or", "of", "for", "about", "on", "topic", "topics", "news", "my"}
)
# A few words people use for the same thing. Each word maps to its group's first word.
_SYNONYM_GROUPS = (
    ("finance", "financial", "investing", "investment", "investments", "markets", "market", "money"),
    ("stocks", "shares", "equities"),
    ("crypto", "cryptocurrency", "cryptocurrencies", "bitcoin", "btc", "coins", "defi"),
    ("ai", "artificial", "intelligence", "ml", "llm", "llms"),
    ("tech", "technology", "software"),
    ("garden", "gardening", "gardens", "plants", "vegetables", "vegtables"),
)
_SYNONYMS = {word: group[0] for group in _SYNONYM_GROUPS for word in group}
_SPLIT = re.compile(r"[^a-z0-9]+")
# What a topic's name or id can be made of. Anything else (a quote, a semicolon, an equals sign) is
# not a topic someone is naming, and is refused before it is compared with anything.
_SAYABLE = re.compile(r"^[A-Za-z0-9 &'’_.,()/+-]+$")


def _words(text: str) -> list[str]:
    return [word for word in _SPLIT.split(str(text or "").lower().replace("&", " and ")) if word]


def _meaningful(text: str) -> set[str]:
    return {_SYNONYMS.get(word, word) for word in _words(text) if word not in _FILLER}


def _flat(text: str) -> str:
    return "".join(_words(text))


def score(asked: str, topic: dict) -> float:
    """How well `asked` fits `topic`, from 0 to 1."""
    topic_id = str(topic.get("topic_id") or "")
    name = str(topic.get("name") or "")
    want = _meaningful(asked)
    have = _meaningful(name) | _meaningful(topic_id.replace("-", " ").replace("_", " "))
    if not want or not have:
        return 0.0
    shared = 0.0
    for word in want:
        if word in have:
            shared += 1
        elif any(difflib.SequenceMatcher(None, word, other).ratio() >= 0.8 for other in have):
            shared += 0.8  # a typo or a plural
    # How much of what was said this topic explains: "crypto" fits "Crypto & Investing" fully, and
    # "crypto and delete" only half.
    overlap = shared / len(want)
    alike = max(
        difflib.SequenceMatcher(None, _flat(asked), _flat(name)).ratio(),
        difflib.SequenceMatcher(None, _flat(asked), _flat(topic_id)).ratio(),
    )
    return round(0.7 * overlap + 0.3 * alike, 3)


@dataclass
class Match:
    how: str  # exact, fuzzy, ambiguous, none or refused
    topic: dict | None = None
    candidates: list[dict] = field(default_factory=list)  # [{"topic_id", "name", "score"}]

    @property
    def topic_id(self) -> str | None:
        return (self.topic or {}).get("topic_id")


def _label(topic: dict) -> str:
    from ops_mcp.tools import _topic_label  # tools imports nothing of this module

    return _topic_label(topic, topic.get("topic_id") or "")


def resolve(asked) -> Match:
    """The topic `asked` means, or the candidates for it."""
    if not isinstance(asked, str) or not asked.strip():
        return Match("none")
    asked = asked.strip()[:ASKED_MAX_CHARS]
    if not _SAYABLE.match(asked):
        return Match("refused")
    if ID_PATTERN.match(asked):
        exact = get_topic(asked)
        if exact is not None:
            return Match("exact", exact)
    topics = list_topics()
    flat = _flat(asked)
    for topic in topics:
        if flat and flat in (_flat(topic.get("name") or ""), _flat(topic.get("topic_id") or "")):
            return Match("exact", topic)
    ranked = sorted(((score(asked, topic), topic) for topic in topics), key=lambda pair: -pair[0])
    candidates = [
        {"topic_id": topic.get("topic_id"), "name": _label(topic), "score": value}
        for value, topic in ranked[:CANDIDATES_MAX]
        if value > 0
    ]
    if not ranked or ranked[0][0] < ACCEPT:
        return Match("none", candidates=candidates)
    best_score, best = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score - runner_up < CLEAR_LEAD:
        close = [c for c in candidates if best_score - c["score"] < CLEAR_LEAD]
        return Match("ambiguous", candidates=close)
    return Match("fuzzy", best, candidates=candidates)


def _either(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def pick(asked) -> tuple[dict | None, dict, str | None]:
    """For a tool that takes a topic: the topic to use, what to put in its result about how it was
    chosen (`matched_topic`), and, when nothing is to be read, the words to answer with instead."""
    match = resolve(asked)
    note = {
        "how": match.how,
        "topic_id": match.topic_id,
        "name": _label(match.topic) if match.topic else None,
        "did_you_mean": [{"topic_id": c["topic_id"], "name": c["name"]} for c in match.candidates],
    }
    if match.how == "exact":
        return match.topic, note, None
    if match.how == "fuzzy":
        return match.topic, note, None
    if match.how == "refused":
        return None, note, "That isn't a topic I can look up."
    names = [c["name"] for c in match.candidates]
    if match.how == "ambiguous":
        return None, note, f"Did you mean {_either(names)}? Say which, and I'll look."
    if names:
        return None, note, f"I couldn't find that topic. Did you mean {_either(names)}?"
    return None, note, "I couldn't find that topic, and there are no topics to suggest."


def took(note: dict) -> str:
    """The sentence a tool starts with when it guessed: which topic it took, so the operator can
    stop it. Empty for an exact match."""
    if note.get("how") != "fuzzy" or not note.get("name"):
        return ""
    return f"I took that to mean {note['name']}; tell me if you meant another topic. "
