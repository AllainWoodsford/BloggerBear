"""The vision agent's decision: is a measured change real, or an artefact of the scene?

docs/enhancements/opencv-agentic-vision-enhancement.md §5 and docs/enhancements/rail-access-monitor.md
§4 (PR E). A vision adapter's diff finds a metric that moved past its thresholds; before that
becomes a Finding, Claude looks at the evidence: the metrics, the site's baseline, why the
detector dropped what it dropped, and the annotated figure itself (an image block, through
Bedrock Converse). It may use a few tools before it answers, which is what makes this a loop
rather than a single classification:

- `look_again`     re-measure the same scene with different settings (active perception: a
                   different size band or shore buffer for ships; another reach or built-up
                   threshold for rail);
- `previous_scene` the site's last measured scene: its metrics and its figure, to compare;
- `site_history`   the metrics, coverage and dates of every stored scene;
- `web_context`    (rail only) recent news about the city's stations and lines, through the
                   existing news search (common/web_search.py): a station or line that opened or
                   closed explains a real change; nothing in the news leans to an artefact.

**One profile per task** (`PROFILES`): the system prompt, the tools, what the model is told about
a measurement, the bounds of `look_again`'s settings and how `web_context` phrases its search.
The ships profile is the module-level `SYSTEM_PROMPT` and `TOOLS`; the rail profile adds
`web_context` and is given the OpenStreetMap network to re-measure with.

**Bounded in code, not in the prompt.** At most `max_tool_calls` tool calls (a request past the
budget is answered "budget used, answer now"), and at most `max_tool_calls + 2` model turns: one
per tool call, one to be told the budget is used, and the answer. A `deadline` (a `clock()` value,
given by the adapter from the research tick's own budget) is kept too: no model turn starts with
less than MIN_SECONDS_TO_ANSWER left, `look_again` is refused with less than
MIN_SECONDS_TO_LOOK_AGAIN, since the worker may take up to its own timeout to answer, and
`web_context` gets at most WEB_CONTEXT_SECONDS of what is left. The answer must be a JSON object
{"verdict": "real" | "artefact", "reason": "..."}; anything else, a Bedrock error, a turn limit or
a tool failure the model can't get past is "artefact" with the reason why. **Fail closed:** an
unsure agent never makes a Finding. Every call's tokens are tallied on the Stats page
(`vision_triage`), and the trail (tools called, with what, and the answer) is returned for the
snapshot, so the decision can be replayed.

No OpenCV here: `look_again` goes through common/vision_client.py to the worker, like the adapter.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

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
# What `web_context` may cost: a handful of headlines from the last two months, within ten
# seconds of the deadline's remainder (the news search has its own retries and fallback, which
# would otherwise run the tick out of time).
WEB_CONTEXT_MAX_RESULTS = 5
WEB_CONTEXT_MAX_AGE_HOURS = 24 * 60
WEB_CONTEXT_SECONDS = 10.0

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

RAIL_SYSTEM_PROMPT = (
    "You check changes measured by an automated satellite and map pipeline before they are "
    "reported. OpenCV estimated built-up density from a Sentinel-2 scene (20 m pixels) and combined "
    "it with OpenStreetMap's rail lines and stations; the share of built-up area within walking "
    "reach of a station, or the station count, moved against the site's usual level. Decide whether "
    "that change is REAL (the city or its mapped network changed) or an ARTEFACT (cloud, haze or "
    "seasonal vegetation shifting the built-up mask; a scene edge; a map edit; a tagging change). "
    "Look at the figure: warm colours are built-up density, outlines are areas beyond reach, lines "
    "and dots are the mapped network; shaded areas were not measured. Use the tools when they would "
    "settle it: web_context says whether a station or line recently opened or closed. Name no "
    "person or company, and do not speculate about causes beyond what the tools return. When you "
    'are not sure, answer "artefact". Answer with only a JSON object: '
    '{"verdict": "real" or "artefact", "reason": "one or two sentences"}.'
)


def _look_again_tool(limits: dict, whole: tuple[str, ...], what: str) -> dict:
    """The `look_again` tool for one task: its settings are the worker contract's parameter
    limits for that task, whole numbers typed as integers, nothing else accepted."""
    return {
        "toolSpec": {
            "name": "look_again",
            "description": (
                "Re-measure the same scene at this site with different settings and get "
                f"the new {what} and figure. Settings you leave out keep their defaults."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        name: {"type": "integer" if name in whole else "number", "minimum": lo, "maximum": hi}
                        for name, (lo, hi) in limits.items()
                    },
                    "additionalProperties": False,
                }
            },
        }
    }


def _site_history_tool(what: str) -> dict:
    return {
        "toolSpec": {
            "name": "site_history",
            "description": f"Every stored scene at this site: capture date, {what} and coverage.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    }


PREVIOUS_SCENE_TOOL = {
    "toolSpec": {
        "name": "previous_scene",
        "description": "The site's previous measured scene: its metrics and its figure.",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }
}
WEB_CONTEXT_TOOL = {
    "toolSpec": {
        "name": "web_context",
        "description": (
            "Recent news headlines about this site's rail stations and lines: whether one opened, "
            "closed or changed lately. Headlines and links only, from the last two months."
        ),
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }
}

TOOLS = [
    _look_again_tool(contract.PARAM_LIMITS, contract.WHOLE_NUMBER_PARAMS, "counts"),
    PREVIOUS_SCENE_TOOL,
    _site_history_tool("count"),
]
RAIL_TOOLS = [
    _look_again_tool(contract.RAIL_PARAM_LIMITS, contract.TASKS["rail_access"].whole_params, "metrics"),
    PREVIOUS_SCENE_TOOL,
    _site_history_tool("served share, desert area, station count"),
    WEB_CONTEXT_TOOL,
]

# What the model is told about one measurement: no detection lists, no station lists, no internal
# keys. `history_keys` is the shorter row `site_history` gives per stored scene.
SHIP_METRIC_KEYS = (
    "scene_id", "captured_at", "count", "coverage", "clear_water_km2", "density_per_km2",
    "size_histogram", "rejected", "quality_flags", "scene_cloud_cover",
)  # fmt: skip
SHIP_HISTORY_KEYS = ("scene_id", "captured_at", "count", "coverage", "quality_flags")
RAIL_METRIC_KEYS = (
    "scene_id", "captured_at", "coverage", "built_up_km2", "served_km2", "desert_km2", "served_share",
    "station_count", "snapped_station_count", "edge_count", "components", "hubs", "flags",
    "quality_flags", "scene_cloud_cover", "network_fetched_at",
)  # fmt: skip
RAIL_HISTORY_KEYS = (
    "scene_id", "captured_at", "served_share", "desert_km2", "station_count", "coverage", "quality_flags",
)  # fmt: skip


@dataclass(frozen=True)
class TaskProfile:
    """How the agent is set up for one task: its system prompt, the tools it may call, what it is
    told about a measurement (`metric_keys`) and about each stored scene (`history_keys`), the
    bounds of `look_again`'s settings (the worker contract's), and how `web_context` phrases its
    search from the site's name (None: the task has no such tool)."""

    task: str
    system_prompt: str
    tools: list[dict]
    metric_keys: tuple[str, ...]
    history_keys: tuple[str, ...]
    look_again_limits: dict
    web_query: Callable[[str], str] | None


def rail_web_query(site_name: str) -> str:
    return f"{site_name} rail station opening closure line"


PROFILES = {
    "ships": TaskProfile(
        task="ships",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        metric_keys=SHIP_METRIC_KEYS,
        history_keys=SHIP_HISTORY_KEYS,
        look_again_limits=contract.PARAM_LIMITS,
        web_query=None,
    ),
    "rail_access": TaskProfile(
        task="rail_access",
        system_prompt=RAIL_SYSTEM_PROMPT,
        tools=RAIL_TOOLS,
        metric_keys=RAIL_METRIC_KEYS,
        history_keys=RAIL_HISTORY_KEYS,
        look_again_limits=contract.RAIL_PARAM_LIMITS,
        web_query=rail_web_query,
    ),
}


def summarise_metrics(entry: dict, keys: tuple[str, ...] = SHIP_METRIC_KEYS) -> dict:
    """What the model is told about one measurement: `keys` of `entry` (the ships keys by
    default), nothing else."""
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
    object_noun: str | None = "objects",
    params: dict | None = None,
    backend: str = "opencv",
    load_image: Callable[[str], bytes | None] | None = None,
    converse: Callable[..., dict] | None = None,
    measure: Callable[..., vision_client.VisionResult] | None = None,
    max_tool_calls: int = 3,
    task: str = contract.DEFAULT_TASK,
    network: dict | None = None,
    web_search: Callable[..., list[dict]] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    read_timeout: float | None = None,
) -> dict:
    """Decide one site's change. `site` is {"id", "name", "polygon"}; `verdict` is the adapter's
    numeric verdict for it; `history` the site's entries, newest last; `scene` the newest scene's
    {"id", "captured_at", "assets"}. `task` picks the profile; `network` is what `look_again`
    sends the worker for a task that takes one; `web_search` (common/web_search.search_web, or
    None to withhold the tool) is what `web_context` calls; `read_timeout` is how long `look_again`
    waits on the worker. `deadline`, if given, is the `clock()` value by which the caller needs
    the answer; running out of time is an artefact like any other doubt. Returns {"verdict",
    "reason", "tool_calls", "model_id", "input_tokens", "output_tokens", "model_calls"}. Never
    raises."""
    converse = converse or _converse
    measure = measure or vision_client.measure
    load_image = load_image or (lambda key: None)
    trail: dict = {"tool_calls": [], "model_id": canonical_model_id(model_id), "input_tokens": 0,
                   "output_tokens": 0, "model_calls": 0}  # fmt: skip

    def finish(verdict_value: str, reason: str) -> dict:
        _record_stats(trail)
        return {"verdict": verdict_value, "reason": reason, **trail}

    profile = PROFILES.get(task)
    if profile is None:
        return finish("artefact", f"no triage profile for task {task!r}")
    # The news tool is offered only when there is a search to answer it with.
    tools = [t for t in profile.tools if t["toolSpec"]["name"] != "web_context" or web_search is not None]

    latest = history[-1] if history else {}
    brief = {
        "site": site.get("name") or site.get("id"),
        "task": profile.task,
        **({"counting": object_noun} if object_noun is not None else {}),
        "latest": summarise_metrics(latest, profile.metric_keys),
        "baseline_median": verdict.get("baseline"),
        "baseline_scenes": verdict.get("baseline_scenes"),
        "change": verdict.get("delta"),
        "relative_change": verdict.get("relative"),
        "flagged_because": verdict.get("reason"),
    }
    content: list[dict] = [{"text": "Measured change to check:\n" + json.dumps(brief, default=str)}]
    figure = _safe_image(load_image, latest.get("image_key"))
    if figure:
        content.append({"image": {"format": "png", "source": {"bytes": figure}}})
    else:
        content.append({"text": "(The figure for this scene is not available.)"})
    messages = [{"role": "user", "content": content}]

    used = 0
    for _turn in range(max_tool_calls + 2):
        if deadline is not None and deadline - clock() < MIN_SECONDS_TO_ANSWER:
            return finish("artefact", "no time left to decide")
        try:
            response = converse(
                modelId=model_id,
                system=[{"text": profile.system_prompt}],
                messages=messages,
                toolConfig={"tools": tools},
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
                    use, profile=profile, site=site, history=history, scene=scene, params=params or {},
                    backend=backend, load_image=load_image, measure=measure, network=network,
                    web_search=web_search, deadline=deadline, clock=clock, read_timeout=read_timeout,
                )  # fmt: skip
            trail["tool_calls"].append(
                {"name": use.get("name"), "input": use.get("input") or {}, "ok": status == "success"}
            )
            result = {"toolUseId": use.get("toolUseId"), "content": body, "status": status}
            results.append({"toolResult": result})
        messages.append({"role": "user", "content": results})

    return finish("artefact", "no verdict within the turn limit")


def _run_tool(
    use,
    *,
    profile: TaskProfile,
    site,
    history,
    scene,
    params,
    backend,
    load_image,
    measure,
    network=None,
    web_search=None,
    deadline=None,
    clock=time.monotonic,
    read_timeout=None,
) -> tuple[list[dict], str]:
    name, given = use.get("name"), use.get("input") or {}
    try:
        if name == "site_history":
            rows = [{k: e.get(k) for k in profile.history_keys} for e in history]
            return [{"json": {"scenes": rows}}], "success"
        if name == "previous_scene":
            if len(history) < 2:
                return [{"text": "There is no previous scene for this site."}], "success"
            previous = history[-2]
            body = [{"json": summarise_metrics(previous, profile.metric_keys)}]
            image = _safe_image(load_image, previous.get("image_key"))
            if image:
                body.append({"image": {"format": "png", "source": {"bytes": image}}})
            return body, "success"
        if name == "look_again":
            if not isinstance(given, dict):
                return [{"text": "look_again takes an object of settings."}], "error"
            if deadline is not None and deadline - clock() < MIN_SECONDS_TO_LOOK_AGAIN:
                return [{"text": "No time left to re-measure. Answer now with what you have."}], "error"
            # The task, its network and the shorter read timeout are passed only when they are not
            # the defaults, so a worker stand-in written before they existed is still called as
            # before (the ships adapter's own tests hold that).
            extra = {}
            if profile.task != contract.DEFAULT_TASK:
                extra["task"] = profile.task
            if network is not None:
                extra["network"] = network
            if read_timeout is not None:
                extra["read_timeout"] = read_timeout
            result = measure(
                {"id": site["id"], "polygon": site["polygon"]},
                scene,
                backend=backend,
                params={**params, **given},
                coverage_floor=0.0,
                **extra,
            )
            metrics = summarise_metrics({**result.metrics, "scene_id": scene.get("id")}, profile.metric_keys)
            body = [{"json": {"settings": {**params, **given}, **metrics}}]
            if result.image_png:
                body.append({"image": {"format": "png", "source": {"bytes": result.image_png}}})
            return body, "success"
        if name == "web_context" and profile.web_query is not None:
            if web_search is None:
                return [{"text": "News search is not available for this site."}], "error"
            return _web_context(profile, site, web_search, deadline, clock)
        return [{"text": f"There is no tool called {name!r}."}], "error"
    except vision_client.VisionError as exc:
        return [{"text": f"The measurement failed: {exc.code}."}], "error"
    except Exception as exc:  # noqa: BLE001 - a tool failure is the model's to work around
        return [{"text": f"The tool failed ({type(exc).__name__})."}], "error"


def _web_context(profile: TaskProfile, site, web_search, deadline, clock) -> tuple[list[dict], str]:
    """Recent headlines for the site, as the model's `web_context` result: at most
    WEB_CONTEXT_MAX_RESULTS from the last WEB_CONTEXT_MAX_AGE_HOURS, within WEB_CONTEXT_SECONDS
    (or the deadline, if sooner). Any failure of the search is an error result, not an exception:
    the model can decide without it."""
    query = profile.web_query(site.get("name") or site.get("id") or "")
    until = clock() + WEB_CONTEXT_SECONDS
    if deadline is not None:
        until = min(deadline, until)
    try:
        found = web_search(
            query,
            max_results=WEB_CONTEXT_MAX_RESULTS,
            max_age_hours=WEB_CONTEXT_MAX_AGE_HOURS,
            deadline=until,
        )
    except Exception as exc:  # noqa: BLE001 - the news is a help, never a dependency
        return [{"text": f"News search failed ({type(exc).__name__})."}], "error"
    rows = [
        {key: item.get(key) for key in ("title", "url", "published_at", "source", "snippet")}
        for item in (found or [])[:WEB_CONTEXT_MAX_RESULTS]
        if isinstance(item, dict)
    ]
    return [{"json": {"query": query, "results": rows}}], "success"


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
