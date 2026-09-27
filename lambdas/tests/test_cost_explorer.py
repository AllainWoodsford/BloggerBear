"""Tests for common/cost_explorer.py: one rolling 30-day Cost Explorer read for API Gateway and
AgentCore, grouped by service.

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
    fetch_service_costs_usd_30d,
)

TODAY = date(2026, 9, 22)


def _day(**amounts: str) -> dict:
    """One DAILY bucket, grouped by service: {service: amount}."""
    return {
        "Groups": [
            {"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}
            for service, amount in amounts.items()
        ]
    }


def _fetch(*responses: dict):
    client = MagicMock()
    client.get_cost_and_usage.side_effect = list(responses)
    with patch("common.cost_explorer._ce_client", return_value=client):
        return fetch_service_costs_usd_30d(today=TODAY), client


def _grouped(*days: dict, token: str | None = None) -> dict:
    response = {"ResultsByTime": list(days)}
    if token:
        response["NextPageToken"] = token
    return response


def test_both_services_are_summed_per_service_from_one_call():
    costs, client = _fetch(
        _grouped(
            _day(**{API_GATEWAY_SERVICE: "1.23", AGENTCORE_SERVICE: "0.07"}),
            _day(**{API_GATEWAY_SERVICE: "4.56", AGENTCORE_SERVICE: "0.14"}),
        )
    )

    assert costs == {API_GATEWAY_SERVICE: Decimal("5.79"), AGENTCORE_SERVICE: Decimal("0.21")}
    assert client.get_cost_and_usage.call_count == 1


def test_a_service_with_no_rows_is_zero_not_missing():
    costs, _ = _fetch(_grouped(_day(**{API_GATEWAY_SERVICE: "2.00"})))

    assert costs == {API_GATEWAY_SERVICE: Decimal("2.00"), AGENTCORE_SERVICE: Decimal("0")}


def test_no_usage_at_all_is_zero_for_every_service():
    costs, _ = _fetch(_grouped())

    assert costs == {service: Decimal("0") for service in POLLED_SERVICES}


def test_the_call_is_30_days_ending_today_exclusive_filtered_and_grouped_by_service():
    _, client = _fetch(_grouped())

    kwargs = client.get_cost_and_usage.call_args.kwargs
    assert kwargs["TimePeriod"] == {"Start": "2026-08-23", "End": "2026-09-22"}
    assert kwargs["Granularity"] == "DAILY"
    assert kwargs["Filter"] == {"Dimensions": {"Key": "SERVICE", "Values": list(POLLED_SERVICES)}}
    assert kwargs["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]


def test_the_api_gateway_service_name_is_unchanged():
    assert API_GATEWAY_SERVICE == "Amazon API Gateway"


def test_a_service_not_asked_for_is_ignored():
    costs, _ = _fetch(_grouped(_day(**{"Amazon DynamoDB": "9.99", API_GATEWAY_SERVICE: "1.00"})))

    assert "Amazon DynamoDB" not in costs and costs[API_GATEWAY_SERVICE] == Decimal("1.00")


def test_a_missing_amount_or_key_on_a_group_is_skipped_not_a_crash():
    costs, _ = _fetch(_grouped({"Groups": [{"Keys": [API_GATEWAY_SERVICE], "Metrics": {}}, {"Metrics": {}}]}))

    assert costs[API_GATEWAY_SERVICE] == Decimal("0")


def test_a_paged_result_is_followed_to_the_end():
    costs, client = _fetch(
        _grouped(_day(**{API_GATEWAY_SERVICE: "1.00"}), token="page-2"),
        _grouped(_day(**{API_GATEWAY_SERVICE: "2.00", AGENTCORE_SERVICE: "0.50"})),
    )

    assert costs == {API_GATEWAY_SERVICE: Decimal("3.00"), AGENTCORE_SERVICE: Decimal("0.50")}
    assert client.get_cost_and_usage.call_count == 2
    assert client.get_cost_and_usage.call_args.kwargs["NextPageToken"] == "page-2"
