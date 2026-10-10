"""The figure for an article: what the detector saw, drawn on the scene.

A contrast-stretched NIR crop, the parts of the site that could not be measured shaded, and each
detection's rotated box. A "Processed imagery" caption is burned into the image itself, so the
figure can't pass as raw satellite imagery wherever it ends up (the risks doc's item 1); the full
attribution sentence is the page's job, not the image's. `burn_caption` and `encode_png` are
shared with the rail access figure (rail_annotate.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np

from vision.detect import stretch

BOX_COLOUR = (0, 215, 255)  # BGR amber
UNMEASURED_TINT = (90, 60, 40)  # BGR slate, blended over what wasn't measured
DEFAULT_CAPTION = "Processed imagery - BloggerBear"


def annotate(
    nir: np.ndarray,
    measurable: np.ndarray,
    detections: Sequence[dict],
    stretch_max: float = 3000.0,
    max_side: int = 1024,
    caption: str = DEFAULT_CAPTION,
) -> bytes:
    """PNG bytes of the annotated scene, scaled down so its longer side is at most `max_side`."""
    grey = stretch(nir, stretch_max)
    image = cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)
    unmeasured = measurable == 0
    if unmeasured.any():
        tint = np.empty_like(image)
        tint[:] = UNMEASURED_TINT
        blended = cv2.addWeighted(image, 0.4, tint, 0.6, 0)
        image[unmeasured] = blended[unmeasured]

    h, w = image.shape[:2]
    scale = min(1.0, max_side / float(max(h, w)))
    if scale < 1.0:
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)

    thickness = 1 if max(image.shape[:2]) < 400 else 2
    for det in detections:
        box = np.round(np.asarray(det["box"], dtype=np.float64) * scale).astype(np.int32)
        cv2.drawContours(image, [box], 0, BOX_COLOUR, thickness)

    burn_caption(image, [caption])
    return encode_png(image)


def burn_caption(image: np.ndarray, lines: Sequence[str]) -> np.ndarray:
    """Burn `lines` of white text on a black bar into the top-left corner of `image` (BGR, in
    place), one under the other; empty lines are skipped. The font scales with the image width
    so the words stay legible after the figure is scaled down."""
    lines = [line for line in lines if line]
    if not lines:
        return image
    font, font_scale = cv2.FONT_HERSHEY_SIMPLEX, max(0.35, image.shape[1] / 1600.0)
    sizes = [cv2.getTextSize(line, font, font_scale, 1) for line in lines]
    width = max(tw for (tw, _), _ in sizes) + 8
    height = sum(th + base + 4 for (_, th), base in sizes) + 4
    cv2.rectangle(image, (0, 0), (width, height), (0, 0, 0), thickness=-1)
    y = 4
    for line, ((_, th), base) in zip(lines, sizes, strict=True):
        cv2.putText(image, line, (4, y + th), font, font_scale, (255, 255, 255), 1, cv2.LINE_AA)
        y += th + base + 4
    return image


def encode_png(image: np.ndarray) -> bytes:
    ok, png = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return png.tobytes()
