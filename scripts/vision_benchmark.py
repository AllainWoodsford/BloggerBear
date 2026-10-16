#!/usr/bin/env python3
"""Benchmark the vision worker's OpenCV work: stock OpenCV 5 against COOL, x86 against Graviton.

The COOL award asks for *measured* value against a baseline (docs/enhancements/
opencv-agentic-vision-enhancement.md §4). This script is how those numbers are made, the same way
on every machine, so the rows of the report compare like with like:

    python scripts/vision_benchmark.py kernels                    # the OpenCV operations alone
    python scripts/vision_benchmark.py scenes                     # whole sites, read over HTTPS
    python scripts/vision_benchmark.py worker --arn <ARN>         # a deployed worker, end to end
    python scripts/vision_benchmark.py compare A.json B.json      # two runs: speed-up, parity
    python scripts/vision_benchmark.py controls --cool-sha <hex>  # the COOL guard's negative controls
    python scripts/vision_benchmark.py fetch-network --scene NAME # a rail scene's OSM network file

`kernels` times the operations the worker spends its CPU on, for both tasks (resize, Gaussian
blur, adaptive Gaussian threshold, morphology, connected components, contours, the affine warp
and the whole per-site ship analysis; the distance transform, hit-or-miss thinning, polylines,
the colour map, the desert components and the whole rail access analysis, with that analysis's
own stage timings, so the Amdahl split is measured) on fixed synthetic images, so network and
data never enter it. Run it where each build lives: stock `opencv-python-headless` on an x86
machine and on Graviton, then inside OpenCV's COOL image on Graviton3/4. `scenes` runs the worker
handler in-process on the pinned scene list (scripts/vision_benchmark_scenes.json), with the read
time reported apart from the OpenCV time, and records what the worker measured: the rail metrics,
their digest, the figure's hash and shape, every timing sample, and the peak memory; a rail scene
needs its `network_file`, which `fetch-network` writes from Overpass (the file is not committed
unless it could be fetched: Overpass is not reachable from every build container). `worker`
invokes a deployed worker (every site id prefixed `bench-`, so production's numbers stay apart)
and adds the round trip as the caller sees it. `compare` puts two runs of one kind, from one
machine and the same inputs, in a ratio with a bootstrap interval, and checks the rail metrics
agree (parity); `controls` shows the COOL guard refusing what it must.

Every report records which OpenCV build ran (vision/build.py's fingerprint: that is what proves a
row is COOL) and which CPU (`cpu`, from /proc/cpuinfo: Graviton2 is Neoverse N1, Graviton3 V1,
Graviton4 V2). `--out FILE` (or `--out DIR/`, then `DIR/<run_id>.json`) writes a
`bloggerbear.vision-eval/1` run file (scripts/vision_runs.py: the code's git sha, the environment,
the inputs' ETags, the parameters, the results, the checks and a flat `summary`); a Markdown
table is always printed.

Options that shape a run: `--threads N` (`cv2.setNumThreads`, recorded); `--pin-cpu K` (one core,
Linux only; elsewhere pin with the OS's own tool, the affinity is recorded either way); `--only
NAME` (repeatable: these scenes only); `--isolate` (each scene in a fresh interpreter, so its peak
memory is its own); `--cache DIR` (every range read answered from the cache after the first, so
repeats and both backends read identical bytes); `--imds` (the EC2 instance identity, five fields,
nothing else); `--label k=v` (the COOL product's name, version, delivery, digest, price).

The in-process modes (`scenes`, `controls`) set the handler's environment before calling it:
`VISION_BACKEND` to `--backend`, and `COOL_BUILD_SHA256` to `--cool-sha`, which `--backend cool`
requires (vision_worker_handler.py reads both at call time). A `cool` run on any other build is
answered `not_cool` by the handler itself, and that row is what gets recorded.

Needs requirements-vision.txt installed (and boto3 for `worker`). Reads only public data.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import vision_runs

ROOT = Path(__file__).resolve().parents[1]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

SCENES_FILE = Path(__file__).with_name("vision_benchmark_scenes.json")

# ARM's CPU part numbers (/proc/cpuinfo "CPU part") for the cores AWS's Graviton generations use.
ARM_PARTS = {
    "0xd0c": "Neoverse N1 (Graviton2)",
    "0xd40": "Neoverse V1 (Graviton3)",
    "0xd4f": "Neoverse V2 (Graviton4)",
    "0xd84": "Neoverse V3 (Graviton5?)",
}

# The synthetic city's grid: 20 m pixels in UTM zone 56S, as the rail task reads a scene.
CITY_TRANSFORM = (300000.0, 20.0, 0.0, 6300000.0, 0.0, -20.0)
CITY_EPSG, CITY_PIXEL_M = 32756, 20.0
# What a missing rail network file is reported as, per scene, in the scenes and worker modes.
NETWORK_FILE_MISSING = "network_file missing"
# The evaluation's manifest: its ETags pin the scenes the benchmark shares with it.
MANIFEST_FILE = Path(__file__).with_name("vision_eval_manifest.json")
# The handler's stages that are OpenCV's work: the headline speed-up is over these. `graph` is a
# part of `analyse` (the handler reports it from the analysis's own timings), so it is not added
# again; `read` is the network, reported apart.
COMPUTE_STAGES = ("analyse", "annotate")
# The rail metrics a run records per scene, and what `compare` holds them to: served share to
# 1e-4 absolute, areas to 1e-3 relative (the metrics are rounded to 4 places by the worker), every
# count, hub, flag and suggestion identical.
RAIL_SCALARS = (
    "served_share", "desert_km2", "built_up_km2", "station_count", "snapped_station_count",
    "node_count", "edge_count", "components",
)  # fmt: skip
PARITY_ABS = {"served_share": 1e-4}
PARITY_REL = {"desert_km2": 1e-3, "built_up_km2": 1e-3}
TOP_SUGGESTIONS = 3
# IMDSv2: a token first, then the identity document, each with a 1 s connect and read timeout, so
# a machine that is not an EC2 instance answers "no host" in about a second.
IMDS_BASE = "http://169.254.169.254"
IMDS_TIMEOUT_SECONDS = 1.0
# The identity document's fields a judge needs, and the names they are stored under. Everything
# else (accountId, instanceId, privateIp, ...) is dropped before anything is stored: gitleaks
# flags account ids, and none of it says what ran.
IMDS_KEEP = {
    "instanceType": "instance_type",
    "imageId": "ami_id",
    "region": "region",
    "architecture": "architecture",
    "marketplaceProductCodes": "marketplace_product_codes",
}
# `--label k=v`: what the Marketplace product was, stored under environment.cool.
LABEL_KEYS = {
    "cool_product": "product",
    "cool_version": "version",
    "cool_delivery": "delivery",
    "image_digest": "image_digest",
    "price": "price",
}
DELIVERIES = ("ami", "container")
# The shared objects that are the OpenCV work: the cv2 extension, OpenCV's own libraries and
# KleidiCV's. Their path, size, sha256 and owning package say which binaries ran.
LIBRARY_PATTERN = re.compile(r"/(cv2[^/]*\.so[^/]*|libopencv_[^/]*|libkleidicv[^/]*)$")
# The build-information lines that tell a vendor build apart.
BUILD_LINE_PREFIXES = ("C++ Compiler:", "C++ flags", "C Compiler:", "C flags")
BUILD_LINE_WORDS = re.compile(r"kleidicv|-mcpu|-march|\b(cool|graviton|vendor)\b", re.IGNORECASE)
# The negative controls: name, the request's backend, whether the pin is set, the expected code
# (None: `ok` when this build is the pinned one, else `not_cool`).
CONTROLS = (
    ("cool_with_pin", "cool", True, None),
    ("cool_without_pin", "cool", False, "not_cool"),
    ("opencv_request_to_cool_backend", "opencv", True, "backend_mismatch"),
)


class CompareRefused(Exception):
    """Two runs that must not be put in a ratio: different kinds, machines or inputs."""


def cpu_identity(cpuinfo: str | None = None, machine: str | None = None) -> str:
    """A readable name for the CPU: the x86 model name, or the Arm core and Graviton generation."""
    machine = machine or platform.machine()
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            cpuinfo = ""
    for line in cpuinfo.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "model name" and value:
            return value
        if key == "cpu part" and value:
            return ARM_PARTS.get(value.lower(), f"Arm core {value}")
    return machine or "unknown"


def summarise(samples_ms: list[float]) -> dict:
    ordered = sorted(samples_ms)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {
        "runs": len(ordered),
        "median_ms": round(statistics.median(ordered), 3),
        "p90_ms": round(p90, 3),
        "min_ms": round(ordered[0], 3),
    }


def time_it(fn: Callable[[], object], repeats: int, warmup: int = 1) -> dict:
    """The summary of `repeats` timed calls after `warmup` untimed ones, with the raw samples
    (`samples_ms`, to 3 places) kept beside it so `compare` can bootstrap."""
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return {**summarise(samples), "samples_ms": [round(s, 3) for s in samples]}


def synthetic_scene(size: int, seed: int = 0):
    """Water with noise, a coastline, a haze gradient and planted ships: deterministic."""
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    nir = (250 + rng.normal(0, 25, (size, size))).clip(1)
    nir += np.linspace(0, 400, size)[None, :]
    green = 900 + rng.normal(0, 25, (size, size))
    nir[:, : size // 8] = 2600
    green[:, : size // 8] = 700
    for _ in range(max(8, size // 32)):
        cx, cy = rng.uniform(size * 0.2, size * 0.95, 2)
        box = cv2.boxPoints(((cx, cy), (rng.uniform(10, 35), rng.uniform(3, 6)), rng.uniform(0, 180)))
        mask = np.zeros((size, size), np.uint8)
        cv2.fillPoly(mask, [box.astype(np.int32)], 255)
        nir[mask > 0], green[mask > 0] = 2800, 800
    return nir.astype(np.uint16), green.astype(np.uint16)


def synthetic_city(size: int, seed: int = 0) -> dict:
    """The rail task's bands (`red`, `nir`, `swir16`, `scl`) of a city at 20 m: canopy everywhere,
    a built district over the middle half, a lake (SCL 6) inside it and a cloud (SCL 9) in one
    corner, with mild noise: deterministic."""
    import numpy as np

    rng = np.random.default_rng(seed)
    district = (slice(size // 4, 3 * size // 4), slice(size // 4, 3 * size // 4))
    bands = {}
    # (canopy, roofs) reflectance x 10000: NDVI 0.67 / 0.09, NDBI -0.43 / 0.14.
    for name, canopy, roofs in (("red", 600, 1500), ("nir", 3000, 1800), ("swir16", 1200, 2400)):
        band = np.full((size, size), float(canopy))
        band[district] = roofs
        band += rng.normal(0, 30, (size, size))
        bands[name] = band.clip(1).astype(np.uint16)
    scl = np.full((size, size), 4, np.uint8)
    scl[district] = 5
    scl[size // 2 : size // 2 + size // 10, size // 3 : size // 3 + size // 10] = 6
    scl[: size // 10, 3 * size // 4 :] = 9
    bands["scl"] = scl
    return bands


def synthetic_network(size: int, lines: int = 4, stations_per_line: int = 5) -> dict:
    """A rail network over `synthetic_city(size)`, in the shape the worker takes: `lines`
    horizontal and `lines` vertical ways across the city (the first vertical one in tunnel),
    `stations_per_line` stations on each (40 by default, none on a junction) and a bus stop
    beside every station. Lon/lat come from pixels through the UTM inverse, as OpenStreetMap's
    would, so the projection is part of what is timed."""
    from vision import geo

    def lonlat(x, y):
        return list(geo.pixel_to_lonlat(CITY_TRANSFORM, CITY_EPSG, x, y))

    margin = size // 8
    span = size - 2 * margin
    positions = [margin + span * i // max(1, lines - 1) for i in range(lines)]
    ways, stations, pois = [], [], []
    for i, p in enumerate(positions):
        ways.append({"id": f"wh{i}", "points": [lonlat(margin, p), lonlat(size - margin, p)],
                     "tunnel": False, "name": f"Line H{i}"})  # fmt: skip
        ways.append({"id": f"wv{i}", "points": [lonlat(p, margin), lonlat(p, size - margin)],
                     "tunnel": i == 0, "name": f"Line V{i}"})  # fmt: skip
        for j in range(stations_per_line):
            along = margin + span * (2 * j + 1) // (2 * stations_per_line)
            for sid, (x, y) in ((f"nh{i}_{j}", (along, p)), (f"nv{i}_{j}", (p, along))):
                lon, lat = lonlat(x, y)
                stations.append({"id": sid, "name": f"Station {sid}", "kind": "station", "lon": lon,
                                 "lat": lat})  # fmt: skip
                lon, lat = lonlat(x + 2, y + 2)
                pois.append({"id": f"b{sid}", "kind": "bus_stop", "lon": lon, "lat": lat})
    return {"ways": ways, "stations": stations, "pois": pois, "fetched_at": "2026-01-01T00:00:00+00:00"}


def kernels(size: int = 2048, repeats: int = 20) -> dict:
    """Every operation's timing summary with its samples; `analyse_rail_access` also carries the
    analysis's own stage timings (`stages`: masks, stations, ..., graph, metrics), from
    `layers["timings"]` of each timed call."""
    import cv2
    import numpy as np
    from vision import network as rail_network
    from vision.analyse import analyse_site
    from vision.detect import DetectParams, stretch
    from vision.rail_analyse import RailParams, analyse_rail_access, project_network

    nir, green = synthetic_scene(size)
    image = stretch(nir, 3000.0)
    binary = cv2.adaptiveThreshold(image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, -25)
    kernel = np.ones((3, 3), np.uint8)
    matrix = np.array([[0.5, 0.0, 0.25], [0.0, 0.5, 0.25]])
    site = [[[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]]]
    params = DetectParams()

    # The rail task's inputs: the city, its network in pixels, the rasters the stages work on.
    city, network = synthetic_city(size), synthetic_network(size)
    projected = project_network(network, CITY_EPSG, CITY_TRANSFORM, (size, size))
    way_pts = [np.round(np.asarray(way)).astype(np.int32).reshape(-1, 1, 2) for way in projected["ways_px"]]
    rails = rail_network.rasterise_ways((size, size), projected["ways_px"] + projected["tunnels_px"])
    not_station = np.ones((size, size), np.uint8)
    for station in projected["stations_px"]:
        if station is not None:
            not_station[int(round(station[1])), int(round(station[0]))] = 0
    built = (city["scl"] == 5).astype(np.uint8)
    heat_u8 = np.clip(cv2.GaussianBlur(built.astype(np.float32), (0, 0), 25) * 255, 0, 255).astype(np.uint8)
    far = cv2.distanceTransform(not_station, cv2.DIST_L2, 5) > 50
    desert = (built * 255 * far).astype(np.uint8)
    rail_params = RailParams()
    rail_timings: list[dict] = []

    def rail_access():
        analysis = analyse_rail_access(
            city, site, CITY_PIXEL_M, CITY_TRANSFORM, CITY_EPSG, network, params=rail_params
        )
        rail_timings.append(analysis.layers["timings"])
        return analysis

    cases = {
        "resize_area_half": lambda: cv2.resize(image, (size // 2, size // 2), interpolation=cv2.INTER_AREA),
        "gaussian_blur_5": lambda: cv2.GaussianBlur(image, (5, 5), 0),
        "adaptive_threshold_gaussian_31": lambda: cv2.adaptiveThreshold(
            image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, -25
        ),
        "morphology_open_3": lambda: cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel),
        "connected_components_stats": lambda: cv2.connectedComponentsWithStats(binary, connectivity=8),
        "find_contours": lambda: cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE),
        "warp_affine_nearest_u16": lambda: cv2.warpAffine(
            nir, matrix, (size, size), flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP
        ),
        "analyse_site": lambda: analyse_site(nir, green, site, 10.0, params=params),
        "distance_transform_l2": lambda: cv2.distanceTransformWithLabels(
            not_station, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_CCOMP
        ),
        "thinning_hitmiss": lambda: rail_network.thin(rails),
        "polylines": lambda: cv2.polylines(
            np.zeros((size, size), np.uint8), way_pts, False, 255, 1, cv2.LINE_8
        ),
        "apply_colormap": lambda: cv2.applyColorMap(heat_u8, cv2.COLORMAP_INFERNO),
        "connected_components_stats_desert": lambda: cv2.connectedComponentsWithStats(desert, connectivity=8),
        "analyse_rail_access": rail_access,
    }
    results = {name: time_it(fn, repeats) for name, fn in cases.items()}
    timed = rail_timings[-repeats:]  # the warm-up call's timings are not samples
    stages = {}
    for stage in (timed[0] if timed else {}):
        values = [round(float(t.get(stage, 0.0)), 3) for t in timed]
        stages[stage] = {**summarise(values), "samples_ms": values}
    results["analyse_rail_access"]["stages"] = stages
    return results


def load_scenes(path: Path = SCENES_FILE) -> list[dict]:
    """The pinned scenes. A scene's `network_file` is relative to the scenes file; it is made
    absolute here so the request builders need not know where the list came from."""
    scenes = json.loads(path.read_text(encoding="utf-8"))["scenes"]
    for scene in scenes:
        if scene.get("network_file"):
            scene["network_file"] = str(path.parent / scene["network_file"])
    return scenes


def select_scenes(scene_list: list[dict], only: list[str] | None) -> list[dict]:
    """The scenes named in `only`, in the list's order; ValueError naming any that is not there."""
    if not only:
        return scene_list
    names = {scene["name"] for scene in scene_list}
    missing = [name for name in only if name not in names]
    if missing:
        raise ValueError(f"no scene named {missing} in the scenes file (it has {sorted(names)})")
    return [scene for scene in scene_list if scene["name"] in only]


def load_network(scene: dict) -> dict:
    """The scene's OpenStreetMap network from its `network_file` (as `fetch-network` wrote it);
    FileNotFoundError, saying so, when the scene names none or the file is not there."""
    path = scene.get("network_file")
    if not path or not Path(path).is_file():
        raise FileNotFoundError(NETWORK_FILE_MISSING)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def request_for(scene: dict, backend: str, network: dict | None = None) -> dict:
    """The worker request for a pinned scene, with its task. A rail scene carries its network:
    `network` when one is given, else the scene's network file."""
    from common import vision_contract

    task = scene.get("task", vision_contract.DEFAULT_TASK)
    request = {
        "version": 1,
        "task": task,
        "backend": backend,
        "site": {"id": scene["site_id"], "polygon": scene["polygon"]},
        "scene": {
            "id": scene["scene_id"],
            "captured_at": scene.get("captured_at", ""),
            "assets": scene["assets"],
        },
        "params": scene.get("params", {}),
        "image": True,
    }
    spec = vision_contract.TASKS.get(task)
    if spec is not None and "network" in spec.extras:
        request["network"] = network if network is not None else load_network(scene)
    return request


# --- what a scene run records ----------------------------------------------------------------------


def flag_pairs(flags: list[dict]) -> list[list[str]]:
    """Each flag as `[type, station]`; a `ghost_line`'s chain of stations joined with `+`."""
    out = []
    for flag in flags:
        station = flag.get("station") or "+".join(flag.get("stations") or [])
        out.append([str(flag.get("type")), str(station)])
    return out


def suggestion_ids(suggestions: list[dict], top: int = TOP_SUGGESTIONS) -> list[str]:
    """The top suggestions as ids: `orbital_link:<from>><to>`, `feeder_corridor:<station>><hub>`."""
    out = []
    for item in suggestions[:top]:
        kind = item.get("type")
        if kind == "orbital_link":
            out.append(f"{kind}:{item.get('from')}>{item.get('to')}")
        elif kind == "feeder_corridor":
            out.append(f"{kind}:{item.get('station')}>{item.get('hub')}")
        else:
            out.append(f"{kind}:{item.get('station', '')}")
    return out


def metrics_record(metrics: dict, task: str) -> dict:
    """What a run keeps of the worker's metrics: the rail scalars, the hub ids, the flags as
    (type, station) pairs and the top suggestions' ids; a ship scene keeps its `count`."""
    if task != "rail_access":
        return {"count": metrics.get("count")}
    record = {key: metrics.get(key) for key in RAIL_SCALARS}
    record["hubs"] = list(metrics.get("hubs") or [])
    record["flags"] = flag_pairs(metrics.get("flags") or [])
    record["suggestions"] = suggestion_ids(metrics.get("suggestions") or [])
    return record


def decode_image(png_b64: str | None) -> tuple[bytes | None, str | None, list[int] | None]:
    """The reply's figure: its PNG bytes, their sha256 and the decoded image's shape."""
    if not png_b64:
        return None, None, None
    import cv2
    import numpy as np

    png = base64.b64decode(png_b64)
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    return png, vision_runs.sha256_bytes(png), (list(image.shape) if image is not None else None)


def scene_row(last: dict, stages: dict[str, list[float]]) -> dict:
    """One scene's row from its last reply and the per-stage samples of every repeat."""
    task = last.get("task", "ships")
    metrics = last.get("metrics") or {}
    record = metrics_record(metrics, task)
    png, png_sha, shape = decode_image(last.get("image_png_b64"))
    return {
        "ok": True,
        "error": None,
        "count": metrics.get("count"),
        "io": last.get("io"),
        "stages": {stage: summarise(values) for stage, values in stages.items()},
        "samples_ms": {stage: [round(v, 3) for v in values] for stage, values in stages.items()},
        "metrics": record,
        "metrics_digest": vision_runs.digest(record),
        "image_png_sha256": png_sha,
        "image_shape": shape,
        "figure": None,
        "png": png,
        "build": last.get("build"),
        "peak_rss_mb": vision_runs.peak_rss_mb(),
        "peak_scope": "process",
    }


def scenes(
    scene_list: list[dict], repeats: int, backend: str, handler=None, fetch=None, isolate=None
) -> dict:
    """The worker handler in-process, per scene: its own read/analyse/annotate/total timings (and
    the rail task's graph stage), the samples, the metrics it measured and the figure's hash. With
    `isolate` (a callable taking the scene), each scene's row comes from a fresh process."""
    if handler is None:
        import vision_worker_handler

        handler = vision_worker_handler.lambda_handler
    out = {}
    for scene in scene_list:
        if isolate is not None:
            out[scene["name"]] = isolate(scene)
            continue
        try:
            request = request_for(scene, backend)
        except FileNotFoundError as exc:
            out[scene["name"]] = {"ok": False, "error": str(exc), "count": None, "io": None, "stages": {}}
            continue
        stages: dict[str, list[float]] = {}
        last = None
        for _ in range(repeats):
            reply = handler(request, None, **({"fetch": fetch} if fetch else {}))
            if not reply.get("ok"):
                last = reply
                break
            last = reply
            for stage, ms in reply["timings_ms"].items():
                stages.setdefault(stage, []).append(ms)
        if last and last.get("ok"):
            out[scene["name"]] = scene_row(last, stages)
        else:
            out[scene["name"]] = {
                "ok": False,
                "error": (last or {}).get("error"),
                "count": None,
                "io": None,
                "stages": {},
            }
    return out


def _run_child(argv: list[str]) -> tuple[int, int | None]:
    """Run `argv` to its end: the exit code and the child's own `ru_maxrss` (through `os.wait4`,
    the per-child form of `RUSAGE_CHILDREN`); None where the OS has no `wait4` (Windows)."""
    proc = subprocess.Popen(argv)
    if hasattr(os, "wait4"):
        _, status, usage = os.wait4(proc.pid, 0)
        proc.returncode = os.waitstatus_to_exitcode(status)
        return proc.returncode, usage.ru_maxrss
    return proc.wait(), None


def isolate_scene(scene: dict, child_args: list[str], run_child=None, python: str | None = None) -> dict:
    """One scene in a fresh interpreter: this script, `child_args` (the run's own arguments) plus
    `--only NAME --out <tmp>`. The row is the child's, with the child's peak resident set as the
    scene's own (`peak_scope: scene`); its figure is carried back as bytes."""
    run_child = run_child or _run_child
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "child.json"
        argv = [python or sys.executable, str(Path(__file__).resolve()), *child_args]
        argv += ["--only", scene["name"], "--out", str(out)]
        code, maxrss = run_child(argv)
        failed = {"ok": False, "error": None, "count": None, "io": None, "stages": {}}
        if code != 0 or not out.is_file():
            return {**failed, "error": f"isolated run exited {code}"}
        row = vision_runs.load(out)["results"].get(scene["name"])
        if row is None:
            return {**failed, "error": "isolated run has no row"}
        figure = row.pop("figure", None)
        if figure and (out.parent / figure).is_file():
            row["png"] = (out.parent / figure).read_bytes()
        if row.get("ok"):
            row["figure"] = None
            if maxrss is not None:
                row["peak_rss_mb"] = vision_runs.rss_to_mb(maxrss)
            row["peak_scope"] = "scene"
        return row


def child_args(argv: list[str]) -> list[str]:
    """The parent's arguments for an isolated child: without `--isolate`, and without the
    `--only`, `--out` and `--tag` the parent adds its own of."""
    out, skip = [], False
    for item in argv:
        if skip:
            skip = False
            continue
        if item == "--isolate":
            continue
        if item in ("--only", "--out", "--tag"):
            skip = True
            continue
        if item.startswith(("--only=", "--out=", "--tag=")):
            continue
        out.append(item)
    return out


def worker(arn: str, scene_list: list[dict], repeats: int, backend: str, client=None) -> dict:
    """A deployed worker, end to end: the caller's round trip next to the worker's own timings.
    Every site id is sent as `bench-<id>`, so the worker's lines never pass for production's."""
    from common import vision_client

    if client is None:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "lambda", region_name=vision_client.region_of(arn), config=Config(read_timeout=120)
        )
    out = {}
    for scene in scene_list:
        try:
            request = {**request_for(scene, backend), "image": False}
        except FileNotFoundError as exc:
            out[scene["name"]] = {"ok": False, "error": str(exc)}
            continue
        request["site"] = {**request["site"], "id": f"bench-{request['site']['id']}"}
        round_trips, totals, analyse, build, last = [], [], [], None, None
        for _ in range(repeats):
            started = time.perf_counter()
            response = client.invoke(FunctionName=arn, Payload=json.dumps(request))
            reply = json.loads(response["Payload"].read())
            round_trips.append((time.perf_counter() - started) * 1000.0)
            if not reply.get("ok"):
                out[scene["name"]] = {"ok": False, "error": reply.get("error") or reply.get("errorMessage")}
                break
            totals.append(reply["timings_ms"]["total"])
            analyse.append(reply["timings_ms"]["analyse"])
            build, last = reply.get("build"), reply
        else:
            task = last.get("task", "ships")
            record = metrics_record(last.get("metrics") or {}, task)
            out[scene["name"]] = {
                "ok": True,
                "round_trip": summarise(round_trips),
                "worker_total": summarise(totals),
                "analyse": summarise(analyse),
                "samples_ms": {
                    "round_trip": [round(v, 3) for v in round_trips],
                    "worker_total": [round(v, 3) for v in totals],
                    "analyse": [round(v, 3) for v in analyse],
                },
                "metrics": record,
                "metrics_digest": vision_runs.digest(record),
                "build": build,
            }
    return out


def deployed_code_sha256(client, arn: str) -> str | None:
    """The deployed function's CodeSha256, when the client may read its configuration."""
    try:
        return client.get_function_configuration(FunctionName=arn).get("CodeSha256")
    except Exception:  # no such permission, or a client without the call: the field stays null
        return None


def fetch_network(scene: dict, out: Path, fetch=None) -> dict:
    """The scene's rail network from Overpass (common/osm.py's `fetch_network`, or `fetch`),
    written to `out` as JSON in the shape the worker takes, and returned."""
    if fetch is None:
        from common import osm

        fetch = osm.fetch_network
    network = fetch(scene["polygon"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(network) + "\n", encoding="utf-8")
    return network


# --- the environment: host, labels, libraries ---------------------------------------------------------


def environment(threads: int | None = None, host: dict | None = None, cool: dict | None = None,
                libraries: list[dict] | None = None) -> dict:  # fmt: skip
    """The run's environment: the CPU, machine, Python and OpenCV build as before, plus the
    cores, affinity, threads, library versions, where it ran, and what `--imds`, `--label` and
    the mapped-library scan found (null when not asked for, or not there)."""
    from vision import build

    return vision_runs.environment(
        cpu_identity(), build.build_info(), threads=threads, host=host, cool=cool, libraries=libraries
    )


def imds_identity(http=None, base: str = IMDS_BASE, timeout: float = IMDS_TIMEOUT_SECONDS) -> dict | None:
    """The EC2 instance identity through IMDSv2 (a PUT for a token, then a GET of the document),
    reduced to IMDS_KEEP's five fields; None off EC2 (the connection fails or times out within
    `timeout`), or when either call does not answer 200."""
    if http is None:
        import requests

        http = requests
    try:
        token = http.put(
            f"{base}/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            timeout=(timeout, timeout),
        )
        if token.status_code != 200:
            return None
        document = http.get(
            f"{base}/latest/dynamic/instance-identity/document",
            headers={"X-aws-ec2-metadata-token": token.text.strip()},
            timeout=(timeout, timeout),
        )
        if document.status_code != 200:
            return None
        data = document.json()
    except Exception:  # not an EC2 instance: refused or timed out, which is the answer
        return None
    return redact_identity(data if isinstance(data, dict) else {})


def redact_identity(document: dict) -> dict:
    """Only IMDS_KEEP's fields, under their stored names; nothing else of the document survives."""
    return {stored: document.get(field) for field, stored in IMDS_KEEP.items()}


def parse_labels(items: list[str] | None) -> dict | None:
    """`--label k=v` items as environment.cool; ValueError on an unknown key, a missing `=` or a
    delivery that is neither `ami` nor `container`. None when no label was given."""
    if not items:
        return None
    found = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or key not in LABEL_KEYS:
            raise ValueError(f"--label takes k=v with k one of {', '.join(LABEL_KEYS)}: got {item!r}")
        if key == "cool_delivery" and value not in DELIVERIES:
            raise ValueError(f"--label cool_delivery takes one of {DELIVERIES}: got {value!r}")
        found[LABEL_KEYS[key]] = value
    return {stored: found.get(stored) for stored in LABEL_KEYS.values()}


def mapped_libraries(maps_text: str | None = None, which=shutil.which, run=subprocess.run,
                     system: str | None = None) -> list[dict] | None:  # fmt: skip
    """The OpenCV shared objects mapped into this process (`/proc/self/maps`, or `maps_text`):
    each with its path, size, sha256 and owning package. None off Linux, where there is no maps
    file: the run file then says so."""
    if maps_text is None:
        if (system or platform.system()) != "Linux":
            return None
        try:
            maps_text = Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    paths: list[str] = []
    for line in maps_text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        path = parts[5].strip()
        if path.startswith("/") and LIBRARY_PATTERN.search(path) and path not in paths:
            paths.append(path)
    out = []
    for path in paths:
        try:
            size, sha = Path(path).stat().st_size, vision_runs.sha256_file(Path(path))
        except OSError:
            size, sha = None, None
        out.append({"path": path, "bytes": size, "sha256": sha, "package": package_of(path, which, run)})
    return out


def package_of(path: str, which=shutil.which, run=subprocess.run) -> str:
    """The package that owns `path`: `dpkg -S` or `rpm -qf` when the tool exists and answers,
    else the pip distribution whose RECORD lists it, else "not packaged"."""
    for tool, argv in (("dpkg", ["dpkg", "-S", path]), ("rpm", ["rpm", "-qf", path])):
        if not which(tool):
            continue
        try:
            done = run(argv, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip():
            first = done.stdout.strip().splitlines()[0]
            return first.split(":", 1)[0].strip() if tool == "dpkg" else first.strip()
    return pip_record_for(path) or "not packaged"


def pip_record_for(path: str) -> str | None:
    """The `*.dist-info` whose RECORD lists `path`, searched in the `site-packages` (or
    `dist-packages`) directory above it; None when no RECORD names the file."""
    file = Path(path)
    for parent in file.parents:
        if parent.name not in ("site-packages", "dist-packages"):
            continue
        relative = file.relative_to(parent).as_posix()
        for record in sorted(parent.glob("*.dist-info/RECORD")):
            try:
                lines = record.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if any(line.split(",", 1)[0] == relative for line in lines):
                return record.parent.name
        return None
    return None


def build_lines(text: str) -> list[str]:
    """The lines of `cv2.getBuildInformation()` that tell a vendor build apart: the compiler
    lines and their flags (`-mcpu`, `-march`), every KleidiCV line, and any line that says COOL,
    Graviton or vendor."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        telling = line.startswith(BUILD_LINE_PREFIXES) or BUILD_LINE_WORDS.search(line)
        if line and telling and line not in out:
            out.append(line)
    return out


def pin_cpu(cpu: int) -> str | None:
    """Pin this process to CPU `cpu` (Linux's `sched_setaffinity`); the refusal to print, where
    that call does not exist or fails."""
    setter = getattr(os, "sched_setaffinity", None)
    if setter is None:
        return (
            f"--pin-cpu needs os.sched_setaffinity, which {platform.system()} does not have: "
            "pin the process with the OS's own tool instead (the affinity is recorded either way)"
        )
    try:
        setter(0, {cpu})
    except (OSError, ValueError) as exc:
        return f"--pin-cpu {cpu}: {exc}"
    return None


def set_threads(threads: int | None) -> int | None:
    """`cv2.setNumThreads(threads)` when asked; the thread count OpenCV then reports."""
    import cv2

    if threads is not None:
        cv2.setNumThreads(threads)
    return cv2.getNumThreads()


# --- the inputs: HEADs, the manifest's pins, the cache, the networks -------------------------------------


def load_manifest(path: Path | None) -> dict | None:
    if not path or not Path(path).is_file():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scene_inputs(scene_list: list[dict], heads: dict[str, dict] | None) -> list[dict]:
    """`inputs.scenes`: each scene's id and its assets' ETags (null when no HEAD was made)."""
    out = []
    for scene in scene_list:
        etags = None
        if heads is not None:
            etags = {band: (heads.get(url) or {}).get("etag") for band, url in scene["assets"].items()}
        out.append({"name": scene["name"], "scene_id": scene["scene_id"], "etag_by_asset": etags})
    return out


def network_inputs(scene_list: list[dict], scenes_file: Path) -> list[dict]:
    """`inputs.networks`: each rail scene's network file (relative to the scenes file) and its
    sha256, for the files that exist."""
    out, seen = [], set()
    for scene in scene_list:
        path = scene.get("network_file")
        if not path or path in seen or not Path(path).is_file():
            continue
        seen.add(path)
        try:
            name = Path(path).relative_to(Path(scenes_file).resolve().parent).as_posix()
        except ValueError:
            name = Path(path).name
        out.append({"file": name, "sha256": vision_runs.sha256_file(Path(path))})
    return out


def prepare_inputs(scene_list: list[dict], manifest: dict | None, cache_dir: Path | None, head=None,
                   manifest_path: Path = MANIFEST_FILE):  # fmt: skip
    """Before anything is read: with a cache or a manifest pin on any asset, HEAD every asset of
    the run (its ETag and size), refuse the run when a pinned ETag changed (InputChanged), and
    open the cache. Returns (heads or None, the cache or None, the manifest's sha256 or None)."""
    pinned = vision_runs.pinned_etags(manifest)
    urls = [url for scene in scene_list for url in scene["assets"].values()]
    pinned_here = any(url in pinned for url in urls)
    if cache_dir is None and not pinned_here:
        return None, None, None
    heads = vision_runs.head_assets(urls, head)
    vision_runs.check_pinned(heads, {url: etag for url, etag in pinned.items() if url in heads})
    cache = None
    if cache_dir is not None:
        import vision_worker_handler

        etags = {url: fields.get("etag") or "" for url, fields in heads.items()}
        cache = vision_runs.CachedFetch(cache_dir, etags, fetch=vision_worker_handler.http_fetch, head=head)
    manifest_path = Path(manifest_path)
    manifest_sha = vision_runs.sha256_file(manifest_path) if pinned_here and manifest_path.is_file() else None
    return heads, cache, manifest_sha


# --- compare ---------------------------------------------------------------------------------------


def ratio_ci(a: list[float], b: list[float], bootstrap: int, rng) -> dict:
    """median(a) / median(b) (the speed-up of B over A when both are times), with a percentile
    bootstrap 95 % interval from `bootstrap` resamples of each; the interval is null when either
    side has one sample or `bootstrap` is 0."""
    import numpy as np

    xa, xb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    median_a, median_b = float(np.median(xa)), float(np.median(xb))
    ratio = round(median_a / median_b, 4) if median_b > 0 else None
    low = high = None
    if bootstrap and len(xa) > 1 and len(xb) > 1 and ratio is not None:
        ra = np.median(rng.choice(xa, size=(bootstrap, len(xa)), replace=True), axis=1)
        rb = np.median(rng.choice(xb, size=(bootstrap, len(xb)), replace=True), axis=1)
        ratios = ra / np.where(rb > 0, rb, np.nan)
        ratios = ratios[np.isfinite(ratios)]
        if len(ratios):
            low, high = (round(float(v), 4) for v in np.percentile(ratios, [2.5, 97.5]))
    return {
        "median_a_ms": round(median_a, 3),
        "median_b_ms": round(median_b, 3),
        "runs_a": len(xa),
        "runs_b": len(xb),
        "ratio": ratio,
        "ci_low": low,
        "ci_high": high,
    }


def same_inputs(a: dict, b: dict) -> list[str]:
    """The scene names both runs read; CompareRefused ("not the same inputs") when a common
    scene's ETags or a shared network file's sha256 differ, or when they share no scene. Kernel
    runs must share the synthetic size."""
    if a["kind"] == "bench-kernels":
        size_a, size_b = a["params"].get("size"), b["params"].get("size")
        if size_a != size_b:
            raise CompareRefused(f"not the same inputs: sizes {size_a} and {size_b}")
        return []
    key = lambda s: s.get("name") or s.get("scene_id")  # noqa: E731 - a one-line key for two runs
    scenes_a = {key(s): s.get("etag_by_asset") for s in a["inputs"]["scenes"]}
    scenes_b = {key(s): s.get("etag_by_asset") for s in b["inputs"]["scenes"]}
    common = [name for name in scenes_a if name in scenes_b]
    if not common:
        raise CompareRefused("not the same inputs: the runs share no scene")
    for name in common:
        if scenes_a[name] != scenes_b[name]:
            raise CompareRefused(f"not the same inputs: {name}'s asset ETags differ")
    nets_a = {n["file"]: n["sha256"] for n in a["inputs"].get("networks") or []}
    nets_b = {n["file"]: n["sha256"] for n in b["inputs"].get("networks") or []}
    for file in nets_a:
        if file in nets_b and nets_a[file] != nets_b[file]:
            raise CompareRefused(f"not the same inputs: network {file} differs")
    return common


def parity(ra: dict, rb: dict) -> dict:
    """Whether two rows measured the same thing: an equal `metrics_digest`, else every scalar
    within PARITY_ABS / PARITY_REL and every count, hub, flag and suggestion identical."""
    ma, mb = ra.get("metrics"), rb.get("metrics")
    if ra.get("metrics_digest") and ra.get("metrics_digest") == rb.get("metrics_digest"):
        return {"pass": True, "equal_digest": True, "differences": []}
    if ma is None or mb is None:
        return {"pass": False, "equal_digest": False, "differences": ["metrics not recorded"]}
    differences = []
    for name in sorted(set(ma) | set(mb)):
        va, vb = ma.get(name), mb.get(name)
        numbers = isinstance(va, int | float) and isinstance(vb, int | float)
        if name in PARITY_ABS and numbers:
            if abs(va - vb) > PARITY_ABS[name]:
                differences.append(f"{name}: {va} vs {vb} (abs {abs(va - vb):.6f} > {PARITY_ABS[name]})")
        elif name in PARITY_REL and numbers:
            scale = max(abs(va), abs(vb), 1e-12)
            relative = abs(va - vb) / scale
            if relative > PARITY_REL[name]:
                differences.append(f"{name}: {va} vs {vb} (rel {relative:.6f} > {PARITY_REL[name]})")
        elif va != vb:
            differences.append(f"{name}: {va} vs {vb}")
    return {"pass": not differences, "equal_digest": False, "differences": differences}


def pixel_mismatch(path_a: Path | None, path_b: Path | None) -> dict:
    """The share of pixels that differ between two figures (reported, never gated); null, with
    why, when a figure was not kept or the shapes differ."""
    if not path_a or not path_b or not Path(path_a).is_file() or not Path(path_b).is_file():
        return {"share": None, "note": "figure not kept"}
    import cv2

    ia, ib = cv2.imread(str(path_a), cv2.IMREAD_UNCHANGED), cv2.imread(str(path_b), cv2.IMREAD_UNCHANGED)
    if ia is None or ib is None:
        return {"share": None, "note": "figure unreadable"}
    if ia.shape != ib.shape:
        return {"share": None, "note": f"shapes differ: {list(ia.shape)} vs {list(ib.shape)}"}
    differs = ia != ib
    if differs.ndim == 3:
        differs = differs.any(axis=2)
    return {"share": round(float(differs.mean()), 6), "note": None}


def compare(a: dict, b: dict, bootstrap: int = 2000, seed: int = 0, dir_a: Path | None = None,
            dir_b: Path | None = None) -> dict:  # fmt: skip
    """Two runs of one kind (kernels or scenes), from one machine and the same inputs: per case
    or stage the ratio of medians with its bootstrap interval (`default_rng(seed)`, so the same
    seed gives the same numbers), the headline on the compute stages with the read apart, each
    stage's share of the total, and for scenes the parity of the metrics and the figures' pixel
    mismatch share. CompareRefused names what must not be compared."""
    import numpy as np

    if a["kind"] != b["kind"]:
        raise CompareRefused(f"not the same kind: {a['kind']} and {b['kind']}")
    if a["kind"] not in ("bench-kernels", "bench-scenes"):
        raise CompareRefused(f"compare takes two bench-kernels or two bench-scenes runs, not {a['kind']}")
    cpu_a, cpu_b = a["environment"].get("cpu"), b["environment"].get("cpu")
    if cpu_a != cpu_b:
        raise CompareRefused(f"not the same machine: {cpu_a!r} and {cpu_b!r}")
    common = same_inputs(a, b)
    rng = np.random.default_rng(seed)
    out = {
        "a": a["run_id"],
        "b": b["run_id"],
        "kind": a["kind"],
        "backend_a": a.get("backend"),
        "backend_b": b.get("backend"),
        "build_a": (a["environment"].get("opencv") or {}).get("build_sha256"),
        "build_b": (b["environment"].get("opencv") or {}).get("build_sha256"),
        "cpu": cpu_a,
        "bootstrap": bootstrap,
        "seed": seed,
        "parity_pass": True,
        "notes": [],
    }
    if a["kind"] == "bench-kernels":
        out["cases"] = _compare_kernels(a["results"], b["results"], bootstrap, rng, out["notes"])
        out["headline"] = out["cases"].get("analyse_rail_access", {}).get("ratio_ci")
        return out
    out["scenes"] = {}
    for name in common:
        ra, rb = a["results"].get(name) or {}, b["results"].get(name) or {}
        if not (ra.get("ok") and rb.get("ok")):
            out["scenes"][name] = {"skipped": f"not ok in both runs ({ra.get('error')}, {rb.get('error')})"}
            continue
        row = _compare_scene(ra, rb, bootstrap, rng, out["notes"], name)
        row["parity"] = parity(ra, rb)
        figure_a = (dir_a / ra["figure"]) if dir_a and ra.get("figure") else None
        figure_b = (dir_b / rb["figure"]) if dir_b and rb.get("figure") else None
        if ra.get("image_png_sha256") and ra.get("image_png_sha256") == rb.get("image_png_sha256"):
            row["pixel_mismatch"] = {"share": 0.0, "note": "identical PNG bytes"}
        else:
            row["pixel_mismatch"] = pixel_mismatch(figure_a, figure_b)
        out["parity_pass"] = out["parity_pass"] and row["parity"]["pass"]
        out["scenes"][name] = row
    compared = [row for row in out["scenes"].values() if "headline" in row]
    out["headline"] = _pooled_headline(compared, bootstrap, rng)
    return out


def _compare_kernels(ra: dict, rb: dict, bootstrap: int, rng, notes: list[str]) -> dict:
    cases = {}
    only_a, only_b = sorted(set(ra) - set(rb)), sorted(set(rb) - set(ra))
    if only_a or only_b:
        notes.append(f"cases only in one run are not compared: A {only_a}, B {only_b}")
    for name in sorted(set(ra) & set(rb)):
        sa, sb = ra[name].get("samples_ms") or [], rb[name].get("samples_ms") or []
        if not sa or not sb:
            cases[name] = {"skipped": "no samples kept"}
            continue
        row = {"ratio_ci": ratio_ci(sa, sb, bootstrap, rng)}
        stages_a, stages_b = ra[name].get("stages") or {}, rb[name].get("stages") or {}
        if stages_a and stages_b:
            row["stages"] = {}
            for stage in [s for s in stages_a if s in stages_b]:
                samples_a, samples_b = stages_a[stage]["samples_ms"], stages_b[stage]["samples_ms"]
                row["stages"][stage] = ratio_ci(samples_a, samples_b, bootstrap, rng)
        cases[name] = row
    return cases


def _compare_scene(ra: dict, rb: dict, bootstrap: int, rng, notes: list[str], name: str) -> dict:
    import numpy as np

    sa, sb = ra.get("samples_ms") or {}, rb.get("samples_ms") or {}
    common = [stage for stage in sa if stage in sb]
    only_a, only_b = sorted(set(sa) - set(sb)), sorted(set(sb) - set(sa))
    if only_a or only_b:
        notes.append(f"{name}: stages only in one run are not compared: A {only_a}, B {only_b}")
    total_a = float(np.median(sa["total"])) if "total" in sa else sum(float(np.median(sa[s])) for s in common)
    total_b = float(np.median(sb["total"])) if "total" in sb else sum(float(np.median(sb[s])) for s in common)
    stages = {}
    for stage in common:
        row = ratio_ci(sa[stage], sb[stage], bootstrap, rng)
        row["share_a"] = round(row["median_a_ms"] / total_a, 4) if total_a else None
        row["share_b"] = round(row["median_b_ms"] / total_b, 4) if total_b else None
        stages[stage] = row
    compute = [s for s in COMPUTE_STAGES if s in common]
    out = {"stages": stages, "compute_stages": compute, "stages_only_in_a": only_a,
           "stages_only_in_b": only_b}  # fmt: skip
    if compute:
        ca = [sum(sa[s][i] for s in compute) for i in range(min(len(sa[s]) for s in compute))]
        cb = [sum(sb[s][i] for s in compute) for i in range(min(len(sb[s]) for s in compute))]
        out["headline"] = ratio_ci(ca, cb, bootstrap, rng)
        out["_compute_samples"] = (ca, cb)
    if "read" in common:
        out["read"] = stages["read"]
    return out


def _pooled_headline(rows: list[dict], bootstrap: int, rng) -> dict | None:
    """The headline over every compared scene: the compute samples of all scenes, summed per
    repeat index (one scene: its own headline)."""
    if not rows:
        return None
    pairs = [row.pop("_compute_samples") for row in rows]
    if len(rows) == 1:
        return rows[0]["headline"]
    n = min(len(ca) for ca, _ in pairs)
    m = min(len(cb) for _, cb in pairs)
    ca = [sum(pair[0][i] for pair in pairs) for i in range(n)]
    cb = [sum(pair[1][i] for pair in pairs) for i in range(m)]
    return ratio_ci(ca, cb, bootstrap, rng)


def compare_checks(result: dict) -> list[dict]:
    checks = []
    for name, row in (result.get("scenes") or {}).items():
        if "parity" in row:
            passed = row["parity"]["pass"]
            got = "equal" if passed else "; ".join(row["parity"]["differences"])[:300]
            checks.append({"name": f"parity:{name}", "expected": "equal", "got": got, "pass": passed})
    return checks


def compare_summary(result: dict) -> dict:
    headline = result.get("headline") or {}
    summary = {
        "headline_ratio": headline.get("ratio"),
        "headline_ci_low": headline.get("ci_low"),
        "headline_ci_high": headline.get("ci_high"),
        "parity_pass": int(bool(result.get("parity_pass"))),
    }
    if "scenes" in result:
        summary["scenes_compared"] = sum(1 for row in result["scenes"].values() if "stages" in row)
        for name, row in result["scenes"].items():
            for stage, ratio in (row.get("stages") or {}).items():
                summary[f"{name}.{stage}_ratio"] = ratio.get("ratio")
            if row.get("pixel_mismatch", {}).get("share") is not None:
                summary[f"{name}.pixel_mismatch_share"] = row["pixel_mismatch"]["share"]
    else:
        summary["cases_compared"] = sum(1 for row in result["cases"].values() if "ratio_ci" in row)
        for name, row in result["cases"].items():
            if "ratio_ci" in row:
                summary[f"{name}_ratio"] = row["ratio_ci"]["ratio"]
    return summary


# --- controls ---------------------------------------------------------------------------------------


def controls(scene: dict, cool_sha: str, handler=None, fetch=None, environ: dict | None = None) -> dict:
    """The COOL guard's negative controls, in this interpreter, on one real scene: (1) a `cool`
    request with the pin set, `ok` only when this build is the pinned one, else `not_cool`; (2)
    `cool` with no pin: `not_cool`; (3) an `opencv` request to the `cool` backend:
    `backend_mismatch`. No images are asked for. The environment is restored afterwards."""
    from vision import build

    environ = os.environ if environ is None else environ
    if handler is None:
        import vision_worker_handler

        handler = vision_worker_handler.lambda_handler
    matches = build.matches(build.build_info(), cool_sha)
    saved = {key: environ.get(key) for key in ("VISION_BACKEND", "COOL_BUILD_SHA256")}
    rows = []
    try:
        for name, request_backend, pinned, expected in CONTROLS:
            environ["VISION_BACKEND"] = "cool"
            if pinned:
                environ["COOL_BUILD_SHA256"] = cool_sha
            else:
                environ.pop("COOL_BUILD_SHA256", None)
            expected = expected or ("ok" if matches else "not_cool")
            request = {**request_for(scene, request_backend), "image": False}
            reply = handler(request, None, **({"fetch": fetch} if fetch else {}))
            got = "ok" if reply.get("ok") else reply.get("error")
            rows.append({
                "name": name,
                "request_backend": request_backend,
                "pinned": pinned,
                "expected": expected,
                "got": got,
                "pass": got == expected,
                "detail": None if reply.get("ok") else reply.get("detail"),
                "timings_ms": reply.get("timings_ms") if reply.get("ok") else None,
                "io": reply.get("io") if reply.get("ok") else None,
            })  # fmt: skip
    finally:
        for key, value in saved.items():
            if value is None:
                environ.pop(key, None)
            else:
                environ[key] = value
    return {"scene": scene["name"], "build_matches_pin": matches, "controls": rows}


# --- the report ---------------------------------------------------------------------------------------


def markdown(report: dict) -> str:
    env = report["environment"]
    opencv = env["opencv"]
    params = report.get("params") or {}
    lines = [
        f"**{report['mode']}** on {env['cpu']} ({env['machine']}), Python {env['python']}, "
        f"OpenCV {opencv['opencv_version']} build `{opencv['build_sha256'][:12]}`"
        f"{' (KleidiCV)' if opencv.get('kleidicv') else ''}",
    ]
    if "threads" in env:
        lines.append(
            f"threads {env.get('threads')}, affinity {env.get('affinity')}, cores {env.get('cpu_count')}, "
            f"where {env.get('where')}, git {(report.get('code') or {}).get('git_sha') or 'null'}"
            f"{' (dirty)' if (report.get('code') or {}).get('dirty') else ''}"
        )
    if params.get("imds") and env.get("host") is None:
        lines.append("host: null (not on EC2, or IMDS did not answer within 1 s)")
    elif env.get("host"):
        host = env["host"]
        lines.append(f"host: {host.get('instance_type')} in {host.get('region')}, AMI {host.get('ami_id')}, "
                     f"product codes {host.get('marketplace_product_codes')}")  # fmt: skip
    if report["mode"] in ("scenes", "controls") and "libraries" in env:
        if env.get("libraries") is None:
            lines.append("libraries: null (not Linux: there is no /proc/self/maps to read)")
        else:
            lines.append(f"libraries: {len(env['libraries'])} OpenCV shared objects mapped "
                         f"({', '.join(Path(lib['path']).name for lib in env['libraries'])})")  # fmt: skip
    lines.append("")
    results = report["results"]
    if report["mode"] == "kernels":
        lines += ["| operation | median ms | p90 ms | runs |", "|---|---:|---:|---:|"]
        for name, s in results.items():
            lines.append(f"| {name} | {s['median_ms']} | {s['p90_ms']} | {s['runs']} |")
            for stage, t in (s.get("stages") or {}).items():
                lines.append(f"| {name}.{stage} | {t['median_ms']} | {t['p90_ms']} | {t['runs']} |")
    elif report["mode"] == "scenes":
        lines += ["| scene | stage | median ms | p90 ms |", "|---|---|---:|---:|"]
        for name, r in results.items():
            if not r["ok"]:
                lines.append(f"| {name} | error: {r['error']} | | |")
                continue
            for stage, s in r["stages"].items():
                lines.append(f"| {name} | {stage} | {s['median_ms']} | {s['p90_ms']} |")
        for name, r in results.items():
            if r.get("ok") and r.get("metrics_digest"):
                lines.append(
                    f"- {name}: metrics `{r['metrics_digest'][:12]}`, figure "
                    f"`{(r.get('image_png_sha256') or 'none')[:12]}` {r.get('image_shape')}, peak RSS "
                    f"{r.get('peak_rss_mb')} MB ({r.get('peak_scope')})"
                )
    elif report["mode"] == "compare":
        lines += _markdown_compare(results)
    elif report["mode"] == "controls":
        lines += ["| control | request backend | pinned | expected | got | pass |",
                  "|---|---|---|---|---|---|"]  # fmt: skip
        for row in results["controls"]:
            lines.append(f"| {row['name']} | {row['request_backend']} | {row['pinned']} | {row['expected']} "
                         f"| {row['got']} | {'yes' if row['pass'] else 'NO'} |")  # fmt: skip
        lines.append(f"\nscene {results['scene']}; this build matches the pin: "
                     f"{results['build_matches_pin']}")  # fmt: skip
    else:
        lines += ["| scene | round trip ms | worker total ms | analyse ms |", "|---|---:|---:|---:|"]
        for name, r in results.items():
            if not r["ok"]:
                lines.append(f"| {name} | error: {r['error']} | | |")
                continue
            lines.append(
                f"| {name} | {r['round_trip']['median_ms']} | {r['worker_total']['median_ms']} "
                f"| {r['analyse']['median_ms']} |"
            )
    return "\n".join(lines)


def _markdown_compare(result: dict) -> list[str]:
    def ci(row):
        if row is None or row.get("ratio") is None:
            return "n/a"
        return f"{row['ratio']} [{row.get('ci_low')}, {row.get('ci_high')}]"

    build_a, build_b = (result.get("build_a") or "")[:12], (result.get("build_b") or "")[:12]
    lines = [
        f"A `{result['a']}` ({result.get('backend_a')}, build `{build_a}`) against "
        f"B `{result['b']}` ({result.get('backend_b')}, build `{build_b}`); "
        f"speed-up of B over A = median A / median B, bootstrap {result['bootstrap']} seed {result['seed']}",
        f"**headline (compute stages {COMPUTE_STAGES}): {ci(result.get('headline'))}**",
        "",
    ]
    if "cases" in result:
        lines += ["| case | median A ms | median B ms | speed-up [95 % CI] |", "|---|---:|---:|---|"]
        for name, row in result["cases"].items():
            if "ratio_ci" not in row:
                lines.append(f"| {name} | | | {row.get('skipped')} |")
                continue
            r = row["ratio_ci"]
            lines.append(f"| {name} | {r['median_a_ms']} | {r['median_b_ms']} | {ci(r)} |")
            for stage, s in (row.get("stages") or {}).items():
                lines.append(f"| {name}.{stage} | {s['median_a_ms']} | {s['median_b_ms']} | {ci(s)} |")
    else:
        lines += ["| scene | stage | median A ms | median B ms | share A | speed-up [95 % CI] |",
                  "|---|---|---:|---:|---:|---|"]  # fmt: skip
        for name, row in result["scenes"].items():
            if "stages" not in row:
                lines.append(f"| {name} | {row.get('skipped')} | | | | |")
                continue
            for stage, s in row["stages"].items():
                lines.append(f"| {name} | {stage} | {s['median_a_ms']} | {s['median_b_ms']} "
                             f"| {s.get('share_a')} | {ci(s)} |")  # fmt: skip
            p = row["parity"]
            verdict = "pass" if p["pass"] else "FAIL: " + "; ".join(p["differences"])
            lines.append(f"| {name} | parity | | | | {verdict} |")
            pm = row["pixel_mismatch"]
            share = pm["share"] if pm["share"] is not None else pm["note"]
            lines.append(f"| {name} | figure pixels differing | | | | {share} |")
    lines += [f"- {note}" for note in result.get("notes") or []]
    return lines


def kernels_summary(results: dict) -> dict:
    summary = {name: row["median_ms"] for name, row in results.items()}
    for stage, row in (results.get("analyse_rail_access", {}).get("stages") or {}).items():
        summary[f"analyse_rail_access.{stage}_ms"] = row["median_ms"]
    return summary


def scenes_summary(results: dict) -> dict:
    summary = {}
    for name, row in results.items():
        summary[f"{name}.ok"] = int(bool(row.get("ok")))
        if not row.get("ok"):
            continue
        for stage, s in row["stages"].items():
            summary[f"{name}.{stage}_ms"] = s["median_ms"]
        for key in ("served_share", "desert_km2", "built_up_km2", "station_count", "count"):
            value = (row.get("metrics") or {}).get(key)
            if value is not None:
                summary[f"{name}.{key}"] = value
        if row.get("peak_rss_mb") is not None:
            summary[f"{name}.peak_rss_mb"] = row["peak_rss_mb"]
    return summary


def worker_summary(results: dict) -> dict:
    summary = {}
    for name, row in results.items():
        summary[f"{name}.ok"] = int(bool(row.get("ok")))
        if row.get("ok"):
            for key in ("round_trip", "worker_total", "analyse"):
                summary[f"{name}.{key}_ms"] = row[key]["median_ms"]
    return summary


def write_figures(results: dict, run_path: Path) -> None:
    """Each scene's figure beside the run file, as `<run file stem>.<scene>.png`, named in the
    row (`figure`, relative to the run file); the PNG bytes leave the row either way."""
    for name, row in results.items():
        png = row.pop("png", None)
        if png is None:
            continue
        figure = run_path.with_name(f"{run_path.stem}.{name}.png")
        figure.write_bytes(png)
        row["figure"] = figure.name


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repeats", type=int, default=None, help="timed runs per case")
    common.add_argument("--out", type=Path,
                        help="the run file: a .json path, or a directory (then <run_id>.json)")  # fmt: skip
    common.add_argument("--tag", help="a suffix for the run id, such as 1t or 4t-r1")
    common.add_argument("--imds", action="store_true", help="record the EC2 instance identity (five fields)")
    common.add_argument("--label", action="append", default=[], metavar="K=V",
                        help=f"one of {', '.join(LABEL_KEYS)} (cool_delivery: ami or container)")  # fmt: skip
    timing = argparse.ArgumentParser(add_help=False)
    timing.add_argument("--threads", type=int, help="cv2.setNumThreads before timing")
    timing.add_argument("--pin-cpu", type=int, metavar="K", help="run on CPU K alone (Linux)")
    backend = argparse.ArgumentParser(add_help=False)
    backend.add_argument("--backend", choices=("opencv", "cool"), default="opencv")
    backend.add_argument("--cool-sha", help="the pinned COOL build's sha256 (required with --backend cool)")
    picking = argparse.ArgumentParser(add_help=False)
    picking.add_argument("--scenes-file", type=Path, default=SCENES_FILE)
    picking.add_argument("--only", action="append", metavar="NAME", help="only this scene (repeatable)")
    reading = argparse.ArgumentParser(add_help=False)
    reading.add_argument("--manifest", type=Path, default=MANIFEST_FILE,
                         help="the manifest whose ETags pin the scenes")  # fmt: skip
    reading.add_argument(
        "--cache", type=vision_runs.cache_dir, nargs="?", const=vision_runs.default_cache_dir(),
        default=None, metavar="DIR", help="answer range reads from DIR (~ expanded), filled on first read",
    )

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    k = sub.add_parser("kernels", parents=[common, timing, backend],
                       help="the OpenCV operations alone, on synthetic images")  # fmt: skip
    k.add_argument("--size", type=int, default=2048)
    s = sub.add_parser("scenes", parents=[common, timing, backend, picking, reading],
                       help="whole sites, in-process")  # fmt: skip
    s.add_argument("--isolate", action="store_true",
                   help="each scene in a fresh interpreter (its own peak memory)")  # fmt: skip
    w = sub.add_parser("worker", parents=[common, backend, picking], help="a deployed worker")
    w.add_argument("--arn", required=True)
    sub.add_parser("controls", parents=[common, timing, backend, picking, reading],
                   help="the COOL guard's negative controls")  # fmt: skip
    cmp_ = sub.add_parser("compare", help="two runs of one kind: speed-up with a bootstrap CI, and parity")
    cmp_.add_argument("a", type=Path)
    cmp_.add_argument("b", type=Path)
    cmp_.add_argument("--bootstrap", type=int, default=2000)
    cmp_.add_argument("--seed", type=int, default=0)
    cmp_.add_argument("--out", type=Path)
    cmp_.add_argument("--tag")
    f = sub.add_parser("fetch-network", help="write a rail scene's OpenStreetMap network file from Overpass")
    f.add_argument("--scene", required=True, help="the scene's name in the scenes file")
    f.add_argument("--scenes-file", type=Path, default=SCENES_FILE)
    f.add_argument("--out", type=Path, help="where to write it (default: the scene's network_file)")
    args = parser.parse_args(argv)

    if args.mode == "fetch-network":
        scene = next((s for s in load_scenes(args.scenes_file) if s["name"] == args.scene), None)
        if scene is None:
            parser.error(f"no scene named {args.scene!r} in {args.scenes_file}")
        out = args.out or (Path(scene["network_file"]) if scene.get("network_file") else None)
        if out is None:
            parser.error("the scene has no network_file: pass --out")
        network = fetch_network(scene, out)
        print(f"wrote {out}: {json.dumps(network.get('counts'))}")
        return 0

    if args.mode == "compare":
        return _main_compare(args)

    if args.mode == "controls" and not args.cool_sha:
        parser.error("controls needs --cool-sha <hex>: the pinned COOL build's fingerprint")
    if args.backend == "cool" and not args.cool_sha:
        parser.error("--backend cool needs --cool-sha <hex>: the pinned COOL build's fingerprint")
    try:
        cool = parse_labels(args.label)
    except ValueError as exc:
        parser.error(str(exc))
    started = vision_runs.utc_now()  # the run's start: before the pin, the threads, IMDS and the work
    if getattr(args, "pin_cpu", None) is not None:
        refusal = pin_cpu(args.pin_cpu)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
    threads = set_threads(getattr(args, "threads", None))
    host = imds_identity() if args.imds else None

    if args.mode in ("scenes", "controls"):
        # The in-process handler reads these at call time; a cool run on another build is not_cool.
        os.environ["VISION_BACKEND"] = args.backend
        if args.cool_sha:
            os.environ["COOL_BUILD_SHA256"] = args.cool_sha
    if args.mode == "kernels" and args.backend == "cool":
        from vision import build

        if not build.matches(build.build_info(), args.cool_sha):
            print("not_cool: this interpreter's OpenCV build is not the pinned COOL build", file=sys.stderr)
            return 1

    params = {key: (str(value) if isinstance(value, Path) else value)
              for key, value in vars(args).items() if key not in ("out", "tag", "label")}  # fmt: skip
    params["cache"] = bool(getattr(args, "cache", None))
    params["labels"] = cool
    inputs: dict = {}
    worker_record = {"kind": "in-process", "region": None, "code_sha256": vision_runs.worker_code_sha256()}
    libraries = None
    scene_list: list[dict] = []
    if args.mode != "kernels":
        try:
            scene_list = select_scenes(load_scenes(args.scenes_file), args.only)
        except ValueError as exc:
            parser.error(str(exc))
        inputs["networks"] = network_inputs(scene_list, args.scenes_file)

    try:
        if args.mode == "kernels":
            results = kernels(args.size, args.repeats or 20)
            summary = kernels_summary(results)
        elif args.mode == "worker":
            results = worker(args.arn, scene_list, args.repeats or 5, args.backend)
            summary = worker_summary(results)
            from common import vision_client

            region = vision_client.region_of(args.arn)
            worker_record = {"kind": "deployed", "region": region, "code_sha256": None}
            inputs["scenes"] = scene_inputs(scene_list, None)
        else:
            heads, cache, manifest_sha = prepare_inputs(
                scene_list, load_manifest(args.manifest), args.cache, manifest_path=args.manifest
            )
            inputs.update({"scenes": scene_inputs(scene_list, heads), "manifest_sha256": manifest_sha,
                           "cache": cache is not None})  # fmt: skip
            if args.mode == "controls":
                results = controls(scene_list[0], args.cool_sha, fetch=cache)
                keys = ("name", "expected", "got", "pass")
                checks = [{key: row[key] for key in keys} for row in results["controls"]]
                summary = {"as_expected": sum(1 for row in checks if row["pass"]), "controls": len(checks)}
            else:
                isolate = None
                if args.isolate:
                    isolate = lambda scene: isolate_scene(scene, child_args(argv))  # noqa: E731
                results = scenes(scene_list, args.repeats or 3, args.backend, fetch=cache, isolate=isolate)
                summary = scenes_summary(results)
            if cache is not None:
                inputs["cache_index_sha256"] = cache.index_sha256()
                inputs["cache_hits"], inputs["cache_misses"] = cache.hits, cache.misses
            libraries = mapped_libraries()
    except (vision_runs.InputChanged, vision_runs.WrongHost) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    env = environment(threads=threads, host=host, cool=cool, libraries=libraries)
    if args.mode in ("scenes", "controls"):
        import cv2

        env["build_lines"] = build_lines(cv2.getBuildInformation())
    run = vision_runs.new_run(
        f"bench-{args.mode}", env["cpu"], args.backend, env, worker=worker_record, inputs=inputs,
        params=params, tag=args.tag, mode=args.mode, started=started,
    )  # fmt: skip
    if args.mode == "controls":
        vision_runs.finish(run, results, checks, summary)
    else:
        vision_runs.finish(run, results, [], summary)
    print(markdown(run))
    if args.out:
        path = vision_runs.out_path(run, args.out)
        if args.mode == "scenes":
            path.parent.mkdir(parents=True, exist_ok=True)
            write_figures(results, path)
        vision_runs.write(run, path)
        print(f"wrote {path}")
    elif args.mode == "scenes":
        for row in results.values():
            row.pop("png", None)
    return 0


def _main_compare(args) -> int:
    started = vision_runs.utc_now()
    try:
        a, b = vision_runs.load(args.a), vision_runs.load(args.b)
        result = compare(a, b, args.bootstrap, args.seed, dir_a=args.a.parent, dir_b=args.b.parent)
    except (ValueError, CompareRefused) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    env = environment()
    run = vision_runs.new_run(
        "bench-compare", env["cpu"], f"{result.get('backend_a')}-vs-{result.get('backend_b')}", env,
        inputs={"scenes": a["inputs"].get("scenes") or [], "networks": a["inputs"].get("networks") or [],
                "runs": [a["run_id"], b["run_id"]]},
        params={"a": str(args.a), "b": str(args.b), "bootstrap": args.bootstrap, "seed": args.seed},
        tag=args.tag, mode="compare", started=started,
    )  # fmt: skip
    vision_runs.finish(run, result, compare_checks(result), compare_summary(result))
    print(markdown(run))
    if args.out:
        path = vision_runs.write(run, args.out)
        print(f"wrote {path}")
    if not result["parity_pass"]:
        print("parity: FAIL (the runs did not measure the same thing)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
