"""Daily authoring cycle Lambda handler.

Manually invoked per-topic (event: {"topic_id": str}). Iterating all topics
on a schedule is Phase 3 and is intentionally not built here.

Flow (see docs/project-plan.md §4 "Daily Authoring Cycle" and §5 data
model):
1. Load topic.
2. Load recent findings; bail out if there's nothing to write about yet.
3. Ideate: ask Bedrock for 3 candidate angles, store them as CandidateIdeas.
4. Select: deterministically pick the first candidate (no scoring model yet).
5. Draft: ask Bedrock for a title and a full article draft.
6. Compliance review (common.compliance.review_draft).
7. Publish (S3 + Articles "published") or route to ModerationQueue
   (Articles "pending_moderation" + a ModerationQueue item), depending on
   the compliance verdict.
"""

import os
import re
import uuid
from datetime import UTC, datetime

import boto3

from common import compliance
from common.bedrock import invoke_claude
from common.dynamo import (
    get_latest_approved_prompt_refinement,
    get_top_voted_articles,
    get_topic,
    list_recent_findings,
    put_article,
    put_candidate_idea,
    put_moderation_item,
)
from common.musings import generate_and_store_article_musing
from common.static_pages import render_and_publish_article_page

_NUM_CANDIDATE_ANGLES = 3
_LIST_MARKER_RE = re.compile(r"^[\s\d.\-\)]+")
_FEW_SHOT_EXCERPT_CHARS = 500
_FEEDBACK_GUIDANCE_HEADER = "Additional guidance based on reader feedback:"
_FINANCIAL_GUIDANCE_HEADER = "Financial-topic guidance (mandatory):"


def handler(event: dict, context) -> dict:
    topic_id = (event or {}).get("topic_id")
    if not topic_id:
        return {"status": "error", "error": "event missing required 'topic_id'"}

    print(f"daily_cycle_handler: starting run for topic_id={topic_id}")
    try:
        return _run_daily_cycle(topic_id)
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        return {"status": "error", "topic_id": topic_id, "error": str(exc)}


def _run_daily_cycle(topic_id: str) -> dict:
    topic = get_topic(topic_id)
    if topic is None:
        return {"status": "error", "topic_id": topic_id, "error": "topic not found"}

    findings = list_recent_findings(topic_id)
    if not findings:
        return {"status": "no_findings", "topic_id": topic_id}

    model_id = os.environ["BEDROCK_MODEL_ID"]
    summaries_block = _format_findings_summaries(findings)

    # Phase 5: fold in any admin-approved prompt refinement and a few-shot
    # excerpt from the topic's best-received past article, if either exists.
    # Both are strictly additive -- when neither exists, the prompts below
    # are built exactly as they were before Phase 5.
    guidance = _get_approved_guidance(topic_id)
    few_shot_excerpt = _get_few_shot_excerpt(topic_id)

    angles = _ideate(topic, summaries_block, model_id, guidance=guidance)
    selected = _select_and_store_candidates(topic_id, angles)

    draft_text = _draft_article(
        topic,
        selected["angle"],
        summaries_block,
        model_id,
        guidance=guidance,
        few_shot_excerpt=few_shot_excerpt,
    )
    title = _draft_title(selected["angle"], model_id)

    # Phase 7: deterministically guarantee the standing "not financial
    # advice" disclaimer on every financial-topic draft, regardless of
    # whether the model actually followed the guidance folded into the
    # prompts above -- see common/compliance.py's append_financial_disclaimer.
    if compliance.is_financial_topic(topic):
        draft_text = compliance.append_financial_disclaimer(draft_text)

    review = compliance.review_draft(draft_text, topic, model_id)

    return _publish_or_moderate(
        topic_id=topic_id,
        topic_name=topic.get("name", topic_id),
        title=title,
        draft_text=draft_text,
        findings=findings,
        review=review,
        model_id=model_id,
    )


def _format_findings_summaries(findings: list[dict]) -> str:
    return "\n".join(f"- {finding.get('summary', '')}" for finding in findings)


def _get_approved_guidance(topic_id: str) -> str | None:
    """Return the latest approved PromptRefinements' guidance text, or None.

    None means no refinement has ever been approved for this topic --
    callers must leave their prompts completely unchanged in that case.
    """
    refinement = get_latest_approved_prompt_refinement(topic_id)
    if refinement is None:
        return None
    return refinement.get("prompt_changes") or None


def _get_few_shot_excerpt(topic_id: str) -> str | None:
    """Return a short excerpt of the topic's top-voted past article, or None.

    None means there's no positively-received article yet for this topic
    (e.g. a brand new topic) -- callers must leave their prompts completely
    unchanged in that case.
    """
    top_articles = get_top_voted_articles(topic_id, limit=1)
    if not top_articles:
        return None

    body_s3_key = top_articles[0].get("body_s3_key")
    if not body_s3_key:
        return None

    s3 = boto3.client("s3")
    response = s3.get_object(Bucket=os.environ["CONTENT_BUCKET"], Key=body_s3_key)
    body = response["Body"].read().decode("utf-8")
    return body[:_FEW_SHOT_EXCERPT_CHARS] or None


def _ideate(
    topic: dict, summaries_block: str, model_id: str, guidance: str | None = None
) -> list[str]:
    prompt = (
        f"Based on the following recent research findings about "
        f"'{topic.get('name', topic.get('topic_id', ''))}', propose exactly "
        f"{_NUM_CANDIDATE_ANGLES} distinct, specific candidate article angles. "
        "Reply with exactly one angle per line, no numbering, no extra "
        "commentary.\n\n"
        f"Findings:\n{summaries_block}"
    )
    if guidance:
        prompt += f"\n\n{_FEEDBACK_GUIDANCE_HEADER}\n{guidance}"
    if compliance.is_financial_topic(topic):
        prompt += f"\n\n{_FINANCIAL_GUIDANCE_HEADER}\n{compliance.FINANCIAL_DRAFTING_GUIDANCE}"
    response = invoke_claude(prompt, model_id)

    angles = []
    for line in response.strip().splitlines():
        cleaned = _LIST_MARKER_RE.sub("", line).strip()
        if cleaned:
            angles.append(cleaned)

    if not angles:
        # Defensive fallback: never proceed with zero angles even if the
        # model's response didn't parse as expected.
        angles = [response.strip() or "untitled angle"]

    return angles[:_NUM_CANDIDATE_ANGLES]


def _select_and_store_candidates(topic_id: str, angles: list[str]) -> dict:
    candidates = [
        put_candidate_idea(topic_id, datetime.now(UTC).isoformat(), angle, status="considered")
        for angle in angles
    ]

    # Phase 1's manual pipeline deterministically picks the first angle --
    # no scoring model yet (later refinement per project-plan.md phases 5/8).
    selected = candidates[0]
    return put_candidate_idea(
        topic_id, selected["created_at"], selected["angle"], status="selected"
    )


def _draft_article(
    topic: dict,
    angle: str,
    summaries_block: str,
    model_id: str,
    guidance: str | None = None,
    few_shot_excerpt: str | None = None,
) -> str:
    prompt = (
        "Write a full article draft in markdown (a few paragraphs) for a blog "
        f"about '{topic.get('name', topic.get('topic_id', ''))}', on this angle: "
        f"{angle}\n\nBase it on these recent findings:\n{summaries_block}"
    )
    if guidance:
        prompt += f"\n\n{_FEEDBACK_GUIDANCE_HEADER}\n{guidance}"
    if compliance.is_financial_topic(topic):
        prompt += f"\n\n{_FINANCIAL_GUIDANCE_HEADER}\n{compliance.FINANCIAL_DRAFTING_GUIDANCE}"
    if few_shot_excerpt:
        prompt += (
            "\n\nHere is an excerpt from a well-received past article on this "
            "topic, for style reference only (do not repeat its content):\n"
            f"{few_shot_excerpt}"
        )
    return invoke_claude(prompt, model_id)


def _draft_title(angle: str, model_id: str) -> str:
    prompt = (
        "Write a short, engaging article title (no surrounding quotes, no "
        f"markdown) for an article with this angle: {angle}"
    )
    return invoke_claude(prompt, model_id).strip()


def _publish_or_moderate(
    *,
    topic_id: str,
    topic_name: str,
    title: str,
    draft_text: str,
    findings: list[dict],
    review: dict,
    model_id: str,
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

    source_refs = []
    for finding in findings:
        source_refs.extend(finding.get("source_refs") or [])

    compliant = review["compliant"]

    put_article(
        article_id=article_id,
        topic_id=topic_id,
        title=title,
        body_s3_key=body_s3_key,
        status="published" if compliant else "pending_moderation",
        created_at=now,
        published_at=now if compliant else None,
        source_refs=source_refs,
    )

    if compliant:
        # Static article publishing (docs/project-plan.md §11): render the
        # public-facing static page the moment this article actually
        # becomes published -- not before, since a pending_moderation
        # article isn't public yet and shouldn't have a live page.
        render_and_publish_article_page(
            article_id=article_id,
            title=title,
            body_markdown=draft_text,
            topic_name=topic_name,
            published_at=now,
            source_refs=source_refs,
            view_count=0,
        )
        generate_and_store_article_musing(
            article_id=article_id,
            topic_id=topic_id,
            topic_name=topic_name,
            title=title,
            compliant=True,
            model_id=model_id,
        )
        return {
            "status": "published",
            "topic_id": topic_id,
            "article_id": article_id,
            "compliant": True,
        }

    put_moderation_item(
        queue_id=str(uuid.uuid4()),
        article_id=article_id,
        topic_id=topic_id,
        reasons=review["reasons"],
        created_at=now,
    )
    return {
        "status": "pending_moderation",
        "topic_id": topic_id,
        "article_id": article_id,
        "compliant": False,
        "reasons": review["reasons"],
    }
