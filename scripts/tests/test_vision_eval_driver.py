"""Tests for scripts/vision_eval_driver.py: the real rail access adapter driven tick by tick.

The worker runs in this process on the synthetic city (vision_benchmark.synthetic_city written as
GeoTIFFs in memory, under a host that cannot resolve), the agent is scripted or recorded, and
requests, boto3 and socket connections raise for every test: nothing here touches the network.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

import vision_eval_driver as driver_module
from vision_eval_driver import (
    Clock,
    Driver,
    LocalStore,
    RecordedAgent,
    ReplayRefused,
    ScriptedAgent,
    TickSpec,
)

ROOT = Path(__file__).resolve().parents[2]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

from common import evidence, vision_client  # noqa: E402
from common.adapters import rail_access  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket
    import urllib.request

    import boto3
    import requests

    def refuse(*args, **kwargs):
        raise AssertionError("the tests must not touch the network")

    for name in ("get", "head", "put", "post", "request"):
        monkeypatch.setattr(requests, name, refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3, "resource", refuse)


@pytest.fixture(scope="module")
def blobs():
    return driver_module.synthetic_blobs()


def synthetic_driver(blobs, agent=None, clock=None, **kwargs):
    site, scenes = driver_module.sequence_scenes("synthetic-city", None)
    topic = driver_module.topic_config("synthetic-city", site["id"], site["polygon"], site_name=site["name"],
                                       thresholds="demo", **kwargs.pop("topic", {}))  # fmt: skip
    fetch = driver_module.synthetic_fetch(blobs)
    measure = driver_module.in_process_measure(fetch, driver_module.SYNTHETIC_ENVIRON)
    driver = Driver(topic, measure=kwargs.pop("measure", measure), agent=agent or ScriptedAgent(),
                    clock=clock or (lambda n: Clock.frozen()), **kwargs)  # fmt: skip
    network = driver_module.load_network_file(driver_module.SYNTHETIC_NETWORK_FILE)
    network_b = driver_module.line_on_largest_desert(network, driver.site_id)
    specs = driver_module.tick_specs("synthetic-city", scenes, network, network_b)
    return driver, specs


# --- the synthetic city as files ----------------------------------------------------------------------


def test_the_geotiff_writer_is_read_back_exactly_by_the_workers_cog_reader():
    from vision import cog

    image = (np.arange(100 * 70, dtype=np.uint32).reshape(100, 70) % 4093).astype(np.uint16)
    transform = (300000.0, 20.0, 0.0, 6300000.0, 0.0, -20.0)
    blob = driver_module.geotiff(image, transform, 32756, tile=32, nodata="0")
    fetch = driver_module.synthetic_fetch({"https://synthetic-city.invalid/x.tif": blob})
    info, src = cog.read_info("https://synthetic-city.invalid/x.tif", fetch)
    assert (info.width, info.height, info.epsg, info.nodata) == (70, 100, 32756, 0.0)
    assert tuple(info.transform) == transform
    window, _ = cog.read_window("https://synthetic-city.invalid/x.tif", (0, 0, 70, 100), fetch, (info, src))
    assert np.array_equal(window, image)
    with pytest.raises(cog.CogError):
        fetch("https://synthetic-city.invalid/missing.tif", 0, 10)


def test_the_committed_network_fixture_is_the_synthetic_network_written_out():
    on_disk = json.loads(driver_module.SYNTHETIC_NETWORK_FILE.read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(driver_module.synthetic_network()))
    assert len(on_disk["stations"]) == 12 and len(on_disk["ways"]) == 4


def test_the_synthetic_scenes_are_manifest_shaped_and_their_box_holds_the_site():
    site, scenes = driver_module.sequence_scenes("synthetic-city", None)
    lons, lats = [p[0] for p in site["polygon"]], [p[1] for p in site["polygon"]]
    west, south, east, north = min(lons), min(lats), max(lons), max(lats)
    for scene in scenes:
        item = driver_module.stac_item(scene)
        box = item["bbox"]
        assert box[0] <= west <= east <= box[2] and box[1] <= south <= north <= box[3]
        assert set(item["assets"]) == set(rail_access.ASSETS_RAIL.values())
        assert all(a["href"].startswith(driver_module.SYNTHETIC_PREFIX) for a in item["assets"].values())


# --- the sequence through the real adapter -----------------------------------------------------------------


def test_the_synthetic_city_runs_through_the_real_adapter_with_every_decision_code(blobs):
    driver, specs = synthetic_driver(blobs)
    results = driver.run(specs)
    codes = [["first_observation"], ["below_floor"], ["artefact"], ["reported"]]
    assert [r["codes"] for r in results] == codes
    assert [r["changed"] for r in results] == [True, False, False, True]
    assert [r["network"] for r in results] == ["A", "A", "A", "B"]
    # The tick happens TICK_LAG after its scene, and that is the decision's `at`.
    ats = [driver_module.tick_time(s.scene["captured_at"]).isoformat() for s in specs]
    assert [r["at"] for r in results] == ats
    # The agent was asked twice (demo thresholds), each time with one look_again re-measurement.
    last = results[-1]["decisions"][0]
    assert last["triage"]["verdict"] == "real" and last["network_changed"] is True
    assert "station_count" in last["moved"] and last["reported"] is True
    assert [s["tool"] for s in last["triage"]["steps"]] == ["look_again"]
    assert last["triage"]["steps"][0]["result"]["settings"] == {"reach_m": 800}
    assert [m["kind"] for m in results[-1]["measurements"]] == ["tick", "look_again"]
    assert results[2]["decisions"][0]["triage"]["verdict"] == "artefact"
    # The ring the driver reports is the state's own.
    ring = driver_module.ring_of(driver.state, driver.site_id)
    assert ring == [entry for r in results for entry in r["decisions"]]
    # Every line printed is an evidence line production's parser takes, with the tick's ids bound
    # (the in-process worker's own line is bound to no tick, as the deployed worker's is not).
    events = []
    for r in results:
        assert r["printed"] == 0
        for line in r["lines"]:
            parsed = evidence.parse(line)
            assert parsed is not None
            if parsed["event"] == "vision_measure":
                assert "tick" not in parsed and parsed["site_id"] == "eval-synthetic"
            else:
                assert parsed["tick"] == f"eval-tick-{r['n']}" and parsed["topic_id"] == "eval-synthetic-city"
            events.append(parsed["event"])
    assert {"vision_site", "vision_measure", "vision_decision", "vision_triage"} <= set(events)
    assert events.count("vision_decision") == 4 and events.count("vision_triage") == 2
    # Every site id the worker saw starts with eval-.
    parsed = [evidence.parse(line) for line in results[0]["lines"]]
    assert all(e.get("site_id", "eval-").startswith("eval-") for e in parsed)
    # The agent's responses are kept in the recorded format, and the clock read in every tick.
    assert [len(r["responses"]) for r in results] == [0, 0, 1, 1]
    assert [len(t["turns"]) for t in results[-1]["responses"]] == [2]
    assert all(r["clock"] and set(r["clock"]) == {0.0} for r in results)


def test_the_network_object_is_the_seam_the_adapter_reads_and_b_is_a_line_on_the_desert(blobs):
    driver, specs = synthetic_driver(blobs)
    driver.run(specs[:3])
    stored = driver.store.load_network(driver.topic_id, driver.site_id)
    assert stored["site_key"] == rail_access.site_key(driver.site["polygon"])
    assert len(stored["stations"]) == 12
    record = driver.state["sites"][driver.site_id]
    assert record["network_key"] == f"vision/{driver.topic_id}/{driver.site_id}/network.json"
    assert "network" not in record  # the state keeps only what it names, never the network
    lon, lat = driver_module.largest_desert(driver.state, driver.site_id)
    result = driver.tick(driver.state, specs[3], n=4)
    stored = driver.store.load_network(driver.topic_id, driver.site_id)
    added = [s for s in stored["stations"] if s["id"].startswith("eval-line-")]
    assert len(added) == 5 and abs(added[2]["lon"] - lon) < 1e-5 and abs(added[2]["lat"] - lat) < 1e-5
    assert result["network_fetched_at"] > specs[0].network["fetched_at"]


def test_a_tick_from_one_state_repeats_exactly_and_leaves_that_state_alone(blobs):
    driver, specs = synthetic_driver(blobs)
    driver.run(specs[:3])
    before = copy.deepcopy(driver.state)
    first = driver.tick(driver.state, specs[3], n=4)
    second = driver.tick(driver.state, specs[3], n=4)
    assert driver.state == before
    assert first["decisions"] == second["decisions"]

    def adapter_lines(result):  # the worker's own line carries its timings, which vary
        return [line for line in result["lines"] if '"event": "vision_measure"' not in line]

    assert adapter_lines(first) == adapter_lines(second)


def test_look_again_can_be_answered_by_another_measure_than_the_ticks_own(blobs):
    asked = []

    def constructed(site, scene, *args, **kwargs):
        asked.append(kwargs.get("params"))
        return driver_module.scripted_measure(lambda s, sc, p: {
            "task": "rail_access", "coverage": 0.99, "served_share": 0.5, "desert_km2": 1.0,
            "built_up_km2": 6.3, "station_count": 12, "flags": [], "quality_flags": [],
        })(site, scene, *args, **kwargs)  # fmt: skip

    driver, specs = synthetic_driver(blobs, look_again_measure=constructed)
    results = driver.run(specs[:3])
    step = results[2]["decisions"][0]["triage"]["steps"][0]
    assert step["result"]["measured"]["served_share"] == 0.5 and asked == [{"reach_m": 800}]
    assert [m["kind"] for m in results[2]["measurements"]] == ["tick", "look_again"]


def test_triage_off_is_the_production_rule_alone(blobs):
    driver, specs = synthetic_driver(blobs, topic={"overrides": {"triage": False}})
    results = driver.run(specs)
    codes = [["first_observation"], ["below_floor"], ["reported"], ["reported"]]
    assert [r["codes"] for r in results] == codes
    assert all(not r["responses"] for r in results)


# --- the tick's time, the clocks, the environment ---------------------------------------------------------


def test_the_adapter_reads_the_wall_clock_only_where_the_driver_sets_it():
    source = (LAMBDAS / "common" / "adapters" / "rail_access.py").read_text(encoding="utf-8")
    # fetch_state and prefetch_networks (never on the replay's path): a third read would escape
    # frozen_now, and the replay's `at`s would stop being the tick's.
    assert source.count("datetime.now(") == 2
    when = datetime(2024, 6, 27, 6, 6, 11, tzinfo=UTC)
    original = rail_access.datetime
    with driver_module.frozen_now(when):
        assert rail_access.datetime.now(UTC) == when
        assert rail_access.datetime.fromisoformat("2024-01-01T00:00:00+00:00").year == 2024
    assert rail_access.datetime is original


def test_clocks_record_replay_and_say_when_a_replay_runs_past_its_recording():
    ticks = iter([100.0, 100.5, 102.25])
    clock = Clock(base=lambda: next(ticks))
    assert [clock(), clock(), clock()] == [0.0, 0.5, 2.25] and clock.readings == [0.0, 0.5, 2.25]
    replay = Clock.replay([0.0, 0.5])
    assert [replay(), replay()] == [0.0, 0.5] and not replay.overrun
    assert replay() == 0.5 and replay.overrun
    assert Clock.frozen()() == 0.0


def test_the_in_process_and_deployed_measures_set_the_workers_variable_only_for_the_call(monkeypatch):
    seen = {}

    def fake_measure(site, scene, backend="opencv", **kwargs):
        import os

        seen.update(arn=os.environ.get("VISION_WORKER_ARN"), client=kwargs.get("client"), site=site["id"])
        return "result"

    monkeypatch.delenv("VISION_WORKER_ARN", raising=False)
    monkeypatch.setattr(vision_client, "measure", fake_measure)
    deployed = driver_module.deployed_measure("arn:aws:lambda:us-west-2:123456789012:function:worker")
    assert deployed({"id": "eval-sydney", "polygon": []}, {"id": "s"}, read_timeout=58) == "result"
    assert seen["arn"].endswith(":function:worker") and seen["client"] is None
    import os

    assert "VISION_WORKER_ARN" not in os.environ
    assert driver_module.WORKER_ENV == vision_client._ENV_BY_BACKEND


# --- the agents ----------------------------------------------------------------------------------------


def brief(change: dict) -> list[dict]:
    text = "Measured change to check:\n" + json.dumps({"change": change})
    return [{"role": "user", "content": [{"text": text}]}]


def test_the_scripted_agent_looks_again_once_then_answers_by_its_rule():
    agent = ScriptedAgent()
    converse = agent.converse_for(3, "eval-sydney")
    tools = {"tools": [{"toolSpec": {"name": "look_again"}}]}
    first = converse(messages=brief({"station_count": 0}), toolConfig=tools)
    assert first["stopReason"] == "tool_use"
    assert first["output"]["message"]["content"][0]["toolUse"]["input"] == {"reach_m": 800}
    second = converse(messages=brief({"station_count": 0}), toolConfig=tools)
    assert json.loads(second["output"]["message"]["content"][0]["text"])["verdict"] == "artefact"
    assert len(converse.responses) == 2 and "ResponseMetadata" not in converse.responses[0]
    assert driver_module.scripted_verdict(brief({"station_count": 5}))[0] == "real"
    assert driver_module.scripted_verdict([])[0] == "artefact"


def test_a_recorded_response_calling_a_tool_the_profile_lost_is_refused_naming_the_step():
    look = {"toolUse": {"toolUseId": "a", "name": "look_again", "input": {}}}
    gone = {"toolUse": {"toolUseId": "b", "name": "street_view", "input": {}}}
    turns = [{"output": {"message": {"content": [look]}}, "stopReason": "tool_use"},
             {"output": {"message": {"content": [gone]}}, "stopReason": "tool_use"}]  # fmt: skip
    agent = RecordedAgent([{"tick": 2, "site_id": "eval-sydney", "turns": turns}], "model")
    converse = agent.converse_for(2, "eval-sydney")
    offered = {"tools": [{"toolSpec": {"name": "look_again"}}]}
    assert converse(toolConfig=offered)["stopReason"] == "tool_use"
    with pytest.raises(ReplayRefused, match=r"tick 2, site eval-sydney, step 2: .*'street_view'"):
        converse(toolConfig=offered)
    short = agent.converse_for(5, "eval-sydney")
    with pytest.raises(ReplayRefused, match="turn 1: the recording has no response"):
        short(toolConfig=offered)
    unused = agent.converse_for(2, "eval-sydney")
    unused(toolConfig=offered)
    unused.finish()
    assert "stopped after turn 1 of the recording's 2" in unused.refused


def test_a_refused_recording_stops_the_replay_out_of_the_adapter(blobs):
    look = {"toolUse": {"toolUseId": "a", "name": "street_view", "input": {}}}
    turns = [{"output": {"message": {"content": [look]}}, "stopReason": "tool_use"}]
    agent = RecordedAgent([{"tick": 3, "site_id": "eval-synthetic", "turns": turns}], "scripted")
    driver, specs = synthetic_driver(blobs, agent=agent)
    with pytest.raises(ReplayRefused, match="tick 3, site eval-synthetic, step 1"):
        driver.run(specs)


# --- the network B, the comparison ---------------------------------------------------------------------


def test_with_new_line_adds_a_line_of_stations_fetched_a_day_later():
    network = {"ways": [], "stations": [], "pois": [], "fetched_at": "2026-10-16T05:00:00+00:00",
               "counts": {"ways": 0, "stations": 0, "pois": 0, "points": 0}}  # fmt: skip
    out = driver_module.with_new_line(network, 151.0, -33.9, stations=5, spacing_m=600.0)
    assert network["stations"] == [] and len(out["stations"]) == 5 and len(out["ways"]) == 1
    assert out["fetched_at"] == "2026-10-17T05:00:00+00:00"
    assert out["counts"] == {"ways": 1, "stations": 5, "pois": 0, "points": 5}
    lons = [s["lon"] for s in out["stations"]]
    assert lons == sorted(lons) and abs(lons[2] - 151.0) < 1e-9
    assert re.fullmatch(r"eval-line-\d", out["stations"][0]["id"])
    assert driver_module.largest_desert({"sites": {}}, "x") is None


def test_compare_entry_holds_code_to_equality_and_measurements_to_the_tolerances(blobs):
    driver, specs = synthetic_driver(blobs)
    entry = driver.run(specs)[-1]["decisions"][0]
    assert driver_module.compare_entry(entry, copy.deepcopy(entry)) == []
    near = copy.deepcopy(entry)
    near["measure"]["served_share"] += 5e-5
    near["tick"], near["run"], near["measured_by"] = "other", "other", {"cpu": "elsewhere"}
    near["triage"]["elapsed_ms"], near["triage"]["cost_usd"] = 12345, 9.9
    near["triage"]["steps"][0]["ms"] = 777
    near["triage"]["steps"][0]["result"]["worker_request_id"] = "another"
    assert driver_module.compare_entry(entry, near) == []
    far = copy.deepcopy(entry)
    far["measure"]["served_share"] += 2e-4
    far["triage"]["reason"] = "something else"
    far["triage"]["steps"][0]["result"]["measured"]["station_count"] += 1
    found = driver_module.compare_entry(entry, far)
    assert any(d.startswith("measure.served_share") for d in found)
    assert any(d.startswith("triage.reason") for d in found)
    assert any("measured.station_count" in d for d in found)


def test_local_store_keys_stay_inside_its_directory(tmp_path):
    store = LocalStore(tmp_path)
    key = store.store_image("eval-t", "eval-s", "scene", b"\x89PNG")
    assert key == "vision/eval-t/eval-s/scene.png" and store.load_image(key) == b"\x89PNG"
    assert store.load_network("eval-t", "eval-s") is None
    with pytest.raises(ValueError):
        store.load_image("vision/../../etc/passwd")


def test_tick_specs_name_their_network_once_and_refuse_a_missing_b():
    site, scenes = driver_module.sequence_scenes("synthetic-city", None)
    specs = driver_module.tick_specs("synthetic-city", scenes, {"fetched_at": "x"}, {"fetched_at": "y"})
    assert [s.network for s in specs] == [{"fetched_at": "x"}, None, None, {"fetched_at": "y"}]
    with pytest.raises(ReplayRefused, match="tick 4 needs network B"):
        driver_module.tick_specs("synthetic-city", scenes, {"fetched_at": "x"}, None)
    assert isinstance(specs[0], TickSpec) and specs[0].news == []
    manifest = {"sites": {"sydney": {"polygon": [[0, 0], [1, 0], [1, 1]]}}, "scenes": []}
    with pytest.raises(KeyError, match="no scene named"):
        driver_module.sequence_scenes("sydney-winter", manifest)
