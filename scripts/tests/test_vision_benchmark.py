"""Tests for scripts/vision_benchmark.py and the COOL image recipe it is run in.

Nothing here touches the network or a deployed worker: the scene and worker modes are given a
fake handler and a fake Lambda client.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

import vision_benchmark as bench

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("cpuinfo", "machine", "expected"),
    [
        ("processor : 0\nmodel name : Intel(R) Xeon(R) 8375C\n", "x86_64", "Intel(R) Xeon(R) 8375C"),
        ("processor : 0\nCPU implementer : 0x41\nCPU part : 0xd0c\n", "aarch64", "Neoverse N1 (Graviton2)"),
        ("CPU part\t: 0xd40\n", "aarch64", "Neoverse V1 (Graviton3)"),
        ("CPU part\t: 0xd4f\n", "aarch64", "Neoverse V2 (Graviton4)"),
        ("CPU part\t: 0xabc\n", "aarch64", "Arm core 0xabc"),
        ("", "aarch64", "aarch64"),
    ],
)
def test_cpu_identity_names_the_graviton_generation(cpuinfo, machine, expected):
    assert bench.cpu_identity(cpuinfo, machine) == expected


def test_summarise():
    summary = bench.summarise([5.0, 1.0, 3.0, 2.0, 4.0])
    assert summary == {"runs": 5, "median_ms": 3.0, "p90_ms": 5.0, "min_ms": 1.0}


def test_kernels_time_every_operation_the_worker_uses():
    results = bench.kernels(size=256, repeats=2)
    assert set(results) == {
        "resize_area_half", "gaussian_blur_5", "adaptive_threshold_gaussian_31", "morphology_open_3",
        "connected_components_stats", "find_contours", "warp_affine_nearest_u16", "analyse_site",
    }  # fmt: skip
    assert all(r["runs"] == 2 and r["median_ms"] >= 0 for r in results.values())


def test_the_synthetic_scene_is_deterministic_and_has_ships():
    from vision.analyse import analyse_site

    a, _ = bench.synthetic_scene(256)
    b, green = bench.synthetic_scene(256)
    assert (a == b).all()
    count = analyse_site(a, green, [[[0, 0], [255, 0], [255, 255], [0, 255]]], 10.0).metrics["count"]
    assert count > 0


def test_the_pinned_scenes_are_valid_worker_requests():
    from common import vision_contract

    scenes = bench.load_scenes()
    assert scenes and len({s["name"] for s in scenes}) == len(scenes)
    for scene in scenes:
        vision_contract.validate_request(bench.request_for(scene, "opencv"))


REPLY = {
    "ok": True,
    "metrics": {"count": 7},
    "io": {"requests": 9, "bytes": 5_000_000},
    "timings_ms": {"read": 100.0, "analyse": 20.0, "annotate": 5.0, "total": 125.0},
    "build": {"build_sha256": "ab" * 32},
}


def test_scenes_mode_reports_each_stage():
    calls = []

    def handler(request, context, **kwargs):
        calls.append(request)
        return REPLY

    results = bench.scenes(bench.load_scenes(), repeats=3, backend="cool", handler=handler)
    first = next(iter(results.values()))
    assert first["ok"] and first["count"] == 7 and first["stages"]["analyse"]["median_ms"] == 20.0
    assert len(calls) == 3 * len(bench.load_scenes()) and calls[0]["backend"] == "cool"


def test_scenes_mode_reports_a_failure():
    def refuse(request, context):
        return {"ok": False, "error": "not_cool"}

    results = bench.scenes(bench.load_scenes()[:1], 3, "opencv", handler=refuse)
    expected = {"ok": False, "error": "not_cool", "count": None, "io": None, "stages": {}}
    assert next(iter(results.values())) == expected


class FakeLambda:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def invoke(self, FunctionName, Payload):
        self.calls.append((FunctionName, json.loads(Payload)))
        return {"Payload": io.BytesIO(json.dumps(self.reply).encode())}


def test_worker_mode_adds_the_round_trip_and_asks_for_no_image():
    fake = FakeLambda(REPLY)
    arn = "arn:aws:lambda:us-west-2:123456789012:function:bloggerbear-dev-vision-worker"
    results = bench.worker(arn, bench.load_scenes(), repeats=2, backend="opencv", client=fake)
    first = next(iter(results.values()))
    assert first["ok"] and first["worker_total"]["median_ms"] == 125.0 and first["round_trip"]["runs"] == 2
    assert fake.calls[0][0] == arn and fake.calls[0][1]["image"] is False


def test_markdown_tables():
    env = {"cpu": "Neoverse V2 (Graviton4)", "machine": "aarch64", "python": "3.12.3",
           "opencv": {"opencv_version": "5.0.0", "build_sha256": "f" * 64, "kleidicv": True}}  # fmt: skip
    results = {"find_contours": bench.summarise([1.0])}
    kernels = bench.markdown({"mode": "kernels", "environment": env, "results": results})
    assert "Neoverse V2 (Graviton4)" in kernels and "(KleidiCV)" in kernels
    assert "| find_contours | 1.0 |" in kernels
    failed = {"x": {"ok": False, "error": "e"}}
    scenes = bench.markdown({"mode": "scenes", "environment": env, "results": failed})
    assert "| x | error: e |" in scenes


def test_main_writes_json(tmp_path, capsys):
    out = tmp_path / "report.json"
    assert bench.main(["kernels", "--size", "128", "--repeats", "1", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["mode"] == "kernels" and report["environment"]["opencv"]["opencv_version"].startswith("5.")
    assert "| analyse_site |" in capsys.readouterr().out


# --- the COOL image recipe -----------------------------------------------------------------------

DOCKERFILE = (ROOT / "docker" / "vision-cool" / "Dockerfile").read_text(encoding="utf-8")


def test_the_cool_image_never_installs_its_own_opencv_or_numpy():
    requirements = (ROOT / "lambdas" / "requirements-vision-cool.txt").read_text(encoding="utf-8")
    pins = dict(re.findall(r"^([a-z0-9-]+)==(\S+)$", requirements, re.M))
    assert set(pins) == {"requests", "awslambdaric", "networkx"}
    shared = (ROOT / "lambdas" / "requirements.txt").read_text(encoding="utf-8")
    assert f"requests=={pins['requests']}" in shared
    runs = re.findall(r"^RUN (.+)$", DOCKERFILE, re.M)
    assert not any(word in run for run in runs for word in ("opencv-python", "numpy"))
    assert re.search(r"pip install .*-r /tmp/requirements-vision-cool\.txt", DOCKERFILE)
    assert "requirements-vision.txt" not in DOCKERFILE


def test_the_cool_image_is_a_cool_worker_with_the_same_code():
    assert re.search(r"^ARG COOL_IMAGE$", DOCKERFILE, re.M)
    assert re.search(r"^FROM \$\{COOL_IMAGE\}$", DOCKERFILE, re.M)
    assert "VISION_BACKEND=cool" in DOCKERFILE
    for copied in ("lambdas/vision/", "lambdas/vision_worker_handler.py", "common/vision_contract.py"):
        assert copied in DOCKERFILE
    assert 'CMD ["vision_worker_handler.lambda_handler"]' in DOCKERFILE
    assert "@sha256:" in DOCKERFILE  # the instructions pin the base by digest
