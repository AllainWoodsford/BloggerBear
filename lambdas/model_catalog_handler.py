"""Model catalog Lambda handler: keeps the Models registry's list of models and their prices up to
date (common/model_catalog.py, docs/enhancements/archive/model-catalog-refresh.md).

Invoked four ways (common/model_catalog.handle):

* by three static EventBridge Scheduler crons in Sydney time, with
  `{"job": ..., "trigger": "schedule"}` -- skipped while that job is paused: discover at 02:00 and
  reprice at 03:00 every second day (the odd days of the month), and the weekly cycle (the topics'
  model rotations, common/model_fleet.py) at 04:00 on Fridays;
* by the admin API's POST /models/discover and POST /models/reprice, with `{"job": ...}`, which run
  the job now even when it is paused;
* by the one-shot review schedules a pricing run creates (1 and 2 hours later by default), with
  `{"job": "review", "run_id": ...}`;
* by the one-shot tick schedules a run's queue creates, a few models at a time, with
  `{"job": "tick", "run_id": ..., "pass": ..., "tick": n}`.

The pause flags and review settings are the `model-catalog` row of the ModelConfig table, read on
every invocation, so changing them needs no deploy.

Never raises unhandled -- like every other scheduled handler in this codebase, runs under a
top-level try/except that logs the real exception and returns an error dict.
"""

from __future__ import annotations

from common import evidence, model_catalog
from common.lambda_timing import track_lambda_duration


@track_lambda_duration("model_catalog")
def handler(event, context) -> dict:
    # The invocation's id joins each job's `model_catalog_job` evidence line (common/evidence.py).
    with evidence.bind(tick=getattr(context, "aws_request_id", None)):
        try:
            return model_catalog.handle(
                event if isinstance(event, dict) else {},
                own_arn=getattr(context, "invoked_function_arn", None),
            )
        except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
            print(f"model_catalog_handler: unhandled exception: {exc!r}")
            return {"status": "error", "error": str(exc)}
