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
import re
from datetime import UTC, datetime, timedelta

import boto3

from common import compliance
from common.attribution import sources_for_topics
from common.bedrock import invoke_model_tracked
from common.costing import build_lineage
from common.digest import DIGEST_TOPIC_ID, DIGEST_TOPIC_NAME
from common.dynamo import (
    get_article,
    get_latest_finding,
    list_topics,
    put_article,
    put_moderation_item,
)
from common.lambda_timing import track_lambda_duration
from common.model_routing import resolve_model
from common.musings import generate_and_store_article_musing
from common.source_refs import dedupe_source_refs
from common.static_pages import render_and_publish_article_page
from common.stats_tracking import record_article_lineage, refresh_articles_snapshot

DIGEST_LOOKBACK_HOURS = 48

# The layout is built in code (_compose_digest), not left to the model: left to it, every day's
# digest came out a different shape -- usually one long paragraph per topic behind an inline bold
# label, no headings, and the title repeated as a body heading. The model now only supplies the
# words, one line each in a fixed format; the headings come from the topics' own names.
_DIGEST_PROMPT_TEMPLATE = """You are writing a short cross-topic "trending everywhere" digest for a \
research-and-publishing platform that independently tracks several unrelated domains.

Below is the most recent research finding for each topic currently showing activity, numbered:

{topic_blocks}

Reply in exactly this format, one line each, and nothing else:
OVERVIEW: <one or two sentences: what genuinely connects these topics (a shared theme, technology or \
event), or, if nothing does, the single standout item>
1: <one or two sentences on the standout from topic 1>
2: <one or two sentences on the standout from topic 2>
...and so on, one numbered line for every topic above, in the same order.

Plain sentences only: no headings, no titles, no bold, no bullet points. Do not speculate beyond \
what's given, and do not give financial or investment advice.
"""

_DIGEST_FINANCIAL_GUIDANCE_HEADER = "Financial-topic guidance (mandatory):"


@track_lambda_duration("trending_digest")
def handler(event, context) -> dict:
    try:
        result = _run_trending_digest()
        # A digest was written: rebuild the Stats page's Articles figures.
        if result.get("status") in ("published", "pending_moderation"):
            refresh_articles_snapshot()
        return result
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"trending_digest_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}


def digest_article_id(day: str) -> str:
    """The digest's article id for a UTC date (`YYYY-MM-DD`): one per day."""
    return f"digest-{day}"


def _run_trending_digest() -> dict:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    article_id = digest_article_id(today)

    # One digest per day. A second run (a manual trigger, a retry) used to write
    # a second identical article; now it is a no-op while the day's digest exists
    # in any state but `rejected`. To regenerate, reject/unpublish it first --
    # the rerun then replaces it. Checked before any model call.
    existing = get_article(article_id)
    if existing is not None and existing.get("status") != "rejected":
        return {
            "status": "already_exists",
            "article_id": article_id,
            "article_status": existing.get("status"),
        }

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
    model_text, synthesis_call = _synthesize_digest(
        contributions, model_id, fallback_model_id, financial=any_financial
    )
    draft_text = _compose_digest(contributions, model_text)
    if any_financial:
        draft_text = compliance.append_financial_disclaimer(draft_text)
    title = f"Trending Everywhere -- {today}"

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
    record_article_lineage(lineage)

    return _publish_or_moderate_digest(
        article_id=article_id,
        title=title,
        draft_text=draft_text,
        source_refs=source_refs,
        review=review,
        model_id=model_id,
        lineage=lineage,
        # The digest is written from several topics' findings, so it credits the union of their
        # adapters' sources -- only the topics that contributed today, not every topic.
        attribution=sources_for_topics(c["topic"] for c in contributions),
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
        f"[{number}] {_topic_name(c)}\n{c['finding'].get('summary', '')}"
        for number, c in enumerate(contributions, start=1)
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


_LINE = re.compile(r"^\s*(OVERVIEW|\d{1,2})\s*[:.)\]-]\s*(.+?)\s*$", re.IGNORECASE)
_MARKUP = re.compile(r"(\*\*|__|`)")
_FALLBACK_MAX_CHARS = 400


def _topic_name(contribution: dict) -> str:
    topic = contribution["topic"]
    name = " ".join(str(topic.get("name") or topic["topic_id"]).split())
    return name.lstrip("#").strip() or topic["topic_id"]


def _clean_sentence_text(text: str) -> str:
    """A line from the model as plain prose: no emphasis markers, no leading heading or bullet marks."""
    text = _MARKUP.sub("", text)
    return re.sub(r"^[#>*\-\s]+", "", text).strip()


def _fallback_from_summary(summary: str) -> str:
    """A topic the model gave no line for still gets its section: the start of its own finding's
    summary, as plain prose, cut at a sentence end where one comes early enough."""
    lines = [line.strip() for line in str(summary or "").splitlines()]
    text = " ".join(_clean_sentence_text(line) for line in lines if line and not line.startswith("#"))
    text = " ".join(text.split())
    if len(text) <= _FALLBACK_MAX_CHARS:
        return text
    cut = text[:_FALLBACK_MAX_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > _FALLBACK_MAX_CHARS // 3 else cut.rstrip() + "…"


def _compose_digest(contributions: list[dict], model_text: str) -> str:
    """The digest body, laid out by code so it is the same shape every day: the model's overview
    paragraph (if it gave one), then a `## <Topic name>` section per contributing topic, in order,
    with the model's line for that topic -- or, if it gave none, the start of that topic's own
    finding summary. No title in the body (the page already shows it)."""
    overview = ""
    per_topic: dict[int, str] = {}
    for line in (model_text or "").splitlines():
        match = _LINE.match(_MARKUP.sub("", line))  # "**OVERVIEW:**" is still the overview
        if not match:
            continue
        key, text = match.group(1).upper(), _clean_sentence_text(match.group(2))
        if not text:
            continue
        if key == "OVERVIEW":
            overview = overview or text
        else:
            per_topic.setdefault(int(key), text)

    sections = [overview] if overview else []
    for number, contribution in enumerate(contributions, start=1):
        heading = f"## {_topic_name(contribution)}"
        text = per_topic.get(number) or _fallback_from_summary(contribution["finding"].get("summary", ""))
        sections.append(f"{heading}\n\n{text}" if text else heading)
    return "\n\n".join(sections)


def _publish_or_moderate_digest(
    *,
    article_id: str,
    title: str,
    draft_text: str,
    source_refs: list[dict],
    review: dict,
    model_id: str,
    lineage: dict,
    attribution: list[dict] | None = None,
) -> dict:
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
        **({"attribution": attribution} if attribution is not None else {}),
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
            attribution=attribution,
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

    # The queue item shares the article's id, so replacing a rejected digest
    # overwrites its old (rejected) queue row instead of leaving two rows for
    # one article.
    put_moderation_item(
        queue_id=article_id,
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
