"""One city, one scene, one mapped network: the rail access pass, and the metrics the adapter
diffs.

The order is the data's: site mask, built-up mask and coverage, the heat map, the network
projected into the scene's pixels, distances to stations (served area, catchments), distances
to the bus, tram and ferry stops (the deserts and what the stops cover of them, reach by any
mode, each station's feeder coverage), the ways rasterised, thinned and read back as a graph,
then the graph measures (betweenness, hubs, distance to the nearest hub, an isolation weight
and its multimodal form), the station flags and the theoretical links with what their straight
lines cross. Everything in `metrics` is JSON-able (no NaN or infinity), rounded, and carries
the parameters it was made with. What was not measured is said: coverage, each station's
catchment coverage, snapped stations, graph components, truncated lists and the quality flags,
never a quiet zero; a network without a stops layer gets None for every stop measure and the
flag no_stops_layer, where an empty layer measures as nothing within reach. networkx is
imported here and in network.py, both worker-only.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import networkx as nx
import numpy as np

from vision import access, geo, masks, urban
from vision import network as rail_network

# Bounds of every parameter, the contract's rail limits (common/vision_contract.py holds a copy).
PARAM_LIMITS = {
    "ndbi_threshold": (-0.5, 0.5),
    "ndvi_max": (0.0, 1.0),
    "reach_m": (200.0, 5000.0),
    "heat_sigma_m": (100.0, 3000.0),
    "snap_m": (20.0, 1000.0),
    "hub_count": (1, 20),
    "intermodal_near_m": (50.0, 1000.0),
    "intermodal_far_m": (100.0, 2000.0),
    "min_desert_km2": (0.01, 100.0),
    "visibility_ndvi_max": (0.0, 1.0),
    "max_orbital_km": (1.0, 20.0),
    "stop_reach_m": (100.0, 2000.0),
    "feeder_reach_m": (500.0, 10000.0),
}
WHOLE_NUMBER_PARAMS = ("hub_count",)
# The modes the stop measures are read for: the stops layer's bus and tram (the contract's
# STOP_MODES, bare [lon, lat] points) and the ferry terminals among the points of interest.
MODES = ("bus", "tram", "ferry")
# A theoretical link whose straight line has at least this share of its pixels over water is
# "mostly over water": reported after the dry links and dashed in its own colour on the map.
MOSTLY_WATER = 0.5
# Caps on the lists in the metrics, so a reply stays a few hundred KB for the largest city.
MAX_STATIONS, MAX_EDGES, MAX_DESERTS = 2000, 5000, 10
# Betweenness costs one Dijkstra pass per source node, each about (nodes + edges) steps, and a
# pass makes up to five calls (edge and node betweenness, then node betweenness on up to three
# simulated links). BETWEENNESS_WORK bounds sources x (nodes + edges) per call: a graph with
# nodes x (nodes + edges) within it is exact; a larger one samples WORK // (nodes + edges) seeded
# source nodes (at least MIN_BETWEENNESS_SAMPLES, at most BETWEENNESS_SAMPLES; the fixed seed makes
# two runs agree), and the metrics say so with the quality flag betweenness_sampled. Measured on a
# 4-core Intel Xeon at 2.1 GHz (networkx 3.4.2): one source costs 1.6 to 1.9 us per (nodes +
# edges); with the old fixed 200 sources the five calls took 22.6 s on a grid of n=4,692 m=9,247
# and 53.9 s on n=10,000 m=19,800. With 600,000 they take 6.3 s on n=4,761 m=9,384 (k=42), 6.3 s
# on n=10,000 m=19,800 (k=20) and 7.2 s on 50 joined lines of n=10,000 m=10,048 (k=29).
BETWEENNESS_WORK = 600_000
MIN_BETWEENNESS_SAMPLES, BETWEENNESS_SAMPLES, BETWEENNESS_SEED = 20, 200, 0
# A skeleton with more node clusters, or more runs between them, than this (a pathological network:
# a city has a few hundred to a few thousand nodes) is not made a graph at all (network.build_graph's
# max_nodes, checked on the labelled pixels before any networkx object exists): the stations are
# reported without degree, betweenness or hub distance, the hubs, flags and suggestions are empty and
# the quality flag graph_too_large says so. Just under the cap (9,000-9,800 nodes), on one core of
# the Intel Xeon above (scene.MAX_WINDOW_PX lists the networks and their totals; their ways reach the
# window's edges): the graph stage (rasterise, thin, snap, build_graph) takes 6.1-18.4 s at 4096 px
# and 3.5-9.2 s at 3,584 px, the rail task's window cap (vision_worker_handler.RAIL_MAX_WINDOW_PX).
# build_graph's passes over the whole window take 3.8-6.8 s and 2.1-4.7 s of it; the thinning 0.4-0.9 s
# and 0.2-0.4 s with the thick parts in one place, but 8.9-10.2 s and 2.0-4.3 s with them at both ends
# of the window, where the region network.thin keeps working on is most of it on every pass. The
# measures, flags and suggestions take 0.2-7.9 s at either size, of which station_adjacency up to
# 1.0 s for 2,000 stations on one junction tree and the simulated links up to 3.7 s. The whole pass
# with its figure, through lambda_handler: at most 37.5 s and 1.69 GB at 4096 px, at most 25.5 s and
# 1.32 GB at 3,584 px. Before the check (the cap was 20,000 and only skipped the measures), a
# 2,632-point mesh of crossing ways on a 3,300 px window made 435,596 nodes: 20 s and 1.4 GB in
# build_graph, 55 s for the pass (four cores). The stops stage (access.distance_to_points over
# every stop, then one transform per mode, no labels) with a layer at the contract's cap of 40,000
# points adds 1.0-1.3 s at 3,584 px: the same two networks' whole pass with both figures took
# 14.0 s (comb_and_diagonals) and 15.3 s (serpentine_and_bar) on the four cores, at a peak of
# 1.24 GB RSS in the test process (tests/test_vision_rail_core.py's VISION_PERF run).
MAX_GRAPH_NODES = 10_000
# A station's activity is floored here in the isolation weight, so an empty catchment far from
# a hub is "very isolated" rather than infinitely so.
MIN_ACTIVITY = 0.05
# A simulated orbital link is this much longer than the straight line between its stations.
ORBITAL_DETOUR = 1.2
# How many candidate pairs are simulated, and how many of each kind of suggestion are kept.
MAX_ORBITAL_CANDIDATES, TOP_SUGGESTIONS = 12, 3
# Where a lon/lat may be for the network to use it: on Earth, and inside the UTM band.
LON_RANGE, LAT_RANGE = (-180.0, 180.0), (-80.0, 84.0)


@dataclass(frozen=True)
class RailParams:
    """What the pass measures with. Distances are metres; the pixel size converts them."""

    # NDBI above this is built up; 0 is the index's own sign change (SWIR brighter than NIR).
    ndbi_threshold: float = 0.0
    # NDVI at or above this is vegetation, built up or not.
    ndvi_max: float = 0.3
    # Walking reach of a station, straight-line.
    reach_m: float = 1000.0
    # The heat map's Gaussian sigma: a neighbourhood's share of built-up ground.
    heat_sigma_m: float = 500.0
    # How far a station may sit from the drawn line and still be on it (OSM station nodes are
    # often on the platform, the way on the track).
    snap_m: float = 300.0
    # How many stations, by betweenness among the busier half, count as hubs.
    hub_count: int = 5
    # Bus stops and bus stations within this radius of a station are its intermodal links...
    intermodal_near_m: float = 300.0
    # ...ferry terminals and park-and-rides within this one.
    intermodal_far_m: float = 500.0
    # A built-up patch beyond reach is reported as a desert from this size up.
    min_desert_km2: float = 0.5
    # A corridor pixel with NDVI below this shows bare ground or track, not canopy.
    visibility_ndvi_max: float = 0.35
    # Orbital links are only simulated between stations this close in a straight line.
    max_orbital_km: float = 5.0
    # Walking reach of a bus, tram or ferry stop, straight-line: the usual 400 m (five minutes
    # at the contract's WALK_M_PER_MIN) of the stop-spacing rule of thumb.
    stop_reach_m: float = 400.0
    # How far beyond a station's own reach its hinterland is read for feeder coverage: the
    # built-up ground between reach_m and this, in the station's cell, that a feeder would serve.
    feeder_reach_m: float = 2000.0

    def __post_init__(self):
        for name, (low, high) in PARAM_LIMITS.items():
            value = getattr(self, name)
            if not isinstance(value, int | float) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        for name in WHOLE_NUMBER_PARAMS:
            if getattr(self, name) != int(getattr(self, name)):
                raise ValueError(f"{name} must be a whole number")
        if self.intermodal_far_m < self.intermodal_near_m:
            raise ValueError("intermodal_far_m must be at least intermodal_near_m")
        if self.feeder_reach_m < self.reach_m:
            raise ValueError("feeder_reach_m must be at least reach_m")


@dataclass
class RailAnalysis:
    """`metrics` is what the worker returns (JSON-able). `layers` are the arrays the figures are
    drawn from (`heat`, `built`, `desert`, `unusable`, `water`, `usable`, `skeleton` and
    `stop_reach`, a uint8 0/255 mask that is all zeros without a stops layer), the `graph`, the
    projected `stations_px`, the `hubs` and the stage `timings` (ms); never sent."""

    metrics: dict
    layers: dict


def project_network(network: dict, epsg: int, transform: Sequence[float], shape: tuple[int, int]) -> dict:
    """The network's lon/lat geometry in the scene: `ways_px` and `tunnels_px` (float (n, 2)
    pixel arrays, surface and tunnel ways apart), `stations` (the station dicts with `x`, `y`
    pixel coordinates, `easting`, `northing` and `inside` the image), `stations_px` (an (x, y)
    per station, None outside the image), `stations_xy_m`, `pois_xy_m`, `poi_kinds`,
    `stops_px`, `stops_xy_m` and `warnings`. Pixel coordinates are of pixel centres, so
    rounding them gives the pixel index. A vertex, station, point of interest or stop whose
    coordinates are not finite or not on Earth is skipped and counted in a warning, never a
    crash; a repeated station id keeps one entry (the first inside the window, else the first)
    and is counted in a warning too.

    With a `stops` layer in the network, `stops_px` is {mode: [(x, y) | None]} and `stops_xy_m`
    {mode: (n, 2) UTM array} for every mode in MODES: bus and tram from the layer (a mode the
    layer lacks is empty), ferry from the pois of kind ferry_terminal. The layer's bus stops are
    appended to the POI arrays as kind bus_stop, and the pois of that kind are then left out,
    so `access.intermodal_counts` counts each stop once whichever way it came: a network cached
    before the layer existed carries its bus stops in `pois` and no `stops`, and that old path
    still counts. Without the layer both are None: the stops were not measured."""
    zone, south = geo.utm_zone_from_epsg(epsg)
    h, w = shape
    skipped = {"way vertices": 0, "stations": 0, "points of interest": 0, "bus stops": 0, "tram stops": 0}

    def project(points, what):
        rows = []
        for point in points:
            lon, lat = _lonlat_of(point)
            if lon is None:
                skipped[what] += 1
                continue
            e, n = geo.lonlat_to_utm(lon, lat, zone, south)
            col, row = geo.map_to_pixel(transform, e, n)
            rows.append((col - 0.5, row - 0.5, e, n))
        return np.asarray(rows, np.float64).reshape(-1, 4)

    ways_px, tunnels_px = [], []
    for way in network.get("ways", ()):
        points = project(way.get("points", ()), "way vertices")[:, :2]
        if len(points) < 2:
            continue
        (tunnels_px if way.get("tunnel") else ways_px).append(points)

    stations, stations_px, stations_xy = [], [], []
    index_of_id: dict[str, int] = {}
    repeated = 0
    for station in network.get("stations", ()):
        projected = project([(station.get("lon"), station.get("lat"))], "stations")
        if not len(projected):
            continue
        x, y, e, n = projected[0]
        inside = 0 <= round(x) < w and 0 <= round(y) < h
        entry = {**station, "x": x, "y": y, "easting": e, "northing": n, "inside": inside}
        sid = rail_network.station_node_id(station)
        if sid in index_of_id:
            repeated += 1
            kept = index_of_id[sid]
            if inside and not stations[kept]["inside"]:  # the first entry inside the window wins
                stations[kept], stations_px[kept], stations_xy[kept] = entry, (x, y), (e, n)
            continue
        index_of_id[sid] = len(stations)
        stations.append(entry)
        stations_px.append((x, y) if inside else None)
        stations_xy.append((e, n))

    stops = network.get("stops")
    layered = isinstance(stops, dict)
    pois_xy, poi_kinds, ferries = [], [], []
    for poi in network.get("pois", ()):
        kind = str(poi.get("kind", ""))
        if layered and kind == "bus_stop":
            continue  # the layer carries the bus stops; counting these too would count them twice
        projected = project([(poi.get("lon"), poi.get("lat"))], "points of interest")
        if not len(projected):
            continue
        pois_xy.append(projected[0, 2:])
        poi_kinds.append(kind)
        if kind == "ferry_terminal":
            ferries.append(projected[0])

    stops_px = stops_xy = None
    if layered:
        stops_px, stops_xy = {}, {}
        for mode in MODES:
            if mode == "ferry":
                points = np.asarray(ferries, np.float64).reshape(-1, 4)
            else:
                points = project(stops.get(mode) or (), f"{mode} stops")
            cols, rows_ = np.round(points[:, 0]), np.round(points[:, 1])
            inside = (0 <= cols) & (cols < w) & (0 <= rows_) & (rows_ < h)
            pixels = zip(points[:, :2], inside, strict=True)
            stops_px[mode] = [(x, y) if ok else None for (x, y), ok in pixels]
            stops_xy[mode] = points[:, 2:].copy()
            if mode == "bus":
                pois_xy.extend(points[:, 2:])
                poi_kinds.extend(["bus_stop"] * len(points))

    warnings = [f"{n} {what} with coordinates not on Earth skipped" for what, n in skipped.items() if n]
    if repeated:
        warnings.append(f"{repeated} repeated station ids dropped: one entry kept for each")
    if stops is not None and not layered:
        warnings.append("the stops layer is not an object: the stops were not measured")
    return {
        "ways_px": ways_px,
        "tunnels_px": tunnels_px,
        "stations": stations,
        "stations_px": stations_px,
        "stations_xy_m": np.asarray(stations_xy, np.float64).reshape(-1, 2),
        "pois_xy_m": np.asarray(pois_xy, np.float64).reshape(-1, 2),
        "poi_kinds": poi_kinds,
        "stops_px": stops_px,
        "stops_xy_m": stops_xy,
        "warnings": warnings,
    }


def _lonlat_of(point) -> tuple[float, float] | tuple[None, None]:
    """(lon, lat) as finite floats inside the usable ranges, or (None, None)."""
    try:
        lon, lat = float(point[0]), float(point[1])
    except (TypeError, ValueError, LookupError):  # a short, non-numeric or dict-shaped point
        return None, None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None, None
    if not (LON_RANGE[0] <= lon <= LON_RANGE[1] and LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
        return None, None
    return lon, lat


def analyse_rail_access(
    bands: dict[str, np.ndarray],
    polygon_px: Sequence[Sequence[Sequence[float]]],
    pixel_size_m: float,
    transform: Sequence[float],
    epsg: int,
    network: dict,
    params: RailParams | None = None,
    coverage_floor: float = 0.6,
) -> RailAnalysis:
    """Measure one city. `bands` holds `red`, `nir`, `swir16` and `scl` on one grid of
    `pixel_size_m` pixels with the north-up `transform` in UTM `epsg`; `polygon_px` outlines
    the site; `network` is the OpenStreetMap network (ways with lon/lat points and a `tunnel`
    flag, stations, points of interest, and the optional `stops` layer of bus and tram points:
    without it every stop measure is None and the quality flag no_stops_layer says so)."""
    params = params or RailParams()
    if pixel_size_m <= 0:
        raise ValueError("pixel_size_m must be positive")
    red, nir, swir, scl = (bands[name] for name in ("red", "nir", "swir16", "scl"))
    if not red.shape == nir.shape == swir.shape == scl.shape:
        raise ValueError("bands must share one shape")
    shape = red.shape
    timings: dict[str, float] = {}
    clock = time.perf_counter()

    def lap(stage):
        nonlocal clock
        now = time.perf_counter()
        timings[stage] = round((now - clock) * 1000.0, 1)
        clock = now

    # Masks, coverage and heat.
    site = masks.polygon_mask(shape, polygon_px)
    built, unusable, water = urban.built_up_mask(red, nir, swir, scl, params.ndbi_threshold, params.ndvi_max)
    built[site == 0] = 0
    usable = np.where((site > 0) & (unusable == 0), 255, 0).astype(np.uint8)
    site_px = int(np.count_nonzero(site))
    coverage = int(np.count_nonzero(usable)) / site_px if site_px else 0.0
    heat = urban.heat_map(built, params.heat_sigma_m / pixel_size_m, usable=usable)
    lap("masks")

    # Stations: distances, served area, catchments.
    projected = project_network(network, epsg, transform, shape)
    stations, stations_px = projected["stations"], projected["stations_px"]
    reach_px = params.reach_m / pixel_size_m
    pixel_km2 = (pixel_size_m / 1000.0) ** 2
    dist_px, labels = access.distance_to_stations(access.station_raster(shape, stations_px))
    station_labels = access.label_of_station(labels, stations_px)
    served = access.served_stats(built, dist_px, reach_px, pixel_km2)
    desert = access.desert_mask(built, dist_px, reach_px)
    activity, catchment_coverage = access.catchment_activity(
        heat, labels, dist_px, reach_px, station_labels, usable=usable
    )
    lap("access")

    # Stops: every mode's points in one raster for the stop reach (the plain transform, no labels),
    # then one transform per mode for its share; the deserts are read here because each says how
    # much of it the stops cover; then what the stops add to the stations and each station's
    # feeder coverage. Without the layer the measures are None and the mask is empty.
    stops_px = projected["stops_px"]
    layered = stops_px is not None
    stop_reach_px = params.stop_reach_m / pixel_size_m
    stop_reach = np.zeros(shape, np.uint8)
    share_by_mode: dict[str, float | None] = dict.fromkeys(MODES)
    stop_stats: dict[str, float | None] = dict.fromkeys(
        ("stop_served_share", "stop_served_km2", "any_mode_served_share", "any_mode_served_km2",
         "any_mode_desert_km2", "stops_only_km2", "stop_overlap_share")
    )  # fmt: skip
    if layered:
        every = [point for mode in MODES for point in stops_px[mode]]
        stop_reach[access.distance_to_points(access.station_raster(shape, every)) <= stop_reach_px] = 255
        for mode in MODES:
            if any(point is not None for point in stops_px[mode]):
                mode_dist = access.distance_to_points(access.station_raster(shape, stops_px[mode]))
                by_mode = access.served_stats(built, mode_dist, stop_reach_px, pixel_km2)
                share_by_mode[mode] = by_mode["served_share"]
            else:
                share_by_mode[mode] = 0.0  # the layer is there and holds no stop of this mode in the window
        stop_stats = access.stop_stats(built, dist_px, reach_px, stop_reach, pixel_km2)
    clusters = access.desert_clusters(
        desert, params.min_desert_km2 / pixel_km2, top=MAX_DESERTS, within=stop_reach if layered else None
    )
    first_station_of_label = {}
    for index, label in enumerate(station_labels):
        if label > 0:  # label 0 is a station outside the window, which is nobody's nearest
            first_station_of_label.setdefault(label, index)
    deserts = []
    for cluster in clusters:
        col, row = int(round(cluster["x"])), int(round(cluster["y"]))
        col, row = min(max(col, 0), shape[1] - 1), min(max(row, 0), shape[0] - 1)
        lon, lat = geo.pixel_to_lonlat(transform, epsg, col, row)
        nearest = first_station_of_label.get(int(labels[row, col]))
        found = nearest is not None and math.isfinite(float(dist_px[row, col]))
        deserts.append(
            {
                "lon": round(lon, 5),
                "lat": round(lat, 5),
                "area_km2": round(cluster["area_px"] * pixel_km2, 4),
                "nearest_station": rail_network.station_node_id(stations[nearest]) if found else None,
                "nearest_station_m": round(float(dist_px[row, col]) * pixel_size_m, 1) if found else None,
                "stop_covered_share": round(cluster["within_share"], 4) if layered else None,
            }
        )
    feeder = access.feeder_coverage(
        built, usable, labels, dist_px, stop_reach if layered else None, reach_px,
        params.feeder_reach_m / pixel_size_m, station_labels,
    )  # fmt: skip
    lap("modes")

    # The network as a graph.
    surface = rail_network.rasterise_ways(shape, projected["ways_px"])
    tunnel = rail_network.rasterise_ways(shape, projected["tunnels_px"])
    skeleton = rail_network.thin(np.maximum(surface, tunnel))
    snapped = rail_network.snap_stations(skeleton, stations_px, params.snap_m / pixel_size_m)
    visible = (
        (urban.ndvi(red, nir) < params.visibility_ndvi_max) & (water == 0) & (unusable == 0)
    ).astype(np.uint8)
    graph, info = rail_network.build_graph(
        skeleton, stations, snapped, pixel_size_m, visible, tunnel, max_nodes=MAX_GRAPH_NODES
    )
    snapped = info["snapped"]
    lap("graph")

    # Graph measures, per-station rows, flags and suggestions.
    node_of_station = {rail_network.station_node_id(s): rail_network.station_node_id(s) for s in stations}
    for kept, dropped in info["merged"]:
        node_of_station[dropped] = kept
    activity_by_node: dict[str, float | None] = {}
    for station, value in zip(stations, activity, strict=True):
        activity_by_node.setdefault(node_of_station[rail_network.station_node_id(station)], value)
    measures = graph_metrics(graph, activity_by_node, params.hub_count)
    intermodal = access.intermodal_counts(
        projected["stations_xy_m"], projected["pois_xy_m"], projected["poi_kinds"],
        params.intermodal_near_m, params.intermodal_far_m,
    )  # fmt: skip
    rows = []
    for index, station in enumerate(stations):
        sid = rail_network.station_node_id(station)
        node = node_of_station[sid]
        d_hub = measures["d_hub_m"].get(node)
        weight = _isolation_weight(d_hub, activity[index])
        rows.append(
            {
                "id": sid,
                "name": station.get("name"),
                "kind": station.get("kind"),
                "lon": round(float(station["lon"]), 5),
                "lat": round(float(station["lat"]), 5),
                "snapped": snapped[index] is not None,
                "degree": int(measures["degree"].get(node, 0)),
                "betweenness": round(measures["betweenness"].get(node, 0.0), 4),
                "activity": None if activity[index] is None else round(activity[index], 4),
                "catchment_coverage": round(catchment_coverage[index], 4),
                "d_hub_m": None if d_hub is None else round(d_hub, 1),
                "isolation_weight": weight,
                "feeder_coverage": None if feeder[index] is None else round(feeder[index], 4),
                "multimodal_isolation_weight": _multimodal_weight(weight, feeder[index]),
                "intermodal": intermodal[index],
            }
        )
    adjacency = rail_network.station_adjacency(graph)
    flags = flag_stations(rows, adjacency)
    suggestions = suggest(
        graph, rows, measures, adjacency, pixel_size_m, params.max_orbital_km, water, unusable, built
    )
    edges = []
    for u, v, data in graph.edges(data=True):
        a, b = sorted((u, v))
        edges.append(
            {
                "from": a,
                "to": b,
                "length_m": data["length_m"],
                "betweenness": round(measures["edge_betweenness"].get((a, b), 0.0), 4),
                "corridor_visibility": data["corridor_visibility"],
                "tunnel": data["tunnel"],
            }
        )
    truncated = max(0, len(edges) - MAX_EDGES)
    if truncated:
        # The edges most paths run through are kept, a station's edges first among equals.
        edges.sort(key=lambda e: (-e["betweenness"], not _is_station(graph, e), e["from"], e["to"]))
        edges = edges[:MAX_EDGES]
    edges.sort(key=lambda e: (e["from"], e["to"]))
    lap("metrics")

    snapped_count = sum(1 for s in snapped if s is not None)
    quality_flags = []
    if site_px == 0:
        quality_flags.append("empty_site")
    if coverage < coverage_floor:
        quality_flags.append("low_coverage")
    if not projected["ways_px"] and not projected["tunnels_px"] or not stations:
        quality_flags.append("no_network")
    if stations and snapped_count < len(stations) / 2:
        quality_flags.append("few_stations_snapped")
    if info["components"] > max(3, len(stations) / 10):
        quality_flags.append("graph_fragmented")
    # Two different things: no graph at all (over MAX_GRAPH_NODES, a pathological skeleton) and a
    # graph whose betweenness was sampled (over BETWEENNESS_WORK, which most real cities are).
    graph_skipped = info["too_large"] or (
        measures["approximate"] and not measures["betweenness"] and graph.number_of_nodes() > 0
    )
    if graph_skipped:
        quality_flags.append("graph_too_large")
    elif measures["approximate"]:
        quality_flags.append("betweenness_sampled")
    if not layered:
        quality_flags.append("no_stops_layer")

    stops = network.get("stops") if layered else None
    by_mode = {mode: _rounded(share_by_mode[mode]) for mode in MODES} if layered else None
    metrics = {
        "task": "rail_access",
        "coverage": round(coverage, 4),
        "built_up_km2": round(served["built_up_km2"], 4),
        "served_km2": round(served["served_km2"], 4),
        "desert_km2": round(served["desert_km2"], 4),
        "served_share": round(served["served_share"], 4),
        # The stops: what was received per mode, the built-up ground within their reach, by any
        # mode (a station or a stop) and beyond both, and how far the mapped stops go: built-up
        # km2 within reach per 100 stops, and the part of it the stations already reach.
        "stops": None if stops is None else {
            "bus": len(stops.get("bus") or ()),
            "tram": len(stops.get("tram") or ()),
            "ferry": sum(1 for poi in network.get("pois", ()) if poi.get("kind") == "ferry_terminal"),
        },  # fmt: skip
        "stop_served_share": _rounded(stop_stats["stop_served_share"]),
        "stop_served_km2": _rounded(stop_stats["stop_served_km2"]),
        "stop_served_share_by_mode": by_mode,
        "any_mode_served_share": _rounded(stop_stats["any_mode_served_share"]),
        "any_mode_served_km2": _rounded(stop_stats["any_mode_served_km2"]),
        "any_mode_desert_km2": _rounded(stop_stats["any_mode_desert_km2"]),
        "stops_only_km2": _rounded(stop_stats["stops_only_km2"]),
        "stop_coverage_km2_per_100": _per_hundred(stop_stats["stop_served_km2"], stops, network),
        "stop_overlap_share": _rounded(stop_stats["stop_overlap_share"]),
        "station_count": len(stations),
        "snapped_station_count": snapped_count,
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "components": info["components"],
        "hubs": measures["hubs"],
        "interchanges": measures["interchanges"],
        "flags": flags,
        "suggestions": suggestions,
        "stations": rows[:MAX_STATIONS],
        "deserts": deserts,
        "edges": edges,
        "truncated": truncated,
        "quality_flags": quality_flags,
        "warnings": projected["warnings"] + info["warnings"],
        # What was received, skipped entries included: the warnings name those.
        "network": {
            "ways": len(network.get("ways", ())),
            "stations": len(network.get("stations", ())),
            "pois": len(network.get("pois", ())),
            "points": sum(len(way.get("points", ())) for way in network.get("ways", ())),
            "stops": None if stops is None else sum(len(points or ()) for points in stops.values()),
            "fetched_at": network.get("fetched_at"),
        },
        "params": {
            "pixel_size_m": pixel_size_m,
            "coverage_floor": coverage_floor,
            **dataclasses.asdict(params),
        },
    }
    layers = {
        "heat": heat,
        "built": built,
        "desert": desert,
        "unusable": unusable,
        "water": water,
        "usable": usable,
        "stop_reach": stop_reach,
        "skeleton": skeleton,
        "graph": graph,
        "stations_px": stations_px,
        "hubs": measures["hubs"],
        "timings": timings,
    }
    return RailAnalysis(metrics=metrics, layers=layers)


def _is_station(graph: nx.Graph, edge: dict) -> bool:
    return any(graph.nodes[n].get("kind") == "station" for n in (edge["from"], edge["to"]) if n in graph)


def _rounded(value: float | None, places: int = 4) -> float | None:
    """`value` rounded for the metrics; None stays None (not measured)."""
    return None if value is None else round(float(value), places)


def _per_hundred(stop_served_km2: float | None, stops: dict | None, network: dict) -> float | None:
    """Built-up km2 within stop reach per 100 mapped stops (the received bus, tram and ferry
    points together): how far the mapped stops go. None without the layer or without a stop."""
    if stops is None or stop_served_km2 is None:
        return None
    count = sum(len(stops.get(mode) or ()) for mode in ("bus", "tram"))
    count += sum(1 for poi in network.get("pois", ()) if poi.get("kind") == "ferry_terminal")
    return round(100.0 * stop_served_km2 / count, 4) if count else None


def _betweenness_sample(graph: nx.Graph) -> dict:
    """networkx's sampling arguments for `graph`: none when every source fits in
    `BETWEENNESS_WORK` (exact), else `BETWEENNESS_WORK // (nodes + edges)` seeded source nodes,
    clamped to [`MIN_BETWEENNESS_SAMPLES`, `BETWEENNESS_SAMPLES`]."""
    n = graph.number_of_nodes()
    size = n + graph.number_of_edges()
    if n * size <= BETWEENNESS_WORK:
        return {}
    k = min(max(BETWEENNESS_WORK // size, MIN_BETWEENNESS_SAMPLES), BETWEENNESS_SAMPLES, n)
    return {"k": k, "seed": BETWEENNESS_SEED}


def node_betweenness(graph: nx.Graph, sample: dict | None = None) -> dict[str, float]:
    """Normalised betweenness per node, weighted by `length_m`, within the work budget; empty for
    an empty graph and for one over `MAX_GRAPH_NODES`. `sample` overrides the graph's own sampling
    arguments: a simulated link is measured with the base graph's, so the before and after values
    come from the same estimator (one more edge can move a graph across the budget's boundary)."""
    if not graph or graph.number_of_nodes() > MAX_GRAPH_NODES:
        return {}
    arguments = _betweenness_sample(graph) if sample is None else sample
    return nx.betweenness_centrality(graph, weight="length_m", normalized=True, **arguments)


def betweenness(graph: nx.Graph) -> tuple[dict, dict, bool]:
    """(node betweenness, edge betweenness keyed by sorted pair, approximate): exact while every
    source fits in `BETWEENNESS_WORK`, sampled above (approximate), and both empty over
    `MAX_GRAPH_NODES` (approximate too)."""
    if not graph:
        return {}, {}, False
    if graph.number_of_nodes() > MAX_GRAPH_NODES:
        return {}, {}, True
    sample = _betweenness_sample(graph)
    per_edge = nx.edge_betweenness_centrality(graph, weight="length_m", normalized=True, **sample)
    edges = {tuple(sorted(edge)): value for edge, value in per_edge.items()}
    return node_betweenness(graph), edges, bool(sample)


def graph_metrics(graph: nx.Graph, activity: dict[str, float | None], hub_count: int) -> dict:
    """Degree and betweenness per node, the edge betweenness per sorted pair, whether the
    betweenness is `approximate` (sampled, over `BETWEENNESS_WORK`; skipped, with no hubs, over
    `MAX_GRAPH_NODES`), the `hubs` (the top `hub_count` station nodes by
    betweenness among those with activity at or above the measured stations' median; by
    betweenness alone when no activity was measured), the `interchanges` (station nodes of
    degree three or more), `d_hub_m` (the shortest path to any hub, None when there is no
    path) and `hub_of` (which hub that is) per node."""
    degree = dict(graph.degree())
    node_betweenness_, edge_betweenness, approximate = betweenness(graph)
    station_nodes = sorted(n for n, d in graph.nodes(data=True) if d.get("kind") == "station")
    hubs: list[str] = []
    if station_nodes and graph.number_of_nodes() <= MAX_GRAPH_NODES:
        measured = {n: activity[n] for n in station_nodes if activity.get(n) is not None}
        if measured:
            median = float(np.median(list(measured.values())))
            busy = [n for n in station_nodes if n in measured and measured[n] >= median]
        else:
            busy = station_nodes
        hubs = sorted(busy, key=lambda n: (-node_betweenness_.get(n, 0.0), n))[: int(hub_count)]
    d_hub_m: dict[str, float | None] = dict.fromkeys(graph.nodes, None)
    hub_of: dict[str, str | None] = dict.fromkeys(graph.nodes, None)
    if hubs:
        distances, paths = nx.multi_source_dijkstra(graph, set(hubs), weight="length_m")
        for node, value in distances.items():
            d_hub_m[node] = float(value)
            hub_of[node] = paths[node][0]
    return {
        "degree": degree,
        "betweenness": node_betweenness_,
        "edge_betweenness": edge_betweenness,
        "approximate": approximate,
        "hubs": hubs,
        "interchanges": [n for n in station_nodes if degree.get(n, 0) >= 3],
        "d_hub_m": d_hub_m,
        "hub_of": hub_of,
    }


def flag_stations(rows: Sequence[dict], adjacency: dict[str, list[str]]) -> list[dict]:
    """The station flags, from the per-station rows of the metrics:

    - `isolated_high_demand`: activity in the top quarter, degree at most 2, isolation weight in
      the top quarter (among stations with one); the condition is on the rail weight, and the
      flag quotes the station's `feeder_coverage` and `multimodal_isolation_weight` beside it;
    - `single_point_of_failure`: the station whose betweenness is at least twice the next;
    - `ghost_line`: three or more consecutive stations (next along a line, through junctions)
      each with activity in the bottom quarter.

    A quarter only exists where the values spread: when every station has the same activity or
    the same weight, nothing stands out and nothing is flagged. A station whose activity was not
    measured (None) is in no quarter.
    """
    flags: list[dict] = []
    measured = [row for row in rows if row["activity"] is not None]
    if not rows:
        return flags
    activity = np.array([row["activity"] for row in measured], dtype=np.float64)
    weights = np.array([row["isolation_weight"] for row in measured if row["isolation_weight"] is not None])
    p25_activity, p75_activity = _quartiles(activity)
    p25_weight, p75_weight = _quartiles(weights)
    spread_activity, spread_weight = p75_activity > p25_activity, p75_weight > p25_weight
    for row in measured:
        w = row["isolation_weight"]
        if w is None or not spread_activity or not spread_weight:
            continue
        if row["activity"] >= p75_activity and row["degree"] <= 2 and w >= p75_weight:
            flags.append(
                {
                    "type": "isolated_high_demand",
                    "station": row["id"],
                    "name": row["name"],
                    "activity": row["activity"],
                    "degree": row["degree"],
                    "isolation_weight": w,
                    "feeder_coverage": row["feeder_coverage"],
                    "multimodal_isolation_weight": row["multimodal_isolation_weight"],
                }
            )

    ranked = sorted(rows, key=lambda row: (-row["betweenness"], row["id"]))
    top, second = (ranked[0]["betweenness"], ranked[1]["betweenness"]) if len(ranked) >= 2 else (0.0, 0.0)
    if top > 0 and top >= 2 * second:
        flags.append(
            {
                "type": "single_point_of_failure",
                "station": ranked[0]["id"],
                "name": ranked[0]["name"],
                "ratio": round(ranked[0]["betweenness"] / second, 4) if second > 0 else None,
            }
        )

    names = {row["id"]: row["name"] for row in rows}
    quiet = {row["id"] for row in measured if row["activity"] <= p25_activity and row["id"] in adjacency}
    if not spread_activity:
        quiet = set()
    seen: set[str] = set()
    for start in sorted(quiet):
        if start in seen:
            continue
        chain = _walk_chain(start, quiet, adjacency)
        seen.update(chain)
        if len(chain) >= 3:
            flags.append({"type": "ghost_line", "stations": chain, "names": [names[s] for s in chain]})
    return flags


def _quartiles(values: np.ndarray) -> tuple[float, float]:
    """(p25, p75) of `values`; (0, 0) of nothing, which spreads nothing."""
    if not len(values):
        return 0.0, 0.0
    low, high = np.percentile(values, (25, 75))
    return float(low), float(high)


def _walk_chain(start: str, members: set[str], adjacency: dict[str, list[str]]) -> list[str]:
    """The members connected to `start` through members, in order along the line: from an end
    of the chain, each next station the nearest unvisited neighbour (by id when several).

    Every membership test is against a set (the visited stations are kept in one beside the
    order), so each step costs the station's adjacency, not the chain's length times it. Counted
    as equality comparisons of station ids (tests/test_vision_rail_core.py, which bounds them at
    four per station): 149 among 150 quiet stations all next to each other, where testing against
    the order kept as a list made 1,113,924; along a line of 600 stations a few hundred against
    about 359,000 (the exact count follows the set's iteration order, so the hash seed)."""
    component, todo = {start}, [start]
    while todo:
        for neighbour in adjacency.get(todo.pop(), ()):
            if neighbour in members and neighbour not in component:
                component.add(neighbour)
                todo.append(neighbour)

    def inside(node):
        return [n for n in adjacency.get(node, ()) if n in component]

    first = min(component, key=lambda n: (len(inside(n)), n))
    order, visited, current = [first], {first}, first
    while True:
        following = [n for n in inside(current) if n not in visited]
        if not following:
            break
        current = min(following)
        order.append(current)
        visited.add(current)
    return order + sorted(component - visited)


def suggest(
    graph: nx.Graph,
    rows: Sequence[dict],
    measures: dict,
    adjacency: dict[str, list[str]],
    pixel_size_m: float,
    max_orbital_km: float,
    water: np.ndarray,
    unusable: np.ndarray,
    built: np.ndarray,
) -> list[dict]:
    """Theoretical changes to the network (every one carries `"theoretical": true`: a what-if on
    the mapped graph and straight lines, never a route or a recommendation), deterministic for
    a given graph:

    - `orbital_link`: among pairs of stations both in the top quarter of distance to a hub,
      within `max_orbital_km` of each other in a straight line and not already next along a
      line, the `MAX_ORBITAL_CANDIDATES` with the largest product of weights (`_ranking_weight`)
      are each tried on a copy of the graph with an edge `ORBITAL_DETOUR` times the straight
      line and ranked by the fall in the mean distance to a hub (Dijkstra alone); a link that
      brings nobody closer is dropped; the top `TOP_SUGGESTIONS` are kept and only they get the
      change in the top hub's betweenness, so betweenness runs a few times, not a dozen. Each
      says what its straight line crosses (`water_share`, `unmeasured_share`, `built_share` of
      the line's pixels over the `water`, `unusable` and `built` layers, and `water_km`), both
      ends' distance to a hub before it (`from_d_hub_m`, `to_d_hub_m`) and how many stations it
      brings closer to a hub (`stations_closer`). A link with `water_share` of `MOSTLY_WATER` or
      more (a harbour crossing) is ranked after every drier one, never dropped;
    - `feeder_corridor`: the `TOP_SUGGESTIONS` stations with the largest weight, each to its
      nearest hub, with the station's `isolation_weight`, `multimodal_isolation_weight` and
      `feeder_coverage`.
    """
    suggestions: list[dict] = []
    hubs = measures["hubs"]
    if not hubs:
        return suggestions
    by_id = {row["id"]: row for row in rows}
    remote_rows = [row for row in rows if row["id"] in graph and row["isolation_weight"] is not None]
    if remote_rows:
        p75 = float(np.percentile([row["d_hub_m"] for row in remote_rows], 75))
        remote = sorted((row for row in remote_rows if row["d_hub_m"] >= p75), key=lambda row: row["id"])
        candidates = []
        for i, a in enumerate(remote):
            next_along = set(adjacency.get(a["id"], ()))  # a set: tested once per pair
            for b in remote[i + 1 :]:
                if b["id"] in next_along:
                    continue
                straight_m = _pixel_distance(graph, a["id"], b["id"]) * pixel_size_m
                if straight_m > max_orbital_km * 1000.0:
                    continue
                product = _ranking_weight(a) * _ranking_weight(b)
                candidates.append((-product, a["id"], b["id"], straight_m))
        candidates.sort()
        reachable = [n for n, d in measures["d_hub_m"].items() if d is not None and n in by_id]
        before_mean = float(np.mean([measures["d_hub_m"][n] for n in reachable])) if reachable else 0.0
        tried = []
        for _, a_id, b_id, straight_m in candidates[:MAX_ORBITAL_CANDIDATES]:
            trial = graph.copy()
            trial.add_edge(a_id, b_id, length_m=round(straight_m * ORBITAL_DETOUR, 1))
            distances, _ = nx.multi_source_dijkstra(trial, set(hubs), weight="length_m")
            after_mean = float(np.mean([distances[n] for n in reachable])) if reachable else 0.0
            delta = round(after_mean - before_mean, 1)
            if delta < 0:  # a link that brings nobody closer to a hub is not a suggestion
                before = measures["d_hub_m"]
                closer = sum(1 for n in reachable if round(distances[n], 1) < round(before[n], 1))
                crossing = _crossing(graph, a_id, b_id, water, unusable, built)
                tried.append((delta, a_id, b_id, straight_m, trial, closer, crossing))
        # The dry links first, by the fall in mean hub distance; then, in the same order, those
        # mostly over water (a crossing whose line left the grid is not known to be dry).
        tried.sort(key=lambda t: (not _is_dry(t[6]["water_share"]), t[0], t[1], t[2]))
        before_top = max(measures["betweenness"].get(h, 0.0) for h in hubs)
        base_sample = _betweenness_sample(graph)  # the estimator before_top came from
        for delta, a_id, b_id, straight_m, trial, closer, crossing in tried[:TOP_SUGGESTIONS]:
            # Nodes only (the edges' are never read), sampled exactly as the base graph was.
            trial_betweenness = node_betweenness(trial, sample=base_sample)
            after_top = max(trial_betweenness.get(h, 0.0) for h in hubs)
            straight_km = round(straight_m / 1000.0, 4)
            water_share = crossing["water_share"]
            suggestions.append(
                {
                    "type": "orbital_link",
                    "theoretical": True,
                    "from": a_id,
                    "to": b_id,
                    "from_name": by_id[a_id]["name"],
                    "to_name": by_id[b_id]["name"],
                    "straight_km": straight_km,
                    "delta_top_hub_betweenness": round(after_top - before_top, 4),
                    "delta_mean_d_hub_m": delta,
                    "from_d_hub_m": by_id[a_id]["d_hub_m"],
                    "to_d_hub_m": by_id[b_id]["d_hub_m"],
                    "stations_closer": closer,
                    **crossing,
                    "water_km": None if water_share is None else round(water_share * straight_km, 4),
                }
            )

    isolated = [row for row in rows if row["isolation_weight"] and row["id"] in graph]
    isolated.sort(key=lambda row: (-_ranking_weight(row), row["id"]))
    for row in isolated[:TOP_SUGGESTIONS]:
        hub = measures["hub_of"].get(row["id"])
        if hub is None:
            continue
        suggestions.append(
            {
                "type": "feeder_corridor",
                "theoretical": True,
                "station": row["id"],
                "name": row["name"],
                "hub": hub,
                "hub_name": by_id[hub]["name"] if hub in by_id else None,
                "length_m": row["d_hub_m"],
                "isolation_weight": row["isolation_weight"],
                "multimodal_isolation_weight": row["multimodal_isolation_weight"],
                "feeder_coverage": row["feeder_coverage"],
            }
        )
    return suggestions


def _ranking_weight(row: dict) -> float:
    """The weight `suggest` ranks a station by: its multimodal isolation weight where it has one
    (the rail weight discounted by the share of its hinterland the stops cover), else its rail
    weight. The two are one scale, not two: the multimodal weight is the rail weight times (1 -
    coverage), and a station without one either has no stops layer (nothing to discount by) or
    no built-up ground beyond its reach in its cell (nothing for a stop to cover, so no
    discount), so each station is ranked by its rail weight less the feeder cover that was
    measured. Ranking by the multimodal weight only when every station had one would fall back
    to the rail weight in every city with a dense core, whose stations' cells end inside their
    own reach. Deterministic either way; the suggestion quotes both weights."""
    weight = row["multimodal_isolation_weight"]
    return row["isolation_weight"] if weight is None else weight


def _is_dry(water_share: float | None) -> bool:
    return water_share is not None and water_share < MOSTLY_WATER


def _crossing(
    graph: nx.Graph, a: str, b: str, water: np.ndarray, unusable: np.ndarray, built: np.ndarray
) -> dict[str, float | None]:
    """What the straight line between nodes `a` and `b` crosses: the shares of its pixels (a 1 px
    `cv2.line` between the two node pixels, drawn into a blank mask of the line's part of the
    grid) that are water, unmeasured (unusable) and built up, 4 dp. Every share None when no
    pixel of the line is in the grid: not measured, which is not "dry"."""
    h, w = water.shape
    ax, ay = round(graph.nodes[a]["x"]), round(graph.nodes[a]["y"])
    bx, by = round(graph.nodes[b]["x"]), round(graph.nodes[b]["y"])
    x0, x1 = max(0, min(ax, bx)), min(w - 1, max(ax, bx))
    y0, y1 = max(0, min(ay, by)), min(h - 1, max(ay, by))
    none: dict[str, float | None] = {"water_share": None, "unmeasured_share": None, "built_share": None}
    if x0 > x1 or y0 > y1:
        return none
    mask = np.zeros((y1 - y0 + 1, x1 - x0 + 1), np.uint8)
    cv2.line(mask, (ax - x0, ay - y0), (bx - x0, by - y0), 255, 1)
    on = mask > 0
    count = int(np.count_nonzero(on))
    if count == 0:
        return none
    region = (slice(y0, y1 + 1), slice(x0, x1 + 1))
    return {
        "water_share": round(int(np.count_nonzero(on & (water[region] > 0))) / count, 4),
        "unmeasured_share": round(int(np.count_nonzero(on & (unusable[region] > 0))) / count, 4),
        "built_share": round(int(np.count_nonzero(on & (built[region] > 0))) / count, 4),
    }


def _isolation_weight(d_hub_m: float | None, activity: float | None) -> float | None:
    """(d_hub / 1 km) / max(activity, MIN_ACTIVITY); None when either was not measured."""
    if d_hub_m is None or activity is None:
        return None
    return round((d_hub_m / 1000.0) / max(activity, MIN_ACTIVITY), 4)


def _multimodal_weight(isolation_weight: float | None, feeder_coverage: float | None) -> float | None:
    """The isolation weight times (1 - the feeder coverage): the rail weight discounted by the
    share of the station's hinterland that bus, tram or ferry stops already cover; None when
    either was not measured."""
    if isolation_weight is None or feeder_coverage is None:
        return None
    return round(isolation_weight * (1.0 - feeder_coverage), 4)


def _pixel_distance(graph: nx.Graph, a: str, b: str) -> float:
    na, nb = graph.nodes[a], graph.nodes[b]
    return math.hypot(na["x"] - nb["x"], na["y"] - nb["y"])
