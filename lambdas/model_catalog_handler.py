"""Model catalog Lambda handler: keeps the Models registry's list of models and their prices up to
date (common/model_catalog.py, docs/enhancements/model-catalog-refresh.md).

Invoked three ways (common/model_catalog.handle):

* by two static EventBridge Scheduler crons, discover on Tuesdays and reprice on Wednesdays, with
  `{"job": ..., "trigger": "schedule"}` -- skipped while that job is paused;
* by the admin API's POST /models/discover and POST /models/reprice, with `{"job": ...}`, which run
  the job now even when it is paused;
* by the one-shot review schedules a pricing run creates (1 and 2 hours later by default), with
  `{"job": "review", "run_id": ...}`.

The pause flags and review settings are the `model-catalog` row of the ModelConfig table, read on
every invocation, so changing them needs no deploy.

Never raises unhandled -- like every other scheduled handler in this codebase, runs under a
top-level try/except that logs the real exception and returns an error dict.
"""

from __future__ import annotations

from common import model_catalog
from common.lambda_timing import track_lambda_duration


@track_lambda_duration("model_catalog")
def handler(event, context) -> dict:
    try:
        return model_catalog.handle(event if isinstance(event, dict) else {})
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"model_catalog_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}
