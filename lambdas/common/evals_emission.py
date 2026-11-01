"""Where continuous evals' events come from: the four points in the pipeline that know a model or
a prompt did well or badly, and the rules they share for what an article's prompts and its model
are (docs/enhancements/archive/model-ranch-prompt-assets-evals.md, sections 4.3 and 5.4).

- **Article Completion** (+2), from daily_cycle_handler's `_stored`, the moment an article's row is
  written, held or published: one event per prompt of the article, `completion:<article_id>:<prompt_id>`.
- **Admin Review** (+2 approved, -2 rejected), from the admin API's approve and reject when a person
  decided: one event per prompt, `admin:<queue_id>:<decision>:<prompt_id>`. Professor Grizzlewick's
  decisions take the same routes and the caller leaves them out: an AI reviewer's verdict is not a
  person's, and only the calibrated judge's verdicts may count towards the gates. The decision is
  also the person's verdict on the article (good approved, bad rejected), kept as the evals table's
  `pair#<article_id>` row `human` (section 4.4): the rollup measures the judge's agreement with people
  from the subjects that have both a judge's row and a person's (common/evals_rollup.judge_agreement),
  so calibration accrues from the decisions people make anyway. The latest decision on an article is
  the one kept while the text is the one the judge judged: a held article that comes back to the
  inbox is decided again on what it says then. A decision on text the judge never saw keeps no pair
  (human_article_pair): once a rewrite was saved after the judge's mark (`eval_judged_at`), the
  person's verdict would be paired with the judge's on the text before, so it is skipped, and a human
  row already there, given on the text the judge saw, stays. That narrows the spec entry's "also write
  pair#human" on purpose, so agreement is only ever measured on one text. With no mark yet the
  decision is kept: the judge judges the text as it stands when it runs, which is the text decided
  unless a rewrite is saved first. Both rows record which text they were given on (`rewrite_number`:
  how many rewrites were saved before it), and the rollup pairs only rows of the same text, so in that
  order the person's row stays unpaired until they decide the text the judge saw.
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

import os
from datetime import UTC, datetime

from common import dynamo, evals
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
# The evals table's pair rows (section 4.4): entity `pair#<subject>`, one row per role (`judge`,
# `human`) holding that role's verdict on the subject. The prefix is the rollup's evals_rollup.PAIR,
# spelled here because this module is imported by the public API, which needs nothing else of the
# rollup's.
PAIR = "pair#"
# On an article's pair rows: the text the verdict was given on (rewrite_number). The rollup reads it as
# evals_rollup.REWRITE_NUMBER.
REWRITE_NUMBER = "rewrite_number"
# A person's decision on a queue item as their verdict on the article.
VERDICTS = {"approved": "good", "rejected": "bad"}


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
    `decision` on queue item `queue_id`: +2 `approved`, -2 `rejected`; and the person's verdict on
    the article as its `pair#<article_id>` row `human` (human_article_pair). The caller leaves
    Professor Grizzlewick's decisions out, so he never writes a person's verdict. Never raises."""
    try:
        if not evals.enabled() or decision not in ADMIN_DELTAS or not isinstance(article, dict):
            return 0
        written = _per_prompt(
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
        written = 0
    human_article_pair(article, article_id=article_id, queue_id=queue_id, decision=decision)
    return written


# --- a person's verdict, for the judge's calibration -------------------------------------------------


def evals_table():
    return dynamo.get_table(os.environ["EVALS_TABLE"])


def pair_key(subject: str, role: str) -> dict:
    return {"entity": f"{PAIR}{subject}", "sk": role}


def rewrite_number(article) -> int:
    """Which text of `article` a verdict is given on: how many rewrites were saved before it (its
    `rewrites`, which only a finished rewrite is added to, common/rewrite.py), 0 for the text the daily
    cycle wrote."""
    rewrites = article.get("rewrites") if isinstance(article, dict) else None
    return len(rewrites) if isinstance(rewrites, list) else 0


def pair_row(subject: str, role: str, verdict: str, ts: str, *, rewrites: int | None = None) -> dict:
    """A pair row as the rollup reads it: `verdict` (good or bad), its `delta` (section 4.3's two
    either way) and when it was given (`ts`); for an article, `rewrites` is the text it was given on
    (rewrite_number), kept as REWRITE_NUMBER, since the rollup pairs a judge's verdict with a person's
    only on the same text (evals_rollup.judge_agreement). A Finding is never rewritten and keeps none."""
    row = {**pair_key(subject, role), "verdict": verdict, "delta": 2 if verdict == "good" else -2, "ts": ts}
    if rewrites is not None:
        row[REWRITE_NUMBER] = rewrites
    return row


def pair_time(now: datetime) -> str:
    """A pair row's `ts`: UTC to the second, as the judge writes its own (common/eval_judge.py)."""
    return now.astimezone(UTC).isoformat(timespec="seconds")


def _moment(value) -> datetime | None:
    """An ISO time as an aware datetime; None when it is missing or not one. Parsed rather than
    compared as text: the judge's mark is to the second (common/eval_judge.py), a saved rewrite's
    `finished_at` carries microseconds (common/rewrite.py)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rewritten_since_judged(article) -> bool:
    """Whether a rewrite of `article` was saved after the judge's mark (`eval_judged_at`), so the text
    a person decides now is not the one the judge judged. False when either time is missing or does
    not parse: the decision is then kept, as it was before the check."""
    judged = _moment(article.get("eval_judged_at")) if isinstance(article, dict) else None
    rewrites = article.get("rewrites") if judged is not None else None
    if not isinstance(rewrites, list):
        return False
    finished = (_moment(entry.get("finished_at")) for entry in rewrites if isinstance(entry, dict))
    return any(moment is not None and moment > judged for moment in finished)


def human_article_pair(article: dict | None, *, article_id: str, queue_id: str, decision: str) -> bool:
    """A person's decision on `article` as their verdict, the `pair#<article_id>` row `human`: good for
    `approved`, bad for `rejected`, with the queue item it was decided on and the text it was decided on
    (rewrite_number). Written over the row already there, so the latest decision is the one kept, but
    never on text rewritten since the judge's mark (_rewritten_since_judged): that verdict is not kept
    as a pair, and the row already there is left alone (see the module's docstring). Only while evals
    are on, and only from admin_review, which is called for a person's decisions alone. True when it
    was written. Never raises."""
    try:
        if not evals.enabled() or decision not in VERDICTS:
            return False
        if _rewritten_since_judged(article):
            print(
                f"evals_emission: queue_id={queue_id} decided article_id={article_id} after a rewrite "
                "the judge has not judged: no human verdict kept"
            )
            return False
        ts = pair_time(datetime.now(UTC))
        row = pair_row(article_id, HUMAN, VERDICTS[decision], ts, rewrites=rewrite_number(article))
        evals_table().put_item(Item={**row, "queue_id": queue_id})
        return True
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the work
        print(f"evals_emission: could not keep queue_id={queue_id}'s decision as a human verdict: {exc!r}")
        return False


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
