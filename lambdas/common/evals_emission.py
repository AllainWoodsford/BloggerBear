"""Where continuous evals' events come from: the four points in the pipeline that know a model or
a prompt did well or badly, and the rules they share for what an article's prompts and its model
are (docs/enhancements/model-ranch-prompt-assets-evals.md, sections 4.3 and 5.4).

- **Article Completion** (+2), from daily_cycle_handler's `_stored`, the moment an article's row is
  written, held or published: one event per prompt of the article, `completion:<article_id>:<prompt_id>`.
- **Admin Review** (+2 approved, -2 rejected), from the admin API's approve and reject when a person
  decided: one event per prompt, `admin:<queue_id>:<decision>:<prompt_id>`. Professor Grizzlewick's
  decisions take the same routes and the caller leaves them out: an AI reviewer's verdict is not a
  person's, and only the calibrated judge's verdicts may count towards the gates.
- **Article Rewrite** (-2), from the admin API's rewrite routes when a person sent an article back:
  one event, `rewrite:<rewrite_id>`, for the model whose text it was, naming the prompt that wrote
  the text. His rewrites are left out like his decisions.
- **User Feedback** (+1 or -1, +3 or -3 with a comment the screening kept), from the public API's
  `_submit_feedback`, once the vote is stored and the response built: one event, `vote:<feedback_id>`,
  for the article's model and no prompt. The rollup joins it to the article's completion events by
  subject to reach the prompts' cells (common/evals_rollup.py).

**The article's prompts** are every prompt id its lineage names: its own calls (`lineage.calls`:
ideation, draft, title, the reviews, a revision, a finished rewrite and its reviews) and the research
of the Findings it was written from (`lineage.research.calls`). Two kinds of call are in the lineage
for their cost alone and carry a mark that leaves them out here: every call of a rewrite that failed
(`failed`, common/rewrite.py), whose text was thrown away, so a decision on the text it would have
replaced counts for none of its prompts; and the research of a Finding that was in the article's
window but that its editorial goal set aside (`set_aside`, common/costing.py). A call stored before
these marks existed carries none and counts like any other. A daily-cycle revision the guards turned
down is not marked: it is part of the run that wrote the text. A prompt id gets one event per
article (section 4.3), so when it was sent more than once the event carries the most recent call's
version: for the article's own calls, which are in the order they were made, the last one (a
rewritten article's reviews are those of the text a person now decides on); for its research, the
newest Finding's. A call recorded before prompts were registered names none and has no event.

**The article's model** is the one that wrote its text as it stands: the model of its latest finished
rewrite (`rewrites`), else the model of its draft call. Every event of one article carries that one
model, even when another call fell back to a second model or the Findings were summarised by another:
the scoring table credits *the* model with the article, the rollup gives a model its share once per
article it is named in (evals_rollup.model_shares), so naming each call's own model would credit a
fallback with a whole article for one call; and a reader's vote, which knows nothing of calls,
carries the same model to the cells the rollup joins it to. So a model's cells read "articles this
model wrote, with this prompt version in their lineage", whichever reason they came from. An article
with neither a draft call nor a finished rewrite has no model and no event.

**Never raises, and costs nothing while evals are off.** Each function returns how many events it
wrote. Nothing is read or built while EVALS_ENABLED is off (common/evals.enabled). An event the
schema refuses is skipped with one line naming why (common/evals.event); the first event Logs could
not take stops the rest, so a Logs outage costs one call's timeouts (common/evals.CLIENT_CONFIG), not
one per prompt, and an approve in the admin API still answers well inside API Gateway's 29 seconds.
"""

from __future__ import annotations

from common import evals
from common.model_pricing import canonical_model_id

# The lineage stages that write an article's text: daily_cycle_handler's draft, and a rewrite's
# (common/rewrite.py, REWRITE_STAGE).
DRAFT_STAGE = "draft"
REWRITE_STAGE = "rewrite"
# The marks that leave a lineage call out of an article's prompts and its writer, spelled as their
# writers spell them: every call of a rewrite that failed (common/rewrite.py, FAILED_MARK), and the
# research of a Finding the article's editorial goal set aside (common/costing.py, SET_ASIDE).
FAILED = "failed"
SET_ASIDE = "set_aside"

COMPLETION = "Article Completion"
ADMIN_REVIEW = "Admin Review"
REWRITE = "Article Rewrite"
FEEDBACK = "User Feedback"
# An Admin Review's delta by the decision the queue item took (its new status, as the
# moderation_decision evidence line spells it).
ADMIN_DELTAS = {"approved": 2, "rejected": -2}
VOTES = ("up", "down")
HUMAN = "human"
# Each event's `source`: the code of the Lambda that writes it.
DAILY_CYCLE, ADMIN_API, PUBLIC_API = "daily_cycle", "admin_api", "public_api"


# --- what an article's lineage says -----------------------------------------------------------------


def _calls(block) -> list[dict]:
    """The calls of a lineage block that are the article's: every one but a marked one."""
    calls = block.get("calls") if isinstance(block, dict) else None
    if not isinstance(calls, list):
        return []
    return [c for c in calls if isinstance(c, dict) and not c.get(FAILED) and not c.get(SET_ASIDE)]


def _prompt(call: dict | None) -> tuple[str, str] | None:
    prompt_id, version = (call or {}).get("prompt_id"), (call or {}).get("prompt_version")
    if isinstance(prompt_id, str) and prompt_id and isinstance(version, str) and version:
        return prompt_id, version
    return None


def _model(call: dict) -> str | None:
    model = call.get("model_id")
    return canonical_model_id(model) if isinstance(model, str) and model else None


def article_prompts(lineage) -> list[tuple[str, str]]:
    """[(prompt id, version)], one per prompt id the lineage's unmarked calls name, in the order each
    first appears: the article's own calls, then its research. A prompt sent more than once carries
    the most recent call's version (see the module's docstring). Never raises."""
    try:
        found: dict[str, str] = {}
        for call in _calls(lineage):
            prompt = _prompt(call)
            if prompt:
                found[prompt[0]] = prompt[1]  # in the order made: a later call's version replaces
        newest: dict[str, tuple[str, str]] = {}
        research = lineage.get("research") if isinstance(lineage, dict) else None
        for call in _calls(research):
            prompt = _prompt(call)
            if prompt is None or prompt[0] in found:
                continue
            captured_at = call.get("captured_at") if isinstance(call.get("captured_at"), str) else ""
            kept = newest.get(prompt[0])
            if kept is None or captured_at > kept[0]:
                newest[prompt[0]] = (captured_at, prompt[1])
        found.update({prompt_id: version for prompt_id, (_, version) in newest.items()})
        return list(found.items())
    except Exception as exc:  # noqa: BLE001 - an unreadable lineage names no prompt
        print(f"evals_emission: could not read an article's prompts: {exc!r}")
        return []


def writer(article) -> tuple[str | None, dict | None]:
    """(model, call): the canonical id of the model that wrote `article`'s text as it stands, and the
    lineage call that wrote it (None when it cannot be found). `article` is an Articles item, or a
    dict holding only the `lineage` of an article being stored. The latest finished rewrite's model
    (`rewrites`, which only a finished rewrite is added to) with that model's latest rewrite call,
    which is the finished rewrite's own: a failed rewrite's calls are marked and left out (FAILED),
    and the model match still passes over an unmarked one stored before the mark when another model
    made it. Else the draft call's. (None, None) when neither is recorded. Never raises."""
    try:
        if not isinstance(article, dict):
            return None, None
        calls = _calls(article.get("lineage"))
        rewrites = article.get("rewrites")
        last = rewrites[-1] if isinstance(rewrites, list) and rewrites else None
        rewriter = _model(last) if isinstance(last, dict) else None
        if rewriter:
            found = (c for c in reversed(calls) if c.get("stage") == REWRITE_STAGE and _model(c) == rewriter)
            return rewriter, next(found, None)
        for call in reversed(calls):
            if call.get("stage") == DRAFT_STAGE and _model(call):
                return _model(call), call
        return None, None
    except Exception as exc:  # noqa: BLE001 - an unreadable article has no known writer
        print(f"evals_emission: could not tell which model wrote an article: {exc!r}")
        return None, None


# --- writing ----------------------------------------------------------------------------------------


def _write(reason: str, events: list[dict]) -> int:
    """Emit each of `events` (common/evals.event's keywords) that the schema accepts, stopping at the
    first one Logs could not take. How many were written."""
    accepted = []
    for fields in events:
        record, refused = evals.event(reason, **fields)
        if record is None:
            print(f"evals_emission: {reason} event refused ({refused})")
        else:
            accepted.append(fields)
    written = 0
    for fields in accepted:
        if not evals.emit(reason, **fields):
            break  # Logs is not taking events: the rest would wait as long and be lost the same
        written += 1
    return written


def _per_prompt(reason: str, article: dict, event_id_of, **fields) -> int:
    """One `reason` event per prompt of `article`, each carrying the article's model."""
    model, _ = writer(article)
    prompts = article_prompts(article.get("lineage"))
    if model is None or not prompts:
        print(f"evals_emission: no {reason} event: the article names no writing model or no prompt")
        return 0
    events = [
        {"event_id": event_id_of(prompt_id), "prompt_id": prompt_id, "prompt_version": version}
        for prompt_id, version in prompts
    ]
    return _write(reason, [{**event, "model_id": model, **fields} for event in events])


def completion(*, article_id: str, topic_id: str, lineage: dict) -> int:
    """One Article Completion per prompt of the article just stored, from the `lineage` stored on it.
    Never raises."""
    try:
        if not evals.enabled():
            return 0
        return _per_prompt(
            COMPLETION,
            {"lineage": lineage},
            lambda prompt_id: evals.event_id(COMPLETION, article_id=article_id, prompt_id=prompt_id),
            subject=article_id,
            topic_id=topic_id,
            source=DAILY_CYCLE,
        )
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the work
        print(f"evals_emission: could not emit article_id={article_id}'s completion events: {exc!r}")
        return 0


def admin_review(article: dict | None, *, article_id: str, queue_id: str, decision: str, topic_id) -> int:
    """One Admin Review per prompt of `article` (the Articles item of `article_id`) for a person's
    `decision` on queue item `queue_id`: +2 `approved`, -2 `rejected`. The caller leaves Professor
    Grizzlewick's decisions out. Never raises."""
    try:
        if not evals.enabled() or decision not in ADMIN_DELTAS or not isinstance(article, dict):
            return 0
        return _per_prompt(
            ADMIN_REVIEW,
            article,
            lambda prompt_id: evals.event_id(
                ADMIN_REVIEW, queue_id=queue_id, decision=decision, prompt_id=prompt_id
            ),
            rating_delta=ADMIN_DELTAS[decision],
            subject=article_id,
            topic_id=topic_id,
            source=ADMIN_API,
            by=HUMAN,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"evals_emission: could not emit queue_id={queue_id}'s admin review events: {exc!r}")
        return 0


def article_rewrite(load_article, *, article_id: str, rewrite_id: str, topic_id) -> int:
    """One Article Rewrite (-2) for the model that wrote article `article_id`, which a person sent
    back to be rewritten (`load_article()` reads its item, only while evals are on). The event names
    the prompt that wrote the text, with its version, when the writing call records one: a model-only
    reason still counts in that model's cell for the prompt (section 3, deviation 15). The caller
    leaves Professor Grizzlewick's rewrites out. Never raises."""
    try:
        if not evals.enabled():
            return 0
        model, call = writer(load_article())
        if model is None:
            print(f"evals_emission: no {REWRITE} event: the article names no writing model")
            return 0
        fields = {
            "event_id": evals.event_id(REWRITE, rewrite_id=rewrite_id),
            "model_id": model,
            "subject": article_id,
            "topic_id": topic_id,
            "source": ADMIN_API,
            "by": HUMAN,
        }
        prompt = _prompt(call)
        if prompt:
            fields.update(prompt_id=prompt[0], prompt_version=prompt[1])
        return _write(REWRITE, [fields])
    except Exception as exc:  # noqa: BLE001
        print(f"evals_emission: could not emit rewrite_id={rewrite_id}'s rewrite event: {exc!r}")
        return 0


def feedback_delta(vote: str, comment_kept: bool) -> int:
    """A reader's vote as a delta: +1 up, -1 down, three times that with a comment the screening kept."""
    return (1 if vote == "up" else -1) * (3 if comment_kept else 1)


def user_feedback(article: dict, *, article_id: str, feedback_id: str, vote: str, comment_kept: bool) -> int:
    """One User Feedback for the model of `article` (the Articles item of `article_id`, which the
    route has already read), and no prompt: the rollup reaches the prompts' cells by joining it to the
    article's completion events by subject. Never raises."""
    try:
        if not evals.enabled() or vote not in VOTES or not isinstance(article, dict):
            return 0
        model, _ = writer(article)
        if model is None:
            print(f"evals_emission: no {FEEDBACK} event: the article names no writing model")
            return 0
        fields = {
            "event_id": evals.event_id(FEEDBACK, feedback_id=feedback_id),
            "model_id": model,
            "rating_delta": feedback_delta(vote, comment_kept),
            "subject": article_id,
            "topic_id": article.get("topic_id"),
            "source": PUBLIC_API,
        }
        return _write(FEEDBACK, [fields])
    except Exception as exc:  # noqa: BLE001
        print(f"evals_emission: could not emit feedback_id={feedback_id}'s feedback event: {exc!r}")
        return 0
