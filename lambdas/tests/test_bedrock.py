from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import common.bedrock as bedrock


def _converse_response(
    text: str, input_tokens: int, output_tokens: int, stop_reason: str | None = None
) -> dict:
    response = {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
        },
    }
    if stop_reason is not None:
        response["stopReason"] = stop_reason
    return response


def test_invoke_claude_unchanged_by_this_pr():
    mock_client = MagicMock()
    mock_client.converse.return_value = _converse_response("hello", 10, 5)
    with patch("common.bedrock._get_client", return_value=mock_client):
        result = bedrock.invoke_claude("a prompt", "some-model")
    assert result == "hello"
    mock_client.converse.assert_called_once_with(
        modelId="some-model",
        messages=[{"role": "user", "content": [{"text": "a prompt"}]}],
        inferenceConfig={"maxTokens": 1024},
    )


def test_invoke_model_tracked_primary_success():
    mock_client = MagicMock()
    mock_client.converse.return_value = _converse_response("hi there", 12, 8)
    with patch("common.bedrock._get_client", return_value=mock_client):
        result = bedrock.invoke_model_tracked("prompt", "primary-model")

    assert result == {
        "text": "hi there",
        "model_id": "primary-model",
        "input_tokens": 12,
        "output_tokens": 8,
        "used_fallback": False,
        "stop_reason": None,
        "attempts": 1,
    }
    mock_client.converse.assert_called_once()
    assert mock_client.converse.call_args.kwargs["modelId"] == "primary-model"


def test_invoke_model_tracked_falls_back_on_primary_failure():
    mock_client = MagicMock()
    mock_client.converse.side_effect = [
        RuntimeError("primary model unavailable"),
        _converse_response("fallback text", 20, 10),
    ]
    with patch("common.bedrock._get_client", return_value=mock_client):
        result = bedrock.invoke_model_tracked("prompt", "primary-model", fallback_model_id="fallback-model")

    assert result == {
        "text": "fallback text",
        "model_id": "fallback-model",
        "input_tokens": 20,
        "output_tokens": 10,
        "used_fallback": True,
        "stop_reason": None,
        "attempts": 1,
    }
    assert mock_client.converse.call_count == 2
    assert mock_client.converse.call_args_list[0].kwargs["modelId"] == "primary-model"
    assert mock_client.converse.call_args_list[1].kwargs["modelId"] == "fallback-model"


def test_invoke_model_tracked_no_fallback_configured_propagates():
    mock_client = MagicMock()
    mock_client.converse.side_effect = RuntimeError("primary model unavailable")
    with patch("common.bedrock._get_client", return_value=mock_client):
        try:
            bedrock.invoke_model_tracked("prompt", "primary-model")
            raised = False
        except RuntimeError:
            raised = True
    assert raised
    mock_client.converse.assert_called_once()


def test_invoke_model_tracked_both_primary_and_fallback_fail_propagates():
    mock_client = MagicMock()
    mock_client.converse.side_effect = [
        RuntimeError("primary model unavailable"),
        RuntimeError("fallback model also unavailable"),
    ]
    with patch("common.bedrock._get_client", return_value=mock_client):
        try:
            bedrock.invoke_model_tracked("prompt", "primary-model", fallback_model_id="fallback-model")
            raised = False
        except RuntimeError as exc:
            raised = True
            assert "fallback model also unavailable" in str(exc)
    assert raised
    assert mock_client.converse.call_count == 2


# --- a reply that is cut off ---------------------------------------------------------------------

_PROFILE = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
_ARN = f"arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/{_PROFILE}"


def _invoke(responses, **kwargs):
    client = MagicMock()
    client.converse.side_effect = responses
    with patch("common.bedrock._get_client", return_value=client):
        result = bedrock.invoke_model_tracked("prompt", kwargs.pop("model_id", "primary-model"), **kwargs)
    return result, client


def test_the_stop_reason_is_surfaced():
    result, _ = _invoke([_converse_response("done", 10, 5, stop_reason="end_turn")])

    assert result["stop_reason"] == "end_turn" and result["attempts"] == 1


def test_a_response_with_no_stop_reason_reports_none():
    result, _ = _invoke([_converse_response("done", 10, 5)])

    assert result["stop_reason"] is None


def test_a_cut_off_reply_is_reported_as_such_when_no_retry_is_asked_for():
    result, client = _invoke(
        [_converse_response("The article stops mid-wo", 10, 1024, stop_reason="max_tokens")]
    )

    assert result["stop_reason"] == "max_tokens" and result["attempts"] == 1
    client.converse.assert_called_once()


def test_a_cut_off_reply_is_retried_once_with_the_larger_limit():
    result, client = _invoke(
        [
            _converse_response("The article stops mid-wo", 100, 1024, stop_reason="max_tokens"),
            _converse_response("The article, complete.", 100, 1500, stop_reason="end_turn"),
        ],
        max_tokens=1024,
        retry_max_tokens=4096,
    )

    assert result["text"] == "The article, complete."
    assert result["stop_reason"] == "end_turn" and result["attempts"] == 2
    limits = [call.kwargs["inferenceConfig"]["maxTokens"] for call in client.converse.call_args_list]
    assert limits == [1024, 4096]


def test_the_cost_of_the_discarded_first_attempt_is_counted():
    result, _ = _invoke(
        [
            _converse_response("cut", 100, 1024, stop_reason="max_tokens"),
            _converse_response("whole", 100, 1500, stop_reason="end_turn"),
        ],
        max_tokens=1024,
        retry_max_tokens=4096,
    )

    assert (result["input_tokens"], result["output_tokens"]) == (200, 2524)


def test_a_reply_still_cut_off_after_the_retry_says_so():
    result, client = _invoke(
        [
            _converse_response("cut", 100, 1024, stop_reason="max_tokens"),
            _converse_response("still cut", 100, 4096, stop_reason="max_tokens"),
        ],
        max_tokens=1024,
        retry_max_tokens=4096,
    )

    assert result["text"] == "still cut" and result["stop_reason"] == "max_tokens"
    assert result["attempts"] == 2 and client.converse.call_count == 2  # one retry, never a loop


def test_if_the_retry_itself_fails_the_first_reply_is_kept_and_still_marked_cut_off():
    result, _ = _invoke(
        [
            _converse_response("the first, cut off", 100, 1024, stop_reason="max_tokens"),
            RuntimeError("throttled"),
        ],
        max_tokens=1024,
        retry_max_tokens=4096,
    )

    assert result["text"] == "the first, cut off"
    assert result["stop_reason"] == "max_tokens" and result["attempts"] == 1
    assert (result["input_tokens"], result["output_tokens"]) == (100, 1024)


@pytest.mark.parametrize("retry_max", [None, 0, 1024, 512])
def test_no_retry_unless_the_retry_limit_is_larger_than_the_first(retry_max):
    _, client = _invoke(
        [_converse_response("cut", 10, 1024, stop_reason="max_tokens")],
        max_tokens=1024,
        retry_max_tokens=retry_max,
    )

    client.converse.assert_called_once()


def test_a_reply_that_finished_is_never_retried():
    _, client = _invoke(
        [_converse_response("whole", 10, 200, stop_reason="end_turn")], max_tokens=1024, retry_max_tokens=4096
    )

    client.converse.assert_called_once()


def test_the_retry_goes_to_the_model_that_produced_the_cut_off_reply_the_fallback():
    result, client = _invoke(
        [
            RuntimeError("primary unavailable"),
            _converse_response("cut", 10, 1024, stop_reason="max_tokens"),
            _converse_response("whole", 10, 1500, stop_reason="end_turn"),
        ],
        fallback_model_id="fallback-model",
        max_tokens=1024,
        retry_max_tokens=4096,
    )

    models = [call.kwargs["modelId"] for call in client.converse.call_args_list]
    assert models == ["primary-model", "fallback-model", "fallback-model"]
    assert result["used_fallback"] is True and result["model_id"] == "fallback-model"


def test_the_retry_is_invoked_by_the_configured_id_but_recorded_by_the_canonical_one():
    result, client = _invoke(
        [
            _converse_response("cut", 10, 1024, stop_reason="max_tokens"),
            _converse_response("whole", 10, 1500, stop_reason="end_turn"),
        ],
        model_id=_ARN,
        max_tokens=1024,
        retry_max_tokens=4096,
    )

    assert [call.kwargs["modelId"] for call in client.converse.call_args_list] == [_ARN, _ARN]
    assert result["model_id"] == _PROFILE
