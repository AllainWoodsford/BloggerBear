"""What `admin_cli evals status` and `evals digest` show: the admin API's GET /evals/status and
GET /evals/digest (docs/enhancements/model-ranch-prompt-assets-evals.md, PR13).

**status** answers "are evals working, and what are they holding back?" in parts, each read on its
own: the switches, the prompt release in place and the prompt fallbacks of the last 24 hours, the
rollup (its watermark and last run), `late_dropped`, the judge (its agreement with people, pairs and
whether it is calibrated), the gated models (as the rotation guard decides it, PR15, with the
protected and the held-open ones apart), culling (the proposal in place, whether it has expired,
and the last `evals cull --apply`: when, and which models it culled), each queue's depth and the
age of its oldest job (eval-work's, and the queue platform's work.fifo), and the topics by dispatch
mode. A part that cannot be read says "could not check" (COULD_NOT_CHECK) and the others still
answer, so one missing grant or one slow service never hides the rest.

**The prompt parts** are where prompts as assets meet evals. The release is the publisher's
`releases/current.json` in the prompts bucket (common/prompts.current_release), read by its two
fields' names (prompts.CURRENT_RELEASE_FIELD, PINNED_RELEASE_FIELD), with whether this deployment
reads assets at all. The fallbacks are the observability module's PromptFallback metric, which its
metric filter counts from every pipeline log group's prompt_fallback evidence lines; read with
`cloudwatch:GetMetricStatistics`, which lambda_exec already holds for the queues' age (the evals
module's EvalWorkAge statement: a metric read takes no resource), so the part needs no grant of its
own. The release is "off" while the deployment has no prompts bucket, and the fallbacks while it does
not read assets (its Lambdas then use the code's copy of every prompt and have nothing to fall back
from); neither reads anything then.

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

from common import (
    dynamo,
    evals,
    evals_rollup,
    evals_settings,
    evidence,
    model_catalog,
    model_fleet,
    model_ranch,
    prompts,
)
from common.model_pricing import canonical_model_id

COULD_NOT_CHECK = "could not check"
# The queues whose depth and age status shows, by the name it shows each under, and the variable
# that holds each one's URL. "Queue depth and age" could mean either, so both are shown: eval-work
# (the judge's and offline scoring's jobs; empty while evals are off) and work.fifo (the topics'
# research jobs; empty until the queue platform is deployed).
QUEUES = {"eval-work": "EVAL_WORK_QUEUE_URL", "work.fifo": "WORK_QUEUE_URL"}
QUEUE_OFF = {
    "eval-work": "evals are off: there is no eval-work queue (EVAL_WORK_QUEUE_URL is empty)",
    "work.fifo": "the queue platform is not deployed: there is no work queue (WORK_QUEUE_URL is empty)",
}
# A queue's oldest job: the most recent of SQS's five-minute datapoints within this.
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
# The prompt fallbacks' metric, as infra/modules/observability's prompt_fallback filter names it: one
# metric with no dimensions, fed by every pipeline log group. The namespace is the project's own name,
# which stays whatever the deployment's prefix is (common/naming.py), and the environment's.
FALLBACK_NAMESPACE = "BloggerBear/{environment}"
FALLBACK_METRIC = "PromptFallback"
FALLBACK_LOOKBACK = timedelta(hours=24)
FALLBACK_PERIOD_SECONDS = 3600
PROMPTS_OFF = {
    "release": "prompts as assets are not deployed: there is no prompts bucket (PROMPTS_BUCKET is empty)",
    "fallbacks": (
        "prompts as assets are off (PROMPT_AS_ASSET_ENABLED): every prompt is the copy in the code, "
        "so nothing falls back"
    ),
}
FALLBACK_DETAIL = (
    "each fallback is a prompt_fallback line in its Lambda's log, naming the prompt, the version and "
    "why; `prompts list` shows what is live"
)

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


def _queue_url(queue: str) -> str:
    return os.environ.get(QUEUES[queue], "")


def _queue_off(queue: str) -> dict:
    return {"status": "off", "detail": QUEUE_OFF[queue]}


def _queue_depth(queue: str) -> dict:
    url = _queue_url(queue)
    if not url:
        return _queue_off(queue)
    attributes = _sqs_client().get_queue_attributes(QueueUrl=url, AttributeNames=list(QUEUE_ATTRIBUTES))
    found = attributes.get("Attributes") or {}
    waiting, in_flight, delayed = (int(found.get(name, 0)) for name in QUEUE_ATTRIBUTES)
    return {"queue": url.rsplit("/", 1)[-1], "waiting": waiting, "in_flight": in_flight, "delayed": delayed}


def _queue_age(queue: str, now: datetime) -> dict:
    url = _queue_url(queue)
    if not url:
        return _queue_off(queue)
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


def _prompt_release() -> dict:
    """The publisher's release in place and, while a rollback holds to one, the pinned release; None
    for each before the first publish."""
    bucket = os.environ.get("PROMPTS_BUCKET", "")
    if not bucket:
        return {"status": "off", "detail": PROMPTS_OFF["release"]}
    current = prompts.current_release()
    answer = {"bucket": bucket, "assets_enabled": prompts.assets_enabled()}
    if current is None:
        return {**answer, "current": None, "pinned": None, "detail": "nothing has been published yet"}
    return {
        **answer,
        "current": current.get(prompts.CURRENT_RELEASE_FIELD),
        "pinned": current.get(prompts.PINNED_RELEASE_FIELD),
    }


def _prompt_fallbacks(now: datetime) -> dict:
    """How many prompts fell back to the code's copy in the last 24 hours, in how many of those hours,
    and the latest such hour."""
    if not prompts.assets_enabled():
        return {"status": "off", "detail": PROMPTS_OFF["fallbacks"]}
    points = _cloudwatch_client().get_metric_statistics(
        Namespace=FALLBACK_NAMESPACE.format(environment=os.environ["ENVIRONMENT_NAME"]),
        MetricName=FALLBACK_METRIC,
        StartTime=now - FALLBACK_LOOKBACK,
        EndTime=now,
        Period=FALLBACK_PERIOD_SECONDS,
        Statistics=["Sum"],
    ).get("Datapoints") or []
    hours = sorted((p for p in points if p.get("Sum", 0) > 0), key=lambda p: p["Timestamp"])
    return {
        "last_24_hours": int(sum(p["Sum"] for p in hours)),
        "hours_with_fallbacks": len(hours),
        "latest_hour": hours[-1]["Timestamp"].isoformat() if hours else None,
        "detail": FALLBACK_DETAIL,
    }


def _topics_by_dispatch_mode() -> dict:
    counts = {DEFAULT_DISPATCH_MODE: 0, "queue": 0}
    for topic in dynamo.list_topics():
        mode = topic.get("dispatch_mode") or DEFAULT_DISPATCH_MODE
        mode = mode if evidence.check("code", mode) else "other"
        counts[mode] = counts.get(mode, 0) + 1
    return counts


def _gated_models(settings: dict, now: datetime) -> dict:
    """The models below a gate (gated_windows), each in one list, decided as the rotation guard
    decides it (common/model_ranch.py: Guard.gated, gate and held_open), so this part's `gated`
    and GET /models' `ranch.gated` agree whenever evals are on (GET /models has no `ranch` while
    they are off; this part answers either way):

    * `gated`: held back from every candidate list until its windows recover;
    * `protected`: the default, the fallback or the judge (its `role`), which are never gated:
      served whatever their scores, each time with a `model_gated_protected` line;
    * `held_open`: an operator's `ungated_until` holds its gate open, a time still ahead, or a value
      that is not a time (`ungated_until_unreadable`), which the guard does not gate either.

    It builds its own Guard from the settings status read and one read of the model-config default
    row, rather than model_ranch.load_guard, which reads nothing while evals are off and protects
    nothing when a read fails: status answers whatever the switch says, and a read that fails says
    "could not check"."""
    config = dynamo.get_model_config() or {}
    guard = model_ranch.Guard(
        settings=settings, protected=model_ranch.never_gated(settings, config), config=config, now=now
    )
    gated, held_open, protected = [], [], []
    for row in sorted(dynamo.list_models(), key=lambda model: model["model_id"]):
        windows = model_ranch.gated_windows(row, settings)
        if not windows:
            continue
        entry = {
            "model_id": row["model_id"],
            "windows": windows,
            **{field: _number(row.get(field)) for field in ("daily_perf", "weekly_perf", "total_perf")},
            "perf_events": _number(row.get("perf_events")),
        }
        role = guard.role(row["model_id"])
        if role is not None:
            protected.append({**entry, "role": role})
        elif guard.gated(row):
            gated.append(entry)
        else:
            held = {**entry, "ungated_until": _number(row.get("ungated_until"))}
            if model_ranch.held_open(row, now) == model_ranch.HELD_OPEN_UNREADABLE:
                held["ungated_until_unreadable"] = True
            held_open.append(held)
    gates = {name: settings[name] for name in ("daily_gate", "weekly_gate", "total_gate", "min_events")}
    return {"gated": gated, "held_open": held_open, "protected": protected, "gates": gates}


def _cull(now: datetime) -> dict:
    """Culling (PR16), from model-catalog-state: the proposal in place as stored (`cull_proposal`,
    which the weekly cycle writes; None until there is one) and whether it has expired (an apply
    refuses it then), and the last `evals cull --apply` as it recorded itself (`last_cull_apply`:
    when, the ids it culled, those it kept and why; None until one has run)."""
    state = model_catalog.get_state()
    proposal = _number(state.get(model_fleet.CULL_PROPOSAL_FIELD))
    part = {"proposal": proposal, "last_apply": _number(state.get(model_fleet.LAST_CULL_FIELD))}
    if proposal is None:
        part["detail"] = "no cull proposal yet"
    else:
        expires = evals_settings._parse(proposal.get("expires_at")) if isinstance(proposal, dict) else None
        part["expired"] = expires is None or now >= expires
    return part


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
        "prompt_release": _part("prompt_release", _prompt_release),
        "prompt_fallbacks": _part("prompt_fallbacks", lambda: _prompt_fallbacks(now)),
        "rollup": _part("rollup", lambda: _rollup(index())),
        "late_dropped": _part("late_dropped", lambda: {"late_dropped": _late_dropped(index())}),
        "judge": _part("judge", lambda: _judge(settings(), index())),
        "gated_models": _part("gated_models", lambda: _gated_models(settings(), now)),
        "cull": _part("cull", lambda: _cull(now)),
        # One part per queue, so one that cannot be read never hides the other.
        "queue_depth": {
            queue: _part(f"queue_depth/{queue}", lambda queue=queue: _queue_depth(queue)) for queue in QUEUES
        },
        "queue_age": {
            queue: _part(f"queue_age/{queue}", lambda queue=queue: _queue_age(queue, now)) for queue in QUEUES
        },
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
