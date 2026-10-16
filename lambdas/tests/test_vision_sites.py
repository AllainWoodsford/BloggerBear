"""Tests for common/vision_sites.py: what the two vision adapters share. The config checks every
vision topic gets, the STAC search and the choice of scene (moved here from the ship adapter's
tests, which now hold only what that adapter decides), the figure store, the baseline's clear
entries, the tick's budget, the STAC references, the order sites are visited in, the rule that a
tick which measured nothing is not yet an observation, and the marker that reports the first one
once."""

from __future__ import annotations

import re
import socket
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
from moto import mock_aws

from common import vision_sites as vs
from common.adapters import rail_access
from common.figures import clean_figure

POLY = [[151.20, -33.97], [151.26, -33.97], [151.26, -34.02], [151.20, -34.02]]
PREFIX = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/56/H/LH/"
SITES = [{"id": "botany", "name": "Botany Bay", "polygon": POLY}]
# A topic's defaults, as an adapter would hand them over, with one key of its own.
DEFAULTS = {
    "backend": "opencv", "params": {}, "max_cloud_cover": 60, "lookback_days": 10, "coverage_floor": 0.7,
    "history_size": 8, "min_baseline": 2, "max_sites_per_tick": 5, "time_budget_seconds": 60,
    "triage": True, "triage_max_tool_calls": 3, "triage_deadline_seconds": 85, "own_threshold": 0.35,
}  # fmt: skip
FLOATS = vs.FLOAT_KEYS + ("own_threshold",)


def item(scene_id, when, bbox=(150.5, -34.5, 151.8, -33.5), assets=("nir", "green", "scl"), cloud=12.0):
    return {
        "id": scene_id,
        "bbox": list(bbox),
        "properties": {"datetime": when, "eo:cloud_cover": cloud},
        "assets": {key: {"href": f"{PREFIX}{scene_id}/{key}.tif"} for key in assets},
    }


class FakeStac:
    def __init__(self, features=None, error=None):
        self.features, self.error, self.bodies = features or [], error, []

    def __call__(self, url, json, timeout):
        assert url == vs.STAC_SEARCH_URL
        self.bodies.append(json)
        if self.error:
            raise self.error
        features = self.features

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"features": features}

        return Response()


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


# --- config ------------------------------------------------------------------------------------


def test_defaults_are_filled_and_the_numbers_typed():
    config = vs.parse_site_config(
        {"sites": SITES, "history_size": Decimal("4"), "own_threshold": Decimal("0.5"), "lookback_days": 7.0},
        DEFAULTS,
        floats=FLOATS,
    )
    assert config["sites"] == SITES and config["backend"] == "opencv" and config["triage"] is True
    assert config["history_size"] == 4 and isinstance(config["history_size"], int)
    assert config["lookback_days"] == 7 and isinstance(config["lookback_days"], int)
    assert config["own_threshold"] == 0.5 and config["coverage_floor"] == 0.7
    assert isinstance(config["time_budget_seconds"], float) and config["time_budget_seconds"] == 60.0


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"sites": []},
        {"sites": "botany"},
        {"sites": [{"id": "a"}]},
        {"sites": [{"polygon": POLY}]},
        {"sites": [{"id": "a", "polygon": POLY}, {"id": "a", "polygon": POLY}]},
        {"sites": SITES, "backend": "gpu"},
        {"sites": SITES, "history_size": 1},
        {"sites": SITES, "min_baseline": 0},
        {"sites": SITES, "history_size": "many"},
        {"sites": SITES, "coverage_floor": None},
        {"sites": SITES, "time_budget_seconds": 81},
        # A float that is not finite would pass a `> cap` check (NaN compares false with anything).
        {"sites": SITES, "time_budget_seconds": "nan"},
        {"sites": SITES, "time_budget_seconds": "inf"},
        {"sites": SITES, "time_budget_seconds": -5},
        # Under the measuring reserve no tick could ever ask the worker (time_to_measure).
        {"sites": SITES, "time_budget_seconds": 44.9},
        {"sites": SITES, "triage_deadline_seconds": "nan"},
        {"sites": SITES, "triage_deadline_seconds": 1e9},
        {"sites": SITES, "triage_deadline_seconds": 0},
        {"sites": SITES, "triage_max_tool_calls": 1e9},
        {"sites": SITES, "triage_max_tool_calls": -1},
        {"sites": SITES, "history_size": "inf"},
        {"sites": SITES, "history_size": 21},
        {"sites": SITES, "min_baseline": 8},  # a history of 8 holds at most 7 earlier scenes
        {"sites": SITES, "max_sites_per_tick": 0},
        {"sites": SITES, "max_sites_per_tick": 11},
        {"sites": SITES, "coverage_floor": -0.1},
        {"sites": SITES, "coverage_floor": "inf"},
        {"sites": SITES, "max_cloud_cover": "nan"},
        {"sites": SITES, "own_threshold": -1},
        {"sites": SITES, "own_threshold": "-inf"},
        {"sites": SITES, "triage": "false"},
        {"sites": SITES, "triage": 1},
        {"sites": SITES, "web_context": "false"},
        {"sites": SITES, "history_size": True},
        {"sites": [{"id": "a", "name": ["Botany", "Bay"], "polygon": POLY}]},
        {"sites": [{"id": "a", "name": "", "polygon": POLY}]},
        {"sites": [{"id": "a", "name": "\u200b\n", "polygon": POLY}]},
        {"sites": [{"id": "a", "name": "B" * 81, "polygon": POLY}]},
        {"sites": [{"id": "bay:1", "polygon": POLY}]},
        {"sites": [{"id": "..", "polygon": POLY}]},
        {"sites": [{"id": "bay/../x", "polygon": POLY}]},
        {"sites": [{"id": "bay.1", "polygon": POLY}]},
        {"sites": [{"id": "x" * 65, "polygon": POLY}]},
        {"sites": [{"id": 12, "polygon": POLY}]},
    ],
)
def test_bad_config_is_refused(config):
    with pytest.raises(vs.ConfigError):
        vs.parse_site_config(config, DEFAULTS, floats=FLOATS)


def test_site_ids_and_names_are_what_a_figure_key_and_a_caption_can_carry():
    sites = [{"id": "Botany_Bay-2", "name": "  Botany\tBay\u202e\n", "polygon": POLY},
             {"id": "b" * 64, "polygon": POLY}]  # fmt: skip
    config = vs.parse_site_config({"sites": sites}, DEFAULTS, floats=FLOATS)
    assert config["sites"][0]["name"] == "Botany Bay" and "name" not in config["sites"][1]
    assert sites[0]["name"] == "  Botany\tBay\u202e\n"  # the caller's config is not changed
    for site in config["sites"]:  # a figure key built from the id is one common/figures.py keeps
        assert clean_figure({"key": f"vision/topic/{site['id']}/S2A_X.png", "caption": "map"})
    # The bounds are inclusive where they say so.
    edge = {"sites": SITES, "time_budget_seconds": 80, "triage_deadline_seconds": 90,
            "triage_max_tool_calls": 5, "history_size": 20, "min_baseline": 19, "max_sites_per_tick": 10,
            "coverage_floor": 0, "own_threshold": 0, "triage": False}  # fmt: skip
    assert vs.parse_site_config(edge, DEFAULTS, floats=FLOATS)["triage"] is False
    # The budget's floor is the measuring reserve itself: one site can still be asked for.
    low = vs.parse_site_config({"sites": SITES, "time_budget_seconds": vs.MEASURE_RESERVE_SECONDS},
                               DEFAULTS, floats=FLOATS)
    assert low["time_budget_seconds"] == vs.MEASURE_RESERVE_SECONDS == 45.0


def test_the_budget_cap_is_the_ticks_and_the_site_cap_is_a_parameter():
    assert vs.MAX_TIME_BUDGET_SECONDS == 80.0
    assert vs.parse_site_config({"sites": SITES, "time_budget_seconds": 80}, DEFAULTS, floats=FLOATS)
    two = [{"id": "a", "polygon": POLY}, {"id": "b", "polygon": POLY}]
    assert len(vs.parse_site_config({"sites": two}, DEFAULTS, floats=FLOATS)["sites"]) == 2
    with pytest.raises(vs.ConfigError):
        vs.parse_site_config({"sites": two}, DEFAULTS, floats=FLOATS, max_sites=1)


def test_dynamodb_decimals_become_plain_numbers():
    config = vs.parse_site_config(
        {"sites": [{"id": "a", "polygon": [[Decimal("151.2"), Decimal("-33.97")]] * 3}],
         "params": {"block_size": Decimal("31")}, "own_threshold": Decimal("4")},  # fmt: skip
        DEFAULTS,
        floats=FLOATS,
    )
    assert config["sites"][0]["polygon"][0] == [151.2, -33.97]
    assert config["params"]["block_size"] == 31 and isinstance(config["params"]["block_size"], int)
    assert config["own_threshold"] == 4.0
    assert vs.plain((Decimal("1"), [Decimal("2.5")])) == [1, [2.5]]


# --- the search ----------------------------------------------------------------------------------


def test_search_asks_for_the_site_and_picks_the_newest_covering_scene():
    stac = FakeStac(
        [
            item("S2B_straddles", "2026-10-05T00:00:00Z", bbox=(151.21, -34.5, 151.8, -33.5)),
            item("S2A_no_scl", "2026-10-04T00:00:00Z", assets=("nir", "green")),
            item("S2A_good", "2026-10-03T00:00:00Z"),
        ]
    )
    config = vs.parse_site_config({"sites": SITES}, DEFAULTS, floats=FLOATS)
    now = datetime(2026, 10, 6, tzinfo=UTC)
    scene = vs.search_newest_scene(config["sites"][0], config, now, http_post=stac)
    assert scene["id"] == "S2A_good"
    assert scene["assets"] == {name: f"{PREFIX}S2A_good/{name}.tif" for name in ("nir", "green", "scl")}
    assert scene["captured_at"] == "2026-10-03T00:00:00Z" and scene["cloud_cover"] == 12.0
    body = stac.bodies[0]
    ring = body["intersects"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    assert body["collections"] == ["sentinel-2-l2a"] and body["limit"] == 10
    assert body["query"] == {"eo:cloud_cover": {"lte": 60.0}}
    assert body["datetime"] == "2026-09-26T00:00:00Z/2026-10-06T00:00:00Z"
    assert body["sortby"][0]["direction"] == "desc"


def test_the_search_wants_every_band_the_task_reads():
    rail = {"red": "red", "nir": "nir", "swir16": "swir16", "scl": "scl"}
    stac = FakeStac(
        [
            item("S2A_no_swir", "2026-10-05T00:00:00Z", assets=("red", "nir", "scl")),
            item("S2A_rail", "2026-10-03T00:00:00Z", assets=("red", "nir", "swir16", "scl", "green")),
        ]
    )
    config = vs.parse_site_config({"sites": SITES}, DEFAULTS, floats=FLOATS)
    now = datetime(2026, 10, 6, tzinfo=UTC)
    scene = vs.search_newest_scene(config["sites"][0], config, now, http_post=stac, assets=rail)
    assert scene["id"] == "S2A_rail" and set(scene["assets"]) == set(rail)
    assert vs.search_newest_scene(config["sites"][0], config, now, http_post=FakeStac([])) is None


# --- the figure store ----------------------------------------------------------------------------


def test_the_figure_store_round_trips_through_the_content_bucket(monkeypatch):
    region = "ap-southeast-2"
    env = {"AWS_DEFAULT_REGION": region, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
           "CONTENT_BUCKET": "content-test"}  # fmt: skip
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        s3 = boto3.client("s3", region_name=region)
        s3.create_bucket(Bucket="content-test", CreateBucketConfiguration={"LocationConstraint": region})
        key = vs.store_image("rail", "sydney", "S2A_56HLH_20260915_0_L2A", b"\x89PNG\r\n\x1a\nfigure")
        assert key == "vision/rail/sydney/S2A_56HLH_20260915_0_L2A.png"
        stored = s3.get_object(Bucket="content-test", Key=key)
        assert stored["ContentType"] == "image/png"
        assert vs.load_image(key) == b"\x89PNG\r\n\x1a\nfigure"


def test_the_content_buckets_calls_are_bounded_so_a_stalled_read_ends_inside_the_tick():
    """S3_CONFIG: a few seconds to connect, 15 s on a read, two attempts in all. Two attempts that
    each stall to their limits, with standard mode's wait of under a second before the second,
    still answer the tick's first visit (its scene search first) inside rail_access's default
    budget, and so well inside the research tick's 120 s Lambda: the site is deferred and the
    state stored. botocore's defaults took about 308 s."""
    config = vs.S3_CONFIG
    assert (config.connect_timeout, config.read_timeout) == (vs.S3_CONNECT_TIMEOUT_SECONDS, 15)
    assert vs.S3_CONNECT_TIMEOUT_SECONDS <= 5
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
    two_attempts = 2 * (vs.S3_CONNECT_TIMEOUT_SECONDS + vs.S3_READ_TIMEOUT_SECONDS) + 1
    assert vs.STAC_TIMEOUT_SECONDS + two_attempts < rail_access.DEFAULTS["time_budget_seconds"] < 120


@pytest.fixture
def stalled_s3(monkeypatch):
    """A loopback socket that accepts and never answers, as the content bucket's endpoint. Yields
    the connections it accepted (one per attempt)."""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    for key in ("AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"):
        monkeypatch.delenv(key, raising=False)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    accepted, stop = [], threading.Event()

    def hold():
        while not stop.is_set():
            try:
                accepted.append(server.accept()[0])  # held open, never answered
            except OSError:
                continue

    for key, value in {
        "AWS_DEFAULT_REGION": "ap-southeast-2", "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing", "CONTENT_BUCKET": "content-test",
        "AWS_ENDPOINT_URL_S3": f"http://127.0.0.1:{server.getsockname()[1]}",
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)
    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        yield accepted
    finally:
        stop.set()
        holder.join(timeout=2)
        for each in accepted:
            each.close()
        server.close()


def test_a_stalled_network_read_raises_after_two_bounded_attempts(stalled_s3, monkeypatch):
    """For real: load_network's client against an endpoint that never answers, with S3_CONFIG's
    retries and its read timeout scaled down to 0.3 s. It raises (rail_access then defers the site,
    NETWORK_READ_ERROR) after two attempts of one read timeout each and the wait between them."""
    read = 0.3
    monkeypatch.setattr(vs, "S3_CONFIG", vs.S3_CONFIG.merge(Config(read_timeout=read)))

    began = time.monotonic()
    with pytest.raises(ReadTimeoutError):
        vs.load_network("rail", "sydney")
    took = time.monotonic() - began

    assert len(stalled_s3) == 2  # two attempts in all
    assert 2 * read <= took < 2 * read + 1 + 1.5  # standard mode waits under 1 s before the second


# --- the baseline --------------------------------------------------------------------------------


def test_clear_entries_take_the_value_key():
    history = [
        {"count": 3, "coverage": 0.9},
        {"count": None, "coverage": 0.9},
        {"count": 4, "coverage": 0.5},
        {"served_share": 0.7, "coverage": 0.95},
        {"served_share": 0.0, "coverage": 0.95},
    ]
    assert vs.clear_entries(history, 0.7) == [history[0]]
    assert vs.clear_entries(history, 0.7, value_key="served_share") == history[3:]
    assert vs.clear_entries(history, 0.96, value_key="served_share") == []


# --- the budget ----------------------------------------------------------------------------------


def test_the_budget_counts_from_when_it_was_made_on_the_given_clock():
    clock = Clock(100.0)
    budget = vs.Budget(clock, 70)
    assert budget.started == 100.0 and budget.seconds == 70.0
    assert budget.elapsed() == 0.0 and budget.remaining() == 70.0 and not budget.exhausted()
    clock.now = 150.0
    assert budget.elapsed() == 50.0 and budget.remaining() == 20.0 and not budget.exhausted()
    clock.now = 170.0
    assert not budget.exhausted()  # the budget is the last allowed moment
    clock.now = 170.5
    assert budget.exhausted() and budget.remaining() == -0.5


# --- references ----------------------------------------------------------------------------------


def test_stac_refs_link_each_measured_scene():
    measured = [{"site_id": "a", "scene_id": "S0"}, {"site_id": "b", "scene_id": "S1"}]
    assert vs.stac_refs(measured, "2026-10-06T00:00:00+00:00") == [
        {
            "url": f"https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/{scene}",
            "title": f"Sentinel-2 L2A scene {scene}",
            "accessed_at": "2026-10-06T00:00:00+00:00",
        }
        for scene in ("S0", "S1")
    ]
    assert vs.stac_refs([], None) == [] and vs.stac_refs(None, None) == []


# --- the order sites are visited in --------------------------------------------------------------


def test_order_sites_puts_the_unvisited_first_then_the_longest_unvisited():
    sites = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    records = {
        "a": {"last_attempt_at": "2026-10-05T00:00:00+00:00", "history": [{"scene_id": "S"}]},
        "b": {"last_attempt_at": "2026-10-03T00:00:00+00:00", "history": []},  # tried, never measured
        "c": {"history": []},
        # d has no record at all: a site added to the topic since the last tick
    }
    assert [s["id"] for s in vs.order_sites(sites, records)] == ["c", "d", "b", "a"]
    # Nothing known: the config's order.
    assert vs.order_sites(sites, None) == sites and vs.order_sites(sites, {}) == sites
    # Ties keep the config's order, so the rotation is stable.
    same = {"a": {"last_attempt_at": "t"}, "b": {"last_attempt_at": "t"}}
    assert [s["id"] for s in vs.order_sites(sites[:2], same)] == ["a", "b"]
    assert [s["id"] for s in vs.order_sites(sites[:2][::-1], same)] == ["b", "a"]


# --- the site key --------------------------------------------------------------------------------


def test_the_site_key_is_a_short_stable_fingerprint_of_the_polygon():
    key = vs.site_key(POLY)
    assert re.fullmatch(r"[0-9a-f]{16}", key)
    # The same polygon however it was read back (DynamoDB's Decimals, ints for whole degrees).
    as_decimals = [[Decimal(str(x)), Decimal(str(y))] for x, y in POLY]
    assert vs.site_key(as_decimals) == vs.site_key(vs.plain(as_decimals)) == key
    assert vs.site_key([[151, -34], [152, -34], [152, -33]]) == vs.site_key([[151.0, -34.0], [152.0, -34.0],
                                                                              [152.0, -33.0]])  # fmt: skip
    # Any edit is another key.
    assert vs.site_key([[151.2, -33.97], *POLY[1:]]) == key
    assert vs.site_key([[151.21, -33.97], *POLY[1:]]) != key and vs.site_key(POLY[::-1]) != key


# --- the first observation -----------------------------------------------------------------------


def test_anything_measured_and_the_errors_of_a_tick_that_measured_nothing():
    assert not vs.anything_measured(None) and not vs.anything_measured({})
    assert not vs.anything_measured({"sites": {"a": {"history": []}, "b": {}}})
    assert vs.anything_measured({"sites": {"a": {"history": []}, "b": {"history": [{"scene_id": "S"}]}}})
    # With a floor, only a scene measured at or above it counts.
    cloudy = {"sites": {"a": {"history": [{"scene_id": "S", "coverage": 0.31}]}}}
    assert vs.anything_measured(cloudy) and not vs.anything_measured(cloudy, 0.6)
    assert vs.anything_measured({"sites": {"a": {"history": [{"coverage": 0.6}]}}}, 0.6)
    state = {
        "sites": {
            "a": {"name": "Botany Bay", "last_error": {"code": "search", "detail": "down"}},
            "b": {"history": []},
            "c": {"last_error": {"code": "osm"}},
        }
    }
    assert vs.site_errors(state) == "Botany Bay: search; c: osm"
    assert vs.site_errors({"sites": {}}) == "" and vs.site_errors({}) == ""


def test_the_first_observation_comes_from_the_first_clear_scene():
    clear = {"name": "Botany Bay", "coverage": 0.9}
    cloudy = {"name": "Kurnell", "coverage": 0.31}
    state = {"sites": {"a": {"name": "Botany Bay", "last_error": {"code": "search"}}}}
    # Nothing measured before: the clear verdicts are the first observation, the cloudy ones are not.
    assert vs.first_observation(None, state, [clear, cloudy], 0.6) == ([clear], "")
    assert vs.first_observation(None, state, [cloudy], 0.6) == (
        [], "nothing measured clearly yet: Kurnell: coverage 31 % is below the 60 % floor"
    )
    assert vs.first_observation(None, state, [], 0.6) == ([], "nothing measured yet: Botany Bay: search")
    assert vs.first_observation(None, {}, [], 0.6) == ([], "nothing measured yet: no scene to measure")
    # A cloudy scene before is not an observation; a clear one is, and then the tick is compared.
    before = {"sites": {"a": {"history": [{"coverage": 0.31}]}}}
    assert vs.first_observation(before, state, [clear], 0.6) == ([clear], "")
    before["sites"]["a"]["history"].append({"coverage": 0.6})
    assert vs.first_observation(before, state, [cloudy], 0.6) is None
    # Once observed, the marker says so, though no clear scene is kept and the floor was raised.
    marked = {"first_observed_at": "2026-10-01T00:00:00+00:00",
              "sites": {"a": {"history": [{"coverage": 0.3}]}}}  # fmt: skip
    assert vs.first_observation(marked, state, [clear], 0.95) is None


def test_the_first_observation_is_marked_on_its_tick_and_carried_forward():
    sites = {"a": {"history": [{"scene_id": "S0", "coverage": 0.3}, {"scene_id": "S1", "coverage": 0.9}]}}
    measured = [{"site_id": "a", "scene_id": "S1"}]
    # Nothing observed before, and this tick measured a clear scene: its fetch marks the state.
    assert vs.first_observed_at(None, sites, measured, 0.6, "T1") == "T1"
    assert vs.first_observed_at({"first_observed_at": None}, sites, measured, 0.6, "T1") == "T1"
    assert vs.first_observed_at(None, sites, measured, 0.95, "T1") is None  # below the floor
    assert vs.first_observed_at(None, sites, [], 0.6, "T1") is None  # nothing measured this tick
    assert vs.first_observed_at(None, sites, [{"site_id": "a", "scene_id": "S0"}], 0.6, "T1") is None
    # Once marked, carried forward whatever the history holds and the floor says.
    assert vs.first_observed_at({"first_observed_at": "T0", "sites": {}}, {}, [], 0.99, "T1") == "T0"
    assert vs.observed_before({"first_observed_at": "T0"}, 0.99)
    # The marker decides when it is there: None is "not yet", whatever an older history kept.
    assert not vs.observed_before({"first_observed_at": None, "sites": sites}, 0.6)
    assert not vs.observed_before(None, 0.6) and not vs.observed_before({}, 0.6)


def test_a_state_from_before_the_marker_is_judged_by_its_own_scenes_and_floor():
    legacy = {"fetched_at": "T0", "thresholds": {"coverage_floor": 0.6},
              "sites": {"a": {"history": [{"scene_id": "S0", "coverage": 0.7}]}}}  # fmt: skip
    # Clear by its own floor (0.6), though not by today's (0.8): observed, and marked from now on.
    assert vs.observed_before(legacy, 0.8)
    assert vs.first_observed_at(legacy, {}, [], 0.8, "T1") == "T0"
    assert vs.first_observed_at({**legacy, "fetched_at": None}, {}, [], 0.8, "T1") == "T1"
    # Not clear by its own floor (0.75), though it would be by today's (0.6): not observed.
    legacy["thresholds"]["coverage_floor"] = 0.75
    assert not vs.observed_before(legacy, 0.6) and vs.first_observed_at(legacy, {}, [], 0.6, "T1") is None
    # A state with no floor stored is judged by the one given.
    del legacy["thresholds"]
    assert vs.observed_before(legacy, 0.6) and not vs.observed_before(legacy, 0.8)


# --- the words the operator's assistant may say (Adapter.status) ---------------------------------


def test_every_code_a_site_can_record_has_words_and_a_kind():
    """The worker's own codes, the client's and the adapters': a code with no words would be said
    as "another error", so every code that can be recorded today has its own."""
    from common import osm, vision_contract

    recorded = set(vision_contract.ERROR_CODES) | {"invoke", "reply", "bad_request", "time_budget", "search"}
    assert recorded <= set(vs.STATUS_WORDS)
    assert {kind for kind, _ in vs.STATUS_WORDS.values()} <= {"worker", "source", "site", "deferred", "other"}
    assert set(osm.ERROR_WORDS) == {"timeout", "http", "rate_limited", "bad_response", "too_large", "fetch"}
    phrases = [w for _, w in vs.STATUS_WORDS.values()]
    phrases += [*osm.ERROR_WORDS.values(), *vs.VERDICT_WORDS.values()]
    for words in phrases:
        assert re.fullmatch(r"[A-Za-z0-9 ',()%-]{3,80}", words), words


def test_error_status_reads_any_shape_and_tells_a_missing_cool_worker_apart():
    words = vs.STATUS_WORDS
    assert vs.error_status(None, words) is None and vs.error_status("x", words) is None
    assert vs.error_status({"code": 5, "detail": None}, words) == {
        "code": "other", "kind": "other", "words": "another error", "at": None, "detail": "",
    }  # fmt: skip
    cool = {"code": "invoke", "detail": "no worker is configured for backend 'cool'", "at": "T"}
    assert vs.error_status(cool, words)["code"] == "no_cool_worker"
    opencv = {"code": "invoke", "detail": "no worker is configured for backend 'opencv'", "at": "T"}
    assert vs.error_status(opencv, words)["code"] == "invoke"
    assert len(vs.error_status({"code": "internal", "detail": "x" * 900}, words)["detail"]) == 300


def test_a_build_is_kept_only_as_twelve_hex_characters():
    assert vs.scene_status({"build_sha256": "ab" * 32})["build"] == "ab" * 6
    assert vs.scene_status({"build_sha256": "not hex at all"})["build"] is None
    assert vs.scene_status({"build_sha256": ""})["build"] is None
    assert vs.scene_status({"triage": {"verdict": "ignore me", "reason": "x"}})["triage"] is None
