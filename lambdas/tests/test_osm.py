"""Tests for common/osm.py: the Overpass client for rail networks.

Overpass is never called: a scripted fake stands in for `requests.post`. These hold the query
text, what is made of the elements, how the network is thinned and capped to fit the worker's
payload, the fallback between the two endpoints, and the error codes the adapter records.
"""

from __future__ import annotations

import json
import math
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from common import osm

POLY = [[151.20, -33.97], [151.26, -33.97], [151.26, -34.02], [151.20, -34.02]]
CLOSED_POLY = "-33.97 151.2 -33.97 151.26 -34.02 151.26 -34.02 151.2 -33.97 151.2"
PRIMARY, MIRROR = osm.OVERPASS_ENDPOINTS
NOW = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)
LAT = -33.9
_LON_PER_METRE = 1 / (math.cos(math.radians(LAT)) * 111_195.0)


def node(osm_id, lon, lat, **tags):
    return {"type": "node", "id": osm_id, "lon": lon, "lat": lat, "tags": tags}


def way(osm_id, points=None, center=None, **tags):
    element = {"type": "way", "id": osm_id, "tags": tags}
    if points is not None:
        element["geometry"] = [{"lon": lon, "lat": lat} for lon, lat in points]
    if center is not None:
        element["center"] = {"lon": center[0], "lat": center[1]}
    return element


def line(osm_id, count, step_m, start_lon=151.2, **tags):
    """A rail way of `count` points `step_m` apart along a parallel, as Overpass prints it."""
    points = [[start_lon + i * step_m * _LON_PER_METRE, LAT] for i in range(count)]
    return way(osm_id, points, railway="rail", **tags)


def elements():
    return [
        way(1, [[151.2, -33.9], [151.21, -33.9], [151.22, -33.91]], railway="rail", name="Illawarra Line"),
        way(2, [[151.2, -33.9], [151.2, -33.89]], railway="subway", tunnel="yes", name="City Circle"),
        way(3, [[151.21, -33.9], [151.21, -33.89]], railway="light_rail", tunnel="building_passage"),
        way(4, [[151.22, -33.9], [151.22, -33.89]], railway="rail", tunnel="no"),
        way(5, [[151.23, -33.9]], railway="rail", name="a single point is not a line"),
        node(10, 151.198, -33.892, railway="station", name="Redfern"),
        node(11, 151.19, -33.88, railway="halt"),
        way(12, center=[151.207, -33.884], railway="station", name="Central"),
        node(13, 151.2, -33.87, railway="station", station="tram", name="Haymarket tram"),
        way(14, railway="station", name="no centre, no position"),
        node(20, 151.199, -33.893, highway="bus_stop", name="Redfern stop"),
        node(21, 151.21, -33.89, amenity="bus_station"),
        way(22, center=[151.2, -33.86], amenity="ferry_terminal"),
        node(23, 151.25, -33.95, amenity="parking", park_ride="yes"),
        node(24, 151.25, -33.96, amenity="parking", park_ride="no"),
        node(30, 151.2, -33.88, railway="subway_entrance"),
        {"type": "relation", "id": 40, "tags": {"railway": "rail"}, "members": []},
        "not an element",
        {"type": "node", "id": "n1"},
    ]


class _Reply:
    """What requests.post answers with stream=True: a status, headers, and a body read in chunks."""

    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None, chunks=None):
        self.status_code, self.content, self.headers = status, body, headers or {}
        self.chunks, self.closed = chunks, False

    def iter_content(self, chunk_size: int):
        if self.chunks is not None:
            yield from self.chunks
            return
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self):
        self.closed = True


def reply(items=None, status=200, remark=None):
    payload = {"version": 0.6, "elements": items if items is not None else elements()}
    if remark is not None:
        payload["remark"] = remark
    return _Reply(json.dumps(payload).encode(), status)


class FakeOverpass:
    """Answers each POST in turn from `outcomes` (a reply or an exception to raise), recording
    what was asked."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls, self.kwargs = list(outcomes), [], []

    def __call__(self, url, data, timeout, **kwargs):
        self.calls.append((url, data, timeout))
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fetch(*outcomes, **kwargs):
    fake = FakeOverpass(*outcomes)
    return osm.fetch_network(POLY, http_post=fake, now=NOW, **kwargs), fake


def ticks(*values):
    """A clock that reads `values` in turn, then the last one for good."""
    left = list(values)
    return lambda: left.pop(0) if len(left) > 1 else left[0]


# --- the module's footprint ------------------------------------------------------------------


def test_importing_the_module_brings_in_no_numpy_opencv_networkx_or_boto():
    # In a fresh interpreter none of them enters sys.modules on import. (Not an in-process reload:
    # that would leave this process with two OsmError classes for the rest of the session.)
    lambdas_dir = Path(osm.__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import common.osm; "
        "heavy = {'numpy', 'cv2', 'networkx', 'boto3', 'botocore'}; "
        "print(sorted(heavy & {m.split('.')[0] for m in sys.modules}))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(lambdas_dir)], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_the_endpoints_are_the_two_allow_listed_overpass_servers_and_the_credit_names_osm():
    assert osm.OVERPASS_ENDPOINTS == (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    )
    assert set(osm.OSM_SOURCE) == {"text", "label", "url"}
    assert "OpenStreetMap contributors" in osm.OSM_SOURCE["text"]
    assert osm.OSM_SOURCE["url"] == "https://www.openstreetmap.org/copyright"


def test_an_osm_error_carries_its_code_and_detail():
    error = osm.OsmError("timeout", "HTTP 504")
    assert isinstance(error, RuntimeError)
    assert (error.code, error.detail, str(error)) == ("timeout", "HTTP 504", "timeout: HTTP 504")
    assert str(osm.OsmError("http")) == "http"


# --- the query ---------------------------------------------------------------------------------


def test_the_query_selects_rail_stations_and_the_intermodal_classes_as_json():
    query = osm.overpass_query(POLY)

    assert query.startswith("[out:json][timeout:25];")
    assert 'way["railway"~"^(rail|light_rail|subway)$"]["service"!~"^(yard|siding|spur|crossover)$"]' in query
    assert '["usage"!~"^(industrial|military|tourism)$"]' in query
    assert 'node["railway"~"^(station|halt)$"]' in query and 'way["railway"~"^(station|halt)$"]' in query
    for selector in ('["amenity"="bus_station"]', '["amenity"="ferry_terminal"]', '["park_ride"]'):
        assert f"node{selector}" in query and f"way{selector}" in query
    assert 'node["highway"="bus_stop"](around.stations:400);' in query
    assert "-> .rail;" in query and "-> .stations;" in query and "-> .pois;" in query
    assert query.rstrip().endswith(".rail out geom; .stations out center; .pois out center;")


# The query as it always was with the default 25 s, byte for byte.
DEFAULT_QUERY = (
    "[out:json][timeout:25];\n"
    '( way["railway"~"^(rail|light_rail|subway)$"]["service"!~"^(yard|siding|spur|crossover)$"]\n'
    '     ["usage"!~"^(industrial|military|tourism)$"](poly:"{p}"); ) -> .rail;\n'
    '( node["railway"~"^(station|halt)$"](poly:"{p}");\n'
    '  way["railway"~"^(station|halt)$"](poly:"{p}"); ) -> .stations;\n'
    '( node["amenity"="bus_station"](poly:"{p}"); way["amenity"="bus_station"](poly:"{p}");\n'
    '  node["amenity"="ferry_terminal"](poly:"{p}"); way["amenity"="ferry_terminal"](poly:"{p}");\n'
    '  node["park_ride"](poly:"{p}"); way["park_ride"](poly:"{p}");\n'
    '  node["highway"="bus_stop"](around.stations:400); ) -> .pois;\n'
    ".rail out geom; .stations out center; .pois out center;\n"
).replace("{p}", CLOSED_POLY)


def test_the_default_query_is_unchanged_and_a_hops_read_timeout_is_its_timeout_rounded_down():
    """Overpass' `[timeout:]` is a server-side limit on the query's runtime: each hop declares the
    read timeout it gets, in whole seconds rounded down, so the server never aborts a query the
    client would still wait for, nor keeps running one the client has given up on."""
    assert osm.overpass_query(POLY) == osm.overpass_query(POLY, osm.TIMEOUT_SECONDS) == DEFAULT_QUERY
    assert osm.TIMEOUT_SECONDS == 25.0
    longer = osm.overpass_query(POLY, 57.9)
    assert longer == DEFAULT_QUERY.replace("[timeout:25];", "[timeout:57];", 1)
    assert osm.overpass_query(POLY, 65.0).startswith("[out:json][timeout:65];\n")
    assert osm.overpass_query(POLY, 2.0).startswith("[out:json][timeout:2];\n")  # _MIN_HOP_SECONDS
    assert osm.overpass_query(POLY, 0.4).startswith("[out:json][timeout:1];\n")  # never 0
    # A hop meant to read for 25 s, measured from a clock read a moment after its deadline's, reads
    # for a hair under it: still 25. A hundredth of a second short is a second short.
    assert osm.overpass_query(POLY, 24.99999) == DEFAULT_QUERY
    assert osm.overpass_query(POLY, 24.98).startswith("[out:json][timeout:24];\n")
    with pytest.raises(ValueError):
        osm.overpass_query([[151.2, -33.9]], 60.0)


def test_the_poly_is_closed_lat_lon_pairs_and_the_same_in_every_selector():
    query = osm.overpass_query(POLY)

    assert query.count(f'(poly:"{CLOSED_POLY}")') == 9  # rail, 2 station, 6 intermodal selectors
    assert query.count("(poly:") == 9
    # An already closed ring is not closed twice.
    assert osm.overpass_query([*POLY, POLY[0]]) == query


def test_coordinates_are_written_to_a_metre_without_trailing_zeros():
    query = osm.overpass_query([[151.2000001, -33.0], [151.26, -33.0], [151.26, -34.123456]])

    assert '(poly:"-33 151.2 -33 151.26 -34.12346 151.26 -33 151.2")' in query


@pytest.mark.parametrize(
    "polygon",
    [
        [],
        [[151.2, -33.9], [151.3, -33.9]],
        [[151.2, "x"], [1, 2], [3, 4]],
        "poly",
        [[151.2, float("nan")], [151.3, -33.9], [151.3, -34.0]],
        [[float("inf"), -33.9], [151.3, -33.9], [151.3, -34.0]],
        [[151.2, -33.9], [151.3, -33.9], [151.3, 95.0]],  # off the planet
        [[-33.9, 151.2], [-33.9, 151.3], [-34.0, 151.3]],  # lon and lat swapped
        [[151.2 + n / 1000, -33.9] for n in range(66)],  # more vertices than a site may have
    ],
)
def test_a_polygon_that_is_not_one_is_refused_before_any_request(polygon):
    with pytest.raises(ValueError):
        osm.overpass_query(polygon)
    with pytest.raises(ValueError):
        osm.query_url(polygon)


# --- the query URL for source references ------------------------------------------------------


def test_the_query_url_is_short_on_an_allow_listed_endpoint_and_asks_for_the_bbox():
    from urllib.parse import parse_qs, urlsplit

    url = osm.query_url(POLY)

    assert len(url) <= 400
    assert url.startswith(PRIMARY + "?data=")
    query = parse_qs(urlsplit(url).query)["data"][0]
    assert "[bbox:-34.02,151.2,-33.97,151.26]" in query  # south, west, north, east
    assert 'way["railway"~"^(rail|light_rail|subway)$"]' in query
    assert 'node["railway"~"^(station|halt)$"]' in query
    assert query.startswith("[out:json][timeout:25]") and query.endswith("out geom;")
    assert osm.query_url(POLY, MIRROR).startswith(MIRROR + "?data=")


def test_the_query_url_refuses_an_endpoint_off_the_allow_list():
    with pytest.raises(ValueError, match="endpoint"):
        osm.query_url(POLY, "https://overpass.example.org/api/interpreter")


# --- parsing -----------------------------------------------------------------------------------


def test_parsing_builds_ways_with_lon_lat_points_tunnels_and_names():
    ways = osm.parse_elements(elements())["ways"]

    assert [w["id"] for w in ways] == ["w1", "w2", "w3", "w4"]
    assert ways[0] == {
        "id": "w1",
        "points": [[151.2, -33.9], [151.21, -33.9], [151.22, -33.91]],
        "tunnel": False,
        "name": "Illawarra Line",
    }
    assert [w["tunnel"] for w in ways] == [False, True, True, False]
    assert [w["name"] for w in ways] == ["Illawarra Line", "City Circle", None, None]


def test_parsing_builds_stations_from_nodes_and_station_areas_and_drops_tram_stations():
    stations = osm.parse_elements(elements())["stations"]

    assert stations == [
        {"id": "n10", "name": "Redfern", "lon": 151.198, "lat": -33.892, "kind": "station"},
        {"id": "n11", "name": "Unnamed station (n11)", "lon": 151.19, "lat": -33.88, "kind": "halt"},
        {"id": "w12", "name": "Central", "lon": 151.207, "lat": -33.884, "kind": "station"},
    ]


def test_parsing_classifies_the_intermodal_points_and_drops_what_is_not_one():
    pois = osm.parse_elements(elements())["pois"]

    assert pois == [
        {"id": "n20", "lon": 151.199, "lat": -33.893, "kind": "bus_stop"},
        {"id": "n21", "lon": 151.21, "lat": -33.89, "kind": "bus_station"},
        {"id": "w22", "lon": 151.2, "lat": -33.86, "kind": "ferry_terminal"},
        {"id": "n23", "lon": 151.25, "lat": -33.95, "kind": "park_ride"},
    ]


def test_parsing_takes_each_element_once_and_tolerates_junk():
    network = osm.parse_elements([*elements(), *elements()])

    assert [w["id"] for w in network["ways"]] == ["w1", "w2", "w3", "w4"]
    assert len(network["stations"]) == 3 and len(network["pois"]) == 4
    assert osm.parse_elements(None) == {"ways": [], "stations": [], "pois": []}
    broken = {"type": "way", "id": 1, "tags": {"railway": "rail"}, "geometry": "x"}
    assert osm.parse_elements([broken])["ways"] == []
    json.dumps(network)


def test_station_names_are_sanitised_and_a_bus_station_is_not_a_park_and_ride():
    parsed = osm.parse_elements(
        [
            node(1, 151.2, -33.9, railway="station", name="  Town\x00 Hall \u202e  "),
            node(2, 151.2, -33.9, railway="station", name="\x1f"),
            node(3, 151.2, -33.9, amenity="bus_station", park_ride="yes"),
        ]
    )

    assert [s["name"] for s in parsed["stations"]] == ["Town Hall", "Unnamed station (n2)"]
    assert parsed["pois"][0]["kind"] == "bus_station"


# --- names -------------------------------------------------------------------------------------


def test_clean_name_strips_controls_collapses_whitespace_and_caps_the_length():
    assert osm.clean_name("  Red\x00fern \t\n Station\u200b  ") == "Redfern Station"
    assert osm.clean_name("x" * 100) == "x" * osm.MAX_NAME_CHARS
    assert osm.clean_name("word " * 30).endswith("word") and len(osm.clean_name("word " * 30)) <= 80
    assert osm.clean_name("Gare de l'Est") == "Gare de l'Est"


@pytest.mark.parametrize("raw", ["", "   ", "\x00\x1f", None, 5, ["Redfern"]])
def test_clean_name_is_none_when_nothing_usable_is_left(raw):
    assert osm.clean_name(raw) is None


# --- decimation --------------------------------------------------------------------------------


def assert_greedy(points, kept, spacing):
    """`kept` is `points` walked once: a point stays when it is `spacing` or more from the last
    kept one, and the ends always stay."""
    assert kept[0] == points[0] and kept[-1] == points[-1]
    assert [p for p in points if p in kept] == kept  # order kept, nothing invented
    for a, b in zip(kept, kept[1:]):  # each gap between kept neighbours
        between = points[points.index(a) + 1 : points.index(b)]
        assert all(osm._metres_between(a, p) < spacing for p in between)
        assert osm._metres_between(a, b) >= spacing or b == points[-1]


def test_decimation_drops_close_points_and_keeps_both_ends():
    points = line(1, 41, 5.0)["geometry"]  # 200 m of track, a point every 5 m
    points = [[p["lon"], p["lat"]] for p in points]

    kept = osm.decimate(points)

    assert_greedy(points, kept, 20.0)
    assert 9 <= len(kept) <= 11  # about one point in four, plus the last


def test_decimation_measures_metres_on_the_ground_not_degrees():
    # 0.0002 deg of longitude is 18.5 m at this latitude: dropped at 20 m, kept at 15 m.
    points = [[151.2, LAT], [151.2002, LAT], [151.2004, LAT], [151.2006, LAT], [151.2008, LAT]]

    assert osm.decimate(points, 20.0) == [[151.2, LAT], [151.2004, LAT], [151.2008, LAT]]
    assert osm.decimate(points, 15.0) == points


def test_decimation_leaves_short_ways_alone_is_idempotent_and_never_mutates_its_input():
    assert osm.decimate([]) == []
    assert osm.decimate([[151.2, LAT]]) == [[151.2, LAT]]
    two = [[151.2, LAT], [151.20001, LAT]]
    assert osm.decimate(two) == two
    points = [[151.2 + i * 1e-5, LAT] for i in range(50)]
    once = osm.decimate(points)
    assert osm.decimate(once) == once
    assert points == [[151.2 + i * 1e-5, LAT] for i in range(50)]
    # The last point is kept even when it is closer than the spacing to the previous kept one.
    tail = [[151.2, LAT], [151.2004, LAT], [151.20041, LAT]]
    assert osm.decimate(tail) == tail


# --- capping -----------------------------------------------------------------------------------


def network_of(*ways, stations=(), pois=()):
    parsed = osm.parse_elements([*ways, *stations, *pois])
    return {"source": "overpass", **parsed}


def test_a_network_under_the_caps_is_unchanged_counted_and_not_truncated():
    station = node(10, 151.2, LAT, railway="station")
    network = network_of(line(1, 10, 20.0), line(2, 5, 20.0), stations=[station])

    capped = osm.cap_network(network)

    assert capped["ways"] == network["ways"] and capped["stations"] == network["stations"]
    assert capped["counts"] == {"ways": 2, "stations": 1, "pois": 0, "points": 15}
    assert capped["truncated"] is False
    assert capped["source"] == "overpass"


def test_over_the_point_budget_the_spacing_grows_before_any_way_is_dropped():
    network = network_of(line(1, 1000, 20.0), line(2, 200, 20.0))

    capped = osm.cap_network(network, max_points=700)

    assert [w["id"] for w in capped["ways"]] == ["w1", "w2"]
    assert capped["counts"]["points"] <= 700
    assert capped["counts"]["points"] > 500  # 30 m spacing keeps every other point; 45 m was not needed
    assert capped["truncated"] is True
    for before, after in zip(network["ways"], capped["ways"]):  # the ends of every way survive
        assert after["points"][0] == before["points"][0] and after["points"][-1] == before["points"][-1]


def test_when_coarser_spacing_is_not_enough_the_longest_ways_go_first():
    # Points 200 m apart survive every spacing up to 101 m, so only dropping ways can help.
    network = network_of(line(1, 100, 200.0), line(2, 50, 200.0), line(3, 30, 200.0))

    capped = osm.cap_network(network, max_points=90)

    assert [w["id"] for w in capped["ways"]] == ["w2", "w3"]
    assert capped["counts"] == {"ways": 2, "stations": 0, "pois": 0, "points": 80}
    assert capped["truncated"] is True


def test_the_way_station_and_poi_caps_cut_the_lists_and_say_so(monkeypatch):
    monkeypatch.setattr(osm, "MAX_WAYS", 2)
    monkeypatch.setattr(osm, "MAX_STATIONS", 1)
    monkeypatch.setattr(osm, "MAX_POIS", 1)
    network = network_of(
        line(1, 3, 20.0),
        line(2, 3, 20.0),
        line(3, 3, 20.0),
        stations=[node(10, 151.2, LAT, railway="station"), node(11, 151.3, LAT, railway="halt")],
        pois=[node(20, 151.2, LAT, highway="bus_stop"), node(21, 151.3, LAT, amenity="bus_station")],
    )

    capped = osm.cap_network(network)

    assert [w["id"] for w in capped["ways"]] == ["w1", "w2"]
    assert [s["id"] for s in capped["stations"]] == ["n10"]
    assert [p["id"] for p in capped["pois"]] == ["n20"]
    assert capped["counts"] == {"ways": 2, "stations": 1, "pois": 1, "points": 6}
    assert capped["truncated"] is True


def test_capping_again_keeps_an_earlier_truncation_on_record():
    network = {**network_of(line(1, 3, 20.0)), "truncated": True}

    assert osm.cap_network(network)["truncated"] is True


# --- fetching ----------------------------------------------------------------------------------


def test_every_hop_names_itself_with_the_bots_user_agent():
    """Overpass refuses an anonymous python-requests client: overpass-api.de with a 406 that is
    not worth the mirror, the mirror with a 429 asking for a User-Agent (dev, 2026-10-16)."""
    _, fake = fetch(reply(status=429), reply())

    assert len(fake.kwargs) == 2
    for kwargs in fake.kwargs:
        assert kwargs["headers"] == {"User-Agent": osm.USER_AGENT}
    assert osm.USER_AGENT.startswith("BloggerBearResearchBot/") and "+https://" in osm.USER_AGENT


def test_fetch_posts_the_query_once_and_returns_the_network_shape():
    network, fake = fetch(reply())

    assert fake.calls == [(PRIMARY, {"data": osm.overpass_query(POLY)}, (5, 25.0))]
    assert network["source"] == "overpass" and network["endpoint"] == PRIMARY
    assert network["fetched_at"] == "2026-10-10T03:00:00+00:00"
    assert network["query_url"] == osm.query_url(POLY, PRIMARY)
    assert [w["id"] for w in network["ways"]] == ["w1", "w2", "w3", "w4"]
    assert [s["name"] for s in network["stations"]] == ["Redfern", "Unnamed station (n11)", "Central"]
    assert [p["kind"] for p in network["pois"]] == ["bus_stop", "bus_station", "ferry_terminal", "park_ride"]
    assert network["counts"] == {"ways": 4, "stations": 3, "pois": 4, "points": 9}
    assert network["truncated"] is False
    assert list(network) == [
        "source", "endpoint", "fetched_at", "query_url", "ways", "stations", "pois", "counts", "truncated"
    ]  # fmt: skip
    json.dumps(network)


def test_fetch_decimates_the_ways_and_caps_the_network():
    dense = line(1, 41, 5.0)  # 200 m of track with a point every 5 m
    points = [[round(p["lon"], 5), round(p["lat"], 5)] for p in dense["geometry"]]

    network, _ = fetch(reply([dense]))

    kept = network["ways"][0]["points"]
    assert_greedy(points, kept, 20.0)
    assert network["counts"] == {"ways": 1, "stations": 0, "pois": 0, "points": len(kept)}
    assert 9 <= len(kept) <= 11


def test_fetch_uses_the_given_timeout_and_the_clock_when_no_time_is_given():
    before = datetime.now(UTC)
    fake = FakeOverpass(reply())

    network = osm.fetch_network(POLY, http_post=fake, timeout=12.5)

    assert fake.calls[0][2] == (5, 12.5)
    fetched_at = datetime.fromisoformat(network["fetched_at"])
    assert fetched_at.tzinfo is not None and before <= fetched_at <= datetime.now(UTC)


@pytest.mark.parametrize(
    "first",
    [
        reply(status=429),
        reply(status=500),
        reply(status=503),
        reply(status=504),
        requests.ReadTimeout("read timed out"),
        requests.ConnectTimeout("connect timed out"),
        requests.ConnectionError("refused"),
        reply([], remark="runtime error: Query timed out in \"query\" at line 1 after 26 seconds."),
        requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead(1234 bytes read)"),
    ],
    ids=["429", "500", "503", "504", "read-timeout", "connect-timeout", "connection", "timed-out-remark",
         "cut-mid-body"],  # fmt: skip
)
def test_a_busy_or_unreachable_primary_is_followed_by_one_try_on_the_mirror(first):
    network, fake = fetch(first, reply())

    assert [call[0] for call in fake.calls] == [PRIMARY, MIRROR]
    assert fake.calls[1][1] == fake.calls[0][1]
    assert network["endpoint"] == MIRROR
    assert network["query_url"].startswith(MIRROR)
    assert network["counts"]["stations"] == 3


@pytest.mark.parametrize(
    "outcomes, code",
    [
        ((reply(status=429), reply(status=503)), "http"),
        ((requests.ReadTimeout("slow"), reply(status=429)), "rate_limited"),
        ((requests.ConnectionError("down"), requests.ReadTimeout("slow")), "timeout"),
        ((reply(status=503), reply(status=504)), "timeout"),
        ((reply(status=502), requests.ConnectionError("down")), "http"),
    ],
    ids=["429-then-503", "timeout-then-429", "connection-then-timeout", "503-then-504", "502-then-conn"],
)
def test_when_both_endpoints_fail_the_last_failure_is_the_error(outcomes, code):
    with pytest.raises(osm.OsmError) as info:
        fetch(*outcomes)

    assert info.value.code == code
    assert info.value.code in {"http", "rate_limited", "timeout", "bad_response", "too_large"}
    assert MIRROR in info.value.detail  # the last failure names the endpoint it came from


def test_a_refused_query_is_an_http_error_at_once_with_no_mirror_try():
    with pytest.raises(osm.OsmError) as info:
        fetch(reply(status=400), reply())

    assert info.value.code == "http" and "HTTP 400" in info.value.detail


def test_an_oversized_body_is_refused_without_reading_it_as_a_network(monkeypatch):
    monkeypatch.setattr(osm, "MAX_RESPONSE_BYTES", 100)
    fake = FakeOverpass(reply(), reply())

    with pytest.raises(osm.OsmError) as info:
        osm.fetch_network(POLY, http_post=fake, now=NOW)

    assert info.value.code == "too_large" and len(fake.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"<html>The server is too busy</html>",
        b'{"version": 0.6}',
        b'{"elements": {"n1": 1}}',
        b"[]",
        b"",
    ],
    ids=["html", "no-elements", "elements-not-a-list", "a-list", "empty"],
)
def test_a_malformed_body_is_a_bad_response(body):
    fake = FakeOverpass(_Reply(body), reply())

    with pytest.raises(osm.OsmError) as info:
        osm.fetch_network(POLY, http_post=fake, now=NOW)

    assert info.value.code == "bad_response" and len(fake.calls) == 1


def test_a_runtime_error_other_than_a_timeout_is_a_bad_response_not_a_partial_network():
    with pytest.raises(osm.OsmError) as info:
        fetch(reply([], remark="runtime error: Query ran out of memory in ..."), reply())

    assert info.value.code == "bad_response" and "out of memory" in info.value.detail


def test_an_informational_remark_is_not_a_failure():
    network, _ = fetch(reply(remark="The server has a backlog of 3 queries."))

    assert network["counts"]["ways"] == 4


def test_the_endpoints_can_be_narrowed_but_not_replaced():
    network, fake = fetch(reply(), endpoints=(MIRROR,))
    assert [call[0] for call in fake.calls] == [MIRROR] and network["endpoint"] == MIRROR

    fake = FakeOverpass(reply())
    with pytest.raises(ValueError, match="allow|not one of"):
        osm.fetch_network(POLY, http_post=fake, endpoints=("https://overpass.example.org/api/interpreter",))
    with pytest.raises(ValueError):
        osm.fetch_network(POLY, http_post=fake, endpoints=())
    assert fake.calls == []


def test_a_bad_polygon_never_reaches_the_network():
    fake = FakeOverpass(reply())

    with pytest.raises(ValueError):
        osm.fetch_network([[151.2, -33.9]], http_post=fake)

    assert fake.calls == []


# --- staleness ---------------------------------------------------------------------------------


def test_a_network_is_stale_when_missing_unreadable_or_past_its_ttl():
    fresh = {"fetched_at": (NOW - timedelta(days=29)).isoformat()}
    old = {"fetched_at": (NOW - timedelta(days=30, seconds=1)).isoformat()}

    assert osm.is_stale(None, NOW, 30) is True
    assert osm.is_stale({}, NOW, 30) is True
    assert osm.is_stale({"fetched_at": "last week"}, NOW, 30) is True
    assert osm.is_stale({"fetched_at": 1_700_000_000}, NOW, 30) is True
    assert osm.is_stale(fresh, NOW, 30) is False
    assert osm.is_stale(old, NOW, 30) is True
    assert osm.is_stale(fresh, NOW, 7) is True
    assert osm.is_stale(fresh, NOW, "29.5") is False


def test_staleness_reads_naive_timestamps_as_utc():
    naive = {"fetched_at": (NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()}

    assert osm.is_stale(naive, NOW, 30) is False
    assert osm.is_stale(naive, NOW.replace(tzinfo=None), 30) is False
    assert osm.is_stale(naive, NOW + timedelta(days=30), 30) is True


# --- what the review added: redirects, streaming, the deadline, odd shapes ----------------------


def test_every_request_refuses_redirects_and_streams_the_body():
    _, fake = fetch(reply())
    assert fake.kwargs == [
        {"headers": {"User-Agent": osm.USER_AGENT}, "allow_redirects": False, "stream": True}
    ]


def test_a_redirect_is_refused_and_no_second_host_is_asked():
    with pytest.raises(osm.OsmError) as caught:
        fetch(reply(status=302), reply())
    assert caught.value.code == "http" and "redirect" in caught.value.detail
    # The fake recorded one call: the mirror would only be redirected too.


def test_an_oversized_body_is_refused_from_its_content_length_before_reading():
    never_read = _Reply(b"{}", headers={"Content-Length": str(osm.MAX_RESPONSE_BYTES + 1)}, chunks=[])
    with pytest.raises(osm.OsmError) as caught:
        fetch(never_read)
    assert caught.value.code == "too_large" and never_read.closed


def test_a_body_that_grows_past_the_cap_is_refused_while_it_arrives():
    handed_over = []

    def endless():
        while True:
            chunk = b"x" * osm._CHUNK_BYTES
            handed_over.append(chunk)
            yield chunk

    with pytest.raises(osm.OsmError) as caught:
        fetch(_Reply(b"", chunks=endless()))
    assert caught.value.code == "too_large"
    # Read up to the cap and one chunk more, never the whole stream.
    assert len(handed_over) <= osm.MAX_RESPONSE_BYTES // osm._CHUNK_BYTES + 1


def test_a_connection_cut_while_reading_is_tried_on_the_mirror():
    def cut():
        yield b'{"elements": '
        raise requests.exceptions.ChunkedEncodingError("Connection broken")

    network, fake = fetch(_Reply(b"", chunks=cut()), reply())
    assert [call[0] for call in fake.calls] == [PRIMARY, MIRROR]
    assert network["endpoint"] == MIRROR


def test_the_deadline_bounds_each_hop_and_skips_a_hop_with_no_time():
    # 40 s left: the first hop keeps the default; after it, 12 s left gives the mirror 12 - 5 = 7 s.
    _, fake = fetch(reply(status=503), reply(), deadline=140.0, clock=ticks(100.0, 128.0))
    assert [call[2] for call in fake.calls] == [(5.0, 25.0), (5.0, 7.0)]

    # With under the minimum left for the mirror, the first failure is what is raised.
    with pytest.raises(osm.OsmError) as caught:
        fetch(reply(status=503), reply(), deadline=140.0, clock=ticks(100.0, 134.0))
    assert caught.value.code == "http" and "503" in caught.value.detail

    # And with no time at all before the first hop, it is a timeout, with nothing asked.
    with pytest.raises(osm.OsmError) as caught:
        fetch(reply(), deadline=100.0, clock=lambda: 99.0)
    assert caught.value.code == "timeout"


def hop_timeout(call) -> str:
    """The `[timeout:N]` a hop's query declared."""
    return call[1]["data"].split(";", 1)[0].removeprefix("[out:json]")


def test_each_hops_query_declares_the_read_timeout_that_hop_gets():
    """A first fetch (the rail adapter's FIRST_FETCH_TIMEOUT_SECONDS of 75 s a hop, 58.4 s to its
    deadline): the first hop reads for 58.4 - 5 = 53.4 s and its query says 53; the mirror, after a
    503 at 40 s, gets 13.4 s and says 13. Without a deadline both hops ask for the timeout given."""
    network, fake = fetch(reply(status=503), reply(), timeout=75.0, deadline=158.4, clock=ticks(100.0, 140.0))
    assert [call[2] for call in fake.calls] == [(5.0, pytest.approx(53.4)), (5.0, pytest.approx(13.4))]
    assert [hop_timeout(call) for call in fake.calls] == ["[timeout:53]", "[timeout:13]"]
    assert fake.calls[0][1]["data"] == osm.overpass_query(POLY, 53.4)
    assert network["endpoint"] == MIRROR

    # The hop cap holds when the deadline is further off than it.
    _, fake = fetch(reply(), timeout=75.0, deadline=300.0, clock=ticks(100.0))
    assert fake.calls[0][2] == (5.0, 75.0) and hop_timeout(fake.calls[0]) == "[timeout:75]"

    _, fake = fetch(reply(status=429), reply(), timeout=40.0)
    assert [call[2] for call in fake.calls] == [(5.0, 40.0), (5.0, 40.0)]
    assert [hop_timeout(call) for call in fake.calls] == ["[timeout:40]", "[timeout:40]"]

    # The default, as before: 25 s a hop, and the query as it always was.
    _, fake = fetch(reply())
    assert fake.calls[0][1]["data"] == DEFAULT_QUERY and fake.calls[0][2] == (5, 25.0)

    # A refresh (the adapter's 30 s deadline, read a microsecond before the hop reads the clock)
    # still asks for 25, as it did before the query followed the read.
    _, fake = fetch(reply(), deadline=130.0, clock=ticks(100.000001))
    assert fake.calls[0][1]["data"] == DEFAULT_QUERY and fake.calls[0][2] == (5.0, pytest.approx(25.0))


class Clock:
    """A scripted clock the replies below advance, as a slow server would."""

    def __init__(self, now=0.0):
        self.now, self.timers = now, []

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """A scripted clock, with osm's timer (the _Shutter's) on it: armed for `interval` scripted
    seconds and fired by a StreamedReply whose read is still waiting when it is due."""
    clock = Clock()

    class ScriptedTimer:
        def __init__(self, interval, function, args=()):
            self.due, self.function, self.args = clock.now + interval, function, args
            self.started = self.cancelled = self.fired = False
            clock.timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def fire(self):
            self.fired = True
            self.function(*self.args)

    monkeypatch.setattr(osm, "Timer", ScriptedTimer)
    return clock


class StreamedReply(_Reply):
    """A 200 streamed as requests gives it, under the scripted clock: `parts`, each (seconds of
    silence, bytes), read in turn and then, with `stall`, silence with the connection held open.
    A read waits for its bytes, for the read timeout (raised as requests raises it mid-body) or for
    the reply to be shut (urllib3's HTTPResponse.shutdown, `raw.shutdown`): a chunked or sized body
    then breaks off, and one read to its connection's close just ends."""

    def __init__(self, clock, parts, read_timeout, stall=True, read_to_close=False, shuttable=True):
        super().__init__(b"")
        self.clock, self.parts, self.read_timeout = clock, list(parts), read_timeout
        self.stall, self.read_to_close, self.shut, self.read = stall, read_to_close, 0, []
        if shuttable:
            self.raw = SimpleNamespace(shutdown=self._shutdown)

    def _shutdown(self):
        self.shut += 1

    def _wait(self, seconds):
        """Advances the clock to the end of a read that waits `seconds` for its bytes, and says how it
        ended: the bytes came, the read timed out, or the shutter's timer was due."""
        timer = next((t for t in self.clock.timers if t.started and not (t.cancelled or t.fired)), None)
        ends = {
            "bytes": self.clock.now + seconds,
            "timeout": self.clock.now + self.read_timeout,
            "shut": timer.due if timer else math.inf,
        }
        how = min(ends, key=ends.get)
        self.clock.now = ends[how]
        if how == "shut":
            timer.fire()
        return how

    def iter_content(self, chunk_size):
        for silence, data in [*self.parts, (math.inf, b"")] if self.stall else self.parts:
            how = self._wait(silence)
            if how == "timeout":
                raise requests.exceptions.ConnectionError("Read timed out.")
            if how == "shut" and self.read_to_close:
                return
            if how == "shut":
                raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
            self.read.append(data)
            yield data


# A big city's reply at the rail adapter's 80 s budget: the hop starts 2 s in with a 70 s deadline, so
# it reads for 63 s; Overpass sends its preamble at once, runs the query for 53 s, then streams.
HOP_START, DEADLINE, HOP_READ = 2.0, 70.0, 63.0
PREAMBLE, ELEMENTS = b'{"version": 0.6, ', b'"elements": ' + json.dumps(elements()).encode()


@pytest.mark.parametrize("read_to_close", [False, True], ids=["chunked", "read-to-close"])
def test_a_body_that_stalls_after_its_first_bytes_ends_at_the_deadline_as_a_timeout(clock, read_to_close):
    """A read timeout bounds each wait between two reads, not the body: a connection that goes
    silent after the body started (a proxy hung mid-reply, a peer gone without a FIN) would hold
    the hop for a whole read timeout more, 63 s past a 70 s deadline here. The shutter shuts the
    reply at the deadline, the stalled read ends there, and the hop is a transient "timeout",
    never a partial network; with no time left the mirror is not asked."""
    clock.now = HOP_START
    parts = [(0.0, PREAMBLE), (53.0, ELEMENTS[:100]), (3.0, ELEMENTS[100:200])]
    stalls = StreamedReply(clock, parts, HOP_READ, read_to_close=read_to_close)
    with pytest.raises(osm.OsmError) as caught:
        fetch(stalls, reply(), timeout=75.0, deadline=DEADLINE, clock=clock)
    assert (caught.value.code, caught.value.transient) == ("timeout", True)
    assert "still arriving at the deadline" in caught.value.detail
    assert clock.now == DEADLINE  # not 58 + 63 = 121
    (timer,) = clock.timers
    assert timer.due == DEADLINE and timer.fired and stalls.shut == 1 and stalls.closed
    assert stalls.read == [PREAMBLE, ELEMENTS[:100], ELEMENTS[100:200]]


def test_a_body_still_arriving_at_the_deadline_is_cut_there_even_when_it_cannot_be_shut(clock):
    """A body that keeps trickling in never trips the read timeout; and a reply with nothing to shut
    (a stand-in, an older urllib3) is cut by the clock, checked after each chunk."""
    clock.now = HOP_START
    trickle = [(0.0, PREAMBLE)] + [(4.0, b" ")] * 30
    slow = StreamedReply(clock, trickle, HOP_READ, stall=False, shuttable=False)
    with pytest.raises(osm.OsmError) as caught:
        fetch(slow, timeout=75.0, deadline=DEADLINE, clock=clock)
    assert caught.value.code == "timeout" and clock.now == DEADLINE and len(slow.read) == 18
    assert clock.timers == []  # nothing to shut, no timer


def test_a_body_read_in_time_is_the_network_and_the_shutter_is_disarmed(clock):
    clock.now = HOP_START
    parts = [(0.0, PREAMBLE), (50.0, ELEMENTS[:-1]), (1.0, ELEMENTS[-1:] + b"}")]
    timely = StreamedReply(clock, parts, HOP_READ, stall=False)
    network, _ = fetch(timely, timeout=75.0, deadline=DEADLINE, clock=clock)
    assert network["counts"]["stations"] == 3 and clock.now == 53.0
    (timer,) = clock.timers
    assert timer.cancelled and not timer.fired and timely.shut == 0

    # Without a deadline nothing is armed: the read timeout alone, as before.
    clock.timers.clear()
    fetch(reply())
    assert clock.timers == []


@pytest.fixture
def local_overpass(monkeypatch):
    """A server on 127.0.0.1 that answers like Overpass with a connection gone silent: the headers
    and the preamble at once, part of the body, then nothing, with the connection held open. Yields
    (url for a reply of the given framing, release)."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    release, servers = threading.Event(), []

    def serve(listener, framing):
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            head = {"chunked": b"Transfer-Encoding: chunked", "close": b"Connection: close"}[framing]
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + head + b"\r\n\r\n")
            for part in (PREAMBLE, ELEMENTS[:100]):
                conn.sendall(b"%x\r\n%s\r\n" % (len(part), part) if framing == "chunked" else part)
            release.wait(30)

    def url(framing):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        thread = threading.Thread(target=serve, args=(listener, framing), daemon=True)
        thread.start()
        servers.append((listener, thread))
        return f"http://127.0.0.1:{listener.getsockname()[1]}/api/interpreter"

    yield url
    release.set()
    for listener, thread in servers:
        thread.join(timeout=5)
        listener.close()


@pytest.mark.parametrize("framing", ["chunked", "close"])
def test_the_shutter_ends_a_real_stalled_read_at_the_deadline(local_overpass, framing):
    """The same with requests and urllib3 over a real socket: a 15 s read timeout, a deadline 1 s
    off, and a body that stops after its first bytes. The read ends at the deadline, not 15 s on."""
    started = time.monotonic()
    with pytest.raises(osm.OsmError) as caught:
        osm._request(requests.post, local_overpass(framing), "query", 15.0, deadline=started + 1.0)
    took = time.monotonic() - started
    assert caught.value.code == "timeout" and caught.value.transient
    assert 1.0 <= took < 5.0, took


def test_a_bad_polygon_is_refused_before_anything_is_asked_even_with_no_time_left():
    fake = FakeOverpass(reply())
    with pytest.raises(ValueError):
        osm.fetch_network([[151.2, -33.9]], http_post=fake, deadline=100.0, clock=lambda: 99.0)
    assert fake.calls == []


def test_malformed_element_shapes_are_skipped_and_unparseable_bodies_are_coded():
    odd = [
        {"type": "node", "id": 1, "lat": -33.9, "lon": 151.2, "tags": {"railway": "station"}},  # the good one
        {"type": "way", "id": 2, "tags": {"railway": "station"}, "center": "not a dict"},
        {"type": 5, "id": 3, "lat": -33.9, "lon": 151.2, "tags": {"railway": "station"}},
        {"type": "node", "id": True, "lat": -33.9, "lon": 151.2, "tags": {"railway": "station"}},
        {"type": "node", "id": -4, "lat": -33.9, "lon": 151.2, "tags": {"railway": "station"}},
        {"type": "node", "id": 2**60, "lat": -33.9, "lon": 151.2, "tags": {"railway": "station"}},
    ]
    assert [s["id"] for s in osm.parse_elements(odd)["stations"]] == ["n1"]

    with pytest.raises(osm.OsmError) as caught:
        fetch(_Reply(b"[" * 200_000))  # nested past the parser's recursion limit
    assert caught.value.code == "bad_response"


def test_clean_name_drops_formatting_characters_by_category_but_keeps_the_joiners():
    # Bidi isolates, a TAG character, a lone surrogate and a private-use character all go.
    assert osm.clean_name("A\u2066B\u2069\U000e0041 C\ud800\ue000") == "AB C"
    # The zero-width joiner and non-joiner spell Persian, Arabic and Indic names: they stay.
    assert osm.clean_name("\u0646\u200c\u0647") == "\u0646\u200c\u0647"
    assert osm.clean_name("\u0915\u200d\u0937") == "\u0915\u200d\u0937"
