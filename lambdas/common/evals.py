"""Continuous evals' scored events: what each reason is worth (SCORING), the one JSON line an event
is, writing it to the evals log group (`emit`), and reading it back (`parse`).

docs/enhancements/model-ranch-prompt-assets-evals.md, sections 4.3 and 4.9. Every time something
says a model or a prompt did well or badly (an article was completed, an operator rewrote one, a
reader voted, a person or the judge reviewed one), the code that knows writes one event to the
`<prefix>-<env>-evals` log group, and the hourly rollup (common/evals_rollup.py, PR13) adds the
events up into the evals table. Nothing emits yet: the emission points are PR14's.

**The event is a schema, not text.** Every field has a kind, and the kinds are evidence.py's (code,
id, model id, ISO time, number) or narrower ones built here (a prompt id, a prompt version, a
subject, an event id, each a fixed shape). `reason` must be one of REASONS, `performance` must
match the sign of `rating_delta`, and `rating_delta` must be one the reason may carry. An event
with any field that fails is refused whole, never written with the field left out: a prompt id
dropped from a Completion would quietly turn it into a model-only event. A field the schema does
not name is never written, and `parse` leaves it out. So free text (a comment, a reply, a reason in
words) has nowhere to go.

**One event per subject and prompt.** `event_id` (EVENT_IDS, built by `event_id`) is what the
rollup deduplicates by, so the same event written twice (a retried invocation) counts once, and
each prompt in an article's lineage gets an event of its own.

**`emit` never raises**, and writes nothing while EVALS_ENABLED is not "true" or EVALS_LOG_GROUP
is empty (no client is even built). An event that cannot be written is lost, with one line in the
function's log saying so: bookkeeping never fails the work. The Logs client makes one connection
attempt of a second, reads for a second, and retries once (section 4.3), so a slow Logs costs the
caller seconds, not its timeout. Each container writes to one stream of its own a day,
`<function>/<yyyy-mm-dd>/<container id>`, made the first time it is needed.

**author_of** is who wrote an item, for the judge (PR18), which never judges its own work.
**wilson** is the interval the picker and the reports rank by (section 4.9), copied from
scripts/vision_eval_scoring.py because scripts/ is not in the Lambda package.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common import evidence
from common.model_pricing import canonical_model_id

SCHEMA = "evals/1"
PERFORMANCES = ("good", "bad", "neutral")
# Who decided (section 3, deviation 10): role codes, never a person's name.
BY = ("human", "judge", "system")

# What a reason's `gates` code means (section 4.3's "gates?" column): `yes`, the windows gate on
# it; `no`, it scores but never gates (reader votes); `while_calibrated`, it gates only while the
# judge agrees with people often enough; `total_only`, it moves total_perf and no window; `counted`,
# it is counted and scores nothing.
GATES = ("yes", "no", "while_calibrated", "total_only", "counted")


@dataclass(frozen=True)
class Scoring:
    """How one reason scores. `deltas` are the rating_delta values it may carry, or None for an
    operator's adjustment, which may carry any whole number of `sign`'s sign. `needs` names the
    fields its events must have beyond the ones every event has: `model_id`, `prompt` (a prompt id
    with its version), `subject`, `topic_id`, or `target` (a model or a prompt version, exactly one).
    `event_id` is the id's prefix and then the parts it is built from."""

    deltas: frozenset[int] | None
    applies_to: str
    gates: str
    needs: frozenset[str]
    event_id: tuple[str, ...]
    sign: int = 0


_REVIEWED = frozenset({"model_id", "subject", "topic_id"})
_PLUS_MINUS_2 = frozenset({2, -2})

# Section 4.3's table, in its order.
SCORING: dict[str, Scoring] = {
    "Article Completion": Scoring(
        deltas=frozenset({2}),
        applies_to="model_and_prompts",
        gates="yes",
        needs=_REVIEWED | {"prompt"},
        event_id=("completion", "article_id", "prompt_id"),
    ),
    "Article Rewrite": Scoring(
        deltas=frozenset({-2}),
        applies_to="model",
        gates="yes",
        needs=_REVIEWED,
        event_id=("rewrite", "rewrite_id"),
    ),
    # +-1, or +-3 with a comment the screening kept. Reaches prompt cells only through the rollup's
    # join to the article's completion events by subject: the public API never loads lineage.
    "User Feedback": Scoring(
        deltas=frozenset({1, -1, 3, -3}),
        applies_to="model",
        gates="no",
        needs=_REVIEWED,
        event_id=("vote", "feedback_id"),
    ),
    # A person's approve or reject in the inbox, one event per prompt of the article.
    "Admin Review": Scoring(
        deltas=_PLUS_MINUS_2,
        applies_to="model_and_prompts",
        gates="yes",
        needs=_REVIEWED | {"prompt"},
        event_id=("admin", "queue_id", "decision", "prompt_id"),
    ),
    "Research Review": Scoring(
        deltas=_PLUS_MINUS_2,
        applies_to="model_and_prompts",
        gates="while_calibrated",
        needs=_REVIEWED,
        event_id=("judge", "subject", "run_id"),
    ),
    "Article Review": Scoring(
        deltas=_PLUS_MINUS_2,
        applies_to="model_and_prompts",
        gates="while_calibrated",
        needs=_REVIEWED,
        event_id=("judge", "subject", "run_id"),
    ),
    "Human Review": Scoring(
        deltas=_PLUS_MINUS_2,
        applies_to="model_and_prompts",
        gates="yes",
        needs=_REVIEWED,
        event_id=("human", "subject", "ts"),
    ),
    # Written only by `evals score --record` runs, for the combination scored.
    "Offline Review": Scoring(
        deltas=_PLUS_MINUS_2,
        applies_to="combination",
        gates="while_calibrated",
        needs=_REVIEWED | {"prompt"},
        event_id=("offline", "run_id", "item", "combo"),
    ),
    # Any whole number of the right sign: the cap of 100 (more with --allow-cull) is the admin
    # API's to enforce (PR13b), not the schema's.
    "Admin Forgive": Scoring(
        deltas=None,
        sign=1,
        applies_to="target",
        gates="total_only",
        needs=frozenset({"target"}),
        event_id=("adjust", "ts", "target"),
    ),
    "Admin Punish": Scoring(
        deltas=None,
        sign=-1,
        applies_to="target",
        gates="total_only",
        needs=frozenset({"target"}),
        event_id=("adjust", "ts", "target"),
    ),
    "Prompt Fallback": Scoring(
        deltas=frozenset({0}),
        applies_to="prompt_version",
        gates="counted",
        needs=frozenset({"prompt"}),
        event_id=("fallback", "request_id", "prompt_id"),
    ),
}
REASONS: tuple[str, ...] = tuple(SCORING)

# The fields of an event, in the order a line is written.
FIELDS = (
    "schema",
    "event_id",
    "timestamp",
    "model_id",
    "prompt_id",
    "prompt_version",
    "performance",
    "rating_delta",
    "reason",
    "subject",
    "topic_id",
    "source",
    "by",
)

MAX_EVENT_ID_CHARS = 512
# Each of these is matched whole (`fullmatch`): with `match` and `$`, a trailing newline would pass,
# and `v1\n` would then be a prompt version of its own to the rollup, which groups by these values.
# `<lambda_id>/<prompt_name>`, each a code (section 4.1).
_PROMPT_ID = re.compile(r"[a-z][a-z0-9_]{0,39}/[a-z][a-z0-9_]{0,39}")
# An asset's S3 version tag (`v2`), or a code fallback's `vanilla:v1`.
_PROMPT_VERSION = re.compile(r"(?:vanilla:)?v[1-9][0-9]{0,5}")
# One part of an event id: an id, a subject, a prompt id, an ISO time, a model id, or a
# combination's `<model>@<prompt_id>=<version>,...`. No spaces, so no words. The parts themselves
# hold colons (a time, a model id), so a whole id is checked by its prefix and its characters.
_ID_PART = re.compile(r"[A-Za-z0-9_.:/#+@=,-]{1,200}")
_ID_CHARS = re.compile(r"[A-Za-z0-9_.:/#+@=,-]+")
_ID_PREFIXES = frozenset(scoring.event_id[0] for scoring in SCORING.values())

# The Logs client (section 4.3): one second to connect, one to read, one retry (botocore's legacy
# `max_attempts` counts retries, so this is two attempts in all).
CLIENT_CONFIG = Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 1})
# This container's id, for its stream's name: a new one each cold start.
CONTAINER_ID = uuid.uuid4().hex
_client = None
# The stream this container has made (or found made), so it is made once a day, not once an event.
_stream = None


# --- the kinds --------------------------------------------------------------------------------------


def _whole(kind: str, value) -> str | None:
    """evidence.py's `kind`, matched whole. Its code, id and model patterns end in `$` and are used
    with `re.match`, which lets one trailing newline past; here an id is a key the rollup groups and
    deduplicates by, so `a1b2\\n` must not pass as an id of its own."""
    if isinstance(value, str) and value.endswith("\n"):
        return None
    return evidence.check(kind, value)


def _subject(value) -> str | None:
    """An article id, or a Finding's `<topic_id>#<captured_at>`."""
    if not isinstance(value, str):
        return None
    if "#" not in value:
        return _whole("id", value)
    topic_id, _, captured_at = value.partition("#")
    if _whole("id", topic_id) is None or evidence.check("iso", captured_at) is None:
        return None
    return value


def _pattern(pattern: re.Pattern):
    return lambda value: value if isinstance(value, str) and pattern.fullmatch(value) else None


def _event_id_value(value) -> str | None:
    """A known prefix, a colon, then an id's characters."""
    if not isinstance(value, str) or len(value) > MAX_EVENT_ID_CHARS:
        return None
    prefix, separator, rest = value.partition(":")
    return value if separator and prefix in _ID_PREFIXES and _ID_CHARS.fullmatch(rest) else None


# Each optional field's kind. schema, reason, performance and rating_delta are checked on their own.
_KINDS = {
    "event_id": _event_id_value,
    "timestamp": lambda value: evidence.check("iso", value),
    "model_id": lambda value: _whole("model", value),
    "prompt_id": _pattern(_PROMPT_ID),
    "prompt_version": _pattern(_PROMPT_VERSION),
    "subject": _subject,
    "topic_id": lambda value: _whole("id", value),
    "source": lambda value: _whole("code", value),
    "by": lambda value: value if value in BY else None,
}


def performance_of(rating_delta: int) -> str:
    """The performance a delta's sign means: good above zero, bad below, neutral at zero."""
    return "good" if rating_delta > 0 else "bad" if rating_delta < 0 else "neutral"


def event_id(reason: str, **parts) -> str | None:
    """The event id for `reason`'s event (section 4.3): its prefix, then its parts in EVENT_IDS'
    order, joined by `:`. e.g. `event_id("Article Completion", article_id="a1b2",
    prompt_id="daily_cycle/draft")` is `completion:a1b2:daily_cycle/draft`. None for an unknown
    reason, a missing part, or a part that is not an id's shape (a sentence, say); emit refuses an
    event without one. Never raises."""
    scoring = SCORING.get(reason) if isinstance(reason, str) else None
    if scoring is None:
        return None
    prefix, *names = scoring.event_id
    values = [parts.get(name) for name in names]
    if set(parts) != set(names) or not all(isinstance(v, str) and _ID_PART.fullmatch(v) for v in values):
        return None
    built = ":".join([prefix, *values])
    return built if len(built) <= MAX_EVENT_ID_CHARS else None


def _validated(given: dict) -> tuple[dict | None, str | None]:
    """(the event as it is written, None), or (None, why it is refused, as a code). Fields the
    schema does not name are left out."""
    if given.get("schema") != SCHEMA:
        return None, "bad_schema"
    reason = given.get("reason")
    scoring = SCORING.get(reason) if isinstance(reason, str) else None
    if scoring is None:
        return None, "unknown_reason"
    delta = given.get("rating_delta")
    if isinstance(delta, bool) or not isinstance(delta, int):
        return None, "bad_rating_delta"
    allowed = delta in scoring.deltas if scoring.deltas is not None else delta * scoring.sign > 0
    if not allowed:
        return None, "delta_not_allowed"
    if given.get("performance") != performance_of(delta):
        return None, "sign_mismatch"
    record = {"schema": SCHEMA}
    for name, kind in _KINDS.items():
        value = given.get(name)
        if value is None:
            continue
        cleaned = kind(value)
        if cleaned is None:
            return None, f"bad_{name}"
        record[name] = cleaned
    record.update(performance=given["performance"], rating_delta=delta, reason=reason)
    if "event_id" not in record:
        return None, "missing_event_id"
    if record["event_id"].partition(":")[0] != scoring.event_id[0]:
        return None, "event_id_reason"
    if "timestamp" not in record:
        return None, "missing_timestamp"
    if "source" not in record:
        return None, "missing_source"
    if ("prompt_id" in record) != ("prompt_version" in record):
        return None, "prompt_half"
    has = {name for name in ("model_id", "subject", "topic_id") if name in record}
    if "prompt_id" in record:
        has.add("prompt")
    if "target" in scoring.needs and len(has & {"model_id", "prompt"}) != 1:
        return None, "bad_target"
    missing = sorted(scoring.needs - has - {"target"})
    if missing:
        return None, f"missing_{missing[0]}"
    return {name: record[name] for name in FIELDS if name in record}, None


def event(
    reason: str,
    *,
    event_id: str | None,
    source: str,
    rating_delta: int | None = None,
    model_id: str | None = None,
    prompt_id: str | None = None,
    prompt_version: str | None = None,
    subject: str | None = None,
    topic_id: str | None = None,
    by: str | None = None,
    timestamp: str | None = None,
) -> tuple[dict | None, str | None]:
    """The event `emit` would write, checked: (record, None), or (None, why not). `rating_delta`
    defaults to the reason's one delta when it has only one (Completion's +2, Rewrite's -2,
    Fallback's 0); `performance` is always its sign's. `timestamp` defaults to now."""
    scoring = SCORING.get(reason) if isinstance(reason, str) else None
    if scoring is None:
        return None, "unknown_reason"
    if rating_delta is None and scoring.deltas and len(scoring.deltas) == 1:
        rating_delta = next(iter(scoring.deltas))
    if isinstance(rating_delta, bool) or not isinstance(rating_delta, int):
        return None, "missing_rating_delta" if rating_delta is None else "bad_rating_delta"
    return _validated(
        {
            "schema": SCHEMA,
            "event_id": event_id,
            "timestamp": timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_id": model_id,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "performance": performance_of(rating_delta),
            "rating_delta": rating_delta,
            "reason": reason,
            "subject": subject,
            "topic_id": topic_id,
            "source": source,
            "by": by,
        }
    )


def line(record: dict) -> str:
    """One event's message: compact JSON, ASCII, in FIELDS order (section 4.3's example)."""
    return json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


# --- writing ----------------------------------------------------------------------------------------


def enabled() -> bool:
    """Whether events are written: the switch is on and there is a log group to write to."""
    return os.environ.get("EVALS_ENABLED") == "true" and bool(os.environ.get("EVALS_LOG_GROUP"))


def stream_name(now: datetime) -> str:
    """This container's stream for `now`'s UTC day: `<function>/<yyyy-mm-dd>/<container id>`."""
    function = os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or "local"
    return f"{function}/{now:%Y-%m-%d}/{CONTAINER_ID}"


def _logs():
    global _client
    if _client is None:
        _client = boto3.client("logs", config=CLIENT_CONFIG)
    return _client


def _put(messages: list[str]) -> None:
    """PutLogEvents of `messages` to this container's stream, making the stream first if this
    container has not today. Raises what the client raises."""
    global _stream
    group = os.environ["EVALS_LOG_GROUP"]
    now = datetime.now(UTC)
    stream = stream_name(now)
    if _stream != stream:
        try:
            _logs().create_log_stream(logGroupName=group, logStreamName=stream)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceAlreadyExistsException":
                raise
        _stream = stream
    timestamp = int(now.timestamp() * 1000)
    events = [{"timestamp": timestamp, "message": message} for message in messages]
    _logs().put_log_events(logGroupName=group, logStreamName=stream, logEvents=events)


def emit(reason: str, **fields) -> bool:
    """Write one event of `reason` (the keywords are `event`'s) to the evals log group. True when it
    was written; False when evals are off, the event is refused (one line naming why, as a code),
    or Logs could not take it (one line with the error). Never raises."""
    global _stream
    try:
        if not enabled():
            return False
        record, refused = event(reason, **fields)
        if record is None:
            print(f"evals: event refused ({refused})")
            return False
        _put([line(record)])
        return True
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the work
        _stream = None  # make the stream again next time, in case it is what went
        print(f"evals: could not write an event: {exc!r}")
        return False


# --- reading ----------------------------------------------------------------------------------------


def parse(message) -> dict | None:
    """A reader's half: an event's fields, checked exactly as the writer checks them, from its log
    message (a JSON string, or the object already decoded). None when it is not a valid event of
    this schema: not JSON, another schema, an unknown reason, a sign that does not match its delta,
    or any field that fails its kind. Fields the schema does not name are left out. Never raises."""
    try:
        record = json.loads(message) if isinstance(message, str) else message
        if not isinstance(record, dict):
            return None
        return _validated(record)[0]
    except Exception:  # noqa: BLE001 - a reader never falls over a log line
        return None


# --- who wrote it -----------------------------------------------------------------------------------

# The lineage stages that write an article's text (daily_cycle_handler, fresh_review's revision,
# rewrite.py), as opposed to the ones that judge it (the adversarial and compliance reviews).
WRITING_STAGES = frozenset({"ideation", "draft", "title", "revision", "rewrite"})


def author_of(item) -> frozenset[str]:
    """Every model that wrote any of an item's text, canonical: an Article's writing stages
    (`lineage.calls`, WRITING_STAGES; a rewrite's model too, since rewrite.py adds its call there),
    or a Finding's summary (`research_call`). The judge is never one of them (section 3, deviation
    9). Empty when the item records none (a Finding from before research tracking): the caller
    decides what an unknown author means. Never raises."""
    try:
        if not isinstance(item, dict):
            return frozenset()
        models = []
        research_call = item.get("research_call")
        if isinstance(research_call, dict):
            models.append(research_call.get("model_id"))
        lineage = item.get("lineage")
        calls = lineage.get("calls") if isinstance(lineage, dict) else None
        for call in calls if isinstance(calls, list) else ():
            if isinstance(call, dict) and call.get("stage") in WRITING_STAGES:
                models.append(call.get("model_id"))
        return frozenset(canonical_model_id(model) for model in models if isinstance(model, str) and model)
    except Exception:  # noqa: BLE001 - an unreadable item has no known author
        return frozenset()


# --- the interval -----------------------------------------------------------------------------------

# The two-sided 95 % normal quantile.
Z95 = 1.959963984540054
ROUND = 4


def wilson(k: int, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    """The Wilson score interval for `k` successes in `n` trials, rounded to 4 places; (None, None)
    when n is 0 (no trial, no interval)."""
    if n <= 0:
        return None, None
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return round(max(0.0, centre - half), ROUND), round(min(1.0, centre + half), ROUND)
