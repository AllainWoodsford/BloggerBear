"""Tests for vision/cog.py (windowed reads of tiled GeoTIFFs) and vision/geo.py (lon/lat to pixels).

The GeoTIFFs are written in memory by tests/vision_fakes.py's writer, which produces what
`sentinel-cogs` serves: single-band, tiled, DEFLATE with the horizontal predictor, georeferenced by
pixel scale and tiepoint with an EPSG GeoKey. It was checked against a real Sentinel-2 B08 file on 2026-10-06
(tile 56HLH: 10980 x 10980, 1024 px tiles, uint16, DEFLATE, predictor 2, EPSG 32756), which the
reader read in three range requests. The UTM formula was checked against PROJ (pyproj) at points in
zones 17N, 36N and 56S: under a millimetre apart.
"""

from __future__ import annotations

import numpy as np
import pytest
from vision_fakes import fetcher, write_tiff

from vision import cog, geo


def image(h=50, w=70, dtype=np.uint16):
    return (np.arange(h * w, dtype=np.int64).reshape(h, w) * 37 % 5000).astype(dtype)


@pytest.mark.parametrize("big", [False, True])
@pytest.mark.parametrize("order", ["<", ">"])
@pytest.mark.parametrize(("compression", "predictor"), [(8, 2), (8, 1), (1, 1)])
def test_window_matches_the_image(big, order, compression, predictor):
    img = image()
    blob = write_tiff(img, compression=compression, predictor=predictor, big=big, order=order)
    window = (5, 7, 41, 39)
    out, info = cog.read_window("u", window, fetcher(blob))
    np.testing.assert_array_equal(out, img[7:39, 5:41])
    assert (info.width, info.height, info.tile_width) == (70, 50, 16)
    assert info.epsg == 32756
    assert info.transform == (300000.0, 10.0, 0.0, 6300040.0, 0.0, -10.0)
    assert info.nodata == 0.0


@pytest.mark.parametrize("dtype", [np.uint8, np.int16, np.float32])
def test_other_sample_types(dtype):
    img = image(dtype=dtype)
    blob = write_tiff(img, predictor=1 if dtype == np.float32 else 2)
    out, info = cog.read_window("u", (0, 0, 70, 50), fetcher(blob))
    np.testing.assert_array_equal(out, img)
    assert out.dtype == np.dtype(dtype)


def test_only_the_tiles_under_the_window_are_fetched():
    blob = write_tiff(image(64, 64), tile=16, gap=cog.HEADER_BYTES)
    log = []
    cog.read_window("u", (17, 17, 30, 30), fetcher(blob, log))
    # The header block, then exactly one tile (16..31 in both directions).
    assert len(log) == 2
    assert log[0] == (0, cog.HEADER_BYTES - 1)


def test_missing_tiles_read_as_nodata():
    img = image(32, 32) + 1
    blob = write_tiff(img, tile=16, drop_tiles=(0,))
    out, _ = cog.read_window("u", (0, 0, 32, 32), fetcher(blob))
    assert not out[:16, :16].any()
    np.testing.assert_array_equal(out[16:, 16:], img[16:, 16:])


def test_window_transform_moves_the_origin():
    blob = write_tiff(image())
    info, _ = cog.read_info("u", fetcher(blob))
    assert cog.window_transform(info, (5, 7, 10, 10)) == (300050.0, 10.0, 0.0, 6299970.0, 0.0, -10.0)


@pytest.mark.parametrize(
    ("blob", "message"),
    [
        (b"GIF89a" + b"\x00" * 20, "not a TIFF"),
        (b"II", "too short"),
    ],
)
def test_garbage_is_refused(blob, message):
    with pytest.raises(cog.CogError, match=message):
        cog.read_info("u", fetcher(blob))


def test_unsupported_compression_is_refused():
    blob = write_tiff(image(), compression=5, predictor=1)
    with pytest.raises(cog.CogError, match="compression 5"):
        cog.read_info("u", fetcher(blob))


def test_window_outside_the_image_is_refused():
    blob = write_tiff(image())
    with pytest.raises(cog.CogError, match="outside"):
        cog.read_window("u", (60, 0, 80, 10), fetcher(blob))


def test_a_corrupt_tile_is_refused():
    blob = write_tiff(image())
    with pytest.raises(cog.CogError):
        cog.read_window("u", (0, 0, 10, 10), fetcher(_corrupt_first_tile(blob)))


def _corrupt_first_tile(blob: bytes) -> bytes:
    info, _ = cog.read_info("u", fetcher(blob))
    start = info.tile_offsets[0]
    return blob[:start] + b"\xff" * info.tile_counts[0] + blob[start + info.tile_counts[0] :]


# --- geo -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lon", "lat", "zone", "south", "expected"),
    [
        # Reference values from PROJ (pyproj 3.7), EPSG:4326 -> 32756 / 32636 / 32617.
        (151.2153, -33.8568, 56, True, (334900.570, 6252288.753)),
        (32.55, 29.9, 36, False, (456554.867, 3307789.674)),
        (-79.9, 9.0, 17, False, (620912.498, 995033.764)),
    ],
)
def test_lonlat_to_utm_matches_proj(lon, lat, zone, south, expected):
    e, n = geo.lonlat_to_utm(lon, lat, zone, south)
    assert e == pytest.approx(expected[0], abs=0.01)
    assert n == pytest.approx(expected[1], abs=0.01)


def test_utm_zone_from_epsg():
    assert geo.utm_zone_from_epsg(32756) == (56, True)
    assert geo.utm_zone_from_epsg(32636) == (36, False)
    with pytest.raises(ValueError):
        geo.utm_zone_from_epsg(3857)


def test_polygon_to_pixels_and_bounds():
    transform = (300000.0, 10.0, 0.0, 6300040.0, 0.0, -10.0)
    # Botany Bay, as read from tile 56HLH on 2026-10-06.
    px = geo.lonlat_polygon_to_pixels([[151.20, -33.97], [151.26, -34.02]], 32756, transform)
    assert px[0] == pytest.approx([3370.51, 6032.95], abs=0.01)
    assert geo.pixel_bounds(px, 10980, 10980, pad=10) == (3360, 6022, 3945, 6588)
    assert geo.pixel_bounds([[-50, -50], [-10, -10]], 100, 100) is None
    assert geo.pixel_bounds([[90, 90], [150, 150]], 100, 100) == (90, 90, 100, 100)


def test_rotated_transforms_are_refused():
    with pytest.raises(ValueError):
        geo.map_to_pixel((0, 1, 0.5, 0, 0, -1), 1, 1)


def test_latitude_outside_utm_is_refused():
    with pytest.raises(ValueError):
        geo.lonlat_to_utm(0, 85, 31, False)
