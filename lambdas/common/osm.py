"""OpenStreetMap rail networks through the Overpass API: fetch, decimate, cap, cache rules.

The rail access task (docs/enhancements/rail-access-monitor.md §3, PR B) needs the rail lines,
stations and intermodal points inside a site polygon. The adapter fetches them here, keeps the
result in its running state for `osm_ttl_days` (`is_stale` says when to fetch again), and sends it
inline to the vision worker, whose egress stays "public imagery over HTTPS, nothing else". So the
network dict must be small: way points are decimated to `MIN_SPACING_M` and the whole thing is
capped (`cap_network`), which keeps the synchronous invoke payload near 1.5 MB.

Two allow-listed Overpass endpoints, one fallback hop: a request that times out or is refused
(429, 5xx) by the first is tried once on the mirror, and a failure on both is an `OsmError` whose
`code` the adapter records ("not measured" is never "nothing there"). Nothing here retries with
sleeps: the research tick has 120 s and the fetch gets at most two bounded requests.

This module imports no numpy, OpenCV, networkx or boto: it is imported by the adapter, and the
adapter registry is imported by every pipeline Lambda.

Data licence: OpenStreetMap data is © OpenStreetMap contributors under the ODbL
(https://www.openstreetmap.org/copyright), which asks for that credit wherever the data is shown;
`OSM_SOURCE` is the credit, in the shape of Adapter.sources.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import requests

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Overpass' own query timeout is the same figure (`[timeout:]` in the query), so a slow server
# answers with a 504 or a "timed out" remark before the client gives up on the read.
TIMEOUT_SECONDS = 25.0
_CONNECT_TIMEOUT_SECONDS = 5.0
# A hop that would get less read time than this before the caller's deadline is not made.
_MIN_HOP_SECONDS = 2.0
# The body is read in chunks of this size, so the cap is enforced while it arrives, not after.
_CHUNK_BYTES = 64 * 1024
# As many vertices as the worker contract allows a site polygon; the ring is repeated in the query.
MAX_POLYGON_VERTICES = 64
# The largest id OpenStreetMap could hand out for a long while; anything else is not an element.
_MAX_ELEMENT_ID = 2**53
# A whole city's rail data is a few MB; anything larger is not what was asked for.
MAX_RESPONSE_BYTES = 20 * 1024 * 1024

# What the worker receives (risk 2 in the design): with these caps the network is near 1.5 MB.
MAX_POINTS = 60_000
MAX_WAYS = 5_000
MAX_STATIONS = 2_000
MAX_POIS = 5_000
# The analysis grid is 20 m, so closer points land in the same pixel.
MIN_SPACING_M = 20.0
MAX_NAME_CHARS = 80

OSM_SOURCE = {
    "text": "Map data © OpenStreetMap contributors, available under the Open Database Licence",
    "label": "OpenStreetMap contributors",
    "url": "https://www.openstreetmap.org/copyright",
}

# Mean Earth radius (6371.0088 km) times pi/180: the metres in one degree of latitude, and of
# longitude at the equator. Good to 0.5 % over a city, which is all decimation needs.
_METRES_PER_DEGREE = 111_195.0

# The query. Rail, light rail and subway ways (yards, sidings and industrial lines left out, so
# the graph is the passenger network), stations and halts, the intermodal points, and bus stops
# within 400 m of a station. `{poly}` is the site as "lat lon lat lon ..." (Overpass' order).
_QUERY = """[out:json][timeout:{timeout}];
( way["railway"~"^(rail|light_rail|subway)$"]["service"!~"^(yard|siding|spur|crossover)$"]
     ["usage"!~"^(industrial|military|tourism)$"](poly:"{poly}"); ) -> .rail;
( node["railway"~"^(station|halt)$"](poly:"{poly}");
  way["railway"~"^(station|halt)$"](poly:"{poly}"); ) -> .stations;
( node["amenity"="bus_station"](poly:"{poly}"); way["amenity"="bus_station"](poly:"{poly}");
  node["amenity"="ferry_terminal"](poly:"{poly}"); way["amenity"="ferry_terminal"](poly:"{poly}");
  node["park_ride"](poly:"{poly}"); way["park_ride"](poly:"{poly}");
  node["highway"="bus_stop"](around.stations:400); ) -> .pois;
.rail out geom; .stations out center; .pois out center;
"""
# The short bbox form of the same question, for a source reference a reader can open.
_LINK_QUERY = (
    '[out:json][timeout:{timeout}][bbox:{bbox}];(way["railway"~"^(rail|light_rail|subway)$"];'
    'node["railway"~"^(station|halt)$"];);out geom;'
)
MAX_QUERY_URL_CHARS = 400

_RAIL_WAYS = ("rail", "light_rail", "subway")
_STATIONS = ("station", "halt")
_POI_AMENITIES = ("bus_station", "ferry_terminal")
_TUNNEL_VALUES = ("yes", "building_passage")
_ELEMENT_PREFIX = {"node": "n", "way": "w", "relation": "r"}
# Characters that do not belong in a name rendered in a caption or a prompt, by Unicode category:
# controls (Cc), formatting characters (Cf: zero-width, bidi overrides and isolates, the TAG block),
# surrogates, private use, unassigned, and the line and paragraph separators. The two joiners are
# formatting characters too, but Persian, Arabic and Indic names are spelt with them, so they stay.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_KEPT_JOINERS = frozenset("\u200c\u200d")


class OsmError(RuntimeError):
    """The network could not be fetched. `code` is one of "http" (a refusal or a connection
    failure), "rate_limited" (429), "timeout", "bad_response" (not the JSON asked for) or
    "too_large" (over MAX_RESPONSE_BYTES). `transient` says whether another endpoint was worth
    asking: a busy server may answer on the mirror, a refused query or an oversized reply would
    only repeat."""

    def __init__(self, code: str, detail: str = "", transient: bool = False):
        detail = detail[:300]  # a server's own words, bounded before they reach a log or a state
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.transient = code, detail, transient


def _coord(value) -> str:
    """A coordinate to 5 decimals (about a metre), without trailing zeros."""
    text = f"{float(value):.5f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _ring(polygon) -> list[tuple[float, float]]:
    """The polygon as (lon, lat) floats, closed; ValueError if it is not a polygon: fewer than 3 or
    more than MAX_POLYGON_VERTICES points, or a coordinate that is not finite or not on Earth (a
    swapped lon/lat pair fails the latitude range), so nothing odd is ever formatted into a query."""
    try:
        points = [(float(lon), float(lat)) for lon, lat in polygon]
    except (TypeError, ValueError) as exc:
        raise ValueError("polygon must be [[lon, lat], ...]") from exc
    if not 3 <= len(points) <= MAX_POLYGON_VERTICES + 1:
        raise ValueError(f"polygon needs 3 to {MAX_POLYGON_VERTICES} points")
    for lon, lat in points:
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("polygon points must be finite [lon, lat] within -180..180 and -90..90")
    return points if points[0] == points[-1] else [*points, points[0]]


def overpass_query(polygon) -> str:
    """The Overpass QL for `polygon` ([[lon, lat], ...]): rail ways with their geometry, stations
    and intermodal points with their centres, as JSON."""
    poly = " ".join(f"{_coord(lat)} {_coord(lon)}" for lon, lat in _ring(polygon))
    return _QUERY.replace("{timeout}", str(int(TIMEOUT_SECONDS))).replace("{poly}", poly)


def query_url(polygon, endpoint: str = OVERPASS_ENDPOINTS[0]) -> str:
    """A GET link to the rail ways and stations in the polygon's bounding box, for source
    references: at most MAX_QUERY_URL_CHARS, on an allow-listed endpoint (ValueError otherwise)."""
    if endpoint not in OVERPASS_ENDPOINTS:
        raise ValueError(f"endpoint must be one of {OVERPASS_ENDPOINTS}")
    ring = _ring(polygon)
    south, north = min(lat for _, lat in ring), max(lat for _, lat in ring)
    west, east = min(lon for lon, _ in ring), max(lon for lon, _ in ring)
    bbox = ",".join(_coord(v) for v in (south, west, north, east))
    query = _LINK_QUERY.replace("{timeout}", str(int(TIMEOUT_SECONDS))).replace("{bbox}", bbox)
    url = f"{endpoint}?data={quote(query, safe='')}"
    if len(url) > MAX_QUERY_URL_CHARS:  # a bbox is four numbers; this would be a bug, not data
        raise ValueError(f"query URL over {MAX_QUERY_URL_CHARS} characters")
    return url


def clean_name(raw) -> str | None:
    """`raw` as a name fit to store and show: control and formatting characters out, whitespace
    collapsed, at most MAX_NAME_CHARS; None when nothing is left (or it was not a string)."""
    if not isinstance(raw, str):
        return None
    text = re.sub(r"\s+", " ", raw)
    text = "".join(
        ch for ch in text if ch in _KEPT_JOINERS or unicodedata.category(ch) not in _DROPPED_CATEGORIES
    ).strip()
    if len(text) > MAX_NAME_CHARS:
        text = text[:MAX_NAME_CHARS].rstrip()
    return text or None


def _position(element: dict) -> tuple[float, float] | None:
    """(lon, lat) of a node, or of a way's centre (`out center`); None when there is neither."""
    center = element.get("center")
    point = element if element.get("type") == "node" else (center if isinstance(center, dict) else {})
    lon, lat = point.get("lon"), point.get("lat")
    if not _is_number(lon) or not _is_number(lat):
        return None
    return round(float(lon), 5), round(float(lat), 5)


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _poi_kind(tags: dict) -> str | None:
    """The intermodal class of an element, or None when it is not one. A `park_ride=no` is a
    car park that is explicitly not one, so it is nothing here."""
    if tags.get("amenity") in _POI_AMENITIES:
        return tags["amenity"]
    if tags.get("park_ride") and tags["park_ride"] != "no":
        return "park_ride"
    if tags.get("highway") == "bus_stop":
        return "bus_stop"
    return None


def parse_elements(elements) -> dict:
    """Overpass `elements` as {"ways", "stations", "pois"}: rail ways with their points as
    [lon, lat], stations and halts (tram stations dropped: tram ways are not fetched), and the
    intermodal points. Elements are classified by their tags, so one that Overpass printed in two
    sets lands in one list, once. Anything unknown or without a position is skipped."""
    ways, stations, pois = [], [], []
    seen: set[str] = set()
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        kind_of_element, element_number = element.get("type"), element.get("id")
        prefix = _ELEMENT_PREFIX.get(kind_of_element) if isinstance(kind_of_element, str) else None
        if (
            prefix is None
            or not isinstance(element_number, int)
            or isinstance(element_number, bool)
            or not 0 < element_number < _MAX_ELEMENT_ID
        ):
            continue
        element_id = f"{prefix}{element['id']}"
        if element_id in seen:
            continue
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        railway = tags.get("railway")
        if railway in _RAIL_WAYS and element["type"] == "way":
            points = [
                [round(float(p["lon"]), 5), round(float(p["lat"]), 5)]
                for p in element.get("geometry") or []
                if isinstance(p, dict) and _is_number(p.get("lon")) and _is_number(p.get("lat"))
            ]
            if len(points) < 2:
                continue
            seen.add(element_id)
            ways.append(
                {
                    "id": element_id,
                    "points": points,
                    "tunnel": tags.get("tunnel") in _TUNNEL_VALUES,
                    "name": clean_name(tags.get("name")),
                }
            )
            continue
        position = _position(element)
        if position is None:
            continue
        lon, lat = position
        if railway in _STATIONS:
            if tags.get("station") == "tram":
                continue
            seen.add(element_id)
            name = clean_name(tags.get("name")) or f"Unnamed station ({element_id})"
            stations.append({"id": element_id, "name": name, "lon": lon, "lat": lat, "kind": railway})
            continue
        kind = _poi_kind(tags)
        if kind is not None:
            seen.add(element_id)
            pois.append({"id": element_id, "lon": lon, "lat": lat, "kind": kind})
    return {"ways": ways, "stations": stations, "pois": pois}


def _metres_between(a, b) -> float:
    """Equirectangular distance between two [lon, lat] points: exact enough at city scale."""
    mean_lat = math.radians((float(a[1]) + float(b[1])) / 2)
    dx = (float(b[0]) - float(a[0])) * math.cos(mean_lat) * _METRES_PER_DEGREE
    dy = (float(b[1]) - float(a[1])) * _METRES_PER_DEGREE
    return math.hypot(dx, dy)


def decimate(points, min_spacing_m: float = MIN_SPACING_M) -> list:
    """`points` ([[lon, lat], ...]) with every point closer than `min_spacing_m` to the last kept
    one dropped. The first and last points are always kept, so a way's ends (the OSM nodes it
    shares with the next way) survive and the graph stays connected."""
    points = [list(p) for p in points]
    if len(points) < 3:
        return points
    kept = [points[0]]
    for point in points[1:-1]:
        if _metres_between(kept[-1], point) >= min_spacing_m:
            kept.append(point)
    kept.append(points[-1])
    return kept


def _counts(network: dict) -> dict:
    return {
        "ways": len(network["ways"]),
        "stations": len(network["stations"]),
        "pois": len(network["pois"]),
        "points": sum(len(way["points"]) for way in network["ways"]),
    }


def cap_network(network: dict, max_points: int = MAX_POINTS) -> dict:
    """`network` (at least "ways", "stations", "pois") brought under the caps, with its "counts"
    and "truncated". Over `max_points`, the ways are decimated again at 1.5x the spacing, up to
    four times (20 m -> 30, 45, 68, 101 m); if that is still too many, the longest ways are
    dropped, as that loses the fewest. Over MAX_WAYS, MAX_STATIONS or MAX_POIS, the list is cut at
    the cap. `truncated` is true whenever anything was taken away."""
    ways = [dict(way) for way in network["ways"]]
    stations, pois = list(network["stations"]), list(network["pois"])
    before = _counts(network)

    spacing = MIN_SPACING_M
    for _ in range(4):
        if sum(len(way["points"]) for way in ways) <= max_points:
            break
        spacing *= 1.5
        for way in ways:
            way["points"] = decimate(way["points"], spacing)
    if sum(len(way["points"]) for way in ways) > max_points:
        by_length = sorted(range(len(ways)), key=lambda i: (len(ways[i]["points"]), i), reverse=True)
        dropped, total = set(), sum(len(way["points"]) for way in ways)
        for index in by_length:
            if total <= max_points:
                break
            dropped.add(index)
            total -= len(ways[index]["points"])
        ways = [way for i, way in enumerate(ways) if i not in dropped]
    ways, stations, pois = ways[:MAX_WAYS], stations[:MAX_STATIONS], pois[:MAX_POIS]

    capped = {**network, "ways": ways, "stations": stations, "pois": pois}
    capped["counts"] = _counts(capped)
    capped["truncated"] = bool(network.get("truncated")) or capped["counts"] != before
    return capped


def is_stale(network, now: datetime, ttl_days) -> bool:
    """Whether the cached `network` should be fetched again: there is none, its `fetched_at` is
    missing or unreadable, or it is older than `ttl_days` at `now`."""
    if not isinstance(network, dict) or not isinstance(network.get("fetched_at"), str):
        return True
    try:
        fetched_at = datetime.fromisoformat(network["fetched_at"])
    except ValueError:
        return True
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now - fetched_at > timedelta(days=float(ttl_days))


def _request(post, endpoint: str, query: str, timeout: float) -> list:
    """One POST to `endpoint`; the reply's `elements`, or the OsmError it amounts to. Redirects are
    not followed (the allowlist is about the host actually read), and the body is streamed so the
    size cap holds while it arrives. Any other failure of the request library is a connection
    failure worth the mirror: a connection cut mid-body is one of them."""
    try:
        response = post(
            endpoint,
            data={"data": query},
            timeout=(_CONNECT_TIMEOUT_SECONDS, timeout),
            allow_redirects=False,
            stream=True,
        )
    except requests.Timeout as exc:
        raise OsmError("timeout", f"{type(exc).__name__} from {endpoint}", transient=True) from exc
    except requests.RequestException as exc:
        raise OsmError("http", f"{type(exc).__name__} from {endpoint}", transient=True) from exc
    status = response.status_code
    if 300 <= status < 400:
        _close(response)
        raise OsmError("http", f"HTTP {status} redirect from {endpoint} not followed")
    if status == 429:
        raise OsmError("rate_limited", f"HTTP {status} from {endpoint}", transient=True)
    if status == 504:
        raise OsmError("timeout", f"HTTP {status} from {endpoint}", transient=True)
    if status >= 500:
        raise OsmError("http", f"HTTP {status} from {endpoint}", transient=True)
    if status >= 400:
        raise OsmError("http", f"HTTP {status} from {endpoint}")
    return _read_elements(response, endpoint)


def _read_elements(response, endpoint: str) -> list:
    """The `elements` of a 2xx reply. The adapter must never take a partial network for a whole
    one, so a "runtime error" remark (Overpass answers 200 with what it had when the query timed
    out) is a failure too, and a timed-out one is worth the mirror."""
    body = _read_body(response, endpoint)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise OsmError("bad_response", f"not JSON from {endpoint}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise OsmError("bad_response", f"no elements from {endpoint}")
    remark = payload.get("remark")
    if isinstance(remark, str) and "error" in remark.lower():
        if "timed out" in remark.lower():
            raise OsmError("timeout", remark[:300], transient=True)
        raise OsmError("bad_response", remark[:300])
    return payload["elements"]


def _read_body(response, endpoint: str) -> bytes:
    """The reply's bytes, refused as `too_large` as soon as they pass MAX_RESPONSE_BYTES: from the
    Content-Length when there is one, else while the chunks arrive, so an oversized body is never
    held whole. A connection cut while reading is a transient failure."""
    declared = (getattr(response, "headers", None) or {}).get("Content-Length")
    if isinstance(declared, str) and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        _close(response)
        raise OsmError("too_large", f"{declared} bytes declared by {endpoint}")
    chunks, total = [], 0
    try:
        for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise OsmError("too_large", f"over {MAX_RESPONSE_BYTES} bytes from {endpoint}")
            chunks.append(chunk)
    except requests.RequestException as exc:
        raise OsmError("http", f"{type(exc).__name__} reading from {endpoint}", transient=True) from exc
    finally:
        _close(response)
    return b"".join(chunks)


def _close(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def fetch_network(
    polygon,
    *,
    http_post=None,
    endpoints=OVERPASS_ENDPOINTS,
    timeout: float = TIMEOUT_SECONDS,
    now: datetime | None = None,
    deadline: float | None = None,
    clock=time.monotonic,
) -> dict:
    """The rail network inside `polygon` ([[lon, lat], ...]) from Overpass, decimated and capped:

        {"source": "overpass", "endpoint": ..., "fetched_at": iso, "query_url": ...,
         "ways": [{"id": "w1", "points": [[lon, lat], ...], "tunnel": bool, "name": str | None}],
         "stations": [{"id": "n2", "name": str, "lon", "lat", "kind": "station" | "halt"}],
         "pois": [{"id": "n3", "lon", "lat",
                   "kind": "bus_stop" | "bus_station" | "ferry_terminal" | "park_ride"}],
         "counts": {"ways", "stations", "pois", "points"}, "truncated": bool}

    One POST per endpoint, in order, each with a 5 s connect and `timeout` read limit; on a 429,
    a 5xx, a timeout or a connection error the next endpoint is tried, once. A 4xx, a redirect, an
    oversized body or a body that is not the JSON asked for is raised at once: the mirror would
    repeat it. When every endpoint fails the last failure is raised as `OsmError`.

    Without a `deadline` the worst case is two hops of connect plus `timeout` plus the transfer.
    With one (a `clock()` value, `time.monotonic` by default), each hop gets at most the time left
    before it, less the connect allowance, and a hop that would get under _MIN_HOP_SECONDS is not
    made: the failure so far is raised, or "timeout" if there was none. `http_post` stands in for
    `requests.post` in tests; `endpoints` must be allow-listed (OVERPASS_ENDPOINTS)."""
    if not endpoints:
        raise ValueError("no endpoints")
    for endpoint in endpoints:
        if endpoint not in OVERPASS_ENDPOINTS:
            raise ValueError(f"endpoint {endpoint!r} is not one of {OVERPASS_ENDPOINTS}")
    post = http_post or requests.post
    query = overpass_query(polygon)
    fetched_at = (now or datetime.now(UTC)).isoformat()
    failure: OsmError | None = None
    for endpoint in endpoints:
        hop_timeout = timeout
        if deadline is not None:
            hop_timeout = min(timeout, deadline - clock() - _CONNECT_TIMEOUT_SECONDS)
            if hop_timeout < _MIN_HOP_SECONDS:
                raise failure or OsmError("timeout", "no time left before the deadline")
        try:
            elements = _request(post, endpoint, query, hop_timeout)
            break
        except OsmError as exc:
            failure = exc
            if not exc.transient:
                raise
    else:
        raise failure

    parsed = parse_elements(elements)
    parsed["ways"] = [{**way, "points": decimate(way["points"])} for way in parsed["ways"]]
    network = cap_network(parsed)
    return {
        "source": "overpass",
        "endpoint": endpoint,
        "fetched_at": fetched_at,
        "query_url": query_url(polygon, endpoint),
        **network,
    }
