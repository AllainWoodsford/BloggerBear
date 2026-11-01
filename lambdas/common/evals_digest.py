"""The week's evals digest by email (docs/enhancements/model-ranch-prompt-assets-evals.md, PR20): on
Fridays, while the `digest_email` setting is on (common/evals_settings.py, `admin_cli evals set
--digest-email on`; off by default), eval-jobs publishes the digest to the alerts topic, the one the
alarms email through.

**What is sent** is GET /evals/digest's own answer (common/evals_status.digest: the day totals of the
last seven days, per model and per prompt version, the judge's agreement and the rollup's watermark),
written out as plain lines by `render`. Every value in it is an id, a code or a number: model ids,
prompt ids and versions as the rollup keyed them, each matched to its shape again here, and counts.
No topic's text, no reader's comment and no model's reply has anywhere to go. The words around them
are this module's.

**Once per Friday.** The check runs after each scheduled hourly rollup (eval_jobs_handler.py), so
no schedule of its own is needed. From DIGEST_HOUR on a Friday in the settings' `timezone`, it
claims the day on the evals table's index row (`last_digest_on`, a conditional write that only one
run a day can make) and then publishes. A Lambda retry, the next hour's run, or two runs at once find
the day claimed and send nothing. A publish that fails gives the claim back, so the next hour tries
again.

**Never fails the rollup**: the caller catches anything this raises, and the rollup's work is
already written by then.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

import boto3
from botocore.config import Config

from common import evals_rollup, evals_settings, evals_status
from common.naming import environment_prefix

TOPIC_ENV = "ALERTS_TOPIC_ARN"
STAMP_FIELD = "last_digest_on"
FRIDAY = 4  # date.weekday()
# Not at midnight: the first hourly run from this hour on, in the settings' timezone.
DIGEST_HOUR = 8
MAX_LINES = 40
# SNS's limit on an email subject, which must also be one line.
SUBJECT_MAX_CHARS = 100

# The shapes a value must have to be written into the email (common/evals.py's, and evidence.py's
# model id).
_MODEL = re.compile(r"[A-Za-z0-9._:/-]{1,160}")
_PROMPT = re.compile(r"[a-z][a-z0-9_]{0,39}/[a-z][a-z0-9_]{0,39}")
_VERSION = re.compile(r"(?:vanilla:)?v[1-9][0-9]{0,5}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_ZONE = re.compile(r"[A-Za-z0-9_+/-]{1,64}")
_WINDOW = re.compile(r"[a-z]{1,10}")
# Why a model below a gate is served all the same (evals_status.digest's `gate` and `role`), in this
# module's words; a state or role outside these is written as unread, never as gated.
_SERVED_ROLES = ("default", "fallback", "judge")
_HELD_OPEN_WORDS = "held open by an override"
_UNREAD_WORDS = "its gate could not be read"

# eval-jobs has 900 seconds; a publish that hangs is given up on well before, and tried next hour.
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})
_sns = None


def _client():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", config=CLIENT_CONFIG)
    return _sns


def _shaped(pattern: re.Pattern, value) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _number(value) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "-"
    return str(round(value, 2)) if isinstance(value, float) else str(value)


def _interval(totals: dict) -> str:
    low, high = (totals.get("interval") or [None, None])[:2]
    return "-" if low is None or high is None else f"{_number(low)}-{_number(high)}"


def _totals(totals: dict) -> str:
    return (
        f"perf {_number(totals.get('perf'))}, events {_number(totals.get('events'))}, "
        f"good {_number(totals.get('good'))}, bad {_number(totals.get('bad'))}, "
        f"good share interval {_interval(totals)}"
    )


def _bounded(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LINES:
        return lines
    return [*lines[:MAX_LINES], f"  and {len(lines) - MAX_LINES} more (`admin_cli evals digest` shows all)"]


def _gate(entry: dict) -> str:
    """What a model's line says of its gate: the windows that gate it while the rotation guard holds
    it back (`gated`), else the windows below their gate and why it is served all the same."""
    gated = [window for window in entry.get("gated") or [] if _shaped(_WINDOW, window)]
    if gated:
        return ", gated: " + ", ".join(gated)
    below = [window for window in entry.get("below_gate") or [] if _shaped(_WINDOW, window)]
    if not below:
        return ""
    state, role = entry.get("gate"), entry.get("role")
    if state == evals_status.PROTECTED and role in _SERVED_ROLES:
        why = f"kept as the {role}"
    elif state == evals_status.HELD_OPEN:
        why = _HELD_OPEN_WORDS
    else:
        why = _UNREAD_WORDS
    return f", below gate: {', '.join(below)} ({why})"


def render(digest: dict, environment: str) -> tuple[str, str]:
    """The email's subject and body for one digest (evals_status.digest's answer)."""
    since = _shaped(_DAY, digest.get("since")) or "-"
    until = _shaped(_DAY, digest.get("until")) or "-"
    zone = _shaped(_ZONE, digest.get("timezone")) or "-"
    where = environment_prefix(environment).rstrip("-")
    subject = f"{where}: evals digest, {since} to {until}"[:SUBJECT_MAX_CHARS]

    models = []
    for entry in digest.get("models") or []:
        model_id = _shaped(_MODEL, entry.get("model_id"))
        if model_id is None:
            continue
        flags = _gate(entry)
        flags += "" if entry.get("in_registry", True) else ", not in the registry"
        models.append(f"  {model_id}: {_totals(entry)}{flags}")
    prompts = []
    for entry in digest.get("prompts") or []:
        prompt_id, version = _shaped(_PROMPT, entry.get("prompt_id")), _shaped(_VERSION, entry.get("version"))
        if prompt_id is not None and version is not None:
            prompts.append(f"  {prompt_id} {version}: {_totals(entry)}")
    judge = digest.get("judge") or {}
    switch = "on" if judge.get("judge_enabled") else "off"
    calibrated = "calibrated" if judge.get("calibrated") else "not calibrated"
    needs = f"{_number(judge.get('min_agreement'))} over {_number(judge.get('min_pairs'))}"
    lines = [
        f"Evals digest for {since} to {until} ({zone} days): the evals table's day totals, added up.",
        "",
        f"Models ({len(models)}), worst first:",
        *(_bounded(models) or ["  none with events"]),
        "",
        f"Prompt versions ({len(prompts)}):",
        *(_bounded(prompts) or ["  none with events"]),
        "",
        (
            f"Judge: {switch}, agreement {_number(judge.get('agreement'))} over "
            f"{_number(judge.get('pairs'))} pairs, {calibrated} (needs {needs})."
        ),
        (
            f"Rollup: last final day {_shaped(_DAY, digest.get('last_rolled_day')) or '-'}, late events "
            f"dropped {_number(digest.get('late_dropped'))}."
        ),
        "",
        "`admin_cli evals digest` shows the same with each reason's totals; `admin_cli evals set "
        "--digest-email off` stops this email.",
    ]
    return subject, "\n".join(lines)


def _claim(table, day: str) -> bool:
    """Take `day` for this run's digest on the index row; False when a run has already taken it."""
    try:
        table.update_item(
            Key=evals_rollup.INDEX_KEY,
            UpdateExpression="SET #stamp = :day",
            ConditionExpression="attribute_not_exists(#stamp) OR #stamp < :day",
            ExpressionAttributeNames={"#stamp": STAMP_FIELD},
            ExpressionAttributeValues={":day": day},
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def _release(table, day: str) -> None:
    """Give `day` back after a publish that failed, so the next hourly run tries again."""
    try:
        table.update_item(
            Key=evals_rollup.INDEX_KEY,
            UpdateExpression="REMOVE #stamp",
            ConditionExpression="#stamp = :day",
            ExpressionAttributeNames={"#stamp": STAMP_FIELD},
            ExpressionAttributeValues={":day": day},
        )
    except Exception as exc:  # noqa: BLE001 - the next Friday sends; this one is missed
        print(f"evals_digest: could not give the day back after a failed publish: {exc!r}")


def maybe_send(now: datetime | None = None, sns=None) -> dict:
    """Publish this week's digest if it is due (see the module's docstring): what happened, as a
    code (`off`, `no_topic`, `not_due`, `sent_already`, `failed`, `sent`)."""
    now = now or datetime.now(UTC)
    settings = evals_settings.get_settings()
    if not settings["digest_email"]:
        return {"status": "off"}
    topic = os.environ.get(TOPIC_ENV, "")
    if not topic:
        print("evals_digest: digest_email is on but ALERTS_TOPIC_ARN is empty; nothing sent")
        return {"status": "no_topic"}
    local = now.astimezone(evals_settings.zone(settings))
    if local.weekday() != FRIDAY or local.hour < DIGEST_HOUR:
        return {"status": "not_due"}
    day = local.date().isoformat()
    table = evals_rollup.evals_table()
    if not _claim(table, day):
        return {"status": "sent_already", "day": day}
    try:
        subject, message = render(evals_status.digest(None, now), os.environ.get("ENVIRONMENT_NAME", ""))
        (sns or _client()).publish(TopicArn=topic, Subject=subject, Message=message)
    except Exception as exc:  # noqa: BLE001 - the claim is given back and the next hour tries again
        print(f"evals_digest: not sent: {exc!r}")
        _release(table, day)
        return {"status": "failed", "day": day}
    print(f"evals_digest: sent the digest for the week to {day}")
    return {"status": "sent", "day": day}
