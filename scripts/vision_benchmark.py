#!/usr/bin/env python3
"""Benchmark the vision worker's OpenCV work: stock OpenCV 5 against COOL, x86 against Graviton.

The COOL award asks for *measured* value against a baseline (docs/enhancements/
opencv-agentic-vision-enhancement.md §4). This script is how those numbers are made, the same way
on every machine, so the rows of the report compare like with like:

    python scripts/vision_benchmark.py kernels                    # the OpenCV operations alone
    python scripts/vision_benchmark.py scenes                     # whole sites, read over HTTPS
    python scripts/vision_benchmark.py worker --arn <ARN>         # a deployed worker, end to end

`kernels` times the operations the worker spends its CPU on (resize, Gaussian blur, adaptive
Gaussian threshold, morphology, connected components, contours, the affine warp, and the whole
per-site analysis) on a fixed synthetic image, so network and data never enter it. Run it where
each build lives: stock `opencv-python-headless` on an x86 machine and on Graviton, then inside
OpenCV's COOL image on Graviton3/4. `scenes` runs the worker handler in-process on the pinned
scene list (scripts/vision_benchmark_scenes.json), with the read time reported apart from the
OpenCV time. `worker` invokes a deployed worker and adds the round trip as the caller sees it.

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

SCENES_FILE = Path(__file__).with_name("vision_benchmark_scenes.json")

# ARM's CPU part numbers (/proc/cpuinfo "CPU part") for the cores AWS's Graviton generations use.
ARM_PARTS = {
    "0xd0c": "Neoverse N1 (Graviton2)",
    "0xd40": "Neoverse V1 (Graviton3)",
    "0xd4f": "Neoverse V2 (Graviton4)",
    "0xd84": "Neoverse V3 (Graviton5?)",
}


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


def kernels(size: int = 2048, repeats: int = 20) -> dict:
    import cv2
    import numpy as np
    from vision.analyse import analyse_site
    from vision.detect import DetectParams, stretch

    nir, green = synthetic_scene(size)
    image = stretch(nir, 3000.0)
    binary = cv2.adaptiveThreshold(image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, -25)
    kernel = np.ones((3, 3), np.uint8)
    matrix = np.array([[0.5, 0.0, 0.25], [0.0, 0.5, 0.25]])
    site = [[[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]]]
    params = DetectParams()
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
    }
    return {name: time_it(fn, repeats) for name, fn in cases.items()}


def load_scenes(path: Path = SCENES_FILE) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["scenes"]


def request_for(scene: dict, backend: str) -> dict:
    return {
        "version": 1,
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


def scenes(scene_list: list[dict], repeats: int, backend: str, handler=None, fetch=None) -> dict:
    """The worker handler in-process, per scene: its own read/analyse/annotate/total timings."""
    if handler is None:
        import vision_worker_handler

        handler = vision_worker_handler.lambda_handler
    out = {}
    for scene in scene_list:
        stages: dict[str, list[float]] = {}
        last = None
        for _ in range(repeats):
            reply = handler(request_for(scene, backend), None, **({"fetch": fetch} if fetch else {}))
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
        round_trips, totals, analyse, build = [], [], [], None
        for _ in range(repeats):
            started = time.perf_counter()
            payload = json.dumps({**request_for(scene, backend), "image": False})
            response = client.invoke(FunctionName=arn, Payload=payload)
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
    k = sub.add_parser("kernels", parents=[common], help="the OpenCV operations alone, on a synthetic image")
    k.add_argument("--size", type=int, default=2048)
    for name, text in (("scenes", "whole sites, in-process"), ("worker", "a deployed worker")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("--scenes-file", type=Path, default=SCENES_FILE)
        p.add_argument("--backend", choices=("opencv", "cool"), default="opencv")
    sub.choices["worker"].add_argument("--arn", required=True)
    args = parser.parse_args(argv)

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
