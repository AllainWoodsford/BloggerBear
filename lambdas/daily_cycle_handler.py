"""Daily authoring cycle Lambda handler.

Manually invoked per-topic (event: {"topic_id": str}). Iterating all topics
on a schedule is Phase 3 and is intentionally not built here.

Flow (see docs/project-plan.md §4 "Daily Authoring Cycle" and §5 data
model):
1. Load topic. A retry of a run that already stored its article (the state machine's execution
   id, or a direct invoke's request id, matches the topic's `last_article_run`) stops here with
   `already_written`: see `_run_id` and `_record_article_written`.
2. Load every finding since the topic's previous article, at most the topic's
   findings window back (FINDINGS_WINDOW_HOURS unless it sets its own
   `findings_window_hours`, see common/findings_window.py; the whole window the
   research loop covered, not just the newest few); bail out if there's nothing
   new to write about.
   Topics with a daily editorial goal (the crypto feed -- see
   common/editorial_goals.py) resolve today's goal here and keep only the
   findings that belong to it; its mandate and article style are folded into
   steps 3 and 5. A topic that rotates through several subjects (its adapter
   names a day subject, common/day_subject.py) does the same with the day's
   subject: the window starts at that subject's previous article, only the
   findings that report the subject are kept, and its own mandate and style
   are folded in. Every other topic is unaffected.
3. Ideate: ask Bedrock for 3 candidate angles, store them as CandidateIdeas.
   Ideation and drafting (step 5) both carry a relevance guardrail keyed to
   the active topic's name (common/relevance.py) and the topic's standing
   editorial goal (topic -> adapter -> global default; see
   common/editorial_resolver.py), so off-topic noise in the
   findings is ignored or reframed rather than written up.
4. Select: deterministically pick the first candidate (no scoring model yet).
5. Draft: ask Bedrock for a title and a full article draft.
6. Fresh-data review (common.fresh_review): compare the draft's claims with what
   the source says now. In shadow mode (the default) it is only recorded on the
   article (and its moderation item); in enforce mode a minor problem gets one
   checked revision pass, and a major one, a revision that fails its checks or a
   review that could not run holds the article (_enforce_review); `off` skips it.
   The topic's own `review_mode`, else `pipeline-config set --review-mode`.
7. Compliance review (common.compliance.review_draft).
8. Publish (S3 + Articles "published") or route to ModerationQueue
   (Articles "pending_moderation" + a ModerationQueue item), depending on
   the compliance verdict.
"""

import json
import os
import random
import re
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime

import boto3

from common import compliance, equipment, fresh_review, goal_rotation, prompts
from common.adapters.base import DaySubject
from common.attribution import sources_for_topic
from common.bedrock import invoke_model_tracked
from common.costing import build_lineage, build_research_lineage
from common.day_subject import ARTICLE_SUBJECT_FIELD, subject_findings
from common.dynamo import (
    get_last_armor_versions,
    get_model_rotation_candidates,
    get_pipeline_config,
    get_top_voted_articles,
    get_topic,
    list_findings,
    list_prompt_refinements,
    list_recent_article_titles,
    list_recent_findings,
    put_article,
    put_candidate_idea,
    put_moderation_item,
    set_last_armor_versions,
    set_topic_goal_rotation_state,
    set_topic_last_article_at,
    set_topic_model_id,
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
from common.fact_check import fact_check_label
from common.figures import figures_for_findings
from common.findings_window import (
    FINDINGS_WINDOW_HOURS,
    SUBJECT_READ_LIMIT,
    findings_window_error,
    findings_window_hours,
    next_article_subject,
    read_stopped_at,
    window_start,
)
from common.lambda_timing import track_lambda_duration
from common.model_routing import resolve_model
from common.musings import generate_and_store_article_musing
from common.relevance import (
    draft_relevance_boundary,
    ideation_relevance_rule,
    topic_label,
)
from common.rewrite import run_rewrite
from common.source_refs import dedupe_source_refs
from common.static_pages import render_and_publish_article_page
from common.stats_tracking import record_article_lineage, refresh_articles_snapshot

# An article is written from everything the research loop found since the last
# daily run, not from the few newest findings: the window is the day the run
# covers (FINDINGS_WINDOW_HOURS, or the topic's own findings_window_hours, up to
# MAX_FINDINGS_WINDOW_HOURS: both from common/findings_window.py), the count is a
# safety cap, and the character budget keeps a busy topic's prompt bounded (the
# oldest findings drop out first). list_recent_findings is a descending Query on
# captured_at with this as its Limit, so when a wide window holds more findings
# than the cap it is the oldest that are left out, never the newest
# (tests/test_findings_window.py, tests/test_daily_cycle_handler.py). A topic that
# rotates through subjects caps its day subject's findings instead: it reads up to
# common/findings_window.SUBJECT_READ_LIMIT of the window, keeps the subject's, then
# caps those (_select_subject_and_findings).
MAX_WINDOW_FINDINGS = 48
SUMMARIES_MAX_CHARS = 40_000
# An article draft is a few paragraphs to a few pages; the 1,024-token default cut real
# drafts off mid-word (a stored body ended "...deep institutional liqu"). Room for a
# long draft, and one automatic retry with more if a reply still runs out.
DRAFT_MAX_TOKENS = 4096
DRAFT_RETRY_MAX_TOKENS = 8192
# Three short angles never need this much. A reasoning model (MiniMax M2 in production) spends
# its budget *thinking* first, and that counts against maxTokens: at the 1,024 default it thought
# for all 1,024 and sent back no text at all, nine days running, and the run bailed out with
# "no usable angles" each time (docs/friction.md). Room for the thinking, one retry with more,
# and a reply that still has no text in it is treated as the failure it is (see _ideate).
IDEATION_MAX_TOKENS = 4096
IDEATION_RETRY_MAX_TOKENS = 8192
TRUNCATED_DRAFT_REASON = (
    "draft truncated: the model ran out of output tokens before finishing the article"
)
IMPLAUSIBLE_TITLE_REASON = (
    "title looks like a refusal or clarifying question, not a title, even after one retry"
)
_NUM_CANDIDATE_ANGLES = 3
_LIST_MARKER_RE = re.compile(r"^[\s\d.\-\)]+")
_FEW_SHOT_EXCERPT_CHARS = 500

# How many of a topic's own recent titles ideation is shown, so a story that stays on GitHub's
# trending list (or similarly persistent source) for several consecutive days doesn't get written
# up again each day just because that day's numbers are technically new (two production articles
# on the same repo, back to back, is what prompted this).
RECENT_TITLES_LIMIT = 5

# Bug writeup (production, two real incidents): ideation can refuse outright when it judges a
# day's findings don't support its editorial mandate ("I cannot propose article angles based on
# these findings... the findings consist almost entirely of..."), and the per-line parser below
# used to have no way to tell that refusal's own sentences apart from real angles -- it just
# harvested whatever non-empty lines came out, and the first one (the refusal itself) got selected
# and fed into both the draft and the title call. Drafting recovered by improvising a topic from
# the raw findings; title-writing did not, and answered the nonsensical "angle" as literally and
# earnestly as a confused person would -- which got published as the article's title, unchecked.
# _is_plausible_reply below is the guard against both ends of that failure: an ideation "angle"
# this rejects is never stored as a candidate, and a title this rejects is never stored as a title.
_REFUSAL_PHRASES = (
    "i cannot",
    "i can't",
    "i'm not able",
    "i am not able",
    "i don't have enough",
    "i do not have enough",
    "i'm not sure",
    "i am not sure",
    "don't understand",
    "do not understand",
    "could you provide",
    "could you clarify",
    "can you provide",
    "can you clarify",
    "as an ai",
    "i'd be happy to help",
    "i would be happy to help",
)
# A real angle is a descriptive phrase or short sentence; a real title is shorter still. Neither
# is ever a multi-paragraph reply -- the actual shape every refusal this guards against takes.
_MAX_ANGLE_WORDS = 40
_MAX_TITLE_WORDS = 20


def _is_plausible_reply(text: str, *, max_words: int) -> bool:
    """A plausible single angle/title -- not a stray refusal or clarifying question the model
    produced instead of following the format it was asked for. Not a guarantee of quality, only a
    guard against that one specific failure mode (see the module-level note above)."""
    if not text or "\n" in text:
        return False
    if any(phrase in text.lower() for phrase in _REFUSAL_PHRASES):
        return False
    return len(text.split()) <= max_words
_FEEDBACK_GUIDANCE_HEADER = "Additional guidance based on reader feedback:"
_FINANCIAL_GUIDANCE_HEADER = "Financial-topic guidance (mandatory):"
_SOURCE_GUIDANCE_HEADER = "How articles on this topic are written (mandatory):"
# The reply formats the two parsers depend on: ideation's one angle per line (the parser in
# `_ideate`), and the title's single short line (`_is_plausible_reply`), with the retry's firmer
# line after it. Values and locked blocks of the daily_cycle prompts (common/prompts_registry.py),
# so a prompt asset can place them but never reword them.
_IDEATION_REPLY_RULE = "Reply with exactly one angle per line, no numbering, no extra commentary."
_TITLE_REPLY_RULE = (
    f"Reply with the title only, as a single line of no more than {_MAX_TITLE_WORDS} words. "
    "No surrounding quotes, no markdown, no explanation, and no questions back -- if the "
    "angle is unclear, title it as best you can rather than asking for clarification."
)
_TITLE_RETRY = (
    "\n\nYour previous reply was not a usable title -- reply with nothing but the title itself "
    "this time."
)


# Outcomes that wrote an article or rewrote one: the Stats page's Articles figures are rebuilt
# after these and no others. already_written is a retry of a run that stored its article: the
# attempt that stored it may have timed out before its own rebuild, so the retry does it.
_ARTICLE_WRITTEN_STATUSES = ("published", "pending_moderation", "rewritten", "already_written")


@track_lambda_duration("daily_cycle")
def handler(event: dict, context) -> dict:
    result = _handle(event, context)
    # A run that stored its article and then failed (article_stored) wrote one too.
    if result.get("status") in _ARTICLE_WRITTEN_STATUSES or result.get("article_stored"):
        refresh_articles_snapshot()
    return result


def _run_id(event: dict, context) -> str | None:
    """The id that names this run across its retries, or None when there is nothing to name it by.

    A scheduled run comes through the state machine, whose NameTheRun state adds the execution's
    id to the input (`execution.id`): every retry of its Task carries the same one, though each
    retry is a new Lambda request. A direct invoke (a manual trigger) has no execution, so its
    Lambda request id stands in: Lambda keeps that across a duplicate delivery of the same invoke.
    With neither (a test, or a context without one) the run is not checked at all, as before.

    The id stops only an attempt that reads the topic after an earlier attempt of the same run
    stored its article (`_written_by_this_run`). Two attempts that overlap both pass the check and
    can each store an article: a duplicate delivery that starts while the first still runs, or a
    Task retry after the invoke itself failed while the function ran on. A timeout, the case this
    is for, leaves no attempt running."""
    execution = (event or {}).get("execution")
    if isinstance(execution, dict):
        execution_id = execution.get("id")
        if isinstance(execution_id, str) and execution_id:
            return execution_id
    request_id = getattr(context, "aws_request_id", None)
    return request_id if isinstance(request_id, str) and request_id else None


def _handle(event: dict, context=None) -> dict:
    # A Re-Write of a held article (common/rewrite.py), invoked asynchronously by the Admin
    # API's POST /moderation-queue/{queue_id}/rewrite. It runs here rather than in a Lambda of
    # its own because it needs exactly this one's timeout, permissions and CoinGecko key.
    if (event or {}).get("action") == "rewrite":
        queue_id, rewrite_id = event.get("queue_id"), event.get("rewrite_id")
        if not queue_id or not rewrite_id:
            return {"status": "error", "error": "rewrite event needs 'queue_id' and 'rewrite_id'"}
        print(f"daily_cycle_handler: starting rewrite for queue_id={queue_id}")
        return run_rewrite(queue_id, rewrite_id)

    topic_id = (event or {}).get("topic_id")
    if not topic_id:
        return {"status": "error", "error": "event missing required 'topic_id'"}

    # Only an operator's manual trigger ever sets this; the schedule never does.
    force = (event or {}).get("force") is True

    print(f"daily_cycle_handler: starting run for topic_id={topic_id} force={force}")
    run_id = _run_id(event, context)
    try:
        # Every prompt of this run draws its A/B arm by the run's id (common/prompts.py), so a
        # retry of the run is given the same versions; a run with no id draws at random.
        with prompts.bind_run(run_id):
            return _run_daily_cycle(topic_id, force=force, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        # Said in the log too: a caught error is a successful invocation to Step Functions, so
        # without this line it shows only in the execution's output.
        print(f"daily_cycle_handler: run failed for topic_id={topic_id}: {exc!r}")
        return {"status": "error", "topic_id": topic_id, "error": str(exc)}


def _window_hours(topic: dict) -> int:
    """The topic's findings window in hours (common/findings_window.py). A stored value that is
    not a whole number of hours in range is said in the log and the default used, never raised:
    a hand-edited topic must not stop its articles."""
    stored = topic.get("findings_window_hours")
    problem = findings_window_error(stored)
    if problem:
        print(
            f"daily_cycle_handler: topic_id={topic.get('topic_id')} findings_window_hours "
            f"{stored!r:.40} {problem}; using the default {FINDINGS_WINDOW_HOURS} h"
        )
    return findings_window_hours(topic)


def _window_start(topic: dict, run_started: datetime, force: bool, subject_key: str | None = None) -> str:
    """ISO timestamp findings must be captured at or after to feed this article: the topic's
    findings window, never reaching back past its previous article (on the day's subject, for a
    topic that rotates: `subject_key`), as common/findings_window.py's `window_start` works it out
    (the one rule, shared with the assistant's topic_findings and the Admin API's findings route).
    `_window_hours` says in the log when a stored window is not valid; the rule itself is the
    shared one."""
    return window_start(topic, run_started, force=force, hours=_window_hours(topic), subject_key=subject_key)


def _record_article_written(
    topic_id: str,
    run_started: datetime,
    subject_key: str | None = None,
    rotation: goal_rotation.Pick | None = None,
    run_id: str | None = None,
    article_id: str | None = None,
) -> None:
    """Remember when this run began, so the next window starts there.

    The run's *start* (not its end) is stored: a finding captured while the article
    was being written belongs to the next one. Called the moment the article's row is
    stored (`_publish_or_moderate`'s `on_stored`), before its page, musing or moderation
    item: if one of those then fails, or the Lambda times out, the next window still
    starts here, so no later run takes these Findings as new (a retry of this run is
    stopped by `last_article_run` instead, below). Never raises -- the article already
    exists by now, and an error here would make Step Functions retry the whole run and
    write a duplicate. For a topic that rotates, the day's subject's own stamp is set
    too (`last_article_by_subject`), so that subject's next day reads from here and the
    other subjects' windows stay where they were.

    With a `run_id` (`_run_id`), the run and the article it stored are kept in the same write as
    the stamp (`last_article_run`). The stamp alone does not stop a retry: a topic with a goal
    rotation would revisit the same Findings at once under its next goal, and a Finding captured
    while this run wrote would be new to the retry. So a retry of this run that starts after this
    write reads `last_article_run` and writes nothing (`_run_daily_cycle`'s already_written); one
    that overlaps this attempt is not stopped (`_run_id`). Without one, the stamp is written
    exactly as it always was.

    With a goal rotation (common/goal_rotation.py), its state for the subject (or the topic) is
    stored too, the same way: a failure is said in the log, and the next run then picks as if this
    article had not been written under its goal (at worst one goal comes round again).
    """
    stamp_kwargs: dict = {}
    if subject_key is not None:
        stamp_kwargs["subject_key"] = subject_key
    if run_id is not None:
        stamp_kwargs["run"] = {"run_id": run_id, "article_id": article_id}
    try:
        set_topic_last_article_at(topic_id, run_started.isoformat(), **stamp_kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"daily_cycle_handler: could not record last_article_at for {topic_id}: {exc!r}")
    if rotation is None:
        return
    try:
        set_topic_goal_rotation_state(topic_id, goal_rotation.state_key(subject_key), rotation.state)
    except Exception as exc:  # noqa: BLE001
        print(f"daily_cycle_handler: could not record goal_rotation_state for {topic_id}: {exc!r}")


def _day_subject(topic: dict, run_started: datetime) -> DaySubject | None:
    """The subject this run writes about, for a topic whose adapter rotates through several
    (common/day_subject.py), asked once at the run's start; None for every other topic. A topic
    with a daily editorial goal (the crypto feed) keeps its goal and is not asked: the goal's path
    is unchanged. Whatever the adapter raises stops the run before any model call (the handler's
    guard returns it as an error), and nothing is stamped: a broken rota writes nothing. The rule
    is common/findings_window.next_article_subject, the one the assistant's topic_findings and the
    Admin API's findings route ask too, so they name the subject this run writes about."""
    return next_article_subject(topic, run_started)


def _read_window(topic_id: str, since: str, day_subject: DaySubject | None) -> list[dict]:
    """The window's findings, newest first. Every topic but a rotating one reads exactly what it
    always read: the newest MAX_WINDOW_FINDINGS. A rotating topic reads further (at most
    SUBJECT_READ_LIMIT, paged as the assistant's topic_findings and the findings route read it),
    so the cap falls on its day subject's own findings, after the subject filter: capped first,
    other subjects' newer findings would push the subject's older ones out, and its stamp would
    then pass them for good. When the window holds more than that read reaches (SUBJECT_READ_LIMIT,
    or fewer when DynamoDB's 1 MB pages run out first), the log says where it stopped and how many
    it read, as it says what the cap left out: the subject's older findings were not seen."""
    if day_subject is None:
        return list_recent_findings(topic_id, limit=MAX_WINDOW_FINDINGS, since=since)
    findings, _, more = list_findings(topic_id, since=since, limit=SUBJECT_READ_LIMIT, unexpired_at=None)
    if more:
        print(
            f"daily_cycle_handler: day_subject={day_subject.key} topic_id={topic_id} read {len(findings)} "
            f"findings of its window and stopped at {read_stopped_at(len(findings))}; any of its older "
            "ones were not read"
        )
    return findings


def _no_findings(topic_id: str, subject_key: str | None) -> dict:
    """The "nothing to write about" result. For a rotating topic it names the day's subject; for
    every other topic it is exactly what it always was."""
    if subject_key is None:
        return {"status": "no_findings", "topic_id": topic_id}
    return {"status": "no_findings", "topic_id": topic_id, ARTICLE_SUBJECT_FIELD: subject_key}


# Added to the mandate of an article that revisits Findings an earlier article used
# (common/goal_rotation.py), so it is a new reading of them, not the old article again.
REVISIT_NOTE = (
    "These findings have already been written up on this topic under a different editorial goal "
    "(the recent titles show how). This article takes the goal above only: a different reading of "
    "the same findings, never a retelling of the earlier article."
)


def _rotation_goals(topic: dict, run_started: datetime) -> list[goal_rotation.RotatingGoal]:
    """The topic's goal rotation (common/goal_rotation.py), or [] for a topic without one. A topic
    with a daily editorial goal (the crypto feed) keeps that rotation and ignores this one; a stored
    rotation that is not valid is said in the log and ignored, never raised."""
    if not topic.get(goal_rotation.FIELD):
        return []
    if resolve_goal_for_topic(topic, run_started.date()) is not None:
        print(
            f"daily_cycle_handler: topic_id={topic.get('topic_id')} has a daily editorial goal; "
            "its goal rotation is ignored"
        )
        return []
    goals = goal_rotation.rotation_goals(topic)
    if not goals:
        print(
            f"daily_cycle_handler: topic_id={topic.get('topic_id')} has a goal rotation that is not "
            "valid; ignored"
        )
    return goals


def _pick_rotation_goal(
    topic: dict,
    goals: list[goal_rotation.RotatingGoal],
    subject_key: str | None,
    findings: list[dict],
    run_started: datetime,
) -> goal_rotation.Pick | None:
    """The goal this article is written under and the Findings it is written from: with new
    `findings`, the next goal over them; without, a revisit of the subject's current set under a
    goal not yet used on it, its Findings read back (still unexpired, the day's subject's only,
    capped as any article's). None when there is nothing to revisit: every goal used, no current
    set, or its Findings expired."""
    topic_id = topic["topic_id"]
    state = goal_rotation.read_state(topic, goal_rotation.state_key(subject_key))
    if findings:
        return goal_rotation.pick_for_new_findings(goals, state, findings)
    goal = goal_rotation.next_unused(goals, state)
    if goal is None:
        print(
            f"daily_cycle_handler: topic_id={topic_id} subject={subject_key} nothing new and no goal "
            "left to revisit with"
        )
        return None
    unexpired_at = int(run_started.timestamp())
    read, _, _ = list_findings(
        topic_id, since=state["from"], until=state["to"], limit=SUBJECT_READ_LIMIT, unexpired_at=unexpired_at
    )
    if subject_key is not None:
        read = subject_findings(read, subject_key)
    if not read:
        print(
            f"daily_cycle_handler: topic_id={topic_id} subject={subject_key} the findings to revisit "
            "have expired"
        )
        return None
    print(
        f"daily_cycle_handler: topic_id={topic_id} subject={subject_key} revisiting {len(read)} "
        f"findings under goal={goal.name}"
    )
    return goal_rotation.pick_for_revisit(state, goal, read[:MAX_WINDOW_FINDINGS])


def _rotation_focus(rotation: goal_rotation.Pick | None) -> str | None:
    """The picked goal's primary focus, standing in for the topic's own (None: no rotation)."""
    return rotation.goal.primary_focus if rotation is not None else None


def _revisit_note(rotation: goal_rotation.Pick | None) -> str:
    """REVISIT_NOTE on its own line for a revisit, else nothing (the prompt is unchanged)."""
    return f"\n{REVISIT_NOTE}" if rotation is not None and rotation.revisit else ""


def _rotation_fields(rotation: goal_rotation.Pick | None) -> dict:
    """What an article and the run's result say about the rotation: the goal's name, and
    `revisit` when it was one; {} without a rotation."""
    if rotation is None:
        return {}
    return {"editorial_goal": rotation.goal.name, **({"revisit": True} if rotation.revisit else {})}


def _written_by_this_run(topic: dict, run_id: str | None) -> dict | None:
    """The topic's `last_article_run` when this same run wrote it (a retry after the article was
    stored), else None. A run with no id is never matched, and a field that is missing or is not
    a map (a hand edit: only `set_topic_last_article_at` writes it) matches nothing."""
    if run_id is None:
        return None
    last_run = topic.get("last_article_run")
    if isinstance(last_run, dict) and last_run.get("run_id") == run_id:
        return last_run
    return None


def _run_daily_cycle(topic_id: str, force: bool = False, run_id: str | None = None) -> dict:
    topic = get_topic(topic_id)
    if topic is None:
        return {"status": "error", "topic_id": topic_id, "error": "topic not found"}
    written = _written_by_this_run(topic, run_id)
    if written is not None:
        # A retry of a run that stored its article and then timed out or crashed: the article is
        # this run's, whatever the rest of the run did. Going on would write another (a goal
        # rotation's next revisit, or a Finding captured meanwhile), so nothing more is read or
        # written. Checked before `paused`: the retry still names its article, and the handler
        # rebuilds the Stats figures the timed-out attempt may not have.
        article_id = written.get("article_id")
        print(
            f"daily_cycle_handler: topic_id={topic_id} run={run_id} already stored "
            f"article_id={article_id}; nothing written"
        )
        return {"status": "already_written", "topic_id": topic_id, "article_id": article_id}
    if topic.get("paused") is True:
        # Paused by the operator (`topics pause`): no candidate ideas, no draft, no article,
        # whatever started this run, until it is resumed. The Topics row decides.
        print(f"daily_cycle_handler: {topic_id} is paused; nothing written")
        return {"status": "paused", "topic_id": topic_id}

    run_started = datetime.now(UTC)
    # A topic that rotates through subjects writes about the day's one (common/day_subject.py),
    # asked once here, so a run that crosses midnight keeps the subject it started with.
    day_subject = _day_subject(topic, run_started)
    subject_key = day_subject.key if day_subject is not None else None
    if day_subject is not None:
        print(f"daily_cycle_handler: day_subject={subject_key} topic_id={topic_id}")
    since = _window_start(topic, run_started, force, subject_key)
    findings = _read_window(topic_id, since, day_subject)
    # A topic with a goal rotation (common/goal_rotation.py) may revisit findings an earlier
    # article used, under a goal not yet used on them, so it goes on even with nothing new.
    rotation_goals = _rotation_goals(topic, run_started)
    if not findings and not rotation_goals:
        # Nothing new since the topic's last article (or in its findings window): better
        # no article than one rewritten from findings a previous run already used.
        return _no_findings(topic_id, subject_key)

    # Topics with a daily editorial goal (the crypto feed) get the day's
    # goal and only the findings that belong to it, a rotating topic the day's
    # subject and only the findings that report it; every other topic gets
    # (None, findings) back unchanged.
    #
    # The research tally is taken from the whole window *before* that selection:
    # a Finding the goal filter sets aside still cost a Bedrock call, and each
    # window starts where the last one ended, so nothing is counted twice.
    research = build_research_lineage(findings)
    window_findings = findings
    editorial_goal, findings = _select_subject_and_findings(topic, findings, day_subject)
    if isinstance(editorial_goal, EditorialGoal):
        print(f"daily_cycle_handler: editorial_goal={editorial_goal.value} topic_id={topic_id}")
    if day_subject is not None:
        if not findings and not rotation_goals:
            # Nothing on the day's subject since its last article: no article today and no stamp,
            # so the subject's next day still reads from its own last article. Never another
            # subject's findings instead: those are their own day's article.
            print(f"daily_cycle_handler: no new findings on day_subject={subject_key} topic_id={topic_id}")
            return _no_findings(topic_id, subject_key)
        # Each subject's window starts at its own last article, so the windows of different
        # subjects overlap and a tally of the whole window would count a Finding once per subject.
        # The tally is of the Findings this article is written from: each is counted on its own
        # subject's day (on each day, for one that reports two subjects).
        research = build_research_lineage(findings)

    rotation = None
    if rotation_goals:
        rotation = _pick_rotation_goal(topic, rotation_goals, subject_key, findings, run_started)
        if rotation is None:
            return _no_findings(topic_id, subject_key)
        print(
            f"daily_cycle_handler: editorial_goal={rotation.goal.name} revisit={rotation.revisit} "
            f"topic_id={topic_id}"
        )
        if rotation.revisit:
            # The revisited findings' research was counted on the article that first used them.
            findings = window_findings = rotation.findings
            research = build_research_lineage([])

    model_id, fallback_model_id = resolve_model(topic)
    summaries_block = _format_findings_summaries(findings)

    # Phase 5: fold in the guidance the bear is wearing (approved prompt refinements, see
    # common/equipment.py) and a few-shot excerpt from the topic's best-received past article,
    # if either exists. Both are strictly additive -- when neither exists, the prompts below
    # are built exactly as they were before Phase 5. What was worn is kept on the article.
    guidance, equipment_used = _get_approved_guidance(topic_id)
    few_shot_excerpt = _get_few_shot_excerpt(topic_id)
    recent_titles = list_recent_article_titles(topic_id, limit=RECENT_TITLES_LIMIT)

    angles, ideate_call = _ideate(
        topic,
        summaries_block,
        model_id,
        fallback_model_id,
        guidance=guidance,
        goal=editorial_goal,
        recent_titles=recent_titles,
        rotation=rotation,
    )
    if not angles:
        # Ideation refused outright (a real production incident: "I cannot propose article
        # angles based on these findings...") rather than producing anything usable -- better no
        # article today than one built around a candidate that was never really an angle. Same
        # shape as the "nothing new since last run" bail-out above.
        # Say what came back, so the next person reading the logs can tell a refusal (text, no
        # angles in it) from a reply with no text in it (a reasoning model that ran out of
        # tokens), without re-running the model to find out.
        print(
            f"daily_cycle_handler: ideation had no usable angles for topic_id={topic_id} "
            f"model_id={ideate_call['model_id']} stop_reason={ideate_call.get('stop_reason')} "
            f"output_tokens={ideate_call['output_tokens']} reply_chars={ideate_call['reply_chars']}"
        )
        # A model that can't propose angles for this topic must not keep the topic: the roll
        # forward used to happen only after a publish, so a topic whose model never published
        # stayed on that model for good (two production topics, nine days each).
        _assign_rotated_model(topic_id)
        return {"status": "no_usable_angles", "topic_id": topic_id}
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
        rotation=rotation,
    )
    title, title_call, title_is_plausible = _draft_title(selected["angle"], model_id, fallback_model_id)

    # A draft that ran out of tokens even after the retry is half an article, and a title that
    # still doesn't look like one after its own retry is a quality problem the model itself
    # couldn't resolve -- neither publishes automatically. Both go to a human, whatever the
    # compliance review says.
    hold_reasons = []
    if draft_call.get("stop_reason") == "max_tokens":
        hold_reasons.append(TRUNCATED_DRAFT_REASON)
    if not title_is_plausible:
        hold_reasons.append(IMPLAUSIBLE_TITLE_REASON)
    if compliance.requires_manual_review(topic):
        hold_reasons.append(compliance.MANUAL_REVIEW_REASON)
    if hold_reasons:
        print(f"daily_cycle_handler: holding {hold_reasons} for topic_id={topic_id}")

    # Fresh-data review (docs/project-plan.md §11, "(C)"): compare the draft and its title with
    # what the source says *now*. In shadow mode that is recorded and nothing more; in enforce
    # mode a minor problem is corrected by one revision pass and a major one (or a review that
    # could not run) holds the article for a person. Placed before the disclaimer is appended
    # so it reviews the article's own text.
    fresh_record, fresh_call, fresh_evidence, on_unavailable = _run_fresh_review(
        topic, title, draft_text, summaries_block, window_findings, model_id, fallback_model_id
    )
    revision_call = None
    original_body = None
    if fresh_record is not None and fresh_record.get("mode") == "enforce":
        enforcement = _enforce_review(
            topic=topic,
            record=fresh_record,
            evidence=fresh_evidence,
            title=title,
            draft_text=draft_text,
            summaries_block=summaries_block,
            model_id=model_id,
            fallback_model_id=fallback_model_id,
            on_unavailable=on_unavailable,
        )
        title, draft_text = enforcement["title"], enforcement["draft_text"]
        revision_call, original_body = enforcement["revision_call"], enforcement["original_body"]
        hold_reasons = hold_reasons + enforcement["hold_reasons"]

    # Phase 7: deterministically guarantee the standing "not financial
    # advice" disclaimer on every financial-topic draft, regardless of
    # whether the model actually followed the guidance folded into the
    # prompts above -- see common/compliance.py's append_financial_disclaimer.
    if compliance.is_financial_topic(topic):
        draft_text = compliance.append_financial_disclaimer(draft_text)

    # The reviewer sees the research the draft was written from, so a figure taken straight from
    # the findings is not mistaken for an invented one (see compliance.py).
    review = compliance.review_draft(
        draft_text,
        topic,
        model_id,
        fallback_model_id=fallback_model_id,
        source_material=summaries_block,
    )

    # AI lineage/cost tracking (docs/project-plan.md §11, PR 2 of 5): every
    # Bedrock call that actually contributed to this article, not just the
    # final draft -- review["lineage_call"] is None on the financial-topic
    # early-return path (no call was made there), filtered out below rather
    # than fabricated.
    calls = [
        call
        for call in (
            ideate_call,
            draft_call,
            title_call,
            fresh_call,
            revision_call,
            review["lineage_call"],
        )
        if call is not None
    ]
    lineage = build_lineage(calls, research=research)
    record_article_lineage(lineage)

    stored: dict = {}

    def _stored(article_id: str) -> None:
        stored["article_id"] = article_id
        _record_article_written(topic_id, run_started, subject_key, rotation, run_id, article_id)

    try:
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
            original_body=original_body,
            equipment_used=equipment_used,
            # The topic's adapter's source credit, as declared right now: stored on the article so
            # it keeps the credit it was written with (common/attribution.py).
            attribution=sources_for_topic(topic),
            subject_key=subject_key,
            rotation=rotation,
            # Stamped as soon as the row exists: see _publish_or_moderate's `on_stored`.
            on_stored=_stored,
        )
    except Exception as exc:
        if "article_id" not in stored:
            raise  # nothing was stored: the window stays open for the next run
        # The article exists and is now the only one these Findings will get: say which it is,
        # so a held one whose moderation item failed can still be found (`articles get`).
        print(
            f"daily_cycle_handler: article_id={stored['article_id']} topic_id={topic_id} was "
            f"stored, then a later step failed: {exc!r}"
        )
        return {
            "status": "error",
            "topic_id": topic_id,
            "article_id": stored["article_id"],
            "article_stored": True,
            "error": str(exc),
            **({ARTICLE_SUBJECT_FIELD: subject_key} if subject_key is not None else {}),
        }
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


def _read_pipeline_config() -> dict:
    """The pipeline-wide settings row, or {} if there is none or it cannot be read: a config
    read must never stop an article from being written (everything then takes its default)."""
    try:
        return get_pipeline_config() or {}
    except Exception as exc:  # noqa: BLE001
        print(f"daily_cycle_handler: could not read the pipeline config, using defaults: {exc!r}")
        return {}


def _run_fresh_review(
    topic: dict,
    title: str,
    draft_text: str,
    summaries_block: str,
    window_findings: list[dict],
    model_id: str,
    fallback_model_id: str | None,
) -> tuple[dict | None, dict | None, str | None, str]:
    """(review record, its lineage call, the evidence it used, what to do if a review can't
    run) -- or (None, None, None, ...) when the review is off for this topic.

    Never raises: the whole step is wrapped so nothing in it can fail a daily run. A step that
    itself fails is recorded as `unavailable`, in the mode it was running in, so enforce mode
    holds the article rather than publishing it unchecked. The evidence is returned separately
    (it is not stored) because the revision pass needs it.
    """
    config = _read_pipeline_config()
    on_unavailable = fresh_review.resolve_on_unavailable(config)
    mode = fresh_review.resolve_review_mode(config, topic)
    try:
        if mode == "off":
            return None, None, None, on_unavailable

        record = fresh_review.run_review(
            topic=topic,
            title=title,
            draft=draft_text,
            findings_text=summaries_block,
            latest_state=_load_latest_snapshot(window_findings),
            model_id=model_id,
            fallback_model_id=fallback_model_id,
            mode=mode,
        )
        lineage_call = record.pop("lineage_call", None)
        evidence = record.pop("evidence", None)
        print(
            f"daily_cycle_handler: fresh-data review mode={mode} status={record['status']} "
            f"outcome={record.get('outcome')} topic_id={topic.get('topic_id')}"
        )
        return record, lineage_call, evidence, on_unavailable
    except Exception as exc:  # noqa: BLE001
        print(f"daily_cycle_handler: fresh-data review failed: {exc!r}")
        failed = {"status": "unavailable", "reason": f"review step failed: {exc}", "mode": mode}
        return failed, None, None, on_unavailable


def _enforce_review(
    *,
    topic: dict,
    record: dict,
    evidence: str | None,
    title: str,
    draft_text: str,
    summaries_block: str,
    model_id: str,
    fallback_model_id: str | None,
    on_unavailable: str,
) -> dict:
    """Act on a review (enforce mode). Returns the (possibly corrected) title and body, any
    reasons to hold the article for a person, the revision's lineage call, and the original
    body if it was replaced.

    Updates `record` in place (revised / held / hold_reasons / revision_rejected) so the
    stored review says what was done. Fails safe: an error in here holds the article; it
    never lets an enforced article through unchecked or fails the run.
    """
    outcome = {
        "title": title,
        "draft_text": draft_text,
        "hold_reasons": [],
        "revision_call": None,
        "original_body": None,
    }
    try:
        action, reason = fresh_review.enforcement_action(record, on_unavailable)
        if action == "hold":
            outcome["hold_reasons"].append(reason)
        elif action == "revise":
            result = fresh_review.run_revision(
                topic=topic,
                title=title,
                body=draft_text,
                claims=record.get("claims") or [],
                findings_text=summaries_block,
                evidence=evidence or "",
                model_id=model_id,
                fallback_model_id=fallback_model_id,
            )
            outcome["revision_call"] = result.get("lineage_call")
            if result["status"] == "revised":
                record["revised"] = True
                if result["title"] != title:
                    record["original_title"] = title
                outcome.update(title=result["title"], draft_text=result["body"], original_body=draft_text)
            else:
                record["revision_rejected"] = result["reason"]
                outcome["hold_reasons"].append(
                    "fresh-data review: the automatic correction could not be trusted "
                    f"({result['reason']}), so a person should check this"
                )
    except Exception as exc:  # noqa: BLE001
        print(f"daily_cycle_handler: enforcing the review failed, holding the article: {exc!r}")
        outcome["hold_reasons"].append(f"fresh-data review could not be applied ({exc})")

    if outcome["hold_reasons"]:
        record["held"] = True
        record["hold_reasons"] = list(outcome["hold_reasons"])
    return outcome


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


def _select_subject_and_findings(
    topic: dict, findings: list[dict], day_subject: DaySubject | None
) -> tuple[EditorialGoal | DaySubject | None, list[dict]]:
    """What this article is about, and the findings it is written from. A rotating topic's day
    subject (common/day_subject.py) keeps only the findings that report it (none, if none do: the
    caller then stops; never crypto's "follow the newest finding's day" fallback, which would write
    about another subject on this one's day), at most MAX_WINDOW_FINDINGS of them, the newest: the
    cap counts the subject's own findings, and the log says how many of them it left out. Without
    one, the crypto feed's editorial goal, as `_select_goal_and_findings` resolves it (an unchanged
    path), or nothing at all."""
    if day_subject is None:
        return _select_goal_and_findings(topic, findings)
    own = subject_findings(findings, day_subject.key)
    if len(own) > MAX_WINDOW_FINDINGS:
        print(
            f"daily_cycle_handler: day_subject={day_subject.key} topic_id={topic.get('topic_id')} has "
            f"{len(own)} findings in its window; drafting from the newest {MAX_WINDOW_FINDINGS}, "
            f"{len(own) - MAX_WINDOW_FINDINGS} older ones left out"
        )
    return day_subject, own[:MAX_WINDOW_FINDINGS]


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


def _goal_mandate(goal: EditorialGoal | DaySubject) -> str:
    """The day's editorial mandate: an editorial goal's, as it always was, or a day subject's own
    (written by its adapter, the subject's name folded in)."""
    return goal.mandate if isinstance(goal, DaySubject) else EDITORIAL_MANDATES[goal]


def _goal_style(goal: EditorialGoal | DaySubject) -> str:
    """The day's article style, the same way."""
    return goal.style if isinstance(goal, DaySubject) else ARTICLE_STYLES[goal]


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


def _get_approved_guidance(topic_id: str) -> tuple[str | None, list[dict]]:
    """Return the guidance the bear is wearing for this topic, and which gear it came from.

    Worn armor (global) plus the topic's rings; a refinement approved before equipment existed
    still applies until the topic has a ring (common/equipment.py). (None, []) means nothing
    applies -- callers must leave their prompts completely unchanged in that case.

    The armor draw avoids repeating the exact combination brought to the *previous* article
    (any topic -- armor is global): reads it before drawing, then records the new draw for next
    time, regardless of what happens to this article afterward -- what the bear brought is
    already a fact by the time drafting starts, whether or not the draft goes on to publish.
    Recording failures are logged and swallowed, never allowed to break drafting itself; so is a
    failure to read back the last draw -- worst case the bear just doesn't avoid a repeat once.
    """
    try:
        avoid_versions = set(get_last_armor_versions())
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break drafting
        print(f"daily_cycle_handler: could not read the last armor drawn: {exc!r}")
        avoid_versions = set()
    guidance, equipment_used = equipment.guidance_for(
        topic_id, list_prompt_refinements(status="approved"), avoid_armor_versions=avoid_versions
    )
    try:
        new_armor_versions = [
            piece["version"] for piece in equipment_used if piece.get("slot") in equipment.ARMOR_SLOTS
        ]
        set_last_armor_versions(new_armor_versions)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break drafting
        print(f"daily_cycle_handler: could not record the armor drawn: {exc!r}")
    return guidance, equipment_used


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


def _guidance_block(guidance: str | None) -> str:
    """The gear's guidance under its header, or nothing when the bear wears none (the prompt is
    then exactly what it was before Phase 5)."""
    return f"\n\n{_FEEDBACK_GUIDANCE_HEADER}\n{guidance}" if guidance else ""


def _financial_guidance_block(topic: dict) -> str:
    """The mandatory financial-topic guidance, or nothing on any other topic: a locked block of
    the ideation and draft prompts, so an asset can never drop it or put text after it."""
    if not compliance.is_financial_topic(topic):
        return ""
    return f"\n\n{_FINANCIAL_GUIDANCE_HEADER}\n{compliance.FINANCIAL_DRAFTING_GUIDANCE}"


def _prompt_fields(prompt: prompts.Prompt) -> dict:
    """What a lineage call records of the prompt it sent: its id, and its version (an asset's
    `v2`, or `vanilla:v1`), so an article's lineage says which version wrote each part of it."""
    return {"prompt_id": prompt.prompt_id, "prompt_version": prompt.version}


def _ideate(
    topic: dict,
    summaries_block: str,
    model_id: str,
    fallback_model_id: str | None,
    guidance: str | None = None,
    goal: EditorialGoal | DaySubject | None = None,
    recent_titles: list[str] | None = None,
    rotation: goal_rotation.Pick | None = None,
) -> tuple[list[str], dict]:
    topic_name = topic_label(topic)
    # A source that stays trending for days (a GitHub repo whose star count keeps climbing, say)
    # looks like fresh material to this call every single day, even when the underlying story is
    # the one already told -- this is the one thing that reminds it. Empty for a topic's first
    # article, or if nothing published recently, in which case the prompt is unchanged from before
    # this existed.
    already_covered = ""
    if recent_titles:
        titles_block = "\n".join(f"- {title}" for title in recent_titles)
        already_covered = (
            "\n\nAlready covered recently -- propose a genuinely different angle, not a rehash "
            f"of the same story with new numbers:\n{titles_block}"
        )
    # The templates are daily_cycle/ideation and daily_cycle/ideation_goal (common/prompts_registry.py).
    # The relevance rule sits before the data in both variants: findings from
    # community feeds and web search can be off-topic noise, and it is the
    # active topic's name (never a hardcoded topic) that anchors what counts
    # as on-topic.
    # The topic's standing editorial goal (topic -> adapter -> global default,
    # see common/editorial_resolver.py) comes first, and the relevance rule
    # then holds every angle to both the topic and that goal.
    values = {
        "topic_name": topic_name,
        "num_angles": _NUM_CANDIDATE_ANGLES,
        "reply_rule": _IDEATION_REPLY_RULE,
        "editorial_goals": resolve_editorial_goals(topic, goal, _rotation_focus(rotation)),
        "relevance_rule": ideation_relevance_rule(topic_name),
        "alignment_rule": MANDATE_ALIGNMENT_RULE,
        "revisit_note": _revisit_note(rotation),
        "already_covered": already_covered,
        "summaries": summaries_block,
        "guidance_block": _guidance_block(guidance),
    }
    prompt_id = "daily_cycle/ideation"
    if goal is not None:
        # The day's rotating editorial mandate shapes the angles within the
        # standing goal; the relevance rule keeps it inside the topic. The
        # one-angle-per-line reply instruction stays -- the parser below
        # depends on it.
        prompt_id = "daily_cycle/ideation_goal"
        values["daily_mandate"] = _goal_mandate(goal)
    prompt = prompts.get_prompt(prompt_id, values, {"financial_guidance": _financial_guidance_block(topic)})
    result = invoke_model_tracked(
        prompt.text,
        model_id,
        fallback_model_id=fallback_model_id,
        max_tokens=IDEATION_MAX_TOKENS,
        retry_max_tokens=IDEATION_RETRY_MAX_TOKENS,
    )
    if _reply_is_all_thinking(result) and fallback_model_id and not result["used_fallback"]:
        # The model ran out of tokens before it had written a single word (a reasoning model
        # that never finished thinking). Bedrock reports that as a normal reply with no text,
        # not as an error, so the fallback that an exception would have reached never ran.
        # Reach it here: a wasted call costs the same as a failed one.
        print(
            f"daily_cycle_handler: ideation on {result['model_id']} stopped at its token limit "
            f"with no text (output_tokens={result['output_tokens']}); trying {fallback_model_id}"
        )
        first = result
        result = invoke_model_tracked(
            prompt.text,
            fallback_model_id,
            max_tokens=IDEATION_MAX_TOKENS,
            retry_max_tokens=IDEATION_RETRY_MAX_TOKENS,
        )
        result = {
            **result,
            "input_tokens": first["input_tokens"] + result["input_tokens"],
            "output_tokens": first["output_tokens"] + result["output_tokens"],
            "attempts": first.get("attempts", 1) + result.get("attempts", 1),
            "used_fallback": True,
        }
    response = result["text"]

    angles = []
    for line in response.strip().splitlines():
        cleaned = _LIST_MARKER_RE.sub("", line).strip()
        if cleaned and _is_plausible_reply(cleaned, max_words=_MAX_ANGLE_WORDS):
            angles.append(cleaned)

    if not angles:
        # The model didn't reply one-angle-per-line as asked -- if what it sent as a whole still
        # reads like a single usable angle, keep that (the old, unconditional fallback here is
        # exactly what let a refusal like "I cannot propose article angles based on these
        # findings..." through as if it were one); otherwise there is genuinely nothing to
        # propose, and angles stays empty for the caller to treat like any other "nothing to
        # write about today" -- never invented.
        candidate = response.strip()
        if candidate and _is_plausible_reply(candidate, max_words=_MAX_ANGLE_WORDS):
            angles = [candidate]

    lineage_call = {
        "stage": "ideation",
        "model_id": result["model_id"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "used_fallback": result["used_fallback"],
        "stop_reason": result.get("stop_reason"),
        "reply_chars": len(response),
        **_prompt_fields(prompt),
    }
    if result.get("attempts", 1) > 1:
        lineage_call["attempts"] = result["attempts"]
    return angles[:_NUM_CANDIDATE_ANGLES], lineage_call


def _reply_is_all_thinking(result: dict) -> bool:
    """A reply that hit its token limit and has no text: every token went on reasoning that
    the Converse API returns in a block of its own, which the caller never sees."""
    return result.get("stop_reason") == "max_tokens" and not result["text"].strip()


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
    goal: EditorialGoal | DaySubject | None = None,
    rotation: goal_rotation.Pick | None = None,
) -> tuple[str, dict]:
    topic_name = topic_label(topic)
    # What the topic's adapter says its articles must always do (how to write figures that move,
    # how the article ends). Declared on the adapter, so nothing here knows the domain.
    rules = fresh_review.writing_rules(topic)
    source_guidance = "\n".join(filter(None, [rules["figure_guidance"], rules["drafting_guidance"]]))
    few_shot = ""
    if few_shot_excerpt:
        few_shot = (
            "\n\nHere is an excerpt from a well-received past article on this "
            "topic, for style reference only (do not repeat its content):\n"
            f"{few_shot_excerpt}"
        )
    # The template is daily_cycle/draft (common/prompts_registry.py). The boundary backs up the
    # ideation rule: an imperfect angle can still slip through, and the draft must not follow
    # noisy data off-topic.
    values = {
        "topic_name": topic_name,
        "angle": angle,
        "editorial_goals": resolve_editorial_goals(topic, goal, _rotation_focus(rotation)),
        "revisit_note": _revisit_note(rotation),
        "summaries": summaries_block,
        "relevance_boundary": draft_relevance_boundary(topic_name),
        "alignment_directive": DRAFT_ALIGNMENT_DIRECTIVE,
        "style_block": f"\n\nArticle style: {_goal_style(goal)}" if goal is not None else "",
        "guidance_block": _guidance_block(guidance),
    }
    locked = {
        "financial_guidance": _financial_guidance_block(topic),
        "source_guidance": f"\n\n{_SOURCE_GUIDANCE_HEADER}\n{source_guidance}" if source_guidance else "",
        "few_shot": few_shot,
    }
    prompt = prompts.get_prompt("daily_cycle/draft", values, locked)
    result = invoke_model_tracked(
        prompt.text,
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
        **_prompt_fields(prompt),
    }
    if result.get("attempts", 1) > 1:
        lineage_call["attempts"] = result["attempts"]
    return result["text"], lineage_call


def _draft_title(angle: str, model_id: str, fallback_model_id: str | None) -> tuple[str, dict, bool]:
    """Ask for a title, and check it actually looks like one (see the module-level note above --
    a refusal or clarifying question, answered as literally as the "angle" it was given and then
    published verbatim as the title, is a real production incident this guards against). One
    automatic retry with a firmer prompt, on the same model, if the first reply doesn't pass. If
    the retry doesn't either, the (still bad) text is still returned -- so a human reviewing it in
    moderation, where the caller routes it when the third element here is False, sees exactly what
    the model actually said -- rather than something invented in its place.
    """
    # The template is daily_cycle/title (common/prompts_registry.py).
    values = {"angle": angle}
    prompt = prompts.get_prompt(
        "daily_cycle/title", values, {"reply_rule": _TITLE_REPLY_RULE, "title_retry": ""}
    )
    result = invoke_model_tracked(prompt.text, model_id, fallback_model_id=fallback_model_id)
    title = result["text"].strip()
    input_tokens, output_tokens, used_fallback = (
        result["input_tokens"],
        result["output_tokens"],
        result["used_fallback"],
    )
    attempts = 1

    if not _is_plausible_reply(title, max_words=_MAX_TITLE_WORDS):
        # The same prompt with the retry's line added, from the same arm of a split whenever the
        # run is bound (`_handle`); lineage records the version of this, the call that counted.
        prompt = prompts.get_prompt(
            "daily_cycle/title", values, {"reply_rule": _TITLE_REPLY_RULE, "title_retry": _TITLE_RETRY}
        )
        result = invoke_model_tracked(prompt.text, model_id, fallback_model_id=fallback_model_id)
        title = result["text"].strip()
        # A discarded first reply still cost real tokens -- summed in below, not dropped from the
        # total, the same principle invoke_model_tracked's own max_tokens retry already follows.
        input_tokens += result["input_tokens"]
        output_tokens += result["output_tokens"]
        used_fallback = used_fallback or result["used_fallback"]
        attempts = 2

    lineage_call = {
        "stage": "title",
        "model_id": result["model_id"],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "used_fallback": used_fallback,
        **_prompt_fields(prompt),
    }
    if attempts > 1:
        lineage_call["attempts"] = attempts
    return title, lineage_call, _is_plausible_reply(title, max_words=_MAX_TITLE_WORDS)


def _assign_rotated_model(topic_id: str) -> None:
    """Once a topic has published an article -- or its model has just failed to propose any
    angle at all -- roll its `model_id` (Topics table) forward to a random pick from the shared
    rotation pool -- common/dynamo.py's "model-rotation" ModelConfig row -- so the *next* run
    for this topic tries a different model, without a Terraform apply or an admin edit. No
    candidates configured is a valid state: rotation then does nothing, same as before this
    existed.

    Deliberately only ever writes `model_id`, the next rung down in common/model_routing.py's
    resolve_model precedence -- a topic with its own `model_id_candidates` already rerolls every
    run on its own and keeps doing exactly that; this never touches or clobbers that list.

    Never allowed to affect an article that has already published: read and write failures, and
    an empty pool, are logged (the latter isn't even logged -- it's the expected steady state
    before anyone has configured a pool) and swallowed, same as the armor bookkeeping above.
    """
    try:
        candidates = get_model_rotation_candidates()
        if not candidates:
            return
        set_topic_model_id(topic_id, random.choice(candidates))
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a publish that already happened
        print(f"daily_cycle_handler: could not roll the model forward for topic_id={topic_id}: {exc!r}")


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
    original_body: str | None = None,
    equipment_used: list[dict] | None = None,
    attribution: list[dict] | None = None,
    subject_key: str | None = None,
    rotation: goal_rotation.Pick | None = None,
    on_stored: Callable[[str], None] | None = None,
) -> dict:
    """Store the article and either publish it or send it to moderation.

    `on_stored` is called with the article's id the moment its row is written, before anything
    after it (the static page, the musing, the model's roll forward, the moderation item) has a
    chance to fail. The run stamps the topic there (`_record_article_written`), so a failure in
    those steps cannot leave the article unstamped and its Findings open for the next run to write
    up again, and it keeps the id, so the error it returns names the article.

    `attribution` is the source credit to store on the article and show on its page
    (common/attribution.py); None stores nothing, and readers fall back to the topic's adapter.

    `subject_key` is the day's subject of a topic that rotates (common/day_subject.py), stored on
    the article as `subject` and named in the result, so Re-Write and the operator can see what it
    was about; None (every other topic) stores and returns nothing new.

    `rotation` is what a topic's goal rotation picked (common/goal_rotation.py): the goal's name is
    stored on the article as `editorial_goal`, and a revisit as `revisit`, and both are named in
    the result; None stores and returns nothing new.

    `hold_reasons` are reasons to keep it from publishing regardless of the compliance
    verdict (today: a truncated draft, a title that is not one, a topic with
    `force_manual_review`, and an enforced fresh-data review that held it). Any of them
    sends it to moderation, listed ahead of the compliance reasons, so a person sees why.
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

    # If a revision replaced the draft, keep the original beside it (private) so a
    # moderator can see exactly what was changed.
    body_original_s3_key = None
    if original_body is not None:
        body_original_s3_key = f"articles/{article_id}.original.md"
        s3.put_object(
            Bucket=os.environ["CONTENT_BUCKET"],
            Key=body_original_s3_key,
            Body=original_body.encode("utf-8"),
            ContentType="text/markdown",
        )

    source_refs = []
    for finding in findings:
        source_refs.extend(finding.get("source_refs") or [])
    source_refs = dedupe_source_refs(source_refs)
    # The pictures those findings came with (common/figures.py): the newest few, each once,
    # stored on the article and copied onto its page when it is published.
    figures = figures_for_findings(findings)

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
        **({"body_original_s3_key": body_original_s3_key} if body_original_s3_key else {}),
        **({"equipment_used": equipment_used} if equipment_used is not None else {}),
        **({"attribution": attribution} if attribution is not None else {}),
        **({ARTICLE_SUBJECT_FIELD: subject_key} if subject_key is not None else {}),
        **_rotation_fields(rotation),
        figures=figures,
    )
    if on_stored is not None:
        on_stored(article_id)
    subject = {ARTICLE_SUBJECT_FIELD: subject_key} if subject_key is not None else {}
    subject.update(_rotation_fields(rotation))

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
            fact_check=fact_check_label(fresh_review_record, "ai_only"),
            equipment_used=equipment_used,
            attribution=attribution,
            figures=figures,
        )
        generate_and_store_article_musing(
            article_id=article_id,
            topic_id=topic_id,
            topic_name=topic_name,
            title=title,
            compliant=True,
            model_id=model_id,
        )
        _assign_rotated_model(topic_id)
        return {
            "status": "published",
            "topic_id": topic_id,
            "article_id": article_id,
            "compliant": True,
            **subject,
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
        **subject,
    }
