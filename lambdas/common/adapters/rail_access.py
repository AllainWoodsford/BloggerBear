"""Rail access: how much of a city's built-up area lies within walking reach of a rail station, scene
by scene, and whether that moved.

docs/enhancements/rail-access-monitor.md is the design (§2 the angle and the cities, §3 the
architecture, §4 PR E this adapter). It is the thin, topic-agnostic part: which cities, which
scenes, the OpenStreetMap network each is read against, the history and the diff. It has no
OpenCV; the measuring (built-up heat from Sentinel-2, station reach, the rail graph) is done by
the vision worker's `rail_access` task through common/vision_client.py, and what it shares with
the satellite_vision adapter (the STAC search, the figure store, the time budget, the site order)
lives in common/vision_sites.py. What a topic watches is configuration, not code:

    adapter_config = {
      "sites": [{"id": "sydney", "name": "Sydney",
                 "polygon": [[lon, lat], ...]}],           # required; 1 to MAX_RAIL_SITES cities, each
                                                           # inside one Sentinel-2 tile
      "backend": "opencv",                                 # or "cool"
      "params": {...},                                     # vision.rail_analyse.RailParams overrides
      "reach_m": 1000,                                     # walking reach of a station, straight line
      "osm_ttl_days": 30,                                  # how long the cached network is used
      "max_cloud_cover": 40, "lookback_days": 20,
      "coverage_floor": 0.6,                               # below it a scene is never material
      "history_size": 6, "min_baseline": 1,
      "served_share_threshold": 0.02,                      # |change in served share| that is material
      "desert_relative_threshold": 0.10,                   # |change in desert area| / baseline median
      "max_sites_per_tick": 1, "time_budget_seconds": 70,  # one city per tick; the budget is capped at 80
      "triage": true, "triage_max_tool_calls": 3, "triage_deadline_seconds": 85,
      "web_context": true,                                 # the agent may ask the news search
    }

**Per tick**, for each site in turn (common/vision_sites.order_sites: those never attempted first,
then the longest unattempted, so a topic with several cities rotates through them one per tick)
and within the tick's budget, the scene first and the network refreshed only when there is
nothing new to measure: (a) the newest Sentinel-2 L2A scene over the whole site (Earth Search
STAC, free, no key); a scene the site already has is nothing new. (b) No usable cached network
(none, or one fetched for another polygon: each carries its polygon's `site_key`): fetch it now
through the Overpass API (common/osm.py) with OSM_RESERVE_SECONDS; a failure is `last_error`
"osm" and the site waits. (c) A new scene: the worker measures it against the cached network,
stale or not (the entry says `network_stale`), when MEASURE_RESERVE_SECONDS remain, else next
tick. (d) Nothing new and the network older than `osm_ttl_days`: fetch it again with the time
this tick would not otherwise use; a failure keeps the cached network, records `network_error`
and is not tried again for NETWORK_RETRY_HOURS. A slow or failing Overpass therefore never stops
a city with a cached network from being measured. A scene the worker refuses for what it is
(REFUSED_SCENE_CODES, recorded as `failed_scene`) is nothing new until a newer scene appears or
NETWORK_RETRY_HOURS pass, so it is not sent again every hour and (d) still gets its ticks. Not
measured is never "nothing there".

**The agent decides.** A numeric change is only a candidate: before it becomes a Finding, the
triage agent (common/vision_triage.py, the rail profile) looks at the figure and the evidence, may
re-measure the scene with other settings, compare the previous scene, or ask the news search
whether a station or line opened or closed (`web_context`), and answers "real" or "artefact".
Only "real" is material; anything unsure is "artefact" (fail closed). Its trail is kept in the
state. It runs inside `material_diff`, after the thresholds, so rule 2 holds: no model call
without a numeric change.

**Material** when the new scene's coverage is at least `coverage_floor`, the site has at least
`min_baseline` earlier clear scenes measured the same way (the same polygon's `site_key` and the
same echoed parameters, so an operator's edit of the reach or the polygon starts a new baseline
rather than reading as a change), and the served share moved by at least
`served_share_threshold` against their median, or the desert area by at least
`desert_relative_threshold` of its median (at least 1 km²), or the station count differs from the
previous such scene's. The first tick with a scene at or above the coverage floor is the first
observation and is reported as such, once: the state records when (`first_observed_at`), so a run
of cloudy scenes that trims every clear one from the history, or a raised floor, never makes a
later scene "first" again. A tick before that, with nothing measured or only scenes below the
floor, is not material.

**What is honest.** The heat is a built-up density proxy from imagery, not population; walking
reach is straight-line distance; the network is OpenStreetMap's (ODbL, credited) and the imagery
only verifies and scores it. The summary prompt says so, and says what was not measured.

**State between ticks.** `keeps_running_state`: the research tick keeps the state after a
no-change tick too, so the baseline includes every scene measured and the network is fetched once
a month, not once a tick. The figure for each scene is stored in the content bucket under
`vision/<topic>/<site>/<scene>.png`; the state holds only its key, and `figures` hands the
newest to the article (common/figures.py). The network (about 3 MB of JSON per city at
common/osm.py's caps with ASCII names, more with long non-ASCII ones; the worker request is sent
as UTF-8, not escaped) is in the state and the worker request, and nowhere a prompt or a reviewer
would see it. Only the newest history entry keeps the per-scene lists (NEWEST_ONLY_KEYS); with
MAX_RAIL_SITES that keeps the state within what the research tick's 512 MB can load.
"""

from __future__ import annotations

import copy
import json
import statistics
import time
from datetime import UTC, datetime, timedelta

from common import osm, rail_notes, vision_client, vision_sites, vision_triage
from common import vision_contract as contract
from common.adapters.base import Adapter, render_review_evidence
from common.figures import MAX_ALT_CHARS, MAX_CAPTION_CHARS
from common.model_routing import resolve_model
from common.vision_sites import (
    AWS_OPEN_DATA_SOURCE,
    COPERNICUS_SOURCE,
    Budget,
    ConfigError,
    clear_entries,
    order_sites,
    search_newest_scene,
    site_key,
)
from common.web_search import search_web

TASK = "rail_access"
# Earth Search v1's asset keys for the bands the rail task reads.
ASSETS_RAIL = {"red": "red", "nir": "nir", "swir16": "swir16", "scl": "scl"}

THRESHOLD_KEYS = ("coverage_floor", "min_baseline", "served_share_threshold", "desert_relative_threshold")
DEFAULTS = {
    "backend": "opencv",
    "params": {},
    "reach_m": 1000,
    "osm_ttl_days": 30,
    "max_cloud_cover": 40,
    "lookback_days": 20,
    "coverage_floor": 0.6,
    "history_size": 6,
    "min_baseline": 1,
    "served_share_threshold": 0.02,
    "desert_relative_threshold": 0.10,
    # One city per tick: the network fetch, the worker and the agent share the tick's 120 s.
    "max_sites_per_tick": 1,
    "time_budget_seconds": 70,
    "triage": True,
    "triage_max_tool_calls": 3,
    # Seconds after the tick started by which the agent must have answered (the tick has 120 s).
    "triage_deadline_seconds": 85,
    "web_context": True,
}
_FLOAT_KEYS = vision_sites.FLOAT_KEYS + (
    "reach_m", "osm_ttl_days", "served_share_threshold", "desert_relative_threshold",
)  # fmt: skip

# The tick's 120 s, shared out (docs §5 risk 1): the network is fetched only with this much of the
# measuring budget left and must finish within it (both Overpass hops included) ...
OSM_RESERVE_SECONDS = 30.0
# ... the worker is asked only with this much left (vision_sites.time_to_measure) ...
MEASURE_RESERVE_SECONDS = vision_sites.MEASURE_RESERVE_SECONDS
# ... and is waited on for at most this long (its own timeout is 60 s; the client's default 90 s
# would run the tick out of time). Both are the rule satellite_vision keeps too, in vision_sites.
WORKER_READ_TIMEOUT = vision_sites.WORKER_READ_TIMEOUT
# After a refresh of a stale network fails, the cached one keeps serving and the refresh waits
# this long: Overpass fails in spells (429s, 504s, timeouts), each try may take OSM_RESERVE_SECONDS
# of a tick, and a struggling mirror asked again every hour only stays struggling. Four tries a day
# is still far inside the 30-day TTL.
NETWORK_RETRY_HOURS = 6
# A scene the worker refuses for what it is (the site is outside it or too big for the worker's
# window, the request does not fit the contract) would be refused again for the same polygon and
# parameters: it is not sent again until a newer scene appears, NETWORK_RETRY_HOURS pass, or the
# operator edits the site's polygon or parameters (the refusal records both). `unreadable_scene` is
# not one: the worker also reports a timed-out or throttled read under it. Any other failure
# (`unreadable_scene`, `invoke`, `internal`, a malformed `reply`) is tried next tick.
REFUSED_SCENE_CODES = ("window_too_large", "site_outside_scene", "bad_request")
# The longest a cached network may serve before it is due again: a year of OpenStreetMap edits.
MAX_OSM_TTL_DAYS = 365.0
# One topic per city is the design (docs §2), so a topic watches at most this many. Each city's
# network is in the running state, which the research tick (512 MB) loads whole, deep-copies site
# by site and writes back. Measured (ru_maxrss) for 3 cities at common/osm.py's caps (2.6-3 MB of
# network JSON each), history_size 20, 2000 station rows per scene and a tick measuring a new one:
# with every entry keeping its lists the state was 54 MB and the tick peaked at 390 MB; with only
# the newest keeping them (NEWEST_ONLY_KEYS) 10 MB and 146 MB, 38 MB of that the imports. A review
# with every name at its 80-character cap measured 11.3 MB and 155 MB (22.6 MB and 181 MB with 80
# CJK characters each). The one tick that slims a state written before peaks at 293 MB.
MAX_RAIL_SITES = 3
# What a budget-starved site returns from _visit: it waits for the next tick, unstamped.
DEFERRED = "deferred"

# A history entry keeps each station's row cut to these fields (the worker's rows also carry
# `kind`, `snapped` and `catchment_coverage`), at most this many: about 2000 rows of ten fields
# (some 600 KB of JSON) is what the running state can carry for the newest scene (docs §5 risk 2).
STATION_FIELDS = (
    "id", "name", "lon", "lat", "degree", "betweenness", "activity", "d_hub_m", "isolation_weight",
    "intermodal",
)  # fmt: skip
MAX_HISTORY_STATIONS = 2000
# Only the newest history entry keeps these lists: the figure, the review evidence and the agent's
# brief (and planner notes built on them) read them from it alone. An older entry keeps the scalars
# and counts the material rules, `site_history` and `previous_scene` read (`flag_count` among them),
# so twenty scenes of a big city cost little more than one.
NEWEST_ONLY_KEYS = ("stations", "flags", "suggestions", "deserts")


def parse_config(adapter_config: dict | None) -> dict:
    """adapter_config with defaults filled in, or ConfigError: the checks every vision topic
    shares (common/vision_sites.py) with at most MAX_RAIL_SITES sites, then `reach_m` within the
    worker contract's bounds, a network TTL in (0, MAX_OSM_TTL_DAYS] and `params` as an object (the
    contract checks each parameter before any call)."""
    config = vision_sites.parse_site_config(
        adapter_config, DEFAULTS, vision_sites.WHOLE_KEYS, _FLOAT_KEYS, max_sites=MAX_RAIL_SITES
    )
    low, high = contract.RAIL_PARAM_LIMITS["reach_m"]
    if not low <= config["reach_m"] <= high:
        raise ConfigError(f"reach_m must be between {low:g} and {high:g} metres")
    if not 0 < config["osm_ttl_days"] <= MAX_OSM_TTL_DAYS:
        raise ConfigError(f"osm_ttl_days must be more than 0 and at most {MAX_OSM_TTL_DAYS:g}")
    if not isinstance(config["params"], dict):
        raise ConfigError("params must be an object")
    return config


def worker_params(config: dict) -> dict:
    """What the worker is asked to measure with: the topic's reach, under any explicit override."""
    return {"reach_m": config["reach_m"], **config["params"]}


def worker_network(network: dict) -> dict:
    """The part of a cached network the worker contract takes (the fetch's `source`, `endpoint`,
    `query_url` and `counts` stay in the state)."""
    return {key: network.get(key) for key in ("ways", "stations", "pois", "fetched_at")}


class RailAccessAdapter(Adapter):
    uses_previous_state = True
    keeps_running_state = True
    sources = (COPERNICUS_SOURCE, AWS_OPEN_DATA_SOURCE, osm.OSM_SOURCE)

    # Injected by tests; the defaults talk to Earth Search, Overpass, the worker, S3, the news
    # search and Bedrock.
    http_post = None
    measure = staticmethod(vision_client.measure)
    fetch_network = staticmethod(osm.fetch_network)
    store_image = staticmethod(vision_sites.store_image)
    load_image = staticmethod(vision_sites.load_image)
    triage_agent = staticmethod(vision_triage.triage)
    choose_model = staticmethod(resolve_model)
    web_search = staticmethod(search_web)
    clock = staticmethod(time.monotonic)

    # Set by fetch_state for material_diff on the same instance.
    _topic: dict | None = None
    _config: dict | None = None
    _started: float | None = None
    # Set by material_diff for planner_notes: the sites its summary reported (None before it runs).
    _reported: list[str] | None = None

    def fetch_state(self, topic_config: dict, previous_state: dict | None = None) -> dict:
        config = parse_config(topic_config.get("adapter_config"))
        # Kept for material_diff on this same instance (the research tick makes one per tick): the
        # triage agent needs the topic's model and the sites' polygons, and the diff is only given
        # the two states.
        self._topic, self._config = topic_config, config
        topic_id = topic_config.get("topic_id", "topic")
        now = datetime.now(UTC)
        budget = Budget(self.clock, config["time_budget_seconds"])
        self._started = budget.started
        previous_sites = (previous_state or {}).get("sites") or {}
        sites, measured = {}, []
        for site in config["sites"]:
            record = copy.deepcopy(previous_sites.get(site["id"])) or {"history": []}
            # A state written before NEWEST_ONLY_KEYS is slimmed on its first tick.
            record["history"] = _slim_history(record.get("history") or [])
            record["name"] = site.get("name") or site["id"]
            sites[site["id"]] = record

        for site in order_sites(config["sites"], previous_sites):
            record = sites[site["id"]]
            if len(measured) >= config["max_sites_per_tick"]:
                continue
            if budget.exhausted():
                record["last_error"] = _error(now, "time_budget", "next tick")
                continue
            outcome = self._visit(site, record, config, now, budget, topic_id)
            if outcome == DEFERRED:
                # Not an attempt: the site keeps its place and goes ahead of those worked on.
                record["last_error"] = _error(now, "time_budget", "next tick")
                continue
            record["last_attempt_at"] = now.isoformat()
            if outcome is not None:
                measured.append(outcome)

        return {
            "fetched_at": now.isoformat(),
            "task": TASK,
            # The diff gets only the two states, so the topic's thresholds travel in the state.
            "thresholds": {key: config[key] for key in THRESHOLD_KEYS},
            # Set on the tick of the first observation and carried forward, so it is reported once.
            vision_sites.FIRST_OBSERVED_KEY: vision_sites.first_observed_at(
                previous_state, sites, measured, config["coverage_floor"], now.isoformat()
            ),
            "sites": sites,
            "measured": measured,
        }

    def _visit(self, site: dict, record: dict, config: dict, now: datetime, budget: Budget, topic_id: str):
        """One site's turn, scene first (the module docstring's (a) to (d)). Returns
        {"site_id", "scene_id"} when a scene was measured, DEFERRED when the budget left too little
        time for what was due, and None for any other outcome (nothing new, or an error recorded
        in `record`)."""
        try:
            scene = search_newest_scene(site, config, now, http_post=self.http_post, assets=ASSETS_RAIL)
        except Exception as exc:  # noqa: BLE001 - one site's search must not stop the others
            record["last_error"] = _error(now, "search", str(exc))
            return None
        known = {entry.get("scene_id") for entry in record["history"]}
        new_scene = scene is not None and scene["id"] not in known
        key = site_key(site["polygon"])
        # A scene the worker refused for what it is is nothing new for a while (REFUSED_SCENE_CODES),
        # so (d) may use the tick; the refusal stays the site's `last_error` meanwhile. An edited
        # polygon or parameters is a new request, so it is sent at once.
        refused = new_scene and _refused_before(record, scene["id"], now, key, worker_params(config))
        new_scene = new_scene and not refused
        network = record.get("network")
        if not (isinstance(network, dict) and network.get("site_key") == key):
            # (b) No usable network: none yet, or the old polygon's (it is never measured against).
            record.pop("network", None)
            if budget.remaining() < OSM_RESERVE_SECONDS:
                return DEFERRED
            network = self._fetch_network(site, record, now, key)
            if network is None:
                error = record.get("network_error") or {}
                record["last_error"] = _error(now, "osm", error.get("detail") or "no network fetched yet")
                return None
            stale = False
        elif new_scene:
            # (c) Measure with what is cached, however old: a slow Overpass must not stop it.
            stale = _is_stale(network, now, config["osm_ttl_days"])
        else:
            # (d) Nothing new to measure: the time goes to a stale network, unless it just failed.
            if (
                _is_stale(network, now, config["osm_ttl_days"])
                and not _retry_waits(record, now)
                and budget.remaining() >= OSM_RESERVE_SECONDS
            ):
                self._fetch_network(site, record, now, key)
            if not refused:
                record.pop("last_error", None)
            return None
        if not new_scene:
            if not refused:
                record.pop("last_error", None)
            return None
        if not vision_sites.time_to_measure(budget):
            return DEFERRED
        try:
            result = self.measure(
                {"id": site["id"], "polygon": site["polygon"]},
                {"id": scene["id"], "captured_at": scene["captured_at"], "assets": scene["assets"]},
                backend=config["backend"],
                params=worker_params(config),
                coverage_floor=config["coverage_floor"],
                task=TASK,
                network=worker_network(network),
                read_timeout=WORKER_READ_TIMEOUT,
            )
            entry = _history_entry(scene, result, network, key, stale)
        except vision_client.VisionError as exc:
            record["last_error"] = _error(now, exc.code, exc.detail)
            record["failed_scene"] = {
                "id": scene["id"],
                "code": exc.code,
                "at": now.isoformat(),
                "site_key": key,
                "params": worker_params(config),
            }
            return None
        if result.image_png:
            try:
                entry["image_key"] = self.store_image(topic_id, site["id"], scene["id"], result.image_png)
            except Exception as exc:  # noqa: BLE001 - a lost figure doesn't lose the measurement
                print(f"rail_access: could not store the figure for {site['id']}: {exc!r}")
        record["history"] = _slim_history([*record["history"], entry][-config["history_size"] :])
        record.pop("last_error", None)
        record.pop("failed_scene", None)
        return {"site_id": site["id"], "scene_id": scene["id"]}

    def _fetch_network(self, site: dict, record: dict, now: datetime, key: str) -> dict | None:
        """The site's network from Overpass within OSM_RESERVE_SECONDS (both hops included), kept
        in `record` with the polygon's `site_key`; None on failure, with `network_error` recorded and
        whatever was cached left as it was."""
        try:
            network = self.fetch_network(
                site["polygon"],
                http_post=self.http_post,
                now=now,
                deadline=self.clock() + OSM_RESERVE_SECONDS,
                clock=self.clock,
            )
        except osm.OsmError as exc:
            record["network_error"] = _error(now, exc.code, exc.detail)
            return None
        except Exception as exc:  # noqa: BLE001 - a fetch that breaks keeps the cached network too
            record["network_error"] = _error(now, "fetch", f"{type(exc).__name__}: {exc}")
            return None
        network["site_key"] = key
        record["network"] = network
        record.pop("network_error", None)
        return network

    def assess(self, new_state: dict) -> list[dict]:
        """One verdict per site measured this tick: its served share, desert area and station
        count against its baseline, and whether that is material, by the thresholds stored in the
        state."""
        config = _thresholds(new_state)
        verdicts = []
        for m in new_state.get("measured") or []:
            record = (new_state.get("sites") or {}).get(m["site_id"]) or {}
            history = record.get("history") or []
            if not history or history[-1].get("scene_id") != m["scene_id"]:
                continue
            latest, earlier = history[-1], history[:-1]
            # Only scenes measured the same way are compared: the same polygon and parameters.
            comparable = [e for e in earlier if _comparable(e, latest)]
            baseline_entries = clear_entries(comparable, config["coverage_floor"], value_key="served_share")
            verdict = {
                "site_id": m["site_id"],
                "name": record.get("name") or m["site_id"],
                "scene_id": latest["scene_id"],
                "captured_at": latest.get("captured_at"),
                "coverage": latest["coverage"],
                "served_share": latest["served_share"],
                "desert_km2": _km2(latest),
                "station_count": latest.get("station_count"),
                "reach_m": _reach_m(latest),
                # The scene before was measured another way (an edited reach, polygon or parameter).
                "params_changed": bool(earlier) and not _comparable(earlier[-1], latest),
                "baseline": None,
                "baseline_scenes": len(baseline_entries),
                "delta": None,
                "relative": None,
                "material": False,
                "reason": "",
            }
            if latest["coverage"] < config["coverage_floor"]:
                verdict["reason"] = "coverage below the floor"
            elif len(baseline_entries) < config["min_baseline"]:
                verdict["reason"] = "baseline still building"
            else:
                share_baseline = statistics.median(e["served_share"] for e in baseline_entries)
                desert_baseline = statistics.median(_km2(e) for e in baseline_entries)
                previous_stations = comparable[-1].get("station_count")
                share_delta = latest["served_share"] - share_baseline
                desert_delta = _km2(latest) - desert_baseline
                desert_scale = max(desert_baseline, 1.0)
                stations_delta = None
                if previous_stations is not None and latest.get("station_count") is not None:
                    stations_delta = latest["station_count"] - previous_stations
                verdict.update(
                    baseline={
                        "served_share": round(share_baseline, 4),
                        "desert_km2": round(desert_baseline, 4),
                        "station_count": previous_stations,
                    },
                    delta={
                        "served_share": round(share_delta, 4),
                        "desert_km2": round(desert_delta, 4),
                        "station_count": stations_delta,
                    },
                    relative={"desert_km2": round(desert_delta / desert_scale, 3)},
                )
                # Compared as stored (4 decimal places), so a move of exactly the threshold is one.
                reasons = []
                if round(abs(share_delta), 4) >= config["served_share_threshold"]:
                    reasons.append("served share moved")
                desert_threshold = round(config["desert_relative_threshold"] * desert_scale, 4)
                if round(abs(desert_delta), 4) >= desert_threshold:
                    reasons.append("desert area moved")
                if stations_delta:
                    reasons.append("station count changed")
                if reasons:
                    verdict.update(material=True, reason=", ".join(reasons))
                else:
                    verdict["reason"] = "within the usual range"
            verdicts.append(verdict)
        return verdicts

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        verdicts = self.assess(new_state)
        self._reported = []
        first = vision_sites.first_observation(
            old_state, new_state, verdicts, _thresholds(new_state)["coverage_floor"]
        )
        if first is not None:
            clear, not_yet = first
            if not clear:
                return False, not_yet
            summary = "First observation. " + "; ".join(_first_line(v) for v in clear)
            return True, self._with_planner_notes(summary, clear, new_state)
        self._triage(new_state, [v for v in verdicts if v["material"]])
        material = [v for v in verdicts if v["material"]]
        if not material:
            return False, "; ".join(f"{v['name']}: {v['reason']}" for v in verdicts) or "no new scene"
        return True, self._with_planner_notes("; ".join(_describe(v) for v in material), material, new_state)

    def _triage(self, new_state: dict, candidates: list[dict]) -> None:
        """Ask the agent about each numerically material site; an "artefact" (or any failure)
        makes that site not material. Records the trail in `new_state` (it is stored after the
        diff). Skipped, leaving the numeric verdict, when the topic turns triage off or the state
        didn't come from this instance's fetch (the review path)."""
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
            network = record.get("network")
            if model_id is None:
                outcome = {"verdict": "artefact", "reason": failure, "tool_calls": []}
            else:
                outcome = self.triage_agent(
                    site={**sites[verdict["site_id"]], "name": record.get("name")},
                    verdict=verdict,
                    history=record["history"],
                    scene={"id": latest["scene_id"], "captured_at": latest.get("captured_at"),
                           "assets": latest.get("assets") or {}},  # fmt: skip
                    model_id=model_id,
                    object_noun=None,
                    params=worker_params(config),
                    backend=config["backend"],
                    load_image=self.load_image,
                    measure=self.measure,
                    max_tool_calls=config["triage_max_tool_calls"],
                    task=TASK,
                    network=worker_network(network) if isinstance(network, dict) else None,
                    web_search=self.web_search if config["web_context"] else None,
                    deadline=deadline,
                    clock=self.clock,
                    read_timeout=WORKER_READ_TIMEOUT,
                )
            trails[verdict["site_id"]] = outcome
            latest["triage"] = {"verdict": outcome["verdict"], "reason": outcome["reason"]}
            verdict["triage"] = latest["triage"]
            if outcome["verdict"] != "real":
                verdict["material"] = False
                note = vision_triage.quoted_note(outcome["reason"])
                verdict["reason"] = f"the agent judged it an artefact; {note}"

    def _with_planner_notes(self, summary: str, verdicts: list[dict], new_state: dict) -> str:
        """`summary` with the planner notes of the sites it reports, which are the sites
        planner_notes gives the Finding the notes of."""
        self._reported = [v["site_id"] for v in verdicts]
        return _with_planner_notes(summary, verdicts, new_state)

    def _reported_scenes(self, new_state: dict):
        """(site id, newest history entry) for each scene measured this tick that the diff summary
        reported (`_reported`; before material_diff has run on this instance, every one), leaving
        out a scene the agent judged an artefact (its measurements are what it doubted).
        planner_notes and figures both take this rule, so the Finding never carries the notes or the
        map of a site its summary did not report (one below the coverage floor, one doubted)."""
        for m in new_state.get("measured") or []:
            if self._reported is not None and m["site_id"] not in self._reported:
                continue
            latest = _latest_entry(new_state, m["site_id"])
            doubted = (latest.get("triage") or {}).get("verdict", "real") != "real"
            if latest.get("scene_id") != m["scene_id"] or doubted:
                continue
            yield m["site_id"], latest

    def figures(self, new_state: dict) -> list[dict]:
        """One figure per scene the diff summary reported that has one (`_reported_scenes`): the
        worker's heat map, with a caption that says what the colours, outlines and lines are and
        carries the two credits, and alt text for a screen reader."""
        figures = []
        for site_id, latest in self._reported_scenes(new_state):
            if not latest.get("image_key"):
                continue
            record = (new_state.get("sites") or {}).get(site_id) or {}
            name = record.get("name") or site_id
            captured_at = latest.get("captured_at") or ""
            date = captured_at[:10] or "(date unknown)"
            year = captured_at[:4] or (new_state.get("fetched_at") or "")[:4]
            reach = _reach_text(_reach_m(latest))

            # The figure's symbols, as PR H draws them: heat, outlined deserts, the network,
            # flagged stations in red rings and simulated links dashed.
            def caption(site_name: str) -> str:
                return (
                    f"Rail access in {site_name}, Sentinel-2 {date}: built-up density (warm colours), "
                    f"areas beyond {reach} of a station (outlined), OpenStreetMap rail lines and stations, "
                    "flagged stations (red rings) and simulated links (dashed). Processed imagery; contains "
                    f"modified Copernicus Sentinel data {year}; map data © OpenStreetMap contributors."
                )

            def alt(site_name: str) -> str:
                return (
                    f"Processed satellite map of {site_name} showing rail lines, stations, flagged stations, "
                    "simulated links and built-up areas beyond walking reach of a station"
                )

            figures.append(
                {
                    "key": latest["image_key"],
                    "caption": _within(MAX_CAPTION_CHARS, caption, name),
                    "alt": _within(MAX_ALT_CHARS, alt, name),
                }
            )
        return figures

    def source_refs(self, new_state: dict) -> list[dict]:
        """The STAC item of each scene measured, and the Overpass query the site's network came
        from (its bbox form, which a reader can open)."""
        measured = new_state.get("measured") or []
        refs = vision_sites.stac_refs(measured, new_state.get("fetched_at"))
        polygons = {site["id"]: site["polygon"] for site in (self._config or {}).get("sites") or []}
        for m in measured:
            record = (new_state.get("sites") or {}).get(m["site_id"]) or {}
            network = record.get("network")
            if not isinstance(network, dict):
                continue
            url = network.get("query_url")
            if not url and m["site_id"] in polygons:
                url = osm.query_url(polygons[m["site_id"]])
            if not url:
                continue
            name = record.get("name") or m["site_id"]
            refs.append(
                {
                    "url": url,
                    "title": f"OpenStreetMap rail data for {name} via the Overpass API",
                    "accessed_at": network.get("fetched_at"),
                }
            )
        return refs

    def review_evidence(self, topic_config: dict, latest_state: dict | None) -> str | None:
        """What was measured, from the stored state: re-running the worker for a review would
        cost a cross-region call and measure the same scenes again. Never the network: a city's
        rail data is far too large for a prompt, and no article claim rests on a way's geometry."""
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
                    "served_share": last.get("served_share"),
                    "desert_km2": last.get("desert_km2"),
                    "station_count": last.get("station_count"),
                    "reach_m": _reach_m(last),
                    "coverage": last.get("coverage"),
                    "flag_count": last.get("flag_count", len(last.get("flags") or [])),
                    "previous_served_shares": [e.get("served_share") for e in history[:-1]],
                }
        return render_review_evidence({"task": TASK, "sites": latest, **_evidence_notes(latest_state)})

    def build_summary_prompt(self, topic: dict, diff_summary: str, new_state: dict) -> str | None:
        name = topic.get("name") or topic.get("topic_id") or "this topic"
        prompt = (
            "You are summarising a change measured in satellite imagery and OpenStreetMap data for "
            f'the topic "{name}".\n\n'
            f"What was measured: {diff_summary}\n\n"
            "The figures are proxies: built-up density from Sentinel-2 reflectance at 20 m, walking "
            "reach as straight-line distance to the nearest station, and network measures from "
            "OpenStreetMap's rail lines; none is ridership, patronage or journey time. Write 2-4 "
            "plain sentences describing only these observations: station and line names exactly as "
            "given, the served share, the built-up area beyond reach, the capture date, and what was "
            "not measured (ridership, service frequency, planned works). Give no cause, forecast, "
            "political, planning or investment view; do not say any area is well or badly served in "
            "absolute terms; do not invent place names or numbers that are not given above."
        )
        return _with_notes_rule(prompt, diff_summary)

    def planner_notes(self, new_state: dict) -> list[dict]:
        """The rule-based flags and simulations (common/rail_notes.py) of each site the diff summary
        reported, each with its site's id, for the Finding: at most rail_notes.MAX_NOTES in all, so
        a site measured this tick that the summary left out (not material, below the coverage
        floor) never takes the reported site's places. Before material_diff has run on this
        instance, those of every scene measured this tick. A scene the agent judged an artefact is
        left out (its measurements are what it doubted). The rule is `_reported_scenes`, which
        figures takes too."""
        notes = []
        for site_id, latest in self._reported_scenes(new_state):
            notes += [{"site_id": site_id, **note} for note in rail_notes.planner_notes(latest)]
        return notes[: rail_notes.MAX_NOTES]


def _error(now: datetime, code: str, detail: str) -> dict:
    return {"at": now.isoformat(), "code": code, "detail": (detail or "")[:300]}


def _thresholds(state: dict) -> dict:
    """The thresholds the diff uses: the topic's, as stored in the state, over the defaults."""
    return {**{key: DEFAULTS[key] for key in THRESHOLD_KEYS}, **(state.get("thresholds") or {})}


def _is_stale(network: dict, now: datetime, ttl_days) -> bool:
    """osm.is_stale, with a TTL it cannot use (parse_config refuses one; an older state might
    still reach here) read as stale rather than raised out of the tick."""
    try:
        return osm.is_stale(network, now, ttl_days)
    except (ArithmeticError, ValueError, TypeError):
        return True


def _retry_waits(record: dict, now: datetime) -> bool:
    """Whether the last network fetch failed less than NETWORK_RETRY_HOURS ago."""
    return _recent(record.get("network_error"), now)


def _refused_before(record: dict, scene_id: str, now: datetime, key: str, params: dict) -> bool:
    """Whether the worker refused this very scene, for this polygon (`key`) and these `params`, for
    what it is (REFUSED_SCENE_CODES) less than NETWORK_RETRY_HOURS ago: then it is not sent again
    yet. A refusal recorded before the polygon and params were kept with it is matched on the
    scene alone."""
    failed = record.get("failed_scene")
    if not isinstance(failed, dict) or failed.get("id") != scene_id:
        return False
    if failed.get("site_key", key) != key or failed.get("params", params) != params:
        return False
    return failed.get("code") in REFUSED_SCENE_CODES and _recent(failed, now)


def _recent(stamp, now: datetime) -> bool:
    """Whether `stamp` ({"at": iso, ...}) is less than NETWORK_RETRY_HOURS old."""
    if not isinstance(stamp, dict) or not isinstance(stamp.get("at"), str):
        return False
    try:
        at = datetime.fromisoformat(stamp["at"])
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return now - at < timedelta(hours=NETWORK_RETRY_HOURS)


def _slim_history(history: list) -> list:
    """`history` with every entry but the newest without NEWEST_ONLY_KEYS (its flags counted in
    `flag_count` if it had none)."""
    return [*(_slim(entry) for entry in history[:-1]), *history[-1:]]


def _slim(entry):
    if not isinstance(entry, dict):
        return entry
    slim = {key: value for key, value in entry.items() if key not in NEWEST_ONLY_KEYS}
    if "flag_count" not in slim and isinstance(entry.get("flags"), list):
        slim["flag_count"] = len(entry["flags"])
    return slim


def _param_values(entry: dict) -> tuple:
    """The parameters an entry was measured with, as the worker echoed them: every RailParams
    field (the worker contract's RAIL_PARAM_LIMITS keys), the reach among them."""
    params = entry.get("params") if isinstance(entry.get("params"), dict) else {}
    return tuple(params.get(key) for key in contract.RAIL_PARAM_LIMITS)


def _comparable(entry: dict, latest: dict) -> bool:
    """Whether `entry` was measured the way `latest` was: the same polygon (`site_key`) and the
    same parameters. Others are not a baseline for it: an edited reach or polygon would read as a
    change in the city."""
    return entry.get("site_key") == latest.get("site_key") and _param_values(entry) == _param_values(latest)


def _history_entry(
    scene: dict, result: vision_client.VisionResult, network: dict, polygon_key: str, network_stale: bool
) -> dict:
    """One scene's measurement as the history keeps it while it is the newest: the metrics an
    article or the agent may use, the station rows cut to STATION_FIELDS and capped (these and the
    other NEWEST_ONLY_KEYS lists go when a newer scene arrives), the build and timings for
    reproducibility, the scene's assets for `look_again`, the polygon's `site_key` and whether the
    network was past its TTL. VisionError "reply" if anything in it would not survive JSON (a NaN
    or an infinity the worker let through): a value that cannot be stored is a measurement that was
    not made."""
    metrics = result.metrics
    stations = [
        {key: row.get(key) for key in STATION_FIELDS}
        for row in (metrics.get("stations") or [])[:MAX_HISTORY_STATIONS]
        if isinstance(row, dict)
    ]
    flags = metrics.get("flags") or []
    entry = {
        "scene_id": scene["id"],
        "captured_at": scene["captured_at"],
        "scene_cloud_cover": scene.get("cloud_cover"),
        "coverage": metrics["coverage"],
        "built_up_km2": metrics.get("built_up_km2"),
        "served_km2": metrics.get("served_km2"),
        "desert_km2": metrics.get("desert_km2"),
        "served_share": metrics["served_share"],
        "station_count": metrics["station_count"],
        "edge_count": metrics.get("edge_count"),
        "hubs": metrics.get("hubs") or [],
        # The lists (NEWEST_ONLY_KEYS) are dropped when a newer scene arrives; the count stays.
        "flags": flags,
        "flag_count": len(flags) if isinstance(flags, list) else 0,
        "suggestions": metrics.get("suggestions") or [],
        "deserts": metrics.get("deserts") or [],
        "stations": stations,
        "quality_flags": metrics.get("quality_flags", []),
        "backend": result.reply["backend"],
        "build_sha256": (result.reply.get("build") or {}).get("build_sha256"),
        "timings_ms": result.reply.get("timings_ms"),
        "image_key": None,
        # For the agent's look_again: the same bands, re-measured with other settings.
        "assets": scene["assets"],
        "network_fetched_at": network.get("fetched_at"),
        # Measured with a cached network past osm_ttl_days (a refresh had not yet succeeded).
        "network_stale": bool(network_stale),
        # Which polygon this scene was measured over: only scenes of the same one are compared.
        "site_key": polygon_key,
        # Echoed by the worker: the reach and the rest, so the figure's caption and a re-run say
        # what this scene was measured with, and the baseline compares only like with like.
        "params": metrics.get("params"),
    }
    try:
        json.dumps(entry, allow_nan=False)
    except ValueError as exc:
        raise vision_client.VisionError("reply", f"metrics are not JSON-clean: {exc}") from exc
    return entry


def _km2(entry: dict) -> float:
    return float(entry.get("desert_km2") or 0.0)


def _reach_m(entry: dict) -> float:
    """The reach a history entry was measured with, from the worker's echoed params."""
    params = entry.get("params") if isinstance(entry.get("params"), dict) else {}
    try:
        return float(params.get("reach_m", DEFAULTS["reach_m"]))
    except (TypeError, ValueError):
        return float(DEFAULTS["reach_m"])


def _reach_text(reach_m: float) -> str:
    """"1 km" for a whole number of kilometres, else metres: how the summaries name the reach."""
    if reach_m >= 1000 and reach_m % 1000 == 0:
        return f"{reach_m / 1000:g} km"
    return f"{reach_m:g} m"


def _pct(share: float) -> str:
    return f"{share * 100:.0f} %"


def _area(km2: float) -> str:
    """A desert area as the summaries print it: one decimal at every size, since whole numbers
    could read a material move as no move ("2 km² against 2 km² (+15%)"). One decimal shows any move
    of more than 0.1 km², and the default threshold (desert_relative_threshold 0.10 of a median of at
    least 1 km²) is never under 0.1 km². A topic that sets a lower desert_relative_threshold can
    still see the same area printed twice for a small median; the percentage beside it shows the move."""
    return f"{km2:.1f} km²"


def _scenes(count: int) -> str:
    return f"{count} earlier clear scene{'' if count == 1 else 's'}"


def _within(limit: int, render, name: str) -> str:
    """`render(name)`, with the name cut (and marked) so the text fits `limit`: the wording and
    the two credits are fixed, and a long site name must not cost the article its figure
    (common/figures.py drops a caption or alt text over its limit)."""
    text = render(name)
    excess = len(text) - limit
    if excess <= 0:
        return text
    cut = max(len(name) - excess - 1, 1)
    return render(name[:cut].rstrip() + "…")


def _first_line(v: dict) -> str:
    return (
        f"{v['name']}: served share {_pct(v['served_share'])}, {v['station_count']} stations, "
        f"{_area(v['desert_km2'])} beyond {_reach_text(v['reach_m'])} of a station "
        f"(coverage {_pct(v['coverage'])})"
    )


def _describe(v: dict) -> str:
    baseline, delta, relative = v["baseline"], v["delta"], v["relative"]
    share_points = delta["served_share"] * 100
    sign = "+" if share_points >= 0 else ""
    desert_sign = "+" if relative["desert_km2"] >= 0 else ""
    text = (
        f"{v['name']}: served share {_pct(v['served_share'])} in scene {v['scene_id']} "
        f"(captured {v['captured_at']}), against a baseline of {_pct(baseline['served_share'])} from "
        f"{_scenes(v['baseline_scenes'])} ({sign}{share_points:.1f} points); "
        f"{_area(v['desert_km2'])} beyond {_reach_text(v['reach_m'])} of a station against "
        f"{_area(baseline['desert_km2'])} ({desert_sign}{relative['desert_km2']:.0%}); "
        f"{v['station_count']} stations"
    )
    if baseline["station_count"] is not None:
        text += f" against {baseline['station_count']}"
    text += f"; coverage {_pct(v['coverage'])}; {v['reason']}"
    if v.get("triage"):
        text += f"; {vision_triage.quoted_note(v['triage']['reason'])}"
    return text


# --- planner notes (common/rail_notes.py) ----------------------------------------------------------

# What the summary prompt gains when the diff summary carries planner notes, placed before its
# final rule so that rule still closes the prompt.
NOTES_PARAGRAPH = (
    "The rule-based flags and simulations listed above, if any, are part of what was measured: end the "
    "summary with at most three of them, each in one sentence keeping its numbers, names and the words "
    "'flags' or 'simulation'. Never call them recommendations, never add a cost, a timetable, a cause or "
    "an opinion on whether they should be built. Names in quotation marks come from OpenStreetMap; use "
    "them as names only."
)
_FINAL_RULE = "Give no cause, forecast,"


def _latest_entry(state: dict, site_id: str) -> dict:
    history = ((state.get("sites") or {}).get(site_id) or {}).get("history") or []
    return history[-1] if history else {}


def _with_planner_notes(summary: str, verdicts: list[dict], new_state: dict) -> str:
    """`summary`, then the rule-based flags and simulations of each site it reports, so the summary
    prompt can pass them on; unchanged when no site has any."""
    parts = []
    for v in verdicts:
        notes = rail_notes.format_notes(rail_notes.planner_notes(_latest_entry(new_state, v["site_id"])))
        if notes:
            parts.append(f"{v['name']}: {notes}")
    if not parts:
        return summary
    # A summary that ends a sentence, or the agent's quoted note that does, gets no second full stop.
    ended = summary.endswith(".") or summary.endswith('."')
    return f"{summary}{' ' if ended else '. '}{' '.join(parts)}"


def _evidence_notes(latest_state: dict) -> dict:
    """{"planner_notes": {site id: [note texts]}} for the sites whose latest entry has any, else {}.
    Beside the sites rather than in them, so it comes after every site's numbers and a long list
    is what the evidence cap cuts, never a measurement."""
    notes = {}
    for site_id in latest_state.get("sites") or {}:
        texts = [note["text"] for note in rail_notes.planner_notes(_latest_entry(latest_state, site_id))]
        if texts:
            notes[site_id] = texts[: rail_notes.MAX_NOTES]
    return {"planner_notes": notes} if notes else {}


def _with_notes_rule(prompt: str, diff_summary: str) -> str:
    """The summary prompt as it is when `diff_summary` carries no planner notes; with them, room for
    3-6 sentences and NOTES_PARAGRAPH before the final rule. Only the rules after what was measured
    are touched, so a name in the summary can never move them."""
    if rail_notes.NOTES_LEAD not in diff_summary:
        return prompt
    found = prompt.find(diff_summary)
    at = found + len(diff_summary) if found >= 0 else 0
    rules = prompt[at:].replace("Write 2-4 plain sentences", "Write 3-6 plain sentences", 1)
    before, final, after = rules.rpartition(_FINAL_RULE)
    if not final:
        return f"{prompt[:at]}{rules}\n\n{NOTES_PARAGRAPH}"
    return f"{prompt[:at]}{before.rstrip()}\n\n{NOTES_PARAGRAPH}\n\n{final}{after}"
