"""A site's bands out of a scene, on one pixel grid.

The NIR band is the reference: the site polygon is projected into its pixels, the window covering
it is read, and every other band is read over the same ground and resampled onto that window with
`cv2.warpAffine` (nearest neighbour, so the 20 m SCL classes stay classes). Sentinel-2's 10 m bands
share a grid already; the warp then is the identity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from vision import cog, geo

# The largest window the worker will read, per side, in reference pixels: 4096 px = 41 km at 10 m,
# larger than any anchorage, and bounded so one request can't ask for a whole 110 km scene.
MAX_WINDOW_PX = 4096
# Extra pixels read around the polygon so the adaptive threshold's window has context at the edge.
PAD_PX = 16


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
) -> SiteBands:
    """Read every band in `assets` over the site, aligned to `assets[reference]`."""
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
    for name, url in assets.items():
        if name == reference:
            continue
        bands[name], nodata[name] = _read_aligned(url, counted, win_transform, ref.shape)

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
    )


def _read_aligned(url, fetch, target_transform, shape) -> tuple[np.ndarray, float | None]:
    """Band `url` over the target window's ground, resampled onto its grid (nearest neighbour)."""
    info, src = cog.read_info(url, fetch)
    tc, ta, _, tf, _, te = target_transform
    sc, sa, _, sf, _, se = info.transform
    h, w = shape
    # The target window's corners in this band's pixels, widened to whole pixels and clipped.
    x0, y0 = (tc - sc) / sa, (tf - sf) / se
    x1, y1 = (tc + ta * w - sc) / sa, (tf + te * h - sf) / se
    window = (
        max(0, math.floor(min(x0, x1))),
        max(0, math.floor(min(y0, y1))),
        min(info.width, math.ceil(max(x0, x1))),
        min(info.height, math.ceil(max(y0, y1))),
    )
    if window[0] >= window[2] or window[1] >= window[3]:
        raise SiteOutsideScene("a band does not overlap the site")
    data, _ = cog.read_window(url, window, fetch, (info, src))
    # Target pixel centre (i + 0.5) -> map -> this band's pixel index, relative to the read window.
    sx, sy = ta / sa, te / se
    matrix = np.array(
        [
            [sx, 0.0, (tc - sc) / sa - window[0] + 0.5 * sx - 0.5],
            [0.0, sy, (tf - sf) / se - window[1] + 0.5 * sy - 0.5],
        ],
        dtype=np.float64,
    )
    fill = info.nodata if info.nodata is not None else 0
    work = data.astype(np.float32) if data.dtype.kind == "f" else data
    aligned = cv2.warpAffine(
        work,
        matrix,
        (w, h),
        flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=fill,
    )
    return aligned.astype(data.dtype), info.nodata
