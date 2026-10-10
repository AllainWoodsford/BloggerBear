"""The vision agent's decision: is a measured change real, or an artefact of the scene?

docs/enhancements/opencv-agentic-vision-enhancement.md §5. The satellite_vision adapter's diff
finds a count that moved past its thresholds; before that becomes a Finding, Claude looks at the
evidence: the metrics, the site's baseline, why the detector dropped what it dropped, and the
annotated figure itself (an image block, through Bedrock Converse). It may use three tools before
it answers, which is what makes this a loop rather than a single classification:

- `look_again`   re-measure the same scene with different detector settings (active perception:
                 a different size band, a stricter brightness offset, a wider shore buffer);
- `previous_scene` the site's last measured scene: its counts and its figure, to compare;
- `site_history` the counts, coverage and dates of every stored scene.

**Bounded in code, not in the prompt.** At most `max_tool_calls` tool calls (a request past the
budget is answered "budget used, answer now"), and at most `max_tool_calls + 2` model turns: one
per tool call, one to be told the budget is used, and the answer. A `deadline` (a `clock()` value,
given by the adapter from the research tick's own budget) is kept too: no model turn starts with
less than MIN_SECONDS_TO_ANSWER left, and `look_again` is refused with less than
MIN_SECONDS_TO_LOOK_AGAIN, since the worker may take up to its own timeout to answer. The
answer must be a JSON object {"verdict": "real" | "artefact", "reason": "..."}; anything else, a
Bedrock error, a turn limit or a tool failure the model can't get past is "artefact" with the
reason why. **Fail closed:** an unsure agent never makes a Finding. Every call's tokens are tallied
on the Stats page (`vision_triage`), and the trail (tools called, with what, and the answer) is
returned for the snapshot, so the decision can be replayed.

No OpenCV here: `look_again` goes through common/vision_client.py to the worker, like the adapter.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

from common import stats_tracking, vision_client
from common import vision_contract as contract
from common.model_pricing import canonical_model_id

CATEGORY = "vision_triage"
VERDICTS = ("real", "artefact")
MAX_TOKENS = 700
MAX_REASON_CHARS = 300
# Seconds that must remain before the deadline for one more model turn, and for a re-measurement
# (a worker call: usually seconds, but up to the worker's own 60 s timeout).
MIN_SECONDS_TO_ANSWER = 10.0
MIN_SECONDS_TO_LOOK_AGAIN = 30.0

SYSTEM_PROMPT = (
    "You check changes measured by an automated satellite image-analysis pipeline before they are "
    "reported. OpenCV counted bright, elongated objects on water at a fixed site in a Sentinel-2 "
    "scene (10 m pixels) and the count moved well away from the site's usual level. Decide whether "
    "that change is REAL (the objects are there) or an ARTEFACT (thin cloud or haze the cloud mask "
    "missed, sun glint, wakes, a scene edge, noise along a shore). Look at the figure: boxes mark "
    "what was counted; shaded areas were not measured. Use the tools when they would settle it. "
    "Do not identify any vessel or owner, and do not speculate about causes. When you are not "
    'sure, answer "artefact". Answer with only a JSON object: '
    '{"verdict": "real" or "artefact", "reason": "one or two sentences"}.'
)

TOOLS = [
    {
        "toolSpec": {
            "name": "look_again",
            "description": (
                "Re-measure the same scene at this site with different detector settings and get "
                "the new counts and figure. Settings you leave out keep their defaults."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        name: {"type": "number", "minimum": lo, "maximum": hi}
                        for name, (lo, hi) in contract.PARAM_LIMITS.items()
                    },
                    "additionalProperties": False,
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "previous_scene",
            "description": "The site's previous measured scene: its metrics and its figure.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "site_history",
            "description": "Every stored scene at this site: capture date, count and coverage.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
]


def summarise_metrics(entry: dict) -> dict:
    """What the model is told about one measurement: no detection lists, no internal keys."""
    keys = (
        "scene_id", "captured_at", "count", "coverage", "clear_water_km2", "density_per_km2",
        "size_histogram", "rejected", "quality_flags", "scene_cloud_cover",
    )  # fmt: skip
    return {key: entry.get(key) for key in keys if key in entry}


def parse_verdict(text: str) -> tuple[str, str]:
    """(verdict, reason) from the model's answer; ("artefact", why) for anything unusable."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return "artefact", "no verdict in the answer"
    try:
        answer = json.loads(match.group(0))
    except ValueError:
        return "artefact", "the verdict was not valid JSON"
    verdict = answer.get("verdict") if isinstance(answer, dict) else None
    reason = answer.get("reason") if isinstance(answer, dict) else None
    if verdict not in VERDICTS:
        return "artefact", "the verdict was neither real nor artefact"
    if not isinstance(reason, str) or not reason.strip():
        reason = "(no reason given)"
    return verdict, reason.strip()[:MAX_REASON_CHARS]


def triage(
    *,
    site: dict,
    verdict: dict,
    history: list[dict],
    scene: dict,
    model_id: str,
    object_noun: str = "objects",
    params: dict | None = None,
    backend: str = "opencv",
    load_image: Callable[[str], bytes | None] | None = None,
    converse: Callable[..., dict] | None = None,
    measure: Callable[..., vision_client.VisionResult] | None = None,
    max_tool_calls: int = 3,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Decide one site's change. `site` is {"id", "name", "polygon"}; `verdict` is the adapter's
    numeric verdict for it; `history` the site's entries, newest last; `scene` the newest scene's
    {"id", "captured_at", "assets"}. `deadline`, if given, is the `clock()` value by which the caller
    needs the answer; running out of time is an artefact like any other doubt. Returns {"verdict",
    "reason", "tool_calls", "model_id", "input_tokens", "output_tokens", "model_calls"}. Never
    raises."""
    converse = converse or _converse
    measure = measure or vision_client.measure
    load_image = load_image or (lambda key: None)
    trail: dict = {"tool_calls": [], "model_id": canonical_model_id(model_id), "input_tokens": 0,
                   "output_tokens": 0, "model_calls": 0}  # fmt: skip

    latest = history[-1] if history else {}
    brief = {
        "site": site.get("name") or site.get("id"),
        "counting": object_noun,
        "latest": summarise_metrics(latest),
        "baseline_median": verdict.get("baseline"),
        "baseline_scenes": verdict.get("baseline_scenes"),
        "change": verdict.get("delta"),
        "relative_change": verdict.get("relative"),
    }
    content: list[dict] = [{"text": "Measured change to check:\n" + json.dumps(brief, default=str)}]
    figure = _safe_image(load_image, latest.get("image_key"))
    if figure:
        content.append({"image": {"format": "png", "source": {"bytes": figure}}})
    else:
        content.append({"text": "(The figure for this scene is not available.)"})
    messages = [{"role": "user", "content": content}]

    def finish(verdict_value: str, reason: str) -> dict:
        _record_stats(trail)
        return {"verdict": verdict_value, "reason": reason, **trail}

    used = 0
    for _turn in range(max_tool_calls + 2):
        if deadline is not None and deadline - clock() < MIN_SECONDS_TO_ANSWER:
            return finish("artefact", "no time left to decide")
        try:
            response = converse(
                modelId=model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=messages,
                toolConfig={"tools": TOOLS},
                inferenceConfig={"maxTokens": MAX_TOKENS},
            )
        except Exception as exc:  # noqa: BLE001 - fail closed: no Finding on an unsure agent
            return finish("artefact", f"triage unavailable ({type(exc).__name__})")
        usage = response.get("usage") or {}
        trail["model_calls"] += 1
        trail["input_tokens"] += int(usage.get("inputTokens", 0))
        trail["output_tokens"] += int(usage.get("outputTokens", 0))
        message = (response.get("output") or {}).get("message") or {"role": "assistant", "content": []}
        messages.append(message)
        uses = [block["toolUse"] for block in message.get("content", []) if "toolUse" in block]
        if response.get("stopReason") != "tool_use" or not uses:
            text = "".join(block.get("text", "") for block in message.get("content", []))
            return finish(*parse_verdict(text))

        results = []
        for use in uses:
            if used >= max_tool_calls:
                body, status = [{"text": "Tool budget used. Answer now with the JSON verdict."}], "error"
            else:
                used += 1
                body, status = _run_tool(
                    use, site=site, history=history, scene=scene, params=params or {}, backend=backend,
                    load_image=load_image, measure=measure, deadline=deadline, clock=clock,
                )  # fmt: skip
            trail["tool_calls"].append(
                {"name": use.get("name"), "input": use.get("input") or {}, "ok": status == "success"}
            )
            result = {"toolUseId": use.get("toolUseId"), "content": body, "status": status}
            results.append({"toolResult": result})
        messages.append({"role": "user", "content": results})

    return finish("artefact", "no verdict within the turn limit")


def _run_tool(
    use, *, site, history, scene, params, backend, load_image, measure, deadline=None, clock=time.monotonic
) -> tuple[list[dict], str]:
    name, given = use.get("name"), use.get("input") or {}
    try:
        if name == "site_history":
            rows = [
                {k: e.get(k) for k in ("scene_id", "captured_at", "count", "coverage", "quality_flags")}
                for e in history
            ]
            return [{"json": {"scenes": rows}}], "success"
        if name == "previous_scene":
            if len(history) < 2:
                return [{"text": "There is no previous scene for this site."}], "success"
            previous = history[-2]
            body = [{"json": summarise_metrics(previous)}]
            image = _safe_image(load_image, previous.get("image_key"))
            if image:
                body.append({"image": {"format": "png", "source": {"bytes": image}}})
            return body, "success"
        if name == "look_again":
            if not isinstance(given, dict):
                return [{"text": "look_again takes an object of settings."}], "error"
            if deadline is not None and deadline - clock() < MIN_SECONDS_TO_LOOK_AGAIN:
                return [{"text": "No time left to re-measure. Answer now with what you have."}], "error"
            result = measure(
                {"id": site["id"], "polygon": site["polygon"]},
                scene,
                backend=backend,
                params={**params, **given},
                coverage_floor=0.0,
            )
            metrics = summarise_metrics({**result.metrics, "scene_id": scene.get("id")})
            body = [{"json": {"settings": {**params, **given}, **metrics}}]
            if result.image_png:
                body.append({"image": {"format": "png", "source": {"bytes": result.image_png}}})
            return body, "success"
        return [{"text": f"There is no tool called {name!r}."}], "error"
    except vision_client.VisionError as exc:
        return [{"text": f"The measurement failed: {exc.code}."}], "error"
    except Exception as exc:  # noqa: BLE001 - a tool failure is the model's to work around
        return [{"text": f"The tool failed ({type(exc).__name__})."}], "error"


def _safe_image(load_image, key: str | None) -> bytes | None:
    if not key:
        return None
    try:
        data = load_image(key)
    except Exception:  # noqa: BLE001 - a missing figure is said, not fatal
        return None
    return data if data and data.startswith(vision_client.PNG_SIGNATURE) else None


def _converse(**kwargs) -> dict:
    from common.bedrock import _get_client

    return _get_client().converse(**kwargs)


def _record_stats(trail: dict) -> None:
    if not trail["model_calls"]:
        return
    stats_tracking.record_model_usage(
        CATEGORY, trail["model_id"], trail["input_tokens"], trail["output_tokens"], trail["model_calls"]
    )
