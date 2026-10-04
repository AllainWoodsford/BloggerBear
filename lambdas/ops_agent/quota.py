"""A daily cap on the questions each user may ask the assistant: what bounds the model spend of one
sign-in, whatever it is used from (the page, or a briefing Alexa+ started).

Every question runs the agent, and every agent run is several model calls. The stage's throttle
bounds how fast; nothing else bounded how many, so a stolen token, a stuck client, or a judge's
login shared too widely could spend for as long as it was valid. So each user (the Cognito
subject) gets `OPS_AGENT_DAILY_QUESTION_CAP` questions a day, counted before the agent runs.

**Where it is counted.** One small item per user per UTC day, in the briefings table
(ops_mcp/briefings.py), whose only writer besides the MCP server is the agent: its key is
`usage#<subject>#<YYYY-MM-DD>`, which can never be a user's own briefing key (those are bare
subjects), and it expires after two days. One conditional `ADD`, so two questions at the same
moment cannot both take the last one.

**Everything that goes wrong refuses**, as the access switch does: a cap that is set but has no
table to count in, a caller with no subject, a write that fails. The one exception is no cap at all
(the variable unset): then nothing is counted, which is what a local run and the tests get. The
deployment always sets it.

    OPS_AGENT_DAILY_QUESTION_CAP  questions per user per UTC day (a whole number, 1 or more)
    OPS_BRIEFINGS_TABLE           where they are counted
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

from ops_mcp import briefings

CAP_ENV = "OPS_AGENT_DAILY_QUESTION_CAP"
KEPT_FOR = timedelta(days=2)

ALLOWED = "within the cap"
NO_CAP = "no cap configured"
OVER_CAP = "over the daily cap"
NOT_CONFIGURED = "cap set but nowhere to count"
NO_USER = "no signed-in user"
UNCOUNTABLE = "the count could not be written"


def cap() -> int | None:
    """The configured cap, or None when there is none. A value that is not a whole number of
    1 or more is treated as 1: a cap somebody mistyped must not become no cap at all."""
    raw = os.environ.get(CAP_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, value)


def message(limit: int) -> str:
    """What the caller is told when the cap is reached. Fixed words: nothing from the request."""
    return (
        f"That's the {limit} questions allowed today for this sign-in. "
        "The count starts again at midnight UTC."
    )


def take(user_id, *, now: datetime | None = None) -> tuple[bool, str]:
    """Count one question for this user, if the cap allows it. Returns (allowed, reason); the
    reason is one of the constants above, for the log line, never anything the caller sent."""
    limit = cap()
    if limit is None:
        return True, NO_CAP
    if not os.environ.get(briefings.TABLE_ENV, "").strip():
        return False, NOT_CONFIGURED
    if not briefings.valid_user(user_id):
        return False, NO_USER
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        briefings._table().update_item(
            Key={"user_id": f"usage#{user_id}#{moment.date().isoformat()}"},
            UpdateExpression="ADD questions :one SET expires_at = :expires",
            ConditionExpression="attribute_not_exists(questions) OR questions < :cap",
            ExpressionAttributeValues={
                ":one": 1,
                ":cap": limit,
                ":expires": int((moment + KEPT_FOR).timestamp()),
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False, OVER_CAP
        return False, UNCOUNTABLE
    except Exception:  # noqa: BLE001 - a count that cannot be written is a refusal
        return False, UNCOUNTABLE
    return True, ALLOWED
