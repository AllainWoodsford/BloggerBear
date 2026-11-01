"""Offline scoring runs (docs/enhancements/archive/model-ranch-prompt-assets-evals.md, sections 2, 4.3 to 4.5,
4.7, 5.3 and 5.4's offline run): an operator asks how other models, or other versions of a prompt,
would have written past articles, and the judge says, item by item, whether each would have been
better or worse than what the live version writes. Nothing is served and no prompt asset is touched:
a candidate version is read from the prompts bucket, never written (common/prompts.py), and a run
only drafts, judges and records on the evals table.

**What is scored.** `daily_cycle/draft`, the prompt that writes an article (SCORABLE). An item is a
past article with the Findings it was written from: its draft prompt is rebuilt with the values and
locked blocks the daily cycle gave it (daily_cycle_handler.rebuild_draft_prompt), so every candidate
is asked for the very draft the article was. No other prompt is scored offline yet: an item has to be
rebuilt as its own call site built it, and the comparison rubric reads articles.

**Combinations** multiply: each model named with each version named, up to MAX_COMBINATIONS. A
version is `vN`, published in this deployment (a file under versions/ whose hash row names this prompt
and version, live or retired: prompts.published_version, whose sha256 the run records), so a version a
later one replaced can still be scored against it; or `vanilla`, the code's own (recorded as
`vanilla:v1`).

**The request** (`start`; the admin API's POST /evals/runs, `admin_cli evals score`): the judge is none
of the candidate models; the judge and every candidate are in the registry, enabled and text models
(model_fleet.suitable(TEXT)); the sample is 1 to 50 items. The items are the newest articles of the last
ITEM_LOOKBACK that can be rebuilt (their Findings still there, the model that drafted them recorded, fit
to draft and not the judge), one topic at a time, so a busy topic does not fill the sample. The estimate
is, for each item and combination, two drafts (the candidate's, and the live version's on the model that
drafted the article) and one judge call, each priced from the registry (eval_judge.call_cost_aud). It is
an estimate, not a bound: it counts the live version's draft for every combination though it is made once
per item, and it leaves out the one retry, with more tokens, of a draft cut off at its limit (both
attempts are billed, so that draft costs about two and a half times its estimate). A run is refused
when its estimate is more than today's cap leaves (eval_judge.remaining_aud, the day row the judge spends
against), when it is more than `score_max_cost_aud` without `yes`, and when it records without `yes`. A
`dry_run` answers the plan and writes nothing: the CLI shows it, with the environment, and asks before
it starts the run.

**The run** is the evals table's `run#<run_id>` (section 4.4): its header (`meta`: status, estimate_aud,
spent_aud, created_at, the combinations and the items) and a row per item and combination,
`item#<n>#<combination>` (outcome, verdict, delta, tokens, cost_aud). One `score` message per item and
combination goes to eval-work (section 4.7), combination by combination, so two messages of one item
seldom run at once.

**The consumer** (`consume`, one message): reads the run's header first (a cancelled run's message is
dropped; one that is not the run's own combination or item is refused), holds the day's cap, checked
against the most the message can cost, a retried draft included (at the cap the run is paused and the
message sent back to wait 900 s: eval_judge._pause; two messages scored at once, as eval-work's mapping
runs two, can pass the check together, so a day can end one message over its cap), claims the item's row
(a copy of the message that comes while it is claimed is dropped, and one that comes after it has its
result counts it once), rebuilds the item's draft prompt, drafts the live version once per item
(`live#<n>`, kept on the run, so every combination of the item is compared with the same draft), renders
the candidate (its versions/ file, or the vanilla), drafts with the candidate model, and asks the judge,
which is neither model, which of the two drafts is better, in an order drawn per item. The verdict is
good (+2) when the candidate's is, bad (-2) when the live version's is. With `record`, it writes one
Offline Review event for the candidate model and prompt version (`offline:<run_id>:<n>:<combination>`,
by judge), which gates only while the judge is calibrated (common/evals.py's SCORING), after the row and
before the message is counted finished. Its spend goes to the same day row, the run and the Stats page's
evals category as the judge's (eval_judge._record_spend).

**Cancel and resume.** `cancel` marks a queued, running or paused run cancelled; its messages are
dropped as they come. `resume` sends again every message of a scoring run that has no result and is not
being scored (a paused run's, a cancelled run's, one whose send failed, one that failed), under the same
cap check as a new run; a copy of one still on the queue is dropped when it comes, once the other has its
result.

Only codes, ids and numbers are ever printed. `consume` returns what became of its message. A model call
that fails, or a failure it does not expect once the row is claimed, is recorded as the message's
outcome (`error`), so the run still closes; an error is no result, so `resume` sends the message again
and the retry keeps what the failed attempt spent on the row. A result already written is never
rewritten by a failure after it (counting the message finished, say): that is tried again instead. A
consumer that dies leaves its claim, which goes stale after CLAIM_STALE.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from boto3.dynamodb.conditions import Attr, Key

from common import (
    dynamo,
    eval_judge,
    evals,
    evals_rollup,
    evals_settings,
    evidence,
    model_fleet,
    prompts,
    rewrite,
)
from common.bedrock import invoke_model_tracked
from common.fresh_review import resolve_review_steering
from common.model_pricing import canonical_model_id
from common.relevance import topic_label

SCORE_JOB = eval_judge.SCORE_JOB
REASON = "Offline Review"
DRAFT_PROMPT_ID = "daily_cycle/draft"
# The prompts an item can be rebuilt for (see the module's docstring).
SCORABLE = (DRAFT_PROMPT_ID,)
COMPARE_PROMPT_ID = "eval_score/compare"
VANILLA = "vanilla"
_VERSION = re.compile(r"v[1-9][0-9]{0,5}")

FIELDS = ("judge_model_id", "model_ids", "prompt_id", "versions", "sample", "record", "yes", "dry_run")
MAX_SAMPLE = 50
MAX_MODELS = 5
MAX_VERSIONS = 5
MAX_COMBINATIONS = 10
# Findings live 14 days, so an older article can no longer be rebuilt.
ITEM_LOOKBACK = timedelta(days=14)
# How many of each status's newest articles the sample reads, and how many it looks at per place: an
# article that cannot be rebuilt is passed over, and a sample of few eligible articles stops early.
ARTICLES_READ = 200
SAMPLE_TRIES = 3

# The estimate's calls. A draft's prompt is at most the findings block (SUMMARIES_MAX_CHARS, 40,000
# characters, about 10,000 tokens) and the template around it; its reply, on the first attempt, at most
# the draft's own limit (daily_cycle_handler.DRAFT_MAX_TOKENS). A reply cut off there is asked for once
# more with DRAFT_RETRY_MAX_TOKENS, and invoke_model_tracked bills both attempts, so a truncated draft
# costs about two and a half times its estimate. A run is priced at one attempt (_draft_tokens): most
# drafts need no retry. Each message's check against the day's cap counts the retry (_draft_ceiling_tokens),
# so no one message takes the day past it. The judge reads both drafts (DRAFT_MAX_CHARS each) and the
# findings (FINDINGS_MAX_CHARS): under 9,000 tokens; its reply is a verdict.
DRAFT_ESTIMATE_INPUT_TOKENS = 12_000
COMPARE_ESTIMATE_INPUT_TOKENS = 9_000
COMPARE_MAX_TOKENS = eval_judge.JUDGE_MAX_TOKENS
# What the rubric shows of each draft and of the findings.
DRAFT_MAX_CHARS = eval_judge.ARTICLE_MAX_CHARS
FINDINGS_MAX_CHARS = 8_000

# A claim older than this is taken to be a consumer that died (eval-jobs' timeout is 900 s), so a copy
# of the message may take it over.
CLAIM_STALE = timedelta(minutes=20)

# The rows of a run beside its header (section 4.4): an item's result per combination, and the live
# version's draft of an item, made once.
ITEM, LIVE = "item#", "live#"
# A row's outcome: claimed and being scored; scored (a verdict); a model call, or something unexpected,
# failed (an error is no result: `resume` sends its message again, and the copy takes the row over); or
# why there is no verdict, which is final.
SCORING, SCORED, ERROR = "scoring", "scored", "error"

# The reply contract (the rubric's locked `reply_rule`): one JSON object of exactly these two keys.
POSITIONS = ("A", "B")
REASON_CODES = ("accuracy", "support", "data_reading", "speculation", "relevance", "clarity", "advice")
REPLY_RULE = (
    'Reply with one JSON object and nothing else: {"better": "A" or "B", "reason_code": the one code '
    "that best names what most separates the two drafts, from "
    + ", ".join(f'"{code}"' for code in REASON_CODES)
    + "}."
)
# Every delimiter the rubric wraps material in; a closing tag inside the material is broken.
_DELIMITER_TAG = re.compile(r"<(/?)(findings|draft_a|draft_b|operator_note)", re.IGNORECASE)


class Refused(Exception):
    """A request the admin API answers with `status`, `message` and anything in `extra` beside it."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def _daily_cycle():
    """The daily cycle's module, where an article's draft prompt is built and rebuilt. Imported when a
    run needs it: it loads the whole pipeline, which the admin API's other routes never need."""
    import daily_cycle_handler

    return daily_cycle_handler


def _model_id(value) -> str | None:
    if not isinstance(value, str) or value.endswith("\n"):
        return None
    return evidence.check("model", value)


def _bool(body: dict, name: str) -> bool:
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise Refused(400, f"'{name}' must be true or false")
    return value


def _number(value) -> float:
    return float(value or 0)


def _run_id(value) -> str:
    run_id = eval_judge._id(value)
    if run_id is None:
        raise Refused(400, "a run id is letters, digits, '.', ':', '_' and '-'")
    return run_id


# --- the request ----------------------------------------------------------------------------------------


def parse_request(body) -> dict:
    """The request's fields, checked as far as they can be without a read; Refused (400) otherwise."""
    if not isinstance(body, dict):
        raise Refused(400, f"send a JSON object with: {', '.join(FIELDS)}")
    unknown = sorted(set(body) - set(FIELDS))
    if unknown:
        raise Refused(400, f"unknown field '{unknown[0]}': send any of {', '.join(FIELDS)}")
    judge = _model_id(body.get("judge_model_id"))
    if judge is None:
        raise Refused(400, "'judge_model_id' must be a model id")
    models = body.get("model_ids")
    if not isinstance(models, list) or not 1 <= len(models) <= MAX_MODELS:
        raise Refused(400, f"'model_ids' must be a list of 1 to {MAX_MODELS} model ids")
    candidates: list[str] = []
    for value in models:
        model_id = _model_id(value)
        if model_id is None:
            raise Refused(400, "'model_ids' must hold model ids")
        if canonical_model_id(model_id) in {canonical_model_id(seen) for seen in candidates}:
            raise Refused(400, f"'{model_id}' is named twice in 'model_ids'")
        candidates.append(model_id)
    # The judge is never an author (section 3, deviation 9): it compares each candidate model's draft.
    if canonical_model_id(judge) in {canonical_model_id(model_id) for model_id in candidates}:
        raise Refused(400, f"the judge '{judge}' is one of the models it would score: name another judge")
    prompt_id = body.get("prompt_id")
    if prompt_id not in SCORABLE:
        scorable = ", ".join(SCORABLE)
        raise Refused(400, f"'prompt_id' must be one of {scorable}: the prompts a past item is rebuilt for")
    versions = body.get("versions")
    if not isinstance(versions, list) or not 1 <= len(versions) <= MAX_VERSIONS:
        raise Refused(400, f"'versions' must be a list of 1 to {MAX_VERSIONS} versions (vN, or vanilla)")
    vanilla_label = prompts.spec_for(prompt_id).vanilla_label
    for version in versions:
        if not isinstance(version, str) or not (
            version in (VANILLA, vanilla_label) or _VERSION.fullmatch(version)
        ):
            raise Refused(400, f"each of 'versions' is vN (published here: live or retired) or {VANILLA}")
    labels = [vanilla_label if version == VANILLA else version for version in versions]
    if len(set(labels)) != len(labels):
        raise Refused(400, "a version is named twice in 'versions'")
    if len(candidates) * len(labels) > MAX_COMBINATIONS:
        raise Refused(400, f"models x versions must be at most {MAX_COMBINATIONS} combinations")
    sample = body.get("sample")
    if isinstance(sample, bool) or not isinstance(sample, int) or not 1 <= sample <= MAX_SAMPLE:
        raise Refused(400, f"'sample' must be a whole number from 1 to {MAX_SAMPLE}")
    record, yes, dry_run = _bool(body, "record"), _bool(body, "yes"), _bool(body, "dry_run")
    if record and not yes and not dry_run:
        raise Refused(400, "'record' writes Offline Review events, which can gate models: it needs 'yes'")
    return {
        "judge_model_id": judge,
        "model_ids": candidates,
        "prompt_id": prompt_id,
        "versions": labels,
        "sample": sample,
        "record": record,
        "yes": yes,
        "dry_run": dry_run,
    }


def model_problem(model_id: str, role: str) -> str | None:
    """Why `model_id` cannot be a run's `role` (judge or candidate), or None: it must be in the
    registry (the estimate is priced from it), enabled and callable, and a text model."""
    row = dynamo.get_model(model_id)
    if row is None and canonical_model_id(model_id) != model_id:
        row = dynamo.get_model(canonical_model_id(model_id))
    if row is None:
        return f"the {role} '{model_id}' is not in the model registry (see `models list`)"
    if not model_fleet.is_usable(row):
        return f"the {role} '{model_id}' is disabled or cannot be called"
    if not model_fleet.suitable(row, model_fleet.TEXT):
        return f"the {role} '{model_id}' is not a text model"
    return None


def resolve_versions(prompt_id: str, labels: list[str]) -> dict[str, str | None]:
    """Each version's sha256 (None for the vanilla): a `vN` must be one this deployment published, live
    or retired (prompts.published_version)."""
    vanilla_label = prompts.spec_for(prompt_id).vanilla_label
    resolved: dict[str, str | None] = {}
    for label in labels:
        if label == vanilla_label:
            resolved[label] = None
            continue
        if not prompts.store_named():
            raise Refused(400, f"this deployment names no prompts bucket: only {VANILLA} can be scored")
        try:
            sha = prompts.published_version(prompt_id, label)
        except Exception as exc:  # noqa: BLE001 - the request is refused, never half made
            print(f"eval_score: could not read the prompts bucket or the prompt-hash table: {exc!r}")
            raise Refused(502, "could not read the prompts bucket or the prompt-hash table") from exc
        if sha is None:
            raise Refused(
                400,
                f"{prompt_id} {label} was never published here (no live or retired row in the "
                f"prompt-hash table names it): name a published one or {VANILLA}",
            )
        resolved[label] = sha
    return resolved


def combinations(model_ids: list[str], prompt_id: str, labels: list[str]) -> list[dict]:
    """Every model with every version, model by model: each `{code, model_id, prompt_versions}`, where
    the code (`<model>@<prompt_id>=<version>`) names the combination in its rows and event ids."""
    return [
        {
            "code": f"{canonical_model_id(model_id)}@{prompt_id}={label}",
            "model_id": model_id,
            "prompt_versions": {prompt_id: label},
        }
        for model_id in model_ids
        for label in labels
    ]


def draft_model(article: dict) -> str | None:
    """The model that drafted `article` (its lineage's draft call), as it was called."""
    lineage = article.get("lineage")
    calls = lineage.get("calls") if isinstance(lineage, dict) else None
    calls = calls if isinstance(calls, list) else []
    drafts = [call for call in calls if isinstance(call, dict) and call.get("stage") == "draft"]
    model_id = drafts[-1].get("model_id") if drafts else None
    return model_id if isinstance(model_id, str) and model_id else None


def _eligible(article: dict, judge: str, topics: dict) -> dict | None:
    """What a message carries of an article that can be an item, with the model the live version is
    drafted on; None for one that cannot."""
    topic_id, article_id = eval_judge._id(article.get("topic_id")), eval_judge._id(article.get("article_id"))
    live_model = draft_model(article)
    if topic_id is None or article_id is None or live_model is None:
        return None
    # The judge is neither model it compares; the live version is drafted on a model still fit to draft.
    if canonical_model_id(live_model) == canonical_model_id(judge) or not eval_judge._fit(live_model):
        return None
    if topic_id not in topics:
        topics[topic_id] = dynamo.get_topic(topic_id)
    topic = topics[topic_id]
    if topic is None or not rewrite.findings_for(article, topic):
        return None
    return {"topic_id": topic_id, "article_id": article_id, "live_model_id": live_model}


def sample_items(judge: str, sample: int, now: datetime) -> list[dict]:
    """Up to `sample` items, numbered from 0: the newest articles of the last ITEM_LOOKBACK, awaiting
    moderation or published, that can be rebuilt, taking each topic's newest in turn."""
    since = eval_judge._iso(now - ITEM_LOOKBACK)
    found: list[dict] = []
    for status in eval_judge.ARTICLE_STATUSES:
        found += eval_judge._query(
            eval_judge._articles_table(),
            ARTICLES_READ,
            IndexName=dynamo.ARTICLES_BY_STATUS_INDEX,
            KeyConditionExpression=Key("status").eq(status) & Key("created_at").gte(since),
        )
    by_topic: dict[str, list[dict]] = {}
    def newest(article: dict) -> tuple[str, str]:
        return str(article.get("created_at", "")), str(article.get("article_id"))

    newest_first = sorted(found, key=newest, reverse=True)
    for article in newest_first:
        by_topic.setdefault(str(article.get("topic_id")), []).append(article)
    topics: dict[str, dict | None] = {}
    picked: list[dict] = []
    for looked, article in enumerate(eval_judge._in_turn(list(by_topic.values()))):
        if len(picked) >= sample or looked >= sample * SAMPLE_TRIES:
            break
        item = _eligible(article, judge, topics)
        if item is not None:
            picked.append({"n": len(picked), **item})
    return picked


def _draft_tokens() -> tuple[int, int]:
    """A draft as a run's estimate prices it: one attempt."""
    return DRAFT_ESTIMATE_INPUT_TOKENS, _daily_cycle().DRAFT_MAX_TOKENS


def _draft_ceiling_tokens() -> tuple[int, int]:
    """The most a draft can bill: a reply cut off at its limit and asked for again with more tokens,
    both attempts billed (invoke_model_tracked)."""
    cycle = _daily_cycle()
    return 2 * DRAFT_ESTIMATE_INPUT_TOKENS, cycle.DRAFT_MAX_TOKENS + cycle.DRAFT_RETRY_MAX_TOKENS


def estimate_aud(judge: str, combos: list[dict], items: list[dict]) -> float:
    """Section 5.4's estimate: for each item and combination, two drafts (the candidate model's and the
    live version's on the item's model) and one judge call, priced from the registry."""
    draft_in, draft_out = _draft_tokens()
    prices: dict[str, float] = {}

    def draft(model_id: str) -> float:
        if model_id not in prices:
            prices[model_id] = eval_judge.call_cost_aud(model_id, draft_in, draft_out)
        return prices[model_id]

    compare = eval_judge.call_cost_aud(judge, COMPARE_ESTIMATE_INPUT_TOKENS, COMPARE_MAX_TOKENS)
    return sum(
        draft(combo["model_id"]) + draft(item["live_model_id"]) + compare
        for item in items
        for combo in combos
    )


# --- starting a run -------------------------------------------------------------------------------------


def _queue_url() -> str:
    queue_url = os.environ.get("EVAL_WORK_QUEUE_URL") or ""
    if os.environ.get("EVALS_ENABLED") != "true" or not queue_url:
        raise Refused(409, "evals are off (EVALS_ENABLED): there is no eval-work queue to score on")
    return queue_url


def _message(run_id: str, item: dict, combo: dict) -> str:
    """One `score` message (section 4.7): ids only."""
    carried = {"n": int(item["n"]), "topic_id": str(item["topic_id"]), "article_id": str(item["article_id"])}
    body = {
        "job": SCORE_JOB,
        "run_id": run_id,
        "item": carried,
        "combination": {"model_id": combo["model_id"], "prompt_versions": combo["prompt_versions"]},
    }
    return json.dumps(body, separators=(",", ":"))


def _send(queue_url: str, run_id: str, meta: dict, numbers: list[int], sqs) -> set[int]:
    """Send the messages numbered `numbers` (combination k, item n is k * items + n), ten at a time.
    The numbers sent; a batch that fails is printed and ends the sending."""
    client = sqs or eval_judge._sqs()
    combos, items = meta["combinations"], meta["articles"]
    count = len(items)
    sent: set[int] = set()
    for start in range(0, len(numbers), 10):
        entries = [
            {"Id": str(m), "MessageBody": _message(run_id, items[m % count], combos[m // count])}
            for m in numbers[start : start + 10]
        ]
        try:
            answer = client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        except Exception as exc:  # noqa: BLE001 - what was not sent waits for `evals runs resume`
            print(f"eval_score: could not send messages of run {run_id}: {exc!r}")
            break
        sent |= {int(entry["Id"]) for entry in answer.get("Successful", [])}
        if answer.get("Failed"):
            print(f"eval_score: eval-work refused {len(answer['Failed'])} message(s) of run {run_id}")
    return sent


def _count_unsent(table, run_id: str, unsent: set[int], now: datetime) -> None:
    """Messages that were never sent count finished, unscored, so the run still closes; `resume`
    sends them again, since they have no result."""
    if not unsent:
        return
    attributes = table.update_item(
        Key=eval_judge._run_key(run_id),
        UpdateExpression="ADD finished_items :unsent",
        ExpressionAttributeValues={":unsent": unsent},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    eval_judge._close_if_finished(table, run_id, attributes, now)


def start(body, *, now: datetime | None = None, sqs=None) -> tuple[int, dict]:
    """POST /evals/runs: check the request, choose its items, price it and, unless it is a dry run, write
    the run's header and send its messages. (status, answer); Refused when it is refused."""
    now = now or datetime.now(UTC)
    request = parse_request(body)
    queue_url = _queue_url()
    judge = request["judge_model_id"]
    for model_id, role in [(judge, "judge"), *((m, "candidate model") for m in request["model_ids"])]:
        problem = model_problem(model_id, role)
        if problem:
            raise Refused(400, problem)
    shas = resolve_versions(request["prompt_id"], request["versions"])
    combos = combinations(request["model_ids"], request["prompt_id"], request["versions"])
    if any(evals.event_id(REASON, run_id="r", item="0", combo=combo["code"]) is None for combo in combos):
        raise Refused(400, "a model id is too long to name a combination")
    items = sample_items(judge, request["sample"], now)
    if not items:
        raise Refused(
            409, "no article of the last 14 days can be drafted again: none whose Findings are still there "
            "and whose draft's model is recorded, fit to draft and not the judge"
        )
    estimate = estimate_aud(judge, combos, items)
    settings = evals_settings.get_settings()
    table = evals_rollup.evals_table()
    remaining = eval_judge.remaining_aud(table, now, settings)
    plan = {
        "environment": os.environ.get("ENVIRONMENT_NAME"),
        "prompt_id": request["prompt_id"],
        "judge_model_id": judge,
        "combinations": [combo["code"] for combo in combos],
        "sample": request["sample"],
        "items": len(items),
        "messages": len(items) * len(combos),
        "estimate_aud": round(estimate, 4),
        "remaining_aud": round(max(remaining, 0.0), 4),
        "score_max_cost_aud": settings["score_max_cost_aud"],
        "record": request["record"],
    }
    if request["dry_run"]:
        return 200, {"dry_run": True, **plan}
    if estimate > remaining:
        raise Refused(409, "the estimate is more than today's evals spend cap leaves; try tomorrow, or "
                      "raise it with `evals set --daily-spend-cap`", **plan)  # fmt: skip
    if estimate > float(settings["score_max_cost_aud"]) and not request["yes"]:
        raise Refused(409, "the estimate is more than score_max_cost_aud: send yes to run it anyway",
                      confirm_required=True, **plan)  # fmt: skip
    run_id = f"score-{now.astimezone(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    meta = {
        **eval_judge._run_key(run_id),
        "job": SCORE_JOB,
        "trigger": "admin",
        "status": eval_judge.QUEUED,
        # What the run waits for before it closes (eval_judge._close_if_finished): every message.
        "items": plan["messages"],
        "messages": plan["messages"],
        "item_count": len(items),
        "sample": request["sample"],
        "prompt_id": request["prompt_id"],
        "judge_model_id": judge,
        "combinations": combos,
        "versions_sha256": {label: sha for label, sha in shas.items() if sha is not None},
        "articles": items,
        "record": request["record"],
        "estimate_aud": Decimal(str(round(estimate, 6))),
        "spent_aud": Decimal("0"),
        "created_at": eval_judge._iso(now),
    }
    table.put_item(Item=meta, ConditionExpression="attribute_not_exists(entity)")
    numbers = list(range(plan["messages"]))
    sent = _send(queue_url, run_id, meta, numbers, sqs)
    _count_unsent(table, run_id, set(numbers) - sent, now)
    print(f"eval_score: run {run_id} sent {len(sent)} of {len(numbers)} message(s)")
    if not sent:
        again = f"`evals runs resume {run_id}` sends them again"
        raise Refused(502, f"eval-work took none of the run's messages; {again}", run_id=run_id, **plan)
    return 202, {"run_id": run_id, **plan, "sent": len(sent)}


# --- reading, cancelling and resuming runs --------------------------------------------------------------


def _meta(table, run_id: str) -> dict | None:
    return table.get_item(Key=eval_judge._run_key(run_id), ConsistentRead=True).get("Item")


def _rows(table, run_id: str, prefix: str) -> list[dict]:
    """A run's rows whose sort key starts with `prefix`, every page."""
    kwargs = {
        "KeyConditionExpression": Key("entity").eq(f"{eval_judge.RUN}{run_id}")
        & Key("sk").begins_with(prefix),
        "ConsistentRead": True,
    }
    rows: list[dict] = []
    while True:
        page = table.query(**kwargs)
        rows += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _summary(meta: dict) -> dict:
    """A run's header as `evals runs list` shows it."""
    return {
        "run_id": str(meta["entity"])[len(eval_judge.RUN) :],
        "job": meta.get("job"),
        "status": meta.get("status"),
        "created_at": meta.get("created_at"),
        "finished_at": meta.get("finished_at"),
        "messages": int(meta.get("messages") or meta.get("items") or 0),
        "finished": len(meta.get("finished_items") or ()),
        "estimate_aud": round(_number(meta.get("estimate_aud")), 4),
        "spent_aud": round(_number(meta.get("spent_aud")), 4),
        "prompt_id": meta.get("prompt_id"),
        "record": meta.get("record"),
    }


def list_runs(limit: int = 20) -> dict:
    """GET /evals/runs: the newest `limit` runs (judge and scoring), and how many there are. A Scan of
    the evals table for run headers: an operator's question, asked now and then."""
    table = evals_rollup.evals_table()
    kwargs = {"FilterExpression": Attr("sk").eq(eval_judge.META) & Attr("entity").begins_with(eval_judge.RUN)}
    headers: list[dict] = []
    while True:
        page = table.scan(**kwargs)
        headers += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    headers.sort(key=lambda meta: (str(meta.get("created_at", "")), str(meta["entity"])), reverse=True)
    return {"runs": [_summary(meta) for meta in headers[:limit]], "count": len(headers)}


def results(combos: list[dict], rows: list[dict]) -> list[dict]:
    """Each combination's results from its item rows: how many were scored, better and worse than the
    live version, their net delta, the candidate's share of wins with its Wilson interval, what came
    of the rest, and the tokens and A$ its drafts and verdicts cost (the live drafts are counted
    apart: they are made once per item, for every combination)."""
    found = {
        combo["code"]: {
            "combination": combo["code"],
            "model_id": combo["model_id"],
            "prompt_versions": combo["prompt_versions"],
            "scored": 0,
            "better": 0,
            "worse": 0,
            "net_delta": 0,
            "other": Counter(),
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_aud": 0.0,
        }
        for combo in combos
    }
    for row in rows:
        entry = found.get(row.get("combo"))
        if entry is None or row.get("outcome") == SCORING:
            continue
        entry["input_tokens"] += int(row.get("input_tokens") or 0)
        entry["output_tokens"] += int(row.get("output_tokens") or 0)
        entry["cost_aud"] += _number(row.get("cost_aud"))
        if row.get("outcome") != SCORED:
            entry["other"][str(row.get("outcome"))] += 1
            continue
        delta = int(row.get("delta") or 0)
        entry["scored"] += 1
        entry["net_delta"] += delta
        entry["better" if delta > 0 else "worse"] += 1
    for entry in found.values():
        scored, better = entry["scored"], entry["better"]
        entry["better_share"] = round(better / scored, 4) if scored else None
        entry["interval"] = list(evals.wilson(better, scored))
        entry["other"] = dict(sorted(entry["other"].items()))
        entry["cost_aud"] = round(entry["cost_aud"], 4)
    return list(found.values())


def run_view(run_id) -> dict | None:
    """GET /evals/runs/{run_id}: the run's header and its progress, read from its item rows (and, for a
    scoring run, each combination's results). None when there is no such run."""
    run_id = _run_id(run_id)
    table = evals_rollup.evals_table()
    meta = _meta(table, run_id)
    if meta is None:
        return None
    rows = _rows(table, run_id, ITEM)
    outcomes = Counter(str(row.get("outcome")) for row in rows)
    view = {
        **_summary(meta),
        "judge_model_id": meta.get("judge_model_id"),
        "progress": {
            "messages": int(meta.get("messages") or meta.get("items") or 0),
            "results": sum(count for outcome, count in outcomes.items() if outcome != SCORING),
            "scoring": outcomes.get(SCORING, 0),
            "outcomes": dict(sorted(outcomes.items())),
        },
    }
    if meta.get("job") == SCORE_JOB:
        live = _rows(table, run_id, LIVE)
        view["combinations"] = results(meta.get("combinations") or [], rows)
        view["live_drafts"] = {
            "drafted": len(live),
            "cost_aud": round(sum(_number(row.get("cost_aud")) for row in live), 4),
        }
    return view


def cancel(run_id, *, now: datetime | None = None) -> dict:
    """DELETE /evals/runs/{run_id}: mark a queued, running or paused run cancelled. Its messages are
    dropped as they come (the consumer reads the status first); what is scored stays."""
    run_id = _run_id(run_id)
    now = now or datetime.now(UTC)
    table = evals_rollup.evals_table()
    try:
        table.update_item(
            Key=eval_judge._run_key(run_id),
            UpdateExpression="SET #s = :cancelled, cancelled_at = :now",
            ConditionExpression="#s IN (:queued, :running, :paused)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":cancelled": eval_judge.CANCELLED,
                ":now": eval_judge._iso(now),
                ":queued": eval_judge.QUEUED,
                ":running": eval_judge.RUNNING,
                ":paused": eval_judge.PAUSED,
            },
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException as exc:
        meta = _meta(table, run_id)
        if meta is None:
            raise Refused(404, f"no run {run_id}") from exc
        raise Refused(
            409, f"run {run_id} is {meta.get('status')}: only a queued, running or paused run is cancelled"
        ) from exc
    return {"run_id": run_id, "status": eval_judge.CANCELLED}


def _claimed(row: dict, now: datetime) -> bool:
    """Whether a row is claimed by a consumer that may still be scoring it."""
    claimed_at = eval_judge._time(row.get("claimed_at"))
    return row.get("outcome") == SCORING and claimed_at is not None and now - claimed_at < CLAIM_STALE


def resume(run_id, *, now: datetime | None = None, sqs=None) -> dict:
    """POST /evals/runs/{run_id}/resume: send again every message of a scoring run that has no result
    and is not being scored, after the same cap check as a new run, and mark the run queued."""
    run_id = _run_id(run_id)
    now = now or datetime.now(UTC)
    queue_url = _queue_url()
    table = evals_rollup.evals_table()
    meta = _meta(table, run_id)
    if meta is None:
        raise Refused(404, f"no run {run_id}")
    if meta.get("job") != SCORE_JOB:
        raise Refused(409, f"run {run_id} is a judge run: the next judge run picks its items again")
    combos, count = meta["combinations"], int(meta["item_count"])
    # A message is held back by its result, or by a consumer still scoring it; an error is no result.
    held = {
        (str(row.get("combo")), int(row.get("n", -1)))
        for row in _rows(table, run_id, ITEM)
        if row.get("outcome") not in (SCORING, ERROR) or _claimed(row, now)
    }
    numbers = [
        k * count + n
        for k, combo in enumerate(combos)
        for n in range(count)
        if (combo["code"], n) not in held
    ]
    if not numbers:
        raise Refused(409, f"every message of run {run_id} has its result, or is being scored")
    total = int(meta.get("messages") or len(combos) * count)
    need = _number(meta.get("estimate_aud")) / total * len(numbers)
    remaining = eval_judge.remaining_aud(table, now, evals_settings.get_settings())
    if need > remaining:
        raise Refused(
            409, "what is left of the run costs more than today's evals spend cap leaves; try tomorrow, or "
            "raise it with `evals set --daily-spend-cap`",
            estimate_aud=round(need, 4), remaining_aud=round(max(remaining, 0.0), 4),
        )  # fmt: skip
    table.update_item(
        Key=eval_judge._run_key(run_id),
        UpdateExpression="SET #s = :queued, #i = :total, resumed_at = :now REMOVE finished_at "
        "DELETE finished_items :again",
        ExpressionAttributeNames={"#s": "status", "#i": "items"},
        ExpressionAttributeValues={
            ":queued": eval_judge.QUEUED,
            ":total": total,
            ":now": eval_judge._iso(now),
            ":again": set(numbers),
        },
    )
    sent = _send(queue_url, run_id, meta, numbers, sqs)
    _count_unsent(table, run_id, set(numbers) - sent, now)
    print(f"eval_score: run {run_id} resumed, sending {len(sent)} of {len(numbers)} message(s) again")
    return {
        "run_id": run_id,
        "status": eval_judge.QUEUED,
        "resent": len(sent),
        "estimate_aud": round(need, 4),
    }


# --- the rubric -------------------------------------------------------------------------------------------


def _defang(text: str) -> str:
    """Break the rubric's block delimiters inside the material, so nothing in it can close a block early
    and pose as instructions after it."""
    return _DELIMITER_TAG.sub(lambda m: f"< {m.group(1)}{m.group(2)}", text)


def _block(tag: str, text: str) -> str:
    return f"<{tag}>\n{_defang(text)}\n</{tag}>"


def candidate_first(run_id: str, n: int, code: str) -> bool:
    """Whether the candidate's draft is shown as A: drawn from the item and the combination, so the
    candidate is first in about half of a run's comparisons, and the same each time one is retried."""
    return int(hashlib.sha256(f"{run_id}|{n}|{code}".encode()).hexdigest(), 16) % 2 == 0


def render(topic: dict, summaries: str, candidate: str, live: str, first: bool) -> prompts.Prompt:
    """The comparison rubric: the topic, the findings the drafts were written from, the two drafts (the
    candidate's first when `first`), the topic's operator note."""
    draft_a, draft_b = (candidate, live) if first else (live, candidate)
    steering = resolve_review_steering(topic)
    values = {
        "topic_name": topic_label(topic),
        "findings_block": _block("findings", summaries[:FINDINGS_MAX_CHARS]),
        "draft_a_block": _block("draft_a", draft_a[:DRAFT_MAX_CHARS]),
        "draft_b_block": _block("draft_b", draft_b[:DRAFT_MAX_CHARS]),
        "steering_block": eval_judge.steering_block(_defang(steering) if steering else None),
    }
    return prompts.get_prompt(COMPARE_PROMPT_ID, values, {"reply_rule": REPLY_RULE})


def parse_answer(text) -> tuple[str | None, str | None]:
    """(the better draft, A or B, and the reason code) from the judge's reply, or (None, None) when it
    is not the contract: one JSON object of exactly `better` and `reason_code`. A reply fenced as a code
    block is read inside its fence; anything else around the object is not the contract."""
    if not isinstance(text, str):
        return None, None
    stripped = text.strip()
    fenced = eval_judge._FENCE.fullmatch(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        answer = json.loads(stripped)
    except ValueError:
        return None, None
    if not isinstance(answer, dict) or set(answer) != {"better", "reason_code"}:
        return None, None
    if answer["better"] not in POSITIONS or answer["reason_code"] not in REASON_CODES:
        return None, None
    return answer["better"], answer["reason_code"]


# --- scoring one message ----------------------------------------------------------------------------------


def _message_item(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    n = value.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n < MAX_SAMPLE:
        return None
    topic_id, article_id = eval_judge._id(value.get("topic_id")), eval_judge._id(value.get("article_id"))
    if topic_id is None or article_id is None:
        return None
    return {"n": n, "topic_id": topic_id, "article_id": article_id}


def _message_combination(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    model_id, versions = _model_id(value.get("model_id")), value.get("prompt_versions")
    if model_id is None or not isinstance(versions, dict) or len(versions) != 1:
        return None
    ((prompt_id, version),) = versions.items()
    if prompt_id not in SCORABLE or not isinstance(version, str):
        return None
    return {"model_id": model_id, "prompt_versions": {prompt_id: version}}


def _claim(table, row: dict, now: datetime) -> bool:
    """Take the item's row for this message (`row`: its key, number and combination, and what earlier
    attempts spent): free, an earlier attempt's that failed (an error), or claimed so long ago that its
    consumer must have died. A copy of a failed message that comes on its own, not sent by `resume`,
    takes an error row over too: it is still under the day's cap, checked before the claim."""
    try:
        table.put_item(
            Item={**row, "outcome": SCORING, "claimed_at": eval_judge._iso(now)},
            ConditionExpression="attribute_not_exists(entity) OR #o = :error "
            "OR (#o = :scoring AND claimed_at < :stale)",
            ExpressionAttributeNames={"#o": "outcome"},
            ExpressionAttributeValues={
                ":error": ERROR,
                ":scoring": SCORING,
                ":stale": eval_judge._iso(now - CLAIM_STALE),
            },
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def _cost(cost: float) -> Decimal:
    return Decimal(str(round(cost, 6)))


def _spent_before(existing: dict | None) -> dict:
    """What earlier attempts at a message recorded on its row (one that failed, or a claim that carried
    it on from one): a retry adds to it, so the row's cost, which results() adds up per combination,
    is what the message cost. The run's spent_aud and the day row have it already."""
    if existing is None:
        return {}
    return {
        "input_tokens": int(existing.get("input_tokens") or 0),
        "output_tokens": int(existing.get("output_tokens") or 0),
        "cost_aud": _cost(_number(existing.get("cost_aud"))),
    }


def _live_draft(table, run_id: str, n: int, values: dict, locked: dict, model_id: str, settings, now) -> dict:
    """The live version's draft of item `n`, made once and kept on the run (`live#<n>`): the prompt as
    the daily cycle would serve it now (its A/B arm drawn by the item's key, section 4.1), on the model
    that drafted the article. Raises when the model call fails."""
    key = eval_judge._run_key(run_id, f"{LIVE}{n}")
    cached = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if cached is not None:
        return cached
    with prompts.bind_run(f"{run_id}:{n}"):
        prompt = prompts.get_prompt(DRAFT_PROMPT_ID, values, locked)
    result = _draft(prompt.text, model_id)
    used = canonical_model_id(result.get("model_id") or model_id)
    cost = eval_judge._record_spend(table, run_id, now, settings, used, result)
    row = {
        **key,
        "text": result["text"],
        "model_id": used,
        "prompt_version": prompt.version,
        "input_tokens": int(result["input_tokens"]),
        "output_tokens": int(result["output_tokens"]),
        "cost_aud": _cost(cost),
        "at": eval_judge._iso(now),
    }
    try:
        table.put_item(Item=row, ConditionExpression="attribute_not_exists(entity)")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Another combination of the item drafted it meanwhile: every combination is compared with one.
        return table.get_item(Key=key, ConsistentRead=True)["Item"]
    return row


def _draft(text: str, model_id: str) -> dict:
    """One draft call, as the daily cycle makes it (its token limit and its one retry with more), with
    no fallback model: a run scores the model it names."""
    cycle = _daily_cycle()
    return invoke_model_tracked(
        text, model_id, max_tokens=cycle.DRAFT_MAX_TOKENS, retry_max_tokens=cycle.DRAFT_RETRY_MAX_TOKENS
    )


def _candidate_prompt(
    meta: dict, prompt_id: str, label: str, values: dict, locked: dict
) -> prompts.Prompt | None:
    """The candidate's prompt: the vanilla, or its version's versions/ file (the sha256 the run recorded)."""
    if label == prompts.spec_for(prompt_id).vanilla_label:
        return prompts.vanilla_render(prompt_id, values, locked)
    sha = (meta.get("versions_sha256") or {}).get(label)
    if not isinstance(sha, str) or not prompts.store_named():
        return None
    return prompts.version_prompt(prompt_id, label, sha, values, locked)


def _emit(run_id: str, n: int, combo: dict, item: dict, label: str, delta: int, now: datetime) -> bool:
    """The Offline Review event (section 4.3) for the candidate model and prompt version."""
    ((prompt_id, _),) = combo["prompt_versions"].items()
    return evals.emit(
        REASON,
        event_id=evals.event_id(REASON, run_id=run_id, item=str(n), combo=combo["code"]),
        source=eval_judge.SOURCE,
        rating_delta=delta,
        model_id=canonical_model_id(combo["model_id"]),
        prompt_id=prompt_id,
        prompt_version=label,
        subject=item["article_id"],
        topic_id=item["topic_id"],
        by=eval_judge.ROLE,
        timestamp=eval_judge._event_time(now),
    )


def _score(raw: str, run_id: str, item: dict, combination: dict, now: datetime, sqs) -> dict:
    """Score one item for one combination of run `run_id` (see the module's docstring for the order)."""
    table = evals_rollup.evals_table()
    meta = _meta(table, run_id)
    if meta is None:
        print(f"eval_score: run {run_id} has no header; its message is dropped")
        return {"status": "dropped", "reason": "no_run"}
    if meta.get("status") == eval_judge.CANCELLED:
        print(f"eval_score: run {run_id} was cancelled; its message is dropped")
        return {"status": "dropped", "reason": "cancelled"}
    combos, articles = meta.get("combinations") or [], meta.get("articles") or []
    k = next((i for i, combo in enumerate(combos) if
              (combo.get("model_id"), combo.get("prompt_versions")) ==
              (combination["model_id"], combination["prompt_versions"])), None)  # fmt: skip
    n = item["n"]
    known = n < len(articles) and (articles[n].get("topic_id"), articles[n].get("article_id")) == (
        item["topic_id"],
        item["article_id"],
    )
    if meta.get("job") != SCORE_JOB or k is None or not known:
        print(f"eval_score: a message that is not one of run {run_id}'s; dropped")
        return {"status": "refused"}
    combo, m = combos[k], k * len(articles) + n
    ((prompt_id, label),) = combo["prompt_versions"].items()
    key = eval_judge._run_key(run_id, f"{ITEM}{n}#{combo['code']}")
    existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if existing is not None and existing.get("outcome") not in (SCORING, ERROR):
        eval_judge._finish(table, run_id, m, now)
        return {"status": "dropped", "reason": "already_scored"}
    if existing is not None and _claimed(existing, now):
        return {"status": "dropped", "reason": "being_scored"}

    settings = evals_settings.get_settings()
    judge, candidate = str(meta["judge_model_id"]), combo["model_id"]
    live_model = str(articles[n]["live_model_id"])
    # The most the message can cost, a retried draft included, so no one message takes the day past it.
    draft_in, draft_out = _draft_ceiling_tokens()
    estimate = eval_judge.call_cost_aud(candidate, draft_in, draft_out) + eval_judge.call_cost_aud(
        judge, COMPARE_ESTIMATE_INPUT_TOKENS, COMPARE_MAX_TOKENS
    )
    if table.get_item(Key=eval_judge._run_key(run_id, f"{LIVE}{n}"), ConsistentRead=True).get("Item") is None:
        estimate += eval_judge.call_cost_aud(live_model, draft_in, draft_out)
    if estimate > eval_judge.remaining_aud(table, now, settings):
        return eval_judge._pause(table, run_id, m, raw, sqs, now)
    row = {**key, "n": n, "combo": combo["code"], **_spent_before(existing)}
    if not _claim(table, row, now):
        return {"status": "dropped", "reason": "being_scored"}
    eval_judge._set_status(table, run_id, eval_judge.RUNNING, (eval_judge.QUEUED, eval_judge.PAUSED))
    scored_row = {**row, "prompt_id": prompt_id, "prompt_version": label}
    scoring = _Scoring(table, run_id, m, scored_row, settings, now)
    try:
        return _scored(scoring, meta, item, combo, live_model)
    except Exception as exc:  # noqa: BLE001 - recorded as the message's outcome, so the run still closes
        print(f"eval_score: item {n} of run {run_id} failed: {exc!r}")
        try:
            return scoring.failed()
        except Exception as again:  # noqa: BLE001 - an unwritten outcome leaves a claim that goes stale
            print(f"eval_score: could not record that item {n} of run {run_id} failed: {again!r}")
            return {"status": ERROR}


class _Scoring:
    """One claimed message's bookkeeping: what its calls cost, and its result row once it has one."""

    def __init__(self, table, run_id: str, m: int, row: dict, settings: dict, now: datetime):
        self.table, self.run_id, self.m, self.settings, self.now = table, run_id, m, settings, now
        # From what earlier attempts spent (_spent_before), so a retry adds to it.
        tokens = {name: int(row.get(name) or 0) for name in ("input_tokens", "output_tokens")}
        self.row = {**row, **tokens}
        self.cost = _number(row.get("cost_aud"))
        # The outcome once the result row is written: from then on nothing rewrites it.
        self.recorded: str | None = None

    def spent(self, model_id: str, result: dict) -> None:
        """A call made for this message: its tokens and cost on the row, its spend on the day row, the
        run and the Stats page (eval_judge._record_spend)."""
        self.row["input_tokens"] += int(result["input_tokens"])
        self.row["output_tokens"] += int(result["output_tokens"])
        used = canonical_model_id(result.get("model_id") or model_id)
        self.cost += eval_judge._record_spend(self.table, self.run_id, self.now, self.settings, used, result)

    def record(self, outcome: str, *, condition: dict | None = None, **fields) -> dict:
        """The message's result row (written only while `condition`, put_item's, holds, when given)."""
        result = {**self.row, **fields, "outcome": outcome, "cost_aud": _cost(self.cost)}
        self.table.put_item(Item={**result, "at": eval_judge._iso(self.now)}, **(condition or {}))
        self.recorded = outcome
        return {"status": outcome}

    def finish(self) -> None:
        """The message counted finished (eval_judge._finish closes the run with the last): a set ADD, so
        counting it again counts it once."""
        eval_judge._finish(self.table, self.run_id, self.m, self.now)

    def done(self, outcome: str, **fields) -> dict:
        """The message's result row, then the message counted finished."""
        answer = self.record(outcome, **fields)
        self.finish()
        return answer

    def failed(self) -> dict:
        """After a failure nothing expected: the message's outcome is an error, unless its row holds its
        result already (a verdict whose count failed, say), which is never rewritten; either way the
        message is counted finished, so the run still closes. Raises when that fails too."""
        if self.recorded is None:
            try:
                self.record(
                    ERROR,
                    stage="unexpected",
                    condition={
                        "ConditionExpression": "#o = :scoring",
                        "ExpressionAttributeNames": {"#o": "outcome"},
                        "ExpressionAttributeValues": {":scoring": SCORING},
                    },
                )
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                # No longer this claim: a result was written whose reply was lost. It stands.
                print(f"eval_score: message {self.m} of run {self.run_id} has an outcome already; it stands")
        self.finish()
        return {"status": self.recorded or ERROR}


def _scored(scoring: _Scoring, meta: dict, item: dict, combo: dict, live_model: str) -> dict:
    """The claimed message's drafts and verdict (see the module's docstring for the order)."""
    run_id, n, now, settings = scoring.run_id, item["n"], scoring.now, scoring.settings
    judge, candidate = str(meta["judge_model_id"]), combo["model_id"]
    ((prompt_id, label),) = combo["prompt_versions"].items()
    scoring.row.update(candidate_model=canonical_model_id(candidate), judge_model=canonical_model_id(judge))
    article, topic = dynamo.get_article(item["article_id"]), dynamo.get_topic(item["topic_id"])
    if article is None or topic is None:
        return scoring.done("gone")
    # The judge is neither model it compares (section 3, deviation 9), checked again on the article as
    # it is now.
    live_model = draft_model(article) or live_model
    if canonical_model_id(judge) in {canonical_model_id(live_model), canonical_model_id(candidate)}:
        return scoring.done("judge_is_author")
    parts = _daily_cycle().rebuild_draft_prompt(topic, article)
    if parts is None:
        return scoring.done("no_findings")
    values, locked = parts
    candidate_prompt = _candidate_prompt(meta, prompt_id, label, values, locked)
    if candidate_prompt is None:
        return scoring.done("version_unreadable")
    try:
        live = _live_draft(scoring.table, run_id, n, values, locked, live_model, settings, now)
    except Exception as exc:  # noqa: BLE001 - the message has no result; `evals runs resume` sends it again
        print(f"eval_score: the live draft of item {n} of run {run_id} failed: {exc!r}")
        return scoring.done(ERROR, stage="live_draft")
    try:
        drafted = _draft(candidate_prompt.text, candidate)
    except Exception as exc:  # noqa: BLE001
        print(f"eval_score: the candidate's draft of item {n} of run {run_id} failed: {exc!r}")
        return scoring.done(ERROR, stage="candidate_draft")
    scoring.spent(candidate, drafted)
    first = candidate_first(run_id, n, combo["code"])
    rubric = render(topic, str(values.get("summaries") or ""), drafted["text"], str(live["text"]), first)
    context = {
        "live_model": live.get("model_id"),
        "live_version": live.get("prompt_version"),
        "candidate_position": POSITIONS[0] if first else POSITIONS[1],
        "judge_prompt_version": rubric.version,
    }
    try:
        answer = invoke_model_tracked(rubric.text, judge, max_tokens=COMPARE_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001
        print(f"eval_score: the judge's call for item {n} of run {run_id} failed: {exc!r}")
        return scoring.done(ERROR, stage="judge", **context)
    scoring.spent(judge, answer)
    better, reason_code = parse_answer(answer.get("text"))
    if better is None:
        # The reply is not the contract: nothing of it is kept.
        print(f"eval_score: the judge's reply for item {n} of run {run_id} was not the contract; ignored")
        return scoring.done("malformed", **context)
    good = better == context["candidate_position"]
    delta = eval_judge.DELTA if good else -eval_judge.DELTA
    verdict = "good" if good else "bad"
    result = scoring.record(SCORED, **context, verdict=verdict, delta=delta, reason_code=reason_code)
    # The event goes after the row and before the message is counted finished, as the judge's does, so a
    # count that fails costs no event (_score's catch-all counts it again).
    if meta.get("record") is True:
        _emit(run_id, n, combo, item, label, delta, now)
    scoring.finish()
    return {**result, "verdict": verdict}


def consume(raw: str, body: dict, *, now: datetime | None = None, sqs=None) -> dict:
    """One `score` message, from eval_judge.consume (which read its JSON): scored, or what became of it."""
    now = now or datetime.now(UTC)
    run_id = eval_judge._id(body.get("run_id"))
    item, combination = _message_item(body.get("item")), _message_combination(body.get("combination"))
    if run_id is None or item is None or combination is None:
        print("eval_score: a score job without a readable run id, item or combination; dropped")
        return {"status": "refused"}
    return _score(raw, run_id, item, combination, now, sqs)
