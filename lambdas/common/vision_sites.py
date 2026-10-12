"""What the vision adapters share: sites, scenes, history, figures and the tick's time budget.

docs/enhancements/rail-access-monitor.md §3 and §4 (PR E). Two adapters watch fixed sites in
Sentinel-2 scenes: satellite_vision (the ships task) and rail_access. This module is the part of
the first that the second needed as it was: the config checks every vision topic shares, the
Earth Search STAC search and the choice of scene, the figure store in the content bucket, the
baseline's clear entries, the budget a tick measures within, the STAC source references, the rule
that a tick which measured nothing is not yet the first observation, and the order sites are
visited in, so a topic with several rotates through them. The adapters keep what differs: which
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

import os
from datetime import datetime, timedelta
from decimal import Decimal

import boto3
import requests

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
    1 to `max_sites` sites each with an id and a polygon (checked properly by the worker contract
    before any call; here only their shape), ids unique, a known backend, `whole` keys as ints and
    `floats` as floats, a history of at least 2 with a baseline of at least 1, and a time budget
    within MAX_TIME_BUDGET_SECONDS."""
    config = {**defaults, **plain(adapter_config or {})}
    sites = config.get("sites")
    if not isinstance(sites, list) or not 1 <= len(sites) <= max_sites:
        raise ConfigError(f"adapter_config.sites needs 1 to {max_sites} sites")
    ids = set()
    for site in sites:
        if not isinstance(site, dict) or not site.get("id") or not isinstance(site.get("polygon"), list):
            raise ConfigError("each site needs an id and a polygon")
        if site["id"] in ids:
            raise ConfigError(f"duplicate site id {site['id']!r}")
        ids.add(site["id"])
    if config["backend"] not in ("opencv", "cool"):
        raise ConfigError("backend must be opencv or cool")
    for keys, kind in ((whole, int), (floats, float)):
        for key in keys:
            try:
                config[key] = kind(config[key])
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{key} must be a number") from exc
    if config["history_size"] < 2 or config["min_baseline"] < 1:
        raise ConfigError("history_size must be at least 2 and min_baseline at least 1")
    if config["time_budget_seconds"] > MAX_TIME_BUDGET_SECONDS:
        raise ConfigError(f"time_budget_seconds must be at most {MAX_TIME_BUDGET_SECONDS:g}")
    return config


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
    attempted when the tick does its work (the fetch, the search, the measurement), not when it is
    skipped for the tick's cap or budget; the adapters record `last_attempt_at` then. So a topic
    with more sites than it measures per tick rotates through them, a site added later is seen
    first, and a site that fails slowly every tick cannot keep the others waiting."""

    def key(indexed):
        index, site = indexed
        record = (records or {}).get(site.get("id")) or {}
        attempted = record.get("last_attempt_at")
        return (attempted is not None, attempted or "", index)

    return [site for _, site in sorted(enumerate(sites), key=key)]


def anything_measured(state: dict | None) -> bool:
    """Whether any site in `state` has a measured scene in its history: the first tick that has
    is the topic's first observation; a tick before that measured nothing and is not."""
    return any((record or {}).get("history") for record in ((state or {}).get("sites") or {}).values())


def site_errors(state: dict) -> str:
    """Each site's last error, for the no-change summary of a tick that measured nothing."""
    parts = []
    for site_id, record in (state.get("sites") or {}).items():
        error = (record or {}).get("last_error")
        if error:
            parts.append(f"{record.get('name') or site_id}: {error.get('code')}")
    return "; ".join(parts)
