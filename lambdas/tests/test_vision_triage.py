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


def test_out_of_time_is_an_artefact_before_any_model_call(recorded):
    script = Script(text('{"verdict": "real", "reason": "x"}'))
    out = run(script, deadline=100.0, clock=lambda: 95.0)  # 5 s left: not enough for a turn
    assert (out["verdict"], out["reason"]) == ("artefact", "no time left to decide")
    assert script.requests == [] and out["model_calls"] == 0 and recorded == []


def test_look_again_is_refused_when_the_worker_might_not_answer_in_time():
    worker = Worker(count=12)
    script = Script(
        tool("look_again", {"offset": 40}), text('{"verdict": "artefact", "reason": "no re-check"}')
    )
    # 20 s left: enough to answer, not enough to wait on the worker.
    out = run(script, worker=worker, deadline=100.0, clock=lambda: 80.0)
    assert worker.calls == []
    result = script.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "error" and "No time left" in result["content"][0]["text"]
    assert out["verdict"] == "artefact"
    assert out["tool_calls"] == [{"name": "look_again", "input": {"offset": 40}, "ok": False}]


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


# --- the rail profile ------------------------------------------------------------------------------

RAIL_SITE = {"id": "sydney", "name": "Sydney", "polygon": POLYGON}
RAIL_SCENE = {
    "id": "R2", "captured_at": "2026-10-03", "assets": {"red": "r", "nir": "n", "swir16": "s", "scl": "c"},
}  # fmt: skip
NETWORK = {
    "ways": [{"id": "w1", "points": [[151.2, -33.9], [151.21, -33.9]], "tunnel": False, "name": None}],
    "stations": [{"id": "n1", "name": "Central", "lon": 151.2, "lat": -33.9, "kind": "station"}],
    "pois": [],
    "fetched_at": "2026-10-01T00:00:00+00:00",
}
RAIL_HISTORY = [
    {"scene_id": "R1", "captured_at": "2026-09-13", "served_share": 0.73, "desert_km2": 111.3,
     "station_count": 178, "coverage": 0.9, "image_key": "k1", "quality_flags": [], "hubs": ["s:n1"],
     "flags": [], "stations": [{"id": "s:n1"}], "edges": [{"from": "s:n1", "to": "j1"}]},
    {"scene_id": "R2", "captured_at": "2026-10-03", "served_share": 0.68, "desert_km2": 130.0,
     "station_count": 180, "coverage": 0.93, "image_key": "k3", "quality_flags": [], "hubs": ["s:n1"],
     "flags": [{"type": "ghost_line", "stations": ["s:n4"], "names": ["X"]}], "stations": [{"id": "s:n1"}],
     "network_fetched_at": "2026-10-01T00:00:00+00:00", "assets": RAIL_SCENE["assets"]},
]  # fmt: skip
RAIL_VERDICT = {
    "baseline": {"served_share": 0.73, "desert_km2": 111.3, "station_count": 178},
    "baseline_scenes": 1,
    "delta": {"served_share": -0.05, "desert_km2": 18.7, "station_count": 2},
    "relative": {"desert_km2": 0.168},
    "reason": "served share moved, station count changed",
}


class RailWorker:
    """The worker for the rail task: records every keyword it is given (a rail re-measurement
    carries the task, the network and the read timeout; a ships one, above, carries none)."""

    def __init__(self, error=None):
        self.error, self.calls = error, []

    def __call__(self, site, scene, backend, params, coverage_floor, **kwargs):
        self.calls.append({"site": site, "scene": scene, "backend": backend, "params": params,
                           "coverage_floor": coverage_floor, **kwargs})  # fmt: skip
        if self.error:
            raise self.error
        metrics = {"served_share": 0.71, "desert_km2": 118.0, "station_count": 180, "coverage": 0.93,
                   "quality_flags": [], "hubs": ["s:n1"], "stations": [{"id": "s:n1"}] * 3,
                   "edges": []}  # fmt: skip
        return vision_client.VisionResult(reply={"metrics": metrics}, image_png=PNG)


class FakeSearch:
    """Stands in for common/web_search.search_web."""

    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results or [], error, []

    def __call__(self, query, *, max_results, max_age_hours, deadline=None, **kwargs):
        self.calls.append({"query": query, "max_results": max_results, "max_age_hours": max_age_hours,
                           "deadline": deadline, **kwargs})  # fmt: skip
        if self.error:
            raise self.error
        return self.results


HEADLINES = [
    {"title": "New metro line opens", "url": "https://news.example/metro", "source": "news.example",
     "published_at": "2026-09-30T00:00:00+00:00", "snippet": None},
    {"title": "Station closed for works", "url": "https://news.example/works", "source": "news.example",
     "published_at": "2026-09-20T00:00:00+00:00", "snippet": "Six weeks."},
]  # fmt: skip


def run_rail(script, worker=None, search=None, **kwargs):
    return vision_triage.triage(
        site=RAIL_SITE,
        verdict=RAIL_VERDICT,
        history=RAIL_HISTORY,
        scene=RAIL_SCENE,
        model_id="au.anthropic.claude-haiku-4-5-20251001-v1:0",
        object_noun=None,
        params={"reach_m": 1000.0},
        load_image={"k1": PNG, "k3": PNG}.get,
        converse=script,
        measure=worker or RailWorker(),
        task="rail_access",
        network=NETWORK,
        web_search=search,
        read_timeout=58.0,
        **kwargs,
    )


def test_the_rail_profile_has_its_own_prompt_tools_and_brief():
    script = Script(text('{"verdict": "real", "reason": "A new line."}'))
    out = run_rail(script, search=FakeSearch())
    assert out["verdict"] == "real"
    sent = script.requests[0]
    assert sent["system"][0]["text"] == vision_triage.RAIL_SYSTEM_PROMPT
    assert all(word in vision_triage.RAIL_SYSTEM_PROMPT for word in ("OpenStreetMap", "web_context"))
    names = [t["toolSpec"]["name"] for t in sent["toolConfig"]["tools"]]
    assert names == ["look_again", "previous_scene", "site_history", "web_context"]
    brief = json.loads(sent["messages"][0]["content"][0]["text"].split("\n", 1)[1])
    assert brief["task"] == "rail_access" and "counting" not in brief
    assert brief["latest"]["served_share"] == 0.68 and brief["latest"]["station_count"] == 180
    assert brief["latest"]["flags"][0]["type"] == "ghost_line"
    assert "stations" not in brief["latest"] and "edges" not in brief["latest"]  # the lists stay out
    assert brief["baseline_median"] == RAIL_VERDICT["baseline"] and brief["change"] == RAIL_VERDICT["delta"]
    assert brief["flagged_because"] == "served share moved, station count changed"
    assert "image" in sent["messages"][0]["content"][1]


def test_the_rail_look_again_schema_is_the_rail_limits_with_whole_numbers_as_integers():
    schema = vision_triage.RAIL_TOOLS[0]["toolSpec"]["inputSchema"]["json"]
    assert set(schema["properties"]) == set(contract.RAIL_PARAM_LIMITS)
    assert schema["additionalProperties"] is False
    for name, (lo, hi) in contract.RAIL_PARAM_LIMITS.items():
        assert (schema["properties"][name]["minimum"], schema["properties"][name]["maximum"]) == (lo, hi)
    assert schema["properties"]["hub_count"]["type"] == "integer"
    assert schema["properties"]["reach_m"]["type"] == "number"
    # The ships schema types its whole numbers the same way.
    ships = vision_triage.TOOLS[0]["toolSpec"]["inputSchema"]["json"]["properties"]
    assert ships["block_size"]["type"] == "integer" and ships["offset"]["type"] == "number"
    assert vision_triage.PROFILES["rail_access"].look_again_limits is contract.RAIL_PARAM_LIMITS


def test_rail_look_again_sends_the_task_the_network_and_the_read_timeout():
    worker = RailWorker()
    script = Script(
        tool("look_again", {"reach_m": 800}),
        text('{"verdict": "real", "reason": "The share falls further at 800 m too."}'),
    )
    out = run_rail(script, worker=worker)
    call = worker.calls[0]
    assert call["site"] == {"id": "sydney", "polygon": POLYGON} and call["scene"] == RAIL_SCENE
    assert call["params"] == {"reach_m": 800} and call["coverage_floor"] == 0.0
    assert call["task"] == "rail_access" and call["network"] == NETWORK and call["read_timeout"] == 58.0
    result = script.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "success"
    answer = result["content"][0]["json"]
    assert answer["settings"] == {"reach_m": 800} and answer["served_share"] == 0.71
    assert "stations" not in answer and "image" in result["content"][1]
    assert out["tool_calls"] == [{"name": "look_again", "input": {"reach_m": 800}, "ok": True}]


def test_rail_site_history_and_previous_scene_tell_the_rail_metrics():
    script = Script(
        tool("site_history", use_id="a"),
        tool("previous_scene", use_id="b"),
        text('{"verdict": "artefact", "reason": "x"}'),
    )
    run_rail(script)
    rows = script.requests[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["json"]["scenes"]
    assert [tuple(r) for r in rows] == [vision_triage.RAIL_HISTORY_KEYS] * 2
    assert [r["served_share"] for r in rows] == [0.73, 0.68]
    previous = script.requests[2]["messages"][-1]["content"][0]["toolResult"]["content"]
    assert previous[0]["json"]["scene_id"] == "R1" and previous[0]["json"]["desert_km2"] == 111.3
    assert "stations" not in previous[0]["json"] and "image" in previous[1]


def test_web_context_searches_the_site_and_returns_headlines():
    search = FakeSearch(HEADLINES)
    script = Script(tool("web_context"), text('{"verdict": "real", "reason": "A new line opened."}'))
    out = run_rail(script, search=search, deadline=200.0, clock=lambda: 100.0)
    assert out["verdict"] == "real"
    assert out["tool_calls"] == [{"name": "web_context", "input": {}, "ok": True}]
    (asked,) = search.calls
    assert asked["query"] == "Sydney rail station opening closure line"
    assert asked["max_results"] == 5 and asked["max_age_hours"] == 24 * 60
    assert asked["deadline"] == 110.0  # ten seconds, well inside the 100 s left
    result = script.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "success"
    answer = result["content"][0]["json"]
    assert answer["query"] == asked["query"]
    assert [r["title"] for r in answer["results"]] == ["New metro line opens", "Station closed for works"]
    assert set(answer["results"][0]) == {"title", "url", "published_at", "source", "snippet"}


def test_web_context_keeps_to_the_result_cap_and_ten_seconds_without_a_deadline():
    search = FakeSearch([dict(HEADLINES[0], title=f"Headline {n}") for n in range(9)])
    script = Script(tool("web_context"), text('{"verdict": "artefact", "reason": "x"}'))
    run_rail(script, search=search, clock=lambda: 100.0)
    assert search.calls[0]["deadline"] == 110.0
    answer = script.requests[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["json"]
    assert len(answer["results"]) == 5


def test_near_the_deadline_web_context_gets_only_what_is_left_and_the_agent_then_fails_closed():
    # 12 s left at the first turn (enough to ask), 7 s when the tool runs: the search may run to the
    # deadline, not ten seconds past it, and with under ten left no further turn starts.
    times = iter([100.0, 105.0])
    search = FakeSearch(HEADLINES)
    script = Script(tool("web_context"), text('{"verdict": "real", "reason": "x"}'))
    out = run_rail(script, search=search, deadline=112.0, clock=lambda: next(times, 105.0))
    assert search.calls[0]["deadline"] == 112.0
    assert (out["verdict"], out["reason"]) == ("artefact", "no time left to decide")
    assert len(script.requests) == 1


def test_a_failed_news_search_is_an_error_result_the_model_can_work_around():
    search = FakeSearch(error=TimeoutError("gdelt slow"))
    answer = text('{"verdict": "artefact", "reason": "Could not check the news."}')
    script = Script(tool("web_context"), answer)
    out = run_rail(script, search=search)
    result = script.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "error"
    assert result["content"][0]["text"] == "News search failed (TimeoutError)."
    assert out["verdict"] == "artefact" and out["tool_calls"][0]["ok"] is False


def test_web_context_is_absent_when_the_topic_turns_it_off():
    script = Script(tool("web_context"), text('{"verdict": "artefact", "reason": "x"}'))
    run_rail(script, search=None)
    names = [t["toolSpec"]["name"] for t in script.requests[0]["toolConfig"]["tools"]]
    assert names == ["look_again", "previous_scene", "site_history"]
    result = script.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "error" and "not available" in result["content"][0]["text"]


def test_the_ships_profile_never_offers_web_context():
    search = FakeSearch(HEADLINES)
    script = Script(tool("web_context"), text('{"verdict": "artefact", "reason": "x"}'))
    run(script, web_search=search)
    names = [t["toolSpec"]["name"] for t in script.requests[0]["toolConfig"]["tools"]]
    assert names == ["look_again", "previous_scene", "site_history"]
    result = script.requests[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "error" and "no tool called" in result["content"][0]["text"]
    assert search.calls == []


def test_an_unknown_task_is_an_artefact_before_any_model_call(recorded):
    script = Script(text('{"verdict": "real", "reason": "x"}'))
    out = run(script, task="rail_yards")
    assert (out["verdict"], out["reason"]) == ("artefact", "no triage profile for task 'rail_yards'")
    assert script.requests == [] and recorded == []
