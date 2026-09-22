"""Tests for cost_explorer_poll_handler.py: fetches the latest Cost Explorer reading and records it.

Both boundaries (the Cost Explorer fetch, the stats write) are patched directly and trusted --
test_cost_explorer.py owns the fetch's own correctness, test_stats_tracking.py owns
record_api_gateway_cost's."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import cost_explorer_poll_handler


def test_a_successful_poll_records_the_reading_and_reports_it():
    with (
        patch(
            "cost_explorer_poll_handler.fetch_api_gateway_cost_usd_30d",
            return_value=Decimal("12.34"),
        ),
        patch("cost_explorer_poll_handler.record_api_gateway_cost") as mock_record,
    ):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "recorded"
    assert result["api_gateway_cost_usd_30d"] == "12.34"
    assert "as_of" in result
    mock_record.assert_called_once()
    cost_arg, as_of_arg = mock_record.call_args.args
    assert cost_arg == Decimal("12.34")
    assert as_of_arg == result["as_of"]


def test_a_failure_fetching_cost_is_reported_not_raised():
    with patch(
        "cost_explorer_poll_handler.fetch_api_gateway_cost_usd_30d",
        side_effect=RuntimeError("cost explorer not enabled"),
    ):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "error"
    assert "cost explorer not enabled" in result["error"]


def test_a_failure_recording_the_reading_is_reported_not_raised():
    with (
        patch(
            "cost_explorer_poll_handler.fetch_api_gateway_cost_usd_30d",
            return_value=Decimal("1.00"),
        ),
        patch(
            "cost_explorer_poll_handler.record_api_gateway_cost",
            side_effect=RuntimeError("dynamo down"),
        ),
    ):
        result = cost_explorer_poll_handler.handler({}, None)

    assert result["status"] == "error"
    assert "dynamo down" in result["error"]
