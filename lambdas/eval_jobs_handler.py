"""Eval jobs Lambda handler: continuous evals' own jobs (docs/enhancements/model-ranch-prompt-assets-evals.md,
section 5.3; infra/modules/evals).

Invoked two ways, and only while evals are switched on (the module makes the function, its two
schedules and its queue's mapping only then):

* by the module's two EventBridge Scheduler schedules, with `{"job": "rollup", "trigger":
  "schedule"}` hourly at five past and `{"job": "judge_select", "trigger": "schedule"}` hourly;
* by the event source mapping on the eval-work queue, with one judge or scoring job per batch
  (`{"Records": [...]}`).

A skeleton for now: it logs which job it was asked for and does nothing. The rollup, the judge's
selector and the queue consumer are PR13's and PR18's, and nothing sends to the queue before then.
It writes no rollup_done line, so with evals switched on before PR13 the module's rollup heartbeat
alarm fires: switch evals on after PR13.

Never raises unhandled, like every other scheduled handler here.
"""

from __future__ import annotations

from common import evidence


def handler(event, context) -> dict:
    try:
        event = event if isinstance(event, dict) else {}
        records = event.get("Records")
        if isinstance(records, list):
            # A queue batch: no job body is read or printed, only how many there were.
            print(f"eval_jobs_handler: {len(records)} queued job(s) received; nothing runs them yet")
            return {"status": "skipped", "records": len(records)}
        # Only a job name that is a code is printed: never what the event carried otherwise.
        job = evidence.check("code", event.get("job")) or "unknown"
        print(f"eval_jobs_handler: job {job} received; nothing runs it yet")
        return {"status": "skipped", "job": job}
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"eval_jobs_handler: unhandled exception: {exc!r}")
        return {"status": "error"}
