"""How far back a topic's daily article reads its Findings.

The daily cycle writes from every Finding since the topic's previous article, but never from
further back than the topic's findings window. A day (FINDINGS_WINDOW_HOURS) suits a topic that
writes every day. A topic that writes on some days only (one day of a rotation, a weekly topic)
sets `findings_window_hours` on its item, so a Finding made on a day it does not write is still
in the window on the day it does. The previous article still bounds the window either way, so a
wide window never writes the same Findings twice. Re-Write (common/rewrite.py) reads the same
window back from an article's creation to find the Findings the article was written from.

Whole hours, 1 to MAX_FINDINGS_WINDOW_HOURS. A stored value that is not valid (edited by hand,
or written before a bound changed) is skipped and the default applies, never trusted.

`window_start` is the one rule for where the window starts: the daily cycle reads from it, and so
does anything that wants to show what the next article will be written from (the operator's
assistant's topic_findings, the Admin API's findings route), so the two can never disagree.

A topic that rotates through several subjects (common/day_subject.py) writes about one of them a
day, so its previous article on *that subject* bounds the window, not its previous article on any:
the topic item keeps one stamp per subject (`last_article_by_subject`, a map of subject key to ISO
time) beside `last_article_at`, which every topic keeps as before. With `subject_key`, the window
reads the subject's own stamp and ignores the topic-wide one; a subject never written about yet is
bounded by the window alone.

Topic-agnostic: nothing here knows about any adapter or domain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

FIELD = "findings_window_hours"
# The topic item's stamps: when its last article began, on any subject and per subject (the daily
# cycle's _record_article_written, common/dynamo.set_topic_last_article_at).
LAST_ARTICLE_FIELD = "last_article_at"
SUBJECT_STAMPS_FIELD = "last_article_by_subject"
FINDINGS_WINDOW_HOURS = 24
# Two weeks: Findings expire after research_tick_handler.FINDING_TTL_DAYS (14), so a longer window
# would reach back to nothing. tests/test_daily_cycle_handler.py holds the two together.
MAX_FINDINGS_WINDOW_HOURS = 336


def _as_hours(value) -> int | None:
    """`value` as a whole number of hours in range, else None. Accepts the Decimal DynamoDB hands
    back; rejects bools, floats, strings and out-of-range."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        return None
    if isinstance(value, Decimal) and not value.is_finite():
        return None  # int() of a NaN or Infinity raises
    if value != int(value):
        return None
    hours = int(value)
    return hours if 1 <= hours <= MAX_FINDINGS_WINDOW_HOURS else None


def findings_window_error(value) -> str | None:
    """A message if `value` isn't a valid stored window, else None. None (unset) is valid and
    means the default."""
    if value is None or _as_hours(value) is not None:
        return None
    return (
        f"must be a whole number of hours from 1 to {MAX_FINDINGS_WINDOW_HOURS}, "
        f"or null for the default ({FINDINGS_WINDOW_HOURS})"
    )


def findings_window_hours(topic: dict) -> int:
    """The window in force for a topic: its own valid `findings_window_hours`, else the default."""
    hours = _as_hours((topic or {}).get(FIELD))
    return FINDINGS_WINDOW_HOURS if hours is None else hours


def last_article_at(topic: dict, subject_key: str | None = None):
    """The stored stamp that bounds the topic's next window: `last_article_at`, or with
    `subject_key` that subject's entry in `last_article_by_subject` (None when the map, or the
    subject in it, is missing or the map is not a map). As stored: not parsed or checked."""
    topic = topic or {}
    if subject_key is None:
        return topic.get(LAST_ARTICLE_FIELD)
    stamps = topic.get(SUBJECT_STAMPS_FIELD)
    return stamps.get(subject_key) if isinstance(stamps, dict) else None


def window_start(
    topic: dict,
    now: datetime,
    *,
    force: bool = False,
    hours: int | None = None,
    subject_key: str | None = None,
) -> str:
    """ISO timestamp Findings must be captured at or after to feed the topic's next article.

    The window is the topic's findings window (`hours`, which defaults to `findings_window_hours`:
    the last FINDINGS_WINDOW_HOURS unless the topic sets its own), but never reaches back past the
    topic's previous article (`last_article_at`), so a second run with nothing found since the last
    one has nothing to write about instead of rewriting the same Findings. `force` ignores the
    previous article (an operator asking for a fresh take on the whole window); the window itself
    still applies. A `last_article_at` that is missing, unreadable or naive is read as the daily
    cycle reads it: missing or unreadable leaves the window alone, naive is UTC.

    `subject_key`, for a topic that rotates through subjects (the day's subject, or the one an
    operator asks about): the previous article *on that subject* bounds the window instead (its
    entry in `last_article_by_subject`, read the same way), and the topic-wide stamp is not read.
    `force` ignores both. None, the default, is the rule every other topic has always had.

    Pure: `now` is passed in, and nothing is read or written.
    """
    floor = now - timedelta(hours=findings_window_hours(topic) if hours is None else hours)
    previous_at = None if force else last_article_at(topic, subject_key)
    if previous_at:
        try:
            previous = datetime.fromisoformat(previous_at)
        except (TypeError, ValueError):
            previous = None
        if previous is not None:
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)  # stored in UTC
            return max(floor, previous).isoformat()
    return floor.isoformat()
