"""The evals rollup: adds the scored events in the evals log group up into the evals table, and puts
each model's scores on its models row (docs/enhancements/model-ranch-prompt-assets-evals.md,
sections 4.4, 4.6, 4.9 and 5.3).

Run hourly by eval-jobs (eval_jobs_handler.py, `{"job": "rollup"}`), and on demand by the admin API's
POST /evals/rollup (`admin_cli evals rollup`), from the watermark, or for the days `--from`/`--to`
name.

**Days, not running totals.** Every run adds up whole days again, from every event of each day it
reads, and writes each day's totals with a plain put: the `day#<yyyy-mm-dd>` row of `model#<id>`,
`prompt#<prompt_id>#<version>` and `cell#<model>#<prompt_id>#<version>`. A run repeated, or two at
once, write the same rows. Windows are summed when they are read (`windows`), never carried forward,
so neither a month boundary nor a retried run can count anything twice (section 3, deviation 4).
Days are those of the settings' `timezone` (Sydney by default), so a day is 23 or 25 hours long when
the clocks change.

**The watermark.** The index row's `last_rolled_day` is the newest final day. A scheduled run reads
the log from the start of that day to now and adds up every day after it. A day becomes final two
hours after the midnight that ends it (FINAL_AFTER), when everything written during it can long be
read; the watermark then moves to it. An event written after its day became final is late, whether
or not a run has yet moved the watermark to that day: it is counted in `late_dropped`, by the day
it was written, and in no day row. One dated on a final day and written before the day became
final was added up while the day was open, and is passed over. A first run, with no watermark, adds
up OLDEST_DAY_BACK days: what the log group still holds whole (it keeps 120).

**What an event counts for** (sections 3 and 4.3). `evals.parse` checks every line exactly as `emit`
wrote it; a line that fails is unreadable and skipped. Events are deduplicated by `event_id` (the
earliest copy is kept), and those the settings' `excluded_sources` name are left out. Then, by what
its reason applies to:

* the model's row, for every reason that applies to a model. A reason written once per prompt of an
  article (Article Completion, Admin Review: their event id ends in the prompt id) counts once per
  article for the model, as the model wrote one article;
* the prompt version's row, for the reasons that apply to prompts, and Prompt Fallback (counted,
  scoring nothing);
* the model and prompt cell, for every event naming both a model and a prompt version, model-only
  reasons included (deviation 15). A reader's vote names no prompt: it reaches the cells of the
  prompts the article was written with, joined to the article's Article Completion events by subject
  (looked up further back in the log when this run's read does not hold them), and never a prompt's
  own row;
* nothing, for an operator's forgive or punish: those are `adjust#` rows, which the admin API writes
  itself (common/evals_adjust.py) beside the event, and they count in `total_perf` only.

A day row holds `perf` (the sum of the deltas), `events`, `good` and `bad` (section 4.9's counts:
events above and below zero, Article Completion and Prompt Fallback left out) and `by_reason` (each
reason's perf, events, good and bad). The index's own `day#<date>` rows list the entities a day has
rows for, so a day added up again deletes the rows it no longer has (an excluded source's, say).

**The models row** (section 4.6). For every model in the registry, and every model an event named,
the windows are summed from its day rows: `daily_perf` today, `weekly_perf` the last seven days,
`monthly_perf` the calendar month so far, `total_perf` every day and the model's `adjust#` rows,
each with its count in `perf_events` (adjustments are not events). They hold what the gates read
(section 4.5): the reasons that gate, the judge's only while it is calibrated, never readers'
votes. Beside them, `perf_good` and `perf_bad` hold section 4.9's counts behind each window (the
day rows' `good` and `bad`: readers' votes and the judge's verdicts included, Article Completion
and Prompt Fallback not), which the rewrite picker ranks by (scripts/review_inbox.py).
`model_roles` (model_fleet.model_roles) and `perf_updated_at` go with them. Each write is
conditional on the row being there (dynamo.set_model_fields, `attribute_exists(model_id)`), so a
model that was culled or deleted is never written back (deviation 8). A model with no purpose yet is
classified from one Bedrock listing: model_catalog.listed_models, the whole listing, so that
`unknown` keeps meaning that Bedrock no longer lists the model.

**The judge's agreement** with people (`judge_agreement`, `judge_pairs`) is computed from every
`pair#<subject>` row that holds both a judge's verdict and a person's. The judge is calibrated while
`judge_pairs >= judge_min_pairs` and `judge_agreement >= judge_min_agreement`.

**When it fails.** A run that finished prints one `rollup_done` evidence line, which the evals
module's heartbeat alarm counts. A run that cannot read the log or read and write the tables raises
RollupError, so eval-jobs' Errors alarm fires (section 6 allows the rollup that); a Bedrock listing
that fails only leaves models unclassified until the next run.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

from common import dynamo, evals, evals_settings, evidence, logs_insights, model_catalog, model_fleet
from common.model_pricing import canonical_model_id
from common.naming import environment_prefix

# The rows one Logs Insights query asks for: its ceiling. A window that returns this many may have
# been cut short, so it is split in two and both halves are read again.
ROW_LIMIT = logs_insights.MAX_QUERY_LIMIT
QUERY = 'fields @timestamp, @message\n| filter schema = "evals/1"\n| sort @timestamp asc'
# The Article Completion events of some subjects, for the votes this run's read cannot join: the
# subjects are quoted in place of `{subjects}`, each one evals.parse has checked and SUBJECT allows.
COMPLETIONS_QUERY = (
    'fields @timestamp, @message\n| filter reason = "Article Completion"\n| filter subject in [{subjects}]'
)
SUBJECT = re.compile(r"[A-Za-z0-9_.:#+-]{1,200}")
SUBJECTS_PER_QUERY = 50
# How long a read waits for its queries (eval-jobs has 900 seconds), how many it runs at once, and
# the narrowest window it splits to: one minute of 10,000 events is not this pipeline's.
WAIT_SECONDS = 300
QUERIES_AT_ONCE = 4
MIN_WINDOW = timedelta(minutes=1)
LABEL = "evals_rollup"

FINAL_AFTER = timedelta(hours=2)
# The evals log group keeps 120 days (infra/modules/evals). The oldest of them is being deleted as
# it ages, so a run never adds up a day older than this: its rows would be replaced by part of it.
OLDEST_DAY_BACK = 118
WEEK_DAYS = 7
WINDOWS = ("daily", "weekly", "monthly", "total")

INDEX = "index"
INDEX_KEY = {"entity": INDEX, "sk": INDEX}
DAY = "day#"
PAIR = "pair#"
# An operator's forgive or punish of a model (common/evals_adjust.py writes these rows): its deltas
# count in total_perf and in no window.
ADJUST_MODEL = "adjust#model#"
VERDICTS = ("good", "bad")

COMPLETION = "Article Completion"
VOTE = "User Feedback"
# Section 4.9: the good and bad counts leave these out.
NOT_GOOD_OR_BAD = frozenset({COMPLETION, "Prompt Fallback"})
# What each kind of row counts, by the reason's `applies_to` (common/evals.SCORING).
FOR_MODEL = frozenset({"model", "model_and_prompts", "combination"})
FOR_PROMPT = frozenset({"model_and_prompts", "combination", "prompt_version"})
ADJUSTMENT = "target"


class RollupError(Exception):
    """The log or a table could not be read or written: an infrastructure failure, which the handler
    lets raise so eval-jobs' Errors alarm fires."""


def function_name() -> str:
    """eval-jobs' name, for the admin API to start a rollup: `<prefix>-<env>-eval-jobs`, the name the
    evals module gives it and the ARN its InvokeEvalJobs grant is scoped to, so no other name could
    be invoked anyway."""
    return f"{environment_prefix(os.environ['ENVIRONMENT_NAME'])}eval-jobs"


# --- days -----------------------------------------------------------------------------------------


def day_start(day: date, zone) -> datetime:
    """The moment `day` starts in `zone`, in UTC (midnight is never skipped or repeated in Sydney)."""
    return datetime.combine(day, clock_time(0), tzinfo=zone).astimezone(UTC)


def final_at(day: date, zone) -> datetime:
    """When `day` becomes final: two hours after the midnight that ends it."""
    return day_start(day + timedelta(days=1), zone) + FINAL_AFTER


def local_day(moment: datetime, zone) -> date:
    return moment.astimezone(zone).date()


def newest_final_day(now: datetime, zone) -> date:
    """The newest day final at `now`: yesterday once today's midnight is two hours past."""
    return local_day(now - FINAL_AFTER, zone) - timedelta(days=1)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def manual_range(day_from, day_to, now: datetime, zone) -> tuple[date, date] | str:
    """The days `--from` and `--to` name, checked: ISO dates, `from` no later than `to` (which is
    today when not given), neither after today, and `from` at most OLDEST_DAY_BACK days back. Else
    the reason, in words."""
    today = local_day(now, zone)
    try:
        first = date.fromisoformat(day_from) if isinstance(day_from, str) else None
        last = date.fromisoformat(day_to) if isinstance(day_to, str) else today if day_to is None else None
    except ValueError:
        first = last = None
    if first is None or last is None:
        return "'from' and 'to' must be dates, YYYY-MM-DD ('to' defaults to today)"
    if first > last:
        return "'from' must not be after 'to'"
    if last > today:
        return f"'to' must not be after today ({today.isoformat()})"
    if first < today - timedelta(days=OLDEST_DAY_BACK):
        return (
            f"'from' must be at most {OLDEST_DAY_BACK} days back: the evals log keeps 120 days, and a day "
            "it no longer holds whole is never added up again"
        )
    return first, last


# --- reading the log ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One event as the rollup reads it: its fields (evals.parse), when Logs took it (`logged_at`,
    the log event's own timestamp) and when it happened (`at`, its `timestamp` field)."""

    record: dict
    logged_at: datetime
    at: datetime


def parse_time(value) -> datetime | None:
    """A time from the log (`2026-10-27 03:04:05.123`, UTC) or an event (ISO), aware; else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_log(start: datetime, end: datetime, *, client, group: str, region: str, query: str = QUERY):
    """Every row `query` finds from `start` to `end`, however many: a window that returns ROW_LIMIT
    rows is split in two at a whole second and both halves are read again. (rows, splits). Raises
    RollupError when a query fails or does not finish, or a minute alone holds ROW_LIMIT rows."""
    rows: list[dict] = []
    splits = 0
    pending = [(start, end)]
    while pending:
        batch, pending = pending[:QUERIES_AT_ONCE], pending[QUERIES_AT_ONCE:]
        jobs = [((a.isoformat(), b.isoformat()), region, group, query, a, b) for a, b in batch]
        results = logs_insights.run_queries(
            jobs, client=client, wait_seconds=WAIT_SECONDS, label=LABEL, limit=ROW_LIMIT
        )
        for (a, b), job in zip(batch, jobs, strict=True):
            found = results.get(job[0])
            if found is None:
                raise RollupError(f"the evals log query from {a.isoformat()} to {b.isoformat()} failed")
            if len(found) < ROW_LIMIT:
                rows.extend(found)
                continue
            if b - a <= MIN_WINDOW:
                raise RollupError(f"the minute of the evals log from {a.isoformat()} holds {ROW_LIMIT} rows")
            # Logs Insights' times are whole seconds, both ends included: the halves meet without
            # sharing a second.
            low, high = int(a.timestamp()), int(b.timestamp())
            middle = datetime.fromtimestamp(low + (high - low) // 2, UTC)
            pending += [(a, middle), (middle + timedelta(seconds=1), b)]
            splits += 1
    return rows, splits


def parse_rows(rows: list[dict]) -> tuple[dict[str, Event], int, int]:
    """({event_id: Event}, unreadable, duplicates): each row's message through evals.parse, and one
    copy of each event id, the earliest Logs took (then by its own time, then by its line, so the
    choice never depends on the order rows came back in)."""
    events: dict[str, Event] = {}
    unreadable = duplicates = 0
    for row in rows:
        record = evals.parse(row.get("@message"))
        logged_at = parse_time(row.get("@timestamp"))
        at = parse_time(record.get("timestamp")) if record else None
        if record is None or logged_at is None or at is None:
            unreadable += 1
            continue
        event = Event(record, logged_at, at)
        kept = events.get(record["event_id"])
        if kept is not None:
            duplicates += 1
            if _order(kept) <= _order(event):
                continue
        events[record["event_id"]] = event
    return events, unreadable, duplicates


def _order(event: Event):
    return (event.logged_at, event.at, evals.line(event.record))


# --- adding up ------------------------------------------------------------------------------------


def count_of(value) -> int:
    """A count or a delta as stored (DynamoDB's Decimal, or plain); 0 for anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return 0
    return int(value)


@dataclass
class Totals:
    """One entity's events on one day (a day row)."""

    perf: int = 0
    events: int = 0
    good: int = 0
    bad: int = 0
    by_reason: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, reason: str, delta: int) -> None:
        self.perf += delta
        self.events += 1
        if reason not in NOT_GOOD_OR_BAD:
            self.good += delta > 0
            self.bad += delta < 0
        counts = self.by_reason.setdefault(reason, {"perf": 0, "events": 0, "good": 0, "bad": 0})
        counts["perf"] += delta
        counts["events"] += 1
        counts["good"] += delta > 0
        counts["bad"] += delta < 0

    def item(self, entity: str, sk: str, rolled_up_at: str) -> dict:
        return {
            "entity": entity,
            "sk": sk,
            "perf": self.perf,
            "events": self.events,
            "good": self.good,
            "bad": self.bad,
            "by_reason": {reason: dict(counts) for reason, counts in sorted(self.by_reason.items())},
            "rolled_up_at": rolled_up_at,
        }


def _per_prompt(reason: str) -> bool:
    """Whether `reason` is written once per prompt of its subject: its event id ends in the prompt."""
    return evals.SCORING[reason].event_id[-1] == "prompt_id"


def model_shares(events) -> set[str]:
    """The event ids that carry the model's share of a reason written once per prompt (Article
    Completion, Admin Review): of each subject's events for one model, the earliest (then the
    smallest id). Chosen among every event read, counted or not, so an article whose prompts' events
    fall either side of a final day's end gives its model the share once."""
    groups: dict[tuple, Event] = {}
    for event in events:
        record = event.record
        if not _per_prompt(record["reason"]) or "model_id" not in record:
            continue
        key = (record["reason"], record["model_id"], record["event_id"].rsplit(":", 1)[0])
        kept = groups.get(key)
        if kept is None or (event.at, record["event_id"]) < (kept.at, kept.record["event_id"]):
            groups[key] = event
    return {event.record["event_id"] for event in groups.values()}


def completions(events) -> dict[str, set[tuple[str, str]]]:
    """{subject: {(prompt id, version)}} from the Article Completion events among `events`."""
    found: dict[str, set[tuple[str, str]]] = {}
    for event in events:
        record = event.record
        if record["reason"] == COMPLETION and "subject" in record and "prompt_id" in record:
            found.setdefault(record["subject"], set()).add((record["prompt_id"], record["prompt_version"]))
    return found


def add_up(counted: list[Event], shares: set[str], joined: dict[str, set], zone) -> dict[tuple, Totals]:
    """{(entity, day): Totals} for the events counted this run (see the module's docstring)."""
    totals: dict[tuple, Totals] = {}

    def add(entity: str, event: Event) -> None:
        key = (entity, local_day(event.at, zone))
        totals.setdefault(key, Totals()).add(event.record["reason"], event.record["rating_delta"])

    for event in counted:
        record = event.record
        applies = evals.SCORING[record["reason"]].applies_to
        model = record.get("model_id")
        prompt = (record["prompt_id"], record["prompt_version"]) if "prompt_id" in record else None
        if model and applies in FOR_MODEL:
            if not _per_prompt(record["reason"]) or record["event_id"] in shares:
                add(f"model#{model}", event)
            if prompt:
                cells = [prompt]
            elif record["reason"] == VOTE:
                cells = sorted(joined.get(record.get("subject"), ()))
            else:
                cells = []
            for prompt_id, version in cells:
                add(f"cell#{model}#{prompt_id}#{version}", event)
        if prompt and applies in FOR_PROMPT:
            add(f"prompt#{prompt[0]}#{prompt[1]}", event)
    return totals


def gating(by_reason, calibrated: bool) -> tuple[int, int]:
    """(perf, events) of a day row's `by_reason` that the gates read: the reasons that gate, and the
    judge's (`while_calibrated`) only while it is calibrated. Readers' votes, fallbacks and
    adjustments never gate (section 4.3)."""
    perf = events = 0
    for reason, counts in (by_reason or {}).items():
        scoring = evals.SCORING.get(reason)
        if scoring is None or not isinstance(counts, dict):
            continue
        if scoring.gates == "yes" or (scoring.gates == "while_calibrated" and calibrated):
            perf += count_of(counts.get("perf"))
            events += count_of(counts.get("events"))
    return perf, events


def _spans(row: dict, today: date) -> list[str]:
    """The windows a day row counts in (section 4.4): every one in `total`, today's in `daily`, the
    last seven days' in `weekly`, the calendar month's so far in `monthly`; none for a row whose sort
    key is not a day."""
    try:
        day = date.fromisoformat(str(row.get("sk", ""))[len(DAY) :])
    except ValueError:
        return []
    spans = ["total"]
    spans += ["daily"] if day == today else []
    spans += ["weekly"] if today - timedelta(days=WEEK_DAYS - 1) <= day <= today else []
    spans += ["monthly"] if today.replace(day=1) <= day <= today else []
    return spans


def windows(day_rows: list[dict], adjustments: list[dict], today: date, calibrated: bool) -> dict:
    """A model's windows from its day rows and `adjust#` rows (section 4.4): `daily_perf` today's
    row, `weekly_perf` the last seven days', `monthly_perf` the calendar month's so far,
    `total_perf` every row's and every adjustment's; `perf_events` the events behind each."""
    perf = dict.fromkeys(WINDOWS, 0)
    events = dict.fromkeys(WINDOWS, 0)
    for row in day_rows:
        gated_perf, gated_events = gating(row.get("by_reason"), calibrated)
        for window in _spans(row, today):
            perf[window] += gated_perf
            events[window] += gated_events
    perf["total"] += sum(count_of(adjustment.get("delta")) for adjustment in adjustments)
    return {**{f"{window}_perf": perf[window] for window in WINDOWS}, "perf_events": events}


def good_and_bad(day_rows: list[dict], today: date) -> dict:
    """Section 4.9's counts behind each window, from a model's day rows: `perf_good` and
    `perf_bad`, {window: events above (below) zero}. They are the day rows' `good` and `bad`, so
    every reason counts but Article Completion and Prompt Fallback, readers' votes and the judge's
    verdicts too, calibrated or not: the picker's statistic, not a gate's."""
    good = dict.fromkeys(WINDOWS, 0)
    bad = dict.fromkeys(WINDOWS, 0)
    for row in day_rows:
        for window in _spans(row, today):
            good[window] += count_of(row.get("good"))
            bad[window] += count_of(row.get("bad"))
    return {"perf_good": good, "perf_bad": bad}


def model_windows(table, key: str, today: date, is_calibrated: bool) -> dict:
    """One model's windows (`windows`) from its day rows and its `adjust#model#` rows in the evals
    table. `key` is the model's canonical id, the one its rows are kept under."""
    day_rows = query_all(table, Key("entity").eq(f"model#{key}") & Key("sk").begins_with(DAY))
    adjustments = query_all(table, Key("entity").eq(f"{ADJUST_MODEL}{key}"))
    return windows(day_rows, adjustments, today, is_calibrated)


def index_calibrated(index: dict, settings: dict) -> bool:
    """Whether the judge was calibrated at the last rollup, from the index row's `judge_agreement`
    and `judge_pairs` (what that run worked out from the pair# rows)."""
    agreement = index.get("judge_agreement")
    agreement = float(agreement) if isinstance(agreement, int | float | Decimal) else None
    return calibrated(agreement, count_of(index.get("judge_pairs")), settings)


# --- the tables -----------------------------------------------------------------------------------


def evals_table():
    return dynamo.get_table(os.environ["EVALS_TABLE"])


def _all(call, **kwargs) -> list[dict]:
    """Every item of a Query or Scan, page by page."""
    items = []
    while True:
        page = call(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def query_all(table, condition) -> list[dict]:
    return _all(table.query, KeyConditionExpression=condition)


def get_index(table=None) -> dict:
    """The index row: the watermark and the run's summaries ({} before the first run)."""
    return (table or evals_table()).get_item(Key=INDEX_KEY, ConsistentRead=True).get("Item") or {}


def day_entities(first: date, last: date, table=None) -> dict[date, set[str]]:
    """{day: the entities with a row that day}, from the index's day rows, for `first` to `last`."""
    rows = query_all(
        table or evals_table(), Key("entity").eq(INDEX) & Key("sk").between(f"{DAY}{first}", f"{DAY}{last}")
    )
    return {date.fromisoformat(row["sk"][len(DAY) :]): set(row.get("entities") or ()) for row in rows}


def write_days(table, totals: dict[tuple, Totals], days: list[date], rolled_up_at: str) -> tuple[int, int]:
    """Put every day row of `days` and the index's list of each day's entities, and delete the rows a
    day had before and no longer has. (rows written, rows deleted)."""
    by_day: dict[date, dict[str, Totals]] = {}
    for (entity, day), total in totals.items():
        by_day.setdefault(day, {})[entity] = total
    days = sorted(set(days) | set(by_day))
    if not days:
        return 0, 0
    before = day_entities(days[0], days[-1], table)
    written = deleted = 0
    with table.batch_writer() as batch:
        for day in days:
            sk = f"{DAY}{day.isoformat()}"
            entities = by_day.get(day, {})
            for entity in sorted(entities):
                batch.put_item(Item=entities[entity].item(entity, sk, rolled_up_at))
                written += 1
            for entity in sorted(before.get(day, set()) - set(entities)):
                batch.delete_item(Key={"entity": entity, "sk": sk})
                deleted += 1
            if entities:
                listed = {"entity": INDEX, "sk": sk, "entities": set(entities), "rolled_up_at": rolled_up_at}
                batch.put_item(Item=listed)
            elif day in before:
                batch.delete_item(Key={"entity": INDEX, "sk": sk})
    return written, deleted


def _verdict(item: dict) -> str | None:
    if item.get("verdict") in VERDICTS:
        return item["verdict"]
    delta = count_of(item.get("delta"))
    return "good" if delta > 0 else "bad" if delta < 0 else None


def judge_agreement(table=None) -> tuple[float | None, int]:
    """(agreement, pairs): of the subjects whose `pair#` rows hold both a judge's verdict and a
    person's, how many there are, and the share on which the two agree (None with no pair)."""
    # The table has no index on its pair rows, so this Scan reads all of it. It holds day rows only
    # for the models, prompt versions and cells in use, a few hundred a day at most, so an hourly
    # Scan costs cents a month.
    table = table or evals_table()
    items = _all(
        table.scan,
        FilterExpression=Attr("entity").begins_with(PAIR),
        ProjectionExpression="#e, #s, verdict, delta",
        ExpressionAttributeNames={"#e": "entity", "#s": "sk"},
    )
    verdicts: dict[str, dict[str, str | None]] = {}
    for item in items:
        verdicts.setdefault(item["entity"], {})[item["sk"]] = _verdict(item)
    pairs = agree = 0
    for found in verdicts.values():
        judge, human = found.get("judge"), found.get("human")
        if judge and human:
            pairs += 1
            agree += judge == human
    return (round(agree / pairs, 4) if pairs else None), pairs


def calibrated(agreement: float | None, pairs: int, settings: dict) -> bool:
    """Whether the judge's verdicts count towards the gates (section 4.5)."""
    return (
        agreement is not None
        and pairs >= settings["judge_min_pairs"]
        and agreement >= settings["judge_min_agreement"]
    )


def update_models(
    table, touched: set[str], today: date, is_calibrated: bool, settings: dict, now_iso: str
) -> tuple[int, int, list[dict]]:
    """Put the windows, the picker's counts (good_and_bad), `model_roles` and `perf_updated_at` on
    every registry model's row and on any other model an event named, never making a row
    (dynamo.set_model_fields' condition is `attribute_exists(model_id)`). `settings` are the evals
    settings in effect, whose judge the roles name. (rows updated, models with no row, the
    registry)."""
    registry = dynamo.list_models()
    by_key = {canonical_model_id(model["model_id"]): model for model in registry}
    roles = model_fleet.model_roles(
        dynamo.list_topics(),
        dynamo.get_model_config(),
        dynamo.get_model_rotation_candidates(),
        settings,
        registry,
    )
    updated = missing = 0
    for key in sorted(set(by_key) | touched):
        day_rows = query_all(table, Key("entity").eq(f"model#{key}") & Key("sk").begins_with(DAY))
        adjustments = query_all(table, Key("entity").eq(f"{ADJUST_MODEL}{key}"))
        fields = windows(day_rows, adjustments, today, is_calibrated)
        fields.update(good_and_bad(day_rows, today))
        fields.update(perf_updated_at=now_iso, model_roles=roles.get(key, []))
        row = by_key.get(key)
        if dynamo.set_model_fields(row["model_id"] if row else key, fields, expected={}):
            updated += 1
        else:
            missing += 1
    return updated, missing, registry


def classify_unclassified(registry: list[dict], region: str, bedrock=None) -> tuple[str, int, int]:
    """Classify the registry models with no purpose yet, from one listing of everything Bedrock
    lists (model_catalog.listed_models), only while each still has none. (status, classified,
    unknown); status `none` when every model has a purpose, `failed` when the listing failed."""
    unclassified = [model for model in registry if not model.get("model_purpose")]
    if not unclassified:
        return "none", 0, 0
    try:
        listed = model_catalog.listed_models(region, bedrock)
    except Exception as exc:  # noqa: BLE001 - the models stay unclassified until the next run
        print(f"evals_rollup: could not list Bedrock's models to classify {len(unclassified)}: {exc!r}")
        return "failed", 0, 0
    classified = unknown = 0
    for model in unclassified:
        updates = model_catalog.purpose_fields(model["model_id"], listed)
        if dynamo.set_model_fields(model["model_id"], updates, expected={"model_purpose": None}):
            if updates["model_purpose"] == model_catalog.UNKNOWN_PURPOSE:
                unknown += 1
            else:
                classified += 1
    return "done", classified, unknown


def _save_index(table, run: dict, *, advance_to: date | None, late_days: dict | None, now_iso: str) -> None:
    """Move the watermark forward (never back: a run alongside may have moved it further), then
    record the judge's agreement, the late counts and this run's summary."""
    if advance_to is not None:
        try:
            table.update_item(
                Key=INDEX_KEY,
                UpdateExpression="SET last_rolled_day = :day",
                ConditionExpression="attribute_not_exists(last_rolled_day) OR last_rolled_day < :day",
                ExpressionAttributeValues={":day": advance_to.isoformat()},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            pass
    values = {
        ":pairs": run["judge_pairs"],
        ":run": {key: _stored(value) for key, value in run.items() if value is not None},
        ":at": now_iso,
    }
    expression = "SET judge_pairs = :pairs, last_run = :run, rolled_up_at = :at"
    if run.get("judge_agreement") is not None:
        expression += ", judge_agreement = :agreement"
        values[":agreement"] = _stored(run["judge_agreement"])
    if late_days is not None:
        expression += ", late_days = :late_days, late_dropped = :late_dropped"
        values[":late_days"] = late_days
        values[":late_dropped"] = sum(late_days.values())
    if run.get("judge_agreement") is None:
        expression += " REMOVE judge_agreement"
    table.update_item(Key=INDEX_KEY, UpdateExpression=expression, ExpressionAttributeValues=values)


def _stored(value):
    return Decimal(str(value)) if isinstance(value, float) else value


# --- the run --------------------------------------------------------------------------------------


def _region() -> str:
    return os.environ.get("AWS_REGION") or boto3.session.Session().region_name


def run(
    *,
    trigger: str = "schedule",
    day_from: date | None = None,
    day_to: date | None = None,
    now: datetime | None = None,
    logs_client=None,
    bedrock=None,
) -> dict:
    """One rollup. Without `day_from` it adds up every day after the watermark and moves the
    watermark; with it, it adds up `day_from` to `day_to` again (each day final or not, by the same
    rule) and leaves the watermark and the late counts alone. Returns the summary its `rollup_done`
    line carries. Raises RollupError when the log or a table cannot be read or written."""
    started = time.monotonic()
    now = now or datetime.now(UTC)
    try:
        return _run(trigger, day_from, day_to, now, logs_client or logs_insights.client, bedrock, started)
    except (ClientError, BotoCoreError) as exc:
        raise RollupError(f"the evals rollup could not read or write a table or the log: {exc!r}") from exc


def _run(trigger, day_from, day_to, now, logs_client, bedrock, started) -> dict:
    now_iso = now.astimezone(UTC).isoformat()
    settings = evals_settings.get_settings()
    zone = evals_settings.zone(settings)
    today = local_day(now, zone)
    table = evals_table()
    index = get_index(table)
    watermark = date.fromisoformat(index["last_rolled_day"]) if index.get("last_rolled_day") else None
    scheduled = day_from is None
    if scheduled:
        oldest = today - timedelta(days=OLDEST_DAY_BACK)
        first = max(watermark + timedelta(days=1), oldest) if watermark else oldest
        last = today
        # From the watermark's own day: its events are read to be passed over, and to find the late
        # ones and the model's share of an article written across midnight.
        read_from = day_start(min(watermark or first, first - timedelta(days=1)), zone)
    else:
        first, last = day_from, day_to or today
        read_from = day_start(first - timedelta(days=1), zone)
    days = _days(first, last) if first <= last else []

    region = _region()
    group = os.environ["EVALS_LOG_GROUP"]
    rows, splits = read_log(read_from, now, client=logs_client, group=group, region=region)
    events, unreadable, duplicates = parse_rows(rows)

    kept: list[Event] = []
    excluded = adjustments = 0
    for event in sorted(events.values(), key=_order):
        if evals_settings.excluded(event.record, settings):
            excluded += 1
        else:
            kept.append(event)

    counted: list[Event] = []
    late_by_day: dict[str, int] = {}
    late = 0
    for event in kept:
        day = local_day(event.at, zone)
        # Late is the event's own fact, whatever the watermark says: written after its day became
        # final, even when no run has moved the watermark to that day yet.
        too_late = event.logged_at >= final_at(day, zone)
        # Final by the watermark: its on-time events were added up while it was open.
        final = watermark is not None and day <= watermark
        if scheduled:
            if too_late:
                late += 1
                written_on = local_day(event.logged_at, zone).isoformat()
                late_by_day[written_on] = late_by_day.get(written_on, 0) + 1
            if too_late or final or day < first:
                continue  # late, added up while its day was open, or older than a run adds up
        elif too_late or not first <= day <= last:
            continue  # outside the days asked for, or late: left out as the scheduled run left it
        if evals.SCORING[event.record["reason"]].applies_to == ADJUSTMENT:
            adjustments += 1
            continue
        counted.append(event)

    # Lineage is a fact whoever's scores are excluded: every event read joins votes to prompts.
    joined = completions(events.values())
    wanted = {
        event.record["subject"]
        for event in counted
        if event.record["reason"] == VOTE and "prompt_id" not in event.record
    }
    missing = sorted(subject for subject in wanted - set(joined) if SUBJECT.fullmatch(subject))
    oldest_logged = now - timedelta(days=OLDEST_DAY_BACK + 2)
    if missing and read_from > oldest_logged:
        for at in range(0, len(missing), SUBJECTS_PER_QUERY):
            quoted = ", ".join(f'"{subject}"' for subject in missing[at : at + SUBJECTS_PER_QUERY])
            found, more = read_log(
                oldest_logged,
                read_from,
                client=logs_client,
                group=group,
                region=region,
                query=COMPLETIONS_QUERY.format(subjects=quoted),
            )
            splits += more
            for subject, prompts in completions(parse_rows(found)[0].values()).items():
                joined.setdefault(subject, set()).update(prompts)

    totals = add_up(counted, model_shares(kept), joined, zone)
    days_written = sorted(set(days) | {day for _, day in totals})
    day_rows, stale_rows = write_days(table, totals, days_written, now_iso)

    agreement, pairs = judge_agreement(table)
    is_calibrated = calibrated(agreement, pairs, settings)
    touched = {entity[len("model#") :] for entity, _ in totals if entity.startswith("model#")}
    models, not_in_registry, registry = update_models(
        table, touched, today, is_calibrated, settings, now_iso
    )
    classify, classified, unknown = classify_unclassified(registry, region, bedrock)

    advance_to = late_days = None
    if scheduled:
        newest_final = newest_final_day(now, zone)
        if watermark is None or newest_final > watermark:
            advance_to = newest_final
        # The late counts of the days this read covered whole are this run's; earlier days' stay.
        covered_from = (watermark or first).isoformat()
        stored = {day: count_of(n) for day, n in (index.get("late_days") or {}).items() if day < covered_from}
        late_days = {**stored, **late_by_day}
    summary = {
        "trigger": trigger,
        "status": "done",
        "from_day": first.isoformat(),
        "to_day": max([last, *days_written]).isoformat() if days_written else last.isoformat(),
        "last_rolled_day": (advance_to or watermark).isoformat() if (advance_to or watermark) else None,
        "rows": len(rows),
        "events": len(counted),
        "duplicates": duplicates,
        "unreadable": unreadable,
        "excluded": excluded,
        "late": late,
        "late_dropped": count_of(index.get("late_dropped")) if late_days is None else sum(late_days.values()),
        "adjustments": adjustments,
        "day_rows": day_rows,
        "stale_rows": stale_rows,
        "models": models,
        "not_in_registry": not_in_registry,
        "classify": classify,
        "classified": classified,
        "unknown": unknown,
        "judge_pairs": pairs,
        "judge_agreement": agreement,
        "calibrated": is_calibrated,
        "splits": splits,
        "ms": int((time.monotonic() - started) * 1000),
    }
    _save_index(table, summary, advance_to=advance_to, late_days=late_days, now_iso=now_iso)
    evidence.emit("rollup_done", **summary)
    return summary
