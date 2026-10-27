"""Tests for common/evidence.py: the evidence line's kinds, its shape, its bounds, and its parser.

Every line is checked the way a reader would read it: through `evidence.parse`, never by eye.
"""

from __future__ import annotations

import json
import re

import pytest

from common import evidence
from ops_mcp import redact

TICK = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
BUILD = "ab" * 6
CPU = "Neoverse V1 (Graviton3)"
MODEL = "au.anthropic.claude-haiku-4-5-20251001-v1:0"

# One realistic sample of every event: what its emitter writes on a good day.
SAMPLES = {
    "research_tick": {
        "adapter": "rail_access", "outcome": "material_change", "stored": True, "decisions": 1,
        "triages": 1, "finding": "2026-10-16T03:00:00+00:00", "force": False, "ms": 41230,
    },
    "vision_site": {
        "site_id": "sydney", "outcome": "measured", "scene_id": "S2A_56HLH_20261014_0_L2A",
        "worker_request_id": TICK, "worker_ms": 31200, "budget_left_s": 28.4,
    },
    "vision_decision": {
        "site_id": "sydney", "scene_id": "S2A_56HLH_20261014_0_L2A", "at": "2026-10-16T03:00:00+00:00",
        "captured_at": "2026-10-14T00:06:21Z", "coverage": 0.93,
        "measure": {"served_share": 0.7012, "desert_km2": 118.3, "station_count": 180, "flag_count": 4},
        "baseline": {"served_share": 0.73, "desert_km2": 111.65, "station_count": 178},
        "delta": {"served_share": -0.0288, "desert_km2": 6.65, "station_count": 2},
        "relative": {"desert_km2": 0.06}, "baseline_scenes": 2,
        "thresholds": {"coverage_floor": 0.6, "min_baseline": 1, "served_share_threshold": 0.02,
                       "desert_relative_threshold": 0.1},
        "code": "reported", "moved": ["served_share", "station_count"], "network_changed": False,
        "triaged": True, "verdict": "real", "stop": "answered", "reported": True, "backend": "opencv",
        "build": BUILD, "cpu": CPU, "worker_request_id": TICK,
    },
    "vision_triage": {
        "site_id": "sydney", "scene_id": "S2A_56HLH_20261014_0_L2A", "verdict": "real", "stop": "answered",
        "model_id": MODEL, "model_calls": 3, "input_tokens": 4200, "output_tokens": 310, "cost_usd": 0.00575,
        "elapsed_ms": 38100, "deadline_left_s": 12.5, "figure": True, "tools": ["look_again", "web_context"],
        "tools_total": 2, "look_again": 1, "budget_refusals": 0,
        "steps": [
            {"tool": "look_again", "ok": True, "ms": 29800,
             "result": {"settings": {"reach_m": 800}, "measured": {"coverage": 0.93, "served_share": 0.6411},
                        "quality_flags": [], "worker_request_id": TICK, "build": BUILD, "worker_ms": 29700}},
            {"tool": "web_context", "ok": True, "ms": 1400, "result": {"results": 3, "dated": 2}},
        ],
        "steps_total": 2, "worker_request_ids": [TICK], "reason_sha256": "0123456789ab",
    },
    "vision_measure": {
        "request_id": TICK, "task": "rail_access", "backend": "opencv", "ok": True, "site_id": "sydney",
        "scene_id": "S2A_56HLH_20261014_0_L2A", "build": BUILD, "opencv": "5.0.0", "kleidicv": True,
        "cpu": CPU, "cpu_count": 2, "arch": "aarch64", "window_px": {"w": 3327, "h": 3306},
        "io_requests": 212, "io_bytes": 54123456, "levels": {"red": 1, "nir": 1, "swir16": 0, "scl": 0},
        "read_ms": 9800, "analyse_ms": 18200, "graph_ms": 2100, "annotate_ms": 1300, "total_ms": 29600,
        "coverage": 0.93, "quality_flags": ["graph_too_large"], "stations": 180, "snapped": 178,
        "nodes": 240, "edges": 262, "components": 2, "rss_peak_mb": 1310,
    },
    "moderation_decision": {
        "topic_id": "rail-sydney", "queue_id": "9b1c3e2a-0000-4000-8000-000000000001",
        "article_id": "9b1c3e2a-0000-4000-8000-000000000002", "decision": "approved",
        "from_status": "pending_moderation", "by": "human",
    },
    "dispatcher_tick": {
        "considered": 12, "dispatched": 1, "started": 0, "skipped_unparsable": 0, "outcome": "ok",
        "ms": 840,
    },
    "topic_changed": {
        "topic_id": "rail-sydney", "action": "updated", "fields": ["adapter_config"],
        "config_keys": ["max_cloud_cover", "triage"], "config_numbers": {"max_cloud_cover": 40},
        "config_off": ["triage"], "force_manual_review": True,
    },
    "model_catalog_job": {
        "job": "discover", "trigger": "schedule", "status": "done", "models": 31,
        "discovered": 2, "not_added": 4, "denied": 1, "access_checked": 7,
    },
    "rollup_done": {
        "trigger": "schedule", "status": "done", "from_day": "2026-10-26", "to_day": "2026-10-27",
        "last_rolled_day": "2026-10-26", "rows": 412, "events": 398, "duplicates": 3, "unreadable": 0,
        "excluded": 11, "late": 1, "late_dropped": 4, "adjustments": 0, "day_rows": 57, "stale_rows": 0,
        "models": 18, "not_in_registry": 1, "classify": "done", "classified": 2, "unknown": 0,
        "judge_pairs": 24, "judge_agreement": 0.7917, "calibrated": True, "splits": 0, "ms": 5310,
    },
    "prompt_fallback": {
        "topic_id": "rail-sydney", "lambda_id": "daily_cycle", "prompt_name": "draft", "version": "v2",
        "code": "locked_order",
    },
}  # fmt: skip


def parsed(event, **fields):
    text = evidence.line(event, **fields)
    out = evidence.parse(text)
    assert out is not None, text
    return out


# --- the kinds -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        ("code", ["a", "desert_km2", "swir16", "aarch64", "x86_64", "a" * 40],
         ["A", "1a", "a-b", "", "a" * 41, 1]),
        ("id", [TICK, "rail-sydney", "S2A_56HLH_20261014_0_L2A", "a.b:c"],
         ["", "a b", "a/b", "x" * 129, None]),
        ("iso", ["2026-10-16T03:00:00+00:00", "2026-10-14T00:06:21Z", "2026-10-14"], ["yesterday", "", 3]),
        ("int", [0, 7, 7.0, 7.4], [True, float("nan"), float("inf"), "7", None]),
        ("num", [0.5, 3, -1.25], [False, float("nan"), "0.5"]),
        ("bool", [True, False], [1, 0, "true", None]),
        ("hex12", [BUILD, "0123456789ab"], ["AB" * 6, "ab" * 5, "ab" * 32, "0123456789ag"]),
        ("model", [MODEL, "anthropic.claude-3-haiku-20240307-v1:0"], ["", "a b", "arn:aws:bedrock:x", 3]),
        ("cpu", [CPU, "Intel(R) Xeon(R) Platinum 8375C CPU @ 2.90GHz", "Arm core 0xd4f", "AMD64",
                 "unknown", "Neoverse V3 (Graviton5, unconfirmed)"],
         ["", "x/y", "a" * 81, "a1\nb", "Neoverse V3 (Graviton5?)", "a:b", 5]),
        ("version", ["5.0.0", "4.11.0.86", "5"], ["v5", "", "5.0-rc"]),
    ],
)
def test_the_scalar_kinds(kind, good, bad):
    for value in good:
        assert evidence.check(kind, value) is not None, (kind, value)
    for value in bad:
        assert evidence.check(kind, value) is None, (kind, value)


def test_num_is_rounded_to_six_significant_figures_and_ints_stay_ints():
    assert evidence.check("num", 0.123456789) == 0.123457
    assert evidence.check("num", 54123456.7) == 54123500.0
    assert evidence.check("num", 3) == 3 and isinstance(evidence.check("num", 3), int)
    assert evidence.check("int", 7.6) == 8


def test_a_model_arn_is_canonicalised_and_an_application_profile_arn_is_dropped():
    arn = f"arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/{MODEL}"
    assert evidence.check("model", arn) == MODEL
    application = "arn:aws:bedrock:ap-southeast-2:123456789012:application-inference-profile/abc123"
    assert evidence.check("model", application) is None


def test_numbers_keep_only_code_to_number_entries_and_at_most_twelve():
    given = {"served_share": 0.7, "station_count": None, "Name": 1, "bad": "x", "ok": True, "desert_km2": 1.5}
    assert evidence.check("numbers", given) == {"served_share": 0.7, "desert_km2": 1.5}
    many = {f"k{n}": n for n in range(20)}
    assert len(evidence.check("numbers", many)) == evidence.MAX_NUMBERS
    assert evidence.check("numbers", [1, 2]) is None


def test_codes_and_ids_are_lists_cut_to_their_caps_and_a_bad_element_drops_the_field():
    assert evidence.check("codes", ["a", "b"]) == ["a", "b"]
    assert evidence.check("codes", ["a", "B"]) is None
    assert evidence.check("ids", [TICK] * 10) == [TICK] * evidence.MAX_IDS
    assert evidence.check("codes", "a,b") == ["a", "b"]  # the comma-joined form a line holds


# --- the step results ----------------------------------------------------------------------------


def test_each_tools_result_keeps_its_own_fields_and_an_unknown_tool_is_other():
    steps = [
        {"tool": "look_again", "ok": True, "ms": 100,
         "result": {"settings": {"reach_m": 800}, "measured": {"coverage": 0.9}, "quality_flags": ["x"],
                    "worker_request_id": TICK, "build": BUILD, "worker_ms": 90, "results": 3}},
        {"tool": "previous_scene", "ok": True, "ms": 5, "result": {"scene_id": "S1", "figure": True}},
        {"tool": "site_history", "ok": True, "ms": 1, "result": {"scenes": 4}},
        {"tool": "web_context", "ok": True, "ms": 900, "result": {"results": 3, "dated": 2, "hosts": ["x"]}},
        {"tool": "look_again", "ok": False, "ms": 2,
         "result": {"code": "no_time", "worker_request_id": "a b"}},
        {"tool": "made_up", "ok": False, "ms": 0, "result": {"code": "unknown_tool", "scenes": 2}},
        {"tool": None, "ok": False, "ms": 0, "result": {"code": "budget"}},
    ]  # fmt: skip
    kept = evidence.check("steps", steps)
    assert [s["tool"] for s in kept] == [
        "look_again", "previous_scene", "site_history", "web_context", "look_again", "other", "other",
    ]  # fmt: skip
    assert set(kept[0]["result"]) == {"settings", "measured", "quality_flags", "worker_request_id", "build",
                                      "worker_ms"}  # fmt: skip
    assert kept[3]["result"] == {"results": 3, "dated": 2}  # never the hosts
    assert kept[4]["result"] == {"code": "no_time"}  # the bad request id is left out, the code kept
    assert kept[5]["result"] == {"code": "unknown_tool"} and kept[6]["result"] == {"code": "budget"}
    # A step that is not a step (`ok` not a bool, `ms` not a number) is left out; the others stay.
    assert evidence.check("steps", [{"tool": "look_again", "ok": "yes", "ms": 1}]) == []
    mixed = [steps[1], {"tool": "look_again", "ok": "yes", "ms": 1}, steps[2],
             {"tool": "site_history", "ok": True, "ms": "fast"}]  # fmt: skip
    assert evidence.check("steps", mixed) == kept[1:3]
    assert evidence.check("steps", "look_again") is None


# --- the line ---------------------------------------------------------------------------------------


def test_the_prefix_is_pinned_byte_for_byte():
    assert evidence.PREFIX == '{"evidence": "bloggerbear/1", "event": "'
    text = evidence.line("research_tick", outcome="no_change")
    assert text.startswith(evidence.PREFIX)
    head = '{"evidence": "bloggerbear/1", "event": "research_tick", "tick": null, "run": null, '
    assert text.startswith(head + '"topic_id": null, ')
    assert json.loads(text)["evidence"] == evidence.VERSION
    assert text.isascii() and "\n" not in text


@pytest.mark.parametrize("event", sorted(SAMPLES))
def test_every_events_sample_round_trips_through_parse(event):
    out = parsed(event, **SAMPLES[event])
    assert out == {"evidence": evidence.VERSION, "event": event, **SAMPLES[event]}
    # Lists are one comma-joined string on the line, so Logs Insights can filter them.
    record = json.loads(evidence.line(event, **SAMPLES[event]))
    for key, value in SAMPLES[event].items():
        if isinstance(value, list) and key != "steps":
            assert record[key] == ",".join(value)


def test_no_event_field_takes_a_common_name():
    """A field named like a COMMON one (`dropped` is the drift marker) would be overwritten by it
    on the line, and read back as the wrong kind."""
    for event, schema in evidence.EVENTS.items():
        assert not set(schema) & set(evidence.COMMON), event


def test_a_review_line_keeps_its_counts_beside_the_drift_marker():
    review = {
        "job": "review", "trigger": "schedule", "status": "reverted", "run_id": "a1b2c3d4", "review": 2,
        "passed": 0, "deferred": 1, "reverted": 0, "unconfirmed": 1, "review_dropped": 0,
    }  # fmt: skip
    event = "model_catalog_job"
    assert parsed(event, **review) == {"evidence": evidence.VERSION, "event": event, **review}
    drifted = parsed(event, **{**review, "trigger": "Not A Code"})
    assert drifted["dropped"] == ["trigger"]
    assert (drifted["review_dropped"], drifted["unconfirmed"]) == (0, 1)


def test_outside_an_invocation_the_ids_are_null_and_nothing_is_counted():
    record = json.loads(evidence.line("vision_site", site_id="sydney", outcome="none"))
    assert (record["tick"], record["run"], record["topic_id"]) == (None, None, None)
    assert "api_request_id" not in record
    assert evidence.emitted("vision_site") == 0 and evidence.bound("tick") is None
    evidence.note(stored=True)  # a no-op outside an invocation
    assert evidence.bound("stored") is None


def test_a_value_of_the_wrong_kind_is_dropped_and_named():
    out = parsed("vision_site", site_id="sydney", outcome="Measured", worker_ms="slow", budget_left_s=1.5)
    assert "outcome" not in out and "worker_ms" not in out and out["budget_left_s"] == 1.5
    assert out["dropped"] == ["outcome", "worker_ms"]


def test_a_field_not_in_the_schema_is_dropped_and_named():
    out = parsed("vision_site", site_id="sydney", outcome="none", reason="the agent's text")
    assert "reason" not in out and out["dropped"] == ["reason"]
    text = evidence.line("vision_site", site_id="sydney", outcome="none", reason="the agent's")
    assert "the agent" not in text


def test_a_field_given_as_none_is_simply_absent():
    text = evidence.line("vision_site", site_id="sydney", outcome="none", code=None, worker_request_id=None)
    record = json.loads(text)
    assert "code" not in record and "dropped" not in record


def test_a_malformed_step_is_left_out_and_the_steps_are_cut_not_dropped():
    """One bad step must not lose the trail: the field keeps the steps that are steps, says `cut`,
    and `steps_total` still says how many there were."""
    steps = [{"tool": "site_history", "ok": True, "ms": 1, "result": {"scenes": 2}},
             {"tool": "look_again", "ok": "yes", "ms": 1},
             {"tool": "previous_scene", "ok": True, "ms": 2, "result": {"scene_id": "S1", "figure": False}}]
    out = parsed("vision_triage", site_id="s", scene_id="x", steps=steps, steps_total=3)
    assert [s["tool"] for s in out["steps"]] == ["site_history", "previous_scene"]
    assert out["steps_total"] == 3 and out["cut"] == ["steps"] and "dropped" not in out


def test_lists_are_cut_not_dropped_and_say_so():
    tools = [f"t{n}" for n in range(15)]
    steps = [{"tool": "other", "ok": False, "ms": 0, "result": {"code": "budget"}} for _ in range(12)]
    out = parsed("vision_triage", site_id="s", scene_id="x", tools=tools, tools_total=15, steps=steps,
                 steps_total=12, budget_refusals=12, worker_request_ids=[TICK] * 9)  # fmt: skip
    assert out["tools"] == tools[: evidence.MAX_TOOLS] and out["tools_total"] == 15
    assert len(out["steps"]) == evidence.MAX_STEPS and out["steps_total"] == 12
    assert len(out["worker_request_ids"]) == evidence.MAX_IDS
    assert out["cut"] == ["tools", "steps", "worker_request_ids"]


def test_an_unknown_event_writes_no_line_and_says_so(capsys):
    assert evidence.line("made_up", a=1) is None
    evidence.emit("made_up", a=1)
    out = capsys.readouterr().out
    assert out.count("evidence: unknown event 'made_up'") == 2 and evidence.PREFIX not in out


def test_emit_never_raises(monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("serialiser down")

    monkeypatch.setattr(evidence.json, "dumps", broken)
    evidence.emit("research_tick", outcome="no_change")
    assert capsys.readouterr().out == "evidence: could not write research_tick: RuntimeError\n"


def big_steps(n=8):
    return [
        {"tool": "look_again", "ok": True, "ms": 100,
         "result": {"settings": {f"k{i}": i for i in range(12)},
                    "measured": {f"m{i}": i + 0.5 for i in range(12)},
                    "quality_flags": [f"flag_{i}" for i in range(12)],
                    "worker_request_id": TICK, "build": BUILD, "worker_ms": 99}}
        for _ in range(n)
    ]  # fmt: skip


def test_a_long_line_loses_the_step_results_first_then_the_steps_then_is_a_stub(monkeypatch):
    fields = {"site_id": "s", "scene_id": "x", "steps": big_steps(), "steps_total": 8,
              "tools": ["look_again"] * 8}  # fmt: skip
    whole = evidence.line("vision_triage", **fields)
    assert len(whole) <= evidence.MAX_LINE_BYTES and json.loads(whole)["steps"][0]["result"]

    monkeypatch.setattr(evidence, "MAX_LINE_BYTES", len(whole) - 1)
    slimmed = json.loads(evidence.line("vision_triage", **fields))
    assert len(slimmed["steps"]) == 8 and slimmed["steps"][0] == {"tool": "look_again", "ok": True, "ms": 100}
    assert slimmed["steps_total"] == 8 and slimmed["tools"] == ",".join(["look_again"] * 8)

    monkeypatch.setattr(evidence, "MAX_LINE_BYTES", 400)
    without = json.loads(evidence.line("vision_triage", **fields))
    assert "steps" not in without and without["steps_total"] == 8 and without["site_id"] == "s"

    monkeypatch.setattr(evidence, "MAX_LINE_BYTES", 120)
    stub = evidence.line("vision_triage", **fields)
    assert stub.startswith(evidence.PREFIX)
    assert json.loads(stub) == {"evidence": evidence.VERSION, "event": "vision_triage", "tick": None,
                                "run": None, "topic_id": None, "truncated": True}  # fmt: skip
    assert evidence.parse(stub)["truncated"] is True


# --- binding ---------------------------------------------------------------------------------------


def test_bound_ids_go_on_every_line_and_are_reset_afterwards(capsys):
    run = evidence.new_run()
    assert re.fullmatch(r"[0-9a-f]{12}", run) and evidence.new_run() != run
    with evidence.bind(tick=TICK, run=run, topic_id="rail-sydney"):
        evidence.note(adapter="rail_access", stored=True)
        assert evidence.bound("adapter") == "rail_access" and evidence.bound("tick") == TICK
        evidence.emit("vision_site", site_id="sydney", outcome="measured")
        evidence.emit("vision_site", site_id="melbourne", outcome="not_reached")
        evidence.emit("vision_decision", site_id="sydney", scene_id="S1", code="within_range")
        assert evidence.emitted("vision_site") == 2 and evidence.emitted("vision_decision") == 1
        assert evidence.emitted("vision_triage") == 0
    assert evidence.bound("tick") is None and evidence.emitted("vision_site") == 0
    lines = [evidence.parse(line) for line in capsys.readouterr().out.splitlines()]
    assert all(line and line["tick"] == TICK and line["run"] == run and line["topic_id"] == "rail-sydney"
               for line in lines)  # fmt: skip
    assert "adapter" not in lines[0]  # noted, but not a field of vision_site
    out = parsed("research_tick", adapter="rail_access", outcome="no_change")
    assert out["adapter"] == "rail_access"


def test_the_admin_api_binds_its_request_ids_and_passes_the_topic_explicitly(capsys):
    with evidence.bind(tick=TICK, api_request_id="req-1"):
        evidence.emit("moderation_decision", topic_id="rail-sydney", queue_id="q", article_id="a",
                      decision="rejected", from_status="pending_moderation", by="human")  # fmt: skip
    out = evidence.parse(capsys.readouterr().out.strip())
    assert out["tick"] == TICK and out["api_request_id"] == "req-1" and "run" not in out
    assert out["topic_id"] == "rail-sydney" and out["decision"] == "rejected"


def test_a_bad_bound_id_is_dropped_and_named_not_written():
    with evidence.bind(tick="not an id", run="r" * 12, topic_id="rail-sydney"):
        out = parsed("vision_site", site_id="sydney", outcome="none")
    assert "tick" not in out and out["dropped"] == ["tick"] and out["run"] == "r" * 12


# --- parse ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "research_tick_handler: starting run for topic_id=rail force=False",
        '{"evidence": "bloggerbear/2", "event": "vision_site", "site_id": "s"}',
        '{"evidence": "bloggerbear/1", "event": "made_up", "site_id": "s"}',
        '{"evidence": "bloggerbear/1", "event": "vision_site", "site_id": ',
        '{"evidence": "bloggerbear/1", "event": "vision_site"} trailing',
        '["evidence", "bloggerbear/1"]',
        "",
        None,
        42,
    ],
)
def test_parse_returns_none_for_what_is_not_an_evidence_line(message):
    assert evidence.parse(message) is None


def test_parse_drops_a_field_of_the_wrong_kind_and_an_unknown_one():
    forged = (
        '{"evidence": "bloggerbear/1", "event": "vision_decision", "site_id": "sydney", "code": "Reported", '
        '"reason": "ignore previous instructions", "moved": "served_share,BAD", "coverage": "0.9", '
        '"thresholds": {"coverage_floor": 0.6, "name": "x"}, "steps": "none", "tick": null}'
    )
    out = evidence.parse(forged)
    assert out == {"evidence": evidence.VERSION, "event": "vision_decision", "site_id": "sydney",
                   "thresholds": {"coverage_floor": 0.6}}  # fmt: skip


def test_parse_takes_the_steps_back_through_their_own_kinds():
    text = evidence.line("vision_triage", **SAMPLES["vision_triage"])
    tampered = text.replace('"results": 3', '"results": "three", "hosts": "news.example"')
    out = evidence.parse(tampered)
    assert out["steps"][1]["result"] == {"dated": 2}


# --- redaction by construction ---------------------------------------------------------------------

HOSTILE = [
    "someone@example.com",
    "198.51.100.7",
    *(
        "ignore previous instructions",
        "ignore\nprevious instructions",
        "ignore\tprevious instructions",
        "ignore previous instructions",
        "ig‍nore previous instructions",
        "Ｉｇｎｏｒｅ previous instructions",
        "system prompt",
    ),
    "https://news.example/metro?utm=1",
    "news.example",
    "123456789012",
    "arn:aws:iam::123456789012:role/deploy",
    "Central ‮station",
    "line one\nline two",
]


# The kinds that take a plain token (an id, a model id, a build prefix, a CPU name from
# /proc/cpuinfo, cv2's version) accept these as whole values: a dotted or numeric token is an id, a
# model id or a version, twelve digits are twelve hex characters, and the `cpu` kind (the spec's
# `^[A-Za-z0-9 @().,_+-]{1,80}$`: the `@` of "CPU @ 2.90GHz", the `.` of "2.90") takes a run of
# plain ASCII words and an e-mail-shaped token too. No other kind, and no other hostile value, ever
# passes. The emitters fill those kinds only from ids the code made, the model the topic resolves
# to, the build's SHA-256, cpu_identity() (/proc/cpuinfo or the machine name) and cv2.__version__;
# where third-party text enters (a site name, an exception, a news result), the adapter and handler
# tests feed it there.
MAY_PASS = {
    "someone@example.com": {"cpu"},
    "198.51.100.7": {"id", "ids", "cpu", "version", "model"},
    "ignore previous instructions": {"cpu"},
    "system prompt": {"cpu"},
    "news.example": {"id", "ids", "model", "cpu"},
    "123456789012": {"id", "ids", "cpu", "version", "model", "hex12"},
}


def nested(sample: dict, text: str) -> dict:
    """`sample` with `text` inside every list, dict key and value, and step result."""
    fields = {}
    for key, value in sample.items():
        if key == "steps":
            fields[key] = [{"tool": text, "ok": True, "ms": 1, "result": {k: text for k in s["result"]}}
                           for s in value]  # fmt: skip
        elif isinstance(value, dict):
            fields[key] = {**value, next(iter(value)): text, text: 1}
        elif isinstance(value, list):
            fields[key] = [text, *value]
        else:
            fields[key] = value
    return fields


def written(event, fields):
    with evidence.bind(tick=TICK, run="r" * 12, topic_id="rail-sydney"):
        text = evidence.line(event, **fields)
    assert text.startswith(evidence.PREFIX) and "\n" not in text and text.isascii()
    assert evidence.parse(text) is not None and json.loads(text)["tick"] == TICK
    return text


@pytest.mark.parametrize("event", sorted(SAMPLES))
def test_a_hostile_value_is_dropped_from_every_field_whose_kind_is_not_a_plain_token(event):
    kinds = evidence.EVENTS[event]
    for text in HOSTILE:
        accepted = set()
        for key, kind in kinds.items():
            line = written(event, {**SAMPLES[event], key: text})
            if text in line:
                accepted.add(kind)
                assert json.loads(line)[key] == text and text not in line.replace(f'"{key}": "{text}"', "")
            else:
                assert "202e" not in line.lower() and not redact.looks_like_instructions(line)
        assert accepted == MAY_PASS.get(text, set()) & set(kinds.values()), (text, accepted)
        # Inside a list, a dict or a step result: only an id inside an `ids` list or a step's
        # `worker_request_id` can carry a plain token through.
        line = written(event, nested(SAMPLES[event], text))
        if MAY_PASS.get(text, set()) & {"id", "ids"} and ("steps" in kinds or "ids" in kinds.values()):
            continue
        assert text not in line, (event, text)
        assert not redact.looks_like_instructions(line) and not redact.personal_data_kinds(line)


def test_a_hostile_value_that_no_kind_takes_leaves_no_trace_in_any_event():
    never = [text for text in HOSTILE if text not in MAY_PASS]
    for event, sample in SAMPLES.items():
        for text in never:
            everywhere = {key: text for key in sample}
            line = written(event, everywhere)
            assert text not in line and "example" not in line and "123456789012" not in line
            assert "\\u202e" not in line and "\\n" not in line
            assert not redact.looks_like_instructions(line) and not redact.personal_data_kinds(line)


@pytest.mark.parametrize("event", sorted(SAMPLES))
def test_no_sample_line_reads_like_instructions_and_no_field_is_named_tool_calls(event):
    text = evidence.line(event, **SAMPLES[event])
    assert not redact.looks_like_instructions(text)
    assert redact.scrub(text, limit=10000) != redact.WITHHELD
    assert "tool_calls" not in text
    for schema in (*evidence.EVENTS.values(), *evidence.STEP_RESULT.values(), evidence.COMMON):
        assert "tool_calls" not in schema
