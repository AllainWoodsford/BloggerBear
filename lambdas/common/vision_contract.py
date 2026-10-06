"""The vision worker's request and response, shared by the worker and the pipeline's client.

docs/enhancements/opencv-agentic-vision-enhancement.md §2: the research tick's adapter (home
region) asks the vision worker (the vision region, next to the imagery) to measure one site in one
scene. This module is the contract between them and nothing else, so it imports no OpenCV and can
sit in the shared pipeline zip as well as the worker's.

A request:

    {"version": 1, "backend": "opencv" | "cool",
     "site": {"id": str, "polygon": [[lon, lat], ...]},
     "scene": {"id": str, "captured_at": iso str,
               "assets": {"nir": url, "green": url, "scl": url (optional)}},
     "params": {DetectParams overrides}, "coverage_floor": float, "image": bool}

A reply is either {"version": 1, "ok": true, ...measurement} or
{"version": 1, "ok": false, "error": <one of ERROR_CODES>, "detail": str}.

Asset URLs must start with an allowed prefix (by default the public `sentinel-cogs` bucket): the
worker fetches whatever URL it is given, so the list is what stops it being pointed anywhere else.
"""

from __future__ import annotations

import math
import re

VERSION = 1
BACKENDS = ("opencv", "cool")

# Sentinel-2 L2A COGs on AWS Open Data (us-west-2). Earth Search's STAC items link to this host.
DEFAULT_ALLOWED_URL_PREFIXES = ("https://sentinel-cogs.s3.us-west-2.amazonaws.com/",)

ERROR_CODES = (
    "bad_request",  # the request doesn't match this contract
    "backend_mismatch",  # asked for a backend this worker isn't
    "not_cool",  # this is the cool backend, but its OpenCV isn't the pinned COOL build
    "site_outside_scene",  # the polygon doesn't overlap the scene
    "window_too_large",  # the site is bigger than the worker will read
    "unreadable_scene",  # a band couldn't be fetched or decoded
    "internal",  # anything else
)

# What a request may override in vision.detect.DetectParams, with the allowed range of each.
PARAM_LIMITS = {
    "stretch_max": (100.0, 20000.0),
    "block_size": (3, 401),
    "offset": (1.0, 200.0),
    "min_length_m": (10.0, 2000.0),
    "max_length_m": (10.0, 2000.0),
    "min_elongation": (1.0, 20.0),
    "edge_buffer_px": (0, 20),
}

WHOLE_NUMBER_PARAMS = ("block_size", "edge_buffer_px")

MAX_POLYGON_VERTICES = 64
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ContractError(ValueError):
    """A request or reply that doesn't match the contract."""


def validate_request(
    request: object, allowed_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_URL_PREFIXES
) -> dict:
    """The request, normalised (defaults filled in), or ContractError naming the first problem."""
    if not isinstance(request, dict):
        raise ContractError("request must be an object")
    if request.get("version") != VERSION:
        raise ContractError(f"version must be {VERSION}")
    backend = request.get("backend", "opencv")
    if backend not in BACKENDS:
        raise ContractError(f"backend must be one of {BACKENDS}")

    site = request.get("site")
    if not isinstance(site, dict) or not _is_id(site.get("id")):
        raise ContractError("site.id is required")
    polygon = site.get("polygon")
    if not isinstance(polygon, list) or not 3 <= len(polygon) <= MAX_POLYGON_VERTICES:
        raise ContractError(f"site.polygon needs 3 to {MAX_POLYGON_VERTICES} [lon, lat] points")
    for point in polygon:
        if (
            not isinstance(point, list | tuple)
            or len(point) != 2
            or not all(_is_number(v) for v in point)
            or not -180 <= point[0] <= 180
            or not -80 <= point[1] <= 84
        ):
            raise ContractError("site.polygon points must be [lon, lat] inside UTM's latitudes")

    scene = request.get("scene")
    if not isinstance(scene, dict) or not _is_id(scene.get("id")):
        raise ContractError("scene.id is required")
    assets = scene.get("assets")
    if not isinstance(assets, dict) or not {"nir", "green"} <= set(assets):
        raise ContractError("scene.assets needs nir and green")
    for name, url in assets.items():
        if name not in ("nir", "green", "scl"):
            raise ContractError(f"unknown asset {name!r}")
        if not isinstance(url, str) or not url.startswith(tuple(allowed_prefixes)):
            raise ContractError(f"asset {name!r} is not at an allowed location")

    params = request.get("params") or {}
    if not isinstance(params, dict):
        raise ContractError("params must be an object")
    for key, value in params.items():
        if key not in PARAM_LIMITS:
            raise ContractError(f"unknown param {key!r}")
        lo, hi = PARAM_LIMITS[key]
        if not _is_number(value) or not lo <= value <= hi:
            raise ContractError(f"param {key!r} must be between {lo} and {hi}")
    for key in WHOLE_NUMBER_PARAMS:
        if key in params:
            if int(params[key]) != params[key]:
                raise ContractError(f"param {key!r} must be a whole number")
            params = {**params, key: int(params[key])}
    if params.get("block_size", 3) % 2 == 0:
        raise ContractError("param 'block_size' must be odd")

    coverage_floor = request.get("coverage_floor", 0.7)
    if not _is_number(coverage_floor) or not 0 <= coverage_floor <= 1:
        raise ContractError("coverage_floor must be between 0 and 1")

    return {
        "version": VERSION,
        "backend": backend,
        "site": {"id": site["id"], "polygon": [[float(x), float(y)] for x, y in polygon]},
        "scene": {
            "id": scene["id"],
            "captured_at": str(scene.get("captured_at") or ""),
            "assets": dict(assets),
        },
        "params": dict(params),
        "coverage_floor": float(coverage_floor),
        "image": bool(request.get("image", True)),
    }


def error(code: str, detail: str = "") -> dict:
    if code not in ERROR_CODES:
        code = "internal"
    return {"version": VERSION, "ok": False, "error": code, "detail": detail[:500]}


def validate_reply(reply: object) -> dict:
    """A worker reply checked for the fields the pipeline relies on; ContractError if malformed.
    An `ok: false` reply is valid (it is returned as is); the caller decides what it means."""
    if not isinstance(reply, dict) or reply.get("version") != VERSION:
        raise ContractError("not a vision worker reply")
    if not isinstance(reply.get("ok"), bool):
        raise ContractError("not a vision worker reply")
    if not reply["ok"]:
        if reply.get("error") not in ERROR_CODES:
            raise ContractError("unknown error code")
        return reply
    metrics = reply.get("metrics")
    if not isinstance(metrics, dict):
        raise ContractError("metrics missing")
    count, coverage = metrics.get("count"), metrics.get("coverage")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ContractError("metrics.count must be a whole number")
    if not _is_number(coverage) or not 0 <= coverage <= 1:
        raise ContractError("metrics.coverage must be between 0 and 1")
    if not isinstance(metrics.get("quality_flags"), list):
        raise ContractError("metrics.quality_flags missing")
    if reply.get("backend") not in BACKENDS:
        raise ContractError("backend missing")
    if not isinstance(reply.get("build"), dict):
        raise ContractError("build missing")
    image = reply.get("image_png_b64")
    if image is not None and not isinstance(image, str):
        raise ContractError("image_png_b64 must be a string")
    return reply


def _is_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
