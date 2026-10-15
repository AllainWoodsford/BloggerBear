"""Tests for scripts/vision_benchmark.py and the COOL image recipe it is run in.

Nothing here touches the network or a deployed worker: the scene and worker modes are given a
fake handler and a fake Lambda client, and `fetch-network` a fake Overpass client. The Sydney
rail scene's network file is not committed (Overpass is unreachable from the build container),
so the tests give that scene a stub network where one is needed.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

import vision_benchmark as bench

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
