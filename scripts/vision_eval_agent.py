"""The agent's evaluation, its counterfactuals and the stability run: `vision_eval.py agent`,
`counterfactual` and `stability` (the specification's 2.6.1-2.6.3, 2.6.6 and E3).

**The cases** are scripts/vision_eval_cases/<case>/case.json (`load_cases`): the history (manifest
scenes, each with the network it was measured with), the newest scene, the thresholds, the news
`web_context` replays (null: the tool withheld), the expected verdict and why, and which input moved
(`driven_by`). A case may build on another (`base`). Fixture A is the city's network file; every other
network is A with one change made by code (scripts/vision_eval_networks.py), anchored where it
asks on the deserts of the baseline's newest measurement, so the cases read the same on any copy of
A. C1-C5 and C10 are the task cases, scored per class; C6-C9 the failure cases.

**`agent`** builds each case through the real RailAccessAdapter with Q2a's driver
(scripts/vision_eval_driver.py): the history ticks with triage off (their measurements kept by what
was asked, so the scenes several cases share are measured once), then the newest tick `--repeats`
times from the same state with the real common/vision_triage.triage, its converse the live model
(`--agent bedrock --model-id`, the operator's credentials) or the case's script (`--agent
scripted`, and always for C7 and C9), its `look_again` the same worker as the tick (C6: a worker
that refuses with `unreadable_scene`), its news the case's, its figure from the run's local store,
and its deadline production's: 85 s from the tick's start on the real clock (C7: a clock moved past
it). The worker is the deployed one (`--worker <ARN>`, every site id `eval-`) or this process
(`--worker in-process`), which is refused without `--cache` off an EC2 instance in us-west-2
(`in_process_refusal`): an uncached re-measurement would run the cases out of time for the
harness's reasons. A case whose newest scene does not pass the gate never reaches the agent; it is
reported as such and left out of the scores (C3 and C10 are included only if they pass). A newest
scene the worker did not measure is neither the gate's result nor the agent's: that repeat keeps
the worker's code, is scored nowhere, and fails the run's check, and the next repeat runs. Every
Converse response is kept in the run file (`agent.responses`, the driver's recorded form), with each
run's verdict, stop, steps, reason, time, tokens and cost in USD and AUD (the built-in price table;
the deployed Models table is not read from an operator's machine).

**`counterfactual`** (2.6.6), for C1 and C10 (C4 when C10 does not pass the gate). (a) The agent
level: the newest tick's repeats in two arms that differ only in what `look_again` answers: the
newest scene's own measurement at the requested settings (`measured`), or the same with the moved
measures set back to the baseline median (`baseline`, a constructed tool output); repeat k of one
arm is paired with repeat k of the other. (b) The gate level, in this process: the newest scene
with SCL clouded inside the site until the worker's coverage is 0.62, then 0.58, either side of the
0.6 floor (`clouded_to`), all else equal: the agent is called or not, a Finding would be written or
not; no model is asked below the floor. A pair counts only when both sides were measured at their
coverage; when C4 stands in, arm (b) runs for C4 alone.

**`stability`** (E3): the year set (the manifest's `sets`, in time order) through the production
rule, triage off, the default thresholds, `history_size` 6, one network throughout: every material
verdict is a false alarm. The rate over the decisions with its Wilson 95 % interval, by month, and
the spreads: served share, desert and built-up area, the built-up mask's IoU scene to scene and to
the first (from the worker's own masks, in this process), and the Jaccard of hubs and of flags.

`--synthetic` runs any of the three on the driver's synthetic city (the cases' scenes mapped onto
synthetic scenes, fixture A the synthetic network), in this process, with no network: what CI runs,
with the scripted model. Every run writes one run file (scripts/vision_runs.py), kind
`eval-agent`, `eval-counterfactual` or `eval-stability`. Runs on Windows as well as Linux and macOS.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import vision_eval_agent_cli as cli
import vision_eval_driver as driver
import vision_eval_networks as networks
import vision_eval_scoring as scoring
import vision_runs

SCRIPTS = Path(__file__).resolve().parent
CASES_DIR = SCRIPTS / "vision_eval_cases"
CASE_SCHEMA = "bloggerbear.vision-case/1"
CASE_KINDS = ("task", "failure")
VERDICTS = ("real", "artefact")
# How the triage loop may stop (common/vision_triage.STOPS, repeated so loading a case needs no
# pipeline import); a case's expected stop is one of them.
STOPS = ("answered", "malformed", "bedrock_error", "deadline", "turn_limit", "no_profile", "no_model")
DRIVEN_BY = ("imagery", "network")
# A case included only when its newest scene passes the gate (the thresholds make it material).
CONDITIONS = ("gate",)
# The topic's thresholds a case may set (common/adapters/rail_access.THRESHOLD_KEYS).
THRESHOLD_KEYS = ("coverage_floor", "min_baseline", "served_share_threshold", "desert_relative_threshold")
NEWS_KEYS = ("title", "url", "published_at", "source", "snippet")
# What a case may ask of a live run: the model or its script, a look_again that fails with a
# worker code, a clock already past the deadline, an invalid model id.
RUN_KEYS = {
    "converse": ("model", "scripted"),
    "look_again": ("unreadable_scene",),
    "clock": ("past_deadline",),
    "model_id": ("invalid",),
}
SCRIPT_TURNS = ("tool", "text", "raise", "decide_by")
TOOLS = ("look_again", "previous_scene", "site_history", "web_context")
CASE_NAME = re.compile(r"^C\d+[a-z]?$")
FIELDS = (
    "schema", "case", "kind", "title", "built_as", "site", "history", "newest", "networks", "thresholds",
    "news", "expected", "why", "driven_by", "conditional", "run", "scripted",
)  # fmt: skip


class CaseError(ValueError):
    """A case file that is not one: refused, naming the case and what is wrong."""


# --- the cases ---------------------------------------------------------------------------------------


def case_order(name: str) -> tuple:
    """C1, C1c, C1d, C1e, C2, ..., C10: by number, then by letter."""
    match = re.match(r"^C(\d+)([a-z]?)$", name)
    return (int(match.group(1)), match.group(2)) if match else (10**6, name)


def read_case_files(directory: Path = CASES_DIR) -> dict[str, dict]:
    """{case name: its case.json as written}, one per sub-directory holding one."""
    out = {}
    for path in sorted(Path(directory).glob("*/case.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CaseError(f"{path}: not JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise CaseError(f"{path}: not an object")
        if raw.get("case") != path.parent.name:
            raise CaseError(f"{path}: its case is {raw.get('case')!r}, not its directory's "
                            f"{path.parent.name!r}")  # fmt: skip
        out[path.parent.name] = raw
    return out


def resolve(raw: dict[str, dict], name: str, seen: tuple = ()) -> dict:
    """Case `name` with its `base` chain filled in: every field the case does not set is its base's
    (one base may have its own); `base` itself is kept, so the run file says what it was built on."""
    if name in seen:
        raise CaseError(f"{name}: its base chain loops ({' -> '.join((*seen, name))})")
    if name not in raw:
        raise CaseError(f"{seen[-1] if seen else name}: no case {name!r} to build on")
    case = raw[name]
    base_name = case.get("base")
    if base_name is None:
        return copy.deepcopy(case)
    base = resolve(raw, base_name, (*seen, name))
    merged = {**base, **copy.deepcopy(case)}
    merged.pop("base", None)
    merged["base"] = base_name
    return merged


def validate_case(case: dict, manifest: dict) -> dict:
    """`case` when it holds every field in its form and names only scenes the manifest pins;
    CaseError naming the case and the field otherwise."""
    name = case.get("case")

    def fail(message: str):
        raise CaseError(f"{name}: {message}")

    if case.get("schema") != CASE_SCHEMA:
        fail(f"schema must be {CASE_SCHEMA!r}")
    if not isinstance(name, str) or not CASE_NAME.match(name):
        fail("case must be C<number> with an optional letter")
    missing = [key for key in FIELDS if key not in case]
    if missing:
        fail(f"missing {missing}")
    if case["kind"] not in CASE_KINDS:
        fail(f"kind must be one of {CASE_KINDS}")
    for key in ("title", "built_as", "why"):
        if not isinstance(case[key], str) or not case[key].strip():
            fail(f"{key} must be a sentence")
    if case["site"] not in (manifest.get("sites") or {}):
        fail(f"site {case['site']!r} is not in the manifest")
    scenes = {scene["name"]: scene for scene in manifest.get("scenes") or []}
    nets = case["networks"]
    if not isinstance(nets, dict) or not nets:
        fail("networks must name at least one network")
    steps = case["history"] + [case["newest"]] if isinstance(case["history"], list) else None
    if not steps or not isinstance(case["newest"], dict):
        fail("history must be a list of scenes and newest one scene")
    for step in steps:
        if not isinstance(step, dict) or step.get("scene") not in scenes:
            fail(f"scene {step.get('scene') if isinstance(step, dict) else step!r} is not in the manifest")
        if scenes[step["scene"]].get("site") != case["site"]:
            fail(f"scene {step['scene']!r} is not of site {case['site']!r}")
        if step.get("network") not in nets:
            fail(f"scene {step['scene']!r} names network {step.get('network')!r}, not in networks")
    for step in case["history"]:
        if "file" not in (nets.get(step["network"]) or {}):
            fail(f"history scene {step['scene']!r} must be measured with a file network (fixture A): a "
                 "derived network is anchored on the history's own measurement")
    if len({step["scene"] for step in steps}) != len(steps):
        fail("a scene appears twice in history and newest")
    for key, spec in nets.items():
        _validate_network(fail, key, spec, nets)
    thresholds = case["thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) - set(THRESHOLD_KEYS):
        fail(f"thresholds may set only {THRESHOLD_KEYS}")
    if case["news"] is not None:
        if not isinstance(case["news"], list):
            fail("news must be a list of headlines, or null for the tool withheld")
        for item in case["news"]:
            if not isinstance(item, dict) or set(item) != set(NEWS_KEYS):
                fail(f"each headline holds exactly {NEWS_KEYS}")
    _validate_expected(fail, case)
    if case["driven_by"] not in (*DRIVEN_BY, None):
        fail(f"driven_by must be one of {DRIVEN_BY} or null")
    if case["kind"] == "task" and case["driven_by"] is None:
        fail("a task case says which input moved (driven_by)")
    if case["conditional"] not in (*CONDITIONS, None):
        fail(f"conditional must be one of {CONDITIONS} or null")
    run = case["run"]
    if not isinstance(run, dict) or "converse" not in run:
        fail("run must say how the model is asked (converse)")
    for key, value in run.items():
        if key not in RUN_KEYS or value not in RUN_KEYS[key]:
            fail(f"run.{key} must be one of {RUN_KEYS.get(key, ())}")
    _validate_script(fail, case["scripted"])
    return case


def _validate_network(fail, key, spec, nets) -> None:
    if not isinstance(spec, dict):
        fail(f"network {key!r} must be an object")
    if "file" in spec:
        if set(spec) != {"file"} or not isinstance(spec["file"], str):
            fail(f"network {key!r} is a file: {{\"file\": <path under scripts/>}} and nothing else")
        return
    if spec.get("from") not in nets or "file" not in nets[spec["from"]]:
        fail(f"network {key!r} must be derived from a file network (from)")
    recipe = spec.get("derive")
    if recipe not in networks.DERIVATIONS:
        fail(f"network {key!r}: unknown derivation {recipe!r}")
    allowed = {"from", "derive", "anchor", *networks.RECIPE_KEYS[recipe]}
    if set(spec) - allowed:
        fail(f"network {key!r}: {recipe} takes only {sorted(allowed)}")
    if "anchor" in spec and spec["anchor"] not in networks.ANCHORS.get(recipe, ()):
        fail(f"network {key!r}: {recipe}'s anchor is one of {networks.ANCHORS.get(recipe, ())}")


def _validate_expected(fail, case) -> None:
    expected = case["expected"]
    if not isinstance(expected, dict):
        fail("expected must be an object")
    verdict = expected.get("verdict")
    verdicts = verdict if isinstance(verdict, list) else [verdict]
    if not verdicts or any(v not in VERDICTS for v in verdicts):
        fail(f"expected.verdict must be one of {VERDICTS}, or a list of them")
    if case["kind"] == "task" and not isinstance(verdict, str):
        fail("a task case expects one class")
    if expected.get("stop") not in STOPS:
        fail(f"expected.stop must be one of {STOPS}")
    if case["kind"] == "task" and expected["stop"] != "answered":
        fail("a task case expects an answer (stop answered)")


def _validate_script(fail, turns) -> None:
    if not isinstance(turns, list) or not turns:
        fail("scripted must be a list of turns")
    for turn in turns:
        kinds = [key for key in SCRIPT_TURNS if key in (turn if isinstance(turn, dict) else {})]
        if len(kinds) != 1:
            fail(f"a scripted turn is one of {SCRIPT_TURNS}: {turn!r}")
        if kinds == ["tool"] and (turn["tool"] not in TOOLS or not isinstance(turn.get("input", {}), dict)):
            fail(f"a scripted tool turn names one of {TOOLS} with an input object: {turn!r}")


def load_cases(manifest: dict, directory: Path = CASES_DIR, only: list[str] | None = None) -> list[dict]:
    """Every case under `directory` (or those in `only`), bases resolved and validated, in
    `case_order`. CaseError for a malformed case, or a name in `only` that is not there."""
    raw = read_case_files(directory)
    names = sorted(raw, key=case_order)
    if only:
        unknown = [name for name in only if name not in raw]
        if unknown:
            raise CaseError(f"no case named {unknown} (there are {names})")
        names = [name for name in names if name in only]
    return [validate_case(resolve(raw, name), manifest) for name in names]


def network_file(spec: dict, root: Path = SCRIPTS) -> Path:
    """Where a file network is: relative to scripts/, as the benchmark's scenes file names it."""
    return root / spec["file"]


def case_networks(case: dict, fixture_a: dict, polygon: list, *, deserts: list | None = None) -> tuple:
    """{key: network} for every network the case names, and {key: what was changed}: a file
    network is `fixture_a` (every case's file network is fixture A, given once to the run), and a
    derived one is made from it (vision_eval_networks.derive), anchored on `deserts` (the
    baseline's newest measurement's) when its recipe asks.

    C1's new line is the replay's own (vision_eval_driver.with_new_line: five stations 600 m apart,
    west to east, centred on the largest desert), so `replay --sequence sydney-c1` and case C1
    measure the same network B; vision_eval_networks.add_line lays it only when that line would
    leave the site, or there is no desert to centre it on (the change says which)."""
    built, changes = {}, {}
    for key, spec in case["networks"].items():
        if "file" in spec:
            built[key], changes[key] = copy.deepcopy(fixture_a), None
    for key, spec in case["networks"].items():
        if "file" in spec:
            continue
        base = built[spec["from"]]
        centre = networks.largest_desert(deserts) if spec.get("anchor") == "largest_desert" else None
        line = None
        if spec["derive"] == "add_line" and centre is not None:
            line = driver.with_new_line(base, *centre, stations=spec.get("stations", 5))
            known = {x["id"] for x in base.get("stations") or []}
            added = [s for s in line["stations"] if s["id"] not in known]
            plane = networks.plane_for(polygon)
            poly = [plane.xy(*p[:2]) for p in polygon]
            if not all(networks.inside(plane.xy(s["lon"], s["lat"]), poly) for s in added):
                line = None
        if line is not None:
            ways = {x["id"] for x in base.get("ways") or []}
            change = networks.change_record(
                "add_line", added_stations=[s["id"] for s in added],
                added_ways=[w["id"] for w in line["ways"] if w["id"] not in ways],
                anchor=[round(v, 7) for v in centre], construction="vision_eval_driver.with_new_line",
            )  # fmt: skip
            built[key], changes[key] = networks.recount(line), change
            continue
        built[key], changes[key] = networks.derive(base, spec, polygon, deserts=deserts)
    return built, changes


def needs_deserts(case: dict) -> bool:
    """Whether a derived network of the case is anchored on the baseline's deserts."""
    return any(spec.get("anchor") for spec in case["networks"].values())


# --- the model: scripted, live and recorded ------------------------------------------------------------

# What a scripted turn reports it used: a few thousand tokens in, a short answer out, so the cost
# path is exercised in CI with numbers of the live run's order.
SCRIPTED_USAGE = {"inputTokens": 2400, "outputTokens": 90}
# The answer a script gives once it has no turn left: no JSON, so the loop ends `malformed`.
SCRIPT_EXHAUSTED = "(the script has no more turns)"
# C8's model id: well-formed, and no model Bedrock serves.
INVALID_MODEL_ID = "eval.invalid-model-v0:0"
# How the scripted `decide_by: look_again` turn reads a re-measurement against the brief's
# baseline: the topic's default thresholds (common/adapters/rail_access.DEFAULTS).
DECIDE_SHARE = 0.02
DECIDE_DESERT_RELATIVE = 0.10
BRIEF_PREFIX = "Measured change to check:\n"


class ScriptedError(Exception):
    """What a scripted `{"raise": <name>}` turn raises: Bedrock refusing the call, as the loop sees
    one (common/vision_triage.triage fails closed on any exception from converse)."""


class ScriptedConverse:
    """A Converse stand-in that answers one scripted turn per call: `{"tool": name, "input": {...}}`
    asks for a tool, `{"text": ...}` answers, `{"raise": name}` fails as Bedrock would, and
    `{"decide_by": "look_again"}` answers real when the last re-measurement moved against the
    brief's baseline by the default thresholds and artefact otherwise (the counterfactual's
    stand-in for a model that reads its tool's numbers). Every request is kept in `requests`."""

    def __init__(self, turns: list[dict], usage: dict | None = None):
        self.turns, self.usage = list(turns), dict(usage or SCRIPTED_USAGE)
        self.calls, self.requests = 0, []

    def __call__(self, **request) -> dict:
        self.requests.append(request)
        turn = self.turns[self.calls] if self.calls < len(self.turns) else {"text": SCRIPT_EXHAUSTED}
        self.calls += 1
        if "raise" in turn:
            raise ScriptedError(turn["raise"])
        if "tool" in turn:
            given = dict(turn.get("input") or {})
            use = {"toolUseId": f"tool-{self.calls}", "name": turn["tool"], "input": given}
            return _response([{"toolUse": use}], "tool_use", self.usage)
        text = decide_by_look_again(request.get("messages") or []) if "decide_by" in turn else turn["text"]
        return _response([{"text": text}], "end_turn", self.usage)


def _response(content: list[dict], stop: str, usage: dict) -> dict:
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop,
            "usage": dict(usage)}  # fmt: skip


def _brief(messages: list[dict]) -> dict:
    for block in (messages[0].get("content") or []) if messages else []:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text.startswith(BRIEF_PREFIX):
            try:
                return json.loads(text[len(BRIEF_PREFIX) :])
            except ValueError:
                return {}
    return {}


def _last_look_again(messages: list[dict]) -> dict | None:
    """The last successful `look_again` result in the conversation (its json body), or None."""
    found = None
    for message in messages:
        for block in message.get("content") or []:
            result = block.get("toolResult") if isinstance(block, dict) else None
            if not result or result.get("status") != "success":
                continue
            body = (result.get("content") or [{}])[0]
            payload = body.get("json") if isinstance(body, dict) else None
            if isinstance(payload, dict) and "settings" in payload:
                found = payload
    return found


def decide_by_look_again(messages: list[dict]) -> str:
    """The scripted verdict from the re-measurement: real when its station count differs from the
    baseline's, or its served share or desert area moved past the default thresholds; artefact
    otherwise, or when nothing was re-measured."""
    baseline = _brief(messages).get("baseline_median") or {}
    measured = _last_look_again(messages)
    if not measured or not baseline:
        return json.dumps({"verdict": "artefact", "reason": "Nothing was re-measured to confirm it."})
    moved = []
    stations = measured.get("station_count")
    if stations is not None and stations != baseline.get("station_count"):
        moved.append("the station count")
    share, base_share = measured.get("served_share"), baseline.get("served_share")
    if isinstance(share, int | float) and isinstance(base_share, int | float):
        if round(abs(share - base_share), 4) >= DECIDE_SHARE:
            moved.append("the served share")
    desert, base_desert = measured.get("desert_km2"), baseline.get("desert_km2")
    if isinstance(desert, int | float) and isinstance(base_desert, int | float):
        if round(abs(desert - base_desert), 4) >= round(DECIDE_DESERT_RELATIVE * max(base_desert, 1.0), 4):
            moved.append("the area beyond reach")
    if moved:
        reason = f"The re-measurement confirms {' and '.join(moved)} moved."
        return json.dumps({"verdict": "real", "reason": reason})
    return json.dumps({"verdict": "artefact", "reason": "The re-measurement is back at the baseline."})


class Conversation:
    """One triage's `converse`, as the driver takes it (vision_eval_driver.Driver._triage): every
    response it hands back is kept in the driver's recorded form (the message, the stop reason and
    the tokens: never ResponseMetadata, whose headers carry the service's request ids), so a run
    file's `agent.responses` replays through vision_eval_driver.RecordedAgent; a call that raised
    is kept apart, as its exception's class name only."""

    def __init__(self, call):
        self.call, self.responses, self.errors, self.refused = call, [], [], None

    def __call__(self, **request) -> dict:
        try:
            response = self.call(**request)
        except Exception as exc:
            self.errors.append(type(exc).__name__)
            raise
        kept = ("output", "stopReason", "usage")
        self.responses.append({key: copy.deepcopy(response.get(key)) for key in kept if key in response})
        return response

    def finish(self) -> None:
        pass


def bedrock_converse(region: str, client=None):
    """The real Converse, from the operator's credentials in `region` (Bedrock's, which may differ
    from the worker's)."""
    if client is None:
        import boto3
        from botocore.config import Config

        config = Config(read_timeout=60, retries={"total_max_attempts": 3})
        client = boto3.client("bedrock-runtime", region_name=region, config=config)
    return client.converse


def model_cost(model_id: str | None, input_tokens: int, output_tokens: int) -> tuple:
    """(USD, AUD) for one run's tokens, by the built-in price table (common/model_pricing.py; the
    deployed Models table is not read from an operator's machine) and the pipeline's own formula
    and rate (common/costing.py); (None, None) for a model without a price, or no tokens."""
    from common import costing, model_pricing

    if not model_id or not (input_tokens or output_tokens):
        return None, None
    usd = costing.call_cost_usd({"input_tokens": input_tokens, "output_tokens": output_tokens},
                                model_pricing.default_model_entry(model_id))  # fmt: skip
    if usd is None:
        return None, None
    return round(usd, 6), round(usd * costing.USD_TO_AUD_RATE, 6)


# --- where the worker runs ------------------------------------------------------------------------------

# The Region whose scenes an in-process re-measurement reads without leaving it.
SCENES_REGION = "us-west-2"
IN_PROCESS_REFUSAL = (
    "refused: --worker in-process with the production deadline needs --cache DIR (a filled scene cache) "
    "or a run on an EC2 instance in us-west-2: an uncached look_again re-reads about 54 MB of bands "
    "before 12-40 s of analysis, so cases would fail closed (deadline, no_time) for the harness's "
    "reasons and be scored as the agent's"
)


def in_process_refusal(cache, where_: str | None, region: str | None) -> str | None:
    """None when an in-process worker keeps the production deadline: with a cache, or on an EC2
    instance in us-west-2 (`where_` from vision_runs.where, `region` from the instance identity).
    Otherwise the refusal, which names `--cache`."""
    if cache is not None:
        return None
    if where_ == "ec2" and region == SCENES_REGION:
        return None
    return IN_PROCESS_REFUSAL


# --- in-process seams: the built-up masks (E3) and SCL clouded to a coverage (2.6.6 b) -------------------

# The cloud class the gate counterfactual writes into SCL (Sentinel-2 L2A: cloud, high probability).
SCL_CLOUD_HIGH = 9


@contextmanager
def capturing_masks(sink: list):
    """While open, every in-process rail analysis (vision_worker_handler.analyse_rail_access, the
    worker's own call) also hands its built-up and usable masks to `sink`, bit-packed, so E3 can
    compare the built-up mask scene to scene. The reply is unchanged."""
    import numpy as np
    import vision_worker_handler as handler

    original = handler.analyse_rail_access

    def analyse(*args, **kwargs):
        analysis = original(*args, **kwargs)
        built, usable = analysis.layers["built"] > 0, analysis.layers["usable"] > 0
        sink.append({"shape": list(built.shape), "built": np.packbits(built), "usable": np.packbits(usable)})
        return analysis

    handler.analyse_rail_access = analyse
    try:
        yield sink
    finally:
        handler.analyse_rail_access = original


def mask_iou(a: dict | None, b: dict | None) -> float | None:
    """The IoU of two captured built-up masks over the ground both scenes could see (usable in
    both); None when either is missing or their grids differ."""
    import numpy as np

    if not a or not b or a["shape"] != b["shape"]:
        return None
    count = a["shape"][0] * a["shape"][1]

    def unpack(packed):
        return np.unpackbits(packed, count=count).astype(bool)

    seen = unpack(a["usable"]) & unpack(b["usable"])
    built_a, built_b = unpack(a["built"]) & seen, unpack(b["built"]) & seen
    return scoring.iou_from_counts(int((built_a & built_b).sum()), int((built_a | built_b).sum()))


@contextmanager
def clouded_to(coverage: float | None, report: dict):
    """While open, every in-process site read (vision.scene.read_site, which the worker handler
    calls) has SCL pixels inside the site rewritten to cloud (SCL_CLOUD_HIGH), a bank from the
    site's top rows down, until the share of the site the worker can measure is `coverage` (to
    one pixel): the spec's "SCL masked in-process", all else equal. `report` gets the coverage
    before and after and the pixels clouded. A scene already under it cannot be clouded to it:
    `report["refused"]` says so and the read raises, which the worker handler answers `internal`,
    so that side is not measured (the harness reads the refusal here, not from the handler).
    `coverage` None leaves every read alone."""
    import numpy as np
    from vision import masks, scene

    original = scene.read_site

    def read_site(*args, **kwargs):
        site = original(*args, **kwargs)
        if coverage is None or "scl" not in site.bands:
            return site
        scl = site.bands["scl"].copy()
        inside = masks.polygon_mask(scl.shape, [site.polygon_px]) > 0
        unusable = masks.unusable_mask(scl) > 0
        # The handler folds the reference band's no-data into SCL's no-data after this read.
        for band, nodata in site.nodata.items():
            if band != "scl" and nodata is not None and band in site.bands:
                unusable |= site.bands[band] == nodata
        usable = inside & ~unusable
        total, seen = int(inside.sum()), int(usable.sum())
        keep = int(round(coverage * total))
        if keep > seen:
            before = round(seen / max(total, 1), 4)
            refused = f"the scene's coverage {before:.4f} is already under {coverage}"
            report.update(target=coverage, coverage_before=before, refused=refused)
            raise ValueError(report["refused"])
        drop = np.flatnonzero(usable.ravel())[: seen - keep]
        scl.reshape(-1)[drop] = SCL_CLOUD_HIGH  # a fresh copy is contiguous: reshape is a view
        site.bands["scl"] = scl
        whole = max(total, 1)
        report.update(target=coverage, coverage_before=round(seen / whole, 4),
                      coverage_after=round(keep / whole, 4), pixels_clouded=int(drop.size))  # fmt: skip
        return site

    scene.read_site = read_site
    try:
        yield report
    finally:
        scene.read_site = original


# --- the harness, on the driver ---------------------------------------------------------------------------

DEFAULT_REPEATS = cli.DEFAULT_REPEATS
# Fixture A for the Sydney cases (sydney.json, the file the runsheet's fixture step captures).
DEFAULT_NETWORK = cli.DEFAULT_NETWORK
# C7's clock is moved this far forward when the agent is asked: past the topic's whole allowance.
DEADLINE_JUMP_SECONDS = 1000.0
# A decision the agent took part in (vision_decisions.CODES): the gate passed.
TRIAGED_CODES = ("reported", "artefact")
COUNTERFACTUAL_CASES = ("C1", "C10")
# The case that stands in when one does not pass the gate (2.6.6: C4 if C10 does not).
COUNTERFACTUAL_STAND_IN = {"C10": "C4"}
COUNTERFACTUAL_ARMS = ("measured", "baseline")
# Arm (b)'s two coverages, either side of the rail floor (0.6).
GATE_COVERAGES = {"above": 0.62, "below": 0.58}
# How near a side's measured coverage must be to its target to count: the clouding is exact to a
# pixel and the worker rounds coverage to 4 places, so this is slack for rounding only.
GATE_COVERAGE_TOLERANCE = 0.001
# The counterfactual's scripted model: re-measure at the topic's own reach, then answer by what came
# back (decide_by_look_again), so CI shows a verdict that follows the tool's numbers.
COUNTERFACTUAL_SCRIPT = ({"tool": "look_again", "input": {"reach_m": 1000}}, {"decide_by": "look_again"})
YEAR_SET = cli.YEAR_SET
# The production deadline the agent keeps, from the tick's start (the adapter's own default).
TRIAGE_DEADLINE_SECONDS = driver.rail_access.DEFAULTS["triage_deadline_seconds"]
# E3's production rule: triage off (every material verdict counts, none is judged away) and the
# history the adapter keeps by default; the thresholds are the adapter's defaults.
STABILITY_RULE = {"triage": False, "history_size": 6}
# The synthetic year: this many clear synthetic scenes, ten days apart (CI's E3).
SYNTHETIC_YEAR_SCENES = cli.SYNTHETIC_YEAR_SCENES
SYNTHETIC_SITE = {"id": "synthetic", "name": "Synthetic city"}
# A manifest scene measured under this coverage keeps its cloud on the synthetic city: the west share
# of the city's SCL set to cloud, as the driver's synthetic cloud scene is (C4's cloud edge in CI).
SYNTHETIC_CLEAR_COVERAGE = 0.95


class ArmableClock(driver.Clock):
    """The driver's clock, which C7's agent moves past the triage deadline when it is asked."""

    def __init__(self, base=time.monotonic):
        super().__init__(base=base)
        self.jump = 0.0

    def arm(self, seconds: float) -> None:
        self.jump += seconds

    def __call__(self) -> float:
        return super().__call__() + self.jump


class Clocks:
    """A new clock for each tick (the driver asks for one), frozen at 0 for CI so no budget or
    deadline ever bites, else the real monotonic clock; the newest kept so the agent can arm it."""

    def __init__(self, frozen: bool):
        self.frozen, self.current = frozen, None

    def __call__(self, n: int) -> ArmableClock:
        self.current = ArmableClock(base=(lambda: 0.0) if self.frozen else time.monotonic)
        return self.current


class CaseAgent:
    """The agent the driver asks during a case's newest tick, in vision_eval_driver's agent shape
    (`mode`, `model_id`, `converse_for`): each conversation is a `Conversation` over `call()` (the
    live Converse or a fresh script) and is kept; with `past_deadline` the tick's clock is moved
    past the deadline as the loop starts (C7)."""

    def __init__(self, mode: str, model_id: str, call, clocks: Clocks | None = None, past_deadline=False):
        self.mode, self.model_id, self.call = mode, model_id, call
        self.clocks, self.past_deadline = clocks, past_deadline
        self.conversations: list[Conversation] = []

    def converse_for(self, tick: int, site_id: str) -> Conversation:
        if self.past_deadline and self.clocks is not None and self.clocks.current is not None:
            self.clocks.current.arm(DEADLINE_JUMP_SECONDS)
        conversation = Conversation(self.call())
        self.conversations.append(conversation)
        return conversation


class MemoMeasure:
    """`measure` with each answer kept by what was asked (the site, the scene, the backend, the
    parameters, the floor, the task and the network's digest): the history scenes several cases
    share are measured once a run. A refusal is not kept."""

    def __init__(self, measure):
        self.measure, self.answers, self.hits, self.misses = measure, {}, 0, 0

    def __call__(self, site, scene, backend="opencv", **kwargs):
        key = vision_runs.digest({
            "site": site, "scene": scene.get("id"), "backend": backend, "params": kwargs.get("params"),
            "floor": kwargs.get("coverage_floor"), "task": kwargs.get("task"),
            "network": vision_runs.digest(kwargs.get("network")),
        })  # fmt: skip
        if key in self.answers:
            self.hits += 1
            return copy.deepcopy(self.answers[key])
        result = self.measure(site, scene, backend=backend, **kwargs)
        self.misses += 1
        self.answers[key] = copy.deepcopy(result)
        return result


def refusing_measure(code: str):
    """C6's `look_again`: the worker refuses every re-measurement with `code`."""
    from common import vision_client

    def measure(site, scene, *args, **kwargs):
        raise vision_client.VisionError(code, "the evaluation's tool-failure case refuses it")

    return measure


@dataclass
class Setup:
    """What one run's cases are measured and asked with: the site ({id, name, polygon}), the scenes
    by manifest name (real, or their synthetic stand-ins), fixture A, the tick's worker and its
    memo for the history, the in-process worker for arm (b) (None when there is none), the model
    (`agent_mode` bedrock or scripted, `model_id`, the live `converse`) and whether clocks are
    frozen (CI)."""

    site: dict
    scenes: dict
    fixture_a: dict
    measure: object
    agent_mode: str = "scripted"
    model_id: str = driver.SCRIPTED_MODEL
    converse: object = None
    frozen: bool = True
    gate_measure: object = None
    memo: MemoMeasure = field(init=False)
    gate_memo: MemoMeasure | None = field(init=False)

    def __post_init__(self):
        self.memo = MemoMeasure(self.measure)
        self.gate_memo = MemoMeasure(self.gate_measure) if self.gate_measure is not None else None


def _overrides(case: dict, triage: bool) -> dict:
    return {**case["thresholds"], "triage": triage, "web_context": case["news"] is not None}


def _topic(case: dict, setup: Setup, triage: bool, name: str | None = None) -> dict:
    return driver.topic_config(name or f"agent-{case['case']}", setup.site["id"], setup.site["polygon"],
                               site_name=setup.site["name"], overrides=_overrides(case, triage))  # fmt: skip


def _specs(steps: list[dict], setup: Setup, nets: dict, news, previous: str | None = None) -> list:
    """The driver's ticks for `steps` ({scene, network}): the network stored when its label changes."""
    specs = []
    for step in steps:
        scene = setup.scenes[step["scene"]]
        label = step["network"]
        network = nets[label] if label != previous else None
        specs.append(driver.TickSpec(scene=scene, now=driver.tick_time(scene["captured_at"]), network=network,
                                     label=label, news=list(news or [])))  # fmt: skip
        previous = label
    return specs


def build_history(case: dict, setup: Setup, memo: MemoMeasure, store, name: str | None = None) -> tuple:
    """The case's history through the real adapter with triage off (fixture A, frozen clocks), as
    topic `name` (the newest tick's own, so it reads the same network object): the state after it
    and each tick's code."""
    history = driver.Driver(_topic(case, setup, triage=False, name=name), measure=memo, store=store,
                            clock=lambda n: driver.Clock.frozen())  # fmt: skip
    files = {key: setup.fixture_a for key, spec in case["networks"].items() if "file" in spec}
    specs = _specs(case["history"], setup, files, [])
    results = history.run(specs)
    rows = [{"scene": spec.scene["name"], "network": spec.label, "code": (r["codes"] or [None])[0]}
            for spec, r in zip(specs, results, strict=True)]  # fmt: skip
    return history.state, rows


def newest_deserts(state: dict | None, site_id: str) -> list:
    """The deserts of the site's newest measured scene in `state` (only the newest entry keeps them)."""
    history = ((((state or {}).get("sites") or {}).get(site_id) or {}).get("history")) or []
    return list(history[-1].get("deserts") or []) if history else []


def networks_record(nets: dict, changes: dict) -> dict:
    return {key: {"sha256": vision_runs.digest(net), "fetched_at": net.get("fetched_at"),
                  "counts": networks.recount(copy.deepcopy(net))["counts"], "change": changes.get(key)}
            for key, net in nets.items()}  # fmt: skip


def case_agent(case: dict, setup: Setup, clocks: Clocks, script=None) -> CaseAgent:
    """The model a case is asked: the live one when the run is live and the case asks for it, else
    the case's script (`script` instead, for the counterfactual); C8's invalid model id; C7's clock."""
    run = case["run"]
    live = setup.agent_mode == "bedrock" and run["converse"] == "model" and setup.converse is not None
    turns = list(script if script is not None else case["scripted"])
    call = (lambda: setup.converse) if live else (lambda: ScriptedConverse(turns))
    model_id = setup.model_id if live else driver.SCRIPTED_MODEL
    if run.get("model_id") == "invalid":
        model_id = INVALID_MODEL_ID
    mode = "bedrock" if live else "scripted"
    return CaseAgent(mode, model_id, call, clocks, past_deadline=run.get("clock") == "past_deadline")


def run_row(case: dict, repeat: int, result: dict, outcome: dict | None, site_id: str) -> dict:
    """One repeat's run, as the scores read it, with the case's labels and the trail's facts."""
    trail = outcome or {}
    steps = list(trail.get("steps") or [])
    tokens_in, tokens_out = int(trail.get("input_tokens") or 0), int(trail.get("output_tokens") or 0)
    usd, aud = (None, None)
    if trail.get("model_calls"):
        usd, aud = model_cost(trail.get("model_id"), tokens_in, tokens_out)
    expected = case["expected"]
    required = case["kind"] == "failure" and expected["stop"] != "answered"
    measured = next((m for m in result.get("measurements") or [] if m.get("kind") == "tick"), {})
    return {
        "case": case["case"],
        "repeat": repeat,
        "kind": case["kind"],
        "expected": expected["verdict"] if isinstance(expected["verdict"], str) else None,
        "expected_stop": expected["stop"] if required else None,
        "driven_by": case["driven_by"],
        "code": (result.get("codes") or [None])[0],
        "agent_called": outcome is not None,
        "finding": bool(result.get("changed")),
        "verdict": trail.get("verdict"),
        "stop": trail.get("stop"),
        "reason": trail.get("reason"),
        "tools": [call.get("name") for call in trail.get("tool_calls") or []],
        "steps": steps,
        "look_again_failed": any(s.get("tool") == "look_again" and not s.get("ok") for s in steps),
        "elapsed_ms": trail.get("elapsed_ms"),
        "deadline_left_s": trail.get("deadline_left_s"),
        "model_id": driver.recorded_model_id(trail.get("model_id")),  # never an ARN, as the replay's
        "model_calls": trail.get("model_calls"),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": usd,
        "cost_aud": aud,
        "measured": measured.get("metrics"),
        "measured_ok": measured.get("ok"),
        # The worker's code when it refused the newest scene (a row with no decision `code` was not
        # measured: neither the gate's result nor the agent's, and never scored).
        "error": measured.get("code"),
    }


def _outcome(state: dict, site_id: str) -> dict | None:
    return ((state or {}).get("triage") or {}).get(site_id)


def _responses(result: dict, case: str, repeat: int, arm: str | None = None) -> list[dict]:
    tag = {"case": case, "repeat": repeat, **({"arm": arm} if arm else {})}
    return [{**tag, **talk} for talk in result.get("responses") or []]


def not_measured(row: dict) -> bool:
    """Whether a run's newest tick decided nothing (the worker refused the scene, or the tick had no
    time to measure it): the gate was not tried and the agent not asked, so the run is neither."""
    return row["code"] is None


def run_case(case: dict, setup: Setup, repeats: int) -> tuple[dict, list[dict]]:
    """One case: its history, its networks, then its newest tick `repeats` times from the same state
    (once when the gate stays shut: the measurement repeats exactly, and no model is asked). A
    repeat whose newest scene was not measured is kept with the worker's code and the next one
    runs: the gate is the first measured repeat's. Returns the case's results and the Converse
    responses kept."""
    out = {"case": case["case"], "kind": case["kind"], "title": case["title"], "expected": case["expected"],
           "driven_by": case["driven_by"], "conditional": case["conditional"], "base": case.get("base"),
           "history": [], "newest": dict(case["newest"]), "networks": {}, "gate": None, "included": False,
           "runs": [], "error": None}  # fmt: skip
    store = driver.LocalStore()
    responses: list[dict] = []
    try:
        before, out["history"] = build_history(case, setup, setup.memo, store)
        site_id = driver.SITE_PREFIX + setup.site["id"]
        nets, changes = case_networks(case, setup.fixture_a, setup.site["polygon"],
                                      deserts=newest_deserts(before, site_id))  # fmt: skip
        out["networks"] = networks_record(nets, changes)
        spec = _specs([case["newest"]], setup, nets, case["news"])[0]
        clocks = Clocks(setup.frozen)
        agent = case_agent(case, setup, clocks)
        look_again = refusing_measure(case["run"]["look_again"]) if case["run"].get("look_again") else None
        newest = driver.Driver(_topic(case, setup, triage=True), measure=setup.measure, agent=agent,
                               store=store, clock=clocks, look_again_measure=look_again)  # fmt: skip
        n = len(case["history"]) + 1
        for repeat in range(repeats):
            result = newest.tick(before, spec, n=n)
            outcome = _outcome(result.pop("state"), newest.site_id)
            row = run_row(case, repeat, result, outcome, newest.site_id)
            out["runs"].append(row)
            responses += _responses(result, case["case"], repeat)
            if not_measured(row):
                continue
            if out["gate"] is None:
                out["gate"], out["included"] = row["code"], outcome is not None
            if outcome is None:
                break
        out["converse_errors"] = [error for talk in agent.conversations for error in talk.errors]
    except (networks.DeriveError, driver.ReplayRefused, KeyError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        store.close()
    return out, responses


def run_agent(cases: list[dict], setup: Setup, repeats: int) -> tuple[dict, list[dict], list[dict], dict]:
    """Every case: (results, the Converse responses, the run's checks, its summary)."""
    by_case, responses = {}, []
    for case in cases:
        result, talks = run_case(case, setup, repeats)
        by_case[case["case"]] = result
        responses += talks
    # Only the runs that reached the agent are scored: a newest scene the worker did not measure is
    # the harness's failure, never the agent's (its check fails instead).
    task_runs = [run for c in by_case.values() if c["kind"] == "task" and c["included"] for run in c["runs"]
                 if run["agent_called"]]  # fmt: skip
    failure_runs = [run for c in by_case.values() if c["kind"] == "failure" for run in c["runs"]
                    if not not_measured(run)]  # fmt: skip
    scores = scoring.agent_scores(task_runs, failure_runs)
    # A case the gate kept from the agent: its newest scene measured, with a code that is not triage's.
    gate_closed = sum(1 for c in by_case.values() if c["kind"] == "task" and gate_held(c))
    results = {
        "cases": by_case,
        "scores": scores,
        "c6": c6_observed(by_case.get("C6")),
        "history": {"measured": setup.memo.misses, "reused": setup.memo.hits},
    }
    summary = scoring.agent_summary(scores, cases=len(by_case), gate_closed=gate_closed,
                                    runs=sum(len(c["runs"]) for c in by_case.values()),
                                    not_measured=sum(1 for c in by_case.values() for run in c["runs"]
                                                     if not_measured(run)))  # fmt: skip
    results["scored"] = {"task_runs": len(task_runs), "failure_runs": len(failure_runs)}
    return results, responses, agent_checks(by_case), summary


def c6_observed(case: dict | None) -> dict | None:
    """C6 as observed (2.6.3): how many repeats called `look_again`, saw it fail, answered, and
    with which verdicts."""
    if not case or not case.get("runs"):
        return None
    runs = [run for run in case["runs"] if not not_measured(run)]
    return {
        "runs": len(runs),
        "look_again_failed": sum(1 for r in runs if r["look_again_failed"]),
        "answered": sum(1 for r in runs if r["stop"] == "answered"),
        "verdicts": {v: sum(1 for r in runs if scoring.label(r) == v) for v in scoring.LABELS},
    }


def measured_check(name: str, runs: list[dict]) -> dict | None:
    """A failed check when any repeat's newest scene was not measured, naming the worker's codes."""
    missed = [run for run in runs if not_measured(run)]
    if not missed:
        return None
    codes = ", ".join(sorted({str(run["error"] or "no decision") for run in missed}))
    return {"name": f"{name}: newest scene measured", "expected": len(runs),
            "got": f"{len(runs) - len(missed)} ({codes})", "pass": False}  # fmt: skip


def agent_checks(by_case: dict) -> list[dict]:
    """A case that could not be built; a repeat whose newest scene the worker did not measure; an
    unconditional task case that never reached the agent; a failure case (C7-C9) that did not end
    as required, in any measured repeat."""
    checks = []
    for name, case in by_case.items():
        if case["error"]:
            checks.append({"name": f"{name}: built", "expected": True, "got": case["error"], "pass": False})
            continue
        missed = measured_check(name, case["runs"])
        if missed:
            checks.append(missed)
        if case["kind"] == "task" and case["conditional"] is None:
            checks.append({"name": f"{name}: reached the agent", "expected": True, "got": case["included"],
                           "pass": bool(case["included"])})  # fmt: skip
        required = [run for run in case["runs"] if run["expected_stop"] and not not_measured(run)]
        if required:
            ok = all(run["verdict"] == "artefact" and run["stop"] == run["expected_stop"] for run in required)
            got = ", ".join(sorted({str(run["stop"]) for run in required}))
            expected = required[0]["expected_stop"]
            checks.append({"name": f"{name}: failed closed as required", "expected": expected, "got": got,
                           "pass": ok})  # fmt: skip
    return checks


# --- the counterfactuals (2.6.6) ----------------------------------------------------------------------------


class ArmMeasure:
    """Arm (a)'s `look_again`: the newest scene measured at the requested settings (each settings
    measured once, through `memo`, so every repeat of both arms sees the same numbers), and in the
    `baseline` arm the moved measures set back to the baseline median (`baseline`, `moved`: the
    tick's own verdict), a constructed tool output. The figure is the re-measurement's in both
    arms: only the numbers differ."""

    def __init__(self, memo: MemoMeasure, arm: str, baseline: dict | None = None, moved=()):
        self.memo, self.arm, self.baseline, self.moved = memo, arm, dict(baseline or {}), tuple(moved)

    def __call__(self, site, scene, *args, **kwargs):
        result = self.memo(site, scene, *args, **kwargs)
        if self.arm == "baseline":
            result = set_back(result, self.baseline, self.moved)
        return result


def set_back(result, baseline: dict, moved) -> object:
    """A copy of a worker result with each moved measure at its baseline value: the served share
    (and the served area with it), the desert area, the station count (and the snapped count by
    the same step)."""
    out = copy.deepcopy(result)
    metrics = out.reply["metrics"]
    if "served_share" in moved and baseline.get("served_share") is not None:
        metrics["served_share"] = baseline["served_share"]
        if isinstance(metrics.get("built_up_km2"), int | float):
            metrics["served_km2"] = round(baseline["served_share"] * metrics["built_up_km2"], 4)
    if "desert_km2" in moved and baseline.get("desert_km2") is not None:
        metrics["desert_km2"] = baseline["desert_km2"]
    if "station_count" in moved and baseline.get("station_count") is not None:
        step = baseline["station_count"] - (metrics.get("station_count") or 0)
        metrics["station_count"] = baseline["station_count"]
        if isinstance(metrics.get("snapped_station_count"), int):
            metrics["snapped_station_count"] += step
    return out


def counterfactual_case(case: dict, setup: Setup, repeats: int, arms: tuple[str, ...],
                        stand_in: str | None = None) -> tuple[dict, list]:  # fmt: skip
    """2.6.6 for one case: arm (a)'s two arms of `repeats` each, and arm (b)'s two coverages. When
    arm (a) finds the gate holding and `stand_in` will run in the case's place, arm (b) is left to
    the stand-in: the rule counts one gate pair for the two, and asks no model for the one replaced."""
    out = {"case": case["case"], "gate": None, "included": False, "agent": None, "gate_level": None,
           "error": None}  # fmt: skip
    responses: list[dict] = []
    store = driver.LocalStore()
    try:
        name = f"counterfactual-{case['case']}"
        before, out["history"] = build_history(case, setup, setup.memo, store, name=name)
        site_id = driver.SITE_PREFIX + setup.site["id"]
        nets, changes = case_networks(case, setup.fixture_a, setup.site["polygon"],
                                      deserts=newest_deserts(before, site_id))  # fmt: skip
        out["networks"] = networks_record(nets, changes)
        spec = _specs([case["newest"]], setup, nets, case["news"])[0]
        n = len(case["history"]) + 1
        if "a" in arms:
            out["agent"], talks = agent_arms(case, setup, store, before, spec, n, repeats)
            responses += talks
            out["gate"], out["included"] = out["agent"]["gate"], out["agent"]["included"]
        if "b" in arms and stand_in and gate_held(out):
            out["gate_level"] = {"skipped": f"the gate held ({out['gate']}): {stand_in} stands in"}
        elif "b" in arms:
            out["gate_level"] = gate_arms(case, setup, nets, spec, n)
    except (networks.DeriveError, driver.ReplayRefused, KeyError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        store.close()
    return out, responses


def gate_held(result: dict) -> bool:
    """Whether arm (a) measured the newest scene and the gate kept it from the agent (a decision
    code that is not triage's), as against a scene the worker did not measure at all."""
    return not result["included"] and not result["error"] and result["gate"] is not None


def agent_arms(case, setup, store, before, spec, n, repeats) -> tuple[dict, list]:
    """Arm (a): `measured` first (its first measured repeat's verdict gives the baseline and the
    moved measures), then `baseline`; the case's model, with the counterfactual script when
    scripted. A repeat whose newest scene was not measured is kept, with the worker's code, and
    the next one runs."""
    runs: dict[str, list[dict]] = {}
    responses: list[dict] = []
    look_memo = MemoMeasure(setup.measure)
    verdict = {}
    out = {"gate": None, "included": False}
    for arm in COUNTERFACTUAL_ARMS:
        clocks = Clocks(setup.frozen)
        agent = case_agent(case, setup, clocks, script=COUNTERFACTUAL_SCRIPT)
        measure = ArmMeasure(look_memo, arm, verdict.get("baseline"), verdict.get("moved") or ())
        tick = driver.Driver(_topic(case, setup, triage=True, name=f"counterfactual-{case['case']}"),
                             measure=setup.measure, agent=agent, store=store, clock=clocks,
                             look_again_measure=measure)  # fmt: skip
        runs[arm] = []
        for repeat in range(repeats):
            result = tick.tick(before, spec, n=n)
            outcome = _outcome(result.pop("state"), tick.site_id)
            row = run_row(case, repeat, result, outcome, tick.site_id)
            runs[arm].append(row)
            responses += _responses(result, case["case"], repeat, arm)
            if not_measured(row):
                continue
            if arm == "measured" and out["gate"] is None:
                entry = (result.get("decisions") or [{}])[0]
                verdict = {"baseline": entry.get("baseline") or {}, "moved": list(entry.get("moved") or [])}
                out.update(gate=row["code"], included=outcome is not None, **verdict)
            if outcome is None:
                break
        if not out["included"]:
            break
    out["arms"] = runs
    out["score"] = scoring.agent_counterfactual(runs) if out["included"] else None
    return out, responses


def gate_arms(case: dict, setup: Setup, nets: dict, spec, n: int) -> dict:
    """Arm (b): the same newest scene in this process, SCL clouded to just above and just below the
    floor (GATE_COVERAGES), from a history measured in this process too; once each (the gate is
    deterministic), with the case's model (its script in CI): called above the floor if the change
    is material, never below it."""
    if setup.gate_measure is None:
        return {"skipped": "no in-process worker for this run (arm (b) needs --cache, or --synthetic)"}
    store = driver.LocalStore()
    try:
        before, _rows = build_history(case, setup, setup.gate_memo, store, name=f"gate-{case['case']}")
        sides = {}
        for side, coverage in GATE_COVERAGES.items():
            clocks = Clocks(setup.frozen)
            agent = case_agent(case, setup, clocks)
            tick = driver.Driver(_topic(case, setup, triage=True, name=f"gate-{case['case']}"),
                                 measure=setup.gate_measure, agent=agent, store=store,
                                 clock=clocks)  # fmt: skip
            report: dict = {}
            with clouded_to(coverage, report):
                result = tick.tick(before, spec, n=n)
            outcome = _outcome(result.pop("state"), tick.site_id)
            measured = next((m for m in result.get("measurements") or [] if m.get("kind") == "tick"), {})
            got = (measured.get("metrics") or {}).get("coverage")
            sides[side] = {
                "target": coverage,
                "coverage": got,
                "clouded": report,
                "measured": bool(measured.get("ok")),
                "error": report.get("refused") or measured.get("code"),
                # Measured, and where it was meant to be: only then does the side say what the gate did.
                "at_target": bool(measured.get("ok")) and isinstance(got, int | float)
                and abs(got - coverage) <= GATE_COVERAGE_TOLERANCE,
                "code": (result.get("codes") or [None])[0],
                "agent_called": outcome is not None,
                "model_calls": (outcome or {}).get("model_calls", 0),
                "verdict": (outcome or {}).get("verdict"),
                "finding": bool(result.get("changed")),
            }
        return scoring.gate_counterfactual(sides["above"], sides["below"])
    finally:
        store.close()


def run_counterfactual(cases: list[dict], setup: Setup, repeats: int, arms: tuple[str, ...]) -> tuple:
    """2.6.6 over `cases` (C1 and C10 by default): a case that does not pass the gate is replaced
    by its stand-in (C10 by C4). Returns (results, responses, checks, summary)."""
    by_name = {case["case"]: case for case in cases}
    stands_for = {stand_in: name for name, stand_in in COUNTERFACTUAL_STAND_IN.items()}
    done, responses = {}, []
    for case in cases:
        if stands_for.get(case["case"]) in by_name:
            continue  # a stand-in runs only in place of the case it stands in for, when both are asked
        stand_in = COUNTERFACTUAL_STAND_IN.get(case["case"])
        stand_in = stand_in if stand_in in by_name and "a" in arms else None
        result, talks = counterfactual_case(case, setup, repeats, arms, stand_in=stand_in)
        responses += talks
        # Only a gate that held hands the case on: a scene the worker did not measure is a failed check.
        if stand_in and gate_held(result):
            result["replaced_by"] = stand_in
            done[case["case"]] = result
            result, talks = counterfactual_case(by_name[stand_in], setup, repeats, arms)
            result["stands_in_for"] = case["case"]
            responses += talks
        done[result["case"]] = result
    agent_rows = {name: r["agent"]["score"] for name, r in done.items()
                  if r.get("agent") and r["agent"]["score"]}  # fmt: skip
    gate_rows = {name: r["gate_level"] for name, r in done.items()
                 if r.get("gate_level") and "skipped" not in r["gate_level"]}  # fmt: skip
    checks = []
    for name, result in done.items():
        if result["error"]:
            checks.append({"name": f"{name}: built", "expected": True, "got": result["error"], "pass": False})
        missed = measured_check(name, [run for arm in ((result.get("agent") or {}).get("arms") or {}).values()
                                       for run in arm])  # fmt: skip
        if missed:
            checks.append(missed)
        row = gate_rows.get(name)
        if row is not None:
            sides = {side: row[side]["coverage"] for side in ("above", "below")}
            checks.append({"name": f"{name}: measured either side of the floor", "expected": GATE_COVERAGES,
                           "got": sides, "pass": row["measured"]})  # fmt: skip
            below = row["below"]
            ok = not below["agent_called"] and not below["finding"] and not below.get("model_calls")
            checks.append({"name": f"{name}: no model below the floor", "expected": True, "got": ok,
                           "pass": ok})  # fmt: skip
    summary = scoring.counterfactual_summary(agent_rows, gate_rows)
    return {"cases": done, "arms": list(arms)}, responses, checks, summary


# --- E3: stability over the year --------------------------------------------------------------------------


def run_stability(setup: Setup, names: list[str], capture: bool) -> tuple[dict, list, dict]:
    """E3 over the scenes `names` in time order: the production rule (triage off, the default
    thresholds, history_size 6), fixture A throughout. Returns (results, checks, summary)."""
    site = setup.site
    topic = driver.topic_config("stability", site["id"], site["polygon"], site_name=site["name"],
                                overrides=STABILITY_RULE)  # fmt: skip
    store = driver.LocalStore()
    masks: list = []
    rows, decisions = [], []
    try:
        stable = driver.Driver(topic, measure=setup.measure, store=store,
                               clock=lambda n: driver.Clock.frozen())  # fmt: skip
        specs = _specs([{"scene": name, "network": "A"} for name in names], setup, {"A": setup.fixture_a}, [])
        state, first_mask, previous_mask = None, None, None
        for n, spec in enumerate(specs, start=1):
            before = len(masks)
            with capturing_masks(masks) if capture else _nothing():
                result = stable.tick(state, spec, n=n)
            state = result.pop("state")
            measured = next((m for m in result.get("measurements") or [] if m.get("kind") == "tick"), {})
            metrics = measured.get("metrics") or {}
            code = (result.get("codes") or [None])[0]
            mask = masks[-1] if len(masks) > before else None
            if mask is not None:
                masks[-1] = None  # keep two masks at a time, not a year of them
            row = {
                "scene": spec.scene["name"], "scene_id": spec.scene["scene_id"],
                "captured_at": spec.scene["captured_at"],
                "code": code, "material": code in TRIAGED_CODES, "ok": bool(measured.get("ok")),
                "error": measured.get("code"), "coverage": metrics.get("coverage"),
                "served_share": metrics.get("served_share"), "desert_km2": metrics.get("desert_km2"),
                "built_up_km2": metrics.get("built_up_km2"), "station_count": metrics.get("station_count"),
                "hubs": metrics.get("hubs"), "flags": metrics.get("flags"),
                "iou_previous": mask_iou(previous_mask, mask), "iou_first": mask_iou(first_mask, mask),
                "moved": (result.get("decisions") or [{}])[0].get("moved"),
            }  # fmt: skip
            rows.append(row)
            decisions.append({key: row[key] for key in ("scene", "captured_at", "code", "material")})
            if mask is not None:
                first_mask = first_mask or mask
                previous_mask = mask
    finally:
        store.close()
    alarms = scoring.false_alarms(decisions)
    spreads = scoring.stability_spread([row for row in rows if row["ok"]])
    checks = [{"name": "every scene measured", "expected": len(rows), "got": sum(1 for r in rows if r["ok"]),
               "pass": all(r["ok"] for r in rows)}]  # fmt: skip
    results = {"scenes": rows, "false_alarms": alarms, "spread": spreads, "masks": capture}
    return results, checks, scoring.stability_summary(alarms, spreads)


@contextmanager
def _nothing():
    yield


# --- the synthetic city and the real one --------------------------------------------------------------------


def synthetic_entry(manifest: dict, name: str, seed: int) -> dict:
    """The synthetic stand-in for a manifest scene: the synthetic city with noise of `seed`, the
    scene's capture time, and its cloud when it was measured under SYNTHETIC_CLEAR_COVERAGE (that
    share of the city's west under SCL cloud, as the driver's cloud scene is)."""
    scene = next(s for s in manifest["scenes"] if s["name"] == name)
    coverage = scene.get("site_coverage")
    cloudy = isinstance(coverage, int | float) and coverage < SYNTHETIC_CLEAR_COVERAGE
    cloud = round(1.0 - coverage, 4) if cloudy else 0.0
    return {"name": f"synthetic-{name}", "seed": seed, "cloud": cloud, "captured_at": scene["captured_at"]}


def synthetic_year_entries(count: int = SYNTHETIC_YEAR_SCENES) -> list[dict]:
    """CI's year set: `count` clear synthetic scenes, ten days apart from 5 January 2024."""
    from datetime import datetime, timedelta

    start = datetime(2024, 1, 5, 0, 6, 11)
    return [{"name": f"synthetic-year-{k + 1}", "seed": 200 + k, "cloud": 0.0,
             "captured_at": (start + timedelta(days=10 * k)).strftime("%Y-%m-%dT%H:%M:%SZ")}
            for k in range(count)]  # fmt: skip


def synthetic_blobs_for(entries: list[dict]) -> dict[str, bytes]:
    """Each entry's four bands as GeoTIFF bytes by URL, as vision_eval_driver.synthetic_blobs writes
    its own scenes (vision_benchmark.synthetic_city, the cloud over the west share in SCL)."""
    from vision_benchmark import CITY_EPSG, synthetic_city

    blobs, transform = {}, driver.synthetic_transform()
    for entry in entries:
        bands = synthetic_city(driver.SYNTHETIC_SIZE, seed=entry["seed"])
        if entry["cloud"]:
            scl = bands["scl"].copy()
            scl[:, : int(round(driver.SYNTHETIC_SIZE * entry["cloud"]))] = 9
            bands["scl"] = scl
        for band, asset in driver.synthetic_scene(entry)["assets"].items():
            nodata = None if band == "scl" else "0"
            blobs[asset["url"]] = driver.geotiff(bands[band], transform, CITY_EPSG, nodata=nodata)
    return blobs


def synthetic_setup(entries: dict[str, dict], fixture_a: dict, **model) -> Setup:
    """A run on the synthetic city: `entries` ({the name the run uses: synthetic entry}), measured by
    the worker handler in this process (arm (b) too), frozen clocks."""
    blobs = synthetic_blobs_for(list(entries.values()))
    measure = driver.in_process_measure(driver.synthetic_fetch(blobs), driver.SYNTHETIC_ENVIRON)
    scenes = {name: driver.synthetic_scene(entry) for name, entry in entries.items()}
    site = {**SYNTHETIC_SITE, "polygon": driver.synthetic_polygon()}
    return Setup(site=site, scenes=scenes, fixture_a=fixture_a, measure=measure, frozen=True,
                 gate_measure=measure, **model)  # fmt: skip


def scene_names(cases: list[dict]) -> list[str]:
    steps = [step for case in cases for step in [*case["history"], case["newest"]]]
    return list(dict.fromkeys(step["scene"] for step in steps))


def bench_shaped(scenes: list[dict]) -> list[dict]:
    """Manifest scenes in the benchmark's shape ({name, scene_id, assets: {band: url}}), for its
    `prepare_inputs` (the HEADs, the pinned-ETag rule, the cache)."""
    return [{"name": s["name"], "scene_id": s["scene_id"],
             "assets": {band: a["url"] for band, a in s["assets"].items()}} for s in scenes]  # fmt: skip


class Refused(Exception):
    """A run refused before anything is measured (exit 2), with the reason to print."""


def real_setup(args, manifest: dict, site_name: str, names: list[str], *, head=None, imds=None,
               lambda_client=None, gate: bool = False, deadline: bool = True,
               **model) -> tuple[Setup, dict, dict]:  # fmt: skip
    """A run on the real city: the manifest's site and scenes, fixture A from `--network`, the
    worker `--worker` (an ARN, or `in-process`: refused without `--cache` off an EC2 instance in
    us-west-2 when the run keeps the production `deadline`; E3, triage off, keeps none and reads
    uncached if it must), each asset HEADed first and a changed pinned ETag refused
    (InputChanged). `gate`: arm (b) needs an in-process worker too, under the same rule. Returns
    (setup, inputs, worker)."""
    import vision_worker_handler

    import vision_benchmark

    in_process = args.worker == "in-process"
    if ((in_process and deadline) or gate) and args.cache is None:
        where_ = vision_runs.where()
        region = None
        if where_ == "ec2":
            region = ((imds or vision_benchmark.imds_identity)() or {}).get("region")
        refusal = in_process_refusal(None, where_, region)
        if refusal and not in_process:
            refusal += "; arm (b) clouds SCL in this process, so it needs the same (or pass --arms a)"
        if refusal:
            raise Refused(refusal)
    path = Path(args.network or DEFAULT_NETWORK)
    try:
        fixture_a = driver.load_network_file(path)
    except FileNotFoundError as exc:
        raise Refused(f"{exc}: {path} (fixture A: the runsheet's fixture step captures it)") from exc
    scenes = {s["name"]: s for s in manifest["scenes"]}
    chosen = [scenes[name] for name in names]
    site = manifest["sites"][site_name]
    heads, cache, manifest_sha = vision_benchmark.prepare_inputs(
        bench_shaped(chosen), manifest, args.cache, head=head, manifest_path=args.manifest
    )
    local = driver.in_process_measure(cache or vision_worker_handler.http_fetch)
    if in_process:
        measure, worker = local, _in_process_worker()
    else:
        from common import vision_client

        client = lambda_client(args.worker) if lambda_client else vision_benchmark.lambda_client(args.worker)
        measure = driver.deployed_measure(args.worker)
        worker = {"kind": "deployed", "region": vision_client.region_of(args.worker),
                  "code_sha256": vision_benchmark.deployed_code_sha256(client, args.worker)}  # fmt: skip
    inputs = {
        "manifest_sha256": manifest_sha,
        "cache": cache is not None,
        "cache_index_sha256": cache.index_sha256() if cache else None,
        "scenes": vision_benchmark.scene_inputs(bench_shaped(chosen), heads),
        "networks": _network_input(path),
    }
    setup = Setup(site={"id": site_name, "name": site_name.capitalize(), "polygon": site["polygon"]},
                  scenes=scenes, fixture_a=fixture_a, measure=measure, frozen=False,
                  gate_measure=local if gate else None, **model)  # fmt: skip
    return setup, inputs, worker


# --- the command line ---------------------------------------------------------------------------------------

# The subcommands' names and arguments are vision_eval_agent_cli's (vision_eval.py builds its parser
# from it without importing this module, and so the driver).
COMMANDS = cli.COMMANDS
add_parsers = cli.add_parsers
KIND = {"agent": "eval-agent", "counterfactual": "eval-counterfactual", "stability": "eval-stability"}
PRICE_SOURCE = "the built-in price table (common/model_pricing.py); AUD at common/costing.USD_TO_AUD_RATE"


def _model(args, converse) -> dict:
    if args.agent == "bedrock":
        if not args.model_id:
            raise Refused("--agent bedrock needs --model-id: the Bedrock model id the rail topic uses")
        return {"agent_mode": "bedrock", "model_id": args.model_id,
                "converse": converse or bedrock_converse(args.region)}  # fmt: skip
    if args.model_id:
        # Never a scripted run under a live run's command line: the model id would be dropped unsaid.
        raise Refused("--model-id is the live model's: pass --agent bedrock with it (without, every case "
                      "runs its script)")  # fmt: skip
    return {"agent_mode": "scripted", "model_id": driver.SCRIPTED_MODEL, "converse": None}


def _case_names(args) -> list[str] | None:
    """The cases `--case`/`--cases` named (commas also separate them), in order, once each."""
    names = [part.strip() for value in args.case or [] for part in value.split(",") if part.strip()]
    return list(dict.fromkeys(names)) or None


def _network_input(path: Path) -> list[dict]:
    return [{"file": vision_runs.shown_path(path), "sha256": vision_runs.sha256_file(path)}]


def _synthetic_inputs(path: Path) -> dict:
    return {"manifest_sha256": None, "cache": False, "synthetic": True, "scenes": [],
            "networks": _network_input(path)}  # fmt: skip


def _in_process_worker() -> dict:
    return {"kind": "in-process", "region": None, "code_sha256": vision_runs.worker_code_sha256()}


def _setup_for(args, manifest: dict, site: str, names: list[str], *, model: dict, deadline: bool, gate: bool,
               seams: dict):  # fmt: skip
    """(setup, inputs, worker) for the synthetic city or the real one."""
    if args.synthetic:
        order = {s["name"]: i for i, s in enumerate(manifest["scenes"])}
        entries = {name: synthetic_entry(manifest, name, 100 + order[name]) for name in names}
        path = Path(args.network or driver.SYNTHETIC_NETWORK_FILE)
        setup = synthetic_setup(entries, driver.load_network_file(path), **model)
        return setup, _synthetic_inputs(path), _in_process_worker()
    if not args.worker:
        raise Refused("pass --worker <ARN> (the deployed worker) or --worker in-process with --cache DIR")
    return real_setup(args, manifest, site, names, gate=gate, deadline=deadline, **seams, **model)


def _run_file(command: str, args, started: str, worker: dict, inputs: dict, params: dict, results, checks,
              summary, agent: dict | None) -> dict:  # fmt: skip
    from vision import build

    from vision_benchmark import cpu_identity

    env = vision_runs.environment(cpu_identity(), build.build_info())
    tag = args.tag
    if tag is None:
        mode = getattr(args, "agent", None) if command != "stability" else None
        tag = "-".join(part for part in (mode, "synthetic" if args.synthetic else None) if part) or None
    run = vision_runs.new_run(KIND[command], env["cpu"], "opencv", env, worker=worker, inputs=inputs,
                              params=params, tag=tag, mode=command, started=started)  # fmt: skip
    # The summary holds numbers only (the index refuses anything else): a value that could not be
    # measured (no run with a price, no decision for an interval) is left out, never zero.
    numbers = {key: value for key, value in summary.items()
               if isinstance(value, int | float) and not isinstance(value, bool)}  # fmt: skip
    vision_runs.finish(run, results, checks, numbers)
    run["results"]["not_measured"] = sorted(key for key in summary if key not in numbers)
    run["agent"] = agent
    return run


def main(args, manifest: dict, **seams) -> int:
    """`agent`, `counterfactual` or `stability` (args.command); 2 when refused before anything is
    measured, 1 when a check failed, else 0. `seams` (converse, head, imds, lambda_client) are the
    tests' stand-ins for Bedrock, the bucket's HEADs, the instance identity and the Lambda client."""
    try:
        if args.command == "stability":
            run = _main_stability(args, manifest, seams)
        else:
            run = (_main_agent if args.command == "agent" else _main_counterfactual)(args, manifest, seams)
    except (Refused, CaseError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (vision_runs.InputChanged, vision_runs.WrongHost) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(MARKDOWN[args.command](run))
    if args.out:
        print(f"wrote {vision_runs.write(run, args.out)}")
    return 0 if all(check["pass"] for check in run["checks"]) else 1


def _one_site(cases: list[dict]) -> str:
    sites = sorted({case["site"] for case in cases})
    if len(sites) != 1:
        raise Refused(f"one run measures one site; these cases name {sites}")
    return sites[0]


def _agent_block(model: dict, responses: list[dict], region) -> dict:
    # The model as the replay records it (driver.recorded_model_id): an inference profile's ARN
    # carries the account id, so the run file keeps the profile's id, or None; Converse alone is
    # given what the operator passed.
    return {"mode": model["agent_mode"], "model_id": driver.recorded_model_id(model["model_id"]),
            "region": region, "price": PRICE_SOURCE, "responses": responses}  # fmt: skip


def _main_agent(args, manifest: dict, seams: dict) -> dict:
    cases = load_cases(manifest, only=_case_names(args))
    if args.repeats < 1:
        raise Refused("--repeats must be at least 1")
    model = _model(args, seams.pop("converse", None))
    started = vision_runs.utc_now()
    setup, inputs, worker = _setup_for(args, manifest, _one_site(cases), scene_names(cases), model=model,
                                       deadline=True, gate=False, seams=seams)  # fmt: skip
    results, responses, checks, summary = run_agent(cases, setup, args.repeats)
    params = {"cases": [c["case"] for c in cases], "repeats": args.repeats, "synthetic": args.synthetic,
              "deadline_seconds": TRIAGE_DEADLINE_SECONDS, "frozen_clock": setup.frozen,
              "cache": args.cache is not None}  # fmt: skip
    return _run_file("agent", args, started, worker, inputs, params, results, checks, summary,
                     _agent_block(model, responses, args.region))  # fmt: skip


def _main_counterfactual(args, manifest: dict, seams: dict) -> dict:
    arms = tuple(part.strip() for part in args.arms.split(",") if part.strip())
    if not arms or set(arms) - {"a", "b"}:
        raise Refused("--arms takes a, b or a,b")
    wanted = _case_names(args) or [*COUNTERFACTUAL_CASES, *COUNTERFACTUAL_STAND_IN.values()]
    cases = load_cases(manifest, only=list(dict.fromkeys(wanted)))
    if args.repeats < 1:
        raise Refused("--repeats must be at least 1")
    model = _model(args, seams.pop("converse", None))
    started = vision_runs.utc_now()
    setup, inputs, worker = _setup_for(args, manifest, _one_site(cases), scene_names(cases), model=model,
                                       deadline=True, gate="b" in arms, seams=seams)  # fmt: skip
    results, responses, checks, summary = run_counterfactual(cases, setup, args.repeats, arms)
    params = {"cases": [c["case"] for c in cases], "repeats": args.repeats, "arms": list(arms),
              "gate_coverages": GATE_COVERAGES, "synthetic": args.synthetic, "frozen_clock": setup.frozen,
              "script": list(COUNTERFACTUAL_SCRIPT) if model["agent_mode"] == "scripted" else None}
    return _run_file("counterfactual", args, started, worker, inputs, params, results, checks, summary,
                     _agent_block(model, responses, args.region))  # fmt: skip


def _main_stability(args, manifest: dict, seams: dict) -> dict:
    started = vision_runs.utc_now()
    model = {"agent_mode": "scripted", "model_id": driver.SCRIPTED_MODEL, "converse": None}
    if args.synthetic:
        entries = {e["name"]: e for e in synthetic_year_entries(args.scenes)}
        path = Path(args.network or driver.SYNTHETIC_NETWORK_FILE)
        setup = synthetic_setup(entries, driver.load_network_file(path), **model)
        inputs, worker, names = _synthetic_inputs(path), _in_process_worker(), list(entries)
    else:
        sets = manifest.get("sets") or {}
        if args.set not in sets:
            raise Refused(f"no set {args.set!r} in the manifest (it has {sorted(sets)}); "
                          "coverage --write records them")  # fmt: skip
        names = list(sets[args.set]["scenes"])
        if not args.worker:
            args.worker = "in-process"
        setup, inputs, worker = _setup_for(args, manifest, sets[args.set]["site"], names, model=model,
                                           deadline=False, gate=False, seams=seams)  # fmt: skip
    capture = worker["kind"] == "in-process"
    results, checks, summary = run_stability(setup, names, capture)
    params = {"set": None if args.synthetic else args.set, "scenes": names, "synthetic": args.synthetic,
              "thresholds": "default", "triage": False, "history_size": 6, "masks": capture}  # fmt: skip
    return _run_file("stability", args, started, worker, inputs, params, results, checks, summary, None)


# --- what each prints ---------------------------------------------------------------------------------------


def _n(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def _row(*cells) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _summary(run: dict) -> dict:
    """The run's summary with every code it left out (not measured) read as None."""
    from collections import defaultdict

    return defaultdict(lambda: None, run["summary"])


def agent_markdown(run: dict) -> str:
    agent, s = run.get("agent") or {}, _summary(run)
    lines = [
        f"**agent** {agent.get('mode')} (model `{agent.get('model_id')}`), worker {run['worker']['kind']}, "
        f"where {run['environment'].get('where')}",
        "",
        _row("case", "expected", "driven by", "gate", "runs", "real", "artefact", "failed closed",
             "not measured", "look_again", "cost (AUD)"),  # fmt: skip
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, case in run["results"]["cases"].items():
        expected, driven = case["expected"]["verdict"], case["driven_by"] or "-"
        if case["error"]:
            lines.append(_row(name, expected, driven, f"not built: {case['error']}", 0, *[""] * 6))
            continue
        runs = case["runs"]
        labels = [scoring.label(r) for r in runs if r["agent_called"]]
        counts = [labels.count(label) for label in scoring.LABELS]
        missed = sum(1 for r in runs if not_measured(r))
        cost = sum(r["cost_aud"] or 0 for r in runs)
        looked = sum(1 for r in runs if "look_again" in r["tools"])
        lines.append(_row(name, expected, driven, case["gate"], len(runs), *counts, missed, looked,
                          f"{cost:.4f}"))  # fmt: skip
    real = f"{_n(s['recall_real'])} [{_n(s['recall_real_wilson_low'])}, {_n(s['recall_real_wilson_high'])}]"
    artefact = (f"{_n(s['recall_artefact'])} [{_n(s['recall_artefact_wilson_low'])}, "
                f"{_n(s['recall_artefact_wilson_high'])}]")  # fmt: skip
    lines += [
        "",
        f"Balanced accuracy {_n(s['balanced_accuracy'])} (recall real {real}, recall artefact {artefact}); "
        f"numeric-only {_n(s['numeric_only_balanced_accuracy'])}, always-artefact "
        f"{_n(s['always_artefact_balanced_accuracy'])}; {s['gate_closed']} case(s) never reached the agent; "
        f"{s['not_measured_runs']} run(s) not measured.",
        f"Stability {_n(s['stability'])}; failed closed as required {s['fail_closed']} of "
        f"{s['fail_closed_runs']}; latency p50 {_n(s['latency_p50_ms'])} ms, p95 "
        f"{_n(s['latency_p95_ms'])} ms; tokens {_n(s['tokens_mean'])} a run; cost "
        f"{_n(s['cost_usd_total'], 4)} USD ({_n(s['cost_aud_total'], 4)} AUD) in all.",
    ]
    return "\n".join(lines)


def counterfactual_markdown(run: dict) -> str:
    s = _summary(run)
    lines = ["**counterfactual**", "",
             _row("case", "arm", "repeats", "called look_again", "real", "artefact", "failed closed"),
             "|---|---|---:|---:|---:|---:|---:|"]  # fmt: skip
    for name, case in run["results"]["cases"].items():
        score = (case.get("agent") or {}).get("score")
        if case.get("error"):
            lines.append(_row(name, f"not built: {case['error']}", "", "", "", "", ""))
        elif case.get("agent") and not score:
            said = f"gate {case['gate']}: never reached the agent" if case["gate"] else None
            lines.append(_row(name, said or "newest scene not measured", "", "", "", "", ""))
        for arm in COUNTERFACTUAL_ARMS if score else ():
            labels = score["labels"].get(arm) or {}
            counts = [labels.get(label, 0) for label in scoring.LABELS]
            lines.append(_row(name, arm, score["repeats"][arm], score["called_look_again"][arm], *counts))
    lines += ["", _row("case", "side", "coverage", "code", "agent called", "a Finding would be written"),
              "|---|---|---:|---|---|---|"]  # fmt: skip
    for name, case in run["results"]["cases"].items():
        gate = case.get("gate_level") or {}
        if "skipped" in gate:
            lines.append(_row(name, f"skipped: {gate['skipped']}", "", "", "", ""))
        for side in ("above", "below") if "above" in gate else ():
            row = gate[side]
            coverage = _n(row["coverage"], 4)
            lines.append(_row(name, side, coverage, row["code"], row["agent_called"], row["finding"]))
    lines += ["", f"Agent level: {s['agent_flips']} flip(s) in {s['agent_pairs']} pair(s) that both called "
              f"look_again ({s['agent_called_look_again']} repeats called it). Gate level: "
              f"{s['gate_flips']} of {s['gate_pairs']} pair(s) flipped."]  # fmt: skip
    return "\n".join(lines)


def stability_markdown(run: dict) -> str:
    s, results = _summary(run), run["results"]
    lines = ["**stability** (E3)", "",
             _row("scene", "captured", "coverage", "served share", "desert km²", "built-up km²", "code",
                  "mask IoU (previous, first)"),
             "|---|---|---:|---:|---:|---:|---|---|"]  # fmt: skip
    for row in results["scenes"]:
        iou = f"{_n(row['iou_previous'], 4)}, {_n(row['iou_first'], 4)}"
        lines.append(_row(row["scene"], str(row["captured_at"])[:10], _n(row["coverage"], 4),
                          _n(row["served_share"], 4), _n(row["desert_km2"], 2), _n(row["built_up_km2"], 2),
                          row["code"], iou))  # fmt: skip
    lines += ["", f"False alarms: {s['false_material']} of {s['decisions']} decisions "
              f"({_n(s['false_material_rate'])}, Wilson 95 % {_n(s['wilson_low'])}-{_n(s['wilson_high'])}). "
              f"Spread: served share {_n(s['served_share_spread'], 4)}, desert "
              f"{_n(s['desert_rel_spread'])} and built-up {_n(s['built_up_rel_spread'])} of their medians; "
              "smallest mask IoU "
              f"{_n(s['mask_iou_min'], 4)}, hub Jaccard {_n(s['hub_jaccard_min'])}, flag Jaccard "
              f"{_n(s['flag_jaccard_min'])}."]  # fmt: skip
    return "\n".join(lines)


MARKDOWN = {"agent": agent_markdown, "counterfactual": counterfactual_markdown,
            "stability": stability_markdown}  # fmt: skip
