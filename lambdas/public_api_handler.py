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

from common import equipment, feedback_limits, feedback_verification, gear, security_events, wear
from common.attribution import (
    clean_sources,
    sources_for_article,
    sources_for_topic,
    sources_for_topics,
)
from common.comment_screening import (
    INJECTION,
    MARKUP,
    MODEL_BUDGET,
    MODEL_ERROR,
    SHELL,
    SQL,
    screen_comment,
)
from common.digest import DIGEST_TOPIC_ID
from common.dynamo import (
    get_article,
    get_current_stats,
    get_latest_finding,
    get_stats_totals,
    get_topic,
    get_view_count,
    increment_view_count,
    list_all_articles,
    list_models,
    list_musings,
    list_pending_moderation_for_topic,
    list_prompt_refinements,
    list_published_articles,
    list_recent_findings,
    list_topics,
    put_feedback,
    update_article_net_votes,
)
from common.fact_check import fact_check_label
from common.source_refs import dedupe_source_refs
from common.static_pages import equipment_snapshot
from common.stats import build_stats
from common.stats_tracking import (
    HISTORIC_EXCLUDES_CURRENT_WEEK_NOTE,
    overall_view,
    public_view,
    record_feedback_given,
    record_feedback_rejected_comment,
)

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


# Scaling PR C: how long the public API's CDN (infra/modules/api-cdn) and browsers may reuse a
# response. The CDN caches nothing unless told to (its default TTL is 0), so only the routes below
# that pass cache_seconds are ever cached: the same answer for every visitor, where a minute or five
# of staleness is invisible. Everything else -- the view counter, feedback, feedback-status (it hands
# out a fresh verification token), every error -- is sent no-store, so neither the CDN nor a browser
# keeps it.
_LISTING_CACHE_SECONDS = 60
_RSS_CACHE_SECONDS = 300


def _response(
    status_code: int,
    payload,
    *,
    content_type: str = "application/json",
    cache_seconds: int | None = None,
) -> dict:
    body = json.dumps(payload) if content_type == "application/json" else payload
    headers = {"Content-Type": content_type, **_CORS_HEADERS}
    if cache_seconds is not None and status_code == 200:
        headers["Cache-Control"] = f"public, max-age={cache_seconds}"
    else:
        headers["Cache-Control"] = "no-store"
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

    # `attribution` is the topic's source credit (common/attribution.py): the sources its adapter
    # declares in code, as [{"text", "label", "url"}]. Only that: the adapter's name and its
    # adapter_config stay private, as before.
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
                "attribution": sources_for_topic(t),
            }
        )
    return _response(200, {"topics": public_topics}, cache_seconds=_LISTING_CACHE_SECONDS)


def _ref_key(ref: dict) -> str:
    return ref.get("url") or ref.get("title") or ""


def _researching_ref(latest: dict, previous: dict | None) -> dict | None:
    """The source to name in "Researching: ...": the first one in the newest finding that
    the finding before it did not have, else simply the first. Findings often lead with the
    same anchors (a crypto finding always starts Bitcoin, Ethereum; a trending list keeps
    its top repos), so the first source alone never changes even when the research does.
    Topic-agnostic: it only compares two findings' source lists."""
    refs = [r for r in dedupe_source_refs(latest.get("source_refs")) if r.get("title") or r.get("url")]
    known = {_ref_key(r) for r in dedupe_source_refs((previous or {}).get("source_refs"))}
    fresh = next((r for r in refs if _ref_key(r) not in known), None)
    return fresh or (refs[0] if refs else None)


def _last_checked_at(topic_id: str, latest_finding: dict) -> str | None:
    """When the topic's source was last successfully checked (a check that finds nothing new
    writes no finding, so the topic row is the only record), else when the newest finding was
    captured. Just a timestamp: nothing else from the topic is exposed."""
    try:
        topic = get_topic(topic_id) or {}
    except Exception as exc:  # noqa: BLE001 - a label must never break the page
        print(f"public_api_handler: could not read topic {topic_id} for its last check: {exc!r}")
        topic = {}
    return topic.get("last_research_at") or latest_finding.get("captured_at") or None


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

    recent_findings = list_recent_findings(topic_id, limit=2)
    latest_finding = recent_findings[0] if recent_findings else None
    if latest_finding is not None:
        researching = True
        research_ref = _researching_ref(
            latest_finding, recent_findings[1] if len(recent_findings) > 1 else None
        )
        researching_item = {
            "status": "researching",
            "label": "Researching",
            "title": (research_ref or {}).get("title") or (research_ref or {}).get("url") or "",
        }
        checked_at = _last_checked_at(topic_id, latest_finding)
        if checked_at:
            researching_item["checked_at"] = checked_at
        pipeline_items.append(researching_item)
    return _response(
        200,
        {
            "topic_id": topic_id,
            "researching": researching,
            "pending_review_count": pending_review_count,
            "pipeline_items": pipeline_items,
        },
        cache_seconds=_LISTING_CACHE_SECONDS,
    )


# --- Articles -------------------------------------------------------------


def _topic_attribution(topic_id: str) -> list[dict]:
    """The source credit shown under a topic's title: its adapter's declared sources. The digest
    is not a Topics row and draws on every topic, so its page credits all of their sources (each
    digest article carries the narrower credit of the topics that actually contributed)."""
    if topic_id == DIGEST_TOPIC_ID:
        return sources_for_topics(list_topics())
    return sources_for_topic(get_topic(topic_id))


def _article_attribution(article: dict) -> list[dict]:
    """The source credit an article was published with (or, for one from before that was stored,
    its topic's adapter's sources today). See common/attribution.py's sources_for_article."""
    return sources_for_article(article, get_topic=get_topic, list_topics=list_topics)


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
            # total_cost_aud (authoring + research) and whether a research tally exists at all --
            # see common/static_pages.py's _summary_cost_label for why the frontend needs both,
            # not just the number: cost_aud alone understates the true cost of most articles.
            "total_cost_aud": (a.get("lineage") or {}).get("total_cost_aud"),
            "has_research": "research" in (a.get("lineage") or {}),
            "model_labels": (a.get("lineage") or {}).get("model_labels"),
            "published_by": a.get("published_by"),
        }
        for a in articles
    ]
    return _response(
        200,
        # `attribution`: the topic's source credit, for the line under the topic page's title.
        {"topic_id": topic_id, "articles": summaries, "attribution": _topic_attribution(topic_id)},
        cache_seconds=_LISTING_CACHE_SECONDS,
    )


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
            "view_count": _view_count(article),
            # AI lineage/cost tracking (docs/project-plan.md §11, PR 3 of
            # 5) -- explicit None (not omitted) on an article published
            # before this feature existed, so the frontend's "no data"
            # detection has something concrete to check against.
            "lineage": article.get("lineage"),
            "published_by": article.get("published_by"),
            # A reader-facing line about the fresh-data review, or None. The review record
            # itself (claims, evidence) stays private; only this sentence is public.
            "fact_check": fact_check_label(article.get("review"), article.get("published_by")),
            # The gear (if any) this article was written with, resolved live -- see
            # common/static_pages.py's equipment_snapshot for why this is live rather than a
            # frozen-at-publish-time snapshot like the static article page's own copy.
            "equipment_used": equipment_snapshot(article.get("equipment_used")),
            # The source credit shown under the title: [{"text", "label", "url"}], nothing else.
            "attribution": _article_attribution(article),
        },
        # Its view_count can be a minute behind; the page shows the live count the view POST returns.
        cache_seconds=_LISTING_CACHE_SECONDS,
    )


def _view_count(article: dict) -> int:
    """An article's total views (sharded counters plus the count kept on the article before them,
    see common/dynamo.py's get_view_count). A counter that can't be read never breaks the article:
    the pre-sharding count is shown instead, and the next view's POST corrects it on the page."""
    stored = int(article.get("view_count", 0))
    try:
        return get_view_count(article["article_id"], stored_view_count=stored)
    except Exception as exc:  # noqa: BLE001 - a view count must never break the page
        print(f"public_api_handler: could not read view counters for {article['article_id']}: {exc!r}")
        return stored


def _view_article(event: dict) -> dict:
    article_id = _path_param(event, "article_id")
    article = _get_published_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")

    new_count = increment_view_count(
        article_id, stored_view_count=int(article.get("view_count", 0))
    )
    return _response(200, {"article_id": article_id, "view_count": new_count})


# --- Feedback -------------------------------------------------------------
#
# A comment is optional and is screened before anything is written (see
# common/comment_screening.py): PII, hate, rudeness, spam, links, prompt-injection or SQL/script
# shapes, and anything unlawful are REJECTED -- not stored, not redacted-and-stored, not logged,
# not echoed back, and the whole submission with it (the vote is not recorded and nothing is
# counted against the limits). The response is a bare 422, and nothing about why.


# Why feedback is closed -> the HTTP status of the refusal: a lock or pause is 423, a limit that
# reopens by itself is 429, the limiter being unreadable is 503.
_CLOSED_STATUS = {
    feedback_limits.ARTICLE_LOCKED: 423,
    feedback_limits.ARTICLE_LIMIT: 423,
    feedback_limits.LOCKDOWN: 423,
    feedback_limits.DAILY_LIMIT: 429,
    feedback_limits.RATE_LIMIT: 429,
    feedback_limits.UNAVAILABLE: 503,
}


# The form's decoy field. It should look like any other optional field: nothing in its name says
# what it is for, and a browser has no autofill for it (not "email", "phone", "name", "company"...).
HONEYPOT_FIELD = "referral_code"


# Comment-screening reasons that mean someone tried to attack the system, not just post a bad
# comment: recorded as security events as well as rejected.
_ATTACK_REASONS = frozenset({INJECTION, SQL, MARKUP, SHELL})
# Drops that say nothing about the comment: the day's model checks ran out, or the model failed.
# Every other drop counts toward the day's "feedback-drops" trend (common/security_events.py).
_NOT_THE_COMMENTS_DOING = frozenset({MODEL_BUDGET, MODEL_ERROR})


def _client_ip(event: dict) -> str:
    """The visitor's address. Behind the API's CloudFront distribution the connection comes from an
    edge, and the visitor's own address is in x-viewer-ip -- believed only alongside x-origin-verify,
    the header the distribution adds (the same rule the WAF's per-visitor limits follow)."""
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    if "x-origin-verify" in headers and headers.get("x-viewer-ip"):
        return str(headers["x-viewer-ip"])
    return str(((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp") or "")


def _closed_response(status: dict) -> dict:
    return _response(
        _CLOSED_STATUS.get(status["reason"], 423),
        {"error": "feedback is closed", "feedback": status},
    )


def _feedback_status(event: dict) -> dict:
    """Is feedback open for this article, and if not, why? The page hides its form and shows the
    reason instead. Read-only: it counts nothing."""
    article_id = _path_param(event, "article_id")
    article = _get_published_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")
    status = feedback_limits.status_for(article)
    if status["open"]:
        # Open: also hand out the one-use token this article's submission must carry, and how
        # long the browser must wait / how much work it must do (common/feedback_verification.py).
        try:
            verification = feedback_verification.issue(
                article_id, feedback_limits.current_settings()
            )
        except Exception as exc:  # noqa: BLE001 - no key, no token: closed, not open
            print(f"public_api_handler: could not issue a feedback token: {exc!r}")
            return _response(200, feedback_limits.unavailable_status())
        if verification is not None:
            status = {**status, "verification": verification}
    return _response(200, status)


def _submit_feedback(event: dict) -> dict:
    article_id = _path_param(event, "article_id")
    article = _get_published_article(article_id)
    if article is None:
        return _error(404, f"article '{article_id}' not found")

    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "invalid JSON body")
    if not isinstance(payload, dict):
        return _error(400, "the body must be a JSON object")

    vote = payload.get("vote")
    if vote not in ("up", "down"):
        return _error(400, "'vote' must be 'up' or 'down'")

    # 0. The honeypot: a field no person sees (hidden from sight and from screen readers, skipped
    #    by the keyboard). A script that fills in every input fills it in. It is told it worked
    #    and nothing is stored or counted, so it learns nothing.
    honeypot = payload.get(HONEYPOT_FIELD)
    if honeypot is not None and str(honeypot).strip():
        print("public_api_handler: rejected a feedback submission (honeypot)")
        return _response(
            201,
            {
                "status": "recorded",
                "article_id": article_id,
                "feedback_id": str(uuid.uuid4()),
                "comment_saved": False,
            },
        )

    # 1. Is feedback open at all? A read only: a closed site costs no model call and stores
    #    nothing (see common/feedback_limits.py), and says why whether or not there is a token.
    status = feedback_limits.status_for(article)
    if not status["open"]:
        return _closed_response(status)

    # 1b. The token from GET .../feedback-status, unused, old enough, with its proof of work if
    #     the site is busy. Checked before anything is screened or counted.
    try:
        settings = feedback_limits.current_settings()
    except Exception as exc:  # noqa: BLE001 - fail closed, like the limiter
        print(f"public_api_handler: could not read the feedback settings: {exc!r}")
        return _closed_response(feedback_limits.unavailable_status())
    verdict = feedback_verification.verify(
        article_id, payload.get("token"), payload.get("work"), settings
    )
    if not verdict["ok"]:
        return _response(
            503 if verdict["reason"] == feedback_verification.UNAVAILABLE else 403,
            {
                "error": "verification failed",
                "verification": {
                    "reason": verdict["reason"],
                    "retry_after_ms": verdict["retry_after_ms"],
                },
            },
        )

    # 2. Screen the comment. A rejected comment rejects the whole submission: nothing is stored,
    #    the vote is not recorded, and it is not counted against any limit, so rejected feedback
    #    cannot use up the room real feedback needs. (The model checks it costs are bounded by
    #    their own daily budget instead.) The reader is told nothing about why.
    screened = screen_comment(
        payload.get("comment"),
        article.get("title") or "",
        os.environ["BEDROCK_MODEL_ID"],
        may_call_model=feedback_limits.take_screening_slot,
    )
    final_comment = screened["comment"]
    if screened["dropped_because"]:
        # The reason code only: the comment itself is never logged. The whole submission is
        # thrown away -- the vote is not recorded either -- so this is what "feedback was
        # rejected" means for the Stats page, not a closed site or a caught bot (see
        # common/stats_tracking.py's FEEDBACK_REJECTED_COMMENT).
        print(f"public_api_handler: rejected a feedback submission ({screened['dropped_because']})")
        if screened["dropped_because"] in _ATTACK_REASONS:
            # An attack, not just an unwanted comment: a security event (common/security_events.py).
            # The reason and a hash of the address only -- the comment is never stored.
            security_events.record_incident(
                source=security_events.COMMENT_SCREENING,
                rule=screened["dropped_because"],
                client_ip=_client_ip(event),
                at=datetime.now(UTC),
                method="POST",
                path=f"/articles/{article_id}/feedback",
            )
        if screened["dropped_because"] not in _NOT_THE_COMMENTS_DOING:
            # Whatever the reason and whoever sent it: ten in a day opens a low incident, fifty
            # makes it medium, a hundred high (and alerts). A count only -- nothing of the comment.
            security_events.record_trend("feedback-drops", datetime.now(UTC))
        try:
            record_feedback_rejected_comment()
        except Exception as exc:  # noqa: BLE001 - the rejection itself must still be returned
            print(f"public_api_handler: could not record the rejected-feedback stat: {exc!r}")
        return _response(422, {"error": "comment not accepted", "recorded": False})

    # 3. Count it against the article, the day and the rate limit. Only now, once it is going to
    #    be kept.
    status = feedback_limits.acquire(article)
    if not status["open"]:
        return _closed_response(status)

    feedback_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    put_feedback(article_id, feedback_id, vote, final_comment, created_at)
    update_article_net_votes(article_id, 1 if vote == "up" else -1)
    # A tag only, never the comment, the vote or the article: a metric filter counts these for the
    # Lambda runs dashboard, next to the rejections above (infra/modules/observability).
    kept = "comment kept" if final_comment else "vote only"
    print(f"public_api_handler: accepted a feedback submission ({kept})")
    try:
        record_feedback_given()
    except Exception as exc:  # noqa: BLE001 - the feedback is already stored; never lose it over this
        print(f"public_api_handler: could not record the feedback-given stat: {exc!r}")

    # The gear this article was written with takes the feedback: a downvote wears it, an upvote
    # repairs it (common/wear.py). Never raises, and says nothing to the reader about it.
    wear.apply_feedback(article, vote)

    return _response(
        201,
        {
            "status": "recorded",
            "article_id": article_id,
            "feedback_id": feedback_id,
            "comment_saved": final_comment is not None,
        },
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
            # A loot drop carries a snapshot of the gear it announces (already the public view).
            **({"gear": m["gear"]} if m.get("gear") else {}),
        }
        for m in items
    ]
    return _response(200, {"musings": musings}, cache_seconds=_LISTING_CACHE_SECONDS)


# --- Stats ------------------------------------------------------------------

# Every hit scans the Articles/Topics/Models tables (fine at this project's
# scale, same as the RSS feed) -- a short public cache keeps a popular page
# from turning into a scan per view. The numbers move on the scale of
# articles-per-day, so five minutes of staleness is invisible.
_STATS_CACHE_SECONDS = 300


def _stats(event: dict) -> dict:
    """Aggregate AI cost/token statistics for the public Stats page --
    aggregates only (common/stats.py plus common/stats_tracking.py), never article content or
    ids. `weekly` (StatsCurrent) and `historic` (StatsHistory's all-time running total, PR 4 of
    the Observability enhancement) sit alongside the original per-article `by_model`/`by_topic`/
    `daily` breakdown -- both single get_item reads, no extra scan. `overall` is Total Stats' cost
    summary from the same two rows (common/stats_tracking.py's overall_view): money and counts."""
    stats = build_stats(list_all_articles(), list_topics(), list_models())
    current, totals = get_current_stats(), get_stats_totals()
    stats["weekly"] = public_view(current)
    stats["historic"] = {**public_view(totals), "note": HISTORIC_EXCLUDES_CURRENT_WEEK_NOTE}
    stats["overall"] = overall_view(totals, current)
    return _response(200, stats, cache_seconds=_STATS_CACHE_SECONDS)


# --- What BloggerBear is wearing ------------------------------------------------------------------

# Gear changes when feedback arrives or an admin equips something, not by the second.
_EQUIPMENT_CACHE_SECONDS = 60


def _equipment(event: dict) -> dict:
    """The gear BloggerBear is wearing, for the Stats page: each armor slot (or None), the rings, and how
    many things are in the backpack. Never what is in the backpack, and never anything about a proposal
    beyond what is shown on the gear itself (common/gear.py, public_view)."""
    topic_names = {t.get("topic_id"): t.get("name") for t in list_topics()}
    view = equipment.describe(
        list_prompt_refinements(status="approved"), decorate=lambda i: gear.public_view(i, topic_names)
    )
    loadout = {
        "armor": view["armor"],
        "rings": view["rings"],
        "max_rings": view["max_rings"],
        "backpack_count": view["backpack_count"],
    }
    return _response(200, loadout, cache_seconds=_EQUIPMENT_CACHE_SECONDS)


# --- RSS feed ---------------------------------------------------------------


def _rss_pub_date(published_at: str | None) -> str | None:
    if not published_at:
        return None
    try:
        parsed = datetime.fromisoformat(published_at)
    except ValueError:
        return None
    return format_datetime(parsed)


def _rss_credit_line(attribution: list[dict] | None) -> str:
    """The source credit as one plain-text line for a feed item, or "".

    The feed item's description is the opening of the article, which is exactly where a crypto
    article states its prices, and a feed reader never loads the page that carries the credit. So
    the credit travels with the excerpt: CoinGecko's guide asks for it "close to where the data is
    displayed". Plain text with the address written out, because a description's markup is not
    reliably rendered by feed readers; the caller XML-escapes it with the rest."""
    return " · ".join(f"{source['text']} ({source['url']})" for source in clean_sources(attribution))


def _rss_item_xml(article: dict, site_url: str) -> str:
    article_id = article["article_id"]
    title = article.get("title", "")
    body = article.get("body", "")
    description = body if len(body) <= _RSS_DESCRIPTION_MAX_CHARS else body[:_RSS_DESCRIPTION_MAX_CHARS]
    # Appended after the cut, so a long article can never truncate its own credit away.
    credit = _rss_credit_line(article.get("attribution"))
    if credit:
        description = f"{description}\n\n{credit}" if description else credit
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
    # One Topics scan for the whole feed, not a read per item: an article with no stored credit
    # falls back to its topic's adapter (common/attribution.py).
    topics = list_topics() if articles else []
    topics_by_id = {topic["topic_id"]: topic for topic in topics}

    items_xml = []
    for article in articles:
        attribution = sources_for_article(
            article, get_topic=topics_by_id.get, list_topics=lambda: topics
        )
        body = ""
        body_s3_key = article.get("body_s3_key")
        if body_s3_key:
            try:
                body = _read_body_from_s3(body_s3_key)
            except Exception as exc:  # noqa: BLE001 - one bad S3 object must not break the whole feed
                article_id = article.get("article_id")
                print(f"public_api_handler: failed to read body for RSS item {article_id}: {exc!r}")
        items_xml.append(_rss_item_xml({**article, "body": body, "attribution": attribution}, site_url))

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
    return _response(
        200, xml, content_type="application/rss+xml; charset=utf-8", cache_seconds=_RSS_CACHE_SECONDS
    )


_ROUTES = {
    "GET /topics": _list_topics,
    "GET /topics/{topic_id}/activity": _topic_activity,
    "GET /articles": _list_articles,
    "GET /articles/{article_id}": _get_article_detail,
    "POST /articles/{article_id}/view": _view_article,
    "GET /articles/{article_id}/feedback-status": _feedback_status,
    "POST /articles/{article_id}/feedback": _submit_feedback,
    "GET /musings": _list_musings,
    "GET /stats": _stats,
    "GET /equipment": _equipment,
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
