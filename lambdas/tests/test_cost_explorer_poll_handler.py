"""Tests for cost_explorer_poll_handler.py: fetches the latest Cost Explorer readings (API Gateway
and AgentCore, one call) and records both.

Both boundaries (the Cost Explorer fetch, the stats writes) are patched directly and trusted --
test_cost_explorer.py owns the fetch's own correctness, test_stats_tracking.py owns
record_api_gateway_cost's and record_agentcore_cost's."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import cost_explorer_poll_handler
from common.cost_explorer import AGENTCORE_SERVICE, API_GATEWAY_SERVICE

COSTS = {API_GATEWAY_SERVICE: Decimal("12.34"), AGENTCORE_SERVICE: Decimal("0.56")}


def _fetch(**kwargs):
    return patch("cost_explorer_poll_handler.fetch_service_costs_usd_30d", **kwargs)


def test_a_successful_poll_records_both_readings_and_reports_them():
    with (
        _fetch(return_value=dict(COSTS)) as mock_fetch,
        patch("cost_explorer_poll_handler.record_api_gateway_cost") as mock_api_gateway,
        patch("cost_explorer_poll_handler.record_agentcore_cost") as mock_agentcore,
    ):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "recorded"
    assert result["api_gateway_cost_usd_30d"] == "12.34"
    assert result["agentcore_cost_usd_30d"] == "0.56"
    mock_fetch.assert_called_once()  # one Cost Explorer call for both services
    assert mock_api_gateway.call_args.args == (Decimal("12.34"), result["as_of"])
    assert mock_agentcore.call_args.args == (Decimal("0.56"), result["as_of"])


def test_a_failure_fetching_cost_is_reported_not_raised():
    with _fetch(side_effect=RuntimeError("cost explorer not enabled")):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "error"
    assert "cost explorer not enabled" in result["error"]


def test_a_failure_recording_the_reading_is_reported_not_raised():
    with (
        _fetch(return_value=dict(COSTS)),
        patch("cost_explorer_poll_handler.record_api_gateway_cost", side_effect=RuntimeError("dynamo down")),
        patch("cost_explorer_poll_handler.record_agentcore_cost"),
    ):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "error"
    assert "dynamo down" in result["error"]
