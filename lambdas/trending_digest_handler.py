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
     On the compliant branch, also renders the static article page
     (docs/project-plan.md §11) and generates a musing, same as
     daily_cycle_handler.py's own compliant branch -- this is a fourth
     publish path alongside that one and admin_api_handler.py's
     moderation-approve/force-publish routes, and was missed when both
     features were first built (confirmed the hard way: a digest article
     published cleanly here never got a static page or a musing).

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
from common.bedrock import invoke_model_tracked
from common.costing import build_lineage
from common.digest import DIGEST_TOPIC_ID, DIGEST_TOPIC_NAME
from common.dynamo import get_latest_finding, list_topics, put_article, put_moderation_item
from common.model_routing import resolve_model
from common.musings import generate_and_store_article_musing
from common.source_refs import dedupe_source_refs
from common.static_pages import render_and_publish_article_page

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

    # No single topic drives the digest -- resolve_model(None) uses the
    # global default (ModelConfig's "default" row, or BEDROCK_MODEL_ID),
    # same precedence chain as any per-topic call minus the topic-override
    # step (docs/project-plan.md §11, PR 1 of 5).
    model_id, fallback_model_id = resolve_model(None)
    draft_text, synthesis_call = _synthesize_digest(
        contributions, model_id, fallback_model_id, financial=any_financial
    )
    if any_financial:
        draft_text = compliance.append_financial_disclaimer(draft_text)
    title = f"Trending Everywhere -- {datetime.now(UTC).strftime('%Y-%m-%d')}"

    # A minimal synthetic "topic" -- review_draft only ever reads
    # is_financial off of it.
    review = compliance.review_draft(
        draft_text, {"is_financial": any_financial}, model_id, fallback_model_id=fallback_model_id
    )

    source_refs = []
    for contribution in contributions:
        source_refs.extend(contribution["finding"].get("source_refs") or [])
    source_refs = dedupe_source_refs(source_refs)

    # AI lineage/cost tracking (docs/project-plan.md §11, PR 2 of 5) --
    # same pattern as daily_cycle_handler.py's own lineage assembly.
    calls = [call for call in (synthesis_call, review["lineage_call"]) if call is not None]
    lineage = build_lineage(calls)

    return _publish_or_moderate_digest(
        title=title,
        draft_text=draft_text,
        source_refs=source_refs,
        review=review,
        model_id=model_id,
        lineage=lineage,
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


def _synthesize_digest(
    contributions: list[dict], model_id: str, fallback_model_id: str | None, *, financial: bool
) -> tuple[str, dict]:
    topic_blocks = "\n\n".join(
        f"## {c['topic'].get('name', c['topic']['topic_id'])}\n{c['finding'].get('summary', '')}"
        for c in contributions
    )
    prompt = _DIGEST_PROMPT_TEMPLATE.format(topic_blocks=topic_blocks)
    if financial:
        prompt += f"\n\n{_DIGEST_FINANCIAL_GUIDANCE_HEADER}\n{compliance.FINANCIAL_DRAFTING_GUIDANCE}"
    result = invoke_model_tracked(prompt, model_id, fallback_model_id=fallback_model_id)
    lineage_call = {
        "stage": "draft",
        "model_id": result["model_id"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "used_fallback": result["used_fallback"],
    }
    return result["text"], lineage_call


def _publish_or_moderate_digest(
    *,
    title: str,
    draft_text: str,
    source_refs: list[dict],
    review: dict,
    model_id: str,
    lineage: dict,
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
        lineage=lineage,
        published_by="ai_only" if compliant else None,
    )

    if compliant:
        # Bugfix: this handler predates both static article publishing
        # (docs/project-plan.md §11) and the Musings feature -- neither
        # ever got wired in here, even though this is a fourth publish
        # path alongside daily_cycle_handler's compliant branch and
        # admin_api_handler's moderation-approve/force-publish routes.
        # Confirmed the hard way: a digest article published cleanly here
        # never got a static page or a musing, silently diverging from
        # every other publish path. compliant=True here for the same
        # reason daily_cycle_handler's own compliant branch passes it --
        # this published on the first pass, no moderation needed.
        render_and_publish_article_page(
            article_id=article_id,
            title=title,
            body_markdown=draft_text,
            topic_name=DIGEST_TOPIC_NAME,
            published_at=now,
            source_refs=source_refs,
            view_count=0,
            lineage=lineage,
            published_by="ai_only",
        )
        generate_and_store_article_musing(
            article_id=article_id,
            topic_id=DIGEST_TOPIC_ID,
            topic_name=DIGEST_TOPIC_NAME,
            title=title,
            compliant=True,
            model_id=model_id,
        )
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
