"""A site's bands out of a scene, on one pixel grid.

One band is the reference (nir at 10 m for ships, swir16 at 20 m for rail access): the site
polygon is projected into its pixels, the window covering it is read, and every other band is
read over the same ground and put onto that grid. How a band gets there depends on what its file
offers:

- a COG overview whose pixel is the reference's (`cog.level_for_pixel_size`) is read instead of
  the full image, so a 10 m band at 20 m costs a quarter of the bytes;
- without one, a band whose pixel divides the reference's a whole number of times is read at
  full resolution and reduced with `cv2.resize(INTER_AREA)` (the block mean), unless it is
  categorical (`scl`), whose classes must stay classes;
- anything else, and whatever residual misalignment is left, goes through `cv2.warpAffine` with
  nearest neighbour; the 20 m SCL onto a 10 m grid is the usual case.

Sentinel-2's bands share one grid origin, so the usual outcome is a plain slice with no warp.
`SiteBands.levels_used` records which level each band came from (0 is the full image).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from vision import cog, geo

# The largest window the worker will read, per side, in reference pixels: 4096 px = 41 km at 10 m
# (larger than any anchorage) or 82 km at 20 m, and bounded so one request can't ask for a whole
# 110 km scene. The rail task has a smaller cap of its own, vision_worker_handler.RAIL_MAX_WINDOW_PX
# = 3,584 px (71.7 km at 20 m; the four city boxes of docs/enhancements/rail-access-monitor.md §2
# need at most 3,327 px with the pad, Sydney's), because its compute grows with the window and the
# worker has one vCPU (2048 MB). That compute (analyse_rail_access and annotate_rail in one process,
# peak RSS from getrusage), on ONE core of a 4-core Intel Xeon at 2.10 GHz (taskset to one CPU,
# cv2.setNumThreads(1), two runs each), for the worst contract-valid networks built, all under the
# graph caps, at 4096 px and at 3,584 px:
#   one way back and forth across the window in 40 rows, 2,000 stations on it, 3,900 spurs 8 px long
#   and a solid bar of 66 adjacent ways (the thinning's full 32 passes): 28.7-29.6 s and 1.41 GB at
#   4096 px, 19.3-19.9 s and 1.12 GB at 3,584 px;
#   1,194 anti-diagonals 3 px apart over one corner (1,044 at 3,584 px), a comb of 3,500 spurs with
#   2,000 stations on one junction tree, and the bar: 29.1-29.3 s and 1.43 GB, 18.3-18.5 s and 1.14 GB;
#   4,850 spurs 4 px apart off spines joined into one tree, 2,000 stations on their tips, and the bar:
#   28.3-28.5 s and 1.37 GB, 18.2-18.8 s and 1.11 GB;
#   the snaking way with a vertex every 4 px (50,158 points; 45,038), 4,300 spurs, 1,000 stations along
#   it and 1,000 on spur tips, 5,000 bus stops and the bar: 32.1-32.2 s and 1.40 GB, 19.7-20.7 s and 1.12 GB;
#   1,828 full-width ways 2 px apart (1,572 at 3,584 px), the most run pixels and the most memory:
#   22.4-23.6 s and 1.56 GB, 11.7-12.0 s and 1.24 GB.
# Taking the worker's arm64 core as 1.3 times slower, the worst is about 42 s of compute at 4096 px:
# with a 10-30 s read, 52-72 s, past the client's 58 s read timeout (rail_access.WORKER_READ_TIMEOUT)
# whenever the read takes over 16 s. At 3,584 px it is about 27 s, and the read covers 77% of the
# pixels: 35-50 s in all.
MAX_WINDOW_PX = 4096
# Extra pixels read around the polygon so the adaptive threshold's window has context at the edge.
PAD_PX = 16
# A band whose grid is the reference's to within this, in pixels over the whole window, is sliced
# rather than warped: the warp would be the identity, and nearest neighbour on a half-pixel shift
# is not.
SHIFT_TOLERANCE_PX = 1e-3
# Tolerance for a window edge that lands a hair off a whole pixel through floating point alone.
_EDGE_EPS = 1e-6
# What cv2.resize can reduce (its INTER_AREA takes these element types).
_AREA_DTYPES = {np.dtype("u1"), np.dtype("u2"), np.dtype("i2"), np.dtype("f4"), np.dtype("f8")}


class SiteOutsideScene(ValueError):
    pass


class WindowTooLarge(ValueError):
    pass


@dataclass
class SiteBands:
    bands: dict[str, np.ndarray]
    polygon_px: list[list[float]]
    transform: tuple[float, ...]
    epsg: int
    pixel_size_m: float
    window: tuple[int, int, int, int]
    requests: int = 0
    bytes_read: int = 0
    nodata: dict[str, float | None] = field(default_factory=dict)
    # Per band, the COG level it was read from: 0 the full image, n the n-th overview.
    levels_used: dict[str, int] = field(default_factory=dict)


class CountingFetch:
    """Wraps a fetch so the worker can report how much it read."""

    def __init__(self, fetch: cog.Fetch):
        self.fetch, self.requests, self.bytes = fetch, 0, 0

    def __call__(self, url: str, start: int, end: int) -> bytes:
        data = self.fetch(url, start, end)
        self.requests += 1
        self.bytes += len(data)
        return data


def read_site(
    assets: dict[str, str],
    polygon_lonlat: list[list[float]],
    fetch: cog.Fetch,
    reference: str = "nir",
    max_window_px: int | None = None,
    categorical: tuple[str, ...] = ("scl",),
) -> SiteBands:
    """Read every band in `assets` over the site, aligned to `assets[reference]`. The bands named
    in `categorical` hold classes and are never averaged."""
    max_window_px = max_window_px or MAX_WINDOW_PX
    counted = CountingFetch(fetch)
    ref_info, ref_src = cog.read_info(assets[reference], counted)
    if ref_info.epsg is None:
        raise cog.CogError("reference band has no EPSG code")
    polygon_px = geo.lonlat_polygon_to_pixels(polygon_lonlat, ref_info.epsg, ref_info.transform)
    window = geo.pixel_bounds(polygon_px, ref_info.width, ref_info.height, pad=PAD_PX)
    if window is None:
        raise SiteOutsideScene("the site does not overlap the scene")
    col0, row0, col1, row1 = window
    if col1 - col0 > max_window_px or row1 - row0 > max_window_px:
        raise WindowTooLarge(f"the site needs a {col1 - col0} x {row1 - row0} px window")

    ref, _ = cog.read_window(assets[reference], window, counted, (ref_info, ref_src))
    win_transform = cog.window_transform(ref_info, window)
    bands = {reference: ref}
    nodata = {reference: ref_info.nodata}
    levels_used = {reference: 0}
    for name, url in assets.items():
        if name == reference:
            continue
        bands[name], nodata[name], levels_used[name] = _read_aligned(
            url, counted, win_transform, ref.shape, categorical=name in categorical
        )

    shifted = [[x - col0, y - row0] for x, y in polygon_px]
    return SiteBands(
        bands=bands,
        polygon_px=shifted,
        transform=win_transform,
        epsg=ref_info.epsg,
        pixel_size_m=abs(ref_info.transform[1]),
        window=window,
        requests=counted.requests,
        bytes_read=counted.bytes,
        nodata=nodata,
        levels_used=levels_used,
    )


def _read_aligned(
    url: str, fetch: cog.Fetch, target_transform, shape: tuple[int, int], categorical: bool = False
) -> tuple[np.ndarray, float | None, int]:
    """Band `url` over the target window's ground, on its grid: (pixels, the band's nodata, the
    COG level read). From the overview at the target's pixel size when the file has one; else from
    the full image, reduced by area when its pixel is a whole fraction of the target's and the
    band is not categorical; the remainder is nearest neighbour."""
    info, src = cog.read_info(url, fetch)
    target_px = abs(target_transform[1])
    fill = info.nodata if info.nodata is not None else 0
    level = cog.level_for_pixel_size(info, target_px)
    if level is not None:
        data, transform = _read_covering(url, level, src, fetch, target_transform, shape)
        used = 0 if level is info else 1 + info.levels.index(level)
        return _onto_grid(data, transform, target_transform, shape, fill), info.nodata, used

    data, transform = _read_covering(url, info, src, fetch, target_transform, shape)
    ratio = target_px / info.pixel_size
    whole = ratio > 1 and abs(ratio - round(ratio)) < _EDGE_EPS
    if not categorical and whole and data.dtype in _AREA_DTYPES:
        r = round(ratio)
        # Whole blocks only, so INTER_AREA is an exact block mean (a partial block at the edge is dropped).
        h, w = (n - n % r if n >= r else n for n in data.shape)
        data = data[:h, :w]
        size = (max(1, w // r), max(1, h // r))
        data = cv2.resize(data, size, interpolation=cv2.INTER_AREA)
        c, a, _, f, _, e = transform
        transform = (c, a * w / size[0], 0.0, f, 0.0, e * h / size[1])
    return _onto_grid(data, transform, target_transform, shape, fill), info.nodata, 0


def _read_covering(url, info: cog.CogInfo, src, fetch, target_transform, shape) -> tuple[np.ndarray, tuple]:
    """The pixels of `info` (the full image or one overview) covering the target window's ground,
    widened to whole pixels and clipped to the image, with their own transform."""
    tc, ta, _, tf, _, te = target_transform
    sc, sa, _, sf, _, se = info.transform
    h, w = shape
    x0, y0 = (tc - sc) / sa, (tf - sf) / se
    x1, y1 = (tc + ta * w - sc) / sa, (tf + te * h - sf) / se
    window = (
        max(0, math.floor(min(x0, x1) + _EDGE_EPS)),
        max(0, math.floor(min(y0, y1) + _EDGE_EPS)),
        min(info.width, math.ceil(max(x0, x1) - _EDGE_EPS)),
        min(info.height, math.ceil(max(y0, y1) - _EDGE_EPS)),
    )
    if window[0] >= window[2] or window[1] >= window[3]:
        raise SiteOutsideScene("a band does not overlap the site")
    data, _ = cog.read_window(url, window, fetch, (info, src))
    return data, cog.window_transform(info, window)


def _onto_grid(data: np.ndarray, transform, target_transform, shape: tuple[int, int], fill) -> np.ndarray:
    """`data` (with `transform`) resampled onto the target grid with nearest neighbour; `data`
    itself when the two grids coincide to within SHIFT_TOLERANCE_PX over the whole window."""
    tc, ta, _, tf, _, te = target_transform
    sc, sa, _, sf, _, se = transform
    h, w = shape
    sx, sy = ta / sa, te / se
    # Target pixel centre (i + 0.5) -> map -> this band's pixel index, relative to the data read.
    dx = (tc - sc) / sa + 0.5 * sx - 0.5
    dy = (tf - sf) / se + 0.5 * sy - 0.5
    if (
        data.shape == (h, w)
        and abs(dx) <= SHIFT_TOLERANCE_PX
        and abs(dy) <= SHIFT_TOLERANCE_PX
        and abs(sx - 1.0) * w <= SHIFT_TOLERANCE_PX
        and abs(sy - 1.0) * h <= SHIFT_TOLERANCE_PX
    ):
        return data
    matrix = np.array([[sx, 0.0, dx], [0.0, sy, dy]], dtype=np.float64)
    work = data.astype(np.float32) if data.dtype.kind == "f" else data
    aligned = cv2.warpAffine(
        work,
        matrix,
        (w, h),
        flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=fill,
    )
    return aligned.astype(data.dtype)
