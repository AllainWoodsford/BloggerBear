"""The mapped rail network as a graph the imagery can score: rasterise, thin, vectorise.

OpenStreetMap's rail ways are drawn into the scene's pixels, thinned to a one-pixel skeleton,
and read back as a graph: junctions and line ends (and the pixels stations snap to) are the
nodes, the skeleton runs between them are the edges, each with its length and the share of its
pixels the imagery shows as a bare corridor. Because the pixel-to-graph step takes any mask, a
learned segmentation of the tracks could replace the rasterised ways later and nothing after it
would change.

Thinning is sequential hit-or-miss thinning with the Golay L elements (Serra's), done with core
OpenCV's `MORPH_HITMISS`: the headless wheel has no `ximgproc`. Only this module imports
networkx, and only the worker installs it.
"""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import networkx as nx
import numpy as np

# Golay's L thinning elements, in OpenCV's hit-or-miss convention (1 must be foreground, -1 must
# be background, 0 is ignored): the "straight" L, whose hit is a pixel on the top edge of a
# run, and the "diagonal" L, a pixel on a 45-degree edge. Their four rotations, applied in
# this alternating order with each hit removed before the next element is tried, peel one layer
# from every side in turn, which is what keeps the skeleton connected.
_L_STRAIGHT = np.array([[-1, -1, -1], [0, 1, 0], [1, 1, 1]], np.int8)
_L_DIAGONAL = np.array([[0, -1, -1], [1, 1, -1], [0, 1, 0]], np.int8)
THINNING_KERNELS = tuple(
    np.ascontiguousarray(np.rot90(kernel, turn)) for turn in range(4) for kernel in (_L_STRAIGHT, _L_DIAGONAL)
)
# The 3x3 ring: a pixel's eight neighbours.
_RING = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.float32)
_SQUARE = np.ones((3, 3), np.uint8)
# (dy, dx) of the eight neighbours, for reading which node a segment's pixels touch.
_SHIFTS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
# More warnings than this say nothing new; the count of the rest is kept.
MAX_WARNINGS = 50


def rasterise_ways(
    shape: tuple[int, int], ways_px: Sequence[Sequence[Sequence[float]]], thickness: int = 1
) -> np.ndarray:
    """255 along every polyline of `ways_px` (each a list of (x, y) pixel vertices), 0 elsewhere.
    Eight-connected lines, so a thin diagonal is one pixel wide and the thinning has little to do."""
    mask = np.zeros(shape, np.uint8)
    pts = [np.round(np.asarray(way, dtype=np.float64)).astype(np.int32).reshape(-1, 1, 2) for way in ways_px]
    pts = [p for p in pts if len(p) >= 2]
    if pts:
        cv2.polylines(mask, pts, False, 255, thickness, cv2.LINE_8)
    return mask


def thin(mask: np.ndarray, max_iterations: int = 32) -> np.ndarray:
    """The one-pixel-wide skeleton of `mask` (anything nonzero), as uint8 0/1.

    Works on the bounding box of the foreground, padded by one pixel of background so no element
    reads past the image's edge, and stops when a full pass over the eight elements removes
    nothing, or after `max_iterations` passes.
    """
    skeleton = (mask > 0).astype(np.uint8)
    ys, xs = np.nonzero(skeleton)
    if not len(ys):
        return skeleton
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    box = np.zeros((y1 - y0 + 2, x1 - x0 + 2), np.uint8)
    box[1:-1, 1:-1] = skeleton[y0:y1, x0:x1]
    for _ in range(max_iterations):
        before = int(np.count_nonzero(box))
        for kernel in THINNING_KERNELS:
            hit = cv2.morphologyEx(box, cv2.MORPH_HITMISS, kernel)
            box = cv2.subtract(box, hit)
        if int(np.count_nonzero(box)) == before:
            break
    skeleton[y0:y1, x0:x1] = box[1:-1, 1:-1]
    return skeleton


def neighbour_count(skel01: np.ndarray) -> np.ndarray:
    """How many of each pixel's eight neighbours are skeleton pixels (uint8, 0-8)."""
    return cv2.filter2D((skel01 > 0).astype(np.uint8), -1, _RING, borderType=cv2.BORDER_CONSTANT)


def junctions_and_ends(skel01: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(junctions, ends), uint8 0/1: skeleton pixels with three or more neighbours, and with
    exactly one."""
    count = neighbour_count(skel01)
    on = skel01 > 0
    return ((count >= 3) & on).astype(np.uint8), ((count == 1) & on).astype(np.uint8)


def snap_stations(
    skel01: np.ndarray, stations_px: Sequence[Sequence[float] | None], snap_px: float
) -> list[tuple[int, int] | None]:
    """The nearest skeleton pixel (x, y) to each station, or None when the nearest is farther
    than `snap_px` or the station is outside the image.

    `DIST_LABEL_PIXEL` labels every pixel with the 1-based scan-order index of the nearest
    zero pixel of the source, so with the skeleton as the zeros the label indexes `np.argwhere`
    of the skeleton directly (tests/test_vision_network.py proves that on a known skeleton).
    """
    h, w = skel01.shape
    if not skel01.any():
        return [None] * len(stations_px)
    not_skeleton = np.where(skel01 > 0, 0, 1).astype(np.uint8)
    dist, labels = cv2.distanceTransformWithLabels(
        not_skeleton, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL
    )
    pixels = np.argwhere(skel01 > 0)  # (row, col), in scan order
    snapped: list[tuple[int, int] | None] = []
    for station in stations_px:
        if station is None:
            snapped.append(None)
            continue
        x, y = int(round(station[0])), int(round(station[1]))
        if not (0 <= x < w and 0 <= y < h) or dist[y, x] > snap_px:
            snapped.append(None)
            continue
        row, col = pixels[int(labels[y, x]) - 1]
        snapped.append((int(col), int(row)))
    return snapped


def station_node_id(station: dict) -> str:
    return f"s:{station['id']}"


def build_graph(
    skel01: np.ndarray,
    stations: Sequence[dict],
    snapped_px: Sequence[tuple[int, int] | None],
    pixel_size_m: float,
    visible01: np.ndarray,
    tunnel_mask: np.ndarray,
) -> tuple[nx.Graph, dict]:
    """The skeleton as a graph.

    Nodes are the connected clusters of junction pixels, end pixels and the pixels stations
    snapped to: a cluster holding a station is that station (id `s:<osm id>`; a second station
    in the same cluster is merged into the first and recorded); the others are `j<n>` (holding
    a junction pixel) or `e<n>`, at the cluster's centroid. Node attributes: `kind` (station,
    junction, end), `x`, `y`, `station` (the OSM id or None). Edges are the skeleton runs left
    once the clusters and their 3x3 neighbourhoods are cut out, joined to the nodes they touch,
    with `length_m`, `pixels`, `corridor_visibility` (the mean of a 3x3 mean of `visible01`
    over the run; None where over half the run is in `tunnel_mask`) and `tunnel`.

    `info` has `warnings`, `unsnapped` (station ids without a skeleton pixel within reach),
    `merged` ([kept id, merged id] pairs) and `components` of the graph.
    """
    if pixel_size_m <= 0:
        raise ValueError("pixel_size_m must be positive")
    h, w = skel01.shape
    warnings: list[str] = []
    junctions, ends = junctions_and_ends(skel01)
    seeds = cv2.bitwise_or(junctions, ends)
    for snapped in snapped_px:
        if snapped is not None:
            seeds[snapped[1], snapped[0]] = 1
    count, node_labels, _, centroids = cv2.connectedComponentsWithStats(seeds, connectivity=8)

    graph = nx.Graph()
    station_of_label: dict[int, int] = {}
    merged: list[list[str]] = []
    unsnapped: list[str] = []
    for index, (station, snapped) in enumerate(zip(stations, snapped_px, strict=True)):
        if snapped is None:
            unsnapped.append(station_node_id(station))
            continue
        label = int(node_labels[snapped[1], snapped[0]])
        if label in station_of_label:
            kept = stations[station_of_label[label]]
            merged.append([station_node_id(kept), station_node_id(station)])
            warnings.append(f"{station_node_id(station)} shares a node with {station_node_id(kept)}")
        else:
            station_of_label[label] = index
    junction_labels = set(np.unique(node_labels[junctions > 0]).tolist())
    node_of_label: dict[int, str] = {}
    n_junctions = n_ends = 0
    for label in range(1, count):
        if label in station_of_label:
            station = stations[station_of_label[label]]
            x, y = snapped_px[station_of_label[label]]
            node = station_node_id(station)
            graph.add_node(node, kind="station", x=float(x), y=float(y), station=station["id"])
        else:
            if label in junction_labels:
                n_junctions += 1
                kind, node = "junction", f"j{n_junctions}"
            else:
                n_ends += 1
                kind, node = "end", f"e{n_ends}"
            x, y = _r2(centroids[label, 0]), _r2(centroids[label, 1])
            graph.add_node(node, kind=kind, x=x, y=y, station=None)
        node_of_label[label] = node

    # The runs between nodes: the skeleton minus every node cluster and its 3x3 neighbourhood,
    # so the branches of a junction do not touch each other diagonally once it is removed.
    node_zone = cv2.dilate(node_labels.astype(np.float32), _SQUARE).astype(np.int32)
    run_src = ((skel01 > 0) & (node_zone == 0)).astype(np.uint8)
    run_count, run_labels, run_stats, _ = cv2.connectedComponentsWithStats(run_src, connectivity=8)

    # Which node zones each run touches, through the eight neighbour shifts.
    touches: dict[int, set[int]] = {}
    for dy, dx in _SHIFTS:
        runs = run_labels[max(0, -dy) : h - max(0, dy), max(0, -dx) : w - max(0, dx)]
        zones = node_zone[max(0, dy) : h - max(0, -dy), max(0, dx) : w - max(0, -dx)]
        both = (runs > 0) & (zones > 0)
        if not both.any():
            continue
        pairs = np.unique(np.stack([runs[both], zones[both]], axis=1), axis=0)
        for run, zone in pairs.tolist():
            touches.setdefault(int(run), set()).add(int(zone))

    flat_runs = run_labels.ravel()
    run_pixels = np.bincount(flat_runs, minlength=run_count)
    tunnel_pixels = np.bincount(flat_runs, weights=(tunnel_mask > 0).ravel(), minlength=run_count)
    visible_sum = np.bincount(flat_runs, weights=cv2.blur(visible01.astype(np.float32), (3, 3)).ravel(),
                              minlength=run_count)  # fmt: skip
    for run in range(1, run_count):
        nodes = sorted(node_of_label[label] for label in touches.get(run, ()) if label in node_of_label)
        if len(nodes) < 2:
            warnings.append(f"run {run} touches {len(nodes)} node(s): a loop or a fragment, left out")
            continue
        if len(nodes) > 2:
            warnings.append(f"run {run} touches {len(nodes)} nodes: joined pairwise")
        pixels = int(run_pixels[run])
        in_tunnel = tunnel_pixels[run] > pixels / 2
        attributes = {
            "length_m": round(_run_length_px(run_labels, run_stats[run], run) * pixel_size_m, 1),
            "pixels": pixels,
            "corridor_visibility": None if in_tunnel else round(float(visible_sum[run] / pixels), 4),
            "tunnel": bool(in_tunnel),
        }
        for i, u in enumerate(nodes):
            for v in nodes[i + 1 :]:
                if graph.has_edge(u, v):
                    if attributes["length_m"] < graph.edges[u, v]["length_m"]:
                        graph.edges[u, v].update(attributes)
                    warnings.append(f"two runs join {u} and {v}: the shorter is kept")
                    continue
                graph.add_edge(u, v, **attributes)

    if len(warnings) > MAX_WARNINGS:
        rest = len(warnings) - MAX_WARNINGS
        warnings = warnings[:MAX_WARNINGS] + [f"... and {rest} more"]
    info = {
        "warnings": warnings,
        "unsnapped": unsnapped,
        "merged": merged,
        "components": nx.number_connected_components(graph),
    }
    return graph, info


def station_adjacency(graph: nx.Graph) -> dict[str, list[str]]:
    """For each station node, the stations reachable from it through non-station nodes only:
    the stations next along a line, however many junctions lie between."""
    out: dict[str, list[str]] = {}
    for node, data in graph.nodes(data=True):
        if data.get("kind") != "station":
            continue
        seen, todo, found = {node}, [node], set()
        while todo:
            current = todo.pop()
            for neighbour in graph.neighbors(current):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                if graph.nodes[neighbour].get("kind") == "station":
                    found.add(neighbour)
                else:
                    todo.append(neighbour)
        out[node] = sorted(found)
    return out


def _run_length_px(run_labels: np.ndarray, stats: np.ndarray, run: int) -> float:
    """A run's length in pixels: half the closed contour around its pixels (the way there and
    back along a one-pixel line), plus one for the run's own last pixel and the pixel removed
    at each end to cut it from its nodes."""
    x, y, w, h = (int(v) for v in stats[:4])
    crop = (run_labels[y : y + h, x : x + w] == run).astype(np.uint8)
    contours, _ = cv2.findContours(crop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    arc = max((cv2.arcLength(c, True) for c in contours), default=0.0)
    return arc / 2.0 + 3.0


def _r2(value) -> float:
    return round(float(value), 2)
