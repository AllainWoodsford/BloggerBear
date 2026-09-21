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
from decimal import Decimal

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
    research_call: dict | None = None,
) -> None:
    """Write a Finding item to the Findings table.

    `expires_at` is an epoch-seconds int -- Terraform configures this
    attribute name as the table's TTL attribute.

    `research_call` is the Bedrock call that produced `summary` --
    {"model_id", "input_tokens", "output_tokens", "used_fallback"} -- kept on
    the Finding so the research spend can be tallied into whichever article the
    Finding ends up feeding (common/costing.py's build_research_lineage). A
    Finding written before research tracking has none.
    """
    table = get_table(os.environ["FINDINGS_TABLE"])
    item = {
        "topic_id": topic_id,
        "captured_at": captured_at,
        "expires_at": expires_at,
        "summary": summary,
        "raw_snapshot_s3_key": raw_snapshot_s3_key,
        "source_refs": source_refs,
    }
    if research_call is not None:
        item["research_call"] = research_call
    table.put_item(Item=item)


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


def list_recent_findings(topic_id: str, limit: int = 5, since: str | None = None) -> list[dict]:
    """Return up to `limit` most recent Findings items for a topic, newest first.

    `since` is an ISO-8601 UTC timestamp; when given, only Findings captured at
    or after it are returned (the sort key is the same ISO string, so this is a
    range condition on it).
    """
    table = get_table(os.environ["FINDINGS_TABLE"])
    condition = Key("topic_id").eq(topic_id)
    if since is not None:
        condition = condition & Key("captured_at").gte(since)
    response = table.query(
        KeyConditionExpression=condition,
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


def _lineage_to_item(lineage: dict | None) -> dict | None:
    """Convert a lineage dict's numeric fields to Decimal for DynamoDB
    storage -- token counts and the nullable cost_aud float, both at the
    top level and inside each `calls` entry. Mirrors put_model's
    Decimal(str(x)) conversion (DynamoDB's boto3 resource rejects native
    float, and Decimal(x) on a binary float preserves its ugly exact
    representation instead of the decimal value meant)."""
    if lineage is None:
        return None
    return _floats_to_decimal(lineage)


def _floats_to_decimal(value):
    """Recursively swap every float for Decimal(str(x)), leaving ints, strings,
    bools and None alone. Recursive because a lineage nests numbers at several
    depths (its own calls, and the `research` block's calls and totals)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _floats_to_decimal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_floats_to_decimal(item) for item in value]
    return value


# Lineage fields that are money (float); every other number in a lineage is a count.
_LINEAGE_COST_KEYS = frozenset({"cost_aud", "total_cost_aud"})


def _decimal_to_number(value, key=None):
    """Recursive inverse of _floats_to_decimal: Decimal back to float for the
    cost fields and to int for every count."""
    if isinstance(value, Decimal):
        return float(value) if key in _LINEAGE_COST_KEYS else int(value)
    if isinstance(value, dict):
        return {k: _decimal_to_number(item, k) for k, item in value.items()}
    if isinstance(value, list):
        return [_decimal_to_number(item, key) for item in value]
    return value


def _lineage_from_item(lineage: dict | None) -> dict | None:
    """Inverse of _lineage_to_item -- Decimal back to int/float after a
    read, the mirror of list_models'/get_model's price-field conversion
    (json.dumps can't serialize Decimal)."""
    if lineage is None:
        return None
    return _decimal_to_number(lineage)


def update_article_lineage(article_id: str, lineage: dict) -> None:
    """Replace an existing Articles item's `lineage` (used by the lineage
    backfill, which recomputes cost once pricing is known). Refuses to create
    an article that doesn't exist."""
    table = get_table(os.environ["ARTICLES_TABLE"])
    table.update_item(
        Key={"article_id": article_id},
        UpdateExpression="SET lineage = :lineage",
        ConditionExpression="attribute_exists(article_id)",
        ExpressionAttributeValues={":lineage": _lineage_to_item(lineage)},
    )


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
    lineage: dict | None = None,
    published_by: str | None = None,
) -> dict:
    """Write an Articles item and return it.

    `lineage` (docs/project-plan.md §11, PR 2 of 5) is fixed once at draft
    time and never changes afterward, regardless of the article's eventual
    publish path -- see common/costing.py's build_lineage for its shape.
    `published_by` is nullable ("ai_only" immediately on a compliant
    publish, None while pending_moderation, set to "humans" later via
    update_article_status if/when an operator approves or force-publishes
    it) -- stored explicitly as None rather than omitted, so "no data" is
    unambiguous to a reader rather than relying on key-absence.
    """
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
        "lineage": _lineage_to_item(lineage),
        "published_by": published_by,
    }
    table.put_item(Item=item)
    return {**item, "lineage": lineage}


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


def set_topic_last_article_at(topic_id: str, timestamp: str) -> None:
    """Set only a Topic's `last_article_at` (ISO-8601 UTC), leaving every other
    attribute alone. Fails (ConditionalCheckFailedException) rather than
    creating a Topic that no longer exists.
    """
    table = get_table(os.environ["TOPICS_TABLE"])
    table.update_item(
        Key={"topic_id": topic_id},
        UpdateExpression="SET last_article_at = :t",
        ConditionExpression="attribute_exists(topic_id)",
        ExpressionAttributeValues={":t": timestamp},
    )


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


def list_pending_moderation_for_topic(topic_id: str) -> list[dict]:
    """Return every pending ModerationQueue item for a topic."""
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    filter_expression = Attr("status").eq("pending") & Attr("topic_id").eq(topic_id)
    return _paginated_scan(table, filter_expression)


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
    return len(list_pending_moderation_for_topic(topic_id))


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
    item = response.get("Item")
    if item is not None and "lineage" in item:
        item["lineage"] = _lineage_from_item(item["lineage"])
    return item


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


def update_article_status(
    article_id: str,
    status: str,
    published_at: str | None = None,
    published_by: str | None = None,
) -> None:
    """Update an Articles item's `status` (and optionally `published_at`/
    `published_by`).

    `published_by` (docs/project-plan.md §11, PR 2 of 5): pass "humans"
    from the moderation-approve/force-publish admin routes, the moment an
    article actually becomes published via one of those paths -- it was
    created with published_by=None (not yet decided) while sitting in
    pending_moderation. Never overwrites `lineage`, which is fixed at
    draft time regardless of publish path.
    """
    table = get_table(os.environ["ARTICLES_TABLE"])
    update_expression = "SET #status = :status"
    expression_attribute_values = {":status": status}
    if published_at is not None:
        update_expression += ", published_at = :published_at"
        expression_attribute_values[":published_at"] = published_at
    if published_by is not None:
        update_expression += ", published_by = :published_by"
        expression_attribute_values[":published_by"] = published_by
    table.update_item(
        Key={"article_id": article_id},
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=expression_attribute_values,
    )


# --- Public API ---------------------------------------------------------


def list_articles_by_status(status: str, topic_id: str | None = None) -> list[dict]:
    """Return every Articles item with the given status.

    If `topic_id` is given, further filters to that topic. The Articles
    table's only key is `article_id` (no sort key, no topic_id GSI -- see
    infra/modules/app-data/main.tf), so this is a Scan + FilterExpression,
    acceptable at this project's scale.
    """
    table = get_table(os.environ["ARTICLES_TABLE"])
    filter_expression = Attr("status").eq(status)
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
    # Same Decimal -> float/int conversion get_article applies -- needed
    # here too now that public_api_handler's _list_articles projects a
    # slim lineage summary (models_used/cost_aud/published_by) onto each
    # item (docs/project-plan.md §11, PR 3 of 5); json.dumps can't
    # serialize a raw Decimal.
    for item in items:
        if "lineage" in item:
            item["lineage"] = _lineage_from_item(item["lineage"])
    return items


def list_all_articles() -> list[dict]:
    """Return every Articles item regardless of status (Scan -- acceptable
    at this project's scale, same as list_published_articles above).

    Backs the public Stats page's aggregate cost figures (PR 5 of 5): spend
    counts for drafted articles that went to moderation or were rejected,
    not just published ones. Same lineage Decimal -> int/float conversion as
    list_published_articles, for the same reason (json.dumps can't serialize
    a raw Decimal) -- callers only ever surface aggregates, never items.
    """
    table = get_table(os.environ["ARTICLES_TABLE"])
    items = _paginated_scan(table)
    for item in items:
        if "lineage" in item:
            item["lineage"] = _lineage_from_item(item["lineage"])
    return items


def list_published_articles(topic_id: str | None = None) -> list[dict]:
    """Return every Articles item with `status == "published"`."""
    return list_articles_by_status("published", topic_id)


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


def delete_musings_for_article(article_id: str) -> int:
    """Delete every Musings item written about `article_id`; return how many.

    Scan + filter, like list_musings: the table has only a hash key and is small.
    """
    table = get_table(os.environ["MUSINGS_TABLE"])
    doomed = [m for m in _paginated_scan(table) if m.get("article_id") == article_id]
    for musing in doomed:
        table.delete_item(Key={"musing_id": musing["musing_id"]})
    return len(doomed)


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


# --- Models / ModelConfig (AI lineage/cost-tracking enhancement, PR 1 of 5) --
#
# Owned by admin_api_handler.py (the /models, /model-config admin routes)
# and read by common/model_routing.py's resolve_model. Models is the
# "supported models" registry (docs/project-plan.md §11) -- populated via
# the admin API/CLI, not Terraform, so adding/switching a model never
# needs an apply. ModelConfig holds a single well-known row
# (config_id = "default") for the current global default/fallback model.


def put_model(item: dict) -> None:
    """Write (create or overwrite) a Models item.

    DynamoDB's boto3 resource rejects native `float` (it requires
    `Decimal` for numeric attributes) -- converts the two price fields via
    `Decimal(str(x))` rather than `Decimal(x)` directly, since the latter
    preserves a binary float's exact (and often ugly, e.g.
    0.00080000000000000004) representation instead of the decimal value
    the caller actually meant.
    """
    table = get_table(os.environ["MODELS_TABLE"])
    item = dict(item)
    for price_field in ("input_price_usd_per_1k_tokens", "output_price_usd_per_1k_tokens"):
        if price_field in item and item[price_field] is not None:
            item[price_field] = Decimal(str(item[price_field]))
    table.put_item(Item=item)


def list_models() -> list[dict]:
    """Return every Models item (Scan -- acceptable at this project's scale).

    DynamoDB returns numeric attributes as Decimal, which json.dumps can't
    serialize -- converts the two price fields back to float on the way
    out, the mirror of put_model's Decimal(str(x)) conversion on the way in.
    """
    table = get_table(os.environ["MODELS_TABLE"])
    items = _paginated_scan(table)
    for item in items:
        for price_field in ("input_price_usd_per_1k_tokens", "output_price_usd_per_1k_tokens"):
            if price_field in item and item[price_field] is not None:
                item[price_field] = float(item[price_field])
    return items


def get_model(model_id: str) -> dict | None:
    """Fetch a single Models item by model_id, or None if it isn't registered.

    Same Decimal -> float conversion as list_models, for the same reason
    (json.dumps can't serialize Decimal) -- needed by common/costing.py's
    per-model pricing lookup (PR 2 of 5).
    """
    table = get_table(os.environ["MODELS_TABLE"])
    response = table.get_item(Key={"model_id": model_id})
    item = response.get("Item")
    if item is None:
        return None
    for price_field in ("input_price_usd_per_1k_tokens", "output_price_usd_per_1k_tokens"):
        if price_field in item and item[price_field] is not None:
            item[price_field] = float(item[price_field])
    return item


_MODEL_CONFIG_ID = "default"


def get_model_config() -> dict | None:
    """Fetch the single "default" ModelConfig row, or None if it doesn't exist
    yet -- a fresh deploy, or an operator who's never touched this, is a
    valid state (see common/model_routing.py's resolve_model fallback).
    """
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    response = table.get_item(Key={"config_id": _MODEL_CONFIG_ID})
    return response.get("Item")


def put_model_config(*, model_id: str | None, fallback_model_id: str | None) -> dict:
    """Write (or overwrite) the single "default" ModelConfig row and return it."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    item = {
        "config_id": _MODEL_CONFIG_ID,
        "model_id": model_id,
        "fallback_model_id": fallback_model_id,
    }
    table.put_item(Item=item)
    return item


# A second row in the same table, separate from the "default" model row above so
# neither overwrites the other. Holds pipeline-wide settings edited from the admin
# API/CLI or straight in DynamoDB -- today just the research interval default.
_PIPELINE_CONFIG_ID = "pipeline"


def get_pipeline_config() -> dict | None:
    """The pipeline-wide settings row, or None if nothing has been set (a valid
    state: every setting then takes its built-in default)."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    item = table.get_item(Key={"config_id": _PIPELINE_CONFIG_ID}).get("Item")
    if item is not None and item.get("research_interval_hours") is not None:
        item["research_interval_hours"] = int(item["research_interval_hours"])
    return item


def put_pipeline_config(*, research_interval_hours: int | None) -> dict:
    """Set (or, with None, clear) the global research interval and return the row.

    Updates only that attribute, so settings added to this row later are not
    wiped by an edit to this one.
    """
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    if research_interval_hours is None:
        table.update_item(
            Key={"config_id": _PIPELINE_CONFIG_ID},
            UpdateExpression="REMOVE research_interval_hours",
        )
    else:
        table.update_item(
            Key={"config_id": _PIPELINE_CONFIG_ID},
            UpdateExpression="SET research_interval_hours = :hours",
            ExpressionAttributeValues={":hours": research_interval_hours},
        )
    return get_pipeline_config() or {"config_id": _PIPELINE_CONFIG_ID}


def set_topic_last_research_at(topic_id: str, timestamp: str) -> None:
    """Set only a Topic's `last_research_at` (ISO-8601 UTC): when its source was
    last successfully checked. Leaves every other attribute alone, and fails
    (ConditionalCheckFailedException) rather than creating a deleted topic."""
    table = get_table(os.environ["TOPICS_TABLE"])
    table.update_item(
        Key={"topic_id": topic_id},
        UpdateExpression="SET last_research_at = :t",
        ConditionExpression="attribute_exists(topic_id)",
        ExpressionAttributeValues={":t": timestamp},
    )
