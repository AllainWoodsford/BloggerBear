"""Which pixels of a scene can be measured.

Every mask is a uint8 array of 0 and 255, the shape OpenCV's own mask arguments take. A pixel is
measured only if it is inside the site polygon, has data, is not cloud or cloud shadow, and is
water. What is left out is reported as lost `coverage`, never silently treated as empty water: a
half-cloudy scene must not look like a vanished queue.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import cv2
import numpy as np

# Sentinel-2 L2A scene classification (SCL) values that hide the surface: 0 no data, 1 saturated or
# defective, 3 cloud shadow, 8 cloud (medium probability), 9 cloud (high probability), 10 thin
# cirrus. Water is 6, but it is not trusted on its own: SCL marks bright ships as "not water".
SCL_UNUSABLE = (0, 1, 3, 8, 9, 10)


def polygon_mask(shape: tuple[int, int], polygons_px: Iterable[Sequence[Sequence[float]]]) -> np.ndarray:
    """255 inside any of `polygons_px` (each a list of (x, y) pixel vertices), 0 outside."""
    mask = np.zeros(shape, dtype=np.uint8)
    pts = [np.round(np.asarray(poly, dtype=np.float64)).astype(np.int32) for poly in polygons_px]
    pts = [p for p in pts if len(p) >= 3]
    if pts:
        cv2.fillPoly(mask, pts, 255)
    return mask


def unusable_mask(scl: np.ndarray, classes: Iterable[int] = SCL_UNUSABLE) -> np.ndarray:
    """255 where the scene classification says the surface can't be seen (cloud, shadow, no data)."""
    return np.where(np.isin(scl, list(classes)), 255, 0).astype(np.uint8)


def ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Normalised difference water index, (G - NIR) / (G + NIR), as float32; 0 where both are 0."""
    g = green.astype(np.float32)
    n = nir.astype(np.float32)
    total = g + n
    out = np.zeros_like(total)
    np.divide(g - n, total, out=out, where=total > 0)
    return out


def water_mask(
    green: np.ndarray,
    nir: np.ndarray,
    threshold: float = 0.0,
    max_object_px: int = 400,
) -> np.ndarray:
    """255 for open water, *including* the objects floating on it.

    NDWI above `threshold` is water. A ship is bright in NIR, so its own pixels read as "not water"
    and would cut holes in the mask exactly where the objects to be counted are. Any non-water
    patch of at most `max_object_px` pixels is therefore taken back into the water: land and
    coastline are far larger than that, ships are not. A 3x3 opening first removes single-pixel
    noise along the shore.
    """
    water = np.where(ndwi(green, nir) > threshold, 255, 0).astype(np.uint8)
    water = cv2.morphologyEx(water, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    not_water = cv2.bitwise_not(water)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(not_water, connectivity=8)
    small = np.zeros(count, dtype=bool)
    small[1:] = stats[1:, cv2.CC_STAT_AREA] <= max_object_px
    # A small patch touching the image edge may be the tip of a headland, not a ship: keep it out.
    h, w = water.shape
    for label in np.flatnonzero(small):
        x, y, bw, bh = stats[label, :4]
        if x == 0 or y == 0 or x + bw >= w or y + bh >= h:
            small[label] = False
    water[small[labels]] = 255
    return water


def measurable(site: np.ndarray, water: np.ndarray, unusable: np.ndarray | None) -> np.ndarray:
    """The pixels the detector may look at: in the site, water, and not hidden."""
    out = cv2.bitwise_and(site, water)
    if unusable is not None:
        out = cv2.bitwise_and(out, cv2.bitwise_not(unusable))
    return out
