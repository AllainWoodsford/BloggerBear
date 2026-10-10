"""Tests for common/osm.py: the Overpass client for rail networks.

Overpass is never called: a scripted fake stands in for `requests.post`. These hold the query
text, what is made of the elements, how the network is thinned and capped to fit the worker's
payload, the fallback between the two endpoints, and the error codes the adapter records.
"""

from __future__ import annotations

import importlib
import json
import math
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    def __init__(self, body: bytes, status: int = 200):
        self.status_code, self.content = status, body


def reply(items=None, status=200, remark=None):
    payload = {"version": 0.6, "elements": items if items is not None else elements()}
    if remark is not None:
        payload["remark"] = remark
    return _Reply(json.dumps(payload).encode(), status)


class FakeOverpass:
    """Answers each POST in turn from `outcomes` (a reply or an exception to raise), recording
    what was asked."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, data, timeout):
        self.calls.append((url, data, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fetch(*outcomes, **kwargs):
    fake = FakeOverpass(*outcomes)
    return osm.fetch_network(POLY, http_post=fake, now=NOW, **kwargs), fake


# --- the module's footprint ------------------------------------------------------------------


def test_importing_the_module_brings_in_no_numpy_opencv_networkx_or_boto(monkeypatch):
    # In this process: with those modules made unimportable, a reload must still succeed...
    for name in ("numpy", "cv2", "networkx", "boto3", "botocore"):
        monkeypatch.setitem(sys.modules, name, None)
    importlib.reload(osm)
    # ...and in a fresh interpreter none of them enters sys.modules on import.
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
    "polygon", [[], [[151.2, -33.9], [151.3, -33.9]], [[151.2, "x"], [1, 2], [3, 4]], "poly"]
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
    ],
    ids=["429", "500", "503", "504", "read-timeout", "connect-timeout", "connection", "timed-out-remark"],
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
