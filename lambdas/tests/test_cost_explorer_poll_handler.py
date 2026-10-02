"""Tests for cost_explorer_poll_handler.py: fetches the latest Cost Explorer readings (API Gateway,
AgentCore and AWS WAF, one call) and records them.

Both boundaries (the Cost Explorer fetch, the stats writes) are patched directly and trusted --
test_cost_explorer.py owns the fetch's own correctness, test_stats_tracking.py owns
record_api_gateway_cost's, record_agentcore_cost's and record_waf_cost's."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import cost_explorer_poll_handler
from common.cost_explorer import AGENTCORE_SERVICE, API_GATEWAY_SERVICE, WAF_SERVICE, CostReading


def _windows(api_gateway: str, agentcore: str, waf: str) -> dict:
    return {
        API_GATEWAY_SERVICE: Decimal(api_gateway),
        AGENTCORE_SERVICE: Decimal(agentcore),
        WAF_SERVICE: Decimal(waf),
    }


READING = CostReading(
    usd_30d=_windows("12.34", "0.56", "10.90"),
    week_to_date=_windows("1", "0.1", "2.10"),
    month_to_date=_windows("1", "0.1", "0.72"),
    previous_month=_windows("12", "0.5", "10.80"),
    week_start="2026-09-28",
    month="2026-10",
    previous_month_label="2026-09",
)


def _fetch(**kwargs):
    return patch("cost_explorer_poll_handler.fetch_costs", **kwargs)


def _recorders():
    return (
        patch("cost_explorer_poll_handler.record_api_gateway_cost"),
        patch("cost_explorer_poll_handler.record_agentcore_cost"),
        patch("cost_explorer_poll_handler.record_waf_cost"),
    )


def test_a_successful_poll_records_every_reading_and_reports_them():
    api_gateway_patch, agentcore_patch, waf_patch = _recorders()
    with (
        _fetch(return_value=READING) as mock_fetch,
        api_gateway_patch as mock_api_gateway,
        agentcore_patch as mock_agentcore,
        waf_patch as mock_waf,
    ):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "recorded"
    assert result["api_gateway_cost_usd_30d"] == "12.34"
    assert result["agentcore_cost_usd_30d"] == "0.56"
    assert result["waf_cost_usd_30d"] == "10.90"
    assert result["waf_cost_usd_month_to_date"] == "0.72"
    mock_fetch.assert_called_once()  # one Cost Explorer call for everything
    assert mock_api_gateway.call_args.args == (Decimal("12.34"), result["as_of"])
    assert mock_agentcore.call_args.args == (Decimal("0.56"), result["as_of"])
    assert mock_waf.call_args.kwargs == {
        "usd_30d": Decimal("10.90"),
        "usd_week_to_date": Decimal("2.10"),
        "usd_month_to_date": Decimal("0.72"),
        "usd_previous_month": Decimal("10.80"),
        "month": "2026-10",
        "previous_month": "2026-09",
        "as_of": result["as_of"],
    }


def test_a_failure_fetching_cost_is_reported_not_raised():
    with _fetch(side_effect=RuntimeError("cost explorer not enabled")):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "error"
    assert "cost explorer not enabled" in result["error"]


def test_a_failure_recording_the_reading_is_reported_not_raised():
    _, agentcore_patch, waf_patch = _recorders()
    with (
        _fetch(return_value=READING),
        patch("cost_explorer_poll_handler.record_api_gateway_cost", side_effect=RuntimeError("dynamo down")),
        agentcore_patch,
        waf_patch,
    ):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "error"
    assert "dynamo down" in result["error"]
