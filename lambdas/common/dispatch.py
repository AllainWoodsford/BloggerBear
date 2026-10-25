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
really did: a research turn whose previous computed turn is more than five minutes from the minute
`last_research_at` moved, and a daily turn whose previous day saw no execution of this topic. A
topic whose `dispatch_mode` is `queue` has no schedules left, so it is dispatched for real whatever
the shadow setting says; a topic on its schedules is only ever shadowed. Zero mismatches over a
day is what the per-topic cutover (Q2) asks for.

**Settings** are two more fields of the `pipeline` config row (SETTINGS below, the model catalog's
pattern): `dispatch_shadow` and `daily_starts_per_tick` (1 to 10). `last_tick` on the same row is
the window's start for the next tick.

**Errors.** A failure of the whole tick (the Topics scan, the queue send, Step Functions
throttling) raises DispatchError, so the dispatcher's Errors alarm fires; a failure of one topic
(an unreadable zone, a missing field) is printed and the next topic is considered. A cadence the
matcher refuses is printed as `dispatcher: cadence unparsable topic_id=... expression=...` and
counted in `skipped_unparsable`, never guessed at.

Nothing here runs yet: the dispatcher Lambda (Q1) is what calls Dispatcher.tick and emits the
`dispatcher_tick` evidence line from the counts it returns. Topic-agnostic: nothing here knows
about any adapter or domain.
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
# queue. The admin API lets a topic be flipped in Q2.
SCHEDULE_MODE = "schedule"
QUEUE_MODE = "queue"
RESEARCH_JOB = "research"

# An execution name is the slug, the local date and the claim number: <= 40 + 11 + a number, inside
# Step Functions' 80 characters.
SLUG_MAX_CHARS = 40
_NOT_SLUG = re.compile(r"[^a-z0-9-]")

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

# A research mismatch in shadow mode: the computed turn and the minute last_research_at moved differ
# by more than this.
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
    """An operator's trigger (Q2): `<slug>-<local date>-manual-<HHMMSS>`."""
    return f"{slug(topic_id)}-{local_date}-manual-{now.strftime('%H%M%S')}"


def _iso(moment: datetime) -> str:
    """A UTC time to the second: the claim values are compared as strings in DynamoDB, so every
    value this module writes has the same width."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def research_message(topic_id: str, *, now: datetime) -> dict:
    """The body of a research job. The tick ignores `job` and re-evaluates is_due at
    `due_checked_at`, so a message that waited in the queue is harmless; `force` is only ever
    true from an operator."""
    return {
        "topic_id": topic_id,
        "job": RESEARCH_JOB,
        "force": False,
        "due_checked_at": _iso(now),
        "dispatched_at": _iso(now),
    }


def research_dedup_id(topic_id: str, now: datetime) -> str:
    """`<topic_id>:research:<yyyy-mm-ddTHH:MM>`: SQS's own guard against the same minute twice."""
    return f"{topic_id}:{RESEARCH_JOB}:{now.astimezone(UTC).strftime('%Y-%m-%dT%H:%M')}"


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


def _zone(topic: dict) -> tzinfo:
    """The zone the topic's daily cadence is read in: its daily_timezone, else UTC (the zone a topic
    from before the field existed keeps, as the admin API reads it)."""
    name = topic.get("daily_timezone") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"daily_timezone {name!r} is not a zone this runtime knows") from exc


def _local_date(value, zone: tzinfo) -> date | None:
    moment = _parse_time(value)
    return moment.astimezone(zone).date() if moment else None


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
        effective = stagger.effective_schedules(
            research_cadence=topic.get("research_cadence") or "",
            daily_cadence=topic.get("daily_cadence") or "",
            slot=stagger.topic_slot(topic),
            stagger_minutes=stagger.resolve_stagger_minutes(config),
            topic_count=topic_count,
        )
        research = cron_match.parse(effective["research"])
        daily = cron_match.parse(effective["daily"], allow_rate=False)
        zone = _zone(topic)
        interval_hours = resolve_interval_hours(topic, config)

        if cron_match.research_turn(research, last_tick, now) and is_due(
            now, topic.get("last_research_at"), interval_hours
        ):
            if live:
                if self._send_research(topic_id, now, interval_hours):
                    counts.dispatched += 1
            else:
                self._shadow_research(topic, now)

        if cron_match.daily_turn_passed(daily, now, zone):
            today = now.astimezone(zone).date()
            if live:
                if topic.get("last_daily_dispatched_on") != today.isoformat() and starts_left > 0:
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

    def _shadow_research(self, topic: dict, now: datetime) -> None:
        """Record the turn, and count a mismatch when the previous computed turn and the minute
        last_research_at moved (the Scheduler path's tick) are more than SHADOW_TOLERANCE apart."""
        previous = _parse_time(topic.get("shadow_last_research_dispatch_at"))
        mismatch = 0
        if previous is not None:
            moved = _parse_time(topic.get("last_research_at"))
            if moved is None or abs(moved - previous) > SHADOW_TOLERANCE:
                mismatch = 1
                print(
                    f"dispatcher: shadow research mismatch topic_id={topic['topic_id']} "
                    f"turn={_iso(previous)} last_research_at={_iso(moved) if moved else None}"
                )
        self._write_shadow(topic["topic_id"], "shadow_last_research_dispatch_at", now, mismatch)

    def _shadow_daily(self, topic: dict, now: datetime, today: date, zone: tzinfo) -> None:
        """Record today's turn, and count a mismatch when the previous recorded day saw no execution
        of this topic on the Scheduler path. A check that could not run counts nothing."""
        previous = _parse_time(topic.get("shadow_last_daily_dispatch_at"))
        mismatch = 0
        if previous is not None:
            previous_day = previous.astimezone(zone).date()
            ran = self._daily_ran(topic["topic_id"], previous_day, zone)
            if ran is False:
                mismatch = 1
                print(
                    f"dispatcher: shadow daily mismatch topic_id={topic['topic_id']} "
                    f"local_date={previous_day.isoformat()}"
                )
        self._write_shadow(topic["topic_id"], "shadow_last_daily_dispatch_at", now, mismatch)

    def _daily_ran(self, topic_id: str, day: date, zone: tzinfo) -> bool | None:
        """Whether an execution of the daily cycle for `topic_id` started on `day` (a local date):
        the executions of that day, newest first, each described for its input. None when Step
        Functions could not be asked."""
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

    def _write_shadow(self, topic_id: str, field: str, now: datetime, mismatch: int) -> None:
        self.topics_table.update_item(
            Key={"topic_id": topic_id},
            UpdateExpression="SET #f = :t ADD shadow_mismatches :n",
            ConditionExpression="attribute_exists(topic_id)",
            ExpressionAttributeNames={"#f": field},
            ExpressionAttributeValues={":t": _iso(now), ":n": mismatch},
        )
