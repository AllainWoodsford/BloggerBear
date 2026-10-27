"""Eval jobs Lambda handler: continuous evals' own jobs (docs/enhancements/model-ranch-prompt-assets-evals.md,
section 5.3; infra/modules/evals).

Invoked three ways, and only while evals are switched on (the module makes the function, its two
schedules and its queue's mapping only then):

* by the module's two EventBridge Scheduler schedules, with `{"job": "rollup", "trigger":
  "schedule"}` hourly at five past and `{"job": "judge_select", "trigger": "schedule"}` hourly;
* by the admin API's POST /evals/rollup (`admin_cli evals rollup`), with `{"job": "rollup",
  "trigger": "admin"}`, and `"from"`/`"to"` (ISO dates) to add those days up again;
* by the event source mapping on the eval-work queue, with one judge or scoring job per batch
  (`{"Records": [...]}`).

`rollup` runs common/evals_rollup.py. The judge's selector and the queue's consumer are PR18's: until
then each logs that it arrived and returns, and nothing sends to the queue.

Never raises unhandled, like every other scheduled handler here, with one exception that section 6
of the spec allows: a rollup that cannot read the evals log or read and write its tables
(evals_rollup.RollupError) raises, so eval-jobs' Errors alarm fires at once rather than the rollup
heartbeat three hours later. A rollup retried by Lambda adds the same days up again and writes the
same rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common import evals_rollup, evals_settings, evidence

ROLLUP_JOB = "rollup"
JUDGE_SELECT_JOB = "judge_select"


def _rollup(event: dict) -> dict:
    trigger = "schedule" if event.get("trigger") == "schedule" else "admin"
    if event.get("from") is None and event.get("to") is None:
        return {"status": "ran", ROLLUP_JOB: evals_rollup.run(trigger=trigger)}
    now = datetime.now(UTC)
    days = evals_rollup.manual_range(
        event.get("from"), event.get("to"), now, evals_settings.zone(evals_settings.get_settings())
    )
    if isinstance(days, str):
        # Only the reason, in this module's words: never what the event carried.
        print(f"eval_jobs_handler: rollup refused: {days}")
        return {"status": "refused", "job": ROLLUP_JOB}
    first, last = days
    summary = evals_rollup.run(trigger=trigger, day_from=first, day_to=last, now=now)
    return {"status": "ran", ROLLUP_JOB: summary}


def handle(event: dict) -> dict:
    """One invocation's job, by the event's shape (see the module's docstring)."""
    records = event.get("Records")
    if isinstance(records, list):
        # A queue batch: no job body is read or printed, only how many there were. Returning
        # deletes them; nothing sends any until the consumer arrives.
        print(f"eval_jobs_handler: {len(records)} queued job(s) received; nothing consumes them yet")
        return {"status": "skipped", "records": len(records)}
    # Only a job name that is a code is printed: never what the event carried otherwise.
    job = evidence.check("code", event.get("job")) or "unknown"
    if job == ROLLUP_JOB:
        return _rollup(event)
    if job == JUDGE_SELECT_JOB:
        print(f"eval_jobs_handler: job {job} received; the judge is not built yet, so nothing ran")
        return {"status": "skipped", "job": job}
    print(f"eval_jobs_handler: job {job} is not one of this function's; nothing ran")
    return {"status": "ignored", "job": job}


def handler(event, context) -> dict:
    # The invocation's id joins the rollup's `rollup_done` evidence line (common/evidence.py).
    with evidence.bind(tick=getattr(context, "aws_request_id", None)):
        try:
            return handle(event if isinstance(event, dict) else {})
        except evals_rollup.RollupError as exc:
            print(f"eval_jobs_handler: the rollup failed: {exc!r}")
            raise  # the one raise section 6 allows: eval-jobs' Errors alarm fires
        except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
            print(f"eval_jobs_handler: unhandled exception: {exc!r}")
            return {"status": "error"}
