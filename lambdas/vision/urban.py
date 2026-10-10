"""Where a city is built up, from Sentinel-2 reflectance: the "heat" of the rail access task.

Built-up ground is bright in short-wave infrared against near-infrared (NDBI above a threshold)
and not vegetated (NDVI below a cap); water is the scene classification's class 6 and anything
cloud, shadow or no-data is unusable, as in masks.py. The heat map is that mask blurred with a
Gaussian whose sigma is a walking distance: the value at a pixel is the local share of built-up
ground. It is deliberately *not* rescaled by a percentile, so two scenes of one city compare
directly and an adapter can diff them. It is a density proxy from imagery, not population.
"""

from __future__ import annotations

import cv2
import numpy as np

from vision import masks

# Sentinel-2 L2A scene classification: open water.
SCL_WATER = 6
# A 3x3 opening removes the single-pixel speckle an index threshold leaves on roofs' edges.
_OPEN_KERNEL = np.ones((3, 3), np.uint8)
# A blur wider than this runs on a grid reduced by REDUCTION and is resized back: a 150 px sigma
# (3 km at 20 m) on a 3000 x 3000 image is a 900-tap kernel and about 15 s, and nothing a 3 km
# blur shows needs 20 m detail; on the reduced grid it is under a second.
LARGE_SIGMA_PX = 40.0
REDUCTION = 4


def ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Normalised difference vegetation index, (NIR - R) / (NIR + R), float32; 0 where both are 0."""
    return _normalised_difference(nir, red)


def ndbi(swir: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Normalised difference built-up index, (SWIR - NIR) / (SWIR + NIR), float32; 0 where both
    are 0."""
    return _normalised_difference(swir, nir)


def _normalised_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    total = a + b
    out = np.zeros_like(total)
    np.divide(a - b, total, out=out, where=total > 0)
    return out


def built_up_mask(
    red: np.ndarray,
    nir: np.ndarray,
    swir: np.ndarray,
    scl: np.ndarray,
    ndbi_threshold: float = 0.0,
    ndvi_max: float = 0.3,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(built, unusable, water), each uint8 0/255 of the bands' shape.

    `built` is NDBI above `ndbi_threshold` and NDVI below `ndvi_max`, not water and not
    unusable, then opened 3x3. `unusable` is masks.unusable_mask(scl); `water` is SCL water.
    """
    if not red.shape == nir.shape == swir.shape == scl.shape:
        raise ValueError("bands must share one shape")
    unusable = masks.unusable_mask(scl)
    water = np.where(scl == SCL_WATER, 255, 0).astype(np.uint8)
    candidate = (ndbi(swir, nir) > ndbi_threshold) & (ndvi(red, nir) < ndvi_max)
    built = np.where(candidate & (water == 0) & (unusable == 0), 255, 0).astype(np.uint8)
    built = cv2.morphologyEx(built, cv2.MORPH_OPEN, _OPEN_KERNEL)
    return built, unusable, water


def heat_map(built: np.ndarray, sigma_px: float, usable: np.ndarray | None = None) -> np.ndarray:
    """The local share of built-up ground, float32 in [0, 1]: the 0/255 `built` mask as 0/1,
    blurred with a Gaussian of `sigma_px` (the border replicated, so the edge of the window is
    not read as empty), and 0 wherever `usable` (uint8 mask) is 0."""
    if sigma_px <= 0:
        raise ValueError("sigma_px must be positive")
    share = (built > 0).astype(np.float32)
    if sigma_px > LARGE_SIGMA_PX:
        h, w = share.shape
        small = cv2.resize(share, (-(-w // REDUCTION), -(-h // REDUCTION)), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (0, 0), sigma_px / REDUCTION, borderType=cv2.BORDER_REPLICATE)
        heat = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    else:
        heat = cv2.GaussianBlur(share, (0, 0), sigma_px, borderType=cv2.BORDER_REPLICATE)
    heat = np.clip(heat, 0.0, 1.0)
    if usable is not None:
        heat[usable == 0] = 0.0
    return heat
