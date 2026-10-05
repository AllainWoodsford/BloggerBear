"""Sign-in events Lambda handler: the assistant's user pool calls it on every sign-in.

One function for two of the pool's triggers (infra/modules/ops-assistant, `lambda_config`):

* **Pre-authentication** (`PreAuthentication_*`): before Cognito checks the password. Counts the
  user's outstanding failed attempts (common/sign_ins.py). At the lockout it refuses the sign-in
  by raising, records the refusal and a high-severity security incident; otherwise it records
  the attempt.
* **Post-authentication** (`PostAuthentication_*`): the user got in. Records the success, which
  is what stops the earlier attempts counting as failures.

A trigger must hand the event back to let the sign-in go on, and raise to stop it.

**Fails open.** If the table cannot be read or written, the sign-in goes ahead and the failure is
logged: a fault here must not lock the operator out of their own assistant. Cognito's own back-off
on wrong passwords still applies. Only a counted lockout refuses.

Cognito gives a trigger five seconds, so this does two DynamoDB calls at most and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common import security_events, sign_ins

# Shown on the sign-in page, after Cognito's own "PreAuthentication failed with error".
LOCKED_MESSAGE = (
    f"Too many failed sign-in attempts. Wait {sign_ins.WINDOW_MINUTES} minutes and try again, "
    "or ask the administrator to unlock the account."
)


class SignInRefused(Exception):
    """Raised to Cognito to stop a sign-in. Its message is shown to the person signing in."""


def _incident(rule: str, username: str, at: datetime) -> None:
    security_events.record_incident(
        source=security_events.SIGN_IN,
        rule=rule,
        client_ip="",
        subject=username,
        at=at,
        method="POST",
        path="/login",
    )


def _before_sign_in(username: str, client_id: str, now: datetime) -> bool:
    """Record the attempt. True when it must be refused."""
    failures = sign_ins.recent_failures(username, now)
    if sign_ins.is_locked(failures):
        sign_ins.record(username, sign_ins.REFUSED, now, client_id)
        _incident(security_events.SIGN_IN_LOCKOUT_RULE, username, now)
        return True
    sign_ins.record(username, sign_ins.ATTEMPT, now, client_id)
    if failures >= sign_ins.REPEATED_FAILURES:
        # The earlier attempts are known to have failed: this one would not be needed otherwise.
        _incident(security_events.SIGN_IN_FAILURES_RULE, username, now)
    return False


# Not wrapped in track_lambda_duration: that is the Stats page's scheduled-pipeline run time.
def handler(event, context):
    source = str(event.get("triggerSource") or "")
    username = str(event.get("userName") or "")
    client_id = str((event.get("callerContext") or {}).get("clientId") or "")
    now = datetime.now(UTC)

    refuse = False
    try:
        if not username:
            print(f"sign_in_events_handler: {source or 'an event'} named no user; nothing recorded")
        elif source.startswith("PreAuthentication"):
            refuse = _before_sign_in(username, client_id, now)
        elif source.startswith("PostAuthentication"):
            sign_ins.record(username, sign_ins.SUCCESS, now, client_id)
        else:
            print(f"sign_in_events_handler: no handling for trigger {source!r}")
    except Exception as exc:  # noqa: BLE001 - a fault here must never stop a sign-in
        print(f"sign_in_events_handler: could not record {source!r}: {exc!r}")
        return event

    if refuse:
        # The name is not logged: who was locked is in the table, for the operator.
        print("sign_in_events_handler: refused a sign-in for a locked user")
        raise SignInRefused(LOCKED_MESSAGE)
    return event
