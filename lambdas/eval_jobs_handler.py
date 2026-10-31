"""Eval jobs Lambda handler: continuous evals' own jobs (docs/enhancements/model-ranch-prompt-assets-evals.md,
section 5.3; infra/modules/evals).

Invoked three ways, and only while evals are switched on (the module makes the function, its two
schedules and its queue's mapping only then):

* by the module's two EventBridge Scheduler schedules, with `{"job": "rollup", "trigger":
  "schedule"}` hourly at five past and `{"job": "judge_select", "trigger": "schedule"}` hourly;
* by the admin API's POST /evals/rollup (`admin_cli evals rollup`), with `{"job": "rollup",
  "trigger": "admin"}`, and `"from"`/`"to"` (ISO dates) to add those days up again; POST
  /evals/revoke (`admin_cli evals revoke`) sends the same with the days that hold the revoked events;
* by the event source mapping on the eval-work queue, with one judge or scoring job per batch
  (`{"Records": [...]}`).

`rollup` runs common/evals_rollup.py. `judge_select` runs the judge's selector, and each queued record
goes to its consumer (common/eval_judge.py: `select` and `consume`). A record is answered by returning,
whatever became of it, so the mapping deletes it: a judge item the day's spend cap holds back is sent
to the queue again with a delay by the consumer itself, and one that failed stays unjudged for a later
run to pick.

Never raises unhandled, like every other scheduled handler here, with one exception that section 6
of the spec allows: a rollup that cannot read the evals log or read and write its tables
(evals_rollup.RollupError) raises, so eval-jobs' Errors alarm fires at once rather than the rollup
heartbeat three hours later. A rollup retried by Lambda adds the same days up again and writes the
same rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common import eval_judge, evals_rollup, evals_settings, evidence

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


def _consume(record) -> dict:
    """One queued record through the judge's consumer. The mapping's batch is one record, but a record
    that fails never takes another down with it."""
    try:
        return eval_judge.consume(record if isinstance(record, dict) else {})
    except Exception as exc:  # noqa: BLE001 - the item stays unjudged; a later run picks it again
        print(f"eval_jobs_handler: a queued job failed: {exc!r}")
        return {"status": "error"}


def handle(event: dict) -> dict:
    """One invocation's job, by the event's shape (see the module's docstring)."""
    records = event.get("Records")
    if isinstance(records, list):
        return {"status": "consumed", "results": [_consume(record) for record in records]}
    # Only a job name that is a code is printed: never what the event carried otherwise.
    job = evidence.check("code", event.get("job")) or "unknown"
    if job == ROLLUP_JOB:
        return _rollup(event)
    if job == JUDGE_SELECT_JOB:
        trigger = "schedule" if event.get("trigger") == "schedule" else "admin"
        return {"status": "ran", JUDGE_SELECT_JOB: eval_judge.select(trigger=trigger)}
    print(f"eval_jobs_handler: job {job} is not one of this function's; nothing ran")
    return {"status": "ignored", "job": job}


def handler(event, context) -> dict:
    # The invocation's id joins its evidence lines (common/evidence.py): the rollup's `rollup_done`, the
    # judge's `judge_skipped` and `judge_tripwire`.
    with evidence.bind(tick=getattr(context, "aws_request_id", None)):
        try:
            return handle(event if isinstance(event, dict) else {})
        except evals_rollup.RollupError as exc:
            print(f"eval_jobs_handler: the rollup failed: {exc!r}")
            raise  # the one raise section 6 allows: eval-jobs' Errors alarm fires
        except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
            print(f"eval_jobs_handler: unhandled exception: {exc!r}")
            return {"status": "error"}
