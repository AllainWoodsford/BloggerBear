"""Admin API Lambda handler (Phase 2 admin console).

Deployed behind an API Gateway HTTP API (payload format version 2.0) that is
authenticated separately (SigV4/IAM + a WAF IP allowlist -- see
`infra/`, owned by another worker in this phase). This module only assumes
that authentication has already happened by the time `handler` runs.

Routing is a plain dict dispatch keyed on `event["routeKey"]`
(e.g. "GET /topics"), matching what API Gateway HTTP APIs send. `handler`
never raises -- every route function is wrapped in a broad top-level
try/except that logs the real exception and returns a generic 500, per the
task contract.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import boto3

from common.dynamo import (
    delete_topic,
    get_moderation_item,
    get_topic,
    list_candidate_ideas,
    list_pending_moderation,
    list_topics,
    put_topic,
    update_article_status,
    update_moderation_status,
)

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

    if get_topic(topic_id) is not None:
        return _error(409, f"topic '{topic_id}' already exists")

    item = {
        "topic_id": topic_id,
        "name": name,
        "adapter": adapter,
        "adapter_config": adapter_config,
        "is_financial": is_financial,
    }
    put_topic(item)
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
    for field in ("name", "adapter", "adapter_config", "is_financial"):
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

    put_topic(updated)
    return _response(200, updated)


def _delete_topic(event: dict) -> dict:
    topic_id = _path_param(event, "topic_id")
    if get_topic(topic_id) is None:
        return _error(404, f"topic '{topic_id}' not found")
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


# --- Moderation queue -----------------------------------------------------


def _list_moderation_queue(event: dict) -> dict:
    return _response(200, {"items": list_pending_moderation()})


def _resolve_moderation_item(event: dict, *, new_status: str, article_status: str) -> dict:
    queue_id = _path_param(event, "queue_id")
    item = get_moderation_item(queue_id)
    if item is None:
        return _error(404, f"moderation queue item '{queue_id}' not found")
    if item.get("status") != "pending":
        return _error(409, f"moderation queue item '{queue_id}' is not pending")

    article_id = item["article_id"]
    published_at = datetime.now(UTC).isoformat() if article_status == "published" else None
    update_article_status(article_id, article_status, published_at=published_at)
    update_moderation_status(queue_id, new_status)

    action_key = "approved" if new_status == "approved" else "rejected"
    return _response(200, {action_key: queue_id, "article_id": article_id})


def _approve_moderation_item(event: dict) -> dict:
    return _resolve_moderation_item(event, new_status="approved", article_status="published")


def _reject_moderation_item(event: dict) -> dict:
    return _resolve_moderation_item(event, new_status="rejected", article_status="rejected")


_ROUTES = {
    "GET /topics": _list_topics,
    "POST /topics": _create_topic,
    "GET /topics/{topic_id}": _get_topic,
    "PUT /topics/{topic_id}": _update_topic,
    "DELETE /topics/{topic_id}": _delete_topic,
    "POST /topics/{topic_id}/trigger": _trigger_topic,
    "GET /topics/{topic_id}/candidates": _list_candidates,
    "GET /moderation-queue": _list_moderation_queue,
    "POST /moderation-queue/{queue_id}/approve": _approve_moderation_item,
    "POST /moderation-queue/{queue_id}/reject": _reject_moderation_item,
}


def handler(event, context) -> dict:
    route_key = event.get("routeKey")
    route_fn = _ROUTES.get(route_key)

    if route_fn is None:
        return _error(404, f"no route for '{route_key}'")

    try:
        return route_fn(event)
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"admin_api_handler: unhandled exception on {route_key}: {exc!r}")
        return _error(500, "internal server error")
