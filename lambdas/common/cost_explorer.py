"""AWS Cost Explorer poll for the API Gateway spend that Bedrock/DynamoDB cost tracking above
doesn't cover (Observability enhancement, PR 3).

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
_SERVICE_NAME = "Amazon API Gateway"


def _ce_client():
    # Cost Explorer has no regional API of its own -- every account's cost data is queried via
    # us-east-1 only, whichever region the query is actually about.
    return boto3.client("ce", region_name="us-east-1")


def fetch_api_gateway_cost_usd_30d(today: date | None = None) -> Decimal:
    """Total unblended USD cost attributed to API Gateway over the 30 days ending yesterday
    (today's own figure is never included -- see the module docstring on Cost Explorer's lag)."""
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=_LOOKBACK_DAYS)
    response = _ce_client().get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": [_SERVICE_NAME]}},
    )
    total = Decimal("0")
    for bucket in response.get("ResultsByTime", []):
        amount = bucket.get("Total", {}).get("UnblendedCost", {}).get("Amount")
        if amount is not None:
            total += Decimal(amount)
    return total
