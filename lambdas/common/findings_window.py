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

Topic-agnostic: nothing here knows about any adapter or domain.
"""

from __future__ import annotations

from decimal import Decimal

FIELD = "findings_window_hours"
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
