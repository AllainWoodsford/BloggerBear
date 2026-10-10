"""Tests for the vision worker (vision_worker_handler.py, vision/scene.py), its contract
(common/vision_contract.py) and the pipeline's client (common/vision_client.py).

The scene is synthetic: a 256 x 256 10 m NIR and green band and a 128 x 128 20 m SCL band, all
tiled GeoTIFFs in memory (tests/vision_fakes.py), positioned in UTM zone 56S so that a small
lon/lat box off Botany Bay lands inside them. Ships are planted inside the box.
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
from common import vision_client
from common import vision_contract as contract
from vision import build, cog, geo, scene

PREFIX = contract.DEFAULT_ALLOWED_URL_PREFIXES[0]
NIR_URL, GREEN_URL, SCL_URL = PREFIX + "x/B08.tif", PREFIX + "x/B03.tif", PREFIX + "x/SCL.tif"
LON0, LAT0 = 151.20, -33.97
# The box: about 1.4 km by 1.3 km.
POLYGON = [[LON0, LAT0], [LON0 + 0.015, LAT0], [LON0 + 0.015, LAT0 - 0.012], [LON0, LAT0 - 0.012]]


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
            log.append(url)
        if url not in blobs:
            raise cog.CogError(f"HTTP 404 reading {url}")
        return fetcher(blobs[url])(url, start, end)

    return fetch


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
