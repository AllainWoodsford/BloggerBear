"""Tests for vision/: masks, detection, the per-site analysis, the figure, the build record, and
the scene reader's choice of COG level and resampling per band.

Every scene is synthetic, built here: dark water (NDWI > 0), bright land, and objects planted with
known sizes, so each test knows exactly what should be counted. Reflectance follows Sentinel-2 L2A
(x 10000) and pixels are 10 m. The scene reader's tests write small tiled GeoTIFFs in memory
(tests/vision_fakes.py), with and without overviews.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from vision_fakes import block_mean, write_tiff

from vision import build, cog, geo, masks
from vision import scene as scene_reader
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


def test_blobs_beside_the_shore_are_dropped_and_tallied():
    nir, green = scene()
    nir[:, :60] = LAND_NIR
    green[:, :60] = LAND_GREEN
    # Moored 3 px (30 m) off the shore: separate from the land, but inside a 4 px buffer.
    plant(nir, green, (65, 100), 20, 4, 90)
    plant(nir, green, (130, 100), 20, 4, 10)  # clear of it
    m = analyse_site(nir, green, WHOLE, PX, params=DetectParams(edge_buffer_px=4)).metrics
    assert m["count"] == 1
    assert m["rejected"]["edge"] == 1
    assert m["rejected"]["candidates"] == 2
    # With the buffer off, the moored one counts too.
    off = analyse_site(nir, green, WHOLE, PX, params=DetectParams(edge_buffer_px=0)).metrics
    assert off["count"] == 2
    assert off["rejected"]["edge"] == 0


def test_rejection_tally_names_the_filters():
    nir, green = scene()
    plant(nir, green, (40, 40), 6, 6, 0)  # shape
    plant(nir, green, (100, 170), 4, 2, 0)  # size
    m = analyse_site(nir, green, WHOLE, PX).metrics
    assert m["rejected"]["shape"] == 1
    assert m["rejected"]["size"] >= 1


# --- the scene reader: overview levels and area resampling ---------------------------------------

ORIGIN, EPSG = (300000.0, 6300040.0), 32756
TRANSFORM_20 = (ORIGIN[0], 20.0, 0.0, ORIGIN[1], 0.0, -20.0)
# A site inside a 64 x 64 scene at 20 m: from pixel (12, 12) to (44, 44), lon/lat through the inverse.
SITE_CORNERS_PX = ((12, 12), (44, 12), (44, 44), (12, 44))
SITE_LONLAT = [list(geo.pixel_to_lonlat(TRANSFORM_20, EPSG, x, y)) for x, y in SITE_CORNERS_PX]


def fetch_blobs(blobs, log=None):
    def fetch(url, start, end):
        if log is not None:
            log.append((url, start, end))
        return blobs[url][start : end + 1]

    return fetch


def assets_of(blobs):
    """The assets map (band name -> URL) for blobs keyed by the name used as the URL."""
    return {name: name for name in blobs}


def fine_band(seed=1):
    """A 128 x 128 10 m band with enough noise that every tile has bytes of its own."""
    rng = np.random.default_rng(seed)
    return (1000 + rng.normal(0, 100, (128, 128))).clip(1).astype(np.uint16)


def test_read_site_reads_a_ten_metre_band_from_its_twenty_metre_overview():
    fine = fine_band()
    checker = np.zeros((128, 128), np.uint8)
    checker[::2, ::2] = checker[1::2, 1::2] = 9  # 4 and 9 in equal measure in every 2 x 2 block
    checker[checker == 0] = 4
    gap = cog.HEADER_BYTES
    blobs = {
        "ref": write_tiff(np.full((64, 64), 7, np.uint16), tile=16, pixel=20.0, nodata=None, gap=gap),
        "fine": write_tiff(fine, tile=16, overviews=(2,), gap=gap),
        "scl": write_tiff(checker, tile=16, nodata=None, gap=gap),
    }
    log = []
    site = scene_reader.read_site(assets_of(blobs), SITE_LONLAT, fetch_blobs(blobs, log), reference="ref")
    assert site.pixel_size_m == 20.0 and site.levels_used == {"ref": 0, "fine": 1, "scl": 0}
    col0, row0, col1, row1 = site.window
    assert (col0, row0) == (0, 0) and (col1, row1) == (61, 61)  # 12 - 16 clipped; past 44, plus 16
    # The overview is the block mean the writer stored, read in the overview's own pixels, and
    # with the two grids sharing an origin it lands without a warp: the exact same values.
    np.testing.assert_array_equal(site.bands["fine"], block_mean(fine, 2)[row0:row1, col0:col1])
    # Classes are never averaged: the 10 m SCL onto the 20 m grid keeps 4 and 9, never 6 or 7.
    assert set(np.unique(site.bands["scl"])) <= {4, 9}
    # The 10 m file's reads past the header all fall in its overview's tiles, none in the base's.
    info, _ = cog.read_info("fine", fetch_blobs(blobs))
    reads = [(s, e) for u, s, e in log if u == "fine" and s >= cog.HEADER_BYTES]
    overview_tiles = list(zip(info.levels[0].tile_offsets, info.levels[0].tile_counts, strict=True))
    assert reads and all(any(o <= s and e < o + c for o, c in overview_tiles) for s, e in reads)
    base_tiles = list(zip(info.tile_offsets, info.tile_counts, strict=True))
    assert not any(o <= s < o + c for o, c in base_tiles for s, _ in reads)


def test_read_site_reduces_a_band_without_overviews_by_area_unless_it_is_categorical():
    fine = fine_band()
    checker = np.zeros((128, 128), np.uint8)
    checker[::2, ::2] = checker[1::2, 1::2] = 9
    checker[checker == 0] = 4
    gap = cog.HEADER_BYTES
    blobs = {
        "ref": write_tiff(np.full((64, 64), 7, np.uint16), tile=16, pixel=20.0, nodata=None, gap=gap),
        "fine": write_tiff(fine, tile=16, gap=gap),
        "scl": write_tiff(checker, tile=16, nodata=None, gap=gap),
        "mix": write_tiff(checker, tile=16, nodata=None, gap=gap),
    }
    site = scene_reader.read_site(assets_of(blobs), SITE_LONLAT, fetch_blobs(blobs), reference="ref")
    assert site.levels_used == {"ref": 0, "fine": 0, "scl": 0, "mix": 0}
    col0, row0, col1, row1 = site.window
    # INTER_AREA over exact 2 x 2 blocks is the block mean, to the rounding of a half.
    expected = block_mean(fine, 2)[row0:row1, col0:col1].astype(np.int64)
    assert np.abs(site.bands["fine"].astype(np.int64) - expected).max() <= 1
    assert site.bands["fine"].dtype == np.uint16
    assert set(np.unique(site.bands["scl"])) <= {4, 9}  # categorical: nearest neighbour
    assert set(np.unique(site.bands["mix"])) <= {6, 7}  # not: the mean of two 4s and two 9s
    # Reading the full image costs about four times the overview's tile bytes (the header block
    # of each file is read either way).
    with_overview = dict(blobs, fine=write_tiff(fine, tile=16, overviews=(2,), gap=gap))
    cheaper = scene_reader.read_site(
        assets_of(with_overview), SITE_LONLAT, fetch_blobs(with_overview), reference="ref"
    )
    headers = len(blobs) * cog.HEADER_BYTES
    assert cheaper.bytes_read - headers < (site.bytes_read - headers) / 2
    assert np.abs(cheaper.bands["fine"].astype(np.int64) - site.bands["fine"].astype(np.int64)).max() <= 1



def test_the_area_reduction_keeps_whole_blocks_when_the_covering_window_is_odd():
    """A target grid 5 m off the 10 m band's makes an odd covering window (21 x 21 fine pixels for
    10 x 10 target ones): the partial block at the edge is dropped, so every value is an exact
    2 x 2 block mean, and the residual quarter-pixel shift is nearest neighbour onto the grid."""
    fine = fine_band()
    blobs = {"fine": write_tiff(fine, tile=16, gap=cog.HEADER_BYTES)}
    target = (ORIGIN[0] + 5.0, 20.0, 0.0, ORIGIN[1] - 5.0, 0.0, -20.0)
    data, nodata, level = scene_reader._read_aligned("fine", fetch_blobs(blobs), target, (10, 10))
    assert data.shape == (10, 10) and data.dtype == np.uint16 and level == 0
    means = block_mean(fine, 2)[:10, :10].astype(np.int64)
    # Nearest neighbour picks each target pixel's block; every value is one block's mean (to the
    # rounding of a half), never a mean across a partial block.
    assert np.abs(data.astype(np.int64) - means).max() <= 1


def test_the_area_reduction_keeps_a_column_narrower_than_one_block():
    """A window clipped to the band's last column is one fine pixel wide, narrower than a 2 x 2
    block: it is kept at its own 10 m size (not stretched to 20 m), and the half of the target
    pixel past the image's edge is the fill."""
    fine = fine_band()
    blobs = {"fine": write_tiff(fine, tile=16, nodata="0", gap=cog.HEADER_BYTES)}
    target = (ORIGIN[0] + 1270.0, 20.0, 0.0, ORIGIN[1], 0.0, -20.0)
    data, nodata, _ = scene_reader._read_aligned("fine", fetch_blobs(blobs), target, (2, 1))
    assert data.shape == (2, 1) and nodata == 0
    # The column is reduced down only (two rows into one), so each value is the mean of a pair of
    # the last column's pixels, to the rounding of a half; or the fill past the edge.
    pairs = fine[:4, 127].astype(np.float64).reshape(2, 2).mean(axis=1)
    for value in data.ravel().tolist():
        assert value == 0 or min(abs(value - p) for p in pairs) <= 1

def test_read_site_warps_a_band_whose_grid_is_shifted_or_coarser():
    fine = fine_band()
    gap = cog.HEADER_BYTES
    ref = np.full((64, 64), 7, np.uint16)
    blobs = {
        "ref": write_tiff(ref, tile=16, pixel=20.0, nodata=None, gap=gap),
        # The same 10 m band with its grid 12 m east: its 20 m overview is read, but the grids
        # differ by 0.6 px, so it is put onto the reference grid with nearest neighbour.
        "shifted": write_tiff(fine, tile=16, overviews=(2,), origin=(ORIGIN[0] + 12.0, ORIGIN[1]), gap=gap),
        # A 40 m band: coarser than the reference, so nearest neighbour spreads each pixel over 2 x 2.
        "coarse": write_tiff(block_mean(fine, 4), tile=16, pixel=40.0, nodata=None, gap=gap),
    }
    site = scene_reader.read_site(assets_of(blobs), SITE_LONLAT, fetch_blobs(blobs), reference="ref")
    assert site.levels_used == {"ref": 0, "shifted": 1, "coarse": 0}
    col0, row0, col1, row1 = site.window
    shifted = site.bands["shifted"]
    overview = block_mean(fine, 2)
    # Target pixel i's centre lies 0.6 px into shifted pixel i - 1, whose value it takes; the
    # first column has no pixel to its west and is the fill (the band's nodata, 0).
    assert shifted.shape == (row1 - row0, col1 - col0)
    assert not shifted[:, 0].any()
    np.testing.assert_array_equal(shifted[:, 1:], overview[row0:row1, col0 : col1 - 1])
    coarse = site.bands["coarse"]
    assert coarse.shape == (row1 - row0, col1 - col0)
    n = (row1 - row0) // 2  # the window is 61 px, so the last row and column are half a 40 m pixel
    even, odd = coarse[: 2 * n : 2, : 2 * n : 2], coarse[1 : 2 * n : 2, 1 : 2 * n : 2]
    np.testing.assert_array_equal(even, odd)
    forty = block_mean(fine, 4)[row0 // 2 : row0 // 2 + n, col0 // 2 : col0 // 2 + n]
    np.testing.assert_array_equal(even, forty)
