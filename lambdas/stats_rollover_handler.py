"""Weekly Stats rollover Lambda handler (Observability enhancement, PR 2).

Manually invocable (`event` is ignored) but designed to run on a static EventBridge Scheduler
weekly schedule wired up directly to this function -- same "one global job, not per-topic" pattern
as weekly_reflection_handler.py/musing_feedback_handler.py. Scheduled a little after
weekly_reflection's own Monday run, so that Monday's reflection cost is tallied into the week it
is reflecting on, not the new week just starting.

Flow: read the current week's StatsCurrent row (common/dynamo.py's get_current_stats), copy it into
a new StatsHistory row keyed by the week it covers, then clear StatsCurrent so the next Bedrock call
or reader-activity event starts the new week's row fresh (see common/stats_tracking.py, which owns
both tables' shape). A week with nothing recorded (no `week_start` on the row -- the normal state if
this ever ran twice, or before anything was tracked yet) rolls nothing over and clears nothing.

Never raises unhandled -- like every other scheduled handler in this codebase, runs under a
top-level try/except that logs the real exception and returns an error dict.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common.dynamo import delete_current_stats, get_current_stats, put_stats_history_row
from common.lambda_timing import track_lambda_duration


@track_lambda_duration("stats_rollover")
def handler(event, context) -> dict:
    try:
        return _roll_over()
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"stats_rollover_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}


def _roll_over() -> dict:
    current = get_current_stats()
    week_start = current.get("week_start")
    if week_start is None:
        print("stats_rollover_handler: nothing was recorded this week; nothing to roll over")
        return {"status": "nothing_to_roll_over"}

    row = {**current, "rolled_over_at": datetime.now(UTC).isoformat()}
    written = put_stats_history_row(week_start, row)
    if not written:
        # Already rolled over (a retried or duplicated invocation) -- the first write is kept,
        # never silently replaced. Still clear StatsCurrent below: whatever wrote it a second
        # time is stale either way, and leaving it would just get folded into next week by mistake.
        print(f"stats_rollover_handler: {week_start} was already rolled over; not overwriting it")

    delete_current_stats()
    return {"status": "rolled_over" if written else "already_rolled_over", "week_start": week_start}
