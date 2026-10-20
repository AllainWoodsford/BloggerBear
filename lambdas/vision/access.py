"""How far the built-up city is from its stations and its stops: reach, deserts, catchments,
feeder coverage and intermodal points.

Everything here is a distance transform away from a raster of points. `cv2.distanceTransform
WithLabels` gives every pixel its straight-line distance to the nearest station *and* which
station that is (the Voronoi cell, as a connected-component label), so served area, transit
deserts and each station's catchment come out of one pass. The stops (bus, tram and ferry) get
the plain transform without labels (`distance_to_points`): which stop is nearest is never read,
and the labelled transform costs the worker's worst case more time and half as much memory
again (the figures are at `distance_to_points`). Distances are straight-line pixels, not
walking routes; the metrics say so.
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


def distance_to_points(point_mask: np.ndarray) -> np.ndarray:
    """Each pixel's straight-line distance to the nearest set pixel of `point_mask` (float32),
    without the labels `distance_to_stations` keeps: nothing reads which stop is nearest, and on
    the worker's worst case (a 3,584 px window) the labelled transform ran the whole pass at 58 s
    and 2 GB against 41-56 s and 1.32 GB without. Infinite everywhere without any point."""
    if not point_mask.any():
        return np.full(point_mask.shape, np.inf, np.float32)
    not_point = np.where(point_mask > 0, 0, 1).astype(np.uint8)
    return cv2.distanceTransform(not_point, cv2.DIST_L2, 5)


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


def desert_clusters(
    desert: np.ndarray, min_px: float, top: int = 10, within: np.ndarray | None = None
) -> list[dict]:
    """The largest connected patches of `desert` of at least `min_px` pixels, area descending:
    `x`, `y` (centroid pixel), `area_px` and `bbox` [x, y, w, h]. With a `within` mask, each
    patch also says its `within_share`: the share of its pixels where the mask is set (the
    stop reach, so a desert says how much of it a bus, tram or ferry stop covers)."""
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(desert, connectivity=8)
    covered = None if within is None else np.bincount(labels[within > 0].ravel(), minlength=count)
    found = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < min_px:
            continue
        x, y, w, h = (int(v) for v in stats[label, :4])
        patch = {
            "x": round(float(centroids[label, 0]), 2),
            "y": round(float(centroids[label, 1]), 2),
            "area_px": area,
            "bbox": [x, y, w, h],
        }
        if covered is not None:
            patch["within_share"] = float(covered[label] / area)
        found.append(patch)
    found.sort(key=lambda d: (-d["area_px"], d["y"], d["x"]))
    return found[:top]


def catchment_activity(
    heat: np.ndarray,
    labels: np.ndarray,
    dist_px: np.ndarray,
    reach_px: float,
    station_labels: Sequence[int],
    usable: np.ndarray | None = None,
) -> tuple[list[float | None], list[float]]:
    """(activity, catchment_coverage) per station. A station's catchment is its Voronoi cell
    clipped to `reach_px`; its activity is the mean heat over the catchment's usable pixels
    (every pixel when `usable` is None) and its coverage the usable share of the catchment.
    A catchment with no usable pixel, or no pixel at all (a station outside the image), has
    coverage 0 and activity None: unmeasured, which is not the same as quiet."""
    within = dist_px <= reach_px
    measured = within if usable is None else within & (usable > 0)
    size = int(labels.max()) + 1 if labels.size else 1
    cell_px = np.bincount(labels[within].ravel(), minlength=size)
    used_px = np.bincount(labels[measured].ravel(), minlength=size)
    total = np.bincount(labels[measured].ravel(), weights=heat[measured].ravel(), minlength=size)
    activity: list[float | None] = []
    coverage: list[float] = []
    for label in station_labels:
        if label <= 0 or label >= size or used_px[label] == 0:
            activity.append(None)
            coverage.append(0.0)
        else:
            activity.append(float(total[label] / used_px[label]))
            coverage.append(float(used_px[label] / cell_px[label]))
    return activity, coverage


def feeder_coverage(
    built: np.ndarray,
    usable: np.ndarray,
    labels: np.ndarray,
    dist_px: np.ndarray,
    stop_reach: np.ndarray | None,
    reach_px: float,
    feeder_px: float,
    station_labels: Sequence[int],
) -> list[float | None]:
    """Per station, how much of its hinterland the stops cover: over the station's Voronoi cell
    (`labels`, as `catchment_activity` reads them), the built-up usable pixels beyond its own
    reach but within the feeder reach (`reach_px` < distance <= `feeder_px`), and the share of
    them inside the `stop_reach` mask. None when the station has no such pixel (nothing built
    beyond its reach in its cell, or a station outside the image) and for every station when
    there is no stops layer (`stop_reach` None): not measured, which is not the same as none."""
    if stop_reach is None:
        return [None] * len(station_labels)
    hinterland = (built > 0) & (usable > 0) & (dist_px > reach_px) & (dist_px <= feeder_px)
    size = int(labels.max()) + 1 if labels.size else 1
    total = np.bincount(labels[hinterland].ravel(), minlength=size)
    covered = np.bincount(labels[hinterland & (stop_reach > 0)].ravel(), minlength=size)
    coverage: list[float | None] = []
    for label in station_labels:
        if label <= 0 or label >= size or total[label] == 0:
            coverage.append(None)
        else:
            coverage.append(float(covered[label] / total[label]))
    return coverage


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


def stop_stats(
    built: np.ndarray, dist_px: np.ndarray, reach_px: float, stop_reach: np.ndarray, pixel_km2: float
) -> dict:
    """The built-up ground against the stops as well as the stations: `stop_served_km2` and
    `stop_served_share` (built up and inside the `stop_reach` mask), `any_mode_served_km2`,
    `any_mode_served_share` and `any_mode_desert_km2` (within `reach_px` of a station or within
    stop reach; beyond both), `stops_only_km2` (beyond station reach, within stop reach) and
    `stop_overlap_share` (the part of the stop-served ground that is also within station reach;
    None when nothing is stop-served). Shares are of the built-up area, 0 when nothing is built."""
    is_built = built > 0
    by_station = is_built & (dist_px <= reach_px)
    by_stop = is_built & (stop_reach > 0)
    built_px = int(np.count_nonzero(is_built))
    station_px = int(np.count_nonzero(by_station))
    stop_px = int(np.count_nonzero(by_stop))
    any_px = int(np.count_nonzero(by_station | by_stop))
    both_px = int(np.count_nonzero(by_station & by_stop))
    return {
        "stop_served_km2": stop_px * pixel_km2,
        "stop_served_share": stop_px / built_px if built_px else 0.0,
        "any_mode_served_km2": any_px * pixel_km2,
        "any_mode_served_share": any_px / built_px if built_px else 0.0,
        "any_mode_desert_km2": (built_px - any_px) * pixel_km2,
        "stops_only_km2": (any_px - station_px) * pixel_km2,
        "stop_overlap_share": both_px / stop_px if stop_px else None,
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
