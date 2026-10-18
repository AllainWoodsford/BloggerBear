"""The agent cases' networks (the specification's 2.6.1), derived from fixture A by code.

Every network-driven case is fixture A (the city's OpenStreetMap network as the deployed tick
measured it, `scripts/vision_benchmark_networks/`) with one change made here, never a hand-edited
copy: so a case reads the same on any copy of A, a judge can see exactly what was changed, and the
run file records what each derivation added and removed by id. The derivations:

- `add_line`: B, A plus a line of `stations` stations (five for C1) laid on built-up ground beyond
  reach. The harness gives the anchor: the largest desert (built-up area beyond reach) of the
  baseline's newest measurement, so the stations sit on ground the worker measured as built up and
  unserved. Without one the point inside the site farthest from every station is used.
- `extend_line`: A with a line extended by `stations` stations (three for C1c), from a terminus
  (an end of a way no other way touches), outward along its last kilometre.
- `remove_line`: A less one line (C1d): the ways of one name, and the stations that snap to them
  and to no other way; the smallest such line with at least `min_stations` stations.
- `add_served_station`: A plus one station (C1e) between two neighbouring stations of one way
  (no station between them), inside ground they already serve: the station count moves while
  the served share barely does.
- `duplicate_stations`: B', A with `stations` stations (three for C2) mapped twice under new ids,
  as a tagging change does: the station count moves, the city does not.

A derived network keeps A's `source`, `endpoint` and `query_url`, recounts `counts`, and is stamped
`fetched_at` one day after A's: it is a later copy of the map, so the decision ring's
`network_changed` says the network moved, as it would in production. Added elements have ids
starting `eval-`, and the run file's change record lists them, so nothing derived passes for
OpenStreetMap's. A new line's stations and way and the infill station are named as the
evaluation's; an extension's way takes the extended line's name (it is that line, longer), and a
duplicate its station's name (a tagging change copies it). No numpy or OpenCV: distances are
metres on a local plane about the site's centre, which is within a few metres over a city's few
tens of kilometres.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta

# Metres per degree on a local plane (the WGS 84 ellipsoid's mean figures; a city is small enough).
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQUATOR = 111_320.0
# A derived element's id starts with this, and stays within the contract's 32 characters.
ID_PREFIX = "eval-"
MAX_ID_CHARS = 32
# How far a station may sit from a way and still be on it: the worker's own snap distance
# (vision/rail_analyse.RailParams.snap_m), so "a line's stations" are the ones the worker joins to it.
SNAP_M = 300.0
# The walking reach the rail topics measure with (common/adapters/rail_access.DEFAULTS["reach_m"]).
REACH_M = 1000.0
# A way end within this of another way is a junction, not a terminus.
TERMINUS_CLEARANCE_M = 100.0
# The new line's station spacing, and an extension's: suburban spacing.
LINE_SPACING_M = 800.0
EXTENSION_SPACING_M = 1000.0
# A line that does not fit at its spacing is tried at these fractions of it, never under MIN_SPACING_M.
SPACING_STEPS = (1.0, 0.75, 0.5, 0.375)
MIN_SPACING_M = 250.0
# How far back along a way its direction is read, so a decimated way's last short segment cannot
# point an extension anywhere.
DIRECTION_BACK_M = 1000.0
# A grid of at most this many points a side, searched for the point farthest from every station.
FARTHEST_GRID = 120
# The spatial index's cell: a station's neighbourhood within SNAP_M is its own and the next cells.
CELL_M = 500.0
# The infill station goes between two stations of one way this far apart (two reaches at most).
INFILL_GAP_M = (600.0, 2 * REACH_M)
# ...and at least this far from every station, so it is never one already mapped under a new id
# (C2's construction, an artefact). Half the smallest gap, less slack for a way that bends.
INFILL_CLEARANCE_M = 250.0
# A derived copy is fetched this long after A.
LATER = timedelta(days=1)


class DeriveError(ValueError):
    """A derivation that cannot be made on this network (no room for the line, no line to remove)."""


# --- the local plane ---------------------------------------------------------------------------


class Plane:
    """Lon/lat to metres east and north of `origin`, and back, on a local equirectangular plane."""

    def __init__(self, origin: tuple[float, float]):
        self.lon0, self.lat0 = origin
        self.kx = M_PER_DEG_LON_EQUATOR * math.cos(math.radians(self.lat0))
        self.ky = M_PER_DEG_LAT

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def lonlat(self, x: float, y: float) -> list[float]:
        return [round(self.lon0 + x / self.kx, 7), round(self.lat0 + y / self.ky, 7)]


def plane_for(polygon: list) -> Plane:
    lons = [p[0] for p in polygon]
    lats = [p[1] for p in polygon]
    return Plane(((min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2))


def inside(point: tuple[float, float], polygon_xy: list[tuple[float, float]]) -> bool:
    """Whether `point` is inside the polygon (even-odd rule), both on the plane."""
    x, y = point
    hit = False
    n = len(polygon_xy)
    for i in range(n):
        (x1, y1), (x2, y2) = polygon_xy[i], polygon_xy[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def segment_distance(p, a, b) -> float:
    """The distance from `p` to the segment `a`-`b`, on the plane."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class WayIndex:
    """Every way segment on the plane, bucketed by CELL_M cells: the ways within `radius` of a
    point are found from its own and neighbouring cells, not from every segment of the city."""

    def __init__(self, ways: list[dict], plane: Plane):
        self.plane = plane
        self.cells: dict[tuple[int, int], list[tuple[int, tuple, tuple]]] = {}
        for index, way in enumerate(ways):
            points = [plane.xy(*point[:2]) for point in way.get("points") or []]
            for a, b in zip(points, points[1:], strict=False):
                for cell in self._cells_of(a, b):
                    self.cells.setdefault(cell, []).append((index, a, b))

    @staticmethod
    def _cell(x: float, y: float) -> tuple[int, int]:
        return int(math.floor(x / CELL_M)), int(math.floor(y / CELL_M))

    def _cells_of(self, a, b):
        (c0, r0), (c1, r1) = self._cell(*a), self._cell(*b)
        for c in range(min(c0, c1), max(c0, c1) + 1):
            for r in range(min(r0, r1), max(r0, r1) + 1):
                yield c, r

    def nearest(self, x: float, y: float, radius: float) -> dict[int, float]:
        """{way index: its distance from (x, y)} for every way within `radius` metres."""
        reach = int(math.ceil(radius / CELL_M))
        c0, r0 = self._cell(x, y)
        out: dict[int, float] = {}
        for c in range(c0 - reach, c0 + reach + 1):
            for r in range(r0 - reach, r0 + reach + 1):
                for index, a, b in self.cells.get((c, r), ()):
                    d = segment_distance((x, y), a, b)
                    if d <= radius and d < out.get(index, math.inf):
                        out[index] = d
        return out


# --- the shared bookkeeping ----------------------------------------------------------------------


def later(fetched_at: str | None) -> str | None:
    """`fetched_at` plus LATER, in the same ISO form; None stays None, and a time that cannot be
    read is kept as it is (the copy is then not told apart by time)."""
    if not isinstance(fetched_at, str):
        return None
    try:
        when = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return fetched_at
    return (when + LATER).isoformat()


def recount(network: dict) -> dict:
    """`network` with `counts` made from its lists, as common/osm.py makes them: `stops` is the
    stops across modes (0 without the layer, which no recipe touches) and `stops_by_mode` is there
    only with the layer, since no layer is not measured."""
    counts = {
        "ways": len(network.get("ways") or []),
        "stations": len(network.get("stations") or []),
        "pois": len(network.get("pois") or []),
        "points": sum(len(way.get("points") or []) for way in network.get("ways") or []),
    }
    stops = network.get("stops")
    if isinstance(stops, dict):
        by_mode = {mode: len(points or []) for mode, points in stops.items()}
        counts["stops"] = sum(by_mode.values())
        counts["stops_by_mode"] = by_mode
    else:
        counts["stops"] = 0
    network["counts"] = counts
    return network


def new_id(kind: str, n: int) -> str:
    return f"{ID_PREFIX}{kind}{n}"[:MAX_ID_CHARS]


def _station(sid: str, name: str, lonlat: list[float]) -> dict:
    return {"id": sid, "name": name, "kind": "station", "lon": lonlat[0], "lat": lonlat[1]}


def change_record(recipe: str, **parts) -> dict:
    change = {"recipe": recipe, "added_stations": [], "removed_stations": [], "added_ways": [],
              "removed_ways": [], "anchor": None}  # fmt: skip
    change.update(parts)
    return change


def _start(network: dict) -> dict:
    """A deep copy of `network`, stamped as the later copy."""
    derived = copy.deepcopy(network)
    derived["fetched_at"] = later(network.get("fetched_at"))
    return derived


def _way_length(points_xy: list[tuple]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points_xy, points_xy[1:], strict=False))


# --- the derivations -------------------------------------------------------------------------------


def farthest_point(network: dict, polygon: list, plane: Plane | None = None,
                   edge_m: float = LINE_SPACING_M) -> list[float]:  # fmt: skip
    """The point inside `polygon`, at least `edge_m` from its edge, farthest from every station, on a
    grid of FARTHEST_GRID points a side over its box: lon/lat. Away from the edge, so a line laid
    through it has room on both sides."""
    plane = plane or plane_for(polygon)
    poly = [plane.xy(*p[:2]) for p in polygon]
    edges = list(zip(poly, poly[1:] + poly[:1], strict=True))
    stations = [plane.xy(s["lon"], s["lat"]) for s in network.get("stations") or []]
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    best, best_d = None, -1.0
    for i in range(FARTHEST_GRID):
        for j in range(FARTHEST_GRID):
            x = min(xs) + (max(xs) - min(xs)) * (i + 0.5) / FARTHEST_GRID
            y = min(ys) + (max(ys) - min(ys)) * (j + 0.5) / FARTHEST_GRID
            if not inside((x, y), poly) or min(segment_distance((x, y), a, b) for a, b in edges) < edge_m:
                continue
            d = min((math.dist((x, y), s) for s in stations), default=math.inf)
            if d > best_d:
                best, best_d = (x, y), d
    if best is None:
        raise DeriveError(f"the site holds no point {edge_m:g} m inside its edge")
    return plane.lonlat(*best)


def _spacings(spacing_m: float) -> list[float]:
    """`spacing_m`, then shorter by SPACING_STEPS, never under MIN_SPACING_M: a line that does not fit
    a small site at suburban spacing is laid closer rather than not at all (the change records the
    spacing used)."""
    return [round(spacing_m * f, 1) for f in SPACING_STEPS if spacing_m * f >= MIN_SPACING_M] or [spacing_m]


def add_line(network: dict, polygon: list, *, stations: int = 5, anchor=None,
             spacing_m: float = LINE_SPACING_M) -> tuple[dict, dict]:  # fmt: skip
    """B: `network` plus a straight line of `stations` stations, `spacing_m` apart, one of them on
    `anchor` ([lon, lat]; the farthest point from every station when None): centred on it if the
    line fits inside the polygon, else slid along its axis a station at a time (the anchor stays a
    station), east-west before north-south, and laid closer (_spacings) only when no position
    fits; with one way from its first station to its last."""
    plane = plane_for(polygon)
    poly = [plane.xy(*p[:2]) for p in polygon]
    centre = list(anchor) if anchor is not None else farthest_point(network, polygon, plane)
    cx, cy = plane.xy(*centre[:2])
    for spacing in _spacings(spacing_m):
        for axis in ((1.0, 0.0), (0.0, 1.0)):
            # Which station sits on the anchor: the middle one first, then outward.
            for on_anchor in sorted(range(stations), key=lambda k: (abs(k - (stations - 1) / 2), k)):
                at = [(cx + (k - on_anchor) * spacing * axis[0], cy + (k - on_anchor) * spacing * axis[1])
                      for k in range(stations)]  # fmt: skip
                if all(inside(p, poly) for p in at):
                    return _with_line(network, plane, at, spacing, centre, on_anchor)
    raise DeriveError(f"no room for a line of {stations} stations about {centre}")


def _with_line(network, plane, at, spacing, centre, on_anchor) -> tuple[dict, dict]:
    derived = _start(network)
    ids = [new_id("n", k + 1) for k in range(len(at))]
    added = [_station(sid, f"Evaluation station {k + 1}", plane.lonlat(*p))
             for k, (sid, p) in enumerate(zip(ids, at, strict=True))]  # fmt: skip
    derived["stations"] = list(derived.get("stations") or []) + added
    way = {"id": new_id("w", 1), "points": [[s["lon"], s["lat"]] for s in added], "tunnel": False,
           "name": "Evaluation line"}  # fmt: skip
    derived["ways"] = list(derived.get("ways") or []) + [way]
    change = change_record("add_line", added_stations=ids, added_ways=[way["id"]],
                     anchor=[round(v, 7) for v in centre[:2]], spacing_m=spacing,
                     anchor_station=ids[on_anchor])  # fmt: skip
    return recount(derived), change


def _direction_back(points_xy: list[tuple]) -> tuple[float, float] | None:
    """The unit vector from the point DIRECTION_BACK_M back along a way (or its start) to its end."""
    end = points_xy[-1]
    walked, back = 0.0, points_xy[0]
    for a, b in zip(reversed(points_xy[:-1]), reversed(points_xy[1:]), strict=True):
        walked += math.dist(a, b)
        back = a
        if walked >= DIRECTION_BACK_M:
            break
    dx, dy = end[0] - back[0], end[1] - back[1]
    norm = math.hypot(dx, dy)
    return None if norm == 0 else (dx / norm, dy / norm)


def _way_ends(ways: list[dict], plane: Plane) -> list[tuple]:
    """Every way's two ends: (is a junction, the way's length (negated, longest first), its id, its
    index, its points on the plane ending at that end, that end's lon/lat as the way holds it)."""
    index = WayIndex(ways, plane)
    ends = []
    for i, way in enumerate(ways):
        points = [plane.xy(*p[:2]) for p in way.get("points") or []]
        if len(points) < 2:
            continue
        raw = [list(p[:2]) for p in way["points"]]
        for end_points, end_lonlat in ((points, raw[-1]), (points[::-1], raw[0])):
            others = {j for j in index.nearest(*end_points[-1], TERMINUS_CLEARANCE_M) if j != i}
            ends.append((bool(others), -_way_length(points), str(way.get("id")), i, end_points, end_lonlat))
    return ends


def extend_line(network: dict, polygon: list, *, stations: int = 3, spacing_m: float = EXTENSION_SPACING_M,
                toward: list | None = None) -> tuple[dict, dict]:  # fmt: skip
    """C1c: one way extended by `stations` stations, a station every `spacing_m` (closer, by
    _spacings, only when nothing fits at that spacing), all inside the polygon, from one of its
    ends: a terminus (an end no other way comes within TERMINUS_CLEARANCE_M of) before a junction.

    With `toward` ([lon, lat] points: the deserts of the baseline's newest measurement, built-up
    ground beyond reach), the end nearest any of them is extended straight toward it, the last
    station at most at the point, as a real extension heads for unserved suburbs, so the stations
    land on built-up ground. Without, or when none fits, the longest way is extended along its last
    kilometre."""
    plane = plane_for(polygon)
    poly = [plane.xy(*p[:2]) for p in polygon]
    ways = network.get("ways") or []
    ends = _way_ends(ways, plane)
    targets = [plane.xy(*t[:2]) for t in toward or []]
    tries = []
    if targets:
        # Each end with its nearest target, terminus first, then nearest; the direction is toward it.
        for junction, _length, wid, i, end_points, end_lonlat in ends:
            end = end_points[-1]
            target = min(targets, key=lambda t: (math.dist(end, t), t))
            d = math.dist(end, target)
            if d > 0:
                direction = ((target[0] - end[0]) / d, (target[1] - end[1]) / d)
                tries.append(((junction, d, wid), i, end_points, end_lonlat, direction, d, "toward_desert"))
    for junction, length, wid, i, end_points, end_lonlat in ends:
        direction = _direction_back(end_points)
        if direction is not None:
            tries.append(((junction, length, wid), i, end_points, end_lonlat, direction, None, "along_line"))
    tries.sort(key=lambda t: (t[-1] != "toward_desert", t[0]))
    for spacing in _spacings(spacing_m):
        for _key, i, end_points, end_lonlat, direction, reach, how in tries:
            step = spacing if reach is None else min(spacing, reach / stations)
            if step < MIN_SPACING_M and step != spacing:
                continue
            end = end_points[-1]
            at = [(end[0] + direction[0] * step * k, end[1] + direction[1] * step * k)
                  for k in range(1, stations + 1)]  # fmt: skip
            if not all(inside(p, poly) for p in at):
                continue
            derived = _start(network)
            ids = [new_id("n", k + 1) for k in range(stations)]
            derived["stations"] = list(derived.get("stations") or []) + [
                _station(sid, f"Evaluation extension station {k + 1}", plane.lonlat(*p))
                for k, (sid, p) in enumerate(zip(ids, at, strict=True))
            ]
            # From the end exactly as the way holds it, so the two ways join.
            way = {"id": new_id("w", 1), "points": [end_lonlat, *(plane.lonlat(*p) for p in at)],
                   "tunnel": False, "name": ways[i].get("name")}  # fmt: skip
            derived["ways"] = list(derived.get("ways") or []) + [way]
            change = change_record("extend_line", added_stations=ids, added_ways=[way["id"]],
                                    extended_way=str(ways[i].get("id")), spacing_m=round(step, 1),
                                    direction=how)  # fmt: skip
            return recount(derived), change
    raise DeriveError(f"no way has room for {stations} more stations inside the site")


def line_stations(network: dict, plane: Plane, snap_m: float = SNAP_M) -> dict[str, list[str]]:
    """{line name: the ids of the stations that snap (within `snap_m`) to that line's ways and to no
    other line's}, over the named ways."""
    ways = network.get("ways") or []
    index = WayIndex(ways, plane)
    out: dict[str, list[str]] = {}
    for station in network.get("stations") or []:
        near = index.nearest(*plane.xy(station["lon"], station["lat"]), snap_m)
        names = {ways[i].get("name") for i in near}
        if len(names) == 1 and None not in names:
            out.setdefault(names.pop(), []).append(str(station["id"]))
    return out


def remove_line(network: dict, polygon: list, *, min_stations: int = 3,
                snap_m: float = SNAP_M) -> tuple[dict, dict]:  # fmt: skip
    """C1d: `network` less one line: every way of one name, and the stations that snap to it alone.
    The line chosen is the smallest (fewest such stations, then fewest ways, then by name) with
    at least `min_stations` stations, so the change is a closure, not the city's trunk."""
    plane = plane_for(polygon)
    ways = network.get("ways") or []
    by_line = line_stations(network, plane, snap_m)
    way_count = {}
    for way in ways:
        if way.get("name") is not None:
            way_count[way["name"]] = way_count.get(way["name"], 0) + 1
    choices = sorted((len(ids), way_count.get(name, 0), name) for name, ids in by_line.items()
                     if len(ids) >= min_stations)  # fmt: skip
    if not choices:
        raise DeriveError(f"no named line has {min_stations} stations of its own")
    name = choices[0][2]
    gone_stations = set(by_line[name])
    derived = _start(network)
    gone_ways = [str(w.get("id")) for w in ways if w.get("name") == name]
    derived["ways"] = [w for w in derived.get("ways") or [] if w.get("name") != name]
    derived["stations"] = [s for s in derived.get("stations") or [] if str(s["id"]) not in gone_stations]
    return recount(derived), change_record("remove_line", removed_stations=sorted(gone_stations),
                                     removed_ways=gone_ways)  # fmt: skip


def add_served_station(network: dict, polygon: list, *, gap_m: tuple[float, float] = INFILL_GAP_M,
                       snap_m: float = SNAP_M) -> tuple[dict, dict]:  # fmt: skip
    """C1e: one station between two neighbouring stations that snap to the same way (next to each
    other along it, so no station lies between), `gap_m` apart (the pair nearest the middle of
    that range), at the way point nearest their midpoint, and INFILL_CLEARANCE_M from every
    station: ground both already serve. The station count moves by one; the served share barely
    moves."""
    plane = plane_for(polygon)
    ways = network.get("ways") or []
    index = WayIndex(ways, plane)
    stations = sorted(network.get("stations") or [], key=lambda s: str(s["id"]))
    located = []
    for s in stations:
        x, y = plane.xy(s["lon"], s["lat"])
        near = index.nearest(x, y, snap_m)
        if near:
            located.append((str(s["id"]), (x, y), min(near, key=lambda i: (near[i], i))))
    middle = (gap_m[0] + gap_m[1]) / 2
    # Each way's stations in order along it: only stations next to each other make a pair.
    along: dict[int, list] = {}
    for sid, p, wi in located:
        along.setdefault(wi, []).append((_along(p, [plane.xy(*q[:2]) for q in ways[wi]["points"]]), sid, p))
    pairs = []
    for wi, row in along.items():
        row.sort()
        for (_ta, ida, pa), (_tb, idb, pb) in zip(row, row[1:], strict=False):
            d = math.dist(pa, pb)
            if gap_m[0] <= d <= gap_m[1]:
                pairs.append((abs(d - middle), ida, idb, pa, pb, wi))
    if not pairs:
        raise DeriveError(f"no two neighbouring stations of one way are {gap_m[0]:g}-{gap_m[1]:g} m apart")
    everyone = [p for _sid, p, _wi in located]
    for _off, ida, idb, pa, pb, wi in sorted(pairs):
        mid = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
        points = [plane.xy(*p[:2]) for p in ways[wi]["points"]]
        best = min(
            (_project(mid, a, b) for a, b in zip(points, points[1:], strict=False)),
            key=lambda p: math.dist(p, mid),
        )
        if min(math.dist(best, p) for p in everyone) >= INFILL_CLEARANCE_M:
            break
    else:
        raise DeriveError(f"every gap between neighbouring stations has a station within "
                          f"{INFILL_CLEARANCE_M:g} m")  # fmt: skip
    derived = _start(network)
    sid = new_id("n", 1)
    derived["stations"] = list(derived.get("stations") or []) + [
        _station(sid, "Evaluation infill station", plane.lonlat(*best))
    ]
    return recount(derived), change_record("add_served_station", added_stations=[sid], between=[ida, idb])


def _along(p, points: list[tuple]) -> float:
    """How far along the way `points` (on the plane) the way point nearest `p` is, in metres."""
    best, best_d, walked = 0.0, math.inf, 0.0
    for a, b in zip(points, points[1:], strict=False):
        q = _project(p, a, b)
        if math.dist(p, q) < best_d:
            best, best_d = walked + math.dist(a, q), math.dist(p, q)
        walked += math.dist(a, b)
    return best


def _project(p, a, b) -> tuple[float, float]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return a[0] + t * dx, a[1] + t * dy


def duplicate_stations(network: dict, polygon: list, *, stations: int = 3) -> tuple[dict, dict]:
    """B': `network` with `stations` of its stations mapped a second time under new ids, at the
    same place and with the same name, spread over the stations in id order: a tagging change."""
    existing = sorted(network.get("stations") or [], key=lambda s: str(s["id"]))
    if len(existing) < stations:
        raise DeriveError(f"the network has fewer than {stations} stations")
    # Evenly through the list, by floor: `stations` different ones even when the network has exactly
    # that many (rounding, which halves to even, picked one station twice there).
    picks = [existing[(k + 1) * len(existing) // (stations + 1)] for k in range(stations)]
    derived = _start(network)
    copies = []
    for k, station in enumerate(picks):
        copies.append({**copy.deepcopy(station), "id": new_id("dup", k + 1)})
    derived["stations"] = list(derived.get("stations") or []) + copies
    return recount(derived), change_record("duplicate_stations", added_stations=[c["id"] for c in copies],
                                     duplicated=[str(s["id"]) for s in picks])  # fmt: skip


DERIVATIONS = {
    "add_line": add_line,
    "extend_line": extend_line,
    "remove_line": remove_line,
    "add_served_station": add_served_station,
    "duplicate_stations": duplicate_stations,
}
# What each derivation takes from a case's recipe, beside the polygon.
RECIPE_KEYS = {
    "add_line": ("stations", "spacing_m"),
    "extend_line": ("stations", "spacing_m"),
    "remove_line": ("min_stations",),
    "add_served_station": (),
    "duplicate_stations": ("stations",),
}
# What a recipe's `anchor` may ask for, by derivation: add_line the largest desert of the baseline's
# newest measurement, extend_line all of its deserts (it heads for the nearest).
ANCHORS = {"add_line": ("largest_desert",), "extend_line": ("deserts",)}


def derive(network: dict, recipe: dict, polygon: list, *, deserts: list | None = None) -> tuple[dict, dict]:
    """The network a case's recipe ({"derive": <name>, ...its keys}) makes from `network` (A),
    and what was changed. `deserts` are the baseline's newest measurement's (its `deserts` list),
    used when the recipe asks for an anchor. DeriveError for a recipe this module does not know,
    or cannot make."""
    name = recipe.get("derive")
    if name not in DERIVATIONS:
        raise DeriveError(f"unknown derivation {name!r} (known: {sorted(DERIVATIONS)})")
    kwargs = {key: recipe[key] for key in RECIPE_KEYS[name] if key in recipe}
    anchor = recipe.get("anchor")
    if anchor == "largest_desert":
        kwargs["anchor"] = largest_desert(deserts)
    elif anchor == "deserts":
        kwargs["toward"] = desert_points(deserts)
    return DERIVATIONS[name](network, polygon, **kwargs)


def desert_points(deserts: list | None) -> list[list[float]]:
    """The [lon, lat] of every desert that has one, largest first."""
    usable = [d for d in deserts or [] if isinstance(d, dict)
              and all(isinstance(d.get(key), int | float) for key in ("lon", "lat"))]  # fmt: skip
    usable.sort(key=lambda d: (-(d.get("area_km2") or 0.0), d["lon"], d["lat"]))
    return [[d["lon"], d["lat"]] for d in usable]


def largest_desert(deserts: list | None) -> list[float] | None:
    """The [lon, lat] of the largest desert (built-up area beyond reach) of a measurement's
    `deserts` list (the newest history entry keeps it), or None when it has none."""
    points = desert_points(deserts)
    return points[0] if points else None
