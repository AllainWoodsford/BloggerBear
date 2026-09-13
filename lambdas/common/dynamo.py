"""DynamoDB access helpers shared across BloggerBear Lambda handlers.

Ownership split (Phase 1, per docs/project-plan.md and the phase-1 task
briefs):

- `get_table`, and the Topics / Findings helpers in the section below, are
  owned by the research-tick worker (alongside `common/adapters/`).
- The `CandidateIdeas` / `Articles` / `ModerationQueue` helpers further down
  are owned by the daily-cycle worker.

Both workers add to this one file -- please keep additions inside their own
clearly-labeled section, and keep function names narrowly scoped (e.g.
`get_topic`, `put_finding`) to avoid collisions.

Table names always come from environment variables (TOPICS_TABLE,
FINDINGS_TABLE, ...) -- never hardcode a table name here.
"""
from __future__ import annotations

import os

import boto3
from boto3.dynamodb.conditions import Key

_dynamodb_resource = None


def get_table(name: str):
    """Return a boto3 DynamoDB Table resource for the given table name."""
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource.Table(name)


# --- Topics / Findings (research-tick worker) -------------------------------


def get_topic(topic_id: str) -> dict | None:
    """Fetch a Topic item by `topic_id` from the Topics table.

    Returns None if no such topic exists.
    """
    table = get_table(os.environ["TOPICS_TABLE"])
    response = table.get_item(Key={"topic_id": topic_id})
    return response.get("Item")


def put_finding(
    topic_id: str,
    captured_at: str,
    expires_at: int,
    summary: str,
    raw_snapshot_s3_key: str,
    source_refs: list[dict],
) -> None:
    """Write a Finding item to the Findings table.

    `expires_at` is an epoch-seconds int -- Terraform configures this
    attribute name as the table's TTL attribute.
    """
    table = get_table(os.environ["FINDINGS_TABLE"])
    table.put_item(
        Item={
            "topic_id": topic_id,
            "captured_at": captured_at,
            "expires_at": expires_at,
            "summary": summary,
            "raw_snapshot_s3_key": raw_snapshot_s3_key,
            "source_refs": source_refs,
        }
    )


def get_latest_finding(topic_id: str) -> dict | None:
    """Fetch the most recent Finding for `topic_id`, or None if there isn't one.

    Queries the Findings table ordered by `captured_at` descending
    (`ScanIndexForward=False`) and takes the first result.
    """
    table = get_table(os.environ["FINDINGS_TABLE"])
    response = table.query(
        KeyConditionExpression=Key("topic_id").eq(topic_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


# --- CandidateIdeas / Articles / ModerationQueue (daily-cycle worker) -------


def list_recent_findings(topic_id: str, limit: int = 5) -> list[dict]:
    """Return up to `limit` most recent Findings items for a topic."""
    table = get_table(os.environ["FINDINGS_TABLE"])
    response = table.query(
        KeyConditionExpression=Key("topic_id").eq(topic_id),
        ScanIndexForward=False,
        Limit=limit,
    )
    return response.get("Items", [])


def put_candidate_idea(
    topic_id: str,
    created_at: str,
    angle: str,
    status: str = "considered",
) -> dict:
    """Write (or overwrite) a CandidateIdeas item and return it.

    Call again with the same (topic_id, created_at) key and an updated
    `status` to move an idea from "considered" to "selected".
    """
    table = get_table(os.environ["CANDIDATE_IDEAS_TABLE"])
    item = {
        "topic_id": topic_id,
        "created_at": created_at,
        "angle": angle,
        "status": status,
    }
    table.put_item(Item=item)
    return item


def put_article(
    *,
    article_id: str,
    topic_id: str,
    title: str,
    body_s3_key: str,
    status: str,
    created_at: str,
    published_at: str | None = None,
    source_refs: list[dict] | None = None,
) -> dict:
    """Write an Articles item and return it."""
    table = get_table(os.environ["ARTICLES_TABLE"])
    item = {
        "article_id": article_id,
        "topic_id": topic_id,
        "title": title,
        "body_s3_key": body_s3_key,
        "status": status,
        "created_at": created_at,
        "published_at": published_at,
        "source_refs": source_refs or [],
    }
    table.put_item(Item=item)
    return item


def put_moderation_item(
    *,
    queue_id: str,
    article_id: str,
    topic_id: str,
    reasons: list[str],
    created_at: str,
    status: str = "pending",
) -> dict:
    """Write a ModerationQueue item and return it."""
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    item = {
        "queue_id": queue_id,
        "article_id": article_id,
        "topic_id": topic_id,
        "reasons": reasons,
        "status": status,
        "created_at": created_at,
    }
    table.put_item(Item=item)
    return item
