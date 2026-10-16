"""Tests for common/vision_decisions.py: the decision code, the ring, its lines and the trace.

The states here are built by hand, in the shape the adapters write; test_rail_access_adapter.py
and test_satellite_vision_adapter.py run the real fetch_state and material_diff over the ring.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from common import evidence
from common import vision_decisions as vd
from common.adapters import rail_access as ra
from common.adapters import satellite_vision as sv
from common.adapters.registry import ADAPTER_REGISTRY
from ops_mcp import redact

AT = "2026-10-16T03:00:00+00:00"
TICK = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
RAIL_VOCABULARY = ra.RailAccessAdapter.decision_vocabulary()
SHIPS_VOCABULARY = sv.SatelliteVisionAdapter.decision_vocabulary()


def verdict(**fields):
    base = {"site_id": "sydney", "scene_id": "S2", "captured_at": "2026-10-14T00:00:00Z", "coverage": 0.93,
            "served_share": 0.70, "desert_km2": 118.0, "station_count": 180, "baseline": None,
            "baseline_scenes": 0, "delta": None, "relative": None, "material": False, "reason": "",
            "moved": [], "network_changed": None}  # fmt: skip
    return {**base, **fields}


def material(**fields):
    """A verdict `_judge` found material (two measures moved), with `fields` over it."""
    base = {"baseline": {"served_share": 0.73, "desert_km2": 111.65, "station_count": 178},
            "baseline_scenes": 2, "delta": {"served_share": -0.03, "desert_km2": 6.35, "station_count": 2},
            "relative": {"desert_km2": 0.057}, "material": True,
            "reason": "served share moved, station count changed",
            "moved": ["served_share", "station_count"], "network_changed": False}  # fmt: skip
    return verdict(**{**base, **fields})


def state(verdicts=(), *, triage=None, decisions=None, history=None):
    sites = {}
    for v in verdicts:
        entry = {"scene_id": v["scene_id"], "captured_at": v["captured_at"], "coverage": v["coverage"],
                 "served_share": v.get("served_share"), "desert_km2": v.get("desert_km2"),
                 "built_up_km2": 412.3, "station_count": v.get("station_count"), "flag_count": 4,
                 "backend": "opencv", "build_sha256": "ab" * 32}  # fmt: skip
        record = {"name": "Sydney", "history": [*(history or []), entry]}
        if decisions is not None:
            record["decisions"] = decisions
        sites[v["site_id"]] = record
    measured = [{"site_id": v["site_id"], "scene_id": v["scene_id"]} for v in verdicts]
    built = {"fetched_at": AT, "thresholds": {key: ra.DEFAULTS[key] for key in ra.THRESHOLD_KEYS},
             "sites": sites, "measured": measured}  # fmt: skip
    if triage is not None:
        built["triage"] = triage
    return built


def outcome(verdict_value="real", stop="answered", **more):
    return {
        "verdict": verdict_value, "reason": "The new line is on the map and in the news.", "tool_calls": [],
        "model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0", "input_tokens": 4200, "output_tokens": 310,
        "model_calls": 3, "stop": stop, "steps": [
            {"tool": "look_again", "ok": True, "ms": 29800,
             "result": {"settings": {"reach_m": 800}, "measured": {"coverage": 0.93, "served_share": 0.6411},
                        "quality_flags": [], "worker_request_id": "req-la", "build": "ab" * 6,
                        "worker_ms": 29700}},
            {"tool": "web_context", "ok": True, "ms": 1400, "result": {"results": 3, "dated": 2}},
        ], "steps_total": 2, "budget_refusals": 0, "elapsed_ms": 38100, "deadline_left_s": 12.5,
        "figure": True, "cost_usd": 0.00575, "hosts": ["news.example"], **more,
    }  # fmt: skip


def record(verdicts, *, first_clear=None, triage=None, decisions=None, visits=None, history=None):
    built = state(verdicts, triage=triage, decisions=decisions, history=history)
    moved = vd.moved_by_site(verdicts)
    appended = vd.record(built, verdicts, moved=moved, first_clear=first_clear, floor=0.6,
                         visits=visits or {}, vocabulary=RAIL_VOCABULARY)  # fmt: skip
    return built, appended


def lines(capsys, event):
    out = [evidence.parse(line) for line in capsys.readouterr().out.splitlines()]
    return [line for line in out if line and line["event"] == event]


# --- the code ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "first_clear", "expected"),
    [
        (verdict(coverage=0.93), {"sydney"}, ("first_observation", True)),
        (verdict(coverage=0.3), {"sydney"}, ("below_floor", False)),  # a cloudy first scene is not first
        (verdict(coverage=0.3), None, ("below_floor", False)),
        (verdict(coverage=0.93, baseline=None), None, ("baseline_building", False)),
        (material(material=False, reason="within the usual range", moved=[]), None, ("within_range", False)),
        (material(triage={"verdict": "artefact", "reason": "haze"}, material=False), None,
         ("artefact", False)),
        (material(triage={"verdict": "real", "reason": "a new line"}), None, ("reported", True)),
        (material(), None, ("reported", True)),  # triage off and material
        (verdict(coverage=0.93, baseline=0, baseline_scenes=2, material=False), None,
         ("within_range", False)),
    ],
)
def test_the_code_rule_reads_only_what_assess_and_triage_set(given, first_clear, expected):
    assert vd.decision_code(given, floor=0.6, first_clear=first_clear) == expected
    assert expected[0] in vd.CODES


def test_a_sites_code_matches_the_status_words_the_assistant_already_speaks():
    from common import vision_sites

    assert set(vd.CODES) == set(vision_sites.VERDICT_WORDS) | {"first_observation"}
    assert set(vd.TRACE_WORDS["codes"]) == set(vd.CODES)


def test_moved_is_read_from_the_verdicts_as_the_judge_wrote_it():
    verdicts = [material(), verdict(site_id="melbourne"), {"no": "site"},
                material(site_id="x", moved=["a", 3])]  # fmt: skip
    expected = {"sydney": ["served_share", "station_count"], "melbourne": [], "x": ["a"]}
    assert vd.moved_by_site(verdicts) == expected


# --- who measured ----------------------------------------------------------------------------------


class Result:
    def __init__(self, reply, request_id=None):
        self.reply, self.request_id = reply, request_id


def test_measured_by_and_failed_by_keep_only_code_derived_fields():
    reply = {"backend": "opencv", "build": {"build_sha256": "ab" * 32, "cpu": "Neoverse V1 (Graviton3)"}}
    who = vd.measured_by(Result(reply, "req-1"), 31.2)
    assert who == {"backend": "opencv", "build": "ab" * 6, "cpu": "Neoverse V1 (Graviton3)",
                   "worker_request_id": "req-1", "worker_ms": 31200, "code": None}  # fmt: skip
    assert vd.measured_by(Result({"metrics": {}}), None)["build"] is None
    assert vd.measured_by(Result({"build": {"build_sha256": "not hex"}}), 0.0)["build"] is None

    class Refused(Exception):
        code, request_id, detail = "site_outside_scene", "req-2", "the worker's detail"

    failed = vd.failed_by(Refused(), 2.5, backend="opencv")
    assert failed == {"backend": "opencv", "build": None, "cpu": None, "worker_request_id": "req-2",
                      "worker_ms": 2500, "code": "site_outside_scene"}  # fmt: skip
    assert vd.failed_by(RuntimeError("x"), 1.0, request_id="req-3")["worker_request_id"] == "req-3"
    assert "detail" not in json.dumps(failed)


# --- the ring ---------------------------------------------------------------------------------------


def test_record_writes_one_entry_per_verdict_with_the_numbers_the_decision_was_made_from(capsys):
    given = material(triage={"verdict": "real", "reason": "a new line"})
    visits = {"sydney": {"backend": "opencv", "build": "ab" * 6, "cpu": "Neoverse V1 (Graviton3)",
                         "worker_request_id": "req-1", "worker_ms": 31200, "code": None}}  # fmt: skip
    with evidence.bind(tick=TICK, run="r" * 12, topic_id="rail-sydney"):
        built, appended = record([given], triage={"sydney": outcome()}, visits=visits)
    (entry,) = appended
    assert built["sites"]["sydney"]["decisions"] == [entry]
    assert entry["v"] == vd.DECISION_VERSION and entry["at"] == AT
    assert (entry["tick"], entry["run"]) == (TICK, "r" * 12)
    assert entry["scene_id"] == "S2" and entry["captured_at"] == "2026-10-14T00:00:00Z"
    assert entry["measure"] == {"served_share": 0.70, "desert_km2": 118.0, "built_up_km2": 412.3,
                                "station_count": 180, "flag_count": 4}  # fmt: skip
    assert entry["baseline"] == given["baseline"] and entry["delta"] == given["delta"]
    assert entry["relative"] == {"desert_km2": 0.057} and entry["baseline_scenes"] == 2
    assert entry["thresholds"] == {"coverage_floor": 0.6, "min_baseline": 1, "served_share_threshold": 0.02,
                                   "desert_relative_threshold": 0.1}  # fmt: skip
    assert (entry["code"], entry["reported"]) == ("reported", True)
    assert entry["moved"] == ["served_share", "station_count"] and entry["network_changed"] is False
    assert entry["measured_by"] == {"backend": "opencv", "build": "ab" * 6, "cpu": "Neoverse V1 (Graviton3)",
                                    "worker_request_id": "req-1", "worker_ms": 31200}  # fmt: skip
    triage = entry["triage"]
    assert (triage["verdict"], triage["stop"]) == ("real", "answered")
    assert triage["model_id"] == outcome()["model_id"]
    assert triage["steps"] == outcome()["steps"] and triage["steps_total"] == 2
    assert triage["hosts"] == ["news.example"] and triage["reason"] == outcome()["reason"]
    assert triage["cost_usd"] == 0.00575 and triage["figure"] is True
    # The line: the same numbers, never the reason or the hosts.
    (line,) = lines(capsys, "vision_decision")
    assert line["code"] == "reported" and line["moved"] == ["served_share", "station_count"]
    assert line["triaged"] is True and line["verdict"] == "real" and line["stop"] == "answered"
    assert line["thresholds"] == entry["thresholds"] and line["build"] == "ab" * 6
    assert line["worker_request_id"] == "req-1" and line["tick"] == TICK
    text = json.dumps(line)
    assert "news.example" not in text and "new line" not in text


def test_a_ships_baseline_is_keyed_by_the_vocabularys_first_measure():
    given = {"site_id": "botany", "scene_id": "S3", "captured_at": "2026-09-13", "coverage": 0.92,
             "count": 30, "baseline": 11.5, "baseline_scenes": 2, "delta": 18.5, "relative": 1.609,
             "material": True, "reason": "changed against the baseline", "moved": ["count"]}  # fmt: skip
    thresholds = {"coverage_floor": 0.7, "min_baseline": 2, "relative_threshold": 0.35,
                  "absolute_threshold": 5}  # fmt: skip
    newest = {"scene_id": "S3", "count": 30, "coverage": 0.92, "density_per_km2": 2.5,
              "clear_water_km2": 12.0}  # fmt: skip
    built = {"fetched_at": AT, "thresholds": thresholds, "sites": {"botany": {"history": [newest]}}}
    (entry,) = vd.record(built, [given], moved={"botany": ["count"]}, first_clear=None, floor=0.7, visits={},
                         vocabulary=SHIPS_VOCABULARY)  # fmt: skip
    assert entry["measure"] == {"count": 30, "density_per_km2": 2.5, "clear_water_km2": 12.0}
    assert entry["baseline"] == {"count": 11.5} and entry["delta"] == {"count": 18.5}
    assert entry["relative"] == {"count": 1.609} and entry["network_changed"] is None
    assert entry["code"] == "reported" and entry["triage"] is None
    assert entry["measured_by"]["build"] is None  # no visit, and the entry has no build


def test_the_ring_keeps_the_last_twelve_and_never_the_same_scene_and_tick_twice(capsys):
    old = [{"v": 1, "at": f"2026-10-{d:02d}T00:00:00+00:00", "scene_id": f"S{d}", "code": "within_range"}
           for d in range(1, 13)]  # fmt: skip
    built, appended = record([verdict()], decisions=list(old))
    ring = built["sites"]["sydney"]["decisions"]
    assert len(ring) == vd.DECISIONS_KEPT == 12 and len(appended) == 1
    assert [e["scene_id"] for e in ring] == [f"S{d}" for d in range(2, 13)] + ["S2"]
    # material_diff may run again on the same state (a review, a test): no second entry, no line.
    capsys.readouterr()
    moved = vd.moved_by_site([verdict()])
    again = vd.record(built, [verdict()], moved=moved, first_clear=None, floor=0.6, visits={},
                      vocabulary=RAIL_VOCABULARY)  # fmt: skip
    assert again == [] and len(built["sites"]["sydney"]["decisions"]) == 12
    assert lines(capsys, "vision_decision") == []


def test_a_state_written_before_the_ring_starts_one_and_an_unknown_version_is_kept():
    built = state([verdict()])
    assert "decisions" not in built["sites"]["sydney"]
    moved = vd.moved_by_site([verdict()])
    vd.record(built, [verdict()], moved=moved, first_clear=None, floor=0.6, visits={},
              vocabulary=RAIL_VOCABULARY)  # fmt: skip
    assert [e["v"] for e in built["sites"]["sydney"]["decisions"]] == [1]
    future = {"v": 9, "at": "2026-10-01T00:00:00+00:00", "scene_id": "S1", "something": "else"}
    built, _ = record([verdict()], decisions=[future, "not even a dict"])
    ring = built["sites"]["sydney"]["decisions"]
    assert ring[0] == future and ring[1] == "not even a dict" and ring[2]["v"] == 1
    # A record whose decisions are not a list starts over.
    built, _ = record([verdict()], decisions="corrupt")
    assert [e["v"] for e in built["sites"]["sydney"]["decisions"]] == [1]


def test_a_verdict_for_a_site_not_in_the_state_or_without_a_site_id_is_skipped(capsys):
    built = state([verdict()])
    verdicts = [verdict(site_id="elsewhere"), {"scene_id": "S2"}, "x"]
    assert vd.record(built, verdicts, moved={}, first_clear=None, floor=0.6, visits={}, vocabulary={}) == []
    assert lines(capsys, "vision_decision") == []


def test_the_first_observation_and_the_cloudy_site_beside_it_get_their_own_entries(capsys):
    clear, cloudy = verdict(site_id="sydney"), verdict(site_id="melbourne", scene_id="S9", coverage=0.3)
    built = state([clear, cloudy])
    vd.record(built, [clear, cloudy], moved={}, first_clear={"sydney"}, floor=0.6, visits={},
              vocabulary=RAIL_VOCABULARY)  # fmt: skip
    assert built["sites"]["sydney"]["decisions"][0]["code"] == "first_observation"
    assert built["sites"]["sydney"]["decisions"][0]["reported"] is True
    assert built["sites"]["melbourne"]["decisions"][0]["code"] == "below_floor"
    assert [line["code"] for line in lines(capsys, "vision_decision")] == ["first_observation", "below_floor"]


# --- the lines the adapters print -------------------------------------------------------------------


class Budget:
    def __init__(self, left):
        self.left = left

    def remaining(self):
        return self.left


def test_the_site_line_carries_the_outcome_the_code_and_the_worker_call(capsys):
    visit = {"worker_request_id": "req-1", "worker_ms": 31200}
    vd.site_line("sydney", "measured", scene_id="S2", budget=Budget(28.4), visit=visit)
    vd.site_line("melbourne", "deferred", code="time_budget", budget=Budget(3.0))
    vd.site_line("boston", "not_reached")
    found = lines(capsys, "vision_site")
    assert found[0]["outcome"] == "measured" and found[0]["worker_request_id"] == "req-1"
    assert found[0]["worker_ms"] == 31200 and found[0]["budget_left_s"] == 28.4
    assert found[0]["scene_id"] == "S2"
    assert found[1]["outcome"] == "deferred" and found[1]["code"] == "time_budget"
    assert found[2] == {"evidence": evidence.VERSION, "event": "vision_site", "site_id": "boston",
                        "outcome": "not_reached"}  # fmt: skip


def test_the_triage_line_carries_the_steps_the_tools_and_the_reasons_hash_never_its_text(capsys):
    given = outcome()
    vd.triage_line("sydney", "S2", given)
    (line,) = lines(capsys, "vision_triage")
    assert line["tools"] == ["look_again", "web_context"] and line["tools_total"] == 2
    assert line["look_again"] == 1 and line["steps_total"] == 2 and line["budget_refusals"] == 0
    assert line["worker_request_ids"] == ["req-la"] and line["steps"][0]["result"]["build"] == "ab" * 6
    assert line["reason_sha256"] == vd.reason_prefix(given["reason"]) and len(line["reason_sha256"]) == 12
    assert line["cost_usd"] == 0.00575 and line["figure"] is True and line["deadline_left_s"] == 12.5
    text = json.dumps(line)
    assert "news.example" not in text and "new line" not in text and "hosts" not in text
    # A fake agent's outcome, or the no-model one, has few of the keys: still one line.
    vd.triage_line("sydney", "S2", {"verdict": "artefact", "reason": "no model to triage with (KeyError)",
                                    "tool_calls": [], "stop": "no_model"})  # fmt: skip
    (line,) = lines(capsys, "vision_triage")
    assert line["stop"] == "no_model" and line["tools"] == [] and line["steps"] == []
    assert vd.reason_prefix("") is None and vd.reason_prefix(None) is None


def test_a_model_that_asked_for_many_tools_still_leaves_its_line_cut_and_counted(capsys):
    """Five allowed calls and seven refusals (the budget) in one turn: the trail keeps all twelve,
    the line keeps the first MAX_STEPS with the totals beside them, says what it cut, and is
    still written."""
    allowed = [{"tool": "site_history", "ok": True, "ms": 3, "result": {"scenes": 4}}] * 5
    refused = [{"tool": "look_again", "ok": False, "ms": 0, "result": {"code": "budget"}}] * 7
    given = outcome(steps=allowed + refused, steps_total=12, budget_refusals=7)
    vd.triage_line("sydney", "S2", given)
    (line,) = lines(capsys, "vision_triage")
    assert len(line["steps"]) == evidence.MAX_STEPS == 8 and line["steps_total"] == 12
    assert line["budget_refusals"] == 7 and line["tools_total"] == 12 and line["look_again"] == 7
    assert len(line["tools"]) == evidence.MAX_CODES == 12 and line["cut"] == ["steps"]  # twelve tools fit
    assert [step["result"] for step in line["steps"][5:]] == [{"code": "budget"}] * 3
    # One refusal more and the tools are cut too, named in order.
    thirteen = outcome(steps=allowed + refused + refused[:1], steps_total=13, budget_refusals=8)
    vd.triage_line("sydney", "S2", thirteen)
    (line,) = lines(capsys, "vision_triage")
    assert len(line["tools"]) == 12 and line["tools_total"] == 13 and line["cut"] == ["tools", "steps"]
    # The ring keeps the same first eight, with the total.
    built, (entry,) = record([material(triage={"verdict": "real", "reason": "x"})], triage={"sydney": given})
    assert len(entry["triage"]["steps"]) == 8 and entry["triage"]["steps_total"] == 12


# --- the trace --------------------------------------------------------------------------------------


def full_entry(**more):
    given = material(triage={"verdict": "real", "reason": "a new line"})
    visits = {"sydney": {"backend": "opencv", "build": "ab" * 6, "cpu": "Neoverse V1 (Graviton3)",
                         "worker_request_id": "req-1", "worker_ms": 31200, "code": None}}  # fmt: skip
    _, (entry,) = record([given], triage={"sydney": outcome(**more)}, visits=visits)
    return entry


def by_step(rows):
    return {(row["step"], row["n"]): row for row in rows}


def test_the_trace_has_the_six_steps_with_facts_only():
    entry = full_entry()
    attempt = {"stored": True, "finding": AT}
    article = {"status": "pending_moderation", "created_at": "2026-10-16T09:00:00+00:00"}
    rows = vd.trace(entry, vocabulary=RAIL_VOCABULARY, attempt=attempt, article=article)
    steps = ["measured", "compared", "tool", "tool", "verdict", "action", "person"]
    assert [row["step"] for row in rows] == steps
    keys = {"step", "n", "code", "numbers", "ids", "effect", "sources", "time"}
    assert all(set(row) == keys for row in rows)
    steps = by_step(rows)
    measured = steps[("measured", 0)]
    assert measured["numbers"]["served_share"] == 0.70 and measured["numbers"]["worker_ms"] == 31200
    assert measured["ids"] == {"scene_id": "S2", "backend": "opencv", "build": "ab" * 6,
                               "cpu": "Neoverse V1 (Graviton3)", "worker_request_id": "req-1"}  # fmt: skip
    compared = steps[("compared", 0)]
    assert compared["code"] == "reported" and compared["effect"] == "sent_to_agent"
    numbers = compared["numbers"]
    assert numbers["baseline_served_share"] == 0.73 and numbers["delta_station_count"] == 2
    assert numbers["threshold_served_share_threshold"] == 0.02 and numbers["baseline_scenes"] == 2
    # The same network copy on both sides: the served share's move came from the imagery alone.
    assert compared["sources"] == {"served_share": "imagery", "station_count": "network"}
    look = steps[("tool", 1)]
    assert look["code"] == "look_again" and look["effect"] == "re_measured"
    assert look["numbers"]["setting_reach_m"] == 800 and look["numbers"]["measured_served_share"] == 0.6411
    assert look["numbers"]["at_topic_settings_served_share"] == 0.70  # the same scene, the topic's settings
    assert look["ids"] == {"worker_request_id": "req-la", "build": "ab" * 6}
    assert look["numbers"]["ms"] == 29800
    assert not any(key.startswith("baseline") for key in look["numbers"])  # never the baseline
    news = steps[("tool", 2)]
    assert news["code"] == "web_context" and news["numbers"] == {"ms": 1400, "results": 3, "dated": 2}
    assert news["effect"] is None and "news.example" not in json.dumps(rows)
    verdict_row = steps[("verdict", 0)]
    assert verdict_row["code"] == "real" and verdict_row["ids"]["stop"] == "answered"
    assert verdict_row["numbers"]["cost_aud"] == pytest.approx(0.008625)
    assert verdict_row["numbers"]["input_tokens"] == 4200
    assert steps[("action", 0)]["code"] == "finding_written" and steps[("action", 0)]["time"] == AT
    person = steps[("person", 0)]
    assert person["code"] == "held" and person["numbers"] == {"hours_after_finding": 6.0}
    assert "a new line" not in json.dumps(rows)


def test_no_row_compares_a_re_measurement_with_the_baseline():
    """Judge 4: look_again re-measures only the newest scene at other settings, and the adapter
    refuses to compare scenes measured with different parameters, so the trace states what was
    re-measured beside the same scene's numbers and offers no "fell back" or "held" effect."""
    assert set(vd.EFFECTS) == {"kept_out_cloud", "kept_out_baseline", "kept_out_range", "sent_to_agent",
                               "reported_first", "reported_untriaged", "re_measured"}  # fmt: skip
    assert set(vd.TRACE_WORDS["effects"]) == set(vd.EFFECTS)
    for words in vd.TRACE_WORDS.values():
        for phrase in words.values():
            assert "fell" not in phrase and "back in" not in phrase
            assert "held" not in phrase.split("held for")[0]
    rows = vd.trace(full_entry(), vocabulary=RAIL_VOCABULARY, attempt={"stored": True, "finding": AT})
    for row in rows:
        if row["step"] == "tool":
            assert row["effect"] in (None, "re_measured")
            assert not {k for k in row["numbers"] if "baseline" in k or "delta" in k}


@pytest.mark.parametrize(
    ("code", "triaged", "effect"),
    [
        ("first_observation", False, "reported_first"),
        ("below_floor", False, "kept_out_cloud"),
        ("baseline_building", False, "kept_out_baseline"),
        ("within_range", False, "kept_out_range"),
        ("reported", True, "sent_to_agent"),
        ("artefact", True, "sent_to_agent"),
        ("reported", False, "reported_untriaged"),
    ],
)
def test_every_decision_code_has_its_effect(code, triaged, effect):
    entry = full_entry()
    entry["code"] = code
    entry["triage"] = entry["triage"] if triaged else None
    rows = vd.trace(entry, vocabulary=RAIL_VOCABULARY)
    assert by_step(rows)[("compared", 0)]["effect"] == effect
    assert ("verdict", 0) in by_step(rows) if triaged else ("verdict", 0) not in by_step(rows)


def test_the_sources_follow_the_vocabulary_and_whether_the_network_changed():
    entry = full_entry()
    entry["moved"] = ["served_share", "desert_km2", "station_count", "made_up"]
    cases = ((False, "imagery"), (True, "imagery_and_network"), (None, "imagery_and_network"))
    for changed, expected in cases:
        entry["network_changed"] = changed
        sources = by_step(vd.trace(entry, vocabulary=RAIL_VOCABULARY))[("compared", 0)]["sources"]
        assert sources == {"served_share": expected, "desert_km2": expected, "station_count": "network"}
    ships = by_step(vd.trace({**entry, "moved": ["count"]}, vocabulary=SHIPS_VOCABULARY))[("compared", 0)]
    assert ships["sources"] == {"count": "imagery"}


@pytest.mark.parametrize(
    ("stop", "verdict_value", "expected"),
    [
        ("answered", "real", "real"),
        ("answered", "artefact", "artefact"),
        ("malformed", "artefact", "failed_closed"),
        ("deadline", "artefact", "failed_closed"),
        ("bedrock_error", "artefact", "failed_closed"),
        ("turn_limit", "artefact", "failed_closed"),
        ("no_profile", "artefact", "failed_closed"),
        ("no_model", "artefact", "failed_closed"),
    ],
)
def test_the_verdict_row_says_failed_closed_unless_the_model_answered(stop, verdict_value, expected):
    entry = full_entry(stop=stop, verdict=verdict_value)
    row = by_step(vd.trace(entry, vocabulary=RAIL_VOCABULARY))[("verdict", 0)]
    assert row["code"] == expected and row["ids"]["stop"] == stop
    assert expected in vd.TRACE_WORDS["verdicts"]


@pytest.mark.parametrize(
    ("reported", "attempt", "expected"),
    [
        (True, {"stored": True, "finding": AT}, "finding_written"),
        (False, {"stored": True}, "no_finding"),
        (True, None, "not_kept"),
        (True, {"stored": False}, "not_kept"),
        (False, {"stored": False}, "not_kept"),
        (True, {"stored": True, "finding": "2026-10-16T02:00:00+00:00"}, "finding_lost"),
        (True, {"stored": True}, "finding_lost"),
        (True, {"stored": True, "replay": True}, "finding_would_be_written"),
    ],
)
def test_the_action_row_tells_kept_reported_and_written_apart(reported, attempt, expected):
    entry = full_entry()
    entry["reported"] = reported
    rows = by_step(vd.trace(entry, vocabulary=RAIL_VOCABULARY, attempt=attempt))
    assert rows[("action", 0)]["code"] == expected and expected in vd.ACTIONS


def test_the_person_row_comes_from_the_decision_the_article_or_the_findings_age():
    entry = full_entry()
    kept = {"stored": True, "finding": AT}

    def person(**kwargs):
        rows = by_step(vd.trace(entry, vocabulary=RAIL_VOCABULARY, attempt=kept, **kwargs))
        return rows.get(("person", 0))

    later = "2026-10-17T03:00:00+00:00"
    assert person(person={"decision": "approved", "at": later})["code"] == "approved"
    assert person(person={"decision": "approved", "at": later})["numbers"] == {"hours_after_finding": 24.0}
    assert person(person={"decision": "rejected"})["code"] == "rejected"
    assert person(person={"decision": "force_published"})["code"] == "approved"
    assert person(person={"decision": "unpublished"})["code"] == "unpublished"
    assert person(person={"decision": "sent_back"})["code"] == "held"
    assert person(article={"status": "pending_moderation"})["code"] == "held"
    assert person(article={"status": "published", "published_by": "humans"})["code"] == "approved"
    unreviewed = {"status": "published", "published_by": "pipeline"}
    assert person(article=unreviewed)["code"] == "published_unreviewed"
    assert person(article={"status": "rejected"})["code"] == "rejected"
    assert person()["code"] == "no_article_yet"
    expired = datetime.fromisoformat(AT) + timedelta(days=15)
    assert person(now=expired)["code"] == "finding_expired"
    assert person(now=datetime.fromisoformat(AT) + timedelta(days=2))["code"] == "no_article_yet"
    # No Finding written, nothing asked about: no person row.
    entry["reported"] = False
    assert person() is None and person(article={"status": "rejected"})["code"] == "rejected"
    assert set(vd.PERSONS) == set(vd.TRACE_WORDS["persons"])


def test_the_trace_reads_an_entry_from_before_with_missing_keys():
    sparse = {"v": 1, "at": AT, "scene_id": "S1", "code": "within_range"}
    rows = vd.trace(sparse, vocabulary={})
    assert [row["step"] for row in rows] == ["measured", "compared", "action"]
    assert by_step(rows)[("action", 0)]["code"] == "not_kept"
    assert by_step(rows)[("compared", 0)]["numbers"] == {}


# --- the words --------------------------------------------------------------------------------------


@pytest.mark.parametrize("adapter_cls", sorted(ADAPTER_REGISTRY.values(), key=lambda c: c.__name__))
def test_every_adapters_vocabulary_is_plain_words_in_the_declared_shape(adapter_cls):
    vocabulary = adapter_cls.decision_vocabulary()
    if not vocabulary:
        return  # an adapter that keeps no ring
    assert set(vocabulary) == {"measures", "moved", "sources", "settings"}
    for code, item in vocabulary["measures"].items():
        assert evidence.check("code", code) and set(item) == {"label", "unit", "scale"}
        assert item["scale"] in (100, 1)
    for code, item in vocabulary["settings"].items():
        assert evidence.check("code", code) and set(item) == {"label", "unit"}
    assert set(vocabulary["moved"]) == set(vocabulary["sources"]) <= set(vocabulary["measures"])
    assert set(vocabulary["sources"].values()) <= set(vd.SOURCES)
    for word in vd.vocabulary_words(vocabulary):
        assert vd.WORD.match(word), word
        assert not redact.looks_like_instructions(word)


def test_the_vision_adapters_declare_a_vocabulary_and_the_others_none():
    from common import vision_contract as contract

    assert RAIL_VOCABULARY and SHIPS_VOCABULARY
    assert set(RAIL_VOCABULARY["settings"]) == set(contract.RAIL_PARAM_LIMITS)
    assert set(SHIPS_VOCABULARY["settings"]) == set(contract.PARAM_LIMITS)
    assert RAIL_VOCABULARY["sources"] == {
        "served_share": "imagery_and_network", "desert_km2": "imagery_and_network",
        "station_count": "network",
    }  # fmt: skip
    assert SHIPS_VOCABULARY["sources"] == {"count": "imagery"}
    for key, cls in ADAPTER_REGISTRY.items():
        if key not in ("rail_access", "satellite_vision"):
            assert cls.decision_vocabulary() == {}
    assert "large vessels" not in json.dumps(SHIPS_VOCABULARY)  # never an operator's object_noun


def test_the_trace_words_are_plain_and_cover_every_code():
    for group, words in vd.TRACE_WORDS.items():
        for code, phrase in words.items():
            assert evidence.check("code", code), (group, code)
            assert vd.WORD.match(phrase), phrase
            assert not redact.looks_like_instructions(phrase)
    assert set(vd.TRACE_WORDS["actions"]) == set(vd.ACTIONS)
    assert set(vd.TRACE_WORDS["sources"]) == set(vd.SOURCES)
    assert set(vd.TRACE_WORDS["steps"]) == {"measured", "compared", "tool", "verdict", "action", "person"}


def test_a_full_ring_at_the_caps_stays_small():
    """Twelve entries with eight look_again steps (the largest kind), five hosts and a
    300-character reason each, for three sites (MAX_RAIL_SITES): about 3.4 KB an entry, some
    125 KB in all, against a rail state of about 10 MB."""
    steps = [outcome()["steps"][0]] * evidence.MAX_STEPS
    full = outcome(steps=steps, steps_total=8, hosts=[f"news{n}.example" for n in range(5)], reason="r" * 300)
    built = state([material(triage={"verdict": "real", "reason": "x"}, site_id=s) for s in ("a", "b", "c")],
                  triage={s: full for s in ("a", "b", "c")})  # fmt: skip
    for n in range(12):
        built["fetched_at"] = f"2026-10-{n + 1:02d}T00:00:00+00:00"
        verdicts = [material(triage={"verdict": "real", "reason": "x"}, site_id=s, scene_id=f"S{n}")
                    for s in ("a", "b", "c")]  # fmt: skip
        vd.record(built, verdicts, moved=vd.moved_by_site(verdicts), first_clear=None, floor=0.6, visits={},
                  vocabulary=RAIL_VOCABULARY)  # fmt: skip
    rings = {s: built["sites"][s]["decisions"] for s in ("a", "b", "c")}
    assert all(len(ring) == 12 for ring in rings.values())
    size = len(json.dumps(rings))
    assert 60_000 < size < 150_000, size
    assert json.loads(json.dumps(built))["sites"]["a"]["decisions"] == rings["a"]  # JSON-plain, as stored
