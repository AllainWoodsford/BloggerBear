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
    VISION_ALLOWED_URL_PREFIXES  optional, comma-separated; defaults to the sentinel-cogs bucket

The request and reply are common/vision_contract.py's. Every reply carries the task, the build
record and per-stage timings (read, analyse, annotate; the rail task adds the graph stage), which
is what the COOL benchmark compares.
"""

from __future__ import annotations

import base64
import logging
import os
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
# The rail task's analysis grid: every band is put onto this one's pixels (the reference band).
RAIL_REFERENCE_BAND = "swir16"
# The rail task's own window cap, per side, in swir16's 20 m pixels: 3,584 px = 71.7 km, which holds
# the four city boxes of docs/enhancements/rail-access-monitor.md §2 (Sydney's is the largest, 3,327 x
# 3,306 px with read_site's 16 px pad round it). The ships task keeps scene.MAX_WINDOW_PX (4096 px).
# Why less: the rail analysis grows with the window and the worker has one vCPU (2048 MB). On one core
# of a 4-core Intel Xeon at 2.10 GHz the worst contract-valid networks (scene.MAX_WINDOW_PX lists them)
# take up to 32.2 s at 4096 px, about 42 s on the arm64 worker (taken as 1.3 times slower), which with
# a 10-30 s read runs past the client's 58 s read timeout (rail_access.WORKER_READ_TIMEOUT) whenever
# the read takes over 16 s. At 3,584 px they take up to 20.7 s at a peak RSS of 1.24 GB, about 27 s on
# arm64, and the read covers 77% of the pixels: 35-50 s in all.
RAIL_MAX_WINDOW_PX = 3584
_session: requests.Session | None = None


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
    of the file)."""
    global _session
    if _session is None:
        _session = requests.Session()
    response = _session.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=_REQUEST_TIMEOUT_SECONDS)
    if response.status_code not in (200, 206):
        raise cog.CogError(f"HTTP {response.status_code} reading {url}")
    data = response.content
    # A server that ignores Range sends the whole file with 200: keep only what was asked for.
    return data[start : end + 1] if response.status_code == 200 else data


def lambda_handler(event, context, fetch=None):
    fetch = fetch or http_fetch
    started = time.perf_counter()
    backend = os.environ.get("VISION_BACKEND", "opencv")
    prefixes = tuple(
        p.strip() for p in os.environ.get("VISION_ALLOWED_URL_PREFIXES", "").split(",") if p.strip()
    ) or contract.DEFAULT_ALLOWED_URL_PREFIXES

    try:
        request = contract.validate_request(event, prefixes)
    except contract.ContractError as exc:
        return contract.error("bad_request", str(exc))
    if request["backend"] != backend:
        return contract.error("backend_mismatch", f"this worker is {backend!r}")

    info = build.build_info()
    if backend == "cool" and not build.matches(info, os.environ.get("COOL_BUILD_SHA256")):
        logger.error("cool backend running a non-COOL build: %s", info)
        return contract.error("not_cool", "this worker's OpenCV is not the pinned COOL build")

    try:
        return _measure(request, info, fetch, started)
    except BadParams as exc:
        return contract.error("bad_request", str(exc))
    except scene.SiteOutsideScene as exc:
        return contract.error("site_outside_scene", str(exc))
    except scene.WindowTooLarge as exc:
        return contract.error("window_too_large", str(exc))
    except (cog.CogError, requests.RequestException) as exc:
        logger.warning("unreadable scene %s: %s", request["scene"]["id"], exc)
        return contract.error("unreadable_scene", str(exc))
    except Exception:  # a ValueError from the analysis included: the worker's fault, not the caller's
        logger.exception("vision worker failed on %s / %s", request["site"]["id"], request["scene"]["id"])
        return contract.error("internal", "the worker failed; see its log")


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
