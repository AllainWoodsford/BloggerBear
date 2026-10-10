"""Finding bright, compact, elongated objects on water: ships in Sentinel-2's NIR band.

Ships are bright in near-infrared against dark water. One global cut-off would move with haze and
sun glint, so the threshold is local: `cv2.adaptiveThreshold` with a Gaussian window marks pixels
brighter than their neighbourhood by `offset`. Each connected blob is then measured with
`cv2.minAreaRect` and kept only if its length and elongation fit a vessel. Adaptive Gaussian
thresholding and contour finding are two of the operations COOL accelerates, which is part of why
they are used here rather than a hand-rolled numpy equivalent.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class DetectParams:
    """What counts as an object. Lengths are in metres; the pixel size converts them."""

    # Reflectance mapped to 255 when stretching to 8 bits. Sentinel-2 L2A stores reflectance x 10000:
    # open water is under ~500 in NIR, a ship's deck well over 1500.
    stretch_max: float = 3000.0
    # Odd window size, in pixels, of the local Gaussian mean (31 px = 310 m at 10 m).
    block_size: int = 31
    # How much brighter than its neighbourhood (on the 0-255 scale) a pixel must be.
    offset: float = 25.0
    min_length_m: float = 60.0
    max_length_m: float = 600.0
    # length / width; a ship is several times longer than it is wide, a buoy or a cloud speck is not.
    min_elongation: float = 1.6

    def __post_init__(self):
        if self.block_size < 3 or self.block_size % 2 == 0:
            raise ValueError("block_size must be an odd number of at least 3")
        if self.stretch_max <= 0:
            raise ValueError("stretch_max must be positive")
        if not 0 < self.min_length_m <= self.max_length_m:
            raise ValueError("need 0 < min_length_m <= max_length_m")


def stretch(band: np.ndarray, stretch_max: float) -> np.ndarray:
    """Linear stretch of a reflectance band to uint8: 0 -> 0, `stretch_max` and above -> 255."""
    scaled = band.astype(np.float32) * (255.0 / float(stretch_max))
    return np.clip(scaled, 0, 255).astype(np.uint8)


def detect_objects(
    nir: np.ndarray,
    measurable: np.ndarray,
    pixel_size_m: float,
    params: DetectParams | None = None,
) -> list[dict]:
    """Every object in `nir` within the `measurable` mask that passes the size and shape filters.

    Returns one dict per object: centroid `x`, `y` (pixels), `length_m`, `width_m`, `angle`
    (degrees, OpenCV's minAreaRect convention), `area_px`, and `box` (the rotated rectangle's four
    corners, for drawing).
    """
    params = params or DetectParams()
    if pixel_size_m <= 0:
        raise ValueError("pixel_size_m must be positive")
    image = stretch(nir, params.stretch_max)
    inside = measurable > 0
    if not inside.any():
        return []
    # Land and cloud are bright too. Left in, they would pull up the local mean along every shore
    # and cloud edge; painted over with typical water they can't.
    background = np.uint8(np.median(image[inside]))
    image = np.where(inside, image, background).astype(np.uint8)
    # C is subtracted from the local mean, so a negative C demands pixels *brighter* than it.
    binary = cv2.adaptiveThreshold(
        image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, params.block_size, -params.offset
    )
    binary = cv2.bitwise_and(binary, measurable)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)

    found = []
    for contour in contours:
        (cx, cy), (w, h), angle = cv2.minAreaRect(contour)
        # minAreaRect measures between pixel centres; an object n pixels long spans n - 1 of them.
        length_px, width_px = max(w, h) + 1.0, min(w, h) + 1.0
        length_m, width_m = length_px * pixel_size_m, width_px * pixel_size_m
        if not params.min_length_m <= length_m <= params.max_length_m:
            continue
        if length_px / width_px < params.min_elongation:
            continue
        box = cv2.boxPoints(((cx, cy), (w, h), angle))
        found.append(
            {
                "x": round(float(cx), 2),
                "y": round(float(cy), 2),
                "length_m": round(length_m, 1),
                "width_m": round(width_m, 1),
                "angle": round(float(angle), 1),
                "area_px": int(cv2.contourArea(contour)) or len(contour),
                "box": [[round(float(px), 2), round(float(py), 2)] for px, py in box],
            }
        )
    found.sort(key=lambda d: (d["y"], d["x"]))
    return found
