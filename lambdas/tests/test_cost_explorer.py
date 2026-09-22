"""Tests for common/cost_explorer.py: summing a rolling 30-day Cost Explorer read for API Gateway.

The real `ce` client is mocked directly, not via moto -- moto's Cost Explorer support (as of the
version pinned here) always returns an empty ResultsByTime regardless of what's "in" the account,
so it can't stand in for real billing data. Same "patch the boundary, trust boto3 itself" approach
as test_bedrock.py takes with the real Bedrock client.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from common.cost_explorer import fetch_api_gateway_cost_usd_30d


def _response(*amounts: str) -> dict:
    return {"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": a, "Unit": "USD"}}} for a in amounts]}


def test_daily_amounts_are_summed_into_one_total():
    client = MagicMock()
    client.get_cost_and_usage.return_value = _response("1.23", "4.56", "0.00")
    with patch("common.cost_explorer._ce_client", return_value=client):
        assert fetch_api_gateway_cost_usd_30d(today=date(2026, 9, 22)) == Decimal("5.79")


def test_no_usage_at_all_is_zero_not_an_error():
    client = MagicMock()
    client.get_cost_and_usage.return_value = _response()
    with patch("common.cost_explorer._ce_client", return_value=client):
        assert fetch_api_gateway_cost_usd_30d(today=date(2026, 9, 22)) == Decimal("0")


def test_the_window_is_30_days_ending_today_exclusive():
    client = MagicMock()
    client.get_cost_and_usage.return_value = _response()
    with patch("common.cost_explorer._ce_client", return_value=client):
        fetch_api_gateway_cost_usd_30d(today=date(2026, 9, 22))

    kwargs = client.get_cost_and_usage.call_args.kwargs
    assert kwargs["TimePeriod"] == {"Start": "2026-08-23", "End": "2026-09-22"}
    assert kwargs["Granularity"] == "DAILY"


def test_the_filter_is_scoped_to_api_gateway_only():
    client = MagicMock()
    client.get_cost_and_usage.return_value = _response()
    with patch("common.cost_explorer._ce_client", return_value=client):
        fetch_api_gateway_cost_usd_30d(today=date(2026, 9, 22))

    kwargs = client.get_cost_and_usage.call_args.kwargs
    assert kwargs["Filter"] == {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon API Gateway"]}}


def test_a_missing_amount_on_a_bucket_is_skipped_not_a_crash():
    client = MagicMock()
    client.get_cost_and_usage.return_value = {"ResultsByTime": [{"Total": {}}]}
    with patch("common.cost_explorer._ce_client", return_value=client):
        assert fetch_api_gateway_cost_usd_30d(today=date(2026, 9, 22)) == Decimal("0")
