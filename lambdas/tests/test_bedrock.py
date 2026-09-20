from __future__ import annotations

from unittest.mock import MagicMock, patch

import common.bedrock as bedrock


def _converse_response(text: str, input_tokens: int, output_tokens: int) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
        },
    }


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
        result = bedrock.invoke_model_tracked(
            "prompt", "primary-model", fallback_model_id="fallback-model"
        )

    assert result == {
        "text": "fallback text",
        "model_id": "fallback-model",
        "input_tokens": 20,
        "output_tokens": 10,
        "used_fallback": True,
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
            bedrock.invoke_model_tracked(
                "prompt", "primary-model", fallback_model_id="fallback-model"
            )
            raised = False
        except RuntimeError as exc:
            raised = True
            assert "fallback model also unavailable" in str(exc)
    assert raised
    assert mock_client.converse.call_count == 2
