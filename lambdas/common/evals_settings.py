"""The evals settings: the `evals` row of the model-config table (`config_id = "evals"`), read on
every run, so changing one needs no deploy.

docs/enhancements/model-ranch-prompt-assets-evals.md, section 4.5. The same pattern as the model
catalog's settings (common/model_catalog.py): SETTINGS names every setting with its default,
`validate_setting` says why a value is not valid, and `effective_settings` gives every setting's
value in effect, the row's where it is valid and the default otherwise, so a hand edit that breaks
one setting never stops a run.

Two kinds:

* **DynamoDB only** (DYNAMODB_ONLY): the gates (`daily_gate`, `weekly_gate`, `total_gate`), the
  events a window needs before a gate applies (`min_events`), and how well the judge must agree
  with people before its verdicts gate (`judge_min_agreement` over at least `judge_min_pairs`
  pairs). They are the Model Ranch's policy: no CLI flag sets them, and the admin API refuses them;
  a change is a deliberate edit of the row itself.
* **The CLI's** (`admin_cli evals set`): culling (`cull_enabled`, `cull_max_per_run`), the judge
  (`judge_enabled`, `judge_interval_hours`, `judge_max_items`, `judge_model_id`), spending
  (`score_max_cost_aud`, `evals_daily_spend_cap_aud`), `trace_calls`, the `timezone` whose days
  the rollup adds events up by, and `excluded_sources`, whose events the rollup leaves out.

**gated_windows** is what the gates say about one models row: the windows whose score is below
their gate with at least `min_events` events behind it. `evals status` shows them; the rotation
guard (PR15) excludes such models from its candidates.
"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from common import dynamo, evals, evidence

CONFIG_ID = "evals"

# Every setting and its default (section 4.5). A setting that is unset, or holds something invalid,
# takes this.
SETTINGS: dict[str, object] = {
    "daily_gate": -50,
    "weekly_gate": -100,
    "total_gate": -1000,
    "min_events": 10,
    "judge_min_agreement": 0.7,
    "judge_min_pairs": 20,
    "cull_enabled": False,
    "cull_max_per_run": 3,
    "judge_enabled": True,
    "judge_interval_hours": 4,
    "judge_max_items": 10,
    # None: the model-config default row's fallback model, else BEDROCK_MODEL_ID (judge_model).
    "judge_model_id": None,
    "score_max_cost_aud": 5,
    "evals_daily_spend_cap_aud": 2,
    "trace_calls": False,
    "timezone": "Australia/Sydney",
    "excluded_sources": [],
}
DYNAMODB_ONLY = frozenset(
    {"daily_gate", "weekly_gate", "total_gate", "min_events", "judge_min_agreement", "judge_min_pairs"}
)
# Not a setting: why the judge's tripwire turned `judge_enabled` off (common/eval_judge.py), written
# beside it on the row and shown by `summary`.
PAUSED_REASON_FIELD = "judge_paused_reason"

# The whole-number settings and their ranges.
_WHOLE = {
    "daily_gate": (-1_000_000, 0),
    "weekly_gate": (-1_000_000, 0),
    "total_gate": (-1_000_000, 0),
    "min_events": (1, 1000),
    "judge_min_pairs": (1, 1000),
    "cull_max_per_run": (1, 50),
    "judge_interval_hours": (1, 24),
    "judge_max_items": (1, 50),
}
# The settings that are any number in a range (Australian dollars, or a share).
_NUMBER = {
    "judge_min_agreement": (0, 1),
    "score_max_cost_aud": (0, 100),
    "evals_daily_spend_cap_aud": (0, 100),
}
_SWITCHES = ("cull_enabled", "judge_enabled", "trace_calls")
MAX_EXCLUDED_SOURCES = 20
# An excluded source that names an offline scoring run rather than a source or a decider.
RUN_SOURCE_PREFIX = "run:"

# The windows that have a gate (section 4.5; the monthly window has none): its name in perf_events,
# the models row's field, and the setting that is its gate.
GATED = (
    ("daily", "daily_perf", "daily_gate"),
    ("weekly", "weekly_perf", "weekly_gate"),
    ("total", "total_perf", "total_gate"),
)


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def source_selector_error(selector) -> str | None:
    """Why `selector` is not something `excluded_sources` can name, or None: a decider (`human`,
    `judge`, `system`: the event's `by`), `run:<run id>` (one offline scoring run's events), or the
    code of the Lambda that emitted the events (the event's `source`)."""
    if not isinstance(selector, str):
        return "must be a string"
    if selector.startswith(RUN_SOURCE_PREFIX):
        run_id = selector[len(RUN_SOURCE_PREFIX) :]
        return None if evidence.check("id", run_id) is not None and "\n" not in run_id else "names no run id"
    if evidence.check("code", selector) is None or selector.endswith("\n"):
        return "must be a decider (human, judge, system), run:<run id>, or a source's code"
    return None


def _excluded_sources_error(value) -> str | None:
    if not isinstance(value, list) or len(value) > MAX_EXCLUDED_SOURCES:
        return f"'excluded_sources' must be a list of at most {MAX_EXCLUDED_SOURCES} sources"
    for entry in value:
        if isinstance(entry, dict):
            if set(entry) - {"source", "since"} or "source" not in entry:
                return "an 'excluded_sources' entry that is an object holds 'source' and, optionally, 'since'"
            since = entry.get("since")
            if since is not None and evidence.check("iso", since) is None:
                return "an 'excluded_sources' entry's 'since' must be an ISO time"
            selector = entry["source"]
        else:
            selector = entry
        problem = source_selector_error(selector)
        if problem:
            return f"'excluded_sources': {selector!r} {problem}"
    return None


def validate_setting(name: str, value) -> str | None:
    """Why `value` is not a valid `name`, or None when it is (None, which clears a setting, is
    always valid)."""
    if value is None:
        return None
    if name not in SETTINGS:
        return f"unknown setting '{name}'"
    if name in _SWITCHES:
        return None if isinstance(value, bool) else f"'{name}' must be true or false"
    if name in _WHOLE:
        low, high = _WHOLE[name]
        if _whole(value) and low <= value <= high:
            return None
        return f"'{name}' must be a whole number from {low} to {high}"
    if name in _NUMBER:
        low, high = _NUMBER[name]
        if _number(value) and low <= value <= high:
            return None
        return f"'{name}' must be a number from {low} to {high}"
    if name == "judge_model_id":
        if isinstance(value, str) and value and not value.endswith("\n") and evidence.check("model", value):
            return None
        return "'judge_model_id' must be a model id"
    if name == "timezone":
        if _zone(value) is not None:
            return None
        return "'timezone' must be an IANA time zone, e.g. Australia/Sydney"
    return _excluded_sources_error(value)


def _zone(value) -> ZoneInfo | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return ZoneInfo(value)
    except (ValueError, KeyError, OSError):  # ZoneInfoNotFoundError is a KeyError
        return None


def effective_settings(row: dict | None) -> dict:
    """Every setting's value in effect: the row's where it is valid, else the default."""
    row = row or {}
    settings = {}
    for name, default in SETTINGS.items():
        value = row.get(name)
        if value is not None and validate_setting(name, value) is not None:
            print(f"evals_settings: ignoring invalid {name}={value!r}; using {default!r}")
            value = None
        settings[name] = copy.deepcopy(default) if value is None else value
    return settings


def get_row() -> dict:
    """The `evals` row as stored ({} while there is none)."""
    return dynamo.get_config_row(CONFIG_ID) or {}


def get_settings() -> dict:
    return effective_settings(get_row())


def zone(settings: dict) -> ZoneInfo:
    """The time zone whose days the rollup and the windows count in."""
    return _zone(settings.get("timezone")) or ZoneInfo(str(SETTINGS["timezone"]))


def judge_model(settings: dict, config: dict | None = None) -> str | None:
    """The judge's model (section 4.5): `judge_model_id`, else the model-config default row's
    fallback model, else BEDROCK_MODEL_ID. `config` is that row (read here when not given)."""
    if settings.get("judge_model_id"):
        return settings["judge_model_id"]
    config = dynamo.get_model_config() if config is None else config
    return (config or {}).get("fallback_model_id") or os.environ.get("BEDROCK_MODEL_ID") or None


def excluded(record: dict, settings: dict) -> bool:
    """Whether one event (common/evals.parse's record) is left out by `excluded_sources`: its
    decider (`by`), its offline run (`offline:<run id>:...`) or its source (`source`) is named, by
    an entry with no `since` or a `since` at or before the event's time."""
    for entry in settings.get("excluded_sources") or ():
        selector, since = entry, None
        if isinstance(entry, dict):
            selector, since = entry.get("source"), entry.get("since")
        if since is not None:
            at, start = _parse(record.get("timestamp")), _parse(since)
            if at is None or start is None or at < start:
                continue
        if selector in evals.BY:
            matched = record.get("by") == selector
        elif isinstance(selector, str) and selector.startswith(RUN_SOURCE_PREFIX):
            run_id = selector[len(RUN_SOURCE_PREFIX) :]
            matched = str(record.get("event_id", "")).startswith(f"offline:{run_id}:")
        else:
            matched = record.get("source") == selector
        if matched:
            return True
    return False


def _parse(value) -> datetime | None:
    """An ISO time, aware (a time without a zone is UTC); None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def ungated(row: dict, now: datetime) -> bool:
    """Whether the models row's `ungated_until` (an operator's override) is still ahead of `now`."""
    until = _parse(row.get("ungated_until"))
    return until is not None and until > now


def gated_windows(row: dict, settings: dict, now: datetime | None = None) -> list[str]:
    """The windows (`daily`, `weekly`, `total`) whose score on the models row is below its gate
    with at least `min_events` events behind it (section 4.5). [] for a row with no scores yet, or
    scores that cannot be read: a gate never applies on a guess. `ungated_until` is not looked at
    here (see `ungated`), so a caller can say what an override is holding back."""
    events = row.get("perf_events") if isinstance(row.get("perf_events"), dict) else {}
    found = []
    for window, field, gate in GATED:
        perf, count = _as_number(row.get(field)), _as_number(events.get(window))
        if perf is None or count is None:
            continue
        if count >= settings["min_events"] and perf < settings[gate]:
            found.append(window)
    return found


def _as_number(value) -> float | None:
    """A number as a float, whether plain or DynamoDB's Decimal (dynamo.list_models converts only
    the prices); None for anything else."""
    return float(value) if _number(value) or isinstance(value, Decimal) else None


def summary() -> dict:
    """The row as stored, every setting in effect, the judge's model and, when the judge's tripwire
    turned it off (common/eval_judge.py), why: for the admin API's GET /evals-config."""
    row = get_row()
    effective = effective_settings(row)
    return {
        "config_id": CONFIG_ID,
        "settings": {name: row.get(name) for name in SETTINGS},
        "effective": effective,
        "judge_model": judge_model(effective),
        PAUSED_REASON_FIELD: row.get(PAUSED_REASON_FIELD),
        "dynamodb_only": sorted(DYNAMODB_ONLY),
    }
