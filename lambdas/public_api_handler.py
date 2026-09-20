"""Public API Lambda handler (Phase 4 public frontend).

Deployed behind a second, unauthenticated API Gateway REST API (v1) fronted
by CloudFront/WAF rate limiting -- see `infra/`, owned by another worker in
this phase. Unlike `admin_api_handler.py` (SigV4 + IP allowlist, for the
operator), every route here is anonymous and read-mostly: list topics,
list/read published articles, bump an anonymous view counter, and serve the
site-wide RSS feed.

Routing mirrors `admin_api_handler.py`'s style exactly: a plain dict
dispatch keyed on a route key of the form "GET /topics", built from
`routeKey` if present or `httpMethod` + `resource` otherwise (see that
module's `_route_key` for why both exist -- this project migrated from API
Gateway HTTP API to REST API after discovering AWS WAF can't attach to HTTP
APIs at all). `handler` never raises -- every route function runs under a
broad top-level try/except that logs the real exception and returns a
generic 500.

This API is called from browser JS on a different origin (the CloudFront
domain) than its own API Gateway domain, so every response needs CORS
headers, and OPTIONS preflight requests need a response of their own --
REST API has no declarative equivalent of HTTP API's cors_configuration
block when every method (OPTIONS included) is proxied straight to Lambda,
so both are handled here instead of in Terraform.

Because a pending-moderation or rejected article must never be
distinguishable from one that doesn't exist at all (per the task contract),
every route that looks up a single article by id treats
`status != "published"` exactly like "not found".
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape

import boto3

from common.compliance import bedrock_redact_review, regex_redact
from common.dynamo import (
    get_article,
    get_latest_finding,
    increment_view_count,
    list_all_articles,
    list_models,
    list_musings,
    list_pending_moderation_for_topic,
    list_published_articles,
    list_topics,
    put_feedback,
    update_article_net_votes,
)

from common.stats import build_stats

from common.source_refs import dedupe_source_refs


_RSS_ITEM_LIMIT = 50
_RSS_DESCRIPTION_MAX_CHARS = 300

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


# Matches this project's original HTTP API cors_configuration
# (allow_origins = ["*"], allow_methods = ["GET","POST","OPTIONS"],
# allow_headers = ["content-type"]) -- REST API has no equivalent
# declarative block, so every response (this dict) and the OPTIONS
# preflight response (handler(), below) carry these explicitly instead.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
}


def _response(
    status_code: int,
    payload,
    *,
    content_type: str = "application/json",
    cache_seconds: int | None = None,
) -> dict:
    body = json.dumps(payload) if content_type == "application/json" else payload
    headers = {"Content-Type": content_type, **_CORS_HEADERS}
    if cache_seconds is not None:
        headers["Cache-Control"] = f"public, max-age={cache_seconds}"
    return {"statusCode": status_code, "headers": headers, "body": body}


def _error(status_code: int, message: str) -> dict:
    return _response(status_code, {"error": message})


def _path_param(event: dict, name: str) -> str | None:
    return (event.get("pathParameters") or {}).get(name)


def _query_param(event: dict, name: str) -> str | None:
    return (event.get("queryStringParameters") or {}).get(name)


def _get_published_article(article_id: str) -> dict | None:
    """Fetch an Articles item, returning None unless it exists AND is published."""
    article = get_article(article_id)
    if article is None or article.get("status") != "published":
        return None
    return article


def _read_body_from_s3(body_s3_key: str) -> str:
    s3 = _get_s3_client()
    response = s3.get_object(Bucket=os.environ["CONTENT_BUCKET"], Key=body_s3_key)
    return response["Body"].read().decode("utf-8")


# --- Topics -------------------------------------------------------------


def _list_topics(event: dict) -> dict:
    """Per-topic article_count/latest_published_at/researching, for the
    frontend's home-page article counts and its capped top-nav ranking
    (most-recently-published topics first, falling back to
    actively-researched-but-not-yet-published ones -- see app.js's
    renderNav). One list_published_articles() scan grouped by topic_id
    here, rather than one Scan per topic; get_latest_finding is only
    called for topics with zero published articles, since a topic that's
    already publishing doesn't need a "is it researching" lookup.
    """
    published_by_topic: dict[str, list[dict]] = {}
    for article in list_published_articles():
        published_by_topic.setdefault(article["topic_id"], []).append(article)

    public_topics = []
    for t in list_topics():
        topic_id = t["topic_id"]
        articles = published_by_topic.get(topic_id, [])
        article_count = len(articles)
        latest_published_at = (
            max((a.get("published_at") or "" for a in articles), default="") or None
        )
        researching = article_count == 0 and get_latest_finding(topic_id) is not None
        public_topics.append(
            {
                "topic_id": topic_id,
                "name": t["name"],
                "article_count": article_count,
                "latest_published_at": latest_published_at,
                "researching": researching,
            }
        )
    return _response(200, {"topics": public_topics})


def _topic_activity(event: dict) -> dict:
    """Static article publishing (docs/project-plan.md §11): lets the
    frontend show a "BloggerBear is researching this topic" placeholder
    instead of a bare empty state when a topic has no published articles
    yet but research has actually started, plus an "Articles in the
    Pipeline" indicator (see app.js's renderPipelineSection) for anything
    sitting in moderation.

    Returns the coarse-grained `researching`/`pending_review_count` fields
    the frontend already uses, plus a small `pipeline_items` list for the
    pipeline box's right-hand titles. Pending-review items expose only an
    article title already stored in DynamoDB; researching exposes only the
    latest source title/url already visible once an article is eventually
    published. No bodies, moderation reasons, queue ids, or finding
    summaries are returned here.
    No topic-existence check, matching _list_articles above -- an unknown
    topic_id just yields `researching: false, pending_review_count: 0`, not
    a 404.
    """
    topic_id = _path_param(event, "topic_id")
    researching = False
    pipeline_items = []
    pending_items = list_pending_moderation_for_topic(topic_id)
    pending_review_count = len(pending_items)

    pending_items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    for pending_item in pending_items:
        article = get_article(pending_item.get("article_id")) or {}
        pipeline_items.append(
            {
                "status": "pending_review",
                "label": "Pending review",
                "title": article.get("title") or "",
            }
        )

    latest_finding = get_latest_finding(topic_id)
    if latest_finding is not None:
        researching = True
        research_ref = next(
            (
                ref
                for ref in dedupe_source_refs(latest_finding.get("source_refs"))
                if ref.get("title") or ref.get("url")
            ),
            None,
        )
        pipeline_items.append(
            {
                "status": "researching",
                "label": "Researching",
                "title": (research_ref or {}).get("title") or (research_ref or {}).get("url") or "",
            }
        )
    return _response(
        200,
        {
            "topic_id": topic_id,
            "researching": researching,
            "pending_review_count": pending_review_count,
            "pipeline_items": pipeline_items,
        },
    )


# --- Articles -------------------------------------------------------------


def _list_articles(event: dict) -> dict:
    topic_id = _query_param(event, "topic_id")
    if not topic_id:
        return _error(400, "'topic_id' query parameter is required")

    articles = list_published_articles(topic_id)
    articles.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    summaries = [
        {
            "article_id": a["article_id"],
            "title": a["title"],
            "published_at": a.get("published_at"),
            # Slim lineage projection for the topic listing's compact
            # one-line summary (docs/project-plan.md §11, PR 3 of 5) --
            # deliberately NOT the full lineage.calls breakdown, since
            # that's detail-page-only territory (fetching the full
            # per-call token detail for every article in a list would be
            # wasteful); everything here already comes from the same
            # Scan list_published_articles already did, no extra reads.
            "models_used": (a.get("lineage") or {}).get("models_used"),
            "total_input_tokens": (a.get("lineage") or {}).get("total_input_tokens"),
            "total_output_tokens": (a.get("lineage") or {}).get("total_output_tokens"),
            "cost_aud": (a.get("lineage") or {}).get("cost_aud"),
            "cost_note": (a.get("lineage") or {}).get("cost_note"),
            "published_by": a.get("published_by"),
        }
        for a in articles
    ]
    return _response(200, {"topic_id": topic_id, "articles": summaries})


def _get_article_detail(event: dict) -> dict:
    article_id = _path_param(event, "article_id")
    article = _get_published_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")

    body = _read_body_from_s3(article["body_s3_key"])
    return _response(
        200,
        {
            "article_id": article["article_id"],
            "title": article["title"],
            "body": body,
            "published_at": article.get("published_at"),
            "source_refs": dedupe_source_refs(article.get("source_refs")),
            "view_count": int(article.get("view_count", 0)),
            # AI lineage/cost tracking (docs/project-plan.md §11, PR 3 of
            # 5) -- explicit None (not omitted) on an article published
            # before this feature existed, so the frontend's "no data"
            # detection has something concrete to check against.
            "lineage": article.get("lineage"),
            "published_by": article.get("published_by"),
        },
    )


def _view_article(event: dict) -> dict:
    article_id = _path_param(event, "article_id")
    article = _get_published_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")

    new_count = increment_view_count(article_id)
    return _response(200, {"article_id": article_id, "view_count": new_count})


# --- Feedback -------------------------------------------------------------
#
# project-plan.md §7: regex redaction, then a Bedrock redaction review pass,
# before anything is written -- raw comment text is never persisted, logged,
# or echoed back, even transiently. `comment` is optional; a missing/empty
# one, or one the Bedrock pass can't confirm is safe, is stored as None.


def _submit_feedback(event: dict) -> dict:
    article_id = _path_param(event, "article_id")
    article = _get_published_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")

    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "invalid JSON body")

    vote = payload.get("vote")
    if vote not in ("up", "down"):
        return _error(400, "'vote' must be 'up' or 'down'")

    final_comment = None
    raw_comment = payload.get("comment")
    if raw_comment:
        redacted_comment = regex_redact(raw_comment)
        model_id = os.environ["BEDROCK_MODEL_ID"]
        final_comment = bedrock_redact_review(redacted_comment, model_id)

    feedback_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    put_feedback(article_id, feedback_id, vote, final_comment, created_at)
    update_article_net_votes(article_id, 1 if vote == "up" else -1)

    return _response(
        201,
        {"status": "recorded", "article_id": article_id, "feedback_id": feedback_id},
    )


# --- Musings --------------------------------------------------------------


def _list_musings(event: dict) -> dict:
    items = list_musings()
    musings = [
        {
            "musing_id": m.get("musing_id"),
            "kind": m.get("kind"),
            "article_id": m.get("article_id"),
            "topic_id": m.get("topic_id"),
            "text": m.get("text"),
            "mood": m.get("mood"),
            "created_at": m.get("created_at"),
        }
        for m in items
    ]
    return _response(200, {"musings": musings})


# --- Stats ------------------------------------------------------------------

# Every hit scans the Articles/Topics/Models tables (fine at this project's
# scale, same as the RSS feed) -- a short public cache keeps a popular page
# from turning into a scan per view. The numbers move on the scale of
# articles-per-day, so five minutes of staleness is invisible.
_STATS_CACHE_SECONDS = 300


def _stats(event: dict) -> dict:
    """Aggregate AI cost/token statistics for the public Stats page --
    aggregates only (common/stats.py), never article content or ids."""
    stats = build_stats(list_all_articles(), list_topics(), list_models())
    return _response(200, stats, cache_seconds=_STATS_CACHE_SECONDS)


# --- RSS feed ---------------------------------------------------------------


def _rss_pub_date(published_at: str | None) -> str | None:
    if not published_at:
        return None
    try:
        parsed = datetime.fromisoformat(published_at)
    except ValueError:
        return None
    return format_datetime(parsed)


def _rss_item_xml(article: dict, site_url: str) -> str:
    article_id = article["article_id"]
    title = article.get("title", "")
    body = article.get("body", "")
    description = body if len(body) <= _RSS_DESCRIPTION_MAX_CHARS else body[:_RSS_DESCRIPTION_MAX_CHARS]
    link = f"{site_url}/#/article/{article_id}"

    pub_date = _rss_pub_date(article.get("published_at"))
    pub_date_xml = f"<pubDate>{escape(pub_date)}</pubDate>" if pub_date else ""

    return (
        "<item>"
        f"<title>{escape(title)}</title>"
        f"<link>{escape(link)}</link>"
        f"<guid>{escape(link)}</guid>"
        f"<description>{escape(description)}</description>"
        f"{pub_date_xml}"
        "</item>"
    )


def _rss_feed(event: dict) -> dict:
    site_url = os.environ["SITE_URL"].rstrip("/")

    articles = list_published_articles()
    articles.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    articles = articles[:_RSS_ITEM_LIMIT]

    # The body text lives in S3, not the Articles item, but only the first
    # ~300 chars are needed for the description -- fetch each body directly
    # rather than pulling in the full article-detail path.
    items_xml = []
    for article in articles:
        body = ""
        body_s3_key = article.get("body_s3_key")
        if body_s3_key:
            try:
                body = _read_body_from_s3(body_s3_key)
            except Exception as exc:  # noqa: BLE001 - one bad S3 object must not break the whole feed
                article_id = article.get("article_id")
                print(f"public_api_handler: failed to read body for RSS item {article_id}: {exc!r}")
        items_xml.append(_rss_item_xml({**article, "body": body}, site_url))

    channel_title = escape("BloggerBear")
    channel_link = escape(site_url)
    channel_description = escape("BloggerBear -- autonomous research and publishing")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0">'
        "<channel>"
        f"<title>{channel_title}</title>"
        f"<link>{channel_link}</link>"
        f"<description>{channel_description}</description>"
        + "".join(items_xml)
        + "</channel>"
        "</rss>"
    )
    return _response(200, xml, content_type="application/rss+xml; charset=utf-8")


_ROUTES = {
    "GET /topics": _list_topics,
    "GET /topics/{topic_id}/activity": _topic_activity,
    "GET /articles": _list_articles,
    "GET /articles/{article_id}": _get_article_detail,
    "POST /articles/{article_id}/view": _view_article,
    "POST /articles/{article_id}/feedback": _submit_feedback,
    "GET /musings": _list_musings,
    "GET /stats": _stats,
    "GET /rss.xml": _rss_feed,
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
    # Browser CORS preflight -- every route this API serves is reachable
    # cross-origin from the frontend's JS (see this module's docstring),
    # so OPTIONS is handled uniformly here rather than per-route. No
    # _ROUTES entry for it: unlike every real route, the response never
    # depends on which path was requested.
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": _CORS_HEADERS, "body": ""}

    route_key = _route_key(event)
    route_fn = _ROUTES.get(route_key)

    if route_fn is None:
        return _error(404, f"no route for '{route_key}'")

    try:
        return route_fn(event)
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"public_api_handler: unhandled exception on {route_key}: {exc!r}")
        return _error(500, "internal server error")
