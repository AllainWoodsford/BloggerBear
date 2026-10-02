"""Tests for common/cost_explorer.py: one Cost Explorer read for API Gateway, AgentCore and AWS WAF,
grouped by service, counted into a rolling 30 days, this week, this month and last month.

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
    API_GATEWAY_SERVICE,
    POLLED_SERVICES,
    WAF_SERVICE,
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
    assert kwargs["TimePeriod"] == {"Start": "2026-09-01", "End": "2026-10-03"}  # last month began first
    assert kwargs["Granularity"] == "DAILY"
    assert kwargs["Filter"] == {"Dimensions": {"Key": "SERVICE", "Values": list(POLLED_SERVICES)}}
    assert kwargs["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]


def test_early_in_a_month_the_30_days_start_before_last_month_does():
    _, client = _fetch(_grouped(), today=date(2026, 3, 1))

    # 30 days before 1 March is 30 January, earlier than 1 February.
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


def test_a_service_not_asked_for_is_ignored():
    reading, _ = _fetch(
        _grouped(_day("2026-09-20", **{"Amazon DynamoDB": "9.99", API_GATEWAY_SERVICE: "1.00"}))
    )

    assert "Amazon DynamoDB" not in reading.usd_30d and reading.usd_30d[API_GATEWAY_SERVICE] == Decimal(
        "1.00"
    )


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
