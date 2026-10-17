"""Satellite vision: count what OpenCV sees at fixed sites, scene by scene, and report real change.

docs/enhancements/archive/opencv-agentic-vision-enhancement.md is the design. This adapter is the thin,
topic-agnostic part of it: which sites, which scenes, the history and the diff. It has no OpenCV;
the measuring is done by the vision worker (vision_worker_handler.py), which may run in another
region, through common/vision_client.py. What it shares with the rail_access adapter (the STAC
search, the figure store, the time budget, the site order) lives in common/vision_sites.py. What
a topic watches is configuration, not code:

    adapter_config = {
      "sites": [{"id": "botany-bay", "name": "Botany Bay anchorage",
                 "polygon": [[lon, lat], ...]}],          # required; 1 to MAX_SITES
      "object_noun": "large vessels",                      # how articles name what is counted
      "backend": "opencv",                                 # or "cool" (docs §4)
      "params": {...},                                     # vision.detect.DetectParams overrides
      "max_cloud_cover": 60,                               # scene-level filter in the search, %
      "lookback_days": 10,
      "coverage_floor": 0.7,                               # below it a count is never material
      "history_size": 8, "min_baseline": 2,
      "relative_threshold": 0.35, "absolute_threshold": 5,
      "max_sites_per_tick": 5, "time_budget_seconds": 60,  # the budget is capped at 80
      "triage": true, "triage_max_tool_calls": 3,          # the agent (common/vision_triage.py)
      "triage_deadline_seconds": 85,                       # the agent answers by then, from the
    }                                                      # tick's start (the tick itself has 120 s)

**Per tick**, for each site in turn (those never visited first, then the longest unvisited, so a
topic with more sites than it measures per tick rotates through them): find the newest Sentinel-2
L2A scene over it (Earth Search STAC, free, no key); if it is one the site already has, do nothing;
otherwise ask the worker to measure it and append the result to the site's history. The worker is
asked only while common/vision_sites.MEASURE_RESERVE_SECONDS of the budget remain, and waited on for
WORKER_READ_TIMEOUT (vision_sites.time_to_measure, the rule rail_access keeps too); a site left
without time is deferred to the next tick and keeps its place. A site that can't be searched or
measured keeps its history and records `last_error`: not measured is never "nothing there".

**The agent decides.** A numeric change is only a candidate: before it becomes a Finding, the
triage agent (common/vision_triage.py) looks at the figure and the evidence, may re-measure the
scene with other settings or compare the previous one, and answers "real" or "artefact". Only
"real" is material; anything unsure is "artefact" (fail closed). Its trail is kept in the state
(`triage`, and on the scene's history entry), so every decision can be replayed. It runs inside
`material_diff`, after the thresholds, so rule 2 holds: no model call without a numeric change.

**Material** when a site's new count differs from the median of its previous clear scenes by at
least `absolute_threshold` *and* `relative_threshold`, the new scene's coverage is at least
`coverage_floor`, and there are at least `min_baseline` earlier clear scenes. The first tick with
a scene at or above the coverage floor is material (the adapter contract's first observation); a
tick before that, with nothing measured (no scene in the lookback, the worker unreachable) or only
scenes below the floor, is not: a Finding with nothing in it, or a count made over a third of the site,
would still cost a model call and make an empty or misleading article. The state records when it was
(`first_observed_at`), so it is reported once, however the history is trimmed or the floor raised
later. Rule 2 holds: every Bedrock call comes after this.

**State between ticks.** The baseline must include every scene measured, not only those that were
reported, so this adapter sets `keeps_running_state`: the research tick keeps its state after a
no-change tick too (research_tick_handler.py). The figure for each scene is stored in the content
bucket under `vision/<topic>/<site>/<scene>.png`; the snapshot holds only its key.

The data licences and the credits are in common/vision_sites.py, beside the search.
"""

from __future__ import annotations

import copy
import statistics
import time
from datetime import UTC, datetime

from common import vision_client, vision_decisions, vision_sites, vision_triage
from common.adapters.base import Adapter, render_review_evidence
from common.model_routing import resolve_model
from common.vision_sites import (
    ASSETS_SHIPS,
    AWS_OPEN_DATA_SOURCE,
    COPERNICUS_SOURCE,
    MAX_SITES,
    STAC_COLLECTION,
    STAC_ITEM_URL,
    STAC_SEARCH_URL,
    Budget,
    ConfigError,
    clear_entries,
    load_image,
    order_sites,
    plain,
    search_newest_scene,
    site_bbox,
    store_image,
)

# The names this module had before common/vision_sites.py existed, still importable from here.
__all__ = [
    "ASSETS",
    "AWS_OPEN_DATA_SOURCE",
    "COPERNICUS_SOURCE",
    "DEFAULTS",
    "MAX_SITES",
    "STAC_COLLECTION",
    "STAC_ITEM_URL",
    "STAC_SEARCH_URL",
    "THRESHOLD_KEYS",
    "ConfigError",
    "SatelliteVisionAdapter",
    "clear_entries",
    "parse_config",
    "plain",
    "search_newest_scene",
    "site_bbox",
]

# Earth Search v1's asset keys for the bands the worker reads.
ASSETS = ASSETS_SHIPS

THRESHOLD_KEYS = ("coverage_floor", "min_baseline", "relative_threshold", "absolute_threshold")
DEFAULTS = {
    "object_noun": "objects",
    "backend": "opencv",
    "params": {},
    "max_cloud_cover": 60,
    "lookback_days": 10,
    "coverage_floor": 0.7,
    "history_size": 8,
    "min_baseline": 2,
    "relative_threshold": 0.35,
    "absolute_threshold": 5,
    "max_sites_per_tick": 5,
    "time_budget_seconds": 60,
    "triage": True,
    "triage_max_tool_calls": 3,
    # Seconds after the tick started by which the agent must have answered. The research tick's Lambda
    # has 120 s: the measuring loop keeps to time_budget_seconds, the agent to this, and what is left
    # is for the summary call and the stores. The agent treats running out of time as an artefact.
    "triage_deadline_seconds": 85,
}
_FLOAT_KEYS = vision_sites.FLOAT_KEYS + ("relative_threshold", "absolute_threshold")
# What the operator's assistant calls a site's count (status): fixed words, never `object_noun`,
# which is the operator's own text and is never spoken.
MEASURE_LABEL = "objects counted"
# The adapter's fixed words for its evidence (Adapter.decision_vocabulary): the measures a decision
# ring entry keeps, the words for the one measure `_judge` can name as moved, which input it depends
# on (the imagery alone), and the words for look_again's settings (the worker contract's
# parameters). Never `object_noun`.
DECISION_VOCABULARY = {
    "measures": {
        "count": {"label": "objects counted", "unit": "", "scale": 1},
        "density_per_km2": {"label": "objects per square kilometre", "unit": "per km²", "scale": 1},
        "clear_water_km2": {"label": "clear water", "unit": "km²", "scale": 1},
    },
    "moved": {"count": "the count"},
    "sources": {"count": "imagery"},
    "settings": {
        "stretch_max": {"label": "stretch ceiling", "unit": ""},
        "block_size": {"label": "block size", "unit": "px"},
        "offset": {"label": "threshold offset", "unit": ""},
        "min_length_m": {"label": "shortest object", "unit": "m"},
        "max_length_m": {"label": "longest object", "unit": "m"},
        "min_elongation": {"label": "least elongation", "unit": ""},
        "edge_buffer_px": {"label": "shore buffer", "unit": "px"},
    },
}


def parse_config(adapter_config: dict | None) -> dict:
    """adapter_config with defaults filled in, or ConfigError (common/vision_sites.py has the
    checks every vision topic shares)."""
    return vision_sites.parse_site_config(adapter_config, DEFAULTS, vision_sites.WHOLE_KEYS, _FLOAT_KEYS)


class SatelliteVisionAdapter(Adapter):
    uses_previous_state = True
    keeps_running_state = True
    # Measures through the vision worker, and its sites are written by hand (base.py says why).
    uses_vision_worker = True
    config_by_hand = True
    sources = (COPERNICUS_SOURCE, AWS_OPEN_DATA_SOURCE)

    # Injected by tests; the defaults talk to Earth Search, the worker, S3 and Bedrock.
    http_post = None
    measure = staticmethod(vision_client.measure)
    store_image = staticmethod(store_image)
    load_image = staticmethod(load_image)
    triage_agent = staticmethod(vision_triage.triage)
    choose_model = staticmethod(resolve_model)
    clock = staticmethod(time.monotonic)

    # Set by fetch_state for material_diff on the same instance.
    _topic: dict | None = None
    _config: dict | None = None
    _started: float | None = None
    # Set by fetch_state for the decision ring: each site's worker call this tick (site id ->
    # vision_decisions.measured_by or failed_by), for the evidence lines and the ring entry.
    _visits: dict | None = None

    def fetch_state(self, topic_config: dict, previous_state: dict | None = None) -> dict:
        config = parse_config(topic_config.get("adapter_config"))
        # Kept for material_diff on this same instance (the research tick makes one per tick): the
        # triage agent needs the topic's model and the sites' polygons, and the diff is only given
        # the two states.
        self._topic, self._config = topic_config, config
        self._visits = {}
        topic_id = topic_config.get("topic_id", "topic")
        now = datetime.now(UTC)
        budget = Budget(self.clock, config["time_budget_seconds"])
        self._started = budget.started
        previous_sites = (previous_state or {}).get("sites") or {}
        sites, measured = {}, []
        for site in config["sites"]:
            record = copy.deepcopy(previous_sites.get(site["id"])) or {"history": []}
            record["name"] = site.get("name") or site["id"]
            sites[site["id"]] = record

        # Each site's turn leaves a `vision_site` line at its branch, as it happens, so a tick that
        # times out mid-loop still leaves the lines of every site visited before it.
        for site in order_sites(config["sites"], previous_sites):
            record = sites[site["id"]]
            if len(measured) >= config["max_sites_per_tick"]:
                vision_decisions.site_line(site["id"], "not_reached", budget=budget)
                continue
            if budget.exhausted():
                record["last_error"] = {"at": now.isoformat(), "code": "time_budget", "detail": "next tick"}
                vision_decisions.site_line(site["id"], "deferred", code="time_budget", budget=budget)
                continue
            try:
                scene = search_newest_scene(site, config, now, http_post=self.http_post)
            except Exception as exc:  # noqa: BLE001 - one site's search must not stop the others
                record["last_attempt_at"] = now.isoformat()
                record["last_error"] = {"at": now.isoformat(), "code": "search", "detail": str(exc)[:300]}
                vision_decisions.site_line(site["id"], "none", code="search", budget=budget)
                continue
            known = {entry.get("scene_id") for entry in record["history"]}
            new_scene = scene is not None and scene["id"] not in known
            if new_scene and not vision_sites.time_to_measure(budget):
                # Too little of the budget left to wait on the worker (rail_access keeps the same
                # rule): not an attempt, so the site keeps its place and goes first next tick.
                record["last_error"] = {"at": now.isoformat(), "code": "time_budget", "detail": "next tick"}
                vision_decisions.site_line(site["id"], "deferred", code="time_budget", budget=budget)
                continue
            record["last_attempt_at"] = now.isoformat()
            if not new_scene:
                record.pop("last_error", None)
                vision_decisions.site_line(site["id"], "no_scene" if scene is None else "none", budget=budget)
                continue
            asked = self.clock()
            try:
                result = self.measure(
                    {"id": site["id"], "polygon": site["polygon"]},
                    {"id": scene["id"], "captured_at": scene["captured_at"], "assets": scene["assets"]},
                    backend=config["backend"],
                    params=config["params"],
                    coverage_floor=config["coverage_floor"],
                    read_timeout=vision_sites.WORKER_READ_TIMEOUT,
                )
            except vision_client.VisionError as exc:
                record["last_error"] = {"at": now.isoformat(), "code": exc.code, "detail": exc.detail[:300]}
                visit = vision_decisions.failed_by(exc, self.clock() - asked, backend=config["backend"])
                self._visits[site["id"]] = visit
                vision_decisions.site_line(
                    site["id"], "none", code=exc.code, scene_id=scene["id"], budget=budget, visit=visit
                )
                continue
            # Who measured, for the evidence lines and the ring: the worker's request id, build
            # and CPU, and the client's wall time.
            self._visits[site["id"]] = vision_decisions.measured_by(result, self.clock() - asked)

            metrics = result.metrics
            entry = {
                "scene_id": scene["id"],
                "captured_at": scene["captured_at"],
                "scene_cloud_cover": scene.get("cloud_cover"),
                "count": metrics["count"],
                "coverage": metrics["coverage"],
                "clear_water_km2": metrics.get("clear_water_km2"),
                "density_per_km2": metrics.get("density_per_km2"),
                "size_histogram": metrics.get("size_histogram"),
                "rejected": metrics.get("rejected"),
                "quality_flags": metrics.get("quality_flags", []),
                "backend": result.reply["backend"],
                "build_sha256": (result.reply.get("build") or {}).get("build_sha256"),
                "timings_ms": result.reply.get("timings_ms"),
                "image_key": None,
                # For the agent's look_again: the same bands, re-measured with other settings.
                "assets": scene["assets"],
            }
            if result.image_png:
                try:
                    entry["image_key"] = self.store_image(topic_id, site["id"], scene["id"], result.image_png)
                except Exception as exc:  # noqa: BLE001 - a lost figure doesn't lose the count
                    print(f"satellite_vision: could not store the figure for {site['id']}: {exc!r}")
            record["history"] = [*record["history"], entry][-config["history_size"] :]
            record.pop("last_error", None)
            measured.append({"site_id": site["id"], "scene_id": scene["id"]})
            vision_decisions.site_line(
                site["id"], "measured", scene_id=scene["id"], budget=budget, visit=self._visits[site["id"]]
            )

        return {
            "fetched_at": now.isoformat(),
            "object_noun": config["object_noun"],
            # The diff gets only the two states, so the topic's thresholds travel in the state.
            "thresholds": {key: config[key] for key in THRESHOLD_KEYS},
            # Set on the tick of the first observation and carried forward, so it is reported once.
            vision_sites.FIRST_OBSERVED_KEY: vision_sites.first_observed_at(
                previous_state, sites, measured, config["coverage_floor"], now.isoformat()
            ),
            "sites": sites,
            "measured": measured,
        }

    def assess(self, new_state: dict) -> list[dict]:
        """One verdict per site measured this tick: its count against its baseline, and whether
        that is material, by the thresholds stored in the state."""
        config = _thresholds(new_state)
        verdicts = []
        for m in new_state.get("measured") or []:
            record = (new_state.get("sites") or {}).get(m["site_id"]) or {}
            history = record.get("history") or []
            if not history or history[-1].get("scene_id") != m["scene_id"]:
                continue
            latest, earlier = history[-1], history[:-1]
            verdict = {
                "site_id": m["site_id"],
                "name": record.get("name") or m["site_id"],
                "scene_id": latest["scene_id"],
                "captured_at": latest.get("captured_at"),
                "count": latest["count"],
                "coverage": latest["coverage"],
            }
            verdict.update(_judge(latest, earlier, config)[1])
            verdicts.append(verdict)
        return verdicts

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        verdicts = self.assess(new_state)
        moved = vision_decisions.moved_by_site(verdicts)
        floor = {**DEFAULTS, **(new_state.get("thresholds") or {})}["coverage_floor"]
        first = vision_sites.first_observation(old_state, new_state, verdicts, floor)
        noun = new_state.get("object_noun") or DEFAULTS["object_noun"]
        if first is not None:
            clear, not_yet = first
            self._record(new_state, verdicts, moved, first_clear={v["site_id"] for v in clear}, floor=floor)
            if not clear:
                return False, not_yet
            lines = [f"{v['name']}: {v['count']} {noun} (coverage {v['coverage']:.0%})" for v in clear]
            return True, "First observation. " + "; ".join(lines)
        self._triage(new_state, [v for v in verdicts if v["material"]])
        self._record(new_state, verdicts, moved, first_clear=None, floor=floor)
        material = [v for v in verdicts if v["material"]]
        if not material:
            return False, "; ".join(f"{v['name']}: {v['reason']}" for v in verdicts) or "no new scene"
        return True, "; ".join(_describe(v, noun) for v in material)

    def _record(self, new_state: dict, verdicts: list[dict], moved: dict, *, first_clear, floor) -> None:
        """One decision ring entry and one `vision_decision` line per verdict, on every return path
        of material_diff (common/vision_decisions.record)."""
        vision_decisions.record(
            new_state, verdicts, moved=moved, first_clear=first_clear, floor=floor,
            visits=self._visits or {}, vocabulary=self.decision_vocabulary(),
        )  # fmt: skip

    def _triage(self, new_state: dict, candidates: list[dict]) -> None:
        """Ask the agent about each numerically material site; an "artefact" (or any failure)
        makes that site not material. Records the trail in `new_state` (it is stored after the
        diff) and prints each site's `vision_triage` line right after its outcome, so a timeout
        later in the tick still leaves it. Skipped, leaving the numeric verdict, when the topic
        turns triage off or the state didn't come from this instance's fetch (the review path)."""
        config = self._config
        if not candidates or not config or not config["triage"] or self._topic is None:
            return
        try:
            model_id, _fallback = self.choose_model(self._topic)
        except Exception as exc:  # noqa: BLE001 - no model, no agent: fail closed
            model_id, failure = None, f"no model to triage with ({type(exc).__name__})"
        sites = {site["id"]: site for site in config["sites"]}
        trails = new_state.setdefault("triage", {})
        # One deadline for every site's decision: the tick's start plus the topic's allowance.
        started = self._started if self._started is not None else self.clock()
        deadline = started + config["triage_deadline_seconds"]
        for verdict in candidates:
            record = new_state["sites"][verdict["site_id"]]
            latest = record["history"][-1]
            if model_id is None:
                outcome = vision_triage.closed_outcome(failure, "no_model")
            else:
                outcome = self.triage_agent(
                    site={**sites[verdict["site_id"]], "name": record.get("name")},
                    verdict=verdict,
                    history=record["history"],
                    scene={"id": latest["scene_id"], "captured_at": latest.get("captured_at"),
                           "assets": latest.get("assets") or {}},  # fmt: skip
                    model_id=model_id,
                    object_noun=new_state.get("object_noun") or DEFAULTS["object_noun"],
                    params=config["params"],
                    backend=config["backend"],
                    load_image=self.load_image,
                    measure=self.measure,
                    max_tool_calls=config["triage_max_tool_calls"],
                    deadline=deadline,
                    clock=self.clock,
                    read_timeout=vision_sites.WORKER_READ_TIMEOUT,
                )
            trails[verdict["site_id"]] = outcome
            vision_decisions.triage_line(verdict["site_id"], latest.get("scene_id"), outcome)
            latest["triage"] = {"verdict": outcome["verdict"], "reason": outcome["reason"]}
            verdict["triage"] = latest["triage"]
            if outcome["verdict"] != "real":
                verdict["material"] = False
                note = vision_triage.quoted_note(outcome["reason"])
                verdict["reason"] = f"the agent judged it an artefact; {note}"

    def source_refs(self, new_state: dict) -> list[dict]:
        return vision_sites.stac_refs(new_state.get("measured") or [], new_state.get("fetched_at"))

    @classmethod
    def status_vocabulary(cls) -> frozenset[str]:
        return vision_sites.STATUS_VOCABULARY | {MEASURE_LABEL}

    @classmethod
    def decision_vocabulary(cls) -> dict:
        return DECISION_VOCABULARY

    def status(self, topic_config: dict, state: dict | None, *, now: datetime) -> dict | None:
        """Each site's last scene, its verdict by `assess`'s rule (`_judge`), its count under fixed
        words (never `object_noun`, which is the operator's) and its last error, for the operator's
        assistant (Adapter.status has the contract). Pure: reads only `topic_config` and `state`."""
        thresholds = _thresholds(state if isinstance(state, dict) else {})

        def more(site: dict, record: dict, config: dict | None) -> dict:
            history = record.get("history") if isinstance(record.get("history"), list) else []
            history = [entry for entry in history if isinstance(entry, dict)]
            if not history:
                return {}
            latest = history[-1]
            try:
                code = _judge(latest, history[:-1], thresholds)[0]
            except (KeyError, TypeError, ValueError, statistics.StatisticsError):
                code = None  # an entry this rule cannot read is given no verdict
            count = latest.get("count")
            measure = None
            if isinstance(count, int | float) and not isinstance(count, bool):
                measure = {"label": MEASURE_LABEL, "value": count}
            return {"last_verdict": vision_sites.verdict_status(code, latest), "measure": measure}

        return vision_sites.topic_status(
            topic_config, state, parse=parse_config, words=vision_sites.STATUS_WORDS, more=more
        )

    def review_evidence(self, topic_config: dict, latest_state: dict | None) -> str | None:
        """What was measured, from the stored state: re-running the worker for a review would
        cost a cross-region call and measure the same scenes again."""
        if not latest_state:
            return None
        latest = {}
        for site_id, record in (latest_state.get("sites") or {}).items():
            history = record.get("history") or []
            if history:
                last = history[-1]
                latest[site_id] = {
                    "name": record.get("name"),
                    "scene_id": last.get("scene_id"),
                    "captured_at": last.get("captured_at"),
                    "count": last.get("count"),
                    "coverage": last.get("coverage"),
                    "previous_counts": [e.get("count") for e in history[:-1]],
                }
        return render_review_evidence({"object_noun": latest_state.get("object_noun"), "sites": latest})

    def build_summary_prompt(self, topic: dict, diff_summary: str, new_state: dict) -> str | None:
        noun = new_state.get("object_noun") or DEFAULTS["object_noun"]
        name = topic.get("name") or topic.get("topic_id") or "this topic"
        return (
            f'You are summarising a change measured in satellite imagery for the topic "{name}".\n\n'
            f"What was measured: {diff_summary}\n\n"
            f"The counts are of {noun} detected by an automated image-analysis pipeline (OpenCV) in "
            "Sentinel-2 imagery at 10 m resolution, compared with the same site's recent scenes.\n\n"
            "Write 2-4 plain sentences describing only these observations: the site names exactly as "
            "given, the counts, the change against the baseline, and the capture date. Rules: name no "
            "ship, vessel, owner, company or person (none can be identified at this resolution); give "
            "no cause, prediction, price, market or trading view; do not say any area is safe, clear "
            "or unaffected; do not invent place names or numbers that are not given above."
        )


def _thresholds(state: dict) -> dict:
    """The thresholds the diff uses: the topic's, as stored in the state, over the defaults."""
    return {**{key: DEFAULTS[key] for key in THRESHOLD_KEYS}, **(state.get("thresholds") or {})}


def _judge(latest: dict, earlier: list[dict], config: dict) -> tuple[str, dict]:
    """The verdict rule for a site's newest history entry against its earlier ones, by the
    thresholds in `config`: below the coverage floor, a baseline still building, within the usual
    range, or changed (material). Returns that code and the verdict's fields. `assess` builds its
    verdicts from it and `status` reports the newest scene's by it, so the two cannot drift."""
    baseline_entries = clear_entries(earlier, config["coverage_floor"])
    fields = {
        "baseline": None,
        "baseline_scenes": len(baseline_entries),
        "delta": None,
        "relative": None,
        "material": False,
        "reason": "",
        # The measure named in `reason` as moved, as a code (the decision ring's `moved`): written
        # by the same condition, so the two cannot drift.
        "moved": [],
    }
    if latest["coverage"] < config["coverage_floor"]:
        fields["reason"] = "coverage below the floor"
        return "below_floor", fields
    if len(baseline_entries) < config["min_baseline"]:
        fields["reason"] = "baseline still building"
        return "baseline_building", fields
    baseline = statistics.median(e["count"] for e in baseline_entries)
    delta = latest["count"] - baseline
    relative = delta / max(baseline, 1.0)
    fields.update(baseline=baseline, delta=delta, relative=round(relative, 3))
    big_enough = abs(delta) >= config["absolute_threshold"]
    if big_enough and abs(relative) >= config["relative_threshold"]:
        fields.update(material=True, reason="changed against the baseline", moved=["count"])
        return "changed", fields
    fields["reason"] = "within the usual range"
    return "within_range", fields


def _describe(v: dict, noun: str) -> str:
    sign = "+" if v["delta"] >= 0 else ""
    text = (
        f"{v['name']}: {v['count']} {noun} in scene {v['scene_id']} (captured {v['captured_at']}), "
        f"against a baseline of {v['baseline']:g} from {v['baseline_scenes']} earlier clear scenes "
        f"({sign}{v['delta']:g}, {sign}{v['relative']:.0%}); coverage {v['coverage']:.0%}"
    )
    if v.get("triage"):
        text += f"; {vision_triage.quoted_note(v['triage']['reason'])}"
    return text
