"""How far the built-up city is from its stations: reach, deserts, catchments and intermodal
points.

Everything here is a distance transform away from the station raster. `cv2.distanceTransform
WithLabels` gives every pixel its straight-line distance to the nearest station *and* which
station that is (the Voronoi cell, as a connected-component label), so served area, transit
deserts and each station's catchment come out of one pass. Distances are straight-line pixels,
not walking routes; the metrics say so.
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np

# Stations compared with points of interest this many at a time: 2000 stations by 5000 points
# in one float64 array would be 80 MB; a chunk of 200 is 8 MB.
_CHUNK = 200
# What `intermodal_counts` tallies: the POI kind, the radius it is counted within, and the key.
INTERMODAL_KINDS = (
    ("bus_stop", "near", "bus_stop_near"),
    ("bus_station", "near", "bus_station_near"),
    ("ferry_terminal", "far", "ferry_terminal_far"),
    ("park_ride", "far", "park_ride_far"),
)


def station_raster(shape: tuple[int, int], stations_px: Sequence[Sequence[float] | None]) -> np.ndarray:
    """255 at the pixel of every station inside the image, 0 elsewhere (uint8)."""
    mask = np.zeros(shape, np.uint8)
    h, w = shape
    for station in stations_px:
        if station is None:
            continue
        x, y = int(round(station[0])), int(round(station[1]))
        if 0 <= x < w and 0 <= y < h:
            mask[y, x] = 255
    return mask


def distance_to_stations(station_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(dist_px, labels): each pixel's distance to the nearest station pixel (float32) and the
    1-based label of that station's connected component (int32), its Voronoi cell. Without any
    station every distance is infinite and every label 0."""
    if not station_mask.any():
        return np.full(station_mask.shape, np.inf, np.float32), np.zeros(station_mask.shape, np.int32)
    not_station = np.where(station_mask > 0, 0, 1).astype(np.uint8)
    dist, labels = cv2.distanceTransformWithLabels(
        not_station, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_CCOMP
    )
    return dist.astype(np.float32), labels.astype(np.int32)


def label_of_station(labels: np.ndarray, stations_px: Sequence[Sequence[float] | None]) -> list[int]:
    """The Voronoi label at each station's pixel; 0 for a station outside the image."""
    h, w = labels.shape
    out = []
    for station in stations_px:
        if station is None:
            out.append(0)
            continue
        x, y = int(round(station[0])), int(round(station[1]))
        out.append(int(labels[y, x]) if 0 <= x < w and 0 <= y < h else 0)
    return out


def desert_mask(built: np.ndarray, dist_px: np.ndarray, reach_px: float) -> np.ndarray:
    """255 where the ground is built up and farther than `reach_px` from any station."""
    return np.where((built > 0) & (dist_px > reach_px), 255, 0).astype(np.uint8)


def desert_clusters(desert: np.ndarray, min_px: float, top: int = 10) -> list[dict]:
    """The largest connected patches of `desert` of at least `min_px` pixels, area descending:
    `x`, `y` (centroid pixel), `area_px` and `bbox` [x, y, w, h]."""
    count, _, stats, centroids = cv2.connectedComponentsWithStats(desert, connectivity=8)
    found = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < min_px:
            continue
        x, y, w, h = (int(v) for v in stats[label, :4])
        found.append(
            {
                "x": round(float(centroids[label, 0]), 2),
                "y": round(float(centroids[label, 1]), 2),
                "area_px": area,
                "bbox": [x, y, w, h],
            }
        )
    found.sort(key=lambda d: (-d["area_px"], d["y"], d["x"]))
    return found[:top]


def catchment_activity(
    heat: np.ndarray,
    labels: np.ndarray,
    dist_px: np.ndarray,
    reach_px: float,
    station_labels: Sequence[int],
) -> list[float]:
    """The mean heat of each station's catchment: its Voronoi cell, clipped to `reach_px`.
    A station with no cell (outside the image, or sharing a pixel with none) reads 0."""
    within = dist_px <= reach_px
    cells = labels[within].ravel()
    size = int(labels.max()) + 1 if labels.size else 1
    total = np.bincount(cells, weights=heat[within].ravel(), minlength=size)
    count = np.bincount(cells, minlength=size)
    out = []
    for label in station_labels:
        if label <= 0 or label >= size or count[label] == 0:
            out.append(0.0)
        else:
            out.append(float(total[label] / count[label]))
    return out


def served_stats(built: np.ndarray, dist_px: np.ndarray, reach_px: float, pixel_km2: float) -> dict:
    """`built_up_km2`, `served_km2` (built up and within reach), `desert_km2` and `served_share`
    (served / built up, 0 when nothing is built up)."""
    is_built = built > 0
    built_px = int(np.count_nonzero(is_built))
    served_px = int(np.count_nonzero(is_built & (dist_px <= reach_px)))
    return {
        "built_up_km2": built_px * pixel_km2,
        "served_km2": served_px * pixel_km2,
        "desert_km2": (built_px - served_px) * pixel_km2,
        "served_share": served_px / built_px if built_px else 0.0,
    }


def intermodal_counts(
    stations_xy_m: np.ndarray,
    pois_xy_m: np.ndarray,
    poi_kinds: Sequence[str],
    near_m: float,
    far_m: float,
) -> list[dict]:
    """Per station, how many points of interest of each kind lie within its radius: bus stops
    and bus stations within `near_m`, ferry terminals and park-and-rides within `far_m`.
    Coordinates are UTM metres, (n, 2) arrays."""
    stations = np.asarray(stations_xy_m, dtype=np.float64).reshape(-1, 2)
    pois = np.asarray(pois_xy_m, dtype=np.float64).reshape(-1, 2)
    kinds = np.asarray(list(poi_kinds), dtype=object)
    if len(kinds) != len(pois):
        raise ValueError("one kind per point of interest")
    radius = {"near": float(near_m), "far": float(far_m)}
    selections = [(kinds == kind, radius[which], key) for kind, which, key in INTERMODAL_KINDS]
    counts = np.zeros((len(stations), len(selections)), dtype=np.int64)
    for start in range(0, len(stations), _CHUNK):
        chunk = stations[start : start + _CHUNK]
        if not len(pois):
            break
        d2 = ((chunk[:, None, :] - pois[None, :, :]) ** 2).sum(axis=2)
        for column, (chosen, r, _) in enumerate(selections):
            counts[start : start + len(chunk), column] = ((d2 <= r * r) & chosen[None, :]).sum(axis=1)
    return [
        {key: int(row[column]) for column, (_, _, key) in enumerate(selections)} for row in counts
    ]
