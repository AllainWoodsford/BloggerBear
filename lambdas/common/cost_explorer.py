"""AWS Cost Explorer poll for the API Gateway spend that Bedrock/DynamoDB cost tracking above
doesn't cover (Observability enhancement, PR 3), for the actual AgentCore Web Search charge --
the real bill to check common/stats_tracking.py's per-query *estimate* against (a wrong
AGENTCORE_WEB_SEARCH_USD_PER_QUERY shows up as the two disagreeing) -- and for AWS WAF, the largest
line on the bill, by week and by month as well as over 30 days.

All three services, and every window, come back from ONE GetCostAndUsage call (filtered to the
three, grouped by SERVICE, daily buckets counted into each window here): each call is billed, and
this runs daily.

Cost Explorer is a global service reachable only via the us-east-1 endpoint, regardless of which
region the rest of this stack runs in -- the client below is pinned there deliberately, not a bug.
Its own data lags real spend by roughly 24 hours, so every window ends *yesterday* (TimePeriod's
`End` is exclusive of that day itself), never today, to avoid a partial, understated final day.

Granularity is DAILY, summed here rather than requested as MONTHLY: Cost Explorer's MONTHLY
granularity requires the queried period to align to calendar-month boundaries, which a rolling
30-day window does not, and one daily series serves every window at once.

Chosen over hand-maintaining API Gateway's per-request price ourselves (the owner's explicit
"least complex to troubleshoot / manage" steer, even at the cost of Cost Explorer's own lag and a
small per-call charge -- GetCostAndUsage is billed at roughly $0.01/request, which is why this is
polled on a daily schedule -- see cost_explorer_poll_handler.py -- rather than on every request).

Requires a one-time, manual enablement of Cost Explorer in the AWS console (it cannot be turned on
via Terraform/the CLI) before GetCostAndUsage will return real data -- see the PR description.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import boto3

_LOOKBACK_DAYS = 30
API_GATEWAY_SERVICE = "Amazon API Gateway"
# Confirmed from billing data (2026-10-03): AgentCore's spend is posted under this name.
AGENTCORE_SERVICE = "Amazon Bedrock AgentCore"
# The web application firewall, the largest single line on the bill (US$10.90 of about US$17 over
# the 30 days to 2026-10-03). Name confirmed from billing data. Account-wide: dev and production
# can't be told apart without cost-allocation tags, and the CloudFront ACL is shared by both, so
# it is reported as the site's cost.
WAF_SERVICE = "AWS WAF"
POLLED_SERVICES = (API_GATEWAY_SERVICE, AGENTCORE_SERVICE, WAF_SERVICE)


@dataclass(frozen=True)
class CostReading:
    """One poll's totals per service (USD), every service asked for present (0 if it had no rows).

    All of them end yesterday: today is never included (see the module docstring on the lag).
    `usd_30d` is the rolling 30 days; `week_to_date` is from `week_start` (the Monday of this ISO
    week) and `month_to_date` from the 1st of this month, both empty on their first day;
    `previous_month` is the whole of last calendar month.
    """

    usd_30d: dict[str, Decimal]
    week_to_date: dict[str, Decimal]
    month_to_date: dict[str, Decimal]
    previous_month: dict[str, Decimal]
    week_start: str  # "2026-09-28"
    month: str  # "2026-10"
    previous_month_label: str  # "2026-09"


def _ce_client():
    # Cost Explorer has no regional API of its own -- every account's cost data is queried via
    # us-east-1 only, whichever region the query is actually about.
    return boto3.client("ce", region_name="us-east-1")


def fetch_costs(services: tuple[str, ...] = POLLED_SERVICES, today: date | None = None) -> CostReading:
    """Every figure in a CostReading, from ONE GetCostAndUsage call: DAILY buckets from whichever
    is earlier, 30 days ago or the 1st of last month, to yesterday, each bucket counted into every
    window its day falls in."""
    today = today or datetime.now(UTC).date()
    start_30d = today - timedelta(days=_LOOKBACK_DAYS)
    week_start = today - timedelta(days=today.weekday())
    this_month = today.replace(day=1)
    previous_month = (this_month - timedelta(days=1)).replace(day=1)
    start = min(start_30d, previous_month, week_start)

    kwargs = {
        "TimePeriod": {"Start": start.isoformat(), "End": today.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "Filter": {"Dimensions": {"Key": "SERVICE", "Values": list(services)}},
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    window_names = ("30d", "week", "month", "prev")
    windows = {name: {service: Decimal("0") for service in services} for name in window_names}
    while True:
        response = _ce_client().get_cost_and_usage(**kwargs)
        for bucket in response.get("ResultsByTime", []):
            try:
                day = date.fromisoformat(bucket["TimePeriod"]["Start"])
            except (KeyError, TypeError, ValueError):
                continue  # a bucket with no usable date can't be put in any window
            for group in bucket.get("Groups", []):
                keys = group.get("Keys") or []
                amount = group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")
                if not keys or keys[0] not in windows["30d"] or amount is None:
                    continue
                cost = Decimal(amount)
                if day >= start_30d:
                    windows["30d"][keys[0]] += cost
                if day >= week_start:
                    windows["week"][keys[0]] += cost
                if day >= this_month:
                    windows["month"][keys[0]] += cost
                elif day >= previous_month:
                    windows["prev"][keys[0]] += cost
        # Grouped results can page (NextPageToken); ~60 days x 3 services never should, but a
        # silently truncated total would be worse than one more (billed) call.
        token = response.get("NextPageToken")
        if not token:
            return CostReading(
                usd_30d=windows["30d"],
                week_to_date=windows["week"],
                month_to_date=windows["month"],
                previous_month=windows["prev"],
                week_start=week_start.isoformat(),
                month=this_month.strftime("%Y-%m"),
                previous_month_label=previous_month.strftime("%Y-%m"),
            )
        kwargs["NextPageToken"] = token
