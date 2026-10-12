"""Tests for common/adapters/rail_access.py and the research tick's running state for it.

The STAC search, the Overpass client, the vision worker, the news search, the agent and S3 are
all faked: the worker's own behaviour is tested in test_vision_worker.py, the Overpass client's
in test_osm.py, the agent's in test_vision_triage.py. These hold what the adapter decides: which
network, which scene, when there is time to measure, what is kept, what is material, what the
article and the agent are given.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import research_tick_handler
from common import osm, vision_client, vision_sites
from common.adapters import MANUAL_REVIEW_ADAPTERS, RAIL_ACCESS_ADAPTER_KEY
from common.adapters import rail_access as ra
from common.adapters.registry import ADAPTER_REGISTRY
from common.figures import MAX_ALT_CHARS, MAX_CAPTION_CHARS, clean_figures

# The city boxes of docs/enhancements/rail-access-monitor.md §2.
SYDNEY = [[150.86, -34.16], [151.56, -34.16], [151.56, -33.58], [150.86, -33.58]]
MELBOURNE = [[144.80, -37.90], [145.30, -37.90], [145.30, -37.55], [144.80, -37.55]]
PREFIX = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/56/H/LH/"
RAIL_ASSETS = ("red", "nir", "swir16", "scl")
PNG = vision_client.PNG_SIGNATURE + b"heat map"
WIDE = (140.0, -40.0, 155.0, -30.0)  # a scene box that covers both cities (a stand-in, not a tile)


def item(scene_id, when, bbox=WIDE, assets=RAIL_ASSETS, cloud=12.0):
    return {
        "id": scene_id,
        "bbox": list(bbox),
        "properties": {"datetime": when, "eo:cloud_cover": cloud},
        "assets": {key: {"href": f"{PREFIX}{scene_id}/{key}.tif"} for key in assets},
    }


def network(fetched_at="2026-10-01T03:00:00+00:00", polygon=SYDNEY):
    """What common/osm.fetch_network returns: the network with its bookkeeping (and, as the
    adapter caches it, the `site_key` of the polygon it was fetched for)."""
    return {
        "source": "overpass",
        "endpoint": osm.OVERPASS_ENDPOINTS[0],
        "fetched_at": fetched_at,
        "query_url": osm.query_url(polygon),
        "site_key": vision_sites.site_key(polygon),
        "ways": [{"id": "w1", "points": [[151.2, -33.9], [151.21, -33.9]], "tunnel": False, "name": "Main"}],
        "stations": [{"id": "n1", "name": "Central", "lon": 151.2, "lat": -33.9, "kind": "station"}],
        "pois": [{"id": "n2", "lon": 151.201, "lat": -33.9, "kind": "bus_stop"}],
        "counts": {"ways": 1, "stations": 1, "pois": 1, "points": 2},
        "truncated": False,
    }


class Clock:
    """The adapter's clock: the fakes advance it, as a slow Overpass or worker would."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeStac:
    def __init__(self, features=None, error=None, takes=0.0, clock=None):
        self.features, self.error, self.bodies = features or [], error, []
        self.takes, self.clock = takes, clock

    def __call__(self, url, json, timeout):
        assert url == vision_sites.STAC_SEARCH_URL
        self.bodies.append(json)
        if self.clock:
            self.clock.advance(self.takes)
        if self.error:
            raise self.error
        features = self.features

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"features": features}

        return Response()


class FakeOverpass:
    """Stands in for common/osm.fetch_network: answers `outcome` (a network, with `fetched_at` set
    to the `now` it is given, as the real one does) or raises it (an OsmError)."""

    def __init__(self, outcome, takes=0.0, clock=None):
        self.outcome, self.takes, self.clock, self.calls = outcome, takes, clock, []

    def __call__(self, polygon, *, http_post=None, now=None, deadline=None, clock=None, **kwargs):
        self.calls.append({"polygon": polygon, "http_post": http_post, "now": now, "deadline": deadline,
                           "clock": clock, **kwargs})  # fmt: skip
        if self.clock:
            self.clock.advance(self.takes)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        answer = json.loads(json.dumps(self.outcome))
        if now is not None:
            answer["fetched_at"] = now.isoformat()
        return answer


def station_row(n):
    return {
        "id": f"s:n{n}", "name": f"Station {n}", "kind": "station", "lon": 151.2, "lat": -33.87,
        "snapped": True, "degree": 2, "betweenness": 0.1, "activity": 0.5, "catchment_coverage": 0.98,
        "d_hub_m": 1000.0, "isolation_weight": 2.0,
        "intermodal": {"bus_stop_near": 1, "bus_station_near": 0, "ferry_terminal_far": 0,
                       "park_ride_far": 0},
    }  # fmt: skip


def metrics(served_share=0.73, desert_km2=111.3, station_count=178, coverage=0.93, stations=None, **extra):
    """The worker's rail metrics (test_vision_worker.py holds the real shape), with what varies."""
    rows = stations if stations is not None else [station_row(n) for n in range(min(station_count, 3))]
    return {
        "task": "rail_access",
        "coverage": coverage,
        "built_up_km2": 412.3,
        "served_km2": 301.0,
        "desert_km2": desert_km2,
        "served_share": served_share,
        "station_count": station_count,
        "snapped_station_count": station_count,
        "node_count": 240,
        "edge_count": 262,
        "components": 2,
        "hubs": ["s:n0"],
        "interchanges": ["s:n0"],
        "flags": [{"type": "single_point_of_failure", "station": "s:n0", "name": "Station 0", "ratio": 2.4}],
        "suggestions": [],
        "stations": rows,
        "deserts": [{"lon": 150.98, "lat": -33.9, "area_km2": 6.2, "nearest_station": "s:n0",
                     "nearest_station_m": 2300.0}],  # fmt: skip
        "edges": [{"from": "s:n0", "to": "j3", "length_m": 1840.0, "betweenness": 0.1,
                   "corridor_visibility": 0.62, "tunnel": False}],  # fmt: skip
        "truncated": 0,
        "quality_flags": [],
        "warnings": [],
        "network": {"ways": 1, "stations": 1, "pois": 1, "points": 2, "fetched_at": None},
        "params": {"pixel_size_m": 20.0, "coverage_floor": 0.6, "ndbi_threshold": 0.0, "reach_m": 1000.0},
        **extra,
    }


class FakeRailWorker:
    """Answers with metrics from a dict keyed by scene id, echoing the params it was given."""

    def __init__(self, by_scene, error=None, takes=0.0, clock=None):
        self.by_scene, self.error, self.calls = by_scene, error, []
        self.takes, self.clock = takes, clock

    def __call__(self, site, scene, backend, params, coverage_floor, task, network, read_timeout):
        self.calls.append({"site": site, "scene": scene, "backend": backend, "params": params,
                           "coverage_floor": coverage_floor, "task": task, "network": network,
                           "read_timeout": read_timeout})  # fmt: skip
        if self.clock:
            self.clock.advance(self.takes)
        if self.error:
            raise self.error
        m = self.by_scene[scene["id"]]
        reply = {
            "version": 1, "ok": True, "task": "rail_access", "backend": backend, "site_id": site["id"],
            "scene_id": scene["id"], "build": {"build_sha256": "ab" * 32},
            "timings_ms": {"total": 1000.0, "graph": 100.0},
            "metrics": {**m, "params": {**m["params"], **params},
                        "network": {**m["network"], "fetched_at": network["fetched_at"]}},
        }  # fmt: skip
        return vision_client.VisionResult(reply=reply, image_png=PNG)


class FakeAgent:
    """Stands in for common/vision_triage.triage: answers `verdict`, records what it was asked."""

    def __init__(self, verdict="real", reason="the new line is in the news and on the map"):
        self.verdict, self.reason, self.calls = verdict, reason, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"verdict": self.verdict, "reason": self.reason, "tool_calls": [], "model_calls": 1}


class FakeSearch:
    def __call__(self, query, **kwargs):
        return []


SITES = [{"id": "sydney", "name": "Sydney", "polygon": SYDNEY}]
# How a summary quotes the agent's reason (common/vision_triage.quoted_note).
NOTE = "the vision agent's note (not a measurement): "


def topic(sites=None, **config):
    return {
        "topic_id": "rail",
        "name": "Rail access",
        "adapter": "rail_access",
        "adapter_config": {"sites": sites or SITES, **config},
    }


@pytest.fixture
def adapter():
    a = ra.RailAccessAdapter()
    a.agent = FakeAgent()
    a.triage_agent = a.agent
    a.choose_model = lambda topic: ("au.anthropic.claude-haiku-4-5-20251001-v1:0", None)
    a.search = FakeSearch()
    a.web_search = a.search
    a.load_image = lambda key: PNG
    a.stored = []

    def store(topic_id, site_id, scene_id, png):
        a.stored.append(scene_id)
        return f"vision/{topic_id}/{site_id}/{scene_id}.png"

    a.store_image = store
    return a


def run(adapter, stac, worker, overpass=None, previous=None, **config):
    adapter.http_post, adapter.measure = stac, worker
    adapter.fetch_network = overpass or FakeOverpass(network())
    return adapter.fetch_state(topic(**config), previous_state=previous)


def scene(n, day=3):
    return FakeStac([item(f"S{n}", f"2026-10-{day:02d}T00:00:00Z")])


# --- registration and footprint ------------------------------------------------------------------


def test_registered_credited_and_held_for_a_person():
    assert ADAPTER_REGISTRY[RAIL_ACCESS_ADAPTER_KEY] is ra.RailAccessAdapter
    assert RAIL_ACCESS_ADAPTER_KEY in MANUAL_REVIEW_ADAPTERS and "satellite_vision" in MANUAL_REVIEW_ADAPTERS
    cls = ra.RailAccessAdapter
    assert cls.keeps_running_state and cls.uses_previous_state
    assert cls.sources == (vision_sites.COPERNICUS_SOURCE, vision_sites.AWS_OPEN_DATA_SOURCE, osm.OSM_SOURCE)
    assert cls.sources[2]["text"] == (
        "Map data © OpenStreetMap contributors, available under the Open Database Licence"
    )
    assert cls.figure_tolerance_percent == 0.0


def test_the_adapters_import_without_numpy_opencv_or_networkx():
    # In a fresh interpreter none of them enters sys.modules on import: the registry is imported by
    # every pipeline Lambda, and only the worker's package has them.
    lambdas_dir = Path(ra.__file__).resolve().parents[2]
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import common.vision_sites, common.adapters.rail_access, common.adapters.registry; "
        "heavy = {'numpy', 'cv2', 'networkx'}; "
        "print(sorted(heavy & {m.split('.')[0] for m in sys.modules}))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(lambdas_dir)], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


# --- config ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"sites": []},
        {"sites": [{"id": "a"}]},
        {"sites": SITES, "time_budget_seconds": 81},
        {"sites": SITES, "reach_m": 100},
        {"sites": SITES, "reach_m": 6000},
        {"sites": SITES, "reach_m": "far"},
        {"sites": SITES, "osm_ttl_days": 0},
        {"sites": SITES, "params": 3},
        {"sites": SITES, "backend": "gpu"},
        {"sites": SITES, "min_baseline": 0},
        # Not finite, or past a bound (docs/enhancements/rail-access-monitor.md §5 risk 1).
        {"sites": SITES, "time_budget_seconds": "nan"},
        {"sites": SITES, "time_budget_seconds": "-inf"},
        {"sites": SITES, "time_budget_seconds": 0},
        {"sites": SITES, "triage_deadline_seconds": "nan"},
        {"sites": SITES, "triage_deadline_seconds": 1e9},
        {"sites": SITES, "triage_deadline_seconds": 91},
        {"sites": SITES, "triage_max_tool_calls": 6},
        {"sites": SITES, "history_size": 21},
        {"sites": SITES, "history_size": "inf"},
        {"sites": SITES, "min_baseline": 6},  # the history keeps 6: at most 5 earlier scenes
        {"sites": SITES, "max_sites_per_tick": 0},
        {"sites": SITES, "max_sites_per_tick": 11},
        {"sites": SITES, "coverage_floor": 1.5},
        {"sites": SITES, "coverage_floor": "nan"},
        {"sites": SITES, "served_share_threshold": -0.01},
        {"sites": SITES, "desert_relative_threshold": "inf"},
        {"sites": SITES, "reach_m": "nan"},
        {"sites": SITES, "osm_ttl_days": 1e9},
        {"sites": SITES, "osm_ttl_days": "inf"},
        {"sites": SITES, "osm_ttl_days": "nan"},
        {"sites": SITES, "osm_ttl_days": 366},
        {"sites": SITES, "triage": "false"},
        {"sites": SITES, "web_context": "false"},
        {"sites": SITES, "web_context": 0},
        {"sites": SITES, "triage_max_tool_calls": True},
        {"sites": [{"id": "sydney", "name": ["Sydney"], "polygon": SYDNEY}]},
        {"sites": [{"id": "sydney", "name": " \n ", "polygon": SYDNEY}]},
        {"sites": [{"id": "sydney", "name": "S" * 81, "polygon": SYDNEY}]},
        {"sites": [{"id": "syd:ney", "polygon": SYDNEY}]},
        {"sites": [{"id": "..", "polygon": SYDNEY}]},
        {"sites": [{"id": "a..b", "polygon": SYDNEY}]},
        {"sites": [{"id": "s" * 65, "polygon": SYDNEY}]},
        {"sites": [{"id": 7, "polygon": SYDNEY}]},
    ],
)
def test_bad_config_is_refused(config):
    with pytest.raises(ra.ConfigError):
        ra.parse_config(config)


def test_defaults_are_the_specified_ones_and_decimals_become_plain_numbers():
    config = ra.parse_config({"sites": SITES})
    assert {key: config[key] for key in ra.DEFAULTS} == {
        **ra.DEFAULTS, "reach_m": 1000.0, "osm_ttl_days": 30.0, "max_cloud_cover": 40.0,
        "coverage_floor": 0.6, "served_share_threshold": 0.02, "desert_relative_threshold": 0.1,
        "time_budget_seconds": 70.0, "triage_deadline_seconds": 85.0,
    }  # fmt: skip
    assert config["max_sites_per_tick"] == 1 and config["history_size"] == 6 and config["min_baseline"] == 1
    stored = ra.parse_config(
        {"sites": [{"id": "s", "polygon": [[Decimal("151.2"), Decimal("-33.9")]] * 3}],
         "reach_m": Decimal("800"), "params": {"hub_count": Decimal("4")},
         "osm_ttl_days": Decimal("7")}  # fmt: skip
    )
    assert stored["reach_m"] == 800.0 and stored["osm_ttl_days"] == 7.0
    assert ra.worker_params(stored) == {"reach_m": 800.0, "hub_count": 4}
    # An explicit reach in params wins over the topic's key, as specified.
    assert ra.worker_params({"reach_m": 1000.0, "params": {"reach_m": 500}}) == {"reach_m": 500}


def test_names_are_one_plain_line_and_the_bounds_are_inclusive():
    config = ra.parse_config(
        {"sites": [{"id": "Syd_ney-1", "name": "  Greater\nSydney\u202e ", "polygon": SYDNEY}],
         "time_budget_seconds": 80, "triage_deadline_seconds": 90, "triage_max_tool_calls": 0,
         "history_size": 20, "min_baseline": 19, "max_sites_per_tick": 10, "coverage_floor": 1,
         "osm_ttl_days": 365, "served_share_threshold": 0, "web_context": False}  # fmt: skip
    )
    assert config["sites"][0]["name"] == "Greater Sydney" and config["web_context"] is False
    assert ra.parse_config({"sites": [{"id": "s", "polygon": SYDNEY}]})["sites"][0] == {"id": "s",
                                                                                     "polygon": SYDNEY}


def test_an_unusable_ttl_reads_as_stale_rather_than_breaking_the_tick():
    # parse_config refuses these; a state or a caller that still has one gets a refresh, not a crash.
    now = datetime.now(UTC)
    for ttl in (1e9, float("inf"), float("nan"), "soon"):
        assert ra._is_stale(network(now.isoformat()), now, ttl) is True
    assert ra._is_stale(network(now.isoformat()), now, 30) is False


# --- fetching ------------------------------------------------------------------------------------


def test_the_first_tick_fetches_the_network_then_measures(adapter):
    overpass = FakeOverpass(network())
    worker = FakeRailWorker({"S1": metrics()})
    state = run(adapter, scene(1), worker, overpass)
    record = state["sites"]["sydney"]

    (fetch,) = overpass.calls
    assert fetch["polygon"] == SYDNEY and fetch["http_post"] is adapter.http_post
    # The fetch gets the reserve, both hops included, on the adapter's own clock.
    assert fetch["clock"] is adapter.clock
    assert fetch["deadline"] == pytest.approx(adapter.clock() + ra.OSM_RESERVE_SECONDS, abs=5)
    assert record["network"]["counts"]["stations"] == 1
    assert record["network"]["fetched_at"] == state["fetched_at"]
    assert "network_error" not in record and "last_error" not in record
    assert record["last_attempt_at"] == state["fetched_at"] and record["name"] == "Sydney"

    entry = record["history"][-1]
    assert entry["scene_id"] == "S1" and entry["served_share"] == 0.73 and entry["station_count"] == 178
    assert entry["image_key"] == "vision/rail/sydney/S1.png" and adapter.stored == ["S1"]
    assert entry["network_fetched_at"] == state["fetched_at"]
    assert state["measured"] == [{"site_id": "sydney", "scene_id": "S1"}]
    assert state["task"] == "rail_access" and state["fetched_at"]
    assert state["thresholds"] == {"coverage_floor": 0.6, "min_baseline": 1, "served_share_threshold": 0.02,
                                   "desert_relative_threshold": 0.1}  # fmt: skip


def test_the_worker_request_carries_the_task_the_network_and_the_reach(adapter):
    worker = FakeRailWorker({"S1": metrics()})
    run(adapter, scene(1), worker, params={"hub_count": 4}, reach_m=800, backend="cool", coverage_floor=0.5)
    (call,) = worker.calls
    assert call["task"] == "rail_access" and call["read_timeout"] == ra.WORKER_READ_TIMEOUT == 58.0
    assert call["backend"] == "cool" and call["coverage_floor"] == 0.5
    assert call["params"] == {"reach_m": 800.0, "hub_count": 4}
    assert call["site"] == {"id": "sydney", "polygon": SYDNEY}
    assert call["scene"]["id"] == "S1" and call["scene"]["assets"]["swir16"].endswith("S1/swir16.tif")
    assert set(call["scene"]["assets"]) == set(RAIL_ASSETS)
    # The worker gets the network and nothing of the fetch's bookkeeping.
    assert set(call["network"]) == {"ways", "stations", "pois", "fetched_at"}
    assert call["network"]["stations"][0]["name"] == "Central"


def test_the_search_asks_for_the_rail_bands_within_the_rail_defaults(adapter):
    stac = FakeStac([item("S1", "2026-10-03T00:00:00Z", assets=("red", "nir", "scl"))])
    state = run(adapter, stac, FakeRailWorker({}))
    assert state["measured"] == [] and "last_error" not in state["sites"]["sydney"]  # no scene with swir16
    assert stac.bodies[0]["query"] == {"eo:cloud_cover": {"lte": 40.0}}
    window = stac.bodies[0]["datetime"].split("/")
    start, end = (datetime.fromisoformat(moment.replace("Z", "+00:00")) for moment in window)
    assert end - start == timedelta(days=20)


def aged(state, days, site="sydney"):
    """`state` with the site's cached network fetched `days` ago."""
    copy = json.loads(json.dumps(state))
    copy["sites"][site]["network"]["fetched_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    return copy


def test_a_fresh_network_is_reused_and_a_stale_one_refreshed_when_there_is_nothing_new(adapter):
    overpass = FakeOverpass(network())
    first = run(adapter, scene(1), FakeRailWorker({"S1": metrics()}), overpass)
    second = run(adapter, scene(2), FakeRailWorker({"S2": metrics()}), overpass, previous=first)
    assert len(overpass.calls) == 1
    assert second["sites"]["sydney"]["network"] == first["sites"]["sydney"]["network"]
    assert second["sites"]["sydney"]["history"][-1]["network_fetched_at"] == first["fetched_at"]
    assert second["sites"]["sydney"]["history"][-1]["network_stale"] is False

    # Past the TTL with a new scene: the scene comes first, measured against the cached network.
    old = aged(second, 31)
    third = run(adapter, scene(3), FakeRailWorker({"S3": metrics()}), overpass, previous=old)
    assert len(overpass.calls) == 1 and third["measured"] == [{"site_id": "sydney", "scene_id": "S3"}]
    entry = third["sites"]["sydney"]["history"][-1]
    assert entry["network_stale"] is True
    assert entry["network_fetched_at"] == old["sites"]["sydney"]["network"]["fetched_at"]
    # Nothing new on the next tick: that tick's time refreshes the network.
    fourth = run(adapter, scene(3), FakeRailWorker({}), overpass, previous=third)
    assert len(overpass.calls) == 2 and fourth["measured"] == []
    assert fourth["sites"]["sydney"]["network"]["fetched_at"] == fourth["fetched_at"]
    assert fourth["sites"]["sydney"]["last_attempt_at"] == fourth["fetched_at"]  # worked on: stamped
    assert not {"network_error", "last_error"} & set(fourth["sites"]["sydney"])
    # A fresh network and nothing new: nothing to do.
    run(adapter, scene(3), FakeRailWorker({}), overpass, previous=fourth)
    assert len(overpass.calls) == 2
    # A shorter TTL refreshes sooner.
    run(adapter, scene(3), FakeRailWorker({}), overpass, previous=aged(fourth, 8), osm_ttl_days=7)
    assert len(overpass.calls) == 3


def test_a_failed_refresh_keeps_the_cached_network_and_waits_before_it_tries_again(adapter):
    first = aged(run(adapter, scene(1), FakeRailWorker({"S1": metrics()})), 31)
    old = first["sites"]["sydney"]["network"]["fetched_at"]

    failing = FakeOverpass(osm.OsmError("rate_limited", "HTTP 429 from overpass-api.de", transient=True))
    state = run(adapter, scene(1), FakeRailWorker({}), failing, previous=first)
    record = state["sites"]["sydney"]
    assert len(failing.calls) == 1 and state["measured"] == []
    assert record["network"] == first["sites"]["sydney"]["network"]  # kept
    assert record["network_error"]["code"] == "rate_limited"
    assert "HTTP 429" in record["network_error"]["detail"]
    assert record["network_error"]["at"] == state["fetched_at"]
    assert "last_error" not in record  # the site itself is fine: nothing new, a cached network

    # Within NETWORK_RETRY_HOURS it is not asked again ...
    assert ra.NETWORK_RETRY_HOURS == 6
    again = run(adapter, scene(1), FakeRailWorker({}), failing, previous=state)
    assert len(failing.calls) == 1 and again["sites"]["sydney"]["network_error"] == record["network_error"]
    # ... and a new scene meanwhile is measured with the cached network, stale as it is.
    worker = FakeRailWorker({"S2": metrics()})
    measured = run(adapter, scene(2), worker, failing, previous=again)
    assert len(failing.calls) == 1 and measured["measured"] == [{"site_id": "sydney", "scene_id": "S2"}]
    assert worker.calls[0]["network"]["fetched_at"] == old
    assert measured["sites"]["sydney"]["history"][-1]["network_fetched_at"] == old
    assert measured["sites"]["sydney"]["history"][-1]["network_stale"] is True

    # Once the wait is over it is tried again, and a fetch that works clears the failure.
    later = json.loads(json.dumps(measured))
    seven_hours_ago = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    later["sites"]["sydney"]["network_error"]["at"] = seven_hours_ago
    recovered = run(adapter, scene(2), FakeRailWorker({}), FakeOverpass(network()), previous=later)
    assert "network_error" not in recovered["sites"]["sydney"]
    assert recovered["sites"]["sydney"]["network"]["fetched_at"] == recovered["fetched_at"]


def test_a_slow_failing_overpass_never_stops_a_city_with_a_cached_network(adapter):
    """The review's case: Overpass answers 504 after 25-30 s every time, the cached network is 31
    days old, and a new scene is out. The scene is measured first, with the cached network; the
    refresh is tried on a tick with nothing new, fails slowly, and the next scene is measured too."""
    clock = Clock()
    adapter.clock = clock
    first = aged(run(adapter, scene(1), FakeRailWorker({"S1": metrics()})), 31)
    slow = FakeOverpass(osm.OsmError("http", "HTTP 504 from overpass-api.de", transient=True), 29.0, clock)

    worker = FakeRailWorker({"S2": metrics(), "S3": metrics()}, takes=20.0, clock=clock)
    state = run(adapter, scene(2), worker, slow, previous=first)
    assert state["measured"] == [{"site_id": "sydney", "scene_id": "S2"}] and slow.calls == []
    assert state["sites"]["sydney"]["history"][-1]["network_stale"] is True

    quiet = run(adapter, scene(2), worker, slow, previous=state)
    assert len(slow.calls) == 1 and quiet["sites"]["sydney"]["network_error"]["code"] == "http"
    assert quiet["sites"]["sydney"]["network"] == first["sites"]["sydney"]["network"]

    third = run(adapter, scene(3), worker, slow, previous=quiet)
    assert third["measured"] == [{"site_id": "sydney", "scene_id": "S3"}] and len(slow.calls) == 1


def test_no_network_means_no_measurement_and_a_last_error(adapter):
    failing = FakeOverpass(osm.OsmError("timeout", "no answer from either endpoint", transient=True))
    worker, stac = FakeRailWorker({"S1": metrics()}), scene(1)
    state = run(adapter, stac, worker, failing)
    record = state["sites"]["sydney"]
    assert worker.calls == [] and len(stac.bodies) == 1 and state["measured"] == []  # the scene first
    assert record["last_error"]["code"] == "osm" and "no answer" in record["last_error"]["detail"]
    assert record["network_error"]["code"] == "timeout" and "network" not in record
    assert record["last_attempt_at"] == state["fetched_at"]  # tried, so the next tick may try others first
    changed, summary = adapter.material_diff(None, state)
    assert not changed and summary == "nothing measured yet: Sydney: osm"
    # With no network the fetch is tried every tick (the retry wait is for refreshing a cached one).
    run(adapter, scene(1), worker, failing, previous=state)
    assert len(failing.calls) == 2

    broken = FakeOverpass(ValueError("polygon must be [[lon, lat], ...]"))
    state = run(adapter, scene(1), worker, broken)
    assert state["sites"]["sydney"]["network_error"]["code"] == "fetch"
    assert "ValueError" in state["sites"]["sydney"]["network_error"]["detail"]


def test_a_changed_polygon_gets_its_own_network(adapter):
    """The cached network is the old polygon's: it is not used for the new one, even fresh, so the
    new area is never measured against the old area's stations and the source link is the new
    bbox's. If the new one cannot be fetched, the site waits, and the old network is dropped."""
    overpass = FakeOverpass(network())
    first = run(adapter, scene(1), FakeRailWorker({"S1": metrics()}), overpass)
    assert first["sites"]["sydney"]["network"]["site_key"] == vision_sites.site_key(SYDNEY)
    wider = [[150.5, -34.3], [151.7, -34.3], [151.7, -33.4], [150.5, -33.4]]
    sites = [{"id": "sydney", "name": "Sydney", "polygon": wider}]

    failing = FakeOverpass(osm.OsmError("timeout", "no answer", transient=True))
    waiting = run(adapter, scene(2), FakeRailWorker({"S2": metrics()}), failing, previous=first, sites=sites)
    assert waiting["measured"] == [] and [c["polygon"] for c in failing.calls] == [wider]
    assert waiting["sites"]["sydney"]["last_error"]["code"] == "osm"
    assert "network" not in waiting["sites"]["sydney"]  # the old polygon's is dropped, never used

    fresh = FakeOverpass(network(polygon=wider))
    state = run(adapter, scene(2), FakeRailWorker({"S2": metrics()}), fresh, previous=first, sites=sites)
    assert [c["polygon"] for c in fresh.calls] == [wider] and len(overpass.calls) == 1
    assert state["measured"] == [{"site_id": "sydney", "scene_id": "S2"}]
    assert state["sites"]["sydney"]["network"]["site_key"] == vision_sites.site_key(wider)
    assert state["sites"]["sydney"]["history"][-1]["site_key"] == vision_sites.site_key(wider)
    assert adapter.source_refs(state)[1]["url"] == osm.query_url(wider) != osm.query_url(SYDNEY)


def test_search_and_worker_failures_keep_the_history_and_say_why(adapter):
    first = run(adapter, scene(1), FakeRailWorker({"S1": metrics()}))
    searched = run(adapter, FakeStac(error=TimeoutError("stac down")), FakeRailWorker({}), previous=first)
    assert searched["sites"]["sydney"]["last_error"]["code"] == "search"
    assert searched["sites"]["sydney"]["history"] == first["sites"]["sydney"]["history"]

    failing = FakeRailWorker({}, error=vision_client.VisionError("window_too_large", "4096 px"))
    measured = run(adapter, scene(2), failing, previous=first)
    assert measured["sites"]["sydney"]["last_error"]["code"] == "window_too_large"
    assert measured["measured"] == [] and len(measured["sites"]["sydney"]["history"]) == 1

    # A scene already measured is nothing new, and clears an earlier error.
    again = run(adapter, scene(1), FakeRailWorker({"S1": metrics()}), previous=measured)
    assert again["measured"] == [] and "last_error" not in again["sites"]["sydney"]


def test_a_lost_figure_does_not_lose_the_measurement(adapter):
    def broken(*args):
        raise OSError("s3 down")

    adapter.store_image = broken
    state = run(adapter, scene(1), FakeRailWorker({"S1": metrics()}))
    assert state["sites"]["sydney"]["history"][-1]["image_key"] is None
    assert state["measured"] == [{"site_id": "sydney", "scene_id": "S1"}]
    assert adapter.figures(state) == []


# --- the budget ----------------------------------------------------------------------------------


def test_measuring_is_deferred_when_the_budget_is_nearly_spent(adapter):
    clock = Clock()
    adapter.clock = clock
    overpass = FakeOverpass(network(), takes=28.0, clock=clock)  # a slow Overpass: 42 s left after it
    worker = FakeRailWorker({"S1": metrics()})
    state = run(adapter, scene(1), worker, overpass)
    record = state["sites"]["sydney"]
    assert worker.calls == [] and state["measured"] == []
    assert record["last_error"] == {"at": state["fetched_at"], "code": "time_budget", "detail": "next tick"}
    assert record["network"]["counts"]["stations"] == 1  # the network was still fetched and kept
    assert "last_attempt_at" not in record  # deferred, not worked on to an outcome

    # The next tick has the network and measures at once.
    second = run(adapter, scene(1), worker, overpass, previous=state)
    assert len(overpass.calls) == 1 and len(worker.calls) == 1
    assert second["measured"] == [{"site_id": "sydney", "scene_id": "S1"}]
    assert "last_error" not in second["sites"]["sydney"]


def test_the_network_is_not_fetched_under_the_reserve_and_a_stale_one_serves(adapter):
    """Two sites in one tick: the first's worker takes most of the budget. The second's cached
    network is stale but there is not the reserve to fetch it again, so it is used as it is; and
    with under the measuring reserve left the measurement itself waits for the next tick."""
    clock = Clock()
    adapter.clock = clock
    sites = [*SITES, {"id": "melbourne", "name": "Melbourne", "polygon": MELBOURNE}]
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    cached = {"name": "Melbourne", "network": network(old, MELBOURNE), "history": []}
    previous = {"sites": {"melbourne": cached}}
    overpass = FakeOverpass(network())
    worker = FakeRailWorker({"S1": metrics()}, takes=50.0, clock=clock)
    state = run(adapter, scene(1), worker, overpass, previous=previous, sites=sites, max_sites_per_tick=2)

    assert [m["site_id"] for m in state["measured"]] == ["sydney"]
    assert [c["polygon"] for c in overpass.calls] == [SYDNEY]  # a new scene: no refresh first
    melbourne = state["sites"]["melbourne"]
    assert melbourne["network"]["fetched_at"] == old and "network_error" not in melbourne
    assert melbourne["last_error"]["code"] == "time_budget"
    # Deferred for the budget is not an attempt: Melbourne goes first next tick.
    assert "last_attempt_at" not in melbourne
    assert len(worker.calls) == 1
    clock.now = 0.0
    second = run(adapter, scene(1), worker, overpass, previous=state, sites=sites)
    assert [m["site_id"] for m in second["measured"]] == ["melbourne"]
    assert second["sites"]["melbourne"]["history"][-1]["network_stale"] is True


def test_a_site_past_the_budget_is_not_attempted_at_all(adapter):
    clock = Clock()
    adapter.clock = clock
    sites = [*SITES, {"id": "melbourne", "name": "Melbourne", "polygon": MELBOURNE}]
    stac = scene(1)
    worker = FakeRailWorker({"S1": metrics()}, takes=75.0, clock=clock)
    state = run(adapter, stac, worker, previous=None, sites=sites, max_sites_per_tick=2)
    melbourne = state["sites"]["melbourne"]
    assert melbourne["last_error"]["code"] == "time_budget" and "last_attempt_at" not in melbourne
    assert len(stac.bodies) == 1 and "network" not in melbourne


def test_a_site_that_fails_slowly_every_tick_does_not_starve_the_next(adapter):
    """The first site in the config has no network and Overpass fails it after 29 s, every tick.
    The second site is fetched in the time left but cannot be measured (under 45 s): deferred, it
    is not stamped, so the next tick takes it first and measures it."""
    clock = Clock()
    adapter.clock = clock
    sites = [{"id": "melbourne", "name": "Melbourne", "polygon": MELBOURNE}, *SITES]

    def fetch(polygon, **kwargs):
        if polygon == MELBOURNE:
            clock.advance(29.0)
            raise osm.OsmError("timeout", "no answer from either endpoint", transient=True)
        return FakeOverpass(network())(polygon, **kwargs)

    worker = FakeRailWorker({"S1": metrics()})
    first = run(adapter, scene(1), worker, fetch, sites=sites)
    assert first["measured"] == [] and first["sites"]["melbourne"]["last_error"]["code"] == "osm"
    assert first["sites"]["melbourne"]["last_attempt_at"] == first["fetched_at"]
    sydney = first["sites"]["sydney"]
    assert sydney["last_error"]["code"] == "time_budget" and "last_attempt_at" not in sydney
    assert sydney["network"]["counts"]["stations"] == 1  # fetched in the 41 s left, kept

    clock.now = 0.0
    second = run(adapter, scene(1), worker, fetch, previous=first, sites=sites)
    assert second["measured"] == [{"site_id": "sydney", "scene_id": "S1"}] and len(worker.calls) == 1
    # Sydney was stamped this time; Melbourne's older stamp puts it first on the tick after.
    assert [s["id"] for s in vision_sites.order_sites(sites, second["sites"])] == ["melbourne", "sydney"]


def test_sites_rotate_one_per_tick_the_unvisited_first(adapter):
    sites = [*SITES, {"id": "melbourne", "name": "Melbourne", "polygon": MELBOURNE}]
    overpass, worker, stac = FakeOverpass(network()), FakeRailWorker({"S1": metrics()}), scene(1)

    first = run(adapter, stac, worker, overpass, sites=sites)
    assert [m["site_id"] for m in first["measured"]] == ["sydney"]
    assert not {"last_attempt_at", "network"} & set(first["sites"]["melbourne"])

    second = run(adapter, stac, worker, overpass, previous=first, sites=sites)
    assert [m["site_id"] for m in second["measured"]] == ["melbourne"]
    assert [c["polygon"] for c in overpass.calls] == [SYDNEY, MELBOURNE]
    assert second["sites"]["sydney"]["history"] == first["sites"]["sydney"]["history"]

    third = run(adapter, stac, worker, overpass, previous=second, sites=sites)
    assert third["measured"] == [] and len(worker.calls) == 2  # both know S1; both were visited
    assert len(stac.bodies) == 4 and len(overpass.calls) == 2


# --- what is kept --------------------------------------------------------------------------------


def test_history_entries_keep_the_metrics_and_cap_the_station_list(adapter):
    rows = [station_row(n) for n in range(ra.MAX_HISTORY_STATIONS + 5)]
    state = run(adapter, scene(1), FakeRailWorker({"S1": metrics(station_count=len(rows), stations=rows)}))
    entry = state["sites"]["sydney"]["history"][-1]
    assert set(entry) == {
        "scene_id", "captured_at", "scene_cloud_cover", "coverage", "built_up_km2", "served_km2",
        "desert_km2", "served_share", "station_count", "edge_count", "hubs", "flags", "suggestions",
        "deserts", "stations", "quality_flags", "backend", "build_sha256", "timings_ms", "image_key",
        "assets", "network_fetched_at", "network_stale", "site_key", "params",
    }  # fmt: skip
    assert entry["scene_cloud_cover"] == 12.0 and entry["built_up_km2"] == 412.3
    assert entry["edge_count"] == 262
    assert len(entry["stations"]) == ra.MAX_HISTORY_STATIONS == 2000
    assert set(entry["stations"][0]) == set(ra.STATION_FIELDS) and entry["stations"][0]["name"] == "Station 0"
    assert entry["hubs"] == ["s:n0"] and entry["flags"][0]["type"] == "single_point_of_failure"
    assert entry["deserts"][0]["nearest_station"] == "s:n0" and entry["params"]["reach_m"] == 1000.0
    assert entry["network_stale"] is False and entry["site_key"] == vision_sites.site_key(SYDNEY)
    assert entry["build_sha256"] == "ab" * 32 and entry["timings_ms"]["graph"] == 100.0
    assert entry["assets"]["red"].endswith("S1/red.tif") and entry["backend"] == "opencv"
    assert "edges" not in entry and "warnings" not in entry
    json.dumps(state, allow_nan=False)


def test_history_is_capped_and_a_reply_with_a_nan_is_not_a_measurement(adapter):
    state = None
    for n in range(4):
        worker = FakeRailWorker({f"S{n}": metrics()})
        state = run(adapter, scene(n, day=n + 1), worker, previous=state, history_size=2)
    assert [e["scene_id"] for e in state["sites"]["sydney"]["history"]] == ["S2", "S3"]

    nan = FakeRailWorker({"S9": metrics(desert_km2=float("nan"))})
    state = run(adapter, scene(9), nan, previous=state, history_size=2)
    assert state["measured"] == [] and state["sites"]["sydney"]["last_error"]["code"] == "reply"
    assert [e["scene_id"] for e in state["sites"]["sydney"]["history"]] == ["S2", "S3"]
    assert adapter.stored == ["S0", "S1", "S2", "S3"]  # the figure of the unusable reply was never stored


# --- the diff ------------------------------------------------------------------------------------


def history_state(adapter, series, **config):
    """One tick per (served_share, desert_km2, station_count[, coverage]) row, each a new scene."""
    state = None
    for n, row in enumerate(series):
        share, desert, stations = row[:3]
        coverage = row[3] if len(row) > 3 else 0.93
        worker = FakeRailWorker({f"S{n}": metrics(share, desert, stations, coverage)})
        state = run(adapter, scene(n, day=10 + n), worker, previous=state, **config)
    return state


# What an earlier tick that measured something leaves behind, as far as material_diff cares.
EARLIER = {
    "sites": {
        "sydney": {
            "name": "Sydney",
            "history": [{"scene_id": "S-1", "served_share": 0.7, "desert_km2": 100.0, "station_count": 178,
                         "coverage": 0.9}],  # fmt: skip
        }
    }
}


def test_the_first_tick_is_material_with_a_plain_summary(adapter):
    state = history_state(adapter, [(0.73, 111.3, 178)])
    changed, summary = adapter.material_diff(None, state)
    assert changed
    assert summary == (
        "First observation. Sydney: served share 73 %, 178 stations, 111 km² beyond 1 km of a station "
        "(coverage 93 %)"
    )
    assert adapter.agent.calls == []  # the first observation is reported, not triaged

    # The reach is named as measured: in metres when it is not whole kilometres.
    state = history_state(adapter, [(0.5, 240.0, 12)], reach_m=800)
    assert "240 km² beyond 800 m of a station" in adapter.material_diff(None, state)[1]


def test_the_first_observation_is_the_first_clear_scene(adapter):
    """A first scene below the coverage floor is kept but reports nothing (a served share over a
    third of a city is not an observation); the next clear scene is the first observation."""
    cloudy = history_state(adapter, [(0.45, 300.0, 178, 0.31)])
    assert cloudy["sites"]["sydney"]["history"][-1]["coverage"] == 0.31
    changed, summary = adapter.material_diff(None, cloudy)
    assert not changed
    assert summary == "nothing measured clearly yet: Sydney: coverage 31 % is below the 60 % floor"
    clear = run(adapter, scene(5, day=20), FakeRailWorker({"S5": metrics()}), previous=cloudy)
    changed, summary = adapter.material_diff(cloudy, clear)
    assert changed and summary.startswith("First observation. Sydney: served share 73 %")
    assert adapter.agent.calls == []
    # After that, a cloudy scene is just below the floor, as before.
    again = run(adapter, scene(6, day=21), FakeRailWorker({"S6": metrics(coverage=0.3)}), previous=clear)
    assert adapter.material_diff(clear, again) == (False, "Sydney: coverage below the floor")


def test_a_first_tick_that_measured_nothing_is_not_material(adapter):
    empty = run(adapter, FakeStac([]), FakeRailWorker({}))
    changed, summary = adapter.material_diff(None, empty)
    assert not changed and summary == "nothing measured yet: no scene to measure"
    unreachable = FakeRailWorker({}, error=vision_client.VisionError("invoke", "no worker"))
    failing = run(adapter, scene(0), unreachable)
    changed, summary = adapter.material_diff(empty, failing)
    assert not changed and summary == "nothing measured yet: Sydney: invoke"
    measured = run(adapter, scene(1), FakeRailWorker({"S1": metrics()}), previous=failing)
    changed, summary = adapter.material_diff(failing, measured)
    assert changed and summary.startswith("First observation") and adapter.agent.calls == []


@pytest.mark.parametrize(
    ("latest", "reason"),
    [
        ((0.76, 111.3, 178), "served share moved"),
        ((0.70, 111.3, 178), "served share moved"),
        ((0.73, 125.0, 178), "desert area moved"),  # +13 km² against a median of 111.65: over 10 %
        ((0.73, 111.3, 180), "station count changed"),
        ((0.73, 111.3, 177), "station count changed"),
        ((0.76, 125.0, 180), "served share moved, desert area moved, station count changed"),
    ],
)
def test_the_served_share_desert_and_station_count_rules(adapter, latest, reason):
    state = history_state(adapter, [(0.73, 111.3, 178), (0.735, 112.0, 178), latest])
    changed, summary = adapter.material_diff(EARLIER, state)
    assert changed, summary
    assert summary.startswith("Sydney: served share") and "in scene S2 (captured 2026-10-12" in summary
    assert "against a baseline of 73 % from 2 earlier clear scenes" in summary
    assert f'; {reason}; {NOTE}"the new line is in the news' in summary
    asked = adapter.agent.calls[0]["verdict"]
    assert asked["reason"] == reason and asked["baseline"]["station_count"] == 178
    assert asked["baseline"]["served_share"] == 0.7325 and asked["baseline"]["desert_km2"] == 111.65


@pytest.mark.parametrize(
    ("series", "reason"),
    [
        ([(0.28, 111.3, 178), (0.30, 111.3, 178)], "served share moved"),  # 0.30 - 0.28 is 0.0199999...
        ([(0.73, 100.5, 178), (0.73, 110.55, 178)], "desert area moved"),  # 10 % of 100.5 km², 10.049999...
    ],
)
def test_a_move_of_exactly_the_threshold_is_material(adapter, series, reason):
    state = history_state(adapter, series)
    changed, summary = adapter.material_diff(EARLIER, state)
    assert changed and f"; {reason};" in summary


@pytest.mark.parametrize(
    "edit",
    [
        {"reach_m": 800},
        {"params": {"hub_count": 4}},
        {"sites": [{"id": "sydney", "name": "Sydney", "polygon": [[150.8, -34.2], *SYDNEY[1:]]}]},
    ],
)
def test_an_edited_reach_params_or_polygon_starts_a_new_baseline(adapter, edit):
    """The reach goes from 1000 to 800 m (or a parameter or the polygon changes): the next scene's
    served share and desert area move sharply, but against scenes measured another way, so it is
    not material; it is the first of a new baseline."""
    state = history_state(adapter, [(0.73, 111.3, 178), (0.735, 112.0, 178)])
    edited = run(adapter, scene(7, day=20), FakeRailWorker({"S7": metrics(0.62, 160.0, 180)}), previous=state,
                 **edit)  # fmt: skip
    changed, summary = adapter.material_diff(state, edited)
    assert (changed, summary) == (False, "Sydney: baseline still building")
    assert adapter.agent.calls == []
    (verdict,) = adapter.assess(edited)
    assert verdict["params_changed"] is True and verdict["baseline_scenes"] == 0
    # The next scene measured the new way has a baseline of one, and moves nothing.
    after = run(adapter, scene(8, day=21), FakeRailWorker({"S8": metrics(0.62, 160.0, 180)}), previous=edited,
                **edit)  # fmt: skip
    assert adapter.material_diff(edited, after) == (False, "Sydney: within the usual range")
    (verdict,) = adapter.assess(after)
    assert verdict["params_changed"] is False and verdict["baseline_scenes"] == 1


def test_the_agent_is_told_the_reach_and_whether_the_scene_before_was_measured_another_way(adapter):
    """1000 m, 1000 m, then one scene at 800 m, then 1000 m again with a moved share: the baseline
    is the two earlier 1000 m scenes, and the brief says the scene just before differs."""
    state = history_state(adapter, [(0.73, 111.3, 178), (0.735, 112.0, 178)])
    state = run(adapter, scene(7, day=20), FakeRailWorker({"S7": metrics(0.62, 160.0, 178)}), previous=state,
                reach_m=800)  # fmt: skip
    state = run(adapter, scene(8, day=21), FakeRailWorker({"S8": metrics(0.70, 111.3, 178)}), previous=state)
    changed, summary = adapter.material_diff(EARLIER, state)
    assert changed and "from 2 earlier clear scenes" in summary
    asked = adapter.agent.calls[0]["verdict"]
    assert asked["reach_m"] == 1000.0 and asked["params_changed"] is True
    assert asked["baseline"]["station_count"] == 178


@pytest.mark.parametrize("latest", [(0.745, 118.0, 178), (0.715, 105.0, 178), (0.7325, 111.65, 178)])
def test_a_change_within_the_usual_range_is_not_material(adapter, latest):
    state = history_state(adapter, [(0.73, 111.3, 178), (0.735, 112.0, 178), latest])
    changed, summary = adapter.material_diff(EARLIER, state)
    assert not changed and summary == "Sydney: within the usual range"
    assert adapter.agent.calls == []  # rule 2: no model call without a numeric change


def test_low_coverage_is_never_material_and_never_baseline(adapter):
    cloudy_now = history_state(adapter, [(0.73, 111.3, 178), (0.5, 300.0, 178, 0.3)])
    changed, summary = adapter.material_diff(EARLIER, cloudy_now)
    assert not changed and summary == "Sydney: coverage below the floor" and adapter.agent.calls == []
    cloudy_before = history_state(adapter, [(0.5, 300.0, 178, 0.2), (0.73, 111.3, 178)])
    changed, summary = adapter.material_diff(EARLIER, cloudy_before)
    assert not changed and summary == "Sydney: baseline still building"
    # With a baseline of two required, one clear scene is not enough.
    building = history_state(adapter, [(0.73, 111.3, 178), (0.80, 90.0, 178)], min_baseline=2)
    assert adapter.material_diff(EARLIER, building) == (False, "Sydney: baseline still building")


def test_a_graph_too_large_is_recorded_and_never_material_on_its_own(adapter):
    """The worker skips betweenness on an oversized graph and says so in the quality flags: a
    normal measured outcome. It is kept on the entry and told to the agent, but it moves nothing
    by itself (the served share, the desert area and the station count are measured anyway)."""
    state = None
    for n, flags in enumerate(([], ["graph_too_large"])):
        m = metrics(0.73, 111.3, 178, quality_flags=flags, hubs=[], flags=[])
        state = run(adapter, scene(n, day=10 + n), FakeRailWorker({f"S{n}": m}), previous=state)
    latest = state["sites"]["sydney"]["history"][-1]
    assert latest["quality_flags"] == ["graph_too_large"] and latest["hubs"] == [] and latest["flags"] == []
    assert state["measured"] == [{"site_id": "sydney", "scene_id": "S1"}]
    changed, summary = adapter.material_diff(EARLIER, state)
    assert not changed and summary == "Sydney: within the usual range"
    assert adapter.agent.calls == []


def test_thresholds_come_from_the_topic(adapter):
    quiet = history_state(adapter, [(0.73, 111.3, 178), (0.745, 111.3, 178)])
    assert not adapter.material_diff(EARLIER, quiet)[0]
    state = history_state(adapter, [(0.73, 111.3, 178), (0.745, 111.3, 178)], served_share_threshold=0.01)
    assert adapter.material_diff(EARLIER, state)[0]
    state = history_state(adapter, [(0.73, 111.3, 178), (0.73, 118.0, 178)], desert_relative_threshold=0.05)
    assert adapter.material_diff(EARLIER, state)[0]


# --- the agent's decision ------------------------------------------------------------------------


def test_triage_gets_the_rail_task_the_network_and_a_deadline(adapter):
    state = history_state(adapter, [(0.73, 111.3, 178), (0.70, 111.3, 178)], params={"hub_count": 4})
    changed, summary = adapter.material_diff(EARLIER, state)
    assert changed and "the vision agent's note (not a measurement)" in summary
    (asked,) = adapter.agent.calls
    assert asked["task"] == "rail_access" and asked["read_timeout"] == 58.0
    assert set(asked["network"]) == {"ways", "stations", "pois", "fetched_at"}
    assert asked["network"]["stations"][0]["name"] == "Central"
    assert asked["web_search"] is adapter.search and asked["object_noun"] is None
    assert asked["params"] == {"reach_m": 1000.0, "hub_count": 4} and asked["backend"] == "opencv"
    assert asked["site"] == {"id": "sydney", "name": "Sydney", "polygon": SYDNEY}
    assert asked["scene"]["id"] == "S1" and asked["scene"]["assets"]["swir16"].endswith("S1/swir16.tif")
    assert asked["history"] is state["sites"]["sydney"]["history"]
    assert asked["load_image"] is adapter.load_image
    assert asked["measure"] is adapter.measure and asked["max_tool_calls"] == 3
    assert asked["verdict"]["delta"]["served_share"] == pytest.approx(-0.03)
    # The agent is given the tick's clock and a deadline counted from when fetching began.
    assert asked["clock"] is adapter.clock and asked["deadline"] == pytest.approx(adapter.clock() + 85, abs=5)
    # The trail is kept in the state the research tick stores.
    assert state["triage"]["sydney"]["verdict"] == "real"
    assert state["sites"]["sydney"]["history"][-1]["triage"]["verdict"] == "real"


def test_web_context_is_withheld_when_the_topic_turns_it_off(adapter):
    state = history_state(adapter, [(0.73, 111.3, 178), (0.70, 111.3, 178)], web_context=False)
    adapter.material_diff(EARLIER, state)
    assert adapter.agent.calls[0]["web_search"] is None


def test_an_artefact_is_not_material_and_triage_can_be_turned_off(adapter):
    adapter.triage_agent = FakeAgent("artefact", "a cloud bank over the western suburbs")
    state = history_state(adapter, [(0.73, 111.3, 178), (0.70, 111.3, 178)])
    changed, summary = adapter.material_diff(EARLIER, state)
    assert not changed
    assert f'the agent judged it an artefact; {NOTE}"a cloud bank' in summary
    assert state["sites"]["sydney"]["history"][-1]["triage"]["verdict"] == "artefact"

    adapter.triage_agent = adapter.agent
    state = history_state(adapter, [(0.73, 111.3, 178), (0.70, 111.3, 178)], triage=False)
    assert adapter.material_diff(EARLIER, state)[0]
    assert adapter.agent.calls == [] and "triage" not in state


def test_no_model_means_no_finding(adapter):
    def broken(topic):
        raise RuntimeError("model config unreadable")

    adapter.choose_model = broken
    state = history_state(adapter, [(0.73, 111.3, 178), (0.70, 111.3, 178)])
    assert not adapter.material_diff(EARLIER, state)[0]
    assert "no model to triage with" in state["triage"]["sydney"]["reason"] and adapter.agent.calls == []


# --- what else the pipeline asks of it -----------------------------------------------------------


def test_figures_name_the_scene_year_and_osm(adapter):
    state = history_state(adapter, [(0.73, 111.3, 178)])
    (figure,) = adapter.figures(state)
    assert figure["key"] == "vision/rail/sydney/S0.png"
    # It names the figure's symbols: the heat, the outlines, the network, the flags and the links.
    assert figure["caption"] == (
        "Rail access in Sydney, Sentinel-2 2026-10-10: built-up density (warm colours), areas beyond 1 km "
        "of a station (outlined), OpenStreetMap rail lines and stations, flagged stations (red rings) and "
        "simulated links (dashed). Processed imagery; contains modified Copernicus Sentinel data 2026; map "
        "data © OpenStreetMap contributors."
    )
    assert figure["alt"] == (
        "Processed satellite map of Sydney showing rail lines, stations, flagged stations, simulated links "
        "and built-up areas beyond walking reach of a station"
    )
    assert clean_figures([figure]) == [figure]  # within common/figures.py's limits, nothing changed
    # The reach the scene was measured with, not the default.
    state = history_state(adapter, [(0.73, 111.3, 178)], reach_m=800)
    assert "areas beyond 800 m of a station" in adapter.figures(state)[0]["caption"]
    # Nothing for a tick that measured nothing.
    assert adapter.figures({"measured": [], "sites": state["sites"]}) == []


def test_a_long_site_name_is_cut_so_the_figure_is_kept(adapter):
    # The longest name a config may give (80 characters): the caption fits, the alt text is cut.
    name = "Greater Sydney metropolitan area with the Illawarra, the Blue Mountains and more"
    assert len(name) == vision_sites.MAX_SITE_NAME_CHARS
    sites = [{"id": "sydney", "name": name, "polygon": SYDNEY}]
    state = run(adapter, scene(1), FakeRailWorker({"S1": metrics()}), sites=sites, reach_m=800)
    (figure,) = adapter.figures(state)
    assert len(figure["caption"]) <= MAX_CAPTION_CHARS and len(figure["alt"]) <= MAX_ALT_CHARS
    assert figure["caption"].startswith("Rail access in Greater Sydney metropolitan area")
    assert "…" in figure["caption"] and figure["caption"].endswith("map data © OpenStreetMap contributors.")
    assert "…" in figure["alt"] and figure["alt"].endswith("beyond walking reach of a station")
    assert clean_figures([figure]) == [figure]
    # A longer one (a state written before names were capped) is cut the same way.
    state["sites"]["sydney"]["name"] = name * 4
    (figure,) = adapter.figures(state)
    assert len(figure["caption"]) == MAX_CAPTION_CHARS and clean_figures([figure]) == [figure]
    assert "(red rings) and simulated links (dashed)" in figure["caption"]


def test_source_refs_link_the_scene_and_the_overpass_query(adapter):
    state = history_state(adapter, [(0.73, 111.3, 178)])
    refs = adapter.source_refs(state)
    assert refs == [
        {
            "url": "https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/S0",
            "title": "Sentinel-2 L2A scene S0",
            "accessed_at": state["fetched_at"],
        },
        {
            "url": state["sites"]["sydney"]["network"]["query_url"],
            "title": "OpenStreetMap rail data for Sydney via the Overpass API",
            "accessed_at": state["sites"]["sydney"]["network"]["fetched_at"],
        },
    ]
    assert refs[1]["url"].startswith("https://overpass-api.de/api/interpreter?data=")
    assert len(refs[1]["url"]) <= 400
    # A network cached without its link (an older state) gets one from the site's polygon.
    bare = json.loads(json.dumps(state))
    del bare["sites"]["sydney"]["network"]["query_url"]
    assert adapter.source_refs(bare)[1]["url"] == osm.query_url(SYDNEY)


def test_review_evidence_is_the_stored_measurement_without_the_network(adapter):
    state = history_state(adapter, [(0.73, 111.3, 178), (0.70, 120.0, 180)])
    adapter.http_post = adapter.measure = adapter.fetch_network = None  # any call would fail
    text = adapter.review_evidence(topic(), state)
    site = json.loads(text)["sites"]["sydney"]
    assert site == {
        "name": "Sydney", "scene_id": "S1", "captured_at": "2026-10-11T00:00:00Z", "served_share": 0.7,
        "desert_km2": 120.0, "station_count": 180, "reach_m": 1000.0, "coverage": 0.93, "flag_count": 1,
        "previous_served_shares": [0.73],
    }  # fmt: skip
    for never in ("ways", "points", "query_url", "Central", "pois"):
        assert never not in text
    assert adapter.review_evidence(topic(), None) is None


def test_the_summary_prompt_keeps_to_proxies():
    prompt = ra.RailAccessAdapter().build_summary_prompt(
        {"name": "Rail access"}, "Sydney: served share 71 % in scene S2", {"task": "rail_access"}
    )
    assert prompt.startswith(
        "You are summarising a change measured in satellite imagery and OpenStreetMap data for the topic "
        '"Rail access".\n\nWhat was measured: Sydney: served share 71 % in scene S2\n\n'
    )
    for rule in (
        "none is ridership, patronage or journey time",
        "station and line names exactly as given",
        "what was not measured (ridership, service frequency, planned works)",
        "Give no cause, forecast, political, planning or investment view",
        "do not say any area is well or badly served in absolute terms",
        "do not invent place names or numbers that are not given above",
    ):
        assert rule in prompt


# --- the research tick keeps the running state -----------------------------------------------------

REGION = "ap-southeast-2"
BUCKET = "bloggerbear-content-test"


@pytest.fixture
def tick_env(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "TOPICS_TABLE": "Topics", "FINDINGS_TABLE": "Findings", "MODEL_CONFIG_TABLE": "ModelConfig",
        "CONTENT_BUCKET": BUCKET, "BEDROCK_MODEL_ID": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)
    research_tick_handler._s3_client = None
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    monkeypatch.setattr(research_tick_handler, "set_topic_last_research_at", lambda topic_id, ts: None)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name, keys in (("Topics", ["topic_id"]), ("Findings", ["topic_id", "captured_at"]),
                           ("ModelConfig", ["config_id"])):  # fmt: skip
            ddb.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": k, "KeyType": t} for k, t in zip(keys, ("HASH", "RANGE"), strict=False)
                ],
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in keys],
                BillingMode="PAY_PER_REQUEST",
            )
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        # As DynamoDB holds it: every number a Decimal, which the adapter must turn back.
        stored = json.loads(json.dumps(topic(reach_m=1000, params={"hub_count": 4})), parse_float=Decimal)
        boto3.resource("dynamodb", region_name=REGION).Table("Topics").put_item(Item=stored)
        yield


def tick_with(monkeypatch, scene_id, m, when, agent=None):
    monkeypatch.setattr(ra.RailAccessAdapter, "triage_agent", staticmethod(agent or FakeAgent()))
    monkeypatch.setattr(ra.RailAccessAdapter, "choose_model", staticmethod(lambda topic: ("m", None)))
    monkeypatch.setattr(ra.RailAccessAdapter, "http_post", FakeStac([item(scene_id, when)]))
    monkeypatch.setattr(ra.RailAccessAdapter, "measure", FakeRailWorker({scene_id: m}))
    monkeypatch.setattr(ra.RailAccessAdapter, "fetch_network", FakeOverpass(network()))
    monkeypatch.setattr(ra.RailAccessAdapter, "web_search", staticmethod(FakeSearch()))
    # The figure store is the real one, into moto's bucket.
    tracked = {"text": "summary", "model_id": "m", "input_tokens": 1, "output_tokens": 1}
    tracked["used_fallback"] = False
    with patch("research_tick_handler.invoke_model_tracked", return_value=tracked) as bedrock:
        result = research_tick_handler.handler({"topic_id": "rail"}, None)
    return result, bedrock


def running_state():
    body = boto3.client("s3", region_name=REGION).get_object(
        Bucket=BUCKET, Key="snapshots/rail/running-state.json"
    )["Body"].read()
    return json.loads(body)


def test_the_running_state_round_trips_through_the_research_tick(tick_env, monkeypatch):
    first, bedrock = tick_with(monkeypatch, "S0", metrics(), "2026-09-10T00:00:00Z")
    assert first["status"] == "material_change"
    prompt = bedrock.call_args[0][0]
    assert "First observation. Sydney: served share 73 %, 178 stations" in prompt
    assert "satellite imagery and OpenStreetMap data" in prompt
    state = running_state()
    sydney = state["sites"]["sydney"]
    assert sydney["network"]["counts"]["stations"] == 1
    assert sydney["network"]["fetched_at"] == state["fetched_at"]
    assert sydney["history"][-1]["image_key"] == "vision/rail/sydney/S0.png"
    png = boto3.client("s3", region_name=REGION).get_object(Bucket=BUCKET, Key="vision/rail/sydney/S0.png")
    assert png["Body"].read() == PNG and png["ContentType"] == "image/png"
    (finding,) = boto3.resource("dynamodb", region_name=REGION).Table("Findings").scan()["Items"]
    assert [f["key"] for f in finding["figures"]] == ["vision/rail/sydney/S0.png"]
    assert "Sentinel data 2026" in finding["figures"][0]["caption"]
    assert [r["title"] for r in finding["source_refs"]] == [
        "Sentinel-2 L2A scene S0", "OpenStreetMap rail data for Sydney via the Overpass API",
    ]  # fmt: skip

    # A quiet tick: no Finding, no model call, the state kept and the network not fetched again.
    result, bedrock = tick_with(monkeypatch, "S1", metrics(served_share=0.735), "2026-09-11T00:00:00Z")
    assert result == {"status": "no_change"}
    bedrock.assert_not_called()  # rule 2: no Bedrock without material change
    state = running_state()
    assert [e["scene_id"] for e in state["sites"]["sydney"]["history"]] == ["S0", "S1"]
    assert state["sites"]["sydney"]["network"]["fetched_at"] == sydney["network"]["fetched_at"]

    # A change the agent calls real: a Finding whose prompt carries the measured change.
    moved = metrics(served_share=0.70, station_count=180)
    result, bedrock = tick_with(monkeypatch, "S2", moved, "2026-09-12T00:00:00Z")
    assert result["status"] == "material_change"
    prompt = bedrock.call_args[0][0]
    assert "served share 70 % in scene S2" in prompt and "180 stations against 178" in prompt
    assert "served share moved, station count changed; the vision agent's note (not a measurement)" in prompt
    assert [e["scene_id"] for e in running_state()["sites"]["sydney"]["history"]] == ["S0", "S1", "S2"]
    assert len(boto3.resource("dynamodb", region_name=REGION).Table("Findings").scan()["Items"]) == 2


def test_an_artefact_tick_is_kept_as_running_state_without_a_finding(tick_env, monkeypatch):
    tick_with(monkeypatch, "S0", metrics(), "2026-09-10T00:00:00Z")
    haze = FakeAgent("artefact", "haze over the harbour")
    hazy = metrics(served_share=0.65)
    result, bedrock = tick_with(monkeypatch, "S1", hazy, "2026-09-11T00:00:00Z", agent=haze)
    assert result == {"status": "no_change"}
    bedrock.assert_not_called()
    state = running_state()
    assert state["triage"]["sydney"]["reason"] == "haze over the harbour"
    assert [e["served_share"] for e in state["sites"]["sydney"]["history"]] == [0.73, 0.65]
    assert haze.calls[0]["task"] == "rail_access"
    assert haze.calls[0]["params"] == {"reach_m": 1000.0, "hub_count": 4}
