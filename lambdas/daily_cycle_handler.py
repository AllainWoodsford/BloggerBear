"""Daily authoring cycle Lambda handler.

Manually invoked per-topic (event: {"topic_id": str}). Iterating all topics
on a schedule is Phase 3 and is intentionally not built here.

Flow (see docs/project-plan.md §4 "Daily Authoring Cycle" and §5 data
model):
1. Load topic.
2. Load every finding since the topic's previous article, at most
   FINDINGS_WINDOW_HOURS back (the whole window the research loop covered, not
   just the newest few); bail out if there's nothing new to write about.
   Topics with a daily editorial goal (the crypto feed -- see
   common/editorial_goals.py) resolve today's goal here and keep only the
   findings that belong to it; its mandate and article style are folded into
   steps 3 and 5. Every other topic is unaffected.
3. Ideate: ask Bedrock for 3 candidate angles, store them as CandidateIdeas.
   Ideation and drafting (step 5) both carry a relevance guardrail keyed to
   the active topic's name (common/relevance.py) and the topic's standing
   editorial goal (topic -> adapter -> global default; see
   common/editorial_resolver.py), so off-topic noise in the
   findings is ignored or reframed rather than written up.
4. Select: deterministically pick the first candidate (no scoring model yet).
5. Draft: ask Bedrock for a title and a full article draft.
6. Fresh-data review (common.fresh_review): compare the draft's claims with what
   the source says now. Shadow mode -- recorded on the article (and its moderation
   item), never acted on; `pipeline-config set --review-mode off` skips it.
7. Compliance review (common.compliance.review_draft).
8. Publish (S3 + Articles "published") or route to ModerationQueue
   (Articles "pending_moderation" + a ModerationQueue item), depending on
   the compliance verdict.
"""

import json
import os
import re
import uuid
from datetime import UTC, date, datetime, timedelta

import boto3

from common import compliance, fresh_review
from common.bedrock import invoke_model_tracked
from common.costing import build_lineage, build_research_lineage
from common.dynamo import (
    get_latest_approved_prompt_refinement,
    get_pipeline_config,
    get_top_voted_articles,
    get_topic,
    list_recent_findings,
    put_article,
    put_candidate_idea,
    put_moderation_item,
    set_topic_last_article_at,
)
from common.editorial_goals import (
    ARTICLE_STYLES,
    EDITORIAL_MANDATES,
    EditorialGoal,
    resolve_goal_for_topic,
)
from common.editorial_resolver import (
    DRAFT_ALIGNMENT_DIRECTIVE,
    MANDATE_ALIGNMENT_RULE,
    resolve_editorial_goals,
)
from common.model_routing import resolve_model
from common.musings import generate_and_store_article_musing
from common.relevance import (
    draft_relevance_boundary,
    ideation_relevance_rule,
    topic_label,
)
from common.source_refs import dedupe_source_refs
from common.static_pages import render_and_publish_article_page

# An article is written from everything the research loop found since the last
# daily run, not from the few newest findings: the window is the day the run
# covers, the count is a safety cap, and the character budget keeps a busy
# topic's prompt bounded (the oldest findings drop out first).
FINDINGS_WINDOW_HOURS = 24
MAX_WINDOW_FINDINGS = 48
SUMMARIES_MAX_CHARS = 40_000
# An article draft is a few paragraphs to a few pages; the 1,024-token default cut real
# drafts off mid-word (a stored body ended "...deep institutional liqu"). Room for a
# long draft, and one automatic retry with more if a reply still runs out.
DRAFT_MAX_TOKENS = 4096
DRAFT_RETRY_MAX_TOKENS = 8192
TRUNCATED_DRAFT_REASON = (
    "draft truncated: the model ran out of output tokens before finishing the article"
)
_NUM_CANDIDATE_ANGLES = 3
_LIST_MARKER_RE = re.compile(r"^[\s\d.\-\)]+")
_FEW_SHOT_EXCERPT_CHARS = 500
_FEEDBACK_GUIDANCE_HEADER = "Additional guidance based on reader feedback:"
_FINANCIAL_GUIDANCE_HEADER = "Financial-topic guidance (mandatory):"


def handler(event: dict, context) -> dict:
    topic_id = (event or {}).get("topic_id")
    if not topic_id:
        return {"status": "error", "error": "event missing required 'topic_id'"}

    # Only an operator's manual trigger ever sets this; the schedule never does.
    force = (event or {}).get("force") is True

    print(f"daily_cycle_handler: starting run for topic_id={topic_id} force={force}")
    try:
        return _run_daily_cycle(topic_id, force=force)
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        return {"status": "error", "topic_id": topic_id, "error": str(exc)}


def _window_start(topic: dict, run_started: datetime, force: bool) -> str:
    """ISO timestamp findings must be captured at or after to feed this article.

    The window is the last FINDINGS_WINDOW_HOURS, but never reaches back past the
    topic's previous article (`last_article_at`), so a second run with nothing
    found since the last one has nothing to write about instead of rewriting the
    same findings. `force` ignores the previous article (an operator asking for
    a fresh take on the whole window).
    """
    floor = run_started - timedelta(hours=FINDINGS_WINDOW_HOURS)
    last_article_at = None if force else topic.get("last_article_at")
    if last_article_at:
        try:
            previous = datetime.fromisoformat(last_article_at)
        except (TypeError, ValueError):
            previous = None
        if previous is not None:
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)  # stored in UTC
            return max(floor, previous).isoformat()
    return floor.isoformat()


def _record_article_written(topic_id: str, run_started: datetime) -> None:
    """Remember when this run began, so the next window starts there.

    The run's *start* (not its end) is stored: a finding captured while the article
    was being written belongs to the next one. Never raises -- the article already
    exists by now, and an error here would make Step Functions retry the whole run
    and write a duplicate.
    """
    try:
        set_topic_last_article_at(topic_id, run_started.isoformat())
    except Exception as exc:  # noqa: BLE001
        print(f"daily_cycle_handler: could not record last_article_at for {topic_id}: {exc!r}")


def _run_daily_cycle(topic_id: str, force: bool = False) -> dict:
    topic = get_topic(topic_id)
    if topic is None:
        return {"status": "error", "topic_id": topic_id, "error": "topic not found"}

    run_started = datetime.now(UTC)
    since = _window_start(topic, run_started, force)
    findings = list_recent_findings(topic_id, limit=MAX_WINDOW_FINDINGS, since=since)
    if not findings:
        # Nothing new since the topic's last article (or in the last day): better
        # no article than one rewritten from findings a previous run already used.
        return {"status": "no_findings", "topic_id": topic_id}

    # Topics with a daily editorial goal (the crypto feed) get the day's
    # goal and only the findings that belong to it; every other topic gets
    # (None, findings) back unchanged.
    #
    # The research tally is taken from the whole window *before* that selection:
    # a Finding the goal filter sets aside still cost a Bedrock call, and each
    # window starts where the last one ended, so nothing is counted twice.
    research = build_research_lineage(findings)
    window_findings = findings
    editorial_goal, findings = _select_goal_and_findings(topic, findings)
    if editorial_goal is not None:
        print(f"daily_cycle_handler: editorial_goal={editorial_goal.value} topic_id={topic_id}")

    model_id, fallback_model_id = resolve_model(topic)
    summaries_block = _format_findings_summaries(findings)

    # Phase 5: fold in any admin-approved prompt refinement and a few-shot
    # excerpt from the topic's best-received past article, if either exists.
    # Both are strictly additive -- when neither exists, the prompts below
    # are built exactly as they were before Phase 5.
    guidance = _get_approved_guidance(topic_id)
    few_shot_excerpt = _get_few_shot_excerpt(topic_id)

    angles, ideate_call = _ideate(
        topic, summaries_block, model_id, fallback_model_id, guidance=guidance, goal=editorial_goal
    )
    selected = _select_and_store_candidates(topic_id, angles)

    draft_text, draft_call = _draft_article(
        topic,
        selected["angle"],
        summaries_block,
        model_id,
        fallback_model_id,
        guidance=guidance,
        few_shot_excerpt=few_shot_excerpt,
        goal=editorial_goal,
    )
    title, title_call = _draft_title(selected["angle"], model_id, fallback_model_id)

    # A draft that ran out of tokens even after the retry is half an article: never
    # publish it. It goes to a human, whatever the compliance review says.
    hold_reasons = [TRUNCATED_DRAFT_REASON] if draft_call.get("stop_reason") == "max_tokens" else []
    if hold_reasons:
        print(f"daily_cycle_handler: holding a truncated draft for topic_id={topic_id}")

    # Fresh-data review (docs/project-plan.md §11, "(C)"): compare the draft's claims with
    # what the source says *now*. Shadow mode: recorded, never acted on. Placed before the
    # disclaimer is appended so it reviews the article's own text.
    fresh_record, fresh_call = _run_fresh_review(
        topic, draft_text, summaries_block, window_findings, model_id, fallback_model_id
    )

    # Phase 7: deterministically guarantee the standing "not financial
    # advice" disclaimer on every financial-topic draft, regardless of
    # whether the model actually followed the guidance folded into the
    # prompts above -- see common/compliance.py's append_financial_disclaimer.
    if compliance.is_financial_topic(topic):
        draft_text = compliance.append_financial_disclaimer(draft_text)

    review = compliance.review_draft(
        draft_text, topic, model_id, fallback_model_id=fallback_model_id
    )

    # AI lineage/cost tracking (docs/project-plan.md §11, PR 2 of 5): every
    # Bedrock call that actually contributed to this article, not just the
    # final draft -- review["lineage_call"] is None on the financial-topic
    # early-return path (no call was made there), filtered out below rather
    # than fabricated.
    calls = [
        call
        for call in (ideate_call, draft_call, title_call, fresh_call, review["lineage_call"])
        if call is not None
    ]
    lineage = build_lineage(calls, research=research)

    result = _publish_or_moderate(
        topic_id=topic_id,
        topic_name=topic.get("name", topic_id),
        title=title,
        draft_text=draft_text,
        findings=findings,
        review=review,
        model_id=model_id,
        lineage=lineage,
        fresh_review_record=fresh_record,
        hold_reasons=hold_reasons,
    )
    _record_article_written(topic_id, run_started)
    return result


def _load_latest_snapshot(findings: list[dict]) -> dict | None:
    """The newest finding's stored snapshot (findings are newest first), or None if it
    can't be read -- an adapter then re-checks without knowing what it looked at."""
    key = (findings[0].get("raw_snapshot_s3_key") if findings else None) or None
    if not key:
        return None
    try:
        body = boto3.client("s3").get_object(Bucket=os.environ["CONTENT_BUCKET"], Key=key)["Body"]
        return json.loads(body.read())
    except Exception as exc:  # noqa: BLE001
        print(f"daily_cycle_handler: could not load the latest snapshot for the review: {exc!r}")
        return None


def _run_fresh_review(
    topic: dict,
    draft_text: str,
    summaries_block: str,
    window_findings: list[dict],
    model_id: str,
    fallback_model_id: str | None,
) -> tuple[dict | None, dict | None]:
    """(review record, its lineage call), or (None, None) when the review is off.

    Never raises and never changes what happens to the article: the whole step is
    wrapped so that nothing in it can fail a daily run, and in this (shadow) version
    its result is only recorded.
    """
    try:
        try:
            mode = fresh_review.resolve_review_mode(get_pipeline_config())
        except Exception as exc:  # noqa: BLE001 - a config read must not stop the article
            print(f"daily_cycle_handler: could not read the pipeline config, review mode default: {exc!r}")
            mode = fresh_review.DEFAULT_REVIEW_MODE
        if mode == "off":
            return None, None

        record = fresh_review.run_review(
            topic=topic,
            draft=draft_text,
            findings_text=summaries_block,
            latest_state=_load_latest_snapshot(window_findings),
            model_id=model_id,
            fallback_model_id=fallback_model_id,
            mode=mode,
        )
        lineage_call = record.pop("lineage_call", None)
        print(
            f"daily_cycle_handler: fresh-data review status={record['status']} "
            f"outcome={record.get('outcome')} topic_id={topic.get('topic_id')}"
        )
        return record, lineage_call
    except Exception as exc:  # noqa: BLE001
        print(f"daily_cycle_handler: fresh-data review failed: {exc!r}")
        return {"status": "unavailable", "reason": f"review step failed: {exc}", "mode": "shadow"}, None


def _review_notes_kwargs(record: dict | None) -> dict:
    notes = fresh_review.review_notes(record)
    return {"review_notes": notes} if notes else {}


def _captured_date(finding: dict) -> date | None:
    """The UTC date a Finding was captured on, or None if unparseable."""
    try:
        captured = datetime.fromisoformat(finding.get("captured_at") or "")
    except ValueError:
        return None
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)  # findings are stored in UTC
    return captured.astimezone(UTC).date()


def _select_goal_and_findings(
    topic: dict, findings: list[dict]
) -> tuple[EditorialGoal | None, list[dict]]:
    """Resolve the topic's editorial goal and keep only the findings it applies to.

    The goal is a function of the UTC date, and the research tick fetches the
    data a goal needs using that same date, so findings captured *today* are
    exactly the ones matching *today's* goal. If nothing has been captured
    today (a failed tick, or a quiet source), the goal follows the newest
    finding's own day instead -- so an article is never written with, say, a
    news-digest mandate over altcoin price data. Topics without a goal are
    returned unchanged.
    """
    today = datetime.now(UTC).date()
    goal = resolve_goal_for_topic(topic, today)
    if goal is None:
        return None, findings

    todays = [f for f in findings if _captured_date(f) == today]
    if todays:
        return goal, todays

    newest_day = _captured_date(findings[0])
    if newest_day is None:
        return goal, findings
    same_day = [f for f in findings if _captured_date(f) == newest_day]
    return resolve_goal_for_topic(topic, newest_day), same_day


def _format_findings_summaries(findings: list[dict]) -> str:
    """One bullet per finding, newest first, stopping at SUMMARIES_MAX_CHARS so
    the oldest of a very busy window are the ones dropped."""
    lines: list[str] = []
    used = 0
    for finding in findings:
        line = f"- {finding.get('summary', '')}"
        if lines and used + len(line) + 1 > SUMMARIES_MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


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
    topic: dict,
    summaries_block: str,
    model_id: str,
    fallback_model_id: str | None,
    guidance: str | None = None,
    goal: EditorialGoal | None = None,
) -> tuple[list[str], dict]:
    topic_name = topic_label(topic)
    # The relevance rule sits before the data in both variants: findings from
    # community feeds and web search can be off-topic noise, and it is the
    # active topic's name (never a hardcoded topic) that anchors what counts
    # as on-topic.
    # The topic's standing editorial goal (topic -> adapter -> global default,
    # see common/editorial_resolver.py) comes first, and the relevance rule
    # then holds every angle to both the topic and that goal.
    standing_goal = (
        f"ACTIVE EDITORIAL MANDATE:\n{resolve_editorial_goals(topic, goal)}\n\n"
        f"{ideation_relevance_rule(topic_name)}\n"
        f"{MANDATE_ALIGNMENT_RULE}"
    )
    if goal is None:
        prompt = (
            f"Based on the following recent research findings about "
            f"'{topic_name}', propose exactly "
            f"{_NUM_CANDIDATE_ANGLES} distinct, specific candidate article angles. "
            "Reply with exactly one angle per line, no numbering, no extra "
            "commentary.\n\n"
            f"{standing_goal}\n\n"
            f"Findings:\n{summaries_block}"
        )
    else:
        # The day's rotating editorial mandate shapes the angles within the
        # standing goal above; the relevance rule keeps it inside the topic.
        # The one-angle-per-line reply instruction stays -- the parser below
        # depends on it.
        prompt = (
            "Based on the following recent research findings and the assigned daily "
            f"editorial vector for '{topic_name}', propose exactly "
            f"{_NUM_CANDIDATE_ANGLES} distinct, specific candidate article angles. "
            "Reply with exactly one angle per line, no numbering, no extra "
            "commentary.\n\n"
            f"{standing_goal}\n\n"
            f"Editorial Mandate: {EDITORIAL_MANDATES[goal]}\n"
            f"Apply this mandate strictly within the theme of '{topic_name}' and the Active "
            "Editorial Mandate above: it decides the shape of each angle, never the subject.\n\n"
            f"Data Payload:\n{summaries_block}"
        )
    if guidance:
        prompt += f"\n\n{_FEEDBACK_GUIDANCE_HEADER}\n{guidance}"
    if compliance.is_financial_topic(topic):
        prompt += f"\n\n{_FINANCIAL_GUIDANCE_HEADER}\n{compliance.FINANCIAL_DRAFTING_GUIDANCE}"
    result = invoke_model_tracked(prompt, model_id, fallback_model_id=fallback_model_id)
    response = result["text"]

    angles = []
    for line in response.strip().splitlines():
        cleaned = _LIST_MARKER_RE.sub("", line).strip()
        if cleaned:
            angles.append(cleaned)

    if not angles:
        # Defensive fallback: never proceed with zero angles even if the
        # model's response didn't parse as expected.
        angles = [response.strip() or "untitled angle"]

    lineage_call = {
        "stage": "ideation",
        "model_id": result["model_id"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "used_fallback": result["used_fallback"],
    }
    return angles[:_NUM_CANDIDATE_ANGLES], lineage_call


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
    fallback_model_id: str | None,
    guidance: str | None = None,
    few_shot_excerpt: str | None = None,
    goal: EditorialGoal | None = None,
) -> tuple[str, dict]:
    topic_name = topic_label(topic)
    # The boundary backs up the ideation rule: an imperfect angle can still
    # slip through, and the draft must not follow noisy data off-topic.
    prompt = (
        "Write a full article draft in markdown (a few paragraphs) for a blog "
        f"about '{topic_name}', on this angle: {angle}\n\n"
        f"CORE EDITORIAL DIRECTION:\n{resolve_editorial_goals(topic, goal)}\n\n"
        f"Base it on these recent findings:\n{summaries_block}"
        f"\n\n{draft_relevance_boundary(topic_name)}"
        f"\n\n{DRAFT_ALIGNMENT_DIRECTIVE}"
    )
    if goal is not None:
        prompt += f"\n\nArticle style: {ARTICLE_STYLES[goal]}"
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
    result = invoke_model_tracked(
        prompt,
        model_id,
        fallback_model_id=fallback_model_id,
        max_tokens=DRAFT_MAX_TOKENS,
        retry_max_tokens=DRAFT_RETRY_MAX_TOKENS,
    )
    lineage_call = {
        "stage": "draft",
        "model_id": result["model_id"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "used_fallback": result["used_fallback"],
        # "max_tokens" here means the draft is cut off even after the retry.
        "stop_reason": result.get("stop_reason"),
    }
    if result.get("attempts", 1) > 1:
        lineage_call["attempts"] = result["attempts"]
    return result["text"], lineage_call


def _draft_title(angle: str, model_id: str, fallback_model_id: str | None) -> tuple[str, dict]:
    prompt = (
        "Write a short, engaging article title (no surrounding quotes, no "
        f"markdown) for an article with this angle: {angle}"
    )
    result = invoke_model_tracked(prompt, model_id, fallback_model_id=fallback_model_id)
    lineage_call = {
        "stage": "title",
        "model_id": result["model_id"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "used_fallback": result["used_fallback"],
    }
    return result["text"].strip(), lineage_call


def _publish_or_moderate(
    *,
    topic_id: str,
    topic_name: str,
    title: str,
    draft_text: str,
    findings: list[dict],
    review: dict,
    model_id: str,
    lineage: dict,
    fresh_review_record: dict | None = None,
    hold_reasons: list[str] | None = None,
) -> dict:
    """Store the article and either publish it or send it to moderation.

    `hold_reasons` are reasons to keep it from publishing regardless of the compliance
    verdict (today: a truncated draft). Any of them sends it to moderation, listed ahead
    of the compliance reasons, so a person sees why.
    """
    hold_reasons = list(hold_reasons or [])
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
    source_refs = dedupe_source_refs(source_refs)

    compliant = review["compliant"] and not hold_reasons
    reasons = hold_reasons + list(review["reasons"])

    put_article(
        article_id=article_id,
        topic_id=topic_id,
        title=title,
        body_s3_key=body_s3_key,
        status="published" if compliant else "pending_moderation",
        created_at=now,
        published_at=now if compliant else None,
        source_refs=source_refs,
        lineage=lineage,
        published_by="ai_only" if compliant else None,
        **({"review": fresh_review_record} if fresh_review_record else {}),
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
            lineage=lineage,
            published_by="ai_only",
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
        reasons=reasons,
        created_at=now,
        **_review_notes_kwargs(fresh_review_record),
    )
    return {
        "status": "pending_moderation",
        "topic_id": topic_id,
        "article_id": article_id,
        "compliant": False,
        "reasons": reasons,
    }
