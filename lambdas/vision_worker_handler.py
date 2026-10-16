"""The vision worker: measure one site in one Sentinel-2 scene with OpenCV 5, and say so.

docs/enhancements/opencv-agentic-vision-enhancement.md §2 and docs/enhancements/rail-access-monitor.md
§3. Invoked by the research tick's adapters through common/vision_client.py, usually from another
region: the worker runs next to the imagery (`sentinel-cogs`, us-west-2), reads only the tiles
under the site, and sends back a few KB of metrics and one small figure. It keeps no state and
writes nothing; the adapter stores what it is given in the home region.

Two tasks share the handler, chosen by the request's `task` (common/vision_contract.py's TASKS):

    ships        nir and green at 10 m, scl optional: vessels counted and measured (vision/analyse.py)
    rail_access  red, nir, swir16 and scl on swir16's 20 m grid, the 10 m bands from their COG
                 overviews, plus the OpenStreetMap network sent with the request: built-up heat,
                 station reach, the rail graph and its measures (vision/rail_analyse.py)

The same handler is both backends. Which one this deployment is comes from its environment, and a
request for the other is refused, so a result can never claim a backend it didn't run on:

    VISION_BACKEND           "opencv" (stock opencv-python-headless) or "cool" (OpenCV's COOL build)
    COOL_BUILD_SHA256        the cool backend only: the fingerprint of the pinned COOL build
                             (vision/build.py). A cool worker whose cv2 doesn't match refuses
                             every request with "not_cool".
    VISION_ALLOWED_URL_PREFIXES  optional, comma-separated; defaults to the sentinel-cogs bucket. Each
                             entry must be https://<host>/...; any other is skipped
                             (common/vision_contract.py's allowed_prefixes)

The request and reply are common/vision_contract.py's. Every reply carries the task, the build
record and per-stage timings (read, analyse, annotate; the rail task adds the graph stage), which
is what the COOL benchmark compares.

**One evidence line per request that returns** (`_evidence`; docs/enhancements/vision-evidence.md):
after the reply is built, success or refusal, the handler prints one JSON line in exactly
common/evidence.py's format (`vision_measure`: the request id, the task, the build's fingerprint
prefix, the CPU, the stage timings, what was read, the coverage, the counts, the peak RSS so far
of this process, which a warm container keeps growing). This package cannot import
common/evidence (it ships only vision_contract.py), so the line is built here with its own small
serialiser; a test holds the two equal. Every value comes from the reply or the validated request,
never from a refusal's `detail`, which can quote a URL. A run killed by the timeout or by running
out of memory never reaches it: Lambda's own REPORT line is its only trace, and the runsheet's
query counts those.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import sys
import time

import numpy as np
import requests

from common import vision_contract as contract
from vision import build, cog, scene
from vision.analyse import analyse_site
from vision.annotate import annotate
from vision.detect import DetectParams
from vision.rail_analyse import RailParams, analyse_rail_access
from vision.rail_annotate import annotate_rail

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REQUEST_TIMEOUT_SECONDS = 20.0
# How much of a range reply is read at a time (http_fetch): requests' own `.content` reads 10 KB
# chunks, so this costs a tile read nothing, and a reply longer than its range is read at most one
# chunk past it.
_READ_CHUNK_BYTES = 64 * 1024
# The rail task's analysis grid: every band is put onto this one's pixels (the reference band).
RAIL_REFERENCE_BAND = "swir16"
# The rail task's own window cap, per side, in swir16's 20 m pixels: 3,584 px = 71.7 km, which holds
# the four city boxes of docs/enhancements/rail-access-monitor.md §2 (Sydney's is the largest, 3,327 x
# 3,306 px with read_site's 16 px pad round it). The ships task keeps scene.MAX_WINDOW_PX (4096 px).
# Why less: the rail analysis grows with the window and the worker has one vCPU (2048 MB). On one core
# of a 4-core Intel Xeon at 2.10 GHz, through lambda_handler (the bands read from in-memory COGs, the
# analysis, the figure, the reply), the worst contract-valid networks (scene.MAX_WINDOW_PX lists them),
# with ways that run off the window's edges as clipped OSM ways do, take up to 37.5 s at 4096 px,
# about 49 s on the arm64 worker (taken as 1.3 times slower): with a 10-30 s read, 59-79 s, past the
# client's 58 s read timeout (rail_access.WORKER_READ_TIMEOUT). At 3,584 px they take up to 25.5 s at
# a peak RSS of 1.32 GB, about 33 s on arm64, and the read covers 77% of the pixels: 41-56 s in all.
# That budget assumes the read scales with the window's pixels; a full 30 s read on top of the
# worst case would not fit. The worst case is constructed (two 1.3 km-wide solid bars of track
# along both edges of the window, no real network looks like it). No real OpenStreetMap network
# has been timed yet: PR J's synthetic city (180 stations, 221 nodes) took 9.2 to 9.6 s at 4096 px
# on four cores. A worker that times out is an `invoke` error the adapter retries on its next tick.
RAIL_MAX_WINDOW_PX = 3584
_session: requests.Session | None = None


class _NoRedirectSession(requests.Session):
    """The worker's session (http_fetch's): no reply is ever a redirect to it. allow_redirects=False
    alone is not enough: on a 3xx with a Location, requests' Session.send still starts its redirect
    machinery (resolve_redirects, to fill the reply's `next`), which reads the whole body and parses
    the Location before the caller sees the status. With no redirect target that never starts, so
    http_fetch gets the 3xx unread, and a Location that does not parse is the CogError every 3xx is
    (`unreadable_scene`), not a ValueError (`internal`)."""

    def get_redirect_target(self, resp):
        return None


class BadParams(Exception):
    """A params dataclass refused a combination the contract's per-field checks let through: the
    caller's to fix (bad_request), found before any scene is read. Not a ValueError, so that every
    ValueError from the reading and the analysis stays an internal error, and is logged."""


def _params(cls, request: dict):
    """`cls` (DetectParams, RailParams) built from the request's params, or BadParams."""
    try:
        return cls(**request["params"])
    except ValueError as exc:
        raise BadParams(str(exc)) from exc


def http_fetch(url: str, start: int, end: int) -> bytes:
    """One HTTP range request. S3 answers 206 with exactly the bytes asked for (fewer at the end
    of the file).

    Redirects are not followed, and any 3xx is a CogError (the caller's `unreadable_scene`), as
    common/osm.py treats one: the allowlist is about the host actually read, and a redirect from an
    allowed URL could point anywhere, the Lambda Runtime API on 127.0.0.1 included. A 3xx's body is
    not read, nor its Location parsed (_NoRedirectSession). Any other body is streamed and read only
    as far as the range: end - start + 1 bytes from a 206, end + 1 from a 200 (a server that ignores
    Range sends the whole object, which must never be held whole)."""
    global _session
    if _session is None:
        _session = _NoRedirectSession()
    response = _session.get(
        url,
        headers={"Range": f"bytes={start}-{end}"},
        timeout=_REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
        stream=True,
    )
    try:
        status = response.status_code
        if 300 <= status < 400:
            raise cog.CogError(f"HTTP {status} redirect from {url} not followed")
        if status not in (200, 206):
            raise cog.CogError(f"HTTP {status} reading {url}")
        data = _read_at_most(response, end - start + 1 if status == 206 else end + 1)
    finally:
        response.close()
    # A server that ignores Range sends the whole file with 200: keep only what was asked for.
    return data[start : end + 1] if status == 200 else data


def _read_at_most(response, limit: int) -> bytes:
    """The first `limit` bytes of a streamed body (all of it when shorter). Read in chunks of at
    most _READ_CHUNK_BYTES, and no further once it is past `limit`; a body of `limit` bytes is read
    to its end, so its connection goes back to the session's pool for the next range."""
    chunks, total = [], 0
    for chunk in response.iter_content(chunk_size=min(_READ_CHUNK_BYTES, limit + 1)):
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            break
    return b"".join(chunks)[:limit]


def lambda_handler(event, context, fetch=None):
    reply, request, info = _handle(event, context, fetch or http_fetch)
    try:
        _evidence(reply, request, info, context)
    except Exception:  # noqa: BLE001 - the line is bookkeeping; the reply is the work
        pass
    return reply


def _handle(event, context, fetch) -> tuple[dict, dict | None, dict | None]:
    """The request's reply, with the validated request (None when the request was refused as it
    came) and the build record (None when it was never looked up), for the evidence line."""
    started = time.perf_counter()
    backend = os.environ.get("VISION_BACKEND", "opencv")
    prefixes = contract.allowed_prefixes(os.environ.get("VISION_ALLOWED_URL_PREFIXES"))

    try:
        request = contract.validate_request(event, prefixes)
    except contract.ContractError as exc:
        return contract.error("bad_request", str(exc)), None, None
    if request["backend"] != backend:
        return contract.error("backend_mismatch", f"this worker is {backend!r}"), request, None

    info = build.build_info()
    if backend == "cool" and not build.matches(info, os.environ.get("COOL_BUILD_SHA256")):
        logger.error("cool backend running a non-COOL build: %s", info)
        return contract.error("not_cool", "this worker's OpenCV is not the pinned COOL build"), request, info

    try:
        return _measure(request, info, fetch, started), request, info
    except BadParams as exc:
        return contract.error("bad_request", str(exc)), request, info
    except scene.SiteOutsideScene as exc:
        return contract.error("site_outside_scene", str(exc)), request, info
    except scene.WindowTooLarge as exc:
        return contract.error("window_too_large", str(exc)), request, info
    except (cog.CogError, requests.RequestException) as exc:
        logger.warning("unreadable scene %s: %s", request["scene"]["id"], exc)
        return contract.error("unreadable_scene", str(exc)), request, info
    except Exception:  # a ValueError from the analysis included: the worker's fault, not the caller's
        logger.exception("vision worker failed on %s / %s", request["site"]["id"], request["scene"]["id"])
        return contract.error("internal", "the worker failed; see its log"), request, info


# --- the evidence line -----------------------------------------------------------------------------

# common/evidence.py's fixed form: its version, the first two keys, the three always-present ids
# (null here: the worker is bound to no tick), and `vision_measure`'s fields in the schema's order.
# A test holds this prefix equal to evidence.PREFIX and the line equal to evidence.line's.
EVIDENCE_VERSION = "bloggerbear/1"
EVIDENCE_PREFIX = '{"evidence": "bloggerbear/1", "event": "'
EVIDENCE_EVENT = "vision_measure"
_EVIDENCE_IDS = ("tick", "run", "topic_id")
# Each field's kind, as evidence.EVENTS["vision_measure"] declares it: a value that is not of its
# kind is left out (the schema, not a filter: nothing but these ever reaches the line).
_EVIDENCE_FIELDS = (
    ("request_id", "id"), ("task", "code"), ("backend", "code"), ("ok", "bool"), ("code", "code"),
    ("site_id", "id"), ("scene_id", "id"), ("build", "hex12"), ("opencv", "version"), ("kleidicv", "bool"),
    ("cpu", "cpu"), ("cpu_count", "int"), ("arch", "code"), ("window_px", "numbers"), ("io_requests", "int"),
    ("io_bytes", "int"), ("levels", "numbers"), ("read_ms", "int"), ("analyse_ms", "int"),
    ("graph_ms", "int"), ("annotate_ms", "int"), ("total_ms", "int"), ("coverage", "num"),
    ("quality_flags", "codes"),
    ("stations", "int"), ("snapped", "int"), ("nodes", "int"), ("edges", "int"), ("components", "int"),
    ("count", "int"), ("rss_peak_mb", "int"),
)  # fmt: skip
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_CPU = re.compile(r"^[A-Za-z0-9 @().,_+-]{1,80}$")  # common/evidence.py's `cpu` kind
_VERSION = re.compile(r"^[0-9][0-9.]{0,19}$")
_MAX_CODES, _MAX_NUMBERS = 12, 12
# The reply's metric names and the line's (rail: the graph's counts; ships: the count).
_COUNT_FIELDS = (
    ("stations", "station_count"), ("snapped", "snapped_station_count"), ("nodes", "node_count"),
    ("edges", "edge_count"), ("components", "components"), ("count", "count"),
)  # fmt: skip


def _evidence(reply: dict, request: dict | None, info: dict | None, context) -> None:
    """Print the request's `vision_measure` line (the module docstring). Never raises past
    lambda_handler's guard; a line that cannot be built is simply not written."""
    print(_evidence_line(_evidence_fields(reply, request, info, context)))


def _evidence_fields(reply: dict, request: dict | None, info: dict | None, context) -> dict:
    """The line's values, from the reply (on success its ids, build, io, timings and metrics), the
    validated request (the ids of a refused one), the build record and the Lambda context: nothing
    from a refusal's `detail`."""
    ok = bool(reply.get("ok"))
    metrics = reply.get("metrics") if ok and isinstance(reply.get("metrics"), dict) else {}
    info = reply.get("build") if ok and isinstance(reply.get("build"), dict) else (info or {})
    request = request or {}
    sha = info.get("build_sha256")
    fields = {
        "request_id": getattr(context, "aws_request_id", None),
        "task": reply.get("task") if ok else request.get("task"),
        "backend": reply.get("backend") if ok else request.get("backend"),
        "ok": ok,
        "code": None if ok else reply.get("error"),
        "site_id": reply.get("site_id") if ok else (request.get("site") or {}).get("id"),
        "scene_id": reply.get("scene_id") if ok else (request.get("scene") or {}).get("id"),
        "build": sha[:12] if isinstance(sha, str) else None,
        "opencv": info.get("opencv_version"),
        "kleidicv": info.get("kleidicv"),
        "cpu": info.get("cpu"),
        "cpu_count": info.get("cpu_count"),
        "arch": info.get("arch"),
    }
    window = reply.get("window") if ok else None
    if isinstance(window, list | tuple) and len(window) == 4:
        fields["window_px"] = {"w": window[2] - window[0], "h": window[3] - window[1]}
    io = reply.get("io") if ok and isinstance(reply.get("io"), dict) else {}
    fields["io_requests"], fields["io_bytes"] = io.get("requests"), io.get("bytes")
    fields["levels"] = io.get("levels_used")
    timings = reply.get("timings_ms") if ok and isinstance(reply.get("timings_ms"), dict) else {}
    for stage in ("read", "analyse", "graph", "annotate", "total"):
        fields[f"{stage}_ms"] = timings.get(stage)
    fields["coverage"], fields["quality_flags"] = metrics.get("coverage"), metrics.get("quality_flags")
    for name, metric in _COUNT_FIELDS:
        fields[name] = metrics.get(metric)
    fields["rss_peak_mb"] = _rss_peak_mb()
    return fields


def _rss_peak_mb() -> int | None:
    """This process's peak resident size so far, in MB, or None where the platform has no
    `resource` (Windows). macOS reports ru_maxrss in bytes, Linux in KB. A warm container keeps
    its largest run's figure."""
    try:
        import resource  # inside the try: every in-process run of this handler imports it too
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak // (1024 * 1024) if sys.platform == "darwin" else peak // 1024


def _evidence_line(fields: dict) -> str:
    """The line, in common/evidence.py's form: `evidence`, `event`, the three ids (null), then each
    field of _EVIDENCE_FIELDS whose value is of its kind, lists comma-joined, `num` to six
    significant figures, one line of ASCII."""
    record = {"evidence": EVIDENCE_VERSION, "event": EVIDENCE_EVENT, **dict.fromkeys(_EVIDENCE_IDS)}
    for name, kind in _EVIDENCE_FIELDS:
        value = _of_kind(kind, fields.get(name))
        if value is not None:
            record[name] = value
    return json.dumps(record, separators=(", ", ": "), ensure_ascii=True, allow_nan=False)


def _of_kind(kind: str, value):
    """`value` as evidence.py's `kind` keeps it, or None."""
    if value is None or isinstance(value, bool) != (kind == "bool"):
        return None
    if kind in ("int", "num"):
        if not isinstance(value, int | float) or not math.isfinite(value):
            return None
        if kind == "int":
            return int(round(value))
        return value if isinstance(value, int) else float(f"{value:.6g}")
    if kind == "bool":
        return value
    if kind == "numbers":
        if not isinstance(value, dict):
            return None
        kept = {}
        for key, item in value.items():
            number = _of_kind("num", item)
            if isinstance(key, str) and _CODE.match(key) and number is not None:
                kept[key] = number
            if len(kept) >= _MAX_NUMBERS:
                break
        return kept
    if kind == "codes":
        if not isinstance(value, list | tuple):
            return None
        if not all(isinstance(v, str) and _CODE.match(v) for v in value):
            return None
        return ",".join(value[:_MAX_CODES])
    pattern = {"code": _CODE, "id": _ID, "hex12": _HEX12, "cpu": _CPU, "version": _VERSION}[kind]
    return value if isinstance(value, str) and pattern.match(value) else None


def _measure(request: dict, info: dict, fetch, started: float) -> dict:
    site, metrics, png, timings = _MEASURERS[request["task"]](request, fetch)
    return {
        "version": contract.VERSION,
        "ok": True,
        "task": request["task"],
        "backend": request["backend"],
        "site_id": request["site"]["id"],
        "scene_id": request["scene"]["id"],
        "captured_at": request["scene"]["captured_at"],
        "metrics": metrics,
        "epsg": site.epsg,
        "window": list(site.window),
        "image_png_b64": base64.b64encode(png).decode("ascii") if png is not None else None,
        "build": info,
        "io": {"requests": site.requests, "bytes": site.bytes_read, "levels_used": site.levels_used},
        "timings_ms": {**timings, "total": _ms(started, time.perf_counter())},
    }


def _measure_ships(request: dict, fetch) -> tuple[scene.SiteBands, dict, bytes | None, dict]:
    """Vessels in the site: the bands on nir's grid, the analysis, the figure, the stage timings."""
    params = _params(DetectParams, request)  # before any read: a bad combination costs no bytes
    t0 = time.perf_counter()
    site = scene.read_site(request["scene"]["assets"], request["site"]["polygon"], fetch)
    t1 = time.perf_counter()

    nir, green = site.bands["nir"], site.bands["green"]
    scl = site.bands.get("scl")
    # No-data pixels in the reference band are unusable whether or not there is a SCL band.
    nodata = site.nodata.get("nir")
    if nodata is not None:
        missing = nir == nodata
        if missing.any():
            scl = np.full(nir.shape, 6, np.uint8) if scl is None else scl.copy()
            scl[missing] = 0
    analysis = analyse_site(
        nir,
        green,
        [site.polygon_px],
        site.pixel_size_m,
        scl=scl,
        params=params,
        transform=site.transform,
        coverage_floor=request["coverage_floor"],
    )
    t2 = time.perf_counter()

    png = None
    if request["image"]:
        detections = analysis.metrics["detections"]
        png = annotate(nir, analysis.measurable, detections, stretch_max=params.stretch_max)
    t3 = time.perf_counter()
    return site, analysis.metrics, png, {"read": _ms(t0, t1), "analyse": _ms(t1, t2), "annotate": _ms(t2, t3)}


def _measure_rail(request: dict, fetch) -> tuple[scene.SiteBands, dict, bytes | None, dict]:
    """Rail access over the site: the bands on swir16's 20 m grid, the analysis against the
    network in the request, the figure, the stage timings (with the graph stage apart)."""
    params = _params(RailParams, request)  # before any read: a bad combination costs no bytes
    t0 = time.perf_counter()
    site = scene.read_site(
        request["scene"]["assets"],
        request["site"]["polygon"],
        fetch,
        reference=RAIL_REFERENCE_BAND,
        max_window_px=RAIL_MAX_WINDOW_PX,
    )
    t1 = time.perf_counter()

    bands = dict(site.bands)
    # No-data pixels in the reference band are unusable: folded into SCL's no-data class, as the
    # ship path does with nir.
    nodata = site.nodata.get(RAIL_REFERENCE_BAND)
    if nodata is not None:
        missing = bands[RAIL_REFERENCE_BAND] == nodata
        if missing.any():
            scl = bands["scl"].copy()
            scl[missing] = 0
            bands["scl"] = scl
    analysis = analyse_rail_access(
        bands,
        [site.polygon_px],
        site.pixel_size_m,
        site.transform,
        site.epsg,
        request["network"],
        params,
        request["coverage_floor"],
    )
    t2 = time.perf_counter()

    png = None
    if request["image"]:
        png = annotate_rail(analysis, bands["red"], year=request["scene"]["captured_at"][:4])
    t3 = time.perf_counter()
    timings = {
        "read": _ms(t0, t1),
        "analyse": _ms(t1, t2),
        "graph": analysis.layers["timings"]["graph"],
        "annotate": _ms(t2, t3),
    }
    return site, analysis.metrics, png, timings


_MEASURERS = {"ships": _measure_ships, "rail_access": _measure_rail}


def _ms(a: float, b: float) -> float:
    return round((b - a) * 1000.0, 1)
