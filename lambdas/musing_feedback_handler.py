"""Periodic feedback-musing Lambda handler.

Manually invocable (`event` is ignored) but designed to run on a static
EventBridge Scheduler `rate(4 days)` schedule wired up directly to this
function -- same "one global job, not per-topic" pattern as
weekly_reflection_handler.py/trending_digest_handler.py, since this looks
across all feedback at once rather than per-topic.

Flow: look back over the last _LOOKBACK_DAYS of Feedback (all topics, all
articles), tally up/down votes, and ask Bedrock for one short in-character
musing reflecting on that -- see common/musings.py for the actual prompt
and mood derivation (mood always comes from the real up/down tally, never
random, including the zero-feedback case, so the Musings feed doesn't go
silent for a whole lookback window at a stretch).

Never raises unhandled -- like the other scheduled handlers in this
codebase, runs under a top-level try/except that logs the real exception
and returns an error dict, since a scheduled job has no one watching
synchronously.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from common.dynamo import list_feedback_since
from common.musings import generate_and_store_feedback_musing

_LOOKBACK_DAYS = 4


def handler(event, context) -> dict:
    try:
        return _run_feedback_musing()
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"musing_feedback_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}


def _run_feedback_musing() -> dict:
    since = (datetime.now(UTC) - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    feedback_items = list_feedback_since(since)

    up_votes = sum(1 for f in feedback_items if f.get("vote") == "up")
    down_votes = sum(1 for f in feedback_items if f.get("vote") == "down")

    model_id = os.environ["BEDROCK_MODEL_ID"]
    musing = generate_and_store_feedback_musing(
        up_votes=up_votes,
        down_votes=down_votes,
        lookback_days=_LOOKBACK_DAYS,
        model_id=model_id,
    )

    return {
        "status": "musing_created",
        "musing_id": musing["musing_id"],
        "mood": musing["mood"],
        "up_votes": up_votes,
        "down_votes": down_votes,
    }
