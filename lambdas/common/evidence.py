"""The evidence trail: one JSON line per step the pipeline takes, and the parser every reader uses.

docs/enhancements/archive/vision-evidence.md. The research tick, the vision adapters, the vision worker
and the admin API each print one line per step (a tick ran, a site's turn, OpenCV measured, the
numbers were compared, the agent decided, a person approved), so that what the pipeline saw and
decided can be asked of the logs alone: Logs Insights in the console (the runsheet's queries), the
operator's assistant, and the export that goes into git.

**Every value is code-derived.** A line holds codes (`^[a-z][a-z0-9_]{0,39}$`), ids (topic, site,
scene, request, run, article and queue ids), times, numbers, hex build prefixes and canonical model
ids, and nothing else: never a site or station name, an exception's message, a news title, URL or
host, a summary, or the agent's reason (its SHA-256 prefix only). That is not a filter applied to
text but the schema itself (EVENTS): a field is kept only when its value passes its kind's check,
and a value that fails is dropped and its *name* recorded under `dropped`, so schema drift can be
read off the log. The logs are read by log_review and by people; third-party and model text never
belongs in them. The agent's reason and the hosts of the news it read live in the running state's
decision ring (common/vision_decisions.py), never here.

**The line's shape is fixed.** It starts with PREFIX (`evidence` then `event` as the first two
keys), which ops_mcp/log_review.py relies on to leave these lines out of its error search (their
codes include words such as "failed" and "timeout"). Lists are cut, never dropped whole (over the
cap, or a step that is not a step is left out), and the field's name goes under `cut`; a line over
MAX_LINE_BYTES loses its step results first, then its steps, and is last replaced by a stub that
says `truncated`. `ensure_ascii` keeps every line to one line of ASCII.

**Print, not logging.** The handlers already print. In Lambda's default Text log format a printed
line is written as it is, so Logs Insights discovers its JSON fields (`filter event = "..."`).
Switching a function to the JSON log format would nest the line under `message`, and the queries
would need `message.event`.

**Never raises.** Bookkeeping never fails the work, as with stats_tracking: any exception inside
prints one fixed line naming the exception's type, and returns.

**A reader parses, never trusts.** `parse` takes a log message back through the same kinds: it
returns None for anything that is not an evidence line of this version with a known event, drops
every field that fails its kind, and never raises. A line in a log group is only as trustworthy as
every print in that group, so readers filter by field equality (never a substring) and report what
they cannot match.

No cv2, numpy or boto3 here: the adapter registry imports this through the adapters, and every
pipeline Lambda imports the registry.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import math
import re
import uuid
from datetime import datetime

from common.model_pricing import canonical_model_id

VERSION = "bloggerbear/1"
# Every line starts with this (the first two keys, in this order, with these separators).
PREFIX = '{"evidence": "bloggerbear/1", "event": "'
# CloudWatch takes events of 256 KB; 8 KB keeps every line readable in the console, and a line this
# long holds eight steps with their results.
MAX_LINE_BYTES = 8192
# The caps on lists: a triage's steps (the trail keeps them all; the line and the ring the first
# of them, with the total beside), codes (tools called, quality flags, measures moved, config keys)
# and ids (the worker request ids of one triage).
MAX_STEPS, MAX_TOOLS, MAX_IDS = 8, 12, 8
MAX_CODES = MAX_TOOLS
# A `numbers` dict keeps at most this many entries.
MAX_NUMBERS = 12
# How many characters of a SHA-256 a `hex12` field carries.
HEX_PREFIX = 12
# An iso time's longest reasonable form: "2026-10-15T03:04:05.123456+00:00".
MAX_ISO_CHARS = 40

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
# A CPU's name (vision_contract.cpu_identity): "Neoverse V1 (Graviton3)", "Intel(R) Xeon(R) Platinum
# 8375C CPU @ 2.90GHz", or the machine name ("aarch64", "unknown") when /proc/cpuinfo names none.
# A shape, not a filter (a run of plain words passes it): the emitters fill it from cpu_identity()
# alone, and the worker's serialiser keeps a copy of this pattern.
_CPU = re.compile(r"^[A-Za-z0-9 @().,_+-]{1,80}$")
_VERSION = re.compile(r"^[0-9][0-9.]{0,19}$")

# What each tool's `result` may carry in a triage step (common/vision_triage.summarise_step builds
# it), each field with its own kind. `code` is every tool's failure code (`budget` for a refusal
# past the tool budget, `no_time`, `bad_settings`, the worker's error code, `tool_failed`, `none`,
# `unavailable`, `search_failed`, `unknown_tool`). The hosts a `web_context` call read are not here:
# a host is part of a URL (third-party text), and the ring alone keeps them.
STEP_RESULT = {
    "look_again": {
        "settings": "numbers",
        "measured": "numbers",
        "quality_flags": "codes",
        "worker_request_id": "id",
        "build": "hex12",
        "worker_ms": "int",
        "code": "code",
    },
    "previous_scene": {"scene_id": "id", "figure": "bool", "code": "code"},
    "site_history": {"scenes": "int", "code": "code"},
    "web_context": {"results": "int", "dated": "int", "code": "code"},
    "other": {"code": "code"},
}

# The fields every line may carry: what `bind` sets for the invocation, and this module's own
# bookkeeping.
COMMON = {
    "tick": "id",
    "run": "id",
    "topic_id": "id",
    "api_request_id": "id",
    "dropped": "codes",
    "cut": "codes",
    "truncated": "bool",
}
# Always written, null when not bound, so every line has the same first keys.
_ALWAYS = ("tick", "run", "topic_id")

# The schema: every event, its fields and each field's kind. Declared, not inferred: a field that
# is not here is never written, and a value that fails its kind is dropped and named.
EVENTS: dict[str, dict[str, str]] = {
    # The research tick, once per invocation, in a `finally` (a timeout skips it: readers treat a
    # missing line as "not kept").
    "research_tick": {
        "adapter": "code",
        "outcome": "code",
        "stored": "bool",
        "decisions": "int",
        "triages": "int",
        "finding": "iso",
        "force": "bool",
        "ms": "int",
        "error_type": "id",
    },
    # Each vision adapter, right after each site's turn, and for each site the loop passed over.
    "vision_site": {
        "site_id": "id",
        "outcome": "code",
        "code": "code",
        "scene_id": "id",
        "worker_request_id": "id",
        "worker_ms": "int",
        "budget_left_s": "num",
    },
    # Each vision adapter's material_diff, once per verdict of assess() (the ring entry's line).
    "vision_decision": {
        "site_id": "id",
        "scene_id": "id",
        "at": "iso",
        "captured_at": "iso",
        "coverage": "num",
        "measure": "numbers",
        "baseline": "numbers",
        "delta": "numbers",
        "relative": "numbers",
        "baseline_scenes": "int",
        "thresholds": "numbers",
        "code": "code",
        "moved": "codes",
        "network_changed": "bool",
        "triaged": "bool",
        "verdict": "code",
        "stop": "code",
        "reported": "bool",
        "backend": "code",
        "build": "hex12",
        "cpu": "cpu",
        "worker_request_id": "id",
    },
    # Each vision adapter's _triage, right after each site's outcome.
    "vision_triage": {
        "site_id": "id",
        "scene_id": "id",
        "verdict": "code",
        "stop": "code",
        "model_id": "model",
        "model_calls": "int",
        "input_tokens": "int",
        "output_tokens": "int",
        "cost_usd": "num",
        "elapsed_ms": "int",
        "deadline_left_s": "num",
        "figure": "bool",
        "tools": "codes",
        "tools_total": "int",
        "look_again": "int",
        "budget_refusals": "int",
        "steps": "steps",
        "steps_total": "int",
        "worker_request_ids": "ids",
        "reason_sha256": "hex12",
    },
    # The vision worker, every request that returns (vision_worker_handler.py writes it with its own
    # serialiser, in this order; a test holds the two equal).
    "vision_measure": {
        "request_id": "id",
        "task": "code",
        "backend": "code",
        "ok": "bool",
        "code": "code",
        "site_id": "id",
        "scene_id": "id",
        "build": "hex12",
        "opencv": "version",
        "kleidicv": "bool",
        "cpu": "cpu",
        "cpu_count": "int",
        "arch": "code",
        "window_px": "numbers",
        "io_requests": "int",
        "io_bytes": "int",
        "levels": "numbers",
        "read_ms": "int",
        "analyse_ms": "int",
        "graph_ms": "int",
        "annotate_ms": "int",
        "total_ms": "int",
        "coverage": "num",
        "quality_flags": "codes",
        "stations": "int",
        "snapped": "int",
        "nodes": "int",
        "edges": "int",
        "components": "int",
        "count": "int",
        "rss_peak_mb": "int",
    },
    # The admin API: a person approved, rejected, force-published, unpublished or sent back an
    # article. Never who.
    "moderation_decision": {
        "queue_id": "id",
        "article_id": "id",
        "decision": "code",
        "from_status": "code",
        "by": "code",
    },
    # The admin API: a topic was created or changed. Generic over every adapter: the keys that
    # changed, the new numbers and switches; never a name, a polygon or any other operator text.
    "topic_changed": {
        "action": "code",
        "fields": "codes",
        "config_keys": "codes",
        "config_numbers": "numbers",
        "config_on": "codes",
        "config_off": "codes",
        "force_manual_review": "bool",
    },
    # The model catalog (common/model_catalog.py, common/model_fleet.py): one line when a job ends,
    # with its counts. `job` is discover, prune, cycle, reprice (its pricing pass), review (one of a
    # run's reviews, numbered) or classify; `status` the job's result code. Pipeline health reads
    # the counters off these lines. Never a model's name or id.
    "model_catalog_job": {
        "job": "code",
        "trigger": "code",
        "status": "code",
        "run_id": "id",
        "mode": "code",
        "models": "int",
        "max_models": "int",
        "discovered": "int",
        "not_added": "int",
        "denied": "int",
        "access_checked": "int",
        "pruned": "int",
        "protected": "int",
        "short_by": "int",
        "topics": "int",
        "added": "int",
        "dropped_inactive": "int",
        "repriced": "int",
        "confirmed": "int",
        "unpriced": "int",
        "lookup_errors": "int",
        "web_lookups": "int",
        "review": "int",
        "passed": "int",
        "deferred": "int",
        "reverted": "int",
        "dropped": "int",
        "classified": "int",
        "unknown": "int",
        "unchanged": "int",
    },
}

# The invocation this process is in: {"fields": {...}, "counts": {event: n}}, or None outside one.
_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar("evidence_context", default=None)


class _Dropped(Exception):
    """A value that fails its kind."""


# --- the kinds -------------------------------------------------------------------------------------


def _code(value):
    if isinstance(value, str) and _CODE.match(value):
        return value
    raise _Dropped


def _id(value):
    if isinstance(value, str) and _ID.match(value):
        return value
    raise _Dropped


def _iso(value):
    if not isinstance(value, str) or len(value) > MAX_ISO_CHARS:
        raise _Dropped
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _Dropped from exc
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _Dropped
    return value


def _int(value):
    return int(round(_number(value)))


def _num(value):
    value = _number(value)
    # Six significant figures: enough for any measure, and no binary expansion in a log line.
    return value if isinstance(value, int) else float(f"{value:.6g}")


def _bool(value):
    if isinstance(value, bool):
        return value
    raise _Dropped


def _hex12(value):
    if isinstance(value, str) and _HEX12.match(value):
        return value
    raise _Dropped


def _model(value):
    # An application-inference-profile ARN comes back from canonical_model_id unchanged, with the
    # account id in it (common/model_pricing.py), so it is dropped.
    canonical = canonical_model_id(value)
    if isinstance(canonical, str) and _MODEL.match(canonical) and not canonical.startswith("arn:"):
        return canonical
    raise _Dropped


def _cpu(value):
    if isinstance(value, str) and _CPU.match(value):
        return value
    raise _Dropped


def _version(value):
    if isinstance(value, str) and _VERSION.match(value):
        return value
    raise _Dropped


def _numbers(value):
    """A dict of at most MAX_NUMBERS `code` -> `num`: entries that fail are left out (a baseline's
    `station_count` of None, a value that is not a number), anything but a dict is dropped."""
    if not isinstance(value, dict):
        raise _Dropped
    kept = {}
    for key, item in value.items():
        try:
            kept[_code(key)] = _num(item)
        except _Dropped:
            continue
        if len(kept) >= MAX_NUMBERS:
            break
    return kept


_SCALAR = {
    "code": _code,
    "id": _id,
    "iso": _iso,
    "int": _int,
    "num": _num,
    "bool": _bool,
    "hex12": _hex12,
    "model": _model,
    "cpu": _cpu,
    "version": _version,
    "numbers": _numbers,
}
# The list kinds: their element kind and their cap.
_LISTS = {"codes": (_code, MAX_CODES), "ids": (_id, MAX_IDS)}


def _list(value, element, cap: int) -> tuple[list, bool]:
    """A list kind's value: every element through `element`, cut to `cap`. (kept, cut). A string
    is the writer's comma-joined form (parse)."""
    if isinstance(value, str):
        value = [part for part in value.split(",") if part] if value else []
    if not isinstance(value, list | tuple):
        raise _Dropped
    kept = [element(item) for item in value]
    return kept[:cap], len(kept) > cap


def _step(value) -> dict:
    """One triage step: {"tool", "ok", "ms", "result"}, the result's fields each through its own
    kind (STEP_RESULT; a failing one left out). A tool not in STEP_RESULT is `other`."""
    if not isinstance(value, dict):
        raise _Dropped
    tool = value.get("tool")
    tool = tool if isinstance(tool, str) and tool in STEP_RESULT else "other"
    step = {"tool": tool, "ok": _bool(value.get("ok")), "ms": _int(value.get("ms"))}
    result, given = {}, value.get("result")
    for name, kind in STEP_RESULT[tool].items():
        if not isinstance(given, dict) or given.get(name) is None:
            continue
        try:
            result[name] = _check(kind, given[name])[0]
        except _Dropped:
            continue
    step["result"] = result
    return step


def _check(kind: str, value) -> tuple[object, bool]:
    """`value` through `kind`: (the cleaned value, whether a list was cut: over its cap, or a step
    that is not a step left out); _Dropped if it fails."""
    if kind in _SCALAR:
        return _SCALAR[kind](value), False
    if kind in _LISTS:
        return _list(value, *_LISTS[kind])
    if kind == "steps":
        if isinstance(value, str) or not isinstance(value, list | tuple):
            raise _Dropped
        steps = []
        for item in value:
            try:
                steps.append(_step(item))
            except _Dropped:
                continue  # one malformed step is left out, and the field is `cut`; the rest stay
        return steps[:MAX_STEPS], len(steps) > MAX_STEPS or len(steps) < len(value)
    raise _Dropped


def check(kind: str, value):
    """`value` as `kind` keeps it (a list cut to its cap), or None when it fails: for a caller that
    wants to know before it writes (the admin API's request id, a build prefix)."""
    try:
        return _check(kind, value)[0]
    except _Dropped:
        return None


# --- the invocation ----------------------------------------------------------------------------------


def new_run() -> str:
    """A fresh id for one attempt of a tick: a retried invocation keeps Lambda's request id, and
    this tells the attempts apart."""
    return uuid.uuid4().hex[:12]


@contextlib.contextmanager
def bind(**fields):
    """One invocation: the ids every line in it carries (`tick`, `run`, `topic_id`,
    `api_request_id`) and what the handler notes along the way. Reset on exit, so a warm container
    never carries one tick's ids into the next."""
    token = _context.set({"fields": dict(fields), "counts": {}})
    try:
        yield
    finally:
        _context.reset(token)


def note(**fields) -> None:
    """Add to what is bound (the adapter's key, that the state was stored, the Finding's time).
    Nothing outside an invocation."""
    current = _context.get()
    if current is not None:
        current["fields"].update(fields)


def bound(key: str):
    """One bound value, or None."""
    current = _context.get()
    return None if current is None else current["fields"].get(key)


def emitted(event: str) -> int:
    """How many lines of `event` this invocation has printed."""
    current = _context.get()
    return 0 if current is None else current["counts"].get(event, 0)


# --- writing ----------------------------------------------------------------------------------------


def _fields(event: str, fields: dict) -> dict:
    """The record of one line: the bound ids, then `fields` through the event's kinds in the
    schema's order, then `dropped` and `cut`. A field given as None is not given."""
    schema = EVENTS[event]
    current = _context.get()
    bound_fields = current["fields"] if current else {}
    record = {"evidence": VERSION, "event": event}
    given = {**{key: bound_fields.get(key) for key in COMMON if key in bound_fields}, **fields}
    dropped, cut = [], []
    for key in _ALWAYS:
        record[key] = None
    order = [(key, "id") for key in (*_ALWAYS, "api_request_id")] + list(schema.items())
    for key, kind in order:
        value = given.get(key)
        if value is None:
            continue
        try:
            record[key], was_cut = _check(kind, value)
        except _Dropped:
            dropped.append(key)
            continue
        if was_cut:
            cut.append(key)
    for key in given:
        if key not in schema and key not in COMMON:
            dropped.append(key)
    if dropped:
        record["dropped"] = sorted(set(dropped))[:MAX_CODES]
    if cut:
        record["cut"] = cut[:MAX_CODES]
    return record


def _serialise(record: dict) -> str:
    """The line: lists comma-joined so Logs Insights can filter them, `evidence` and `event` first."""
    out = {}
    for key, value in record.items():
        if isinstance(value, list) and key != "steps":
            value = ",".join(value)
        out[key] = value
    return json.dumps(out, separators=(", ", ": "), ensure_ascii=True, allow_nan=False)


def _fit(record: dict) -> str:
    """The serialised line within MAX_LINE_BYTES: whole, else without the step results, else without
    the steps, else the stub that says so."""
    text = _serialise(record)
    if len(text) <= MAX_LINE_BYTES:
        return text
    if record.get("steps"):
        slimmed = dict(record)
        slimmed["steps"] = [{k: v for k, v in step.items() if k != "result"} for step in record["steps"]]
        text = _serialise(slimmed)
        if len(text) <= MAX_LINE_BYTES:
            return text
        slimmed.pop("steps")
        text = _serialise(slimmed)
        if len(text) <= MAX_LINE_BYTES:
            return text
    stub = {key: record.get(key) for key in ("evidence", "event", *_ALWAYS)}
    stub["truncated"] = True
    return _serialise(stub)


def line(event: str, **fields) -> str | None:
    """The cleaned, serialised line for `event`, or None for an event not in EVENTS (a programming
    error: one fixed line says so). Raises nothing of its own beyond what json could; `emit` is the
    guarded way to write one."""
    if event not in EVENTS:
        print(f"evidence: unknown event {event!r}")
        return None
    return _fit(_fields(event, fields))


def emit(event: str, **fields) -> None:
    """Print one line of `event`, and count it for `emitted`. Never raises."""
    try:
        text = line(event, **fields)
        if text is None:
            return
        print(text)
        current = _context.get()
        if current is not None:
            current["counts"][event] = current["counts"].get(event, 0) + 1
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the work
        print(f"evidence: could not write {event}: {type(exc).__name__}")


# --- reading ----------------------------------------------------------------------------------------


def parse(message) -> dict | None:
    """A reader's half: the fields of an evidence line, through the same kinds the writer used, or
    None when `message` is not one (not the prefix, not one JSON object, another version, an unknown
    event). Unknown and failing fields are dropped; comma-joined lists come back as lists; `tick`,
    `run` and `topic_id` are present only when the line had them. Never raises."""
    try:
        if not isinstance(message, str) or not message.startswith(PREFIX):
            return None
        record = json.loads(message)
        if not isinstance(record, dict) or record.get("evidence") != VERSION:
            return None
        event = record.get("event")
        if not isinstance(event, str) or event not in EVENTS:
            return None
        kinds = {**COMMON, **EVENTS[event]}
        out = {"evidence": VERSION, "event": event}
        for key, value in record.items():
            if key in ("evidence", "event") or key not in kinds or value is None:
                continue
            try:
                out[key] = _check(kinds[key], value)[0]
            except _Dropped:
                continue
        return out
    except Exception:  # noqa: BLE001 - a reader never falls over a log line
        return None
