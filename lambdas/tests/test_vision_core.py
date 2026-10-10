"""Tests for vision/: masks, detection, the per-site analysis, the figure and the build record.

Every scene is synthetic, built here: dark water (NDWI > 0), bright land, and objects planted with
known sizes, so each test knows exactly what should be counted. Reflectance follows Sentinel-2 L2A
(x 10000) and pixels are 10 m.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from vision import build, masks
from vision.analyse import analyse_site, pixel_to_map, size_histogram
from vision.annotate import annotate
from vision.detect import DetectParams, detect_objects

PX = 10.0
SIZE = 200
WATER_NIR, WATER_GREEN = 250, 900
LAND_NIR, LAND_GREEN = 2600, 700
SHIP_NIR, SHIP_GREEN = 2600, 800


def scene(seed: int = 7):
    """A 200x200 scene of water with mild noise, and the whole frame as the site."""
    rng = np.random.default_rng(seed)
    nir = (WATER_NIR + rng.normal(0, 20, (SIZE, SIZE))).clip(0).astype(np.uint16)
    green = (WATER_GREEN + rng.normal(0, 20, (SIZE, SIZE))).clip(0).astype(np.uint16)
    return nir, green


def plant(nir, green, centre, length_px, width_px, angle):
    """A filled rotated rectangle of ship-bright pixels."""
    box = cv2.boxPoints((centre, (length_px, width_px), angle)).astype(np.int32)
    mask = np.zeros(nir.shape, np.uint8)
    cv2.fillPoly(mask, [box], 255)
    nir[mask > 0] = SHIP_NIR
    green[mask > 0] = SHIP_GREEN


WHOLE = [[[0, 0], [SIZE - 1, 0], [SIZE - 1, SIZE - 1], [0, SIZE - 1]]]


def test_counts_elongated_ships_and_measures_them():
    nir, green = scene()
    plant(nir, green, (50, 50), 20, 4, 0)  # 200 m
    plant(nir, green, (140, 60), 30, 5, 35)  # 300 m, rotated
    plant(nir, green, (100, 150), 12, 3, 90)  # 120 m, vertical
    m = analyse_site(nir, green, WHOLE, PX).metrics
    assert m["count"] == 3
    lengths = sorted(d["length_m"] for d in m["detections"])
    assert lengths[0] == pytest.approx(120, abs=25)
    assert lengths[1] == pytest.approx(200, abs=25)
    assert lengths[2] == pytest.approx(300, abs=30)
    assert m["coverage"] == 1.0
    assert m["quality_flags"] == []
    assert sum(m["size_histogram"].values()) == 3


def test_round_blobs_specks_and_oversized_objects_are_rejected():
    nir, green = scene()
    plant(nir, green, (40, 40), 6, 6, 0)  # buoy-like square: not elongated
    nir[120, 120] = SHIP_NIR  # single bright pixel
    plant(nir, green, (100, 170), 4, 2, 0)  # 40 m: too short
    m = analyse_site(nir, green, WHOLE, PX).metrics
    assert m["count"] == 0


def test_land_inside_the_site_is_neither_counted_nor_measured():
    nir, green = scene()
    nir[:, :60] = LAND_NIR  # a coastline down the left side
    green[:, :60] = LAND_GREEN
    plant(nir, green, (130, 100), 20, 4, 10)
    result = analyse_site(nir, green, WHOLE, PX)
    assert result.metrics["count"] == 1
    assert not result.measurable[:, :55].any()
    assert result.metrics["clear_water_km2"] < SIZE * SIZE * (PX / 1000) ** 2


def test_ships_stay_inside_the_water_mask():
    nir, green = scene()
    plant(nir, green, (100, 100), 25, 5, 20)
    water = masks.water_mask(green, nir, max_object_px=400)
    assert water[100, 100] == 255


def test_cloud_hides_ships_and_lowers_coverage():
    nir, green = scene()
    plant(nir, green, (50, 100), 20, 4, 0)
    plant(nir, green, (150, 100), 20, 4, 0)
    scl = np.full(nir.shape, 6, np.uint8)
    scl[:, 100:] = 9  # cloud over the right half, including one ship
    m = analyse_site(nir, green, WHOLE, PX, scl=scl).metrics
    assert m["count"] == 1
    assert m["coverage"] == pytest.approx(0.5, abs=0.01)
    assert "low_coverage" in m["quality_flags"]


def test_only_the_site_polygon_is_looked_at():
    nir, green = scene()
    plant(nir, green, (40, 40), 20, 4, 0)
    plant(nir, green, (160, 160), 20, 4, 0)
    site = [[[0, 0], [99, 0], [99, 99], [0, 99]]]
    assert analyse_site(nir, green, site, PX).metrics["count"] == 1


def test_an_empty_site_is_flagged_not_crashed():
    nir, green = scene()
    m = analyse_site(nir, green, [], PX).metrics
    assert m["count"] == 0
    assert m["coverage"] == 0.0
    assert m["density_per_km2"] is None
    assert "empty_site" in m["quality_flags"]


def test_haze_gradient_does_not_create_detections():
    nir, green = scene()
    # Brightening haze across the scene; a global threshold would light up the right-hand side.
    nir = (nir + np.linspace(0, 700, SIZE, dtype=np.float32)[None, :]).astype(np.uint16)
    plant(nir, green, (150, 100), 20, 4, 0)
    nir[nir == SHIP_NIR] = SHIP_NIR + 700
    assert analyse_site(nir, green, WHOLE, PX).metrics["count"] == 1


def test_the_result_is_json_and_reproducible():
    import json

    nir, green = scene()
    plant(nir, green, (60, 60), 20, 4, 0)
    first = analyse_site(nir, green, WHOLE, PX, transform=(500000, 10, 0, 7000000, 0, -10)).metrics
    second = analyse_site(nir, green, WHOLE, PX, transform=(500000, 10, 0, 7000000, 0, -10)).metrics
    assert json.loads(json.dumps(first)) == first == second
    det = first["detections"][0]
    assert det["map_x"] == pytest.approx(500000 + 10 * (det["x"] + 0.5))
    assert first["params"]["block_size"] == DetectParams().block_size


def test_detect_requires_some_measurable_pixels():
    nir, green = scene()
    assert detect_objects(nir, np.zeros_like(nir, dtype=np.uint8), PX) == []


@pytest.mark.parametrize(
    "kwargs", [{"block_size": 30}, {"block_size": 1}, {"min_length_m": 0}, {"stretch_max": 0}]
)
def test_bad_params_are_refused(kwargs):
    with pytest.raises(ValueError):
        DetectParams(**kwargs)


def test_mismatched_bands_are_refused():
    nir, green = scene()
    with pytest.raises(ValueError):
        analyse_site(nir, green[:-1], WHOLE, PX)


def test_size_histogram_bands():
    assert size_histogram([100, 150, 249, 250, 399, 400, 900]) == {
        "<150": 1,
        "150-250": 2,
        "250-400": 2,
        ">=400": 2,
    }


def test_pixel_to_map_uses_pixel_centres():
    assert pixel_to_map((100.0, 10.0, 0.0, 200.0, 0.0, -10.0), 0, 0) == (105.0, 195.0)


def test_annotate_makes_a_scaled_png():
    nir, green = scene()
    plant(nir, green, (60, 60), 20, 4, 0)
    result = analyse_site(nir, green, WHOLE, PX)
    png = annotate(nir, result.measurable, result.metrics["detections"], max_side=100)
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert image.shape[:2] == (100, 100)
    # The caption bar is burned in at the top left.
    assert image[2, 2].tolist() == [0, 0, 0]


def test_build_info_and_fingerprint_match():
    info = build.build_info()
    assert info["opencv_version"].startswith("5.")
    assert len(info["build_sha256"]) == 64
    assert build.matches(info, info["build_sha256"])
    assert build.matches(info, info["build_sha256"].upper() + "\n")
    assert not build.matches(info, None)
    assert not build.matches(info, "")
    assert not build.matches(info, "0" * 64)
