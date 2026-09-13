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
    get_topic,
    list_recent_findings,
    put_article,
    put_candidate_idea,
    put_moderation_item,
)

_NUM_CANDIDATE_ANGLES = 3
_LIST_MARKER_RE = re.compile(r"^[\s\d.\-\)]+")


def handler(event: dict, context) -> dict:
    topic_id = (event or {}).get("topic_id")
    if not topic_id:
        return {"status": "error", "error": "event missing required 'topic_id'"}

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

    angles = _ideate(topic, summaries_block, model_id)
    selected = _select_and_store_candidates(topic_id, angles)

    draft_text = _draft_article(topic, selected["angle"], summaries_block, model_id)
    title = _draft_title(selected["angle"], model_id)

    review = compliance.review_draft(draft_text, topic, model_id)

    return _publish_or_moderate(
        topic_id=topic_id,
        title=title,
        draft_text=draft_text,
        findings=findings,
        review=review,
    )


def _format_findings_summaries(findings: list[dict]) -> str:
    return "\n".join(f"- {finding.get('summary', '')}" for finding in findings)


def _ideate(topic: dict, summaries_block: str, model_id: str) -> list[str]:
    prompt = (
        f"Based on the following recent research findings about "
        f"'{topic.get('name', topic.get('topic_id', ''))}', propose exactly "
        f"{_NUM_CANDIDATE_ANGLES} distinct, specific candidate article angles. "
        "Reply with exactly one angle per line, no numbering, no extra "
        "commentary.\n\n"
        f"Findings:\n{summaries_block}"
    )
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


def _draft_article(topic: dict, angle: str, summaries_block: str, model_id: str) -> str:
    prompt = (
        "Write a full article draft in markdown (a few paragraphs) for a blog "
        f"about '{topic.get('name', topic.get('topic_id', ''))}', on this angle: "
        f"{angle}\n\nBase it on these recent findings:\n{summaries_block}"
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
    title: str,
    draft_text: str,
    findings: list[dict],
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
