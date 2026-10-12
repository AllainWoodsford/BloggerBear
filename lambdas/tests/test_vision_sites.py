"""Tests for common/vision_sites.py: what the two vision adapters share. The config checks every
vision topic gets, the STAC search and the choice of scene (moved here from the ship adapter's
tests, which now hold only what that adapter decides), the figure store, the baseline's clear
entries, the tick's budget, the STAC references, the order sites are visited in, and the rule
that a tick which measured nothing is not yet an observation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from common import vision_sites as vs
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
