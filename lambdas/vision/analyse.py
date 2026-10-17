"""One site, one scene: masks, detection and the metrics the adapter diffs.

The result is plain JSON-able data. `coverage` is the share of the site the scene let us see (not
cloud, shadow or no data); the adapter treats a low-coverage count as never material, so missing
pixels can't pass for missing ships. Nothing here judges whether a change matters; that is the
adapter's diff and then the triage agent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from vision import masks
from vision.detect import DetectParams, detect_objects

# Upper edges, in metres, of the size histogram's bins; the last bin is everything longer.
SIZE_BINS_M = (150.0, 250.0, 400.0)


@dataclass
class SiteAnalysis:
    """`metrics` is what the worker returns (JSON-able); `measurable` is the mask the detector
    used, kept for drawing the figure and never sent."""

    metrics: dict
    measurable: np.ndarray


def size_histogram(lengths_m: Sequence[float], bins: Sequence[float] = SIZE_BINS_M) -> dict[str, int]:
    """Counts per length band, keyed like "<150", "150-250", "250-400", ">=400"."""
    edges = list(bins)
    labels = [f"<{edges[0]:g}"]
    labels += [f"{lo:g}-{hi:g}" for lo, hi in zip(edges, edges[1:], strict=False)]
    labels.append(f">={edges[-1]:g}")
    counts = dict.fromkeys(labels, 0)
    for length in lengths_m:
        index = int(np.searchsorted(edges, length, side="right"))
        counts[labels[index]] += 1
    return counts


def pixel_to_map(transform: Sequence[float], x: float, y: float) -> tuple[float, float]:
    """Pixel (x, y) to map coordinates with a GDAL-style affine transform (c, a, b, f, d, e):
    X = c + a*x + b*y, Y = f + d*x + e*y. Pixel coordinates here are of pixel centres."""
    c, a, b, f, d, e = transform
    return c + a * (x + 0.5) + b * (y + 0.5), f + d * (x + 0.5) + e * (y + 0.5)


def analyse_site(
    nir: np.ndarray,
    green: np.ndarray,
    polygons_px: Sequence[Sequence[Sequence[float]]],
    pixel_size_m: float,
    scl: np.ndarray | None = None,
    params: DetectParams | None = None,
    transform: Sequence[float] | None = None,
    max_object_px: int | None = None,
    coverage_floor: float = 0.7,
) -> SiteAnalysis:
    """Measure one site. `nir` and `green` are same-shaped reflectance arrays; `scl` (optional) is
    the scene classification for cloud and no data; `polygons_px` outlines the site in pixels.

    The metrics are `count`, `coverage`, `clear_water_km2`, `density_per_km2`, `size_histogram`,
    `detections` (with map coordinates when `transform` is given), `quality_flags` and the
    `params` used, so a result can always be reproduced.
    """
    if nir.shape != green.shape or (scl is not None and scl.shape != nir.shape):
        raise ValueError("bands must share one shape")
    params = params or DetectParams()
    if max_object_px is None:
        # The largest object worth keeping, as pixels: a max-length ship a fifth as wide.
        longest = params.max_length_m / pixel_size_m
        max_object_px = int(longest * max(longest / 5.0, 1.0))
    site = masks.polygon_mask(nir.shape, polygons_px)
    unusable = masks.unusable_mask(scl) if scl is not None else None
    water = masks.water_mask(green, nir, max_object_px=max_object_px)
    measurable = masks.measurable(site, water, unusable)

    site_px = int(np.count_nonzero(site))
    visible_px = site_px if unusable is None else int(np.count_nonzero(site & ~unusable))
    measurable_px = int(np.count_nonzero(measurable))
    coverage = visible_px / site_px if site_px else 0.0
    pixel_km2 = (pixel_size_m / 1000.0) ** 2
    clear_water_km2 = measurable_px * pixel_km2

    rejected: dict = {}
    detections = detect_objects(nir, measurable, pixel_size_m, params, stats=rejected)
    if transform is not None:
        for det in detections:
            det["map_x"], det["map_y"] = (round(v, 2) for v in pixel_to_map(transform, det["x"], det["y"]))

    flags = []
    if site_px == 0:
        flags.append("empty_site")
    if coverage < coverage_floor:
        flags.append("low_coverage")
    if site_px and measurable_px == 0:
        flags.append("no_clear_water")

    metrics = {
        "count": len(detections),
        "coverage": round(coverage, 4),
        "clear_water_km2": round(clear_water_km2, 4),
        "density_per_km2": round(len(detections) / clear_water_km2, 4) if clear_water_km2 else None,
        "size_histogram": size_histogram([d["length_m"] for d in detections]),
        "detections": detections,
        "rejected": rejected,
        "quality_flags": flags,
        "params": {
            "pixel_size_m": pixel_size_m,
            "stretch_max": params.stretch_max,
            "block_size": params.block_size,
            "offset": params.offset,
            "min_length_m": params.min_length_m,
            "max_length_m": params.max_length_m,
            "min_elongation": params.min_elongation,
            "edge_buffer_px": params.edge_buffer_px,
            "max_object_px": max_object_px,
            "coverage_floor": coverage_floor,
        },
    }
    return SiteAnalysis(metrics=metrics, measurable=measurable)
