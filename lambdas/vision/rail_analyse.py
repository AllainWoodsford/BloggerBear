"""One city, one scene, one mapped network: the rail access pass, and the metrics the adapter
diffs.

The order is the data's: site mask, built-up mask and coverage, the heat map, the network
projected into the scene's pixels, distances to stations (served area, deserts, catchments),
the ways rasterised, thinned and read back as a graph, then the graph measures (betweenness,
hubs, distance to the nearest hub, an isolation weight), the station flags and the simulated
links. Everything in `metrics` is JSON-able, rounded, and carries the parameters it was made
with. What was not measured is said: coverage, snapped stations, graph components and the
quality flags, never a quiet zero.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass

import networkx as nx
import numpy as np

from vision import access, geo, masks, network, urban

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
}
WHOLE_NUMBER_PARAMS = ("hub_count",)
# Caps on the lists in the metrics, so a reply stays a few hundred KB for the largest city.
MAX_STATIONS, MAX_EDGES, MAX_DESERTS = 2000, 5000, 10
# A station's activity is floored here in the isolation weight, so an empty catchment far from
# a hub is "very isolated" rather than infinitely so.
MIN_ACTIVITY = 0.05
# A simulated orbital link is this much longer than the straight line between its stations.
ORBITAL_DETOUR = 1.2
# How many candidate pairs are simulated, and how many of each kind of suggestion are kept.
MAX_ORBITAL_CANDIDATES, TOP_SUGGESTIONS = 12, 3


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


@dataclass
class RailAnalysis:
    """`metrics` is what the worker returns (JSON-able). `layers` are the arrays the figure is
    drawn from (`heat`, `built`, `desert`, `unusable`, `water`, `usable`, `skeleton`), the
    `graph`, the projected `stations_px`, the `hubs` and the stage `timings` (ms); never sent."""

    metrics: dict
    layers: dict


def project_network(
    network_data: dict, epsg: int, transform: Sequence[float], shape: tuple[int, int]
) -> dict:
    """The network's lon/lat geometry in the scene: `ways_px` and `tunnels_px` (float (n, 2)
    pixel arrays, surface and tunnel ways apart), `stations` (the station dicts with `x`, `y`
    pixel coordinates, `easting`, `northing` and `inside` the image), `stations_px` (an (x, y)
    per station, None outside the image), `stations_xy_m`, `pois_xy_m` and `poi_kinds`.
    Pixel coordinates are of pixel centres, so rounding them gives the pixel index."""
    zone, south = geo.utm_zone_from_epsg(epsg)
    h, w = shape

    def project(points):
        out = np.zeros((len(points), 4), np.float64)
        for i, (lon, lat) in enumerate(points):
            e, n = geo.lonlat_to_utm(float(lon), float(lat), zone, south)
            col, row = geo.map_to_pixel(transform, e, n)
            out[i] = (col - 0.5, row - 0.5, e, n)
        return out

    ways_px, tunnels_px = [], []
    for way in network_data.get("ways", ()):
        points = project(way.get("points", ()))[:, :2]
        if len(points) < 2:
            continue
        (tunnels_px if way.get("tunnel") else ways_px).append(points)

    stations, stations_px, stations_xy = [], [], []
    for station in network_data.get("stations", ()):
        (x, y, e, n), = project([(station["lon"], station["lat"])])
        inside = 0 <= round(x) < w and 0 <= round(y) < h
        stations.append({**station, "x": x, "y": y, "easting": e, "northing": n, "inside": inside})
        stations_px.append((x, y) if inside else None)
        stations_xy.append((e, n))

    pois = network_data.get("pois", ())
    pois_xy = project([(poi["lon"], poi["lat"]) for poi in pois])[:, 2:] if pois else np.zeros((0, 2))
    return {
        "ways_px": ways_px,
        "tunnels_px": tunnels_px,
        "stations": stations,
        "stations_px": stations_px,
        "stations_xy_m": np.asarray(stations_xy, np.float64).reshape(-1, 2),
        "pois_xy_m": np.asarray(pois_xy, np.float64).reshape(-1, 2),
        "poi_kinds": [str(poi.get("kind", "")) for poi in pois],
    }


def analyse_rail_access(
    bands: dict[str, np.ndarray],
    polygon_px: Sequence[Sequence[Sequence[float]]],
    pixel_size_m: float,
    transform: Sequence[float],
    epsg: int,
    network_data: dict,
    params: RailParams | None = None,
    coverage_floor: float = 0.6,
) -> RailAnalysis:
    """Measure one city. `bands` holds `red`, `nir`, `swir16` and `scl` on one grid of
    `pixel_size_m` pixels with the north-up `transform` in UTM `epsg`; `polygon_px` outlines
    the site; `network_data` is the OpenStreetMap network (ways with lon/lat points and a
    `tunnel` flag, stations, points of interest)."""
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

    # Stations: distances, served area, deserts, catchments.
    projected = project_network(network_data, epsg, transform, shape)
    stations, stations_px = projected["stations"], projected["stations_px"]
    reach_px = params.reach_m / pixel_size_m
    pixel_km2 = (pixel_size_m / 1000.0) ** 2
    dist_px, labels = access.distance_to_stations(access.station_raster(shape, stations_px))
    station_labels = access.label_of_station(labels, stations_px)
    served = access.served_stats(built, dist_px, reach_px, pixel_km2)
    desert = access.desert_mask(built, dist_px, reach_px)
    clusters = access.desert_clusters(desert, params.min_desert_km2 / pixel_km2, top=MAX_DESERTS)
    first_station_of_label = {}
    for index, label in enumerate(station_labels):
        first_station_of_label.setdefault(label, index)
    deserts = []
    for cluster in clusters:
        col, row = int(round(cluster["x"])), int(round(cluster["y"]))
        col, row = min(max(col, 0), shape[1] - 1), min(max(row, 0), shape[0] - 1)
        lon, lat = geo.pixel_to_lonlat(transform, epsg, col, row)
        nearest = first_station_of_label.get(int(labels[row, col]))
        found = nearest is not None
        deserts.append(
            {
                "lon": round(lon, 5),
                "lat": round(lat, 5),
                "area_km2": round(cluster["area_px"] * pixel_km2, 4),
                "nearest_station": network.station_node_id(stations[nearest]) if found else None,
                "nearest_station_m": round(float(dist_px[row, col]) * pixel_size_m, 1) if found else None,
            }
        )
    activity = access.catchment_activity(heat, labels, dist_px, reach_px, station_labels)
    lap("access")

    # The network as a graph.
    surface = network.rasterise_ways(shape, projected["ways_px"])
    tunnel = network.rasterise_ways(shape, projected["tunnels_px"])
    skeleton = network.thin(np.maximum(surface, tunnel))
    snapped = network.snap_stations(skeleton, stations_px, params.snap_m / pixel_size_m)
    visible = (
        (urban.ndvi(red, nir) < params.visibility_ndvi_max) & (water == 0) & (unusable == 0)
    ).astype(np.uint8)
    graph, info = network.build_graph(skeleton, stations, snapped, pixel_size_m, visible, tunnel)
    lap("graph")

    # Graph measures, per-station rows, flags and suggestions.
    node_of_station = {network.station_node_id(s): network.station_node_id(s) for s in stations}
    for kept, dropped in info["merged"]:
        node_of_station[dropped] = kept
    activity_by_node = {}
    for station, value in zip(stations, activity, strict=True):
        activity_by_node.setdefault(node_of_station[network.station_node_id(station)], value)
    measures = graph_metrics(graph, activity_by_node, params.hub_count)
    intermodal = access.intermodal_counts(
        projected["stations_xy_m"], projected["pois_xy_m"], projected["poi_kinds"],
        params.intermodal_near_m, params.intermodal_far_m,
    )  # fmt: skip
    rows = []
    for index, station in enumerate(stations):
        sid = network.station_node_id(station)
        node = node_of_station[sid]
        d_hub = measures["d_hub_m"].get(node)
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
                "activity": round(activity[index], 4),
                "d_hub_m": None if d_hub is None else round(d_hub, 1),
                "isolation_weight": _isolation_weight(d_hub, activity[index]),
                "intermodal": intermodal[index],
            }
        )
    adjacency = network.station_adjacency(graph)
    flags = flag_stations(rows, adjacency)
    suggestions = suggest(graph, rows, measures, adjacency, pixel_size_m, params.max_orbital_km)
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

    metrics = {
        "task": "rail_access",
        "coverage": round(coverage, 4),
        "built_up_km2": round(served["built_up_km2"], 4),
        "served_km2": round(served["served_km2"], 4),
        "desert_km2": round(served["desert_km2"], 4),
        "served_share": round(served["served_share"], 4),
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
        "edges": edges[:MAX_EDGES],
        "quality_flags": quality_flags,
        "warnings": info["warnings"],
        "network": {
            "ways": len(network_data.get("ways", ())),
            "stations": len(stations),
            "pois": len(projected["poi_kinds"]),
            "points": sum(len(way.get("points", ())) for way in network_data.get("ways", ())),
            "fetched_at": network_data.get("fetched_at"),
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
        "skeleton": skeleton,
        "graph": graph,
        "stations_px": stations_px,
        "hubs": measures["hubs"],
        "timings": timings,
    }
    return RailAnalysis(metrics=metrics, layers=layers)


def graph_metrics(graph: nx.Graph, activity: dict[str, float], hub_count: int) -> dict:
    """Degree and betweenness (normalised, weighted by `length_m`) per node, the edge
    betweenness per sorted pair, the `hubs` (the top `hub_count` station nodes by betweenness
    among those with activity at or above the stations' median), the `interchanges` (station
    nodes of degree three or more), `d_hub_m` (the shortest path to any hub, None when there is
    no path) and `hub_of` (which hub that is) per node."""
    degree = dict(graph.degree())
    betweenness, edge_betweenness = {}, {}
    if graph:
        betweenness = nx.betweenness_centrality(graph, weight="length_m", normalized=True)
        for edge, value in nx.edge_betweenness_centrality(graph, weight="length_m", normalized=True).items():
            edge_betweenness[tuple(sorted(edge))] = value
    station_nodes = sorted(n for n, d in graph.nodes(data=True) if d.get("kind") == "station")
    hubs: list[str] = []
    if station_nodes:
        levels = [activity.get(n, 0.0) for n in station_nodes]
        median = float(np.median(levels))
        busy = [n for n in station_nodes if activity.get(n, 0.0) >= median]
        hubs = sorted(busy, key=lambda n: (-betweenness.get(n, 0.0), n))[: int(hub_count)]
    d_hub_m: dict[str, float | None] = dict.fromkeys(graph.nodes, None)
    hub_of: dict[str, str | None] = dict.fromkeys(graph.nodes, None)
    if hubs:
        distances, paths = nx.multi_source_dijkstra(graph, set(hubs), weight="length_m")
        for node, value in distances.items():
            d_hub_m[node] = float(value)
            hub_of[node] = paths[node][0]
    return {
        "degree": degree,
        "betweenness": betweenness,
        "edge_betweenness": edge_betweenness,
        "hubs": hubs,
        "interchanges": [n for n in station_nodes if degree.get(n, 0) >= 3],
        "d_hub_m": d_hub_m,
        "hub_of": hub_of,
    }


def flag_stations(rows: Sequence[dict], adjacency: dict[str, list[str]]) -> list[dict]:
    """The station flags, from the per-station rows of the metrics:

    - `isolated_high_demand`: activity in the top quarter, degree at most 2, isolation weight in
      the top quarter (among stations with one);
    - `single_point_of_failure`: the station whose betweenness is at least twice the next;
    - `ghost_line`: three or more consecutive stations (next along a line, through junctions)
      each with activity in the bottom quarter.

    A quarter only exists where the values spread: when every station has the same activity or
    the same weight, nothing stands out and nothing is flagged.
    """
    flags: list[dict] = []
    if not rows:
        return flags
    activity = np.array([row["activity"] for row in rows], dtype=np.float64)
    weights = np.array([row["isolation_weight"] for row in rows if row["isolation_weight"] is not None])
    p25_activity, p75_activity = (float(v) for v in np.percentile(activity, (25, 75)))
    p25_weight, p75_weight = 0.0, 0.0
    if len(weights):
        p25_weight, p75_weight = (float(v) for v in np.percentile(weights, (25, 75)))
    spread_activity, spread_weight = p75_activity > p25_activity, p75_weight > p25_weight
    for row in rows:
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
    quiet = {row["id"] for row in rows if row["activity"] <= p25_activity and row["id"] in adjacency}
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


def _walk_chain(start: str, members: set[str], adjacency: dict[str, list[str]]) -> list[str]:
    """The members connected to `start` through members, in order along the line: from an end
    of the chain, each next station the nearest unvisited neighbour (by id when several)."""
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


def suggest(
    graph: nx.Graph,
    rows: Sequence[dict],
    measures: dict,
    adjacency: dict[str, list[str]],
    pixel_size_m: float,
    max_orbital_km: float,
) -> list[dict]:
    """Simulated changes to the network, deterministic for a given graph:

    - `orbital_link`: among pairs of stations both in the top quarter of distance to a hub,
      within `max_orbital_km` of each other in a straight line and not already next along a
      line, the `MAX_ORBITAL_CANDIDATES` with the largest product of isolation weights are each
      tried on a copy of the graph with an edge `ORBITAL_DETOUR` times the straight line; the
      top `TOP_SUGGESTIONS` by the fall in the mean distance to a hub are kept, with the change
      in the top hub's betweenness;
    - `feeder_corridor`: the `TOP_SUGGESTIONS` stations with the largest isolation weight, each
      to its nearest hub.
    """
    suggestions: list[dict] = []
    hubs = measures["hubs"]
    if not hubs:
        return suggestions
    by_id = {row["id"]: row for row in rows}
    in_graph = [row for row in rows if row["id"] in graph and row["d_hub_m"] is not None]
    if in_graph:
        p75 = float(np.percentile([row["d_hub_m"] for row in in_graph], 75))
        remote = sorted((row for row in in_graph if row["d_hub_m"] >= p75), key=lambda row: row["id"])
        candidates = []
        for i, a in enumerate(remote):
            for b in remote[i + 1 :]:
                if b["id"] in adjacency.get(a["id"], ()):
                    continue
                straight_m = _pixel_distance(graph, a["id"], b["id"]) * pixel_size_m
                if straight_m > max_orbital_km * 1000.0:
                    continue
                product = a["isolation_weight"] * b["isolation_weight"]
                candidates.append((-product, a["id"], b["id"], straight_m))
        candidates.sort()
        reachable = [n for n, d in measures["d_hub_m"].items() if d is not None and n in by_id]
        before_mean = float(np.mean([measures["d_hub_m"][n] for n in reachable])) if reachable else 0.0
        before_top = max(measures["betweenness"].get(h, 0.0) for h in hubs)
        tried = []
        for _, a_id, b_id, straight_m in candidates[:MAX_ORBITAL_CANDIDATES]:
            trial = graph.copy()
            trial.add_edge(a_id, b_id, length_m=round(straight_m * ORBITAL_DETOUR, 1))
            distances, _ = nx.multi_source_dijkstra(trial, set(hubs), weight="length_m")
            after_mean = float(np.mean([distances[n] for n in reachable])) if reachable else 0.0
            betweenness = nx.betweenness_centrality(trial, weight="length_m", normalized=True)
            after_top = max(betweenness.get(h, 0.0) for h in hubs)
            tried.append(
                {
                    "type": "orbital_link",
                    "from": a_id,
                    "to": b_id,
                    "from_name": by_id[a_id]["name"],
                    "to_name": by_id[b_id]["name"],
                    "straight_km": round(straight_m / 1000.0, 4),
                    "delta_top_hub_betweenness": round(after_top - before_top, 4),
                    "delta_mean_d_hub_m": round(after_mean - before_mean, 1),
                }
            )
        # A link that brings nobody closer to a hub is not a suggestion.
        tried = [s for s in tried if s["delta_mean_d_hub_m"] < 0]
        tried.sort(key=lambda s: (s["delta_mean_d_hub_m"], s["from"], s["to"]))
        suggestions.extend(tried[:TOP_SUGGESTIONS])

    isolated = [row for row in rows if row["isolation_weight"] and row["id"] in graph]
    isolated.sort(key=lambda row: (-row["isolation_weight"], row["id"]))
    for row in isolated[:TOP_SUGGESTIONS]:
        hub = measures["hub_of"].get(row["id"])
        if hub is None:
            continue
        suggestions.append(
            {
                "type": "feeder_corridor",
                "station": row["id"],
                "name": row["name"],
                "hub": hub,
                "hub_name": by_id[hub]["name"] if hub in by_id else None,
                "length_m": row["d_hub_m"],
            }
        )
    return suggestions


def _isolation_weight(d_hub_m: float | None, activity: float) -> float | None:
    if d_hub_m is None:
        return None
    return round((d_hub_m / 1000.0) / max(activity, MIN_ACTIVITY), 4)


def _pixel_distance(graph: nx.Graph, a: str, b: str) -> float:
    na, nb = graph.nodes[a], graph.nodes[b]
    return math.hypot(na["x"] - nb["x"], na["y"] - nb["y"])
