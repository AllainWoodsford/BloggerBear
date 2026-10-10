"""Longitude/latitude to a scene's pixels, without PROJ.

Sentinel-2 L2A scenes are in UTM on WGS 84 (EPSG 326zz north, 327zz south). A site is configured
as a lon/lat polygon; to read and mask it the worker needs the polygon in the scene's pixels. GDAL
and PROJ would do this, but they don't fit in a Lambda zip beside OpenCV (the enhancement doc,
PR 3), and the one projection needed is a closed formula: the transverse Mercator series from
Snyder, *Map Projections: A Working Manual* (USGS PP 1395, 1987), pp. 60-61, which is good to well
under a metre inside a UTM zone. Pixels are 10 m.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

# WGS 84
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
# UTM
_K0 = 0.9996
_FALSE_EASTING = 500000.0
_FALSE_NORTHING_SOUTH = 10000000.0


def utm_zone_from_epsg(epsg: int) -> tuple[int, bool]:
    """(zone, is_south) for a WGS 84 UTM EPSG code; ValueError for anything else."""
    if 32601 <= epsg <= 32660:
        return epsg - 32600, False
    if 32701 <= epsg <= 32760:
        return epsg - 32700, True
    raise ValueError(f"EPSG {epsg} is not a WGS 84 UTM zone")


def lonlat_to_utm(lon: float, lat: float, zone: int, south: bool) -> tuple[float, float]:
    """Easting and northing, in metres, of (lon, lat) in the given UTM zone."""
    if not -80.0 <= lat <= 84.0:
        raise ValueError("UTM covers latitudes -80 to 84")
    phi = math.radians(lat)
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)
    lam = math.radians(lon)
    sin_phi, cos_phi, tan_phi = math.sin(phi), math.cos(phi), math.tan(phi)
    n = _A / math.sqrt(1 - _E2 * sin_phi**2)
    t = tan_phi**2
    c = _EP2 * cos_phi**2
    a = (lam - lam0) * cos_phi
    e4, e6 = _E2**2, _E2**3
    m = _A * (
        (1 - _E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
        - (3 * _E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
        - (35 * e6 / 3072) * math.sin(6 * phi)
    )
    easting = _FALSE_EASTING + _K0 * n * (
        a + (1 - t + c) * a**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * _EP2) * a**5 / 120
    )
    northing = _K0 * (
        m
        + n
        * tan_phi
        * (
            a**2 / 2
            + (5 - t + 9 * c + 4 * c**2) * a**4 / 24
            + (61 - 58 * t + t**2 + 600 * c - 330 * _EP2) * a**6 / 720
        )
    )
    if south:
        northing += _FALSE_NORTHING_SOUTH
    return easting, northing


def map_to_pixel(transform: Sequence[float], x: float, y: float) -> tuple[float, float]:
    """Map (x, y) to fractional pixel (col, row) with a north-up GDAL-style transform
    (c, a, b, f, d, e). Pixel (0, 0) is the top-left corner of the top-left pixel."""
    c, a, b, f, d, e = transform
    if b or d:
        raise ValueError("rotated transforms are not supported")
    return (x - c) / a, (y - f) / e


def lonlat_polygon_to_pixels(
    polygon: Iterable[Sequence[float]], epsg: int, transform: Sequence[float]
) -> list[list[float]]:
    """A lon/lat polygon as (col, row) pixel vertices of a UTM scene."""
    zone, south = utm_zone_from_epsg(epsg)
    out = []
    for lon, lat in polygon:
        x, y = lonlat_to_utm(float(lon), float(lat), zone, south)
        out.append(list(map_to_pixel(transform, x, y)))
    return out


def pixel_bounds(
    polygon_px: Sequence[Sequence[float]], width: int, height: int, pad: int = 0
) -> tuple[int, int, int, int] | None:
    """The integer window (col0, row0, col1, row1), end-exclusive and clipped to the image, that
    covers `polygon_px` plus `pad` pixels; None if the polygon is entirely outside."""
    cols = [p[0] for p in polygon_px]
    rows = [p[1] for p in polygon_px]
    col0 = max(0, math.floor(min(cols)) - pad)
    row0 = max(0, math.floor(min(rows)) - pad)
    col1 = min(width, math.ceil(max(cols)) + pad)
    row1 = min(height, math.ceil(max(rows)) + pad)
    if col0 >= col1 or row0 >= row1:
        return None
    return col0, row0, col1, row1
