"""A person's review of research Findings (docs/enhancements/model-ranch-prompt-assets-evals.md, sections
3's deviation 10, 4.3, 4.4 and 5.3, PR19): the admin API's GET /evals/findings, the Findings waiting
for a person, and POST /evals/findings/review, one person's verdict on one of them.
`admin_cli approve --source findings` goes through both (scripts/review_inbox.py).

**The queue** is the Findings no person has reviewed yet (`reviewed_by` does not hold `human`), still
alive, captured in the last LOOKBACK (a Finding's whole life), with a summary and the research call that
wrote it (a Finding from before research tracking names no model, so a verdict on it would score
nothing). The ones the judge has reviewed come first: a person's verdict on those pairs with the
judge's, and the pairs are what the judge's calibration is measured from. Then the rest. Oldest first
within each, as the review inbox goes, since a Finding expires after 14 days. `offset` (an addition to
the spec's `?count=`) pages through it in that order, and `more` says whether anything is left after
the page: the CLI hides what the person skipped on its own side (the skips never leave their machine),
so without paging, skipped Findings at the head would hide every one behind them.

**Nothing of the judge's verdict is shown.** The reviewer gets what they need to judge the summary, and
no more: VIEW_FIELDS of the Finding (its topic, when it was captured, the summary, its sources' titles
and links), its subject, and whether the judge has reviewed it (not what it said). The judge's verdict
is on its pair row and its run's item row, which the queue never reads; the Finding itself holds only
the role codes that reviewed it. Not the model that wrote it either: a reviewer marks the work, not its
maker.

**A verdict** (`good` or `bad`) writes three things for the Finding's subject, `<topic_id>#<captured_at>`:

1. the `pair#<subject>` row `human` (verdict, delta, ts: common/evals_emission.pair_row), which the
   rollup pairs with the judge's row for the same subject (common/evals_rollup.judge_agreement);
2. the person's marks on the Finding, exactly as the judge marks it but with the role `human`
   (common/eval_judge.mark_finding: `ADD review_count :one, reviewed_by :roles` with {"human"}, under
   `attribute_not_exists(reviewed_by) OR NOT contains(reviewed_by, :role)`);
3. one Human Review event (+2 good, -2 bad, `by: human`, section 4.3) for the model that wrote the
   summary and the prompt it wrote it with (common/eval_judge.credit: the Finding's `research_call`),
   event id `human:<subject>:<ts>`. Only while evals are on (common/evals.emit's rule); the pair row and
   the marks are written either way, so the judge has people's verdicts to meet when it starts.

**A retried POST writes nothing twice.** The pair row is written first, and only where there is none
(a conditional put): its `ts`, the moment of the first POST, is the event id's `<ts>`, and a retry
reads it back rather than taking a new one, so every attempt names the same event. A retry writes no
second pair row (that condition), no second review_count (the marks' condition), and no second event:
the event is claimed on the pair row (its `event_id`, set only where it is not) before it is written,
and the claim is let go when the write does not happen (evals off, Logs refusing), so a later retry can
still write it. A crash between the claim and the write loses that one event, as emit's own failures
do. A second POST with the other verdict is refused (409): the first verdict stands, as its event
does.

Reads and writes only what lambda_exec already holds: the Findings table (an app table), the evals
table and the evals log group (the evals module's grants).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from boto3.dynamodb.conditions import Attr, Key

from common import dynamo, eval_judge, evals, evals_emission

ROLE = evals_emission.HUMAN
SOURCE = evals_emission.ADMIN_API
REASON = "Human Review"
VERDICTS = ("good", "bad")
BODY_FIELDS = frozenset({"topic_id", "captured_at", "verdict"})

# GET /evals/findings?count=&offset=: how many Findings one call returns, and how far into the queue
# it may start (far past what anyone skips; scripts/review_inbox.py stops paging well before it).
COUNT_DEFAULT = 10
COUNT_MAX = 50
OFFSET_MAX = 1000
# How far back the queue reads: a Finding's whole life (research_tick_handler's FINDING_TTL_DAYS).
LOOKBACK = timedelta(days=14)
# A topic's Findings are read newest first, at most this many pages of a Query (1 MB each, hundreds of
# Findings), so the judge's, which are recent, are always among them.
QUERY_PAGES = 3
# What the reviewer is shown of a Finding, and what is read for it (with what decides whether it is
# waiting and whether it can be scored).
VIEW_FIELDS = ("topic_id", "captured_at", "summary", "source_refs")
READ_FIELDS = (*VIEW_FIELDS, "research_call", "reviewed_by", "expires_at")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _event_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _findings_table():
    return dynamo.get_table(os.environ["FINDINGS_TABLE"])


def _judged(finding: dict) -> bool:
    return eval_judge.ROLE in (finding.get("reviewed_by") or ())


# --- the queue ----------------------------------------------------------------------------------------


def _waiting(topic_id: str, now: datetime) -> list[dict]:
    """A topic's Findings of the last LOOKBACK that no person has marked and that have not expired,
    newest first, the fields READ_FIELDS names."""
    unmarked = Attr("reviewed_by").not_exists() | ~Attr("reviewed_by").contains(ROLE)
    alive = Attr("expires_at").not_exists() | Attr("expires_at").gt(int(now.timestamp()))
    names = {f"#f{index}": name for index, name in enumerate(READ_FIELDS)}
    kwargs = {
        "KeyConditionExpression": Key("topic_id").eq(topic_id) & Key("captured_at").gte(_iso(now - LOOKBACK)),
        "FilterExpression": unmarked & alive,
        "ProjectionExpression": ", ".join(names),
        "ExpressionAttributeNames": names,
        "ScanIndexForward": False,
    }
    table = _findings_table()
    found: list[dict] = []
    for _ in range(QUERY_PAGES):
        page = table.query(**kwargs)
        found.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return found


def _reviewable(finding: dict) -> bool:
    """Whether a verdict on the Finding can be kept and scored: its keys are a subject, it has a summary
    to read, and its research call names the model the verdict scores."""
    summary = finding.get("summary")
    return (
        eval_judge.message_item(eval_judge.RESEARCH, finding) is not None
        and isinstance(summary, str)
        and bool(summary.strip())
        and eval_judge.credit(finding, eval_judge.RESEARCH) is not None
    )


def view(finding: dict) -> dict:
    """What the reviewer is shown of a Finding (see the module's docstring): VIEW_FIELDS, each source
    as its title and link only, the subject, and whether the judge has reviewed it."""
    refs = finding.get("source_refs") if isinstance(finding.get("source_refs"), list) else []
    return {
        "topic_id": finding["topic_id"],
        "captured_at": finding["captured_at"],
        "subject": f"{finding['topic_id']}#{finding['captured_at']}",
        "summary": finding["summary"],
        "source_refs": [
            {"title": ref.get("title"), "url": ref.get("url")} for ref in refs if isinstance(ref, dict)
        ],
        "judged": _judged(finding),
    }


def queue(count: int, offset: int = 0, now: datetime | None = None) -> tuple[list[dict], bool]:
    """(page, more): up to `count` Findings waiting for a person, as `view` shows them, from `offset`
    on in the queue's order (the judge's first, then the rest, oldest first within each), and whether
    any are left after them. The whole queue is read and sorted on every call, so a page is a slice of
    it: a Finding reviewed between two calls shifts the rest by one, and one that slips past a page's
    edge that way is in the next run's."""
    now = now or datetime.now(UTC)
    waiting: list[dict] = []
    for topic in sorted(dynamo.list_topics(), key=lambda topic: str(topic.get("topic_id", ""))):
        topic_id = topic.get("topic_id")
        if isinstance(topic_id, str) and topic_id:
            waiting += [finding for finding in _waiting(topic_id, now) if _reviewable(finding)]
    waiting.sort(key=lambda finding: (not _judged(finding), finding["captured_at"], finding["topic_id"]))
    end = offset + count
    return [view(finding) for finding in waiting[offset:end]], len(waiting) > end


# --- a verdict ----------------------------------------------------------------------------------------


def _keep_pair(table, subject: str, verdict: str, now: datetime) -> tuple[dict, bool]:
    """The person's pair row for `subject`: written now when there is none, else the one kept. (row,
    whether it was written now)."""
    row = evals_emission.pair_row(subject, ROLE, verdict, evals_emission.pair_time(now))
    try:
        table.put_item(Item=row, ConditionExpression="attribute_not_exists(entity)")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        kept = table.get_item(Key=evals_emission.pair_key(subject, ROLE), ConsistentRead=True).get("Item")
        return (kept or row), False
    return row, True


def _event_fields(
    event_id: str, scored: dict, subject: str, topic_id: str, delta: int, now: datetime
) -> dict:
    """The verdict's event, with the summary's prompt when it is one the schema takes (a prompt id the
    call recorded in another shape is left out rather than losing the event, as the judge's are)."""
    fields = {
        "event_id": event_id,
        "source": SOURCE,
        "rating_delta": delta,
        "model_id": scored["model_id"],
        "subject": subject,
        "topic_id": topic_id,
        "by": ROLE,
        "timestamp": _event_time(now),
    }
    prompt = {name: scored[name] for name in ("prompt_id", "prompt_version") if name in scored}
    if prompt and evals.event(REASON, **fields, **prompt)[0] is not None:
        fields.update(prompt)
    return fields


def _emit_once(table, subject: str, fields: dict) -> str:
    """Write the verdict's event unless an earlier attempt has: claim it on the pair row first, and let
    the claim go when the write does not happen. `written`, `written_before` or `not_written`."""
    if not evals.enabled():
        return "not_written"
    key = evals_emission.pair_key(subject, ROLE)
    claim = {":id": fields["event_id"]}
    try:
        table.update_item(
            Key=key,
            UpdateExpression="SET event_id = :id",
            ConditionExpression="attribute_exists(entity) AND attribute_not_exists(event_id)",
            ExpressionAttributeValues=claim,
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return "written_before"
    if evals.emit(REASON, **fields):
        return "written"
    try:
        table.update_item(
            Key=key,
            UpdateExpression="REMOVE event_id",
            ConditionExpression="event_id = :id",
            ExpressionAttributeValues=claim,
        )
    except Exception as exc:  # noqa: BLE001 - the verdict is kept; only a later retry's event is lost
        print(f"human_review: could not let go of an unwritten event's claim: {exc!r}")
    return "not_written"


def review(body, now: datetime | None = None) -> tuple[int, dict | str]:
    """POST /evals/findings/review: one person's verdict on one Finding (see the module's docstring).
    (200, what was kept) or (an error status, why)."""
    now = now or datetime.now(UTC)
    if not isinstance(body, dict) or set(body) != BODY_FIELDS:
        return 400, "send exactly topic_id, captured_at and verdict (good or bad)"
    item = eval_judge.message_item(eval_judge.RESEARCH, body)
    if item is None:
        return 400, "'topic_id' must be a topic id and 'captured_at' the Finding's ISO capture time"
    verdict = body["verdict"]
    if verdict not in VERDICTS:
        return 400, "'verdict' must be good or bad"
    key = {"topic_id": item["topic_id"], "captured_at": item["captured_at"]}
    finding = _findings_table().get_item(Key=key, ConsistentRead=True).get("Item")
    if finding is None:
        return 404, f"no Finding of topic '{item['topic_id']}' captured at {item['captured_at']}"
    scored = eval_judge.credit(finding, eval_judge.RESEARCH)
    if scored is None:
        return 409, "this Finding records no research call, so there is no model for a verdict to score"
    table = evals_emission.evals_table()
    subject = item["subject"]
    pair, first = _keep_pair(table, subject, verdict, now)
    if pair.get("verdict") != verdict:
        return 409, f"already reviewed as {pair.get('verdict')} at {pair.get('ts')}: the first verdict stands"
    eval_judge.mark_finding(finding, ROLE, now)
    ts = pair.get("ts") if isinstance(pair.get("ts"), str) else None
    event_id = evals.event_id(REASON, subject=subject, ts=ts) if ts else None
    delta = 2 if verdict == "good" else -2
    event = "not_written"
    if event_id is not None:
        fields = _event_fields(event_id, scored, subject, item["topic_id"], delta, now)
        event = _emit_once(table, subject, fields)
    return 200, {
        "subject": subject,
        "verdict": verdict,
        "reviewed_at": pair.get("ts"),
        "already_reviewed": not first,
        "event_id": event_id,
        "event": event,
    }
