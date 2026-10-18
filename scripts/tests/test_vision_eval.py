"""Tests for scripts/vision_eval.py (`pin`, `coverage`, `failures`, `access-baseline`, `index`) and
the scene manifest; `replay` has its own file (test_vision_eval_replay.py).

Nothing here touches the network: `pin` is given fake HEAD and GET answers, `coverage` reads
in-memory COGs built with the worker's own test writer (lambdas/tests/vision_fakes.py), the
failure rows answer from a scripted handler, `access-baseline` measures the synthetic city, and
requests, urllib, boto3 and socket connections raise for every test. `index --check` and the
failure catalogue's collection check run here against the repository itself: CI fails when a run
file is added without re-indexing, or a test the catalogue names is renamed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

import vision_eval
import vision_runs

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "lambdas" / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "lambdas" / "tests"))
from vision_fakes import write_tiff  # noqa: E402 - the worker's in-memory COG writer

BUCKET = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs"
SCENE_ID = re.compile(r"^S2[ABC]_56HLH_\d{8}_\d_L2A$")
SYDNEY = [[150.86, -33.58], [151.56, -33.58], [151.56, -34.16], [150.86, -34.16]]


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
    import boto3

    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3, "resource", refuse)


# --- the manifest file ------------------------------------------------------------------------------


def test_the_manifest_parses_and_pins_only_bucket_assets():
    manifest = vision_eval.load_manifest()
    assert manifest["schema"] == "bloggerbear.vision-manifest/1" and "never change one" in manifest["about"]
    assert (
        manifest["sites"]["sydney"]["polygon"] == SYDNEY
        and manifest["sites"]["sydney"]["task"] == "rail_access"
    )
    scenes = manifest["scenes"]
    names = [scene["name"] for scene in scenes]
    assert len(names) == len(set(names)) and len(scenes) >= 13
    for scene in scenes:
        assert SCENE_ID.match(scene["scene_id"]), scene["scene_id"]
        assert scene["role"] in vision_eval.ROLES and scene["site"] in manifest["sites"]
        assert set(scene["assets"]) == {"red", "nir", "swir16", "scl"}
        for band, asset in scene["assets"].items():
            assert asset["url"].startswith(f"{BUCKET}/56/H/LH/") and asset["url"].endswith(
                f"/{vision_eval.BANDS[band]}.tif"
            )
            assert scene["scene_id"] in asset["url"]
        assert set(scene["item"]) >= {"cloud_cover", "nodata_pct"}
        if scene["role"] in ("cloud", "nodata"):
            assert scene["expected"] == {"quality_flags": ["low_coverage"]}
        else:
            assert scene["expected"] is None
    by_name = {scene["name"]: scene for scene in scenes}
    assert by_name["sydney-2024-08-26"]["scene_id"] == "S2A_56HLH_20240826_0_L2A"
    assert (
        by_name["sydney-2024-01-05"]["role"] == "cloud" and by_name["sydney-2024-08-21a"]["role"] == "nodata"
    )
    assert by_name["sydney-2019-12-26-smoke"]["role"] == "smoke"
    assert sum(1 for scene in scenes if scene["role"] == "baseline") == 4
    # The year set's listing is kept whole, so the selection counts can be re-made from it.
    listing = manifest["listings"]["56HLH-2024"]
    assert listing["site"] == "sydney" and listing["year"] == 2024 and listing["max_cloud_cover"] == 40
    assert len(listing["items"]) > 100 and all(SCENE_ID.match(item["scene_id"]) for item in listing["items"])
    year = [scene for scene in scenes if scene["role"] == "year"]
    assert year and all(scene["item"]["cloud_cover"] <= 40 for scene in year)
    duplicates = [scene for scene in scenes if scene.get("duplicate_of")]
    assert all(scene["duplicate_of"] in {s["scene_id"] for s in scenes} for scene in duplicates)
    # The year set (E3's input) is recorded by name, in time order, as year_set derives it; the
    # `year` role marks a candidate only: some candidates are outside the set, and named clear and
    # season scenes are inside it, so a run of the year set must read `sets`, not the roles.
    year_set = manifest["sets"]["year-2024"]
    members = vision_eval.year_set(manifest, "56HLH", 2024, 40.0)
    assert sorted(year_set["scenes"]) == sorted(members) and len(members) == len(set(members))
    times = [by_name[name]["captured_at"] for name in year_set["scenes"]]
    assert times == sorted(times)
    assert (year_set["site"], year_set["tile"], year_set["year"]) == ("sydney", "56HLH", 2024)
    assert (year_set["max_cloud_cover"], year_set["min_coverage"]) == (40.0, 0.95)
    assert manifest["sets"] == vision_eval.year_sets(manifest, 40.0)
    assert {by_name[name]["role"] for name in members} == {"clear", "season", "year"}
    assert any(scene["role"] == "year" and scene["name"] not in members for scene in scenes)
    assert all(by_name[name]["site_coverage"] >= 0.95 and not by_name[name].get("duplicate_of")
               for name in members)  # fmt: skip
    assert "sets as year-2024" in manifest["about"] and "never by the year role" in manifest["about"]


def test_the_agent_cases_and_the_year_set_name_only_scenes_the_manifest_pins():
    import vision_eval_agent

    manifest = vision_eval.load_manifest()
    pinned = {scene["name"]: scene for scene in manifest["scenes"]}
    cases = vision_eval_agent.load_cases(manifest)  # refuses a case naming a scene the manifest lacks
    named = vision_eval_agent.scene_names(cases)
    assert named and all(all(asset["etag"] for asset in pinned[name]["assets"].values()) for name in named)
    year = manifest["sets"][vision_eval_agent.YEAR_SET]["scenes"]
    assert len(year) >= 10 and all(pinned[name]["site_coverage"] >= 0.95 for name in year)


def test_the_bucket_urls_are_derived_from_the_scene_id_and_other_hosts_are_refused():
    assets = vision_eval.scene_assets("S2B_56HLH_20191226_0_L2A")
    assert assets["scl"]["url"] == f"{BUCKET}/56/H/LH/2019/12/S2B_56HLH_20191226_0_L2A/SCL.tif"
    assert assets["red"] == {
        "url": f"{BUCKET}/56/H/LH/2019/12/S2B_56HLH_20191226_0_L2A/B04.tif",
        "etag": None,
        "bytes": None,
    }
    assert vision_eval.stac_item_url("S2A_56HLH_20240105_0_L2A").endswith(
        "/2024/1/S2A_56HLH_20240105_0_L2A/S2A_56HLH_20240105_0_L2A.json"
    )
    assert vision_eval.aot_url("S2A_56HLH_20240105_0_L2A").endswith("/S2A_56HLH_20240105_0_L2A/AOT.tif")
    assert (
        vision_eval.tile_of("S2A_56HLH_20240105_0_L2A") == "56HLH"
        and vision_eval.date_of("S2A_56HLH_20240105_0_L2A") == "2024-01-05"
    )
    with pytest.raises(ValueError, match="not a Sentinel-2 L2A scene id"):
        vision_eval.scene_folder("LC08_L1TP_089083")
    with pytest.raises(vision_runs.WrongHost):
        vision_runs.check_host("https://sentinel-cogs.s3.us-west-2.amazonaws.com.evil.example/x.tif")
    with pytest.raises(vision_runs.WrongHost):
        vision_runs.check_host("http://sentinel-cogs.s3.us-west-2.amazonaws.com/x.tif")
    with pytest.raises(ValueError, match="role must be one of"):
        vision_eval.new_scene("x", "sydney", "S2A_56HLH_20240105_0_L2A", "hazy")


# --- pin ----------------------------------------------------------------------------------------------


def stac_item(
    scene_id, cloud=1.5, nodata=14.0, bbox=(150.8, -34.5, 152.0, -33.4), when="2024-08-26T00:06:06.130000Z"
):
    return {
        "id": scene_id,
        "bbox": list(bbox),
        "properties": {"datetime": when, "eo:cloud_cover": cloud, "s2:nodata_pixel_percentage": nodata,
                       "platform": "sentinel-2a", "s2:datatake_id": "GS2A_20240826T000231_045845_N05.10"},
    }  # fmt: skip


class FakeBucket:
    """HEAD and GET answers for the bucket, recording every URL asked."""

    def __init__(self, etag="e1", size=100, items=None, listings=None):
        self.etag, self.size, self.items, self.listings = etag, size, items or {}, listings or {}
        self.heads, self.gets = [], []

    def head(self, url):
        self.heads.append(url)
        return {"etag": self.etag, "bytes": self.size}

    def get(self, url):
        self.gets.append(url)
        if url.startswith("https://sentinel-cogs.s3.us-west-2.amazonaws.com/?list-type=2"):
            prefix = url.split("prefix=")[1].split("&")[0]
            return self.listings.get(prefix, "<ListBucketResult></ListBucketResult>").encode()
        scene_id = url.rsplit("/", 1)[1][:-5]
        return json.dumps(self.items.get(scene_id) or stac_item(scene_id)).encode()


def small_manifest(*scene_ids, listings=None):
    manifest = {
        "about": "test", "schema": vision_eval.MANIFEST_SCHEMA,
        "sites": {"sydney": {"task": "rail_access", "polygon": SYDNEY, "params": {"reach_m": 1000}}},
        "scenes": [vision_eval.new_scene(f"sydney-{vision_eval.date_of(s)}", "sydney", s, "clear")
                   for s in scene_ids],  # fmt: skip
        "listings": listings or {},
    }  # fmt: skip
    return manifest


def test_pin_fills_the_etags_sizes_and_item_fields_then_reports_what_changed():
    manifest = small_manifest("S2A_56HLH_20240826_0_L2A")
    bucket = FakeBucket()
    lines, changed = vision_eval.pin(manifest, head=bucket.head, get=bucket.get)
    scene = manifest["scenes"][0]
    red = f"{BUCKET}/56/H/LH/2024/8/S2A_56HLH_20240826_0_L2A/B04.tif"
    assert not changed and scene["assets"]["red"] == {"url": red, "etag": "e1", "bytes": 100}
    assert scene["item"] == {"cloud_cover": 1.5, "nodata_pct": 14.0, "bbox": [150.8, -34.5, 152.0, -33.4]}
    assert scene["captured_at"] == "2024-08-26T00:06:06.130000Z"
    assert len(bucket.heads) == 4 and bucket.gets == [vision_eval.stac_item_url("S2A_56HLH_20240826_0_L2A")]
    assert (
        "sydney-2024-08-26 red etag: set to e1" in lines
        and "sydney-2024-08-26 captured_at: None -> 2024-08-26T00:06:06.130000Z" in lines
    )
    assert len(lines) == 4 * 2 + 3 + 1
    # Pinned and unchanged: nothing to say. Changed: said, and flagged.
    assert vision_eval.pin(manifest, head=bucket.head, get=bucket.get) == ([], False)
    moved = FakeBucket(etag="e2", size=100)
    lines, changed = vision_eval.pin(manifest, head=moved.head, get=moved.get)
    assert changed and lines[0] == "sydney-2024-08-26 red etag: CHANGED from e1 to e2" and len(lines) == 4
    assert scene["assets"]["red"]["etag"] == "e2"


def test_pin_refuses_a_url_at_another_host_before_any_request():
    manifest = small_manifest("S2A_56HLH_20240826_0_L2A")
    manifest["scenes"][0]["assets"]["nir"]["url"] = "https://example.com/B08.tif"
    bucket = FakeBucket()
    with pytest.raises(vision_runs.WrongHost, match="example.com"):
        vision_eval.pin(manifest, head=bucket.head, get=bucket.get)
    assert bucket.heads == [] and bucket.gets == []


def test_pin_main_prints_differences_writes_with_the_flag_and_exits_1_on_a_changed_etag(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "manifest.json"
    vision_eval.save_manifest(small_manifest("S2A_56HLH_20240826_0_L2A"), path)
    bucket = FakeBucket()
    monkeypatch.setattr(vision_runs, "default_head", bucket.head)
    monkeypatch.setattr(vision_eval, "default_get", bucket.get)
    assert vision_eval.main(["pin", "--manifest", str(path)]) == 0
    out = capsys.readouterr().out
    assert (
        "set to e1" in out and vision_eval.load_manifest(path)["scenes"][0]["assets"]["red"]["etag"] is None
    )
    assert vision_eval.main(["pin", "--manifest", str(path), "--write"]) == 0
    assert vision_eval.load_manifest(path)["scenes"][0]["assets"]["red"]["etag"] == "e1"
    assert (
        vision_eval.main(["pin", "--manifest", str(path)]) == 0
        and "matches the bucket" in capsys.readouterr().out
    )
    monkeypatch.setattr(vision_runs, "default_head", FakeBucket(etag="e9").head)
    assert vision_eval.main(["pin", "--manifest", str(path)]) == 1
    assert "input changed" in capsys.readouterr().err


LISTING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult><Name>sentinel-cogs</Name><Prefix>{prefix}</Prefix><Delimiter>/</Delimiter>
<CommonPrefixes><Prefix>{prefix}{a}/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>{prefix}{b}/</Prefix></CommonPrefixes>
</ListBucketResult>"""

INSIDE = (150.8, -34.5, 152.0, -33.4)  # holds the Sydney box
SHORT = (150.8, -33.78, 152.0, -33.4)  # stops short of the site's south edge (-34.16)


def test_list_year_reads_the_bucket_month_by_month_and_the_year_rule_keeps_one_per_date():
    prefix = "sentinel-s2-l2a-cogs/56/H/LH/2024/12/"
    ids = {"a": "S2A_56HLH_20241214_0_L2A", "b": "S2C_56HLH_20241214_1_L2A"}
    listings = {prefix: LISTING_XML.format(prefix=prefix, **ids)}
    jan = "sentinel-s2-l2a-cogs/56/H/LH/2024/1/"
    listings[jan] = LISTING_XML.format(prefix=jan, a="S2A_56HLH_20240105_0_L2A", b="S2B_56HLH_20240120_0_L2A")
    items = {
        "S2A_56HLH_20241214_0_L2A": stac_item(
            "S2A_56HLH_20241214_0_L2A", cloud=15.7, nodata=15.8, bbox=INSIDE, when="2024-12-14T00:06:00Z"
        ),
        "S2C_56HLH_20241214_1_L2A": stac_item(
            "S2C_56HLH_20241214_1_L2A", cloud=15.2, nodata=16.3, bbox=INSIDE, when="2024-12-14T00:08:00Z"
        ),
        "S2A_56HLH_20240105_0_L2A": stac_item(
            "S2A_56HLH_20240105_0_L2A", cloud=66.5, nodata=18.0, bbox=INSIDE, when="2024-01-05T00:06:00Z"
        ),
        "S2B_56HLH_20240120_0_L2A": stac_item(
            "S2B_56HLH_20240120_0_L2A", cloud=2.6, nodata=18.0, bbox=SHORT, when="2024-01-20T00:06:00Z"
        ),
    }
    bucket = FakeBucket(items=items, listings=listings)
    listed = vision_eval.list_year("56HLH", 2024, get=bucket.get)
    assert [item["scene_id"] for item in listed] == [
        "S2A_56HLH_20240105_0_L2A", "S2B_56HLH_20240120_0_L2A",
        "S2A_56HLH_20241214_0_L2A", "S2C_56HLH_20241214_1_L2A",
    ]  # fmt: skip
    assert sum(1 for url in bucket.gets if "list-type=2" in url) == 12 and len(bucket.gets) == 16
    assert listed[0]["cloud_cover"] == 66.5 and listed[0]["bbox"] == list(INSIDE)
    # The cloud filter drops 5 January, the footprint rule 20 January, the date rule the 2C item.
    candidates = vision_eval.year_candidates(listed, SYDNEY, 40)
    a14, c14 = "S2A_56HLH_20241214_0_L2A", "S2C_56HLH_20241214_1_L2A"
    assert [(item["scene_id"], dup) for item, dup in candidates] == [(a14, None), (c14, a14)]
    # Least no-data wins among 2A/2B items of one date; the lowest sequence on a tie.
    b0, b1 = "S2B_56HLH_20240821_0_L2A", "S2B_56HLH_20240821_1_L2A"
    twins = [vision_eval.item_fields(stac_item(s, cloud=c, nodata=n, bbox=INSIDE)) | {"scene_id": s}
             for s, c, n in ((b0, 19.1, 77.8), (b1, 0.8, 21.7))]  # fmt: skip
    kept = vision_eval.year_candidates(twins, SYDNEY, 40)
    assert [(i["scene_id"], d) for i, d in kept] == [(b1, None), (b0, b1)]
    manifest = small_manifest("S2A_56HLH_20240826_0_L2A")
    added = vision_eval.add_year_set(manifest, "sydney", "56HLH", 2024, listed, 40)
    assert added == ["sydney-2024-12-14", "sydney-2024-12-14b"]
    by_name = {scene["name"]: scene for scene in manifest["scenes"]}
    assert (
        by_name["sydney-2024-12-14"]["role"] == "year" and "duplicate_of" not in by_name["sydney-2024-12-14"]
    )
    assert by_name["sydney-2024-12-14b"]["duplicate_of"] == "S2A_56HLH_20241214_0_L2A"
    assert by_name["sydney-2024-12-14"]["item"] == {
        "cloud_cover": 15.7,
        "nodata_pct": 15.8,
        "bbox": list(INSIDE),
    }
    assert by_name["sydney-2024-12-14"]["captured_at"] == "2024-12-14T00:06:00Z"
    listing = manifest["listings"]["56HLH-2024"]
    assert listing["items"][0]["scene_id"] == "S2A_56HLH_20240105_0_L2A" and listing["max_cloud_cover"] == 40
    # Running it again adds nothing; the year set needs a measured coverage.
    assert vision_eval.add_year_set(manifest, "sydney", "56HLH", 2024, listed, 40) == []
    assert vision_eval.year_set(manifest, "56HLH", 2024, 40) == []
    by_name["sydney-2024-12-14"]["site_coverage"] = 0.99
    by_name["sydney-2024-12-14b"]["site_coverage"] = 0.99
    assert vision_eval.year_set(manifest, "56HLH", 2024, 40) == ["sydney-2024-12-14"]
    by_name["sydney-2024-12-14"]["site_coverage"] = 0.9
    assert vision_eval.year_set(manifest, "56HLH", 2024, 40) == []
    by_name["sydney-2024-12-14"]["site_coverage"] = 0.99
    assert vision_eval.year_sets(manifest, 40) == {"year-2024": {
        "site": "sydney", "tile": "56HLH", "year": 2024, "max_cloud_cover": 40, "min_coverage": 0.95,
        "scenes": ["sydney-2024-12-14"],
    }}  # fmt: skip
    # Two sites listed for one year: each set is named with its site.
    manifest["listings"]["55HCU-2024"] = {**listing, "site": "melbourne", "tile": "55HCU"}
    assert sorted(vision_eval.year_sets(manifest, 40)) == ["melbourne-year-2024", "sydney-year-2024"]
    assert (
        vision_eval.bbox_covers(list(SHORT), SYDNEY) is False
        and vision_eval.bbox_covers(None, SYDNEY) is False
    )


# --- coverage ----------------------------------------------------------------------------------------


def synthetic_scl_and_aot(
    size: int = 64, cloud_rows: slice | None = slice(10, 30), nodata_cols: slice | None = None
):
    """A 20 m SCL image (vegetation, a band of high-probability cloud, an optional no-data strip)
    and an AOT image of 0.44 everywhere, each as a COG with one overview; and the lon/lat polygon
    of the pixel box (10, 10)-(50, 50) inside them."""
    import numpy as np
    from vision import geo

    scl = np.full((size, size), 4, np.uint8)
    if cloud_rows is not None:
        scl[cloud_rows, :] = 9
    if nodata_cols is not None:
        scl[:, nodata_cols] = 0
    aot = np.full((size, size), 440, np.uint16)
    transform = (300000.0, 20.0, 0.0, 6300040.0, 0.0, -20.0)
    polygon = [
        list(geo.pixel_to_lonlat(transform, 32756, x, y)) for x, y in ((10, 10), (50, 10), (50, 50), (10, 50))
    ]
    blobs = {
        "scl": write_tiff(scl, tile=16, pixel=20.0, nodata="0", overviews=(2,)),
        "aot": write_tiff(aot, tile=16, pixel=20.0, nodata=None, overviews=(2,)),
    }
    return blobs, polygon


def fetch_from(blobs: dict, scene_ids: list[str], log: list | None = None):
    urls = {}
    for scene_id in scene_ids:
        urls[vision_eval.scene_assets(scene_id)["scl"]["url"]] = blobs["scl"]
        urls[vision_eval.aot_url(scene_id)] = blobs["aot"]

    def fetch(url, start, end):
        if log is not None:
            log.append((url, start, end))
        return urls[url][start : end + 1]

    return fetch


def test_decompose_gives_the_class_shares_the_coverage_and_the_cause():
    import numpy as np

    scl = np.array([[4, 4, 9, 9], [9, 9, 0, 6], [5, 5, 5, 5], [3, 8, 10, 1]], np.uint8)
    row = vision_eval.decompose(scl, floor=0.6)
    assert (
        row["pixels"] == 16 and row["shares"]["cloud_high"] == 0.25 and row["shares"]["vegetation"] == 0.125
    )
    assert row["shares"]["snow"] == 0.0 and set(row["shares"]) == set(vision_eval.SCL_NAMES.values())
    # Unusable: 0, 1, 3, 8, 9, 10 (vision/masks.SCL_UNUSABLE): 9 of 16 pixels, as the worker counts it.
    assert row["coverage"] == round(7 / 16, 4) and row["below_floor"] and row["floor"] == 0.6
    assert row["largest_loss"] == {"class": "cloud_high", "share_of_loss": round(4 / 9, 4)}
    assert row["cause"] is None and row["explained"] is False  # no class holds half the loss
    mostly_cloud = np.where(np.arange(100) < 70, 9, 4).astype(np.uint8)
    row = vision_eval.decompose(mostly_cloud, floor=0.6)
    assert row["coverage"] == 0.3 and row["cause"] == "cloud_high" and row["explained"]
    clear = vision_eval.decompose(np.full((10, 10), 5, np.uint8), floor=0.6)
    assert clear["coverage"] == 1.0 and not clear["below_floor"] and clear["largest_loss"] is None
    assert vision_eval.decompose(np.zeros((0,), np.uint8), floor=0.6)["coverage"] == 0.0


def test_scene_coverage_reads_the_site_with_the_repositorys_cog_reader_and_the_aot_mean():
    blobs, polygon = synthetic_scl_and_aot()
    scene = vision_eval.new_scene("s", "sydney", "S2A_56HLH_20240826_0_L2A", "clear")
    log = []
    row = vision_eval.scene_coverage(scene, polygon, fetch_from(blobs, [scene["scene_id"]], log), floor=0.6)
    # The box's far corner lands on pixel 50 to within rounding, so the window and the filled
    # site are 40 or 41 pixels a side; the cloud band is rows 10-29, 20 rows of the site.
    assert row["ok"] and row["window"][:2] == [10, 10] and row["window"][2] in (50, 51)
    side = round(row["pixels"] ** 0.5)
    assert side in (40, 41) and row["pixels"] == side * side
    cloud = round(20 * side / row["pixels"], 4)
    assert row["shares"]["cloud_high"] == cloud and row["shares"]["vegetation"] == round(1 - cloud, 4)
    assert row["coverage"] == round(1 - cloud, 4) and row["below_floor"] and row["cause"] == "cloud_high"
    assert row["explained"] and row["largest_loss"] == {"class": "cloud_high", "share_of_loss": 1.0}
    assert row["aot_mean"] == 0.44 and row["aot_scope"] in ("clear", "all") and row["aot_pixel_m"] == 40.0
    assert row["requests"] == len(log) == 3 and 0 < row["bytes"] <= sum(
        end - start + 1 for _, start, end in log
    )
    assert all(url.endswith(("SCL.tif", "AOT.tif")) for url, _, _ in log)
    clear_blobs, _ = synthetic_scl_and_aot(cloud_rows=None)
    row = vision_eval.scene_coverage(scene, polygon, fetch_from(clear_blobs, [scene["scene_id"]]), floor=0.6)
    assert row["coverage"] == 1.0 and not row["below_floor"] and row["aot_scope"] == "clear"
    outside = [[140.0, -20.0], [140.1, -20.0], [140.1, -20.1], [140.0, -20.1]]
    row = vision_eval.scene_coverage(scene, outside, fetch_from(blobs, [scene["scene_id"]]), floor=0.6)
    assert not row["ok"] and "does not overlap" in row["error"]


def test_coverage_counts_e2_from_the_listing_and_main_writes_the_run_file(tmp_path, monkeypatch, capsys):
    blobs, polygon = synthetic_scl_and_aot()
    clear_blobs, _ = synthetic_scl_and_aot(cloud_rows=None)
    cloudy, clear, skipped = (
        "S2A_56HLH_20240105_0_L2A",
        "S2A_56HLH_20240826_0_L2A",
        "S2B_56HLH_20240120_0_L2A",
    )
    manifest = small_manifest(cloudy, clear)
    manifest["sites"]["sydney"]["polygon"] = polygon
    manifest["scenes"][0].update({"role": "cloud", "expected": {"quality_flags": ["low_coverage"]}})
    manifest["scenes"][1]["role"] = "year"
    for scene in manifest["scenes"]:
        scene["captured_at"] = f"{vision_eval.date_of(scene['scene_id'])}T00:00:00Z"
        scene["item"] = {"cloud_cover": 10.0, "nodata_pct": 15.0, "bbox": list(INSIDE)}
        for asset in scene["assets"].values():
            asset["etag"], asset["bytes"] = "pinned", 1
    from common.vision_sites import site_bbox

    west, south, east, north = site_bbox(polygon)
    holds, short = [west - 1, south - 1, east + 1, north + 1], [west - 1, south + 0.001, east + 1, north + 1]
    for scene in manifest["scenes"]:
        scene["item"]["bbox"] = holds
    manifest["listings"]["56HLH-2024"] = {
        "site": "sydney", "tile": "56HLH", "year": 2024, "listed_at": "2026-10-16T00:00:00Z",
        "max_cloud_cover": 40,
        # Picked and under the floor; picked and clear; the footprint rule; the cloud filter; unmeasured.
        "items": [
            {"scene_id": cloudy, "cloud_cover": 10.0, "nodata_pct": 15.0, "bbox": holds},
            {"scene_id": clear, "cloud_cover": 10.0, "nodata_pct": 15.0, "bbox": holds},
            {"scene_id": skipped, "cloud_cover": 2.6, "nodata_pct": 18.0, "bbox": short},
            {"scene_id": "S2A_56HLH_20240115_0_L2A", "cloud_cover": 99.6, "nodata_pct": 19.3, "bbox": holds},
            {"scene_id": "S2A_56HLH_20240129_0_L2A", "cloud_cover": 2.7, "nodata_pct": 15.1, "bbox": holds},
        ],
    }  # fmt: skip
    blobs_by_scene = {cloudy: blobs, clear: clear_blobs}

    def fetch(url, start, end):
        for scene_id, b in blobs_by_scene.items():
            if scene_id in url:
                return fetch_from(b, [scene_id])(url, start, end)
        raise AssertionError(url)

    floor, max_cloud = vision_eval.rail_rule()
    assert (floor, max_cloud) == (0.6, 40.0)
    results, summary = vision_eval.coverage(manifest, manifest["scenes"], fetch)
    cloudy_coverage = results["sydney-2024-01-05"]["coverage"]
    assert 0.48 < cloudy_coverage < 0.52 and results["sydney-2024-08-26"]["coverage"] == 1.0
    assert summary["scenes"] == 2 and summary["explained"] == 1
    counts = ("items", "passed_filter", "passed_bbox", "below_floor_after_pick", "unmeasured", "year_set")
    # The clear scene, measured in this run, is already in the year set.
    assert [summary[k] for k in counts] == [5, 4, 3, 1, 1, 1]
    assert summary["sydney-2024-08-26.coverage"] == 1.0 and summary["sydney-2024-08-26.aot_mean"] == 0.44
    assert vision_eval.write_coverage(manifest, results) == 2
    assert manifest["scenes"][1]["site_coverage"] == 1.0 and manifest["scenes"][1]["aot_mean"] == 0.44
    assert vision_eval.year_set(manifest, "56HLH", 2024, 40) == [
        "sydney-2024-08-26"
    ]  # the cloudy one is under 0.95
    assert vision_eval.coverage_checks(manifest["scenes"], results) == [
        {"name": "sydney-2024-01-05:low_coverage", "expected": True, "got": True, "pass": True}]  # fmt: skip
    # Through main: HEADs first (the ETag rule), then the reads, the run file, and the manifest with --write.
    path = tmp_path / "manifest.json"
    for scene in manifest["scenes"]:
        scene["site_coverage"] = scene["aot_mean"] = None
    vision_eval.save_manifest(manifest, path)
    heads = []
    monkeypatch.setattr(
        vision_runs, "default_head", lambda url: heads.append(url) or {"etag": "pinned", "bytes": 1}
    )
    import vision_worker_handler

    monkeypatch.setattr(vision_worker_handler, "http_fetch", fetch)
    runs = tmp_path / "runs"
    manifest_as_read = vision_runs.sha256_file(path)
    code = vision_eval.main(
        [
            "coverage",
            "--manifest",
            str(path),
            "--out",
            str(runs),
            "--write",
            "--cache",
            str(tmp_path / "cogs"),
        ]
    )
    assert code == 0 and len(heads) == 4  # SCL and AOT of each scene
    run = vision_runs.load(next(runs.glob("*.json")))
    assert run["kind"] == "eval-coverage" and run["backend"] == "none" and run["mode"] == "coverage"
    assert run["started_at"] <= run["finished_at"]
    assert run["run_id"].startswith(run["started_at"][:10] + "-eval-coverage-") and run["run_id"].endswith(
        "-none"
    )
    assert run["inputs"]["manifest_sha256"] == manifest_as_read and run["inputs"]["cache"] is True
    assert manifest_as_read != vision_runs.sha256_file(path)  # --write changed it after the run read it
    assert run["inputs"]["scenes"][0]["etag_by_asset"] == {"scl": "pinned", "aot": "pinned"}
    assert run["inputs"]["cache_index_sha256"] and (tmp_path / "cogs" / "index.json").is_file()
    assert run["params"]["floor"] == 0.6 and run["params"]["year_set_min_coverage"] == 0.95
    assert (
        run["summary"]["below_floor_after_pick"] == 1 and run["summary"]["year_set"] == 1
    )  # written, so counted
    assert run["checks"] == [
        {"name": "sydney-2024-01-05:low_coverage", "expected": True, "got": True, "pass": True}
    ]
    assert run["environment"]["opencv"]["opencv_version"].startswith("5.")
    written = vision_eval.load_manifest(path)
    assert (
        written["scenes"][0]["site_coverage"] == cloudy_coverage and written["scenes"][1]["aot_mean"] == 0.44
    )
    # --write records the year set by name: the clear scene, not the cloudy one under 0.95.
    assert written["sets"]["year-2024"]["scenes"] == ["sydney-2024-08-26"]
    out = capsys.readouterr().out
    assert (
        f"| sydney-2024-01-05 | cloud | {cloudy_coverage} (below floor) | cloud_high 1.0 | cloud_high |"
        in out
    )
    assert "E2: 5 items listed, 4 pass the cloud filter, 3 of them the footprint rule, 1 of those" in out
    # A pinned ETag that changed refuses the run before any read.
    monkeypatch.setattr(vision_runs, "default_head", lambda url: {"etag": "moved", "bytes": 1})
    monkeypatch.setattr(vision_worker_handler, "http_fetch", lambda *a: pytest.fail("must not read"))
    assert vision_eval.main(["coverage", "--manifest", str(path)]) == 1
    assert "input changed" in capsys.readouterr().err
    assert vision_eval.main(["coverage", "--manifest", str(path), "--only", "nope"]) == 2


# --- failures (2.5) ---------------------------------------------------------------------------------


def test_every_test_the_failure_catalogue_names_is_collected_by_pytest():
    # The CI check: node ids, never line numbers, and every one of them collected as it is named.
    cases = vision_eval.FAILURE_CASES
    assert [case["case"] for case in cases] == [
        "cloud", "swath_nodata", "smoke", "site_outside_scene", "window_too_large", "graph_too_large",
        "betweenness_sampled", "unreadable_scene", "url_off_allowlist", "wrong_build", "overpass_fails",
        "out_of_time", "agent_fails", "injected_news",
    ]  # fmt: skip
    nodes = [node for case in cases for node in vision_eval.node_ids(case)]
    pattern = r"lambdas/tests/test_\w+\.py::test_\w+"
    assert len(nodes) == 42 and all(re.fullmatch(pattern, node) for node in nodes)
    files = sorted({node.split("::")[0] for node in nodes})
    collected = vision_eval.collected_tests(files)
    assert collected and vision_eval.missing_tests(cases, collected) == []
    assert vision_eval.missing_tests(cases, collected - {nodes[0]}) == [nodes[0]]


def scripted_handler(reads: list):
    """The worker handler for the failure rows, scripted by scene and polygon: what the real handler
    answers on each real input, with its header reads for the refusals that follow them."""

    def handler(request, context, fetch=None):
        import os

        scene = request["scene"]["id"]
        lons = [lon for lon, _ in request["site"]["polygon"]]
        assets = request["scene"]["assets"]
        if any(not url.startswith(vision_runs.BUCKET_PREFIX) for url in assets.values()):
            return {"version": 1, "ok": False, "error": "bad_request", "detail": "not at an allowed location"}
        if request["backend"] != os.environ.get("VISION_BACKEND", "opencv"):
            return {"version": 1, "ok": False, "error": "backend_mismatch", "detail": "x"}
        if request["backend"] == "cool":
            return {"version": 1, "ok": False, "error": "not_cool", "detail": "x"}
        refused = None
        if min(lons) > 152.0:
            refused = "site_outside_scene"
        elif max(lons) - min(lons) > 0.8:
            refused = "window_too_large"
        elif assets["swir16"].endswith("-missing.tif"):
            return {"version": 1, "ok": False, "error": "unreadable_scene", "detail": "HTTP 403"}
        if refused:
            for url in assets.values():
                fetch(url, 0, 65535)
                reads.append(url)
            return {"version": 1, "ok": False, "error": refused, "detail": "x"}
        coverage = {
            "S2A_56HLH_20240105_0_L2A": 0.1467,
            "S2B_56HLH_20240821_0_L2A": 0.086,
            "S2B_56HLH_20191226_0_L2A": 0.9949,
        }[scene]
        flags = ["low_coverage"] if coverage < 0.6 else []
        metrics = {"coverage": coverage, "quality_flags": flags, "served_share": 0.4}
        return {"version": 1, "ok": True, "metrics": metrics}

    return handler


def test_the_failure_rows_decide_on_what_the_worker_answers(tmp_path, monkeypatch, capsys):
    import vision_worker_handler

    import vision_eval_driver as driver

    manifest = vision_eval.load_manifest()
    pinned = vision_runs.pinned_etags(manifest)
    heads, reads = [], []

    def head(url):
        heads.append(url)
        return {"etag": pinned[url], "bytes": 1}

    monkeypatch.setattr(vision_runs, "default_head", head)
    monkeypatch.setattr(vision_worker_handler, "lambda_handler", scripted_handler(reads))
    monkeypatch.setattr(
        vision_worker_handler, "http_fetch", lambda url, start, end: b"\0" * (end - start + 1)
    )
    gate = []
    picked_metrics = {
        "task": "rail_access", "coverage": 0.4891, "served_share": 0.5, "desert_km2": 10.0,
        "built_up_km2": 900.0, "station_count": 0, "flags": [], "quality_flags": [],
    }  # fmt: skip

    def picked(fetch, environ=None):
        def measure(site, scene, **kwargs):
            gate.append(scene["id"])
            return driver.scripted_measure(lambda s, sc, p: dict(picked_metrics))(site, scene, **kwargs)

        return measure

    monkeypatch.setattr(driver, "in_process_measure", picked)
    # With no Sydney fixture the rows run on an empty network (the committed one is the next test's).
    monkeypatch.setattr(vision_eval, "NETWORKS_DIR", tmp_path / "no-networks")
    out = tmp_path / "runs"
    code = vision_eval.main(
        ["failures", "--no-collect", "--cache", str(tmp_path / "cogs"), "--out", str(out)]
    )
    assert code == 0, capsys.readouterr()
    run = vision_runs.load(next(out.glob("*.json")))
    rows = run["results"]
    assert run["kind"] == "eval-failures" and run["summary"]["rows"] == 14
    assert run["summary"]["ran"] == run["summary"]["as_expected"] == 8
    cloud = rows["cloud"]["got"]
    assert cloud["measured"] == {"code": None, "coverage": 0.1467, "quality_flags": ["low_coverage"]}
    assert cloud["chosen_by_the_search"] is False  # cloud cover 66 over 40, and its footprint misses
    assert cloud["gate"]["picked_by_the_search"] is True and cloud["gate"]["codes"] == ["below_floor"]
    assert cloud["gate"]["model_calls"] == 0 and cloud["gate"]["first_observed_at"] is None
    assert gate == ["S2A_56HLH_20240228_0_L2A"]
    assert rows["swath_nodata"]["got"]["chosen_by_the_search"] is False  # its footprint stops short
    assert (
        rows["window_too_large"]["got"]["tile_reads"] == 0 and rows["window_too_large"]["got"]["reads"] == 4
    )
    assert rows["site_outside_scene"]["got"]["code"] == "site_outside_scene"
    assert rows["unreadable_scene"]["got"] == {"code": "unreadable_scene"}
    assert rows["url_off_allowlist"]["got"] == {"code": "bad_request", "reads": 0}
    both = {"cool_to_stock": "backend_mismatch", "cool_without_pin": "not_cool", "reads": 0}
    assert rows["wrong_build"]["got"] == both
    assert not rows["graph_too_large"]["ran"] and rows["agent_fails"]["elsewhere"] == "agent C6-C9 (Q2b)"
    names = {s["name"] for s in run["inputs"]["scenes"]}
    read = ("sydney-2024-01-05", "sydney-2024-02-28", "sydney-2024-08-21a", "sydney-2019-12-26-smoke")
    assert names == {*read, "sydney-2024-08-26"}
    assert (
        run["inputs"]["cache"] is True
        and run["inputs"]["networks"][0]["note"] == "no fixture: an empty network"
    )
    # A row whose worker answered otherwise is said, and the run fails its check.
    monkeypatch.setattr(
        vision_worker_handler,
        "lambda_handler",
        lambda request, context, fetch=None: {"version": 1, "ok": True,
                                              "metrics": {"coverage": 0.9, "quality_flags": []}},
    )  # fmt: skip
    argv = [
        "failures",
        "--no-collect",
        "--only",
        "smoke",
        "--only",
        "swath_nodata",
        "--cache",
        str(tmp_path / "c"),
    ]
    assert vision_eval.main(argv) == 1
    assert "swath_nodata: NOT AS EXPECTED" in capsys.readouterr().out
    # A pinned ETag that moved refuses the reading rows before any read.
    monkeypatch.setattr(vision_runs, "default_head", lambda url: {"etag": "moved", "bytes": 1})
    code = vision_eval.main(
        ["failures", "--no-collect", "--only", "cloud", "--cache", str(tmp_path / "cogs")]
    )
    assert code == vision_eval.REFUSED and "input changed" in capsys.readouterr().err


def test_the_failure_rows_name_the_committed_sydney_network_by_its_sha256(tmp_path, monkeypatch):
    monkeypatch.setattr(vision_eval, "collected_tests", lambda files: set())
    out = tmp_path / "runs"
    vision_eval.main(["failures", "--no-collect", "--only", "url_off_allowlist", "--out", str(out)])
    run = vision_runs.load(next(out.glob("*.json")))
    sydney = vision_eval.NETWORKS_DIR / "sydney.json"
    assert run["inputs"]["networks"][0] == {
        "file": vision_runs.shown_path(sydney), "sha256": vision_runs.sha256_file(sydney)
    }
    assert run["results"]["url_off_allowlist"]["got"] == {"code": "bad_request", "reads": 0}


def test_without_the_cache_only_the_rows_that_read_nothing_run_and_the_collection_is_checked(
    tmp_path, monkeypatch, capsys
):
    nodes = {node for case in vision_eval.FAILURE_CASES for node in vision_eval.node_ids(case)}
    monkeypatch.setattr(vision_eval, "collected_tests", lambda files: nodes | {"x::y"})
    out = tmp_path / "runs"
    assert vision_eval.main(["failures", "--out", str(out)]) == 0
    run = vision_runs.load(next(out.glob("*.json")))
    assert run["summary"] == {"rows": 14, "ran": 2, "as_expected": 2, "tests_named": 42, "tests_missing": 0}
    assert run["results"]["cloud"]["why_not"].startswith("reads the bucket")
    first = {"name": "every test the catalogue names is collected", "expected": [], "got": [], "pass": True}
    assert run["checks"][0] == first
    gone = sorted(nodes)[0]
    monkeypatch.setattr(vision_eval, "collected_tests", lambda files: nodes - {gone})
    assert vision_eval.main(["failures"]) == 1
    assert vision_eval.main(["failures", "--only", "nope"]) == vision_eval.REFUSED
    assert "no case named ['nope']" in capsys.readouterr().err


# --- access-baseline (E5) ------------------------------------------------------------------------------


def test_the_buffer_is_discs_and_the_precise_transform_is_euclidean():
    import numpy as np

    built = np.ones((101, 101), np.uint8)
    masks, times = vision_eval.served_masks(built, [(50.0, 50.0), None], 20.0)
    yy, xx = np.mgrid[:101, :101]
    euclid = np.hypot(xx - 50, yy - 50) <= 20.0
    assert np.array_equal(masks["precise"], euclid)
    assert abs(int(masks["buffer"].sum()) - int(euclid.sum())) <= 0.02 * euclid.sum()
    assert abs(int(masks["production"].sum()) - int(euclid.sum())) <= 0.02 * euclid.sum()
    assert set(times) == {"production", "precise", "buffer"} and all(v >= 0 for v in times.values())
    empty, _ = vision_eval.served_masks(built, [], 20.0)
    assert not any(mask.any() for mask in empty.values())


def test_access_baseline_on_the_synthetic_city_writes_e5s_run_file(tmp_path, capsys):
    out = tmp_path / "runs"
    assert vision_eval.main(["access-baseline", "--scene", "synthetic-1", "--out", str(out)]) == 0
    run = vision_runs.load(next(out.glob("*.json")))
    assert run["kind"] == "eval-access-baseline" and run["mode"] == "access-baseline"
    assert set(run["summary"]) == {
        "delta_vs_precise", "delta_vs_buffer", "iou_vs_precise", "iou_vs_buffer", "ms_production",
        "ms_precise", "ms_buffer",
    }  # fmt: skip
    methods = run["results"]["methods"]
    assert methods["production"]["iou_vs_production"] == 1.0
    assert 0.9 < run["summary"]["iou_vs_precise"] <= 1.0 and 0.9 < run["summary"]["iou_vs_buffer"] <= 1.0
    assert abs(run["summary"]["delta_vs_precise"]) < 0.01 and abs(run["summary"]["delta_vs_buffer"]) < 0.01
    assert (
        run["checks"][0]["pass"] and run["results"]["reach_px"] == 50.0 and run["results"]["stations"] == 12
    )
    assert run["inputs"]["networks"][0]["file"] == "scripts/tests/fixtures/synthetic-city-network.json"
    assert "| production | " in capsys.readouterr().out
    assert vision_eval.main(["access-baseline", "--scene", "synthetic-9"]) == vision_eval.REFUSED
    missing = tmp_path / "none.json"
    assert vision_eval.main(["access-baseline", "--network", str(missing)]) == vision_eval.REFUSED
    assert "network_file missing" in capsys.readouterr().err


def test_access_baseline_takes_a_reach_past_the_default_feeder_reach(tmp_path, capsys):
    # RailParams refuses a feeder reach under the station reach, and --reach-m may go to 5000 m
    # against the 2000 m default feeder reach: the run lifts the feeder reach to the reach, as the
    # adapter does, instead of crashing. 2500 m is 125 px on the synthetic city's 20 m grid.
    out = tmp_path / "runs"
    assert vision_eval.main(["access-baseline", "--scene", "synthetic-1", "--reach-m", "2500",
                             "--out", str(out)]) == 0  # fmt: skip
    run = vision_runs.load(next(out.glob("*.json")))
    assert run["params"]["reach_m"] == 2500.0 and run["results"]["reach_px"] == 125.0
    assert run["checks"][0]["pass"] and run["results"]["methods"]["production"]["iou_vs_production"] == 1.0
    assert "| production | " in capsys.readouterr().out


# --- index (2.9) -----------------------------------------------------------------------------------------


def test_the_committed_index_is_current():
    # The CI check: a run file added to docs/hackathon/evidence/runs/ without re-indexing fails here.
    assert vision_eval.main(["index", "--check"]) == 0


def evidence_copy(tmp_path: Path) -> Path:
    import shutil

    evidence = tmp_path / "evidence"
    (evidence / "runs").mkdir(parents=True)
    for path in (ROOT / "docs" / "hackathon" / "evidence" / "runs").glob("*.json"):
        shutil.copy(path, evidence / "runs" / path.name)
    return evidence


def test_index_writes_one_table_per_kind_and_check_fails_until_a_new_run_is_indexed(tmp_path, capsys):
    evidence = evidence_copy(tmp_path)
    assert vision_eval.main(["index", "--evidence", str(evidence), "--check"]) == 1
    assert "stale: index.json, RESULTS.md" in capsys.readouterr().err
    assert vision_eval.main(["index", "--evidence", str(evidence)]) == 0
    assert vision_eval.main(["index", "--evidence", str(evidence), "--check"]) == 0
    index = json.loads((evidence / "index.json").read_text(encoding="utf-8"))
    row = index["runs"][0]
    assert index["schema"] == "bloggerbear.vision-index/1" and row["kind"] == "eval-coverage"
    assert set(row) >= {"run_id", "kind", "date", "cpu", "backend", "build12", "git_sha", "summary"}
    assert row["build12"] == "0ef08003c311" and row["file"] == f"runs/{row['run_id']}.json"
    # A replay run file added without re-indexing: --check fails; indexed, it gets its own table.
    fixture = Path(__file__).with_name("fixtures") / "replay-synthetic-city-scripted.json"
    recording = json.loads(fixture.read_text(encoding="utf-8"))
    (evidence / "runs" / f"{recording['run_id']}.json").write_text(json.dumps(recording), encoding="utf-8")
    assert vision_eval.main(["index", "--evidence", str(evidence), "--check"]) == 1
    assert vision_eval.main(["index", "--evidence", str(evidence)]) == 0
    text = (evidence / "RESULTS.md").read_text(encoding="utf-8")
    assert text.index("## eval-coverage (1 run)") < text.index("## eval-replay (1 run)")
    assert f"[{recording['run_id']}](runs/{recording['run_id']}.json)" in text
    assert "| steps | decisions | triaged | look_again | verdict_real | verdict_artefact | finding |" in text
    # A note for a run measured on older code is rendered; one for a run that is not there is refused.
    notes = evidence / "older-code.json"
    notes.write_text(json.dumps({row["run_id"]: "coverage reads SCL only"}), encoding="utf-8")
    assert vision_eval.main(["index", "--evidence", str(evidence)]) == 0
    assert "measured on older code: coverage reads SCL only" in (evidence / "RESULTS.md").read_text()
    notes.write_text(json.dumps({"2026-01-01-nothing": "x"}), encoding="utf-8")
    assert vision_eval.main(["index", "--evidence", str(evidence)]) == vision_eval.REFUSED
    assert "is not a run file in runs/" in capsys.readouterr().err


def _with(path: Path, **changes) -> None:
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **changes}), encoding="utf-8")


@pytest.mark.parametrize(
    "spoil, said",
    [
        (lambda path: path.write_text("{not json", encoding="utf-8"), "not a run file"),
        (lambda path: _with(path, kind="eval-dreams"), "unknown run kind 'eval-dreams'"),
        (lambda path: path.rename(path.with_name("renamed.json")), "named 'renamed' but its run_id is"),
        (lambda path: _with(path, summary={"a": "b"}), "summary must map codes to numbers"),
    ],
)
def test_index_refuses_a_malformed_run_file_naming_it(tmp_path, capsys, spoil, said):
    evidence = evidence_copy(tmp_path)
    spoil(next((evidence / "runs").glob("*.json")))
    assert vision_eval.main(["index", "--evidence", str(evidence)]) == vision_eval.REFUSED
    err = capsys.readouterr().err
    assert said in err and "runs/" in err


def test_index_checks_the_operators_labels_when_there_are_any(tmp_path, capsys):
    evidence = evidence_copy(tmp_path)
    assert vision_eval.main(["index", "--evidence", str(evidence)]) == 0  # no live/ yet: nothing to check
    assert vision_eval.label_problems(evidence) == ([], 0)
    live = evidence / "live" / "2026-10-24"
    live.mkdir(parents=True)
    good = (
        "# reasons: looks_real, network_edit, cloud_or_haze, season, other_artefact, unsure\n"
        "decision,verdict,figure,trace,label,reason\n"
        "rail-sydney-demo/demo-sydney/S2A_56HLH_20241010_0_L2A,artefact,f.png,t.md,agree,cloud_or_haze\n"
        "rail-cities/sydney/S2B_56HLH_20241015_0_L2A,real,f.png,t.md,disagree,network_edit\n"
    )
    (live / "labels.csv").write_text(good, encoding="utf-8")
    assert vision_eval.label_problems(evidence) == ([], 2)
    assert vision_eval.main(["index", "--evidence", str(evidence), "--check"]) == 0
    (live / "labels.csv").write_text(
        good + "rail-cities/sydney,maybe,f.png,t.md,sure,because\n", encoding="utf-8"
    )
    problems, read = vision_eval.label_problems(evidence)
    assert read == 3 and len(problems) == 4
    assert all(problem.startswith("live/2026-10-24/labels.csv row 4") for problem in problems)
    assert vision_eval.main(["index", "--evidence", str(evidence), "--check"]) == 1
    (evidence / "live" / "labels.csv").write_text("decision,verdict,label\n", encoding="utf-8")
    assert (
        "live/labels.csv: no column ['figure', 'trace', 'reason']" in vision_eval.label_problems(evidence)[0]
    )


def test_access_baseline_refuses_a_scene_whose_pinned_etag_moved(tmp_path, monkeypatch, capsys):
    import vision_worker_handler

    network = tmp_path / "sydney.json"
    network.write_text(json.dumps({"ways": [], "stations": [], "pois": [], "fetched_at": None}))
    monkeypatch.setattr(vision_runs, "default_head", lambda url: {"etag": "moved", "bytes": 1})
    monkeypatch.setattr(vision_worker_handler, "http_fetch", lambda *a: pytest.fail("must not read"))
    code = vision_eval.main(["access-baseline", "--network", str(network), "--cache", str(tmp_path / "c")])
    assert code == vision_eval.REFUSED and "input changed" in capsys.readouterr().err


# --- review fixes: collection, the network, the index's edges, the operator's commands ------------------


class Done:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_failures_refuses_when_pytest_cannot_collect_rather_than_call_every_test_missing(
    tmp_path, monkeypatch, capsys
):
    files = ["lambdas/tests/test_vision_worker.py"]
    no_pytest = Done(1, stderr="/home/op/.venv/bin/python: No module named pytest\n")
    with pytest.raises(vision_eval.CollectionFailed, match="exited 1: .*No module named pytest"):
        vision_eval.collected_tests(files, run=lambda *args, **kwargs: no_pytest)
    broken = Done(2, stdout="ERROR lambdas/tests/test_rail_access_adapter.py\n!!! Interrupted: 1 error !!!\n")
    with pytest.raises(vision_eval.CollectionFailed, match="exited 2"):
        vision_eval.collected_tests(files, run=lambda *args, **kwargs: broken)
    found = Done(0, stdout=f"{files[0]}::test_a\n\n1 test collected in 0.1s\n")
    assert vision_eval.collected_tests(files, run=lambda *args, **kwargs: found) == {f"{files[0]}::test_a"}
    assert vision_eval.collected_tests(files, run=lambda *args, **kwargs: Done(5)) == set()
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: no_pytest)
    out = tmp_path / "runs"
    assert vision_eval.main(["failures", "--out", str(out)]) == vision_eval.REFUSED
    err = capsys.readouterr().err
    assert "No module named pytest" in err and "--no-collect" in err and "requirements-dev.txt" in err
    assert not out.exists()


def test_failures_refuses_a_network_file_that_is_not_there_when_one_is_named(tmp_path, capsys):
    code = vision_eval.main(["failures", "--no-collect", "--network", str(tmp_path / "typo.json")])
    assert code == vision_eval.REFUSED and "network_file missing" in capsys.readouterr().err


def test_index_lists_a_summary_value_that_is_null(tmp_path):
    # vision_benchmark.compare_summary writes None for an interval it could not compute.
    evidence = evidence_copy(tmp_path)
    path = next((evidence / "runs").glob("*.json"))
    run = json.loads(path.read_text(encoding="utf-8"))
    run["summary"] = {**run["summary"], "scenes": None}
    path.write_text(json.dumps(run), encoding="utf-8")
    assert vision_eval.main(["index", "--evidence", str(evidence)]) == 0
    row = next(line for line in (evidence / "RESULTS.md").read_text(encoding="utf-8").splitlines()
               if line.startswith(f"| [{run['run_id']}]"))  # fmt: skip
    assert "| n/a | 7 |" in row


def test_index_refuses_a_missing_evidence_directory_and_names_labels_that_are_not_utf8(tmp_path, capsys):
    assert vision_eval.main(["index", "--evidence", str(tmp_path / "nowhere")]) == vision_eval.REFUSED
    assert "is not a directory" in capsys.readouterr().err
    evidence = evidence_copy(tmp_path)
    assert vision_eval.main(["index", "--evidence", str(evidence)]) == 0
    live = evidence / "live" / "2026-10-24"
    live.mkdir(parents=True)
    text = (
        "decision,verdict,figure,trace,label,reason\n"
        "rail-cities/sydney/S2B_56HLH_20241015_0_L2A,real,f.png,t.md,agree,looks_real\n"
        "# saved by a spreadsheet in its own encoding: café\n"
    )
    (live / "labels.csv").write_bytes(text.encode("cp1252"))
    assert vision_eval.main(["index", "--evidence", str(evidence), "--check"]) == 1
    assert 'live/2026-10-24/labels.csv: not UTF-8 text (save it as "CSV UTF-8")' in capsys.readouterr().err


def test_ci_runs_index_check_on_a_pull_request_that_adds_only_run_files():
    """A run file's commit touches docs/hackathon/ alone: python-ci must run on it, so that this file's
    `index --check` test catches a run file added without re-indexing, and the docs must not say
    otherwise."""
    workflow = (ROOT / ".github" / "workflows" / "python-ci.yml").read_text(encoding="utf-8")
    pull_request = workflow.split("pull_request:", 1)[1].split("\n  push:", 1)[0]
    assert "'docs/hackathon/**'" in pull_request
    evidence = ROOT / "docs" / "hackathon" / "evidence"
    cool = ROOT / "docs" / "cool-benchmark-runsheet.md"
    scripts = ROOT / "scripts" / "README.md"
    for path in (evidence / "README.md", evidence / "RESULTS.md", scripts, RUNSHEET, cool):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "until python-ci also lists" not in text, path


RUNSHEET = ROOT / "docs" / "deployment-runsheet.md"
RUNSHEET_EVAL = RUNSHEET.read_text(encoding="utf-8").split("### The evaluation's real-data runs", 1)[1]
RUNSHEET_EVAL = RUNSHEET_EVAL.split("\n## ", 1)[0]


def test_the_runsheets_powershell_runs_the_venvs_python_and_checks_what_bash_checks():
    # `py -3.11` runs the global 3.11, not the activated venv's: every line after Activate.ps1 is `python`.
    powershell = re.findall(r"```powershell\n(.*?)```", RUNSHEET_EVAL, re.S)
    bash = "".join(re.findall(r"```sh\n(.*?)```", RUNSHEET_EVAL, re.S))
    lines = [line.strip() for block in powershell for line in block.splitlines()]
    after = lines[next(n for n, line in enumerate(lines) if "Activate.ps1" in line) + 1 :]
    assert not [line for line in after if "py -3.11" in line]
    # Q2a's 6, Q2b's 3, Q3's builtup-check, and the labels' index --check after the export (O9).
    assert sum(line.count("python scripts\\vision_eval.py") for line in after) == 11
    # The export's three commands, in both shells.
    for command in ("evidence_export.py export", "evidence_export.py trace", "evidence_export.py agreement"):
        assert command in bash and command.replace("/", "\\") in "".join(powershell), command
    for step in ("requirements-dev.txt", "index --check", "pii_denylist_check.py --staged"):
        assert step in bash and step in "".join(powershell), step
    assert "ids only" not in RUNSHEET_EVAL and "the agent's note" in RUNSHEET_EVAL
