"""Satellite vision: count what OpenCV sees at fixed sites, scene by scene, and report real change.

docs/enhancements/opencv-agentic-vision-enhancement.md is the design. This adapter is the thin,
topic-agnostic part of it: which sites, which scenes, the history and the diff. It has no OpenCV;
the measuring is done by the vision worker (vision_worker_handler.py), which may run in another
region, through common/vision_client.py. What a topic watches is configuration, not code:

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
      "max_sites_per_tick": 5, "time_budget_seconds": 60,
    }

**Per tick**, for each site: find the newest Sentinel-2 L2A scene over it (Earth Search STAC,
free, no key); if it is one the site already has, do nothing; otherwise ask the worker to measure
it and append the result to the site's history. A site that can't be searched or measured keeps
its history and records `last_error`: not measured is never "nothing there".

**Material** when a site's new count differs from the median of its previous clear scenes by at
least `absolute_threshold` *and* `relative_threshold`, the new scene's coverage is at least
`coverage_floor`, and there are at least `min_baseline` earlier clear scenes. The very first tick
is always material (the adapter contract). Rule 2 holds: every Bedrock call comes after this.

**State between ticks.** The baseline must include every scene measured, not only those that were
reported, so this adapter sets `keeps_running_state`: the research tick keeps its state after a
no-change tick too (research_tick_handler.py). The figure for each scene is stored in the content
bucket under `vision/<topic>/<site>/<scene>.png`; the snapshot holds only its key.

**Data licences** (docs/risks/opencv-bushfire-watch-01.md item 1, checked 2026-10-03):
Sentinel-2 is free, full and open under the Copernicus Sentinel data legal notice, which asks
derived products to carry "Contains modified Copernicus Sentinel data [year]"
(https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice). The AWS copy
asks to be cited as accessed from https://registry.opendata.aws/sentinel-2-l2a-cogs. The year
varies by scene, so the class-level credit below names the data and its source; each article's
figure caption carries the capture year.
"""

from __future__ import annotations

import copy
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import requests

from common import vision_client
from common.adapters.base import Adapter, render_review_evidence

STAC_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
STAC_ITEM_URL = "https://earth-search.aws.element84.com/v1/collections/{collection}/items/{item_id}"
STAC_COLLECTION = "sentinel-2-l2a"
# Earth Search v1's asset keys for the bands the worker reads.
ASSETS = {"nir": "nir", "green": "green", "scl": "scl"}
_STAC_TIMEOUT_SECONDS = 20.0

MAX_SITES = 10
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
}

COPERNICUS_SOURCE = {
    "text": "Contains modified Copernicus Sentinel data",
    "label": "Copernicus Sentinel data",
    "url": "https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice",
}
AWS_OPEN_DATA_SOURCE = {
    "text": "Sentinel-2 Cloud-Optimized GeoTIFFs accessed from the Registry of Open Data on AWS",
    "label": "Registry of Open Data on AWS",
    "url": "https://registry.opendata.aws/sentinel-2-l2a-cogs/",
}


class ConfigError(ValueError):
    """The topic's adapter_config can't be used."""


def plain(value):
    """`value` with DynamoDB's Decimals turned into ints and floats, all the way down."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def parse_config(adapter_config: dict | None) -> dict:
    """adapter_config with defaults filled in, or ConfigError. Site polygons are checked properly
    by the worker contract before any call; here only their shape."""
    config = {**DEFAULTS, **plain(adapter_config or {})}
    sites = config.get("sites")
    if not isinstance(sites, list) or not 1 <= len(sites) <= MAX_SITES:
        raise ConfigError(f"adapter_config.sites needs 1 to {MAX_SITES} sites")
    ids = set()
    for site in sites:
        if not isinstance(site, dict) or not site.get("id") or not isinstance(site.get("polygon"), list):
            raise ConfigError("each site needs an id and a polygon")
        if site["id"] in ids:
            raise ConfigError(f"duplicate site id {site['id']!r}")
        ids.add(site["id"])
    if config["backend"] not in ("opencv", "cool"):
        raise ConfigError("backend must be opencv or cool")
    for key in ("history_size", "min_baseline", "max_sites_per_tick", "lookback_days"):
        config[key] = int(config[key])
    for key in ("coverage_floor", "relative_threshold", "absolute_threshold", "max_cloud_cover",
                "time_budget_seconds"):  # fmt: skip
        config[key] = float(config[key])
    if config["history_size"] < 2 or config["min_baseline"] < 1:
        raise ConfigError("history_size must be at least 2 and min_baseline at least 1")
    return config


def site_bbox(polygon: list) -> tuple[float, float, float, float]:
    lons = [float(p[0]) for p in polygon]
    lats = [float(p[1]) for p in polygon]
    return min(lons), min(lats), max(lons), max(lats)


def search_newest_scene(
    site: dict, config: dict, now: datetime, http_post=None
) -> dict | None:
    """The newest scene that fully covers the site and has every band the worker reads, as
    {"id", "captured_at", "cloud_cover", "assets"}; None if there is none in the lookback."""
    post = http_post or requests.post
    start = (now - timedelta(days=config["lookback_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "collections": [STAC_COLLECTION],
        "intersects": {"type": "Polygon", "coordinates": [_closed_ring(site["polygon"])]},
        "datetime": f"{start}/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "query": {"eo:cloud_cover": {"lte": config["max_cloud_cover"]}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        "limit": 10,
    }
    response = post(STAC_SEARCH_URL, json=body, timeout=_STAC_TIMEOUT_SECONDS)
    response.raise_for_status()
    west, south, east, north = site_bbox(site["polygon"])
    for item in response.json().get("features") or []:
        box = item.get("bbox") or []
        if len(box) != 4 or not (box[0] <= west <= east <= box[2] and box[1] <= south <= north <= box[3]):
            continue  # the site straddles this tile's edge; another tile covers it whole
        assets = item.get("assets") or {}
        hrefs = {name: (assets.get(key) or {}).get("href") for name, key in ASSETS.items()}
        if not all(hrefs.values()):
            continue
        props = item.get("properties") or {}
        return {
            "id": item.get("id"),
            "captured_at": props.get("datetime"),
            "cloud_cover": props.get("eo:cloud_cover"),
            "assets": hrefs,
        }
    return None


def _closed_ring(polygon: list) -> list:
    ring = [[float(x), float(y)] for x, y in polygon]
    return ring if ring[0] == ring[-1] else [*ring, ring[0]]


def _store_image(topic_id: str, site_id: str, scene_id: str, png: bytes) -> str:
    key = f"vision/{topic_id}/{site_id}/{scene_id}.png"
    boto3.client("s3").put_object(
        Bucket=os.environ["CONTENT_BUCKET"], Key=key, Body=png, ContentType="image/png"
    )
    return key


def clear_entries(history: list[dict], floor: float) -> list[dict]:
    """The history entries a baseline may use: measured, and with coverage at the floor."""
    return [e for e in history if e.get("count") is not None and (e.get("coverage") or 0) >= floor]


class SatelliteVisionAdapter(Adapter):
    uses_previous_state = True
    keeps_running_state = True
    sources = (COPERNICUS_SOURCE, AWS_OPEN_DATA_SOURCE)

    # Injected by tests; the defaults talk to Earth Search, the worker and S3.
    http_post = None
    measure = staticmethod(vision_client.measure)
    store_image = staticmethod(_store_image)
    clock = staticmethod(time.monotonic)

    def fetch_state(self, topic_config: dict, previous_state: dict | None = None) -> dict:
        config = parse_config(topic_config.get("adapter_config"))
        topic_id = topic_config.get("topic_id", "topic")
        now = datetime.now(UTC)
        started = self.clock()
        previous_sites = (previous_state or {}).get("sites") or {}
        sites, measured = {}, []

        for site in config["sites"]:
            record = copy.deepcopy(previous_sites.get(site["id"])) or {"history": []}
            record["name"] = site.get("name") or site["id"]
            sites[site["id"]] = record
            if len(measured) >= config["max_sites_per_tick"]:
                continue
            if self.clock() - started > config["time_budget_seconds"]:
                record["last_error"] = {"at": now.isoformat(), "code": "time_budget", "detail": "next tick"}
                continue
            try:
                scene = search_newest_scene(site, config, now, http_post=self.http_post)
            except Exception as exc:  # noqa: BLE001 - one site's search must not stop the others
                record["last_error"] = {"at": now.isoformat(), "code": "search", "detail": str(exc)[:300]}
                continue
            known = {entry.get("scene_id") for entry in record["history"]}
            if scene is None or scene["id"] in known:
                record.pop("last_error", None)
                continue
            try:
                result = self.measure(
                    {"id": site["id"], "polygon": site["polygon"]},
                    {"id": scene["id"], "captured_at": scene["captured_at"], "assets": scene["assets"]},
                    backend=config["backend"],
                    params=config["params"],
                    coverage_floor=config["coverage_floor"],
                )
            except vision_client.VisionError as exc:
                record["last_error"] = {"at": now.isoformat(), "code": exc.code, "detail": exc.detail[:300]}
                continue

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
            }
            if result.image_png:
                try:
                    entry["image_key"] = self.store_image(topic_id, site["id"], scene["id"], result.image_png)
                except Exception as exc:  # noqa: BLE001 - a lost figure doesn't lose the count
                    print(f"satellite_vision: could not store the figure for {site['id']}: {exc!r}")
            record["history"] = [*record["history"], entry][-config["history_size"] :]
            record.pop("last_error", None)
            measured.append({"site_id": site["id"], "scene_id": scene["id"]})

        return {
            "fetched_at": now.isoformat(),
            "object_noun": config["object_noun"],
            # The diff gets only the two states, so the topic's thresholds travel in the state.
            "thresholds": {key: config[key] for key in THRESHOLD_KEYS},
            "sites": sites,
            "measured": measured,
        }

    def assess(self, new_state: dict) -> list[dict]:
        """One verdict per site measured this tick: its count against its baseline, and whether
        that is material, by the thresholds stored in the state."""
        config = {**{key: DEFAULTS[key] for key in THRESHOLD_KEYS}, **(new_state.get("thresholds") or {})}
        verdicts = []
        for m in new_state.get("measured") or []:
            record = (new_state.get("sites") or {}).get(m["site_id"]) or {}
            history = record.get("history") or []
            if not history or history[-1].get("scene_id") != m["scene_id"]:
                continue
            latest, earlier = history[-1], history[:-1]
            baseline_entries = clear_entries(earlier, config["coverage_floor"])
            verdict = {
                "site_id": m["site_id"],
                "name": record.get("name") or m["site_id"],
                "scene_id": latest["scene_id"],
                "captured_at": latest.get("captured_at"),
                "count": latest["count"],
                "coverage": latest["coverage"],
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
                baseline = statistics.median(e["count"] for e in baseline_entries)
                delta = latest["count"] - baseline
                relative = delta / max(baseline, 1.0)
                verdict.update(baseline=baseline, delta=delta, relative=round(relative, 3))
                big_enough = abs(delta) >= config["absolute_threshold"]
                if big_enough and abs(relative) >= config["relative_threshold"]:
                    verdict.update(material=True, reason="changed against the baseline")
                else:
                    verdict["reason"] = "within the usual range"
            verdicts.append(verdict)
        return verdicts

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        verdicts = self.assess(new_state)
        noun = new_state.get("object_noun") or DEFAULTS["object_noun"]
        if old_state is None:
            lines = [f"{v['name']}: {v['count']} {noun} (coverage {v['coverage']:.0%})" for v in verdicts]
            return True, "First observation. " + ("; ".join(lines) if lines else "No scene measured yet.")
        material = [v for v in verdicts if v["material"]]
        if not material:
            return False, "; ".join(f"{v['name']}: {v['reason']}" for v in verdicts) or "no new scene"
        return True, "; ".join(_describe(v, noun) for v in material)

    def source_refs(self, new_state: dict) -> list[dict]:
        refs = []
        for m in new_state.get("measured") or []:
            refs.append(
                {
                    "url": STAC_ITEM_URL.format(collection=STAC_COLLECTION, item_id=m["scene_id"]),
                    "title": f"Sentinel-2 L2A scene {m['scene_id']}",
                    "accessed_at": new_state.get("fetched_at"),
                }
            )
        return refs

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


def _describe(v: dict, noun: str) -> str:
    sign = "+" if v["delta"] >= 0 else ""
    return (
        f"{v['name']}: {v['count']} {noun} in scene {v['scene_id']} (captured {v['captured_at']}), "
        f"against a baseline of {v['baseline']:g} from {v['baseline_scenes']} earlier clear scenes "
        f"({sign}{v['delta']:g}, {sign}{v['relative']:.0%}); coverage {v['coverage']:.0%}"
    )
