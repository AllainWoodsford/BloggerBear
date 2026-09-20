"""DynamoDB access helpers shared across BloggerBear Lambda handlers.

Ownership split (Phase 1, per docs/project-plan.md and the phase-1 task
briefs):

- `get_table`, and the Topics / Findings helpers in the section below, are
  owned by the research-tick worker (alongside `common/adapters/`).
- The `CandidateIdeas` / `Articles` / `ModerationQueue` helpers further down
  are owned by the daily-cycle worker.
- The "Admin API" section at the bottom (Phase 2) is owned by the
  `admin_api_handler` worker.

All workers add to this one file -- please keep additions inside their own
clearly-labeled section, and keep function names narrowly scoped (e.g.
`get_topic`, `put_finding`) to avoid collisions.

Table names always come from environment variables (TOPICS_TABLE,
FINDINGS_TABLE, ...) -- never hardcode a table name here.
"""
from __future__ import annotations

import os

import boto3
from boto3.dynamodb.conditions import Attr, Key

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


# --- Admin API ---------------------------------------------------------------


def list_topics() -> list[dict]:
    """Return every Topic item (Scan -- acceptable at this project's scale)."""
    table = get_table(os.environ["TOPICS_TABLE"])
    response = table.scan()
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def put_topic(item: dict) -> None:
    """Write (create or overwrite) a Topic item as-is."""
    table = get_table(os.environ["TOPICS_TABLE"])
    table.put_item(Item=item)


def delete_topic(topic_id: str) -> None:
    """Delete a Topic item by `topic_id`."""
    table = get_table(os.environ["TOPICS_TABLE"])
    table.delete_item(Key={"topic_id": topic_id})


def list_candidate_ideas(topic_id: str) -> list[dict]:
    """Return every CandidateIdeas item for a topic, regardless of status.

    This backs the admin "candidates considered but not published" view, so
    unlike a pipeline-internal helper it must not filter by status.
    """
    table = get_table(os.environ["CANDIDATE_IDEAS_TABLE"])
    response = table.query(KeyConditionExpression=Key("topic_id").eq(topic_id))
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.query(
            KeyConditionExpression=Key("topic_id").eq(topic_id),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))
    return items


def list_pending_moderation() -> list[dict]:
    """Return every ModerationQueue item with `status == "pending"`.

    Scan + filter -- acceptable at this project's scale, no GSI.
    """
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    response = table.scan(FilterExpression=Attr("status").eq("pending"))
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression=Attr("status").eq("pending"),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))
    return items


def count_pending_moderation_for_topic(topic_id: str) -> int:
    """Return how many ModerationQueue items are `status == "pending"` for `topic_id`.

    Deliberately returns only a count, never the items themselves --
    public_api_handler.py's GET /topics/{topic_id}/activity surfaces this
    to anonymous visitors (an "N pending review" indicator), and a pending
    article hasn't cleared compliance review yet, so its title/content/
    reasons must never leak through this path. Same Scan + combined-filter
    pattern as list_prompt_refinements above, just returning len() instead
    of the items.
    """
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    filter_expression = Attr("status").eq("pending") & Attr("topic_id").eq(topic_id)
    return len(_paginated_scan(table, filter_expression))


def list_all_moderation_items() -> list[dict]:
    """Return every ModerationQueue item regardless of status.

    Phase 6: backs the admin "what's actually been flagged so far" stats
    view (see admin_api_handler.py's _moderation_queue_stats) -- unlike
    list_pending_moderation above, this must include approved/rejected
    history too, since the whole point is to look back at what compliance
    review has flagged over time, not just what's still open. Same
    Scan-until-no-LastEvaluatedKey pattern as list_pending_moderation,
    just without the status filter.
    """
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    response = table.scan()
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def get_moderation_item(queue_id: str) -> dict | None:
    """Fetch a ModerationQueue item by `queue_id`, or None if it doesn't exist."""
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    response = table.get_item(Key={"queue_id": queue_id})
    return response.get("Item")


def update_moderation_status(queue_id: str, status: str) -> None:
    """Update a ModerationQueue item's `status` field in place."""
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    table.update_item(
        Key={"queue_id": queue_id},
        UpdateExpression="SET #status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": status},
    )


def get_article(article_id: str) -> dict | None:
    """Fetch an Articles item by `article_id`, or None if it doesn't exist."""
    table = get_table(os.environ["ARTICLES_TABLE"])
    response = table.get_item(Key={"article_id": article_id})
    return response.get("Item")


def get_moderation_item_by_article_id(article_id: str) -> dict | None:
    """Fetch the ModerationQueue item for `article_id`, or None if there isn't one.

    The ModerationQueue table's only key is `queue_id` (no article_id GSI),
    so this is a Scan + FilterExpression -- same pattern as
    list_pending_moderation above, acceptable at this project's scale.
    Backs the force-publish admin route's best-effort moderation-status
    consistency (see admin_api_handler.py's _publish_article): at most one
    ModerationQueue item should ever exist per article_id, so the first
    match is returned.
    """
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    response = table.scan(FilterExpression=Attr("article_id").eq(article_id))
    items = response.get("Items", [])
    return items[0] if items else None


def update_article_status(article_id: str, status: str, published_at: str | None = None) -> None:
    """Update an Articles item's `status` (and optionally `published_at`)."""
    table = get_table(os.environ["ARTICLES_TABLE"])
    update_expression = "SET #status = :status"
    expression_attribute_values = {":status": status}
    if published_at is not None:
        update_expression += ", published_at = :published_at"
        expression_attribute_values[":published_at"] = published_at
    table.update_item(
        Key={"article_id": article_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=expression_attribute_values,
    )


# --- Public API ---------------------------------------------------------


def list_published_articles(topic_id: str | None = None) -> list[dict]:
    """Return every Articles item with `status == "published"`.

    If `topic_id` is given, further filters to that topic. The Articles
    table's only key is `article_id` (no sort key, no topic_id GSI -- see
    infra/modules/app-data/main.tf), so this is a Scan + FilterExpression,
    same pattern as `list_pending_moderation` above -- acceptable at this
    project's scale.
    """
    table = get_table(os.environ["ARTICLES_TABLE"])
    filter_expression = Attr("status").eq("published")
    if topic_id is not None:
        filter_expression = filter_expression & Attr("topic_id").eq(topic_id)

    response = table.scan(FilterExpression=filter_expression)
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression=filter_expression,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))
    return items


def increment_view_count(article_id: str) -> int:
    """Atomically increment an Articles item's `view_count` and return the new value.

    Uses `ADD view_count :incr`, which DynamoDB initializes to the operand
    if the attribute doesn't exist yet.
    """
    table = get_table(os.environ["ARTICLES_TABLE"])
    response = table.update_item(
        Key={"article_id": article_id},
        UpdateExpression="ADD view_count :incr",
        ExpressionAttributeValues={":incr": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(response["Attributes"]["view_count"])


# --- PromptRefinements (Phase 5) ---------------------------------------
#
# Owned by the weekly-reflection-handler worker. PromptRefinements items
# (PK topic_id / SK version, per docs/project-plan.md §5) are proposed by
# `weekly_reflection_handler.py` with status "pending" and approved/rejected
# via the Admin API (below) before `daily_cycle_handler.py` will ever use
# one. `version` is an ISO-8601 timestamp string rather than a sequential
# counter -- it sorts correctly as a plain string and needs no atomic
# increment.
#
# `list_feedback_since` also lives here even though it reads the Feedback
# table: it's a helper for the weekly reflection job, not a Feedback
# CRUD primitive, and reads FEEDBACK_TABLE without redefining it -- that env
# var and the Feedback table's write path belong to the Phase 5
# feedback-collection worker.


def _paginated_scan(table, filter_expression=None) -> list[dict]:
    """Scan a table to completion, optionally filtered, and return all items.

    Local helper for this section only -- same Scan-until-no-LastEvaluatedKey
    pattern used throughout this file (list_topics, list_pending_moderation,
    list_published_articles), just factored out to avoid repeating it four
    more times below.
    """
    scan_kwargs = {"FilterExpression": filter_expression} if filter_expression is not None else {}
    response = table.scan(**scan_kwargs)
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(**scan_kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def put_prompt_refinement(
    topic_id: str,
    version: str,
    rationale: str,
    prompt_changes: str,
    status: str = "pending",
) -> dict:
    """Write a PromptRefinements item and return it.

    `proposed_at` is set equal to `version` -- both are the same ISO-8601
    timestamp; keeping them identical is simpler than tracking two separate
    clock reads for one write.
    """
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])
    item = {
        "topic_id": topic_id,
        "version": version,
        "proposed_at": version,
        "rationale": rationale,
        "prompt_changes": prompt_changes,
        "status": status,
    }
    table.put_item(Item=item)
    return item


def list_prompt_refinements(topic_id: str | None = None, status: str | None = None) -> list[dict]:
    """Return PromptRefinements items, optionally filtered by topic_id and/or status.

    Scan + filter -- same pattern as list_pending_moderation /
    list_published_articles above -- acceptable at this project's scale, no
    GSI. Both filters are optional; either, neither, or both may be given.
    """
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])

    filter_expression = None
    if topic_id is not None:
        filter_expression = Attr("topic_id").eq(topic_id)
    if status is not None:
        status_condition = Attr("status").eq(status)
        filter_expression = (
            status_condition if filter_expression is None else filter_expression & status_condition
        )

    return _paginated_scan(table, filter_expression)


def get_prompt_refinement(topic_id: str, version: str) -> dict | None:
    """Fetch a PromptRefinements item by (topic_id, version), or None."""
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])
    response = table.get_item(Key={"topic_id": topic_id, "version": version})
    return response.get("Item")


def update_prompt_refinement_status(topic_id: str, version: str, status: str) -> None:
    """Update a PromptRefinements item's `status` field in place."""
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])
    table.update_item(
        Key={"topic_id": topic_id, "version": version},
        UpdateExpression="SET #status = :status",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": status},
    )


def get_latest_approved_prompt_refinement(topic_id: str) -> dict | None:
    """Return the most recently approved PromptRefinements item for a topic.

    Returns None if none has been approved yet. ISO-8601 timestamps sort
    correctly as plain strings, so the max `version` string is the most
    recent approval.
    """
    approved = list_prompt_refinements(topic_id=topic_id, status="approved")
    if not approved:
        return None
    return max(approved, key=lambda item: item["version"])


def list_feedback_since(since_iso: str) -> list[dict]:
    """Return every Feedback item with `created_at >= since_iso`.

    Scan + filter, paginated -- same pattern as the other Scan helpers in
    this file. Reads the Feedback table via the FEEDBACK_TABLE env var,
    which the Phase 5 feedback-collection worker defines and writes to; this
    function only reads it.
    """
    table = get_table(os.environ["FEEDBACK_TABLE"])
    filter_expression = Attr("created_at").gte(since_iso)
    return _paginated_scan(table, filter_expression)


# --- Feedback (Phase 5) ------------------------------------------------------
#
# Owned by the public-api worker. Table name comes from the FEEDBACK_TABLE
# env var (PK `article_id`, SK `feedback_id`), wired up by the infra worker.
# Per project-plan.md §7, a Feedback item carries no requester identifier of
# any kind -- no IP, user agent, or session id -- so `put_feedback` doesn't
# even accept such a parameter.


def put_feedback(
    article_id: str,
    feedback_id: str,
    vote: str,
    comment: str | None,
    created_at: str,
) -> None:
    """Write a Feedback item to the Feedback table.

    `comment` is None when no comment was submitted, or when the Bedrock
    redaction-review pass (`common.compliance.bedrock_redact_review`) could
    not confirm the text was safe to store.
    """
    table = get_table(os.environ["FEEDBACK_TABLE"])
    table.put_item(
        Item={
            "article_id": article_id,
            "feedback_id": feedback_id,
            "vote": vote,
            "comment": comment,
            "created_at": created_at,
        }
    )


def update_article_net_votes(article_id: str, delta: int) -> None:
    """Atomically add `delta` to an Articles item's `net_votes` attribute.

    Uses `ADD net_votes :delta`, which DynamoDB initializes to the operand
    if the attribute doesn't exist yet -- same pattern as
    `increment_view_count` above. `delta` is +1 for an upvote, -1 for a
    downvote.
    """
    table = get_table(os.environ["ARTICLES_TABLE"])
    table.update_item(
        Key={"article_id": article_id},
        UpdateExpression="ADD net_votes :delta",
        ExpressionAttributeValues={":delta": delta},
    )


def get_top_voted_articles(topic_id: str, limit: int = 2) -> list[dict]:
    """Return up to `limit` published articles for `topic_id`, best net_votes first.

    The Articles table's only key is `article_id` (no sort key, no topic_id
    GSI), so this is a Scan + FilterExpression, same pattern as
    `list_published_articles` above, followed by an in-Python sort on
    `net_votes` (missing/absent treated as 0). Only articles with
    `net_votes > 0` are eligible -- a net-negative or neutral article isn't
    worth reusing as a few-shot example for future drafts.
    """
    table = get_table(os.environ["ARTICLES_TABLE"])
    filter_expression = Attr("topic_id").eq(topic_id) & Attr("status").eq("published")

    response = table.scan(FilterExpression=filter_expression)
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression=filter_expression,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    positively_voted = [item for item in items if int(item.get("net_votes", 0)) > 0]
    positively_voted.sort(key=lambda item: int(item.get("net_votes", 0)), reverse=True)
    return positively_voted[:limit]


# --- FailedExecutions (DLQ consumer) -------------------------------------
#
# Owned by the dlq_handler worker. A FailedExecutions item is written once
# per message `dlq_handler.py` receives from the pipeline_dlq SQS queue --
# i.e. once per daily_cycle Step Functions execution that exhausted its
# retries (see aws_sfn_state_machine.daily_cycle's Catch state in
# infra/environments/*/main.tf). This is purely an admin-visibility record
# ("what failed and why") -- it does not drive any automatic replay.


def put_failed_execution(
    failure_id: str,
    topic_id: str | None,
    error: dict | str | None,
    raw_message: str,
    created_at: str,
) -> dict:
    """Write a FailedExecutions item and return it.

    `topic_id` and `error` may be None/malformed if the DLQ message body
    didn't parse as expected -- `raw_message` (the untouched SQS message
    body) is always kept so a human can inspect it either way.
    """
    table = get_table(os.environ["FAILED_EXECUTIONS_TABLE"])
    item = {
        "failure_id": failure_id,
        "topic_id": topic_id,
        "error": error,
        "raw_message": raw_message,
        "created_at": created_at,
    }
    table.put_item(Item=item)
    return item


def list_failed_executions() -> list[dict]:
    """Return every FailedExecutions item (Scan -- acceptable at this project's scale)."""
    table = get_table(os.environ["FAILED_EXECUTIONS_TABLE"])
    return _paginated_scan(table)


# --- Musings ---------------------------------------------------------------
#
# Owned by common/musings.py (article musings, called from the three publish
# paths) and musing_feedback_handler.py (periodic feedback musings). A single
# site-wide reverse-chronological feed, same single-hash-key-scan pattern as
# ModerationQueue/FailedExecutions -- small table, no GSI needed at this
# project's scale.


def put_musing(
    *,
    musing_id: str,
    kind: str,
    text: str,
    mood: str,
    created_at: str,
    article_id: str | None = None,
    topic_id: str | None = None,
) -> dict:
    """Write a Musings item and return it. `kind` is "article" or "feedback"."""
    table = get_table(os.environ["MUSINGS_TABLE"])
    item = {
        "musing_id": musing_id,
        "kind": kind,
        "article_id": article_id,
        "topic_id": topic_id,
        "text": text,
        "mood": mood,
        "created_at": created_at,
    }
    table.put_item(Item=item)
    return item


def list_musings(limit: int = 50) -> list[dict]:
    """Return up to `limit` Musings items, most recent first.

    Scan-all (small table, same pattern as list_all_moderation_items) then
    sort/truncate in Python -- there's no sort key to query against here,
    just a hash key.
    """
    table = get_table(os.environ["MUSINGS_TABLE"])
    items = _paginated_scan(table)
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items[:limit]
