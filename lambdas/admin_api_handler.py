"""Admin API Lambda handler (Phase 2 admin console).

Deployed behind an API Gateway REST API (v1) that is authenticated
separately (SigV4/IAM + a WAF IP allowlist -- see `infra/`, owned by another
worker in this phase). This module only assumes that authentication has
already happened by the time `handler` runs.

Routing is a plain dict dispatch keyed on a route key of the form
"GET /topics". REST API's Lambda proxy event has no `routeKey` field (that's
an HTTP API v2 thing, from this project's original design before AWS WAF's
lack of HTTP API support forced a migration to REST API -- see
infra/modules/rest-api's header comment) -- `handler` builds the equivalent
from `httpMethod` + `resource` instead, so `_ROUTES` below needs no changes
either way. `handler` never raises -- every route function is wrapped in a
broad top-level try/except that logs the real exception and returns a
generic 500, per the task contract.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import boto3

from common import equipment, feedback_limits, gear
from common.adapters import CRYPTO_FEED_ADAPTER_KEY
from common.digest import DIGEST_TOPIC_ID, DIGEST_TOPIC_NAME
from common.dynamo import (
    REWRITE_FAILED_STATUS,
    claim_moderation_for_rewrite,
    delete_musings_for_article,
    delete_prompt_refinement,
    delete_topic,
    finish_moderation_rewrite,
    get_article,
    get_feedback_config,
    get_latest_finding,
    get_model,
    get_model_config,
    get_moderation_item,
    get_moderation_item_by_article_id,
    get_pipeline_config,
    get_prompt_refinement,
    get_stats_history_row,
    get_topic,
    get_view_count,
    increment_stats_totals,
    list_all_articles,
    list_all_moderation_items,
    list_candidate_ideas,
    list_failed_executions,
    list_models,
    list_moderation_by_status,
    list_pending_moderation,
    list_prompt_refinements,
    list_topics,
    put_feedback_config,
    put_model,
    put_model_config,
    put_moderation_item,
    put_pipeline_config,
    put_prompt_refinement,
    put_stats_history_row,
    put_topic,
    set_article_feedback_lock,
    set_prompt_refinement_equipment,
    set_prompt_refinement_fields,
    update_article_lineage,
    update_article_status,
    update_moderation_status,
    update_prompt_refinement_status,
)
from common.editorial_resolver import (
    DEFAULT_ADAPTER,
    normalize_editorial_goals,
    validate_editorial_goals,
)
from common.fact_check import fact_check_label
from common.fresh_review import (
    on_unavailable_error,
    resolve_on_unavailable,
    resolve_review_mode,
    review_mode_error,
)
from common.lineage_tools import audit_lineage, plan_backfill
from common.model_routing import resolve_model
from common.musings import (
    generate_and_store_article_musing,
    generate_and_store_loot_musing,
    generate_and_store_rejection_musing,
)
from common.research_schedule import DEFAULT_RESEARCH_INTERVAL_HOURS, interval_error
from common.review_report import DEFAULT_SAMPLE_SIZE, MAX_SAMPLE_SIZE, build_review_report
from common.rewrite import (
    MAX_INSTRUCTIONS_CHARS,
    SENT_BACK_REASON,
    release_stale_rewrites,
    rewrite_issues,
)
from common.scheduler import (
    DEFAULT_TIMEZONE,
    _validate_schedule_expression,
    delete_topic_schedules,
    upsert_topic_schedules,
    validate_timezone,
)
from common.source_refs import dedupe_source_refs
from common.static_pages import (
    invalidate_article_page,
    read_article_body,
    remove_article_page,
    render_and_publish_article_page,
)
from common.stats_tracking import ARTICLES_BACKFILL_MARKER, plan_articles_backfill, to_stats_updates

_DEFAULT_RESEARCH_CADENCE = "rate(1 hour)"
# New topics get their daily article at 9 AM Sydney time (the scheduler reads the
# cron in `daily_timezone`, so it follows daylight saving). A topic created before
# `daily_timezone` existed has none stored and keeps the UTC its schedule was
# made with -- see _LEGACY_DAILY_TIMEZONE in _update_topic.
_DEFAULT_DAILY_CADENCE = "cron(0 9 * * ? *)"
_DEFAULT_DAILY_TIMEZONE = "Australia/Sydney"
_LEGACY_DAILY_TIMEZONE = DEFAULT_TIMEZONE

_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _json_number(value):
    """json.dumps' fallback: DynamoDB hands every number back as a Decimal, which JSON cannot carry.
    Whole numbers become ints and the rest floats, so no route can fail with a 500 because some stored
    item happens to hold a number (a topic's research_interval_hours did exactly that on GET /topics)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _response(status_code: int, payload: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=_json_number),
    }


def _error(status_code: int, message: str) -> dict:
    return _response(status_code, {"error": message})


def _parse_body(event: dict) -> dict:
    """Parse `event["body"]` as JSON, tolerating absent/None/empty bodies."""
    raw = event.get("body")
    if not raw:
        return {}
    return json.loads(raw)


def _path_param(event: dict, name: str) -> str | None:
    return (event.get("pathParameters") or {}).get(name)


def _query_param(event: dict, name: str) -> str | None:
    return (event.get("queryStringParameters") or {}).get(name)


# --- Topics -------------------------------------------------------------


def _list_topics(event: dict) -> dict:
    return _response(200, {"topics": list_topics()})


def _model_candidates_error(value) -> str | None:
    """Validate a topic's optional `model_id_candidates` (PR 4 of 5): None
    (unset) or a list of non-empty strings. Returns an error message, or
    None if valid. An empty list is valid and means "no rotation"."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(c, str) and c for c in value):
        return "'model_id_candidates' must be a list of non-empty strings if provided"
    return None


def _create_topic(event: dict) -> dict:
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")

    topic_id = body.get("topic_id")
    name = body.get("name")
    # A topic with no adapter does independent web research: it defaults to
    # the reusable web_search adapter and, with no query configured, searches
    # on its own name (see common/editorial_resolver.py for its default goal).
    adapter = body["adapter"] if "adapter" in body else DEFAULT_ADAPTER

    if not isinstance(topic_id, str) or not topic_id:
        return _error(400, "'topic_id' is required and must be a non-empty string")
    if topic_id == equipment.GLOBAL_TOPIC_ID:
        return _error(
            400, f"'{equipment.GLOBAL_TOPIC_ID}' is reserved (armor created by hand is filed under it)"
        )
    if not isinstance(name, str) or not name:
        return _error(400, "'name' is required and must be a non-empty string")
    if not isinstance(adapter, str) or not adapter:
        return _error(
            400, f"'adapter' must be a non-empty string if provided (default: '{DEFAULT_ADAPTER}')"
        )

    adapter_config = body.get("adapter_config", {})
    if not isinstance(adapter_config, dict):
        return _error(400, "'adapter_config' must be an object if provided")

    editorial_goals_error = validate_editorial_goals(body.get("editorial_goals"))
    if editorial_goals_error:
        return _error(400, editorial_goals_error)

    is_financial = body.get("is_financial", False)
    if not isinstance(is_financial, bool):
        return _error(400, "'is_financial' must be a boolean if provided")
    # Phase 7: the crypto_feed adapter is inherently financial/
    # investment-adjacent -- force is_financial = True regardless of what
    # was passed (or omitted), so this safety property can never be
    # bypassed by an operator forgetting the flag. See
    # common/adapters/crypto_feed.py's module docstring.
    if adapter == CRYPTO_FEED_ADAPTER_KEY:
        is_financial = True

    research_cadence = body.get("research_cadence", _DEFAULT_RESEARCH_CADENCE)
    daily_cadence = body.get("daily_cadence", _DEFAULT_DAILY_CADENCE)
    daily_timezone = body.get("daily_timezone", _DEFAULT_DAILY_TIMEZONE)
    if not isinstance(research_cadence, str) or not research_cadence:
        return _error(400, "'research_cadence' must be a non-empty string if provided")
    if not isinstance(daily_cadence, str) or not daily_cadence:
        return _error(400, "'daily_cadence' must be a non-empty string if provided")
    try:
        _validate_schedule_expression(research_cadence)
        _validate_schedule_expression(daily_cadence)
        validate_timezone(daily_timezone)
    except ValueError as exc:
        return _error(400, str(exc))

    # How often a heartbeat of `research_cadence` actually does work. Optional:
    # unset inherits the pipeline-wide default (see common/research_schedule.py).
    research_interval_hours = body.get("research_interval_hours")
    interval_problem = interval_error(research_interval_hours)
    if interval_problem:
        return _error(400, f"'research_interval_hours' {interval_problem}")

    # Per-topic override of the fresh-data review mode (else the pipeline-wide one).
    review_mode = body.get("review_mode")
    mode_problem = review_mode_error(review_mode)
    if mode_problem:
        return _error(400, f"'review_mode' {mode_problem}")

    # AI lineage/cost-tracking enhancement (docs/project-plan.md §11, PR 1
    # of 5): optional per-topic model overrides, read by
    # common/model_routing.py's resolve_model. Both None by default --
    # falls through to the global ModelConfig default, then the
    # Terraform-set BEDROCK_MODEL_ID env var, same as before this existed.
    model_id = body.get("model_id")
    if model_id is not None and (not isinstance(model_id, str) or not model_id):
        return _error(400, "'model_id' must be a non-empty string if provided")
    fallback_model_id = body.get("fallback_model_id")
    if fallback_model_id is not None and (
        not isinstance(fallback_model_id, str) or not fallback_model_id
    ):
        return _error(400, "'fallback_model_id' must be a non-empty string if provided")
    # PR 4 of 5: optional rotation -- resolve_model picks one of these at
    # random per run, ahead of model_id (see common/model_routing.py).
    model_id_candidates = body.get("model_id_candidates")
    candidates_error = _model_candidates_error(model_id_candidates)
    if candidates_error:
        return _error(400, candidates_error)

    if get_topic(topic_id) is not None:
        return _error(409, f"topic '{topic_id}' already exists")

    item = {
        "topic_id": topic_id,
        "name": name,
        "adapter": adapter,
        "adapter_config": adapter_config,
        "is_financial": is_financial,
        "research_cadence": research_cadence,
        "daily_cadence": daily_cadence,
        "daily_timezone": daily_timezone,
        "model_id": model_id,
        "fallback_model_id": fallback_model_id,
        "model_id_candidates": model_id_candidates,
    }
    # Only stored when provided, so a topic without one simply inherits its
    # adapter's/the global default goal (common/editorial_resolver.py).
    if body.get("editorial_goals") is not None:
        item["editorial_goals"] = normalize_editorial_goals(body["editorial_goals"])
    if research_interval_hours is not None:
        item["research_interval_hours"] = research_interval_hours
    if review_mode is not None:
        item["review_mode"] = review_mode
    put_topic(item)
    try:
        upsert_topic_schedules(topic_id, research_cadence, daily_cadence, daily_timezone)
    except ValueError as exc:
        return _error(400, str(exc))
    return _response(201, item)


def _get_topic(event: dict) -> dict:
    topic_id = _path_param(event, "topic_id")
    topic = get_topic(topic_id)
    if topic is None:
        return _error(404, f"topic '{topic_id}' not found")
    return _response(200, topic)


def _update_topic(event: dict) -> dict:
    topic_id = _path_param(event, "topic_id")
    topic = get_topic(topic_id)
    if topic is None:
        return _error(404, f"topic '{topic_id}' not found")

    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")

    updated = dict(topic)
    for field in (
        "name",
        "adapter",
        "adapter_config",
        "editorial_goals",
        "is_financial",
        "research_cadence",
        "research_interval_hours",
        "review_mode",
        "daily_cadence",
        "daily_timezone",
        "model_id",
        "fallback_model_id",
        "model_id_candidates",
    ):
        if field in body:
            updated[field] = body[field]

    if "name" in body and (not isinstance(updated["name"], str) or not updated["name"]):
        return _error(400, "'name' must be a non-empty string")
    if "adapter" in body and (not isinstance(updated["adapter"], str) or not updated["adapter"]):
        return _error(400, "'adapter' must be a non-empty string")
    if "adapter_config" in body and not isinstance(updated["adapter_config"], dict):
        return _error(400, "'adapter_config' must be an object")
    if "editorial_goals" in body:
        editorial_goals_error = validate_editorial_goals(body["editorial_goals"])
        if editorial_goals_error:
            return _error(400, editorial_goals_error)
        # A whole-block replace, like adapter_config; {} or null clears it.
        updated["editorial_goals"] = normalize_editorial_goals(body["editorial_goals"])
    if "is_financial" in body and not isinstance(updated["is_financial"], bool):
        return _error(400, "'is_financial' must be a boolean")
    # Phase 7: same forced-True guarantee as _create_topic above -- applies
    # whether this update is switching a topic onto crypto_feed, or the
    # topic was already on it and this update just isn't touching
    # is_financial (or is trying to unset it).
    if updated["adapter"] == CRYPTO_FEED_ADAPTER_KEY:
        updated["is_financial"] = True
    if "research_cadence" in body and (
        not isinstance(updated["research_cadence"], str) or not updated["research_cadence"]
    ):
        return _error(400, "'research_cadence' must be a non-empty string")
    if "daily_cadence" in body and (
        not isinstance(updated["daily_cadence"], str) or not updated["daily_cadence"]
    ):
        return _error(400, "'daily_cadence' must be a non-empty string")
    if "research_interval_hours" in body:
        interval_problem = interval_error(updated["research_interval_hours"])
        if interval_problem:
            return _error(400, f"'research_interval_hours' {interval_problem}")
        if updated["research_interval_hours"] is None:
            del updated["research_interval_hours"]  # cleared: inherit the pipeline default
    if "review_mode" in body:
        mode_problem = review_mode_error(updated["review_mode"])
        if mode_problem:
            return _error(400, f"'review_mode' {mode_problem}")
        if updated["review_mode"] is None:
            del updated["review_mode"]  # cleared: inherit the pipeline-wide mode
    if "model_id" in body and updated["model_id"] is not None and (
        not isinstance(updated["model_id"], str) or not updated["model_id"]
    ):
        return _error(400, "'model_id' must be a non-empty string if provided")
    if "fallback_model_id" in body and updated["fallback_model_id"] is not None and (
        not isinstance(updated["fallback_model_id"], str) or not updated["fallback_model_id"]
    ):
        return _error(400, "'fallback_model_id' must be a non-empty string if provided")
    if "model_id_candidates" in body:
        candidates_error = _model_candidates_error(updated["model_id_candidates"])
        if candidates_error:
            return _error(400, candidates_error)

    research_cadence = updated.setdefault("research_cadence", _DEFAULT_RESEARCH_CADENCE)
    daily_cadence = updated.setdefault("daily_cadence", _DEFAULT_DAILY_CADENCE)
    # Not the new-topic default: a topic with no stored zone has a UTC schedule,
    # and defaulting it to Sydney here would move its run on an unrelated edit.
    daily_timezone = updated.setdefault("daily_timezone", _LEGACY_DAILY_TIMEZONE)
    try:
        _validate_schedule_expression(research_cadence)
        _validate_schedule_expression(daily_cadence)
        validate_timezone(daily_timezone)
    except ValueError as exc:
        return _error(400, str(exc))

    put_topic(updated)
    try:
        upsert_topic_schedules(topic_id, research_cadence, daily_cadence, daily_timezone)
    except ValueError as exc:
        return _error(400, str(exc))
    return _response(200, updated)


def _delete_topic(event: dict) -> dict:
    topic_id = _path_param(event, "topic_id")
    if get_topic(topic_id) is None:
        return _error(404, f"topic '{topic_id}' not found")
    delete_topic_schedules(topic_id)
    delete_topic(topic_id)
    return _response(200, {"deleted": topic_id})


_PIPELINE_FUNCTION_ENV_VARS = {
    "research_tick": "RESEARCH_TICK_FUNCTION_NAME",
    "daily_cycle": "DAILY_CYCLE_FUNCTION_NAME",
}


def _trigger_topic(event: dict) -> dict:
    topic_id = _path_param(event, "topic_id")

    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")

    pipeline = body.get("pipeline")
    if pipeline not in _PIPELINE_FUNCTION_ENV_VARS:
        return _error(400, "'pipeline' must be one of: research_tick, daily_cycle")

    if get_topic(topic_id) is None:
        return _error(404, f"topic '{topic_id}' not found")

    payload = {"topic_id": topic_id}
    # A manual research_tick is "check now": it must not be refused because the
    # topic's research interval hasn't elapsed, so it always bypasses that check.
    if pipeline == "research_tick":
        payload["force"] = True
    # `force` (daily_cycle only): write from the whole window even if the topic
    # already has an article since -- for an intentional regenerate. Sent only
    # when asked for, so the default payload is unchanged.
    force = body.get("force", False)
    if not isinstance(force, bool):
        return _error(400, "'force' must be a boolean if provided")
    if force:
        if pipeline != "daily_cycle":
            return _error(
                400,
                "'force' only applies to the daily_cycle pipeline "
                "(a manual research_tick always runs now)",
            )
        payload["force"] = True

    function_name = os.environ[_PIPELINE_FUNCTION_ENV_VARS[pipeline]]
    client = _get_lambda_client()
    client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    return _response(202, {"triggered": pipeline, "topic_id": topic_id})


def _list_candidates(event: dict) -> dict:
    topic_id = _path_param(event, "topic_id")
    if get_topic(topic_id) is None:
        return _error(404, f"topic '{topic_id}' not found")
    candidates = list_candidate_ideas(topic_id)
    return _response(200, {"topic_id": topic_id, "candidates": candidates})


def _get_latest_finding_route(event: dict) -> dict:
    """Surfaces get_latest_finding for scripts/admin_cli.py's `topics trigger`
    to poll against -- research_tick's own Lambda invocation is fire-and-
    forget (InvocationType="Event"), so this is how a caller finds out
    whether it's actually finished yet, without a dedicated job-status
    system: compare a Finding's captured_at against the time it triggered.
    """
    topic_id = _path_param(event, "topic_id")
    if get_topic(topic_id) is None:
        return _error(404, f"topic '{topic_id}' not found")
    finding = get_latest_finding(topic_id)
    if finding is None:
        return _error(404, f"no findings yet for topic '{topic_id}'")
    return _response(200, finding)


# --- Articles ---------------------------------------------------------


def _topic_display_name(topic_id: str) -> str:
    """A topic's display name, the digest's included (it has no Topics-table row), else its id."""
    if topic_id == DIGEST_TOPIC_ID:
        return DIGEST_TOPIC_NAME
    topic = get_topic(topic_id)
    return (topic or {}).get("name", topic_id)


def _baked_view_count(article: dict) -> int:
    """The view count written into a freshly rendered page (the page's own script replaces it with
    the live count on the first view). A counter read failing never stops a publish: the count kept
    on the article from before view counts were sharded is used instead."""
    stored = int(article.get("view_count", 0))
    try:
        return get_view_count(article["article_id"], stored_view_count=stored)
    except Exception as exc:  # noqa: BLE001 - a view count must never block a publish
        print(f"admin_api_handler: could not read view counters for {article['article_id']}: {exc!r}")
        return stored


def _render_published_page(article: dict, *, published_at: str) -> None:
    """Regenerate the static article page (docs/project-plan.md §11) and
    generate an article musing for an article that just became published.

    Shared by both admin-console publish paths below (moderation-approve
    and force-publish) -- daily_cycle_handler.py's own compliant-draft
    branch calls common.static_pages.render_and_publish_article_page and
    common.musings.generate_and_store_article_musing directly instead,
    since it already has the freshly-drafted body text in memory and
    doesn't need read_article_body's S3 round-trip. Both admin-console
    paths reaching this function needed a moderation-approve or
    force-publish override first, so their musing is always generated with
    compliant=False -- the more measured/thoughtful mood, not the
    published-cleanly proud one.
    """
    # Bugfix: get_topic("digest") returns None -- the cross-topic digest
    # (trending_digest_handler.py) isn't a real Topics-table row -- so
    # without this special case, a digest article reaching this function
    # (approved out of moderation, or force-published) would render with
    # the raw topic_id "digest" as its display name instead of the
    # friendly one, inconsistent with what a digest article gets when
    # trending_digest_handler.py publishes it directly on the first pass.
    topic_name = _topic_display_name(article["topic_id"])
    body_markdown = read_article_body(article["body_s3_key"])
    render_and_publish_article_page(
        article_id=article["article_id"],
        title=article["title"],
        body_markdown=body_markdown,
        topic_name=topic_name,
        published_at=published_at,
        source_refs=article.get("source_refs"),
        view_count=_baked_view_count(article),
        # lineage was fixed at draft time and never changes -- reread from
        # the already-stored article, not recomputed here. published_by is
        # hardcoded "humans": both routes reaching this function (approve,
        # force-publish) required an operator action.
        lineage=article.get("lineage"),
        published_by="humans",
        # The reader-facing line about the fresh-data review, from the stored record:
        # a person approving a held article is what "reviewed by a person" means.
        fact_check=fact_check_label(article.get("review"), "humans"),
        equipment_used=article.get("equipment_used"),
    )
    generate_and_store_article_musing(
        article_id=article["article_id"],
        topic_id=article["topic_id"],
        topic_name=topic_name,
        title=article["title"],
        compliant=False,
        model_id=os.environ["BEDROCK_MODEL_ID"],
    )


def _get_article(event: dict) -> dict:
    """One article, in any status, for a person deciding what to do with it (the review inbox in
    scripts/review_inbox.py): its title, topic, status, when it was made, what it cost, the sources
    it cites, and its full text. Admin only: unlike the public API, which serves published articles
    alone, this shows a draft that is still waiting for a decision. Nothing is changed."""
    article_id = _path_param(event, "article_id")
    article = get_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")

    body, body_error = "", None
    try:
        body = read_article_body(article["body_s3_key"])
    except Exception as exc:  # noqa: BLE001 - the rest of the article is still worth showing
        body_error = f"could not read the article text ({type(exc).__name__})"

    lineage = article.get("lineage") or {}
    total_cost = lineage.get("total_cost_aud", lineage.get("cost_aud"))
    payload = {
        "article_id": article_id,
        "topic_id": article.get("topic_id"),
        "title": article.get("title"),
        "status": article.get("status"),
        "created_at": article.get("created_at"),
        "published_at": article.get("published_at"),
        "published_by": article.get("published_by"),
        "body": body,
        "source_refs": [
            {"title": ref.get("title"), "url": ref.get("url")}
            for ref in dedupe_source_refs(article.get("source_refs"))
        ],
        "models_used": lineage.get("models_used") or [],
        "cost_aud": float(total_cost) if isinstance(total_cost, int | float) else None,
    }
    if body_error:
        payload["body_error"] = body_error
    return _response(200, payload)


def _publish_article(event: dict) -> dict:
    """Force an Articles item to `status="published"`, regardless of its
    current status (unlike _resolve_moderation_item below, which only acts
    on a `pending` ModerationQueue item). Covers cases the moderation
    approve/reject flow doesn't: publishing an article that was never
    routed to moderation in the first place, or overriding one stuck in an
    unwanted state.

    If a ModerationQueue item exists for this article and is still
    `pending`, it's marked `approved` too (best-effort consistency) so the
    two records don't disagree about whether this article was reviewed.
    """
    article_id = _path_param(event, "article_id")
    article = get_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")

    published_at = datetime.now(UTC).isoformat()
    # published_by="humans": an operator action produced this publish,
    # regardless of the article's prior status -- lineage (tokens/models/
    # cost) is untouched, it was already fixed at draft time and this
    # route never regenerates content (docs/project-plan.md §11, PR 2 of 5).
    update_article_status(article_id, "published", published_at=published_at, published_by="humans")
    _render_published_page(article, published_at=published_at)

    moderation_item = get_moderation_item_by_article_id(article_id)
    # "rewriting" too: approving it here makes a Re-Write still running for it discard its
    # result (common/rewrite.py only finishes an item that is still "rewriting").
    if moderation_item is not None and moderation_item.get("status") in ("pending", "rewriting"):
        update_moderation_status(moderation_item["queue_id"], "approved")

    return _response(200, {"published": article_id})


def _unpublish_article(event: dict) -> dict:
    """Take a published article down -- the inverse of _publish_article.

    Deletes its static page, marks the article and its moderation-queue item
    `rejected` (the existing vocabulary; the public API only lists `published`
    articles, so it disappears from every listing), removes the musings written
    about it, and asks CloudFront to drop its cached page. The article's markdown
    body in the content bucket is kept, so it can be force-published again.

    Ordered so a failure part-way is repaired by simply running it again: the
    page is deleted first, and an already-`rejected` article is allowed through
    to redo the (idempotent) cleanup. An article still waiting in moderation is
    refused -- that is what `moderation reject` is for.
    """
    article_id = _path_param(event, "article_id")
    article = get_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")
    if article.get("status") not in ("published", "rejected"):
        return _error(
            409,
            f"article '{article_id}' is '{article.get('status')}', not published; "
            "use the moderation reject route for one still awaiting review",
        )

    remove_article_page(article_id)
    update_article_status(article_id, "rejected")

    moderation_item = get_moderation_item_by_article_id(article_id)
    if moderation_item is not None and moderation_item.get("status") != "rejected":
        update_moderation_status(moderation_item["queue_id"], "rejected")

    return _response(200, {"unpublished": article_id, **_clear_article_traces(article_id)})


def _clear_article_traces(article_id: str) -> dict:
    """What is left of an article once its page is gone: the musings written about it, and its
    page in the CDN's cache. Shared by unpublish and a rewrite of a published article."""
    return {
        "musings_removed": delete_musings_for_article(article_id),
        "cache_invalidated": invalidate_article_page(article_id),
    }


# --- Lineage audit / backfill ---------------------------------------------


def _lineage_audit(event: dict) -> dict:
    """Where article lineage is missing or incomplete (ids only, no content)."""
    return _response(200, audit_lineage(list_all_articles()))


def _lineage_backfill(event: dict) -> dict:
    """Recompute stored lineage's cost from its recorded token counts at today's
    prices, and canonicalise model ids. A dry run unless `{"apply": true}`.

    Only touches `lineage`; tokens and what each call did are kept. Articles with
    no lineage cannot be backfilled (their tokens were never recorded) and are
    reported by the audit instead.
    """
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")
    apply = body.get("apply", False)
    if not isinstance(apply, bool):
        return _error(400, "'apply' must be a boolean if provided")

    plan = plan_backfill(list_all_articles())
    changed = [item for item in plan if item["changed"]]
    if apply:
        for item in changed:
            update_article_lineage(item["article_id"], item["lineage"])

    return _response(
        200,
        {
            "applied": apply,
            "examined": len(plan),
            "changed": len(changed),
            "articles": [{k: v for k, v in item.items() if k != "lineage"} for item in plan],
        },
    )


def _stats_backfill_articles(event: dict) -> dict:
    """One-time catch-up (Observability enhancement, PR 5): fold every existing article's
    already-recorded lineage cost into StatsHistory's all-time row, under the same `articles`
    category common/stats_tracking.py's record_article_lineage tallies onto going forward -- for
    every article drafted before that category existed. A dry run unless `{"apply": true}`.

    Refuses to double-count: a reserved StatsHistory row (`week_start` = the sentinel
    ARTICLES_BACKFILL_MARKER) marks that this has already run, written by the same conditional
    put_stats_history_row every real week's rollover already uses, so a retried or duplicated
    call -- or simply running this a second time on purpose -- can never fold these articles
    into the running total twice. A dry run still reports the current totals either way, so an
    operator can preview or audit them even after they've already been applied.
    """
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")
    apply = body.get("apply", False)
    if not isinstance(apply, bool):
        return _error(400, "'apply' must be a boolean if provided")

    plan = plan_articles_backfill(list_all_articles())
    already_run = get_stats_history_row(ARTICLES_BACKFILL_MARKER) is not None
    written = False
    if apply and not already_run and plan["totals"]:
        marker = {
            "applied_at": datetime.now(UTC).isoformat(),
            "examined": plan["examined"],
            "included": plan["included"],
        }
        written = put_stats_history_row(ARTICLES_BACKFILL_MARKER, marker)
        if written:
            increment_stats_totals(to_stats_updates(plan["totals"]))

    return _response(
        200,
        {
            "already_run": already_run,
            "applied": written,
            "examined": plan["examined"],
            "included": plan["included"],
            "totals": plan["totals"],
        },
    )


# --- Fresh-data review report ---------------------------------------------


def _review_report(event: dict) -> dict:
    """How the fresh-data review is doing, from the records on the articles: outcome and
    status counts, per-topic rates, and what enforcement *would have* held and revised,
    so turning it on is decided from numbers. `?sample=N` sets how many recent flagged
    claims to include (default 10, at most 50)."""
    raw = _query_param(event, "sample")
    sample = DEFAULT_SAMPLE_SIZE
    if raw is not None:
        try:
            sample = int(raw)
        except ValueError:
            return _error(400, "'sample' must be a whole number")
        if not 0 <= sample <= MAX_SAMPLE_SIZE:
            return _error(400, f"'sample' must be between 0 and {MAX_SAMPLE_SIZE}")
    return _response(200, build_review_report(list_all_articles(), list_topics(), sample_size=sample))


# --- Moderation queue -----------------------------------------------------


def _list_moderation_queue(event: dict) -> dict:
    """The pending items, plus how many are being rewritten right now. Listing is also when a
    Re-Write that never finished is put back (common/rewrite.py's release_stale_rewrites), so
    opening the inbox is enough to recover one.

    `failed_rewrites` are rewrites of articles that are still published: such an article is
    never in the inbox, so this is where a person learns its rewrite did not work, and why.
    They clear by themselves (TTL)."""
    try:
        release_stale_rewrites()
    except Exception as exc:  # noqa: BLE001 - listing must still work
        print(f"admin_api_handler: could not release stale rewrites: {exc!r}")
    rewriting = list_moderation_by_status("rewriting")
    failed = [
        {
            "queue_id": item.get("queue_id"),
            "article_id": item.get("article_id"),
            "topic_id": item.get("topic_id"),
            "requested_at": item.get("rewrite_requested_at"),
            "error": item.get("last_rewrite_error"),
        }
        for item in list_moderation_by_status(REWRITE_FAILED_STATUS)
    ]
    return _response(
        200, {"items": list_pending_moderation(), "rewriting": len(rewriting), "failed_rewrites": failed}
    )


def _rewrite_moderation_item(event: dict) -> dict:
    """Start a Re-Write of a held article with the model the operator chose (common/rewrite.py).

    Claims the item (pending -> rewriting, so it leaves the inbox and a second trigger gets a
    409) and hands the work to the daily-cycle Lambda asynchronously: a rewrite takes minutes,
    far longer than API Gateway waits. The rewritten article comes back to the inbox as a new
    pending item; a failure puts this one back, with the reason.
    """
    queue_id = _path_param(event, "queue_id")
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")
    model_id = body.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        return _error(400, "'model_id' is required: one of the models from GET /models")
    model_error = _rewrite_model_error(model_id)
    if model_error:
        return _error(400, model_error)
    instructions, instructions_error = _rewrite_instructions(body, required=False)
    if instructions_error:
        return _error(400, instructions_error)

    item = get_moderation_item(queue_id)
    if item is None:
        return _error(404, f"moderation queue item '{queue_id}' not found")
    if item.get("status") != "pending":
        return _error(409, f"moderation queue item '{queue_id}' is not pending")
    if not rewrite_issues(item) and not instructions:
        return _error(
            400,
            "nothing to fix: the reviews flagged nothing on this article, so approve or reject "
            "it, or say what is wrong with 'instructions' (admin_cli articles rewrite)",
        )

    return _start_rewrite(item, model_id, instructions)


def _rewrite_model_error(model_id: str) -> str | None:
    model = get_model(model_id)
    if model is None or model.get("enabled") is False:
        return f"model '{model_id}' is not a registered, enabled model (see GET /models)"
    return None


def _rewrite_instructions(body: dict, *, required: bool) -> tuple[str, str | None]:
    """The request's 'instructions' (what a person says is wrong), stripped, and an error if
    they are missing when `required`, not a string, or too long."""
    instructions = body.get("instructions")
    if instructions is None:
        instructions = ""
    if not isinstance(instructions, str):
        return "", "'instructions' must be a string"
    instructions = instructions.strip()
    if required and not instructions:
        return "", "'instructions' is required: say what is wrong with the article"
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        return "", f"'instructions' must be at most {MAX_INSTRUCTIONS_CHARS} characters"
    return instructions, None


def _start_rewrite(item: dict, model_id: str, instructions: str, extra: dict | None = None) -> dict:
    """Claim a pending queue item for a Re-Write and start it in the background (the daily-cycle
    Lambda, asynchronously). 202, or 409 if someone else claimed it first, or 502 (and the item
    back to pending) if the Lambda could not be invoked."""
    queue_id = item["queue_id"]
    rewrite_id = str(uuid.uuid4())
    requested_at = datetime.now(UTC).isoformat()
    if not claim_moderation_for_rewrite(
        queue_id,
        rewrite_id=rewrite_id,
        model_id=model_id,
        requested_at=requested_at,
        instructions=instructions or None,
    ):
        return _error(409, f"moderation queue item '{queue_id}' is not pending")
    return _invoke_rewrite(item, rewrite_id, model_id, extra)


def _invoke_rewrite(
    item: dict, rewrite_id: str, model_id: str, extra: dict | None = None, *, still_published: bool = False
) -> dict:
    """Start the Re-Write that owns `item` (already `rewriting` under `rewrite_id`). 202, or 502
    with the item released if the Lambda could not be invoked: back to the inbox, or, when the
    article is `still_published` (it stays up while it is rewritten), closed as `rewrite_failed`."""
    queue_id = item["queue_id"]
    try:
        _get_lambda_client().invoke(
            FunctionName=os.environ["DAILY_CYCLE_FUNCTION_NAME"],
            InvocationType="Event",
            Payload=json.dumps(
                {"action": "rewrite", "queue_id": queue_id, "rewrite_id": rewrite_id}
            ).encode("utf-8"),
        )
    except Exception as exc:  # noqa: BLE001 - never leave it claimed with nothing running
        print(f"admin_api_handler: could not start the rewrite for {queue_id}: {exc!r}")
        if still_published:
            finish_moderation_rewrite(
                queue_id,
                rewrite_id=rewrite_id,
                status=REWRITE_FAILED_STATUS,
                fields={"last_rewrite_error": "the rewrite could not be started"},
            )
            return _error(502, "could not start the rewrite; the article is unchanged and still published")
        update_moderation_status(queue_id, "pending")
        return _error(502, "could not start the rewrite; the item is back in the inbox")

    return _response(
        202,
        {"rewriting": queue_id, "article_id": item["article_id"], "model_id": model_id, **(extra or {})},
    )


def _rewrite_article(event: dict) -> dict:
    """Rewrite any article, steered by what a person says is wrong with it (`admin_cli articles
    rewrite <id> --instructions "..."`). The rewrite goes through the reviews again and waits in
    the inbox for approval, like any other draft (common/rewrite.py).

    - published: stays up, untouched, while it is rewritten. It is taken down (page deleted,
      musings removed, CDN cache cleared, set back to `pending_moderation`) only once the rewrite
      is ready to take its place in the inbox; a rewrite that fails leaves it published as it was
      (common/rewrite.py). With `"force": true` it is taken down first, as `articles unpublish`
      would, and then rewritten: for an article that must not stay up meanwhile.
    - pending_moderation: its waiting queue item is rewritten.
    - rejected: put back to `pending_moderation` with a new queue item.

    409 while a rewrite of the article is already running. `model_id` is optional: by default,
    the model the topic would write with today.
    """
    article_id = _path_param(event, "article_id")
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")
    instructions, instructions_error = _rewrite_instructions(body, required=True)
    if instructions_error:
        return _error(400, instructions_error)
    force = body.get("force", False)
    if not isinstance(force, bool):
        return _error(400, "'force' must be true or false, if given")

    article = get_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")
    status = article.get("status")
    if status not in ("published", "pending_moderation", "rejected"):
        return _error(409, f"article '{article_id}' is '{status}' and cannot be rewritten")

    model_id = body.get("model_id")
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id.strip():
            return _error(400, "'model_id' must be one of the models from GET /models, if given")
        model_error = _rewrite_model_error(model_id)
        if model_error:
            return _error(400, model_error)
    else:
        model_id, _ = resolve_model(get_topic(article["topic_id"]))

    item = get_moderation_item_by_article_id(article_id)
    # Whatever the article's status: a published one being rewritten while it stays up has a
    # `rewriting` item too.
    if item is not None and item.get("status") == "rewriting":
        return _error(409, f"article '{article_id}' is already being rewritten")

    extra = {}
    still_published = status == "published" and not force
    if still_published:
        # Nothing changes yet: common/rewrite.py takes it down when the rewrite is ready. Its
        # queue item is made already claimed, in one write: it must never be `pending`, where the
        # inbox would offer to approve or reject an article that is still public.
        rewrite_id = str(uuid.uuid4())
        claim = {
            "rewrite_id": rewrite_id,
            "rewrite_model_id": model_id,
            "rewrite_requested_at": datetime.now(UTC).isoformat(),
            "rewrite_instructions": instructions,
            "article_still_published": True,
        }
        item = put_moderation_item(
            queue_id=str(uuid.uuid4()),
            article_id=article_id,
            topic_id=article["topic_id"],
            reasons=[SENT_BACK_REASON],
            created_at=claim["rewrite_requested_at"],
            status="rewriting",
            extra=claim,
        )
        extra = {"unpublished": False, "stays_published_until_rewritten": True}
        return _invoke_rewrite(item, rewrite_id, model_id, extra, still_published=True)
    if status == "published":
        # Forced. Off the site first (page, then status), so a failure part-way leaves it down,
        # not half-up.
        remove_article_page(article_id)
        update_article_status(article_id, "pending_moderation")
        extra = {"unpublished": True, **_clear_article_traces(article_id)}
    elif status == "rejected":
        update_article_status(article_id, "pending_moderation")

    if status != "pending_moderation" or item is None or item.get("status") != "pending":
        item = put_moderation_item(
            queue_id=str(uuid.uuid4()),
            article_id=article_id,
            topic_id=article["topic_id"],
            reasons=[SENT_BACK_REASON],
            created_at=datetime.now(UTC).isoformat(),
        )
    return _start_rewrite(item, model_id, instructions, extra)


_STATS_RECENT_LIMIT = 20


def _moderation_queue_stats(event: dict) -> dict:
    """Summarize what compliance review has flagged, across all history for a pending or
    approved item -- for a rejected one, only as far back as the Cleanup PR's TTL window
    (CLEANUP_TTL_DAYS, common/dynamo.py's update_moderation_status): rejected items self-clear
    after that, a deliberate trade-off between this endpoint's original "across all history"
    reach and not keeping rejected compliance-review output around indefinitely.

    Phase 6 ("Prompt iteration on the compliance-review step based on
    what's actually been flagged so far"): iterating the compliance
    prompt (common/compliance.py's _REVIEW_PROMPT_TEMPLATE) responsibly
    needs to look at what has actually tripped it, not guesswork -- this
    is the read-only visibility that makes that possible. The prompt
    edit itself stays a manual, human-reviewed step (same as any other
    prompt change in this project); this endpoint only surfaces the data
    to base that edit on.

    `reason_counts` tallies raw reason strings as-is (no normalization) --
    the one reason that's a fixed constant (financial-topic routing, see
    compliance.py's _FINANCIAL_REASON) naturally buckets together;
    Bedrock-authored reasons are free text and mostly won't collide, so
    `recent` (the most recent items with their full reasons list) is
    where the actual signal is for those.
    """
    items = list_all_moderation_items()

    by_status: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    reason_counts: dict[str, int] = {}

    for item in items:
        status = item.get("status", "unknown")
        by_status[status] = by_status.get(status, 0) + 1

        topic_id = item.get("topic_id", "unknown")
        by_topic[topic_id] = by_topic.get(topic_id, 0) + 1

        for reason in item.get("reasons") or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    recent = sorted(items, key=lambda i: i.get("created_at") or "", reverse=True)[:_STATS_RECENT_LIMIT]
    recent_summaries = [
        {
            "queue_id": i.get("queue_id"),
            "topic_id": i.get("topic_id"),
            "status": i.get("status"),
            "reasons": i.get("reasons", []),
            "created_at": i.get("created_at"),
        }
        for i in recent
    ]

    return _response(
        200,
        {
            "total_flagged": len(items),
            "by_status": by_status,
            "by_topic": by_topic,
            "reason_counts": reason_counts,
            "recent": recent_summaries,
        },
    )


def _resolve_moderation_item(event: dict, *, new_status: str, article_status: str) -> dict:
    queue_id = _path_param(event, "queue_id")
    item = get_moderation_item(queue_id)
    if item is None:
        return _error(404, f"moderation queue item '{queue_id}' not found")
    if item.get("status") != "pending":
        return _error(409, f"moderation queue item '{queue_id}' is not pending")

    article_id = item["article_id"]
    published_at = datetime.now(UTC).isoformat() if article_status == "published" else None
    if article_status == "published":
        article = get_article(article_id)
        # published_by="humans": this only ever reaches "published" via an
        # operator's moderation-approve action (docs/project-plan.md §11,
        # PR 2 of 5) -- lineage is untouched, fixed at draft time.
        update_article_status(
            article_id, article_status, published_at=published_at, published_by="humans"
        )
        if article is not None:
            _render_published_page(article, published_at=published_at)
    else:
        update_article_status(article_id, article_status, published_at=published_at)
    update_moderation_status(queue_id, new_status)
    if article_status == "rejected":
        _post_rejection_musing(item.get("topic_id") or (get_article(article_id) or {}).get("topic_id"))

    action_key = "approved" if new_status == "approved" else "rejected"
    return _response(200, {action_key: queue_id, "article_id": article_id})


def _post_rejection_musing(topic_id: str | None) -> None:
    """BloggerBear's shocked musing about a rejected draft: the topic's name only, never the article's
    id or title (common/musings.py). The rejection has already happened by now, so a failure here is
    logged and swallowed -- it must never turn a successful reject into an error."""
    if not topic_id:
        return
    try:
        generate_and_store_rejection_musing(
            topic_id=topic_id,
            topic_name=_topic_display_name(topic_id),
            model_id=os.environ["BEDROCK_MODEL_ID"],
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        print(f"admin_api_handler: could not post the rejection musing: {exc!r}")


def _approve_moderation_item(event: dict) -> dict:
    return _resolve_moderation_item(event, new_status="approved", article_status="published")


def _reject_moderation_item(event: dict) -> dict:
    return _resolve_moderation_item(event, new_status="rejected", article_status="rejected")


# --- Prompt refinements (Phase 5) ----------------------------------------


def _plain(value):
    """DynamoDB hands numbers back as Decimal, which JSON cannot carry."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _view(item: dict) -> dict:
    """A refinement as the admin sees it: plain numbers, and the gear's name."""
    return {**_plain(item), "name": gear.display_name(item)}


def _gear_summary(item: dict) -> dict:
    plain = _view(item)
    return {key: plain.get(key) for key in ("name", "rarity", "durability", "max_durability")}


def _as_worn(item: dict, plan: dict | None) -> dict:
    """The item as it is once placed, so its name has the noun for the slot it went into."""
    return {**item, "slot": plan["slot"]} if plan else item


def _ensure_identity(item: dict) -> dict:
    """Give an item that predates gear (or was proposed without a name) a rarity and a durability.

    Rolled once and stored, so asking again never re-rolls. Its theme, if it has one, is kept; otherwise
    it gets one made from its topic (no model is called here)."""
    if gear.has_identity(item):
        return item
    identity = gear.new_identity(
        item.get("theme") or gear.fallback_theme(item["topic_id"]), item.get("slot_hint")
    )
    set_prompt_refinement_fields(item["topic_id"], item["version"], identity)
    return {**item, **identity}


def _list_prompt_refinements(event: dict) -> dict:
    topic_id = _query_param(event, "topic_id")
    status = _query_param(event, "status")
    refinements = list_prompt_refinements(topic_id=topic_id, status=status)
    return _response(200, {"refinements": [_view(item) for item in refinements]})


def _placement_body(event: dict) -> dict | None:
    """The optional {scope, slot, replace} body of an approve / equip call: {} for no body, and
    None when it is not a JSON object (the caller answers 400)."""
    try:
        body = _parse_body(event)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _wear(item: dict, plan: dict) -> None:
    """Apply an equip plan. The displaced item comes off first, so a failure part way leaves a slot
    empty rather than two items fighting over it."""
    now = datetime.now(UTC).isoformat()
    displaced = plan["displaced"]
    if displaced is not None:
        set_prompt_refinement_equipment(
            displaced["topic_id"],
            displaced["version"],
            equipped=False,
            at=now,
            reason=equipment.DISPLACED,
        )
    set_prompt_refinement_equipment(
        item["topic_id"], item["version"], equipped=True, at=now, slot=plan["slot"], scope=plan["scope"]
    )


def _announce_drop(
    item: dict, plan: dict | None, *, announce: bool = True, force: bool = False, raise_errors: bool = False
) -> str | None:
    """Tell readers BloggerBear has new gear: one loot-drop musing, the first time a piece is worn.

    Returns the musing id, or None if nothing was announced. It stays quiet if the piece has been
    announced before (a repaired piece put back on does not drop twice), if it was not worn, or if the
    caller asked for silence, unless `force`. A failure never stops the equip that led here (unless
    `raise_errors`, for the command whose whole job is announcing).
    """
    if not announce or (plan is None and not force):
        return None
    if item.get("loot_announced_at") and not force:
        return None
    try:
        worn = _as_worn(item, plan)
        topic = get_topic(item["topic_id"]) if worn.get("slot") == equipment.RING_SLOT else None
        names = {item["topic_id"]: topic.get("name")} if topic else {}
        view = {**gear.public_view(worn, names), "topic_id": item["topic_id"] if topic else None}
        musing = generate_and_store_loot_musing(gear=view, model_id=os.environ["BEDROCK_MODEL_ID"])
        set_prompt_refinement_fields(
            item["topic_id"], item["version"], {"loot_announced_at": datetime.now(UTC).isoformat()}
        )
        return musing["musing_id"]
    except Exception as exc:  # noqa: BLE001 - announcing is never worth failing the equip for
        print(f"admin_api_handler: could not announce a loot drop: {exc!r}")
        if raise_errors:
            raise
        return None


def _announce_flag(body: dict) -> bool:
    value = body.get("announce", True)
    return value if isinstance(value, bool) else True


def _placement_view(item: dict, plan: dict | None) -> dict:
    """What happened to an item, for the response and for the CLI to tell the admin."""
    if plan is None:
        return {"equipped": False, "slot": None, "scope": item.get("scope"), "displaced": None}
    displaced = plan["displaced"]
    return {
        "equipped": True,
        "slot": plan["slot"],
        "scope": plan["scope"],
        "displaced": equipment.ref(displaced) if displaced else None,
    }


def _resolve_prompt_refinement(event: dict, *, new_status: str) -> dict:
    topic_id = _path_param(event, "topic_id")
    version = _path_param(event, "version")
    item = get_prompt_refinement(topic_id, version)
    if item is None:
        return _error(404, f"prompt refinement '{topic_id}'/'{version}' not found")
    if item.get("status") != "pending":
        return _error(409, f"prompt refinement '{topic_id}'/'{version}' is not pending")

    plan = None
    if new_status == "approved":
        item = _ensure_identity(item)
        body = _placement_body(event)
        if body is None:
            return _error(400, "request body must be a JSON object")
        # Work out where it goes before changing anything, so a placement that cannot be done
        # leaves the item pending. With no choice made it takes a ring for its own topic, which
        # is what approving always meant; if every ring is worn it waits in the backpack.
        scope = body.get("scope")
        if scope != "backpack":
            approved = list_prompt_refinements(status="approved")
            no_choice = scope is None and not body.get("slot") and not body.get("replace")
            try:
                if no_choice and equipment.suggest_slot(approved, equipment.SCOPE_TOPIC) is None:
                    plan = None
                else:
                    plan = equipment.plan_equip(
                        approved,
                        item,
                        scope=scope or equipment.SCOPE_TOPIC,
                        slot=body.get("slot"),
                        replace=body.get("replace"),
                    )
            except equipment.EquipError as exc:
                return _error(exc.status, exc.message)

    update_prompt_refinement_status(topic_id, version, new_status)
    action_key = "approved" if new_status == "approved" else "rejected"
    payload = {"topic_id": topic_id, "version": version}
    if new_status == "approved":
        if plan is not None:
            _wear(item, plan)
        else:
            # In the backpack. Parked (no room for it) may be put on automatically if something
            # wears out; shelved (the admin chose the backpack) never is. See common/equipment.py.
            parked = body.get("scope") != "backpack"
            set_prompt_refinement_equipment(
                topic_id,
                version,
                equipped=False,
                at=datetime.now(UTC).isoformat(),
                reason=equipment.PARKED if parked else equipment.SHELVED,
                scope=equipment.SCOPE_TOPIC if parked else None,
            )
        payload["placement"] = _placement_view(item, plan)
        payload["item"] = _gear_summary(_as_worn(item, plan))
        payload["loot_drop"] = _announce_drop(item, plan, announce=_announce_flag(body))
    return _response(200, {action_key: payload})


def _approve_prompt_refinement(event: dict) -> dict:
    return _resolve_prompt_refinement(event, new_status="approved")


def _reject_prompt_refinement(event: dict) -> dict:
    return _resolve_prompt_refinement(event, new_status="rejected")


def _approved_refinement_or_error(event: dict):
    topic_id = _path_param(event, "topic_id")
    version = _path_param(event, "version")
    item = get_prompt_refinement(topic_id, version)
    if item is None:
        return None, _error(404, f"prompt refinement '{topic_id}'/'{version}' not found")
    if item.get("status") != "approved":
        return None, _error(409, f"prompt refinement '{topic_id}'/'{version}' is not approved")
    return item, None


def _equip_prompt_refinement(event: dict) -> dict:
    item, error = _approved_refinement_or_error(event)
    if error:
        return error
    body = _placement_body(event)
    if body is None:
        return _error(400, "request body must be a JSON object")
    item = _ensure_identity(item)
    approved = list_prompt_refinements(status="approved")
    try:
        plan = equipment.plan_equip(
            approved,
            item,
            scope=body.get("scope") or item.get("scope") or equipment.SCOPE_TOPIC,
            slot=body.get("slot"),
            replace=body.get("replace"),
        )
    except equipment.EquipError as exc:
        return _error(exc.status, exc.message)
    _wear(item, plan)
    placement = _placement_view(item, plan)
    worn = _gear_summary(_as_worn(item, plan))
    drop = _announce_drop(item, plan, announce=_announce_flag(body))
    return _response(200, {"equipped": {**equipment.ref(item), **placement, "item": worn, "loot_drop": drop}})


def _unequip_prompt_refinement(event: dict) -> dict:
    item, error = _approved_refinement_or_error(event)
    if error:
        return error
    if not equipment.is_equipped(item):
        return _error(409, "that refinement is not equipped")
    set_prompt_refinement_equipment(
        item["topic_id"],
        item["version"],
        equipped=False,
        at=datetime.now(UTC).isoformat(),
        reason=equipment.BENCHED,
    )
    return _response(200, {"unequipped": equipment.ref(item)})


def _repair_gear(event: dict) -> dict:
    """Restore a piece of gear's durability (body {"amount": n}, or none for all of it), never above
    its maximum. It stays where it is: a worn-out piece is repaired into the backpack, and putting it
    back on is a separate, deliberate step."""
    item, error = _approved_refinement_or_error(event)
    if error:
        return error
    body = _placement_body(event)
    if body is None:
        return _error(400, "request body must be a JSON object")
    amount = body.get("amount")
    if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool) or amount < 1):
        return _error(400, "'amount' must be a whole number of at least 1")
    item = _ensure_identity(item)
    top = int(item["max_durability"])
    now = int(item.get("durability", top))
    repaired = top if amount is None else min(top, now + amount)
    if repaired <= now:
        return _error(409, f"it is already at full durability ({now}/{top})")
    set_prompt_refinement_fields(item["topic_id"], item["version"], {"durability": repaired})
    summary = _gear_summary({**item, "durability": repaired})
    return _response(200, {"repaired": {**equipment.ref(item), "was": now, **summary}})


def _create_equipment(event: dict) -> dict:
    """Make a new piece of gear by hand, and (by default) put it on.

    Body: {text (the guidance, required), scope ("global" armor or "topic" ring; inferred from topic_id),
    topic_id (rings), slot, rarity (else rolled), theme (else the bear names it), equip (default true),
    replace (a ring to swap out when all five are worn)}. The item is approved from the start: a person
    made it, so it skips the proposal step. Everything is checked before anything is written, so a
    refusal creates nothing.
    """
    body = _placement_body(event)
    if not body:
        return _error(400, "request body must be a JSON object with at least 'text'")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return _error(400, "'text' (what the gear tells the bear to do) is required")
    text = " ".join(text.split())
    if len(text) > equipment.MAX_GUIDANCE_LENGTH:
        return _error(400, f"'text' must be at most {equipment.MAX_GUIDANCE_LENGTH} characters")

    topic_id = body.get("topic_id")
    scope = body.get("scope") or (equipment.SCOPE_TOPIC if topic_id else equipment.SCOPE_GLOBAL)
    if scope not in (equipment.SCOPE_GLOBAL, equipment.SCOPE_TOPIC):
        return _error(400, "'scope' must be 'global' (armor) or 'topic' (a ring)")
    if scope == equipment.SCOPE_TOPIC:
        if not isinstance(topic_id, str) or not topic_id:
            return _error(400, "a ring is tied to a topic: 'topic_id' is required")
        if get_topic(topic_id) is None:
            return _error(404, f"topic '{topic_id}' not found")
        key = topic_id
    else:
        if topic_id not in (None, "", equipment.GLOBAL_TOPIC_ID):
            return _error(400, "armor applies to every topic: leave out 'topic_id', or use scope 'topic'")
        key = equipment.GLOBAL_TOPIC_ID

    rarity = body.get("rarity")
    if rarity is not None and rarity not in gear.RARITIES:
        return _error(400, f"'rarity' must be one of: {', '.join(gear.RARITIES)}")
    slot = body.get("slot")
    all_slots = (*equipment.ARMOR_SLOTS, equipment.RING_SLOT)
    if slot is not None and slot not in all_slots:
        return _error(400, f"'slot' must be one of: {', '.join(all_slots)}")
    equip = body.get("equip", True)
    if not isinstance(equip, bool):
        return _error(400, "'equip' must be true or false")

    theme = body.get("theme")
    slot_hint = None
    if theme is not None:
        theme = gear.clean_theme(theme)
        if theme is None:
            return _error(400, "'theme' must be 2 to 5 plain words (letters, spaces, apostrophes, hyphens)")
    else:
        # The bear names it and suggests a slot; any failure falls back to a plain name from the topic.
        try:
            generated = gear.generate_identity(key, text, os.environ["BEDROCK_MODEL_ID"])
            theme, slot_hint = generated["theme"], generated["slot_hint"]
        except Exception as exc:  # noqa: BLE001 - a name is never worth refusing the gear for
            print(f"admin_api_handler: could not name new gear: {exc!r}")
            theme = gear.fallback_theme(key)

    version = datetime.now(UTC).isoformat()
    identity = gear.new_identity(theme, slot or slot_hint, rarity=rarity)
    item = {
        "topic_id": key,
        "version": version,
        "proposed_at": version,
        "rationale": "Created by an admin.",
        "prompt_changes": text,
        "status": "approved",
        "scope": scope,
        "created_by": "admin",
        **identity,
    }

    plan = None
    if equip:
        try:
            plan = equipment.plan_equip(
                list_prompt_refinements(status="approved"),
                item,
                scope=scope,
                slot=slot,
                replace=body.get("replace"),
            )
        except equipment.EquipError as exc:
            return _error(exc.status, exc.message)

    fixed = ("topic_id", "version", "proposed_at", "rationale", "prompt_changes", "status")
    put_prompt_refinement(
        key,
        version,
        item["rationale"],
        text,
        status="approved",
        extra={k: v for k, v in item.items() if k not in fixed},
    )
    if plan is not None:
        _wear(item, plan)
    else:
        set_prompt_refinement_equipment(
            key,
            version,
            equipped=False,
            at=datetime.now(UTC).isoformat(),
            reason=equipment.SHELVED,
        )
    return _response(
        201,
        {
            "created": {
                **equipment.ref(item),
                "placement": _placement_view(item, plan),
                "item": _gear_summary(_as_worn(item, plan)),
                "loot_drop": _announce_drop(item, plan, announce=_announce_flag(body)),
            }
        },
    )


def _announce_loot(event: dict) -> dict:
    """Announce a piece of gear as a loot drop (again, if it already was): the fix when the first
    announcement was skipped or failed."""
    item, error = _approved_refinement_or_error(event)
    if error:
        return error
    item = _ensure_identity(item)
    plan = {"slot": item["slot"]} if item.get("slot") else None
    musing_id = _announce_drop(item, plan, force=True, raise_errors=True)
    return _response(200, {"announced": {**equipment.ref(item), "musing_id": musing_id}})


def _delete_prompt_refinement(event: dict) -> dict:
    """Remove a piece of gear entirely (worn or not). Articles written with it keep their own record."""
    topic_id = _path_param(event, "topic_id")
    version = _path_param(event, "version")
    item = get_prompt_refinement(topic_id, version)
    if item is None:
        return _error(404, f"prompt refinement '{topic_id}'/'{version}' not found")
    delete_prompt_refinement(topic_id, version)
    return _response(
        200,
        {
            "deleted": {
                **equipment.ref(item),
                "name": _view(item)["name"],
                "was_equipped": equipment.is_equipped(item),
            }
        },
    )


def _raise_rarity(event: dict) -> dict:
    """Bump an item's rarity up. Body {"rarity": "epic"}, or none for one step up. Only up."""
    topic_id = _path_param(event, "topic_id")
    version = _path_param(event, "version")
    item = get_prompt_refinement(topic_id, version)
    if item is None:
        return _error(404, f"prompt refinement '{topic_id}'/'{version}' not found")
    if item.get("status") == "rejected":
        return _error(409, "a rejected prompt change has no gear to bump")
    body = _placement_body(event)
    if body is None:
        return _error(400, "request body must be a JSON object")
    item = _ensure_identity(item)
    target = body.get("rarity") or gear.next_rarity(item["rarity"])
    if target is None:
        return _error(409, f"it is already {item['rarity']}: there is nothing higher")
    try:
        changes = gear.bump(item, target)
    except ValueError as exc:
        return _error(400 if target not in gear.RARITIES else 409, str(exc))
    set_prompt_refinement_fields(topic_id, version, changes)
    summary = _gear_summary({**item, **changes})
    return _response(200, {"bumped": {**equipment.ref(item), "was": item["rarity"], **summary}})


def _get_equipment(event: dict) -> dict:
    return _response(
        200, equipment.describe(list_prompt_refinements(status="approved"), decorate=_view)
    )


# --- Failed executions (DLQ consumer) --------------------------------------


def _list_failed_executions(event: dict) -> dict:
    return _response(200, {"items": list_failed_executions()})


# --- Models / ModelConfig (AI lineage/cost-tracking enhancement, PR 1) ------
#
# The "supported models" registry (docs/project-plan.md §11) -- adding or
# switching a model happens here, via the admin API/CLI, never via a
# Terraform apply. common/model_routing.py's resolve_model is what
# actually reads these at call time.


def _list_models(event: dict) -> dict:
    return _response(200, {"models": list_models()})


def _put_model(event: dict) -> dict:
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")

    model_id = body.get("model_id")
    display_name = body.get("display_name")
    provider = body.get("provider")
    input_price = body.get("input_price_usd_per_1k_tokens")
    output_price = body.get("output_price_usd_per_1k_tokens")
    enabled = body.get("enabled", True)

    if not isinstance(model_id, str) or not model_id:
        return _error(400, "'model_id' is required and must be a non-empty string")
    if not isinstance(display_name, str) or not display_name:
        return _error(400, "'display_name' is required and must be a non-empty string")
    if not isinstance(provider, str) or not provider:
        return _error(400, "'provider' is required and must be a non-empty string")
    if not isinstance(input_price, int | float) or isinstance(input_price, bool) or input_price < 0:
        return _error(400, "'input_price_usd_per_1k_tokens' is required and must be a non-negative number")
    if not isinstance(output_price, int | float) or isinstance(output_price, bool) or output_price < 0:
        return _error(400, "'output_price_usd_per_1k_tokens' is required and must be a non-negative number")
    if not isinstance(enabled, bool):
        return _error(400, "'enabled' must be a boolean if provided")

    item = {
        "model_id": model_id,
        "display_name": display_name,
        "provider": provider,
        "input_price_usd_per_1k_tokens": input_price,
        "output_price_usd_per_1k_tokens": output_price,
        "enabled": enabled,
    }
    put_model(item)
    return _response(200, item)


def _get_model_config(event: dict) -> dict:
    config = get_model_config() or {"config_id": "default", "model_id": None, "fallback_model_id": None}
    return _response(200, config)


def _put_model_config_route(event: dict) -> dict:
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")

    model_id = body.get("model_id")
    if model_id is not None and (not isinstance(model_id, str) or not model_id):
        return _error(400, "'model_id' must be a non-empty string if provided")
    fallback_model_id = body.get("fallback_model_id")
    if fallback_model_id is not None and (
        not isinstance(fallback_model_id, str) or not fallback_model_id
    ):
        return _error(400, "'fallback_model_id' must be a non-empty string if provided")

    item = put_model_config(model_id=model_id, fallback_model_id=fallback_model_id)
    return _response(200, item)


def _get_pipeline_config_route(event: dict) -> dict:
    config = get_pipeline_config() or {}
    return _response(
        200,
        {
            "config_id": "pipeline",
            "research_interval_hours": config.get("research_interval_hours"),
            "effective_default_research_interval_hours": (
                config.get("research_interval_hours") or DEFAULT_RESEARCH_INTERVAL_HOURS
            ),
            "review_mode": config.get("review_mode"),
            "effective_review_mode": resolve_review_mode(config),
            "review_on_unavailable": config.get("review_on_unavailable"),
            "effective_review_on_unavailable": resolve_on_unavailable(config),
        },
    )


def _put_pipeline_config_route(event: dict) -> dict:
    """Set (or, with null, clear) pipeline-wide settings. Send either or both:

    - `research_interval_hours`: how often a topic without its own interval does real
      work on a heartbeat.
    - `review_mode`: the fresh-data review of drafts: `off`, `shadow` (recorded only) or
      `enforce` (it acts). A topic's own `review_mode` overrides it.
    - `review_on_unavailable`: what enforce mode does when the review could not run:
      `hold` the article for a person (the default) or `note` it and publish.

    A setting that isn't in the body is left as it is.
    """
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")

    updates = {}
    if "research_interval_hours" in body:
        interval_problem = interval_error(body["research_interval_hours"])
        if interval_problem:
            return _error(400, f"'research_interval_hours' {interval_problem}")
        updates["research_interval_hours"] = body["research_interval_hours"]
    if "review_mode" in body:
        mode_problem = review_mode_error(body["review_mode"])
        if mode_problem:
            return _error(400, f"'review_mode' {mode_problem}")
        updates["review_mode"] = body["review_mode"]
    if "review_on_unavailable" in body:
        problem = on_unavailable_error(body["review_on_unavailable"])
        if problem:
            return _error(400, f"'review_on_unavailable' {problem}")
        updates["review_on_unavailable"] = body["review_on_unavailable"]
    if not updates:
        return _error(
            400,
            "send 'research_interval_hours', 'review_mode' and/or 'review_on_unavailable' "
            "(null clears a setting)",
        )

    put_pipeline_config(**updates)
    return _get_pipeline_config_route(event)


# --- Feedback limits -----------------------------------------------------------------------

_FEEDBACK_SETTINGS = (
    "locked_down",
    "lockdown_reason",
    "rate_limit_count",
    "rate_limit_window_minutes",
    "daily_limit",
    "article_limit",
    "screening_limit",
    "verification_required",
    "token_delay_min_ms",
    "token_delay_max_ms",
    "pow_threshold_percent",
    "pow_difficulty_bits",
    "daily_timezone",
)


def _get_feedback_config_route(event: dict) -> dict:
    """The stored feedback settings, the ones in force (a missing or invalid one takes its
    default), and today's and this window's usage."""
    row = get_feedback_config() or {}
    return _response(
        200,
        {
            "config_id": "feedback",
            **{name: row.get(name) for name in _FEEDBACK_SETTINGS},
            "effective": feedback_limits.effective_settings(row),
            "usage": feedback_limits.usage(),
        },
    )


def _put_feedback_config_route(event: dict) -> dict:
    """Set (or, with null, clear back to the default) feedback settings. Send any of:

    - `locked_down` (true/false): stop taking feedback site-wide; the page shows why.
    - `lockdown_reason` (short text): what the page says while locked down.
    - `rate_limit_count` and `rate_limit_window_minutes`: at most this many in a window.
    - `daily_limit`: at most this many a day, resetting at the start of the day.
    - `article_limit`: at most this many on one article, then it is locked.
    - `screening_limit`: at most this many comments a day are sent to the model check (rejected
      feedback does not count against the limits above, so this is what bounds its cost).
    - `verification_required` (true/false, default true): whether a submission must carry the
      token from the feedback-status call. Turn it off only in an emergency.
    - `token_delay_min_ms` / `token_delay_max_ms`: how long after a token is issued it becomes
      valid, at random between the two (default 500 to 2000).
    - `pow_threshold_percent` and `pow_difficulty_bits`: when the site is this full (of its daily
      or rate limit), tokens need proof of work of this many bits (default 70 and 16; 0 bits
      never asks for it).
    - `daily_timezone` (IANA name): where a day starts.

    A setting that isn't in the body is left as it is.
    """
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")
    if not isinstance(body, dict):
        return _error(400, "the body must be a JSON object")

    updates = {}
    for name in _FEEDBACK_SETTINGS:
        if name not in body:
            continue
        problem = feedback_limits.settings_error(name, body[name])
        if problem:
            return _error(400, f"'{name}' {problem}")
        value = body[name]
        updates[name] = value.strip() if isinstance(value, str) else value
    if not updates:
        return _error(400, f"send at least one of: {', '.join(_FEEDBACK_SETTINGS)} (null clears one)")

    put_feedback_config(updates)
    return _get_feedback_config_route(event)


def _feedback_lock_article(event: dict) -> dict:
    """Lock (or unlock) feedback on one article: `{"locked": true|false}`. Unlocking with
    `{"locked": false, "reset_count": true}` also gives the article its full allowance again.
    The same flag can be flipped by hand in the Articles table (`feedback_locked`)."""
    article_id = _path_param(event, "article_id")
    article = get_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")
    if not isinstance(body, dict) or not isinstance(body.get("locked"), bool):
        return _error(400, "'locked' must be true or false")
    reset_count = body.get("reset_count", False)
    if not isinstance(reset_count, bool):
        return _error(400, "'reset_count' must be true or false if provided")

    set_article_feedback_lock(article_id, body["locked"], reset_count=reset_count)
    updated = get_article(article_id) or {}
    return _response(
        200,
        {
            "article_id": article_id,
            "feedback_locked": bool(updated.get("feedback_locked")),
            "feedback_count": int(updated.get("feedback_count") or 0),
        },
    )


_ROUTES = {
    "GET /topics": _list_topics,
    "POST /topics": _create_topic,
    "GET /topics/{topic_id}": _get_topic,
    "PUT /topics/{topic_id}": _update_topic,
    "DELETE /topics/{topic_id}": _delete_topic,
    "POST /topics/{topic_id}/trigger": _trigger_topic,
    "GET /topics/{topic_id}/candidates": _list_candidates,
    "GET /topics/{topic_id}/findings/latest": _get_latest_finding_route,
    "GET /articles/{article_id}": _get_article,
    "POST /articles/{article_id}/publish": _publish_article,
    "POST /articles/{article_id}/unpublish": _unpublish_article,
    "POST /articles/{article_id}/rewrite": _rewrite_article,
    "GET /review/report": _review_report,
    "GET /lineage/audit": _lineage_audit,
    "POST /lineage/backfill": _lineage_backfill,
    "POST /stats/backfill-articles": _stats_backfill_articles,
    "GET /moderation-queue": _list_moderation_queue,
    "GET /moderation-queue/stats": _moderation_queue_stats,
    "POST /moderation-queue/{queue_id}/approve": _approve_moderation_item,
    "POST /moderation-queue/{queue_id}/reject": _reject_moderation_item,
    "POST /moderation-queue/{queue_id}/rewrite": _rewrite_moderation_item,
    "GET /prompt-refinements": _list_prompt_refinements,
    "POST /prompt-refinements/{topic_id}/{version}/approve": _approve_prompt_refinement,
    "POST /prompt-refinements/{topic_id}/{version}/reject": _reject_prompt_refinement,
    "POST /prompt-refinements/{topic_id}/{version}/equip": _equip_prompt_refinement,
    "POST /prompt-refinements/{topic_id}/{version}/unequip": _unequip_prompt_refinement,
    "POST /prompt-refinements/{topic_id}/{version}/rarity": _raise_rarity,
    "POST /prompt-refinements/{topic_id}/{version}/repair": _repair_gear,
    "GET /equipment": _get_equipment,
    "POST /equipment": _create_equipment,
    "POST /prompt-refinements/{topic_id}/{version}/announce": _announce_loot,
    "DELETE /prompt-refinements/{topic_id}/{version}": _delete_prompt_refinement,
    "GET /failed-executions": _list_failed_executions,
    "GET /models": _list_models,
    "POST /models": _put_model,
    "GET /model-config": _get_model_config,
    "PUT /model-config": _put_model_config_route,
    "GET /pipeline-config": _get_pipeline_config_route,
    "PUT /pipeline-config": _put_pipeline_config_route,
    "GET /feedback-config": _get_feedback_config_route,
    "PUT /feedback-config": _put_feedback_config_route,
    "PUT /articles/{article_id}/feedback-lock": _feedback_lock_article,
}


def _route_key(event: dict) -> str | None:
    """"METHOD /path" for this event -- routeKey (HTTP API) if present,
    else httpMethod + resource (REST API's Lambda proxy event shape).
    """
    route_key = event.get("routeKey")
    if route_key is not None:
        return route_key
    method = event.get("httpMethod")
    resource = event.get("resource")
    if method is None or resource is None:
        return None
    return f"{method} {resource}"


def handler(event, context) -> dict:
    route_key = _route_key(event)
    route_fn = _ROUTES.get(route_key)

    if route_fn is None:
        return _error(404, f"no route for '{route_key}'")

    try:
        return route_fn(event)
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"admin_api_handler: unhandled exception on {route_key}: {exc!r}")
        return _error(500, "internal server error")
