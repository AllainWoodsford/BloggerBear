"""E8, the built-up check (scripts/vision_eval_reference.py, `vision_eval.py builtup-check`), on
synthetic inputs: the synthetic city of vision_eval_driver.py read and analysed in this process as
the worker does, against a synthetic reference in EPSG:4326 (the city's built district by
construction, written at WorldCover's 10 m in lon/lat with the geographic GeoKey). Held: the run
file (its kind, which the index takes; its summary's five codes; its reference block), the geometry
(a reference in register agrees whole, one moved by about 500 m does not), the reference's pins and
host, its CRS, the tile rule, the lon/lat grid against vision/geo.py, and the arithmetic. Nothing
touches the network: requests, urllib and socket.connect refuse.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "lambdas") not in sys.path:
    sys.path.insert(0, str(ROOT / "lambdas"))

import vision_eval  # noqa: E402
import vision_eval_driver as driver  # noqa: E402
import vision_eval_reference as e8  # noqa: E402
import vision_runs  # noqa: E402


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


def only_run(directory: Path) -> dict:
    files = sorted(directory.glob("*.json"))
    assert len(files) == 1, files
    return vision_runs.load(files[0])


def run_synthetic(tmp_path: Path, reference=None) -> tuple[int, dict]:
    args = vision_eval_args(["builtup-check", "--scene", "synthetic-1", "--out", str(tmp_path / "runs")])
    code = e8.main(args, vision_eval.load_manifest(), reference=reference)
    return code, only_run(tmp_path / "runs") if (tmp_path / "runs").is_dir() else {}


def vision_eval_args(argv: list[str]):
    """vision_eval's parser over `argv`, as main builds it, without running the subcommand."""
    captured = {}

    def grab(args, manifest, **kwargs):
        captured["args"] = args
        return 0

    original = e8.main
    e8.main = grab
    try:
        vision_eval.main(argv)
    finally:
        e8.main = original
    return captured["args"]


def test_the_synthetic_check_agrees_whole_and_writes_a_run_file_the_index_takes(tmp_path, capsys):
    assert vision_eval.main(["builtup-check", "--scene", "synthetic-1", "--out", str(tmp_path / "runs")]) == 0
    run = only_run(tmp_path / "runs")
    assert run["kind"] == e8.KIND == "eval-builtup-check" and run["mode"] == "builtup-check"
    assert e8.KIND in vision_runs.KINDS and e8.KIND in vision_eval.KIND_WORDS
    assert run["summary"] == {
        "iou": 1.0,
        "precision": 1.0,
        "recall": 1.0,
        "served_share_ours": run["summary"]["served_share_ours"],
        "served_share_reference": run["summary"]["served_share_ours"],
    }
    assert 0 < run["summary"]["served_share_ours"] < 1
    assert all(check["pass"] for check in run["checks"])
    results = run["results"]
    assert results["reference"] == {
        "tile": "S36E150",
        "crs": "EPSG:4326",
        "window_px": results["reference"]["window_px"],
        "pixel_deg": pytest.approx(1 / 12000),
        "synthetic": True,
    }
    assert (
        results["ours_by_reference_class"] == {"built_up": 1.0} and results["compared_share_of_usable"] == 1.0
    )
    assert set(results["reference_classes_compared"]) == {"tree_cover", "built_up", "water"}
    reference = run["inputs"]["reference"]
    assert (
        reference["licence"] == "CC BY 4.0" and reference["synthetic"] is True and reference["etag"] is None
    )
    assert run["params"]["synthetic"] is True and run["worker"]["kind"] == "in-process"
    rows = vision_eval.index_rows(tmp_path / "runs")
    assert [row["kind"] for row in rows] == [e8.KIND] and rows[0]["summary"]["iou"] == 1.0
    markdown = vision_eval.results_markdown(rows, {})
    assert f"## {e8.KIND} (1 run)" in markdown and "ESA WorldCover" in markdown
    assert "| iou | 1.0 |" in capsys.readouterr().out


def test_a_reference_out_of_register_is_seen(tmp_path):
    # About 300 m east and 300 m north (inside the margin round the site): the district no longer lies
    # under itself.
    code, run = run_synthetic(tmp_path, reference=e8.synthetic_reference(shift_deg=(0.0032, 0.0027)))
    assert code == 0
    assert (
        run["summary"]["iou"] < 0.7 and run["summary"]["precision"] < 0.9 and run["summary"]["recall"] < 0.9
    )


def test_the_reference_must_be_geographic_and_the_site_in_one_tile(tmp_path, capsys):
    url, _blob = e8.synthetic_reference()
    projected = e8.synthetic_reference()[1].replace(
        b"\x00\x08\x00\x00\x01\x00\xe6\x10", b"\x00\x0c\x00\x00\x01\x00\xe6\x10"
    )
    assert (
        projected != _blob
    )  # the GeoKey moved from GeographicTypeGeoKey (2048) to ProjectedCSTypeGeoKey (3072)
    code, _run = run_synthetic(tmp_path, reference=(url, projected))
    assert code == 2 and "not EPSG:4326" in capsys.readouterr().err
    sydney = vision_eval.load_manifest()["sites"]["sydney"]["polygon"]
    assert e8.tile_for(sydney) == "S36E150"
    assert e8.tile_name(-71.06, 42.36) == "N42W072" and e8.tile_name(103.8, 1.35) == "N00E102"
    with pytest.raises(e8.ReferenceRefused, match="S36E147, S36E150"):
        e8.tile_for([[149.9, -34.0], [150.1, -34.0], [150.1, -33.9]])


def test_the_real_reference_is_the_pinned_object_at_the_bucket_and_nothing_else(tmp_path, capsys):
    url = e8.reference_url("S36E150")
    assert url == (
        "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
        "ESA_WorldCover_10m_2021_v200_S36E150_Map.tif"
    )
    assert e8.check_reference_url(url) == url
    for bad in (
        "https://esa-worldcover.s3.amazonaws.com.evil/v200/2021/map/x.tif",
        "http://" + url[8:],
        "https://sentinel-cogs.s3.us-west-2.amazonaws.com/v200/2021/map/x.tif",
    ):
        with pytest.raises(e8.ReferenceRefused):
            e8.check_reference_url(bad)
    e8.check_pin("S36E150", {"etag": "b80441256064dfdeacf0e397722cd017-3", "bytes": 20060792})
    with pytest.raises(vision_runs.InputChanged, match="input changed"):
        e8.check_pin("S36E150", {"etag": "0" * 32, "bytes": 20060792})
    with pytest.raises(vision_runs.InputChanged, match="no pin"):
        e8.check_pin("N42W072", {"etag": "x", "bytes": 1})

    # The real scene: a moved reference is refused after its HEAD, before any byte is read.
    heads = []
    pins = vision_runs.pinned_etags(vision_eval.load_manifest())

    def head(asked):
        heads.append(asked)
        if asked.startswith(e8.REFERENCE_PREFIX):
            return {"etag": "moved", "bytes": 1}
        return {"etag": pins.get(asked), "bytes": None}  # the scene's assets, as pinned

    def no_reads(*args):
        raise AssertionError("nothing is read once the reference is refused")

    network = tmp_path / "network.json"
    network.write_text(json.dumps(driver.synthetic_network()), encoding="utf-8")
    args = vision_eval_args(["builtup-check", "--network", str(network), "--out", str(tmp_path / "runs")])
    assert (
        e8.main(args, vision_eval.load_manifest(), head=head, fetch=no_reads, reference=(None, no_reads)) == 2
    )
    assert "input changed: reference tile S36E150" in capsys.readouterr().err
    assert heads[-1] == url and not (tmp_path / "runs").exists()


def test_the_lonlat_grid_is_geos_own_projection_between_its_nodes():
    from vision import geo

    transform = (300000.0, 20.0, 0.0, 6300000.0, 0.0, -20.0)
    lon, lat = e8.scene_lonlat((300, 517), transform, 32756, step=32)
    rng = random.Random(7)
    for _ in range(40):
        row, col = rng.randrange(300), rng.randrange(517)
        expected = geo.utm_to_lonlat(300000.0 + 20.0 * (col + 0.5), 6300000.0 - 20.0 * (row + 0.5), 56, True)
        # 1e-6 degrees is about 0.1 m: the interpolation between nodes 640 m apart is well inside it.
        assert lon[row, col] == pytest.approx(expected[0], abs=1e-6)
        assert lat[row, col] == pytest.approx(expected[1], abs=1e-6)
    with pytest.raises(ValueError):
        e8.scene_lonlat((1, 5), transform, 32756)
    with pytest.raises(ValueError):
        e8.scene_lonlat((5, 5), transform, 4326)  # geo converts UTM only


def test_the_comparison_counts_only_what_both_saw():
    import numpy as np

    built = np.zeros((4, 4), np.uint8)
    built[0, :] = 255  # ours: the top row
    reference = np.full((4, 4), 10, np.uint8)
    reference[0, :2] = 50
    reference[1, :2] = 50  # theirs: the top-left 2 x 2
    reference[3, :] = 0  # no data
    usable = np.full((4, 4), 255, np.uint8)
    usable[0, 3] = 0  # cloud
    results, summary = e8.compare(built, usable, reference, [(0.0, 0.0)], reach_px=1.5, pixel_size_m=20.0)
    # Compared: 12 pixels (rows 0-2, less the cloudy one). Ours 3 (row 0, cols 0-2), theirs 4, both 2.
    assert (
        results["compared_px"],
        results["true_positive_px"],
        results["false_positive_px"],
        results["false_negative_px"],
    ) == (11, 2, 1, 2)
    assert summary["iou"] == pytest.approx(2 / 5) and summary["precision"] == pytest.approx(2 / 3)
    assert summary["recall"] == pytest.approx(2 / 4)
    # Within 1.5 px of the station at (0, 0): (0,0), (0,1), (1,0), (1,1).
    assert summary["served_share_ours"] == pytest.approx(2 / 3) and summary["served_share_reference"] == 1.0
    assert results["ours_by_reference_class"] == {
        "built_up": pytest.approx(2 / 3),
        "tree_cover": pytest.approx(1 / 3),
    }
    empty, nothing = e8.compare(
        np.zeros((2, 2), np.uint8), usable[:2, :2], np.zeros((2, 2), np.uint8), [], 1.0, 20.0
    )
    assert (
        nothing
        == {
            "iou": None,
            "precision": None,
            "recall": None,
            "served_share_ours": None,
            "served_share_reference": None,
        }
        and empty["ours_by_reference_class"] == {}
    )


def test_importing_the_check_reaches_no_adapter_and_no_cv2():
    import subprocess

    code = (
        "import sys; sys.path[:0] = ['scripts', 'lambdas']; import vision_eval_reference; "
        "print(sorted(m for m in ('cv2', 'numpy', 'common.adapters.rail_access') if m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
