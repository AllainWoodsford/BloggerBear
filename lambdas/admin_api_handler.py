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
from datetime import UTC, datetime

import boto3

from common.adapters import CRYPTO_FEED_ADAPTER_KEY
from common.digest import DIGEST_TOPIC_ID, DIGEST_TOPIC_NAME
from common.dynamo import (
    delete_topic,
    get_article,
    get_latest_finding,
    get_model_config,
    get_moderation_item,
    get_moderation_item_by_article_id,
    get_prompt_refinement,
    get_topic,
    list_all_moderation_items,
    list_candidate_ideas,
    list_failed_executions,
    list_models,
    list_pending_moderation,
    list_prompt_refinements,
    list_topics,
    put_model,
    put_model_config,
    put_topic,
    update_article_status,
    update_moderation_status,
    update_prompt_refinement_status,
)
from common.musings import generate_and_store_article_musing
from common.scheduler import (
    _validate_schedule_expression,
    delete_topic_schedules,
    upsert_topic_schedules,
)
from common.static_pages import read_article_body, render_and_publish_article_page

_DEFAULT_RESEARCH_CADENCE = "rate(1 hour)"
_DEFAULT_DAILY_CADENCE = "cron(0 6 * * ? *)"

_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _response(status_code: int, payload: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
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


def _create_topic(event: dict) -> dict:
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, TypeError):
        return _error(400, "request body must be valid JSON")

    topic_id = body.get("topic_id")
    name = body.get("name")
    adapter = body.get("adapter")

    if not isinstance(topic_id, str) or not topic_id:
        return _error(400, "'topic_id' is required and must be a non-empty string")
    if not isinstance(name, str) or not name:
        return _error(400, "'name' is required and must be a non-empty string")
    if not isinstance(adapter, str) or not adapter:
        return _error(400, "'adapter' is required and must be a non-empty string")

    adapter_config = body.get("adapter_config", {})
    if not isinstance(adapter_config, dict):
        return _error(400, "'adapter_config' must be an object if provided")

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
    if not isinstance(research_cadence, str) or not research_cadence:
        return _error(400, "'research_cadence' must be a non-empty string if provided")
    if not isinstance(daily_cadence, str) or not daily_cadence:
        return _error(400, "'daily_cadence' must be a non-empty string if provided")
    try:
        _validate_schedule_expression(research_cadence)
        _validate_schedule_expression(daily_cadence)
    except ValueError as exc:
        return _error(400, str(exc))

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
        "model_id": model_id,
        "fallback_model_id": fallback_model_id,
    }
    put_topic(item)
    try:
        upsert_topic_schedules(topic_id, research_cadence, daily_cadence)
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
        "is_financial",
        "research_cadence",
        "daily_cadence",
        "model_id",
        "fallback_model_id",
    ):
        if field in body:
            updated[field] = body[field]

    if "name" in body and (not isinstance(updated["name"], str) or not updated["name"]):
        return _error(400, "'name' must be a non-empty string")
    if "adapter" in body and (not isinstance(updated["adapter"], str) or not updated["adapter"]):
        return _error(400, "'adapter' must be a non-empty string")
    if "adapter_config" in body and not isinstance(updated["adapter_config"], dict):
        return _error(400, "'adapter_config' must be an object")
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
    if "model_id" in body and updated["model_id"] is not None and (
        not isinstance(updated["model_id"], str) or not updated["model_id"]
    ):
        return _error(400, "'model_id' must be a non-empty string if provided")
    if "fallback_model_id" in body and updated["fallback_model_id"] is not None and (
        not isinstance(updated["fallback_model_id"], str) or not updated["fallback_model_id"]
    ):
        return _error(400, "'fallback_model_id' must be a non-empty string if provided")

    research_cadence = updated.setdefault("research_cadence", _DEFAULT_RESEARCH_CADENCE)
    daily_cadence = updated.setdefault("daily_cadence", _DEFAULT_DAILY_CADENCE)
    try:
        _validate_schedule_expression(research_cadence)
        _validate_schedule_expression(daily_cadence)
    except ValueError as exc:
        return _error(400, str(exc))

    put_topic(updated)
    try:
        upsert_topic_schedules(topic_id, research_cadence, daily_cadence)
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

    function_name = os.environ[_PIPELINE_FUNCTION_ENV_VARS[pipeline]]
    client = _get_lambda_client()
    client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"topic_id": topic_id}).encode("utf-8"),
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
    if article["topic_id"] == DIGEST_TOPIC_ID:
        topic_name = DIGEST_TOPIC_NAME
    else:
        topic = get_topic(article["topic_id"])
        topic_name = (topic or {}).get("name", article["topic_id"])
    body_markdown = read_article_body(article["body_s3_key"])
    render_and_publish_article_page(
        article_id=article["article_id"],
        title=article["title"],
        body_markdown=body_markdown,
        topic_name=topic_name,
        published_at=published_at,
        source_refs=article.get("source_refs"),
        view_count=int(article.get("view_count", 0)),
    )
    generate_and_store_article_musing(
        article_id=article["article_id"],
        topic_id=article["topic_id"],
        topic_name=topic_name,
        title=article["title"],
        compliant=False,
        model_id=os.environ["BEDROCK_MODEL_ID"],
    )


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
    if moderation_item is not None and moderation_item.get("status") == "pending":
        update_moderation_status(moderation_item["queue_id"], "approved")

    return _response(200, {"published": article_id})


# --- Moderation queue -----------------------------------------------------


def _list_moderation_queue(event: dict) -> dict:
    return _response(200, {"items": list_pending_moderation()})


_STATS_RECENT_LIMIT = 20


def _moderation_queue_stats(event: dict) -> dict:
    """Summarize what compliance review has flagged, across all history.

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

    action_key = "approved" if new_status == "approved" else "rejected"
    return _response(200, {action_key: queue_id, "article_id": article_id})


def _approve_moderation_item(event: dict) -> dict:
    return _resolve_moderation_item(event, new_status="approved", article_status="published")


def _reject_moderation_item(event: dict) -> dict:
    return _resolve_moderation_item(event, new_status="rejected", article_status="rejected")


# --- Prompt refinements (Phase 5) ----------------------------------------


def _list_prompt_refinements(event: dict) -> dict:
    topic_id = _query_param(event, "topic_id")
    status = _query_param(event, "status")
    refinements = list_prompt_refinements(topic_id=topic_id, status=status)
    return _response(200, {"refinements": refinements})


def _resolve_prompt_refinement(event: dict, *, new_status: str) -> dict:
    topic_id = _path_param(event, "topic_id")
    version = _path_param(event, "version")
    item = get_prompt_refinement(topic_id, version)
    if item is None:
        return _error(404, f"prompt refinement '{topic_id}'/'{version}' not found")
    if item.get("status") != "pending":
        return _error(409, f"prompt refinement '{topic_id}'/'{version}' is not pending")

    update_prompt_refinement_status(topic_id, version, new_status)
    action_key = "approved" if new_status == "approved" else "rejected"
    return _response(200, {action_key: {"topic_id": topic_id, "version": version}})


def _approve_prompt_refinement(event: dict) -> dict:
    return _resolve_prompt_refinement(event, new_status="approved")


def _reject_prompt_refinement(event: dict) -> dict:
    return _resolve_prompt_refinement(event, new_status="rejected")


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


_ROUTES = {
    "GET /topics": _list_topics,
    "POST /topics": _create_topic,
    "GET /topics/{topic_id}": _get_topic,
    "PUT /topics/{topic_id}": _update_topic,
    "DELETE /topics/{topic_id}": _delete_topic,
    "POST /topics/{topic_id}/trigger": _trigger_topic,
    "GET /topics/{topic_id}/candidates": _list_candidates,
    "GET /topics/{topic_id}/findings/latest": _get_latest_finding_route,
    "POST /articles/{article_id}/publish": _publish_article,
    "GET /moderation-queue": _list_moderation_queue,
    "GET /moderation-queue/stats": _moderation_queue_stats,
    "POST /moderation-queue/{queue_id}/approve": _approve_moderation_item,
    "POST /moderation-queue/{queue_id}/reject": _reject_moderation_item,
    "GET /prompt-refinements": _list_prompt_refinements,
    "POST /prompt-refinements/{topic_id}/{version}/approve": _approve_prompt_refinement,
    "POST /prompt-refinements/{topic_id}/{version}/reject": _reject_prompt_refinement,
    "GET /failed-executions": _list_failed_executions,
    "GET /models": _list_models,
    "POST /models": _put_model,
    "GET /model-config": _get_model_config,
    "PUT /model-config": _put_model_config_route,
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
