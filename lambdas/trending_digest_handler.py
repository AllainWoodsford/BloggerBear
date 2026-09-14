"""Cross-topic "trending everywhere" digest Lambda (Phase 8 stretch).

Manually invocable (`event` is ignored) but designed to run on a static
daily EventBridge Scheduler schedule wired up directly to this function --
same "one global job, not per-topic" pattern as Phase 5's
weekly_reflection_handler.py, since this isn't topic-specific: there's
nothing per-topic to configure about a job that looks across every topic
at once.

Flow (docs/PROGRESS.md's Phase 8 "Cross-topic 'trending everywhere'
digest"):
  1. Load every Topic.
  2. For each, fetch its single most recent Finding, if any, and skip
     topics with nothing recent (per DIGEST_LOOKBACK_HOURS) -- a topic
     that hasn't had a material research-tick change lately has nothing
     to contribute to a "what's trending right now" digest.
  3. Ask Bedrock to synthesize one short digest surveying what's
     currently trending across every contributing topic, calling out any
     genuine cross-topic connection if one exists.
  4. Run the digest through the exact same compliance review as any other
     draft (docs/project-plan.md §2 hard constraint #3: "Drafts must pass
     compliance review before publish") -- routed to manual moderation
     unconditionally if any contributing topic is financial, same
     "regardless of confidence" rule any financial draft gets, since the
     digest may echo financial-topic content even though it isn't itself
     a financial-topic draft in the Topics-table sense.
  5. Publish (Articles "published", topic_id=DIGEST_TOPIC_ID) or route to
     ModerationQueue -- reusing put_article/put_moderation_item exactly
     like daily_cycle_handler.py's own publish-or-moderate step, so the
     digest shows up through the EXISTING public API/RSS/frontend
     (GET /articles?topic_id=digest, GET /rss.xml) with zero new routes.
     This is deliberately a small amount of duplicated logic with
     daily_cycle_handler.py's own _publish_or_moderate rather than a
     shared refactor -- that function's existing tests patch
     `daily_cycle_handler.put_article`/`put_moderation_item` directly, so
     extracting it into a shared module would silently stop those patches
     from intercepting the real call. Not worth the risk for ~25 lines.

Never raises unhandled -- like the other handlers in this codebase, runs
under a top-level try/except that logs the real exception and returns an
error dict, since a scheduled job has no one watching synchronously.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import boto3

from common import compliance
from common.bedrock import invoke_claude
from common.dynamo import get_latest_finding, list_topics, put_article, put_moderation_item

DIGEST_TOPIC_ID = "digest"
DIGEST_LOOKBACK_HOURS = 48

_DIGEST_PROMPT_TEMPLATE = """You are writing a short cross-topic "trending everywhere" digest for a \
research-and-publishing platform that independently tracks several unrelated domains.

Below is the most recent research finding for each topic currently showing activity:

{topic_blocks}

Write a concise digest (3-6 sentences) surveying what's currently trending across these domains. If \
anything meaningfully connects across more than one topic (a shared theme, technology, or event), call \
that out explicitly; otherwise just summarize the standout item from each topic in turn. Do not \
speculate beyond what's given, and do not give financial or investment advice.
"""

_DIGEST_FINANCIAL_GUIDANCE_HEADER = "Financial-topic guidance (mandatory):"


def handler(event, context) -> dict:
    try:
        return _run_trending_digest()
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"trending_digest_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}


def _run_trending_digest() -> dict:
    contributions = _recent_contributions()
    if not contributions:
        return {"status": "no_recent_findings"}

    # True if ANY contributing topic is financial -- the digest gets the
    # same defense-in-depth a financial-topic daily_cycle draft gets:
    # guidance folded into the synthesis prompt, a deterministically
    # appended disclaimer regardless of what the model actually wrote,
    # and (below) the same unconditional manual-moderation routing. See
    # daily_cycle_handler.py's own use of these two compliance helpers --
    # this must stay consistent with that, since the digest can echo
    # financial-topic content even though it isn't itself a Topics-table
    # financial topic.
    any_financial = any(compliance.is_financial_topic(c["topic"]) for c in contributions)

    model_id = os.environ["BEDROCK_MODEL_ID"]
    draft_text = _synthesize_digest(contributions, model_id, financial=any_financial)
    if any_financial:
        draft_text = compliance.append_financial_disclaimer(draft_text)
    title = f"Trending Everywhere -- {datetime.now(UTC).strftime('%Y-%m-%d')}"

    # A minimal synthetic "topic" -- review_draft only ever reads
    # is_financial off of it.
    review = compliance.review_draft(draft_text, {"is_financial": any_financial}, model_id)

    source_refs = []
    for contribution in contributions:
        source_refs.extend(contribution["finding"].get("source_refs") or [])

    return _publish_or_moderate_digest(
        title=title,
        draft_text=draft_text,
        source_refs=source_refs,
        review=review,
    )


def _recent_contributions() -> list[dict]:
    """Return [{"topic": Topic, "finding": Finding}, ...] for every topic
    whose most recent Finding falls within DIGEST_LOOKBACK_HOURS.

    A topic with no Finding yet, or whose latest Finding has aged out of
    the window, contributes nothing -- there's no "trending" signal to
    include from it right now.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=DIGEST_LOOKBACK_HOURS)).isoformat()

    contributions = []
    for topic in list_topics():
        finding = get_latest_finding(topic["topic_id"])
        if finding is None:
            continue
        if (finding.get("captured_at") or "") < cutoff:
            continue
        contributions.append({"topic": topic, "finding": finding})

    return contributions


def _synthesize_digest(contributions: list[dict], model_id: str, *, financial: bool) -> str:
    topic_blocks = "\n\n".join(
        f"## {c['topic'].get('name', c['topic']['topic_id'])}\n{c['finding'].get('summary', '')}"
        for c in contributions
    )
    prompt = _DIGEST_PROMPT_TEMPLATE.format(topic_blocks=topic_blocks)
    if financial:
        prompt += f"\n\n{_DIGEST_FINANCIAL_GUIDANCE_HEADER}\n{compliance.FINANCIAL_DRAFTING_GUIDANCE}"
    return invoke_claude(prompt, model_id)


def _publish_or_moderate_digest(
    *,
    title: str,
    draft_text: str,
    source_refs: list[dict],
    review: dict,
) -> dict:
    article_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    body_s3_key = f"articles/{article_id}.md"

    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=os.environ["CONTENT_BUCKET"],
        Key=body_s3_key,
        Body=draft_text.encode("utf-8"),
        ContentType="text/markdown",
    )

    compliant = review["compliant"]

    put_article(
        article_id=article_id,
        topic_id=DIGEST_TOPIC_ID,
        title=title,
        body_s3_key=body_s3_key,
        status="published" if compliant else "pending_moderation",
        created_at=now,
        published_at=now if compliant else None,
        source_refs=source_refs,
    )

    if compliant:
        return {"status": "published", "article_id": article_id, "compliant": True}

    put_moderation_item(
        queue_id=str(uuid.uuid4()),
        article_id=article_id,
        topic_id=DIGEST_TOPIC_ID,
        reasons=review["reasons"],
        created_at=now,
    )
    return {
        "status": "pending_moderation",
        "article_id": article_id,
        "compliant": False,
        "reasons": review["reasons"],
    }
