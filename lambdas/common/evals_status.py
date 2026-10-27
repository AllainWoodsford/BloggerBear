"""What `admin_cli evals status` and `evals digest` show: the admin API's GET /evals/status and
GET /evals/digest (docs/enhancements/model-ranch-prompt-assets-evals.md, PR13).

**status** answers "are evals working, and what are they holding back?" in parts, each read on its
own: the switches, the rollup (its watermark and last run), `late_dropped`, the judge (its agreement
with people, pairs and whether it is calibrated), the gated models, the eval-work queue's depth and
the age of its oldest job, and the topics by dispatch mode. A part that cannot be read says "could
not check" (COULD_NOT_CHECK) and the others still answer, so one missing grant or one slow service
never hides the rest. The prompt release in place and the prompt fallbacks of the last 24 hours join
it where prompts as assets meet evals (PR14); the last cull or proposal with culling (PR16).

**digest** adds the day rows up from a day (`since`, a week ago by default) to today, per model and
per prompt version: perf, events, good and bad with the Wilson interval of good/(good + bad)
(section 4.9), and each model's gated windows now. It reads only the entities the index lists for
those days (common/evals_rollup.day_entities), one Query each.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from common import dynamo, evals, evals_rollup, evals_settings, evidence
from common.model_pricing import canonical_model_id

COULD_NOT_CHECK = "could not check"
# eval-work's oldest job: the most recent of SQS's five-minute datapoints within this.
AGE_LOOKBACK = timedelta(minutes=15)
QUEUE_ATTRIBUTES = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)
# A digest's days, each entity one Query over them: bounded so the route answers well inside API
# Gateway's 29 seconds.
DIGEST_DAYS = 7
DIGEST_MAX_DAYS = 120
# Topics with no dispatch_mode run on their own schedules (the queue platform's cutover sets it).
DEFAULT_DISPATCH_MODE = "schedule"

_sqs = None
_cloudwatch = None


def _sqs_client():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


def _cloudwatch_client():
    global _cloudwatch
    if _cloudwatch is None:
        _cloudwatch = boto3.client("cloudwatch")
    return _cloudwatch


class _Once:
    """A read shared by the parts that need it, made at most once while it succeeds; one that fails
    fails each part that asks for it, and only those."""

    def __init__(self, load):
        self._load = load
        self._loaded = False
        self._value = None

    def __call__(self):
        if not self._loaded:
            self._value = self._load()
            self._loaded = True
        return self._value


def _part(name: str, build) -> dict:
    try:
        return build()
    except Exception as exc:  # noqa: BLE001 - one part that cannot be read never hides the others
        print(f"evals_status: could not check {name}: {exc!r}")
        return {"status": COULD_NOT_CHECK, "error": type(exc).__name__}


def _number(value):
    """DynamoDB's Decimals as plain numbers (whole ones as int), all the way down."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _number(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_number(item) for item in value]
    return value


def _queue_url() -> str:
    return os.environ.get("EVAL_WORK_QUEUE_URL", "")


QUEUE_OFF = {
    "status": "off",
    "detail": "evals are off: there is no eval-work queue (EVAL_WORK_QUEUE_URL is empty)",
}


def _queue_depth() -> dict:
    url = _queue_url()
    if not url:
        return dict(QUEUE_OFF)
    attributes = _sqs_client().get_queue_attributes(QueueUrl=url, AttributeNames=list(QUEUE_ATTRIBUTES))
    found = attributes.get("Attributes") or {}
    waiting, in_flight, delayed = (int(found.get(name, 0)) for name in QUEUE_ATTRIBUTES)
    return {"queue": url.rsplit("/", 1)[-1], "waiting": waiting, "in_flight": in_flight, "delayed": delayed}


def _queue_age(now: datetime) -> dict:
    url = _queue_url()
    if not url:
        return dict(QUEUE_OFF)
    name = url.rsplit("/", 1)[-1]
    points = _cloudwatch_client().get_metric_statistics(
        Namespace="AWS/SQS",
        MetricName="ApproximateAgeOfOldestMessage",
        Dimensions=[{"Name": "QueueName", "Value": name}],
        StartTime=now - AGE_LOOKBACK,
        EndTime=now,
        Period=300,
        Statistics=["Maximum"],
    ).get("Datapoints") or []
    if not points:
        idle = "no datapoint in 15 minutes: an idle queue sends none"
        return {"queue": name, "oldest_seconds": None, "detail": idle}
    latest = max(points, key=lambda point: point["Timestamp"])
    return {"queue": name, "oldest_seconds": int(latest["Maximum"]), "at": latest["Timestamp"].isoformat()}


def _topics_by_dispatch_mode() -> dict:
    counts = {DEFAULT_DISPATCH_MODE: 0, "queue": 0}
    for topic in dynamo.list_topics():
        mode = topic.get("dispatch_mode") or DEFAULT_DISPATCH_MODE
        mode = mode if evidence.check("code", mode) else "other"
        counts[mode] = counts.get(mode, 0) + 1
    return counts


def _gated_models(settings: dict, now: datetime) -> dict:
    gated, held_open = [], []
    for row in sorted(dynamo.list_models(), key=lambda model: model["model_id"]):
        windows = evals_settings.gated_windows(row, settings)
        if not windows:
            continue
        entry = {
            "model_id": row["model_id"],
            "windows": windows,
            **{field: _number(row.get(field)) for field in ("daily_perf", "weekly_perf", "total_perf")},
            "perf_events": _number(row.get("perf_events")),
        }
        if evals_settings.ungated(row, now):
            held_open.append({**entry, "ungated_until": row["ungated_until"]})
        else:
            gated.append(entry)
    gates = {name: settings[name] for name in ("daily_gate", "weekly_gate", "total_gate", "min_events")}
    return {"gated": gated, "held_open": held_open, "gates": gates}


def _judge(settings: dict, index: dict) -> dict:
    agreement = _number(index.get("judge_agreement"))
    pairs = _number(index.get("judge_pairs")) or 0
    return {
        "judge_enabled": settings["judge_enabled"],
        "judge_model": evals_settings.judge_model(settings),
        "agreement": agreement,
        "pairs": pairs,
        "calibrated": evals_rollup.calibrated(agreement, pairs, settings),
        "min_agreement": settings["judge_min_agreement"],
        "min_pairs": settings["judge_min_pairs"],
    }


def _late_dropped(index: dict) -> int:
    return _number(index.get("late_dropped")) or 0


def _rollup(index: dict) -> dict:
    return {
        "last_rolled_day": index.get("last_rolled_day"),
        "rolled_up_at": index.get("rolled_up_at"),
        "last_run": _number(index.get("last_run")),
    }


def status(now: datetime | None = None) -> dict:
    """Every part of `evals status`, each its own answer or COULD_NOT_CHECK."""
    now = now or datetime.now(UTC)
    settings = _Once(evals_settings.get_settings)
    index = _Once(evals_rollup.get_index)
    return {
        "switches": _part(
            "switches",
            lambda: {
                "evals_enabled": os.environ.get("EVALS_ENABLED") == "true",
                **{name: settings()[name] for name in ("judge_enabled", "cull_enabled", "trace_calls")},
            },
        ),
        "rollup": _part("rollup", lambda: _rollup(index())),
        "late_dropped": _part("late_dropped", lambda: {"late_dropped": _late_dropped(index())}),
        "judge": _part("judge", lambda: _judge(settings(), index())),
        "gated_models": _part("gated_models", lambda: _gated_models(settings(), now)),
        "queue_depth": _part("queue_depth", _queue_depth),
        "queue_age": _part("queue_age", lambda: _queue_age(now)),
        "topics_by_dispatch_mode": _part("topics_by_dispatch_mode", _topics_by_dispatch_mode),
    }


# --- the digest -----------------------------------------------------------------------------------


def _since_day(since, today: date, zone) -> date | str:
    if since is None or since == "":
        return today - timedelta(days=DIGEST_DAYS - 1)
    try:
        day = date.fromisoformat(since)
    except (TypeError, ValueError):
        moment = evals_rollup.parse_time(since)
        if moment is None:
            return "'since' must be a date (YYYY-MM-DD) or an ISO time"
        day = evals_rollup.local_day(moment, zone)
    if day > today:
        return f"'since' must not be after today ({today.isoformat()})"
    if day < today - timedelta(days=DIGEST_MAX_DAYS - 1):
        return f"'since' must be at most {DIGEST_MAX_DAYS} days back"
    return day


def _sum(rows: list[dict]) -> dict:
    perf = events = good = bad = 0
    by_reason: dict[str, dict[str, int]] = {}
    for row in rows:
        perf += evals_rollup.count_of(row.get("perf"))
        events += evals_rollup.count_of(row.get("events"))
        good += evals_rollup.count_of(row.get("good"))
        bad += evals_rollup.count_of(row.get("bad"))
        for reason, counts in (row.get("by_reason") or {}).items():
            total = by_reason.setdefault(reason, {"perf": 0, "events": 0, "good": 0, "bad": 0})
            for name in total:
                total[name] += evals_rollup.count_of((counts or {}).get(name))
    low, high = evals.wilson(good, good + bad)
    return {
        "perf": perf,
        "events": events,
        "good": good,
        "bad": bad,
        "n": good + bad,
        "interval": [low, high],
        "by_reason": dict(sorted(by_reason.items())),
    }


def digest(since, now: datetime | None = None) -> dict | str:
    """The day rows added up from `since` (a date or an ISO time; a week ago, today included, when
    not given) to today, per model and per prompt version. A string, the reason, when `since` is not
    a date in range."""
    now = now or datetime.now(UTC)
    settings = evals_settings.get_settings()
    zone = evals_settings.zone(settings)
    today = evals_rollup.local_day(now, zone)
    first = _since_day(since, today, zone)
    if isinstance(first, str):
        return first
    table = evals_rollup.evals_table()
    entities = set().union(*evals_rollup.day_entities(first, today, table).values())
    registry = {canonical_model_id(row["model_id"]): row for row in dynamo.list_models()}
    span = Key("sk").between(f"{evals_rollup.DAY}{first}", f"{evals_rollup.DAY}{today}")
    models, prompts = [], []
    for entity in sorted(entities):
        kind, _, rest = entity.partition("#")
        if kind not in ("model", "prompt"):
            continue
        totals = _sum(evals_rollup.query_all(table, Key("entity").eq(entity) & span))
        if kind == "model":
            row = registry.get(rest)
            gated = evals_settings.gated_windows(row, settings) if row else []
            models.append({"model_id": rest, "in_registry": row is not None, "gated": gated, **totals})
        else:
            prompt_id, _, version = rest.rpartition("#")
            prompts.append({"prompt_id": prompt_id, "version": version, **totals})
    index = evals_rollup.get_index(table)
    return {
        "since": first.isoformat(),
        "until": today.isoformat(),
        "timezone": settings["timezone"],
        "models": sorted(models, key=lambda model: (model["perf"], model["model_id"])),
        "prompts": prompts,
        "judge": _judge(settings, index),
        "last_rolled_day": index.get("last_rolled_day"),
        "late_dropped": _late_dropped(index),
    }
