"""AWS Cost Explorer poll for the API Gateway spend that Bedrock/DynamoDB cost tracking above
doesn't cover (Observability enhancement, PR 3), and for the actual AgentCore Web Search charge --
the real bill to check common/stats_tracking.py's per-query *estimate* against (a wrong
AGENTCORE_WEB_SEARCH_USD_PER_QUERY shows up as the two disagreeing).

Both services come back from ONE GetCostAndUsage call (filtered to the two, grouped by SERVICE):
each call is billed, and this runs daily.

Cost Explorer is a global service reachable only via the us-east-1 endpoint, regardless of which
region the rest of this stack runs in -- the client below is pinned there deliberately, not a bug.
Its own data lags real spend by roughly 24 hours, so this always asks for a rolling 30-day window
ending *yesterday* (TimePeriod's `End` is exclusive of that day itself), never today, to avoid
returning a partial, understated final day.

Granularity is DAILY, summed here rather than requested as MONTHLY: Cost Explorer's MONTHLY
granularity requires the queried period to align to calendar-month boundaries, which an arbitrary
rolling 30-day window does not.

Chosen over hand-maintaining API Gateway's per-request price ourselves (the owner's explicit
"least complex to troubleshoot / manage" steer, even at the cost of Cost Explorer's own lag and a
small per-call charge -- GetCostAndUsage is billed at roughly $0.01/request, which is why this is
polled on a daily schedule -- see cost_explorer_poll_handler.py -- rather than on every request).

Requires a one-time, manual enablement of Cost Explorer in the AWS console (it cannot be turned on
via Terraform/the CLI) before GetCostAndUsage will return real data -- see the PR description.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import boto3

_LOOKBACK_DAYS = 30
API_GATEWAY_SERVICE = "Amazon API Gateway"
# ASSUMED, not yet confirmed from billing data: when this was written the account had no AgentCore
# spend posted, so Cost Explorer listed no AgentCore service to copy the name from. A wrong name
# simply reads as $0 (a service with no rows is 0, never an error) -- if the actual stays at $0
# while the per-query estimate grows, check `aws ce get-dimension-values --dimension SERVICE`
# and correct this.
AGENTCORE_SERVICE = "Amazon Bedrock AgentCore"
POLLED_SERVICES = (API_GATEWAY_SERVICE, AGENTCORE_SERVICE)


def _ce_client():
    # Cost Explorer has no regional API of its own -- every account's cost data is queried via
    # us-east-1 only, whichever region the query is actually about.
    return boto3.client("ce", region_name="us-east-1")


def fetch_service_costs_usd_30d(
    services: tuple[str, ...] = POLLED_SERVICES, today: date | None = None
) -> dict[str, Decimal]:
    """Total unblended USD cost per service over the 30 days ending yesterday (today's own figure
    is never included -- see the module docstring on Cost Explorer's lag), in one call. Every
    service asked for is in the result: one with no cost rows at all is Decimal("0")."""
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=_LOOKBACK_DAYS)
    kwargs = {
        "TimePeriod": {"Start": start.isoformat(), "End": today.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "Filter": {"Dimensions": {"Key": "SERVICE", "Values": list(services)}},
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    totals = {service: Decimal("0") for service in services}
    while True:
        response = _ce_client().get_cost_and_usage(**kwargs)
        for bucket in response.get("ResultsByTime", []):
            for group in bucket.get("Groups", []):
                keys = group.get("Keys") or []
                amount = group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")
                if keys and keys[0] in totals and amount is not None:
                    totals[keys[0]] += Decimal(amount)
        # Grouped results can page (NextPageToken); 30 days x 2 services never should, but a
        # silently truncated total would be worse than one more (billed) call.
        token = response.get("NextPageToken")
        if not token:
            return totals
        kwargs["NextPageToken"] = token
