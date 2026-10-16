"""The decision ring: one entry per verdict in each site's running state, its evidence lines, and
the trace a reader builds from an entry.

docs/enhancements/vision-evidence.md. The two vision adapters share this module, so their records
cannot drift (vision_sites.site_status is the pattern). It knows no adapter: what is adapter-specific
(the measure keys and their words, which input each measure depends on, the look_again settings)
comes in through the adapter's `decision_vocabulary()`. No cv2, numpy or boto3: the adapter registry
imports this through the adapters, and every pipeline Lambda imports the registry.

**The ring** is `state["sites"][site_id]["decisions"]`, the last DECISIONS_KEPT entries. It lives
in the site's record because both adapters' `fetch_state` deep-copy each record from the previous
state and rebuild only the top level (which is why the agent's `triage` dict used to be lost on the
next tick). One entry per verdict of `assess()`, written on every return path of `material_diff`
(`record`), and never twice for the same scene and tick. An entry holds the numbers the decision was
made from (the measure, the baseline, the thresholds as stored, so a decision can be explained after
the ring rolls over), the code the decision came to, which measures moved before the agent was asked,
whether the rail network copy differed from the baseline's, the agent's trail with each tool's
result, who measured (backend, build, CPU, the worker's request id) and whether the site was
reported. It is the one place that keeps the agent's reason and the hosts of the news it read: both
are third-party or model text, shown only in an "(unverified)" cell on the page, never logged.

**The code rule** (`decision_code`) reads only fields `assess()` and `_triage` already set and
compares no measure with a threshold, so it cannot drift from the adapter's rule; `moved` is the
adapter's own list, written by `_judge` from the same conditions as its reasons.

**The trace** (`trace`) is the one rendering of a decision's six steps (measured, compared, each
tool, the verdict, the action, the person), used by every reader so they agree. It states facts
only: a `look_again` row says what was re-measured at which settings, beside the same scene's
numbers at the topic's settings, and never compares a re-measurement with the baseline (the
adapter itself refuses to compare scenes measured with different parameters). The words for its
codes are TRACE_WORDS and the adapter's vocabulary; the rows hold codes, ids and numbers.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime

from common import evidence
from common.costing import USD_TO_AUD_RATE

DECISION_VERSION = 1
# Sentinel-2 revisits a tile every 2-5 days: 12 scene decisions cover 4-8 weeks; the logs hold 90
# days and the export keeps the rest.
DECISIONS_KEPT = 12
# What a decision came to. The last five are vision_sites.VERDICT_WORDS' keys; the first is the
# topic's first observation, reported without a baseline.
CODES = ("first_observation", "below_floor", "baseline_building", "within_range", "reported", "artefact")
# What OpenCV's output did, in the trace's `compared` and `tool` rows.
EFFECTS = (
    "kept_out_cloud", "kept_out_baseline", "kept_out_range", "sent_to_agent", "reported_first",
    "reported_untriaged", "re_measured",
)  # fmt: skip
# Which input a moved measure came from, by the vocabulary's declaration and `network_changed`.
SOURCES = ("imagery", "network", "imagery_and_network")
# What the pipeline did with the decision (the trace's `action` row).
ACTIONS = ("finding_written", "no_finding", "not_kept", "finding_lost", "finding_would_be_written")
# What a person did with the article (the trace's `person` row).
PERSONS = (
    "held", "approved", "rejected", "unpublished", "published_unreviewed", "no_article_yet",
    "finding_expired",
)  # fmt: skip
# A moderation decision's code (common/evidence.py's moderation_decision), as the person's state.
_DECISION_TO_PERSON = {
    "approved": "approved",
    "force_published": "approved",
    "rejected": "rejected",
    "unpublished": "unpublished",
    "sent_back": "held",
}
# A Finding expires after this many days (research_tick_handler.FINDING_TTL_DAYS; repeated so this
# module imports no handler).
FINDING_TTL_DAYS = 14
# The fixed words every reader puts beside the trace's codes. An adapter's `decision_vocabulary`
# adds the measures' and settings' words. Every phrase passes the vocabulary's own check
# (letters, digits, spaces, a few marks).
TRACE_WORDS = {
    "steps": {
        "measured": "OpenCV measured the scene",
        "compared": "the numbers were compared with the baseline",
        "tool": "the agent used a tool",
        "verdict": "the agent decided",
        "action": "what the pipeline did",
        "person": "what a person did",
    },
    "codes": {
        "first_observation": "the first observation, reported once",
        "below_floor": "too cloudy to count",
        "baseline_building": "still building its baseline",
        "within_range": "within the usual range",
        "reported": "changed, and reported",
        "artefact": "changed, and the triage agent judged it an artefact",
    },
    "effects": {
        "kept_out_cloud": "kept out, too much of the site was cloud",
        "kept_out_baseline": "kept out, the baseline is still building",
        "kept_out_range": "kept out, within the usual range",
        "sent_to_agent": "sent to the agent",
        "reported_first": "reported as the first observation",
        "reported_untriaged": "reported, triage is off for this topic",
        "re_measured": "re-measured the same scene at other settings",
    },
    "sources": {
        "imagery": "from the imagery",
        "network": "from the OpenStreetMap network",
        "imagery_and_network": "from the imagery and the OpenStreetMap network",
    },
    "verdicts": {"real": "real", "artefact": "an artefact", "failed_closed": "failed closed, no verdict"},
    "actions": {
        "finding_written": "a Finding was written",
        "no_finding": "no Finding, the change was not reported",
        "not_kept": "not kept, the tick failed and the scene is measured again",
        "finding_lost": "the tick failed before the Finding was written",
        "finding_would_be_written": "a Finding would be written (a replay)",
    },
    "persons": {
        "held": "held for a person",
        "approved": "approved by a person",
        "rejected": "rejected by a person",
        "unpublished": "taken down by a person",
        "published_unreviewed": "published with no review",
        "no_article_yet": "no article yet",
        "finding_expired": "the Finding expired before an article was drafted",
    },
}
# Every word the vocabulary and TRACE_WORDS may hold: plain words, digits, a few marks.
WORD = re.compile(r"^[A-Za-z0-9 ,'()%²-]{1,60}$")
_HEX = re.compile(r"^[0-9a-f]{12,}$")


# --- the code, the moves, who measured --------------------------------------------------------------


def decision_code(verdict: dict, *, floor: float, first_clear) -> tuple[str, bool]:
    """(code, reported) for one verdict of `assess()`: below the floor by the same first test as
    `assess` (a cloudy scene is never a first observation, whatever the caller passed); a first
    observation when the site is in `first_clear` (vision_sites.first_observation's clear verdicts'
    site ids, None once the topic has been observed); a baseline still building when the verdict
    has none; within range when nothing moved and no agent was asked; an artefact when the agent
    said so (or failed closed); else reported (triaged real, or triage off and material). Reads
    only fields `assess()` and `_triage` set: no measure is compared with a threshold here, so it
    cannot drift from the adapter's rule (vision_sites.verdict_status gives the same code for the
    newest scene; a test holds the two equal)."""
    if (verdict.get("coverage") or 0) < floor:
        return "below_floor", False
    if first_clear and verdict.get("site_id") in first_clear:
        return "first_observation", True
    if verdict.get("baseline") is None:
        return "baseline_building", False
    if "triage" not in verdict and not verdict.get("material"):
        return "within_range", False
    if "triage" in verdict and (verdict["triage"] or {}).get("verdict") != "real":
        return "artefact", False
    return "reported", True


def moved_by_site(verdicts: list[dict]) -> dict[str, list[str]]:
    """{site id: the measure codes the adapter's `_judge` named as moved}, read from the verdicts
    before anything rewrites them."""
    return {
        v["site_id"]: [code for code in (v.get("moved") or []) if isinstance(code, str)]
        for v in verdicts
        if isinstance(v, dict) and "site_id" in v
    }


def measured_by(result, seconds: float | None) -> dict:
    """Who measured, from a worker result (common/vision_client.VisionResult): the backend, the
    build's SHA-256 prefix, the CPU the worker reported, the worker's request id and the client's
    wall time in ms."""
    reply = result.reply if isinstance(getattr(result, "reply", None), dict) else {}
    build = reply.get("build") if isinstance(reply.get("build"), dict) else {}
    return {
        "backend": reply.get("backend") if isinstance(reply.get("backend"), str) else None,
        "build": build_prefix(build.get("build_sha256")),
        "cpu": build.get("cpu") if isinstance(build.get("cpu"), str) else None,
        "worker_request_id": getattr(result, "request_id", None),
        "worker_ms": _ms(seconds),
        "code": None,
    }


def failed_by(exc, seconds: float | None, *, backend: str | None = None, request_id=None) -> dict:
    """Who was asked when the measurement failed (common/vision_client.VisionError): the request
    id the worker answered with, if it answered, the client's wall time, and the error's code."""
    return {
        "backend": backend,
        "build": None,
        "cpu": None,
        "worker_request_id": getattr(exc, "request_id", None) or request_id,
        "worker_ms": _ms(seconds),
        "code": getattr(exc, "code", None),
    }


def build_prefix(sha256) -> str | None:
    """The first 12 hex characters of a build's SHA-256, or None."""
    if isinstance(sha256, str) and _HEX.match(sha256):
        return sha256[: evidence.HEX_PREFIX]
    return None


# --- the lines the adapters print --------------------------------------------------------------------


def site_line(site_id: str, outcome: str, *, code=None, scene_id=None, budget=None, visit=None) -> None:
    """One `vision_site` line, right after a site's turn: the outcome (`measured`, `deferred`,
    `not_reached`, `none`, `no_scene`), the error code this tick stamped on the site, the scene
    measured or failed, the worker call (`visit`: measured_by or failed_by) and the budget left."""
    visit = visit or {}
    evidence.emit(
        "vision_site",
        site_id=site_id,
        outcome=outcome,
        code=code,
        scene_id=scene_id,
        worker_request_id=visit.get("worker_request_id"),
        worker_ms=visit.get("worker_ms"),
        budget_left_s=budget.remaining() if budget is not None else None,
    )


def triage_line(site_id: str, scene_id: str | None, outcome: dict) -> None:
    """One `vision_triage` line, right after the agent's outcome for a site: the verdict and how
    the loop stopped, the model and its spend, the tools in call order, the steps with their
    results, every worker request id of the triage, and the reason's SHA-256 prefix (so a reason
    quoted from the ring can be checked against the log without logging it)."""
    outcome = outcome if isinstance(outcome, dict) else {}
    steps = [step for step in outcome.get("steps") or [] if isinstance(step, dict)]
    tools = [step["tool"] for step in steps if isinstance(step.get("tool"), str)]
    worker_ids = []
    for step in steps:
        result = step.get("result") if isinstance(step.get("result"), dict) else {}
        request_id = result.get("worker_request_id")
        if isinstance(request_id, str) and request_id not in worker_ids:
            worker_ids.append(request_id)
    reason = outcome.get("reason")
    evidence.emit(
        "vision_triage",
        site_id=site_id,
        scene_id=scene_id,
        verdict=outcome.get("verdict"),
        stop=outcome.get("stop"),
        model_id=outcome.get("model_id"),
        model_calls=outcome.get("model_calls"),
        input_tokens=outcome.get("input_tokens"),
        output_tokens=outcome.get("output_tokens"),
        cost_usd=outcome.get("cost_usd"),
        elapsed_ms=outcome.get("elapsed_ms"),
        deadline_left_s=outcome.get("deadline_left_s"),
        figure=outcome.get("figure"),
        tools=tools,
        tools_total=len(tools),
        look_again=sum(1 for tool in tools if tool == "look_again"),
        budget_refusals=outcome.get("budget_refusals"),
        steps=steps,
        steps_total=outcome.get("steps_total", len(steps)),
        worker_request_ids=worker_ids,
        reason_sha256=reason_prefix(reason),
    )


def reason_prefix(reason) -> str | None:
    """The first 12 hex characters of the SHA-256 of the agent's reason (UTF-8), or None."""
    if not isinstance(reason, str) or not reason:
        return None
    return hashlib.sha256(reason.encode("utf-8")).hexdigest()[: evidence.HEX_PREFIX]


# --- the ring ---------------------------------------------------------------------------------------


def record(
    new_state: dict,
    verdicts: list[dict],
    *,
    moved: dict[str, list[str]],
    first_clear,
    floor: float,
    visits: dict,
    vocabulary: dict,
) -> list[dict]:
    """Append one entry per verdict to its site's ring in `new_state`, trim each ring to
    DECISIONS_KEPT, and print one `vision_decision` line per entry appended. `moved` is
    moved_by_site's; `first_clear` the first observation's clear verdicts' site ids (None when
    the topic was observed before); `visits` the adapter's {site id: measured_by | failed_by} of
    this tick; `vocabulary` the adapter's decision_vocabulary(). An entry already at the end of the
    ring (the same scene and tick) is not appended twice. Returns the entries appended."""
    appended = []
    at = new_state.get("fetched_at")
    thresholds = _numbers(new_state.get("thresholds"))
    trails = new_state.get("triage") if isinstance(new_state.get("triage"), dict) else {}
    for verdict in verdicts:
        if not isinstance(verdict, dict) or not isinstance(verdict.get("site_id"), str):
            continue
        site_id = verdict["site_id"]
        site = (new_state.get("sites") or {}).get(site_id)
        if not isinstance(site, dict):
            continue
        ring = site.get("decisions")
        if not isinstance(ring, list):
            ring = []
        scene_id = verdict.get("scene_id")
        last = ring[-1] if ring and isinstance(ring[-1], dict) else {}
        if (last.get("scene_id"), last.get("at")) == (scene_id, at):
            continue
        history = site.get("history") if isinstance(site.get("history"), list) else []
        entries = [e for e in history if isinstance(e, dict) and e.get("scene_id") == scene_id]
        latest = entries[-1] if entries else {}
        code, reported = decision_code(verdict, floor=floor, first_clear=first_clear)
        measures = (vocabulary or {}).get("measures") or {}
        triage = _triage_part(trails.get(site_id)) if "triage" in verdict else None
        entry = {
            "v": DECISION_VERSION,
            "at": at,
            "tick": evidence.bound("tick"),
            "run": evidence.bound("run"),
            "scene_id": scene_id,
            "captured_at": verdict.get("captured_at") or latest.get("captured_at"),
            "coverage": verdict.get("coverage"),
            "measure": _measure(latest, verdict, measures),
            "baseline": _keyed(verdict.get("baseline"), measures),
            "delta": _keyed(verdict.get("delta"), measures),
            "relative": _keyed(verdict.get("relative"), measures),
            "baseline_scenes": verdict.get("baseline_scenes"),
            "thresholds": thresholds,
            "code": code,
            "moved": list(moved.get(site_id) or []),
            "network_changed": verdict.get("network_changed"),
            "triage": triage,
            "reported": reported,
            "measured_by": _measured_by(visits.get(site_id), latest),
        }
        ring = [*ring, entry][-DECISIONS_KEPT:]
        site["decisions"] = ring
        appended.append(entry)
        who = entry["measured_by"]
        evidence.emit(
            "vision_decision",
            site_id=site_id,
            scene_id=scene_id,
            at=at,
            captured_at=entry["captured_at"],
            coverage=entry["coverage"],
            measure=entry["measure"],
            baseline=entry["baseline"],
            delta=entry["delta"],
            relative=entry["relative"],
            baseline_scenes=entry["baseline_scenes"],
            thresholds=thresholds,
            code=code,
            moved=entry["moved"],
            network_changed=entry["network_changed"],
            triaged=triage is not None,
            verdict=(triage or {}).get("verdict"),
            stop=(triage or {}).get("stop"),
            reported=reported,
            backend=who.get("backend"),
            build=who.get("build"),
            cpu=who.get("cpu"),
            worker_request_id=who.get("worker_request_id"),
        )
    return appended


def _measure(latest: dict, verdict: dict, measures: dict) -> dict:
    """The vocabulary's measures of the newest entry (the verdict's copy when the entry lacks one):
    numbers only."""
    found = {}
    for key in measures:
        value = latest.get(key, verdict.get(key))
        if _is_number(value):
            found[key] = value
    return found


def _keyed(value, measures: dict) -> dict | None:
    """A verdict's baseline, delta or relative as a dict of numbers: the rail adapter's dict as it
    is, the ships adapter's one number keyed by the vocabulary's first measure."""
    if value is None:
        return None
    if isinstance(value, dict):
        return _numbers(value)
    if _is_number(value) and measures:
        return {next(iter(measures)): value}
    return None


def _numbers(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str) and _is_number(item)}


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _triage_part(outcome) -> dict | None:
    """The agent's part of an entry, from the outcome `_triage` stored: the trail's numbers and
    steps (the first MAX_STEPS, with the total), the hosts and the reason (the two kinds of
    third-party text the ring alone keeps)."""
    if not isinstance(outcome, dict):
        return None
    steps = [step for step in outcome.get("steps") or [] if isinstance(step, dict)]
    hosts = [host for host in outcome.get("hosts") or [] if isinstance(host, str)]
    return {
        "verdict": outcome.get("verdict"),
        "stop": outcome.get("stop"),
        "model_id": evidence.check("model", outcome.get("model_id")),
        "model_calls": outcome.get("model_calls"),
        "input_tokens": outcome.get("input_tokens"),
        "output_tokens": outcome.get("output_tokens"),
        "cost_usd": outcome.get("cost_usd"),
        "elapsed_ms": outcome.get("elapsed_ms"),
        "deadline_left_s": outcome.get("deadline_left_s"),
        "figure": outcome.get("figure"),
        "steps": steps[: evidence.MAX_STEPS],
        "steps_total": outcome.get("steps_total", len(steps)),
        "budget_refusals": outcome.get("budget_refusals"),
        "hosts": hosts,
        "reason": outcome.get("reason") if isinstance(outcome.get("reason"), str) else None,
    }


def _measured_by(visit, latest: dict) -> dict:
    """The tick's worker call for the site (`visits`), else what the history entry kept."""
    if isinstance(visit, dict):
        return {key: visit.get(key) for key in ("backend", "build", "cpu", "worker_request_id", "worker_ms")}
    return {
        "backend": latest.get("backend") if isinstance(latest.get("backend"), str) else None,
        "build": build_prefix(latest.get("build_sha256")),
        "cpu": None,
        "worker_request_id": None,
        "worker_ms": None,
    }


def _ms(seconds) -> int | None:
    if not _is_number(seconds):
        return None
    return max(0, int(round(seconds * 1000.0)))


# --- the trace --------------------------------------------------------------------------------------


def trace(
    entry: dict,
    *,
    vocabulary: dict,
    attempt: dict | None = None,
    article: dict | None = None,
    person: dict | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """The six-step trace of one ring entry, as rows of codes, ids and numbers: `measured`,
    `compared`, one `tool` row per step, `verdict`, `action` and `person`. Each row is {"step",
    "n", "code", "numbers", "ids", "effect", "sources", "time"}; the caller adds the words from
    TRACE_WORDS and `vocabulary`. `attempt` is the parsed `research_tick` line of the entry's tick
    ({"stored", "finding"}; {"replay": True} for a replay; None when there is none: not kept);
    `article` the Articles row the Finding fed ({"status", "published_by", "created_at",
    "published_at"}); `person` a parsed `moderation_decision` ({"decision", "at"}). Facts only: a
    `look_again` row carries the re-measurement beside the same scene's numbers at the topic's
    settings, and no row compares a re-measurement with the baseline."""
    rows = []
    at = entry.get("at")
    who = entry.get("measured_by") if isinstance(entry.get("measured_by"), dict) else {}
    measure = _numbers(entry.get("measure"))
    rows.append(_row(
        "measured", 0, "measured",
        numbers={"coverage": entry.get("coverage"), **measure, "worker_ms": who.get("worker_ms")},
        ids={"scene_id": entry.get("scene_id"), "backend": who.get("backend"), "build": who.get("build"),
             "cpu": who.get("cpu"), "worker_request_id": who.get("worker_request_id")},
        time=entry.get("captured_at") or at,
    ))  # fmt: skip

    code = entry.get("code")
    triage = entry.get("triage") if isinstance(entry.get("triage"), dict) else None
    numbers = {"baseline_scenes": entry.get("baseline_scenes")}
    for prefix, key in (("baseline", "baseline"), ("delta", "delta"), ("relative", "relative"),
                        ("threshold", "thresholds")):  # fmt: skip
        for name, value in _numbers(entry.get(key)).items():
            numbers[f"{prefix}_{name}"] = value
    rows.append(_row(
        "compared", 0, code, numbers=numbers,
        effect=_effect(code, triage is not None),
        sources=_sources(entry.get("moved") or [], entry.get("network_changed"), vocabulary),
        time=at,
    ))  # fmt: skip

    if triage is not None:
        for n, step in enumerate(triage.get("steps") or [], start=1):
            rows.append(_tool_row(n, step, measure, at))
        stop = triage.get("stop")
        verdict = triage.get("verdict") if stop == "answered" else "failed_closed"
        cost = triage.get("cost_usd")
        rows.append(_row(
            "verdict", 0, verdict,
            numbers={"model_calls": triage.get("model_calls"), "input_tokens": triage.get("input_tokens"),
                     "output_tokens": triage.get("output_tokens"),
                     "cost_aud": round(cost * USD_TO_AUD_RATE, 6) if _is_number(cost) else None,
                     "elapsed_ms": triage.get("elapsed_ms"),
                     "deadline_left_s": triage.get("deadline_left_s")},
            ids={"stop": stop, "model_id": triage.get("model_id")},
            time=at,
        ))  # fmt: skip

    action = _action(entry, attempt)
    finding_at = (attempt or {}).get("finding") if action == "finding_written" else None
    rows.append(_row("action", 0, action, time=finding_at or at))
    if action in ("finding_written", "finding_would_be_written") or article is not None or person is not None:
        rows.append(_person_row(entry, article, person, now))
    return rows


def _row(step, n, code, *, numbers=None, ids=None, effect=None, sources=None, time=None) -> dict:
    return {
        "step": step,
        "n": n,
        "code": code,
        "numbers": {key: value for key, value in (numbers or {}).items() if _is_number(value)},
        "ids": {key: value for key, value in (ids or {}).items() if isinstance(value, str)},
        "effect": effect,
        "sources": sources or {},
        "time": time if isinstance(time, str) else None,
    }


def _effect(code, triaged: bool) -> str | None:
    if code == "first_observation":
        return "reported_first"
    if code == "below_floor":
        return "kept_out_cloud"
    if code == "baseline_building":
        return "kept_out_baseline"
    if code == "within_range":
        return "kept_out_range"
    if code in ("reported", "artefact"):
        return "sent_to_agent" if triaged else "reported_untriaged"
    return None


def _sources(moved: list, network_changed, vocabulary: dict) -> dict:
    """Which input each moved measure came from: the vocabulary's declaration, narrowed to the
    imagery when a measure depends on both and the network copy was the baseline's."""
    declared = (vocabulary or {}).get("sources") or {}
    sources = {}
    for code in moved:
        source = declared.get(code)
        if source not in SOURCES:
            continue
        if source == "imagery_and_network" and network_changed is False:
            source = "imagery"
        sources[code] = source
    return sources


def _tool_row(n: int, step: dict, measure: dict, at) -> dict:
    tool, ok = step.get("tool"), bool(step.get("ok"))
    result = step.get("result") if isinstance(step.get("result"), dict) else {}
    numbers, ids, effect = {"ms": step.get("ms")}, {}, None
    code = tool if ok else (result.get("code") or tool)
    if tool == "look_again" and ok:
        effect = "re_measured"
        for key, value in _numbers(result.get("settings")).items():
            numbers[f"setting_{key}"] = value
        measured = _numbers(result.get("measured"))
        for key, value in measured.items():
            numbers[f"measured_{key}"] = value
            if key in measure:
                numbers[f"at_topic_settings_{key}"] = measure[key]
        numbers["worker_ms"] = result.get("worker_ms")
    elif ok:
        for key in ("results", "dated", "scenes"):
            if key in result:
                numbers[key] = result[key]
        if isinstance(result.get("figure"), bool):
            numbers["figure"] = int(result["figure"])
    for key in ("worker_request_id", "build", "scene_id"):
        if isinstance(result.get(key), str):
            ids[key] = result[key]
    return _row("tool", n, code, numbers=numbers, ids=ids, effect=effect, time=at)


def _action(entry: dict, attempt: dict | None) -> str:
    if attempt is None or not attempt.get("stored"):
        return "not_kept"
    if not entry.get("reported"):
        return "no_finding"
    if attempt.get("replay"):
        return "finding_would_be_written"
    if attempt.get("finding") == entry.get("at"):
        return "finding_written"
    return "finding_lost"


def _person_row(entry: dict, article: dict | None, person: dict | None, now: datetime | None) -> dict:
    code, when = None, None
    if isinstance(person, dict) and person.get("decision") in _DECISION_TO_PERSON:
        code, when = _DECISION_TO_PERSON[person["decision"]], person.get("at")
    elif isinstance(article, dict):
        status, by = article.get("status"), article.get("published_by")
        when = article.get("published_at") or article.get("created_at")
        if status == "pending_moderation":
            code = "held"
        elif status == "published":
            code = "approved" if by == "humans" else "published_unreviewed"
        elif status == "rejected":
            code = "rejected"
        else:
            code = "held"
    elif now is not None and _expired(entry.get("at"), now):
        code = "finding_expired"
    else:
        code = "no_article_yet"
    numbers = {}
    hours = _hours_between(entry.get("at"), when)
    if hours is not None:
        numbers["hours_after_finding"] = hours
    return _row("person", 0, code, numbers=numbers, time=when)


def _expired(at, now: datetime) -> bool:
    try:
        then = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return False
    then = then if then.tzinfo else then.replace(tzinfo=UTC)
    return (now - then).days >= FINDING_TTL_DAYS


def _hours_between(start, end) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    a = a if a.tzinfo else a.replace(tzinfo=UTC)
    b = b if b.tzinfo else b.replace(tzinfo=UTC)
    return round((b - a).total_seconds() / 3600.0, 2)


def vocabulary_words(vocabulary: dict) -> list[str]:
    """Every phrase a decision_vocabulary offers to be shown or spoken: the measures' and settings'
    labels and units, and the moved measures' words. A test holds each to WORD."""
    words = []
    for key in ("measures", "settings"):
        for item in ((vocabulary or {}).get(key) or {}).values():
            words += [item.get("label"), item.get("unit")] if isinstance(item, dict) else []
    words += list(((vocabulary or {}).get("moved") or {}).values())
    return [word for word in words if isinstance(word, str) and word]
