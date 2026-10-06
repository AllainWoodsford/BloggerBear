"""The vision worker: measure one site in one Sentinel-2 scene with OpenCV 5, and say so.

docs/enhancements/opencv-agentic-vision-enhancement.md §2. Invoked by the research tick's
`satellite_vision` adapter through common/vision_client.py, usually from another region: the
worker runs next to the imagery (`sentinel-cogs`, us-west-2), reads only the tiles under the site,
and sends back a few KB of metrics and one small figure. It keeps no state and writes nothing; the
adapter stores what it is given in the home region.

The same handler is both backends. Which one this deployment is comes from its environment, and a
request for the other is refused, so a result can never claim a backend it didn't run on:

    VISION_BACKEND           "opencv" (stock opencv-python-headless) or "cool" (OpenCV's COOL build)
    COOL_BUILD_SHA256        the cool backend only: the fingerprint of the pinned COOL build
                             (vision/build.py). A cool worker whose cv2 doesn't match refuses
                             every request with "not_cool".
    VISION_ALLOWED_URL_PREFIXES  optional, comma-separated; defaults to the sentinel-cogs bucket

The request and reply are common/vision_contract.py's. Every reply carries the build record and
per-stage timings (read, analyse, annotate), which is what the COOL benchmark compares.
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

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REQUEST_TIMEOUT_SECONDS = 20.0
_session: requests.Session | None = None


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
    except scene.SiteOutsideScene as exc:
        return contract.error("site_outside_scene", str(exc))
    except scene.WindowTooLarge as exc:
        return contract.error("window_too_large", str(exc))
    except (cog.CogError, requests.RequestException) as exc:
        logger.warning("unreadable scene %s: %s", request["scene"]["id"], exc)
        return contract.error("unreadable_scene", str(exc))
    except Exception:
        logger.exception("vision worker failed on %s / %s", request["site"]["id"], request["scene"]["id"])
        return contract.error("internal", "the worker failed; see its log")


def _measure(request: dict, info: dict, fetch, started: float) -> dict:
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
    params = DetectParams(**request["params"])
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

    image_b64 = None
    if request["image"]:
        detections = analysis.metrics["detections"]
        png = annotate(nir, analysis.measurable, detections, stretch_max=params.stretch_max)
        image_b64 = base64.b64encode(png).decode("ascii")
    t3 = time.perf_counter()

    def ms(a, b):
        return round((b - a) * 1000.0, 1)

    return {
        "version": contract.VERSION,
        "ok": True,
        "backend": request["backend"],
        "site_id": request["site"]["id"],
        "scene_id": request["scene"]["id"],
        "captured_at": request["scene"]["captured_at"],
        "metrics": analysis.metrics,
        "epsg": site.epsg,
        "window": list(site.window),
        "image_png_b64": image_b64,
        "build": info,
        "io": {"requests": site.requests, "bytes": site.bytes_read},
        "timings_ms": {
            "read": ms(t0, t1),
            "analyse": ms(t1, t2),
            "annotate": ms(t2, t3),
            "total": ms(started, t3),
        },
    }
