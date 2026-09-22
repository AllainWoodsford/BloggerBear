"""Tests for common/lambda_timing.py: self-timed Lambda duration, tallied onto this week's row."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from common.lambda_timing import track_lambda_duration


def test_the_handler_runs_normally_and_its_return_value_is_unchanged():
    @track_lambda_duration("some_function")
    def handler(event, context):
        return {"status": "ok", "event": event}

    with patch("common.lambda_timing.record_lambda_duration"):
        assert handler({"a": 1}, "ctx") == {"status": "ok", "event": {"a": 1}}


def test_a_positive_duration_is_recorded_under_the_given_function_name():
    @track_lambda_duration("research_tick")
    def handler(event, context):
        return "done"

    with patch("common.lambda_timing.record_lambda_duration") as mock_record:
        handler({}, None)

    mock_record.assert_called_once()
    name, duration_ms = mock_record.call_args.args
    assert name == "research_tick"
    assert isinstance(duration_ms, int) and duration_ms >= 0


def test_an_exception_from_the_handler_still_propagates_and_is_still_timed():
    @track_lambda_duration("daily_cycle")
    def handler(event, context):
        raise RuntimeError("boom")

    with patch("common.lambda_timing.record_lambda_duration") as mock_record:
        with pytest.raises(RuntimeError, match="boom"):
            handler({}, None)

    mock_record.assert_called_once_with("daily_cycle", pytest.approx(0, abs=10_000))


def test_a_failure_recording_the_duration_never_breaks_the_real_invocation():
    @track_lambda_duration("musing_feedback")
    def handler(event, context):
        return "the real answer"

    with patch("common.lambda_timing.record_lambda_duration", side_effect=RuntimeError("dynamo down")):
        assert handler({}, None) == "the real answer"


def test_the_wrapped_handler_keeps_its_original_name_and_docstring():
    @track_lambda_duration("x")
    def handler(event, context):
        """The real docstring."""

    assert handler.__name__ == "handler"
    assert handler.__doc__ == "The real docstring."


def test_two_different_functions_are_decorated_independently():
    @track_lambda_duration("a")
    def handler_a(event, context):
        return "a"

    @track_lambda_duration("b")
    def handler_b(event, context):
        return "b"

    with patch("common.lambda_timing.record_lambda_duration") as mock_record:
        handler_a({}, None)
        handler_b({}, None)

    names = [call.args[0] for call in mock_record.call_args_list]
    assert names == ["a", "b"]
