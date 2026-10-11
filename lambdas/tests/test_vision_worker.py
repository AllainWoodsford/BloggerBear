"""Tests for the vision worker (vision_worker_handler.py, vision/scene.py), its contract
(common/vision_contract.py) and the pipeline's client (common/vision_client.py).

The scenes are synthetic tiled GeoTIFFs in memory (tests/vision_fakes.py), positioned in UTM zone
56S so that a small lon/lat box off Botany Bay lands inside them. For the ships task: a 256 x 256
10 m NIR and green band and a 128 x 128 20 m SCL band, with ships planted inside the box. For the
rail access task: 512 x 512 10 m red and nir bands written with a 20 m overview, 256 x 256 20 m
swir16 and scl bands, a built district with a lake in it, and a rail network drawn in pixels and
turned into lon/lat with the UTM inverse.
"""

from __future__ import annotations

import base64
import io
import json

import cv2
import numpy as np
import pytest
from vision_fakes import fetcher, write_tiff

import vision_worker_handler as worker
from common import osm, vision_client
from common import vision_contract as contract
from vision import build, cog, geo, rail_analyse, scene

PREFIX = contract.DEFAULT_ALLOWED_URL_PREFIXES[0]
NIR_URL, GREEN_URL, SCL_URL = PREFIX + "x/B08.tif", PREFIX + "x/B03.tif", PREFIX + "x/SCL.tif"
RED_URL, SWIR_URL = PREFIX + "x/B04.tif", PREFIX + "x/B11.tif"
LON0, LAT0 = 151.20, -33.97
# The box: about 1.4 km by 1.3 km.
POLYGON = [[LON0, LAT0], [LON0 + 0.015, LAT0], [LON0 + 0.015, LAT0 - 0.012], [LON0, LAT0 - 0.012]]
# The rail box: about 2.8 km by 3 km from the same corner, inside a 5.12 km scene.
RAIL_POLYGON = [[LON0, LAT0], [LON0 + 0.03, LAT0], [LON0 + 0.03, LAT0 - 0.027], [LON0, LAT0 - 0.027]]


@pytest.fixture
def files():
    e, n = geo.lonlat_to_utm(LON0, LAT0, 56, True)
    origin = (round(e) - 500.0, round(n) + 500.0)  # the box's corner at pixel (50, 50)
    rng = np.random.default_rng(3)
    nir = (250 + rng.normal(0, 20, (256, 256))).clip(1).astype(np.uint16)
    green = (900 + rng.normal(0, 20, (256, 256))).clip(1).astype(np.uint16)
    for cx, cy, angle in ((90, 90, 0), (140, 120, 30), (110, 160, 80)):
        box = cv2.boxPoints(((cx, cy), (20, 4), angle)).astype(np.int32)
        mask = np.zeros(nir.shape, np.uint8)
        cv2.fillPoly(mask, [box], 255)
        nir[mask > 0], green[mask > 0] = 2600, 800
    scl = np.full((128, 128), 6, np.uint8)
    blobs = {
        NIR_URL: write_tiff(nir, tile=64, origin=origin),
        GREEN_URL: write_tiff(green, tile=64, origin=origin),
        SCL_URL: write_tiff(scl, tile=64, origin=origin, pixel=20.0, nodata=None),
    }
    return {"blobs": blobs, "origin": origin, "nir": nir, "scl": scl}


def fetch_from(blobs, log=None):
    def fetch(url, start, end):
        if log is not None:
            log.append((url, start, end))
        if url not in blobs:
            raise cog.CogError(f"HTTP 404 reading {url}")
        return fetcher(blobs[url])(url, start, end)

    return fetch


# --- the rail scene ------------------------------------------------------------------------------

# The city, in 20 m pixels of a 256 x 256 scene: (red, nir, swir16) reflectance x 10000 of canopy
# (NDVI 0.67, NDBI -0.43) and roofs (0.09, 0.14), a district, a lake (SCL 6) inside it.
VEGETATION, BUILT = (600, 3000, 1200), (1500, 1800, 2400)
DISTRICT = (slice(60, 150), slice(60, 150))
LAKE = (slice(100, 120), slice(70, 90))
# Two crossing lines, five stations (Central on the crossing), a bus stop beside Central.
RAIL_STATIONS = {"n1": ("West", 45, 130), "n2": ("Central", 105, 130), "n3": ("East", 165, 130),
                 "n4": ("North", 105, 45), "n5": ("South", 105, 175)}  # fmt: skip
RAIL_WAYS = {"w1": ((30, 130), (170, 130)), "w2": ((105, 30), (105, 180))}


def rail_city(overviews=(2,)):
    """The four bands as blobs: the 10 m red and nir at 512 px (with `overviews`), the 20 m swir16
    and scl at 256 px, every tile past the header block so each read shows in the fetch log."""
    e, n = geo.lonlat_to_utm(LON0, LAT0, 56, True)
    origin = (round(e) - 500.0, round(n) + 500.0)  # the box's corner at 20 m pixel (25, 25)
    names = ("red", "nir", "swir16")
    coarse = {name: np.full((256, 256), v, np.uint16) for name, v in zip(names, VEGETATION, strict=True)}
    for name, value in zip(names, BUILT, strict=True):
        coarse[name][DISTRICT] = value  # the lake looks built up too; only SCL tells
    scl = np.full((256, 256), 4, np.uint8)
    scl[DISTRICT], scl[LAKE] = 5, 6
    rng = np.random.default_rng(5)

    def fine(name):  # the 10 m band: each 20 m pixel as a 2 x 2 block, with noise so tiles have bytes
        band = np.repeat(np.repeat(coarse[name], 2, axis=0), 2, axis=1).astype(np.float64)
        return (band + rng.normal(0, 20, band.shape)).clip(1).astype(np.uint16)

    gap = cog.HEADER_BYTES
    blobs = {
        RED_URL: write_tiff(fine("red"), tile=64, origin=origin, overviews=overviews, gap=gap),
        NIR_URL: write_tiff(fine("nir"), tile=64, origin=origin, overviews=overviews, gap=gap),
        SWIR_URL: write_tiff(coarse["swir16"], tile=64, origin=origin, pixel=20.0, gap=gap),
        SCL_URL: write_tiff(scl, tile=64, origin=origin, pixel=20.0, nodata=None, gap=gap),
    }
    transform = (origin[0], 20.0, 0.0, origin[1], 0.0, -20.0)
    return blobs, transform


def rail_network(transform):
    def lonlat(x, y):
        return geo.pixel_to_lonlat(transform, 32756, x, y)

    ways = [
        {"id": wid, "points": [list(lonlat(*a)), list(lonlat(*b))], "tunnel": False, "name": None}
        for wid, (a, b) in RAIL_WAYS.items()
    ]
    stations = [
        {"id": sid, "name": name, "kind": "station", "lon": lonlat(x, y)[0], "lat": lonlat(x, y)[1]}
        for sid, (name, x, y) in RAIL_STATIONS.items()
    ]
    pois = [{"id": "n9", "kind": "bus_stop", "lon": lonlat(108, 132)[0], "lat": lonlat(108, 132)[1]}]
    return {"ways": ways, "stations": stations, "pois": pois, "fetched_at": FETCHED_AT}


@pytest.fixture
def rail_files():
    blobs, transform = rail_city()
    return {"blobs": blobs, "transform": transform, "network": rail_network(transform)}


def rail_request(rail_files, **overrides):
    req = {
        "version": 1,
        "task": "rail_access",
        "backend": "opencv",
        "site": {"id": "sydney-test", "polygon": RAIL_POLYGON},
        "scene": {
            "id": "S2A_56HLH_20240105_0_L2A",
            "captured_at": "2024-01-05T00:00:00Z",
            "assets": {"red": RED_URL, "nir": NIR_URL, "swir16": SWIR_URL, "scl": SCL_URL},
        },
        # Half the default reach, so part of the district lies beyond it.
        "params": {"reach_m": 500},
        "coverage_floor": 0.6,
        "image": True,
        "network": json.loads(json.dumps(rail_files["network"])),
    }
    req.update(overrides)
    return req


def request(**overrides):
    req = {
        "version": 1,
        "backend": "opencv",
        "site": {"id": "botany-bay", "polygon": POLYGON},
        "scene": {
            "id": "S2A_56HLH_20240105_0_L2A",
            "captured_at": "2024-01-05T00:00:00Z",
            "assets": {"nir": NIR_URL, "green": GREEN_URL, "scl": SCL_URL},
        },
        "params": {},
        "coverage_floor": 0.7,
        "image": True,
    }
    req.update(overrides)
    return req


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("VISION_BACKEND", "opencv")
    monkeypatch.delenv("COOL_BUILD_SHA256", raising=False)
    monkeypatch.delenv("VISION_ALLOWED_URL_PREFIXES", raising=False)


# --- the worker ----------------------------------------------------------------------------------


def test_measures_the_site(files):
    reply = worker.lambda_handler(request(), None, fetch=fetch_from(files["blobs"]))
    assert reply["ok"], reply
    assert reply["metrics"]["count"] == 3
    assert reply["metrics"]["coverage"] == 1.0
    assert reply["epsg"] == 32756
    assert reply["build"]["opencv_version"].startswith("5.")
    assert set(reply["timings_ms"]) == {"read", "analyse", "annotate", "total"}
    # Header + tiles for each of three bands; never more than the handful under the site.
    assert 3 <= reply["io"]["requests"] <= 3 + 3 * 4
    png = base64.b64decode(reply["image_png_b64"])
    assert png.startswith(vision_client.PNG_SIGNATURE)
    json.dumps(reply)  # a Lambda reply must be JSON


def test_detections_carry_map_coordinates_inside_the_site(files):
    reply = worker.lambda_handler(request(), None, fetch=fetch_from(files["blobs"]))
    origin = files["origin"]
    xs = [d["map_x"] for d in reply["metrics"]["detections"]]
    e0, _ = geo.lonlat_to_utm(LON0, LAT0, 56, True)
    e1, _ = geo.lonlat_to_utm(LON0 + 0.015, LAT0, 56, True)
    assert all(e0 <= x <= e1 for x in xs)
    assert all(x > origin[0] for x in xs)


def test_twenty_metre_cloud_is_resampled_onto_the_ten_metre_grid(files):
    blobs = dict(files["blobs"])
    scl = files["scl"].copy()
    # Cloud over the box's right half: the box spans 10 m pixels ~50..190, so 20 m columns 60+.
    scl[:, 60:] = 9
    blobs[SCL_URL] = write_tiff(scl, tile=64, origin=files["origin"], pixel=20.0, nodata=None)
    reply = worker.lambda_handler(request(), None, fetch=fetch_from(blobs))
    m = reply["metrics"]
    assert 0.3 < m["coverage"] < 0.65
    assert "low_coverage" in m["quality_flags"]
    assert m["count"] == 2  # the ship at x=140 is under cloud


def test_scl_is_optional_and_nodata_still_counts_as_unseen(files):
    blobs = dict(files["blobs"])
    nir = files["nir"].copy()
    nir[:, :120] = 0  # nodata over the left of the box
    blobs[NIR_URL] = write_tiff(nir, tile=64, origin=files["origin"])
    req = request()
    del req["scene"]["assets"]["scl"]
    reply = worker.lambda_handler(req, None, fetch=fetch_from(blobs))
    assert reply["ok"], reply
    assert reply["metrics"]["coverage"] < 0.6


def test_no_image_when_not_asked(files):
    reply = worker.lambda_handler(request(image=False), None, fetch=fetch_from(files["blobs"]))
    assert reply["image_png_b64"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 2},
        {"backend": "gpu"},
        {"site": {"id": "x", "polygon": [[0, 0], [1, 1]]}},
        {"site": {"id": "bad id!", "polygon": POLYGON}},
        {"site": {"id": "x", "polygon": [[0, 0], [1, 1], [1, 95]]}},
        {"params": {"block_size": 30}},
        {"params": {"mystery": 1}},
        {"params": {"edge_buffer_px": 1.5}},
        {"params": {"offset": float("nan")}},
        {"params": {"min_length_m": 700}},  # above the default max_length_m
        {"params": {"min_length_m": 300, "max_length_m": 200}},
        {"coverage_floor": 2},
        {"scene": {"id": "s", "assets": {"nir": NIR_URL}}},
        {"scene": {"id": "s", "assets": {"nir": NIR_URL, "green": "https://example.com/B03.tif"}}},
        {"scene": {"id": "s", "assets": {"nir": NIR_URL, "green": GREEN_URL, "swir": GREEN_URL}}},
    ],
)
def test_bad_requests_are_refused_before_any_read(files, bad):
    log = []
    reply = worker.lambda_handler(request(**bad), None, fetch=fetch_from(files["blobs"], log))
    assert reply == {"version": 1, "ok": False, "error": "bad_request", "detail": reply["detail"]}
    assert log == []


def test_the_contracts_default_length_band_is_the_detectors():
    """The contract checks a lone min or max against the other's default, so that default must be the
    detector's own; the contract can't import it (no OpenCV in the pipeline zip)."""
    defaults = worker.DetectParams()
    assert contract.DEFAULT_LENGTH_M == (defaults.min_length_m, defaults.max_length_m)


def test_asset_hosts_can_be_configured(files, monkeypatch):
    monkeypatch.setenv("VISION_ALLOWED_URL_PREFIXES", "https://mirror.example/")
    reply = worker.lambda_handler(request(), None, fetch=fetch_from(files["blobs"]))
    assert reply["error"] == "bad_request"


def test_backend_mismatch(files):
    reply = worker.lambda_handler(request(backend="cool"), None, fetch=fetch_from(files["blobs"]))
    assert reply["error"] == "backend_mismatch"


def test_a_cool_worker_without_the_pinned_build_refuses(files, monkeypatch):
    monkeypatch.setenv("VISION_BACKEND", "cool")
    reply = worker.lambda_handler(request(backend="cool"), None, fetch=fetch_from(files["blobs"]))
    assert reply["error"] == "not_cool"
    monkeypatch.setenv("COOL_BUILD_SHA256", "0" * 64)
    reply = worker.lambda_handler(request(backend="cool"), None, fetch=fetch_from(files["blobs"]))
    assert reply["error"] == "not_cool"


def test_a_cool_worker_with_the_pinned_build_measures(files, monkeypatch):
    monkeypatch.setenv("VISION_BACKEND", "cool")
    monkeypatch.setenv("COOL_BUILD_SHA256", build.build_info()["build_sha256"])
    reply = worker.lambda_handler(request(backend="cool"), None, fetch=fetch_from(files["blobs"]))
    assert reply["ok"] and reply["backend"] == "cool"


def test_site_outside_the_scene(files):
    far = [[150.0, -35.0], [150.01, -35.0], [150.01, -35.01]]
    reply = worker.lambda_handler(
        request(site={"id": "far", "polygon": far}), None, fetch=fetch_from(files["blobs"])
    )
    assert reply["error"] == "site_outside_scene"


def test_window_too_large(files, monkeypatch):
    monkeypatch.setattr(scene, "MAX_WINDOW_PX", 64)
    reply = worker.lambda_handler(request(), None, fetch=fetch_from(files["blobs"]))
    assert reply["error"] == "window_too_large"


def test_unreadable_scene(files):
    blobs = dict(files["blobs"])
    del blobs[GREEN_URL]
    reply = worker.lambda_handler(request(), None, fetch=fetch_from(blobs))
    assert reply["error"] == "unreadable_scene"
    blobs[GREEN_URL] = b"not a tiff at all, just some bytes"
    reply = worker.lambda_handler(request(), None, fetch=fetch_from(blobs))
    assert reply["error"] == "unreadable_scene"


def test_unexpected_failures_are_internal_and_quiet(files, monkeypatch):
    def boom(*a, **k):
        raise ZeroDivisionError("secret detail")

    monkeypatch.setattr(worker, "analyse_site", boom)
    reply = worker.lambda_handler(request(), None, fetch=fetch_from(files["blobs"]))
    assert reply["error"] == "internal"
    assert "secret" not in reply["detail"]


class FakeResponse:
    def __init__(self, status, content):
        self.status_code, self.content = status, content


class FakeSession:
    def __init__(self, response):
        self.response, self.headers = response, None

    def get(self, url, headers, timeout):
        self.headers = headers
        return self.response


@pytest.mark.parametrize(
    ("status", "content", "expected"),
    [(206, b"abc", b"abc"), (200, b"0123456789", b"234")],
)
def test_http_fetch_handles_ranged_and_whole_replies(monkeypatch, status, content, expected):
    session = FakeSession(FakeResponse(status, content))
    monkeypatch.setattr(worker, "_session", session)
    assert worker.http_fetch("u", 2, 4) == expected
    assert session.headers == {"Range": "bytes=2-4"}


def test_http_fetch_errors_are_unreadable(monkeypatch):
    monkeypatch.setattr(worker, "_session", FakeSession(FakeResponse(403, b"denied")))
    with pytest.raises(cog.CogError, match="403"):
        worker.http_fetch("u", 0, 1)


# --- the rail access task ------------------------------------------------------------------------

RAIL_METRIC_KEYS = {
    "task", "coverage", "built_up_km2", "served_km2", "desert_km2", "served_share", "station_count",
    "snapped_station_count", "node_count", "edge_count", "components", "hubs", "interchanges", "flags",
    "suggestions", "stations", "deserts", "edges", "truncated", "quality_flags", "warnings", "network",
    "params",
}  # fmt: skip
FETCHED_AT = "2026-10-10T03:00:00+00:00"


def test_the_rail_task_measures_the_city(rail_files):
    reply = worker.lambda_handler(rail_request(rail_files), None, fetch=fetch_from(rail_files["blobs"]))
    assert reply["ok"], reply
    assert reply["task"] == "rail_access" and reply["metrics"]["task"] == "rail_access"
    m = reply["metrics"]
    assert set(m) == RAIL_METRIC_KEYS
    assert m["station_count"] == m["snapped_station_count"] == 5
    assert m["coverage"] == 1.0 and m["quality_flags"] == []
    assert 0 < m["served_share"] < 1 and m["desert_km2"] > 0
    assert m["params"]["pixel_size_m"] == 20.0 and m["params"]["reach_m"] == 500
    assert m["network"] == {"ways": 2, "stations": 5, "pois": 1, "points": 4, "fetched_at": FETCHED_AT}
    central = next(s for s in m["stations"] if s["id"] == "s:n2")
    assert central["degree"] == 4 and central["intermodal"]["bus_stop_near"] == 1
    assert reply["epsg"] == 32756
    assert set(reply["timings_ms"]) == {"read", "analyse", "graph", "annotate", "total"}
    assert reply["io"]["levels_used"] == {"swir16": 0, "red": 1, "nir": 1, "scl": 0}
    png = base64.b64decode(reply["image_png_b64"])
    assert png.startswith(vision_client.PNG_SIGNATURE)
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert max(image.shape[:2]) <= 1024 and image[2, 2].tolist() == [0, 0, 0]  # the caption bar
    json.dumps(reply, allow_nan=False)  # no NaN or infinity anywhere in a Lambda reply
    assert contract.validate_reply(json.loads(json.dumps(reply))) == reply


def test_the_ten_metre_bands_are_read_from_their_overview(rail_files):
    log = []
    reply = worker.lambda_handler(rail_request(rail_files), None, fetch=fetch_from(rail_files["blobs"], log))
    assert reply["ok"], reply
    for url in (RED_URL, NIR_URL):
        info, _ = cog.read_info(url, fetcher(rail_files["blobs"][url]))
        overview = info.levels[0]
        tiles = list(zip(overview.tile_offsets, overview.tile_counts, strict=True))  # (offset, bytes)
        base = list(zip(info.tile_offsets, info.tile_counts, strict=True))
        reads = [(s, e) for u, s, e in log if u == url and s >= cog.HEADER_BYTES]
        assert reads, "every tile lies past the header block, so each read must show here"
        assert all(any(o <= s and e < o + c for o, c in tiles) for s, e in reads)
        assert not any(o <= s < o + c for o, c in base for s, _ in reads)
        assert len(reads) < len(base) / 2  # a quarter of the tiles, give or take the window's edges
    # The same city with the 10 m bands written without overviews: read at 10 m and reduced by
    # area, which costs about four times the tile bytes and gives the same measurement.
    full_blobs, _ = rail_city(overviews=())
    full = worker.lambda_handler(rail_request(rail_files), None, fetch=fetch_from(full_blobs))
    assert full["ok"], full
    assert full["io"]["levels_used"] == {"swir16": 0, "red": 0, "nir": 0, "scl": 0}
    headers = 4 * cog.HEADER_BYTES  # one header block per band, the same either way
    assert reply["io"]["bytes"] - headers < (full["io"]["bytes"] - headers) / 3
    assert full["metrics"]["station_count"] == 5
    assert full["metrics"]["served_share"] == pytest.approx(reply["metrics"]["served_share"], abs=0.02)


def test_swir_nodata_is_folded_into_the_scl_as_unseen(rail_files):
    blobs = dict(rail_files["blobs"])
    e, n = geo.lonlat_to_utm(LON0, LAT0, 56, True)
    origin = (round(e) - 500.0, round(n) + 500.0)
    swir = np.full((256, 256), VEGETATION[2], np.uint16)
    swir[DISTRICT] = BUILT[2]
    swir[:, :100] = 0  # nodata over the west of the box
    blobs[SWIR_URL] = write_tiff(swir, tile=64, origin=origin, pixel=20.0, gap=cog.HEADER_BYTES)
    reply = worker.lambda_handler(rail_request(rail_files), None, fetch=fetch_from(blobs))
    assert reply["ok"], reply
    assert 0.4 < reply["metrics"]["coverage"] < 0.6 and "low_coverage" in reply["metrics"]["quality_flags"]
    west = next(s for s in reply["metrics"]["stations"] if s["id"] == "s:n1")
    assert west["catchment_coverage"] == 0.0 and west["activity"] is None  # unmeasured, not quiet


def test_the_rail_reply_has_no_image_when_not_asked(rail_files):
    req = rail_request(rail_files, image=False)
    reply = worker.lambda_handler(req, None, fetch=fetch_from(rail_files["blobs"]))
    assert reply["ok"] and reply["image_png_b64"] is None


def _far_too_many_points(r):
    r["network"]["ways"] = [{"id": "w", "points": [[151.2, -33.9]] * 60_001, "tunnel": False}]


@pytest.mark.parametrize(
    "spoil",
    [
        lambda r: r["scene"]["assets"].pop("swir16"),
        lambda r: r["scene"]["assets"].update(green=GREEN_URL),
        lambda r: r.pop("network"),
        lambda r: r.update(network=[]),
        lambda r: r["network"].pop("pois"),
        _far_too_many_points,
        lambda r: r["network"].update(stations=[{"id": "n", "lon": 151.2, "lat": -33.9}] * 2_001),
        lambda r: r["network"]["stations"][0].update(lat=95),
        lambda r: r["network"]["stations"][0].pop("id"),
        lambda r: r["network"]["stations"][0].update(kind="x" * 40),
        lambda r: r["network"]["ways"][0].update(points=[[151.2, -33.9]]),
        lambda r: r["network"]["ways"][0].update(tunnel="yes"),
        lambda r: r["network"]["pois"][0].pop("kind"),
        lambda r: r["network"].update(fetched_at=12),
        lambda r: r.update(task="mystery"),
        lambda r: r["params"].update(hub_count=2.5),
        lambda r: r["params"].update(reach_m=100),
        lambda r: r["params"].update(block_size=31),  # a ships parameter
        lambda r: r["params"].update(intermodal_near_m=900),  # above the default far radius
        lambda r: r["params"].update(intermodal_near_m=400, intermodal_far_m=300),
    ],
)
def test_bad_rail_requests_are_refused_before_any_read(rail_files, spoil):
    req = rail_request(rail_files)
    spoil(req)
    log = []
    reply = worker.lambda_handler(req, None, fetch=fetch_from(rail_files["blobs"], log))
    assert reply == {"version": 1, "ok": False, "error": "bad_request", "detail": reply["detail"]}, reply
    assert log == []


def test_a_ships_request_takes_no_network(files):
    req = request(network={"ways": [], "stations": [], "pois": []})
    reply = worker.lambda_handler(req, None, fetch=fetch_from(files["blobs"]))
    assert reply["error"] == "bad_request" and "network" in reply["detail"]


def test_a_ships_request_without_task_still_works_and_replies_ships(files, configured):
    req = request()
    assert "task" not in req
    normalised = contract.validate_request(req)
    assert normalised["task"] == "ships" and "network" not in normalised
    reply = worker.lambda_handler(req, None, fetch=fetch_from(files["blobs"]))
    assert reply["ok"] and reply["task"] == "ships" and reply["metrics"]["count"] == 3
    assert set(reply["timings_ms"]) == {"read", "analyse", "annotate", "total"}
    fake = FakeLambda(blobs=files["blobs"])
    result = vision_client.measure(SITE, SCENE, client=fake)
    assert result.reply["task"] == "ships" and fake.calls[0][1]["task"] == "ships"
    assert "network" not in fake.calls[0][1]


def test_a_params_value_error_past_the_contract_is_a_bad_request(files, rail_files, monkeypatch):
    """The contract checks a lone min or max against the other's default; if that default ever
    drifted from the detector's, the dataclass would still refuse the pair, and the worker must
    say bad_request rather than internal."""
    monkeypatch.setattr(contract, "DEFAULT_LENGTH_M", (60.0, 5000.0))
    req = request(params={"min_length_m": 700})
    reply = worker.lambda_handler(req, None, fetch=fetch_from(files["blobs"]))
    assert reply["error"] == "bad_request" and "min_length_m" in reply["detail"]
    monkeypatch.setattr(contract, "DEFAULT_INTERMODAL_M", (300.0, 5000.0))
    req = rail_request(rail_files, params={"intermodal_near_m": 900})
    reply = worker.lambda_handler(req, None, fetch=fetch_from(rail_files["blobs"]))
    assert reply["error"] == "bad_request" and "intermodal_far_m" in reply["detail"]


# --- the contract --------------------------------------------------------------------------------


def test_the_contracts_rail_limits_and_defaults_are_the_cores():
    """The contract may not import the OpenCV side, so its copies of the rail core's bounds, whole
    numbers and the intermodal defaults are held equal here, as the ship length band is."""
    assert contract.RAIL_PARAM_LIMITS == rail_analyse.PARAM_LIMITS
    assert contract.TASKS["rail_access"].whole_params == rail_analyse.WHOLE_NUMBER_PARAMS
    defaults = rail_analyse.RailParams()
    assert contract.DEFAULT_INTERMODAL_M == (defaults.intermodal_near_m, defaults.intermodal_far_m)
    assert contract.TASKS["ships"].param_limits is contract.PARAM_LIMITS
    assert contract.TASKS["ships"].whole_params == contract.WHOLE_NUMBER_PARAMS
    # And the network caps are the OSM client's, so a fetched network always fits the contract.
    assert contract.NETWORK_LIMITS == {
        "points": osm.MAX_POINTS, "ways": osm.MAX_WAYS, "stations": osm.MAX_STATIONS,
        "pois": osm.MAX_POIS, "name_chars": osm.MAX_NAME_CHARS,
    }  # fmt: skip


def test_validate_network_accepts_what_the_osm_client_returns_and_keeps_only_the_network():
    def node(osm_id, lon, lat, **tags):
        return {"type": "node", "id": osm_id, "lon": lon, "lat": lat, "tags": tags}

    elements = [
        {"type": "way", "id": 1, "tags": {"railway": "rail", "name": "Illawarra Line"},
         "geometry": [{"lon": 151.2, "lat": -33.9}, {"lon": 151.21, "lat": -33.9}]},
        {"type": "way", "id": 2, "tags": {"railway": "subway", "tunnel": "yes"},
         "geometry": [{"lon": 151.2, "lat": -33.9}, {"lon": 151.2, "lat": -33.89}]},
        node(10, 151.198, -33.892, railway="station", name="Redfern"),
        node(11, 151.19, -33.88, railway="halt"),
        node(20, 151.199, -33.893, highway="bus_stop"),
    ]  # fmt: skip
    fetched = {
        "source": "overpass", "endpoint": osm.OVERPASS_ENDPOINTS[0], "fetched_at": FETCHED_AT,
        "query_url": "https://overpass-api.de/api/interpreter?data=...",
        **osm.cap_network(osm.parse_elements(elements)),
    }  # fmt: skip
    assert fetched["counts"] == {"ways": 2, "stations": 2, "pois": 1, "points": 4}
    network = contract.validate_network(fetched)
    assert set(network) == {"ways", "stations", "pois", "fetched_at"}
    assert network["ways"] == fetched["ways"] and network["stations"] == fetched["stations"]
    assert network["pois"] == fetched["pois"] and network["fetched_at"] == fetched["fetched_at"]
    assert network["ways"][1]["name"] is None and network["stations"][1]["name"] == "Unnamed station (n11)"


def test_validate_network_normalises_ids_and_names():
    long_name = " Bad\x00 ‮Name  " + "x" * 100  # a control character, a bidi override, too long
    network = contract.validate_network(
        {
            "ways": [{"id": 7, "points": [[151.2, -33.9], [151.21, -33.9]], "name": long_name}],
            "stations": [
                {"id": 12, "lon": 151.2, "lat": -33.9, "name": "\x1f"},
                {"id": "n3", "lon": 1, "lat": 1},
            ],
            "pois": [{"id": 99, "lon": 151.2, "lat": -33.9, "kind": "bus_stop"}],
        }
    )
    way, first, second = network["ways"][0], *network["stations"]
    assert way["id"] == "7" and way["tunnel"] is False
    assert way["name"].startswith("Bad Name xxx")
    assert len(way["name"]) == contract.NETWORK_LIMITS["name_chars"]
    unnamed = {"id": "12", "name": "Unnamed station (12)", "lon": 151.2, "lat": -33.9, "kind": "station"}
    assert first == unnamed
    assert second["name"] == "Unnamed station (n3)" and second["lon"] == 1.0
    assert network["pois"] == [{"id": "99", "lon": 151.2, "lat": -33.9, "kind": "bus_stop"}]
    assert network["fetched_at"] is None


def rail_reply(**overrides):
    reply = {
        "version": 1, "ok": True, "task": "rail_access", "backend": "opencv", "site_id": "s", "scene_id": "x",
        "metrics": {"coverage": 0.9, "station_count": 3, "served_share": 0.5, "quality_flags": []},
        "build": {}, "image_png_b64": None,
    }  # fmt: skip
    reply.update(overrides)
    return reply


def test_replies_are_checked_per_task():
    assert contract.validate_reply(rail_reply()) == rail_reply()
    assert contract.validate_reply(good_reply(task="ships")) == good_reply(task="ships")
    bad = [
        rail_reply(task="mystery"),
        rail_reply(metrics={"coverage": 0.9, "station_count": -1, "served_share": 0.5, "quality_flags": []}),
        rail_reply(metrics={"coverage": 0.9, "station_count": 3.0, "served_share": 0.5, "quality_flags": []}),
        rail_reply(metrics={"coverage": 0.9, "station_count": 3, "served_share": 1.5, "quality_flags": []}),
        rail_reply(metrics={"coverage": 0.9, "station_count": 3, "served_share": 0.5}),
        rail_reply(metrics={"count": 2, "coverage": 0.9, "quality_flags": []}),  # the ships metrics
        good_reply(metrics={"coverage": 0.9, "station_count": 3, "served_share": 0.5, "quality_flags": []}),
    ]
    for reply in bad:
        with pytest.raises(contract.ContractError):
            contract.validate_reply(reply)


# --- the client ----------------------------------------------------------------------------------

ARN = "arn:aws:lambda:us-west-2:123456789012:function:bloggerbear-dev-vision-worker"


class FakeLambda:
    """Invokes the real handler in-process, the way Lambda would."""

    def __init__(self, blobs=None, reply=None, function_error=None, raises=None):
        self.blobs, self.reply, self.function_error, self.raises = blobs, reply, function_error, raises
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append((FunctionName, json.loads(Payload)))
        if self.raises:
            raise self.raises
        if self.reply is not None:
            body = self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode()
        else:
            reply = worker.lambda_handler(json.loads(Payload), None, fetch=fetch_from(self.blobs))
            body = json.dumps(reply).encode()
        out = {"Payload": io.BytesIO(body), "StatusCode": 200}
        if self.function_error:
            out["FunctionError"] = self.function_error
        return out


SITE = {"id": "botany-bay", "polygon": POLYGON}
SCENE = request()["scene"]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("VISION_WORKER_ARN", ARN)
    monkeypatch.delenv("VISION_COOL_WORKER_ARN", raising=False)


def test_client_round_trip(files, configured):
    fake = FakeLambda(blobs=files["blobs"])
    result = vision_client.measure(SITE, SCENE, client=fake)
    assert result.metrics["count"] == 3
    assert result.image_png.startswith(vision_client.PNG_SIGNATURE)
    name, payload = fake.calls[0]
    assert name == ARN
    assert payload["backend"] == "opencv" and payload["version"] == 1


def test_client_region_comes_from_the_arn():
    assert vision_client.region_of(ARN) == "us-west-2"
    with pytest.raises(ValueError):
        vision_client.region_of("bloggerbear-dev-vision-worker")


def test_client_not_configured(monkeypatch):
    monkeypatch.delenv("VISION_WORKER_ARN", raising=False)
    assert not vision_client.is_configured()
    with pytest.raises(vision_client.VisionError) as err:
        vision_client.measure(SITE, SCENE, client=FakeLambda())
    assert err.value.code == "invoke"
    with pytest.raises(ValueError):
        vision_client.worker_arn("gpu")


def test_client_validates_before_invoking(configured):
    fake = FakeLambda(reply={})
    with pytest.raises(vision_client.VisionError) as err:
        vision_client.measure({"id": "x", "polygon": [[0, 0]]}, SCENE, client=fake)
    assert err.value.code == "bad_request"
    assert fake.calls == []


def test_client_passes_on_the_worker_error(files, configured):
    blobs = dict(files["blobs"])
    del blobs[NIR_URL]
    with pytest.raises(vision_client.VisionError) as err:
        vision_client.measure(SITE, SCENE, client=FakeLambda(blobs=blobs))
    assert err.value.code == "unreadable_scene"


@pytest.mark.parametrize(
    ("fake", "code"),
    [
        (FakeLambda(raises=TimeoutError("read timeout")), "invoke"),
        (FakeLambda(reply={"errorMessage": "boom"}, function_error="Unhandled"), "invoke"),
        (FakeLambda(reply=b"not json"), "reply"),
        (FakeLambda(reply={"version": 1, "ok": True, "metrics": {}}), "reply"),
        (FakeLambda(reply={"version": 1, "ok": False, "error": "made-up"}), "reply"),
    ],
)
def test_client_failures_raise(configured, fake, code):
    with pytest.raises(vision_client.VisionError) as err:
        vision_client.measure(SITE, SCENE, client=fake)
    assert err.value.code == code


def good_reply(**overrides):
    reply = {
        "version": 1,
        "ok": True,
        "backend": "opencv",
        "site_id": SITE["id"],
        "scene_id": SCENE["id"],
        "metrics": {"count": 2, "coverage": 0.9, "quality_flags": []},
        "build": {},
        "image_png_b64": None,
    }
    reply.update(overrides)
    return reply


@pytest.mark.parametrize(
    "overrides",
    [
        {"site_id": "elsewhere"},
        {"scene_id": "other-scene"},
        {"backend": "cool"},
        {"image_png_b64": base64.b64encode(b"GIF89a....").decode()},
        {"image_png_b64": "***"},
        {"metrics": {"count": -1, "coverage": 0.9, "quality_flags": []}},
        {"metrics": {"count": True, "coverage": 0.9, "quality_flags": []}},
        {"metrics": {"count": 1, "coverage": 1.5, "quality_flags": []}},
    ],
)
def test_client_refuses_replies_that_dont_match(configured, overrides):
    with pytest.raises(vision_client.VisionError) as err:
        vision_client.measure(SITE, SCENE, client=FakeLambda(reply=good_reply(**overrides)))
    assert err.value.code == "reply"


def test_client_refuses_an_oversized_image(configured, monkeypatch):
    monkeypatch.setattr(vision_client, "MAX_IMAGE_BYTES", 16)
    png = base64.b64encode(vision_client.PNG_SIGNATURE + b"\x00" * 64).decode()
    with pytest.raises(vision_client.VisionError, match="too large"):
        vision_client.measure(SITE, SCENE, client=FakeLambda(reply=good_reply(image_png_b64=png)))


def test_client_cool_backend_uses_its_own_arn(files, configured, monkeypatch):
    with pytest.raises(vision_client.VisionError):
        vision_client.measure(SITE, SCENE, backend="cool", client=FakeLambda(blobs=files["blobs"]))
    cool_arn = ARN.replace("vision-worker", "vision-cool")
    monkeypatch.setenv("VISION_COOL_WORKER_ARN", cool_arn)
    reply = good_reply(backend="cool")
    fake = FakeLambda(reply=reply)
    vision_client.measure(SITE, SCENE, backend="cool", client=fake)
    assert fake.calls[0][0] == cool_arn


RAIL_SITE = {"id": "sydney-test", "polygon": RAIL_POLYGON}


def test_client_sends_the_task_and_the_network_and_checks_the_task_in_the_reply(rail_files, configured):
    fake = FakeLambda(blobs=rail_files["blobs"])
    rail_scene = rail_request(rail_files)["scene"]
    result = vision_client.measure(
        RAIL_SITE, rail_scene, params={"reach_m": 500}, coverage_floor=0.6,
        task="rail_access", network=rail_files["network"], client=fake,
    )  # fmt: skip
    assert result.metrics["station_count"] == 5 and result.reply["task"] == "rail_access"
    assert result.image_png.startswith(vision_client.PNG_SIGNATURE)
    payload = fake.calls[0][1]
    assert payload["task"] == "rail_access" and payload["params"] == {"reach_m": 500}
    assert set(payload["network"]) == {"ways", "stations", "pois", "fetched_at"}
    assert [s["id"] for s in payload["network"]["stations"]] == list(RAIL_STATIONS)
    # A bad network fails at home, before any cross-region call.
    fake = FakeLambda(blobs=rail_files["blobs"])
    with pytest.raises(vision_client.VisionError) as err:
        vision_client.measure(RAIL_SITE, rail_scene, task="rail_access", network={"ways": []}, client=fake)
    assert err.value.code == "bad_request" and fake.calls == []
    # A reply for the other task is refused, even when it is a well-formed reply for that task.
    rail_for_ships = FakeLambda(reply=rail_reply(site_id=SITE["id"], scene_id=SCENE["id"]))
    with pytest.raises(vision_client.VisionError, match="different task"):
        vision_client.measure(SITE, SCENE, client=rail_for_ships)
    with pytest.raises(vision_client.VisionError, match="different task"):
        vision_client.measure(
            RAIL_SITE, rail_scene, task="rail_access", network=rail_files["network"],
            client=FakeLambda(reply=good_reply(site_id=RAIL_SITE["id"], scene_id=rail_scene["id"])),
        )  # fmt: skip


def test_client_keeps_one_client_per_region_and_read_timeout(configured, monkeypatch):
    made = []

    def fake_boto_client(service, region_name, config):
        made.append((service, region_name, config.connect_timeout, config.read_timeout, config.retries))
        return FakeLambda(reply=good_reply())

    monkeypatch.setattr(vision_client.boto3, "client", fake_boto_client)
    monkeypatch.setattr(vision_client, "_clients", {})
    default = vision_client._client("us-west-2")
    shorter = vision_client._client("us-west-2", 58)
    assert default is vision_client._client("us-west-2") and shorter is vision_client._client("us-west-2", 58)
    assert default is not shorter
    retries = {"max_attempts": 1, "mode": "standard"}
    assert made == [("lambda", "us-west-2", 5, 90, retries), ("lambda", "us-west-2", 5, 58, retries)]
    # measure() asks for the client with its read timeout: the triage agent's look_again waits less.
    vision_client.measure(SITE, SCENE, read_timeout=30)
    assert made[-1][1:4] == ("us-west-2", 5, 30) and len(made) == 3
    vision_client.measure(SITE, SCENE, read_timeout=30)
    assert len(made) == 3
