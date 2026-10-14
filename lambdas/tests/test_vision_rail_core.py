"""Tests for the rail access core: vision/urban.py, access.py, rail_analyse.py and
rail_annotate.py.

The scene is a synthetic 400 x 400 city at 20 m in UTM zone 56S: a vegetated background, a
built district, a lake (SCL 6) inside it and a cloud (SCL 9) outside it. The network is drawn in
pixels and turned into lon/lat with `geo.utm_to_lonlat`, so every test also round-trips the
projection. The graph measures, flags and suggestions are tested on hand-built graphs whose
answers are known. The figure is tested by decoding the PNG and sampling pixels, on the city's
own analysis, on copies of its metrics with flags and suggestions injected, on the city upscaled
to the worker's 1024 px, and on a crowded city of noise built to be as many bytes as a figure gets.
"""

from __future__ import annotations

import json
import os
import time

import cv2
import networkx as nx
import numpy as np
import pytest

from common import vision_contract as contract
from common.vision_client import MAX_IMAGE_BYTES
from vision import access, geo, rail_analyse, rail_annotate, urban
from vision import network as rail_network
from vision.annotate import CAPTION_BACKGROUND, CAPTION_PAD, burn_caption, caption_bar_size
from vision.rail_analyse import (
    PARAM_LIMITS,
    RailAnalysis,
    RailParams,
    analyse_rail_access,
    flag_stations,
    graph_metrics,
    project_network,
    suggest,
)
from vision.rail_annotate import (
    CAPTION_LINES,
    DESERT_COLOUR,
    FIGURE_BYTE_BUDGET,
    FIGURE_MIN_SIDE,
    FLAG_COLOUR,
    HUB_COLOUR,
    LEGEND_MIN_WIDTH,
    RULE_COLOUR,
    STRETCH_MAX,
    SUGGESTION_COLOUR,
    _dashed_line,
    annotate_rail,
    figure_strip,
    legend_strip,
)

SIZE, PX = 400, 20.0
TRANSFORM, EPSG = (300000.0, 20.0, 0.0, 6300000.0, 0.0, -20.0), 32756
WHOLE = [[[0, 0], [SIZE - 1, 0], [SIZE - 1, SIZE - 1], [0, SIZE - 1]]]
# (red, nir, swir16) reflectance x 10000: canopy (NDVI 0.67, NDBI -0.43) and roofs (0.09, 0.14).
VEGETATION = (600, 3000, 1200)
BUILT = (1500, 1800, 2400)
DISTRICT = (slice(100, 300), slice(100, 300))  # 4 km square
LAKE = (slice(220, 260), slice(120, 160))  # inside the district
CLOUD = (slice(30, 80), slice(300, 380))  # outside it


def city(size=SIZE):
    """The bands of the synthetic city: `red`, `nir`, `swir16` and `scl` (at another `size`, the
    district, lake and cloud scaled with it)."""
    names = ("red", "nir", "swir16")
    bands = {name: np.full((size, size), v, np.uint16) for name, v in zip(names, VEGETATION, strict=True)}
    scl = np.full((size, size), 4, np.uint8)  # vegetation

    def scaled(region):
        return tuple(slice(part.start * size // SIZE, part.stop * size // SIZE) for part in region)

    for region, scl_class in ((scaled(DISTRICT), 5), (scaled(LAKE), 6), (scaled(CLOUD), 9)):
        for name, value in zip(names, BUILT, strict=True):
            bands[name][region] = value  # the lake and the cloud look built up; only SCL tells
        scl[region] = scl_class
    bands["scl"] = scl
    return bands


def lonlat(x, y):
    return list(geo.pixel_to_lonlat(TRANSFORM, EPSG, x, y))


STATIONS_PX = {
    "n1": ("West", 40, 200), "n2": ("Central", 200, 200), "n3": ("East", 360, 200), "n4": ("North", 200, 40),
    "n5": ("South", 200, 360), "n6": ("Branch End", 340, 120), "n7": ("Midwest", 120, 203),
}  # fmt: skip
POIS_PX = (
    ("p1", "bus_stop", 205, 200), ("p2", "bus_stop", 200, 210), ("p3", "bus_stop", 220, 200),
    ("p4", "bus_station", 120, 215), ("p5", "ferry_terminal", 360, 222), ("p6", "park_ride", 390, 200),
)  # fmt: skip
FETCHED_AT = "2026-10-10T03:00:00+00:00"
NO_INTERMODAL = {"bus_stop_near": 0, "bus_station_near": 0, "ferry_terminal_far": 0, "park_ride_far": 0}


def grid_network():
    """Two crossing lines and a branch, the southern arm in tunnel, seven stations (one 3 px
    off its line), bus stops near Central, a bus station near Midwest, a ferry terminal and a
    park-and-ride near East."""
    ways = [
        {"id": "w1", "points": [lonlat(40, 200), lonlat(360, 200)], "tunnel": False, "name": "East West"},
        {"id": "w2", "points": [lonlat(200, 40), lonlat(200, 200)], "tunnel": False, "name": "North"},
        {"id": "w3", "points": [lonlat(200, 200), lonlat(200, 360)], "tunnel": True, "name": "South tunnel"},
        {"id": "w4", "points": [lonlat(280, 200), lonlat(340, 120)], "tunnel": False, "name": "Branch"},
    ]
    stations = [
        {"id": sid, "name": name, "kind": "station", "lon": lonlat(x, y)[0], "lat": lonlat(x, y)[1]}
        for sid, (name, x, y) in STATIONS_PX.items()
    ]
    pois = [
        {"id": pid, "kind": kind, "lon": lonlat(x, y)[0], "lat": lonlat(x, y)[1]}
        for pid, kind, x, y in POIS_PX
    ]
    return {"ways": ways, "stations": stations, "pois": pois, "fetched_at": FETCHED_AT}


@pytest.fixture(scope="module")
def analysis():
    return analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, grid_network())


# --- urban ---------------------------------------------------------------------------------------


def test_built_up_excludes_water_cloud_and_vegetation():
    bands = city()
    built, unusable, water = urban.built_up_mask(bands["red"], bands["nir"], bands["swir16"], bands["scl"])
    assert built[150, 250] == 255 and built[DISTRICT].mean() > 200
    assert not built[LAKE].any() and not built[CLOUD].any()
    assert not built[:90, :90].any()  # canopy
    assert water[LAKE].all() and not water[DISTRICT[0], 170:].any()
    assert unusable[CLOUD].all() and not unusable[DISTRICT].any()
    assert built.dtype == unusable.dtype == water.dtype == np.uint8


def test_ndvi_and_ndbi_are_zero_where_there_is_no_signal():
    zero = np.zeros((2, 2), np.uint16)
    assert not urban.ndvi(zero, zero).any() and not urban.ndbi(zero, zero).any()
    red, nir = np.array([[600]], np.uint16), np.array([[3000]], np.uint16)
    assert urban.ndvi(red, nir)[0, 0] == pytest.approx(2400 / 3600)
    assert urban.ndbi(red, nir)[0, 0] == pytest.approx(-2400 / 3600)
    assert urban.ndvi(red, nir).dtype == np.float32


def test_heat_is_a_share_in_unit_range_that_peaks_in_the_district():
    bands = city()
    built, unusable, _ = urban.built_up_mask(bands["red"], bands["nir"], bands["swir16"], bands["scl"])
    usable = cv2.bitwise_not(unusable)
    heat = urban.heat_map(built, sigma_px=500 / PX, usable=usable)
    assert heat.dtype == np.float32 and heat.min() >= 0.0 and heat.max() <= 1.0
    # Deep inside the district (over 2.4 sigma from its edges and the lake) the local share of
    # built-up ground is one: no percentile rescaling.
    assert heat[180, 240] == pytest.approx(1.0, abs=0.02)
    assert heat[20, 20] < 0.01
    assert heat[200, 200] > heat[100, 100] > heat[60, 60]
    assert not heat[CLOUD].any()
    with pytest.raises(ValueError):
        urban.heat_map(built, 0)


# --- access --------------------------------------------------------------------------------------


def test_distance_is_zero_at_a_station_and_served_share_in_range():
    stations = [(200, 200), (120, 203)]
    mask = access.station_raster((SIZE, SIZE), stations)
    assert mask.dtype == np.uint8 and mask.sum() == 2 * 255
    dist, labels = access.distance_to_stations(mask)
    assert dist[200, 200] == 0.0 and dist[203, 120] == 0.0
    assert dist[200, 210] == pytest.approx(10.0, rel=0.05)
    assert access.label_of_station(labels, stations) == [labels[200, 200], labels[203, 120]]
    assert labels[200, 200] != labels[203, 120]
    bands = city()
    built, _, _ = urban.built_up_mask(bands["red"], bands["nir"], bands["swir16"], bands["scl"])
    stats = access.served_stats(built, dist, reach_px=50, pixel_km2=0.04)
    assert 0 < stats["served_share"] < 1
    assert stats["served_km2"] + stats["desert_km2"] == pytest.approx(stats["built_up_km2"])
    assert stats["built_up_km2"] == pytest.approx(np.count_nonzero(built) * 0.04)
    assert access.served_stats(np.zeros_like(built), dist, 50, 0.04)["served_share"] == 0.0


def test_no_station_means_everything_is_out_of_reach():
    dist, labels = access.distance_to_stations(np.zeros((10, 10), np.uint8))
    assert np.isinf(dist).all() and not labels.any()
    assert access.label_of_station(labels, [(1, 1), (50, 50), None]) == [0, 0, 0]
    assert access.catchment_activity(np.ones((10, 10), np.float32), labels, dist, 5, [0]) == ([None], [0.0])


def test_desert_clusters_are_built_up_beyond_reach_with_lonlat_centroids(analysis):
    deserts = analysis.metrics["deserts"]
    assert deserts and deserts[0]["area_km2"] > 1.0
    assert deserts == sorted(deserts, key=lambda d: -d["area_km2"])
    for desert in deserts:
        assert 150.8 < desert["lon"] < 151.0 and -33.5 < desert["lat"] < -33.4
        assert desert["nearest_station"] in {f"s:{sid}" for sid in STATIONS_PX}
        assert desert["nearest_station_m"] > 0
    desert, built = analysis.layers["desert"], analysis.layers["built"]
    assert desert.any() and not desert[built == 0].any()
    assert not desert[190:210, 190:210].any()  # around Central


def test_desert_clusters_are_filtered_and_capped():
    desert = np.zeros((50, 50), np.uint8)
    desert[2:12, 2:12] = 255  # 100 px
    desert[20:22, 20:22] = 255  # 4 px
    desert[30:45, 30:45] = 255  # 225 px
    found = access.desert_clusters(desert, min_px=10)
    assert [c["area_px"] for c in found] == [225, 100]
    assert found[0]["bbox"] == [30, 30, 15, 15] and found[0]["x"] == 37.0
    assert access.desert_clusters(desert, min_px=1, top=1) == found[:1]


def test_catchment_activity_is_the_mean_heat_of_a_clipped_voronoi_cell():
    heat = np.zeros((20, 20), np.float32)
    heat[:, :10], heat[:, 10:] = 0.4, 0.8
    stations = [(2, 10), (17, 10)]
    dist, labels = access.distance_to_stations(access.station_raster(heat.shape, stations))
    station_labels = access.label_of_station(labels, stations)

    def activity(reach, usable=None):
        return access.catchment_activity(heat, labels, dist, reach, station_labels, usable=usable)

    assert activity(4) == (pytest.approx([0.4, 0.8]), [1.0, 1.0])
    # With a wide reach the cells meet in the middle; the left one is all 0.4, the right all 0.8.
    assert activity(100) == (pytest.approx([0.4, 0.8]), [1.0, 1.0])
    heat[:, 8:10] = 1.0  # the far edge of the left cell: 6 px from its station, beyond a reach of 4
    assert activity(4)[0][0] == pytest.approx(0.4)
    assert activity(100)[0][0] == pytest.approx(0.52)
    # Cloud over the right cell: unmeasured, not quiet. Over the left cell's far edge: a partial
    # catchment, averaged over what was seen, with its coverage said.
    usable = np.full(heat.shape, 255, np.uint8)
    usable[:, 10:] = 0
    usable[:, 8:10] = 0
    activities, coverage = activity(100, usable)
    assert activities == [pytest.approx(0.4), None]
    assert coverage == [pytest.approx(0.8), 0.0]


def test_intermodal_counts_at_both_radii():
    stations = np.array([[0.0, 0.0], [10000.0, 0.0]])
    pois = np.array([[100.0, 0.0], [0.0, 250.0], [0.0, 450.0], [0.0, 450.0], [0.0, 600.0], [10200.0, 0.0]])
    kinds = ["bus_stop", "bus_station", "ferry_terminal", "bus_stop", "park_ride", "park_ride"]
    counts = access.intermodal_counts(stations, pois, kinds, near_m=300, far_m=500)
    assert counts == [
        {"bus_stop_near": 1, "bus_station_near": 1, "ferry_terminal_far": 1, "park_ride_far": 0},
        {"bus_stop_near": 0, "bus_station_near": 0, "ferry_terminal_far": 0, "park_ride_far": 1},
    ]
    assert access.intermodal_counts(stations, np.zeros((0, 2)), [], 300, 500) == [NO_INTERMODAL] * 2
    with pytest.raises(ValueError):
        access.intermodal_counts(stations, pois, kinds[:-1], 300, 500)


# --- the graph measures, flags and suggestions ---------------------------------------------------


def star():
    """A hub with four spokes of known length, every node a station."""
    graph = nx.Graph()
    graph.add_node("s:H", kind="station", x=0.0, y=0.0, station="H")
    arms = (("A", (50, 0), 1000.0), ("B", (0, 50), 2000.0), ("C", (-50, 0), 3000.0), ("D", (0, -50), 4000.0))
    for name, (x, y), length in arms:
        graph.add_node(f"s:{name}", kind="station", x=float(x), y=float(y), station=name)
        graph.add_edge("s:H", f"s:{name}", length_m=length)
    return graph


def test_betweenness_hubs_and_d_hub_on_a_star():
    graph = star()
    activity = {"s:H": 0.9, "s:A": 0.5, "s:B": 0.5, "s:C": 0.1, "s:D": 0.1}
    measures = graph_metrics(graph, activity, hub_count=1)
    assert measures["hubs"] == ["s:H"] and measures["interchanges"] == ["s:H"]
    assert measures["betweenness"]["s:H"] == 1.0 and measures["betweenness"]["s:A"] == 0.0
    assert measures["degree"] == {"s:H": 4, "s:A": 1, "s:B": 1, "s:C": 1, "s:D": 1}
    assert measures["d_hub_m"] == {"s:H": 0.0, "s:A": 1000.0, "s:B": 2000.0, "s:C": 3000.0, "s:D": 4000.0}
    assert set(measures["hub_of"].values()) == {"s:H"}
    assert measures["edge_betweenness"][("s:A", "s:H")] == pytest.approx(0.4)
    # Two hubs: the second is the busiest-half station with the next betweenness, ties by id.
    assert graph_metrics(graph, activity, hub_count=2)["hubs"] == ["s:H", "s:A"]
    # A node cut off from every hub has no distance.
    graph.add_node("s:X", kind="station", x=9.0, y=9.0, station="X")
    assert graph_metrics(graph, activity, hub_count=1)["d_hub_m"]["s:X"] is None
    assert graph_metrics(nx.Graph(), {}, 5) == {
        "degree": {}, "betweenness": {}, "edge_betweenness": {}, "approximate": False, "hubs": [],
        "interchanges": [], "d_hub_m": {}, "hub_of": {},
    }  # fmt: skip
    # An unmeasured station (activity None) is never a hub; with nothing measured, betweenness alone picks.
    assert graph_metrics(graph, {**activity, "s:H": None}, hub_count=1)["hubs"] == ["s:A"]
    assert graph_metrics(graph, dict.fromkeys(graph.nodes), hub_count=1)["hubs"] == ["s:H"]
    assert not graph_metrics(graph, activity, hub_count=1)["approximate"]


def row(sid, activity, degree, betweenness, d_hub_m):
    weight = None if d_hub_m is None else round((d_hub_m / 1000) / max(activity, 0.05), 4)
    return {"id": sid, "name": sid[2:], "activity": activity, "degree": degree, "betweenness": betweenness,
            "d_hub_m": d_hub_m, "isolation_weight": weight}  # fmt: skip


def test_the_three_station_flags():
    rows = [
        row("s:A", 0.9, 1, 0.1, 9000.0),  # busy, on a stub, far from a hub: isolated high demand
        row("s:B", 0.8, 3, 0.8, 8000.0),  # busy and far, but an interchange; and the single point of failure
        row("s:C1", 0.1, 2, 0.3, 500.0), row("s:C2", 0.1, 2, 0.0, 500.0), row("s:C3", 0.1, 2, 0.0, 500.0),
        row("s:C4", 0.1, 2, 0.0, 500.0), row("s:D", 0.1, 1, 0.0, 200.0), row("s:E", 0.1, 1, 0.0, None),
    ]  # fmt: skip
    adjacency = {"s:A": ["s:B"], "s:B": ["s:A", "s:C4"], "s:C4": ["s:B", "s:C3"], "s:C3": ["s:C4", "s:C2"],
                 "s:C2": ["s:C3", "s:C1"], "s:C1": ["s:C2"], "s:D": [], "s:E": []}  # fmt: skip
    flags = flag_stations(rows, adjacency)
    assert [f["type"] for f in flags] == ["isolated_high_demand", "single_point_of_failure", "ghost_line"]
    assert flags[0] == {"type": "isolated_high_demand", "station": "s:A", "name": "A", "activity": 0.9,
                        "degree": 1, "isolation_weight": 10.0}  # fmt: skip
    assert flags[1] == {"type": "single_point_of_failure", "station": "s:B", "name": "B",
                        "ratio": pytest.approx(8 / 3, abs=1e-4)}  # fmt: skip
    assert flags[2] == {"type": "ghost_line", "stations": ["s:C1", "s:C2", "s:C3", "s:C4"],
                        "names": ["C1", "C2", "C3", "C4"]}  # fmt: skip
    assert flag_stations([], {}) == []
    # Nothing stands out when every station is alike.
    alike = [row(f"s:{i}", 0.5, 2, 0.2, 1000.0) for i in range(5)]
    assert flag_stations(alike, {r["id"]: [] for r in alike}) == []


def walk_chain_with_a_list(start, members, adjacency):
    """rail_analyse._walk_chain as it was, testing each step against the list of the order so
    far. The walk with a visited set must give exactly this."""
    component, todo = {start}, [start]
    while todo:
        for neighbour in adjacency.get(todo.pop(), ()):
            if neighbour in members and neighbour not in component:
                component.add(neighbour)
                todo.append(neighbour)

    def inside(node):
        return [n for n in adjacency.get(node, ()) if n in component]

    first = min(component, key=lambda n: (len(inside(n)), n))
    order, current = [first], first
    while True:
        following = [n for n in inside(current) if n not in order]
        if not following:
            break
        current = min(following)
        order.append(current)
    return order + sorted(component - set(order))


def test_the_ghost_line_walk_with_a_visited_set_is_the_walk_it_replaces():
    rng = np.random.default_rng(3)
    for trial in range(300):
        ids = [f"s:{i:02d}" for i in range(int(rng.integers(1, 30)))]
        density = float(rng.choice([0.05, 0.15, 0.4]))
        adjacency = {s: set() for s in ids}
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                if rng.random() < density:
                    adjacency[a].add(b)
                    adjacency[b].add(a)
        adjacency = {s: sorted(n) for s, n in adjacency.items()}
        members = {s for s in ids if rng.random() < 0.7} or {ids[0]}
        for start in sorted(members):
            expected = walk_chain_with_a_list(start, members, adjacency)
            assert rail_analyse._walk_chain(start, members, adjacency) == expected, trial


def test_a_two_thousand_station_ghost_line_is_walked_in_order():
    """2,000 quiet stations one after another along a line (ids shuffled along it), and 700 busy
    ones off it: one ghost line, in line order, the same as the list walk gave."""
    rng = np.random.default_rng(5)
    line = [f"s:q{i:04d}" for i in rng.permutation(2000)]
    adjacency = {s: sorted(line[max(0, i - 1) : i] + line[i + 1 : i + 2]) for i, s in enumerate(line)}
    adjacency.update({f"s:b{i:03d}": [] for i in range(700)})
    rows = [row(s, 0.0, 2, 0.0, 5000.0) for s in line] + [row(f"s:b{i:03d}", 0.6, 1, 0.0, 900.0)
                                                          for i in range(700)]  # fmt: skip
    started = time.perf_counter()
    flags = flag_stations(rows, adjacency)
    seconds = time.perf_counter() - started
    (ghost,) = (f for f in flags if f["type"] == "ghost_line")
    ends = sorted((line[0], line[-1]))
    assert ghost["stations"] == (line if ends[0] == line[0] else line[::-1])
    assert ghost["stations"] == walk_chain_with_a_list(ends[0], set(line), adjacency)
    assert seconds < 5.0  # well under 0.1 s here


def test_a_ghost_line_of_stations_all_next_to_each_other_is_walked_quickly():
    """1,500 quiet stations off one junction tree are all next to each other: the walk takes the
    nearest id each time. Testing each step against the order so far took 10.1 s here."""
    ids = [f"s:{i:04d}" for i in range(2000)]
    adjacency = {s: [t for t in ids if t != s] for s in ids}
    rows = [row(s, 0.0 if i % 4 else 0.7, 1, 0.0, 5000.0) for i, s in enumerate(ids)]
    started = time.perf_counter()
    flags = flag_stations(rows, adjacency)
    seconds = time.perf_counter() - started
    (ghost,) = (f for f in flags if f["type"] == "ghost_line")
    assert ghost["stations"] == [s for i, s in enumerate(ids) if i % 4]
    assert seconds < 10.0  # 0.35 s here


def spokes():
    """Three spokes from a hub H; the far ends of two of them (A2 and B3) are 800 m apart in a
    straight line but 10 km apart by rail: the case for an orbital link."""
    graph = nx.Graph()
    nodes = {"s:H": (0, 0), "s:A1": (75, 0), "s:A2": (100, 0), "s:B1": (0, 75), "s:B2": (0, 300),
             "s:B3": (100, 40), "s:C1": (-75, 0)}  # fmt: skip
    for node, (x, y) in nodes.items():
        graph.add_node(node, kind="station", x=float(x), y=float(y), station=node[2:])
    edges = (("s:H", "s:A1", 1500.0), ("s:A1", "s:A2", 2500.0), ("s:H", "s:B1", 1500.0),
             ("s:B1", "s:B2", 1500.0), ("s:B2", "s:B3", 3000.0), ("s:H", "s:C1", 1500.0))  # fmt: skip
    for u, v, length in edges:
        graph.add_edge(u, v, length_m=length)
    activity = {"s:H": 0.9, "s:A1": 0.5, "s:A2": 0.3, "s:B1": 0.5, "s:B2": 0.3, "s:B3": 0.2,
                "s:C1": 0.5}  # fmt: skip
    return graph, activity


def rows_of(graph, activity, measures):
    return [
        row(node, activity[node], measures["degree"][node], measures["betweenness"][node],
            measures["d_hub_m"][node])  # fmt: skip
        for node in sorted(graph.nodes)
    ]


def test_orbital_links_lower_the_mean_distance_to_a_hub_and_are_deterministic():
    graph, activity = spokes()
    measures = graph_metrics(graph, activity, hub_count=1)
    assert measures["hubs"] == ["s:H"]
    rows = rows_of(graph, activity, measures)
    adjacency = {node: sorted(graph.neighbors(node)) for node in graph}
    first = suggest(graph, rows, measures, adjacency, PX, max_orbital_km=5.0)
    second = suggest(graph, rows, measures, adjacency, PX, max_orbital_km=5.0)
    assert first == second
    orbital = [s for s in first if s["type"] == "orbital_link"]
    assert len(orbital) == 1
    link = orbital[0]
    assert (link["from"], link["to"], link["from_name"], link["to_name"]) == ("s:A2", "s:B3", "A2", "B3")
    assert link["straight_km"] == pytest.approx(0.8)
    # B3 reaches the hub through A2 in 960 + 4000 m instead of 6000: the mean over the seven falls.
    assert link["delta_mean_d_hub_m"] == pytest.approx(-(6000 - 4960) / 7, abs=0.1)
    assert link["delta_top_hub_betweenness"] < 0
    # Out of straight-line range, no link; linking the two is not suggested when they are adjacent.
    assert not [s for s in suggest(graph, rows, measures, adjacency, PX, 0.5) if s["type"] == "orbital_link"]
    adjacency["s:A2"].append("s:B3")
    assert not [s for s in suggest(graph, rows, measures, adjacency, PX, 5.0) if s["type"] == "orbital_link"]



def test_a_simulated_link_is_measured_with_the_base_graphs_sampling(monkeypatch):
    """The before and after betweenness of an orbital link come from one estimator: with the work
    budget set so the spokes graph is exact but the graph plus one link would be sampled, the trial
    is still exact, and the suggestion is the one an unlimited budget gives."""
    graph, activity = spokes()
    adjacency = {node: sorted(graph.neighbors(node)) for node in graph}

    def run():
        measures = graph_metrics(graph, activity, hub_count=1)
        return suggest(graph, rows_of(graph, activity, measures), measures, adjacency, PX, 5.0)

    unlimited = run()
    n, m = graph.number_of_nodes(), graph.number_of_edges()
    monkeypatch.setattr(rail_analyse, "BETWEENNESS_WORK", n * (n + m))  # exact; one more edge is not
    monkeypatch.setattr(rail_analyse, "MIN_BETWEENNESS_SAMPLES", 2)
    calls = []
    real = nx.betweenness_centrality

    def recording(g, **kwargs):
        calls.append((g.number_of_edges(), kwargs.get("k")))
        return real(g, **kwargs)

    monkeypatch.setattr(rail_analyse.nx, "betweenness_centrality", recording)
    assert run() == unlimited
    assert calls and all(k is None for _, k in calls)  # the trial (m + 1 edges) included
    assert any(edges == m + 1 for edges, _ in calls)

def test_feeder_corridors_target_the_nearest_hub():
    graph, activity = spokes()
    measures = graph_metrics(graph, activity, hub_count=2)
    assert measures["hubs"] == ["s:H", "s:B1"]  # B1 carries every path to B2 and B3
    rows = rows_of(graph, activity, measures)
    suggestions = suggest(graph, rows, measures, {n: [] for n in graph}, PX, 5.0)
    feeders = [s for s in suggestions if s["type"] == "feeder_corridor"]
    # The three largest isolation weights, each to the hub nearest by rail: B3 and B2 to B1, A2 to H.
    assert [(f["station"], f["hub"], f["length_m"]) for f in feeders] == [
        ("s:B3", "s:B1", 4500.0), ("s:A2", "s:H", 4000.0), ("s:B2", "s:B1", 1500.0),
    ]  # fmt: skip
    assert feeders[0] == {"type": "feeder_corridor", "station": "s:B3", "name": "B3", "hub": "s:B1",
                          "hub_name": "B1", "length_m": 4500.0}  # fmt: skip
    assert suggest(graph, rows, {**measures, "hubs": []}, {}, PX, 5.0) == []


# --- the whole pass ------------------------------------------------------------------------------

METRIC_KEYS = {
    "task", "coverage", "built_up_km2", "served_km2", "desert_km2", "served_share", "station_count",
    "snapped_station_count", "node_count", "edge_count", "components", "hubs", "interchanges", "flags",
    "suggestions", "stations", "deserts", "edges", "truncated", "quality_flags", "warnings", "network",
    "params",
}  # fmt: skip
STATION_KEYS = {
    "id", "name", "kind", "lon", "lat", "snapped", "degree", "betweenness", "activity", "catchment_coverage",
    "d_hub_m", "isolation_weight", "intermodal",
}  # fmt: skip
FLAG_TYPES = {"isolated_high_demand", "single_point_of_failure", "ghost_line"}


def test_the_analysis_is_json_and_echoes_its_params(analysis):
    metrics = analysis.metrics
    assert json.loads(json.dumps(metrics, allow_nan=False)) == metrics
    assert set(metrics) == METRIC_KEYS
    assert all(set(station) == STATION_KEYS for station in metrics["stations"])
    assert metrics["task"] == "rail_access"
    assert metrics["params"] == {"pixel_size_m": PX, "coverage_floor": 0.6, **RailParams().__dict__}
    assert metrics["network"] == {"ways": 4, "stations": 7, "pois": 6, "points": 8, "fetched_at": FETCHED_AT}
    again = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, grid_network()).metrics
    assert again == metrics


def test_the_analysis_measures_the_city(analysis):
    m = analysis.metrics
    assert m["coverage"] == pytest.approx(1 - 50 * 80 / SIZE**2, abs=1e-4)
    assert m["quality_flags"] == [] and m["warnings"] == [] and m["truncated"] == 0
    assert m["station_count"] == m["snapped_station_count"] == 7
    assert m["components"] == 1 and m["edge_count"] == 7 and m["node_count"] == 8
    assert 0 < m["served_share"] < 1
    assert m["served_km2"] + m["desert_km2"] == pytest.approx(m["built_up_km2"], abs=1e-3)
    assert m["built_up_km2"] == pytest.approx((200 * 200 - 40 * 40) * (PX / 1000) ** 2, rel=0.02)
    assert m["interchanges"] == ["s:n2"] and m["hubs"][0] == "s:n2"
    by_id = {s["id"]: s for s in m["stations"]}
    central = by_id["s:n2"]
    assert central["degree"] == 4 and central["activity"] > 0.9 and central["d_hub_m"] == 0.0
    assert central["intermodal"] == {**NO_INTERMODAL, "bus_stop_near": 2}
    assert central["catchment_coverage"] == 1.0
    assert 0.9 < by_id["s:n6"]["catchment_coverage"] < 1.0  # the cloud clips Branch End's catchment
    midwest = by_id["s:n7"]
    assert midwest["snapped"] and midwest["intermodal"] == {**NO_INTERMODAL, "bus_station_near": 1}
    assert by_id["s:n3"]["intermodal"] == {**NO_INTERMODAL, "ferry_terminal_far": 1}
    assert by_id["s:n1"]["activity"] < 0.1 < by_id["s:n7"]["activity"]
    assert by_id["s:n1"]["lon"] == pytest.approx(lonlat(40, 200)[0], abs=1e-5)
    assert all(s["kind"] == "station" and s["name"] for s in m["stations"])
    edges = {(e["from"], e["to"]): e for e in m["edges"]}
    assert list(edges) == sorted(edges)
    south = edges["s:n2", "s:n5"]
    assert south["tunnel"] is True and south["corridor_visibility"] is None
    assert south["length_m"] == pytest.approx(160 * PX, rel=0.1)
    north = edges["s:n2", "s:n4"]
    assert north["tunnel"] is False and 0.5 < north["corridor_visibility"] < 0.8  # half in the canopy
    assert edges["s:n2", "s:n7"]["corridor_visibility"] == 1.0
    assert all(0 <= e["betweenness"] <= 1 for e in m["edges"])
    assert {f["type"] for f in m["flags"]} <= FLAG_TYPES
    assert analysis.layers["timings"].keys() == {"masks", "access", "graph", "metrics"}
    assert analysis.layers["skeleton"].sum() > 300 and analysis.layers["heat"].max() <= 1.0


def test_project_network_round_trips_pixels():
    projected = project_network(grid_network(), EPSG, TRANSFORM, (SIZE, SIZE))
    for station in projected["stations"]:
        _, x, y = STATIONS_PX[station["id"]]
        assert station["x"] == pytest.approx(x, abs=1e-3) and station["y"] == pytest.approx(y, abs=1e-3)
        assert station["inside"]
    assert len(projected["ways_px"]) == 3 and len(projected["tunnels_px"]) == 1
    np.testing.assert_allclose(projected["tunnels_px"][0], [[200, 200], [200, 360]], atol=1e-3)
    assert projected["stations_xy_m"].shape == (7, 2) and projected["pois_xy_m"].shape == (6, 2)
    assert projected["poi_kinds"][3] == "bus_station"
    far = {"stations": [{"id": "x", "name": "Far", "lon": 152.0, "lat": -34.0}]}
    projected = project_network(far, EPSG, TRANSFORM, (SIZE, SIZE))
    assert projected["stations"][0]["inside"] is False and projected["stations_px"] == [None]
    assert projected["pois_xy_m"].shape == (0, 2)


def test_low_coverage_and_no_network_are_flagged_not_crashes():
    bands = city()
    bands["scl"][:, :220] = 9  # cloud over more than half the site
    m = analyse_rail_access(bands, WHOLE, PX, TRANSFORM, EPSG, grid_network()).metrics
    assert "low_coverage" in m["quality_flags"] and m["coverage"] < 0.6
    m = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, {}).metrics
    assert m["quality_flags"] == ["no_network"]
    assert m["station_count"] == m["edge_count"] == m["node_count"] == 0
    assert m["served_share"] == 0.0 and m["desert_km2"] == m["built_up_km2"] > 0
    assert m["hubs"] == m["stations"] == m["edges"] == m["flags"] == m["suggestions"] == []
    assert m["deserts"][0]["nearest_station"] is None and m["deserts"][0]["nearest_station_m"] is None
    assert m["network"] == {"ways": 0, "stations": 0, "pois": 0, "points": 0, "fetched_at": None}
    m = analyse_rail_access(city(), [], PX, TRANSFORM, EPSG, grid_network()).metrics
    assert "empty_site" in m["quality_flags"] and m["coverage"] == 0.0 and m["built_up_km2"] == 0.0


def test_unsnapped_stations_and_fragments_are_flagged():
    net = grid_network()
    for station in net["stations"]:
        station["lon"], station["lat"] = lonlat(20, 390)  # all far from any line
    m = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, net).metrics
    assert "few_stations_snapped" in m["quality_flags"] and m["snapped_station_count"] == 0
    assert all(not s["snapped"] and s["degree"] == 0 and s["d_hub_m"] is None for s in m["stations"])
    assert all(s["isolation_weight"] is None and s["activity"] is not None for s in m["stations"])
    net["ways"] = [
        {"id": f"w{i}", "points": [lonlat(20 + 40 * i, 20), lonlat(20 + 40 * i, 60)], "tunnel": False}
        for i in range(5)
    ]
    m = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, net).metrics
    assert "graph_fragmented" in m["quality_flags"] and m["components"] == 5


def test_stations_outside_the_window_leave_deserts_without_a_nearest_station():
    net = grid_network()
    for station in net["stations"]:
        station["lon"], station["lat"] = 152.0, -34.0  # 70 km east of the window
    m = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, net).metrics
    assert m["deserts"]
    assert all(d["nearest_station"] is None and d["nearest_station_m"] is None for d in m["deserts"])
    assert m["served_share"] == 0.0 and m["station_count"] == 7 and m["snapped_station_count"] == 0
    assert all(s["activity"] is None and s["catchment_coverage"] == 0.0 for s in m["stations"])
    assert json.loads(json.dumps(m, allow_nan=False)) == m


def test_a_station_under_cloud_is_unmeasured_not_quiet():
    bands = city()
    bands["scl"][145:256, 145:256] = 9  # cloud over Central and its whole catchment (reach 50 px)
    m = analyse_rail_access(bands, WHOLE, PX, TRANSFORM, EPSG, grid_network()).metrics
    by_id = {s["id"]: s for s in m["stations"]}
    assert by_id["s:n2"]["activity"] is None and by_id["s:n2"]["catchment_coverage"] == 0.0
    assert by_id["s:n2"]["isolation_weight"] is None and by_id["s:n2"]["snapped"]
    assert "s:n2" not in m["hubs"]  # unmeasured, so not among the busier half
    assert 0.0 < by_id["s:n7"]["catchment_coverage"] < 1.0 and by_id["s:n7"]["activity"] is not None
    assert not [f for f in m["flags"] if f["type"] == "ghost_line" and "s:n2" in f["stations"]]
    assert json.loads(json.dumps(m, allow_nan=False)) == m


def test_network_elements_off_the_earth_are_skipped_with_a_warning():
    net = grid_network()
    net["ways"][0]["points"].insert(1, [float("nan"), -33.9])
    net["ways"][0]["points"].insert(1, {"lon": 151.0, "lat": -33.9})  # a dict where a pair should be
    net["ways"].append({"id": "w9", "points": [[200.0, 0.0], [1e400, 0.0]], "tunnel": False})
    net["stations"].append({"id": "n8", "name": "Pole", "lon": 151.0, "lat": 95.0})
    net["stations"].append({"id": "n9", "name": "Text", "lon": "x", "lat": -33.9})
    net["pois"].append({"id": "p9", "kind": "bus_stop", "lon": 200.0, "lat": -33.9})
    projected = project_network(net, EPSG, TRANSFORM, (SIZE, SIZE))
    assert projected["warnings"] == [
        "4 way vertices with coordinates not on Earth skipped",
        "2 stations with coordinates not on Earth skipped",
        "1 points of interest with coordinates not on Earth skipped",
    ]
    assert (len(projected["ways_px"]), len(projected["stations"]), len(projected["poi_kinds"])) == (3, 7, 6)
    m = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, net).metrics
    assert m["warnings"][:3] == projected["warnings"] and m["station_count"] == 7
    # The network block counts what was received; the warnings say what was skipped.
    assert m["network"] == {"ways": 5, "stations": 9, "pois": 7, "points": 12, "fetched_at": FETCHED_AT}


def test_a_repeated_station_id_keeps_one_entry():
    net = grid_network()
    outside = {"id": "n2", "name": "Central (dup)", "kind": "station", "lon": 152.0, "lat": -34.0}
    twin = {**net["stations"][6], "id": "n7", "name": "Midwest (dup)"}
    net["stations"] = [outside, *net["stations"], twin]  # a twin outside first, a twin inside last
    projected = project_network(net, EPSG, TRANSFORM, (SIZE, SIZE))
    assert projected["warnings"] == ["2 repeated station ids dropped: one entry kept for each"]
    kept = {s["id"]: s for s in projected["stations"]}
    assert len(projected["stations"]) == 7 and kept["n2"]["name"] == "Central" and kept["n2"]["inside"]
    assert kept["n7"]["name"] == "Midwest"  # the first of two entries inside wins
    m = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, net).metrics
    assert m["station_count"] == 7 and len({s["id"] for s in m["stations"]}) == 7
    assert m["snapped_station_count"] == 7 and m["network"]["stations"] == 9
    assert m["warnings"] == projected["warnings"]


def test_a_pathological_graph_skips_the_graph_measures_and_says_so(monkeypatch):
    monkeypatch.setattr("vision.rail_analyse.MAX_GRAPH_NODES", 5)
    graph, activity = spokes()
    measures = graph_metrics(graph, activity, hub_count=2)
    assert measures["approximate"] and measures["hubs"] == [] and measures["betweenness"] == {}
    assert measures["edge_betweenness"] == {} and set(measures["d_hub_m"].values()) == {None}
    assert measures["degree"]["s:H"] == 3 and measures["interchanges"] == ["s:H"]


def test_a_skeleton_over_the_cap_is_never_made_a_graph_and_says_so(monkeypatch):
    full = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, grid_network()).metrics
    assert full["node_count"] == 8 and "graph_too_large" not in full["quality_flags"]
    assert "betweenness_sampled" not in full["quality_flags"]  # eight nodes: exact
    monkeypatch.setattr("vision.rail_analyse.MAX_GRAPH_NODES", 5)
    analysis = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, grid_network())
    m = analysis.metrics
    assert "graph_too_large" in m["quality_flags"] and "graph_fragmented" not in m["quality_flags"]
    assert (m["node_count"], m["edge_count"], m["components"], m["truncated"]) == (0, 0, 0, 0)
    assert m["hubs"] == m["interchanges"] == m["flags"] == m["suggestions"] == m["edges"] == []
    # The stations are still on the drawn lines, and still measured against the imagery.
    assert m["snapped_station_count"] == full["snapped_station_count"] == 7
    assert [s["activity"] for s in m["stations"]] == [s["activity"] for s in full["stations"]]
    for station in m["stations"]:
        assert (station["degree"], station["betweenness"], station["d_hub_m"]) == (0, 0.0, None)
        assert station["isolation_weight"] is None
    assert m["warnings"][-1] == "the skeleton has 8 nodes, over 5: no graph was built"
    assert json.loads(json.dumps(m, allow_nan=False)) == m
    assert annotate_rail(analysis, city()["red"], 2024).startswith(b"\x89PNG")


def mesh_network(size, gap):
    """Two-point ways `gap` px apart across a `size` px window, each way crossing every way the
    other way: (size / gap) squared junctions from a few hundred points."""
    steps = range(gap, size - gap + 1, gap)
    ways = [{"id": f"h{y}", "points": [lonlat(1, y), lonlat(size - 2, y)], "tunnel": False} for y in steps]
    ways += [{"id": f"v{x}", "points": [lonlat(x, 1), lonlat(x, size - 2)], "tunnel": False} for x in steps]
    stations = [{"id": f"n{k}", "name": f"S{k}", "kind": "station", "lon": lonlat(5 * k + 7, 5 * k + 5)[0],
                 "lat": lonlat(5 * k + 7, 5 * k + 5)[1]} for k in range(20)]  # fmt: skip
    return {"ways": ways, "stations": stations, "pois": [], "fetched_at": FETCHED_AT}


def test_a_dense_mesh_of_ways_is_refused_before_the_graph_is_built():
    """206 two-point ways 5 px apart over a 520 px window cross 10,609 times: over MAX_GRAPH_NODES,
    found from the labelled pixels, so the pass takes a fraction of a second, not minutes."""
    size = 520
    names = ("red", "nir", "swir16")
    bands = {name: np.full((size, size), value, np.uint16) for name, value in zip(names, BUILT, strict=True)}
    bands["scl"] = np.full((size, size), 5, np.uint8)
    window = [[[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]]]
    network = contract.validate_network(mesh_network(size, 5))
    assert sum(len(way["points"]) for way in network["ways"]) == 412
    started = time.perf_counter()
    m = analyse_rail_access(bands, window, PX, TRANSFORM, EPSG, network).metrics
    assert time.perf_counter() - started < 2.0  # 0.1 s here; 29 s when the graph was built first
    assert "graph_too_large" in m["quality_flags"] and m["node_count"] == 0
    crossings_and_ends = 103 * 103 + 4 * 103
    cap = rail_analyse.MAX_GRAPH_NODES
    assert m["warnings"][-1] == f"the skeleton has {crossings_and_ends} nodes, over {cap}: no graph was built"
    reply = {"version": 1, "ok": True, "task": "rail_access", "backend": "opencv", "build": {},
             "image_png_b64": None, "metrics": json.loads(json.dumps(m, allow_nan=False))}  # fmt: skip
    assert contract.validate_reply(reply) == reply


def test_edges_over_the_cap_are_dropped_by_betweenness_and_counted(monkeypatch):
    full = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, grid_network()).metrics
    monkeypatch.setattr("vision.rail_analyse.MAX_EDGES", 3)
    capped = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, grid_network()).metrics
    assert capped["truncated"] == 4 and capped["edge_count"] == 7 and len(capped["edges"]) == 3

    def station_touching(edge):
        return edge["from"].startswith("s:") or edge["to"].startswith("s:")

    def rank(edge):
        return -edge["betweenness"], not station_touching(edge), edge["from"], edge["to"]

    kept = sorted(full["edges"], key=rank)[:3]
    assert capped["edges"] == sorted(kept, key=lambda e: (e["from"], e["to"]))


def big_graph(side=30, stations=40):
    """A `side` x `side` grid of junctions, the first `stations` of them stations: at the default
    side, over the exact-betweenness budget."""
    grid = nx.grid_2d_graph(side, side)
    graph = nx.Graph()
    for index, (i, j) in enumerate(sorted(grid.nodes)):
        if index < stations:
            graph.add_node(f"s:{index}", kind="station", x=float(i * 10), y=float(j * 10), station=str(index))
        else:
            graph.add_node(f"j{index}", kind="junction", x=float(i * 10), y=float(j * 10), station=None)
    names = {node: (f"s:{k}" if k < stations else f"j{k}") for k, node in enumerate(sorted(grid.nodes))}
    for u, v in grid.edges:
        graph.add_edge(names[u], names[v], length_m=200.0)
    return graph


def test_a_large_graph_uses_sampled_betweenness_and_says_so():
    graph = big_graph()
    activity = {f"s:{k}": 0.5 for k in range(40)}
    started = time.perf_counter()
    measures = graph_metrics(graph, activity, hub_count=3)
    assert time.perf_counter() - started < 15.0  # about 2 s here; exact betweenness would be ten times that
    assert measures["approximate"] and len(measures["hubs"]) == 3
    assert measures == graph_metrics(graph, activity, hub_count=3)  # the seed makes the sample repeat
    assert all(d is not None for d in measures["d_hub_m"].values())


def test_betweenness_samples_what_the_work_budget_affords(monkeypatch):
    graph = big_graph()  # 900 nodes, 1,740 edges: 2,640 steps a source
    assert rail_analyse.BETWEENNESS_WORK < 900 * 2640
    assert rail_analyse._betweenness_sample(graph) == {"k": 200, "seed": 0}  # capped at BETWEENNESS_SAMPLES
    monkeypatch.setattr(rail_analyse, "BETWEENNESS_WORK", 50 * 2640)
    assert rail_analyse._betweenness_sample(graph) == {"k": 50, "seed": 0}
    monkeypatch.setattr(rail_analyse, "BETWEENNESS_WORK", 5 * 2640)
    assert rail_analyse._betweenness_sample(graph) == {"k": rail_analyse.MIN_BETWEENNESS_SAMPLES, "seed": 0}
    _, _, approximate = rail_analyse.betweenness(graph)
    assert approximate
    monkeypatch.setattr(rail_analyse, "BETWEENNESS_WORK", 900 * 2640)  # every source fits: exact
    assert rail_analyse._betweenness_sample(graph) == {}
    small = big_graph(side=5, stations=3)
    assert rail_analyse._betweenness_sample(small) == {} and not rail_analyse.betweenness(small)[2]


@pytest.mark.skipif(not os.environ.get("VISION_PERF"), reason="set VISION_PERF=1 to time the graph measures")
def test_the_graph_measures_of_a_five_thousand_node_graph_stay_within_budget():
    graph = big_graph(side=71, stations=180)  # 5,041 nodes, 9,940 edges
    stations = [node for node in sorted(graph) if node.startswith("s:")]
    activity = {node: 0.1 + (index % 9) / 10 for index, node in enumerate(stations)}
    started = time.perf_counter()
    measures = graph_metrics(graph, activity, hub_count=5)
    between, d_hub = measures["betweenness"], measures["d_hub_m"]
    rows = [row(n, activity[n], measures["degree"][n], between.get(n, 0.0), d_hub[n]) for n in stations]
    adjacency = rail_network.station_adjacency(graph)
    flag_stations(rows, adjacency)
    suggest(graph, rows, measures, adjacency, PX, max_orbital_km=20.0)
    seconds = time.perf_counter() - started
    print(f"graph measures, flags and suggestions on {graph.number_of_nodes()} nodes: {seconds:.2f} s")
    assert measures["approximate"] and len(measures["hubs"]) == 5
    assert seconds < 20.0  # about 3 s here; 200 sources in each of the five betweenness calls took 22 s


def test_a_city_sized_network_completes_in_seconds_with_the_flag():
    # 38 lines each way, 10 px apart: about 1,440 junctions, 60 of them stations.
    steps = [10 + 10 * i for i in range(38)]
    lines = [{"id": f"h{y}", "points": [lonlat(10, y), lonlat(390, y)], "tunnel": False} for y in steps]
    lines += [{"id": f"v{x}", "points": [lonlat(x, 10), lonlat(x, 390)], "tunnel": False} for x in steps]
    stations = []
    for k in range(60):
        lon, lat = lonlat(steps[3 * k % 38], steps[7 * k % 38])
        stations.append({"id": f"n{k}", "name": f"S{k}", "kind": "station", "lon": lon, "lat": lat})
    net = {"ways": lines, "stations": stations, "pois": [], "fetched_at": FETCHED_AT}
    started = time.perf_counter()
    m = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, net).metrics
    assert time.perf_counter() - started < 15.0  # about 3 s here
    assert m["node_count"] > 1200 and "betweenness_sampled" in m["quality_flags"]
    assert "graph_too_large" not in m["quality_flags"]  # the graph was built; only betweenness sampled
    assert m["components"] == 1 and m["truncated"] == 0 and m["hubs"]
    assert json.loads(json.dumps(m, allow_nan=False)) == m


def two_point_way(wid, a, b):
    return {"id": wid, "points": [lonlat(*a), lonlat(*b)], "tunnel": False}


def station_at(sid, x, y):
    lon, lat = lonlat(x, y)
    return {"id": sid, "name": f"Station {sid}", "kind": "station", "lon": lon, "lat": lat}


def comb_and_diagonals(size=4096):
    """A contract-valid network under both graph caps that was the heaviest before the station
    passes and run lengths were linear: anti-diagonal ways 3 px apart over one corner (runs whose
    bounding boxes are most of the window), a comb of 3,500 spurs 8 px long off spines 600 px
    apart, joined by one way (one junction tree), 2,000 stations on spur tips (510 in the built
    district, the rest in canopy: three quarters quiet, one long ghost line, every station next
    to every other), and a solid bar of 66 adjacent ways that the thinning peels for its full
    32 passes."""
    reach = 2 * size - 3600  # the diagonals x + y >= reach; the comb stays left of them
    edge = size - 3
    corner = range(reach, 2 * size - 20, 3)
    ways = [two_point_way(f"d{c}", (c - edge, edge), (edge, c - edge)) for c in corner]
    tips, spines = [], []
    for y in range(60, size - 60, 600):
        right = min(size - 20, reach - 60 - y)
        spurs = range(28, right - 8, 4)
        if len(spurs) < 50 or len(tips) >= 3500:
            break
        spines.append(y)
        ways.append(two_point_way(f"sp{y}", (20, y), (right, y)))
        for x in spurs[: 3500 - len(tips)]:
            ways.append(two_point_way(f"u{len(tips)}", (x, y), (x, y + 8)))
            tips.append((x, y + 8))
    ways.append(two_point_way("conn", (22, spines[0]), (22, spines[-1])))
    low, high = size // 4 + 100, 3 * size // 4 - 100
    inside = [(x, y) for x, y in tips if low <= x <= high and low <= y <= high]
    near = range(size // 4 - 100, 3 * size // 4 + 101)  # the district and a margin round it
    outside = [(x, y) for x, y in tips if not (x in near and y in near)]
    outside = [(x, y) for x, y in outside if not (y < size // 10 + 20 and x > 3 * size // 4 - 20)]
    picked = inside[:: max(1, len(inside) // 510)][:510]
    picked += outside[:: max(1, len(outside) // (2000 - len(picked)))][: 2000 - len(picked)]
    stations = [station_at(f"n{k}", x, y) for k, (x, y) in enumerate(picked)]
    bar = (spines[0] + spines[1]) // 2
    ways += [two_point_way(f"bar{r}", (100, bar + r), (size - 200, bar + r)) for r in range(66)]
    return {"ways": ways, "stations": stations, "pois": [], "fetched_at": FETCHED_AT}


def serpentine_and_bar(size=4096):
    """The heaviest contract-valid network measured after: one way back and forth across the
    window in 40 rows, 2,000 stations along it and 3,900 spurs 8 px long off it (9,807 nodes, one
    long line whose far stations give the orbital links their full set of trials), and the same
    66-way bar between its first two rows."""
    rows = [40 + i * (size - 80) // 39 for i in range(40)]
    points = []
    for i, y in enumerate(rows):
        ends = (40, size - 40) if i % 2 == 0 else (size - 40, 40)
        points += [lonlat(ends[0], y), lonlat(ends[1], y)]
    ways, stations = [{"id": "serp", "points": points, "tunnel": False}], []
    for k, (x, y) in enumerate([(x, y) for y in rows for x in range(60, size - 60, 20)][:5900]):
        if k % 2 == 0 and len(stations) < 2000:
            stations.append(station_at(f"n{len(stations)}", x, y))
        else:
            ways.append(two_point_way(f"u{k}", (x, y), (x, y + 8)))
    bar = rows[0] + 18
    ways += [two_point_way(f"bar{r}", (100, bar + r), (size - 100, bar + r)) for r in range(66)]
    return {"ways": ways, "stations": stations, "pois": [], "fetched_at": FETCHED_AT}


@pytest.mark.skipif(not os.environ.get("VISION_PERF"), reason="set VISION_PERF=1 to time the worst networks")
@pytest.mark.parametrize("make", [comb_and_diagonals, serpentine_and_bar])
def test_the_worst_networks_measured_stay_within_the_analysis_budget(make):
    """The whole pass with its figure, and the graph measures, flags and suggestions alone (the
    stage the quadratic station passes were in). On the machine of scene.MAX_WINDOW_PX: 20-22 s
    and 2.5-2.8 s for comb_and_diagonals (39 s and 17.5 s before), 24-26 s and 5.5-5.7 s for
    serpentine_and_bar; 30-42 s and 2.7-6.4 s with other test suites sharing its four cores (the
    thinning's memory traffic suffers most). So the whole pass is bounded by the worker's 60 s
    invoke, and the stage, which moves little with load, by a figure the quadratic passes miss."""
    size = 4096
    network = contract.validate_network(make(size))
    assert len(network["stations"]) == 2000 and len(network["ways"]) <= contract.NETWORK_LIMITS["ways"]
    bands = city(size)
    window = [[[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]]]
    started = time.perf_counter()
    analysis = analyse_rail_access(bands, window, PX, TRANSFORM, EPSG, network)
    png = annotate_rail(analysis, bands["red"], 2024)
    seconds = time.perf_counter() - started
    m = analysis.metrics
    stages = analysis.layers["timings"]
    print(f"{make.__name__} at {size} px: {m['node_count']} nodes, {seconds:.1f} s, stages (ms) {stages}")
    assert 9000 < m["node_count"] <= rail_analyse.MAX_GRAPH_NODES
    assert "graph_too_large" not in m["quality_flags"] and png.startswith(b"\x89PNG")
    if make is comb_and_diagonals:
        assert max(len(f["stations"]) for f in m["flags"] if f["type"] == "ghost_line") > 1000
    assert seconds < 60.0 and stages["metrics"] < 12_000.0


def test_a_wide_heat_blur_runs_on_a_reduced_grid_and_matches():
    bands = city()
    built, _, _ = urban.built_up_mask(bands["red"], bands["nir"], bands["swir16"], bands["scl"])
    share = (built > 0).astype(np.float32)
    for sigma in (50.0, 150.0):
        exact = cv2.GaussianBlur(share, (0, 0), sigma, borderType=cv2.BORDER_REPLICATE)
        fast = urban.heat_map(built, sigma)
        assert np.abs(fast - exact).max() < 0.01
        assert fast.shape == share.shape and fast.dtype == np.float32
    started = time.perf_counter()
    urban.heat_map(np.zeros((3000, 3000), np.uint8), 150.0)
    assert time.perf_counter() - started < 3.0


@pytest.mark.parametrize(
    "kwargs",
    [{"ndbi_threshold": 0.6}, {"ndvi_max": -0.1}, {"reach_m": 100}, {"heat_sigma_m": 5000}, {"snap_m": 10},
     {"hub_count": 0}, {"hub_count": 2.5}, {"intermodal_near_m": 600, "intermodal_far_m": 500},
     {"min_desert_km2": 0}, {"visibility_ndvi_max": 2}, {"max_orbital_km": 50}, {"reach_m": "1000"},
     {"hub_count": True}],
)  # fmt: skip
def test_bad_params_are_refused(kwargs):
    with pytest.raises(ValueError):
        RailParams(**kwargs)


def test_params_default_inside_their_limits():
    params = RailParams()
    for name, (low, high) in PARAM_LIMITS.items():
        assert low <= getattr(params, name) <= high
    assert RailParams(hub_count=20, intermodal_near_m=1000, intermodal_far_m=1000).hub_count == 20


def test_mismatched_bands_and_bad_pixel_size_are_refused():
    bands = city()
    bands["scl"] = bands["scl"][:-1]
    with pytest.raises(ValueError):
        analyse_rail_access(bands, WHOLE, PX, TRANSFORM, EPSG, grid_network())
    with pytest.raises(ValueError):
        analyse_rail_access(city(), WHOLE, 0, TRANSFORM, EPSG, grid_network())


# --- the figure ----------------------------------------------------------------------------------


CAPTION = [line.format(year="2024") for line in CAPTION_LINES]


def strip_height(width):
    """The rows the strip (the caption, then the legend) adds under a map `width` px wide."""
    return figure_strip(np.zeros((1, width, 3), np.uint8), CAPTION).shape[0]


def decode(png):
    return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)


def test_the_figure_is_a_png_within_the_budget_with_the_caption_under_the_map(analysis):
    png = annotate_rail(analysis, city()["red"], year="2024", max_side=256)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) <= FIGURE_BYTE_BUDGET <= MAX_IMAGE_BYTES
    image = decode(png)
    assert image.shape == (256 + strip_height(256), 256, 3) and strip_height(256) > 0
    # The caption is at the strip's top-left, white on black; the map's corner is the scene.
    assert image[256 + 2, 2].tolist() == [0, 0, 0] and count_white(image[256 : 256 + 30, :150]) > 50
    assert image[2, 2].tolist() != [0, 0, 0] and image[1, 250].tolist() != [0, 0, 0]
    # The cloud is tinted, the district carries the heat map's warm colours.
    cloud = image[int(55 * 0.64), int(340 * 0.64)]
    district = image[int(150 * 0.64), int(250 * 0.64)]
    assert cloud[0] > cloud[2] and district[2] > district[0]
    png = annotate_rail(analysis, city()["red"], 2024)
    assert decode(png).shape == (SIZE + strip_height(SIZE), SIZE, 3)
    with pytest.raises(ValueError):
        annotate_rail(analysis, city()["red"][:-1], 2024)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def figure(analysis, max_side=1024, layers=None, **metrics):
    """The figure of `analysis` decoded to BGR, with `metrics` entries replacing its metrics' (and
    `layers` entries its layers'); the fixture is never changed. The PNG must fit the pipeline's
    image cap and render the same twice."""
    variant = RailAnalysis(
        metrics={**analysis.metrics, **metrics}, layers={**analysis.layers, **(layers or {})}
    )
    png = annotate_rail(variant, city()["red"], "2024", max_side=max_side)
    assert png[:8] == PNG_SIGNATURE and len(png) <= FIGURE_BYTE_BUDGET
    assert annotate_rail(variant, city()["red"], "2024", max_side=max_side) == png
    return decode(png)


def near(pixel, colour, tolerance=40):
    return all(abs(int(p) - c) <= tolerance for p, c in zip(pixel, colour, strict=True))


def white(pixel, floor=200):
    return min(int(v) for v in pixel) >= floor


def test_a_flagged_station_gets_a_red_outer_ring(analysis):
    # The city flags Central (a hub, drawn at radius 6) as its single point of failure: a red ring
    # of radius 10, outside the hub marker and over the lines that meet there.
    image = figure(analysis)
    assert near(image[200, 210], FLAG_COLOUR) and near(image[190, 200], FLAG_COLOUR)
    # West (radius 2) is not flagged, so there is nothing red at radius 6 above it ...
    assert not near(image[194, 40], FLAG_COLOUR)
    # ... until a flag names it.
    flag = {"type": "isolated_high_demand", "station": "s:n1", "name": "West", "activity": 0.0336,
            "degree": 1, "isolation_weight": 32.0}  # fmt: skip
    image = figure(analysis, flags=[*analysis.metrics["flags"], flag])
    assert near(image[194, 40], FLAG_COLOUR)
    assert near(image[200, 210], FLAG_COLOUR)  # Central keeps its own


def test_the_top_quarter_of_isolation_weight_gets_a_thicker_ring(analysis):
    # East (weight 62.8) is in the top quarter and West (32.0) is not. Both are drawn at radius 6
    # here, so the ring's inner pixel is clear of the lines that meet them: a thick ring fills it,
    # a thin ring leaves only its anti-aliased fringe.
    stations = analysis.metrics["stations"]
    busy = [{**s, "activity": 1.0} if s["id"] in ("s:n1", "s:n3") else s for s in stations]
    image = figure(analysis, stations=busy)
    assert white(image[194, 360], 180) and white(image[195, 360])
    assert white(image[194, 40], 180) and not white(image[195, 40], 120)
    # When every weight is the same there is no top quarter, and every ring is thin.
    alike = [{**s, "isolation_weight": 1.0} for s in busy]
    image = figure(analysis, stations=alike)
    assert white(image[194, 360], 180) and not white(image[195, 360], 120)


def green_pixels(image, rows, cols):
    region = image[rows, cols].astype(int)
    return int(((region[..., 1] > 180) & (region[..., 0] < 140) & (region[..., 2] < 140)).sum())


def test_suggested_links_are_dashed_in_green_and_a_feeder_corridor_is_thinner(analysis):
    # Branch End (340, 120) to East (360, 200): over dark ground, away from both hub markers.
    link = {"type": "orbital_link", "from": "s:n6", "to": "s:n3", "from_name": "Branch End",
            "to_name": "East", "straight_km": 1.65, "delta_top_hub_betweenness": -0.1,
            "delta_mean_d_hub_m": -100.0}  # fmt: skip
    image = figure(analysis, suggestions=[link])
    along = np.linspace(0.15, 0.85, 60)
    on = [near(image[round(120 + 80 * t), round(340 + 20 * t)], SUGGESTION_COLOUR, 60) for t in along]
    assert 15 <= sum(on) <= 50  # dashes, and gaps between them
    feeder = {"type": "feeder_corridor", "station": "s:n6", "name": "Branch End", "hub": "s:n3",
              "hub_name": "East", "length_m": 1650.0}  # fmt: skip
    thin = figure(analysis, suggestions=[feeder])
    rows, cols = slice(135, 185), slice(330, 370)
    assert green_pixels(image, rows, cols) > green_pixels(thin, rows, cols) > 0
    assert green_pixels(figure(analysis, suggestions=[]), rows, cols) == 0


def test_a_ghost_line_is_a_red_polyline_through_its_stations(analysis):
    # West, Midwest (3 px off its line) and Central in order; a station without a position is
    # left out and the line still runs through the others.
    ghost = {"type": "ghost_line", "stations": ["s:n1", "s:far", "s:n7", "s:n2"],
             "names": ["West", "Far", "Midwest", "Central"]}  # fmt: skip
    image = figure(analysis, flags=[ghost])
    for x in (80, 160):  # the midpoints of the two legs, at y 201.5
        assert any(near(image[y, x], FLAG_COLOUR) for y in (201, 202))
    plain = figure(analysis, flags=[])
    assert not any(near(plain[y, x], FLAG_COLOUR) for y in (201, 202) for x in (80, 160))


def test_odd_flags_and_suggestions_draw_nothing_and_the_figure_still_renders(analysis):
    far = {**analysis.metrics["stations"][0], "id": "s:far", "name": "Far", "activity": None,
           "isolation_weight": None}  # fmt: skip
    stations = [*analysis.metrics["stations"], far]
    layers = {"stations_px": [*analysis.layers["stations_px"], None]}
    flags = [
        {"type": "isolated_high_demand", "station": "s:far"},  # no position
        {"type": "ghost_line", "stations": ["s:far", "s:n1"]},  # one point left
        {"type": "single_point_of_failure", "station": "s:unknown"},
    ]
    suggestions = [
        {"type": "orbital_link", "from": "s:nowhere", "to": "s:n3"},
        {"type": "feeder_corridor", "station": "s:n3", "hub": "s:nowhere"},
        {"type": "mystery"},
    ]
    odd = figure(analysis, layers=layers, stations=stations, flags=flags, suggestions=suggestions)
    plain = figure(analysis, flags=[], suggestions=[])
    assert odd.shape == plain.shape == (SIZE + strip_height(SIZE), SIZE, 3)
    assert np.array_equal(odd, plain)
    assert figure(analysis, max_side=256, flags=[], suggestions=[]).shape == (256 + strip_height(256), 256, 3)


@pytest.fixture(scope="module")
def big_city():
    """The review's case: the city upscaled to 1024 px (the worker's max_side, so the figure is
    not scaled), its pixels 400/1024 as large, the same network. The analysis and its red band."""
    side = 1024
    bands = {
        name: cv2.resize(band, (side, side), interpolation=cv2.INTER_NEAREST) for name, band in city().items()
    }
    px = PX * SIZE / side
    transform = (TRANSFORM[0], px, 0.0, TRANSFORM[3], 0.0, -px)
    window = [[[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]]]
    return analyse_rail_access(bands, window, px, transform, EPSG, grid_network()), bands["red"]


def count_near(region, colour, tolerance=40):
    return int((np.abs(region.astype(int) - colour).max(axis=2) <= tolerance).sum())


def count_white(region, floor=200):
    return int((region.min(axis=2) >= floor).sum())


@pytest.mark.parametrize("which", ["synthetic", "upscaled"])
def test_the_caption_and_legend_go_under_the_map_and_hide_nothing(which, analysis, big_city, monkeypatch):
    base, red = (analysis, city()["red"]) if which == "synthetic" else big_city
    side = red.shape[1]
    # Every station flagged, so each has a flag ring, and no suggestions: their green is the hubs'.
    flags = [{"type": "isolated_high_demand", "station": s["id"]} for s in base.metrics["stations"]]
    flagged = RailAnalysis(metrics={**base.metrics, "flags": flags, "suggestions": []}, layers=base.layers)
    png = annotate_rail(flagged, red, "2024")
    assert len(png) <= FIGURE_BYTE_BUDGET
    image = decode(png)
    strip = figure_strip(image[:side], CAPTION)
    with monkeypatch.context() as patch:  # the strip disabled: the map alone
        patch.setattr("vision.rail_annotate.figure_strip", lambda image, caption: image[:0])
        map_only = decode(annotate_rail(flagged, red, "2024"))
    assert map_only.shape == (side, side, 3)
    # The image grew by exactly the strip, and the caption and legend changed no pixel of the map.
    assert image.shape == (side + strip.shape[0], side, 3)
    assert np.array_equal(image[:side], map_only) and np.array_equal(image[side:], strip)
    background = (strip == CAPTION_BACKGROUND).all(axis=2)
    assert background[0].all() and background[:, -1].all() and background.mean() > 0.8
    # A strip with the caption alone (a figure too narrow for a legend) leaves the map as it is too.
    monkeypatch.setattr("vision.rail_annotate.LEGEND_MIN_WIDTH", side + 1)
    narrow = decode(annotate_rail(flagged, red, "2024"))
    assert narrow.shape == (side + caption_bar_size(map_only, CAPTION)[1], side, 3)
    assert np.array_equal(narrow[:side], map_only)
    # Every station's symbol and flag ring shows at its position, North (200, 40) on the 400 px city
    # too, which the caption bar hid when it was burned into the map. A flagged hub's ring clears
    # its marker: on a quiet hub (radius 2) the marker hid all but 12 px.
    hubs = set(base.layers["hubs"])
    for station, position in zip(base.metrics["stations"], base.layers["stations_px"], strict=True):
        x, y = round(position[0]), round(position[1])  # neither city is scaled
        patch = image[y - 12 : y + 13, x - 12 : x + 13]
        assert count_near(patch, FLAG_COLOUR) >= 60, station["id"]
        if station["id"] in hubs:
            assert count_near(patch, HUB_COLOUR) >= 20, station["id"]
        else:
            assert count_white(patch) >= 4, station["id"]


def test_the_caption_is_burned_into_the_strip_as_the_ships_figure_burns_it():
    for width in (200, LEGEND_MIN_WIDTH, 400, 1024, 2000):
        canvas = np.zeros((1, width, 3), np.uint8)
        strip, legend = figure_strip(canvas, CAPTION), legend_strip(canvas)
        bar = np.zeros((strip.shape[0], width, 3), np.uint8)  # CAPTION_BACKGROUND
        burn_caption(bar, CAPTION)
        height = caption_bar_size(canvas, CAPTION)[1]
        assert np.array_equal(strip[:height], bar[:height]), width
        if legend is None:  # too narrow for a legend: the caption alone
            assert width < LEGEND_MIN_WIDTH and strip.shape[0] == height
            continue
        # A rule under the caption, then the legend.
        assert strip.shape[0] == height + 1 + legend.shape[0], width
        assert (strip[height, CAPTION_PAD : width - CAPTION_PAD] == RULE_COLOUR).all()
        assert np.array_equal(strip[height + 1 :], legend)


def glyph_centres(strip):
    """The (x, y) centres of the flag's red glyph and the desert's amber one in a legend strip."""
    centres = []
    for colour in (FLAG_COLOUR, DESERT_COLOUR):
        ys, xs = np.nonzero(np.abs(strip.astype(int) - colour).max(axis=2) <= 60)
        centres.append((xs.mean(), ys.mean()))
    return centres


def test_the_legend_lays_its_entries_out_in_as_many_columns_as_fit():
    assert legend_strip(np.zeros((1, LEGEND_MIN_WIDTH - 1, 3), np.uint8)) is None
    strips = {width: legend_strip(np.zeros((320, width, 3), np.uint8)) for width in (256, 400, 1024, 3000)}
    for width, strip in strips.items():
        assert strip.shape[1:] == (width, 3)
        assert (strip[:, -2:] == CAPTION_BACKGROUND).all()  # no words cut off at the right edge
        for colour in (FLAG_COLOUR, SUGGESTION_COLOUR, DESERT_COLOUR, (255, 255, 255)):
            assert count_near(strip, colour, 60), (width, colour)  # every symbol is drawn
    # Row by row: station, hub, flag, suggestion, desert. One column at 256 px ...
    (flag_x, flag_y), (desert_x, desert_y) = glyph_centres(strips[256])
    assert flag_x < 40 and desert_x < 40 and flag_y < desert_y
    # ... two at 400 (the flag starts the second row, the desert the third) ...
    (flag_x, flag_y), (desert_x, desert_y) = glyph_centres(strips[400])
    assert flag_x < 40 and desert_x < 40 and flag_y < desert_y and strips[400].shape[0] < strips[256].shape[0]
    # ... and three from 485 px up: the flag ends the first row, the desert is second in the second.
    for width in (1024, 3000):
        strip = strips[width]
        (flag_x, flag_y), (desert_x, desert_y) = glyph_centres(strip)
        assert flag_x > width / 2 and width / 5 < desert_x < flag_x and flag_y < strip.shape[0] / 2 < desert_y
    assert strips[1024].shape[0] < 100  # two rows of words at the caption's 1024 px scale


def test_the_legend_never_loses_a_column_as_the_figure_widens(monkeypatch):
    # The glyphs' centres, recorded as the legend draws them: one distinct x per column.
    centres = []
    draw = rail_annotate._legend_glyph

    def recording(image, kind, centre, size):
        centres.append(centre[0])
        return draw(image, kind, centre, size)

    monkeypatch.setattr(rail_annotate, "_legend_glyph", recording)
    counts = []
    for width in range(LEGEND_MIN_WIDTH, 3001, 8):
        centres.clear()
        strip = legend_strip(np.zeros((1, width, 3), np.uint8))
        assert (strip[:, -2:] == CAPTION_BACKGROUND).all(), width  # and no words cut off
        counts.append(len(set(centres)))
    assert counts == sorted(counts) and counts[0] == 1 and counts[-1] == 3


def test_a_wide_short_figure_has_a_legend(analysis):
    # 1024 x 320: the old legend, kept to a quarter of the height, did not fit; the strip does.
    def wide(layer, interpolation):
        return cv2.resize(layer, (1024, 320), interpolation=interpolation)

    layers = {
        **analysis.layers,
        "heat": wide(analysis.layers["heat"], cv2.INTER_LINEAR),
        "usable": wide(analysis.layers["usable"], cv2.INTER_NEAREST),
        "desert": wide(analysis.layers["desert"], cv2.INTER_NEAREST),
    }
    variant = RailAnalysis(metrics=analysis.metrics, layers=layers)
    image = decode(annotate_rail(variant, wide(city()["red"], cv2.INTER_NEAREST), "2024"))
    strip = figure_strip(image[:320], CAPTION)
    assert image.shape == (320 + strip.shape[0], 1024, 3) and np.array_equal(image[320:], strip)
    assert legend_strip(image) is not None and count_near(strip, FLAG_COLOUR, 60)  # the legend
    assert image[320 + 2, 2].tolist() == [0, 0, 0] and count_white(image[320 : 320 + 30, :300]) > 100


def test_the_figure_draws_a_city_with_no_graph_and_one_with_sampled_betweenness(monkeypatch):
    red = city()["red"]
    with monkeypatch.context() as patch:
        patch.setattr("vision.rail_analyse.MAX_GRAPH_NODES", 5)
        bare = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, grid_network())
    assert "graph_too_large" in bare.metrics["quality_flags"] and bare.metrics["node_count"] == 0
    assert bare.layers["graph"].number_of_nodes() == 0 and bare.layers["hubs"] == []
    image = decode(annotate_rail(bare, red, "2024"))
    assert image.shape == (SIZE + strip_height(SIZE), SIZE, 3)
    # The stations are still drawn where they are; there is no hub, flag or link to draw.
    for x, y in ((round(x), round(y)) for x, y in bare.layers["stations_px"]):
        assert count_white(image[y - 8 : y + 9, x - 8 : x + 9]) >= 4, (x, y)
    assert count_near(image[:SIZE], HUB_COLOUR) == count_near(image[:SIZE], FLAG_COLOUR) == 0
    with monkeypatch.context() as patch:
        patch.setattr("vision.rail_analyse.BETWEENNESS_WORK", 1)
        sampled = analyse_rail_access(city(), WHOLE, PX, TRANSFORM, EPSG, grid_network())
    assert "betweenness_sampled" in sampled.metrics["quality_flags"] and sampled.layers["hubs"]
    image = decode(annotate_rail(sampled, red, "2024"))
    assert image.shape == (SIZE + strip_height(SIZE), SIZE, 3)
    ids = (station["id"] for station in sampled.metrics["stations"])
    positions = dict(zip(ids, sampled.layers["stations_px"], strict=True))
    for hub in sampled.layers["hubs"]:
        x, y = round(positions[hub][0]), round(positions[hub][1])
        assert count_near(image[y - 9 : y + 10, x - 9 : x + 10], HUB_COLOUR) >= 20, hub


def test_the_dashed_line_leaves_gaps():
    for thickness, lit in ((2, (3, 7)), (1, (4, 7))):
        canvas = np.zeros((5, 60), np.uint8)
        _dashed_line(canvas, (2, 2), (57, 2), 255, thickness)
        on = canvas[2] >= 128
        assert on[5] and not on[10]  # the first dash's middle, the first gap's middle
        assert lit[0] <= on[:10].sum() <= lit[1]  # about 6 of every 10 px, caps and all
        assert 25 <= on.sum() <= 42
        assert canvas[0].max() < 128 and canvas[4].max() < 128  # an anti-aliased fringe at most
    canvas = np.zeros((5, 5), np.uint8)
    _dashed_line(canvas, (2, 2), (2, 2), 255, 2)
    assert not canvas.any()


def test_the_dashed_line_is_clipped_to_the_image_first(monkeypatch):
    drawn = []
    line = cv2.line

    def counting_line(image, p, q, *args):
        drawn.append((p, q))
        return line(image, p, q, *args)

    monkeypatch.setattr(cv2, "line", counting_line)
    canvas = np.zeros((5, 60), np.uint8)
    started = time.perf_counter()
    _dashed_line(canvas, (2, 2), (10**7, 2), 255, 2)  # a million dashes, six of them in the image
    assert time.perf_counter() - started < 0.5
    assert 6 <= len(drawn) <= 9 and all(p[0] < 75 and q[0] < 75 for p, q in drawn)
    # The same dashes, counted from the same end, as a line that ends just past the image.
    short = np.zeros_like(canvas)
    _dashed_line(short, (2, 2), (200, 2), 255, 2)
    assert canvas.any() and np.array_equal(canvas, short)
    # From far outside in: only the dashes near the image are drawn, none past the end.
    drawn.clear()
    canvas = np.zeros((5, 60), np.uint8)
    _dashed_line(canvas, (-(10**7), 2), (40, 2), 255, 2)
    assert len(drawn) <= 6 and canvas[:, 2:38].any() and not canvas[:, 41:].any()
    drawn.clear()
    _dashed_line(canvas, (-500, -50), (500, -40), 255, 2)  # wholly outside
    assert drawn == []
    with pytest.raises(ValueError):
        _dashed_line(canvas, (2, 2), (57, 2), 255, 2, on=0, off=0)


def crowded_city(analysis, side=1024, count=2000, seed=0):
    """As many bytes as a figure gets: 2,000 stations (the metrics' cap) each joined to its two
    nearest neighbours by short edges, 400 flags, 10 suggestions and 10 hubs, over a red band of
    uniform noise across the stretch's whole range (maximum entropy) on a `side` px grid that is
    usable everywhere. The analysis and its red band."""
    rng = np.random.default_rng(seed)
    red = rng.integers(0, int(STRETCH_MAX) + 1, (side, side)).astype(np.uint16)
    xs, ys = rng.uniform(0, side - 1, count), rng.uniform(0, side - 1, count)
    squared = (xs[:, None] - xs[None, :]) ** 2 + (ys[:, None] - ys[None, :]) ** 2
    np.fill_diagonal(squared, np.inf)
    nearest = np.argpartition(squared, 2, axis=1)[:, :2]
    graph, stations = nx.Graph(), []
    for i in range(count):
        graph.add_node(f"s:{i}", kind="station", x=float(xs[i]), y=float(ys[i]), station=str(i))
        stations.append({"id": f"s:{i}", "name": f"S{i}", "activity": float(rng.uniform(0, 1)),
                         "isolation_weight": float(rng.uniform(0, 100))})  # fmt: skip
    pairs = sorted({(min(i, int(j)), max(i, int(j))) for i in range(count) for j in nearest[i]})
    edges = []
    for i, j in pairs:
        graph.add_edge(f"s:{i}", f"s:{j}", length_m=100.0)
        edges.append({"from": f"s:{i}", "to": f"s:{j}", "betweenness": float(rng.uniform(0, 1))})
    flags = [{"type": "isolated_high_demand", "station": f"s:{i}"} for i in range(200)]
    flags += [{"type": "single_point_of_failure", "station": f"s:{i}"} for i in range(200, 390)]
    flags += [
        {"type": "ghost_line", "stations": [f"s:{j}" for j in range(k, k + 5)]} for k in range(400, 450, 5)
    ]
    suggestions = [{"type": "orbital_link", "from": f"s:{i}", "to": f"s:{i + 1000}"} for i in range(5)]
    suggestions += [
        {"type": "feeder_corridor", "station": f"s:{i}", "hub": f"s:{i + 500}"} for i in range(5, 10)
    ]
    assert (len(stations), len(flags), len(suggestions)) == (2000, 400, 10)
    layers = {
        **analysis.layers,
        "heat": cv2.resize(analysis.layers["heat"], (side, side), interpolation=cv2.INTER_LINEAR),
        "usable": np.ones((side, side), np.uint8),
        "desert": cv2.resize(analysis.layers["desert"], (side, side), interpolation=cv2.INTER_NEAREST),
        "graph": graph,
        "stations_px": list(zip(xs, ys, strict=True)),
        "hubs": [f"s:{i}" for i in range(10)],
    }
    metrics = {**analysis.metrics, "stations": stations, "edges": edges, "flags": flags}
    metrics["suggestions"] = suggestions
    return RailAnalysis(metrics=metrics, layers=layers), red


def test_a_city_sized_figure_with_every_flag_and_suggestion_stays_within_the_byte_budget(analysis):
    """The worst case for bytes: the PNG must fit FIGURE_BYTE_BUDGET, and so the pipeline's cap,
    at the worker's max_side. It does not at a 1024 px map, so it is drawn again smaller."""
    side = 1024
    crowded, red = crowded_city(analysis, side)
    started = time.perf_counter()
    png = annotate_rail(crowded, red, 2024)
    assert time.perf_counter() - started < 10.0  # about a second here: two draws and encodes
    assert len(png) <= FIGURE_BYTE_BUDGET <= MAX_IMAGE_BYTES
    image = decode(png)
    width = image.shape[1]
    assert FIGURE_MIN_SIDE < width < side and image.shape == (width + strip_height(width), width, 3)


def test_a_figure_over_the_byte_budget_is_drawn_again_smaller(analysis, big_city, monkeypatch):
    big, red = big_city
    full = annotate_rail(big, red, "2024")
    assert decode(full).shape == (1024 + strip_height(1024), 1024, 3)
    # One byte short: drawn again at 0.8x, the strip laid out again for the narrower map.
    monkeypatch.setattr("vision.rail_annotate.FIGURE_BYTE_BUDGET", len(full) - 1)
    smaller = annotate_rail(big, red, "2024")
    assert len(smaller) < len(full) and smaller[:8] == PNG_SIGNATURE
    assert decode(smaller).shape == (819 + strip_height(819), 819, 3)
    # Nothing fits: shrunk to the floor (819, 655, 524, then 512 px) and returned over the budget.
    monkeypatch.setattr("vision.rail_annotate.FIGURE_BYTE_BUDGET", 1)
    floor = annotate_rail(big, red, "2024")
    image = decode(floor)
    assert image.shape == (FIGURE_MIN_SIDE + strip_height(FIGURE_MIN_SIDE), FIGURE_MIN_SIDE, 3)
    assert count_near(image[:FIGURE_MIN_SIDE], FLAG_COLOUR) > 0  # Central's flag ring, drawn again
    # A map already under the floor is never shrunk.
    small = decode(annotate_rail(analysis, city()["red"], "2024"))
    assert small.shape == (SIZE + strip_height(SIZE), SIZE, 3)
