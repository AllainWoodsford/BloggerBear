"""The dispatcher's decisions: which topic is due for what this minute, how a job is claimed, and
what is sent (docs/enhancements/model-ranch-prompt-assets-evals.md sections 4.5, 4.7 and 5.1).

One dispatcher run (a *tick*) every minute replaces the two EventBridge Scheduler schedules each
topic owns today. For every topic it reads the same effective expressions the stagger writes
(common/stagger.effective_schedules) through the cron matcher (common/cron_match.py):

- **research**: the cadence has a turn in `(last_tick, now]` and the topic is due
  (common/research_schedule.is_due, the rule the research tick applies itself). The topic is
  *claimed* with a conditional write on its row, then one message goes to the FIFO work queue,
  `MessageGroupId` the topic id (one job per topic at a time), deduplicated by the minute.
- **daily**: the first of the cadence's turns for today, in the topic's `daily_timezone`, has
  passed and the topic has not been dispatched today. Claimed the same way, then the daily-cycle
  state machine is started with an execution named `<slug>-<local date>-<n>`, so a repeat is
  refused by Step Functions itself (`ExecutionAlreadyExists` is treated as consumed). At most
  `daily_starts_per_tick` starts leave one tick; the rest wait a minute.

A **claim** is what stops two overlapping ticks queueing the same work twice: daily
`SET last_daily_dispatched_on = :today ADD daily_claim_n :one` only when the row does not already
say today; research `SET research_claimed_at = :now` only when the last claim is older than the
research interval. A send or start that fails after its claim releases it, so the next tick tries
again. The claim strings are constants here, and the tests hold them to the spec.

**Shadow mode** (`dispatch_shadow`, on by default): a topic still on its schedules is never sent
or started; the tick records what it would have done on the row (`shadow_last_research_dispatch_at`,
`shadow_last_daily_dispatch_at`) and counts `shadow_mismatches` against what the Scheduler path
really did. A topic whose `dispatch_mode` is `queue` has no schedules left, so it is dispatched for
real whatever the shadow setting says; a topic on its schedules is only ever shadowed. Zero
mismatches over a day is what the per-topic cutover (Q2) asks for, so a mismatch is only ever
counted where the two paths really disagreed:

- **research**: a turn is recorded when the live path would have claimed it (the topic due, and
  no record younger than the claim's expiry, research_claim_expiry), or when the Scheduler path's
  own research tick, which fires in the same minute and may read the row first, has already
  stamped `last_research_at` within SHADOW_TOLERANCE of now. A recorded turn is judged once, by the
  tick whose window `(last_tick, now]` holds the turn plus SHADOW_TOLERANCE: a mismatch when
  `last_research_at` is missing or more than SHADOW_TOLERANCE from the turn (the research tick
  stamps its start within its 120 s timeout). Judging after the fact rather than at the next turn
  means neither order of the two ticks miscounts, and a missed turn is seen before the next turn's
  stamp can overwrite the evidence. A `rate(...)` research cadence is recorded the same way (once
  an interval) but never judged: every minute is its turn, the Scheduler's firing minute for a rate
  cannot be derived from the expression, and the first due minute sits exactly DUE_TOLERANCE
  before the next heartbeat, so any comparison would sit on the tolerance edge.
- **a drifted heartbeat** (decided in Q1): a Scheduler heartbeat that runs more than
  SHADOW_TOLERANCE off the computed minute, early or late, is not detected beyond the first turn
  after the drift began, which is counted once. From then on the topic is not due at the computed
  minute (just researched, or its interval not yet out), so those turns are neither recorded nor
  judged. One mismatch is enough to stop the cutover; a drift that was there before shadow mode
  began leaves no research turn recorded at all, which the Q2 pre-flight refuses too. Missing
  it costs little: at cutover the research minute moves to the computed one, the stagger's own.
  Counting more would mean judging every move of `last_research_at`, and every operator run-now
  moves it.
- **a run-now** is never a mismatch. A stamp that lands within SHADOW_TOLERANCE of a turn is that
  turn's; one later than that when the turn is judged (the judging tick runs within a minute of
  the tolerance running out) is a run-now made since, which replaced the turn's own evidence, so
  the turn is left unjudged rather than counted. A run-now at any other time stamps a minute no
  recorded turn is compared with, or one the next turn then finds not due.
- **daily**: a turn whose previous recorded day saw no execution of this topic (_daily_ran). A
  day the check could not answer (Step Functions refused, or its pages ran out) is counted in
  `shadow_unchecked`, never as a match: a missing grant must not pass as zero mismatches. A day it
  did answer is counted in `shadow_checked_days`, so a full day watched can be told from a first
  shadow day that had nothing to compare yet. The Q2 pre-flight asks for zero mismatches, zero
  unchecked days and at least one checked day.

**The cutover** (Q2, the admin API's `topics update <id> --dispatch-mode queue`): before a topic's
two schedules are deleted, cutover_refusals must find nothing wrong (it is not paused, the
dispatcher reads its cadences and zone, its slug and queue ids are usable, and shadow mode watched
it as above), and every cadence of a topic on the queue must stay readable (cadence_problem).

**Cutover day.** The Scheduler may already have run today's daily cycle when a topic is flipped to
the queue. The live path counts a same-day shadow daily record as today's dispatch (a live topic
never writes one, so it can only mean the Scheduler owned today), and the Q2 flip also writes
`last_daily_dispatched_on` (today, in the topic's zone) when today's turn has already passed
(cutover_day), which covers a flip made while shadow mode was off.

**Settings** are two more fields of the `pipeline` config row (SETTINGS below, the model catalog's
pattern): `dispatch_shadow` and `daily_starts_per_tick` (1 to 10). `last_tick` on the same row is
the window's start for the next tick.

**Errors.** A failure of the whole tick (the Topics scan, the queue send, Step Functions
throttling) raises DispatchError, so the dispatcher's Errors alarm fires; a failure of one topic
(an unreadable zone, a missing field) is printed and the next topic is considered. A cadence the
matcher refuses is printed as `dispatcher: cadence unparsable topic_id=... expression=...` and
counted in `skipped_unparsable`, never guessed at.

The dispatcher Lambda (dispatcher_handler.py, every minute from infra/modules/work-queue) runs one
tick and emits the `dispatcher_tick` evidence line from the counts it returns. Topic-agnostic:
nothing here knows about any adapter or domain.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from botocore.exceptions import ClientError

from common import cron_match, dynamo, stagger
from common.cron_match import CadenceError
from common.research_schedule import DUE_TOLERANCE, is_due, resolve_interval_hours
from common.scheduler import DEFAULT_TIMEZONE

# The pipeline config row (common/dynamo.get_pipeline_config; its id, as dynamo spells it).
PIPELINE_CONFIG_ID = "pipeline"
# The settings this module reads from that row, and their defaults. A setting that is unset, or
# holds something invalid, takes the default (effective_settings).
DISPATCH_SHADOW = "dispatch_shadow"
DAILY_STARTS_PER_TICK = "daily_starts_per_tick"
MAX_DAILY_STARTS_PER_TICK = 10
SETTINGS: dict[str, object] = {DISPATCH_SHADOW: True, DAILY_STARTS_PER_TICK: 3}
# Where the last tick's time is kept, for the next tick's research window.
LAST_TICK = "last_tick"

# A topic's dispatch_mode: on its two schedules (the default, and the permanent undo), or on the
# queue. The admin API flips a topic (`topics update <id> --dispatch-mode`, the cutover below).
SCHEDULE_MODE = "schedule"
QUEUE_MODE = "queue"
RESEARCH_JOB = "research"

# An execution name is the slug, the local date and the claim number: <= 40 + 11 + a number, inside
# Step Functions' 80 characters.
SLUG_MAX_CHARS = 40
_NOT_SLUG = re.compile(r"[^a-z0-9-]")
_SLUG_LETTER_OR_DIGIT = re.compile(r"[a-z0-9]")

# SQS takes a message group id and a deduplication id of at most 128 letters, digits and
# punctuation. The topic id is the first and starts the second (research_dedup_id, and the longer
# manual_research_dedup_id with its token of MANUAL_TOKEN_CHARS). Matched with fullmatch: `$`
# would also match before a trailing newline, which SQS refuses.
_SQS_ID_MAX_CHARS = 128
_SQS_ID = re.compile(r"[A-Za-z0-9!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~]+")
MANUAL_TOKEN_CHARS = 32

# The day count the daily check answered (see the module docstring).
SHADOW_CHECKED_DAYS = "shadow_checked_days"

# The claims (spec 4.7), word for word.
DAILY_CLAIM_UPDATE = "SET last_daily_dispatched_on = :today ADD daily_claim_n :one"
DAILY_CLAIM_CONDITION = (
    "attribute_not_exists(last_daily_dispatched_on) OR last_daily_dispatched_on <> :today"
)
RESEARCH_CLAIM_UPDATE = "SET research_claimed_at = :now"
RESEARCH_CLAIM_CONDITION = (
    "attribute_not_exists(research_claimed_at) OR research_claimed_at < :expiry"
)
# A release puts the claimed field back (REMOVE when there was nothing before) only while it still
# holds this tick's value. daily_claim_n is left as it is: the name it numbered may have reached
# Step Functions, and a name is never reused.
DAILY_RELEASE_CONDITION = "last_daily_dispatched_on = :today"
RESEARCH_RELEASE_CONDITION = "research_claimed_at = :now"

# A research mismatch in shadow mode: the recorded turn and the minute last_research_at moved differ
# by more than this. It is also how long after a turn the turn is judged, and how recent a stamp
# must be to say the Scheduler path's tick took this very turn.
SHADOW_TOLERANCE = timedelta(minutes=5)
# How many pages of executions the daily shadow check reads back before giving up (newest first,
# 100 a page).
_MAX_EXECUTION_PAGES = 10
# Step Functions error codes that mean "try again later", which the tick raises on so the alarm
# fires; everything else about a start is that topic's problem.
_THROTTLED = frozenset({"ThrottlingException", "TooManyRequestsException", "ExecutionLimitExceeded"})
_ALREADY_EXISTS = "ExecutionAlreadyExists"


class DispatchError(RuntimeError):
    """The tick could not do its job (the scan, a send, a throttled start): raised out of
    Dispatcher.tick so the dispatcher's Errors alarm fires."""


# --- settings -------------------------------------------------------------------------------------


def _whole(value) -> bool:
    """A whole number as Python or as the Decimal DynamoDB hands back; never a bool."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, Decimal) and value == int(value))


def validate_setting(name: str, value) -> str | None:
    """Why `value` is not a valid `name`, or None when it is (None, which clears a setting, is
    always valid)."""
    if value is None:
        return None
    if name not in SETTINGS:
        return f"unknown setting '{name}'"
    if name == DISPATCH_SHADOW:
        return None if isinstance(value, bool) else f"'{DISPATCH_SHADOW}' must be true or false"
    if _whole(value) and 1 <= value <= MAX_DAILY_STARTS_PER_TICK:
        return None
    return f"'{DAILY_STARTS_PER_TICK}' must be a whole number from 1 to {MAX_DAILY_STARTS_PER_TICK}"


def effective_settings(row: dict | None) -> dict:
    """Every setting's value in effect: the row's where it is valid, else the default."""
    row = row or {}
    settings = {}
    for name, default in SETTINGS.items():
        value = row.get(name)
        if value is not None and validate_setting(name, value) is not None:
            print(f"dispatcher: ignoring invalid {name}={value!r}; using {default!r}")
            value = None
        settings[name] = default if value is None else (int(value) if _whole(value) else value)
    return settings


# --- names and messages (spec 4.7) ----------------------------------------------------------------


def dispatch_mode(topic: dict) -> str:
    """`queue` when the topic says so; anything else, or nothing, is schedule mode."""
    return QUEUE_MODE if topic.get("dispatch_mode") == QUEUE_MODE else SCHEDULE_MODE


def slug(topic_id: str) -> str:
    """The topic id lower-cased with every character outside [a-z0-9-] replaced by '-', at most
    SLUG_MAX_CHARS: the first part of an execution name. The Q2 pre-flight refuses a topic whose
    slug collides with another's."""
    return _NOT_SLUG.sub("-", (topic_id or "").lower())[:SLUG_MAX_CHARS]


def execution_name(topic_id: str, local_date: str, n: int) -> str:
    """The dispatcher's execution name: `<slug>-<local date>-<n>`, n the topic's claim count."""
    return f"{slug(topic_id)}-{local_date}-{n}"


def manual_execution_name(topic_id: str, local_date: str, now: datetime) -> str:
    """An operator's trigger (Q2): `<slug>-<local date>-manual-<HHMMSS>`, `now` on the topic's
    local clock like the date."""
    return f"{slug(topic_id)}-{local_date}-manual-{now.strftime('%H%M%S')}"


def _iso(moment: datetime) -> str:
    """A UTC time to the second: the claim values are compared as strings in DynamoDB, so every
    value this module writes has the same width."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def research_message(topic_id: str, *, now: datetime, force: bool = False) -> dict:
    """The body of a research job; `force` is only ever true from an operator (the admin API's
    manual trigger of a topic on the queue).

    The contract the research tick honours for a queue record (research_tick_handler): it ignores
    `job`, and it uses `due_checked_at` as the run's time both for its own is_due check and for the
    last_research_at stamp a successful fetch writes (never moving the stamp backwards), never the
    time it ran.
    The dispatcher's next is_due reads that stamp, so a stamp made after a queue wait longer than
    DUE_TOLERANCE would make the next fixed-minute turn not due and skip a heartbeat. Only under
    that contract is a message that waited in the queue harmless."""
    return {
        "topic_id": topic_id,
        "job": RESEARCH_JOB,
        "force": force,
        "due_checked_at": _iso(now),
        "dispatched_at": _iso(now),
    }


def research_dedup_id(topic_id: str, now: datetime) -> str:
    """`<topic_id>:research:<yyyy-mm-ddTHH:MM>`: SQS's own guard against the same minute twice."""
    return f"{topic_id}:{RESEARCH_JOB}:{now.astimezone(UTC).strftime('%Y-%m-%dT%H:%M')}"


def manual_research_dedup_id(topic_id: str, token: str) -> str:
    """`<topic_id>:research:manual:<token>`, a deduplication id of its own for an operator's
    research job, so SQS never takes it for the dispatcher's job of the same minute, or for the
    operator's last one. `token` is fresh each time (MANUAL_TOKEN_CHARS hex characters)."""
    return f"{topic_id}:{RESEARCH_JOB}:manual:{token}"


def research_claim_expiry(now: datetime, interval_hours: int) -> str:
    """The `:expiry` of the research claim: a claim older than this no longer holds. One interval
    back, less the tolerance research_schedule.is_due allows, so the claim and the due rule agree:
    without it a topic claimed at exactly :05 is due at the next :05 but not yet claimable, and
    skips a whole heartbeat."""
    return _iso(now - timedelta(hours=interval_hours) + DUE_TOLERANCE)


# --- claims ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """A claim this tick holds: what the field held before (None: nothing), and for a daily claim
    the number the execution name takes."""

    previous: str | None
    n: int | None = None


def _condition_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def claim_daily(table, topic_id: str, today: str) -> Claim | None:
    """Claim today's daily cycle for the topic, or None when today is already claimed (or the topic
    is gone: a vanished row is never recreated by a claim)."""
    try:
        response = table.update_item(
            Key={"topic_id": topic_id},
            UpdateExpression=DAILY_CLAIM_UPDATE,
            ConditionExpression=f"attribute_exists(topic_id) AND ({DAILY_CLAIM_CONDITION})",
            ExpressionAttributeValues={":today": today, ":one": 1},
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if _condition_failed(exc):
            return None
        raise
    before = response.get("Attributes") or {}
    return Claim(previous=before.get("last_daily_dispatched_on"), n=int(before.get("daily_claim_n", 0)) + 1)


def release_daily(table, topic_id: str, today: str, previous: str | None) -> None:
    """Give today's claim back after a start that failed, if the row still carries it."""
    _release(
        table,
        topic_id,
        field="last_daily_dispatched_on",
        condition=DAILY_RELEASE_CONDITION,
        held={":today": today},
        previous=previous,
    )


def claim_research(table, topic_id: str, now: datetime, interval_hours: int) -> Claim | None:
    """Claim a research job for the topic, or None when a claim younger than the interval holds."""
    try:
        response = table.update_item(
            Key={"topic_id": topic_id},
            UpdateExpression=RESEARCH_CLAIM_UPDATE,
            ConditionExpression=f"attribute_exists(topic_id) AND ({RESEARCH_CLAIM_CONDITION})",
            ExpressionAttributeValues={
                ":now": _iso(now),
                ":expiry": research_claim_expiry(now, interval_hours),
            },
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if _condition_failed(exc):
            return None
        raise
    before = response.get("Attributes") or {}
    return Claim(previous=before.get("research_claimed_at"))


def release_research(table, topic_id: str, now: datetime, previous: str | None) -> None:
    """Give the research claim back after a send that failed, if the row still carries it."""
    _release(
        table,
        topic_id,
        field="research_claimed_at",
        condition=RESEARCH_RELEASE_CONDITION,
        held={":now": _iso(now)},
        previous=previous,
    )


def _release(table, topic_id: str, *, field: str, condition: str, held: dict, previous) -> None:
    if previous is None:
        expression, values = f"REMOVE {field}", held
    else:
        expression, values = f"SET {field} = :previous", {**held, ":previous": previous}
    try:
        table.update_item(
            Key={"topic_id": topic_id},
            UpdateExpression=expression,
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if not _condition_failed(exc):  # someone else holds it now; theirs to keep
            raise


# --- the tick -------------------------------------------------------------------------------------


@dataclass
class Tick:
    """What one tick did: the counts the dispatcher_tick evidence line carries."""

    considered: int = 0
    dispatched: int = 0
    started: int = 0
    skipped_unparsable: int = 0

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "dispatched": self.dispatched,
            "started": self.started,
            "skipped_unparsable": self.skipped_unparsable,
        }


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def topic_zone(topic: dict) -> tzinfo:
    """The zone the topic's daily cadence is read in: its daily_timezone, else UTC (the zone a topic
    from before the field existed keeps, as the admin API reads it). ValueError when the runtime
    does not know it."""
    name = topic.get("daily_timezone") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"daily_timezone {name!r} is not a zone this runtime knows") from exc


def effective_cadences(topic: dict, *, config: dict | None, topic_count: int) -> dict:
    """The topic's effective expressions as the stagger writes them for the Scheduler
    (stagger.effective_schedules), from the arguments every tick passes: the topic's cadences and
    slot, the pipeline row's stagger, and how many topics there are. The admin API reads a queue
    topic's cadences through this too, so it judges exactly what the dispatcher will read."""
    return stagger.effective_schedules(
        research_cadence=topic.get("research_cadence") or "",
        daily_cadence=topic.get("daily_cadence") or "",
        slot=stagger.topic_slot(topic),
        stagger_minutes=stagger.resolve_stagger_minutes(config),
        topic_count=topic_count,
    )


def parse_cadences(
    topic: dict, *, config: dict | None, topic_count: int
) -> tuple[cron_match.Cadence, cron_match.Cadence]:
    """The effective research and daily cadences, parsed (a daily rate is refused). CadenceError
    names the expression the matcher refused."""
    effective = effective_cadences(topic, config=config, topic_count=topic_count)
    return cron_match.parse(effective["research"]), cron_match.parse(effective["daily"], allow_rate=False)


def _local_date(value, zone: tzinfo) -> date | None:
    moment = _parse_time(value)
    return moment.astimezone(zone).date() if moment else None


def _in_window(moment: datetime, last_tick: datetime | None, now: datetime) -> bool:
    """Whether `moment` falls in this tick's window: `(last_tick, now]` as research_turn reads it
    (at most MAX_WINDOW long), or the last minute when there is no earlier last_tick. Consecutive
    windows meet, so a moment falls in exactly one tick's window."""
    if last_tick is None or last_tick >= now:
        start = now - timedelta(minutes=1)
    else:
        start = max(last_tick, now - cron_match.MAX_WINDOW)
    return start < moment <= now


def _daily_done(topic: dict, today: date, zone: tzinfo) -> bool:
    """Whether the topic's daily cycle has had today's turn: the dispatcher claimed it, or shadow
    mode recorded today's turn while the topic was still on its schedules (the cutover day; a live
    topic never writes a shadow field, so the Scheduler owned today)."""
    return topic.get("last_daily_dispatched_on") == today.isoformat() or (
        _local_date(topic.get("shadow_last_daily_dispatch_at"), zone) == today
    )


class Dispatcher:
    """One tick's worth of decisions and sends. The queue and Step Functions clients, the queue URL
    and the state machine ARN are given (the handler builds them from its environment); the
    Topics and config tables default to the ones common/dynamo reads."""

    def __init__(self, *, sqs, sfn, queue_url: str, state_machine_arn: str, topics_table=None):
        self.sqs = sqs
        self.sfn = sfn
        self.queue_url = queue_url
        self.state_machine_arn = state_machine_arn
        self._topics_table = topics_table

    @property
    def topics_table(self):
        if self._topics_table is None:
            self._topics_table = dynamo.get_table(os.environ["TOPICS_TABLE"])
        return self._topics_table

    def tick(self, now: datetime | None = None) -> dict:
        """Consider every topic once and return the counts (Tick.as_dict). Raises DispatchError when
        the tick as a whole could not run; a topic's own trouble is printed and skipped."""
        now = now or datetime.now(UTC)
        try:
            config = dynamo.get_pipeline_config() or {}
            topics = dynamo.list_topics(table=self.topics_table)
        except Exception as exc:
            raise DispatchError(f"could not read the topics or the pipeline config: {exc!r}") from exc
        settings = effective_settings(config)
        last_tick = _parse_time(config.get(LAST_TICK))
        counts = Tick()
        for topic in sorted(topics, key=lambda item: item.get("topic_id") or ""):
            topic_id = topic.get("topic_id") or ""
            if topic.get("paused") is True:
                continue
            live = dispatch_mode(topic) == QUEUE_MODE
            if not live and not settings[DISPATCH_SHADOW]:
                continue
            counts.considered += 1
            try:
                self._consider(
                    topic,
                    now=now,
                    last_tick=last_tick,
                    config=config,
                    topic_count=len(topics),
                    live=live,
                    starts_left=settings[DAILY_STARTS_PER_TICK] - counts.started,
                    counts=counts,
                )
            except CadenceError as exc:
                print(f"dispatcher: cadence unparsable topic_id={topic_id} expression={exc.expression!r}")
                counts.skipped_unparsable += 1
            except DispatchError:
                raise
            except Exception as exc:  # noqa: BLE001 - one topic's trouble never stops the rest
                print(f"dispatcher: topic_id={topic_id} skipped: {exc!r}")
        dynamo.update_config_row(PIPELINE_CONFIG_ID, {LAST_TICK: _iso(now)})
        return counts.as_dict()

    def _consider(
        self, topic: dict, *, now, last_tick, config, topic_count, live, starts_left, counts
    ) -> None:
        topic_id = topic["topic_id"]
        research, daily = parse_cadences(topic, config=config, topic_count=topic_count)
        zone = topic_zone(topic)
        interval_hours = resolve_interval_hours(topic, config)

        research_turn = cron_match.research_turn(research, last_tick, now)
        if live:
            if research_turn and is_due(now, topic.get("last_research_at"), interval_hours):
                if self._send_research(topic_id, now, interval_hours):
                    counts.dispatched += 1
        else:
            if not research.is_rate:
                self._judge_research(topic, last_tick, now)
            if research_turn:
                self._shadow_research(topic, now, interval_hours, is_rate=research.is_rate)

        if cron_match.daily_turn_passed(daily, now, zone):
            today = now.astimezone(zone).date()
            if live:
                if not _daily_done(topic, today, zone) and starts_left > 0:
                    if self._start_daily(topic_id, today.isoformat()):
                        counts.started += 1
            elif _local_date(topic.get("shadow_last_daily_dispatch_at"), zone) != today:
                self._shadow_daily(topic, now, today, zone)

    # --- the real thing ---------------------------------------------------------------------------

    def _send_research(self, topic_id: str, now: datetime, interval_hours: int) -> bool:
        claim = claim_research(self.topics_table, topic_id, now, interval_hours)
        if claim is None:
            return False
        try:
            self.sqs.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps(research_message(topic_id, now=now)),
                MessageGroupId=topic_id,
                MessageDeduplicationId=research_dedup_id(topic_id, now),
            )
        except Exception as exc:
            release_research(self.topics_table, topic_id, now, claim.previous)
            raise DispatchError(f"could not send the research job for topic_id={topic_id}: {exc!r}") from exc
        return True

    def _start_daily(self, topic_id: str, today: str) -> bool:
        claim = claim_daily(self.topics_table, topic_id, today)
        if claim is None:
            return False
        name = execution_name(topic_id, today, claim.n)
        try:
            self.sfn.start_execution(
                stateMachineArn=self.state_machine_arn,
                name=name,
                input=json.dumps({"topic_id": topic_id}),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == _ALREADY_EXISTS:
                # Step Functions already has today's run under this name: consumed, no retry.
                print(f"dispatcher: execution exists topic_id={topic_id} name={name}; consumed")
                return False
            release_daily(self.topics_table, topic_id, today, claim.previous)
            if code in _THROTTLED:
                raise DispatchError(f"StartExecution throttled for topic_id={topic_id}: {exc!r}") from exc
            raise
        except Exception as exc:
            release_daily(self.topics_table, topic_id, today, claim.previous)
            raise DispatchError(f"could not start the daily cycle for topic_id={topic_id}: {exc!r}") from exc
        return True

    # --- shadow mode -------------------------------------------------------------------------------

    def _shadow_research(self, topic: dict, now: datetime, interval_hours: int, *, is_rate: bool) -> None:
        """At a research turn, record it (with no mismatch: _judge_research decides that later)
        when the live path would have claimed it: the topic is due, and no record is younger than
        the claim's expiry (RESEARCH_CLAIM_CONDITION's rule), so a rate cadence is recorded once an
        interval rather than on every due minute. A cron turn is also recorded when the Scheduler
        path's research tick, firing in the same minute, has already stamped last_research_at
        within SHADOW_TOLERANCE of now: it took this very turn, and the record must not depend on
        which of the two ticks read the row first."""
        recorded = _parse_time(topic.get("shadow_last_research_dispatch_at"))
        if recorded is not None and _iso(recorded) >= research_claim_expiry(now, interval_hours):
            return  # the live claim would still hold: no second job this interval
        moved = _parse_time(topic.get("last_research_at"))
        stamped_this_turn = not is_rate and moved is not None and abs(now - moved) <= SHADOW_TOLERANCE
        if stamped_this_turn or is_due(now, topic.get("last_research_at"), interval_hours):
            self._write_shadow(topic["topic_id"], "shadow_last_research_dispatch_at", now, 0)

    def _judge_research(self, topic: dict, last_tick: datetime | None, now: datetime) -> None:
        """Judge the recorded research turn once, on the tick whose window holds the turn plus
        SHADOW_TOLERANCE: by then the Scheduler path's tick of that minute has stamped
        last_research_at (its start, within its 120 s timeout), and the next turn's stamp has not
        yet replaced it. A mismatch when last_research_at is missing or more than SHADOW_TOLERANCE
        from the turn. Never called for a rate cadence (see the module docstring)."""
        turn = _parse_time(topic.get("shadow_last_research_dispatch_at"))
        if turn is None or not _in_window(turn + SHADOW_TOLERANCE, last_tick, now):
            return
        moved = _parse_time(topic.get("last_research_at"))
        if moved is not None and abs(moved - turn) <= SHADOW_TOLERANCE:
            return
        if moved is not None and moved > turn + SHADOW_TOLERANCE:
            # Stamped after the turn's own tolerance ran out and before this tick: only an
            # operator's run-now stamps there, and it replaced the turn's evidence. Not a mismatch.
            print(
                f"dispatcher: shadow research turn not judged topic_id={topic['topic_id']} "
                f"turn={_iso(turn)} last_research_at={_iso(moved)}: stamped since by a run-now"
            )
            return
        print(
            f"dispatcher: shadow research mismatch topic_id={topic['topic_id']} "
            f"turn={_iso(turn)} last_research_at={_iso(moved) if moved else None}"
        )
        self.topics_table.update_item(
            Key={"topic_id": topic["topic_id"]},
            UpdateExpression="ADD shadow_mismatches :n",
            ConditionExpression="attribute_exists(topic_id)",
            ExpressionAttributeValues={":n": 1},
        )

    def _shadow_daily(self, topic: dict, now: datetime, today: date, zone: tzinfo) -> None:
        """Record today's turn, and count a mismatch when the previous recorded day saw no execution
        of this topic on the Scheduler path. A check that could not run is no mismatch, and no match
        either: it counts one day in `shadow_unchecked`. A check that answered, either way, counts
        one day in `shadow_checked_days`: the first shadow day has nothing to compare, and must not
        pass for a day watched."""
        previous = _parse_time(topic.get("shadow_last_daily_dispatch_at"))
        mismatch = unchecked = checked = 0
        if previous is not None:
            previous_day = previous.astimezone(zone).date()
            ran = self._daily_ran(topic["topic_id"], previous_day, zone)
            if ran is None:
                unchecked = 1
                print(
                    f"dispatcher: shadow daily unchecked topic_id={topic['topic_id']} "
                    f"local_date={previous_day.isoformat()}"
                )
            else:
                checked = 1
                if ran is False:
                    mismatch = 1
                    print(
                        f"dispatcher: shadow daily mismatch topic_id={topic['topic_id']} "
                        f"local_date={previous_day.isoformat()}"
                    )
        self._write_shadow(
            topic["topic_id"],
            "shadow_last_daily_dispatch_at",
            now,
            mismatch,
            daily_counts={"unchecked": unchecked, "checked": checked},
        )

    def _daily_ran(self, topic_id: str, day: date, zone: tzinfo) -> bool | None:
        """Whether an execution of the daily cycle for `topic_id` started on `day` (a local date):
        the executions of that day, newest first, each described for its input; the walk stops at
        the first execution older than the day. None when Step Functions could not be asked or the
        pages ran out first.

        The dispatcher's role (`lambda_exec`) needs `states:ListExecutions` on the state machine
        and `states:DescribeExecution` on its executions (`arn:aws:states:<region>:<account>:
        execution:<state machine name>:*`), which the work-queue module's policy grants beside
        SendMessage and StartExecution (infra/modules/work-queue). Without them every check is
        AccessDenied, and every day is counted as unchecked."""
        start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
        end = start + timedelta(days=1)
        try:
            kwargs = {"stateMachineArn": self.state_machine_arn, "maxResults": 100}
            for _ in range(_MAX_EXECUTION_PAGES):
                page = self.sfn.list_executions(**kwargs)
                for execution in page.get("executions", []):
                    started = execution.get("startDate")
                    if started is None:
                        continue
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=UTC)
                    if started < start:
                        return False
                    if started >= end:
                        continue
                    described = self.sfn.describe_execution(executionArn=execution["executionArn"])
                    try:
                        if json.loads(described.get("input") or "{}").get("topic_id") == topic_id:
                            return True
                    except (json.JSONDecodeError, AttributeError):
                        continue
                if not page.get("nextToken"):
                    return False
                kwargs["nextToken"] = page["nextToken"]
        except Exception as exc:  # noqa: BLE001 - a check that failed is not a mismatch
            print(f"dispatcher: could not check the executions of topic_id={topic_id}: {exc!r}")
            return None
        print(f"dispatcher: execution check ran out of pages topic_id={topic_id}")
        return None

    def _write_shadow(
        self, topic_id: str, field: str, now: datetime, mismatch: int, *, daily_counts: dict | None = None
    ) -> None:
        """Record a shadow turn and add to the counts. A daily record also adds to
        `shadow_unchecked` and `shadow_checked_days` (`daily_counts`; zero included, so the fields
        are there from the first shadow day)."""
        update, values = "SET #f = :t ADD shadow_mismatches :n", {":t": _iso(now), ":n": mismatch}
        if daily_counts is not None:
            update = f"{update}, shadow_unchecked :u, {SHADOW_CHECKED_DAYS} :c"
            values = {**values, ":u": daily_counts["unchecked"], ":c": daily_counts["checked"]}
        self.topics_table.update_item(
            Key={"topic_id": topic_id},
            UpdateExpression=update,
            ConditionExpression="attribute_exists(topic_id)",
            ExpressionAttributeNames={"#f": field},
            ExpressionAttributeValues=values,
        )


# --- the cutover (Q2) -----------------------------------------------------------------------------
#
# A topic moves from its two schedules to the queue with `topics update <id> --dispatch-mode queue`
# (admin_api_handler). The checks below are what the admin API refuses that move on, each as one
# reason in plain words; it answers 400 with them before anything is deleted.


def cadence_problem(topic: dict, *, config: dict | None, topic_count: int) -> str | None:
    """Why the dispatcher could not run this topic, in the matcher's words, or None: its research
    cadence, then its daily cadence (a rate refused), then its zone. The dispatcher would skip such
    a topic every minute (`cadence unparsable`), so a topic on the queue is never allowed one.
    `topic_count` counts every topic, this one included, as a tick does.

    Each cadence is judged twice: as the stagger writes it now, and as stored. The dispatcher works
    out the first again every tick, and it falls back to the stored text whenever the topic's
    offset comes to 0 (`stagger_minutes` set to 0, or, for the daily cadence, a topic added or
    deleted), with no check made then. So a daily `rate(...)`, which the stagger turns into a cron
    only while the offset is not 0, is refused whatever the stagger is now (spec 4.8)."""
    effective = effective_cadences(topic, config=config, topic_count=topic_count)
    for kind, allow_rate in (("research", True), ("daily", False)):
        stored = (topic.get(f"{kind}_cadence") or "").strip()
        for expression in dict.fromkeys((effective[kind], stored)):
            try:
                cron_match.parse(expression, allow_rate=allow_rate)
            except CadenceError as exc:
                # The stagger may have rewritten the cadence (its minute); name both when it did.
                shown = stored or exc.expression
                written = f" (written as {exc.expression!r})" if shown != exc.expression else ""
                return f"the dispatcher cannot read its {kind} cadence {shown!r}{written}: {exc.why}"
    try:
        topic_zone(topic)
    except ValueError as exc:
        return str(exc)
    return None


def slug_problem(topic_id: str, topics) -> str | None:
    """Why the topic's slug (spec 4.7, the start of its daily runs' execution names) will not do,
    or None: it must name a letter or a digit, and no other topic in `topics` may share it, or the
    two topics' runs would take each other's names and one would be refused as already started."""
    own = slug(topic_id)
    if not _SLUG_LETTER_OR_DIGIT.search(own):
        return f"its slug {own!r} (the start of its daily runs' execution names) has no letter or digit"
    for other in topics:
        other_id = other.get("topic_id")
        if other_id and other_id != topic_id and slug(other_id) == own:
            return (
                f"its slug {own!r} is also topic '{other_id}''s, so the two topics' daily runs "
                "would share execution names"
            )
    return None


def queue_id_problem(topic_id: str) -> str | None:
    """Why SQS would refuse the topic id as a research job's message group id (the deduplication
    ids start with it too), or None. A send SQS refuses fails the whole tick, not only the topic."""
    longest = manual_research_dedup_id(topic_id, "0" * MANUAL_TOKEN_CHARS)
    if _SQS_ID.fullmatch(topic_id or "") and len(longest) <= _SQS_ID_MAX_CHARS:
        return None
    limit = _SQS_ID_MAX_CHARS - len(manual_research_dedup_id("", "0" * MANUAL_TOKEN_CHARS))
    return (
        "its topic id cannot be the work queue's message group id "
        f"(at most {limit} letters, digits and punctuation, no spaces)"
    )


def _shadow_count(topic: dict, field: str) -> int | None:
    """A shadow counter as a number (none yet is 0), or None when the row holds something else."""
    value = topic.get(field)
    if value is None:
        return 0
    return int(value) if _whole(value) else None


def shadow_problems(topic: dict, *, research_is_rate: bool) -> list[str]:
    """What shadow mode has not yet shown about this topic: a full day watched (one day its daily
    check answered), no mismatch, no day it could not check (a check that could not run never
    passes as a match), and for a cron research cadence at least one research turn recorded: a
    heartbeat that drifted off its computed minute before shadow mode began records none, and is
    otherwise never seen. A `rate(...)` research cadence is recorded but never judged (the module
    docstring), so such a topic is judged by its daily record alone."""
    counts = {
        field: _shadow_count(topic, field)
        for field in ("shadow_mismatches", "shadow_unchecked", SHADOW_CHECKED_DAYS)
    }
    unreadable = sorted(field for field, count in counts.items() if count is None)
    if unreadable:
        return [f"its shadow record is unreadable ({', '.join(unreadable)})"]
    reasons = []
    mismatches, unchecked = counts["shadow_mismatches"], counts["shadow_unchecked"]
    if mismatches:
        reasons.append(
            f"shadow mode counted {mismatches} mismatch{'es' if mismatches != 1 else ''} with its "
            "schedules (`topics schedule` shows the record)"
        )
    if unchecked:
        reasons.append(
            f"shadow mode could not check {unchecked} of its days, and a day it could not check "
            "never counts as a match"
        )
    if counts[SHADOW_CHECKED_DAYS] < 1:
        reasons.append(
            "shadow mode has not yet watched a full day of it (its first check runs at its second "
            "shadow daily turn)"
        )
    if not research_is_rate and not topic.get("shadow_last_research_dispatch_at"):
        reasons.append(
            "shadow mode has recorded none of its research turns (a heartbeat running off the "
            "dispatcher's minute records none)"
        )
    return reasons


def cutover_refusals(topic: dict, *, topics: list[dict], config: dict | None) -> list[str]:
    """Every reason the topic may not leave its schedules for the queue now, or [] when it may.
    `topic` is the row as it would be written; `topics` is every topic, this one included."""
    reasons = []
    if topic.get("paused") is True:
        reasons.append("it is paused, and shadow mode does not watch a paused topic: resume it first")
    problem = cadence_problem(topic, config=config, topic_count=len(topics))
    if problem:
        reasons.append(problem)
    topic_id = topic.get("topic_id") or ""
    reasons.extend(check for check in (slug_problem(topic_id, topics), queue_id_problem(topic_id)) if check)
    research = effective_cadences(topic, config=config, topic_count=len(topics))["research"]
    try:
        research_is_rate = cron_match.parse(research).is_rate
    except CadenceError:
        research_is_rate = False  # already refused above; judge it by the stricter rule
    reasons.extend(shadow_problems(topic, research_is_rate=research_is_rate))
    return reasons


def cutover_day(topic: dict, *, config: dict | None, topic_count: int, now: datetime) -> str | None:
    """What `last_daily_dispatched_on` must say when the topic starts on the queue at `now`: today's
    date in its zone when today's daily turn has already passed (the Scheduler had today, or would
    have, and the dispatcher must not start a second run), else None (the dispatcher starts today's
    run at its turn). Its claim count, `daily_claim_n`, is left as it is: tomorrow's run is named
    with the next number. Raises CadenceError or ValueError as cadence_problem reports them."""
    _research, daily = parse_cadences(topic, config=config, topic_count=topic_count)
    zone = topic_zone(topic)
    if cron_match.daily_turn_passed(daily, now, zone):
        return now.astimezone(zone).date().isoformat()
    return None
