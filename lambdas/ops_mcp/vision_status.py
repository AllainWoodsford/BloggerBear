"""`vision_status`: whether the vision worker is on and where, and for each topic that watches
satellite imagery how its sites are doing: when each was last measured, and what stopped the ones
that were not. "How is the vision pipeline doing?", "is the satellite worker on?", "why wasn't
Sydney measured?". pipeline_health and the memory's topic watch use the same reading
(`site_health`) for their one vision sentence.

Not `ops_mcp/vision.py`: `lambdas/vision/` is the OpenCV package, imported as `vision`.

**What is read.** The Topics table (one Scan), and for each topic whose adapter watches sites
(`Adapter.status` is not None, rule 5: no adapter is named here) its running state,
`snapshots/<topic>/running-state.json` in the content bucket, the one place a site's last error
is kept (the research tick writes it; the adapters print only a failed figure store). The state
goes straight through the adapter's own `status` hook, a pure method that returns code-derived
fields only, and the parsed state is dropped. The worker's Region is the deployment's setting
(OPS_VISION_REGION, set only where vision is on): with it unset nothing is read at all. The pipeline
config (for each topic's research interval) and this week's stats row (for the triage agent's
spend) are already readable.

**Why the reads are safe and bounded.** The role may read only `snapshots/*/running-state.json`
(infra/modules/ops-assistant/vision.tf), and the key is built here only from an id that passes
ID_PATTERN: any other id is refused before S3 is called. A rail state can pass 20 MB, the
function has 512 MB, and pipeline_health opens every briefing, so the reads share one budget,
weighted by each object's ContentLength and taken before its body is read (IN_FLIGHT_BYTES): two
large states never parse at once. pipeline_health and the memory watch read nothing over
LARGE_STATE_BYTES, and nothing at all is read over MAX_STATE_BYTES. Each call has a deadline (8 s
here, 3 s for pipeline_health, 2 s per topic for the watch), S3 is asked with a short timeout and
no retry, and the deadline is read before GetObject, once it answers, between the chunks a body is
read in and before the parse (and the budget gives nothing past it). A parse is not interrupted, so
it is begun only with time left for the state's size at the slowest rate measured
(PARSE_BYTES_PER_SECOND), and so is the read that leads to it: a call ends by its deadline, or, when
a parse begun with that time to spare runs slower still (a slower CPU, or parses sharing one), up to
that parse's own length later. Every body is closed and every thread joined before the call
returns: nothing keeps reading after the answer.

**What is never spoken.** `spoken` is fixed templates over counts, ages, the Region, topics named
by `tools.spoken_label`, and words from the adapters' declared vocabularies (each checked against
it here). Never a site's name or id, an error's detail, a config error or a scene id. Those are
operator, exception or third-party text: a site's name, an error's detail and the config error go,
scrubbed (redact.scrub), only into the table's "(unverified)" columns, and only for a caller with a
screen (memory.screen_caller): Alexa+ gets counts, ages and code words only. No `untrusted` block:
the page would never show it (ops_agent/policy.py copies only spoken, findings and table), and no
card carries operator or third-party text.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, ReadTimeoutError

from common.adapters.registry import ADAPTER_REGISTRY
from common.dynamo import get_current_stats, get_pipeline_config, list_topics
from common.research_schedule import next_due_at, resolve_interval_hours
from common.vision_sites import SITE_ID
from ops_mcp import architecture, redact, topic_match
from ops_mcp.suggestions import ID_PATTERN, finding
from ops_mcp.tools import _age, _join, _parse, spoken_label

# The deployment's setting: the vision worker's Region when vision is on, "" when it is off
# (infra/modules/ops-assistant/main.tf). One variable is both the switch and the Region.
VISION_REGION_ENV = "OPS_VISION_REGION"
WORKER_FUNCTION = "vision-worker"

# The read budgets (the module docstring says why). The bytes bound the download, not the parsed
# size. A real rail state (11.3 MB measured, 22.6 MB with CJK names at the caps: common/adapters/
# rail_access.py, MAX_RAIL_SITES) parses to about six times its bytes; a 24 MiB JSON of a million
# tiny site records, which only a hostile writer of the content bucket could put under snapshots/
# (the research tick alone writes there), parses and is copied (topic_status's records and stamps)
# to about seventeen times, 496 MB. The in-flight budget serialises such reads to one at a time in
# the process; the function's memory (512 MB, infra/modules/ops-assistant/main.tf) is the knob if a
# state ever grows past that. Neither constant can shrink: MAX_STATE_BYTES below a real state would
# refuse it, and IN_FLIGHT_BYTES below MAX_STATE_BYTES would refuse the largest readable one.
MAX_STATE_BYTES = 24 * 1024 * 1024
LARGE_STATE_BYTES = 8 * 1024 * 1024
IN_FLIGHT_BYTES = 24 * 1024 * 1024
READS_AT_ONCE = 4
READ_SECONDS = 8.0
HEALTH_READ_SECONDS = 3.0
WATCH_READ_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 2.0
CHUNK_BYTES = 256 * 1024
# A parse is not interrupted once begun, so it is begun only with time left for its size at the
# slowest rate measured: json.loads and the adapter's status() took 3.8 s on a 24 MiB state of a
# million tiny site records (about 6 MiB a second); a real-shaped 24 MiB state takes 0.7 s. The cost
# is a real state refused in the last seconds of a call (a 22 MB one with under 3.5 s left), which
# then says "not_checked", as a read the deadline stops does.
PARSE_BYTES_PER_SECOND = 6 * 1024 * 1024
# snapshots/ expires after this many days (the content bucket's lifecycle rule, infra/environments/
# */main.tf): a state older than that is gone, not unreadable.
SNAPSHOT_DAYS = 21
# A site that keeps waiting for time: the topic's ticks are running, and none of its sites has
# been worked on in this many research intervals.
STARVED_INTERVALS = 3

CLASSES = (
    "config_error",
    "worker_off",
    "not_checked",
    "refused",
    "site_problem",
    "worker_error",
    "source_error",
    "starved",
    "deferred",
    "no_scene",
    "never_tried",
    "measured",
)
PROBLEM_CLASSES = ("config_error", "refused", "site_problem", "worker_error", "source_error", "starved")
READ_STATES = ("read", "missing", "unreadable", "too_large", "not_checked", "worker_off")
KINDS = ("worker", "source", "site", "deferred", "other")
BACKENDS = ("opencv", "cool")
OTHER_WORDS = "another error"

# The checks every value from an adapter's hook passes before it is shown (rule 5: the adapters
# own the words, this module only checks them).
_CODE = re.compile(r"^[a-z_]{1,40}$")
_SCENE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_BUILD = re.compile(r"^[0-9a-f]{1,12}$")
MAX_QUALITY_FLAGS = 8
NAME_CELL_CHARS = 60
TEXT_CELL_CHARS = 120
SPOKEN_MAX_WORDS = 120
PROBLEM_SENTENCES_MAX = 4
LABELS_PER_SENTENCE = 3

COLUMNS_SCREEN = [
    "Topic",
    "Site",
    "Name (unverified)",
    "Turn",
    "Last scene",
    "Coverage",
    "Measure",
    "Triage",
    "Measured by",
    "Figure",
    "Last tried",
    "State",
    "Map data",
    "Error (unverified)",
]
COLUMNS_VOICE = [column for column in COLUMNS_SCREEN if not column.endswith("(unverified)")]

_s3_lock = threading.Lock()


# --- the worker --------------------------------------------------------------------------------------


def worker_region() -> str | None:
    """The vision worker's Region, or None when vision is off here: architecture.component_region's
    rule for the catalogue's vision-worker (region_env OPS_VISION_REGION). A value that is not a
    Region counts as off, with one printed line."""
    raw = os.environ.get(VISION_REGION_ENV, "")
    if not raw:
        return None
    found = architecture.component_region(architecture.by_key("function", WORKER_FUNCTION))
    if found is None:
        print(f"vision_status: {VISION_REGION_ENV} is not a Region, so vision counts as off here")
    return found


def worker_on() -> bool:
    return worker_region() is not None


def worker(env: str | None) -> dict:
    """Whether the worker is on, where, its log group and a console link to it."""
    where = worker_region()
    if where is None:
        return {"on": False, "region": None, "log_group": None, "console_url": None}
    component = architecture.by_key("function", WORKER_FUNCTION)
    log_group = architecture.fill(component.log_groups[0], env)
    return {
        "on": True,
        "region": where,
        "log_group": log_group,
        "console_url": architecture.console_url("log_group", log_group, where),
    }


# --- which topics watch sites ------------------------------------------------------------------------


@lru_cache(maxsize=1)
def vocabulary() -> frozenset[str]:
    """Every phrase an adapter's status may carry: the union of their declared vocabularies."""
    return frozenset().union(*(cls.status_vocabulary() for cls in ADAPTER_REGISTRY.values()))


def site_topics(items: list[dict], now: datetime) -> list[dict]:
    """The topics whose adapter watches sites (its `status` is not None), each with its adapter and
    its config-only status. A topic with no adapter, or an unknown one, is skipped, never a KeyError."""
    found = []
    for item in items:
        key = item.get("adapter") if isinstance(item, dict) else None
        cls = ADAPTER_REGISTRY.get(key) if isinstance(key, str) else None
        if cls is None:
            continue
        adapter = cls()
        try:
            shape = adapter.status(item, None, now=now)
        except Exception as exc:  # noqa: BLE001 - one adapter's bug must not cost every topic's answer
            print(f"vision_status: the {key} adapter's status failed: {type(exc).__name__}")
            continue
        if shape is not None:
            found.append({"item": item, "adapter": adapter, "key": key, "config_only": shape})
    return found


# --- reading the running state -----------------------------------------------------------------------


def running_state_key(topic_id: str) -> str:
    """research_tick_handler._running_state_key, restated: that handler is not in this function's
    package (infra/modules/ops-assistant/main.tf copies only common/ and ops_mcp/). A test holds
    the two equal."""
    return f"snapshots/{topic_id}/running-state.json"


class _ByteBudget:
    """One budget for every state being read at once in this process, in bytes: a read takes its
    object's ContentLength before its body is read and gives it back once the state is parsed and
    dropped. A read that cannot get its bytes before its deadline is not made, nor one whose
    deadline has passed while there is room: it would end after it."""

    def __init__(self, total: int):
        self.total, self.used = total, 0
        self._changed = threading.Condition()

    def take(self, size: int, deadline: float) -> bool:
        with self._changed:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                if self.used + size <= self.total:
                    self.used += size
                    return True
                self._changed.wait(left)

    def give(self, size: int) -> None:
        with self._changed:
            self.used -= size
            self._changed.notify_all()


_IN_FLIGHT = _ByteBudget(IN_FLIGHT_BYTES)


def _s3(read_timeout: float):
    """An S3 client for one call: a one-second connect, a short read, and no retry (the next
    question asks again; the call's deadline is what binds). One attempt in all: botocore reads
    `max_attempts` as the number of retries, so it is `total_max_attempts` that says one."""
    config = Config(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=max(read_timeout, 0.1),
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
    with _s3_lock:  # creating a client from the default session is not thread-safe
        return boto3.client("s3", config=config)


def _recently_researched(topic: dict, now: datetime) -> bool:
    last = _parse(topic.get("last_research_at"))
    return last is not None and now - last <= timedelta(days=SNAPSHOT_DAYS)


def _time_to_parse(size: int, deadline: float) -> bool:
    """Whether a state of `size` bytes, parsed from now at PARSE_BYTES_PER_SECOND, ends before
    `deadline` (False once the deadline has passed, whatever the size)."""
    return deadline - time.monotonic() > size / PARSE_BYTES_PER_SECOND


def _read_one(client, bucket: str, topic: dict, deadline: float, max_bytes: int, then, now: datetime):
    """One topic's running state through `then(state)`, and "read"; or None and why not."""
    topic_id = topic.get("topic_id")
    if not isinstance(topic_id, str) or not ID_PATTERN.match(topic_id):
        return None, "unreadable"  # never a key built from an id the IAM grant does not expect
    if time.monotonic() >= deadline:
        return None, "not_checked"
    try:
        response = client.get_object(Bucket=bucket, Key=running_state_key(topic_id))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        print(f"vision_status: S3 answered {code or 'an error'} for a running state")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None, "missing"
        if code in ("AccessDenied", "403", "Forbidden"):
            # The role has no ListBucket, so a missing key is answered 403 too: it is missing when
            # the topic was not researched within the snapshots' lifetime, else something is wrong.
            return None, "unreadable" if _recently_researched(topic, now) else "missing"
        return None, "unreadable"
    except (ConnectTimeoutError, ReadTimeoutError):
        return None, "not_checked"
    except BotoCoreError as exc:
        print(f"vision_status: S3 could not be asked for a running state: {type(exc).__name__}")
        return None, "not_checked"
    body = response.get("Body")
    size = response.get("ContentLength")
    taken = 0
    try:
        if time.monotonic() >= deadline:
            return None, "not_checked"  # GetObject itself answered after it
        if not isinstance(size, int) or size < 0:
            return None, "unreadable"
        if size > MAX_STATE_BYTES or size > _IN_FLIGHT.total:
            return None, "too_large"
        if size > max_bytes or not _time_to_parse(size, deadline):
            return None, "not_checked"
        if not _IN_FLIGHT.take(size, deadline):
            return None, "not_checked"
        taken = size
        data = bytearray()
        for chunk in body.iter_chunks(CHUNK_BYTES):
            data += chunk
            if len(data) > size:
                return None, "unreadable"  # more than it said it was: not what was stored
            if time.monotonic() >= deadline:
                return None, "not_checked"
        if not _time_to_parse(size, deadline):
            return None, "not_checked"  # the parse would end after it, or the stream did
        try:
            state = json.loads(data)
        except (ValueError, RecursionError):
            return None, "unreadable"
        del data
        if not isinstance(state, dict):
            return None, "unreadable"
        return then(state), "read"
    except (ConnectTimeoutError, ReadTimeoutError):
        return None, "not_checked"
    except (BotoCoreError, OSError) as exc:
        print(f"vision_status: a running state could not be read: {type(exc).__name__}")
        return None, "not_checked"
    finally:
        if body is not None:
            try:
                body.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        if taken:
            _IN_FLIGHT.give(taken)


def running_states(topics: list[dict], *, seconds: float, max_bytes: int, then, now: datetime) -> dict:
    """Each topic's running state through `then(topic, state)`, read within `seconds` and
    `max_bytes` each, at most READS_AT_ONCE at a time: {topic_id: (then's result or None, "read" or
    the reason it was not read)}. Every body is closed and every thread done when this returns."""
    if not topics:
        return {}
    deadline = time.monotonic() + seconds
    bucket = os.environ.get("CONTENT_BUCKET", "")
    if not bucket:
        print("vision_status: CONTENT_BUCKET is not set, so no running state is read")
        return {topic.get("topic_id"): (None, "unreadable") for topic in topics}
    client = _s3(min(READ_TIMEOUT_SECONDS, seconds))
    results = {}
    with ThreadPoolExecutor(max_workers=READS_AT_ONCE) as pool:
        futures = {
            topic.get("topic_id"): pool.submit(
                _read_one, client, bucket, topic, deadline, max_bytes, lambda s, t=topic: then(t, s), now
            )
            for topic in topics
        }
        for topic_id, future in futures.items():
            try:
                results[topic_id] = future.result()
            except Exception as exc:  # noqa: BLE001 - one topic's failure is that topic's answer
                print(f"vision_status: reading a running state failed: {type(exc).__name__}")
                results[topic_id] = (None, "unreadable")
    return results


def running_state(topic: dict, *, seconds: float = READ_SECONDS, max_bytes: int = MAX_STATE_BYTES, now=None):
    """One topic's running state and "read", or None and the reason (running_states)."""
    now = now or datetime.now(UTC)
    found = running_states([topic], seconds=seconds, max_bytes=max_bytes, then=lambda t, s: s, now=now)
    return found.get(topic.get("topic_id"), (None, "unreadable"))


# --- checking what a hook returned -------------------------------------------------------------------


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def _iso(value) -> str | None:
    parsed = _parse(value)
    return parsed.isoformat() if parsed else None


def _code(value) -> str:
    return value if isinstance(value, str) and _CODE.match(value) else "other"


def _said(value, words: frozenset) -> str | None:
    return value if isinstance(value, str) and value in words else None


def _error(raw, words: frozenset, *, kind: str | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    found = kind or (raw.get("kind") if raw.get("kind") in KINDS else "other")
    return {
        "code": _code(raw.get("code")),
        "kind": found,
        "words": _said(raw.get("words"), words) or OTHER_WORDS,
        "at": _iso(raw.get("at")),
        # The exception's or the server's own text: scrubbed, and only for an "(unverified)" cell.
        "detail": redact.scrub(raw.get("detail"), TEXT_CELL_CHARS) if raw.get("detail") else "",
    }


def _site_id(value) -> str | None:
    if not isinstance(value, str) or not SITE_ID.fullmatch(value) or redact.looks_like_instructions(value):
        return None
    return value


def _scene(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    scene_id = raw.get("scene_id")
    flags = raw.get("quality_flags") if isinstance(raw.get("quality_flags"), list) else []
    build = raw.get("build")
    coverage = _number(raw.get("coverage"))
    return {
        "scene_id": scene_id if isinstance(scene_id, str) and _SCENE_ID.match(scene_id) else None,
        "captured_at": _iso(raw.get("captured_at")),
        "coverage": coverage if coverage is not None and 0 <= coverage <= 1 else None,
        "triage": raw.get("triage") if raw.get("triage") in ("real", "artefact") else None,
        "quality_flags": [flag for flag in flags if isinstance(flag, str) and _CODE.match(flag)][
            :MAX_QUALITY_FLAGS
        ],
        "backend": raw.get("backend") if raw.get("backend") in BACKENDS else None,
        "build": build if isinstance(build, str) and _BUILD.match(build) else None,
        "figure_stored": raw.get("figure_stored") is True,
    }


def checked(raw: dict, words: frozenset | None = None) -> dict:
    """A hook's status with every value checked (the table in the design): codes and kinds from
    their patterns and lists, words and labels only from the adapters' vocabularies, ids, scene
    ids, builds, times and numbers by shape, and the on-screen-only text scrubbed. Anything that
    fails is dropped or blanked."""
    words = vocabulary() if words is None else words
    raw = raw if isinstance(raw, dict) else {}
    sites = []
    for site in raw.get("sites") if isinstance(raw.get("sites"), list) else []:
        if not isinstance(site, dict):
            continue
        order, scenes = _number(site.get("order")), _number(site.get("scenes"))
        verdict = site.get("last_verdict") if isinstance(site.get("last_verdict"), dict) else {}
        measure = site.get("measure") if isinstance(site.get("measure"), dict) else {}
        refused = site.get("refused") if isinstance(site.get("refused"), dict) else None
        inputs = []
        for entry in site.get("inputs") if isinstance(site.get("inputs"), list) else []:
            if not isinstance(entry, dict) or _said(entry.get("label"), words) is None:
                continue
            inputs.append(
                {
                    "label": entry["label"],
                    "fetched_at": _iso(entry.get("fetched_at")),
                    "stale": entry.get("stale") if isinstance(entry.get("stale"), bool) else None,
                    "error": _error(entry.get("error"), words, kind="source"),
                }
            )
        sites.append(
            {
                "site_id": _site_id(site.get("site_id")),
                "name": redact.scrub(site.get("name"), NAME_CELL_CHARS) if site.get("name") else "",
                "order": int(order) if order is not None and order >= 1 else None,
                "scenes": int(scenes) if scenes is not None and scenes >= 0 else 0,
                "last_scene": _scene(site.get("last_scene")),
                "last_verdict": (
                    {"code": _code(verdict.get("code")), "words": verdict["words"]}
                    if _said(verdict.get("words"), words)
                    else None
                ),
                "measure": (
                    {"label": measure["label"], "value": _number(measure.get("value"))}
                    if _said(measure.get("label"), words) and _number(measure.get("value")) is not None
                    else None
                ),
                "last_attempt_at": _iso(site.get("last_attempt_at")),
                "error": _error(site.get("error"), words),
                "refused": (
                    {
                        "scene_id": refused.get("scene_id")
                        if isinstance(refused.get("scene_id"), str) and _SCENE_ID.match(refused["scene_id"])
                        else None,
                        "code": _code(refused.get("code")),
                        "words": _said(refused.get("words"), words) or OTHER_WORDS,
                        "at": _iso(refused.get("at")),
                        "retry_at": _iso(refused.get("retry_at")),
                    }
                    if refused is not None
                    else None
                ),
                "inputs": inputs,
            }
        )
    config_error = raw.get("config_error")
    return {
        "config_error": redact.scrub(config_error, TEXT_CELL_CHARS) if config_error else None,
        "backend": raw.get("backend") if raw.get("backend") in BACKENDS else None,
        "fetched_at": _iso(raw.get("fetched_at")),
        "first_observed_at": _iso(raw.get("first_observed_at")),
        "sites": sites,
    }


# --- classifying each site ---------------------------------------------------------------------------

# Marks the one row a topic gets when its sites list itself doesn't parse (missing, not a list, or
# no usable site id): no site to classify, yet the research tick stops on its config every tick, so
# the config error still needs its row, card and sentence. Not counted as a site.
WHOLE_TOPIC = "whole_topic"


def _whole_topic_row() -> dict:
    return {
        "site_id": None,
        "name": "",
        "order": None,
        "scenes": 0,
        "last_scene": None,
        "last_verdict": None,
        "measure": None,
        "last_attempt_at": None,
        "error": None,
        "refused": None,
        "inputs": [],
        WHOLE_TOPIC: True,
    }


def _starved(status: dict, interval_hours: float, now: datetime) -> bool:
    """Whether the topic's ticks run (its state is fresh) but none of its sites has been worked on
    in STARVED_INTERVALS research intervals: every site that was due was left without time. A
    deferred site is otherwise rotation, not a failure: it goes first next tick (order_sites)."""
    fetched = _parse(status["fetched_at"])
    interval = timedelta(hours=max(float(interval_hours), 1.0))
    if fetched is None or now - fetched > 2 * interval:
        return False  # the ticks are not running: pipeline_health says research is late
    attempts = [_parse(site["last_attempt_at"]) for site in status["sites"]]
    newest = max((at for at in attempts if at is not None), default=None)
    return newest is None or now - newest > STARVED_INTERVALS * interval


def classify(site: dict, status: dict, *, state: str, starved: bool) -> str:
    """One site's class, in the order every reader shares (vision_status, pipeline_health, the
    memory watch): the first that holds."""
    error = site["error"]
    if status["config_error"]:
        return "config_error"
    if status["backend"] == "cool" and error and error["code"] == "no_cool_worker":
        return "config_error"
    if state == "worker_off":
        return "worker_off"
    if state not in ("read", "missing"):
        return "not_checked"
    if site["refused"]:
        return "refused"
    if error:
        if error["kind"] == "site":
            return "site_problem"
        if error["kind"] in ("worker", "other"):
            return "worker_error"
        if error["kind"] == "source":
            return "source_error"
        if error["kind"] == "deferred":
            return "starved" if starved else "deferred"
    if any(entry["error"] and entry["fetched_at"] is None for entry in site["inputs"]):
        return "source_error"
    if site["last_scene"] is None:
        return "no_scene" if site["last_attempt_at"] else "never_tried"
    return "measured"


def survey(found: list[dict], now: datetime, *, on: bool, seconds: float, max_bytes: int, config=None):
    """Every site topic in `found` (site_topics), read (when the worker is on) and classified: one
    entry per topic with its checked status, read state, research interval and each site's class."""
    if config is None:
        config = get_pipeline_config()
    states = {}
    if on:

        def then(topic: dict, state: dict):
            entry = next(each for each in found if each["item"] is topic)
            return entry["adapter"].status(topic, state, now=now)

        states = running_states(
            [entry["item"] for entry in found], seconds=seconds, max_bytes=max_bytes, then=then, now=now
        )
    surveyed = []
    for entry in found:
        item = entry["item"]
        if on:
            hooked, state = states.get(item.get("topic_id"), (None, "unreadable"))
        else:
            hooked, state = None, "worker_off"
        status = checked(hooked if hooked is not None else entry["config_only"])
        if status["config_error"] and not status["sites"]:
            status["sites"] = [_whole_topic_row()]  # its class is config_error (classify's first rule)
        interval = resolve_interval_hours(item, config)
        starved = _starved(status, interval, now)
        classes = [classify(site, status, state=state, starved=starved) for site in status["sites"]]
        surveyed.append(
            {
                "item": item,
                "key": entry["key"],
                "label": spoken_label(item),
                "uses_worker": bool(entry["adapter"].uses_vision_worker),
                "state": state,
                "status": status,
                "classes": classes,
                "interval_hours": interval,
            }
        )
    return surveyed


def counts(entry: dict) -> dict:
    """How many sites the topic has, and how many are in each class. A topic whose sites list
    doesn't parse has no site and one config_error (WHOLE_TOPIC)."""
    found = {"total": sum(1 for site in entry["status"]["sites"] if not site.get(WHOLE_TOPIC))}
    for name in CLASSES:
        found[name] = entry["classes"].count(name)
    return found


# --- the cards ---------------------------------------------------------------------------------------

_CARD_FOR_CLASS = {
    "config_error": "vision_config_error",
    "refused": "vision_site_refused",
    "site_problem": "vision_site_refused",
    "worker_error": "vision_worker_error",
    "source_error": "vision_source_error",
    "starved": "vision_site_starved",
}
_NOTICED = {
    "vision_config_error": "{whose} sites config can't be used, so its sites aren't measured",
    "vision_site_refused": "The vision worker refuses {sites} of {topic} as set up",
    "vision_worker_error": "{topic}: {sites} not measured, the vision worker failed",
    "vision_source_error": "{topic}: {sites} not measured on the last try, a source failed",
    "vision_site_starved": "{whose} sites keep waiting for time and aren't measured",
}


def _class_words(entry: dict, site_index: int, name: str) -> str:
    """A class in fixed words, for a card's reasons and the State cell."""
    site = entry["status"]["sites"][site_index]
    error = site["error"]
    if name == "config_error":
        if entry["status"]["config_error"]:
            return "the sites setting doesn't parse"
        return "asks for a COOL worker that isn't deployed"
    if name in ("refused",):
        return site["refused"]["words"]
    if name in ("site_problem", "worker_error") and error:
        return error["words"]
    if name == "source_error":
        if error:
            return error["words"]
        failed = next((each for each in site["inputs"] if each["error"]), None)
        return failed["error"]["words"] if failed else OTHER_WORDS
    return {
        "worker_off": "the vision worker is off",
        "not_checked": "not checked this time",
        "starved": "waiting for time every tick",
        "deferred": "waiting for time in the tick",
        "no_scene": "no new scene in the lookback",
        "never_tried": "not tried yet",
        "measured": "measured",
    }.get(name, OTHER_WORDS)


def cards(surveyed: list[dict], env: str | None, *, on: bool) -> list[dict]:
    """The attention cards: one vision_worker_off for the environment while site topics need the
    worker and it is off, and one per topic and reason class. Code-derived only: topic labels,
    counts and fixed words; never an `untrusted` block."""
    found = []
    needing = [entry for entry in surveyed if entry["uses_worker"]]
    if not on and needing:
        labels = [entry["label"] for entry in needing]
        sites = sum(counts(entry)["total"] for entry in needing)
        found.append(
            finding(
                "vision_worker_off",
                f"The vision worker is off, so {_some(labels)} can't be measured",
                env if isinstance(env, str) and ID_PATTERN.match(env) else None,
                topics=_some(labels),
                sites=sites,
                reasons="the vision worker is off",
            )
        )
    for entry in surveyed:
        topic_id = entry["item"].get("topic_id")
        card_id = topic_id if isinstance(topic_id, str) and ID_PATTERN.match(topic_id) else None
        by_kind: dict[str, list[int]] = {}
        for index, name in enumerate(entry["classes"]):
            kind = _CARD_FOR_CLASS.get(name)
            if kind:
                by_kind.setdefault(kind, []).append(index)
        for kind, indexes in by_kind.items():
            words = list(dict.fromkeys(_class_words(entry, i, entry["classes"][i]) for i in indexes))
            noticed = _NOTICED[kind].format(
                topic=entry["label"], whose=_whose(entry["label"]), sites=_plural(len(indexes), "site")
            )
            # As counts() counts: the one row of a topic whose sites list doesn't parse (WHOLE_TOPIC)
            # is no site, so the card says 0 where the topic's sites total says 0.
            sites = sum(1 for i in indexes if not entry["status"]["sites"][i].get(WHOLE_TOPIC))
            found.append(
                finding(kind, noticed, card_id, topic=entry["label"], sites=sites, reasons="; ".join(words))
            )
    return found


def _some(labels: list[str]) -> str:
    """Up to LABELS_PER_SENTENCE labels, then "and {k} more"."""
    if len(labels) <= LABELS_PER_SENTENCE:
        return _join(labels)
    shown = labels[:LABELS_PER_SENTENCE]
    return f"{', '.join(shown)} and {len(labels) - len(shown)} more"


# --- what pipeline_health and the memory watch use ----------------------------------------------------


def site_health(
    topics: list[dict],
    now: datetime,
    *,
    budget_seconds: float,
    max_bytes: int = LARGE_STATE_BYTES,
    config=None,
) -> dict:
    """For pipeline_health and the memory's topic watch: per site topic among `topics`, its class
    counts and read state, and the cards (cards above). Code-derived only, nothing from a state's
    text. Nothing is read with the worker off; a topic with no adapter, or one that watches no
    sites, is left out."""
    found = site_topics(topics, now)
    if not found:
        return {"on": None, "topics": {}, "findings": []}
    env = architecture.environment()
    on = worker_on()
    surveyed = survey(found, now, on=on, seconds=budget_seconds, max_bytes=max_bytes, config=config)
    rows = {}
    for entry in surveyed:
        found_counts = counts(entry)
        rows[entry["item"].get("topic_id")] = {
            "label": entry["label"],
            "uses_worker": entry["uses_worker"],
            "state": entry["state"],
            "sites": {
                "total": found_counts["total"],
                "measured": found_counts["measured"],
                "worker_error": found_counts["worker_error"],
                "source_error": found_counts["source_error"],
                "refused": found_counts["refused"],
                "site_problem": found_counts["site_problem"],
                "starved": found_counts["starved"],
                "config_error": found_counts["config_error"],
                "not_checked": found_counts["not_checked"],
                "state": entry["state"],
            },
        }
    return {"on": on, "topics": rows, "findings": cards(surveyed, env, on=on)}


# --- the tool ----------------------------------------------------------------------------------------


def _ago(value: str | None, now: datetime) -> str:
    at = _parse(value)
    return f"{_age(at, now)} ago" if at else ""


def _site_row(entry: dict, index: int, now: datetime, *, screen: bool) -> list:
    site, name = entry["status"]["sites"][index], entry["classes"][index]
    scene = site["last_scene"] or {}
    last_scene = ""
    if scene.get("scene_id"):
        captured = _parse(scene.get("captured_at"))
        when = f", captured {captured.date().isoformat()} ({_age(captured, now)} ago)" if captured else ""
        last_scene = f"{scene['scene_id']}{when}"
    coverage = f"{round(scene['coverage'] * 100)}%" if scene.get("coverage") is not None else ""
    measure = ""
    if site["measure"]:
        measure = f"{site['measure']['label']}: {site['measure']['value']:g}"
    if site["last_verdict"]:
        measure = "; ".join(part for part in (measure, site["last_verdict"]["words"]) if part)
    measured_by = scene.get("backend") or ""
    if measured_by and scene.get("build"):
        measured_by += f" (build {scene['build']})"
    figure = "" if not scene else ("stored" if scene.get("figure_stored") else "not stored")
    state = _class_words(entry, index, name)
    if name == "refused" and site["refused"]["retry_at"]:
        state = f"refused, retried in about {_hours_until(site['refused']['retry_at'], now)} hours"
    elif name in ("worker_error", "source_error", "site_problem"):
        state = f"not measured: {state}"
    map_data = ""
    for each in site["inputs"]:
        if each["error"] and each["fetched_at"]:
            map_data = f"refresh failed ({each['error']['words']}), cached copy used"
        elif each["error"]:
            map_data = f"no copy yet ({each['error']['words']})"
        elif each["fetched_at"]:
            map_data = f"fetched {_ago(each['fetched_at'], now)}" + (" (stale)" if each["stale"] else "")
    row = [
        entry["label"],
        site["site_id"] or "-",
        site["name"],
        site["order"] if site["order"] is not None else "",
        last_scene,
        coverage,
        measure,
        scene.get("triage") or "",
        measured_by,
        figure,
        _ago(site["last_attempt_at"], now),
        state,
        map_data,
        (site["error"] or {}).get("detail") or entry["status"]["config_error"] or "",
    ]
    if not screen:
        return [cell for cell, column in zip(row, COLUMNS_SCREEN, strict=True) if column in COLUMNS_VOICE]
    return row


def _hours_until(value: str | None, now: datetime) -> int:
    at = _parse(value)
    if at is None:
        return 1
    return max(math.ceil((at - now).total_seconds() / 3600), 1)


def _site_result(entry: dict, index: int) -> dict:
    site = entry["status"]["sites"][index]
    scene = site["last_scene"] or {}
    first_input = site["inputs"][0] if site["inputs"] else None
    return {
        "topic_id": entry["item"].get("topic_id"),
        "site_id": site["site_id"],
        "order": site["order"],
        "class": entry["classes"][index],
        "scenes": site["scenes"],
        "last_scene_id": scene.get("scene_id"),
        "last_captured_at": scene.get("captured_at"),
        "coverage": scene.get("coverage"),
        "triage": scene.get("triage"),
        "last_verdict": (site["last_verdict"] or {}).get("code"),
        "measured_by": scene.get("backend"),
        "build": scene.get("build"),
        "figure_stored": scene.get("figure_stored") if scene else None,
        "measure_label": (site["measure"] or {}).get("label"),
        "measure_value": (site["measure"] or {}).get("value"),
        "last_attempt_at": site["last_attempt_at"],
        "error_code": (site["error"] or {}).get("code"),
        "error_at": (site["error"] or {}).get("at"),
        "refused_code": (site["refused"] or {}).get("code"),
        "retry_at": (site["refused"] or {}).get("retry_at"),
        "input_label": first_input["label"] if first_input else None,
        "input_fetched_at": first_input["fetched_at"] if first_input else None,
        "input_stale": first_input["stale"] if first_input else None,
        "input_error_code": (first_input["error"] or {}).get("code") if first_input else None,
    }


def _topic_result(entry: dict, config) -> dict:
    item = entry["item"]
    due = next_due_at(item.get("last_research_at"), resolve_interval_hours(item, config))
    return {
        "topic_id": item.get("topic_id"),
        "label": entry["label"],
        "adapter": entry["key"],
        "backend": entry["status"]["backend"],
        "state": entry["state"],
        "fetched_at": entry["status"]["fetched_at"],
        "first_observed_at": entry["status"]["first_observed_at"],
        "next_research_at": due.isoformat() if due else None,
        "sites": counts(entry),
    }


def _spend() -> dict | None:
    """This week's vision triage from the stats row, or None when it has none."""
    try:
        row = get_current_stats()
    except Exception as exc:  # noqa: BLE001 - spend is a bonus, never the answer's failure
        print(f"vision_status: the stats row could not be read: {type(exc).__name__}")
        return None
    calls = _number(_plain(row.get("vision_triage_calls")))
    if not calls:
        return None
    cost = _number(_plain(row.get("vision_triage_cost_aud"))) or 0.0
    unpriced = _number(_plain(row.get("vision_triage_unpriced_calls"))) or 0
    week = row.get("week_start")
    return {
        "week_start": week if isinstance(week, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", week) else None,
        "calls": int(calls),
        "cost_aud": round(float(cost), 2),
        "unpriced_calls": int(unpriced),
    }


def _plain(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _whose(label: str) -> str:
    """A topic's label as a possessive, as it is said: "Anchorages'", "Rail access: Sydney's"."""
    return f"{label}'" if label.endswith("s") else f"{label}'s"


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _problem_sentences(surveyed: list[dict], now: datetime) -> list[tuple[str, str]]:
    """(class, sentence): one per topic and problem, in a fixed order, from fixed templates."""
    sentences = []
    for entry in surveyed:
        label, classes, status = entry["label"], entry["classes"], entry["status"]
        total = len(classes)
        if "config_error" in classes:
            if status["config_error"]:
                said = f"{_whose(label)} sites setting doesn't parse, so its research stops before measuring."
            else:
                said = f"{label} asks for the COOL worker, which isn't deployed, so it can't be measured."
            sentences.append(("config_error", said))
        for name, template in (
            ("worker_error", "{who} measured: {words}."),
            ("source_error", "{who} measured on the last try: {words}; the next tick tries again."),
        ):
            indexes = [i for i, each in enumerate(classes) if each == name]
            if not indexes:
                continue
            words = "; ".join(dict.fromkeys(_class_words(entry, i, name) for i in indexes))
            if len(indexes) == total:
                who = f"{_whose(label)} site{'' if total == 1 else 's'} {'wasn' if total == 1 else 'weren'}'t"
            else:
                who = f"{len(indexes)} of {_whose(label)} sites {'wasn' if len(indexes) == 1 else 'weren'}'t"
            sentences.append((name, template.format(who=who, words=words)))
        refused = [i for i, each in enumerate(classes) if each == "refused"]
        if refused:
            site = status["sites"][refused[0]]["refused"]
            hours = _hours_until(site["retry_at"], now)
            sentences.append(
                (
                    "refused",
                    f"{_whose(label)} scene is refused as the site is set up: {site['words']}. It is tried "
                    f"again in about {hours} hours, or at once if the site is edited or a newer scene comes.",
                )
            )
        problem = [i for i, each in enumerate(classes) if each == "site_problem"]
        if problem:
            words = "; ".join(dict.fromkeys(_class_words(entry, i, "site_problem") for i in problem))
            sentences.append(("site_problem", f"{_whose(label)} site is refused as it is set up: {words}."))
        if "starved" in classes:
            sentences.append(
                (
                    "starved",
                    f"{_whose(label)} sites keep waiting for time in each tick, so they aren't measured: the "
                    "worker is asked only with 45 seconds of the time budget left.",
                )
            )
    return sentences


def _read_sentences(surveyed: list[dict]) -> list[str]:
    sentences = []
    for entry in surveyed:
        if entry["state"] == "unreadable":
            sentences.append(f"I couldn't read {_whose(entry['label'])} running state.")
        elif entry["state"] == "too_large":
            sentences.append(f"{_whose(entry['label'])} running state is too large for me to read.")
        elif entry["state"] == "missing":
            sentences.append(f"{entry['label']} has no running state yet.")
    skipped = sum(1 for entry in surveyed if entry["state"] == "not_checked")
    if skipped:
        were = "wasn't" if skipped == 1 else "weren't"
        sentences.append(f"{_plural(skipped, 'topic')} {were} checked this time.")
    return sentences


def _fit(opening: list[str], problems: list[str], closing: list[str], prefix: str = "") -> str:
    """The spoken answer: the opening, at most PROBLEM_SENTENCES_MAX problem sentences, and the
    closing, under SPOKEN_MAX_WORDS in all; problems that do not fit are "more on screen"."""
    kept = problems[:PROBLEM_SENTENCES_MAX]
    while True:
        more = ["There's more on screen."] if len(kept) < len(problems) else []
        text = " ".join([*opening, *kept, *more, *closing])
        if len((prefix + text).split()) < SPOKEN_MAX_WORDS or not kept:
            return prefix + text
        kept = kept[:-1]


def _spoken(
    surveyed: list[dict], env: str, the_worker: dict, spend: dict | None, now: datetime, *, one: str | None
) -> str:
    """The spoken answer, from fixed templates (the module docstring says what is never in it).
    `one` is the prefix for an answer about one topic (which topic was taken), None for every one."""
    on = the_worker["on"]
    if not surveyed:
        where = f"on, in {the_worker['region']}" if on else "off"
        return f"No topic watches satellite imagery in {env}. The vision worker is {where}."
    labels = [entry["label"] for entry in surveyed]
    closing = []
    if spend:
        money = f"${spend['cost_aud']:,.2f}"
        unpriced = f", {spend['unpriced_calls']} of them unpriced" if spend["unpriced_calls"] else ""
        closing.append(f"Vision triage this week: {_plural(spend['calls'], 'check')}, {money}{unpriced}.")
    if not on:
        n = len(surveyed)
        opening = [
            f"The vision worker is off in {env}, so its {_plural(n, 'vision topic')} can't be measured: "
            f"{_some(labels)}. Turning it on is a deployment setting, VISION_ENABLED. "
            f"{'Its' if n == 1 else 'Their'} sites are on screen."
        ]
        config = [said for name, said in _problem_sentences(surveyed, now) if name == "config_error"]
        return _fit(opening, config, closing, one or "")
    sites = sum(counts(entry)["total"] for entry in surveyed)
    # Measured at least once: a site whose last try failed still has its newest scene (the problem
    # sentences say why the last try didn't measure it).
    measured = sum(1 for entry in surveyed for site in entry["status"]["sites"] if site["last_scene"])
    captured = [
        _parse((site["last_scene"] or {}).get("captured_at"))
        for entry in surveyed
        for site in entry["status"]["sites"]
    ]
    newest = max((at for at in captured if at is not None), default=None)
    topics = len(surveyed)
    watch = "watches" if topics == 1 else "watch"
    tally = f"{measured} measured" if measured else "none measured yet"
    if measured and newest:
        tally += f", the newest scene {_age(newest, now)} ago"
    opening = [
        f"The vision worker is on, in {the_worker['region']}. {_plural(topics, 'vision topic')} {watch} "
        f"{_plural(sites, 'site')}: {tally}."
    ]
    problems = [said for _name, said in _problem_sentences(surveyed, now)] + _read_sentences(surveyed)
    no_scene = [entry for entry in surveyed if "no_scene" in entry["classes"]]
    for entry in no_scene:
        k = entry["classes"].count("no_scene")
        problems.append(f"{entry['label']} found no new scene for {_plural(k, 'site')} in its lookback.")
    if one is not None and len(surveyed) == 1:
        entry = surveyed[0]
        verdict = _newest_verdict(entry["status"]["sites"])
        if verdict:
            closing.insert(0, f"Its newest scene: {verdict}.")
        due = next_due_at(entry["item"].get("last_research_at"), entry["interval_hours"])
        if due is not None:
            when = f"in {_age(now, due)}" if due > now else "now"
            closing.insert(
                1 if verdict else 0,
                f"Its next research is due {when}; the sites take turns, the longest waiting first.",
            )
    closing.append("The sites are on screen.")
    return _fit(opening, problems, closing, one or "")


def _newest_verdict(sites: list[dict]) -> str | None:
    """The verdict words of the topic's newest scene: the site whose last scene was captured last.
    None when that site has no verdict, or no scene has a capture time."""
    dated = [(at, site) for site in sites if (at := _parse((site["last_scene"] or {}).get("captured_at")))]
    if not dated:
        return None
    newest = max(dated, key=lambda pair: pair[0])[1]
    return newest["last_verdict"]["words"] if newest["last_verdict"] else None


def _find_site(asked: str, found: list[dict]) -> list[dict]:
    """The site topics with a site the operator's words name: by its id (the words slugged) or its
    name (case-folded). Site ids are unique only within a topic, so several topics can match."""
    from ops_mcp.cli_guide import _slug_from  # cli_guide imports nothing of this module

    slug = _slug_from(asked)
    folded = " ".join(asked.casefold().split())
    matched = []
    for entry in found:
        for site in entry["config_only"].get("sites") or []:
            site_id = site.get("site_id") if isinstance(site, dict) else None
            name = site.get("name") if isinstance(site, dict) else None
            by_id = isinstance(site_id, str) and slug is not None and site_id.lower() == slug
            by_name = isinstance(name, str) and " ".join(name.casefold().split()) == folded
            if by_id or by_name:
                matched.append(entry)
                break
    return matched


def _refusal(spoken: str, matched: dict | None) -> dict:
    return {"spoken": spoken, "findings": [], "matched_topic": matched}


def vision_status(topic: str | None = None, *, screen: bool, now: datetime | None = None) -> dict:
    """Whether the vision worker is on and where, and for each topic that watches satellite imagery
    how its sites are doing; with `topic`, that one (or the topic of a site it names). `screen` is
    whether the caller reads a screen (memory.screen_caller): a voice caller's table has no
    "(unverified)" column."""
    now = now or datetime.now(UTC)
    env = architecture.environment()
    if env is None:
        return {"spoken": "I haven't been told which environment I'm for.", "findings": []}
    found = site_topics(list_topics(), now)
    matched, prefix = None, None
    if topic is not None:
        one, matched, refusal = topic_match.pick_spoken(topic)
        if one is None and matched.get("how") == "none" and isinstance(topic, str):
            sites = _find_site(topic.strip()[: topic_match.ASKED_MAX_CHARS], found)
            if len(sites) == 1:
                one = sites[0]["item"]
                matched = {
                    "how": "site",
                    "topic_id": one.get("topic_id"),
                    "name": spoken_label(one),
                    "did_you_mean": [],
                }
                prefix = f"I took that as a site of {spoken_label(one)}. "
            elif len(sites) > 1:
                labels = [spoken_label(entry["item"]) for entry in sites]
                matched = {**matched, "how": "ambiguous_site"}
                return _refusal(
                    f"Sites by that name are in {topic_match._either(labels)}. Say which topic, and "
                    "I'll look.",
                    matched,
                )
        if one is None:
            return _refusal(refusal, matched)
        found = [entry for entry in found if entry["item"].get("topic_id") == one.get("topic_id")]
        if not found:
            return _refusal(
                "That topic doesn't watch sites: this covers the topics that measure satellite imagery.",
                matched,
            )
        if prefix is None:
            prefix = topic_match.took(matched)
    the_worker = worker(env)
    config = get_pipeline_config()
    surveyed = survey(
        found, now, on=the_worker["on"], seconds=READ_SECONDS, max_bytes=MAX_STATE_BYTES, config=config
    )
    spend = _spend() if surveyed else None
    columns = COLUMNS_SCREEN if screen else COLUMNS_VOICE
    rows = [
        _site_row(entry, index, now, screen=screen)
        for entry in surveyed
        for index in range(len(entry["classes"]))
    ]
    result = {
        "spoken": _spoken(surveyed, env, the_worker, spend, now, one=prefix if topic is not None else None),
        "findings": cards(surveyed, env, on=the_worker["on"]),
        "environment": env,
        "worker": the_worker,
        "topics": [_topic_result(entry, config) for entry in surveyed],
        "sites": [_site_result(entry, i) for entry in surveyed for i in range(len(entry["classes"]))],
        "spend": spend,
        "table": {"title": f"Vision sites in {env}", "columns": list(columns), "rows": rows},
        "as_of": now.isoformat(),
    }
    if topic is not None:
        result["matched_topic"] = matched
    return result
