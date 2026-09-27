"""Cost Explorer poll Lambda handler: API Gateway spend (Observability enhancement, PR 3) and the
actual AgentCore Web Search charge, both from one Cost Explorer call.

Manually invocable (`event` is ignored) but designed to run on a static EventBridge Scheduler
daily schedule wired up directly to this function -- same "one global job, not per-topic" pattern
as trending_digest_handler.py. Polls Cost Explorer once a day (see common/cost_explorer.py for why
daily, and why a rolling 30-day window ending yesterday, not today) and writes the latest reading
straight onto this week's StatsCurrent row -- a refreshed snapshot, not an accumulated counter, so
a repeat run just overwrites the previous reading rather than double-counting it (unlike every
other figure on StatsCurrent, which are ADD'd -- see common/stats_tracking.py's
record_api_gateway_cost).

Never raises unhandled -- like every other scheduled handler in this codebase, runs under a
top-level try/except that logs the real exception and returns an error dict.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common.cost_explorer import AGENTCORE_SERVICE, API_GATEWAY_SERVICE, fetch_service_costs_usd_30d
from common.lambda_timing import track_lambda_duration
from common.stats_tracking import record_agentcore_cost, record_api_gateway_cost


@track_lambda_duration("cost_explorer_poll")
def handler(event, context) -> dict:
    try:
        return _poll()
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"cost_explorer_poll_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}


def _poll() -> dict:
    costs = fetch_service_costs_usd_30d()
    as_of = datetime.now(UTC).isoformat()
    record_api_gateway_cost(costs[API_GATEWAY_SERVICE], as_of)
    record_agentcore_cost(costs[AGENTCORE_SERVICE], as_of)
    return {
        "status": "recorded",
        "api_gateway_cost_usd_30d": str(costs[API_GATEWAY_SERVICE]),
        "agentcore_cost_usd_30d": str(costs[AGENTCORE_SERVICE]),
        "as_of": as_of,
    }
