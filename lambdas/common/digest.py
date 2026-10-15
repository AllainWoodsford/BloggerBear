"""Shared constants for the cross-topic "trending everywhere" digest.

Lives in `common/` rather than inside `trending_digest_handler.py` (which
originally owned both) because `admin_api_handler.py`'s moderation-approve
and force-publish routes also need to resolve a friendly display name for
a digest article -- `get_topic("digest")` returns None (it isn't a real
Topics-table row), so without this, those paths would fall back to the
raw topic_id string "digest" instead of a proper name when rendering a
digest article's static page or generating its musing. Confirmed the hard
way: this exact gap existed until a digest article's *own* direct publish
path (trending_digest_handler.py's compliant branch) was wired up to
static-page rendering and musings, at which point the inconsistency
between the two paths' topic names became real and visible.
"""

from __future__ import annotations

import re

DIGEST_TOPIC_ID = "digest"

# Matches frontend/app.js's own hardcoded heading for this topic_id
# (renderArticleList's DIGEST_TOPIC_ID special case) -- keep both in sync
# if either ever changes.
DIGEST_TOPIC_NAME = "Trending Everywhere"


# --- The digest's steering: no figures, and nothing more specific than it needs to be -----------
#
# The digest is a cross-topic survey, read by people who follow none of the topics closely. It
# says what is moving and in which direction, in general terms; it never carries a figure. That
# is asked of the model in the synthesis prompt, and held to here: a sentence with a figure in it
# is dropped before the digest is composed (trending_digest_handler.py's _compose_digest), on the
# model's lines and on the fallback taken from a finding's own summary alike.
#
# A figure is money, a percentage, a number with a thousands separator or a decimal point, a
# count with a scale word (thousand, million, 5k, 2M), a multiple (3x), or any number of four or
# more digits that is not a year. A bare small number ("OpenCV 5", "three", "Python 3") is not a
# figure: dropping those would cost names, not numbers.

_FIGURE_PATTERNS = (
    r"(?:US|AU|NZ|CA|HK|SG|A)?\$\s?\d",  # $5, US$ 12
    r"[€£¥₹]\s?\d",
    r"\d\s?(?:USD|AUD|EUR|GBP|NZD|CAD|BTC|ETH)\b",
    r"\d\s?(?:%|percent|per cent|pct)(?!\w)",  # not \b: after "%" comes punctuation, not a word
    r"\d{1,3}(?:,\d{3})+",  # 1,234 or 12,345,678
    r"\d+\.\d+",  # 3.5, 0.25
    r"\d+\s?(?:thousand|million|billion|trillion|[kKmMbB])\b",  # 5k, 2M, 3 billion
    r"\d+\s?x\b",  # 10x
    r"\b(?!(?:19|20)\d\d\b)\d{4,}\b",  # 12000, but not 2026
)
_FIGURE = re.compile("|".join(f"(?:{pattern})" for pattern in _FIGURE_PATTERNS), re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def has_figure(text: str) -> bool:
    """Whether `text` carries a figure as defined above."""
    return bool(_FIGURE.search(text or ""))


def without_figures(text: str) -> str:
    """`text` with every sentence that carries a figure left out. A line that was only figures
    comes back empty, and the caller decides what stands in for it."""
    sentences = _SENTENCE_END.split((text or "").strip())
    kept = [sentence.strip() for sentence in sentences if sentence.strip() and not has_figure(sentence)]
    return " ".join(kept)
