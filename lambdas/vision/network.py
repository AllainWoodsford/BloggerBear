"""The mapped rail network as a graph the imagery can score: rasterise, thin, vectorise.

OpenStreetMap's rail ways are drawn into the scene's pixels, thinned to a one-pixel skeleton,
and read back as a graph: junctions and line ends (and the pixels stations snap to) are the
nodes, the skeleton runs between them are the edges, each with its length and the share of its
pixels the imagery shows as a bare corridor. Because the pixel-to-graph step takes any mask, a
learned segmentation of the tracks could replace the rasterised ways later and nothing after it
would change.

Thinning is sequential hit-or-miss thinning with the Golay L elements (Serra's), done with core
OpenCV's `MORPH_HITMISS`: the headless wheel has no `ximgproc`. networkx is imported here and in
rail_analyse.py, both worker-only modules; the adapters never import either.
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
# (dy, dx) of the eight neighbours, for reading which node a run's pixels touch.
_SHIFTS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
# More warnings than this say nothing new; the count of the rest is kept.
MAX_WARNINGS = 50
# An OSM id is a letter and up to 19 digits; anything longer is not one, and node ids must stay
# bounded wherever they travel (metrics, history, prompts).
MAX_ID_CHARS = 32


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
    """The graph node id of a station: `s:` and its OSM id, bounded to `MAX_ID_CHARS`."""
    return f"s:{str(station['id'])[:MAX_ID_CHARS]}"


def build_graph(
    skel01: np.ndarray,
    stations: Sequence[dict],
    snapped_px: Sequence[tuple[int, int] | None],
    pixel_size_m: float,
    visible01: np.ndarray,
    tunnel_mask: np.ndarray,
    max_nodes: int | None = None,
) -> tuple[nx.Graph, dict]:
    """The skeleton as a graph.

    Nodes are the connected clusters of junction pixels, end pixels and the pixels stations
    snapped to: a cluster holding a station is that station (id `s:<osm id>`; a second station
    in the same cluster is merged into the first and recorded; a repeated station id keeps its
    first entry and leaves the others unsnapped); the others are `j<n>` (holding a junction
    pixel) or `e<n>`, at the cluster's centroid. Node attributes: `kind` (station, junction,
    end), `x`, `y`, `station` (the OSM id or None).

    Edges are the skeleton runs left once the clusters and their 3x3 neighbourhoods are cut
    out, joined to the nodes they touch. The skeleton pixels those neighbourhoods swallow are
    "bridges": a bridge touching one node is that node's doorstep, through which a run reaches
    it (so only skeleton pixels carry adjacency, and a parallel line two pixels away is never
    joined); a bridge touching two nodes (clusters two or three pixels apart, which leave no
    run between them) is an edge itself. Where two paths join the same nodes the shorter is
    kept. No edge joins a node to itself.

    `length_m` is the number of steps along the skeleton from one node's pixel to the other's,
    times the pixel size: a bridge's pixels plus one, or a run's own pixels (half its closed
    contour, plus one) plus the pixel cut from each end plus one. Edge attributes: `length_m`,
    `pixels`, `corridor_visibility` (the mean of a 3x3 mean of `visible01` over the pixels;
    None where over half of them are in `tunnel_mask`) and `tunnel`.

    `info` has `warnings`, `unsnapped` (station ids without a skeleton pixel within reach),
    `merged` ([kept id, merged id] pairs), `snapped` (the per-station pixels actually used,
    None for the unsnapped), `components` of the graph and `too_large`.

    With `max_nodes`, a skeleton of more node clusters or more runs than that is not built: the
    graph comes back empty, with `too_large` true, no components and a warning saying why. Both
    counts come from `connectedComponentsWithStats` before any per-node Python loop or networkx
    allocation, so a dense mesh of crossing ways (a few thousand points can make hundreds of
    thousands of junctions) costs a labelling pass, not gigabytes and minutes.
    """
    if pixel_size_m <= 0:
        raise ValueError("pixel_size_m must be positive")
    warnings: list[str] = []
    # One node per station id: a repeated id keeps its first entry, the others are unsnapped.
    snapped: list[tuple[int, int] | None] = list(snapped_px)
    seen_ids: set[str] = set()
    for index, station in enumerate(stations):
        sid = station_node_id(station)
        if sid in seen_ids and snapped[index] is not None:
            warnings.append(f"{sid} is repeated: the later entry is left unsnapped")
            snapped[index] = None
        seen_ids.add(sid)

    junctions, ends = junctions_and_ends(skel01)
    seeds = cv2.bitwise_or(junctions, ends)
    for pixel in snapped:
        if pixel is not None:
            seeds[pixel[1], pixel[0]] = 1
    count, node_labels, _, centroids = cv2.connectedComponentsWithStats(seeds, connectivity=8)
    if max_nodes is not None and count - 1 > max_nodes:
        return _too_large(stations, snapped, warnings, f"{count - 1} nodes", max_nodes)

    # The runs between nodes: the skeleton minus every node cluster and its 3x3 neighbourhood,
    # so the branches of a junction do not touch each other diagonally once it is removed. The
    # skeleton pixels inside a neighbourhood but not in a cluster are the bridges.
    node_zone = cv2.dilate(node_labels.astype(np.float32), _SQUARE).astype(np.int32)
    on = skel01 > 0
    run_src = (on & (node_zone == 0)).astype(np.uint8)
    run_count, run_labels, _, _ = cv2.connectedComponentsWithStats(run_src, connectivity=8)
    if max_nodes is not None and run_count - 1 > max_nodes:
        return _too_large(stations, snapped, warnings, f"{run_count - 1} runs", max_nodes)

    graph = nx.Graph()
    station_of_label: dict[int, int] = {}
    merged: list[list[str]] = []
    unsnapped: list[str] = []
    for index, (station, pixel) in enumerate(zip(stations, snapped, strict=True)):
        if pixel is None:
            unsnapped.append(station_node_id(station))
            continue
        label = int(node_labels[pixel[1], pixel[0]])
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
            x, y = snapped[station_of_label[label]]
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

    bridge_src = (on & (node_zone > 0) & (node_labels == 0)).astype(np.uint8)
    bridge_count, bridge_labels = cv2.connectedComponents(bridge_src, connectivity=8)
    bridge_touches = _touching(bridge_labels, node_labels, bridge_count)

    # Which node each skeleton pixel speaks for: a node pixel its own, a bridge pixel the one
    # node it touches (a bridge between several nodes falls back to the neighbourhood's label).
    # Only these pixels carry adjacency, never the empty pixels of a neighbourhood.
    node_of_bridge = np.zeros(bridge_count, np.int32)
    for bridge, touched in bridge_touches.items():
        node_of_bridge[bridge] = next(iter(touched)) if len(touched) == 1 else -1
    carrier = node_labels.copy()
    mapped = node_of_bridge[bridge_labels]
    carrier[mapped > 0] = mapped[mapped > 0]
    carrier[mapped < 0] = node_zone[mapped < 0]

    visible_blur = cv2.blur(visible01.astype(np.float32), (3, 3))
    run_stats_px = _pixel_stats(run_labels, run_count, tunnel_mask, visible_blur)
    run_pixels = _run_pixels(run_src, run_labels, run_count)
    for run, touched in _touching(run_labels, carrier, run_count).items():
        nodes = sorted(node_of_label[label] for label in touched if label in node_of_label)
        if len(nodes) < 2:
            warnings.append(f"run {run} touches {len(nodes)} node(s): a loop or a fragment, left out")
            continue
        if len(nodes) > 2:
            warnings.append(f"run {run} touches {len(nodes)} nodes: joined pairwise")
        # The run's pixels, the pixel cut from each end, and the step onto the far node.
        length_px = float(run_pixels[run]) + 3.0
        _join(graph, nodes, _attributes(run_stats_px, run, length_px, pixel_size_m), warnings)

    bridge_stats_px = _pixel_stats(bridge_labels, bridge_count, tunnel_mask, visible_blur)
    for bridge, touched in bridge_touches.items():
        nodes = sorted(node_of_label[label] for label in touched if label in node_of_label)
        if len(nodes) < 2:
            continue
        length_px = float(bridge_stats_px[0][bridge]) + 1.0  # its pixels, and the step onto the far node
        _join(graph, nodes, _attributes(bridge_stats_px, bridge, length_px, pixel_size_m), None)

    if len(warnings) > MAX_WARNINGS:
        rest = len(warnings) - MAX_WARNINGS
        warnings = warnings[:MAX_WARNINGS] + [f"... and {rest} more"]
    info = {
        "warnings": warnings,
        "unsnapped": list(dict.fromkeys(unsnapped)),
        "merged": merged,
        "snapped": snapped,
        "components": nx.number_connected_components(graph),
        "too_large": False,
    }
    return graph, info


def _too_large(
    stations: Sequence[dict], snapped: list, warnings: list[str], what: str, max_nodes: int
) -> tuple[nx.Graph, dict]:
    """The empty graph and the `info` of a skeleton over `max_nodes`: the stations keep their
    snapping (they are on the drawn lines), but no node, edge or component was made."""
    warnings = warnings[:MAX_WARNINGS] + [f"the skeleton has {what}, over {max_nodes}: no graph was built"]
    unsnapped = [station_node_id(s) for s, pixel in zip(stations, snapped, strict=True) if pixel is None]
    info = {
        "warnings": warnings,
        "unsnapped": list(dict.fromkeys(unsnapped)),
        "merged": [],
        "snapped": snapped,
        "components": 0,
        "too_large": True,
    }
    return nx.Graph(), info


def station_adjacency(graph: nx.Graph) -> dict[str, list[str]]:
    """For each station node, the stations reachable from it through non-station nodes only:
    the stations next along a line, however many junctions lie between.

    One pass over the graph, not one per station: the connected pieces of the graph without its
    stations are found once, each with the stations it touches, and a station's neighbours are
    the stations next to it and those of every piece it touches, itself left out. So the cost is
    the graph's size plus the answer's, where a walk from each station through every junction it
    could reach cost stations x (nodes + edges): for 2,000 stations off one 89 x 89 junction grid,
    14.6 s that way and 1.0 s this way (the answer is then every station for every station)."""
    stations = [node for node, data in graph.nodes(data=True) if data.get("kind") == "station"]
    is_station = set(stations)
    piece_of: dict[str, int] = {}
    touching: list[set[str]] = []  # per piece, the stations next to one of its nodes
    for node in graph:
        if node in is_station or node in piece_of:
            continue
        piece, found, todo = len(touching), set(), [node]
        piece_of[node] = piece
        while todo:
            for neighbour in graph.neighbors(todo.pop()):
                if neighbour in is_station:
                    found.add(neighbour)
                elif neighbour not in piece_of:
                    piece_of[neighbour] = piece
                    todo.append(neighbour)
        touching.append(found)
    out: dict[str, list[str]] = {}
    for station in stations:
        found, pieces = set(), set()
        for neighbour in graph.neighbors(station):
            if neighbour in is_station:
                found.add(neighbour)
            else:
                pieces.add(piece_of[neighbour])
        for piece in pieces:
            found |= touching[piece]
        found.discard(station)
        out[station] = sorted(found)
    return out


def _touching(labels: np.ndarray, others: np.ndarray, count: int) -> dict[int, set[int]]:
    """For every label of `labels` (1..count-1), the labels of `others` that one of its pixels
    is eight-adjacent to."""
    h, w = labels.shape
    touches: dict[int, set[int]] = {label: set() for label in range(1, count)}
    for dy, dx in _SHIFTS:
        mine = labels[max(0, -dy) : h - max(0, dy), max(0, -dx) : w - max(0, dx)]
        theirs = others[max(0, dy) : h - max(0, -dy), max(0, dx) : w - max(0, -dx)]
        both = (mine > 0) & (theirs > 0)
        if not both.any():
            continue
        pairs = np.unique(np.stack([mine[both], theirs[both]], axis=1), axis=0)
        for label, other in pairs.tolist():
            touches[int(label)].add(int(other))
    return touches


def _pixel_stats(labels: np.ndarray, count: int, tunnel_mask: np.ndarray, visible_blur: np.ndarray):
    """Per label: its pixel count, how many are in the tunnel mask, and the sum of the blurred
    visibility over them."""
    flat = labels.ravel()
    pixels = np.bincount(flat, minlength=count)
    tunnel = np.bincount(flat, weights=(tunnel_mask > 0).ravel(), minlength=count)
    visible = np.bincount(flat, weights=visible_blur.ravel(), minlength=count)
    return pixels, tunnel, visible


def _attributes(stats, label: int, length_px: float, pixel_size_m: float) -> dict:
    pixels, tunnel, visible = (int(stats[0][label]), float(stats[1][label]), float(stats[2][label]))
    in_tunnel = tunnel > pixels / 2
    return {
        "length_m": round(length_px * pixel_size_m, 1),
        "pixels": pixels,
        "corridor_visibility": None if in_tunnel else round(visible / pixels, 4),
        "tunnel": bool(in_tunnel),
    }


def _join(graph: nx.Graph, nodes: list[str], attributes: dict, warnings: list[str] | None) -> None:
    """An edge between every pair of `nodes`, never from a node to itself. Where an edge exists
    already the shorter of the two is kept, and said so when `warnings` is given (a run beside
    another run); a bridge beside a run is the expected case and stays quiet."""
    for i, u in enumerate(nodes):
        for v in nodes[i + 1 :]:
            if u == v:
                continue
            if graph.has_edge(u, v):
                if attributes["length_m"] < graph.edges[u, v]["length_m"]:
                    graph.edges[u, v].update(attributes)
                if warnings is not None:
                    warnings.append(f"two runs join {u} and {v}: the shorter is kept")
                continue
            graph.add_edge(u, v, **attributes)


def _run_pixels(run_src: np.ndarray, run_labels: np.ndarray, run_count: int) -> np.ndarray:
    """Per run label, the run's extent in pixels along its length: half the closed contour
    around its pixels (the way there and back along a one-pixel line) plus one for the last
    pixel.

    One `findContours` over every run at once: each run is one eight-connected component, so it
    has exactly one outer border (`RETR_CCOMP` keeps them at its top level, with no parent, even
    for a run inside another's hole), traced from the run's first pixel in scan order exactly as
    in a crop holding the run alone, and named by the label under its first point. The cost is one
    pass over the image plus each run's own border, where a crop per run cost its bounding box:
    a long diagonal's is the whole window."""
    arcs = np.zeros(run_count, np.float64)
    contours, hierarchy = cv2.findContours(run_src, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if contours:
        for contour, (_, _, _, parent) in zip(contours, hierarchy[0], strict=True):
            if parent < 0:
                x, y = contour[0, 0]
                run = run_labels[y, x]
                arcs[run] = max(arcs[run], cv2.arcLength(contour, True))
    return arcs / 2.0 + 1.0


def _r2(value) -> float:
    return round(float(value), 2)
