#!/usr/bin/env python3
"""Benchmark the vision worker's OpenCV work: stock OpenCV 5 against COOL, x86 against Graviton.

The COOL award asks for *measured* value against a baseline (docs/enhancements/
opencv-agentic-vision-enhancement.md §4). This script is how those numbers are made, the same way
on every machine, so the rows of the report compare like with like:

    python scripts/vision_benchmark.py kernels                    # the OpenCV operations alone
    python scripts/vision_benchmark.py scenes                     # whole sites, read over HTTPS
    python scripts/vision_benchmark.py worker --arn <ARN>         # a deployed worker, end to end
    python scripts/vision_benchmark.py fetch-network --scene NAME # a rail scene's OSM network file

`kernels` times the operations the worker spends its CPU on, for both tasks (resize, Gaussian
blur, adaptive Gaussian threshold, morphology, connected components, contours, the affine warp
and the whole per-site ship analysis; the distance transform, hit-or-miss thinning, polylines,
the colour map, the desert components and the whole rail access analysis) on fixed synthetic
images, so network and data never enter it. Run it where each build lives: stock
`opencv-python-headless` on an x86 machine and on Graviton, then inside OpenCV's COOL image on
Graviton3/4. `scenes` runs the worker handler in-process on the pinned scene list
(scripts/vision_benchmark_scenes.json), with the read time reported apart from the OpenCV time;
a rail scene needs its `network_file`, which `fetch-network` writes from Overpass (the file is
not committed unless it could be fetched: Overpass is not reachable from every build container).
`worker` invokes a deployed worker and adds the round trip as the caller sees it.

Every report records which OpenCV build ran (vision/build.py's fingerprint: that is what proves a
row is COOL) and which CPU (`cpu`, from /proc/cpuinfo: Graviton2 is Neoverse N1, Graviton3 V1,
Graviton4 V2). `--out FILE` writes the report as JSON; a Markdown table is always printed.

Needs requirements-vision.txt installed (and boto3 for `worker`). Reads only public data.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

# The CPU table is the worker contract's, so the worker's reply, this benchmark and the assistant
# name a CPU the same way (ARM_PARTS is re-exported for the tests that read it here).
from common.vision_contract import ARM_PARTS, cpu_identity  # noqa: E402,F401 - after the path above

SCENES_FILE = Path(__file__).with_name("vision_benchmark_scenes.json")

# The synthetic city's grid: 20 m pixels in UTM zone 56S, as the rail task reads a scene.
CITY_TRANSFORM = (300000.0, 20.0, 0.0, 6300000.0, 0.0, -20.0)
CITY_EPSG, CITY_PIXEL_M = 32756, 20.0
# What a missing rail network file is reported as, per scene, in the scenes and worker modes.
NETWORK_FILE_MISSING = "network_file missing"


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
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return summarise(samples)


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
        "analyse_rail_access": lambda: analyse_rail_access(
            city, site, CITY_PIXEL_M, CITY_TRANSFORM, CITY_EPSG, network, params=rail_params
        ),
    }
    return {name: time_it(fn, repeats) for name, fn in cases.items()}


def load_scenes(path: Path = SCENES_FILE) -> list[dict]:
    """The pinned scenes. A scene's `network_file` is relative to the scenes file; it is made
    absolute here so the request builders need not know where the list came from."""
    scenes = json.loads(path.read_text(encoding="utf-8"))["scenes"]
    for scene in scenes:
        if scene.get("network_file"):
            scene["network_file"] = str(path.parent / scene["network_file"])
    return scenes


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


def scenes(scene_list: list[dict], repeats: int, backend: str, handler=None, fetch=None) -> dict:
    """The worker handler in-process, per scene: its own read/analyse/annotate/total timings (and
    the rail task's graph stage)."""
    if handler is None:
        import vision_worker_handler

        handler = vision_worker_handler.lambda_handler
    out = {}
    for scene in scene_list:
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
        out[scene["name"]] = {
            "ok": bool(last and last.get("ok")),
            "error": None if last and last.get("ok") else (last or {}).get("error"),
            "count": (last or {}).get("metrics", {}).get("count"),
            "io": (last or {}).get("io"),
            "stages": {stage: summarise(values) for stage, values in stages.items()},
        }
    return out


def worker(arn: str, scene_list: list[dict], repeats: int, backend: str, client=None) -> dict:
    """A deployed worker, end to end: the caller's round trip next to the worker's own timings."""
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
        round_trips, totals, analyse, build = [], [], [], None
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
            build = reply.get("build")
        else:
            out[scene["name"]] = {
                "ok": True,
                "round_trip": summarise(round_trips),
                "worker_total": summarise(totals),
                "analyse": summarise(analyse),
                "build": build,
            }
    return out


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


def environment() -> dict:
    from vision import build

    return {"cpu": cpu_identity(), "machine": platform.machine(), "python": platform.python_version(),
            "opencv": build.build_info()}  # fmt: skip


def markdown(report: dict) -> str:
    env = report["environment"]
    opencv = env["opencv"]
    lines = [
        f"**{report['mode']}** on {env['cpu']} ({env['machine']}), Python {env['python']}, "
        f"OpenCV {opencv['opencv_version']} build `{opencv['build_sha256'][:12]}`"
        f"{' (KleidiCV)' if opencv.get('kleidicv') else ''}",
        "",
    ]
    results = report["results"]
    if report["mode"] == "kernels":
        lines += ["| operation | median ms | p90 ms | runs |", "|---|---:|---:|---:|"]
        for name, s in results.items():
            lines.append(f"| {name} | {s['median_ms']} | {s['p90_ms']} | {s['runs']} |")
    elif report["mode"] == "scenes":
        lines += ["| scene | stage | median ms | p90 ms |", "|---|---|---:|---:|"]
        for name, r in results.items():
            if not r["ok"]:
                lines.append(f"| {name} | error: {r['error']} | | |")
                continue
            for stage, s in r["stages"].items():
                lines.append(f"| {name} | {stage} | {s['median_ms']} | {s['p90_ms']} |")
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


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repeats", type=int, default=None, help="timed runs per case")
    common.add_argument("--out", type=Path, help="also write the report here, as JSON")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    k = sub.add_parser("kernels", parents=[common], help="the OpenCV operations alone, on synthetic images")
    k.add_argument("--size", type=int, default=2048)
    for name, text in (("scenes", "whole sites, in-process"), ("worker", "a deployed worker")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("--scenes-file", type=Path, default=SCENES_FILE)
        p.add_argument("--backend", choices=("opencv", "cool"), default="opencv")
    sub.choices["worker"].add_argument("--arn", required=True)
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

    if args.mode == "kernels":
        results = kernels(args.size, args.repeats or 20)
    elif args.mode == "scenes":
        results = scenes(load_scenes(args.scenes_file), args.repeats or 3, args.backend)
    else:
        results = worker(args.arn, load_scenes(args.scenes_file), args.repeats or 5, args.backend)

    report = {"mode": args.mode, "environment": environment(), "results": results}
    print(markdown(report))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
