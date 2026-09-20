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


def _invoke_once(client, prompt: str, model_id: str, max_tokens: int) -> dict:
    """One untracked Converse call -- shared by both attempts in
    invoke_model_tracked below. Returns {"text", "input_tokens",
    "output_tokens"}. The Converse API's `usage` block is a top-level
    sibling of `output` (`{inputTokens, outputTokens, totalTokens}`), not
    nested under it.
    """
    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens},
    )
    blocks = response["output"]["message"]["content"]
    text = "".join(block.get("text", "") for block in blocks if "text" in block)
    usage = response.get("usage", {})
    return {
        "text": text,
        "input_tokens": int(usage.get("inputTokens", 0)),
        "output_tokens": int(usage.get("outputTokens", 0)),
    }


def invoke_model_tracked(
    prompt: str,
    model_id: str,
    *,
    fallback_model_id: str | None = None,
    max_tokens: int = 1024,
) -> dict:
    """Like invoke_claude, but returns token usage and which model actually
    produced the response -- the building block for per-article lineage
    (docs/project-plan.md §11, PR 1 of 5). Returns:

        {"text", "model_id", "input_tokens", "output_tokens", "used_fallback"}

    On any exception from `model_id`: if `fallback_model_id` is set, retry
    once with it. If that also fails, or no fallback was configured, the
    exception propagates -- this function does not swallow errors itself;
    each caller's own top-level "never raise unhandled" guard is what's
    meant to catch it, same as every other Bedrock call in this codebase.
    """
    client = _get_client()
    try:
        result = _invoke_once(client, prompt, model_id, max_tokens)
        return {**result, "model_id": model_id, "used_fallback": False}
    except Exception:
        if not fallback_model_id:
            raise
        result = _invoke_once(client, prompt, fallback_model_id, max_tokens)
        return {**result, "model_id": fallback_model_id, "used_fallback": True}
