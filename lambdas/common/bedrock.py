"""Generic Amazon Bedrock (Claude) invocation helper.

Shared by both Lambda handlers. Keep this file free of any prompt content --
prompt text is owned by whichever handler needs it, not by this module.
"""
from __future__ import annotations

import json

import boto3

BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"

_bedrock_runtime_client = None


def _get_client():
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        _bedrock_runtime_client = boto3.client("bedrock-runtime")
    return _bedrock_runtime_client


def invoke_claude(prompt: str, model_id: str, max_tokens: int = 1024) -> str:
    """Invoke a Claude model on Bedrock with a single user-turn prompt.

    Returns the concatenated text of every text content block in the
    response (Bedrock's Anthropic Messages-style response shape).
    """
    client = _get_client()
    body = {
        "anthropic_version": BEDROCK_ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(response["body"].read())
    blocks = payload.get("content", [])
    return "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
