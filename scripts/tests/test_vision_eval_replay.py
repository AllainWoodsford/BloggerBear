"""Tests for `scripts/vision_eval.py replay` (2.6.4): the real adapter over a pinned sequence.

CI replays the synthetic city with the real worker handler in this process (scripted agent), and
re-runs the committed recording (`scripts/tests/fixtures/replay-synthetic-city-scripted.json`, a
scripted run written in the recorded format) with `--agent recorded`, which must reproduce it. The
Sydney sequences run with a scripted worker. requests, boto3 and socket connections raise.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

import vision_eval
import vision_eval_driver as driver
import vision_runs

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
RECORDING = FIXTURES / "replay-synthetic-city-scripted.json"
ARN = "arn:aws:lambda:us-west-2:123456789012:function:bloggerbear-dev-vision-worker"


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


def only_run(directory: Path) -> dict:
    files = sorted(directory.glob("*.json"))
    assert len(files) == 1, files
    return vision_runs.load(files[0])


# --- the synthetic city, scripted -------------------------------------------------------------------------


def test_a_scripted_replay_of_the_synthetic_city_writes_the_run_file_and_the_trace(tmp_path, capsys):
    out, trace = tmp_path / "runs", tmp_path / "replay" / "synthetic-city.md"
    code = vision_eval.main(
        ["replay", "--sequence", "synthetic-city", "--out", str(out), "--trace", str(trace)]
    )
    assert code == 0
    run = only_run(out)
    assert run["kind"] == "eval-replay" and run["mode"] == "replay" and run["backend"] == "opencv"
    assert re.search(r"-eval-replay-.+-opencv-synthetic-city-scripted$", run["run_id"])
    assert run["summary"] == {
        "steps": 4,
        "decisions": 4,
        "triaged": 2,
        "look_again": 2,
        "verdict_real": 1,
        "verdict_artefact": 1,
        "finding": 2,
    }
    ticks = run["results"]["ticks"]
    assert [t["codes"] for t in ticks] == [["first_observation"], ["below_floor"], ["artefact"], ["reported"]]
    assert run["results"]["ring"] == [e for t in ticks for e in t["decisions"]]
    assert run["results"]["excluded"] == list(driver.EXCLUDED) and run["checks"] == []
    assert run["agent"]["mode"] == "scripted" and run["agent"]["model_id"] == "scripted"
    assert [r["tick"] for r in run["agent"]["responses"]] == [3, 4]
    assert run["params"]["scripted"] == {
        "tools": ["look_again", "web_context"],
        "look_again": {"reach_m": 800},
        "rule": driver.SCRIPTED_RULE,
    }
    assert run["worker"]["kind"] == "in-process" and run["worker"]["code_sha256"]
    assert run["inputs"]["networks"][0]["file"] == "scripts/tests/fixtures/synthetic-city-network.json"
    assert run["inputs"]["networks"][1]["derived"].startswith("A with a five-station line")
    # Each tick's figure is written beside the run file and named in its tick.
    assert all((out / t["figure"]).is_file() for t in ticks)
    text = trace.read_text(encoding="utf-8")
    assert text.startswith("# Replay: synthetic-city") and f"runs/{run['run_id']}.json" in text
    assert "**Thresholds of 0 (the demo topic's), disclosed:**" in text and "**Synthetic:**" in text
    assert "a Finding would be written (a replay)" in text and "too cloudy to count" in text
    assert "re-measured the same scene at other settings" in text and "reach 800 m" in text
    assert "| Step | What happened | Numbers | What OpenCV's output did | Time |" in text
    assert "1 Finding(s)" not in capsys.readouterr().out


def test_limit_replays_the_first_ticks_only(tmp_path):
    assert (
        vision_eval.main(["replay", "--sequence", "synthetic-city", "--limit", "2", "--out", str(tmp_path)])
        == 0
    )
    run = only_run(tmp_path)
    assert run["summary"]["steps"] == 2 and len(run["inputs"]["networks"]) == 1


# --- the committed recording, replayed ----------------------------------------------------------------------


def test_the_committed_recording_replays_exactly_without_aws(tmp_path):
    recording = vision_runs.load(RECORDING)
    assert recording["kind"] == "eval-replay" and recording["agent"]["mode"] == "scripted"
    assert recording["params"]["sequence"] == "synthetic-city" and recording["summary"]["triaged"] == 2
    code = vision_eval.main(
        [
            "replay",
            "--sequence",
            "synthetic-city",
            "--agent",
            "recorded",
            "--from",
            str(RECORDING),
            "--out",
            str(tmp_path),
            "--no-figures",
        ]
    )
    run = only_run(tmp_path)
    assert code == 0 and run["checks"] and all(check["pass"] for check in run["checks"]), run["checks"]
    assert run["params"]["from"] == recording["run_id"] and run["agent"]["mode"] == "recorded"
    assert run["results"]["parity"]["within_tolerance"] == run["results"]["parity"]["calls"] == 6
    assert [t["at"] for t in run["results"]["ticks"]] == [t["at"] for t in recording["results"]["ticks"]]
    assert run["agent"]["responses"] == recording["agent"]["responses"]


def recording_copy(tmp_path: Path, change) -> Path:
    data = json.loads(RECORDING.read_text(encoding="utf-8"))
    change(data)
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_recording_that_no_longer_fits_the_profile_is_refused_naming_the_step(tmp_path, capsys):
    def rename_tool(data):
        data["agent"]["responses"][0]["turns"][0]["output"]["message"]["content"][0]["toolUse"]["name"] = (
            "street_view"
        )

    path = recording_copy(tmp_path, rename_tool)
    code = vision_eval.main(
        ["replay", "--sequence", "synthetic-city", "--agent", "recorded", "--from", str(path)]
    )
    assert code == vision_eval.REFUSED
    assert (
        "tick 3, site eval-synthetic, step 1: the recorded response calls 'street_view'"
        in capsys.readouterr().err
    )


def test_a_recorded_number_the_replay_does_not_reproduce_fails_its_check(tmp_path, capsys):
    def move(data):
        data["results"]["ticks"][2]["decisions"][0]["measure"]["served_share"] += 0.01

    path = recording_copy(tmp_path, move)
    out = tmp_path / "out"
    code = vision_eval.main(
        [
            "replay",
            "--sequence",
            "synthetic-city",
            "--agent",
            "recorded",
            "--from",
            str(path),
            "--out",
            str(out),
            "--no-figures",
        ]
    )
    assert code == 1
    failed = [check for check in only_run(out)["checks"] if not check["pass"]]
    assert [check["name"] for check in failed] == ["tick 3 (synthetic-3): the ring equals the recording"]
    assert failed[0]["got"].startswith("measure.served_share")
    assert "FAIL tick 3" in capsys.readouterr().err


def test_a_recorded_replay_refuses_another_network_or_another_sequence(tmp_path, capsys):
    other = tmp_path / "network.json"
    network = json.loads(driver.SYNTHETIC_NETWORK_FILE.read_text(encoding="utf-8"))
    network["stations"] = network["stations"][:-1]
    other.write_text(json.dumps(network), encoding="utf-8")
    code = vision_eval.main(
        [
            "replay",
            "--sequence",
            "synthetic-city",
            "--agent",
            "recorded",
            "--from",
            str(RECORDING),
            "--network",
            str(other),
        ]
    )
    assert code == vision_eval.REFUSED and "input changed: network A" in capsys.readouterr().err
    path = recording_copy(tmp_path, lambda data: data["params"].update(sequence="sydney-demo"))
    code = vision_eval.main(
        ["replay", "--sequence", "synthetic-city", "--agent", "recorded", "--from", str(path)]
    )
    assert code == vision_eval.REFUSED and "is not a replay of synthetic-city" in capsys.readouterr().err


def test_usage_is_refused_before_anything_runs(tmp_path, capsys, monkeypatch):
    cases = [
        (["--agent", "recorded"], "needs --from"),
        (["--agent", "recorded", "--from", str(RECORDING), "--worker", ARN], "re-measures in-process"),
        (["--agent", "bedrock"], "needs --model-id"),
        (["--backend", "cool"], "needs --cool-sha"),
        (["--worker", ARN], "exists only in this process"),
    ]
    for extra, said in cases:
        assert vision_eval.main(["replay", "--sequence", "synthetic-city", *extra]) == vision_eval.REFUSED
        assert said in capsys.readouterr().err
    # 2.6.2's rule: an in-process worker under the production deadline needs the cache off us-west-2.
    assert vision_eval.in_imagery_region({"region": "us-west-2"}) and not vision_eval.in_imagery_region({})
    assert vision_eval.in_process_refusal(None, synthetic=True) is None
    assert vision_eval.in_process_refusal(tmp_path, synthetic=False) is None
    assert vision_eval.in_process_refusal(None, False, identity={"region": "us-west-2"}) is None
    assert "--cache DIR" in vision_eval.in_process_refusal(None, False, identity={"region": "eu-west-1"})
    monkeypatch.setattr(vision_eval, "in_imagery_region", lambda identity=None: False)
    code = vision_eval.main(["replay", "--sequence", "sydney-demo", "--agent", "bedrock", "--model-id", "m"])
    assert code == vision_eval.REFUSED and "needs --cache DIR" in capsys.readouterr().err
    code = vision_eval.main(
        ["replay", "--sequence", "sydney-winter", "--network", str(tmp_path / "none.json")]
    )
    assert code == vision_eval.REFUSED and "network_file missing" in capsys.readouterr().err


# --- the Sydney sequences, with a scripted worker ---------------------------------------------------


def scripted_worker(calls: list):
    """The deployed worker, scripted: stations counted from the network it is sent, the served share
    up a little with each one, and the largest desert in the middle of Sydney's box."""

    def measure(
        site,
        scene,
        backend="opencv",
        params=None,
        coverage_floor=0.6,
        image=True,
        client=None,
        task="rail_access",
        network=None,
        read_timeout=None,
    ):
        calls.append(
            {"site": site["id"], "scene": scene["id"], "params": params, "read_timeout": read_timeout}
        )
        stations = len((network or {}).get("stations") or [])
        reach = (params or {}).get("reach_m", 1000)
        share = round(0.40 + 0.01 * stations * reach / 1000.0, 4)
        metrics = {
            "task": "rail_access",
            "coverage": 0.99,
            "served_share": share,
            "built_up_km2": 900.0,
            "desert_km2": round(900.0 * (1 - share), 4),
            "station_count": stations,
            "flags": [],
            "quality_flags": [],
            "deserts": [{"lon": 151.2, "lat": -33.87, "area_km2": 40.0}],
            "params": {"reach_m": reach},
        }
        reply = {
            "version": 1,
            "ok": True,
            "task": task,
            "backend": backend,
            "site_id": site["id"],
            "scene_id": scene["id"],
            "captured_at": scene.get("captured_at"),
            "metrics": metrics,
            "build": {"build_sha256": "ab" * 32, "cpu": "Neoverse V2 (Graviton4)"},
        }
        from common import vision_client

        return vision_client.VisionResult(reply=reply, image_png=None, request_id=f"req-{len(calls)}")

    return measure


@pytest.fixture
def sydney(monkeypatch, tmp_path):
    """The manifest's Sydney scenes with their pinned ETags answered by a fake HEAD, the deployed
    worker scripted, and a small network fixture inside Sydney's box."""
    monkeypatch.setattr(vision_runs, "default_head", lambda url: {"etag": pinned[url], "bytes": 1})
    manifest = vision_eval.load_manifest()
    pinned = vision_runs.pinned_etags(manifest)
    calls = []
    from common import vision_client

    monkeypatch.setattr(vision_client, "measure", scripted_worker(calls))
    import vision_benchmark

    monkeypatch.setattr(vision_benchmark, "lambda_client", lambda arn: object())
    monkeypatch.setattr(vision_benchmark, "deployed_code_sha256", lambda client, arn: "deployed-code")
    network = {
        "ways": [{"id": "w1", "points": [[151.0, -33.8], [151.4, -33.8]], "tunnel": False, "name": "L"}],
        "stations": [
            {"id": f"n{i}", "name": f"S{i}", "kind": "station", "lon": 151.0 + 0.1 * i, "lat": -33.8}
            for i in range(5)
        ],
        "pois": [],
        "fetched_at": "2026-10-17T03:00:00+00:00",
    }
    path = tmp_path / "networks" / "sydney.json"
    path.parent.mkdir()
    path.write_text(json.dumps(network), encoding="utf-8")
    return {"calls": calls, "network": path, "manifest": manifest}


def test_sydney_c1_through_the_deployed_worker_reports_the_new_line_and_names_no_arn(sydney, tmp_path):
    out = tmp_path / "runs"
    code = vision_eval.main(
        [
            "replay",
            "--sequence",
            "sydney-c1",
            "--worker",
            ARN,
            "--network",
            str(sydney["network"]),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    run = only_run(out)
    ticks = run["results"]["ticks"]
    assert [t["scene"] for t in ticks] == list(driver.WINTER) and [t["network"] for t in ticks] == list(
        "AAAB"
    )
    assert [t["codes"] for t in ticks] == [
        ["first_observation"],
        ["within_range"],
        ["within_range"],
        ["reported"],
    ]
    last = ticks[-1]["decisions"][0]
    assert {"served_share", "station_count"} <= set(last["moved"]) and last["network_changed"] is True
    assert last["triage"]["verdict"] == "real" and last["triage"]["hosts"] == ["example.org"]
    assert [s["tool"] for s in last["triage"]["steps"]] == ["look_again", "web_context"]
    assert last["triage"]["steps"][1]["result"] == {"results": 1, "dated": 1}
    # Every site id the worker was sent starts with eval-; the run names the Region, never the ARN.
    assert {call["site"] for call in sydney["calls"]} == {"eval-sydney"}
    assert all(call["read_timeout"] for call in sydney["calls"])
    assert run["worker"] == {"kind": "deployed", "region": "us-west-2", "code_sha256": "deployed-code"}
    text = json.dumps(run)
    assert "123456789012" not in text and "arn:aws" not in text
    assert run["inputs"]["manifest_sha256"] == vision_runs.sha256_file(vision_eval.MANIFEST_FILE)
    scene = run["inputs"]["scenes"][0]
    assert scene["name"] == "sydney-2024-06-27" and set(scene["etag_by_asset"]) == {
        "red",
        "nir",
        "swir16",
        "scl",
    }
    assert run["results"]["sequence"]["news"][0]["title"].startswith("Evaluation case C1 (constructed)")


def test_sydney_demo_sends_every_scene_after_the_first_to_the_agent(sydney, tmp_path):
    trace = tmp_path / "sydney-demo.md"
    code = vision_eval.main(
        [
            "replay",
            "--sequence",
            "sydney-demo",
            "--worker",
            ARN,
            "--network",
            str(sydney["network"]),
            "--out",
            str(tmp_path),
            "--trace",
            str(trace),
        ]
    )
    assert code == 0
    run = only_run(tmp_path)
    assert [t["codes"] for t in run["results"]["ticks"]] == [
        ["first_observation"],
        ["artefact"],
        ["artefact"],
        ["artefact"],
    ]
    assert run["params"]["thresholds"] == "demo" and run["summary"]["look_again"] == 3
    assert "Contains modified Copernicus Sentinel data" in trace.read_text(encoding="utf-8")


def test_a_pinned_etag_that_moved_refuses_the_replay_before_any_read(sydney, monkeypatch, capsys):
    monkeypatch.setattr(vision_runs, "default_head", lambda url: {"etag": "moved", "bytes": 1})
    code = vision_eval.main(
        ["replay", "--sequence", "sydney-winter", "--worker", ARN, "--network", str(sydney["network"])]
    )
    assert code == vision_eval.REFUSED and "input changed" in capsys.readouterr().err
    assert sydney["calls"] == []


def test_the_bedrock_agent_records_each_response_without_its_metadata():
    class Client:
        def __init__(self):
            self.calls = []

        def converse(self, **request):
            self.calls.append(request)
            return {
                "output": {"message": {"role": "assistant", "content": [{"text": '{"verdict": "real"}'}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 2},
                "ResponseMetadata": {"RequestId": "r", "HTTPHeaders": {"date": "x"}},
            }

    client = Client()
    agent = driver.BedrockAgent("au.anthropic.claude-haiku-4-5-20251001-v1:0", client=client)
    converse = agent.converse_for(2, "eval-sydney")
    reply = converse(modelId=agent.model_id, messages=[])
    assert reply["ResponseMetadata"]["RequestId"] == "r" and client.calls
    assert converse.responses == [
        {
            "output": copy.deepcopy(reply["output"]),
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 2},
        }
    ]
    spend = vision_eval.agent_spend(
        [{"decisions": [{"triage": {"input_tokens": 1000, "output_tokens": 100}}]}], agent.model_id
    )
    assert spend["cost_usd"] and spend["cost_aud"] > spend["cost_usd"]


def test_pin_and_coverage_never_import_the_driver_and_the_sequence_names_agree():
    # The Graviton session's stock venv has the worker's requirements only (no boto3): `pin` and
    # `coverage` must not reach the adapter. A fresh interpreter, so earlier imports do not count.
    import subprocess
    import sys

    code = (
        "import sys; sys.path[:0] = sys.argv[1:3]; import vision_eval; "
        "assert 'vision_eval_driver' not in sys.modules and 'boto3' not in sys.modules, sorted(sys.modules)"
    )
    done = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts"), str(ROOT / "lambdas")],
                          capture_output=True, text=True, timeout=120)  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert set(vision_eval.SEQUENCE_NAMES) == set(driver.SEQUENCES)
    assert vision_eval.main(["replay", "--sequence", "sydney-spring"]) == vision_eval.REFUSED
