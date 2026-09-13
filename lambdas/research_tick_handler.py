"""Hourly research-tick Lambda.

Phase 1 scope: manually invoked per-topic. `event` is `{"topic_id": str}`.
Iterating over every Topic on a schedule is Phase 3 (EventBridge Scheduler)
-- do not add a topic-iteration loop here.

Flow (per docs/project-plan.md §4 "Hourly Research Tick" and §2 rule 2 --
diff-first, no Bedrock call unless something material changed):
  1. Load the Topic.
  2. Instantiate its adapter and fetch current state.
  3. Load the prior state (if any) from the last Finding's S3 snapshot.
  4. Diff. If unchanged, stop -- no Bedrock call.
  5. If changed, summarize with Bedrock, store the raw snapshot in S3, and
     write a new Finding.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import boto3

from common.adapters.github_trending import GitHubTrendingAdapter
from common.bedrock import invoke_claude
from common.dynamo import get_latest_finding, get_topic, put_finding

FINDING_TTL_DAYS = 14
COMPACT_STATE_MAX_CHARS = 4000

# Maps a Topic's `adapter` field to the concrete Adapter implementation.
# Adding a new domain means adding one line here plus a new adapter module --
# no change to the flow below (docs/project-plan.md §2 rule 5).
ADAPTER_REGISTRY = {
    "github_trending": GitHubTrendingAdapter,
}

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _snapshot_key(topic_id: str, captured_at: str) -> str:
    return f"snapshots/{topic_id}/{captured_at}.json"


def _load_prior_state(bucket: str, s3_key: str) -> dict:
    s3 = _get_s3_client()
    response = s3.get_object(Bucket=bucket, Key=s3_key)
    return json.loads(response["Body"].read())


def _build_prompt(topic: dict, diff_summary: str, new_state: dict) -> str:
    compact_state = json.dumps(new_state)[:COMPACT_STATE_MAX_CHARS]
    topic_label = topic.get("name") or topic.get("topic_id")
    return (
        f'You are monitoring the topic "{topic_label}" for a research '
        "digest. A material change was just detected in its source data.\n\n"
        f"What changed: {diff_summary}\n\n"
        f"Current state (compact JSON, may be truncated): {compact_state}\n\n"
        "Summarize what changed and why it might be interesting to a reader "
        "following this topic. Be concise (2-4 sentences). Do not speculate "
        "beyond what the data shows, and do not give financial or "
        "investment advice."
    )


def handler(event, context) -> dict:
    topic_id = event["topic_id"]

    topic = get_topic(topic_id)
    if topic is None:
        return {"status": "error", "reason": f"unknown topic_id: {topic_id}"}

    adapter_key = topic.get("adapter")
    adapter_cls = ADAPTER_REGISTRY.get(adapter_key)
    if adapter_cls is None:
        return {"status": "error", "reason": f"unknown adapter: {adapter_key}"}

    adapter = adapter_cls()
    new_state = adapter.fetch_state(topic)

    bucket = os.environ["CONTENT_BUCKET"]

    prior_finding = get_latest_finding(topic_id)
    old_state = None
    if prior_finding is not None:
        old_state = _load_prior_state(bucket, prior_finding["raw_snapshot_s3_key"])

    changed, diff_summary = adapter.material_diff(old_state, new_state)
    if not changed:
        return {"status": "no_change"}

    model_id = os.environ["BEDROCK_MODEL_ID"]
    prompt = _build_prompt(topic, diff_summary, new_state)
    summary = invoke_claude(prompt, model_id)

    captured_at = new_state.get("fetched_at") or datetime.now(UTC).isoformat()
    snapshot_key = _snapshot_key(topic_id, captured_at)

    s3 = _get_s3_client()
    s3.put_object(
        Bucket=bucket,
        Key=snapshot_key,
        Body=json.dumps(new_state).encode("utf-8"),
        ContentType="application/json",
    )

    expires_at = int((datetime.now(UTC) + timedelta(days=FINDING_TTL_DAYS)).timestamp())
    refs = adapter.source_refs(new_state)

    put_finding(
        topic_id=topic_id,
        captured_at=captured_at,
        expires_at=expires_at,
        summary=summary,
        raw_snapshot_s3_key=snapshot_key,
        source_refs=refs,
    )

    return {"status": "material_change", "summary": summary}
