"""sign_ins: who has signed in to this assistant, who failed to, and who is locked out.

Reads the sign-in log the user pool's triggers write (common/sign_ins.py). The same rules as the
other tools: read-only, and what is said aloud is written here.

**What is said.** Counts, ages and fixed words. A username is said and shown only when it is
shaped like an id (suggestions.ID_PATTERN): the pool's users are made by an administrator, so a
name is the operator's own word, but the log is still a table, and a name that is not a plain id
is called "a user" and gets no command.

**What is a finding.** A user locked out right now (with the command that unlocks them), and a
user who was refused, or failed FAILURES_WORTH_SAYING times or more, in the period (with the
command that lists the log). One slip of the keyboard is not a finding.

**Not remembered.** These findings are not kept in the assistant's memory of suggestions
(server.py does not pass this tool through it): a lock lifts by itself in minutes, and a suggestion
to unlock that outlived it would be noise.

**One environment each.** The table is this environment's own; dev's assistant never sees
production's sign-ins.
"""

from __future__ import annotations

from datetime import datetime

from common import sign_ins as log
from ops_mcp.suggestions import ID_PATTERN, finding
from ops_mcp.tools import _age, _join, _now, _parse

DEFAULT_DAYS = log.DEFAULT_DAYS
FAILURES_WORTH_SAYING = 3
SPOKEN_USERS = 3


def _name(username) -> str | None:
    return username if isinstance(username, str) and ID_PATTERN.match(username) else None


def _row(user: dict, now: datetime) -> dict:
    last = _parse(user.get("last_success"))
    return {
        "user": _name(user.get("username")),
        "attempts": int(user.get("attempts") or 0),
        "successes": int(user.get("successes") or 0),
        "failed": int(user.get("failed") or 0),
        "refused": int(user.get("refused") or 0),
        "locked": bool(user.get("locked")),
        "last_success": last.isoformat() if last else None,
        "last_success_ago": _age(last, now) if last else None,
    }


def _who(row: dict) -> str:
    return f"User {row['user']}" if row["user"] else "A user"


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def sign_ins(days: int = DEFAULT_DAYS, *, now: datetime | None = None) -> dict:
    """Each user's sign-ins over the last `days` (1 to 30), and what about them is unusual."""
    now = _now(now)
    report = log.report(days, now)
    rows = [_row(user, now) for user in report["users"]]

    findings = []
    for row in rows:
        if row["locked"]:
            findings.append(
                finding(
                    "sign_in_locked",
                    f"{_who(row)} is locked out of the assistant after "
                    f"{_plural(log.LOCKOUT_FAILURES, 'failed sign-in')} in "
                    f"{log.WINDOW_MINUTES} minutes",
                    row["user"],
                    user=row["user"],
                )
            )
        elif row["refused"] or row["failed"] >= FAILURES_WORTH_SAYING:
            refused = f", and was refused {_plural(row['refused'], 'time')}" if row["refused"] else ""
            findings.append(
                finding(
                    "sign_in_failures",
                    f"{_who(row)} failed to sign in {_plural(row['failed'], 'time')} "
                    f"in the last {_plural(report['days'], 'day')}{refused}",
                    row["user"],
                    user=row["user"],
                )
            )

    return {
        "spoken": _spoken(rows, report),
        "findings": findings,
        "days": report["days"],
        "lockout": report["lockout"],
        "totals": report["totals"],
        "users": rows,
        "as_of": report["as_of"],
    }


def _spoken(rows: list[dict], report: dict) -> str:
    days = report["days"]
    window = f"the last {days} days" if days != 1 else "the last day"
    totals = report["totals"]
    if not rows:
        return f"Nobody has tried to sign in to the assistant in {window}."
    opening = (
        f"In {window}: {_plural(totals['successes'], 'sign-in')} by {_plural(len(rows), 'user')}, "
        f"{_plural(totals['failed'], 'failed attempt')}"
        f"{', ' + _plural(totals['refused'], 'refusal') if totals['refused'] else ''}."
    )
    locked = [row for row in rows if row["locked"]]
    if locked:
        names = _join([_who(row) for row in locked[:SPOKEN_USERS]])
        more = len(locked) - SPOKEN_USERS
        return (
            f"{opening} {names}{f' and {more} more' if more > 0 else ''} "
            f"{'is' if len(locked) == 1 else 'are'} locked out right now."
        )
    noisy = [row for row in rows if row["refused"] or row["failed"] >= FAILURES_WORTH_SAYING]
    if noisy:
        return f"{opening} {_plural(len(noisy), 'user')} had repeated failures; nobody is locked out now."
    return f"{opening} Nothing unusual."
