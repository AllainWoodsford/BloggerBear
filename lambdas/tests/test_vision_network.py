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
    assert info == {"warnings": [], "unsnapped": [], "merged": [], "components": 1}
    kinds = Counter(data["kind"] for _, data in graph.nodes(data=True))
    assert kinds == {"station": 2, "junction": 2, "end": 3}
    assert graph.number_of_edges() == 6
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
    assert "s:ring" in graph and graph.number_of_edges() == 0
    assert any("loop" in w for w in info["warnings"])


def test_station_adjacency_runs_through_junctions_and_stops_at_stations():
    graph = nx.Graph()
    kinds = (("s:1", "station"), ("s:2", "station"), ("s:3", "station"), ("j1", "junction"), ("e1", "end"))
    for node, kind in kinds:
        graph.add_node(node, kind=kind)
    graph.add_edges_from([("s:1", "j1"), ("j1", "s:2"), ("s:2", "s:3"), ("j1", "e1")])
    assert network.station_adjacency(graph) == {"s:1": ["s:2"], "s:2": ["s:1", "s:3"], "s:3": ["s:2"]}


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
