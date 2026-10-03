"""AWS Cost Explorer poll for the API Gateway spend that Bedrock/DynamoDB cost tracking above
doesn't cover (Observability enhancement, PR 3), for the actual AgentCore Web Search charge --
the real bill to check common/stats_tracking.py's per-query *estimate* against (a wrong
AGENTCORE_WEB_SEARCH_USD_PER_QUERY shows up as the two disagreeing) -- and for AWS WAF, the largest
line on the bill, by week and by month as well as over 30 days.

It also reads **the whole bill**: every service, not just those three, so that whatever the
project starts paying for (CloudWatch dashboards, CloudFront, DynamoDB...) shows up without a code
change. Each service is stored by name; the Stats page groups them (bill_category below).

Every service, and every window, come back from ONE GetCostAndUsage call (grouped by SERVICE,
daily buckets counted into each window here): each call is billed, and this runs daily. The bill
is account-wide, so dev and production are one figure (telling them apart needs cost-allocation
tags, activated by hand in the Billing console).

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
# The three services with readings of their own on the Stats page: always present in a reading
# (0 if they had no rows). Every other service on the bill is read too (the whole-bill view below).
POLLED_SERVICES = (API_GATEWAY_SERVICE, AGENTCORE_SERVICE, WAF_SERVICE)
# Cost Explorer reports sales tax (GST here) as a "service" of its own. Left out: every figure is
# the bill before tax, the same as the per-service readings have always been.
TAX_SERVICE = "Tax"
# How many complete Monday-to-Sunday weeks every poll re-reads in full. Each completed week's
# StatsHistory row is filled in from these once Cost Explorer has caught up (its ~24h lag means a
# week's Sunday is only known on the Tuesday), and re-reading several each day picks up AWS's own
# late corrections -- and, on the first run, fills in the weeks recorded before this existed.
COMPLETE_WEEKS = 6

# --- The whole bill, by category (Stats page) ----------------------------------------------------
#
# Every service is stored by its own name; the Stats page shows only these three groups, so a new
# service on the bill needs no code change: it lands in Infrastructure unless it says otherwise.
AI_CATEGORY = "ai"  # Bedrock and everything sold through it (models show up as their own services)
SECURITY_CATEGORY = "security"
INFRASTRUCTURE_CATEGORY = "infrastructure"
BILL_CATEGORIES = (AI_CATEGORY, SECURITY_CATEGORY, INFRASTRUCTURE_CATEGORY)
_SECURITY_SERVICES = frozenset(
    {
        WAF_SERVICE,
        "AWS Shield",
        "Amazon GuardDuty",
        "AWS Security Hub",
        "Amazon Inspector",
        "AWS Key Management Service",
        "AWS Secrets Manager",
    }
)


def bill_category(service: str) -> str:
    """Which Stats-page group a service on the bill belongs to. "Bedrock" anywhere in the name is
    AI: Bedrock itself, AgentCore, and third-party models, which the bill lists as e.g.
    "Claude Haiku 4.5 (Amazon Bedrock Edition)"."""
    if "bedrock" in service.lower():
        return AI_CATEGORY
    if service in _SECURITY_SERVICES:
        return SECURITY_CATEGORY
    return INFRASTRUCTURE_CATEGORY


@dataclass(frozen=True)
class CostReading:
    """One poll's totals per service (USD): every service on the bill that had rows, tax left out,
    and POLLED_SERVICES always present (0 if they had none).

    All of them end yesterday: today is never included (see the module docstring on the lag).
    `usd_30d` is the rolling 30 days; `week_to_date` is from `week_start` (the Monday of this ISO
    week) and `month_to_date` from the 1st of this month, both empty on their first day;
    `previous_month` is the whole of last calendar month. `complete_weeks` is each of the last
    COMPLETE_WEEKS whole Monday-to-Sunday weeks, keyed by its Monday ("2026-09-21").
    """

    usd_30d: dict[str, Decimal]
    week_to_date: dict[str, Decimal]
    month_to_date: dict[str, Decimal]
    previous_month: dict[str, Decimal]
    week_start: str  # "2026-09-28"
    month: str  # "2026-10"
    previous_month_label: str  # "2026-09"
    complete_weeks: dict[str, dict[str, Decimal]]


def _ce_client():
    # Cost Explorer has no regional API of its own -- every account's cost data is queried via
    # us-east-1 only, whichever region the query is actually about.
    return boto3.client("ce", region_name="us-east-1")


def _add(window: dict[str, Decimal], service: str, cost: Decimal) -> None:
    window[service] = window.get(service, Decimal("0")) + cost


def fetch_costs(today: date | None = None, complete_weeks: int = COMPLETE_WEEKS) -> CostReading:
    """Every figure in a CostReading, for every service on the bill, from ONE GetCostAndUsage
    call (grouped by service, not filtered to any): DAILY buckets from the earliest window any
    figure needs to yesterday, each bucket counted into every window its day falls in."""
    today = today or datetime.now(UTC).date()
    start_30d = today - timedelta(days=_LOOKBACK_DAYS)
    week_start = today - timedelta(days=today.weekday())
    this_month = today.replace(day=1)
    previous_month = (this_month - timedelta(days=1)).replace(day=1)
    first_complete_week = week_start - timedelta(weeks=complete_weeks)
    start = min(start_30d, previous_month, first_complete_week)

    kwargs = {
        "TimePeriod": {"Start": start.isoformat(), "End": today.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    window_names = ("30d", "week", "month", "prev")
    windows = {name: {service: Decimal("0") for service in POLLED_SERVICES} for name in window_names}
    weeks = {
        (first_complete_week + timedelta(weeks=n)).isoformat(): {} for n in range(complete_weeks)
    }
    while True:
        response = _ce_client().get_cost_and_usage(**kwargs)
        for bucket in response.get("ResultsByTime", []):
            try:
                day = date.fromisoformat(bucket["TimePeriod"]["Start"])
            except (KeyError, TypeError, ValueError):
                continue  # a bucket with no usable date can't be put in any window
            monday = (day - timedelta(days=day.weekday())).isoformat()
            for group in bucket.get("Groups", []):
                keys = group.get("Keys") or []
                amount = group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount")
                if not keys or keys[0] == TAX_SERVICE or amount is None:
                    continue
                service, cost = keys[0], Decimal(amount)
                if day >= start_30d:
                    _add(windows["30d"], service, cost)
                if day >= week_start:
                    _add(windows["week"], service, cost)
                if day >= this_month:
                    _add(windows["month"], service, cost)
                elif day >= previous_month:
                    _add(windows["prev"], service, cost)
                if monday in weeks:
                    _add(weeks[monday], service, cost)
        # Grouped results can page (NextPageToken): ~75 days x every service on the bill can, and
        # a silently truncated total would be worse than one more (billed) call.
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
                complete_weeks=weeks,
            )
        kwargs["NextPageToken"] = token
