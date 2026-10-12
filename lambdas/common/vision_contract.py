"""The vision worker's request and response, shared by the worker and the pipeline's client.

docs/enhancements/opencv-agentic-vision-enhancement.md §2 and docs/enhancements/rail-access-monitor.md
§3: the research tick's adapter (home region) asks the vision worker (the vision region, next to
the imagery) to measure one site in one scene. This module is the contract between them and
nothing else, so it imports no OpenCV and nothing from the pipeline, and can sit in the shared
pipeline zip as well as the worker's.

A request names a `task` (`TASKS`; "ships" when absent, so older callers are unchanged):

    {"version": 1, "task": "ships" | "rail_access", "backend": "opencv" | "cool",
     "site": {"id": str, "polygon": [[lon, lat], ...]},
     "scene": {"id": str, "captured_at": iso str, "assets": {name: url, ...}},
     "params": {per-task overrides}, "coverage_floor": float, "image": bool,
     "network": {...}}                       # rail_access only: the OpenStreetMap network

Each task says which assets it needs and may take, the bounds of its parameters, what else it
requires (`extras`) and which metrics its reply must carry. The ships task reads nir and green
(scl optional) and counts vessels; the rail task reads red, nir, swir16 and scl and is sent the
rail network (`validate_network`: the shape common/osm.py's `fetch_network` returns, under
`NETWORK_LIMITS`).

A reply is either {"version": 1, "ok": true, "task": ..., ...measurement} or
{"version": 1, "ok": false, "error": <one of ERROR_CODES>, "detail": str}.

Asset URLs must start with an allowed prefix (by default the public `sentinel-cogs` bucket): the
worker fetches whatever URL it is given, so the list is what stops it being pointed anywhere else.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

VERSION = 1
BACKENDS = ("opencv", "cool")
DEFAULT_TASK = "ships"

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


@dataclass(frozen=True)
class TaskSpec:
    """What one task asks for and answers with: the assets it needs and the others it may take,
    the bounds of its parameters and which of them are whole numbers, the extra request fields it
    requires, and the metrics every reply must carry (checked in `validate_reply`)."""

    assets_required: tuple[str, ...]
    assets_optional: tuple[str, ...]
    param_limits: dict[str, tuple[float, float]]
    whole_params: tuple[str, ...]
    extras: tuple[str, ...]
    reply_metrics: tuple[str, ...]


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
# vision.detect.DetectParams' default length band, repeated here because this module may not import
# the OpenCV side (a test in test_vision_worker.py holds the two together). A request that sets only
# one of the pair is checked against the other's default, so the worker never builds a DetectParams
# it would reject: that was an "internal" error where it should have been "bad_request".
DEFAULT_LENGTH_M = (60.0, 600.0)

# What a request may override in vision.rail_analyse.RailParams, with the allowed range of each
# (that module's PARAM_LIMITS; the same test holds the two equal).
RAIL_PARAM_LIMITS = {
    "ndbi_threshold": (-0.5, 0.5),
    "ndvi_max": (0.0, 1.0),
    "reach_m": (200.0, 5000.0),
    "heat_sigma_m": (100.0, 3000.0),
    "snap_m": (20.0, 1000.0),
    "hub_count": (1, 20),
    "intermodal_near_m": (50.0, 1000.0),
    "intermodal_far_m": (100.0, 2000.0),
    "min_desert_km2": (0.01, 100.0),
    "visibility_ndvi_max": (0.0, 1.0),
    "max_orbital_km": (1.0, 20.0),
}
# RailParams' default intermodal radii, for the same reason as DEFAULT_LENGTH_M: the far radius
# must not be under the near one, and a request may set only one of them.
DEFAULT_INTERMODAL_M = (300.0, 500.0)

TASKS = {
    "ships": TaskSpec(
        assets_required=("nir", "green"),
        assets_optional=("scl",),
        param_limits=PARAM_LIMITS,
        whole_params=WHOLE_NUMBER_PARAMS,
        extras=(),
        reply_metrics=("count", "coverage", "quality_flags"),
    ),
    "rail_access": TaskSpec(
        assets_required=("red", "nir", "swir16", "scl"),
        assets_optional=(),
        param_limits=RAIL_PARAM_LIMITS,
        whole_params=("hub_count",),
        extras=("network",),
        reply_metrics=("coverage", "station_count", "served_share", "quality_flags"),
    ),
}

# The most a rail network may hold (common/osm.py caps what it fetches to the same figures, so a
# fetched network always passes): with these the request stays near 1.5 MB, well under Lambda's
# 6 MB synchronous payload. `name_chars` is where a way or station name is cut.
NETWORK_LIMITS = {"points": 60_000, "ways": 5_000, "stations": 2_000, "pois": 5_000, "name_chars": 80}
# A station or point-of-interest kind is a short OpenStreetMap tag value ("station", "bus_stop").
MAX_KIND_CHARS = 32
# The longest network element id (vision/network.py's MAX_ID_CHARS, which bounds graph node ids; a
# test holds the two equal): an OpenStreetMap id is a letter and up to 16 digits.
MAX_NETWORK_ID_CHARS = 32
# network.fetched_at is a timestamp (an ISO one is 32 characters), not a place to put a document.
MAX_FETCHED_AT_CHARS = 64
# Where a lon/lat may be: on Earth, and inside UTM's latitudes (as a site polygon's points).
LON_RANGE, LAT_RANGE = (-180.0, 180.0), (-80.0, 84.0)

MAX_POLYGON_VERTICES = 64
# Matched whole (`fullmatch`): with `match` and `$`, a trailing newline would pass.
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# Characters that do not belong in a name shown in a caption or a prompt, by Unicode category:
# controls, formatting characters, surrogates, private use, unassigned, and the line and paragraph
# separators; the two joiners are formatting characters that Persian, Arabic and Indic names need.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# ZERO WIDTH NON-JOINER and ZERO WIDTH JOINER, written as escapes because the characters are invisible.
_KEPT_JOINERS = frozenset("\u200c\u200d")


class ContractError(ValueError):
    """A request or reply that doesn't match the contract."""


def validate_request(
    request: object, allowed_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_URL_PREFIXES
) -> dict:
    """The request, normalised (defaults filled in, the task named, the network checked), or
    ContractError naming the first problem."""
    if not isinstance(request, dict):
        raise ContractError("request must be an object")
    if request.get("version") != VERSION:
        raise ContractError(f"version must be {VERSION}")
    backend = request.get("backend", "opencv")
    if backend not in BACKENDS:
        raise ContractError(f"backend must be one of {BACKENDS}")
    task = request.get("task", DEFAULT_TASK)
    if not isinstance(task, str) or task not in TASKS:  # a list or dict is unhashable: no TypeError
        raise ContractError(f"task must be one of {tuple(TASKS)}")
    spec = TASKS[task]

    site = request.get("site")
    if not isinstance(site, dict) or not _is_id(site.get("id")):
        raise ContractError("site.id is required")
    polygon = site.get("polygon")
    if not isinstance(polygon, list) or not 3 <= len(polygon) <= MAX_POLYGON_VERTICES:
        raise ContractError(f"site.polygon needs 3 to {MAX_POLYGON_VERTICES} [lon, lat] points")
    for point in polygon:
        if not _is_lonlat(point):
            raise ContractError("site.polygon points must be [lon, lat] inside UTM's latitudes")

    scene = request.get("scene")
    if not isinstance(scene, dict) or not _is_id(scene.get("id")):
        raise ContractError("scene.id is required")
    assets = scene.get("assets")
    if not isinstance(assets, dict) or not set(spec.assets_required) <= set(assets):
        raise ContractError(f"scene.assets needs {_listed(spec.assets_required)}")
    for name, url in assets.items():
        if name not in spec.assets_required + spec.assets_optional:
            raise ContractError(f"unknown asset {name!r} for task {task!r}")
        if not isinstance(url, str) or not url.startswith(tuple(allowed_prefixes)):
            raise ContractError(f"asset {name!r} is not at an allowed location")

    params = _validate_params(request.get("params") or {}, task, spec)

    network = None
    if "network" in spec.extras:
        network = validate_network(request.get("network"))
    elif request.get("network") is not None:
        raise ContractError(f"task {task!r} takes no network")

    coverage_floor = request.get("coverage_floor", 0.7)
    if not _is_number(coverage_floor) or not 0 <= coverage_floor <= 1:
        raise ContractError("coverage_floor must be between 0 and 1")

    normalised = {
        "version": VERSION,
        "task": task,
        "backend": backend,
        "site": {"id": site["id"], "polygon": [[float(x), float(y)] for x, y in polygon]},
        "scene": {
            "id": scene["id"],
            "captured_at": str(scene.get("captured_at") or ""),
            "assets": dict(assets),
        },
        "params": params,
        "coverage_floor": float(coverage_floor),
        "image": bool(request.get("image", True)),
    }
    if network is not None:
        normalised["network"] = network
    return normalised


def _validate_params(params: object, task: str, spec: TaskSpec) -> dict:
    """The task's parameter overrides checked against its limits, whole numbers made int, and the
    pairs that must be ordered checked against each other or the other's default."""
    if not isinstance(params, dict):
        raise ContractError("params must be an object")
    for key, value in params.items():
        if key not in spec.param_limits:
            raise ContractError(f"unknown param {key!r} for task {task!r}")
        lo, hi = spec.param_limits[key]
        if not _is_number(value) or not lo <= value <= hi:
            raise ContractError(f"param {key!r} must be between {lo} and {hi}")
    for key in spec.whole_params:
        if key in params:
            if int(params[key]) != params[key]:
                raise ContractError(f"param {key!r} must be a whole number")
            params = {**params, key: int(params[key])}
    if task == "ships":
        if params.get("block_size", 3) % 2 == 0:
            raise ContractError("param 'block_size' must be odd")
        if params.get("min_length_m", DEFAULT_LENGTH_M[0]) > params.get("max_length_m", DEFAULT_LENGTH_M[1]):
            raise ContractError("param 'min_length_m' must not exceed 'max_length_m'")
    elif task == "rail_access":
        near = params.get("intermodal_near_m", DEFAULT_INTERMODAL_M[0])
        far = params.get("intermodal_far_m", DEFAULT_INTERMODAL_M[1])
        if far < near:
            raise ContractError("param 'intermodal_far_m' must be at least 'intermodal_near_m'")
    return dict(params)


def validate_network(network: object) -> dict:
    """The rail network in the shape common/osm.py's `fetch_network` returns, normalised: ids as
    strings, names sanitised (control and formatting characters out, whitespace collapsed, cut at
    NETWORK_LIMITS["name_chars"]; a nameless station is "Unnamed station (<id>)"), every lon/lat
    checked as a site polygon's points are, ids at most MAX_NETWORK_ID_CHARS, `tunnel` a bool,
    `fetched_at` a string of at most MAX_FETCHED_AT_CHARS or None. Only
    `ways`, `stations`, `pois` and `fetched_at` are kept (the fetch's `source`, `counts` and the
    like are the adapter's). ContractError on anything else, and on a list over NETWORK_LIMITS."""
    if not isinstance(network, dict):
        raise ContractError("network must be an object with ways, stations and pois")
    lists = {}
    for key in ("ways", "stations", "pois"):
        items = network.get(key)
        if not isinstance(items, list):
            raise ContractError(f"network.{key} must be a list")
        if len(items) > NETWORK_LIMITS[key]:
            raise ContractError(f"network.{key} has more than {NETWORK_LIMITS[key]} entries")
        lists[key] = items

    ways, points = [], 0
    for way in lists["ways"]:
        if not isinstance(way, dict):
            raise ContractError("network.ways entries must be objects")
        raw_points = way.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            raise ContractError("a network way needs at least 2 [lon, lat] points")
        points += len(raw_points)
        if points > NETWORK_LIMITS["points"]:
            raise ContractError(f"network.ways have more than {NETWORK_LIMITS['points']} points in all")
        tunnel = way.get("tunnel", False)
        if not isinstance(tunnel, bool):
            raise ContractError("a network way's tunnel must be true or false")
        ways.append(
            {
                "id": _element_id(way, "way"),
                "points": [_lonlat(point, "a network way point") for point in raw_points],
                "tunnel": tunnel,
                "name": _clean_name(way.get("name")),
            }
        )

    stations = []
    for station in lists["stations"]:
        if not isinstance(station, dict):
            raise ContractError("network.stations entries must be objects")
        sid = _element_id(station, "station")
        lon, lat = _lonlat((station.get("lon"), station.get("lat")), "a network station")
        stations.append(
            {
                "id": sid,
                "name": _clean_name(station.get("name")) or f"Unnamed station ({sid})",
                "lon": lon,
                "lat": lat,
                "kind": _kind(station.get("kind", "station"), "station"),
            }
        )

    pois = []
    for poi in lists["pois"]:
        if not isinstance(poi, dict):
            raise ContractError("network.pois entries must be objects")
        lon, lat = _lonlat((poi.get("lon"), poi.get("lat")), "a network point of interest")
        pois.append(
            {
                "id": _element_id(poi, "point of interest"),
                "lon": lon,
                "lat": lat,
                "kind": _kind(poi.get("kind"), "point of interest"),
            }
        )

    fetched_at = network.get("fetched_at")
    if fetched_at is not None and (not isinstance(fetched_at, str) or len(fetched_at) > MAX_FETCHED_AT_CHARS):
        raise ContractError(f"network.fetched_at must be a string of at most {MAX_FETCHED_AT_CHARS} chars")
    return {"ways": ways, "stations": stations, "pois": pois, "fetched_at": fetched_at}


def error(code: str, detail: str = "") -> dict:
    if code not in ERROR_CODES:
        code = "internal"
    return {"version": VERSION, "ok": False, "error": code, "detail": detail[:500]}


def validate_reply(reply: object) -> dict:
    """A worker reply checked for the fields the pipeline relies on, per the reply's task;
    ContractError if malformed. An `ok: false` reply is valid (it is returned as is); the caller
    decides what it means."""
    if not isinstance(reply, dict) or reply.get("version") != VERSION:
        raise ContractError("not a vision worker reply")
    if not isinstance(reply.get("ok"), bool):
        raise ContractError("not a vision worker reply")
    if not reply["ok"]:
        if reply.get("error") not in ERROR_CODES:
            raise ContractError("unknown error code")
        return reply
    task = reply.get("task", DEFAULT_TASK)
    if not isinstance(task, str) or task not in TASKS:
        raise ContractError("unknown task in the reply")
    metrics = reply.get("metrics")
    if not isinstance(metrics, dict):
        raise ContractError("metrics missing")
    for name in TASKS[task].reply_metrics:
        _REPLY_METRIC_CHECKS[name](name, metrics.get(name))
    if reply.get("backend") not in BACKENDS:
        raise ContractError("backend missing")
    if not isinstance(reply.get("build"), dict):
        raise ContractError("build missing")
    image = reply.get("image_png_b64")
    if image is not None and not isinstance(image, str):
        raise ContractError("image_png_b64 must be a string")
    return reply


def _whole_metric(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(f"metrics.{name} must be a whole number")


def _share_metric(name: str, value: object) -> None:
    if not _is_number(value) or not 0 <= value <= 1:
        raise ContractError(f"metrics.{name} must be between 0 and 1")


def _list_metric(name: str, value: object) -> None:
    if not isinstance(value, list):
        raise ContractError(f"metrics.{name} missing")


# How each metric a task's reply must carry is checked: counts are whole numbers, coverage and
# shares lie in [0, 1], the quality flags are a list.
_REPLY_METRIC_CHECKS = {
    "count": _whole_metric,
    "station_count": _whole_metric,
    "coverage": _share_metric,
    "served_share": _share_metric,
    "quality_flags": _list_metric,
}


def _listed(names: tuple[str, ...]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _is_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_lonlat(point: object) -> bool:
    return (
        isinstance(point, list | tuple)
        and len(point) == 2
        and all(_is_number(v) for v in point)
        and LON_RANGE[0] <= point[0] <= LON_RANGE[1]
        and LAT_RANGE[0] <= point[1] <= LAT_RANGE[1]
    )


def _lonlat(point: object, what: str) -> list[float]:
    if not _is_lonlat(point):
        raise ContractError(f"{what} must be [lon, lat] inside UTM's latitudes")
    return [float(point[0]), float(point[1])]


def _element_id(element: dict, what: str) -> str:
    """The element's `id` as a string: OpenStreetMap's "n123" / "w45", or a number; at most
    `MAX_NETWORK_ID_CHARS` long."""
    value = element.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not _is_id(value) or len(value) > MAX_NETWORK_ID_CHARS:
        raise ContractError(f"a network {what} needs an id of at most {MAX_NETWORK_ID_CHARS} characters")
    return value


def _kind(value: object, what: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_KIND_CHARS or not _ID.fullmatch(value):
        raise ContractError(f"a network {what} needs a short kind")
    return value


def _clean_name(raw: object) -> str | None:
    """`raw` fit to store and show, or None when it is not a string or nothing usable is left."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    text = "".join(
        ch for ch in text if ch in _KEPT_JOINERS or unicodedata.category(ch) not in _DROPPED_CATEGORIES
    ).strip()
    if len(text) > NETWORK_LIMITS["name_chars"]:
        text = text[: NETWORK_LIMITS["name_chars"]].rstrip()
    return text or None
