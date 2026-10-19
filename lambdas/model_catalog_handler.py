"""Model catalog Lambda handler: keeps the Models registry's list of models and their prices up to
date (common/model_catalog.py, docs/enhancements/model-catalog-refresh.md).

Invoked hourly by a static EventBridge Scheduler schedule (the heartbeat: a due price review, then
the discover and reprice jobs on their days), and on demand by the admin API's POST
/models/discover and POST /models/reprice with `{"job": "discover"|"reprice"}`, which run that job
now. Settings and pause flags are the `model-catalog` row of the ModelConfig table, read on every
beat, so changing them needs no deploy.

Never raises unhandled -- like every other scheduled handler in this codebase, runs under a
top-level try/except that logs the real exception and returns an error dict.
"""

from __future__ import annotations

from common import model_catalog
from common.lambda_timing import track_lambda_duration


@track_lambda_duration("model_catalog")
def handler(event, context) -> dict:
    try:
        return model_catalog.heartbeat(event if isinstance(event, dict) else {})
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"model_catalog_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}
