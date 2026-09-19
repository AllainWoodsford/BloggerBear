"""Generic Amazon Bedrock invocation helper.

Shared by both Lambda handlers. Keep this file free of any prompt content --
prompt text is owned by whichever handler needs it, not by this module.

Uses Bedrock's Converse API rather than the per-provider InvokeModel body
schema (e.g. Anthropic's "anthropic_version"/messages shape) specifically so
`model_id` isn't locked to one model family: Converse normalizes the
request/response shape across every provider it supports (Anthropic, Amazon
Nova, Meta, Mistral, Cohere, ...), so swapping var.bedrock_model_id to a
different provider's model ID or cross-region inference profile works
without a code change here, as long as Bedrock's Converse API supports that
model.
"""
from __future__ import annotations

import boto3

_bedrock_runtime_client = None


def _get_client():
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        _bedrock_runtime_client = boto3.client("bedrock-runtime")
    return _bedrock_runtime_client


def invoke_claude(prompt: str, model_id: str, max_tokens: int = 1024) -> str:
    """Invoke a Bedrock model with a single user-turn prompt via Converse.

    Returns the concatenated text of every text content block in the
    response message.
    """
    client = _get_client()
    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens},
    )
    blocks = response["output"]["message"]["content"]
    return "".join(block.get("text", "") for block in blocks if "text" in block)
