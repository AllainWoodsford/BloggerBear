"""Sign-ins to the operator's assistant: a log of every attempt, and a lockout.

Cognito's hosted sign-in page keeps no record this project can read, and says nothing when someone
is guessing a password. So two of the pool's Lambda triggers (sign_in_events_handler.py) write one
row here per event:

* **attempt**: someone submitted a sign-in for a user that exists (the pre-authentication trigger,
  which runs before the password is checked);
* **success**: that user got in (the post-authentication trigger, after the password and, where
  the pool asks for one, the authenticator code);
* **refused**: an attempt this module turned away because the user was locked;
* **unlocked**: an administrator cleared the lock (`admin_cli sign-ins unlock`).

**A failure is an attempt with no success after it.** Cognito has no trigger for a wrong password,
so a failure is never written down; it is what is left when attempts are counted since the user's
last success or unlock. A sign-in abandoned half-way (the tab closed at the authenticator step)
counts as one, which errs on the safe side.

**The lockout.** LOCKOUT_FAILURES outstanding attempts inside WINDOW_MINUTES, and the next attempt
is refused before Cognito looks at the password. It lifts by itself as the attempts age out of the
window, or at once when an administrator unlocks the user. This is on top of Cognito's own, slower
back-off, and unlike it, it is recorded: the refusal raises a high-severity security incident
(common/security_events.py), which emails the alert address.

**What it cannot see.** Cognito does not run the trigger for a username that does not exist, so
guesses at usernames leave nothing here. The triggers are given no client address either, so a row
says who and when, never from where.

**What it stores.** The username, the event, the time and the app client's id. Usernames are made
by an administrator and belong to operators, not readers. Rows expire after RETENTION_DAYS. A
security incident about a user holds only a keyed hash of the name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common.dynamo import put_sign_in_event, query_sign_in_events, scan_sign_in_events

ATTEMPT, SUCCESS, REFUSED, UNLOCKED = "attempt", "success", "refused", "unlocked"
EVENTS = (ATTEMPT, SUCCESS, REFUSED, UNLOCKED)

LOCKOUT_FAILURES = 5
WINDOW_MINUTES = 15
RETENTION_DAYS = 120
# Outstanding attempts at which a user's failures become a security incident, before the lockout.
REPEATED_FAILURES = 2

DEFAULT_DAYS = 7
MAX_DAYS = 30


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def record(username: str, event: str, at: datetime, client_id: str = "", by: str = "") -> None:
    """Write one event for `username`. `event` is one of EVENTS."""
    if event not in EVENTS:
        raise ValueError(f"unknown sign-in event {event!r}")
    fields = {"event": event}
    if client_id:
        fields["client_id"] = client_id
    if by:
        fields["by"] = by
    put_sign_in_event(
        username,
        _iso(at),
        fields,
        expires_at=int((at + timedelta(days=RETENTION_DAYS)).timestamp()),
    )


def outstanding_failures(events: list[dict]) -> int:
    """How many attempts in `events` (oldest first) have no success or unlock after them."""
    failures = 0
    for item in events:
        kind = item.get("event")
        if kind == ATTEMPT:
            failures += 1
        elif kind in (SUCCESS, UNLOCKED):
            failures = 0
    return failures


def recent_failures(username: str, now: datetime) -> int:
    """The user's outstanding failed attempts inside the lockout window."""
    since = _iso(now - timedelta(minutes=WINDOW_MINUTES))
    return outstanding_failures(query_sign_in_events(username, since))


def is_locked(failures: int) -> bool:
    return failures >= LOCKOUT_FAILURES


def unlock(username: str, now: datetime, by: str = "admin") -> dict:
    """Clear a user's lock: every attempt before this moment stops counting. Safe to run for a
    user who is not locked. Returns what the user's count was."""
    failures = recent_failures(username, now)
    record(username, UNLOCKED, now, by=by)
    return {"username": username, "was_locked": is_locked(failures), "failures_cleared": failures}


def clamp_days(days) -> int:
    try:
        days = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(MAX_DAYS, days))


def report(days: int = DEFAULT_DAYS, now: datetime | None = None) -> dict:
    """Every user's sign-ins over the last `days`: counts, the last success, and whether the user
    is locked right now. Users with the most failures first."""
    now = now or datetime.now(UTC)
    days = clamp_days(days)
    events = scan_sign_in_events(_iso(now - timedelta(days=days)))
    lock_since = _iso(now - timedelta(minutes=WINDOW_MINUTES))

    by_user: dict[str, list[dict]] = {}
    for item in sorted(events, key=lambda item: str(item.get("at", ""))):
        by_user.setdefault(str(item.get("username", "")), []).append(item)

    users = []
    for username, items in by_user.items():
        counts = {kind: sum(item.get("event") == kind for item in items) for kind in EVENTS}
        successes = [str(item["at"]) for item in items if item.get("event") == SUCCESS]
        # Every attempt either became a success or did not.
        failed = max(0, counts[ATTEMPT] - counts[SUCCESS])
        in_window = [item for item in items if str(item.get("at", "")) >= lock_since]
        users.append(
            {
                "username": username,
                "attempts": counts[ATTEMPT],
                "successes": counts[SUCCESS],
                "failed": failed,
                "refused": counts[REFUSED],
                "unlocks": counts[UNLOCKED],
                "last_success": successes[-1].split("#")[0] if successes else None,
                "last_event": str(items[-1]["at"]).split("#")[0],
                "locked": is_locked(outstanding_failures(in_window)),
            }
        )
    users.sort(key=lambda user: (-int(user["locked"]), -user["refused"], -user["failed"], user["username"]))
    return {
        "days": days,
        "as_of": _iso(now),
        "lockout": {"failures": LOCKOUT_FAILURES, "window_minutes": WINDOW_MINUTES},
        "users": users,
        "totals": {
            key: sum(user[key] for user in users)
            for key in ("attempts", "successes", "failed", "refused")
        },
    }
