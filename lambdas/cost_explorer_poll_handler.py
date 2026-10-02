"""Cost Explorer poll Lambda handler: API Gateway spend (Observability enhancement, PR 3), the
actual AgentCore Web Search charge, and AWS WAF's spend by 30 days, week and month -- all from one
Cost Explorer call.

Manually invocable (`event` is ignored) but designed to run on a static EventBridge Scheduler
daily schedule wired up directly to this function -- same "one global job, not per-topic" pattern
as trending_digest_handler.py. Polls Cost Explorer once a day (see common/cost_explorer.py for why
daily, and why every window ends yesterday, not today) and writes the latest readings straight
onto this week's StatsCurrent row -- refreshed snapshots, not accumulated counters, so a repeat
run just overwrites the previous reading rather than double-counting it (unlike every other
figure on StatsCurrent, which are ADD'd -- see common/stats_tracking.py's
record_api_gateway_cost).

Never raises unhandled -- like every other scheduled handler in this codebase, runs under a
top-level try/except that logs the real exception and returns an error dict.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common.cost_explorer import AGENTCORE_SERVICE, API_GATEWAY_SERVICE, WAF_SERVICE, fetch_costs
from common.lambda_timing import track_lambda_duration
from common.stats_tracking import record_agentcore_cost, record_api_gateway_cost, record_waf_cost


@track_lambda_duration("cost_explorer_poll")
def handler(event, context) -> dict:
    try:
        return _poll()
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"cost_explorer_poll_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}


def _poll() -> dict:
    reading = fetch_costs()
    as_of = datetime.now(UTC).isoformat()
    record_api_gateway_cost(reading.usd_30d[API_GATEWAY_SERVICE], as_of)
    record_agentcore_cost(reading.usd_30d[AGENTCORE_SERVICE], as_of)
    record_waf_cost(
        usd_30d=reading.usd_30d[WAF_SERVICE],
        usd_week_to_date=reading.week_to_date[WAF_SERVICE],
        usd_month_to_date=reading.month_to_date[WAF_SERVICE],
        usd_previous_month=reading.previous_month[WAF_SERVICE],
        month=reading.month,
        previous_month=reading.previous_month_label,
        as_of=as_of,
    )
    return {
        "status": "recorded",
        "api_gateway_cost_usd_30d": str(reading.usd_30d[API_GATEWAY_SERVICE]),
        "agentcore_cost_usd_30d": str(reading.usd_30d[AGENTCORE_SERVICE]),
        "waf_cost_usd_30d": str(reading.usd_30d[WAF_SERVICE]),
        "waf_cost_usd_month_to_date": str(reading.month_to_date[WAF_SERVICE]),
        "as_of": as_of,
    }
