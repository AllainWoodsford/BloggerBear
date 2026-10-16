"""The evaluation's driver: the real rail access adapter, tick by tick, over a pinned sequence of
scenes, with every outside service it talks to replaced by something the run controls.

The research tick makes one `RailAccessAdapter` per tick, calls `fetch_state` then
`material_diff`, and stores the state. This does the same, with the adapter's own class-attribute
seams (common/adapters/rail_access.py: `http_post`, `measure`, `fetch_network`, `store_image`,
`load_image`, `store_network`, `load_network`, `triage_agent`, `choose_model`, `web_search`,
`clock`) set on the instance, so the verdict, the gate, the agent's brief, the decision ring and
the evidence lines are production's own:

- `http_post`: a scripted STAC search that answers each tick with that tick's scene (its item is
  built from the manifest: id, capture time, cloud cover, footprint box, the four band URLs);
- `store_network` / `load_network` / `fetch_network`: a local directory holds each site's network
  object, as the content bucket's `vision/<topic>/<site>/network.json` does since the day-subject
  round; the driver writes the tick's network fixture into it (with the polygon's `site_key`)
  before the tick, and an Overpass refresh answers the same fixture;
- `store_image` / `load_image`: the same directory, for the figures;
- `measure`: one of `in_process_measure` (the worker handler in this process, its reads through
  any `fetch`: the cache, a fetch that rewrites SCL, the synthetic city), `deployed_measure` (the
  deployed worker by its ARN, through common/vision_client.py, as the research tick calls it;
  every site id the driver sends starts `eval-`) or `scripted_measure` (fixed metrics);
- `triage_agent`: the real common/vision_triage.triage, with its `converse` from one of
  `BedrockAgent` (real Bedrock, every response recorded), `RecordedAgent` (a run file's recorded
  responses, refused when one no longer fits) or `ScriptedAgent` (fixed answers, no model);
- `choose_model`: the agent's model id; `web_search`: the tick's scripted news (never the live
  search: a replay must not depend on what the news holds that day);
- `clock`: `Clock` (the real monotonic clock, every reading kept), `Clock.frozen()` (always 0:
  budgets and deadlines never bite) or `Clock.replay(readings)` (a recorded tick's readings,
  in order, so a budget or deadline decision replays exactly).

The tick's wall-clock time (`datetime.now(UTC)` in `fetch_state`, which becomes the decision's
`at`) is the tick's `now`: by default the scene's capture time plus TICK_LAG, so the trace reads in
the scenes' own dates; the recorded `at`s in a recorded replay. The adapter reads the wall clock
nowhere else; `frozen_now` sets it by replacing the module's `datetime` name for the tick's length.

**Q2b builds on this** (`agent`, `counterfactual`, `stability`):

    driver = Driver(topic_config(...), measure=..., agent=..., clock=Clock.frozen)
    results = driver.run(specs)                      # [TickResult], in order; driver.state is the last state
    before = driver.state                            # e.g. after a case's history ticks
    again = driver.tick(before, spec, n=len(specs))  # the newest tick again from the same state (repeats)

- `TickSpec(scene, now, network=None, label=None, news=None)`: `network` is a dict, None (keep
  what the store holds) or a callable `state -> network` (sydney-c1's line laid on the previous
  tick's largest desert, `with_new_line`).
- `Driver(..., look_again_measure=fn)` answers `look_again` with another measure than the tick's
  own (2.6.6 (a)'s arms); `in_process_measure(fetch=...)` takes any fetch, so a hook that rewrites
  SCL pixels to cloud before the analysis (2.6.6 (b)) runs through the real handler; `topic_config(
  ..., overrides={"triage": False, "history_size": 6})` is E3's production rule with triage off.
- A `TickResult` (a dict) holds the tick's evidence lines as printed, the ring entries it appended,
  every worker call (the tick's own and each `look_again`, with the metrics record and its digest,
  vision_benchmark.metrics_record), the agent's recorded responses, the clock's readings and
  `changed` (the diff's answer: a Finding would be written). `driver.state` is the running state.
- `compare_recorded` holds a replay to a recording (2.6.4's parity: the ring's code-derived fields
  and each re-measurement within the tolerances of 2.7.1, the excluded fields listed in EXCLUDED);
  `render_markdown` is the trace through vision_decisions.trace and the assistant's own table
  (ops_mcp/vision_evidence.trace_rows), so the replay, the assistant and the export show one table.

The synthetic city (`synthetic_sequence`, `synthetic_fetch`) is vision_benchmark.synthetic_city
written as tiled GeoTIFFs in memory, under a host that cannot resolve, so CI runs the real worker
handler, reads included, on it with no network.

No Lambda code is changed by any of this. Runs on Windows, macOS and Linux.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import vision_runs

ROOT = Path(__file__).resolve().parents[1]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

from common import evidence, osm, vision_client, vision_decisions, vision_sites, vision_triage  # noqa: E402
from common.adapters import rail_access  # noqa: E402

# Every site id the driver gives the adapter, and so every id the worker is sent, starts with this
# (vision_sites.SITE_ID allows it): the worker's lines carry no topic id, so this is how an
# evaluation run is told apart from production's in the worker's logs (2.3, 1.18.2).
SITE_PREFIX = "eval-"
# A replay tick happens this long after its scene was captured: the deployed topic ticks hourly and
# Earth Search lists a scene a few hours after capture. The decision's `at` is then in the scene's
# own week, so a trace reads in the scenes' dates; the run file records when the run really ran.
TICK_LAG = timedelta(hours=6)
# The thresholds a sequence runs under: the adapter's defaults, or the demo topic's zeros (every
# scene after the first goes to the agent; disclosed wherever a demo trace is shown).
THRESHOLDS = {
    "default": {},
    "demo": {"served_share_threshold": 0.0, "desert_relative_threshold": 0.0, "min_baseline": 1},
}
# The scripted agent's moves: one `look_again` at this reach (the settings the trace's example
# names), then the news (`web_context`, when the topic offers it), then an answer by SCRIPTED_RULE.
# Its model id is this code (the `model` kind takes it).
SCRIPTED_MODEL = "scripted"
SCRIPTED_LOOK_AGAIN = {"reach_m": 800}
SCRIPTED_TOOLS = ("look_again", "web_context")
SCRIPTED_RULE = "real when the station count changed (the network moved), else artefact"
# Where the in-process worker is "deployed": vision_client.measure needs an ARN in the backend's
# variable before it calls the client it is given; this one names no account and is never invoked.
IN_PROCESS_ARN = "arn:aws:lambda:us-west-2:000000000000:function:in-process"
WORKER_ENV = {"opencv": "VISION_WORKER_ARN", "cool": "VISION_COOL_WORKER_ARN"}
# The ring's fields a recorded replay does not hold equal (2.6.4): the ids of a run, the machine's
# timings and the price (the price table may change), and who measured (the replay recomputes on
# another machine by design: the build and CPU are the judge's). Everything else is code-derived
# and compared: exactly, or within PARITY for the measurements.
EXCLUDED = (
    "tick", "run", "measured_by", "triage.elapsed_ms", "triage.deadline_left_s", "triage.cost_usd",
    "triage.steps[].ms", "triage.steps[].result.worker_request_id", "triage.steps[].result.worker_ms",
    "triage.steps[].result.build",
)  # fmt: skip
# The tolerances of a re-measurement (2.7.1, vision_benchmark's PARITY_ABS and PARITY_REL): a share
# to 1e-4 absolute, an area to 1e-3 relative, every count identical. A difference of two such values
# (the ring's `delta`) may differ by both errors; `relative` is stored rounded to 3 places.
PARITY_ABS = {"served_share": 1e-4, "coverage": 1e-4}
PARITY_REL = {"desert_km2": 1e-3, "built_up_km2": 1e-3}
PARITY_SLACK = 1e-9
RELATIVE_ABS = 3e-3

# --- the synthetic city -------------------------------------------------------------------------------

# The synthetic city CI replays: vision_benchmark.synthetic_city at 20 m, 5.12 km across, its
# network vision_benchmark.synthetic_network with two lines each way and three stations a line,
# which leaves the middle of the built district beyond reach (a desert the C1 line is laid on).
SYNTHETIC_SIZE = 256
SYNTHETIC_LINES, SYNTHETIC_STATIONS_PER_LINE = 2, 3
# Synthetic band URLs are under a host that never resolves (.invalid, RFC 2606), allowed for the
# run's length only (VISION_ALLOWED_URL_PREFIXES), and answered from memory.
SYNTHETIC_PREFIX = "https://synthetic-city.invalid/"
SYNTHETIC_FILES = {"red": "B04", "nir": "B08", "swir16": "B11", "scl": "SCL"}
# The site is the city less this many pixels round its edge: the worker reads its window with a
# 16 px pad (vision/scene.PAD_PX), which then stays inside the image.
SYNTHETIC_MARGIN_PX = 16
# The synthetic sequence: a clear scene (the first observation), one under a cloud over the west
# of the city (below the 0.6 coverage floor), a clear one (with the demo thresholds, sent to the
# agent), and one measured against network B (a new line through the previous scene's desert).
SYNTHETIC_SCENES = (
    {"name": "synthetic-1", "seed": 0, "cloud": 0.0, "captured_at": "2024-06-27T00:06:11Z",
     "network": "A"},
    {"name": "synthetic-2-cloud", "seed": 1, "cloud": 0.7, "captured_at": "2024-07-07T00:06:11Z",
     "network": "A"},
    {"name": "synthetic-3", "seed": 2, "cloud": 0.0, "captured_at": "2024-07-17T00:06:11Z",
     "network": "A"},
    {"name": "synthetic-4", "seed": 3, "cloud": 0.0, "captured_at": "2024-07-27T00:06:11Z",
     "network": "B"},
)  # fmt: skip
SYNTHETIC_NETWORK_FILE = ROOT / "scripts" / "tests" / "fixtures" / "synthetic-city-network.json"
# The tile of the GeoTIFFs the synthetic city is written as.
GEOTIFF_TILE = 64

# --- the sequences ----------------------------------------------------------------------------------

# A replay's sequences: which manifest site, which scenes in order, the thresholds, the network
# each tick is measured against (A: the fixture; B: A with a new line, `with_new_line`), and the
# news `web_context` answers with. sydney-c1's news is constructed, like its line, and says so.
C1_NEWS = (
    {
        "title": "Evaluation case C1 (constructed): a new rail line with five stations opened",
        "url": "https://example.org/bloggerbear-evaluation/c1",
        "published_at": "2024-08-30T00:00:00Z",
        "source": "BloggerBear evaluation (constructed)",
        "snippet": "A constructed news item for evaluation case C1: five new stations on one new line.",
    },
)
WINTER = ("sydney-2024-06-27", "sydney-2024-08-21b", "sydney-2024-08-26", "sydney-2024-08-31")
SEQUENCES = {
    "sydney-winter": {"site": "sydney", "scenes": WINTER, "thresholds": "default", "networks": "AAAA",
                      "news": ()},
    "sydney-demo": {"site": "sydney", "scenes": WINTER, "thresholds": "demo", "networks": "AAAA", "news": ()},
    "sydney-c1": {"site": "sydney", "scenes": WINTER, "thresholds": "default", "networks": "AAAB",
                  "news": C1_NEWS},
    "synthetic-city": {"site": "synthetic", "scenes": tuple(s["name"] for s in SYNTHETIC_SCENES),
                       "thresholds": "demo", "networks": "".join(s["network"] for s in SYNTHETIC_SCENES),
                       "news": ()},
}  # fmt: skip


class ReplayRefused(Exception):
    """A recorded replay that cannot go on: a recorded response that no longer fits (a tool the
    profile no longer offers, a turn the recording does not have), named by its tick and step."""


# --- clocks -----------------------------------------------------------------------------------------


class Clock:
    """The adapter's monotonic clock for one tick, every reading kept relative to the first (so a
    run file holds small numbers, and `replay` hands back the same ones in the same order)."""

    def __init__(self, base: Callable[[], float] = time.monotonic, replay: list[float] | None = None):
        self.base, self.replay_from = base, (list(replay) if replay is not None else None)
        self.readings: list[float] = []
        self.overrun = False
        self._origin = None

    @classmethod
    def frozen(cls) -> Clock:
        return cls(base=lambda: 0.0)

    @classmethod
    def replay(cls, readings: list[float]) -> Clock:
        return cls(replay=readings)

    def __call__(self) -> float:
        if self.replay_from is not None:
            index = len(self.readings)
            if index < len(self.replay_from):
                value = float(self.replay_from[index])
            else:  # the replay read the clock more often than the recording: the paths differ
                self.overrun = True
                value = float(self.replay_from[-1]) if self.replay_from else 0.0
            self.readings.append(value)
            return value
        now = self.base()
        if self._origin is None:
            self._origin = now
        value = round(now - self._origin, 6)
        self.readings.append(value)
        return value


@contextlib.contextmanager
def frozen_now(when: datetime):
    """The adapter's `datetime.now()` answers `when` for the block (rail_access reads the wall clock
    once a tick, in fetch_state, and nowhere else on the replay's path)."""

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)

    original = rail_access.datetime
    rail_access.datetime = _Now
    try:
        yield
    finally:
        rail_access.datetime = original


@contextlib.contextmanager
def environment(values: dict[str, str | None]):
    """`os.environ` with `values` set (None: removed) for the block, restored after."""
    saved = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def tick_time(captured_at: str) -> datetime:
    """When a replay tick happens: the scene's capture time plus TICK_LAG, to the second, in UTC."""
    when = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    when = when if when.tzinfo else when.replace(tzinfo=UTC)
    return (when + TICK_LAG).astimezone(UTC).replace(microsecond=0)


# --- the stores, the STAC search, the news --------------------------------------------------------------


class LocalStore:
    """The content bucket's two kinds of object the adapter writes and reads (figures, and each
    site's network object), in a local directory under the same keys."""

    def __init__(self, directory: Path | None = None):
        self._tmp = None
        if directory is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="bloggerbear-replay-")
            directory = Path(self._tmp.name)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if ".." in key.split("/"):
            raise ValueError(f"not a store key: {key!r}")
        return self.directory.joinpath(*key.split("/"))

    def store_image(self, topic_id: str, site_id: str, scene_id: str, png: bytes) -> str:
        key = f"vision/{topic_id}/{site_id}/{scene_id}.png"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        return key

    def load_image(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def store_network(self, topic_id: str, site_id: str, network: dict) -> str:
        key = vision_sites.network_key(topic_id, site_id)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(network, ensure_ascii=False), encoding="utf-8")
        return key

    def load_network(self, topic_id: str, site_id: str) -> dict | None:
        path = self._path(vision_sites.network_key(topic_id, site_id))
        if not path.is_file():
            return None
        network = json.loads(path.read_text(encoding="utf-8"))
        return network if isinstance(network, dict) else None

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


class ScriptedStac:
    """Earth Search, answering each search with the tick's one item (`item`), and keeping the
    request bodies the adapter sent."""

    def __init__(self):
        self.item: dict | None = None
        self.bodies: list[dict] = []

    def __call__(self, url, json=None, timeout=None):  # noqa: A002 - requests.post's own keyword
        self.bodies.append(json)
        features = [self.item] if self.item else []

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"features": copy.deepcopy(features)}

        return Response()


def stac_item(scene: dict) -> dict:
    """A manifest scene as the Earth Search item the adapter's search reads: its id, footprint box,
    capture time, cloud cover and the four rail bands' URLs under Earth Search's asset keys."""
    item = scene.get("item") or {}
    return {
        "id": scene["scene_id"],
        "bbox": list(item.get("bbox") or []),
        "properties": {"datetime": scene["captured_at"], "eo:cloud_cover": item.get("cloud_cover")},
        "assets": {
            key: {"href": scene["assets"][band]["url"]} for band, key in rail_access.ASSETS_RAIL.items()
        },
    }


def scripted_search(news) -> Callable:
    """The news search `web_context` asks, answering the tick's `news` (a list of {title, url,
    published_at, source, snippet}; an empty list is "nothing in the news")."""
    rows = [dict(row) for row in news or []]

    def search(query, max_results=5, max_age_hours=None, deadline=None):
        return copy.deepcopy(rows[:max_results])

    return search


# --- networks -------------------------------------------------------------------------------------------


def load_network_file(path: Path) -> dict:
    """A network fixture file (as vision_benchmark.load_network reads one: a byte-order mark is
    skipped); FileNotFoundError with the benchmark's own words when it is not there."""
    from vision_benchmark import NETWORK_FILE_MISSING

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(NETWORK_FILE_MISSING)
    network = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(network, dict):
        raise ValueError(f"{path}: not a network")
    return network


def for_site(network: dict, polygon: list) -> dict:
    """The fixture as the site's network object holds it: with the replay polygon's `site_key`, so
    the adapter measures with it (a fixture captured for the same polygon carries the same key)."""
    out = copy.deepcopy(network)
    out["site_key"] = vision_sites.site_key(polygon)
    return out


def with_new_line(network: dict, lon: float, lat: float, *, stations: int = 5, spacing_m: float = 600.0,
                  prefix: str = "eval-line") -> dict:  # fmt: skip
    """`network` with one new straight line west to east through (lon, lat): `stations` stations
    `spacing_m` apart on one way (C1's "fixture B = A plus a synthetic line"), fetched a day after
    the original, so the decision ring says the network copy changed. Constructed, and named so."""
    out = copy.deepcopy(network)
    step = spacing_m / (111320.0 * math.cos(math.radians(lat)))
    middle = (stations - 1) / 2.0
    points = [[round(lon + (k - middle) * step, 6), round(lat, 6)] for k in range(stations)]
    out["ways"] = [*(out.get("ways") or []),
                   {"id": f"{prefix}-way", "points": points, "tunnel": False, "name": "Evaluation line"}]
    out["stations"] = [*(out.get("stations") or []), *(
        {"id": f"{prefix}-{k + 1}", "name": f"Evaluation station {k + 1}", "kind": "station",
         "lon": point[0], "lat": point[1]}
        for k, point in enumerate(points)
    )]  # fmt: skip
    fetched = network.get("fetched_at")
    try:
        when = datetime.fromisoformat(str(fetched).replace("Z", "+00:00")) + timedelta(days=1)
        out["fetched_at"] = when.isoformat()
    except ValueError:
        out["fetched_at"] = "2026-01-02T00:00:00+00:00"
    if isinstance(out.get("counts"), dict):
        counts = dict(out["counts"])
        counts["ways"] = len(out["ways"])
        counts["stations"] = len(out["stations"])
        counts["points"] = sum(len(way.get("points") or []) for way in out["ways"])
        out["counts"] = counts
    return out


def largest_desert(state: dict | None, site_id: str) -> tuple[float, float] | None:
    """(lon, lat) of the largest area beyond reach in the site's newest measured scene, or None."""
    history = ((((state or {}).get("sites") or {}).get(site_id) or {}).get("history")) or []
    deserts = [d for d in (history[-1].get("deserts") or [] if history else []) if isinstance(d, dict)]
    deserts = [d for d in deserts if all(isinstance(d.get(k), int | float) for k in ("lon", "lat"))]
    if not deserts:
        return None
    best = max(deserts, key=lambda d: d.get("area_km2") or 0)
    return float(best["lon"]), float(best["lat"])


def line_on_largest_desert(network: dict, site_id: str) -> Callable[[dict | None], dict]:
    """Network B for a tick: `network` with a new line through the largest desert of the previous
    tick's scene (worked out when the tick starts, from the state the tick is given)."""

    def build(state: dict | None) -> dict:
        found = largest_desert(state, site_id)
        if found is None:
            raise ReplayRefused("no area beyond reach in the previous scene to lay the new line on: "
                                "pass the second network with --network-b")  # fmt: skip
        return with_new_line(network, *found)

    return build


# --- measures -------------------------------------------------------------------------------------------


class _InProcessLambda:
    """A Lambda client whose `invoke` runs the worker handler in this process, its reads through
    `fetch` (the handler's own HTTPS range reads when None)."""

    def __init__(self, fetch=None):
        self.fetch = fetch

    def invoke(self, FunctionName=None, InvocationType=None, Payload=b""):  # noqa: N803 - boto3's names
        import vision_worker_handler

        request = json.loads(Payload)
        extra = {"fetch": self.fetch} if self.fetch else {}
        reply = vision_worker_handler.lambda_handler(request, None, **extra)
        return {"Payload": io.BytesIO(json.dumps(reply).encode("utf-8")), "ResponseMetadata": {}}


def in_process_measure(fetch=None, environ: dict | None = None) -> Callable:
    """`measure` as the adapter calls it (common/vision_client.measure's signature), answered by
    the worker handler in this process through the real client's checks (the request validated
    before, the reply after). `environ` is set for each call (VISION_BACKEND and COOL_BUILD_SHA256
    for a COOL run, VISION_ALLOWED_URL_PREFIXES for the synthetic city)."""
    client = _InProcessLambda(fetch)

    def measure(site, scene, backend="opencv", **kwargs):
        kwargs.pop("client", None)
        values = {WORKER_ENV[backend]: IN_PROCESS_ARN, **(environ or {})}
        with environment(values):
            return vision_client.measure(site, scene, backend=backend, client=client, **kwargs)

    return measure


def deployed_measure(arn: str) -> Callable:
    """`measure` answered by the deployed worker `arn`, exactly as the research tick calls it
    (common/vision_client.measure, its read timeouts and its checks)."""

    def measure(site, scene, backend="opencv", **kwargs):
        with environment({WORKER_ENV[backend]: arn}):
            return vision_client.measure(site, scene, backend=backend, **kwargs)

    return measure


def scripted_measure(answer: Callable[[dict, dict, dict], dict]) -> Callable:
    """`measure` answered by `answer(site, scene, params)`: the metrics of a rail reply (the
    contract's reply is built round them), or a vision_client.VisionError raised."""

    def measure(site, scene, backend="opencv", params=None, coverage_floor=0.6, image=True,
                task="rail_access", network=None, read_timeout=None, client=None):  # fmt: skip
        metrics = answer(site, scene, dict(params or {}))
        reply = {"version": 1, "ok": True, "task": task, "backend": backend, "site_id": site["id"],
                 "scene_id": scene["id"], "captured_at": scene.get("captured_at"), "metrics": metrics,
                 "build": {"build_sha256": None}, "timings_ms": {}}  # fmt: skip
        return vision_client.VisionResult(reply=reply, image_png=None)

    return measure


class MeasureLog:
    """Every worker call of a run, the tick's own and each `look_again`: the scene, the settings,
    the outcome and what was measured (vision_benchmark.metrics_record and its digest), so a replay
    can be held to its recording measurement by measurement."""

    def __init__(self):
        self.calls: list[dict] = []

    def tagged(self, kind: str, measure: Callable) -> Callable:
        from vision_benchmark import metrics_record

        def call(site, scene, *args, **kwargs):
            row = {"kind": kind, "site_id": site.get("id"), "scene_id": scene.get("id"),
                   "params": dict(kwargs.get("params") or {}), "ok": False, "code": None,
                   "request_id": None, "metrics": None, "metrics_digest": None}  # fmt: skip
            self.calls.append(row)
            try:
                result = measure(site, scene, *args, **kwargs)
            except vision_client.VisionError as exc:
                row["code"], row["request_id"] = exc.code, getattr(exc, "request_id", None)
                raise
            record = metrics_record(result.metrics, kwargs.get("task") or "rail_access")
            record["coverage"] = result.metrics.get("coverage")
            row.update(ok=True, request_id=result.request_id, metrics=record,
                       metrics_digest=vision_runs.digest(record))  # fmt: skip
            return result

        return call


# --- agents ---------------------------------------------------------------------------------------------


def _trimmed(response: dict) -> dict:
    """What a run file keeps of a Converse response: the message, why it stopped and the tokens
    (never ResponseMetadata, whose headers carry the service's request ids and times)."""
    return {key: copy.deepcopy(response.get(key)) for key in ("output", "stopReason", "usage")
            if key in response}  # fmt: skip


def _offered(request: dict) -> set[str]:
    tools = ((request.get("toolConfig") or {}).get("tools")) or []
    return {tool.get("toolSpec", {}).get("name") for tool in tools if isinstance(tool, dict)}


class _Recording:
    """A `converse` that keeps every response it hands back."""

    def __init__(self, call: Callable[..., dict]):
        self.call, self.responses, self.refused = call, [], None

    def __call__(self, **request) -> dict:
        response = self.call(**request)
        self.responses.append(_trimmed(response))
        return response

    def finish(self) -> None:
        pass


class BedrockAgent:
    """The real model through Bedrock Converse (`--model-id`, the operator's credentials and model
    access), each response recorded for `--agent recorded`."""

    mode = "bedrock"

    def __init__(self, model_id: str, client=None, region: str | None = None):
        self.model_id = model_id
        if client is None:
            import boto3

            client = boto3.client("bedrock-runtime", **({"region_name": region} if region else {}))
        self.client = client

    def converse_for(self, tick: int, site_id: str) -> _Recording:
        return _Recording(self.client.converse)


class ScriptedAgent:
    """No model: the tools SCRIPTED_TOOLS names, one a turn, each when the request offers it (a
    `look_again` at SCRIPTED_LOOK_AGAIN, then the news), then the answer by SCRIPTED_RULE, in
    Converse's shape, recorded as a real run's responses are."""

    mode = "scripted"

    def __init__(self, look_again: dict | None = None, model_id: str = SCRIPTED_MODEL):
        self.look_again = dict(SCRIPTED_LOOK_AGAIN if look_again is None else look_again)
        self.model_id = model_id

    def converse_for(self, tick: int, site_id: str) -> _Recording:
        state = {"turn": 0, "planned": None}

        def converse(**request):
            state["turn"] += 1
            if state["planned"] is None:
                offered = _offered(request)
                inputs = {"look_again": self.look_again}
                state["planned"] = [(name, inputs.get(name, {})) for name in SCRIPTED_TOOLS
                                    if name in offered]  # fmt: skip
            if state["planned"]:
                name, given = state["planned"].pop(0)
                block = {"toolUse": {"toolUseId": f"scripted-{tick}-{state['turn']}", "name": name,
                                     "input": dict(given)}}  # fmt: skip
                return {"output": {"message": {"role": "assistant", "content": [block]}},
                        "stopReason": "tool_use", "usage": {"inputTokens": 0, "outputTokens": 0}}  # fmt: skip
            verdict, why = scripted_verdict(request.get("messages") or [])
            text = json.dumps({"verdict": verdict, "reason": f"Scripted answer, no model was asked: {why}."})
            return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                    "stopReason": "end_turn", "usage": {"inputTokens": 0, "outputTokens": 0}}  # fmt: skip

        return _Recording(converse)


def scripted_verdict(messages: list[dict]) -> tuple[str, str]:
    """SCRIPTED_RULE, read from the agent's brief (the first message): `real` when the brief's
    change has a station count that moved, else `artefact`."""
    try:
        text = messages[0]["content"][0]["text"]
        brief = json.loads(text.split("\n", 1)[1])
        moved = (brief.get("change") or {}).get("station_count")
    except (IndexError, KeyError, TypeError, ValueError):
        return "artefact", "the brief could not be read"
    if moved:
        return "real", "the station count changed, so the mapped network moved"
    return "artefact", "the station count did not change, so only imagery-derived measures moved"


class _Replaying:
    """A `converse` that hands back a recording's responses in order, and refuses (ReplayRefused,
    through `refused`, since triage() turns any exception into `bedrock_error`) when the next one
    calls a tool the request no longer offers or the recording has no response for the turn."""

    def __init__(self, responses: list[dict], where: str):
        self.responses, self.where = [copy.deepcopy(r) for r in responses], where
        self.turn = self.steps = 0
        self.refused: str | None = None

    def __call__(self, **request) -> dict:
        if self.turn >= len(self.responses):
            self.refused = f"{self.where}, turn {self.turn + 1}: the recording has no response for it"
            raise ReplayRefused(self.refused)
        response = self.responses[self.turn]
        self.turn += 1
        offered = _offered(request)
        for block in ((response.get("output") or {}).get("message") or {}).get("content") or []:
            use = block.get("toolUse") if isinstance(block, dict) else None
            if use is None:
                continue
            self.steps += 1
            if use.get("name") not in offered:
                self.refused = (f"{self.where}, step {self.steps}: the recorded response calls "
                                f"{use.get('name')!r}, a tool the profile no longer offers")  # fmt: skip
                raise ReplayRefused(self.refused)
        return copy.deepcopy(response)

    def finish(self) -> None:
        if self.refused is None and self.turn < len(self.responses):
            self.refused = (f"{self.where}: the replay stopped after turn {self.turn} of the recording's "
                            f"{len(self.responses)}")  # fmt: skip

    @property
    def responses_used(self) -> list[dict]:
        return self.responses[: self.turn]


class RecordedAgent:
    """A run file's recorded responses (`agent.responses`), replayed exactly, tick by tick."""

    mode = "recorded"

    def __init__(self, recorded: list[dict], model_id: str | None):
        self.model_id = model_id or SCRIPTED_MODEL
        self.by_turn = {(row["tick"], row["site_id"]): row["turns"] for row in recorded or []}

    def converse_for(self, tick: int, site_id: str) -> _Replaying:
        return _Replaying(self.by_turn.get((tick, site_id), []), f"tick {tick}, site {site_id}")


# --- the driver -----------------------------------------------------------------------------------------


@dataclass
class TickSpec:
    """One tick's inputs: the scene (manifest-shaped), the tick's wall-clock time, the network the
    site's object holds for the tick (a dict, a callable of the previous state, or None to keep
    what it holds), its label (A, B), and the news `web_context` answers with."""

    scene: dict
    now: datetime
    network: dict | Callable | None = None
    label: str | None = None
    news: list | tuple | None = None
    extra: dict = field(default_factory=dict)


def topic_config(name: str, site_id: str, polygon: list, *, site_name: str | None = None,
                 thresholds: str = "default", params: dict | None = None,
                 overrides: dict | None = None) -> dict:  # fmt: skip
    """The replay's one-site topic (the shape of `trains-sydney`: no rota), its site id prefixed
    SITE_PREFIX, with the sequence's thresholds and any `overrides` of the adapter's config."""
    if not site_id.startswith(SITE_PREFIX):
        site_id = SITE_PREFIX + site_id
    adapter_config = {"sites": [{"id": site_id, "name": site_name or site_id, "polygon": polygon}],
                      **THRESHOLDS[thresholds], **(params or {}), **(overrides or {})}  # fmt: skip
    return {"topic_id": f"eval-{name}", "name": f"Rail access replay: {name}",
            "adapter_config": adapter_config}  # fmt: skip


class Driver:
    """The real RailAccessAdapter, one instance per tick as the research tick makes one, with every
    seam set to what the run controls. `run` drives a sequence; `tick` one tick from a given state
    (it never changes that state: fetch_state deep-copies every site's record)."""

    def __init__(self, topic: dict, *, measure: Callable, agent=None, store: LocalStore | None = None,
                 clock: Callable[[int], Clock] | None = None,
                 look_again_measure: Callable | None = None):  # fmt: skip
        self.topic = topic
        self.config = rail_access.parse_config(topic.get("adapter_config"))
        if len(self.config["sites"]) != 1:
            raise ValueError("the driver runs a one-site topic")
        self.site = self.config["sites"][0]
        self.measure, self.agent, self.look_again_measure = measure, agent, look_again_measure
        self.store = store or LocalStore()
        self.clock_for = clock or (lambda n: Clock())
        self.log = MeasureLog()
        self.stac = ScriptedStac()
        self.state: dict | None = None
        self.results: list[dict] = []
        self._tick_n = 0
        self._network: dict | None = None
        self._conversations: list = []

    @property
    def topic_id(self) -> str:
        return self.topic["topic_id"]

    @property
    def site_id(self) -> str:
        return self.site["id"]

    def run(self, specs: list[TickSpec], state: dict | None = None) -> list[dict]:
        self.state = state
        for n, spec in enumerate(specs, start=1):
            result = self.tick(self.state, spec, n=n)
            self.state = result.pop("state")
            self.results.append(result)
        return self.results

    def _adapter(self, spec: TickSpec, clock: Clock, news) -> rail_access.RailAccessAdapter:
        adapter = rail_access.RailAccessAdapter()
        adapter.http_post = self.stac
        adapter.measure = self.log.tagged("tick", self.measure)
        adapter.fetch_network = self._fetch_network
        adapter.store_image = self.store.store_image
        adapter.load_image = self.store.load_image
        adapter.store_network = self.store.store_network
        adapter.load_network = self.store.load_network
        adapter.triage_agent = self._triage
        adapter.choose_model = self._choose_model
        adapter.web_search = scripted_search(news)
        adapter.clock = clock
        return adapter

    def _choose_model(self, topic):
        if self.agent is None:
            raise LookupError("no agent for this run")
        return self.agent.model_id, None

    def _fetch_network(self, polygon, **kwargs) -> dict:
        """Overpass, answered with the tick's network (a refresh of a stale fixture changes nothing)."""
        if self._network is None:
            raise osm.OsmError("unavailable", "no network fixture for this tick")
        return copy.deepcopy(self._network)

    def _triage(self, **kwargs) -> dict:
        site_id = kwargs["site"]["id"]
        converse = self.agent.converse_for(self._tick_n, site_id)
        measure = self.look_again_measure or self.measure
        kwargs["measure"] = self.log.tagged("look_again", measure)
        outcome = vision_triage.triage(converse=converse, **kwargs)
        converse.finish()
        turns = list(getattr(converse, "responses_used", converse.responses))
        self._conversations.append({"tick": self._tick_n, "site_id": site_id, "turns": turns})
        if converse.refused:
            raise ReplayRefused(converse.refused)
        return outcome

    def tick(self, state: dict | None, spec: TickSpec, *, n: int) -> dict:
        """One research tick's adapter part on `state`: the result, with the new state under
        `state` (the caller keeps or drops it)."""
        self._tick_n = n
        network = spec.network(state) if callable(spec.network) else spec.network
        if network is not None:
            self._network = for_site(network, self.site["polygon"])
            self.store.store_network(self.topic_id, self.site_id, self._network)
        self.stac.item = stac_item(spec.scene)
        clock = self.clock_for(n)
        calls_before, talks_before = len(self.log.calls), len(self._conversations)
        adapter = self._adapter(spec, clock, spec.news)
        printed = io.StringIO()
        # The tick's ids, as the research tick binds its own: a run id that is never twelve digits,
        # which the account-id checks (and the export's redaction) would read as an AWS account.
        with evidence.bind(tick=f"eval-tick-{n}", run=f"eval-run-{n}", topic_id=self.topic_id):
            with frozen_now(spec.now), contextlib.redirect_stdout(printed):
                new_state = adapter.fetch_state(self.topic, state)
                changed, _summary = adapter.material_diff(state, new_state)
        lines = printed.getvalue().splitlines()
        evidence_lines = [line for line in lines if line.startswith(evidence.PREFIX)]
        for line in lines:
            if not line.startswith(evidence.PREFIX):
                print(line, file=sys.stderr)  # the adapter's own notes, for the operator; not kept
        old_ring = ((((state or {}).get("sites") or {}).get(self.site_id) or {}).get("decisions")) or []
        seen = {(entry.get("scene_id"), entry.get("at")) for entry in old_ring if isinstance(entry, dict)}
        ring = (((new_state.get("sites") or {}).get(self.site_id) or {}).get("decisions")) or []
        appended = [entry for entry in ring if (entry.get("scene_id"), entry.get("at")) not in seen]
        latest = (((new_state.get("sites") or {}).get(self.site_id) or {}).get("history") or [{}])[-1]
        measured = any(m.get("scene_id") == spec.scene["scene_id"] for m in new_state.get("measured") or [])
        return {
            "n": n,
            "scene": spec.scene.get("name"),
            "scene_id": spec.scene["scene_id"],
            "captured_at": spec.scene.get("captured_at"),
            "at": new_state.get("fetched_at"),
            "network": spec.label,
            "network_fetched_at": (self._network or {}).get("fetched_at"),
            # The network this tick stored (None: it kept the last), by its canonical digest, so a
            # derived network B is held to the recording's too.
            "network_digest": vision_runs.digest(self._network) if network is not None else None,
            "measured": measured,
            "changed": bool(changed),
            "codes": [entry.get("code") for entry in appended],
            "decisions": appended,
            "lines": evidence_lines,
            "printed": len(lines) - len(evidence_lines),
            "measurements": self.log.calls[calls_before:],
            "responses": self._conversations[talks_before:],
            "clock": list(clock.readings),
            "clock_overrun": clock.overrun,
            "figure_key": latest.get("image_key") if measured else None,
            "state": new_state,
        }


def ring_of(state: dict | None, site_id: str) -> list[dict]:
    return list(((((state or {}).get("sites") or {}).get(site_id) or {}).get("decisions")) or [])


# --- the synthetic city ---------------------------------------------------------------------------------


def geotiff(image, transform, epsg: int, *, tile: int = GEOTIFF_TILE, nodata: str | None = None) -> bytes:
    """`image` (2-D, uint8 or uint16) as a little-endian tiled GeoTIFF, DEFLATE without predictor,
    georeferenced by pixel scale and tiepoint with the EPSG GeoKey: the subset vision/cog.py reads,
    as `sentinel-cogs` lays a band out (tiles after the directory)."""
    import numpy as np

    image = np.ascontiguousarray(image)
    h, w = image.shape
    tiles = []
    for row in range(0, h, tile):
        for col in range(0, w, tile):
            block = np.zeros((tile, tile), image.dtype)
            part = image[row : row + tile, col : col + tile]
            block[: part.shape[0], : part.shape[1]] = part
            tiles.append(zlib.compress(block.astype(image.dtype.newbyteorder("<")).tobytes()))
    origin_x, pixel, _, origin_y, _, _ = transform
    entries = [
        (256, 3, [w]), (257, 3, [h]), (258, 3, [image.dtype.itemsize * 8]), (259, 3, [8]), (277, 3, [1]),
        (317, 3, [1]), (322, 3, [tile]), (323, 3, [tile]), (324, 4, [0] * len(tiles)),
        (325, 4, [len(t) for t in tiles]), (339, 3, [1]), (33550, 12, [pixel, pixel, 0.0]),
        (33922, 12, [0.0, 0.0, 0.0, origin_x, origin_y, 0.0]), (34735, 3, [1, 1, 0, 1, 3072, 0, 1, epsg]),
    ]  # fmt: skip
    if nodata is not None:
        entries.append((42113, 2, nodata.encode("ascii") + b"\x00"))
    codes = {2: "c", 3: "H", 4: "I", 12: "d"}

    def packed(kind, values):
        return values if kind == 2 else struct.pack("<" + codes[kind] * len(values), *values)

    ifd_size = 2 + 12 * len(entries) + 4
    extra_at = 8 + ifd_size
    sizes = [len(packed(kind, values)) for _, kind, values in entries]
    extra_size = sum(size + size % 2 for size in sizes if size > 4)
    data_at = extra_at + extra_size
    offsets, at = [], data_at
    for data in tiles:
        offsets.append(at)
        at += len(data)
    body, extra = b"", b""
    for tag, kind, values in entries:
        values = offsets if tag == 324 else values
        data = packed(kind, values)
        if len(data) <= 4:
            value = data.ljust(4, b"\x00")
        else:
            value = struct.pack("<I", extra_at + len(extra))
            extra += data + (b"\x00" if len(data) % 2 else b"")
        body += struct.pack("<HHI", tag, kind, len(values)) + value
    out = b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", len(entries)) + body + struct.pack("<I", 0)
    return out + extra + b"".join(tiles)


def synthetic_transform() -> tuple[float, ...]:
    from vision_benchmark import CITY_TRANSFORM

    return tuple(CITY_TRANSFORM)


def synthetic_polygon(size: int = SYNTHETIC_SIZE) -> list[list[float]]:
    """The synthetic site: the city less SYNTHETIC_MARGIN_PX, as lon/lat corners."""
    from vision import geo

    from vision_benchmark import CITY_EPSG

    m = SYNTHETIC_MARGIN_PX
    corners = ((m, m), (size - m, m), (size - m, size - m), (m, size - m))
    transform = synthetic_transform()
    return [[round(v, 6) for v in geo.pixel_to_lonlat(transform, CITY_EPSG, x, y)] for x, y in corners]


def synthetic_bbox(size: int = SYNTHETIC_SIZE) -> list[float]:
    from vision import geo

    from vision_benchmark import CITY_EPSG

    transform = synthetic_transform()
    points = [geo.pixel_to_lonlat(transform, CITY_EPSG, x, y) for x in (0, size) for y in (0, size)]
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    return [min(lons), min(lats), max(lons), max(lats)]


def synthetic_scene(entry: dict, size: int = SYNTHETIC_SIZE) -> dict:
    """A synthetic scene in the manifest's shape: its id, capture time, footprint and four URLs."""
    folder = f"{SYNTHETIC_PREFIX}{entry['name']}/"
    return {
        "name": entry["name"],
        "site": "synthetic",
        "scene_id": entry["name"],
        "captured_at": entry["captured_at"],
        "role": "synthetic",
        "assets": {band: {"url": f"{folder}{file}.tif", "etag": None, "bytes": None}
                   for band, file in SYNTHETIC_FILES.items()},  # fmt: skip
        "item": {"cloud_cover": round(entry["cloud"] * 100.0, 3), "nodata_pct": 0.0,
                 "bbox": synthetic_bbox(size)},  # fmt: skip
    }


def synthetic_blobs(size: int = SYNTHETIC_SIZE) -> dict[str, bytes]:
    """Every synthetic scene's four bands as GeoTIFF bytes, by URL: vision_benchmark.synthetic_city
    with the scene's seed, and SCL set to cloud (9) over the west `cloud` share of the city."""
    from vision_benchmark import CITY_EPSG, synthetic_city

    blobs = {}
    for entry in SYNTHETIC_SCENES:
        bands = synthetic_city(size, seed=entry["seed"])
        if entry["cloud"]:
            scl = bands["scl"].copy()
            scl[:, : int(round(size * entry["cloud"]))] = 9
            bands["scl"] = scl
        scene = synthetic_scene(entry, size)
        for band, asset in scene["assets"].items():
            nodata = None if band == "scl" else "0"
            blobs[asset["url"]] = geotiff(bands[band], synthetic_transform(), CITY_EPSG, nodata=nodata)
    return blobs


def synthetic_fetch(blobs: dict[str, bytes] | None = None, log: list | None = None) -> Callable:
    """`fetch(url, start, end)` over the synthetic city's files; any other URL is unreadable."""
    from vision import cog

    blobs = synthetic_blobs() if blobs is None else blobs

    def fetch(url, start, end):
        if url not in blobs:
            raise cog.CogError(f"no synthetic file at {url}")
        if log is not None:
            log.append((url, start, end))
        return blobs[url][start : end + 1]

    return fetch


SYNTHETIC_ENVIRON = {"VISION_ALLOWED_URL_PREFIXES": SYNTHETIC_PREFIX}


def synthetic_network(size: int = SYNTHETIC_SIZE) -> dict:
    """The synthetic city's network (the committed fixture is this, written out)."""
    from vision_benchmark import synthetic_network as benchmark_network

    return benchmark_network(size, lines=SYNTHETIC_LINES, stations_per_line=SYNTHETIC_STATIONS_PER_LINE)


# --- sequences into ticks ---------------------------------------------------------------------------------


def sequence_scenes(name: str, manifest: dict | None) -> tuple[dict, list[dict]]:
    """A sequence's site ({id, name, polygon, params}) and its scenes in order, from the manifest
    (the synthetic city's from code); KeyError naming what is missing."""
    sequence = SEQUENCES[name]
    if sequence["site"] == "synthetic":
        by_name = {entry["name"]: synthetic_scene(entry) for entry in SYNTHETIC_SCENES}
        site = {"id": "synthetic", "name": "Synthetic city", "polygon": synthetic_polygon(), "params": {}}
    else:
        by_name = {scene["name"]: scene for scene in (manifest or {}).get("scenes") or []}
        found = ((manifest or {}).get("sites") or {}).get(sequence["site"])
        if found is None:
            raise KeyError(f"no site {sequence['site']!r} in the manifest")
        site = {"id": sequence["site"], "name": sequence["site"].capitalize(), "polygon": found["polygon"],
                "params": dict(found.get("params") or {})}  # fmt: skip
    missing = [scene for scene in sequence["scenes"] if scene not in by_name]
    if missing:
        raise KeyError(f"sequence {name}: no scene named {missing} in the manifest")
    return site, [by_name[scene] for scene in sequence["scenes"]]


def tick_specs(name: str, scenes: list[dict], network_a: dict, network_b: dict | Callable | None,
               nows: list[datetime] | None = None) -> list[TickSpec]:  # fmt: skip
    """The sequence's ticks: each scene with its time (TICK_LAG after capture, or `nows`), the
    network its label names (A for the first tick and whenever the label changes back, B by
    `network_b`, a dict or a callable of the previous state), and the sequence's news."""
    sequence = SEQUENCES[name]
    specs, previous = [], None
    for index, scene in enumerate(scenes):
        label = sequence["networks"][index]
        network = None
        if label != previous:
            network = network_a if label == "A" else network_b
            if network is None:
                raise ReplayRefused(f"sequence {name}: tick {index + 1} needs network {label}")
        now = nows[index] if nows else tick_time(scene["captured_at"])
        news = list(sequence["news"])
        specs.append(TickSpec(scene=scene, now=now, network=network, label=label, news=news))
        previous = label
    return specs


# --- a replay held to its recording -----------------------------------------------------------------------


def _within(name: str, a, b, kind: str = "value", scale: float = 1.0) -> bool:
    numbers = all(isinstance(v, int | float) and not isinstance(v, bool) for v in (a, b))
    if not numbers:
        return a == b
    if kind == "relative":
        return abs(a - b) <= RELATIVE_ABS + PARITY_SLACK
    if name in PARITY_ABS:
        allowed = PARITY_ABS[name] * (2 if kind == "delta" else 1)
        return abs(a - b) <= allowed + PARITY_SLACK
    if name in PARITY_REL:
        if kind == "delta":
            return abs(a - b) <= 2 * PARITY_REL[name] * max(scale, 1.0) + PARITY_SLACK
        return abs(a - b) <= PARITY_REL[name] * max(abs(a), abs(b), 1e-12) + PARITY_SLACK
    return a == b


def _numbers_differ(path: str, a, b, kind: str, scales: dict) -> list[str]:
    if a is None or b is None or not isinstance(a, dict) or not isinstance(b, dict):
        return [] if a == b else [f"{path}: {a!r} vs {b!r}"]
    out = []
    for key in sorted(set(a) | set(b)):
        if not _within(key, a.get(key), b.get(key), kind, scales.get(key, 1.0)):
            out.append(f"{path}.{key}: {a.get(key)!r} vs {b.get(key)!r}")
    return out


def compare_entry(recorded: dict, replayed: dict) -> list[str]:
    """Where a replayed ring entry differs from the recorded one: the code-derived fields exactly,
    the measurements within PARITY (EXCLUDED left out). An empty list is a match."""
    out = []
    scales = {}
    for key in ("desert_km2", "built_up_km2"):
        pair = ((recorded.get("measure") or {}).get(key), (recorded.get("baseline") or {}).get(key))
        values = [abs(v) for v in pair if isinstance(v, int | float)]
        scales[key] = max(values) if values else 1.0
    for key in ("measure", "baseline"):
        out += _numbers_differ(key, recorded.get(key), replayed.get(key), "value", scales)
    out += _numbers_differ("delta", recorded.get("delta"), replayed.get("delta"), "delta", scales)
    out += _numbers_differ("relative", recorded.get("relative"), replayed.get("relative"), "relative", scales)
    if not _within("coverage", recorded.get("coverage"), replayed.get("coverage")):
        out.append(f"coverage: {recorded.get('coverage')!r} vs {replayed.get('coverage')!r}")
    for key in ("v", "at", "scene_id", "captured_at", "baseline_scenes", "thresholds", "code", "moved",
                "network_changed", "reported"):  # fmt: skip
        if recorded.get(key) != replayed.get(key):
            out.append(f"{key}: {recorded.get(key)!r} vs {replayed.get(key)!r}")
    ta, tb = recorded.get("triage"), replayed.get("triage")
    if (ta is None) != (tb is None):
        shown = ["none" if t is None else "present" for t in (ta, tb)]
        return [*out, f"triage: {shown[0]} vs {shown[1]}"]
    if ta is None:
        return out
    for key in ("verdict", "stop", "model_id", "model_calls", "input_tokens", "output_tokens", "figure",
                "steps_total", "budget_refusals", "hosts", "reason"):  # fmt: skip
        if ta.get(key) != tb.get(key):
            out.append(f"triage.{key}: {ta.get(key)!r} vs {tb.get(key)!r}")
    steps_a, steps_b = ta.get("steps") or [], tb.get("steps") or []
    if len(steps_a) != len(steps_b):
        return [*out, f"triage.steps: {len(steps_a)} vs {len(steps_b)}"]
    for n, (sa, sb) in enumerate(zip(steps_a, steps_b, strict=True), start=1):
        for key in ("tool", "ok"):
            if sa.get(key) != sb.get(key):
                out.append(f"triage.steps[{n}].{key}: {sa.get(key)!r} vs {sb.get(key)!r}")
        ra, rb = dict(sa.get("result") or {}), dict(sb.get("result") or {})
        for key in ("worker_request_id", "worker_ms", "build"):
            ra.pop(key, None)
            rb.pop(key, None)
        out += _numbers_differ(f"triage.steps[{n}].result.measured", ra.pop("measured", None),
                               rb.pop("measured", None), "value", {})  # fmt: skip
        if ra != rb:
            out.append(f"triage.steps[{n}].result: {ra!r} vs {rb!r}")
    return out


def compare_recorded(recorded_ticks: list[dict], replayed_ticks: list[dict]) -> tuple[list[dict], dict]:
    """2.6.4's assertions, as run-file checks: per tick, the ring entries equal (compare_entry)
    and the clock replayed without running past its recording; and, reported apart, how many worker
    calls gave the recorded metrics digest exactly (same CPU family) and how many matched within
    the parity tolerances (vision_benchmark.parity)."""
    from vision_benchmark import parity

    checks = []
    if len(recorded_ticks) != len(replayed_ticks):
        checks.append({"name": "ticks", "expected": len(recorded_ticks), "got": len(replayed_ticks),
                       "pass": False})  # fmt: skip
    digests = {"calls": 0, "equal_digest": 0, "within_tolerance": 0, "differences": []}
    for old, new in zip(recorded_ticks, replayed_ticks, strict=False):
        n = new["n"]
        differences = []
        old_entries, new_entries = old.get("decisions") or [], new.get("decisions") or []
        if len(old_entries) != len(new_entries):
            differences.append(f"{len(old_entries)} recorded entries vs {len(new_entries)}")
        for a, b in zip(old_entries, new_entries, strict=False):
            differences += compare_entry(a, b)
        if old.get("network_digest") != new.get("network_digest"):
            differences.append("the network this tick stored differs from the recording's")
        checks.append({"name": f"tick {n} ({new.get('scene')}): the ring equals the recording",
                       "expected": "equal", "got": "; ".join(differences[:6]) or "equal",
                       "pass": not differences})  # fmt: skip
        within = not new.get("clock_overrun")
        checks.append({"name": f"tick {n}: the clock replayed within its recording", "expected": True,
                       "got": within, "pass": within})  # fmt: skip
        old_calls, new_calls = old.get("measurements") or [], new.get("measurements") or []
        for a, b in zip(old_calls, new_calls, strict=False):
            if not (a.get("ok") and b.get("ok")):
                continue
            digests["calls"] += 1
            result = parity(a, b)
            digests["equal_digest"] += int(result["equal_digest"])
            digests["within_tolerance"] += int(result["pass"])
            if not result["pass"]:
                digests["differences"].append(
                    {"tick": n, "kind": b.get("kind"), "differences": result["differences"]}
                )
        if len(old_calls) != len(new_calls):
            digests["differences"].append({"tick": n, "kind": "calls", "differences": [
                f"{len(old_calls)} recorded worker calls vs {len(new_calls)}"]})  # fmt: skip
    checks.append({"name": "every worker call within the parity tolerances", "expected": True,
                   "got": not digests["differences"], "pass": not digests["differences"]})  # fmt: skip
    return checks, digests


# --- the trace, rendered ----------------------------------------------------------------------------------


def replay_rows(entry: dict) -> list[dict]:
    """The six-step trace of a replayed decision (vision_decisions.trace, with the replay's own
    action: a Finding would be written, or none)."""
    vocabulary = rail_access.RailAccessAdapter.decision_vocabulary()
    return vision_decisions.trace(entry, vocabulary=vocabulary, attempt={"stored": True, "replay": True})


def _cell(text) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "/").strip()


def render_markdown(run: dict) -> str:
    """The replay as Markdown: what was run, then each tick's decision as the assistant's scene
    view shows it (ops_mcp/vision_evidence.trace_rows over vision_decisions.trace)."""
    from ops_mcp import vision_evidence

    vocabulary = rail_access.RailAccessAdapter.decision_vocabulary()
    params, results = run.get("params") or {}, run.get("results") or {}
    agent = run.get("agent") or {}
    thresholds = params.get("thresholds")
    lines = [
        f"# Replay: {params.get('sequence')}",
        "",
        f"Generated by `scripts/vision_eval.py replay` from the run file `runs/{run.get('run_id')}.json` "
        "(do not edit by hand). The real rail access adapter, tick by tick, over pinned scenes: "
        f"the agent {agent.get('mode')} (model `{agent.get('model_id')}`), the worker {params.get('worker')} "
        f"({run.get('backend')}), the code at `{(run.get('code') or {}).get('git_sha')}`.",
        "",
    ]
    if thresholds == "demo":
        lines += ["**Thresholds of 0 (the demo topic's), disclosed:** every scene after the first goes to "
                  "the agent, whatever moved.", ""]  # fmt: skip
    if params.get("sequence") == "sydney-c1":
        lines += ["**Constructed:** the last tick's network is the fixture with one synthetic line of five "
                  "stations added, and its news item is constructed (evaluation case C1).", ""]  # fmt: skip
    if params.get("synthetic"):
        lines += ["**Synthetic:** a synthetic city and network, for the tests; not a measurement of a "
                  "place.", ""]  # fmt: skip
    lines += ["Each tick happens six hours after its scene was captured. The replay stops at the adapter's "
              "decision: no summary is written and no article drafted, so a reported change reads "
              "\"a Finding would be written\".", ""]  # fmt: skip
    for tick in results.get("ticks") or []:
        scene = f"{tick.get('scene')} ({tick.get('scene_id')})"
        lines += [f"## Tick {tick['n']}: {scene}, network {tick.get('network')}", ""]
        if not tick.get("decisions"):
            lines += ["No decision: nothing was measured this tick.", ""]
        for entry in tick.get("decisions") or []:
            table = vision_evidence.trace_rows(replay_rows(entry), entry, vocabulary)
            lines += ["| " + " | ".join(vision_evidence.TRACE_COLUMNS) + " |",
                      "|" + "---|" * len(vision_evidence.TRACE_COLUMNS)]  # fmt: skip
            lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in table]
            if (entry.get("triage") or {}).get("reason"):
                note = _cell(entry["triage"]["reason"])
                lines += ["", f'The agent\'s note (model text, unverified): "{note}"']
            lines.append("")
    if params.get("synthetic"):
        lines += ["Synthetic inputs: no Copernicus or OpenStreetMap data.", ""]
    else:
        lines += ["Contains modified Copernicus Sentinel data; rail network data © OpenStreetMap "
                  "contributors (ODbL).", ""]  # fmt: skip
    return "\n".join(lines)
