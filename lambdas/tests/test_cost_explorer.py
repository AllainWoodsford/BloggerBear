"""Tests for common/cost_explorer.py: one Cost Explorer read of the whole bill, grouped by service,
counted into a rolling 30 days, this week, this month, last month and each recent complete week,
with API Gateway, AgentCore and AWS WAF always present.

The real `ce` client is mocked directly, not via moto -- moto's Cost Explorer support (as of the
version pinned here) always returns an empty ResultsByTime regardless of what's "in" the account,
so it can't stand in for real billing data. Same "patch the boundary, trust boto3 itself" approach
as test_bedrock.py takes with the real Bedrock client.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from common.cost_explorer import (
    AGENTCORE_SERVICE,
    AI_CATEGORY,
    API_GATEWAY_SERVICE,
    INFRASTRUCTURE_CATEGORY,
    POLLED_SERVICES,
    SECURITY_CATEGORY,
    TAX_SERVICE,
    WAF_SERVICE,
    bill_category,
    fetch_costs,
)

TODAY = date(2026, 10, 3)  # a Saturday; its week began Monday 2026-09-28


def _day(day: str, **amounts: str) -> dict:
    """One DAILY bucket for `day`, grouped by service: {service: amount}."""
    return {
        "TimePeriod": {"Start": day, "End": day},
        "Groups": [
            {"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}
            for service, amount in amounts.items()
        ],
    }


def _grouped(*days: dict, token: str | None = None) -> dict:
    response = {"ResultsByTime": list(days)}
    if token:
        response["NextPageToken"] = token
    return response


def _fetch(*responses: dict, today: date = TODAY):
    client = MagicMock()
    client.get_cost_and_usage.side_effect = list(responses)
    with patch("common.cost_explorer._ce_client", return_value=client):
        return fetch_costs(today=today), client


def _zero(**amounts: str) -> dict:
    return {service: Decimal(amounts.get(service, "0")) for service in POLLED_SERVICES}


def test_every_service_is_summed_per_service_from_one_call():
    reading, client = _fetch(
        _grouped(
            _day(
                "2026-09-20", **{API_GATEWAY_SERVICE: "1.23", AGENTCORE_SERVICE: "0.07", WAF_SERVICE: "0.36"}
            ),
            _day(
                "2026-09-21", **{API_GATEWAY_SERVICE: "4.56", AGENTCORE_SERVICE: "0.14", WAF_SERVICE: "0.36"}
            ),
        )
    )

    assert reading.usd_30d == {
        API_GATEWAY_SERVICE: Decimal("5.79"),
        AGENTCORE_SERVICE: Decimal("0.21"),
        WAF_SERVICE: Decimal("0.72"),
    }
    assert client.get_cost_and_usage.call_count == 1


def test_each_day_counts_into_every_window_it_falls_in():
    reading, _ = _fetch(
        _grouped(
            _day("2026-08-31", **{WAF_SERVICE: "100"}),  # before every window: counted nowhere
            _day("2026-09-02", **{WAF_SERVICE: "1"}),  # last month, before the 30 days began (09-03)
            _day("2026-09-03", **{WAF_SERVICE: "2"}),  # last month and the 30 days
            _day("2026-09-28", **{WAF_SERVICE: "4"}),  # last month, the 30 days and this week
            _day("2026-10-01", **{WAF_SERVICE: "8"}),  # this month, the 30 days and this week
            _day("2026-10-02", **{WAF_SERVICE: "16"}),  # yesterday: the same three
        )
    )

    assert reading.usd_30d[WAF_SERVICE] == Decimal("30")  # 2 + 4 + 8 + 16
    assert reading.week_to_date[WAF_SERVICE] == Decimal("28")  # 4 + 8 + 16
    assert reading.month_to_date[WAF_SERVICE] == Decimal("24")  # 8 + 16
    assert reading.previous_month[WAF_SERVICE] == Decimal("7")  # 1 + 2 + 4
    assert (reading.week_start, reading.month, reading.previous_month_label) == (
        "2026-09-28",
        "2026-10",
        "2026-09",
    )


def test_the_call_starts_at_the_earliest_window_and_ends_today_exclusive():
    _, client = _fetch(_grouped())

    kwargs = client.get_cost_and_usage.call_args.kwargs
    # The six complete weeks before this one began first: Monday 2026-08-17.
    assert kwargs["TimePeriod"] == {"Start": "2026-08-17", "End": "2026-10-03"}
    assert kwargs["Granularity"] == "DAILY"
    assert "Filter" not in kwargs  # the whole bill, every service
    assert kwargs["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]


def test_with_fewer_complete_weeks_the_30_days_can_start_first():
    client = MagicMock()
    client.get_cost_and_usage.return_value = _grouped()
    with patch("common.cost_explorer._ce_client", return_value=client):
        fetch_costs(today=date(2026, 3, 1), complete_weeks=1)  # 1 March 2026 is a Sunday

    # 30 days before 1 March is 30 January: earlier than 1 February and the week of 16 February.
    assert client.get_cost_and_usage.call_args.kwargs["TimePeriod"] == {
        "Start": "2026-01-30",
        "End": "2026-03-01",
    }


def test_on_the_first_of_the_month_nothing_is_this_month_yet():
    reading, _ = _fetch(_grouped(_day("2026-09-30", **{WAF_SERVICE: "0.36"})), today=date(2026, 10, 1))

    assert reading.month_to_date[WAF_SERVICE] == Decimal("0")
    assert reading.previous_month[WAF_SERVICE] == Decimal("0.36")
    assert reading.month == "2026-10" and reading.previous_month_label == "2026-09"


def test_on_a_monday_nothing_is_this_week_yet():
    reading, _ = _fetch(_grouped(_day("2026-09-27", **{WAF_SERVICE: "0.36"})), today=date(2026, 9, 28))

    assert reading.week_to_date[WAF_SERVICE] == Decimal("0")
    assert reading.week_start == "2026-09-28"


def test_a_service_with_no_rows_is_zero_not_missing():
    reading, _ = _fetch(_grouped(_day("2026-09-20", **{API_GATEWAY_SERVICE: "2.00"})))

    assert reading.usd_30d == _zero(**{API_GATEWAY_SERVICE: "2.00"})


def test_no_usage_at_all_is_zero_for_every_service_and_window():
    reading, _ = _fetch(_grouped())

    for window in (reading.usd_30d, reading.week_to_date, reading.month_to_date, reading.previous_month):
        assert window == _zero()


def test_the_service_names_match_the_bill():
    assert API_GATEWAY_SERVICE == "Amazon API Gateway"
    assert AGENTCORE_SERVICE == "Amazon Bedrock AgentCore"
    assert WAF_SERVICE == "AWS WAF"


def test_every_other_service_on_the_bill_is_read_too_but_not_tax():
    reading, _ = _fetch(
        _grouped(
            _day(
                "2026-09-29",
                **{"Amazon DynamoDB": "0.40", "AmazonCloudWatch": "0.10", TAX_SERVICE: "0.05"},
            )
        )
    )

    assert reading.usd_30d == {
        **_zero(),
        "Amazon DynamoDB": Decimal("0.40"),
        "AmazonCloudWatch": Decimal("0.10"),
    }
    assert reading.week_to_date["Amazon DynamoDB"] == Decimal("0.40")
    assert TAX_SERVICE not in reading.week_to_date


def test_each_complete_week_is_read_in_full_monday_to_sunday():
    reading, _ = _fetch(
        _grouped(
            _day("2026-08-16", **{WAF_SERVICE: "100"}),  # the Sunday before the first week: nowhere
            _day("2026-08-17", **{WAF_SERVICE: "1"}),  # Monday of the first complete week
            _day("2026-09-21", **{WAF_SERVICE: "2", "Amazon S3": "0.01"}),  # last week's Monday
            _day("2026-09-27", **{WAF_SERVICE: "4"}),  # last week's Sunday
            _day("2026-09-28", **{WAF_SERVICE: "8"}),  # this week: not a complete week
        )
    )

    assert list(reading.complete_weeks) == [
        "2026-08-17",
        "2026-08-24",
        "2026-08-31",
        "2026-09-07",
        "2026-09-14",
        "2026-09-21",
    ]
    assert reading.complete_weeks["2026-08-17"] == {WAF_SERVICE: Decimal("1")}
    assert reading.complete_weeks["2026-09-21"] == {WAF_SERVICE: Decimal("6"), "Amazon S3": Decimal("0.01")}
    assert reading.complete_weeks["2026-09-14"] == {}


def test_services_are_grouped_into_three_categories_for_the_stats_page():
    assert bill_category("Amazon Bedrock") == AI_CATEGORY
    assert bill_category(AGENTCORE_SERVICE) == AI_CATEGORY
    assert bill_category("Claude Haiku 4.5 (Amazon Bedrock Edition)") == AI_CATEGORY
    assert bill_category(WAF_SERVICE) == SECURITY_CATEGORY
    assert bill_category("AWS Secrets Manager") == SECURITY_CATEGORY
    for service in ("AmazonCloudWatch", "Amazon CloudFront", "AWS Lambda", "Amazon DynamoDB", "New"):
        assert bill_category(service) == INFRASTRUCTURE_CATEGORY


def test_a_missing_amount_key_or_date_is_skipped_not_a_crash():
    reading, _ = _fetch(
        _grouped(
            {
                "TimePeriod": {"Start": "2026-09-20"},
                "Groups": [{"Keys": [API_GATEWAY_SERVICE], "Metrics": {}}, {"Metrics": {}}],
            },
            {"Groups": [{"Keys": [WAF_SERVICE], "Metrics": {"UnblendedCost": {"Amount": "5"}}}]},  # no date
        )
    )

    assert reading.usd_30d == _zero()


def test_a_paged_result_is_followed_to_the_end():
    reading, client = _fetch(
        _grouped(_day("2026-09-20", **{API_GATEWAY_SERVICE: "1.00"}), token="page-2"),
        _grouped(_day("2026-09-21", **{API_GATEWAY_SERVICE: "2.00", AGENTCORE_SERVICE: "0.50"})),
    )

    assert reading.usd_30d == _zero(**{API_GATEWAY_SERVICE: "3.00", AGENTCORE_SERVICE: "0.50"})
    assert client.get_cost_and_usage.call_count == 2
    assert client.get_cost_and_usage.call_args.kwargs["NextPageToken"] == "page-2"
