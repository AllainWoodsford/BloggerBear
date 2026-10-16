"""The agent's evaluation (the specification's 2.6), its counterfactuals (2.6.6) and the stability
run (E3): `vision_eval.py agent`, `counterfactual` and `stability`.

(The driver-dependent half of this module is written once Q2a's driver lands.)
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import vision_eval_networks as networks

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
