"""When BloggerBear is, and isn't, taking feedback.

Every submission (a vote, with or without a comment) is one piece of feedback and counts
against four things, checked in this order; the first one that is closed is the reason shown:

1. The article. `feedback_locked` on its Articles row (true/false: flip it by hand in DynamoDB
   to lock an article) and `feedback_count` against `article_limit` (default 50). It supersedes
   everything else: a locked article is locked whatever the site-wide state is. An article that
   reaches its limit gets `feedback_locked` set to true.
2. A site-wide lockdown (`locked_down`, a boolean), with an optional public `lockdown_reason`.
3. The daily limit (`daily_limit`, default 100), which resets at the start of each day in
   `daily_timezone` (default Australia/Sydney).
4. The rate limit (`rate_limit_count` per `rate_limit_window_minutes`, default 20 per 5), which
   opens again when the window ends.

All of it is configuration in the `feedback` row of the config table (see
common/dynamo.py), so it can be changed from the admin CLI or in DynamoDB, and a missing or
invalid value takes its default. Nothing here is read from the request.

Enforcement is atomic: each count is a conditional DynamoDB update ("add one, only if still
under the limit"), so two submissions at the edge cannot both get in, and a submission refused
by a later check gives back the counts it took. The windows are fixed, not sliding: a burst
straddling two windows can briefly reach twice the limit.

If the limiter itself cannot be read or written, feedback is refused ("unavailable") rather
than allowed: these limits are what stop a flood of submissions running up a model bill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.dynamo import (
    consume_article_feedback,
    consume_feedback_counter,
    get_article,
    get_feedback_config,
    get_feedback_counter,
    refund_article_feedback,
    refund_feedback_counter,
    set_article_feedback_lock,
)

DEFAULT_RATE_LIMIT_COUNT = 20
DEFAULT_RATE_LIMIT_WINDOW_MINUTES = 5
DEFAULT_DAILY_LIMIT = 100
DEFAULT_ARTICLE_LIMIT = 50
DEFAULT_DAILY_TIMEZONE = "Australia/Sydney"

MAX_LIMIT = 1_000_000
MAX_WINDOW_MINUTES = 1440
MAX_REASON_CHARS = 100

# Why feedback is closed. Also the order of precedence, first to last.
ARTICLE_LOCKED = "article_locked"
ARTICLE_LIMIT = "article_limit"
LOCKDOWN = "lockdown"
DAILY_LIMIT = "daily_limit"
RATE_LIMIT = "rate_limit"
UNAVAILABLE = "unavailable"

LABELS = {
    ARTICLE_LOCKED: "This article is locked",
    ARTICLE_LIMIT: "This article has reached its feedback limit",
    LOCKDOWN: "Feedback is paused",
    DAILY_LIMIT: "Daily limit reached",
    RATE_LIMIT: "Rate limit",
    UNAVAILABLE: "Feedback is unavailable right now",
}

_ROW_TTL_SLACK = timedelta(days=2)


def _is_true(value) -> bool:
    """A lock flag as a boolean. It can be edited by hand in DynamoDB, so a "true" typed as text
    or a 1 still locks: a typo must not leave feedback open."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    if isinstance(value, int | Decimal):
        return value == 1
    return False


def _whole_number(value, *, low: int = 1, high: int = MAX_LIMIT) -> int | None:
    """`value` as a whole number in range, else None (booleans, floats and strings are not)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def settings_error(name: str, value) -> str | None:
    """A message if `value` isn't valid for the setting `name`, else None. None (unset) is
    valid for every setting: it means "use the default"."""
    if value is None:
        return None
    if name == "locked_down":
        return None if isinstance(value, bool) else "must be true or false"
    if name == "lockdown_reason":
        if isinstance(value, str) and 0 < len(value.strip()) <= MAX_REASON_CHARS:
            return None
        return f"must be a short text (1 to {MAX_REASON_CHARS} characters)"
    if name == "daily_timezone":
        return None if _zone(value) is not None else "must be an IANA zone such as Australia/Sydney"
    high = MAX_WINDOW_MINUTES if name == "rate_limit_window_minutes" else MAX_LIMIT
    if _whole_number(value, high=high) is None:
        return f"must be a whole number from 1 to {high}"
    return None


def _zone(name) -> ZoneInfo | None:
    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def effective_settings(row: dict | None) -> dict:
    """The settings in force: the stored value where it is valid, else the default. A stored
    value that is no longer valid (edited by hand) is skipped, never trusted."""
    row = row or {}
    reason = row.get("lockdown_reason")
    zone_name = row.get("daily_timezone")
    return {
        "locked_down": _is_true(row.get("locked_down")),
        "lockdown_reason": reason.strip()[:MAX_REASON_CHARS]
        if isinstance(reason, str) and reason.strip()
        else None,
        "rate_limit_count": _whole_number(row.get("rate_limit_count")) or DEFAULT_RATE_LIMIT_COUNT,
        "rate_limit_window_minutes": _whole_number(
            row.get("rate_limit_window_minutes"), high=MAX_WINDOW_MINUTES
        )
        or DEFAULT_RATE_LIMIT_WINDOW_MINUTES,
        "daily_limit": _whole_number(row.get("daily_limit")) or DEFAULT_DAILY_LIMIT,
        "article_limit": _whole_number(row.get("article_limit")) or DEFAULT_ARTICLE_LIMIT,
        "daily_timezone": zone_name if _zone(zone_name) is not None else DEFAULT_DAILY_TIMEZONE,
    }


def _window(settings: dict, now: datetime) -> tuple[str, datetime]:
    """The current rate-limit window: its counter key, and when it ends."""
    seconds = settings["rate_limit_window_minutes"] * 60
    index = int(now.timestamp()) // seconds
    return f"feedback-window#{seconds}#{index}", datetime.fromtimestamp((index + 1) * seconds, UTC)


def _day(settings: dict, now: datetime) -> tuple[str, datetime]:
    """Today in the configured zone: its counter key, and when the next day starts."""
    zone = _zone(settings["daily_timezone"]) or ZoneInfo(DEFAULT_DAILY_TIMEZONE)
    local = now.astimezone(zone)
    start_of_next = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone)
    return f"feedback-day#{local.date().isoformat()}", start_of_next.astimezone(UTC)


def _expiry(ends: datetime) -> int:
    return int((ends + _ROW_TTL_SLACK).timestamp())


def _closed(reason: str, settings: dict, retry_at: datetime | None = None) -> dict:
    label = LABELS[reason]
    if reason == LOCKDOWN and settings.get("lockdown_reason"):
        label = settings["lockdown_reason"]
    return {
        "open": False,
        "reason": reason,
        "label": label,
        "retry_at": retry_at.isoformat() if retry_at else None,
    }


_OPEN = {"open": True, "reason": None, "label": None, "retry_at": None}


def _count(value) -> int:
    """An article's stored feedback_count as an int (DynamoDB gives a Decimal; junk is 0)."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return 0
    return int(value)


def _article_closed_reason(article: dict, settings: dict) -> str | None:
    at_limit = _count(article.get("feedback_count")) >= settings["article_limit"]
    if _is_true(article.get("feedback_locked")):
        # A lock set by reaching the limit reads differently from one set by hand.
        return ARTICLE_LIMIT if at_limit else ARTICLE_LOCKED
    return ARTICLE_LIMIT if at_limit else None


def status_for(article: dict, now: datetime | None = None) -> dict:
    """Is feedback open for this article right now, and if not, why? Read-only: nothing is
    counted. Returns {open, reason, label, retry_at}. Never raises: if the limiter cannot be
    read, feedback shows as unavailable."""
    now = now or datetime.now(UTC)
    try:
        settings = effective_settings(get_feedback_config())
        reason = _article_closed_reason(article, settings)
        if reason:
            return _closed(reason, settings)
        if settings["locked_down"]:
            return _closed(LOCKDOWN, settings)
        day_key, day_ends = _day(settings, now)
        if get_feedback_counter(day_key) >= settings["daily_limit"]:
            return _closed(DAILY_LIMIT, settings, day_ends)
        window_key, window_ends = _window(settings, now)
        if get_feedback_counter(window_key) >= settings["rate_limit_count"]:
            return _closed(RATE_LIMIT, settings, window_ends)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        print(f"feedback_limits: could not read the feedback limits: {exc!r}")
        return _closed(UNAVAILABLE, effective_settings(None))
    return dict(_OPEN)


def acquire(article: dict, now: datetime | None = None) -> dict:
    """Try to take one piece of feedback for this article. Returns the status: open (and the
    submission has been counted against the article, the day and the window) or closed with
    why (and nothing has been counted). Never raises."""
    now = now or datetime.now(UTC)
    closed = status_for(article, now)  # cheap reads first: a closed site costs no writes
    if not closed["open"]:
        return closed

    article_id = article["article_id"]
    taken: list = []  # what to give back if a later check refuses
    try:
        settings = effective_settings(get_feedback_config())
        new_count = consume_article_feedback(article_id, settings["article_limit"])
        if new_count is None:
            # Lost a race for the article's last place (or it was locked a moment ago): the copy
            # we were given is stale, so read it again to say which.
            fresh = get_article(article_id) or article
            return _closed(_article_closed_reason(fresh, settings) or ARTICLE_LOCKED, settings)
        taken.append(lambda: refund_article_feedback(article_id))

        day_key, day_ends = _day(settings, now)
        if not consume_feedback_counter(day_key, settings["daily_limit"], _expiry(day_ends)):
            _give_back(taken)
            return _closed(DAILY_LIMIT, settings, day_ends)
        taken.append(lambda: refund_feedback_counter(day_key))

        window_key, window_ends = _window(settings, now)
        if not consume_feedback_counter(window_key, settings["rate_limit_count"], _expiry(window_ends)):
            _give_back(taken)
            return _closed(RATE_LIMIT, settings, window_ends)

    except Exception as exc:  # noqa: BLE001 - see the module docstring
        print(f"feedback_limits: could not take feedback: {exc!r}")
        _give_back(taken)
        return _closed(UNAVAILABLE, effective_settings(None))

    if new_count >= settings["article_limit"]:
        # This was the article's last one: lock it, so the flag says so. If this write fails
        # nothing is lost: the next submission finds the article at its limit and is refused.
        try:
            set_article_feedback_lock(article_id, True)
        except Exception as exc:  # noqa: BLE001
            print(f"feedback_limits: could not lock article {article_id} at its limit: {exc!r}")
    return dict(_OPEN)


def _give_back(taken: list) -> None:
    for refund in reversed(taken):
        try:
            refund()
        except Exception as exc:  # noqa: BLE001 - a failed refund only costs one count
            print(f"feedback_limits: could not give back a count: {exc!r}")


def usage(now: datetime | None = None) -> dict:
    """Today's and the current window's counts against their limits, for the admin view."""
    now = now or datetime.now(UTC)
    settings = effective_settings(get_feedback_config())
    day_key, day_ends = _day(settings, now)
    window_key, window_ends = _window(settings, now)
    return {
        "today": get_feedback_counter(day_key),
        "daily_limit": settings["daily_limit"],
        "day_ends_at": day_ends.isoformat(),
        "this_window": get_feedback_counter(window_key),
        "rate_limit_count": settings["rate_limit_count"],
        "window_ends_at": window_ends.isoformat(),
    }
