"""Tests for vision/network.py: rasterised ways to a thinned skeleton to a graph.

Every skeleton here is drawn with `rasterise_ways` or set pixel by pixel, so each test knows
which pixels are the junctions, the ends and the runs between them. Pixels are 20 m.
"""

from __future__ import annotations

import os
import time
from collections import Counter

import cv2
import networkx as nx
import numpy as np
import pytest

from vision import network

PX = 20.0
SHAPE = (120, 120)
CROSS = [[(10, 60), (110, 60)], [(60, 10), (60, 110)]]
BRANCH = [(80, 60), (110, 30)]  # a 30 px diagonal off the eastern arm


def skeleton_of(ways, thickness=1):
    return network.thin(network.rasterise_ways(SHAPE, ways, thickness=thickness))


def ones():
    return np.ones(SHAPE, np.uint8)


def zeros():
    return np.zeros(SHAPE, np.uint8)


def test_thinning_reduces_a_thick_line_to_one_pixel_and_keeps_it_connected():
    mask = network.rasterise_ways(SHAPE, CROSS, thickness=3)
    skel = network.thin(mask)
    assert skel.dtype == np.uint8 and set(np.unique(skel).tolist()) == {0, 1}
    # One pixel wide along both arms, away from the crossing.
    assert (skel[:, 20:50].sum(axis=0) == 1).all()
    assert (skel[20:50, :].sum(axis=1) == 1).all()
    count, _ = cv2.connectedComponents(skel, connectivity=8)
    assert count == 2
    assert skel.sum() < np.count_nonzero(mask) / 2


def test_thinning_converges_and_is_idempotent():
    mask = network.rasterise_ways(SHAPE, CROSS, thickness=7)
    skel = network.thin(mask)
    np.testing.assert_array_equal(network.thin(skel), skel)
    # One pass peels one layer from each side: a 7 px line is not yet thin after it.
    assert network.thin(mask, max_iterations=1).sum() > skel.sum()
    assert not network.thin(zeros()).any()


def test_a_one_pixel_diagonal_survives_thinning():
    mask = network.rasterise_ways(SHAPE, [[(10, 10), (100, 100)]])
    np.testing.assert_array_equal(network.thin(mask), (mask > 0).astype(np.uint8))


def test_junctions_and_ends_on_a_cross():
    skel = skeleton_of(CROSS)
    junctions, ends = network.junctions_and_ends(skel)
    assert {tuple(p) for p in np.argwhere(ends > 0).tolist()} == {(60, 10), (60, 110), (10, 60), (110, 60)}
    found = np.argwhere(junctions > 0)
    assert len(found) >= 1 and (np.abs(found - [60, 60]) <= 1).all()
    assert network.neighbour_count(skel)[60, 60] == 4
    assert network.neighbour_count(skel)[60, 50] == 2


def test_pixel_labels_index_skeleton_pixels_in_scan_order():
    skel = zeros()
    for row, col in ((5, 30), (20, 7), (20, 90), (70, 50)):
        skel[row, col] = 1
    not_skeleton = np.where(skel > 0, 0, 1).astype(np.uint8)
    _, labels = cv2.distanceTransformWithLabels(not_skeleton, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    for index, (row, col) in enumerate(np.argwhere(skel > 0).tolist(), start=1):
        assert labels[row, col] == index
    # Which is what lets snap_stations read the pixel straight off the label.
    snapped = network.snap_stations(skel, [(32, 6), (8, 21), (88, 19), (49, 71)], snap_px=5)
    assert snapped == [(30, 5), (7, 20), (90, 20), (50, 70)]


def test_stations_snap_within_reach_and_not_beyond():
    skel = skeleton_of([[(10, 60), (110, 60)]])
    stations = [(50, 63), (50, 70), (50, 60), (500, 500), None]
    assert network.snap_stations(skel, stations, snap_px=5) == [(50, 60), None, (50, 60), None, None]
    assert network.snap_stations(skel, stations, snap_px=12) == [(50, 60), (50, 60), (50, 60), None, None]
    assert network.snap_stations(zeros(), stations, snap_px=5) == [None] * 5


def test_graph_of_a_cross_with_a_branch():
    skel = skeleton_of([*CROSS, BRANCH])
    stations = [{"id": "nA", "name": "A"}, {"id": "nB", "name": "B"}]
    snapped = network.snap_stations(skel, [(10, 60), (110, 30)], snap_px=3)
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    assert info == {
        "warnings": [], "unsnapped": [], "merged": [], "snapped": snapped, "components": 1,
        "too_large": False,
    }  # fmt: skip
    kinds = Counter(data["kind"] for _, data in graph.nodes(data=True))
    assert kinds == {"station": 2, "junction": 2, "end": 3}
    assert graph.number_of_edges() == 6 and nx.number_of_selfloops(graph) == 0
    assert graph.nodes["s:nA"] == {"kind": "station", "x": 10.0, "y": 60.0, "station": "nA"}
    centre = next(n for n, d in graph.nodes(data=True) if d["kind"] == "junction" and abs(d["x"] - 60) < 2)
    assert abs(graph.nodes[centre]["y"] - 60) < 2
    # The western arm: 50 px from the station to the crossing.
    assert graph.edges["s:nA", centre]["length_m"] == pytest.approx(50 * PX, rel=0.1)
    # The branch: a diagonal of 30 px.
    (fork,) = (n for n in graph.neighbors("s:nB"))
    assert graph.nodes[fork]["kind"] == "junction"
    assert graph.edges["s:nB", fork]["length_m"] == pytest.approx(30 * np.sqrt(2) * PX, rel=0.1)
    assert graph.edges["s:nB", fork]["corridor_visibility"] == 1.0
    assert graph.edges["s:nB", fork]["tunnel"] is False
    assert graph.edges["s:nB", fork]["pixels"] > 20


def test_a_skeleton_over_max_nodes_is_refused_before_any_graph_is_made(monkeypatch):
    # Nine ways each way, 10 px apart: 81 crossings and 36 ends are 117 nodes, joined by 180 runs;
    # a station on one of them adds a node and splits a run.
    steps = range(20, 101, 10)
    skel = skeleton_of([[(5, y), (114, y)] for y in steps] + [[(x, 5), (x, 114)] for x in steps])
    stations = [{"id": "n1"}, {"id": "n2"}]
    snapped = network.snap_stations(skel, [(25, 20), (25, 23)], snap_px=2)
    assert snapped == [(25, 20), None]
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros(), max_nodes=1000)
    assert graph.number_of_nodes() == 118 and not info["too_large"]

    def no_graph(*args, **kwargs):
        raise AssertionError("the graph was being built")

    monkeypatch.setattr(network, "_touching", no_graph)  # the first per-run step after the checks
    monkeypatch.setattr(network.nx.Graph, "add_node", no_graph)
    for max_nodes, what in ((117, "118 nodes, over 117"), (118, "181 runs, over 118")):
        graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros(), max_nodes=max_nodes)
        assert graph.number_of_nodes() == 0 and info == {
            "warnings": [f"the skeleton has {what}: no graph was built"], "unsnapped": ["s:n2"], "merged": [],
            "snapped": snapped, "components": 0, "too_large": True,
        }  # fmt: skip


def test_two_stations_in_one_cluster_merge_with_a_warning():
    skel = skeleton_of([[(10, 60), (110, 60)]])
    stations = [{"id": "n1", "name": "First"}, {"id": "n2", "name": "Second"}, {"id": "n3", "name": "Far"}]
    snapped = network.snap_stations(skel, [(50, 60), (51, 60), (50, 90)], snap_px=5)
    assert snapped == [(50, 60), (51, 60), None]
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    assert info["merged"] == [["s:n1", "s:n2"]]
    assert info["unsnapped"] == ["s:n3"]
    assert any("s:n2 shares a node with s:n1" in w for w in info["warnings"])
    assert "s:n1" in graph and "s:n2" not in graph and "s:n3" not in graph
    assert graph.degree("s:n1") == 2


@pytest.mark.parametrize(
    ("tunnel_cols", "in_tunnel", "visibility"),
    [
        ((0, 0), False, pytest.approx(0.5, abs=0.05)),
        ((10, 85), True, None),  # three quarters of the run
        ((10, 40), False, pytest.approx(0.5, abs=0.05)),  # under half: still a surface corridor
    ],
)
def test_corridor_visibility_ignores_tunnels_and_vegetation(tunnel_cols, in_tunnel, visibility):
    skel = skeleton_of([[(10, 60), (110, 60)]])
    stations = [{"id": "w", "name": "W"}, {"id": "e", "name": "E"}]
    snapped = network.snap_stations(skel, [(10, 60), (110, 60)], snap_px=2)
    visible = zeros()
    visible[:, :60] = 1  # bare ground on the western half, canopy on the eastern
    tunnel = zeros()
    tunnel[:, tunnel_cols[0] : tunnel_cols[1]] = 255
    graph, _ = network.build_graph(skel, stations, snapped, PX, visible, tunnel)
    edge = graph.edges["s:w", "s:e"]
    assert edge["tunnel"] is in_tunnel
    assert edge["corridor_visibility"] == visibility


def test_a_run_touching_one_node_is_a_loop_and_left_out():
    mask = zeros()
    cv2.circle(mask, (60, 60), 30, 255, 1)
    skel = network.thin(mask)
    stations = [{"id": "ring", "name": "Ring"}]
    snapped = network.snap_stations(skel, [(60, 30)], snap_px=3)
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    assert "s:ring" in graph and graph.number_of_edges() == 0 and nx.number_of_selfloops(graph) == 0
    assert any("loop" in w for w in info["warnings"])


def test_nodes_two_or_three_pixels_apart_are_bridged_and_lengths_count_steps():
    skel = skeleton_of([[(10, 60), (110, 60)]])
    stations = [{"id": sid, "name": sid.upper()} for sid in "abcdef"]
    columns = [50, 52, 70, 73, 90, 94]
    snapped = network.snap_stations(skel, [(x, 60) for x in columns], snap_px=2)
    assert snapped == [(x, 60) for x in columns]
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    assert info["components"] == 1 and info["merged"] == [] and info["warnings"] == []
    # A and B (2 px apart) and C and D (3 px apart) leave no run between them; the bridge is the
    # edge. E and F (4 px apart) leave a one-pixel run. Every length is the step count between
    # the two nodes' pixels, whichever way the path was found.
    assert graph.edges["s:a", "s:b"]["length_m"] == 2 * PX and graph.edges["s:a", "s:b"]["pixels"] == 1
    assert graph.edges["s:c", "s:d"]["length_m"] == 3 * PX and graph.edges["s:c", "s:d"]["pixels"] == 2
    assert graph.edges["s:e", "s:f"]["length_m"] == 4 * PX and graph.edges["s:e", "s:f"]["pixels"] == 1
    assert graph.edges["s:b", "s:c"]["length_m"] == 18 * PX
    assert graph.edges["s:d", "s:e"]["length_m"] == 17 * PX
    west = next(n for n in graph.neighbors("s:a") if n != "s:b")  # the line's western end, 40 px away
    assert graph.edges["s:a", west]["length_m"] == 40 * PX
    assert graph.edges["s:a", "s:b"]["corridor_visibility"] == 1.0 and not graph.edges["s:a", "s:b"]["tunnel"]
    assert graph.number_of_edges() == 7 and nx.number_of_selfloops(graph) == 0


def test_a_bridge_beside_a_long_run_replaces_it_when_shorter():
    mask = zeros()
    cv2.rectangle(mask, (20, 20), (100, 100), 255, 1)
    skel = network.thin(mask)
    stations = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
    snapped = network.snap_stations(skel, [(50, 20), (52, 20)], snap_px=2)
    assert snapped == [(50, 20), (52, 20)]
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    # The ring joins A and B the long way round; the one pixel between them is the short way.
    assert graph.number_of_edges() == 1 and graph.edges["s:a", "s:b"]["length_m"] == 2 * PX
    assert info["components"] == 1


def test_parallel_lines_two_pixels_apart_are_not_joined():
    skel = skeleton_of([[(10, 60), (110, 60)], [(10, 62), (110, 62)]])
    stations = [{"id": "s", "name": "S"}]
    snapped = network.snap_stations(skel, [(50, 60)], snap_px=1)
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    assert graph.number_of_edges() == 3 and info["components"] == 2 and info["warnings"] == []
    assert graph.degree("s:s") == 2
    # A siding ending two pixels from a through line is not joined to it either.
    skel = skeleton_of([[(10, 60), (110, 60)], [(50, 62), (50, 90)]])
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    assert info["components"] == 2 and info["warnings"] == []
    line = nx.node_connected_component(graph, "s:s")
    assert all(abs(graph.nodes[n]["y"] - 60) < 1 for n in line) and len(line) == 3


def test_a_short_stub_does_not_make_a_second_component():
    skel = skeleton_of([[(10, 60), (110, 60)], [(60, 60), (60, 57)]])
    stations = [{"id": "w", "name": "W"}]
    snapped = network.snap_stations(skel, [(10, 60)], snap_px=2)
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    assert info["components"] == 1
    ends = [n for n, d in graph.nodes(data=True) if d["kind"] == "end"]
    assert len(ends) == 2 and all(graph.degree(n) == 1 for n in ends)
    (junction,) = (n for n, d in graph.nodes(data=True) if d["kind"] == "junction")
    (stub,) = (n for n in ends if abs(graph.nodes[n]["x"] - 60) < 1)
    # The stub's first pixel is diagonally adjacent to the line too, so it is part of the junction
    # cluster; the one pixel left between the clusters is a bridge, one pixel plus the step.
    expected = {"length_m": 2 * PX, "pixels": 1, "corridor_visibility": 1.0, "tunnel": False}
    assert graph.edges[junction, stub] == expected


def test_a_repeated_station_id_keeps_the_first_entry():
    skel = skeleton_of([[(10, 60), (110, 60)]])
    stations = [{"id": "dup", "name": "First"}, {"id": "dup", "name": "Second"}, {"id": "x", "name": "X"}]
    snapped = network.snap_stations(skel, [(30, 60), (80, 60), (100, 60)], snap_px=2)
    graph, info = network.build_graph(skel, stations, snapped, PX, ones(), zeros())
    assert info["snapped"] == [(30, 60), None, (100, 60)] and info["unsnapped"] == ["s:dup"]
    assert any("s:dup is repeated" in w for w in info["warnings"])
    assert graph.nodes["s:dup"]["x"] == 30.0 and graph.number_of_nodes() == 4
    assert network.station_node_id({"id": "n" + "9" * 40}) == "s:n" + "9" * 31


def test_station_adjacency_runs_through_junctions_and_stops_at_stations():
    graph = nx.Graph()
    kinds = (("s:1", "station"), ("s:2", "station"), ("s:3", "station"), ("j1", "junction"), ("e1", "end"))
    for node, kind in kinds:
        graph.add_node(node, kind=kind)
    graph.add_edges_from([("s:1", "j1"), ("j1", "s:2"), ("s:2", "s:3"), ("j1", "e1")])
    assert network.station_adjacency(graph) == {"s:1": ["s:2"], "s:2": ["s:1", "s:3"], "s:3": ["s:2"]}


def adjacency_by_walking(graph):
    """station_adjacency as it was: a walk from every station through every non-station node it
    can reach. The one-pass version must give exactly this."""
    out = {}
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


def random_rail_graph(seed):
    """A seeded random graph of station, junction and end nodes (and the odd node without a kind,
    or with a loop onto itself): sparse or dense, few stations or most."""
    rng = np.random.default_rng(seed)
    n = int(rng.integers(0, 40))
    shape = nx.gnp_random_graph(n, float(rng.choice([0.03, 0.08, 0.2, 0.5])), seed=int(seed))
    share = float(rng.choice([0.1, 0.4, 0.8]))
    graph = nx.Graph()
    names = {}
    for i in shape.nodes:
        kind = "station" if rng.random() < share else str(rng.choice(["junction", "end", "none"]))
        names[i] = f"s:{i}" if kind == "station" else f"j{i}"
        if kind == "none":
            graph.add_node(names[i])
        else:
            graph.add_node(names[i], kind=kind)
    graph.add_edges_from((names[u], names[v]) for u, v in shape.edges)
    if n and rng.random() < 0.2:
        loop = names[int(rng.integers(0, n))]
        graph.add_edge(loop, loop)
    return graph


def test_station_adjacency_in_one_pass_is_the_walk_from_every_station():
    graphs = [random_rail_graph(seed) for seed in range(400)]
    assert sum(1 for g in graphs if any(d.get("kind") == "station" for _, d in g.nodes(data=True))) > 300
    for seed, graph in enumerate(graphs):
        assert network.station_adjacency(graph) == adjacency_by_walking(graph), seed


def junction_tree_with_station_leaves(side=45, stations=2000):
    """A `side` x `side` grid of junctions with `stations` stations hanging off it: every station
    is next to every other through the junctions, the shape that made the walk from each station
    cost stations x (nodes + edges)."""
    grid = nx.grid_2d_graph(side, side)
    graph = nx.Graph()
    graph.add_nodes_from((f"j{i}_{j}" for i, j in grid.nodes), kind="junction")
    graph.add_edges_from((f"j{a}_{b}", f"j{c}_{d}") for (a, b), (c, d) in grid.edges)
    core = sorted(f"j{i}_{j}" for i, j in grid.nodes)
    for k in range(stations):
        graph.add_node(f"s:{k:04d}", kind="station")
        graph.add_edge(core[(k * 7919) % len(core)], f"s:{k:04d}")
    return graph


def test_station_adjacency_of_two_thousand_stations_off_one_junction_tree_is_quick():
    graph = junction_tree_with_station_leaves()
    started = time.perf_counter()
    adjacency = network.station_adjacency(graph)
    seconds = time.perf_counter() - started
    stations = sorted(n for n in graph if n.startswith("s:"))
    assert len(adjacency) == 2000
    assert all(adjacency[s] == [t for t in stations if t != s] for s in ("s:0000", "s:1234", "s:1999"))
    assert seconds < 10.0  # 0.9 s here; the walk from every station took 5.9 s on this graph


def crop_per_run(run_labels, stats, run):
    """A run's length as build_graph measured it before: a crop of the run's bounding box, the
    run's pixels in it, and its outer contour. The one-pass version must give exactly this."""
    x, y, w, h = (int(v) for v in stats[:4])
    crop = (run_labels[y : y + h, x : x + w] == run).astype(np.uint8)
    contours, _ = cv2.findContours(crop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    arc = max((cv2.arcLength(c, True) for c in contours), default=0.0)
    return arc / 2.0 + 1.0


def diagonals(size, spacing):
    """Anti-diagonal ways x + y = c, `spacing` px apart, corner to corner: each run's bounding box
    is the square on its diagonal, up to the whole window."""
    mask = np.zeros((size, size), np.uint8)
    for c in range(8, 2 * size - 8, spacing):
        low = max(0, c - (size - 1))
        cv2.line(mask, (low, c - low), (c - low, low), 255, 1, cv2.LINE_8)
    return network.thin(mask)


def ring_and_box():
    mask = zeros()
    cv2.circle(mask, (60, 60), 30, 255, 1)
    cv2.rectangle(mask, (45, 45), (75, 75), 255, 1)  # inside the ring's hole
    cv2.line(mask, (50, 60), (70, 60), 255, 1)
    return network.thin(mask)


def random_ways(seed, size=300):
    rng = np.random.default_rng(seed)
    ways = [[tuple(int(v) for v in p) for p in rng.integers(0, size, (4, 2))] for _ in range(12)]
    return network.thin(network.rasterise_ways((size, size), ways, thickness=int(rng.integers(1, 4))))


RUN_FIXTURES = {
    "cross and branch": lambda: skeleton_of([*CROSS, BRANCH]),
    "thick cross": lambda: skeleton_of(CROSS, thickness=3),
    "mesh": lambda: skeleton_of([[(5, y), (114, y)] for y in range(20, 101, 10)]
                                + [[(x, 5), (x, 114)] for x in range(20, 101, 10)]),  # fmt: skip
    "parallel lines": lambda: skeleton_of([[(10, 60), (110, 60)], [(10, 62), (110, 62)]]),
    "stub": lambda: skeleton_of([[(10, 60), (110, 60)], [(60, 60), (60, 57)]]),
    "ring and box": ring_and_box,
    "diagonals 3 px apart": lambda: diagonals(400, 3),
    "one long diagonal": lambda: network.thin(network.rasterise_ways((1200, 1200), [[(0, 1199), (1199, 0)]])),
    **{f"random ways {seed}": (lambda seed=seed: random_ways(seed)) for seed in range(6)},
}


@pytest.mark.parametrize("name", list(RUN_FIXTURES))
def test_run_lengths_in_one_pass_are_those_of_a_crop_per_run(name, monkeypatch):
    skel = RUN_FIXTURES[name]()
    seen = []
    one_pass = network._run_pixels

    def recording(run_src, run_labels, run_count):
        lengths = one_pass(run_src, run_labels, run_count)
        seen.append((run_src, run_labels, run_count, lengths))
        return lengths

    monkeypatch.setattr(network, "_run_pixels", recording)
    blank = np.zeros(skel.shape, np.uint8)
    graph, _ = network.build_graph(skel, [], [], PX, blank + 1, blank)
    ((run_src, run_labels, run_count, lengths),) = seen
    count, labels, stats, _ = cv2.connectedComponentsWithStats(run_src, connectivity=8)
    assert count == run_count > 1 and np.array_equal(labels, run_labels)
    assert lengths.tolist()[1:] == [crop_per_run(run_labels, stats[run], run) for run in range(1, count)]
    assert graph.number_of_edges() > 0


def test_rasterise_skips_degenerate_ways_and_rounds_vertices():
    mask = network.rasterise_ways(SHAPE, [[(10.4, 10.4)], [(10.4, 10.4), (10.6, 30.2)]])
    assert mask.dtype == np.uint8 and mask[10, 10] == 255 and mask[30, 11] == 255
    assert np.count_nonzero(mask) == 21


@pytest.mark.skipif(not os.environ.get("VISION_PERF"), reason="set VISION_PERF=1 to time the thinning")
def test_thinning_a_city_sized_sparse_mask_is_fast():
    rng = np.random.default_rng(1)
    ways = [[(int(x), int(y)) for x, y in rng.integers(0, 3000, (6, 2))] for _ in range(60)]
    mask = network.rasterise_ways((3000, 3000), ways)
    started = time.perf_counter()
    skel = network.thin(mask)
    seconds = time.perf_counter() - started
    print(f"thin() on a 3000x3000 mask with {np.count_nonzero(mask)} pixels: {seconds:.2f} s")
    assert skel.any() and seconds < 2.0
