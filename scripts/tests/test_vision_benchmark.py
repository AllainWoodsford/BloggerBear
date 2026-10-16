"""Tests for scripts/vision_benchmark.py and the COOL image recipe it is run in.

Nothing here touches the network or a deployed worker: the scene and worker modes are given a
fake handler and a fake Lambda client, and `fetch-network` a fake Overpass client. The Sydney
rail scene's network file is not committed (Overpass is unreachable from the build container),
so the tests give that scene a stub network where one is needed.
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest

import vision_benchmark as bench
import vision_runs

ROOT = Path(__file__).resolve().parents[2]
SHIP_KERNELS = {
    "resize_area_half", "gaussian_blur_5", "adaptive_threshold_gaussian_31", "morphology_open_3",
    "connected_components_stats", "find_contours", "warp_affine_nearest_u16", "analyse_site",
}  # fmt: skip
RAIL_KERNELS = {
    "distance_transform_l2", "thinning_hitmiss", "polylines", "apply_colormap",
    "connected_components_stats_desert", "analyse_rail_access",
}  # fmt: skip
# A network in the worker's shape, standing in for a scene's network file.
STUB_NETWORK = {
    "ways": [{"id": "w1", "points": [[151.2, -33.9], [151.21, -33.9]], "tunnel": False, "name": "Stub"}],
    "stations": [{"id": "n1", "name": "Stub", "lon": 151.2, "lat": -33.9, "kind": "station"}],
    "pois": [],
    "fetched_at": "2026-10-10T03:00:00+00:00",
}


def ship_scenes():
    return [s for s in bench.load_scenes() if s.get("task", "ships") == "ships"]


def rail_scene():
    return next(s for s in bench.load_scenes() if s.get("task") == "rail_access")


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
    assert set(results) == SHIP_KERNELS | RAIL_KERNELS
    assert all(r["runs"] == 2 and r["median_ms"] >= 0 for r in results.values())


def test_the_synthetic_scene_is_deterministic_and_has_ships():
    from vision.analyse import analyse_site

    a, _ = bench.synthetic_scene(256)
    b, green = bench.synthetic_scene(256)
    assert (a == b).all()
    count = analyse_site(a, green, [[[0, 0], [255, 0], [255, 255], [0, 255]]], 10.0).metrics["count"]
    assert count > 0


def test_the_synthetic_city_and_network_are_deterministic_and_measure():
    from common import vision_contract
    from vision.rail_analyse import analyse_rail_access

    city, again = bench.synthetic_city(128), bench.synthetic_city(128)
    assert set(city) == {"red", "nir", "swir16", "scl"}
    assert all((city[name] == again[name]).all() for name in city)
    assert city["scl"][64, 64] == 5 and city["scl"][2, 125] == 9 and city["scl"][66, 44] == 6
    network = bench.synthetic_network(128)
    assert len(network["ways"]) == 8 and len(network["stations"]) == 40 and len(network["pois"]) == 40
    assert vision_contract.validate_network(network)["stations"] == network["stations"]
    whole = [[[0, 0], [127, 0], [127, 127], [0, 127]]]
    grid = (bench.CITY_PIXEL_M, bench.CITY_TRANSFORM, bench.CITY_EPSG)
    m = analyse_rail_access(city, whole, *grid, network).metrics
    assert m["station_count"] == m["snapped_station_count"] == 40
    assert m["components"] == 1 and 0 < m["served_share"] <= 1
    assert all(s["intermodal"]["bus_stop_near"] >= 1 for s in m["stations"])


def test_load_scenes_makes_the_network_file_absolute():
    scene = rail_scene()
    path = Path(scene["network_file"])
    assert path.is_absolute() and path.parent == bench.SCENES_FILE.parent / "vision_benchmark_networks"
    assert path.name == "sydney.json"


def test_the_pinned_scenes_are_valid_worker_requests():
    from common import vision_contract

    scenes = bench.load_scenes()
    assert scenes and len({s["name"] for s in scenes}) == len(scenes)
    for scene in scenes:
        network = None
        if scene.get("task") == "rail_access" and not Path(scene["network_file"]).is_file():
            network = STUB_NETWORK  # Overpass is unreachable here; the file is fetched by hand
        request = vision_contract.validate_request(bench.request_for(scene, "opencv", network=network))
        assert request["task"] == scene.get("task", "ships")
    sydney = rail_scene()
    assert sydney["name"] == "sydney-rail-2024-01-05" and sydney["scene_id"] == "S2A_56HLH_20240105_0_L2A"
    assert set(sydney["assets"]) == {"red", "nir", "swir16", "scl"}
    bands = ("/B04.tif", "/B08.tif", "/B11.tif", "/SCL.tif")  # red, nir, swir16, scl
    assert all(url.endswith(bands) for url in sydney["assets"].values())
    assert sydney["params"] == {"reach_m": 1000}
    ship = bench.request_for(ship_scenes()[0], "opencv")
    assert ship["task"] == "ships" and "network" not in ship


def test_request_for_a_rail_scene_carries_the_network_file_or_the_one_given(tmp_path):
    scene = {**rail_scene(), "network_file": str(tmp_path / "net.json")}
    with pytest.raises(FileNotFoundError, match="network_file missing"):
        bench.request_for(scene, "opencv")
    assert bench.request_for(scene, "opencv", network=STUB_NETWORK)["network"] == STUB_NETWORK
    (tmp_path / "net.json").write_text(json.dumps(STUB_NETWORK), encoding="utf-8")
    request = bench.request_for(scene, "cool")
    assert request["task"] == "rail_access" and request["network"] == STUB_NETWORK
    with pytest.raises(FileNotFoundError, match="network_file missing"):
        bench.request_for({**scene, "network_file": None}, "opencv")


REPLY = {
    "ok": True,
    "metrics": {"count": 7},
    "io": {"requests": 9, "bytes": 5_000_000},
    "timings_ms": {"read": 100.0, "analyse": 20.0, "annotate": 5.0, "total": 125.0},
    "build": {"build_sha256": "ab" * 32},
}
RAIL_REPLY = {
    **REPLY,
    "task": "rail_access",
    "metrics": {"station_count": 178, "served_share": 0.73},
    "timings_ms": {"read": 900.0, "analyse": 400.0, "graph": 150.0, "annotate": 30.0, "total": 1330.0},
}


def test_scenes_mode_reports_each_stage():
    calls = []

    def handler(request, context, **kwargs):
        calls.append(request)
        return REPLY

    results = bench.scenes(ship_scenes(), repeats=3, backend="cool", handler=handler)
    first = next(iter(results.values()))
    assert first["ok"] and first["count"] == 7 and first["stages"]["analyse"]["median_ms"] == 20.0
    assert len(calls) == 3 * len(ship_scenes()) and calls[0]["backend"] == "cool"


def test_scenes_mode_runs_a_rail_scene_with_its_network_and_reports_the_graph_stage(tmp_path):
    calls = []

    def handler(request, context, **kwargs):
        calls.append(request)
        return RAIL_REPLY

    (tmp_path / "sydney.json").write_text(json.dumps(STUB_NETWORK), encoding="utf-8")
    scene = {**rail_scene(), "network_file": str(tmp_path / "sydney.json")}
    results = bench.scenes([scene], repeats=2, backend="opencv", handler=handler)
    row = results["sydney-rail-2024-01-05"]
    assert row["ok"] and row["stages"]["graph"]["median_ms"] == 150.0 and row["count"] is None
    assert len(calls) == 2 and calls[0]["task"] == "rail_access" and calls[0]["network"] == STUB_NETWORK


def test_scenes_mode_reports_a_failure():
    def refuse(request, context):
        return {"ok": False, "error": "not_cool"}

    results = bench.scenes(ship_scenes()[:1], 3, "opencv", handler=refuse)
    expected = {"ok": False, "error": "not_cool", "count": None, "io": None, "stages": {}}
    assert next(iter(results.values())) == expected


def test_scenes_and_worker_modes_report_a_missing_network_file(tmp_path):
    def never(*args, **kwargs):
        raise AssertionError("the handler must not run without a network")

    scene = {**rail_scene(), "network_file": str(tmp_path / "nowhere.json")}
    results = bench.scenes([scene], 1, "opencv", handler=never)
    expected = {"ok": False, "error": "network_file missing", "count": None, "io": None, "stages": {}}
    assert results == {"sydney-rail-2024-01-05": expected}
    fake = FakeLambda(REPLY)
    arn = "arn:aws:lambda:us-west-2:123456789012:function:w"
    results = bench.worker(arn, [scene], 1, "opencv", client=fake)
    assert results == {"sydney-rail-2024-01-05": {"ok": False, "error": "network_file missing"}}
    assert fake.calls == []
    # In the report it reads as any other failed row.
    opencv = {"opencv_version": "5.0.0", "build_sha256": "f" * 64}
    env = {"cpu": "x", "machine": "x86_64", "python": "3.11", "opencv": opencv}
    assert "| sydney-rail-2024-01-05 | error: network_file missing |" in bench.markdown(
        {"mode": "scenes", "environment": env, "results": results | {"sydney-rail-2024-01-05": expected}}
    )


class FakeLambda:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def invoke(self, FunctionName, Payload):
        self.calls.append((FunctionName, json.loads(Payload)))
        return {"Payload": io.BytesIO(json.dumps(self.reply).encode())}


def test_worker_mode_adds_the_round_trip_and_asks_for_no_image():
    fake = FakeLambda(REPLY)
    arn = "arn:aws:lambda:us-west-2:123456789012:function:bloggerbear-dev-vision-worker"
    results = bench.worker(arn, ship_scenes(), repeats=2, backend="opencv", client=fake)
    first = next(iter(results.values()))
    assert first["ok"] and first["worker_total"]["median_ms"] == 125.0 and first["round_trip"]["runs"] == 2
    assert fake.calls[0][0] == arn and fake.calls[0][1]["image"] is False


def test_fetch_network_writes_the_scenes_network_file(tmp_path, monkeypatch, capsys):
    from common import osm

    polygons = []

    def fake_fetch(polygon):
        polygons.append(polygon)
        return {**STUB_NETWORK, "counts": {"ways": 1, "stations": 1, "pois": 0, "points": 2}}

    out = tmp_path / "networks" / "sydney.json"
    network = bench.fetch_network(rail_scene(), out, fetch=fake_fetch)
    assert polygons == [rail_scene()["polygon"]] and json.loads(out.read_text()) == network
    # The subcommand goes through the OSM client, writes the scene's network_file by default and
    # says what it wrote; an unknown scene is a usage error.
    monkeypatch.setattr(osm, "fetch_network", fake_fetch)
    scenes_file = tmp_path / "scenes.json"
    scenes_file.write_text(
        json.dumps({"scenes": [{**rail_scene(), "network_file": "nets/sydney.json"}]}), encoding="utf-8"
    )
    argv = ["fetch-network", "--scenes-file", str(scenes_file), "--scene", "sydney-rail-2024-01-05"]
    assert bench.main(argv) == 0
    assert json.loads((tmp_path / "nets" / "sydney.json").read_text()) == network
    assert "wrote" in capsys.readouterr().out
    elsewhere = tmp_path / "elsewhere.json"
    assert bench.main([*argv, "--out", str(elsewhere)]) == 0
    assert elsewhere.is_file()
    with pytest.raises(SystemExit):
        bench.main(["fetch-network", "--scenes-file", str(scenes_file), "--scene", "nope"])


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
    printed = capsys.readouterr().out
    assert "| analyse_site |" in printed and "| analyse_rail_access |" in printed


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


# --- the submission round: what a run records ---------------------------------------------------------
#
# Nothing below touches the network either: requests, urllib and socket connections are made to
# raise for every test in this section, so a path that reached for the bucket or IMDS would fail
# loudly instead of quietly succeeding on a machine that happens to be online.


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket
    import urllib.request

    import requests

    def refuse(*args, **kwargs):
        raise AssertionError("the tests must not touch the network")

    for name in ("get", "head", "put", "post", "request"):
        monkeypatch.setattr(requests, name, refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def tiny_png() -> bytes:
    import cv2
    import numpy as np

    image = np.zeros((4, 6, 3), np.uint8)
    image[1:3, 2:4] = (10, 200, 30)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def rail_metrics() -> dict:
    return {
        "task": "rail_access", "coverage": 0.98, "served_share": 0.7312, "desert_km2": 41.25,
        "built_up_km2": 312.5, "served_km2": 271.25, "station_count": 178, "snapped_station_count": 170,
        "node_count": 240,
        "edge_count": 251, "components": 2, "hubs": ["n7", "n3", "n12"], "interchanges": ["n7"],
        "flags": [
            {"type": "isolated_high_demand", "station": "n40", "name": "Far", "activity": 9.1},
            {"type": "ghost_line", "stations": ["n1", "n2", "n3"], "names": ["A", "B", "C"]},
        ],
        "suggestions": [
            {"type": "orbital_link", "from": "n40", "to": "n41", "straight_km": 2.1},
            {"type": "feeder_corridor", "station": "n40", "hub": "n7", "length_m": 5000.0},
            {"type": "feeder_corridor", "station": "n41", "hub": "n7", "length_m": 4800.0},
            {"type": "feeder_corridor", "station": "n42", "hub": "n3", "length_m": 4700.0},
        ],
        "stations": [], "deserts": [], "edges": [], "truncated": {}, "quality_flags": [], "warnings": [],
    }  # fmt: skip


def rail_reply(metrics: dict | None = None, png: bytes | None = None, total: float = 1330.0) -> dict:
    import base64

    return {
        **RAIL_REPLY,
        "metrics": metrics or rail_metrics(),
        "image_png_b64": base64.b64encode(png if png is not None else tiny_png()).decode("ascii"),
        "timings_ms": {"read": 900.0, "analyse": 400.0, "graph": 150.0, "annotate": 30.0, "total": total},
    }


def rail_scene_with_network(tmp_path) -> dict:
    (tmp_path / "sydney.json").write_text(json.dumps(STUB_NETWORK), encoding="utf-8")
    return {**rail_scene(), "network_file": str(tmp_path / "sydney.json")}


def test_scenes_mode_records_the_rail_metrics_their_digest_the_figure_and_every_sample(tmp_path):
    png = tiny_png()
    replies = iter([rail_reply(png=png, total=1330.0), rail_reply(png=png, total=1340.5)])
    results = bench.scenes(
        [rail_scene_with_network(tmp_path)], 2, "opencv", handler=lambda *a, **k: next(replies)
    )
    row = results["sydney-rail-2024-01-05"]
    assert row["ok"] and row["count"] is None
    assert row["metrics"] == {
        "served_share": 0.7312, "desert_km2": 41.25, "built_up_km2": 312.5, "station_count": 178,
        "snapped_station_count": 170, "node_count": 240, "edge_count": 251, "components": 2,
        "hubs": ["n7", "n3", "n12"],
        "flags": [["isolated_high_demand", "n40"], ["ghost_line", "n1+n2+n3"]],
        "suggestions": ["orbital_link:n40>n41", "feeder_corridor:n40>n7", "feeder_corridor:n41>n7"],
    }  # fmt: skip
    assert row["metrics_digest"] == vision_runs.digest(row["metrics"])
    assert row["image_png_sha256"] == vision_runs.sha256_bytes(png) and row["image_shape"] == [4, 6, 3]
    assert row["png"] == png and row["figure"] is None
    assert row["samples_ms"]["total"] == [1330.0, 1340.5] and row["samples_ms"]["graph"] == [150.0, 150.0]
    assert row["stages"]["total"]["median_ms"] == 1335.25
    assert row["peak_scope"] == "process" and (row["peak_rss_mb"] is None or row["peak_rss_mb"] > 0)
    # A ship scene keeps its count, digested the same way.
    ship = bench.scenes(ship_scenes()[:1], 1, "opencv", handler=lambda *a, **k: REPLY)
    first = next(iter(ship.values()))
    assert first["metrics"] == {"count": 7} and first["metrics_digest"] == vision_runs.digest({"count": 7})
    assert first["image_png_sha256"] is None  # REPLY carries no figure


def test_the_digest_is_canonical_sorted_keys_and_six_places():
    assert vision_runs.digest({"b": 1.00000004, "a": [1.5, {"z": 2}]}) == vision_runs.digest(
        {"a": [1.5, {"z": 2}], "b": 1.0}
    )
    assert vision_runs.digest({"a": 1.0000006}) != vision_runs.digest({"a": 1.0})


def test_main_writes_the_run_file_schema_and_its_run_id(tmp_path, capsys):
    runs = tmp_path / "runs"
    code = bench.main(
        ["kernels", "--size", "64", "--repeats", "2", "--threads", "1", "--out", str(runs), "--tag", "1t"]
    )
    assert code == 0
    files = list(runs.glob("*.json"))
    assert len(files) == 1
    run = vision_runs.load(files[0])
    date = run["started_at"][:10]
    slug = vision_runs.cpu_slug(run["environment"]["cpu"])
    assert run["run_id"] == f"{date}-bench-kernels-{slug}-opencv-1t" and files[0].stem == run["run_id"]
    assert run["schema"] == "bloggerbear.vision-eval/1" and run["kind"] == "bench-kernels"
    assert run["mode"] == "kernels" and run["backend"] == "opencv"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", run["finished_at"])
    assert run["started_at"] <= run["finished_at"]
    assert set(run["code"]) == {"git_sha", "dirty"}
    env = run["environment"]
    for key in ("cpu", "machine", "python", "opencv", "cpu_count", "affinity", "threads", "numpy", "networkx",
                "where", "host", "cool", "libraries"):  # fmt: skip
        assert key in env, key
    assert env["threads"] == 1 and env["opencv"]["cv2_path"].endswith((".py", ".so", ".pyd"))
    assert env["host"] is None and env["cool"] is None and env["libraries"] is None
    assert env["where"] in ("ec2", "wsl", "windows", "macos", "linux", "ci")
    assert run["worker"] == {
        "kind": "in-process",
        "region": None,
        "code_sha256": run["worker"]["code_sha256"],
    }
    assert len(run["worker"]["code_sha256"]) == 64
    assert run["inputs"] == {"manifest_sha256": None, "cache": False, "scenes": [], "networks": []}
    assert (
        run["params"]["size"] == 64 and run["params"]["threads"] == 1 and run["params"]["backend"] == "opencv"
    )
    assert set(run["results"]) == SHIP_KERNELS | RAIL_KERNELS
    assert len(run["results"]["find_contours"]["samples_ms"]) == 2
    stages = run["results"]["analyse_rail_access"]["stages"]
    assert "graph" in stages and stages["graph"]["runs"] == 2 and len(stages["graph"]["samples_ms"]) == 2
    assert run["summary"]["analyse_rail_access"] == run["results"]["analyse_rail_access"]["median_ms"]
    assert run["summary"]["analyse_rail_access.graph_ms"] == stages["graph"]["median_ms"]
    assert run["checks"] == [] and run["agent"] is None
    printed = capsys.readouterr().out
    assert "| analyse_rail_access.graph |" in printed and "threads 1" in printed and "wrote" in printed


def test_cpu_slug_and_run_id():
    assert vision_runs.cpu_slug("Neoverse V2 (Graviton4)") == "neoverse-v2-graviton4"
    assert (
        vision_runs.cpu_slug("Intel(R) Xeon(R) Platinum 8375C CPU @ 2.90GHz")
        == "intel-xeon-platinum-8375c-cpu-2-90ghz"
    )
    run_id = vision_runs.make_run_id("bench-scenes", "Neoverse V2 (Graviton4)", "cool", "4t r1", "2026-10-19")
    assert run_id == "2026-10-19-bench-scenes-neoverse-v2-graviton4-cool-4t-r1"
    with pytest.raises(ValueError, match="unknown run kind"):
        vision_runs.make_run_id("bench-nothing", "x", "opencv")


def test_out_path_takes_a_directory_or_a_file(tmp_path):
    run = {"run_id": "r1"}
    assert vision_runs.out_path(run, tmp_path / "runs") == tmp_path / "runs" / "r1.json"
    assert vision_runs.out_path(run, tmp_path / "x.json") == tmp_path / "x.json"
    (tmp_path / "there").mkdir()
    assert vision_runs.out_path(run, tmp_path / "there") == tmp_path / "there" / "r1.json"


def test_new_run_keeps_the_start_it_is_given_and_cache_dir_expands_the_home(tmp_path):
    env = {"cpu": "x", "machine": "x86_64", "python": "3.11", "opencv": {}}
    run = vision_runs.new_run("bench-kernels", "x", "opencv", env, started="2026-10-19T01:02:03Z")
    assert run["started_at"] == "2026-10-19T01:02:03Z"
    assert run["run_id"] == "2026-10-19-bench-kernels-x-opencv"
    # The runsheet's `C="--cache ~/cogs"` reaches the script with the tilde unexpanded.
    assert vision_runs.cache_dir("~/cogs") == Path.home() / "cogs"
    assert vision_runs.cache_dir(str(tmp_path / "cogs")) == tmp_path / "cogs"


def test_threads_and_the_affinity_are_recorded_and_pin_cpu_is_refused_off_linux(
    tmp_path, monkeypatch, capsys
):
    pins = []
    monkeypatch.setattr(
        os, "sched_setaffinity", lambda pid, cpus: pins.append((pid, set(cpus))), raising=False
    )
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {2}, raising=False)
    out = tmp_path / "pinned.json"
    assert (
        bench.main(
            [
                "kernels",
                "--size",
                "64",
                "--repeats",
                "1",
                "--threads",
                "2",
                "--pin-cpu",
                "2",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    run = vision_runs.load(out)
    assert pins == [(0, {2})] and run["environment"]["affinity"] == [2] and run["environment"]["threads"] == 2
    assert run["params"]["pin_cpu"] == 2
    monkeypatch.delattr(os, "sched_setaffinity")
    assert bench.main(["kernels", "--size", "64", "--repeats", "1", "--pin-cpu", "0"]) == 2
    assert "sched_setaffinity" in capsys.readouterr().err
    monkeypatch.delattr(os, "sched_getaffinity")
    assert vision_runs.affinity() is None


def test_peak_rss_units_and_absence():
    assert vision_runs.rss_to_mb(2048, system="linux") == 2.0  # kilobytes on Linux
    assert vision_runs.rss_to_mb(2 * 1024 * 1024, system="darwin") == 2.0  # bytes on macOS
    try:
        import resource  # noqa: F401
    except ImportError:
        assert vision_runs.peak_rss_mb() is None
    else:
        assert vision_runs.peak_rss_mb() > 0


def test_isolate_runs_each_scene_in_a_child_and_takes_the_childs_peak(tmp_path):
    scene = rail_scene_with_network(tmp_path)
    argvs = []

    def fake_child(argv):
        argvs.append(argv)
        out = Path(argv[argv.index("--out") + 1])
        env = {
            "cpu": "x",
            "machine": "x86_64",
            "python": "3.11",
            "opencv": {"opencv_version": "5.0.0", "build_sha256": "f" * 64},
        }
        row = bench.scene_row(rail_reply(), {"read": [1.0], "analyse": [2.0], "total": [3.0]})
        run = vision_runs.new_run("bench-scenes", "x", "opencv", env, mode="scenes")
        results = {scene["name"]: row}
        bench.write_figures(results, out)
        vision_runs.write(vision_runs.finish(run, results), out)
        return 0, 150 * 1024  # ru_maxrss in kilobytes: 150 MB

    results = bench.scenes(
        [scene],
        2,
        "opencv",
        isolate=lambda s: bench.isolate_scene(s, ["scenes", "--repeats", "2"], run_child=fake_child),
    )
    row = results[scene["name"]]
    assert row["ok"] and row["peak_rss_mb"] == 150.0 and row["peak_scope"] == "scene"
    assert row["png"] == tiny_png() and row["figure"] is None and row["samples_ms"]["total"] == [3.0]
    assert argvs[0][1].endswith("vision_benchmark.py") and argvs[0][2:5] == ["scenes", "--repeats", "2"]
    assert argvs[0][-4:-2] == ["--only", scene["name"]] and argvs[0][-2] == "--out"
    failed = bench.isolate_scene(scene, ["scenes"], run_child=lambda argv: (3, None))
    assert failed == {"ok": False, "error": "isolated run exited 3", "count": None, "io": None, "stages": {}}
    assert bench.child_args(
        ["scenes", "--isolate", "--only", "a", "--out=x", "--tag", "t", "--repeats", "5"]
    ) == ["scenes", "--repeats", "5"]


def test_only_picks_scenes_and_an_unknown_name_is_a_usage_error(tmp_path):
    scenes = bench.load_scenes()
    assert [s["name"] for s in bench.select_scenes(scenes, ["sydney-rail-2024-01-05"])] == [
        "sydney-rail-2024-01-05"
    ]
    assert bench.select_scenes(scenes, None) == scenes
    with pytest.raises(ValueError, match="no scene named"):
        bench.select_scenes(scenes, ["nope"])
    with pytest.raises(SystemExit) as exc:
        bench.main(["scenes", "--only", "nope", "--repeats", "1"])
    assert exc.value.code == 2
    calls = []
    fake = FakeLambda(REPLY)
    results = bench.worker(
        "arn:aws:lambda:us-west-2:123456789012:function:w",
        bench.select_scenes(scenes, ["botany-bay-2024-01-05"]),
        1,
        "opencv",
        client=fake,
    )
    assert list(results) == ["botany-bay-2024-01-05"] and not calls
    # Every site id a deployed worker is sent is prefixed, so its lines never pass for production's.
    assert fake.calls[0][1]["site"]["id"] == "bench-botany-bay"
    assert results["botany-bay-2024-01-05"]["samples_ms"]["worker_total"] == [125.0]
    assert results["botany-bay-2024-01-05"]["metrics"] == {"count": 7}


def test_the_cache_fills_then_serves_and_refuses_a_changed_etag(tmp_path):
    url = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/x/SCL.tif"
    fetched = []

    def fetch(u, start, end):
        fetched.append((u, start, end))
        return bytes(range(start, end + 1))

    cache = vision_runs.CachedFetch(tmp_path / "cogs", {url: "etag-1"}, fetch=fetch)
    assert cache(url, 0, 9) == bytes(range(10)) and fetched == [(url, 0, 9)]
    assert cache(url, 0, 9) == bytes(range(10)) and fetched == [(url, 0, 9)]  # served, not fetched
    assert cache.hits == 1 and cache.misses == 1
    index = json.loads((tmp_path / "cogs" / "index.json").read_text())
    key = vision_runs.CachedFetch.key(url, "etag-1", 0, 9)
    assert index == {key: {"url": url, "etag": "etag-1", "start": 0, "end": 9, "bytes": 10}}
    assert (tmp_path / "cogs" / key[:2] / key).is_file()
    assert cache.index_sha256() == vision_runs.sha256_file(tmp_path / "cogs" / "index.json")
    # The object changed: the entry under the old ETag is refused and replaced, never served.
    again = vision_runs.CachedFetch(tmp_path / "cogs", {url: "etag-2"}, fetch=fetch)
    assert again(url, 0, 9) == bytes(range(10)) and len(fetched) == 2 and again.refused == 1
    assert key not in again.index and not (tmp_path / "cogs" / key[:2] / key).exists()
    # A URL without a known ETag is HEADed once; any other host is refused before that.
    heads = []
    lazy = vision_runs.CachedFetch(
        tmp_path / "cogs", {}, fetch=fetch, head=lambda u: heads.append(u) or {"etag": "e", "bytes": 1}
    )
    lazy(url, 0, 1)
    lazy(url, 2, 3)
    assert heads == [url]
    with pytest.raises(vision_runs.WrongHost):
        lazy("https://example.com/x.tif", 0, 1)
    assert vision_runs.default_cache_dir().name == "bloggerbear-cogs"


def test_a_manifest_etag_that_changed_refuses_the_run_before_any_read(tmp_path, monkeypatch, capsys):
    scene = rail_scene_with_network(tmp_path)
    pinned_url = scene["assets"]["red"]
    manifest = {"scenes": [{"assets": {"red": {"url": pinned_url, "etag": "pinned-etag", "bytes": 1}}}]}
    heads = []

    def head(url):
        heads.append(url)
        return {"etag": "another-etag" if url == pinned_url else "x", "bytes": 1}

    with pytest.raises(vision_runs.InputChanged, match=f"input changed: {re.escape(pinned_url)}"):
        bench.prepare_inputs([scene], manifest, None, head=head)
    assert len(heads) == 4  # every asset HEADed, nothing read
    # Through main: exit 1, the handler never runs; with no cache and no pin, nothing is HEADed.
    import vision_worker_handler

    monkeypatch.setattr(vision_worker_handler, "lambda_handler", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(vision_runs, "default_head", head)
    scenes_file = tmp_path / "scenes.json"
    scenes_file.write_text(
        json.dumps({"scenes": [{**scene, "network_file": "sydney.json"}]}), encoding="utf-8"
    )
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    heads.clear()
    assert (
        bench.main(
            ["scenes", "--scenes-file", str(scenes_file), "--manifest", str(manifest_file), "--repeats", "1"]
        )
        == 1
    )
    assert len(heads) == 4
    # With a matching pin the run proceeds, the cache is used, and the run file records both.
    monkeypatch.setattr(vision_runs, "default_head", lambda url: {"etag": "pinned-etag", "bytes": 1})
    monkeypatch.setattr(
        vision_worker_handler, "lambda_handler", lambda request, context, fetch=None: rail_reply()
    )
    out = tmp_path / "run.json"
    argv = ["scenes", "--scenes-file", str(scenes_file), "--manifest", str(manifest_file), "--repeats", "1"]
    code = bench.main([*argv, "--cache", str(tmp_path / "cogs"), "--out", str(out)])
    assert code == 0
    run = vision_runs.load(out)
    assert (
        run["inputs"]["cache"] is True and run["inputs"]["cache_index_sha256"] is None
    )  # nothing was fetched
    assert run["inputs"]["manifest_sha256"] == vision_runs.sha256_file(manifest_file)
    assert run["inputs"]["scenes"][0]["etag_by_asset"]["red"] == "pinned-etag"
    assert run["inputs"]["networks"] == [
        {"file": "sydney.json", "sha256": vision_runs.sha256_file(tmp_path / "sydney.json")}
    ]
    row = run["results"]["sydney-rail-2024-01-05"]
    assert (
        row["figure"] == "run.sydney-rail-2024-01-05.png"
        and (tmp_path / row["figure"]).read_bytes() == tiny_png()
    )
    assert "png" not in row and row["metrics"]["served_share"] == 0.7312
    assert run["summary"]["sydney-rail-2024-01-05.served_share"] == 0.7312
    assert run["summary"]["sydney-rail-2024-01-05.total_ms"] == 1330.0
    assert run["started_at"] <= run["finished_at"]
    # A scenes file whose asset is at another host is refused before any request, with exit 1.
    foreign = {**scene, "network_file": "sydney.json", "assets": {**scene["assets"]}}
    foreign["assets"]["red"] = "https://example.com/B04.tif"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"scenes": [foreign]}), encoding="utf-8")
    argv = ["scenes", "--scenes-file", str(elsewhere), "--manifest", str(manifest_file), "--repeats", "1"]
    assert bench.main([*argv, "--cache", str(tmp_path / "cogs")]) == 1
    assert "not under https://sentinel-cogs" in capsys.readouterr().err


class FakeImds:
    def __init__(self, document, fail=False):
        self.document, self.fail, self.calls = document, fail, []

    def put(self, url, headers, timeout, proxies=None):
        self.calls.append(("PUT", url, headers, timeout, proxies))
        if self.fail:
            raise OSError("connect timeout")
        return FakeResponse(200, text="token-1")

    def get(self, url, headers, timeout, proxies=None):
        self.calls.append(("GET", url, headers, timeout, proxies))
        return FakeResponse(200, payload=self.document)


class FakeResponse:
    def __init__(self, status_code, text="", payload=None):
        self.status_code, self.text, self.payload = status_code, text, payload

    def json(self):
        return self.payload


IDENTITY = {
    "accountId": "123456789012", "instanceId": "i-0abc", "privateIp": "10.0.0.5",
    "instanceType": "c8g.xlarge", "imageId": "ami-0123", "region": "us-west-2", "architecture": "arm64",
    "marketplaceProductCodes": ["abc123"], "pendingTime": "2026-10-19T00:00:00Z",
    "availabilityZone": "us-west-2a",
}  # fmt: skip


def test_imds_makes_both_calls_with_one_second_timeouts_and_keeps_five_fields():
    fake = FakeImds(IDENTITY)
    host = bench.imds_identity(http=fake)
    assert host == {"instance_type": "c8g.xlarge", "ami_id": "ami-0123", "region": "us-west-2",
                    "architecture": "arm64", "marketplace_product_codes": ["abc123"]}  # fmt: skip
    assert "accountId" not in json.dumps(host) and "123456789012" not in json.dumps(host)
    put, get = fake.calls
    assert put[1] == "http://169.254.169.254/latest/api/token" and put[3] == (1.0, 1.0)
    assert put[2] == {"X-aws-ec2-metadata-token-ttl-seconds": "60"}
    assert get[1] == "http://169.254.169.254/latest/dynamic/instance-identity/document" and get[3] == (
        1.0,
        1.0,
    )
    assert get[2] == {"X-aws-ec2-metadata-token": "token-1"}
    # The link-local service is never asked through a proxy from the environment.
    assert put[4] == get[4] == {"http": None, "https": None}
    # Off EC2 the connection fails inside the timeout: host is null.
    assert bench.imds_identity(http=FakeImds(IDENTITY, fail=True)) is None


def test_imds_off_ec2_is_null_and_said(tmp_path, capsys):
    out = tmp_path / "run.json"
    assert bench.main(["kernels", "--size", "64", "--repeats", "1", "--imds", "--out", str(out)]) == 0
    run = vision_runs.load(out)
    assert run["environment"]["host"] is None and run["params"]["imds"] is True
    assert "host: null (not on EC2" in capsys.readouterr().out


def test_labels_are_parsed_into_the_cool_record_and_an_unknown_key_is_refused(tmp_path):
    labels = [
        "cool_product=prod-abc",
        "cool_version=1.2",
        "cool_delivery=ami",
        "image_digest=sha256:ff",
        "price=0.1 USD/h",
    ]
    assert bench.parse_labels(labels) == {"product": "prod-abc", "version": "1.2", "delivery": "ami",
                                          "image_digest": "sha256:ff", "price": "0.1 USD/h"}  # fmt: skip
    assert bench.parse_labels(["cool_product=p"]) == {"product": "p", "version": None, "delivery": None,
                                                      "image_digest": None, "price": None}  # fmt: skip
    assert bench.parse_labels([]) is None
    with pytest.raises(ValueError, match="one of"):
        bench.parse_labels(["vendor=x"])
    with pytest.raises(ValueError, match="takes k=v"):
        bench.parse_labels(["cool_product"])
    with pytest.raises(ValueError, match="ami or container|takes one of"):
        bench.parse_labels(["cool_delivery=zip"])
    with pytest.raises(SystemExit) as exc:
        bench.main(["kernels", "--size", "64", "--repeats", "1", "--label", "vendor=x"])
    assert exc.value.code == 2
    out = tmp_path / "run.json"
    assert (
        bench.main(
            [
                "kernels",
                "--size",
                "64",
                "--repeats",
                "1",
                "--label",
                "cool_delivery=container",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert vision_runs.load(out)["environment"]["cool"]["delivery"] == "container"


def test_mapped_libraries_from_a_maps_fixture_with_dpkg_rpm_and_pip_records(tmp_path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    cv2_so = site / "cv2" / "cv2.abi3.so"
    core = tmp_path / "usr" / "lib" / "aarch64-linux-gnu" / "libopencv_core.so.5.0.0"
    kleidi = tmp_path / "opt" / "cool" / "libkleidicv.so.0.3"
    libc = tmp_path / "usr" / "lib" / "libc.so.6"
    for path, content in ((cv2_so, b"cv2!"), (core, b"core!!"), (kleidi, b"k"), (libc, b"c")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    record = site / "opencv_python_headless-5.0.0.93.dist-info" / "RECORD"
    record.parent.mkdir(parents=True)
    record.write_text("cv2/__init__.py,sha256=abc,10\ncv2/cv2.abi3.so,sha256=def,4\n", encoding="utf-8")
    maps = "\n".join([
        f"7f00-7f10 r-xp 00000000 08:01 1 {cv2_so}",
        f"7f10-7f20 r--p 00000000 08:01 1 {cv2_so}",  # a second mapping of the same file: listed once
        f"7f20-7f30 r-xp 00000000 08:01 2 {core}",
        f"7f30-7f40 r-xp 00000000 08:01 3 {kleidi}",
        f"7f40-7f50 r-xp 00000000 08:01 4 {libc}",
        "7f50-7f60 rw-p 00000000 00:00 0 [heap]",
        "7f60-7f70 rw-p 00000000 00:00 0",
    ])  # fmt: skip

    def which(tool):
        return "/usr/bin/dpkg" if tool == "dpkg" else None

    def run(argv, capture_output, text, timeout):
        class Done:
            returncode = 0 if "libopencv" in argv[-1] else 1
            stdout = "libopencv-core5.0:arm64: /usr/lib/aarch64-linux-gnu/libopencv_core.so.5.0.0\n"

        return Done()

    libraries = bench.mapped_libraries(maps, which=which, run=run)
    assert [lib["path"] for lib in libraries] == [str(cv2_so), str(core), str(kleidi)]
    assert libraries[0] == {"path": str(cv2_so), "bytes": 4, "sha256": vision_runs.sha256_bytes(b"cv2!"),
                            "package": "opencv_python_headless-5.0.0.93.dist-info"}  # fmt: skip
    assert libraries[1]["package"] == "libopencv-core5.0" and libraries[1]["bytes"] == 6
    assert libraries[2]["package"] == "not packaged"
    class RpmDone:
        returncode, stdout = 0, "opencv-core-5.0.0-1.aarch64\n"

    rpm = bench.package_of(str(core), which=lambda t: "/usr/bin/rpm" if t == "rpm" else None,
                           run=lambda *a, **k: RpmDone())  # fmt: skip
    assert rpm == "opencv-core-5.0.0-1.aarch64"
    assert (
        bench.mapped_libraries(system="Darwin") is None and bench.mapped_libraries(system="Windows") is None
    )
    # On this machine the real scan finds the cv2 extension that is loaded (Linux only).
    import platform

    if platform.system() == "Linux":
        import cv2  # noqa: F401 - loaded, so mapped

        real = bench.mapped_libraries()
        assert real and any("cv2" in Path(lib["path"]).name for lib in real)
        assert all(len(lib["sha256"]) == 64 and lib["bytes"] > 0 for lib in real)


BUILD_TEXT = """
General configuration for OpenCV 5.0.0 =====================================
  Version control:               5.0.0
  Platform:
    Timestamp:                   2026-07-28T09:22:32Z
    Host:                        Linux 6.8.0 aarch64
  C/C++:
    C++ standard:                17
    C++ Compiler:                /usr/bin/c++  (ver 13.2.0)
    C++ flags (Release):         -mcpu=neoverse-v2 -O3 -DNDEBUG
    C++ flags (Debug):           -mcpu=neoverse-v2 -g
    C Compiler:                  /usr/bin/cc
    C flags (Release):           -O3
    Linker flags (Release):      -Wl,--gc-sections
  Other third-party libraries:
    KleidiCV:                    YES (0.3.0)
    Custom HAL:                  YES (kleidicv_hal)
  Vendor:                      COOL for AWS Graviton4
  Python 3:
    Interpreter:                 /usr/bin/python3
"""


def test_build_lines_keep_what_tells_a_vendor_build_apart():
    lines = bench.build_lines(BUILD_TEXT)
    assert lines == [
        "C++ Compiler:                /usr/bin/c++  (ver 13.2.0)",
        "C++ flags (Release):         -mcpu=neoverse-v2 -O3 -DNDEBUG",
        "C++ flags (Debug):           -mcpu=neoverse-v2 -g",
        "C Compiler:                  /usr/bin/cc",
        "C flags (Release):           -O3",
        "KleidiCV:                    YES (0.3.0)",
        "Custom HAL:                  YES (kleidicv_hal)",
        "Vendor:                      COOL for AWS Graviton4",
    ]
    assert not any("Timestamp" in line or "Interpreter" in line for line in lines)


def run_dict(kind, cpu, backend, results, scenes_inputs=None, networks=None, size=None, run_id=None):
    env = {
        "cpu": cpu,
        "machine": "aarch64",
        "python": "3.12.3",
        "opencv": {"opencv_version": "5.0.0", "build_sha256": backend * 32},
    }
    inputs = {"scenes": scenes_inputs or [], "networks": networks or []}
    run = vision_runs.new_run(
        kind, cpu, backend, env, inputs=inputs, params={"size": size} if size else {}, mode=kind[6:]
    )
    if run_id:
        run["run_id"] = run_id
    return vision_runs.finish(run, results)


def scene_results(seed, served=0.7312, stages=("read", "analyse", "graph", "annotate", "total"), scale=1.0):
    import numpy as np

    rng = np.random.default_rng(seed)
    base = {"read": 900.0, "analyse": 400.0, "graph": 150.0, "annotate": 30.0, "total": 1330.0}
    samples = {s: [round(float(base[s] * scale + rng.normal(0, 2)), 3) for _ in range(5)] for s in stages}
    metrics = bench.metrics_record({**rail_metrics(), "served_share": served}, "rail_access")
    return {"sydney-rail-2024-01-05": {
        "ok": True, "error": None, "count": None, "io": None, "metrics": metrics,
        "metrics_digest": vision_runs.digest(metrics), "image_png_sha256": "a" * 64, "image_shape": [4, 6, 3],
        "figure": None, "stages": {s: bench.summarise(v) for s, v in samples.items()}, "samples_ms": samples,
    }}  # fmt: skip


INPUTS = [
    {"name": "sydney-rail-2024-01-05", "scene_id": "S2A_56HLH_20240105_0_L2A", "etag_by_asset": {"red": "e1"}}
]
NETS = [{"file": "vision_benchmark_networks/sydney.json", "sha256": "n" * 64}]


def test_compare_is_reproducible_with_a_seed_and_reports_the_compute_headline_apart_from_the_read():
    a = run_dict(
        "bench-scenes", "Neoverse V2 (Graviton4)", "opencv", scene_results(1), INPUTS, NETS, run_id="A"
    )
    b = run_dict(
        "bench-scenes",
        "Neoverse V2 (Graviton4)",
        "cool",
        scene_results(2, scale=0.8),
        INPUTS,
        NETS,
        run_id="B",
    )
    first, second = bench.compare(a, b, 300, 0), bench.compare(a, b, 300, 0)
    assert first == second
    assert bench.compare(a, b, 300, 1)["headline"]["ci_low"] != first["headline"]["ci_low"]
    row = first["scenes"]["sydney-rail-2024-01-05"]
    assert row["compute_stages"] == ["analyse", "annotate"]  # graph is inside analyse; read is apart
    assert (
        1.15 < row["headline"]["ratio"] < 1.35
        and row["headline"]["ci_low"] <= row["headline"]["ratio"] <= row["headline"]["ci_high"]
    )
    assert row["read"] == row["stages"]["read"] and 1.1 < row["read"]["ratio"] < 1.4
    assert abs(row["stages"]["analyse"]["share_a"] - 400 / 1330) < 0.02
    assert set(row["stages"]) == {"read", "analyse", "graph", "annotate", "total"}
    assert row["parity"] == {"pass": True, "equal_digest": True, "differences": []}
    assert row["pixel_mismatch"] == {"share": 0.0, "note": "identical PNG bytes"}
    assert first["parity_pass"] and first["headline"] == row["headline"]
    summary = bench.compare_summary(first)
    assert summary["headline_ratio"] == first["headline"]["ratio"] and summary["parity_pass"] == 1
    assert summary["sydney-rail-2024-01-05.graph_ratio"] == row["stages"]["graph"]["ratio"]


def test_compare_parity_fails_on_a_served_share_difference_and_exits_1(tmp_path, capsys):
    a = run_dict("bench-scenes", "N", "opencv", scene_results(1), INPUTS, NETS, run_id="A")
    b = run_dict("bench-scenes", "N", "cool", scene_results(2, served=0.7322), INPUTS, NETS, run_id="B")
    result = bench.compare(a, b, 50, 0)
    row = result["scenes"]["sydney-rail-2024-01-05"]
    assert not row["parity"]["pass"] and row["parity"]["differences"] == [
        "served_share: 0.7312 vs 0.7322 (abs 0.001000 > 0.0001)"
    ]
    assert row["pixel_mismatch"] == {"share": 0.0, "note": "identical PNG bytes"}
    difference = "served_share: 0.7312 vs 0.7322 (abs 0.001000 > 0.0001)"
    assert bench.compare_checks(result) == [{"name": "parity:sydney-rail-2024-01-05", "expected": "equal",
                                             "got": difference, "pass": False}]  # fmt: skip
    # Within the tolerances, and with identical lists, it passes without an equal digest.
    close = bench.compare(
        a, run_dict("bench-scenes", "N", "cool", scene_results(2, served=0.73125), INPUTS, NETS), 50, 0
    )
    assert close["parity_pass"] and not close["scenes"]["sydney-rail-2024-01-05"]["parity"]["equal_digest"]
    # A figure that was kept is decoded and its pixel mismatch share reported, not gated.
    (tmp_path / "A.sydney-rail-2024-01-05.png").write_bytes(tiny_png())
    other = tiny_png().replace(b"IDAT", b"IDAT")  # the same bytes: share 0 by decoding
    (tmp_path / "B.sydney-rail-2024-01-05.png").write_bytes(other)
    for run, name in ((a, "A"), (b, "B")):
        run["results"]["sydney-rail-2024-01-05"]["figure"] = f"{name}.sydney-rail-2024-01-05.png"
        run["results"]["sydney-rail-2024-01-05"]["image_png_sha256"] = name * 64
        vision_runs.write(run, tmp_path / f"{name}.json")
    assert (
        bench.main(
            [
                "compare",
                str(tmp_path / "A.json"),
                str(tmp_path / "B.json"),
                "--bootstrap",
                "50",
                "--out",
                str(tmp_path / "cmp"),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "parity: FAIL" in err
    written = vision_runs.load(next((tmp_path / "cmp").glob("*.json")))
    assert written["kind"] == "bench-compare" and written["summary"]["parity_pass"] == 0
    assert written["results"]["scenes"]["sydney-rail-2024-01-05"]["pixel_mismatch"] == {
        "share": 0.0,
        "note": None,
    }
    assert written["backend"] == "opencv-vs-cool" and written["inputs"]["runs"] == ["A", "B"]
    assert bench.pixel_mismatch(None, tmp_path / "nope.png") == {"share": None, "note": "figure not kept"}


def test_compare_refuses_other_inputs_other_machines_and_other_kinds(tmp_path, capsys):
    a = run_dict("bench-scenes", "N", "opencv", scene_results(1), INPUTS, NETS, run_id="A")
    other_etag = [{**INPUTS[0], "etag_by_asset": {"red": "e2"}}]
    with pytest.raises(
        bench.CompareRefused, match="not the same inputs: sydney-rail-2024-01-05's asset ETags differ"
    ):
        bench.compare(a, run_dict("bench-scenes", "N", "cool", scene_results(2), other_etag, NETS), 10, 0)
    other_net = [{**NETS[0], "sha256": "m" * 64}]
    with pytest.raises(bench.CompareRefused, match="not the same inputs: network"):
        bench.compare(a, run_dict("bench-scenes", "N", "cool", scene_results(2), INPUTS, other_net), 10, 0)
    with pytest.raises(bench.CompareRefused, match="not the same machine: 'N' and 'Intel"):
        bench.compare(
            a, run_dict("bench-scenes", "Intel Xeon", "opencv", scene_results(2), INPUTS, NETS), 10, 0
        )
    with pytest.raises(bench.CompareRefused, match="not the same kind"):
        bench.compare(a, run_dict("bench-kernels", "N", "cool", {}, size=64), 10, 0)
    with pytest.raises(bench.CompareRefused, match="compare takes two"):
        bench.compare(
            run_dict("bench-controls", "N", "cool", {}), run_dict("bench-controls", "N", "cool", {}), 10, 0
        )
    with pytest.raises(bench.CompareRefused, match="share no scene"):
        bench.compare(
            a, run_dict("bench-scenes", "N", "cool", {}, [{**INPUTS[0], "name": "other"}], NETS), 10, 0
        )
    vision_runs.write(a, tmp_path / "A.json")
    vision_runs.write(
        run_dict("bench-scenes", "Intel Xeon", "opencv", scene_results(2), INPUTS, NETS), tmp_path / "B.json"
    )
    assert bench.main(["compare", str(tmp_path / "A.json"), str(tmp_path / "B.json")]) == 1
    assert "not the same machine" in capsys.readouterr().err
    (tmp_path / "bad.json").write_text("{}", encoding="utf-8")
    assert bench.main(["compare", str(tmp_path / "A.json"), str(tmp_path / "bad.json")]) == 1
    assert "not a bloggerbear.vision-eval/1 run file" in capsys.readouterr().err


def test_compare_with_unequal_stages_compares_the_common_ones_and_says_so():
    a = run_dict("bench-scenes", "N", "opencv", scene_results(1), INPUTS, NETS, run_id="A")
    b = run_dict(
        "bench-scenes",
        "N",
        "cool",
        scene_results(2, stages=("read", "analyse", "annotate", "total")),
        INPUTS,
        NETS,
    )
    result = bench.compare(a, b, 20, 0)
    row = result["scenes"]["sydney-rail-2024-01-05"]
    assert set(row["stages"]) == {"read", "analyse", "annotate", "total"} and row["stages_only_in_a"] == [
        "graph"
    ]
    assert result["notes"] == [
        "sydney-rail-2024-01-05: stages only in one run are not compared: A ['graph'], B []"
    ]
    assert "A ['graph']" in bench.markdown(run_dict("bench-compare", "N", "x", result))
    # Kernel runs: per case, with the rail analysis's stages, the headline on analyse_rail_access.
    graph = {**bench.summarise([4.0, 4.2, 4.1]), "samples_ms": [4.0, 4.2, 4.1]}
    cases = {"find_contours": {**bench.summarise([1.0, 1.2, 1.1]), "samples_ms": [1.0, 1.2, 1.1]},
             "analyse_rail_access": {**bench.summarise([10.0, 11.0, 10.5]), "samples_ms": [10.0, 11.0, 10.5],
                                     "stages": {"graph": graph}}}  # fmt: skip
    faster = {name: {**row, "samples_ms": [v / 2 for v in row["samples_ms"]]} for name, row in cases.items()}
    faster["analyse_rail_access"]["stages"] = {
        "graph": {"samples_ms": [2.0, 2.1, 2.05]},
        "masks": {"samples_ms": [1.0]},
    }
    k = bench.compare(
        run_dict("bench-kernels", "N", "opencv", cases, size=64),
        run_dict("bench-kernels", "N", "cool", faster, size=64),
        20,
        0,
    )
    assert k["headline"]["ratio"] == 2.0 and k["cases"]["find_contours"]["ratio_ci"]["ratio"] == 2.0
    assert set(k["cases"]["analyse_rail_access"]["stages"]) == {"graph"} and k["parity_pass"]
    with pytest.raises(bench.CompareRefused, match="not the same inputs: sizes 64 and 128"):
        bench.compare(
            run_dict("bench-kernels", "N", "opencv", cases, size=64),
            run_dict("bench-kernels", "N", "cool", cases, size=128),
            20,
            0,
        )


def test_controls_show_the_cool_guard_refusing_what_it_must(monkeypatch, capsys):
    from vision import build

    monkeypatch.setattr(
        build, "build_info", lambda: {"opencv_version": "5.0.0", "build_sha256": "ab" * 32, "kleidicv": True}
    )
    scene = ship_scenes()[0]
    for key in ("VISION_BACKEND", "COOL_BUILD_SHA256"):
        monkeypatch.delenv(key, raising=False)

    def never_read(url, start, end):
        raise AssertionError("a refused request must read nothing")

    # The real handler, with a pin that is not this build: not_cool, not_cool, backend_mismatch, nothing read.
    result = bench.controls(scene, "cd" * 32, fetch=never_read)
    assert result["build_matches_pin"] is False and result["scene"] == scene["name"]
    assert [(r["name"], r["expected"], r["got"], r["pass"]) for r in result["controls"]] == [
        ("cool_with_pin", "not_cool", "not_cool", True),
        ("cool_without_pin", "not_cool", "not_cool", True),
        ("opencv_request_to_cool_backend", "backend_mismatch", "backend_mismatch", True),
    ]
    assert "VISION_BACKEND" not in os.environ and "COOL_BUILD_SHA256" not in os.environ  # restored
    # With the pinned build, the first control expects ok; a handler that answers as the guard would.
    seen = []

    def handler(request, context, fetch=None):
        seen.append(
            (
                request["backend"],
                os.environ.get("VISION_BACKEND"),
                os.environ.get("COOL_BUILD_SHA256"),
                request["image"],
            )
        )
        if request["backend"] != "cool":
            return {"ok": False, "error": "backend_mismatch"}
        if os.environ.get("COOL_BUILD_SHA256") != "ab" * 32:
            return {"ok": False, "error": "not_cool"}
        return {**REPLY, "task": "ships"}

    result = bench.controls(scene, "ab" * 32, handler=handler, fetch=never_read)
    assert result["build_matches_pin"] is True
    assert [(r["expected"], r["got"], r["pass"]) for r in result["controls"]] == [
        ("ok", "ok", True), ("not_cool", "not_cool", True),
        ("backend_mismatch", "backend_mismatch", True)]  # fmt: skip
    assert seen == [
        ("cool", "cool", "ab" * 32, False),
        ("cool", "cool", None, False),
        ("opencv", "cool", "ab" * 32, False),
    ]
    assert (
        result["controls"][0]["timings_ms"] == REPLY["timings_ms"]
        and result["controls"][1]["timings_ms"] is None
    )
    opencv = {"opencv_version": "5.0.0", "build_sha256": "f" * 64}
    env = {"cpu": "x", "machine": "x86_64", "python": "3.11", "opencv": opencv, "libraries": None}
    text = bench.markdown({"mode": "controls", "environment": env, "results": result})
    assert "| cool_without_pin | cool | False | not_cool | not_cool | yes |" in text
    assert "libraries: null (not Linux" in text


def test_controls_mode_writes_a_run_file_with_the_checks(tmp_path, monkeypatch):
    import vision_worker_handler

    monkeypatch.setattr(
        vision_worker_handler,
        "lambda_handler",
        lambda request, context, fetch=None: {"ok": False, "error": "not_cool"},
    )
    out = tmp_path / "controls.json"
    no_pins = str(tmp_path / "no-manifest.json")  # with no pin and no cache, nothing is HEADed
    assert (
        bench.main(
            [
                "controls",
                "--cool-sha",
                "cd" * 32,
                "--backend",
                "cool",
                "--out",
                str(out),
                "--manifest",
                no_pins,
            ]
        )
        == 0
    )
    run = vision_runs.load(out)
    assert run["kind"] == "bench-controls" and run["mode"] == "controls" and run["backend"] == "cool"
    assert [c["name"] for c in run["checks"]] == [
        "cool_with_pin",
        "cool_without_pin",
        "opencv_request_to_cool_backend",
    ]
    assert run["summary"] == {
        "as_expected": 2,
        "controls": 3,
    }  # the third got not_cool from the stub, not backend_mismatch
    assert run["results"]["build_matches_pin"] is False and "build_lines" in run["environment"]
    assert run["environment"]["libraries"] is None or isinstance(run["environment"]["libraries"], list)
    assert "image_png_b64" not in json.dumps(run)
    with pytest.raises(SystemExit) as exc:
        bench.main(["controls"])
    assert exc.value.code == 2


def test_scenes_with_the_cool_backend_needs_the_pin_and_sets_the_handlers_environment(
    tmp_path, monkeypatch, capsys
):
    with pytest.raises(SystemExit) as exc:
        bench.main(["scenes", "--backend", "cool", "--repeats", "1"])
    assert exc.value.code == 2
    assert "--cool-sha" in capsys.readouterr().err
    import vision_worker_handler

    seen = {}

    def handler(request, context, fetch=None):
        seen.update(
            {
                "backend": os.environ.get("VISION_BACKEND"),
                "sha": os.environ.get("COOL_BUILD_SHA256"),
                "request": request["backend"],
            }
        )
        return {"ok": False, "error": "not_cool"}

    monkeypatch.setattr(vision_worker_handler, "lambda_handler", handler)
    for key in ("VISION_BACKEND", "COOL_BUILD_SHA256"):
        monkeypatch.delenv(key, raising=False)
    no_pins = str(tmp_path / "no-manifest.json")
    argv = ["scenes", "--backend", "cool", "--cool-sha", "AB" * 32, "--repeats", "1", "--manifest", no_pins]
    assert bench.main([*argv, "--only", "botany-bay-2024-01-05"]) == 0
    assert seen == {"backend": "cool", "sha": "AB" * 32, "request": "cool"}
    assert "error: not_cool" in capsys.readouterr().out
    # The kernels mode refuses a cool label outright when this build is not the pinned one.
    assert (
        bench.main(
            ["kernels", "--size", "64", "--repeats", "1", "--backend", "cool", "--cool-sha", "AB" * 32]
        )
        == 1
    )
    assert "not_cool" in capsys.readouterr().err


def test_where_and_code_identity(monkeypatch, tmp_path):
    assert vision_runs.where(environ={"GITHUB_ACTIONS": "true"}) == "ci"
    assert vision_runs.where(environ={}, system="Windows") == "windows"
    assert vision_runs.where(environ={}, system="Darwin") == "macos"
    assert (
        vision_runs.where(environ={}, system="Linux", release="5.15.0-microsoft-standard-WSL2", dmi_vendor="")
        == "wsl"
    )
    assert vision_runs.where(environ={}, system="Linux", release="6.8.0", dmi_vendor="Amazon EC2\n") == "ec2"
    assert (
        vision_runs.where(
            host={"region": "us-west-2"}, environ={}, system="Linux", release="6.8.0", dmi_vendor=""
        )
        == "ec2"
    )
    assert vision_runs.where(environ={}, system="Linux", release="6.8.0", dmi_vendor="QEMU") == "linux"
    code = vision_runs.code_identity()
    assert re.fullmatch(r"[0-9a-f]{40}", code["git_sha"]) and isinstance(code["dirty"], bool)
    assert vision_runs.code_identity(tmp_path) == {"git_sha": None, "dirty": None}

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    assert vision_runs.code_identity(run=no_git) == {"git_sha": None, "dirty": None}


def test_the_scenes_file_gained_the_clear_sydney_scene_and_kept_the_old_two():
    scenes = {s["name"]: s for s in bench.load_scenes()}
    assert list(scenes)[:2] == ["botany-bay-2024-01-05", "sydney-rail-2024-01-05"]
    clear = scenes["sydney-rail-2024-08-26"]
    assert clear["scene_id"] == "S2A_56HLH_20240826_0_L2A" and clear["task"] == "rail_access"
    assert clear["polygon"] == scenes["sydney-rail-2024-01-05"]["polygon"] and clear["params"] == {
        "reach_m": 1000
    }
    assert clear["role"] == "clear" and clear["captured_at"].startswith("2024-08-26")
    prefix = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/56/H/LH/2024/8/S2A_56HLH_20240826_0_L2A/"
    files = {band: url.rsplit("/", 1)[1] for band, url in clear["assets"].items()}
    assert files == {"red": "B04.tif", "nir": "B08.tif", "swir16": "B11.tif", "scl": "SCL.tif"}
    assert all(url.startswith(prefix) for url in clear["assets"].values())
    assert Path(clear["network_file"]).name == "sydney.json"
    assert scenes["sydney-rail-2024-01-05"]["scene_id"] == "S2A_56HLH_20240105_0_L2A"  # never changed
    assert scenes["botany-bay-2024-01-05"]["scene_id"] == "S2A_56HLH_20240105_0_L2A"


def test_the_network_fixture_directory_carries_the_odbl_notice_and_no_fixture_yet():
    notice = (ROOT / "scripts" / "vision_benchmark_networks" / "NOTICE.md").read_text(encoding="utf-8")
    assert (
        "Contains information from OpenStreetMap, © OpenStreetMap contributors, available under the Open\n"
        "Database Licence 1.0 (https://opendatacommons.org/licenses/odbl/)"
    ) in notice
    assert "sha256" in notice and "query" in notice.lower() and "endpoint" in notice.lower()
    assert "Apache-2.0 licence does not cover" in notice


def test_a_network_file_written_with_a_byte_order_mark_still_loads(tmp_path):
    # Windows PowerShell 5.1 writes one with `-Encoding utf8`, and the owner's machine is Windows.
    (tmp_path / "sydney.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(STUB_NETWORK).encode("utf-8"))
    scene = {**rail_scene(), "network_file": str(tmp_path / "sydney.json")}
    assert bench.load_network(scene) == STUB_NETWORK
    assert bench.request_for(scene, "opencv")["network"] == STUB_NETWORK


def test_isolated_runs_record_the_childrens_cache_counts_and_mapped_libraries(tmp_path, monkeypatch):
    scene = rail_scene_with_network(tmp_path)
    library = {"path": "~/stock/lib/python3.12/site-packages/cv2/cv2.abi3.so", "bytes": 4,
               "sha256": "ab" * 32, "package": "opencv_python_headless-5.0.0.93.dist-info"}  # fmt: skip
    argvs = []

    def fake_child(argv):
        argvs.append(argv)
        name, out = argv[argv.index("--only") + 1], Path(argv[argv.index("--out") + 1])
        opencv = {"opencv_version": "5.0.0", "build_sha256": "f" * 64}
        env = {"cpu": "x", "machine": "aarch64", "python": "3.12", "opencv": opencv, "libraries": [library]}
        run = vision_runs.new_run("bench-scenes", "x", "opencv", env, mode="scenes",
                                  inputs={"cache": True, "cache_hits": 5, "cache_misses": 2})  # fmt: skip
        results = {name: bench.scene_row(rail_reply(), {"read": [1.0], "analyse": [2.0], "total": [3.0]})}
        bench.write_figures(results, out)
        vision_runs.write(vision_runs.finish(run, results), out)
        return 0, 100 * 1024

    # take_children: the counts summed, each library once, the rows left without the child's record.
    rows = bench.scenes(
        [scene, {**scene, "name": "again"}], 1, "opencv",
        isolate=lambda s: bench.isolate_scene(s, ["scenes"], run_child=fake_child),
    )  # fmt: skip
    assert all(bench.CHILD_KEY in row for row in rows.values())
    assert bench.take_children(rows) == {"cache_hits": 10, "cache_misses": 4, "libraries": [library]}
    assert not any(bench.CHILD_KEY in row for row in rows.values())
    nothing = {"cache_hits": 0, "cache_misses": 0, "libraries": None}
    assert bench.take_children({"x": {"ok": False}}) == nothing
    # Through main: the run file's inputs and libraries are the children's, and no child record is left.
    monkeypatch.setattr(bench, "_run_child", fake_child)
    monkeypatch.setattr(vision_runs, "default_head", lambda url: {"etag": "e", "bytes": 1})
    scenes_file = tmp_path / "scenes.json"
    scenes_file.write_text(
        json.dumps({"scenes": [{**scene, "network_file": "sydney.json"}]}), encoding="utf-8"
    )
    out = tmp_path / "isolated.json"
    argv = ["scenes", "--scenes-file", str(scenes_file), "--manifest", str(tmp_path / "none.json"),
            "--isolate", "--repeats", "1", "--cache", str(tmp_path / "cogs"), "--out", str(out)]  # fmt: skip
    argvs.clear()
    assert bench.main(argv) == 0
    run = vision_runs.load(out)
    assert run["inputs"]["cache"] is True
    assert (run["inputs"]["cache_hits"], run["inputs"]["cache_misses"]) == (5, 2)
    assert run["environment"]["libraries"] == [library]
    row = run["results"]["sydney-rail-2024-01-05"]
    assert row["peak_scope"] == "scene" and row["peak_rss_mb"] == 100.0 and bench.CHILD_KEY not in row
    assert "--isolate" not in argvs[0] and argvs[0][argvs[0].index("--cache") + 1] == str(tmp_path / "cogs")


def test_worker_mode_records_the_deployed_worker_by_region_and_code_never_its_arn(tmp_path, monkeypatch):
    arn = "arn:aws:lambda:us-west-2:123456789012:function:bloggerbear-dev-vision-worker"

    class Configured(FakeLambda):
        def get_function_configuration(self, FunctionName):
            return {"CodeSha256": "c0de=", "Role": "arn:aws:iam::123456789012:role/worker"}

    fake = Configured(REPLY)
    monkeypatch.setattr(bench, "lambda_client", lambda arn_: fake)
    out = tmp_path / "worker.json"
    argv = ["worker", "--arn", arn, "--repeats", "1", "--only", "botany-bay-2024-01-05", "--out", str(out)]
    assert bench.main(argv) == 0
    run = vision_runs.load(out)
    assert run["kind"] == "bench-worker" and run["mode"] == "worker"
    assert run["worker"] == {"kind": "deployed", "region": "us-west-2", "code_sha256": "c0de="}
    assert "arn" not in run["params"]
    assert run["params"]["scenes_file"] == "scripts/vision_benchmark_scenes.json"
    text = out.read_text(encoding="utf-8")
    assert "123456789012" not in text and "arn:" not in text
    assert fake.calls[0][1]["site"]["id"] == "bench-botany-bay"
    # A caller that may not read the function's configuration records null.
    assert bench.deployed_code_sha256(FakeLambda(REPLY), arn) is None


def test_shown_path_keeps_the_users_name_out_of_run_files(tmp_path):
    home = tmp_path / "home" / "someone"
    root = home / "BloggerBear"
    (root / "scripts").mkdir(parents=True)

    def show(path):
        return vision_runs.shown_path(path, root=root, home=home)

    assert show(root / "scripts" / "vision_benchmark_scenes.json") == "scripts/vision_benchmark_scenes.json"
    assert show(home / "stock" / "lib" / "cv2.abi3.so") == "~/stock/lib/cv2.abi3.so"
    system = "/usr/lib/aarch64-linux-gnu/libopencv_core.so.5.0"
    assert show(system) == system and vision_runs.shown_path(None) is None
    # The environment records cv2's path that way too.
    from vision import build

    env = vision_runs.environment("x", build.build_info())
    assert not env["opencv"]["cv2_path"].startswith(str(Path.home()))
