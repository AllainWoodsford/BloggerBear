"""Tests for `vision_eval.py agent`, `counterfactual` and `stability` (scripts/vision_eval_agent.py)
run end to end: the real rail adapter through Q2a's driver, the real worker handler in this process
on the driver's synthetic city, the real common/vision_triage.triage with a scripted model (or a
fake Converse standing in for Bedrock), and the run file each writes.

Nothing here touches the network or a model: requests, boto3 and socket connections raise for
every test, including the module-scoped runs, which patch them for their own length.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from test_vision_eval_replay import ARN as DEPLOYED_ARN
from test_vision_eval_replay import scripted_worker

import vision_eval
import vision_eval_agent as agent
import vision_eval_agent_cli
import vision_eval_driver as driver
import vision_eval_scoring as scoring
import vision_runs


def guard(patch):
    import socket

    import boto3
    import requests

    def refuse(*args, **kwargs):
        raise AssertionError("the tests must not touch the network")

    for name in ("get", "head", "put", "post", "request"):
        patch.setattr(requests, name, refuse)
    patch.setattr(requests.Session, "request", refuse)
    patch.setattr(boto3, "client", refuse)
    patch.setattr(boto3, "resource", refuse)
    patch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    guard(monkeypatch)


@pytest.fixture(scope="module")
def manifest():
    return vision_eval.load_manifest()


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    agent.add_parsers(parser.add_subparsers(dest="command", required=True), vision_eval.MANIFEST_FILE)
    return parser.parse_args(argv)


def run_main(argv: list[str], out: Path, **seams) -> tuple[int, dict | None]:
    """vision_eval_agent.main on `argv` with --out `out`; the exit code and the run file written."""
    code = agent.main(parse([*argv, "--out", str(out)]), vision_eval.load_manifest(), **seams)
    files = sorted(out.glob("*.json")) if out.is_dir() else []
    return code, (json.loads(files[-1].read_text()) if files else None)


@pytest.fixture(scope="module")
def agent_run(tmp_path_factory):
    """Every case on the synthetic city, scripted, twice each: the run CI makes."""
    out = tmp_path_factory.mktemp("agent")
    with pytest.MonkeyPatch.context() as patch:
        guard(patch)
        code = vision_eval.main(["agent", "--synthetic", "--repeats", "2", "--out", str(out)])
    path = next(out.glob("*.json"))
    return code, path, json.loads(path.read_text())


# --- agent -------------------------------------------------------------------------------------------------


def test_the_scripted_synthetic_run_scores_every_case_and_writes_a_run_file(agent_run):
    code, path, run = agent_run
    assert code == 0 and all(check["pass"] for check in run["checks"])
    assert vision_runs.load(path)["kind"] == "eval-agent" and run["mode"] == "agent"
    assert run["run_id"].endswith("-eval-agent-" + vision_runs.cpu_slug(run["environment"]["cpu"])
                                  + "-opencv-scripted-synthetic")  # fmt: skip
    assert run["inputs"]["synthetic"] is True and run["inputs"]["networks"][0]["file"].endswith(
        "synthetic-city-network.json")
    assert run["worker"]["kind"] == "in-process" and len(run["worker"]["code_sha256"]) == 64
    cases = run["results"]["cases"]
    assert list(cases) == ["C1", "C1c", "C1d", "C1e", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10"]
    # C3's summer stand-in measures as its winter history did, and C10's smoke stand-in as its clear
    # baseline (the synthetic city has no smoke): neither reaches the agent, each measured once.
    for name in ("C3", "C10"):
        assert cases[name]["gate"] == "within_range" and not cases[name]["included"]
        assert len(cases[name]["runs"]) == 1
    s = run["summary"]
    assert (s["cases"], s["gate_closed"], s["runs"], s["not_measured_runs"]) == (13, 2, 11 * 2 + 2, 0)
    assert all(isinstance(value, int | float) and not isinstance(value, bool) for value in s.values())
    assert s["recall_real"] == 1.0 and s["recall_artefact"] == 1.0 and s["balanced_accuracy"] == 1.0
    assert s["numeric_only_balanced_accuracy"] == 0.5 and s["always_artefact_balanced_accuracy"] == 0.5
    assert (s["fail_closed"], s["fail_closed_runs"]) == (6, 6) and s["stability"] == 1.0
    assert s["recall_network_driven"] == 1.0 and s["recall_imagery_driven"] == 1.0
    # The scripted model has no price: no cost is invented for it, and the summary says not measured.
    assert "cost_usd_total" not in s and "cost_usd_total" in run["results"]["not_measured"]
    assert s["tokens_mean"] > 0
    assert run["results"]["history"]["reused"] > 0  # the shared winter history is measured once
    assert run["params"]["frozen_clock"] is True and run["params"]["repeats"] == 2


def test_each_run_keeps_its_verdict_stop_steps_reason_and_tokens(agent_run):
    run = agent_run[2]
    row = run["results"]["cases"]["C1"]["runs"][0]
    assert (row["verdict"], row["stop"], row["code"]) == ("real", "answered", "reported") and row["finding"]
    assert row["tools"] == ["look_again", "web_context"] and [s["tool"] for s in row["steps"]] == row["tools"]
    assert row["steps"][0]["result"]["settings"]["reach_m"] == 1200
    assert row["steps"][1]["result"]["results"] == 1
    assert row["reason"].startswith("Five new stations") and row["input_tokens"] == 3 * 2400
    c2 = run["results"]["cases"]["C2"]["runs"][0]
    assert (c2["verdict"], c2["code"], c2["finding"]) == ("artefact", "artefact", False)
    # Every response is kept in the driver's recorded form, tagged with its case and repeat.
    responses = [r for r in run["agent"]["responses"] if r["case"] == "C1"]
    assert [r["repeat"] for r in responses] == [0, 1] and len(responses[0]["turns"]) == 3
    assert set(responses[0]["turns"][0]) == {"output", "stopReason", "usage"}
    assert run["agent"]["mode"] == "scripted" and "built-in price table" in run["agent"]["price"]


def test_the_failure_cases_end_as_each_requires(agent_run):
    cases = agent_run[2]["results"]["cases"]
    c6 = cases["C6"]["runs"]
    assert all(r["look_again_failed"] and r["stop"] == "answered" for r in c6)
    assert c6[0]["steps"][0]["result"]["code"] == "unreadable_scene"
    assert agent_run[2]["results"]["c6"]["look_again_failed"] == 2
    for name, stop in (("C7", "deadline"), ("C8", "bedrock_error"), ("C9", "malformed")):
        assert {(r["verdict"], r["stop"]) for r in cases[name]["runs"]} == {("artefact", stop)}, name
        assert all(not r["finding"] for r in cases[name]["runs"])
    assert {r["model_calls"] for r in cases["C7"]["runs"]} == {0}  # no model turn starts past the deadline
    assert cases["C8"]["converse_errors"] == ["ScriptedError", "ScriptedError"]


def test_the_networks_are_recorded_by_digest_and_change(agent_run):
    cases = agent_run[2]["results"]["cases"]
    b = cases["C1"]["networks"]["B"]
    assert b["change"]["construction"] == "vision_eval_driver.with_new_line"
    assert b["counts"]["stations"] == cases["C1"]["networks"]["A"]["counts"]["stations"] + 5
    assert len(b["sha256"]) == 64 and b["sha256"] != cases["C1"]["networks"]["A"]["sha256"]
    assert cases["C1d"]["networks"]["B"]["change"]["removed_stations"]
    assert cases["C2"]["networks"]["B'"]["change"]["recipe"] == "duplicate_stations"


def fake_bedrock(model_ids: list, refuse_all: bool = False):
    """Bedrock's Converse, faked: refuses an unknown model (or every call), else re-measures once and
    answers by the re-measurement, with token counts."""

    def converse(**request):
        model_ids.append(request["modelId"])
        if refuse_all or request["modelId"] == agent.INVALID_MODEL_ID:
            raise RuntimeError("ValidationException: the model id is invalid")
        turns = len([m for m in request["messages"] if m["role"] == "assistant"])
        script = agent.ScriptedConverse([*agent.COUNTERFACTUAL_SCRIPT][turns:], usage={"inputTokens": 3000,
                                                                                       "outputTokens": 120})
        return script(**request)

    return converse


def test_with_the_live_model_c8_is_asked_with_an_invalid_id_c9_stays_scripted_and_runs_are_priced(tmp_path):
    from common import costing, model_pricing

    model_id = next(iter(model_pricing.DEFAULT_MODEL_PRICES))
    asked = []
    argv = ["agent", "--synthetic", "--agent", "bedrock", "--model-id", model_id, "--repeats", "1",
            "--case", "C1", "--case", "C8", "--case", "C9"]  # fmt: skip
    code, run = run_main(argv, tmp_path, converse=fake_bedrock(asked))
    assert code == 0 and run["agent"]["mode"] == "bedrock" and run["agent"]["model_id"] == model_id
    cases = run["results"]["cases"]
    c1 = cases["C1"]["runs"][0]
    assert c1["verdict"] == "real" and c1["model_id"] == model_pricing.canonical_model_id(model_id)
    usd = costing.call_cost_usd({"input_tokens": 6000, "output_tokens": 240},
                                model_pricing.default_model_entry(model_id))  # fmt: skip
    assert c1["cost_usd"] == round(usd, 6) and c1["cost_aud"] == round(usd * costing.USD_TO_AUD_RATE, 6)
    assert run["summary"]["cost_usd_total"] == round(usd, 4)
    assert cases["C8"]["runs"][0]["stop"] == "bedrock_error"
    assert cases["C9"]["runs"][0]["stop"] == "malformed"  # its scripted answer, even on a live run
    assert asked == [model_id, model_id, agent.INVALID_MODEL_ID]  # C1's two turns, C8's one; C9 never
    assert run["run_id"].endswith("-bedrock-synthetic")


@pytest.mark.parametrize("arn, kept", [
    ("arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/au.anthropic.claude-haiku-4-5-20251001-v1:0",
     "au.anthropic.claude-haiku-4-5-20251001-v1:0"),
    ("arn:aws:bedrock:ap-southeast-2:123456789012:application-inference-profile/abc123", None),
])
def test_a_profile_arn_given_as_model_id_reaches_converse_but_never_the_run_file(tmp_path, arn, kept):
    """The deployed Lambdas' model is an ARN with the account id in it: Converse is given it as passed,
    and the run file (which is committed) keeps what the replay keeps, the profile's id or nothing."""
    asked = []
    argv = ["agent", "--synthetic", "--agent", "bedrock", "--model-id", arn, "--repeats", "1", "--case", "C1"]
    code, run = run_main(argv, tmp_path, converse=fake_bedrock(asked))
    assert code == 0 and asked and set(asked) == {arn}
    assert run["agent"]["model_id"] == kept and run["results"]["cases"]["C1"]["runs"][0]["model_id"] == kept
    written = "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file())
    assert "123456789012" not in written


def test_with_no_bedrock_access_every_run_is_bedrock_error_counted_and_said_never_dropped(tmp_path):
    argv = ["agent", "--synthetic", "--agent", "bedrock", "--model-id", "m", "--repeats", "2", "--case", "C1",
            "--case", "C2"]  # fmt: skip
    code, run = run_main(argv, tmp_path, converse=fake_bedrock([], refuse_all=True))
    assert code == 0  # both reached the agent; the agent failing closed is a result, not a broken run
    runs = [r for case in run["results"]["cases"].values() for r in case["runs"]]
    assert len(runs) == 4 and {r["stop"] for r in runs} == {"bedrock_error"}
    s = run["summary"]
    assert s["runs"] == 4 and s["failed_closed_task_runs"] == 4
    assert s["recall_real"] == 0.0 and s["recall_artefact"] == 0.0 and s["balanced_accuracy"] == 0.0
    matrix = run["results"]["scores"]["agent"]["confusion"]
    assert matrix["real"]["failed_closed"] == 2 and matrix["artefact"]["failed_closed"] == 2
    # Said in the printed table too: each case's runs in the failed-closed column.
    assert "| C1 | real | network | artefact | 2 | 0 | 0 | 2 |" in agent.agent_markdown(run)


def test_bedrock_without_a_model_id_and_a_bad_repeat_count_are_refused(tmp_path, capsys):
    assert agent.main(parse(["agent", "--synthetic", "--agent", "bedrock"]), vision_eval.load_manifest()) == 2
    assert "--model-id" in capsys.readouterr().err
    # The specification's command line names --model-id alone: never a scripted run under it, unsaid.
    for command in ("agent", "counterfactual"):
        argv = [command, "--synthetic", "--model-id", "some.model-v1:0", "--case", "C1", "--repeats", "1"]
        assert agent.main(parse(argv), vision_eval.load_manifest()) == 2
        assert "--agent bedrock" in capsys.readouterr().err
    assert agent.main(parse(["agent", "--synthetic", "--repeats", "0"]), vision_eval.load_manifest()) == 2
    assert agent.main(parse(["agent", "--synthetic", "--case", "C99"]), vision_eval.load_manifest()) == 2
    assert "no case named" in capsys.readouterr().err


def test_cases_are_named_by_case_repeated_or_by_cases_as_the_specification_writes_it():
    for argv in (["--case", "C1", "--case", "C9"], ["--cases", "C1", "C9"], ["--cases", "C1,C9"],
                 ["--cases", "C1,", "--case", "C9", "--case", "C1"]):  # fmt: skip
        assert agent._case_names(parse(["agent", *argv])) == ["C1", "C9"], argv
    assert agent._case_names(parse(["counterfactual"])) is None


def failing_newest(monkeypatch, refuse):
    """The worker refuses a tick's own measurement (never a `look_again`) of a scene captured on a
    date in `refuse` ({"YYYY-MM-DD": which calls of that scene, from 1, are refused}), with
    `worker_timeout`, as a deployed worker timing out on a cold read would."""
    from common import vision_client

    original, seen = driver.MeasureLog.tagged, {}

    def tagged(self, kind, measure):
        def call(site, scene, *args, **kwargs):
            day = str(scene.get("captured_at"))[:10]
            if kind == "tick" and day in refuse:
                seen[day] = seen.get(day, 0) + 1
                if refuse[day](seen[day]):
                    raise vision_client.VisionError("worker_timeout", "the test's worker times out")
            return measure(site, scene, *args, **kwargs)

        return original(self, kind, call)

    monkeypatch.setattr(driver.MeasureLog, "tagged", tagged)
    return seen


def test_a_newest_scene_the_worker_did_not_measure_is_neither_the_gates_result_nor_the_agents(
        tmp_path, monkeypatch):  # fmt: skip
    # C1's newest scene (31 August) times out on its second repeat; C3's (24 December) every time.
    failing_newest(monkeypatch, {"2024-08-31": lambda k: k == 2, "2024-12-24": lambda k: True})
    code, run = run_main(["agent", "--synthetic", "--repeats", "3", "--case", "C1", "--case", "C3"], tmp_path)
    assert code == 1  # a failed check, never a quiet 0
    cases = run["results"]["cases"]
    c1 = cases["C1"]["runs"]
    assert len(c1) == 3  # the timeout did not stop the repeats after it
    assert [(r["code"], r["error"], r["agent_called"]) for r in c1] == [
        ("reported", None, True), (None, "worker_timeout", False), ("reported", None, True)]
    assert cases["C1"]["included"] and cases["C1"]["gate"] == "reported"
    # C3 was never measured: not a gate that held, and not counted as one.
    assert cases["C3"]["gate"] is None and not cases["C3"]["included"] and len(cases["C3"]["runs"]) == 3
    s = run["summary"]
    assert (s["runs"], s["not_measured_runs"], s["gate_closed"]) == (6, 4, 0)
    # The agent answered every time it was asked: the timeouts are not its failures.
    assert s["recall_real"] == 1.0 and s["failed_closed_task_runs"] == 0
    matrix = run["results"]["scores"]["agent"]["confusion"]
    assert matrix["real"] == {"real": 2, "artefact": 0, "failed_closed": 0}
    checks = {c["name"]: c for c in run["checks"]}
    assert not checks["C1: newest scene measured"]["pass"] and checks["C1: newest scene measured"]["got"] == (
        "2 (worker_timeout)")
    assert not checks["C3: newest scene measured"]["pass"]
    # The printed table says the same: C1 two real, one not measured.
    assert "| C1 | real | network | reported | 3 | 2 | 0 | 0 | 1 |" in agent.agent_markdown(run)


def test_a_counterfactual_case_the_worker_did_not_measure_is_not_replaced_by_its_stand_in(
        tmp_path, monkeypatch):  # fmt: skip
    failing_newest(monkeypatch, {"2019-12-26": lambda k: True})  # C10's smoke scene
    code, run = run_main(["counterfactual", "--synthetic", "--repeats", "1", "--arms", "a"], tmp_path)
    cases = run["results"]["cases"]
    assert code == 1 and list(cases) == ["C1", "C10"]  # C4 did not stand in for a worker timeout
    assert cases["C10"]["gate"] is None and "replaced_by" not in cases["C10"]
    assert [c["name"] for c in run["checks"] if not c["pass"]] == ["C10: newest scene measured"]
    assert "| C10 | newest scene not measured |" in agent.counterfactual_markdown(run)


# --- the real city's refusals ------------------------------------------------------------------------------


def test_in_process_without_a_cache_off_an_us_west_2_instance_is_refused_naming_cache(monkeypatch, capsys):
    monkeypatch.setattr(vision_runs, "where", lambda **kwargs: "linux")
    code = agent.main(parse(["agent", "--worker", "in-process"]), vision_eval.load_manifest())
    assert code == 2 and "--cache" in capsys.readouterr().err
    # On an instance in us-west-2 it is not refused for that (it then needs fixture A).
    monkeypatch.setattr(vision_runs, "where", lambda **kwargs: "ec2")
    code = agent.main(parse(["agent", "--worker", "in-process", "--network", "missing.json"]),
                      vision_eval.load_manifest(), imds=lambda: {"region": "us-west-2"})  # fmt: skip
    err = capsys.readouterr().err
    assert code == 2 and "network_file missing" in err and "--cache" not in err


def test_arm_b_without_an_in_process_worker_is_refused_naming_cache_and_arms_a(monkeypatch, capsys):
    monkeypatch.setattr(vision_runs, "where", lambda **kwargs: "windows")
    arn = "arn:aws:lambda:us-west-2:000000000000:function:worker"
    code = agent.main(parse(["counterfactual", "--worker", arn]), vision_eval.load_manifest())
    err = capsys.readouterr().err
    assert code == 2 and "--cache" in err and "--arms a" in err


def test_a_missing_fixture_a_gives_the_network_file_missing_error(capsys):
    arn = "arn:aws:lambda:us-west-2:000000000000:function:worker"
    code = agent.main(parse(["agent", "--worker", arn, "--network", "no-such-network.json"]),
                      vision_eval.load_manifest())  # fmt: skip
    assert code == 2 and "network_file missing" in capsys.readouterr().err


def test_a_changed_etag_refuses_the_run_before_anything_is_measured(tmp_path, capsys):
    network = tmp_path / "sydney.json"
    network.write_text(json.dumps(driver.load_network_file(driver.SYNTHETIC_NETWORK_FILE)))
    arn = "arn:aws:lambda:us-west-2:000000000000:function:worker"

    def head(url):
        return {"etag": "not-the-pinned-one", "bytes": 1}

    class Client:
        def get_function_configuration(self, FunctionName):  # noqa: N803 - boto3's name
            return {"CodeSha256": "c" * 44}

    code = agent.main(parse(["agent", "--worker", arn, "--network", str(network), "--case", "C1"]),
                      vision_eval.load_manifest(), head=head, lambda_client=lambda arn: Client())  # fmt: skip
    assert code == 1 and "input changed" in capsys.readouterr().err


@pytest.fixture
def deployed(monkeypatch, tmp_path):
    """The deployed worker, scripted as Q2a's replay tests script it (every call recorded), the
    manifest's pinned ETags answered by a fake HEAD, and a small fixture A inside Sydney's box."""
    from common import vision_client

    import vision_benchmark

    manifest = vision_eval.load_manifest()
    pinned = vision_runs.pinned_etags(manifest)
    monkeypatch.setattr(vision_runs, "default_head", lambda url: {"etag": pinned[url], "bytes": 1})
    calls: list = []
    monkeypatch.setattr(vision_client, "measure", scripted_worker(calls))
    monkeypatch.setattr(vision_benchmark, "lambda_client", lambda arn: object())
    monkeypatch.setattr(vision_benchmark, "deployed_code_sha256", lambda client, arn: "deployed-code")
    stations = [{"id": f"n{i}", "name": f"S{i}", "kind": "station", "lon": 151.0 + 0.1 * i, "lat": -33.8}
                for i in range(5)]  # fmt: skip
    way = {"id": "w1", "points": [[151.0, -33.8], [151.4, -33.8]], "tunnel": False, "name": "L"}
    network = {"ways": [way], "stations": stations, "pois": [], "fetched_at": "2026-10-17T03:00:00+00:00"}
    path = tmp_path / "sydney.json"
    path.write_text(json.dumps(network), encoding="utf-8")
    return {"calls": calls, "network": path}


@pytest.mark.parametrize("argv", [
    ["agent", "--case", "C1", "--repeats", "1"],
    ["counterfactual", "--case", "C1", "--repeats", "1", "--arms", "a"],
    ["stability"],
])  # fmt: skip
def test_through_the_deployed_worker_every_site_id_is_eval_and_the_run_file_names_no_arn(deployed, tmp_path,
                                                                                         argv):  # fmt: skip
    code, run = run_main([*argv, "--worker", DEPLOYED_ARN, "--network", str(deployed["network"])],
                         tmp_path / "runs")  # fmt: skip
    assert code == 0 and deployed["calls"] and all(check["pass"] for check in run["checks"])
    # Every site id the deployed worker was sent says it is the evaluation's, never production's...
    assert {call["site"] for call in deployed["calls"]} == {"eval-sydney"}
    # ...and the run file names the worker's Region and code, never its ARN or the account.
    assert run["worker"] == {"kind": "deployed", "region": "us-west-2", "code_sha256": "deployed-code"}
    text = json.dumps(run)
    assert "arn:aws" not in text and "123456789012" not in text


def test_without_a_worker_the_real_city_is_refused(capsys):
    assert agent.main(parse(["agent"]), vision_eval.load_manifest()) == 2
    assert "--worker" in capsys.readouterr().err


# --- counterfactual -----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def counterfactual_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("counterfactual")
    with pytest.MonkeyPatch.context() as patch:
        guard(patch)
        code = vision_eval.main(["counterfactual", "--synthetic", "--repeats", "2", "--out", str(out)])
    return code, json.loads(next(out.glob("*.json")).read_text())


def test_only_the_re_measurement_differs_between_the_arms_and_the_verdict_follows_it(counterfactual_run):
    code, run = counterfactual_run
    assert code == 0 and run["kind"] == "eval-counterfactual" and all(c["pass"] for c in run["checks"])
    cases = run["results"]["cases"]
    # The synthetic city has no smoke: C10's gate holds against its clear baseline, and C4 stands in.
    assert list(cases) == ["C1", "C10", "C4"]
    assert cases["C10"]["replaced_by"] == "C4" and cases["C4"]["stands_in_for"] == "C10"
    c1 = cases["C1"]["agent"]
    assert "station_count" in c1["moved"]  # the new line's five stations
    measured, baseline = c1["arms"]["measured"], c1["arms"]["baseline"]
    assert [r["verdict"] for r in measured] == ["real", "real"]
    assert [r["verdict"] for r in baseline] == ["artefact", "artefact"]
    # The brief and the tick's own measurement are the same in both arms; only look_again differs.
    assert {json.dumps(r["measured"], sort_keys=True) for r in measured + baseline} == {
        json.dumps(measured[0]["measured"], sort_keys=True)}
    first = [r["steps"][0]["result"]["measured"] for r in (measured[0], baseline[0])]
    assert first[0]["station_count"] != first[1]["station_count"]
    assert first[1]["station_count"] == c1["baseline"]["station_count"]
    assert c1["score"]["pairs"] == 2 and c1["score"]["flips"] == 2
    s = run["summary"]
    assert cases["C4"]["agent"]["score"]["pairs"] == 2 and cases["C10"]["agent"]["score"] is None
    assert (s["agent_pairs"], s["agent_flips"], s["agent_called_look_again"]) == (4, 4, 8)


def test_the_gate_arm_calls_the_agent_above_the_floor_and_no_model_below_it(counterfactual_run):
    run = counterfactual_run[1]
    for name in ("C1", "C4"):
        gate = run["results"]["cases"][name]["gate_level"]
        above, below = gate["above"], gate["below"]
        assert above["coverage"] == pytest.approx(0.62, abs=1e-3)
        assert below["coverage"] == pytest.approx(0.58, abs=1e-3)
        assert above["agent_called"] and above["model_calls"] >= 1
        assert below["code"] == "below_floor" and not below["agent_called"] and below["model_calls"] == 0
        assert not below["finding"] and gate["flipped"] and gate["measured"]
    assert run["results"]["cases"]["C1"]["gate_level"]["above"]["finding"] is True
    # C4 stood in for C10, so the rule's two pairs are C1's and C4's: C10's arm (b) never ran, and
    # no model was asked for C10 at all (its gate held in arm (a)).
    assert "skipped" in run["results"]["cases"]["C10"]["gate_level"]
    assert not [r for r in run["agent"]["responses"] if r["case"] == "C10"]
    assert (run["summary"]["gate_pairs"], run["summary"]["gate_flips"]) == (2, 2)


def test_c4_stands_in_when_c10_does_not_pass_the_gate_and_its_gate_pair_replaces_c10s(tmp_path):
    code, run = run_main(["counterfactual", "--synthetic", "--repeats", "1", "--cases", "C10,C4"], tmp_path)
    cases = run["results"]["cases"]
    assert cases["C10"]["replaced_by"] == "C4" and not cases["C10"]["included"]
    assert cases["C4"]["stands_in_for"] == "C10" and cases["C4"]["included"]
    assert "skipped" in cases["C10"]["gate_level"] and cases["C4"]["gate_level"]["measured"]
    assert run["summary"]["gate_pairs"] == 1 and code == 0  # one pair for the two, never two


def test_the_stand_in_named_alone_runs_as_its_own_case(tmp_path):
    code, run = run_main(["counterfactual", "--synthetic", "--repeats", "1", "--case", "C4", "--arms", "a"],
                         tmp_path)  # fmt: skip
    cases = run["results"]["cases"]
    assert code == 0 and list(cases) == ["C4"] and "stands_in_for" not in cases["C4"]
    assert cases["C4"]["agent"]["score"]["pairs"] == 1 and run["summary"]["agent_pairs"] == 1


def test_a_gate_pair_not_measured_at_its_coverage_fails_its_check_and_is_not_counted(tmp_path, monkeypatch):
    # A scene already under the target cannot be clouded to it: that side is not measured, and the
    # pair says nothing about the gate (it once passed as "the gate did not flip").
    monkeypatch.setattr(agent, "GATE_COVERAGES", {"above": 1.01, "below": 1.005})
    code, run = run_main(["counterfactual", "--synthetic", "--repeats", "1", "--case", "C1", "--arms", "b"],
                         tmp_path)  # fmt: skip
    gate = run["results"]["cases"]["C1"]["gate_level"]
    assert code == 1 and not gate["measured"]
    for side in ("above", "below"):
        assert not gate[side]["measured"] and not gate[side]["at_target"]
        assert "already under" in gate[side]["error"] and not gate[side]["agent_called"]
    checks = {c["name"]: c["pass"] for c in run["checks"]}
    assert checks == {"C1: measured either side of the floor": False, "C1: no model below the floor": True}
    assert (run["summary"]["gate_pairs"], run["summary"]["gate_flips"]) == (0, 0)


# --- stability (E3) -----------------------------------------------------------------------------------------


def test_the_synthetic_year_through_the_production_rule(tmp_path):
    code, run = run_main(["stability", "--synthetic", "--scenes", "4"], tmp_path)
    assert code == 0 and run["kind"] == "eval-stability" and run["agent"] is None
    s = run["summary"]
    assert list(s) == ["decisions", "false_material", "false_material_rate", "wilson_low", "wilson_high",
                       "served_share_spread", "desert_rel_spread", "built_up_rel_spread", "mask_iou_min",
                       "hub_jaccard_min", "flag_jaccard_min"]  # fmt: skip
    assert (s["decisions"], s["false_material"], s["wilson_low"]) == (3, 0, 0.0)
    assert s["wilson_high"] == scoring.wilson(0, 3)[1] and s["mask_iou_min"] == 1.0
    rows = run["results"]["scenes"]
    assert [r["code"] for r in rows] == ["first_observation"] + ["within_range"] * 3
    assert rows[0]["iou_previous"] is None and rows[1]["iou_first"] == 1.0
    assert run["params"]["triage"] is False and run["params"]["history_size"] == 6


def test_a_cloud_moves_the_measures_and_is_counted_while_a_scene_below_the_floor_is_no_decision():
    entries = {e["name"]: e for e in agent.synthetic_year_entries(5)}
    names = list(entries)
    entries[names[2]] = {**entries[names[2]], "cloud": 0.7}  # below the floor: not a decision
    entries[names[3]] = {**entries[names[3]], "cloud": 0.3}  # the west under cloud: the measures move
    setup = agent.synthetic_setup(entries, driver.load_network_file(driver.SYNTHETIC_NETWORK_FILE))
    results, checks, summary = agent.run_stability(setup, names, capture=True)
    codes = [row["code"] for row in results["scenes"]]
    assert codes[0] == "first_observation" and codes[2] == "below_floor"
    assert summary["decisions"] == 3 and summary["false_material"] == codes.count("reported") >= 1
    assert results["false_alarms"]["by_month"] and all(c["pass"] for c in checks)
    clouded = results["scenes"][3]
    # The IoU is over ground both scenes saw: what the cloud hid does not count against it.
    assert clouded["iou_previous"] is not None and clouded["iou_previous"] <= 1.0
    assert summary["served_share_spread"] > 0


def test_stability_on_the_real_city_names_a_set_the_manifest_holds(capsys):
    assert agent.main(parse(["stability", "--set", "year-1999"]), vision_eval.load_manifest()) == 2
    assert "no set 'year-1999'" in capsys.readouterr().err


# --- the command line --------------------------------------------------------------------------------------


def test_the_index_takes_the_three_kinds_run_files(agent_run, counterfactual_run, tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    for run in (agent_run[2], counterfactual_run[1]):
        vision_runs.write(run, runs)
    code, stability = run_main(["stability", "--synthetic", "--scenes", "3"], runs)
    assert code == 0 and stability["kind"] == "eval-stability"
    assert vision_eval.main(["index", "--evidence", str(tmp_path)]) == 0
    rows = json.loads((tmp_path / "index.json").read_text())
    rows = rows["runs"] if isinstance(rows, dict) else rows
    assert {row["kind"] for row in rows} == {"eval-agent", "eval-counterfactual", "eval-stability"}
    text = (tmp_path / "RESULTS.md").read_text()
    for kind in ("eval-agent", "eval-counterfactual", "eval-stability"):
        assert f"## {kind} (1 run)" in text and vision_eval.KIND_WORDS[kind] in text


def test_the_three_subcommands_are_vision_evals_and_their_kinds_are_known(capsys):
    assert {"eval-agent", "eval-counterfactual", "eval-stability"} <= set(vision_runs.KINDS)
    for command in agent.COMMANDS:
        with pytest.raises(SystemExit) as done:
            vision_eval.main([command, "--help"])
        assert done.value.code == 0
        assert "--synthetic" in capsys.readouterr().out


def test_building_the_parser_never_imports_the_driver():
    # Q2a's rule: `pin` and `coverage` run in a venv with the worker's requirements only (no boto3),
    # so the parser the three subcommands join must not import vision_eval_agent (and so the driver).
    # A fresh interpreter, so earlier imports do not count; subprocess is not the network.
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; sys.path[:0] = sys.argv[1:3]; import vision_eval\n"
        "for argv in (['coverage', '--help'], ['agent', '--help']):\n"
        "    try:\n"
        "        vision_eval.main(argv)\n"
        "    except SystemExit:\n"
        "        pass\n"
        "loaded = {'vision_eval_agent', 'vision_eval_driver', 'boto3'} & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )
    done = subprocess.run([sys.executable, "-c", code, str(root / "scripts"), str(root / "lambdas")],
                          capture_output=True, text=True, timeout=120)  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert "--repeats" in done.stdout
    assert agent.COMMANDS == vision_eval_agent_cli.COMMANDS
    assert agent.add_parsers is vision_eval_agent_cli.add_parsers
