"""Tests for common/adapters/satellite_vision.py and the research tick's running state for it.

The STAC search, the vision worker and S3 are all faked: the worker's own behaviour is tested in
test_vision_worker.py. These hold what the adapter decides: which scene, when to measure, what is
kept, and what is material.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import research_tick_handler
from common import vision_client
from common.adapters import satellite_vision as sv
from common.adapters.registry import ADAPTER_REGISTRY

POLY = [[151.20, -33.97], [151.26, -33.97], [151.26, -34.02], [151.20, -34.02]]
PREFIX = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/56/H/LH/"


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
        assert url == sv.STAC_SEARCH_URL
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


class FakeWorker:
    """Answers with counts from a dict keyed by scene id."""

    def __init__(self, counts, coverage=None, error=None):
        self.counts, self.coverage, self.error, self.calls = counts, coverage or {}, error, []

    def __call__(self, site, scene, backend, params, coverage_floor):
        self.calls.append((site["id"], scene["id"], backend, params))
        if self.error:
            raise self.error
        reply = {
            "backend": backend,
            "build": {"build_sha256": "ab" * 32},
            "timings_ms": {"total": 100.0},
            "metrics": {
                "count": self.counts[scene["id"]],
                "coverage": self.coverage.get(scene["id"], 0.95),
                "clear_water_km2": 12.0,
                "density_per_km2": 1.0,
                "size_histogram": {},
                "rejected": {"candidates": 3},
                "quality_flags": [],
            },
        }
        return vision_client.VisionResult(reply=reply, image_png=b"\x89PNG\r\n\x1a\nfake")


SITE_NAME = "Botany Bay anchorage"


def topic(**config):
    return {
        "topic_id": "anchorages",
        "name": "Anchorages",
        "adapter": "satellite_vision",
        "adapter_config": {"sites": [{"id": "botany", "name": SITE_NAME, "polygon": POLY}], **config},
    }


class FakeAgent:
    """Stands in for common/vision_triage.triage: answers `verdict`, records what it was asked."""

    def __init__(self, verdict="real", reason="boxes sit on open water, well clear of cloud"):
        self.verdict, self.reason, self.calls = verdict, reason, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"verdict": self.verdict, "reason": self.reason, "tool_calls": [], "model_calls": 1}


@pytest.fixture
def adapter():
    a = sv.SatelliteVisionAdapter()
    a.agent = FakeAgent()
    a.triage_agent = a.agent
    a.choose_model = lambda topic: ("au.anthropic.claude-haiku-4-5-20251001-v1:0", None)
    a.stored = []

    def store(topic_id, site_id, scene_id, png):
        a.stored.append(scene_id)
        return f"vision/{scene_id}.png"

    a.store_image = store
    return a


def run(adapter, stac, worker, previous=None, **config):
    adapter.http_post, adapter.measure = stac, worker
    return adapter.fetch_state(topic(**config), previous_state=previous)


# --- config ------------------------------------------------------------------------------------


def test_registered_and_credited():
    assert ADAPTER_REGISTRY["satellite_vision"] is sv.SatelliteVisionAdapter
    assert sv.SatelliteVisionAdapter.keeps_running_state and sv.SatelliteVisionAdapter.uses_previous_state
    assert "Copernicus Sentinel data" in sv.SatelliteVisionAdapter.sources[0]["text"]


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"sites": []},
        {"sites": [{"id": "a"}]},
        {"sites": [{"id": "a", "polygon": POLY}, {"id": "a", "polygon": POLY}]},
        {"sites": [{"id": "a", "polygon": POLY}], "backend": "gpu"},
        {"sites": [{"id": "a", "polygon": POLY}], "history_size": 1},
    ],
)
def test_bad_config_is_refused(config):
    with pytest.raises(sv.ConfigError):
        sv.parse_config(config)


def test_dynamodb_decimals_become_plain_numbers():
    config = sv.parse_config(
        {"sites": [{"id": "a", "polygon": [[Decimal("151.2"), Decimal("-33.97")]] * 3}],
         "params": {"block_size": Decimal("31")}, "absolute_threshold": Decimal("4")}  # fmt: skip
    )
    assert config["sites"][0]["polygon"][0] == [151.2, -33.97]
    assert config["params"]["block_size"] == 31 and isinstance(config["params"]["block_size"], int)
    assert config["absolute_threshold"] == 4.0


# --- the search ----------------------------------------------------------------------------------


def test_search_asks_for_the_site_and_picks_the_newest_covering_scene():
    stac = FakeStac(
        [
            item("S2B_straddles", "2026-10-05T00:00:00Z", bbox=(151.21, -34.5, 151.8, -33.5)),
            item("S2A_no_scl", "2026-10-04T00:00:00Z", assets=("nir", "green")),
            item("S2A_good", "2026-10-03T00:00:00Z"),
        ]
    )
    config = sv.parse_config(topic()["adapter_config"])
    now = datetime(2026, 10, 6, tzinfo=UTC)
    scene = sv.search_newest_scene(config["sites"][0], config, now, http_post=stac)
    assert scene["id"] == "S2A_good"
    assert scene["assets"]["nir"].endswith("S2A_good/nir.tif")
    body = stac.bodies[0]
    ring = body["intersects"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    assert body["query"] == {"eo:cloud_cover": {"lte": 60.0}}
    assert body["datetime"] == "2026-09-26T00:00:00Z/2026-10-06T00:00:00Z"
    assert body["sortby"][0]["direction"] == "desc"


# --- fetching ------------------------------------------------------------------------------------


def test_a_new_scene_is_measured_and_kept(adapter):
    state = run(adapter, FakeStac([item("S1", "2026-10-03T00:00:00Z")]), FakeWorker({"S1": 12}))
    entry = state["sites"]["botany"]["history"][-1]
    assert entry["scene_id"] == "S1" and entry["count"] == 12 and entry["image_key"] == "vision/S1.png"
    assert state["measured"] == [{"site_id": "botany", "scene_id": "S1"}]
    assert state["sites"]["botany"]["name"] == "Botany Bay anchorage"
    assert state["thresholds"]["absolute_threshold"] == 5.0


def test_a_scene_already_measured_is_not_measured_again(adapter):
    stac, worker = FakeStac([item("S1", "2026-10-03T00:00:00Z")]), FakeWorker({"S1": 12})
    first = run(adapter, stac, worker)
    second = run(adapter, stac, worker, previous=first)
    assert len(worker.calls) == 1
    assert second["measured"] == []
    assert second["sites"]["botany"]["history"] == first["sites"]["botany"]["history"]


def test_the_backend_and_params_are_passed_through(adapter):
    worker = FakeWorker({"S1": 1})
    stac = FakeStac([item("S1", "2026-10-03T00:00:00Z")])
    run(adapter, stac, worker, backend="cool", params={"offset": 30})
    assert worker.calls[0][2:] == ("cool", {"offset": 30})


def test_failures_keep_the_history_and_say_why(adapter):
    first = run(adapter, FakeStac([item("S1", "2026-10-03T00:00:00Z")]), FakeWorker({"S1": 12}))
    searched = run(adapter, FakeStac(error=TimeoutError("stac down")), FakeWorker({}), previous=first)
    assert searched["sites"]["botany"]["last_error"]["code"] == "search"
    assert searched["sites"]["botany"]["history"] == first["sites"]["botany"]["history"]
    failing = FakeWorker({}, error=vision_client.VisionError("unreadable_scene", "HTTP 503"))
    measured = run(adapter, FakeStac([item("S2", "2026-10-08T00:00:00Z")]), failing, previous=first)
    assert measured["sites"]["botany"]["last_error"]["code"] == "unreadable_scene"
    assert measured["measured"] == []


def test_a_lost_figure_does_not_lose_the_count(adapter):
    def broken(*args):
        raise OSError("s3 down")

    adapter.store_image = broken
    state = run(adapter, FakeStac([item("S1", "2026-10-03T00:00:00Z")]), FakeWorker({"S1": 4}))
    assert state["sites"]["botany"]["history"][-1]["image_key"] is None


def test_history_is_capped(adapter):
    state = None
    for n in range(5):
        state = run(
            adapter, FakeStac([item(f"S{n}", f"2026-10-0{n + 1}T00:00:00Z")]), FakeWorker({f"S{n}": n}),
            previous=state, history_size=3,
        )  # fmt: skip
    assert [e["scene_id"] for e in state["sites"]["botany"]["history"]] == ["S2", "S3", "S4"]


def test_sites_per_tick_and_time_budget_are_bounded(adapter):
    sites = [{"id": f"s{n}", "polygon": POLY} for n in range(4)]
    stac, worker = FakeStac([item("S1", "2026-10-03T00:00:00Z")]), FakeWorker({"S1": 1})
    adapter.http_post, adapter.measure = stac, worker
    config = {"sites": sites, "max_sites_per_tick": 2}
    state = adapter.fetch_state({"topic_id": "t", "adapter_config": config})
    assert len(state["measured"]) == 2 and len(state["sites"]) == 4

    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    adapter.clock = lambda: next(ticks)
    worker.calls.clear()
    config = {"sites": sites, "time_budget_seconds": 60}
    state = adapter.fetch_state({"topic_id": "t", "adapter_config": config})
    assert len(worker.calls) == 1
    assert state["sites"]["s1"]["last_error"]["code"] == "time_budget"


def test_sites_rotate_one_per_tick_with_the_unvisited_first(adapter):
    """Two sites and one measurement per tick: the first tick takes the first site, the next the
    one never visited, and from then on the longest unvisited (common/vision_sites.order_sites)."""
    sites = [{"id": "a", "polygon": POLY}, {"id": "b", "polygon": POLY}]
    stac, worker = FakeStac([item("S1", "2026-10-03T00:00:00Z")]), FakeWorker({"S1": 1})
    adapter.http_post, adapter.measure = stac, worker
    config = {"sites": sites, "max_sites_per_tick": 1}

    first = adapter.fetch_state({"topic_id": "t", "adapter_config": config})
    assert [m["site_id"] for m in first["measured"]] == ["a"]
    assert first["sites"]["a"]["last_attempt_at"] == first["fetched_at"]
    assert "last_attempt_at" not in first["sites"]["b"]  # skipped for the cap: not an attempt

    second = adapter.fetch_state({"topic_id": "t", "adapter_config": config}, previous_state=first)
    assert [m["site_id"] for m in second["measured"]] == ["b"]
    assert [c[0] for c in worker.calls] == ["a", "b"]

    third = adapter.fetch_state({"topic_id": "t", "adapter_config": config}, previous_state=second)
    assert third["measured"] == [] and len(worker.calls) == 2  # both know S1; both were visited
    assert len(stac.bodies) == 4


# --- the diff ------------------------------------------------------------------------------------


def history_state(adapter, counts, coverage=None, **config):
    """Run one tick per count, each with a new scene; return the last state."""
    state = None
    for n, count in enumerate(counts):
        scene = f"S{n}"
        state = run(
            adapter, FakeStac([item(scene, f"2026-09-{10 + n}T00:00:00Z")]),
            FakeWorker({scene: count}, coverage={scene: (coverage or {}).get(n, 0.95)}),
            previous=state, **config,
        )  # fmt: skip
    return state


# What an earlier tick that measured something leaves behind, as far as material_diff cares: the
# running state has a site with a measured scene in its history. (A dict with no such history would
# read as "nothing measured yet", and the tick would be treated as the first observation.)
EARLIER = {
    "sites": {
        "botany": {
            "name": "Botany Bay anchorage",
            "history": [{"scene_id": "S-1", "count": 9, "coverage": 0.9}],
        }
    }
}


def test_a_first_tick_that_measured_nothing_is_not_material(adapter):
    """No scene, or no worker: a Finding with nothing in it would cost a model call and make an
    empty article, so there is none until something is measured; the first measurement is then the
    first observation, whether or not an empty tick came before it."""
    empty = run(adapter, FakeStac([]), FakeWorker({}))
    changed, summary = adapter.material_diff(None, empty)
    assert not changed and summary.startswith("nothing measured yet")
    assert adapter.agent.calls == []

    failing = run(adapter, FakeStac([item("S0", "2026-09-10T00:00:00Z")]),
                  FakeWorker({}, error=vision_client.VisionError("invoke", "no worker")))  # fmt: skip
    changed, summary = adapter.material_diff(empty, failing)
    assert not changed and "Botany Bay anchorage: invoke" in summary

    worker = FakeWorker({"S1": 7})
    measured = run(adapter, FakeStac([item("S1", "2026-09-11T00:00:00Z")]), worker, previous=failing)
    changed, summary = adapter.material_diff(failing, measured)
    assert changed and summary.startswith("First observation") and "7 objects" in summary
    assert adapter.agent.calls == []  # the first observation is reported, not triaged


def test_the_first_tick_is_material(adapter):
    state = history_state(adapter, [10])
    changed, summary = adapter.material_diff(None, state)
    assert changed and summary.startswith("First observation") and "10 objects" in summary


def test_the_first_observation_is_the_first_clear_scene(adapter):
    """A first scene below the coverage floor (0.7 here) is kept but reports nothing: a count over
    a third of the site is not an observation. The next clear scene is the first observation."""
    cloudy = history_state(adapter, [3], coverage={0: 0.31})
    changed, summary = adapter.material_diff(None, cloudy)
    assert not changed
    assert summary == (
        "nothing measured clearly yet: Botany Bay anchorage: coverage 31 % is below the 70 % floor"
    )
    assert cloudy["sites"]["botany"]["history"][-1]["coverage"] == 0.31  # kept, not lost
    stac = FakeStac([item("S1", "2026-09-11T00:00:00Z")])
    clear = run(adapter, stac, FakeWorker({"S1": 12}), previous=cloudy)
    changed, summary = adapter.material_diff(cloudy, clear)
    assert changed and summary == "First observation. Botany Bay anchorage: 12 objects (coverage 95%)"
    assert adapter.agent.calls == []


def diff_tick(adapter, previous, n, count, coverage=0.95, **config):
    """One research tick as research_tick_handler runs it, with a new scene S<n>: the fetch on the
    previous state, then the diff against it."""
    scene = f"S{n}"
    stac = FakeStac([item(scene, f"2026-09-{10 + n}T00:00:00Z")])
    worker = FakeWorker({scene: count}, coverage={scene: coverage})
    state = run(adapter, stac, worker, previous=previous, **config)
    return state, adapter.material_diff(previous, state)


def test_the_first_observation_is_reported_once(adapter):
    """The state records it (`first_observed_at`): a run of history_size cloudy scenes trims every
    clear one from the history, yet the next clear scene is not a first observation again."""
    small = {"history_size": 2, "min_baseline": 1}
    state, (changed, summary) = diff_tick(adapter, None, 0, 10, **small)
    assert changed and summary.startswith("First observation")
    assert state["first_observed_at"] == state["fetched_at"]
    for n in (1, 2):
        state, diff = diff_tick(adapter, state, n, 3, coverage=0.3, **small)
        assert diff == (False, f"{SITE_NAME}: coverage below the floor")
    assert [e["coverage"] for e in state["sites"]["botany"]["history"]] == [0.3, 0.3]
    state, diff = diff_tick(adapter, state, 3, 30, **small)
    assert diff == (False, f"{SITE_NAME}: baseline still building") and adapter.agent.calls == []


def test_a_raised_floor_or_a_state_from_before_the_marker_does_not_make_a_scene_first(adapter):
    state, (changed, _) = diff_tick(adapter, None, 0, 10, coverage=0.75)
    assert changed
    # The floor goes to 0.8, above the only scene so far: the next clear one is compared.
    later, diff = diff_tick(adapter, state, 1, 30, coverage=0.9, coverage_floor=0.8)
    assert diff == (False, f"{SITE_NAME}: baseline still building")
    # A state written before the marker is judged by its own scene against its own floor (0.7).
    legacy = json.loads(json.dumps(state))
    del legacy["first_observed_at"]
    later, diff = diff_tick(adapter, legacy, 1, 30, coverage=0.9, coverage_floor=0.8)
    assert diff == (False, f"{SITE_NAME}: baseline still building")
    assert later["first_observed_at"] == legacy["fetched_at"]
    # One from before with only a cloudy scene: the next clear scene is the first observation.
    cloudy, _ = diff_tick(adapter, None, 0, 3, coverage=0.3)
    del cloudy["first_observed_at"]
    clear, (changed, summary) = diff_tick(adapter, cloudy, 1, 12)
    assert changed and summary.startswith("First observation")


def test_no_material_change_while_the_baseline_builds(adapter):
    state = history_state(adapter, [10, 40])
    changed, summary = adapter.material_diff(EARLIER, state)
    assert not changed and "baseline still building" in summary


def test_a_change_within_the_usual_range_is_not_material(adapter):
    state = history_state(adapter, [10, 12, 11, 15])
    changed, summary = adapter.material_diff(EARLIER, state)
    assert not changed and "usual range" in summary


@pytest.mark.parametrize(("last", "sign"), [(30, "+"), (2, "-")])
def test_a_big_change_either_way_is_material(adapter, last, sign):
    state = history_state(adapter, [10, 12, 11, last], object_noun="large vessels")
    changed, summary = adapter.material_diff(EARLIER, state)
    assert changed
    assert f"{last} large vessels" in summary and "baseline of 11" in summary and f"({sign}" in summary
    assert "Botany Bay anchorage" in summary


def test_both_thresholds_must_be_crossed(adapter):
    # +5 vessels on a baseline of 100 is 5%: big in number, small in proportion.
    assert not adapter.material_diff(EARLIER, history_state(adapter, [100, 100, 105]))[0]
    # +3 on a baseline of 2 is 150%: big in proportion, small in number.
    assert not adapter.material_diff(EARLIER, history_state(adapter, [2, 2, 5]))[0]


def test_low_coverage_is_never_material_and_never_baseline(adapter):
    cloudy_now = history_state(adapter, [10, 11, 0], coverage={2: 0.3})
    changed, summary = adapter.material_diff(EARLIER, cloudy_now)
    assert not changed and "coverage below the floor" in summary
    # Two cloudy scenes earlier don't count toward the baseline.
    cloudy_before = history_state(adapter, [0, 0, 10, 40], coverage={0: 0.2, 1: 0.2})
    changed, summary = adapter.material_diff(EARLIER, cloudy_before)
    assert not changed and "baseline still building" in summary


def test_thresholds_come_from_the_topic(adapter):
    state = history_state(adapter, [10, 12, 11, 14], absolute_threshold=2, relative_threshold=0.2)
    assert adapter.material_diff(EARLIER, state)[0]


# --- what else the pipeline asks of it -----------------------------------------------------------


def test_review_evidence_is_the_stored_measurement_with_no_new_calls(adapter):
    state = history_state(adapter, [10, 12])
    adapter.http_post = adapter.measure = None  # any call would fail
    evidence = json.loads(adapter.review_evidence(topic(), state))
    assert evidence["sites"]["botany"]["count"] == 12
    assert evidence["sites"]["botany"]["previous_counts"] == [10]
    assert adapter.review_evidence(topic(), None) is None


def test_source_refs_link_each_measured_scene(adapter):
    state = history_state(adapter, [10])
    assert adapter.source_refs(state) == [
        {
            "url": "https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/S0",
            "title": "Sentinel-2 L2A scene S0",
            "accessed_at": state["fetched_at"],
        }
    ]


def test_the_summary_prompt_keeps_to_observations():
    prompt = sv.SatelliteVisionAdapter().build_summary_prompt(
        {"name": "Anchorages"}, "Botany: 30 large vessels", {"object_noun": "large vessels"}
    )
    assert "Botany: 30 large vessels" in prompt
    for rule in ("name no ship", "no cause, prediction, price", "safe, clear", "do not invent place names"):
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
        stored = json.loads(json.dumps(topic()), parse_float=Decimal)
        boto3.resource("dynamodb", region_name=REGION).Table("Topics").put_item(Item=stored)
        yield


def tick_with(monkeypatch, scene_id, count, when, agent=None):
    monkeypatch.setattr(sv.SatelliteVisionAdapter, "triage_agent", staticmethod(agent or FakeAgent()))
    monkeypatch.setattr(sv.SatelliteVisionAdapter, "choose_model", staticmethod(lambda topic: ("m", None)))
    monkeypatch.setattr(sv.SatelliteVisionAdapter, "http_post", FakeStac([item(scene_id, when)]))
    monkeypatch.setattr(sv.SatelliteVisionAdapter, "measure", FakeWorker({scene_id: count}))
    monkeypatch.setattr(sv.SatelliteVisionAdapter, "store_image", staticmethod(lambda *a: None))
    tracked = {"text": "summary", "model_id": "m", "input_tokens": 1, "output_tokens": 1}
    tracked["used_fallback"] = False
    with patch("research_tick_handler.invoke_model_tracked", return_value=tracked) as bedrock:
        result = research_tick_handler.handler({"topic_id": "anchorages"}, None)
    return result, bedrock


def running_state():
    body = boto3.client("s3", region_name=REGION).get_object(
        Bucket=BUCKET, Key="snapshots/anchorages/running-state.json"
    )["Body"].read()
    return json.loads(body)


def test_the_baseline_includes_scenes_that_were_not_reported(tick_env, monkeypatch):
    first, _ = tick_with(monkeypatch, "S0", 10, "2026-09-10T00:00:00Z")
    assert first["status"] == "material_change"
    for n, count in ((1, 11), (2, 12)):
        result, bedrock = tick_with(monkeypatch, f"S{n}", count, f"2026-09-1{n}T00:00:00Z")
        assert result == {"status": "no_change"}
        bedrock.assert_not_called()  # rule 2: no Bedrock without material change
    assert [e["count"] for e in running_state()["sites"]["botany"]["history"]] == [10, 11, 12]

    # Without the running state the baseline would be just the first scene, and S3 never seen.
    result, bedrock = tick_with(monkeypatch, "S3", 30, "2026-09-13T00:00:00Z")
    assert result["status"] == "material_change"
    prompt = bedrock.call_args[0][0]
    assert "baseline of 11 from 3 earlier clear scenes" in prompt
    assert [e["count"] for e in running_state()["sites"]["botany"]["history"]] == [10, 11, 12, 30]
    findings = boto3.resource("dynamodb", region_name=REGION).Table("Findings").scan()["Items"]
    assert len(findings) == 2


def test_without_a_running_state_the_last_findings_snapshot_is_used(tick_env, monkeypatch):
    tick_with(monkeypatch, "S0", 10, "2026-09-10T00:00:00Z")
    boto3.client("s3", region_name=REGION).delete_object(
        Bucket=BUCKET, Key="snapshots/anchorages/running-state.json"
    )
    result, _ = tick_with(monkeypatch, "S1", 11, "2026-09-11T00:00:00Z")
    assert result == {"status": "no_change"}
    assert [e["count"] for e in running_state()["sites"]["botany"]["history"]] == [10, 11]


# --- the agent's decision ------------------------------------------------------------------------


def test_the_agent_is_asked_only_about_a_numeric_change_and_its_yes_is_material(adapter):
    quiet = history_state(adapter, [10, 12, 11, 13])
    assert not adapter.material_diff(EARLIER, quiet)[0]
    assert adapter.agent.calls == []  # rule 2: no model call without a numeric change

    state = history_state(adapter, [10, 12, 11, 30])
    changed, summary = adapter.material_diff(EARLIER, state)
    # The agent's reason is quoted as its note, not given as a measurement.
    assert changed and 'the vision agent\'s note (not a measurement): "boxes sit on open water' in summary
    asked = adapter.agent.calls[0]
    assert asked["site"]["id"] == "botany" and asked["site"]["polygon"] == POLY
    assert asked["scene"]["id"] == "S3" and asked["scene"]["assets"]["nir"].endswith("S3/nir.tif")
    assert asked["verdict"]["baseline"] == 11 and asked["max_tool_calls"] == 3
    # The agent is given the tick's clock and a deadline counted from when fetching began (here the
    # state was built by hand, so from the diff), so it can never push the tick past 120 s.
    assert asked["clock"] is adapter.clock and asked["deadline"] == pytest.approx(adapter.clock() + 85, abs=5)
    # The trail is kept in the state the research tick stores.
    assert state["triage"]["botany"]["verdict"] == "real"
    assert state["sites"]["botany"]["history"][-1]["triage"]["verdict"] == "real"


def test_an_artefact_is_not_material_and_says_why(adapter):
    adapter.triage_agent = FakeAgent("artefact", "the extra boxes trace a thin cloud edge")
    state = history_state(adapter, [10, 12, 11, 30])
    changed, summary = adapter.material_diff(EARLIER, state)
    assert not changed
    assert (
        'the agent judged it an artefact; the vision agent\'s note (not a measurement): "the extra boxes '
        'trace a thin cloud edge"' in summary
    )
    assert state["sites"]["botany"]["history"][-1]["triage"]["verdict"] == "artefact"


def test_no_model_means_no_finding(adapter):
    def broken(topic):
        raise RuntimeError("model config unreadable")

    adapter.choose_model = broken
    state = history_state(adapter, [10, 12, 11, 30])
    assert not adapter.material_diff(EARLIER, state)[0]
    assert "no model to triage with" in state["triage"]["botany"]["reason"]
    assert adapter.agent.calls == []


def test_triage_can_be_turned_off_and_is_skipped_on_the_first_tick(adapter):
    state = history_state(adapter, [10, 12, 11, 30], triage=False)
    assert adapter.material_diff(EARLIER, state)[0]
    assert adapter.agent.calls == [] and "triage" not in state
    first = history_state(adapter, [10])
    assert adapter.material_diff(None, first)[0] and adapter.agent.calls == []


def test_an_artefact_tick_is_kept_as_running_state_without_a_finding(tick_env, monkeypatch):
    for n, count in enumerate((10, 11, 12)):
        tick_with(monkeypatch, f"S{n}", count, f"2026-09-1{n}T00:00:00Z")
    glint = FakeAgent("artefact", "glint")
    result, bedrock = tick_with(monkeypatch, "S3", 40, "2026-09-13T00:00:00Z", agent=glint)
    assert result == {"status": "no_change"}
    bedrock.assert_not_called()
    state = running_state()
    assert state["triage"]["botany"]["reason"] == "glint"
    assert [e["count"] for e in state["sites"]["botany"]["history"]] == [10, 11, 12, 40]
