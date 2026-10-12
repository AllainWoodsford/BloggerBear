"""What the vision adapters share: sites, scenes, history, figures and the tick's time budget.

docs/enhancements/rail-access-monitor.md §3 and §4 (PR E). Two adapters watch fixed sites in
Sentinel-2 scenes: satellite_vision (the ships task) and rail_access. This module is the part of
the first that the second needed as it was: the config checks every vision topic shares, the
Earth Search STAC search and the choice of scene, the figure store in the content bucket, the
baseline's clear entries, the budget a tick measures within, the STAC source references, the rule
that the first observation is the first scene at or above the coverage floor (a tick that measured
nothing, or only below it, is not), a polygon's `site_key`, and the order sites are visited in, so
a topic with several rotates through them. The adapters keep what differs: which
bands, which metrics, what is material, what the agent is told.

No OpenCV, numpy or networkx here: the adapter registry is imported by every pipeline Lambda.

**Data licences** (docs/risks/opencv-bushfire-watch-01.md item 1, checked 2026-10-03):
Sentinel-2 is free, full and open under the Copernicus Sentinel data legal notice, which asks
derived products to carry "Contains modified Copernicus Sentinel data [year]"
(https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice). The AWS copy
asks to be cited as accessed from https://registry.opendata.aws/sentinel-2-l2a-cogs. The year
varies by scene, so the class-level credits below name the data and its source; each article's
figure caption carries the capture year.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal

import boto3
import requests

from common.figures import plain_text

STAC_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
STAC_ITEM_URL = "https://earth-search.aws.element84.com/v1/collections/{collection}/items/{item_id}"
STAC_COLLECTION = "sentinel-2-l2a"
# Earth Search v1's asset keys for the bands the ships task reads: the search's default.
ASSETS_SHIPS = {"nir": "nir", "green": "green", "scl": "scl"}
STAC_TIMEOUT_SECONDS = 20.0

MAX_SITES = 10
# The research tick's Lambda has 120 s. Whatever a topic asks, its measuring loop may not take
# more than this, so the triage agent and the summary call keep their share of the tick.
MAX_TIME_BUDGET_SECONDS = 80.0
# ... and the agent must have answered by this many seconds after the tick started, so the
# summary call and the stores keep the last 30 (docs §5 risk 1 sets the default at 85).
MAX_TRIAGE_DEADLINE_SECONDS = 90.0
# The agent's tool calls: each may be a worker call or a news search, inside the deadline above.
MAX_TRIAGE_TOOL_CALLS = 5
# Scenes kept per site: the running state and a Finding's snapshot carry every one (a rail entry
# can hold 2000 station rows), and a baseline of a few months of Sentinel-2 revisits is plenty.
MAX_HISTORY_SIZE = 20
# A site's name goes into captions, alt text, summaries and the agent's brief: one plain line,
# short enough that a caption keeps its credits (common/figures.py) without cutting it.
MAX_SITE_NAME_CHARS = 80
# A site id becomes part of an S3 key and a figure key (vision/<topic>/<site>/<scene>.png), which
# common/figures.py's _KEY accepts only with plain path characters and no "..": letters, digits,
# "_" and "-" always pass it, and 64 keeps the key well under its 200.
SITE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# The config keys every vision topic has, by how they are read: whole numbers and floats. An
# adapter adds its own to these when it parses its config.
WHOLE_KEYS = ("history_size", "min_baseline", "max_sites_per_tick", "lookback_days", "triage_max_tool_calls")
FLOAT_KEYS = ("coverage_floor", "max_cloud_cover", "time_budget_seconds", "triage_deadline_seconds")

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


def parse_site_config(
    adapter_config: dict | None,
    defaults: dict,
    whole: tuple[str, ...] = WHOLE_KEYS,
    floats: tuple[str, ...] = FLOAT_KEYS,
    max_sites: int = MAX_SITES,
) -> dict:
    """`adapter_config` over `defaults`, with the checks every vision topic shares, or ConfigError:
    1 to `max_sites` sites each with a figure-safe id (SITE_ID) and a polygon (checked properly by
    the worker contract before any call; here only its shape), ids unique, a name (when given) of
    one plain line within MAX_SITE_NAME_CHARS, a known backend, `whole` keys as ints and `floats`
    as finite floats, `triage` and `web_context` as booleans, and every bound below: a time budget
    in (0, MAX_TIME_BUDGET_SECONDS], the agent's deadline in (0, MAX_TRIAGE_DEADLINE_SECONDS], its
    tool calls 0 to MAX_TRIAGE_TOOL_CALLS, a history of 2 to MAX_HISTORY_SIZE with a baseline of
    at least 1 and fewer scenes than the history keeps, 1 to `max_sites` sites per tick, a coverage
    floor in [0, 1] and no negative `*_threshold`."""
    config = {**defaults, **plain(adapter_config or {})}
    sites = config.get("sites")
    if not isinstance(sites, list) or not 1 <= len(sites) <= max_sites:
        raise ConfigError(f"adapter_config.sites needs 1 to {max_sites} sites")
    ids, cleaned = set(), []
    for site in sites:
        if not isinstance(site, dict) or not site.get("id") or not isinstance(site.get("polygon"), list):
            raise ConfigError("each site needs an id and a polygon")
        if not isinstance(site["id"], str) or not SITE_ID.fullmatch(site["id"]):
            raise ConfigError("a site id may only have letters, digits, _ and - (at most 64)")
        if site["id"] in ids:
            raise ConfigError(f"duplicate site id {site['id']!r}")
        ids.add(site["id"])
        if site.get("name") is not None:
            name = plain_text(site["name"]) if isinstance(site["name"], str) else ""
            if not name or len(name) > MAX_SITE_NAME_CHARS:
                raise ConfigError(f"a site name must be text of 1 to {MAX_SITE_NAME_CHARS} characters")
            site = {**site, "name": name}
        cleaned.append(site)
    config["sites"] = cleaned
    if config["backend"] not in ("opencv", "cool"):
        raise ConfigError("backend must be opencv or cool")
    for keys, kind in ((whole, int), (floats, float)):
        for key in keys:
            if isinstance(config[key], bool):
                raise ConfigError(f"{key} must be a number")
            try:
                config[key] = kind(config[key])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ConfigError(f"{key} must be a number") from exc
            if kind is float and not math.isfinite(config[key]):
                raise ConfigError(f"{key} must be a finite number")
    for key in ("triage", "web_context"):
        if key in config and not isinstance(config[key], bool):
            raise ConfigError(f"{key} must be true or false")
    _within(config, "time_budget_seconds", 0, MAX_TIME_BUDGET_SECONDS, low_open=True)
    _within(config, "triage_deadline_seconds", 0, MAX_TRIAGE_DEADLINE_SECONDS, low_open=True)
    _within(config, "triage_max_tool_calls", 0, MAX_TRIAGE_TOOL_CALLS)
    _within(config, "history_size", 2, MAX_HISTORY_SIZE)
    # The baseline is drawn from the scenes before the newest, so the history must keep more.
    _within(config, "min_baseline", 1, config["history_size"] - 1)
    _within(config, "max_sites_per_tick", 1, max_sites)
    _within(config, "coverage_floor", 0, 1)
    for key in floats:
        if key.endswith("_threshold") and config[key] < 0:
            raise ConfigError(f"{key} must not be negative")
    return config


def _within(config: dict, key: str, low: float, high: float, low_open: bool = False) -> None:
    value = config[key]
    if value > high or value < low or (low_open and value == low):
        bracket = "(" if low_open else "["
        raise ConfigError(f"{key} must be in {bracket}{low:g}, {high:g}]")


def site_key(polygon) -> str:
    """A short, stable fingerprint of a site's polygon (the first 16 hex of the SHA-256 of its JSON,
    numbers as floats so a Decimal or an int read back from DynamoDB gives the same key). A history
    entry and a cached network carry it, so a polygon edited in the config is seen: its scenes are
    not compared with the old area's, and the old area's network is not used for it."""

    def norm(value):
        if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, list | tuple):
            return [norm(v) for v in value]
        return value

    text = json.dumps(norm(polygon), separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def site_bbox(polygon: list) -> tuple[float, float, float, float]:
    lons = [float(p[0]) for p in polygon]
    lats = [float(p[1]) for p in polygon]
    return min(lons), min(lats), max(lons), max(lats)


def search_newest_scene(
    site: dict, config: dict, now: datetime, http_post=None, assets: dict = ASSETS_SHIPS
) -> dict | None:
    """The newest scene that fully covers the site and has every band in `assets` ({band name:
    Earth Search asset key}), as {"id", "captured_at", "cloud_cover", "assets"}; None if there is
    none in the lookback."""
    post = http_post or requests.post
    start = (now - timedelta(days=config["lookback_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "collections": [STAC_COLLECTION],
        "intersects": {"type": "Polygon", "coordinates": [closed_ring(site["polygon"])]},
        "datetime": f"{start}/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "query": {"eo:cloud_cover": {"lte": config["max_cloud_cover"]}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        "limit": 10,
    }
    response = post(STAC_SEARCH_URL, json=body, timeout=STAC_TIMEOUT_SECONDS)
    response.raise_for_status()
    west, south, east, north = site_bbox(site["polygon"])
    for item in response.json().get("features") or []:
        box = item.get("bbox") or []
        if len(box) != 4 or not (box[0] <= west <= east <= box[2] and box[1] <= south <= north <= box[3]):
            continue  # the site straddles this tile's edge; another tile covers it whole
        found = item.get("assets") or {}
        hrefs = {name: (found.get(key) or {}).get("href") for name, key in assets.items()}
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


def closed_ring(polygon: list) -> list:
    ring = [[float(x), float(y)] for x, y in polygon]
    return ring if ring[0] == ring[-1] else [*ring, ring[0]]


def store_image(topic_id: str, site_id: str, scene_id: str, png: bytes) -> str:
    """The worker's figure for one scene, kept in the content bucket; returns its key."""
    key = f"vision/{topic_id}/{site_id}/{scene_id}.png"
    boto3.client("s3").put_object(
        Bucket=os.environ["CONTENT_BUCKET"], Key=key, Body=png, ContentType="image/png"
    )
    return key


def load_image(key: str) -> bytes | None:
    response = boto3.client("s3").get_object(Bucket=os.environ["CONTENT_BUCKET"], Key=key)
    return response["Body"].read()


def clear_entries(history: list[dict], floor: float, value_key: str = "count") -> list[dict]:
    """The history entries a baseline may use: measured (`value_key` present) and with coverage
    at the floor."""
    return [e for e in history if e.get(value_key) is not None and (e.get("coverage") or 0) >= floor]


class Budget:
    """The time a tick's measuring loop may take, counted from when the budget was made, by
    `clock` (time.monotonic in the Lambda; a counter in tests). Each question asks the clock
    once."""

    def __init__(self, clock, seconds: float):
        self.clock, self.seconds = clock, float(seconds)
        self.started = clock()

    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining(self) -> float:
        return self.seconds - self.elapsed()

    def exhausted(self) -> bool:
        return self.elapsed() > self.seconds


def stac_refs(measured: list[dict], fetched_at: str | None) -> list[dict]:
    """One source reference per scene measured this tick: the STAC item a reader can open."""
    return [
        {
            "url": STAC_ITEM_URL.format(collection=STAC_COLLECTION, item_id=m["scene_id"]),
            "title": f"Sentinel-2 L2A scene {m['scene_id']}",
            "accessed_at": fetched_at,
        }
        for m in measured or []
    ]


def order_sites(sites: list[dict], records: dict | None) -> list[dict]:
    """`sites` (the config's list) in the order a tick visits them: a site never attempted first,
    then by the oldest `last_attempt_at` in its record, the config's order breaking ties. A site is
    attempted when the tick works on it to an outcome (measured, nothing new, a fetch, search or
    worker error); the adapters record `last_attempt_at` then, and not when a site is skipped for
    the tick's cap or deferred for its budget. So a topic with more sites than it measures per tick
    rotates through them, a site added later is seen first, and a site that fails slowly every
    tick cannot keep the others waiting: a site it left without time is still unstamped, or
    stamped earlier, and goes first next tick. (Never attempted, not never measured, leads: a site
    that has failed once and never measured would otherwise lead every tick and could block the
    rest.)"""

    def key(indexed):
        index, site = indexed
        record = (records or {}).get(site.get("id")) or {}
        attempted = record.get("last_attempt_at")
        return (attempted is not None, attempted or "", index)

    return [site for _, site in sorted(enumerate(sites), key=key)]


def anything_measured(state: dict | None, floor: float = 0.0) -> bool:
    """Whether any site in `state` has a scene in its history measured at a coverage of at least
    `floor`: the first tick with one is the topic's first observation; a tick before that measured
    nothing clearly (nothing at all, or only scenes below the coverage floor) and is not."""
    return any(
        (entry.get("coverage") or 0) >= floor
        for record in ((state or {}).get("sites") or {}).values()
        for entry in ((record or {}).get("history") or [])
        if isinstance(entry, dict)
    )


def first_observation(old_state: dict | None, new_state: dict, verdicts: list[dict], floor: float):
    """The first-observation rule both adapters share. None when `old_state` already has a clear
    scene: this tick is compared with a baseline, not first. Otherwise (`clear`, `summary`):
    `clear` is this tick's verdicts at or above the coverage `floor`, which are the topic's first
    observation; when there are none, `summary` says why it is not material yet ("nothing measured
    yet: ..." with each site's error, or "nothing measured clearly yet: ..." with each scene's
    coverage). A first scene below the floor is kept in the history but reports nothing, so the
    next clear scene is the first observation."""
    if anything_measured(old_state, floor):
        return None
    clear = [v for v in verdicts if v["coverage"] >= floor]
    if clear:
        return clear, ""
    if not verdicts:
        return [], f"nothing measured yet: {site_errors(new_state) or 'no scene to measure'}"
    lines = [
        f"{v['name']}: coverage {v['coverage'] * 100:.0f} % is below the {floor * 100:.0f} % floor"
        for v in verdicts
    ]
    return [], "nothing measured clearly yet: " + "; ".join(lines)


def site_errors(state: dict) -> str:
    """Each site's last error, for the no-change summary of a tick that measured nothing."""
    parts = []
    for site_id, record in (state.get("sites") or {}).items():
        error = (record or {}).get("last_error")
        if error:
            parts.append(f"{record.get('name') or site_id}: {error.get('code')}")
    return "; ".join(parts)
