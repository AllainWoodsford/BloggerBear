"""Tests for common/vision_triage.py: the vision agent's bounded, fail-closed decision.

Bedrock Converse is scripted: each test says what the model "replies" turn by turn, and checks what
the agent sent, what the tools did, and the verdict. The worker is faked too.
"""

from __future__ import annotations

import json

import pytest

from common import vision_client, vision_triage
from common import vision_contract as contract

PNG = vision_client.PNG_SIGNATURE + b"figure"
POLYGON = [[151.2, -33.97], [151.26, -33.97], [151.26, -34.0]]
SITE = {"id": "botany", "name": "Botany Bay anchorage", "polygon": POLYGON}
SCENE = {"id": "S3", "captured_at": "2026-09-13", "assets": {"nir": "n", "green": "g", "scl": "s"}}
HISTORY = [
    {"scene_id": "S1", "captured_at": "2026-09-08", "count": 11, "coverage": 0.9, "image_key": "k1",
     "quality_flags": [], "rejected": {"edge": 2}},
    {"scene_id": "S2", "captured_at": "2026-09-10", "count": 12, "coverage": 0.95, "image_key": "k2",
     "quality_flags": []},
    {"scene_id": "S3", "captured_at": "2026-09-13", "count": 30, "coverage": 0.92, "image_key": "k3",
     "quality_flags": [], "rejected": {"candidates": 60, "edge": 25}, "assets": SCENE["assets"]},
]  # fmt: skip
VERDICT = {"baseline": 11.5, "baseline_scenes": 2, "delta": 18.5, "relative": 1.609}


def text(answer, tokens=(100, 20)):
    return {
        "stopReason": "end_turn",
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
        "output": {"message": {"role": "assistant", "content": [{"text": answer}]}},
    }


def tool(name, payload=None, use_id="t1"):
    return {
        "stopReason": "tool_use",
        "usage": {"inputTokens": 50, "outputTokens": 10},
        "output": {"message": {"role": "assistant", "content": [
            {"text": "Let me check."},
            {"toolUse": {"toolUseId": use_id, "name": name, "input": payload or {}}},
        ]}},
    }  # fmt: skip


class Script:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, **request):
        # A deep copy of what was sent, as it was at the time (the agent keeps appending).
        self.requests.append(json.loads(json.dumps(request, default=lambda b: b.decode("latin-1"))))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Worker:
    def __init__(self, count=29, error=None):
        self.count, self.error, self.calls = count, error, []

    def __call__(self, site, scene, backend, params, coverage_floor):
        self.calls.append({"site": site, "scene": scene, "backend": backend, "params": params,
                           "coverage_floor": coverage_floor})  # fmt: skip
        if self.error:
            raise self.error
        metrics = {"count": self.count, "coverage": 0.92, "quality_flags": [], "rejected": {"edge": 30}}
        return vision_client.VisionResult(reply={"metrics": metrics}, image_png=PNG)


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(
        vision_triage.stats_tracking, "record_model_usage", lambda *args: calls.append(args)
    )
    return calls


def run(script, worker=None, images=None, **kwargs):
    images = {"k1": PNG, "k2": PNG, "k3": PNG} if images is None else images
    return vision_triage.triage(
        site=SITE,
        verdict=VERDICT,
        history=HISTORY,
        scene=SCENE,
        model_id="au.anthropic.claude-haiku-4-5-20251001-v1:0",
        object_noun="large vessels",
        params={"offset": 25},
        load_image=images.get,
        converse=script,
        measure=worker or Worker(),
        **kwargs,
    )


def test_a_direct_answer_with_the_figure_in_front_of_it(recorded):
    script = Script(text('{"verdict": "real", "reason": "Boxes sit on open water."}'))
    out = run(script)
    assert (out["verdict"], out["reason"]) == ("real", "Boxes sit on open water.")
    sent = script.requests[0]
    assert sent["system"][0]["text"] == vision_triage.SYSTEM_PROMPT
    names = [t["toolSpec"]["name"] for t in sent["toolConfig"]["tools"]]
    assert names == ["look_again", "previous_scene", "site_history"]
    first = sent["messages"][0]["content"]
    brief = json.loads(first[0]["text"].split("\n", 1)[1])
    assert brief["baseline_median"] == 11.5 and brief["latest"]["count"] == 30
    assert brief["latest"]["rejected"] == {"candidates": 60, "edge": 25}
    assert "image" in first[1] and first[1]["image"]["format"] == "png"
    assert out["model_calls"] == 1 and (out["input_tokens"], out["output_tokens"]) == (100, 20)
    assert recorded == [("vision_triage", "au.anthropic.claude-haiku-4-5-20251001-v1:0", 100, 20, 1)]


def test_without_a_figure_it_is_told_so():
    script = Script(text('{"verdict": "artefact", "reason": "cannot see"}'))
    run(script, images={})
    said = script.requests[0]["messages"][0]["content"][1]
    assert said == {"text": "(The figure for this scene is not available.)"}


def test_look_again_re_measures_the_same_scene_with_merged_settings():
    worker = Worker(count=12)
    script = Script(
        tool("look_again", {"edge_buffer_px": 6}),
        text('{"verdict": "artefact", "reason": "With a wider shore buffer the count falls to 12."}'),
    )
    out = run(script, worker=worker)
    assert out["verdict"] == "artefact"
    call = worker.calls[0]
    assert call["scene"] == SCENE and call["site"] == {"id": "botany", "polygon": SITE["polygon"]}
    assert call["params"] == {"offset": 25, "edge_buffer_px": 6} and call["coverage_floor"] == 0.0
    result = script.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "success" and result["toolUseId"] == "t1"
    assert result["content"][0]["json"]["count"] == 12
    assert "image" in result["content"][1]
    assert out["tool_calls"] == [{"name": "look_again", "input": {"edge_buffer_px": 6}, "ok": True}]


def test_previous_scene_and_history_tools():
    script = Script(
        tool("previous_scene", use_id="a"),
        tool("site_history", use_id="b"),
        text('{"verdict": "real", "reason": "A steady rise across scenes."}'),
    )
    out = run(script)
    previous = script.requests[1]["messages"][-1]["content"][0]["toolResult"]["content"]
    assert previous[0]["json"]["scene_id"] == "S2" and "image" in previous[1]
    history = script.requests[2]["messages"][-1]["content"][0]["toolResult"]["content"][0]["json"]
    assert [s["count"] for s in history["scenes"]] == [11, 12, 30]
    assert out["verdict"] == "real" and len(out["tool_calls"]) == 2


def test_the_tool_budget_and_turn_limit_are_enforced_in_code():
    script = Script(*[tool("site_history", use_id=f"t{n}") for n in range(5)])
    out = run(script, max_tool_calls=2)
    assert (out["verdict"], out["reason"]) == ("artefact", "no verdict within the turn limit")
    assert len(script.requests) == 4  # max_tool_calls + 2 turns
    over = script.requests[3]["messages"][-1]["content"][0]["toolResult"]
    assert over["status"] == "error" and "budget" in over["content"][0]["text"]
    assert [c["ok"] for c in out["tool_calls"]] == [True, True, False, False]  # refused ones logged


def test_a_request_past_the_budget_is_refused_and_the_model_still_answers():
    script = Script(tool("site_history", use_id="a"), tool("site_history", use_id="b"))
    script.replies.append(text('{"verdict": "real", "reason": "ok"}'))
    out = run(script, max_tool_calls=1)
    assert out["verdict"] == "real"
    assert [c["ok"] for c in out["tool_calls"]] == [True, False]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("Looks real to me.", "no verdict in the answer"),
        ('{"verdict": "real", reason: oops}', "the verdict was not valid JSON"),
        ('{"verdict": "probably", "reason": "x"}', "the verdict was neither real nor artefact"),
    ],
)
def test_an_unusable_answer_is_an_artefact(answer, reason):
    out = run(Script(text(answer)))
    assert (out["verdict"], out["reason"]) == ("artefact", reason)


def test_a_bedrock_failure_is_an_artefact(recorded):
    out = run(Script(RuntimeError("throttled")))
    assert out["verdict"] == "artefact" and "triage unavailable" in out["reason"]
    assert recorded == []  # no call completed, nothing to tally


def test_a_failed_tool_is_reported_to_the_model_which_still_decides():
    worker = Worker(error=vision_client.VisionError("unreadable_scene", "503"))
    script = Script(
        tool("look_again", {"offset": 40}), text('{"verdict": "artefact", "reason": "could not re-check"}')
    )
    out = run(script, worker=worker)
    result = script.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "error" and "unreadable_scene" in result["content"][0]["text"]
    assert out["tool_calls"][0]["ok"] is False and out["verdict"] == "artefact"


def test_an_unknown_tool_is_an_error_result():
    script = Script(tool("delete_everything"), text('{"verdict": "artefact", "reason": "x"}'))
    run(script)
    assert script.requests[1]["messages"][-1]["content"][0]["toolResult"]["status"] == "error"


def test_the_look_again_schema_is_the_worker_contracts_limits():
    schema = vision_triage.TOOLS[0]["toolSpec"]["inputSchema"]["json"]
    assert set(schema["properties"]) == set(contract.PARAM_LIMITS)
    assert schema["additionalProperties"] is False
    for name, (lo, hi) in contract.PARAM_LIMITS.items():
        assert (schema["properties"][name]["minimum"], schema["properties"][name]["maximum"]) == (lo, hi)


def test_parse_verdict_trims_and_defaults_the_reason():
    assert vision_triage.parse_verdict('Sure.\n{"verdict": "real", "reason": "  ok  "}') == ("real", "ok")
    assert vision_triage.parse_verdict('{"verdict": "real"}') == ("real", "(no reason given)")
    long = "x" * 1000
    assert len(vision_triage.parse_verdict(json.dumps({"verdict": "real", "reason": long}))[1]) == 300
