"""The agent's evaluation (the specification's 2.6), its counterfactuals (2.6.6) and the stability
run (E3): `vision_eval.py agent`, `counterfactual` and `stability`.

(The driver-dependent half of this module is written once Q2a's driver lands.)
"""

from __future__ import annotations

import copy
import json
import re
from contextlib import contextmanager
from pathlib import Path

import vision_eval_networks as networks
import vision_eval_scoring as scoring

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
# How a network is anchored for add_line.
ANCHORS = ("largest_desert",)
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
    if "anchor" in spec and (recipe != "add_line" or spec["anchor"] not in ANCHORS):
        fail(f"network {key!r}: anchor is add_line's, one of {ANCHORS}")


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


def case_networks(case: dict, fixture_a: dict, polygon: list, *, anchor=None) -> tuple[dict, dict]:
    """{key: network} for every network the case names, and {key: what was changed}: a file
    network is `fixture_a` (every case's file network is fixture A, given once to the run), and a
    derived one is made from it (vision_eval_networks.derive), with `anchor` for an add_line that
    asks for one."""
    built, changes = {}, {}
    for key, spec in case["networks"].items():
        if "file" in spec:
            built[key], changes[key] = copy.deepcopy(fixture_a), None
    for key, spec in case["networks"].items():
        if "file" in spec:
            continue
        derived, change = networks.derive(built[spec["from"]], spec, polygon,
                                          anchor=anchor if spec.get("anchor") else None)  # fmt: skip
        built[key], changes[key] = derived, change
    return built, changes


def needs_anchor(case: dict) -> bool:
    return any(spec.get("anchor") == "largest_desert" for spec in case["networks"].values())


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


class RecordingConverse:
    """`converse` with every response kept: the content blocks the model returned (text and tool
    uses; a response holds no image), its stop reason and token usage, in call order, under the
    tag of the case and repeat. A call that raised is kept as its exception's class name only."""

    def __init__(self, converse, sink: list, tag: dict):
        self.converse, self.sink, self.tag, self.calls = converse, sink, dict(tag), 0

    def __call__(self, **request) -> dict:
        self.calls += 1
        try:
            response = self.converse(**request)
        except Exception as exc:
            self.sink.append({**self.tag, "turn": self.calls, "error": type(exc).__name__})
            raise
        message = (response.get("output") or {}).get("message") or {}
        usage = response.get("usage") or {}
        blocks = [block for block in message.get("content") or [] if "text" in block or "toolUse" in block]
        self.sink.append({
            **self.tag,
            "turn": self.calls,
            "stopReason": response.get("stopReason"),
            "content": blocks,
            "usage": {key: usage.get(key) for key in ("inputTokens", "outputTokens")},
        })  # fmt: skip
        return response


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
    before and after and the pixels clouded. ValueError when the scene is already under it.
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
            raise ValueError(f"the scene's coverage {seen / max(total, 1):.4f} is already under {coverage}")
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
