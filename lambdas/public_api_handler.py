"""Public API Lambda handler (Phase 4 public frontend).

Deployed behind a second, unauthenticated API Gateway HTTP API (payload
format version 2.0) fronted by CloudFront/WAF rate limiting -- see `infra/`,
owned by another worker in this phase. Unlike `admin_api_handler.py` (SigV4
+ IP allowlist, for the operator), every route here is anonymous and
read-mostly: list topics, list/read published articles, bump an anonymous
view counter, and serve the site-wide RSS feed.

Routing mirrors `admin_api_handler.py`'s style exactly: a plain dict
dispatch keyed on `event["routeKey"]` (e.g. "GET /topics"), and `handler`
never raises -- every route function runs under a broad top-level
try/except that logs the real exception and returns a generic 500.

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
    increment_view_count,
    list_published_articles,
    list_topics,
    put_feedback,
    update_article_net_votes,
)

_RSS_ITEM_LIMIT = 50
_RSS_DESCRIPTION_MAX_CHARS = 300

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _response(status_code: int, payload, *, content_type: str = "application/json") -> dict:
    body = json.dumps(payload) if content_type == "application/json" else payload
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": content_type},
        "body": body,
    }


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
    public_topics = [{"topic_id": t["topic_id"], "name": t["name"]} for t in list_topics()]
    return _response(200, {"topics": public_topics})


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
            "source_refs": article.get("source_refs", []),
            "view_count": int(article.get("view_count", 0)),
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
    "GET /articles": _list_articles,
    "GET /articles/{article_id}": _get_article_detail,
    "POST /articles/{article_id}/view": _view_article,
    "POST /articles/{article_id}/feedback": _submit_feedback,
    "GET /rss.xml": _rss_feed,
}


def handler(event, context) -> dict:
    route_key = event.get("routeKey")
    route_fn = _ROUTES.get(route_key)

    if route_fn is None:
        return _error(404, f"no route for '{route_key}'")

    try:
        return route_fn(event)
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"public_api_handler: unhandled exception on {route_key}: {exc!r}")
        return _error(500, "internal server error")
