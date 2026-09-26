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
import secrets
from datetime import UTC, datetime, timedelta
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


# Cleanup PR: how long CandidateIdeas/FailedExecutions items live, and (only from the moment of
# rejection, never before) ModerationQueue/PromptRefinements items -- one shared constant so every
# TTL this file sets agrees, the same "one clock" reasoning research_tick_handler.py's own
# FINDING_TTL_DAYS already follows for Findings (kept separate, at 14 days, since that one has to
# outlive the article window it feeds, not just an operator's 7-day glance-back).
CLEANUP_TTL_DAYS = 7


def _expires_in(days: int) -> int:
    """An `expires_at` epoch-seconds value `days` from now, for a TTL attribute."""
    return int((datetime.now(UTC) + timedelta(days=days)).timestamp())


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
    `status` to move an idea from "considered" to "selected". Carries
    `expires_at` (Cleanup PR, CLEANUP_TTL_DAYS from now) so it self-clears
    via TTL once it's well past useful -- a pure operational scratchpad
    superseded by the topic's next research cycle, nothing reads it back
    historically the way Findings' research cost is.
    """
    table = get_table(os.environ["CANDIDATE_IDEAS_TABLE"])
    item = {
        "topic_id": topic_id,
        "created_at": created_at,
        "angle": angle,
        "status": status,
        "expires_at": _expires_in(CLEANUP_TTL_DAYS),
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
    review: dict | None = None,
    body_original_s3_key: str | None = None,
    equipment_used: list[dict] | None = None,
) -> dict:
    """Write an Articles item and return it.

    `equipment_used` is the gear whose guidance was in the prompts that wrote this article
    (common/equipment.py: [{"topic_id", "version", "slot"}]); an empty list means it was written
    with none, which is different from the field being absent (written before gear existed).
    Never part of the public API's projection.

    `review` is the fresh-data review's record (common/fresh_review.py), stored only
    when a review ran; never part of the public API's projection. `body_original_s3_key`
    is where the draft as first written is kept when a revision replaced it, so a
    moderator can compare; stored only then, and never public.

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
    if review is not None:
        item["review"] = review
    if body_original_s3_key is not None:
        item["body_original_s3_key"] = body_original_s3_key
    if equipment_used is not None:
        item["equipment_used"] = equipment_used
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
    review_notes: list[str] | None = None,
) -> dict:
    """Write a ModerationQueue item and return it.

    `review_notes` are the fresh-data review's findings in plain words, so whoever
    reads `moderation list` sees why an article may be stale. Stored only if non-empty.
    """
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    item = {
        "queue_id": queue_id,
        "article_id": article_id,
        "topic_id": topic_id,
        "reasons": reasons,
        "status": status,
        "created_at": created_at,
    }
    if review_notes:
        item["review_notes"] = review_notes
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
    """Update a ModerationQueue item's `status` field in place.

    Cleanup PR: a `status` of "rejected" also sets `expires_at` (CLEANUP_TTL_DAYS from now), so a
    rejected item self-clears via TTL; "approved" or "pending" never gets one and the item
    persists indefinitely, same as before this PR. A real trade-off, not a free cleanup --
    admin_api_handler.py's _moderation_queue_stats reads rejected items' reasons "across all
    history" to inform compliance-prompt iteration; see that function's own updated docstring.
    """
    table = get_table(os.environ["MODERATION_QUEUE_TABLE"])
    expression = "SET #status = :status"
    values = {":status": status}
    if status == "rejected":
        expression += ", expires_at = :expires_at"
        values[":expires_at"] = _expires_in(CLEANUP_TTL_DAYS)
    table.update_item(
        Key={"queue_id": queue_id},
        UpdateExpression=expression,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=values,
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
    extra: dict | None = None,
) -> dict:
    """Write a PromptRefinements item and return it.

    `extra` is any further fields to store with it (its gear identity: common/gear.py).

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
        **(extra or {}),
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
    """Update a PromptRefinements item's `status` field in place.

    Cleanup PR: a `status` of "rejected" also sets `expires_at` (CLEANUP_TTL_DAYS from now), so a
    rejected version self-clears via TTL; "approved" or "pending" never gets one, so real, adopted
    refinement history persists exactly as before. Unlike ModerationQueue's rejected items, nothing
    reads a rejected version back historically -- no follow-on trade-off here.
    """
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])
    expression = "SET #status = :status"
    values = {":status": status}
    if status == "rejected":
        expression += ", expires_at = :expires_at"
        values[":expires_at"] = _expires_in(CLEANUP_TTL_DAYS)
    table.update_item(
        Key={"topic_id": topic_id, "version": version},
        UpdateExpression=expression,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=values,
    )


def set_prompt_refinement_equipment(
    topic_id: str,
    version: str,
    *,
    equipped: bool,
    at: str,
    slot: str | None = None,
    scope: str | None = None,
    reason: str | None = None,
) -> None:
    """Wear (`equipped=True`, with `slot` and `scope`) or take off a PromptRefinements item.

    Taking off clears `slot` and records why (`reason`, see common/equipment.py: it decides whether
    the item may be put back on automatically). It keeps `scope` unless one is given, and always
    leaves `equipped` set to False, which is what tells an item that was benched on purpose from a
    legacy approval that never had the field. The item must exist.
    """
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])
    if equipped:
        table.update_item(
            Key={"topic_id": topic_id, "version": version},
            UpdateExpression="SET equipped = :t, slot = :slot, #scope = :scope, equipped_at = :at",
            ExpressionAttributeNames={"#scope": "scope"},
            ExpressionAttributeValues={":t": True, ":slot": slot, ":scope": scope, ":at": at},
            ConditionExpression="attribute_exists(topic_id)",
        )
        return
    sets = ["equipped = :f", "unequipped_at = :at", "unequipped_reason = :reason"]
    names = {}
    values = {":f": False, ":at": at, ":reason": reason}
    if scope is not None:
        sets.append("#scope = :scope")
        names["#scope"] = "scope"
        values[":scope"] = scope
    table.update_item(
        Key={"topic_id": topic_id, "version": version},
        UpdateExpression="SET " + ", ".join(sets) + " REMOVE slot",
        **({"ExpressionAttributeNames": names} if names else {}),
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(topic_id)",
    )


def apply_prompt_refinement_wear(topic_id: str, version: str, delta: int) -> dict | None:
    """Damage (`delta` < 0) or repair (`delta` > 0) a piece of gear that is being worn, atomically.

    Returns {"durability", "max_durability"} after the change, or None if there was nothing to do:
    the item is not worn, has no durability (it predates gear), is already at 0 when damaged, or is
    already full when repaired. The conditions are in the write itself, so concurrent feedback can
    neither push durability below 0 nor above its maximum, and exactly one caller sees it reach 0.
    """
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])
    bound = "durability > :zero" if delta < 0 else "durability < max_durability"
    try:
        response = table.update_item(
            Key={"topic_id": topic_id, "version": version},
            UpdateExpression="SET durability = durability + :delta",
            ConditionExpression=(
                f"equipped = :yes AND #status = :approved AND attribute_exists(durability) AND {bound}"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":delta": delta,
                ":yes": True,
                ":approved": "approved",
                **({":zero": 0} if delta < 0 else {}),
            },
            ReturnValues="ALL_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None
    item = response["Attributes"]
    return {"durability": int(item["durability"]), "max_durability": int(item["max_durability"])}


def delete_prompt_refinement(topic_id: str, version: str) -> None:
    """Remove a PromptRefinements item outright (deleting an absent one is not an error)."""
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])
    table.delete_item(Key={"topic_id": topic_id, "version": version})


def set_prompt_refinement_fields(topic_id: str, version: str, fields: dict) -> None:
    """Set plain fields on an existing PromptRefinements item (its gear identity, a rarity bump).
    Refuses to create an item that does not exist."""
    table = get_table(os.environ["PROMPT_REFINEMENTS_TABLE"])
    names = {f"#f{n}": key for n, key in enumerate(fields)}
    values = {f":v{n}": value for n, value in enumerate(fields.values())}
    table.update_item(
        Key={"topic_id": topic_id, "version": version},
        UpdateExpression="SET " + ", ".join(f"#f{n} = :v{n}" for n in range(len(fields))),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(topic_id)",
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


def list_recent_article_titles(topic_id: str, limit: int = 5) -> list[str]:
    """Return up to `limit` of `topic_id`'s own published articles' titles, most recent first.

    Same Scan + FilterExpression as get_top_voted_articles above (no topic_id GSI on this table),
    just sorted by created_at instead of net_votes. Fed into daily_cycle_handler.py's ideation
    prompt so a source that stays trending for days doesn't get written up again each day just
    because that day's numbers are technically new -- titles only, never full articles or ids,
    since that's all a "don't repeat this" reminder needs."""
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

    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return [item["title"] for item in items[:limit]]


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
    body) is always kept so a human can inspect it either way. Carries
    `expires_at` (Cleanup PR, CLEANUP_TTL_DAYS from now) so old failures
    self-clear via TTL -- an operator-visibility record with a bounded
    glance-back window, not a permanent audit log.
    """
    table = get_table(os.environ["FAILED_EXECUTIONS_TABLE"])
    item = {
        "failure_id": failure_id,
        "topic_id": topic_id,
        "error": error,
        "raw_message": raw_message,
        "created_at": created_at,
        "expires_at": _expires_in(CLEANUP_TTL_DAYS),
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
    gear: dict | None = None,
) -> dict:
    """Write a Musings item and return it. `kind` is "article", "feedback" or "loot". A loot musing
    carries `gear`, a public snapshot of the piece it announces."""
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
    if gear is not None:
        item["gear"] = gear
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


_UNSET = object()


def put_pipeline_config(
    *,
    research_interval_hours=_UNSET,
    review_mode=_UNSET,
    review_on_unavailable=_UNSET,
) -> dict:
    """Update pipeline-wide settings and return the row.

    Only the settings passed are touched (a value sets it, None clears it), so
    editing one never wipes another or one added later.
    """
    updates = {
        "research_interval_hours": research_interval_hours,
        "review_mode": review_mode,
        "review_on_unavailable": review_on_unavailable,
    }
    sets, removes, values = [], [], {}
    for index, (name, value) in enumerate(updates.items()):
        if value is _UNSET:
            continue
        if value is None:
            removes.append(name)
        else:
            sets.append(f"{name} = :v{index}")
            values[f":v{index}"] = value

    if sets or removes:
        expression = ""
        if sets:
            expression += "SET " + ", ".join(sets)
        if removes:
            expression += (" " if expression else "") + "REMOVE " + ", ".join(removes)
        kwargs = {"ExpressionAttributeValues": values} if values else {}
        table = get_table(os.environ["MODEL_CONFIG_TABLE"])
        table.update_item(
            Key={"config_id": _PIPELINE_CONFIG_ID}, UpdateExpression=expression, **kwargs
        )
    return get_pipeline_config() or {"config_id": _PIPELINE_CONFIG_ID}


# A third row in the same table: the armor piece versions the bear brought to its most recently
# drafted article (any topic -- armor is global, see common/equipment.py's SCOPE_GLOBAL). Read
# and rewritten every time common.equipment.pick_armor draws again, so that draw can avoid
# repeating the exact same combination twice running. No TTL -- like the two rows above, this is
# ongoing operational state, not something that should ever expire on its own.

_LAST_ARMOR_CONFIG_ID = "last-armor"


def get_last_armor_versions() -> list[str]:
    """The armor piece versions drawn for the most recent article, or [] if none has ever been
    recorded (a fresh deploy, or every draw so far came up "no armor")."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    item = table.get_item(Key={"config_id": _LAST_ARMOR_CONFIG_ID}).get("Item")
    return list(item["versions"]) if item and item.get("versions") is not None else []


def set_last_armor_versions(versions: list[str]) -> None:
    """Record the armor piece versions just drawn (possibly an empty list -- "brought nothing"
    is recorded too, so the next draw can avoid repeating *that* twice running as well)."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    table.put_item(Item={"config_id": _LAST_ARMOR_CONFIG_ID, "versions": versions})


# --- Feedback limits (see common/feedback_limits.py) ---------------------------------------
#
# The settings live as one more row in the same config table as the pipeline settings, so they
# can be changed from the admin API/CLI or straight in DynamoDB. The counters that enforce them
# are rows in that table too (`feedback-window#<n>`, `feedback-day#<date>`), each with an
# `expires_at` the table's TTL clears out. An article's own state is two attributes on its
# Articles row: `feedback_locked` (true/false, editable by hand) and `feedback_count`.

_FEEDBACK_CONFIG_ID = "feedback"

_FEEDBACK_CONFIG_NUMBERS = (
    "rate_limit_count",
    "rate_limit_window_minutes",
    "daily_limit",
    "article_limit",
    "screening_limit",
    "token_delay_min_ms",
    "token_delay_max_ms",
    "pow_threshold_percent",
    "pow_difficulty_bits",
)


def get_feedback_config() -> dict | None:
    """The feedback settings row, or None if nothing has been set (every setting then takes its
    built-in default). Whole numbers come back as int (DynamoDB hands back Decimal)."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    item = table.get_item(Key={"config_id": _FEEDBACK_CONFIG_ID}).get("Item")
    if item is not None:
        for name in _FEEDBACK_CONFIG_NUMBERS:
            if item.get(name) is not None:
                item[name] = int(item[name])
    return item


def put_feedback_config(updates: dict) -> dict:
    """Update feedback settings and return the row. Only the settings in `updates` are touched
    (a value sets it, None clears it), so editing one never wipes another."""
    sets, removes, values = [], [], {}
    for index, (name, value) in enumerate(updates.items()):
        if value is None:
            removes.append(name)
        else:
            sets.append(f"{name} = :v{index}")
            values[f":v{index}"] = value
    if sets or removes:
        expression = ""
        if sets:
            expression += "SET " + ", ".join(sets)
        if removes:
            expression += (" " if expression else "") + "REMOVE " + ", ".join(removes)
        kwargs = {"ExpressionAttributeValues": values} if values else {}
        table = get_table(os.environ["MODEL_CONFIG_TABLE"])
        table.update_item(
            Key={"config_id": _FEEDBACK_CONFIG_ID}, UpdateExpression=expression, **kwargs
        )
    return get_feedback_config() or {"config_id": _FEEDBACK_CONFIG_ID}


def get_feedback_counter(key: str) -> int:
    """How many pieces of feedback a window/day counter has counted (0 if it doesn't exist)."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    item = table.get_item(Key={"config_id": key}).get("Item")
    return int(item["count"]) if item and item.get("count") is not None else 0


def consume_feedback_counter(key: str, limit: int, expires_at: int) -> bool:
    """Atomically count one more piece of feedback against `key`, but only if it is still under
    `limit`. Returns False (and changes nothing) when the limit is already reached, so two
    submissions at the edge can never both get in. `expires_at` (epoch seconds) is the row's TTL."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    try:
        table.update_item(
            Key={"config_id": key},
            UpdateExpression="ADD #c :one SET expires_at = :e",
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":e": expires_at},
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def refund_feedback_counter(key: str) -> None:
    """Give back one count taken by consume_feedback_counter (a later check refused the
    submission, so it should not have used up this one). Never goes below zero."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    try:
        table.update_item(
            Key={"config_id": key},
            UpdateExpression="ADD #c :minus_one",
            ConditionExpression="#c > :zero",
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={":minus_one": -1, ":zero": 0},
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        pass


def consume_article_feedback(article_id: str, limit: int) -> int | None:
    """Atomically count one more piece of feedback on an article, but only if it is not locked
    (`feedback_locked` is not true) and is still under `limit`. Returns the new count, or None
    if refused (locked, at its limit, or no such article)."""
    table = get_table(os.environ["ARTICLES_TABLE"])
    try:
        response = table.update_item(
            Key={"article_id": article_id},
            UpdateExpression="ADD feedback_count :one",
            ConditionExpression=(
                "attribute_exists(article_id) AND "
                "(attribute_not_exists(feedback_locked) OR feedback_locked = :no) AND "
                "(attribute_not_exists(feedback_count) OR feedback_count < :limit)"
            ),
            ExpressionAttributeValues={":one": 1, ":no": False, ":limit": limit},
            ReturnValues="UPDATED_NEW",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None
    return int(response["Attributes"]["feedback_count"])


def refund_article_feedback(article_id: str) -> None:
    """Give back one count taken by consume_article_feedback. Never goes below zero."""
    table = get_table(os.environ["ARTICLES_TABLE"])
    try:
        table.update_item(
            Key={"article_id": article_id},
            UpdateExpression="ADD feedback_count :minus_one",
            ConditionExpression="feedback_count > :zero",
            ExpressionAttributeValues={":minus_one": -1, ":zero": 0},
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        pass


def set_article_feedback_lock(article_id: str, locked: bool, *, reset_count: bool = False) -> None:
    """Set an article's `feedback_locked` flag (and, optionally, zero its `feedback_count` so it
    has its full allowance again). Fails (ConditionalCheckFailedException) for a missing article."""
    table = get_table(os.environ["ARTICLES_TABLE"])
    expression = "SET feedback_locked = :locked"
    values: dict = {":locked": locked}
    if reset_count:
        expression += ", feedback_count = :zero"
        values[":zero"] = 0
    table.update_item(
        Key={"article_id": article_id},
        UpdateExpression=expression,
        ConditionExpression="attribute_exists(article_id)",
        ExpressionAttributeValues=values,
    )



# --- Feedback verification (see common/feedback_verification.py) -----------------------------

_VERIFICATION_SECRET_ID = "verification-secret"


def get_verification_secret() -> str:
    """The key that signs feedback tokens, created on first use.

    It lives in the config table (row `verification-secret`), so nothing has to be provisioned
    and it is only readable by whoever can read that table. Two Lambdas racing to create it
    cannot end up with different keys: the write is conditional, and the loser reads the winner's.
    Deleting the row rotates the key (tokens already issued stop working, and they only live an
    hour or two).
    """
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    item = table.get_item(Key={"config_id": _VERIFICATION_SECRET_ID}).get("Item")
    if item and item.get("secret"):
        return item["secret"]
    try:
        table.put_item(
            Item={"config_id": _VERIFICATION_SECRET_ID, "secret": secrets.token_hex(32)},
            ConditionExpression="attribute_not_exists(config_id)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        pass
    return table.get_item(Key={"config_id": _VERIFICATION_SECRET_ID}, ConsistentRead=True)["Item"][
        "secret"
    ]


def consume_verification_nonce(nonce: str, expires_at: int) -> bool:
    """Mark a token's random value as used. Returns False if it already was, so a token is good
    for exactly one submission. The row expires with the token (`expires_at`, cleared by TTL) and
    holds nothing about the visitor."""
    table = get_table(os.environ["MODEL_CONFIG_TABLE"])
    try:
        table.put_item(
            Item={"config_id": f"nonce#{nonce}", "expires_at": expires_at},
            ConditionExpression="attribute_not_exists(config_id)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


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


# --- StatsCurrent (Observability enhancement, PR 1) ---------------------------------------
#
# Owned by common/stats_tracking.py. One row (stats_id="current"), updated in place all week
# with ADD expressions -- same pattern as the model_config counters above -- so the row and
# every attribute on it come into existence on first use; nothing has to create it first.


def increment_current_stats(updates: dict[str, int | Decimal], week_start: str) -> None:
    """ADD each of `updates` onto the current week's StatsCurrent row (creating it, and any
    attribute in `updates` not yet present, on first use -- same as consume_feedback_counter
    above). `week_start` (the Monday of the ISO week this row covers, e.g. "2026-09-15") is
    recorded once, the first time this week's row is touched, and left alone after that -- a
    later week's rollover job resets it, this function never does."""
    if not updates:
        return
    table = get_table(os.environ["STATS_CURRENT_TABLE"])
    names = {f"#f{n}": key for n, key in enumerate(updates)}
    values = {f":v{n}": value for n, value in enumerate(updates.values())}
    adds = ", ".join(f"#f{n} :v{n}" for n in range(len(updates)))
    table.update_item(
        Key={"stats_id": "current"},
        UpdateExpression=f"SET week_start = if_not_exists(week_start, :week) ADD {adds}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**values, ":week": week_start},
    )


def get_current_stats() -> dict:
    """The current week's StatsCurrent row, or an empty shell if nothing has been recorded yet
    this week (never raises for "no row" -- that is the normal state right after a rollover)."""
    table = get_table(os.environ["STATS_CURRENT_TABLE"])
    response = table.get_item(Key={"stats_id": "current"})
    return response.get("Item") or {"stats_id": "current"}


def delete_current_stats() -> None:
    """Clear the current week's StatsCurrent row (stats_rollover_handler.py, after copying it into
    StatsHistory). Deleting it outright, not zeroing its attributes, so the next increment
    recreates it fresh -- same "the row comes into existence on first use" rule
    increment_current_stats already follows. Deleting an already-empty row is not an error."""
    table = get_table(os.environ["STATS_CURRENT_TABLE"])
    table.delete_item(Key={"stats_id": "current"})


# --- StatsHistory (Observability enhancement, PR 2) ----------------------------------------
#
# Owned by stats_rollover_handler.py. One row per completed week, written once and never
# updated after that -- the same shape as StatsCurrent's row, just keyed by the week it covers.


def put_stats_history_row(week_start: str, row: dict) -> bool:
    """Write `row` (a copy of a completed week's StatsCurrent row) into StatsHistory, keyed by
    `week_start`. Refuses to overwrite a week that has already been rolled over -- returns False
    (and writes nothing) rather than silently replacing a historical record, which a retried or
    duplicated rollover invocation could otherwise do. Returns True on a real write."""
    table = get_table(os.environ["STATS_HISTORY_TABLE"])
    item = {**row, "week_start": week_start}
    item.pop("stats_id", None)  # StatsCurrent's key, meaningless once this is a StatsHistory row
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(week_start)",
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def get_stats_history_row(week_start: str) -> dict | None:
    """One completed week's StatsHistory row, or None if that week was never rolled over."""
    table = get_table(os.environ["STATS_HISTORY_TABLE"])
    return table.get_item(Key={"week_start": week_start}).get("Item")


# StatsHistory's permanent running-total row (Observability enhancement, PR 4): a sentinel
# `week_start` that can never collide with a real Monday date (those are always "YYYY-MM-DD"
# ISO dates -- this is neither a valid date nor formatted like one). Kept in sync by
# stats_rollover_handler.py at every rollover (common/stats_tracking.py's split_for_rollover
# decides which fields get ADD'd here versus SET) so "Total Stats" is one get_item away, never a
# scan-and-sum over every week there has ever been.
_STATS_ALL_TIME_KEY = "all-time"


def increment_stats_totals(updates: dict[str, int | Decimal]) -> None:
    """ADD each of `updates` onto StatsHistory's all-time row -- same ADD-onto-first-use
    mechanics as increment_current_stats, just keyed by the all-time sentinel instead of the
    current week."""
    if not updates:
        return
    table = get_table(os.environ["STATS_HISTORY_TABLE"])
    names = {f"#f{n}": key for n, key in enumerate(updates)}
    values = {f":v{n}": value for n, value in enumerate(updates.values())}
    adds = ", ".join(f"#f{n} :v{n}" for n in range(len(updates)))
    table.update_item(
        Key={"week_start": _STATS_ALL_TIME_KEY},
        UpdateExpression=f"ADD {adds}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def set_stats_totals_fields(fields: dict) -> None:
    """SET (not ADD) each of `fields` onto StatsHistory's all-time row -- for a refreshed
    snapshot value (API Gateway's rolling 30-day reading) that must overwrite, not accumulate,
    the same reason set_current_stats_fields SETs rather than ADDs."""
    if not fields:
        return
    table = get_table(os.environ["STATS_HISTORY_TABLE"])
    names = {f"#f{n}": key for n, key in enumerate(fields)}
    values = {f":v{n}": value for n, value in enumerate(fields.values())}
    sets = ", ".join(f"#f{n} = :v{n}" for n in range(len(fields)))
    table.update_item(
        Key={"week_start": _STATS_ALL_TIME_KEY},
        UpdateExpression=f"SET {sets}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def get_stats_totals() -> dict:
    """StatsHistory's all-time running-total row, or an empty shell if no week has ever been
    rolled over yet (the normal state on a fresh deploy)."""
    table = get_table(os.environ["STATS_HISTORY_TABLE"])
    response = table.get_item(Key={"week_start": _STATS_ALL_TIME_KEY})
    return response.get("Item") or {"week_start": _STATS_ALL_TIME_KEY}


def set_current_stats_fields(fields: dict, week_start: str) -> None:
    """SET (not ADD) each of `fields` onto the current week's StatsCurrent row -- for a value
    that is a refreshed snapshot each time it's written (e.g. cost_explorer_poll_handler.py's
    latest Cost Explorer reading), not one accumulated across calls the way
    increment_current_stats's ADD counters are: a repeat write overwrites the previous reading
    instead of compounding it. Creates the row on first use, and sets `week_start` the same way
    increment_current_stats does (once, left alone after)."""
    if not fields:
        return
    table = get_table(os.environ["STATS_CURRENT_TABLE"])
    names = {f"#f{n}": key for n, key in enumerate(fields)}
    values = {f":v{n}": value for n, value in enumerate(fields.values())}
    sets = ", ".join(f"#f{n} = :v{n}" for n in range(len(fields)))
    table.update_item(
        Key={"stats_id": "current"},
        UpdateExpression=f"SET week_start = if_not_exists(week_start, :week), {sets}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**values, ":week": week_start},
    )
