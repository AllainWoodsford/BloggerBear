#!/usr/bin/env python3
"""The evaluation: its inputs, the pinned Sentinel-2 scenes (`pin`) and what each one shows of the
site (`coverage`); the replay trace (`replay`), the failure catalogue (`failures`), the access
baseline (`access-baseline`) and the results index (`index`). One run file per run, as
scripts/vision_runs.py writes it.

    python scripts/vision_eval.py pin           # HEAD every asset, read every STAC item; print the changes
    python scripts/vision_eval.py pin --write   # ... and write them into the manifest
    python scripts/vision_eval.py pin --list-year 56HLH 2024 --write   # add a tile's year (the year set)
    python scripts/vision_eval.py coverage --out docs/hackathon/evidence/runs --write   # E1, E2, the run file
    python scripts/vision_eval.py replay --sequence sydney-demo --agent bedrock --model-id ID --worker ARN
    python scripts/vision_eval.py failures --cache ~/cogs --out docs/hackathon/evidence/runs   # 2.5
    python scripts/vision_eval.py access-baseline --cache ~/cogs --out docs/hackathon/evidence/runs   # E5
    python scripts/vision_eval.py index [--check]   # docs/hackathon/evidence/index.json and RESULTS.md

The manifest, scripts/vision_eval_manifest.json (`bloggerbear.vision-manifest/1`), names each
scene by its id on AWS Open Data (`sentinel-cogs`, us-west-2), its four assets (B04, B08, B11, SCL)
with the ETag and size `pin` found, the STAC item's cloud cover and no-data share (read from the
item JSON kept beside the assets, so no Earth Search call is made), its role, and what `coverage`
measured. Add a scene; never change one: a run compares each asset's ETag with a HEAD before it
reads, and refuses the run ("input changed") on a difference, so every run file's inputs are the
bytes the manifest names.

`pin` reads `sentinel-cogs.s3.us-west-2.amazonaws.com` only, with 10 s timeouts; a URL at any
other host is refused before any request. `--list-year TILE YEAR` lists the bucket's items for
that tile and year (`list-type=2` with a delimiter, month by month) and, with `--write`, adds
every item that passes the production rule (cloud cover at most the adapter's `max_cloud_cover`,
a footprint that holds the site's box) as a `year` scene, one per date: an item of the same date
from Sentinel-2C (its tandem flight beside 2A), or with more no-data, or a later processing
sequence, is kept as `duplicate_of` the one chosen, so it is still measured but not in the set.

`coverage` reads each scene's SCL inside the site polygon with the repository's own COG reader
(E1: the share of every scene-classification class, the coverage as the worker defines it from
the same unusable classes, the dominant cause when it is under the adapter's floor) and the mean
aerosol optical thickness from the scene's `AOT` band (smoke that SCL does not flag), about 60
range requests and 0.7 MB a scene. From the same pass over the year's listing it counts E2: how
many items pass the cloud filter and the footprint rule, and how many of those are still under
the floor, worker calls that could only be wasted. `--write` puts `site_coverage` and `aot_mean`
into the manifest, and the year sets under `sets` (`year-2024`: the scenes a run of the year set
reads, by name; a scene's `year` role only marks a candidate); `--cache DIR` answers the reads
from the cache after the first.

`replay` (2.6.4) drives the real rail access adapter over a pinned sequence through
scripts/vision_eval_driver.py (whose docstring says how): the worker in this process or deployed,
the agent on Bedrock, recorded or scripted, the trace rendered through vision_decisions.trace. A
recorded replay recomputes every measurement and is held to its recording. `failures` (2.5) runs
the failure catalogue's real-data rows through the worker handler in this process, and checks that
pytest collects every test the catalogue names. `access-baseline` (E5) compares the production
distance transform with the precise one and with a straight-line buffer on one scene. `index`
(2.9) writes the results index from the run files, refusing a malformed one; `--check` fails
when it is stale (scripts/tests/test_vision_eval.py runs it wherever python-ci runs).

Runs on Windows as well as Linux and macOS (nothing here pins a CPU or reads `resource`). Needs
lambdas/requirements-vision.txt and scripts/requirements.txt installed (and, for `coverage`,
`replay`, `failures` and `access-baseline`, lambdas/requirements.txt: they import the adapter;
for the check that pytest collects the failure catalogue's tests, lambdas/requirements-dev.txt).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import vision_runs
from vision_benchmark import NETWORK_FILE_MISSING, cpu_identity

ROOT = Path(__file__).resolve().parents[1]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

MANIFEST_FILE = Path(__file__).with_name("vision_eval_manifest.json")
MANIFEST_SCHEMA = "bloggerbear.vision-manifest/1"
BUCKET = vision_runs.BUCKET_PREFIX + "sentinel-s2-l2a-cogs"
SCENE_ID = re.compile(r"^S2[ABC]_(\d{2})([A-Z])([A-Z]{2})_(\d{4})(\d{2})(\d{2})_(\d)_L2A$")
# The rail task's bands, in the manifest's names, and the bucket's file for each.
BANDS = {"red": "B04", "nir": "B08", "swir16": "B11", "scl": "SCL"}
AOT_FILE = "AOT"
ROLES = ("clear", "season", "cloud", "nodata", "smoke", "baseline", "year")
# Sentinel-2 L2A scene classification: the class each value names.
SCL_NAMES = {
    0: "no_data", 1: "saturated", 2: "dark", 3: "cloud_shadow", 4: "vegetation", 5: "not_vegetated",
    6: "water", 7: "unclassified", 8: "cloud_medium", 9: "cloud_high", 10: "cirrus", 11: "snow",
}  # fmt: skip
# The classes over which the mean aerosol optical thickness is taken: the surface, seen.
SCL_CLEAR = (4, 5, 6)
# The L2A AOT band is the optical thickness times 1000, as an integer.
AOT_SCALE = 1000.0
# A below-floor scene is "explained" when one unusable class holds at least this share of what
# was lost: a judge can then name the cause (cloud, or the swath's edge) rather than a mixture.
DOMINANT_SHARE = 0.5
# A `year` scene is in the year set (E3's input) from this site coverage up: the clear scenes of a
# year on an unchanged city, so every difference the rule sees comes from the imagery's edges.
YEAR_SET_MIN_COVERAGE = 0.95
GET_TIMEOUT_SECONDS = vision_runs.HEAD_TIMEOUT_SECONDS


# --- the manifest ----------------------------------------------------------------------------------


def load_manifest(path: Path = MANIFEST_FILE) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"{path}: not a {MANIFEST_SCHEMA} manifest")
    return manifest


def save_manifest(manifest: dict, path: Path = MANIFEST_FILE) -> None:
    Path(path).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")


def scene_folder(scene_id: str) -> str:
    """The bucket folder of a scene: `<zone>/<band>/<square>/<year>/<month, no zero>/<scene>`."""
    match = SCENE_ID.match(scene_id)
    if not match:
        raise ValueError(f"not a Sentinel-2 L2A scene id: {scene_id!r}")
    zone, band, square, year, month, _day, _seq = match.groups()
    return f"{BUCKET}/{zone}/{band}/{square}/{year}/{int(month)}/{scene_id}"


def scene_assets(scene_id: str) -> dict[str, dict]:
    """The four rail assets of a scene, unpinned (`pin` fills the ETag and size)."""
    folder = scene_folder(scene_id)
    return {band: {"url": f"{folder}/{file}.tif", "etag": None, "bytes": None}
            for band, file in BANDS.items()}  # fmt: skip


def stac_item_url(scene_id: str) -> str:
    return f"{scene_folder(scene_id)}/{scene_id}.json"


def aot_url(scene_id: str) -> str:
    return f"{scene_folder(scene_id)}/{AOT_FILE}.tif"


def tile_of(scene_id: str) -> str:
    zone, band, square = SCENE_ID.match(scene_id).groups()[:3]
    return f"{zone}{band}{square}"


def date_of(scene_id: str) -> str:
    _zone, _band, _square, year, month, day, _seq = SCENE_ID.match(scene_id).groups()
    return f"{year}-{month}-{day}"


def new_scene(name: str, site: str, scene_id: str, role: str, note: str | None = None,
              expected: dict | None = None) -> dict:  # fmt: skip
    """A manifest scene, unpinned and unmeasured."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}: {role!r}")
    return {
        "name": name,
        "site": site,
        "scene_id": scene_id,
        "tile": tile_of(scene_id),
        "captured_at": None,
        "role": role,
        "note": note,
        "assets": scene_assets(scene_id),
        "item": {"cloud_cover": None, "nodata_pct": None, "bbox": None},
        "site_coverage": None,
        "aot_mean": None,
        "expected": expected,
    }


# --- pin ----------------------------------------------------------------------------------------------


def default_get(url: str, timeout: float = GET_TIMEOUT_SECONDS) -> bytes:
    """One GET of a bucket URL (no redirects followed), its body."""
    import requests

    response = requests.get(vision_runs.check_host(url), timeout=(timeout, timeout), allow_redirects=False)
    if response.status_code != 200:
        raise OSError(f"GET {url}: HTTP {response.status_code}")
    return response.content


def item_fields(item: dict) -> dict:
    """What the manifest keeps of a STAC item: the capture time, the cloud cover, the tile's
    no-data share, the footprint's bbox, and the platform and datatake for the year rule."""
    props = item.get("properties") or {}
    return {
        "captured_at": props.get("datetime"),
        "cloud_cover": props.get("eo:cloud_cover"),
        "nodata_pct": props.get("s2:nodata_pixel_percentage"),
        "bbox": item.get("bbox"),
        "platform": props.get("platform"),
        "datatake_id": props.get("s2:datatake_id"),
        "processing_baseline": props.get("s2:processing_baseline"),
    }


def read_item(scene_id: str, get=None) -> dict:
    get = get or default_get
    return item_fields(json.loads(get(stac_item_url(scene_id)).decode("utf-8")))


def pin(manifest: dict, head=None, get=None) -> tuple[list[str], bool]:
    """HEAD every asset of every scene (ETag, Content-Length) and read every scene's STAC item;
    the manifest is updated in place. Returns the differences found, one line each, and whether
    any pinned ETag changed (an alarm: that scene's bytes are not the ones earlier runs read).
    Every URL is checked against the bucket before the first request."""
    head = head or vision_runs.default_head
    for scene in manifest["scenes"]:
        if not scene.get("assets"):
            scene["assets"] = scene_assets(scene["scene_id"])
        for asset in scene["assets"].values():
            vision_runs.check_host(asset["url"])
        vision_runs.check_host(stac_item_url(scene["scene_id"]))
    lines, changed = [], False
    for scene in manifest["scenes"]:
        for band, asset in scene["assets"].items():
            found = head(asset["url"])
            for key in ("etag", "bytes"):
                old, new = asset.get(key), found.get(key)
                if old == new:
                    continue
                if old is None:
                    lines.append(f"{scene['name']} {band} {key}: set to {new}")
                else:
                    lines.append(f"{scene['name']} {band} {key}: CHANGED from {old} to {new}")
                    changed = changed or key == "etag"
                asset[key] = new
        item = read_item(scene["scene_id"], get)
        for key in ("cloud_cover", "nodata_pct", "bbox"):
            old, new = (scene.get("item") or {}).get(key), item.get(key)
            if old != new:
                lines.append(f"{scene['name']} item.{key}: {old} -> {new}")
                scene.setdefault("item", {})[key] = new
        if scene.get("captured_at") != item["captured_at"]:
            lines.append(f"{scene['name']} captured_at: {scene.get('captured_at')} -> {item['captured_at']}")
            scene["captured_at"] = item["captured_at"]
    return lines, changed


def list_year(tile: str, year: int, get=None) -> list[dict]:
    """Every item of `tile` in `year` from the bucket's listing (month by month, one level deep),
    each with its STAC fields: the year set's candidates, in scene-id order."""
    get = get or default_get
    zone, band, square = tile[:2], tile[2], tile[3:]
    items = []
    for month in range(1, 13):
        prefix = f"sentinel-s2-l2a-cogs/{zone}/{band}/{square}/{year}/{month}/"
        xml = get(f"{vision_runs.BUCKET_PREFIX}?list-type=2&prefix={prefix}&delimiter=/").decode("utf-8")
        for found in re.findall(r"<Prefix>([^<]+/)</Prefix>", xml):
            scene_id = found.rstrip("/").rsplit("/", 1)[-1]
            if not SCENE_ID.match(scene_id):
                continue  # the listing echoes the request's own prefix first
            items.append({"scene_id": scene_id, **read_item(scene_id, get)})
    items.sort(key=lambda item: (date_of(item["scene_id"]), item["scene_id"]))
    return items


def bbox_covers(bbox, polygon: list) -> bool:
    """The production rule (common/vision_sites.search_newest_scene): the item's footprint box
    holds the whole site box, else another tile covers the site whole."""
    from common.vision_sites import site_bbox

    west, south, east, north = site_bbox(polygon)
    box = bbox or []
    return len(box) == 4 and box[0] <= west <= east <= box[2] and box[1] <= south <= north <= box[3]


def year_candidates(items: list[dict], polygon: list, max_cloud_cover: float) -> list[tuple]:
    """The items that pass the cloud filter and the footprint rule, each with the scene it is a
    duplicate of (None for the one kept on its date). On a date with several items the one kept
    is not Sentinel-2C when another platform flew, then the least tile no-data, then the lowest
    processing sequence."""
    passing = [
        item for item in items
        if item.get("cloud_cover") is not None and item["cloud_cover"] <= max_cloud_cover
        and bbox_covers(item.get("bbox"), polygon)
    ]  # fmt: skip
    by_date: dict[str, list[dict]] = {}
    for item in passing:
        by_date.setdefault(date_of(item["scene_id"]), []).append(item)
    out = []
    for date in sorted(by_date):
        group = sorted(
            by_date[date],
            key=lambda item: (
                item["scene_id"].startswith("S2C"),
                item.get("nodata_pct") if item.get("nodata_pct") is not None else 101.0,
                item["scene_id"],
            ),
        )
        out.append((group[0], None))
        out += [(item, group[0]["scene_id"]) for item in group[1:]]
    return out


def add_year_set(manifest: dict, site: str, tile: str, year: int, items: list[dict],
                 max_cloud_cover: float) -> list[str]:  # fmt: skip
    """Record the listing under `listings` and add every candidate not yet in the manifest as a
    `year` scene named `<site>-<date>` (a second item of that date gets a letter suffix); the
    names added."""
    polygon = manifest["sites"][site]["polygon"]
    listing = {
        "site": site,
        "tile": tile,
        "year": year,
        "listed_at": vision_runs.utc_now(),
        "max_cloud_cover": max_cloud_cover,
        "items": [
            {key: item.get(key) for key in ("scene_id", "captured_at", "cloud_cover", "nodata_pct", "bbox")}
            for item in items
        ],
    }
    manifest.setdefault("listings", {})[f"{tile}-{year}"] = listing
    present = {scene["scene_id"]: scene for scene in manifest["scenes"]}
    names = {scene["name"] for scene in manifest["scenes"]}
    added = []
    for item, duplicate_of in year_candidates(items, polygon, max_cloud_cover):
        scene_id = item["scene_id"]
        if scene_id in present:
            if duplicate_of and not present[scene_id].get("duplicate_of"):
                present[scene_id]["duplicate_of"] = duplicate_of
            continue
        base = f"{site}-{date_of(scene_id)}"
        name = base
        for suffix in "bcdefgh":
            if name not in names:
                break
            name = f"{base}{suffix}"
        scene = new_scene(name, site, scene_id, "year")
        scene["captured_at"] = item.get("captured_at")
        scene["item"] = {key: item.get(key) for key in ("cloud_cover", "nodata_pct", "bbox")}
        if duplicate_of:
            scene["duplicate_of"] = duplicate_of
        manifest["scenes"].append(scene)
        present[scene_id], added = scene, added + [name]
        names.add(name)
    return added


def year_set(
    manifest: dict,
    tile: str,
    year: int,
    max_cloud_cover: float,
    min_coverage: float = YEAR_SET_MIN_COVERAGE,
    coverage_by_id: dict | None = None,
) -> list[str]:
    """The names of the year set: the scenes of `tile` in `year`, whatever their role, that are
    nobody's duplicate, pass the cloud filter and the footprint rule, and were measured at
    `min_coverage` or more (the manifest's `site_coverage`, or `coverage_by_id` for a run that has
    just measured them)."""
    out = []
    for scene in manifest["scenes"]:
        if scene.get("tile") != tile or not (scene.get("captured_at") or "").startswith(str(year)):
            continue
        if scene.get("duplicate_of"):
            continue
        item = scene.get("item") or {}
        if item.get("cloud_cover") is None or item["cloud_cover"] > max_cloud_cover:
            continue
        if not bbox_covers(item.get("bbox"), manifest["sites"][scene["site"]]["polygon"]):
            continue
        coverage_ = (coverage_by_id or {}).get(scene["scene_id"], scene.get("site_coverage"))
        if coverage_ is not None and coverage_ >= min_coverage:
            out.append(scene["name"])
    return out


def year_sets(manifest: dict, max_cloud_cover: float) -> dict:
    """The manifest's `sets`: for each year listing, its year set by name (`year-2024`; prefixed
    with the site when two sites have a listing for one year), the rule it was taken by, and its
    scenes as `year_set` gives them, in time order. A scene's `year` role marks a candidate of the
    listing, not a member: a candidate measured under the coverage bar, or another's duplicate, is
    outside the set, and a named scene (`clear`, `season`) that passes is inside it, so a run
    selects by this."""
    listings = list((manifest.get("listings") or {}).values())
    years = [listing["year"] for listing in listings]
    by_name = {scene["name"]: scene for scene in manifest["scenes"]}
    sets = {}
    for listing in listings:
        name = f"year-{listing['year']}"
        if years.count(listing["year"]) > 1:
            name = f"{listing['site']}-{name}"
        sets[name] = {
            "site": listing["site"],
            "tile": listing["tile"],
            "year": listing["year"],
            "max_cloud_cover": max_cloud_cover,
            "min_coverage": YEAR_SET_MIN_COVERAGE,
            "scenes": sorted(year_set(manifest, listing["tile"], listing["year"], max_cloud_cover),
                             key=lambda name: by_name[name].get("captured_at") or ""),
        }
    return sets


# --- coverage ----------------------------------------------------------------------------------------


def rail_rule() -> tuple[float, float]:
    """The production rule's constants, from the adapter that applies them: the coverage floor
    under which a scene is never material, and the STAC cloud cover the search allows."""
    from common.adapters.rail_access import DEFAULTS

    return float(DEFAULTS["coverage_floor"]), float(DEFAULTS["max_cloud_cover"])


def decompose(scl, floor: float, unusable=None) -> dict:
    """E1 for the pixels inside the site: every class's share, the coverage as the worker defines
    it (the share outside the unusable classes, vision/masks.SCL_UNUSABLE), the largest loss, and
    the cause when the scene is under `floor` and one class holds DOMINANT_SHARE of the loss."""
    import numpy as np
    from vision import masks

    unusable = tuple(masks.SCL_UNUSABLE if unusable is None else unusable)
    values = np.asarray(scl).ravel().astype(np.int64)
    total = int(values.size)
    counts = np.bincount(values, minlength=max(SCL_NAMES) + 1) if total else np.zeros(max(SCL_NAMES) + 1, int)
    shares = {name: round(float(counts[code]) / total, 4) if total else 0.0
              for code, name in SCL_NAMES.items()}  # fmt: skip
    lost = int(sum(counts[code] for code in unusable if code < len(counts)))
    coverage = round(1.0 - lost / total, 4) if total else 0.0
    below = coverage < floor
    largest = None
    if lost:
        code = max((c for c in unusable if c < len(counts)), key=lambda c: counts[c])
        largest = {"class": SCL_NAMES[code], "share_of_loss": round(float(counts[code]) / lost, 4)}
    cause = largest["class"] if below and largest and largest["share_of_loss"] >= DOMINANT_SHARE else None
    return {
        "pixels": total,
        "shares": shares,
        "coverage": coverage,
        "floor": floor,
        "below_floor": below,
        "largest_loss": largest,
        "cause": cause,
        "explained": bool(below and cause),
    }


def scl_window(url: str, polygon: list, fetch, level=None):
    """The SCL (or any band's) pixels inside the site's box at `level` (the base image when not
    given): the array, the window and the site mask over it."""
    from vision import cog, geo, masks

    info, src = cog.read_info(url, fetch)
    level = level or info
    px = geo.lonlat_polygon_to_pixels(polygon, level.epsg, level.transform)
    window = geo.pixel_bounds(px, level.width, level.height, pad=0)
    if window is None:
        raise cog.CogError("the site does not overlap the scene")
    data, _ = cog.read_window(url, window, fetch, (level, src))
    col0, row0 = window[0], window[1]
    site = masks.polygon_mask(data.shape, [[[x - col0, y - row0] for x, y in px]])
    return data, window, site, info, src


def aot_over_site(scene_id: str, polygon: list, fetch) -> dict:
    """The mean aerosol optical thickness over the site, from the AOT band's coarsest overview
    (a mean needs no detail), over the surface pixels SCL shows clear at that resolution, else
    over every pixel with a value."""
    import numpy as np
    from vision import cog, geo

    url = aot_url(scene_id)
    info, src = cog.read_info(url, fetch)
    level = info.levels[-1] if info.levels else info
    px = geo.lonlat_polygon_to_pixels(polygon, level.epsg, level.transform)
    window = geo.pixel_bounds(px, level.width, level.height, pad=0)
    if window is None:
        raise cog.CogError("the site does not overlap the AOT band")
    aot, _ = cog.read_window(url, window, fetch, (level, src))
    aot = aot.astype(np.float64)
    scope, selected = "all", aot > 0
    scl_url = scene_assets(scene_id)["scl"]["url"]
    scl_info, scl_src = cog.read_info(scl_url, fetch)
    scl_level = cog.level_for_pixel_size(scl_info, level.pixel_size)
    if scl_level is not None:
        scl_px = geo.lonlat_polygon_to_pixels(polygon, scl_level.epsg, scl_level.transform)
        scl_window_ = geo.pixel_bounds(scl_px, scl_level.width, scl_level.height, pad=0)
        if scl_window_ is not None:
            scl, _ = cog.read_window(scl_url, scl_window_, fetch, (scl_level, scl_src))
            if scl.shape == aot.shape:
                clear = np.isin(scl, SCL_CLEAR) & (aot > 0)
                if clear.any():
                    scope, selected = "clear", clear
    mean = round(float(aot[selected].mean()) / AOT_SCALE, 4) if selected.any() else None
    return {"aot_mean": mean, "aot_scope": scope, "aot_pixel_m": round(level.pixel_size, 1),
            "aot_pixels": int(selected.sum())}  # fmt: skip


def scene_coverage(scene: dict, polygon: list, fetch, floor: float) -> dict:
    """E1 for one scene: the decomposition inside the site, the AOT mean, and what was read."""
    from vision import cog
    from vision.scene import CountingFetch

    counted = CountingFetch(fetch)
    try:
        scl, window, site, _info, _src = scl_window(scene["assets"]["scl"]["url"], polygon, counted)
        inside = scl[site > 0]
        row = {"ok": True, "error": None, **decompose(inside, floor), "window": list(window)}
        try:
            row.update(aot_over_site(scene["scene_id"], polygon, counted))
        except (cog.CogError, OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:200]
            row.update({"aot_mean": None, "aot_scope": None, "aot_error": error})
    except (cog.CogError, OSError, ValueError) as exc:
        row = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200], "coverage": None, "aot_mean": None}
    row["requests"], row["bytes"] = counted.requests, counted.bytes
    return row


def selection_counts(manifest: dict, coverage_by_id: dict[str, float | None], floor: float,
                     max_cloud_cover: float) -> dict:  # fmt: skip
    """E2 over every listing in the manifest: the items, those that pass the cloud filter, those
    of them whose footprint holds the site, and those of them measured under the floor (worker
    calls the search would make for nothing); `unmeasured` says how many of the picked items
    have no coverage yet. `year_set` counts the scenes in the sets."""
    counts = {"items": 0, "passed_filter": 0, "passed_bbox": 0, "below_floor_after_pick": 0, "unmeasured": 0,
              "year_set": 0}  # fmt: skip
    for listing in (manifest.get("listings") or {}).values():
        polygon = manifest["sites"][listing["site"]]["polygon"]
        for item in listing["items"]:
            counts["items"] += 1
            if item.get("cloud_cover") is None or item["cloud_cover"] > max_cloud_cover:
                continue
            counts["passed_filter"] += 1
            if not bbox_covers(item.get("bbox"), polygon):
                continue
            counts["passed_bbox"] += 1
            coverage = coverage_by_id.get(item["scene_id"])
            if coverage is None:
                counts["unmeasured"] += 1
            elif coverage < floor:
                counts["below_floor_after_pick"] += 1
        tile, year = listing["tile"], listing["year"]
        members = year_set(manifest, tile, year, max_cloud_cover, coverage_by_id=coverage_by_id)
        counts["year_set"] += len(members)
    return counts


def coverage(manifest: dict, scenes: list[dict], fetch) -> tuple[dict, dict]:
    """E1 for `scenes` and E2 for the manifest's listings: the per-scene rows and the summary."""
    floor, max_cloud_cover = rail_rule()
    results = {}
    for scene in scenes:
        polygon = manifest["sites"][scene["site"]]["polygon"]
        row = scene_coverage(scene, polygon, fetch, floor)
        results[scene["name"]] = {"scene_id": scene["scene_id"], "role": scene.get("role"), **row}
    coverage_by_id = {s["scene_id"]: s.get("site_coverage") for s in manifest["scenes"]}
    for row in results.values():
        if row.get("ok"):
            coverage_by_id[row["scene_id"]] = row["coverage"]
    summary = {
        "scenes": sum(1 for row in results.values() if row.get("ok")),
        "explained": sum(1 for row in results.values() if row.get("explained")),
        **selection_counts(manifest, coverage_by_id, floor, max_cloud_cover),
    }
    for name, row in results.items():
        if row.get("ok"):
            summary[f"{name}.coverage"] = row["coverage"]
            if row.get("aot_mean") is not None:
                summary[f"{name}.aot_mean"] = row["aot_mean"]
    return results, summary


def coverage_checks(scenes: list[dict], results: dict) -> list[dict]:
    """Each scene's `expected` against what was measured: `low_coverage` means under the floor."""
    checks = []
    for scene in scenes:
        expected = scene.get("expected") or {}
        row = results.get(scene["name"]) or {}
        if "low_coverage" in (expected.get("quality_flags") or []):
            got = bool(row.get("below_floor")) if row.get("ok") else None
            checks.append({"name": f"{scene['name']}:low_coverage", "expected": True, "got": got,
                           "pass": got is True})  # fmt: skip
    return checks


def write_coverage(manifest: dict, results: dict) -> int:
    """`site_coverage` and `aot_mean` into the manifest's scenes; how many were written."""
    by_name = {scene["name"]: scene for scene in manifest["scenes"]}
    written = 0
    for name, row in results.items():
        if row.get("ok") and name in by_name:
            by_name[name]["site_coverage"] = row["coverage"]
            by_name[name]["aot_mean"] = row.get("aot_mean")
            written += 1
    return written


def coverage_markdown(run: dict) -> str:
    env = run["environment"]
    lines = [
        f"**coverage** on {env['cpu']} ({env['machine']}), Python {env['python']}, where {env.get('where')}",
        "",
        "| scene | role | coverage | largest loss | cause | AOT mean | requests | bytes |",
        "|---|---|---:|---|---|---:|---:|---:|",
    ]
    for name, row in run["results"].items():
        if not row.get("ok"):
            lines.append(f"| {name} | {row.get('role')} | error: {row.get('error')} | | | | | |")
            continue
        largest = row.get("largest_loss") or {}
        loss = f"{largest.get('class')} {largest.get('share_of_loss')}" if largest else "none"
        coverage_ = f"{row['coverage']}{' (below floor)' if row['below_floor'] else ''}"
        lines.append(
            f"| {name} | {row.get('role')} | {coverage_} | {loss} | {row.get('cause') or ''} "
            f"| {row.get('aot_mean')} ({row.get('aot_scope')}) | {row['requests']} | {row['bytes']} |"
        )
    s = run["summary"]
    lines += [
        "",
        f"E2: {s['items']} items listed, {s['passed_filter']} pass the cloud filter, {s['passed_bbox']} of "
        f"them the footprint rule, {s['below_floor_after_pick']} of those are under the floor "
        f"({s['unmeasured']} unmeasured); the year set holds {s['year_set']} scenes",
    ]
    return "\n".join(lines)


# --- shared by the in-process subcommands ------------------------------------------------------------------

# The Region the imagery lives in: an in-process worker there reads `sentinel-cogs` as fast as the
# deployed one does.
IMAGERY_REGION = "us-west-2"
NETWORKS_DIR = Path(__file__).with_name("vision_benchmark_networks")
EVIDENCE_DIR = ROOT / "docs" / "hackathon" / "evidence"
REPLAY_DIR = EVIDENCE_DIR / "replay"
# How a refusal and a usage error end (a run that started and failed a check ends 1).
REFUSED = 2
# The replay's sequences, by name (vision_eval_driver.SEQUENCES holds them; a test holds the two
# equal, so --help can name them without importing the driver).
SEQUENCE_NAMES = ("sydney-winter", "sydney-demo", "sydney-c1", "synthetic-city")
# sydney-c1's network B when no file is given (vision_eval_driver.line_on_largest_desert).
NETWORK_B_DERIVED = "A with a five-station line through the previous scene's largest area beyond reach"


def in_imagery_region(identity=None) -> bool:
    """Whether this machine is an EC2 instance in IMAGERY_REGION (IMDS answers within 1 s, or not:
    off EC2 it is not)."""
    if identity is None:
        from vision_benchmark import imds_identity

        identity = imds_identity()
    return bool(identity) and identity.get("region") == IMAGERY_REGION


def in_process_refusal(cache, synthetic: bool, identity=None) -> str | None:
    """2.6.2's rule: an in-process worker under the production deadline is refused without `--cache`
    off an instance in IMAGERY_REGION, because a `look_again` would then re-read about 54 MB inside
    the agent's 85 s and fail closed for the harness's reasons (`no_time`, `deadline`), scored as
    the agent's. None when allowed (the synthetic city reads nothing)."""
    if synthetic or cache is not None or in_imagery_region(identity):
        return None
    return ("refused: --worker in-process with the production deadline needs --cache DIR (or an EC2 "
            f"instance in {IMAGERY_REGION}): the agent's look_again would re-read the scene inside its "
            "deadline and fail closed for the harness's reasons")  # fmt: skip


def network_file_for(site: str, given: Path | None) -> Path:
    """The network fixture a run reads: `--network`, else the site's committed one."""
    import vision_eval_driver as driver

    if given is not None:
        return Path(given)
    if site == "synthetic":
        return driver.SYNTHETIC_NETWORK_FILE
    return NETWORKS_DIR / f"{site}.json"


def head_scenes(scenes: list[dict], manifest: dict, head=None) -> dict[str, dict]:
    """Every asset's HEAD, refused (InputChanged) when a pinned ETag moved; {} for synthetic scenes."""
    urls = [asset["url"] for scene in scenes for asset in scene["assets"].values()
            if asset["url"].startswith(vision_runs.BUCKET_PREFIX)]  # fmt: skip
    if not urls:
        return {}
    heads = vision_runs.head_assets(urls, head)
    vision_runs.check_pinned(heads, vision_runs.pinned_etags(manifest))
    return heads


def scene_etags(scenes: list[dict], heads: dict[str, dict]) -> list[dict]:
    return [
        {"name": scene["name"], "scene_id": scene["scene_id"],
         "etag_by_asset": {band: (heads.get(asset["url"]) or {}).get("etag") if heads else None
                           for band, asset in scene["assets"].items()}}
        for scene in scenes
    ]  # fmt: skip


def worker_fetch(cache_dir: Path | None, heads: dict[str, dict], synthetic: bool, fetch=None):
    """What the in-process worker reads through: `fetch` when given, the synthetic city's files, the
    cache over the handler's own HTTPS reads, or (None) those reads alone."""
    import vision_eval_driver as driver

    if fetch is not None:
        return fetch, None
    if synthetic:
        return driver.synthetic_fetch(), None
    if cache_dir is None:
        return None, None
    import vision_worker_handler

    etags = {url: fields.get("etag") or "" for url, fields in heads.items()}
    cache = vision_runs.CachedFetch(cache_dir, etags, fetch=vision_worker_handler.http_fetch)
    return cache, cache


def worker_environ(backend: str, cool_sha: str | None, synthetic: bool) -> dict:
    """The in-process handler's environment for the run (it reads these at call time)."""
    import vision_eval_driver as driver

    environ = {"VISION_BACKEND": backend}
    if cool_sha:
        environ["COOL_BUILD_SHA256"] = cool_sha
    if synthetic:
        environ.update(driver.SYNTHETIC_ENVIRON)
    return environ


# --- replay (2.6.4) -------------------------------------------------------------------------------------


def replay_summary(ticks: list[dict]) -> dict:
    """The replay's `summary`: ticks replayed (`steps`), decisions, how many the agent was asked
    about, its look_again calls, its verdicts, and the ticks whose diff would write a Finding."""
    entries = [entry for tick in ticks for entry in tick.get("decisions") or []]
    triaged = [entry["triage"] for entry in entries if entry.get("triage")]
    look_again = sum(1 for t in triaged for step in t.get("steps") or [] if step.get("tool") == "look_again")
    return {
        "steps": len(ticks),
        "decisions": len(entries),
        "triaged": len(triaged),
        "look_again": look_again,
        "verdict_real": sum(1 for t in triaged if t.get("verdict") == "real" and t.get("stop") == "answered"),
        "verdict_artefact": sum(1 for t in triaged if t.get("verdict") != "real"),
        "finding": sum(1 for tick in ticks if tick.get("changed")),
    }


def agent_spend(ticks: list[dict], model_id: str | None) -> dict:
    """The agent's tokens over the run and their price from the built-in table (the Models registry
    is the deployment's; a replay prices with the table the code ships), USD and AUD, or null when
    the table has no price for the model."""
    from common.costing import USD_TO_AUD_RATE, call_cost_usd
    from common.model_pricing import default_model_entry

    entries = [entry["triage"] for tick in ticks for entry in tick.get("decisions") or []
               if entry.get("triage")]  # fmt: skip
    tokens = {"input_tokens": sum(int(t.get("input_tokens") or 0) for t in entries),
              "output_tokens": sum(int(t.get("output_tokens") or 0) for t in entries)}  # fmt: skip
    usd = call_cost_usd(tokens, default_model_entry(model_id)) if model_id and any(tokens.values()) else None
    return {**tokens, "cost_usd": round(usd, 6) if usd is not None else None,
            "cost_aud": round(usd * USD_TO_AUD_RATE, 6) if usd is not None else None,
            "price_source": "common/model_pricing.py's built-in table"}  # fmt: skip


def lambda_arn(value: str) -> bool:
    """Whether `--worker` is a Lambda function ARN (common/vision_client.region_of's rule), checked
    before anything runs, so a function name is a usage error rather than a traceback."""
    from common import vision_client

    try:
        vision_client.region_of(value)
    except ValueError:
        return False
    return True


def _dicts(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def recording_problem(recorded: dict) -> str | None:
    """The first field of a `--from` recording that a recorded replay reads and the file does not
    hold in the shape the replay wrote it (vision_runs.load checks the top level only); None when
    the recording is whole. A cut or hand-edited file is then refused by name, never a traceback."""
    results = recorded.get("results")
    ticks = results.get("ticks") if isinstance(results, dict) else None
    if not ticks or not _dicts(ticks):
        return "results.ticks"
    for n, tick in enumerate(ticks, start=1):
        try:
            datetime.fromisoformat(tick.get("at"))
        except (TypeError, ValueError):
            return f"results.ticks[{n}].at"
        for key in ("decisions", "measurements"):
            if not _dicts(tick.get(key) or []):
                return f"results.ticks[{n}].{key}"
        clock = tick.get("clock") or []
        if not isinstance(clock, list) or not all(
            isinstance(value, int | float) and not isinstance(value, bool) for value in clock
        ):
            return f"results.ticks[{n}].clock"
    inputs = recorded.get("inputs")
    if not isinstance(inputs, dict):
        return "inputs"
    for key in ("networks", "scenes"):
        if not _dicts(inputs.get(key) or []):
            return f"inputs.{key}"
    if not all("scene_id" in row for row in inputs.get("scenes") or []):
        return "inputs.scenes[].scene_id"
    agent = recorded.get("agent")
    responses = (agent.get("responses") or []) if isinstance(agent, dict) else None
    if not _dicts(responses) or not all(
        {"tick", "site_id", "turns"} <= set(row) and _dicts(row["turns"]) for row in responses
    ):
        return "agent.responses"
    return None


def _main_replay(args, manifest: dict) -> int:
    import vision_eval_driver as driver

    if args.sequence not in driver.SEQUENCES:
        print(f"no sequence {args.sequence!r}; the sequences are {sorted(driver.SEQUENCES)}", file=sys.stderr)
        return REFUSED
    sequence = driver.SEQUENCES[args.sequence]
    synthetic = sequence["site"] == "synthetic"
    agent_mode = args.agent
    deployed = args.worker != "in-process"
    usage = None
    if agent_mode == "recorded" and not args.from_run:
        usage = "--agent recorded needs --from <a replay run file>"
    elif agent_mode == "recorded" and deployed:
        usage = "--agent recorded re-measures in-process (--worker in-process): it needs no AWS"
    elif agent_mode == "bedrock" and not args.model_id:
        usage = "--agent bedrock needs --model-id <the topic's model or inference profile id>"
    elif args.backend == "cool" and not args.cool_sha:
        usage = "--backend cool needs --cool-sha <hex>: the pinned COOL build's fingerprint"
    elif deployed and not lambda_arn(args.worker):
        usage = (f"--worker takes the deployed worker's function ARN (arn:aws:lambda:<region>:<account>:"
                 f"function:<name>) or in-process, not {args.worker!r}")  # fmt: skip
    elif deployed and args.cache is not None:
        usage = "--cache answers the in-process worker's reads; the deployed worker reads the bucket itself"
    elif deployed and synthetic:
        usage = "the synthetic city exists only in this process: --worker in-process"
    if usage:
        print(usage, file=sys.stderr)
        return REFUSED
    host = None
    if agent_mode == "bedrock" and not deployed:
        # Recorded as the run's environment.host when it is what allowed the run (no cache).
        if args.cache is None and not synthetic:
            from vision_benchmark import imds_identity

            host = imds_identity()
        refusal = in_process_refusal(args.cache, synthetic, identity=host or {})
        if refusal:
            print(refusal, file=sys.stderr)
            return REFUSED

    recorded = None
    if agent_mode == "recorded":
        try:
            recorded = vision_runs.load(args.from_run)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return REFUSED
        was = recorded.get("params") if isinstance(recorded.get("params"), dict) else {}
        if recorded["kind"] != "eval-replay" or was.get("sequence") != args.sequence:
            print(f"refused: {args.from_run} is not a replay of {args.sequence}", file=sys.stderr)
            return REFUSED
        problem = recording_problem(recorded)
        if problem:
            print(f"refused: {args.from_run} is not a whole replay recording: {problem}", file=sys.stderr)
            return REFUSED

    started = vision_runs.utc_now()
    try:
        site, scenes = driver.sequence_scenes(args.sequence, manifest)
    except KeyError as exc:
        print(str(exc.args[0]), file=sys.stderr)
        return REFUSED
    limit = len(recorded["results"]["ticks"]) if recorded else (args.limit or len(scenes))
    scenes = scenes[:limit]
    network_path = network_file_for(sequence["site"], args.network)
    try:
        network_a = driver.load_network_file(network_path)
        network_b_file = driver.load_network_file(args.network_b) if args.network_b else None
    except (FileNotFoundError, ValueError) as exc:
        print(f"{exc} ({vision_runs.shown_path(network_path)})", file=sys.stderr)
        return REFUSED
    networks = [{"label": "A", "file": vision_runs.shown_path(network_path),
                 "sha256": vision_runs.sha256_file(network_path)}]  # fmt: skip
    if "B" in sequence["networks"][:limit]:
        given = args.network_b
        networks.append({
            "label": "B", "file": vision_runs.shown_path(given) if given else None,
            "sha256": vision_runs.sha256_file(given) if given else None,
            "derived": None if given else NETWORK_B_DERIVED,
        })  # fmt: skip
    if recorded:
        old = {row.get("label"): row.get("sha256") for row in recorded["inputs"].get("networks") or []}
        moved = [row["label"] for row in networks if row["sha256"] and old.get(row["label"]) != row["sha256"]]
        if moved:
            said = ", ".join(moved)
            print(f"input changed: network {said} is not the file the recording read", file=sys.stderr)
            return REFUSED

    try:
        heads = head_scenes(scenes, manifest)
    except (vision_runs.InputChanged, vision_runs.WrongHost) as exc:
        print(str(exc), file=sys.stderr)
        return REFUSED
    if recorded:
        then = {row["scene_id"]: row.get("etag_by_asset") for row in recorded["inputs"].get("scenes") or []}
        for row in scene_etags(scenes, heads):
            if then.get(row["scene_id"]) != row["etag_by_asset"]:
                print(f"input changed: {row['name']} is not the bytes the recording read", file=sys.stderr)
                return REFUSED

    fetch, cache = worker_fetch(args.cache, heads, synthetic)
    if deployed:
        from common import vision_client

        from vision_benchmark import deployed_code_sha256, lambda_client

        measure = driver.deployed_measure(args.worker)
        worker = {"kind": "deployed", "region": vision_client.region_of(args.worker),
                  "code_sha256": deployed_code_sha256(lambda_client(args.worker), args.worker)}  # fmt: skip
    else:
        measure = driver.in_process_measure(fetch, worker_environ(args.backend, args.cool_sha, synthetic))
        worker = {"kind": "in-process", "region": None, "code_sha256": vision_runs.worker_code_sha256()}

    if agent_mode == "bedrock":
        agent, clock = driver.BedrockAgent(args.model_id, region=args.region), (lambda n: driver.Clock())
    elif agent_mode == "recorded":
        told = recorded["agent"] or {}
        agent = driver.RecordedAgent(told.get("responses") or [], told.get("model_id"))
        readings = [tick.get("clock") or [] for tick in recorded["results"]["ticks"]]
        clock = lambda n: driver.Clock.replay(readings[n - 1])  # noqa: E731
    else:
        agent, clock = driver.ScriptedAgent(), (lambda n: driver.Clock.frozen())
    # A recorded replay runs under the recording's thresholds, whatever the sequence's are now.
    thresholds = args.thresholds or sequence["thresholds"]
    if recorded:
        thresholds = (recorded.get("params") or {}).get("thresholds") or thresholds
    topic = driver.topic_config(args.sequence, site["id"], site["polygon"], site_name=site["name"],
                                thresholds=thresholds, params=site.get("params"),
                                overrides={"backend": args.backend})  # fmt: skip
    run_driver = driver.Driver(topic, measure=measure, agent=agent, clock=clock)
    network_b = network_b_file or driver.line_on_largest_desert(network_a, run_driver.site_id)
    nows = None
    if recorded:
        nows = [datetime.fromisoformat(tick["at"]) for tick in recorded["results"]["ticks"]]
    try:
        specs = driver.tick_specs(args.sequence, scenes, network_a, network_b, nows=nows)
        ticks = run_driver.run(specs)
    except driver.ReplayRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    finally:
        figures = {tick["n"]: tick.pop("figure_key", None) for tick in run_driver.results}

    checks, parity_ = [], None
    if recorded:
        checks, parity_ = driver.compare_recorded(recorded["results"]["ticks"], ticks)

    from vision_benchmark import environment as bench_environment

    env = bench_environment(host=host)
    model_id = driver.recorded_model_id(agent.model_id)  # never an ARN: Converse alone is given that
    tag = "-".join(part for part in (args.sequence, agent_mode, args.tag) if part)
    run = vision_runs.new_run(
        "eval-replay", env["cpu"], args.backend, env, worker=worker, tag=tag, mode="replay", started=started,
        inputs={"manifest_sha256": None if synthetic else vision_runs.sha256_file(args.manifest),
                "cache": cache is not None, "cache_index_sha256": cache.index_sha256() if cache else None,
                "scenes": scene_etags(scenes, heads), "networks": networks},
        params={"sequence": args.sequence, "agent": agent_mode,
                "worker": "deployed" if deployed else "in-process", "backend": args.backend,
                "thresholds": thresholds, "limit": args.limit, "synthetic": synthetic,
                "model_id": model_id, "from": recorded["run_id"] if recorded else None,
                "tick_lag_hours": driver.TICK_LAG.total_seconds() / 3600.0,
                "scripted": {"tools": list(driver.SCRIPTED_TOOLS), "look_again": driver.SCRIPTED_LOOK_AGAIN,
                             "rule": driver.SCRIPTED_RULE}
                if agent_mode == "scripted" else None},
    )  # fmt: skip
    results = {
        "sequence": {"site": sequence["site"], "scenes": [scene["name"] for scene in scenes],
                     "networks": sequence["networks"][:limit], "news": list(sequence["news"])},
        "topic_id": run_driver.topic_id,
        "site_id": run_driver.site_id,
        "ticks": ticks,
        "ring": driver.ring_of(run_driver.state, run_driver.site_id),
        "excluded": list(driver.EXCLUDED),
        "parity": parity_,
    }  # fmt: skip
    vision_runs.finish(run, results, checks, replay_summary(ticks))
    run["agent"] = {"mode": agent_mode, "model_id": model_id,
                    "responses": [talk for tick in ticks for talk in tick.get("responses") or []],
                    **agent_spend(ticks, agent.model_id)}  # fmt: skip
    for tick in ticks:
        tick.pop("responses", None)  # kept once, under agent.responses
    if args.out:
        path = vision_runs.out_path(run, args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        for tick in ticks:
            png = run_driver.store.load_image(figures[tick["n"]]) if figures.get(tick["n"]) else None
            tick["figure"] = None
            if png and args.figures:
                figure = path.with_name(f"{path.stem}.{tick['n']}-{tick['scene']}.png")
                figure.write_bytes(png)
                tick["figure"] = figure.name
        vision_runs.write(run, path)
        print(f"wrote {path}")
    if args.trace:
        trace_path = Path(args.trace) if args.trace is not True else REPLAY_DIR / f"{args.sequence}.md"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(driver.render_markdown(run), encoding="utf-8")
        print(f"wrote {trace_path}")
    run_driver.store.close()
    s = run["summary"]
    print(f"replay {args.sequence} ({agent_mode}): {s['steps']} ticks, {s['decisions']} decisions, "
          f"{s['triaged']} sent to the agent ({s['look_again']} look_again), {s['verdict_real']} real, "
          f"{s['verdict_artefact']} artefact, {s['finding']} Finding(s) would be written")  # fmt: skip
    for check in run["checks"]:
        if not check["pass"]:
            print(f"FAIL {check['name']}: {check['got']}", file=sys.stderr)
    return 0 if all(check["pass"] for check in run["checks"]) else 1


# --- failures (2.5) ---------------------------------------------------------------------------------------

# The constructed failure catalogue: each case, what it is fed, what must happen, the tests that
# hold it (pytest node ids, never line numbers: re-anchored on 16 October from f974cf4's lines) and
# the real-data row that runs it (`real`: a key of FAILURE_RUNS, or None). Rows the agent
# evaluation holds (C5, C6-C9, C10) name it in `elsewhere`.
WORKER_TESTS = "lambdas/tests/test_vision_worker.py"
ADAPTER_TESTS = "lambdas/tests/test_rail_access_adapter.py"
CORE_TESTS = "lambdas/tests/test_vision_rail_core.py"
TRIAGE_TESTS = "lambdas/tests/test_vision_triage.py"
WORKER, ADAPTER, CORE, TRIAGE = WORKER_TESTS, ADAPTER_TESTS, CORE_TESTS, TRIAGE_TESTS
FAILURE_CASES = (
    {"case": "cloud", "input": "sydney-2024-01-05", "real": "cloud",
     "expected": "measured directly, the low_coverage flag at a coverage of about 0.145, and never chosen "
                 "by the search; a scene the search does pick under the floor (sydney-2024-02-28) is kept "
                 "out by the adapter: below the floor, no model call, never a first observation",
     "tests": ((ADAPTER, "test_low_coverage_is_never_material_and_never_baseline"),
               (ADAPTER, "test_the_first_observation_and_a_cloudy_first_scene_each_get_their_code"),
               (CORE, "test_low_coverage_and_no_network_are_flagged_not_crashes"))},
    {"case": "swath_nodata", "input": "sydney-2024-08-21a", "real": "nodata",
     "expected": "not chosen by the search (its footprint misses the site's box); measured directly, "
                 "the low_coverage flag at about 0.09",
     "tests": ((WORKER, "test_swir_nodata_is_folded_into_the_scl_as_unseen"),)},
    {"case": "smoke", "input": "sydney-2019-12-26-smoke", "real": "smoke",
     "expected": "a coverage of about 0.995, so the coverage gate passes; whether the numbers move is "
                 "the agent evaluation's C10", "tests": (), "elsewhere": "agent C10 (Q2b)"},
    {"case": "site_outside_scene", "input": "the Sydney polygon moved 2 degrees east, off the tile",
     "real": "outside", "expected": "site_outside_scene; refused, and not sent again for 6 h",
     "tests": ((WORKER, "test_site_outside_the_scene"),
               (ADAPTER, "test_a_scene_the_worker_refuses_for_what_it_is_is_not_sent_every_tick"))},
    {"case": "window_too_large", "input": "the Sydney polygon widened to about 76 km", "real": "window",
     "expected": "window_too_large after the header reads, before any tile",
     "tests": ((WORKER, "test_window_too_large"),
               (WORKER, "test_a_rail_site_over_the_rail_window_cap_is_refused_before_any_tile_is_read"),
               (ADAPTER, "test_a_scene_the_worker_refuses_for_what_it_is_is_not_sent_every_tick"),
               (ADAPTER, "test_a_refused_scene_is_sent_again_at_once_after_the_operator_edits_the_site"))},
    {"case": "graph_too_large", "input": "the dense synthetic mesh", "real": None,
     "expected": "the graph_too_large flag; measured, never material on its own",
     "tests": ((WORKER, "test_a_dense_mesh_through_the_worker_is_measured_with_graph_too_large"),
               (ADAPTER, "test_a_graph_too_large_is_recorded_and_never_material_on_its_own"))},
    {"case": "betweenness_sampled", "input": "a graph over BETWEENNESS_WORK", "real": None,
     "expected": "the betweenness_sampled flag; the notes say about",
     "tests": ((CORE, "test_a_large_graph_uses_sampled_betweenness_and_says_so"),),
     "elsewhere": "E6 on the real graph (Q3)"},
    {"case": "unreadable_scene", "input": "a missing key under the allowed prefix; a redirect",
     "real": "unreadable", "expected": "unreadable_scene",
     "tests": ((WORKER, "test_unreadable_scene"),
               (WORKER, "test_a_redirect_is_never_followed_and_the_scene_is_unreadable"),
               (WORKER, "test_a_redirect_is_refused_unread_whatever_its_body_or_location"))},
    {"case": "url_off_allowlist", "input": "an asset at another host", "real": "allowlist",
     "expected": "bad_request, before any read",
     "tests": ((WORKER, "test_bad_requests_are_refused_before_any_read"),
               (WORKER, "test_the_allowlist_takes_only_https_prefixes_with_a_path"),
               (WORKER, "test_the_worker_refuses_a_lookalike_host_when_a_prefix_lacks_its_slash"))},
    {"case": "wrong_build", "input": "a cool request to a stock worker; a cool worker without its pin",
     "real": "build", "expected": "backend_mismatch; not_cool",
     "tests": ((WORKER, "test_backend_mismatch"),
               (WORKER, "test_a_cool_worker_without_the_pinned_build_refuses"),
               (WORKER, "test_a_cool_worker_with_the_pinned_build_measures")),
     "elsewhere": "the Graviton session's controls (2.7.4)"},
    {"case": "overpass_fails", "input": "429, 504, a timeout, an oversize reply", "real": None,
     "expected": "the cached network serves and the refresh waits 6 h; a failing first fetch is an osm "
                 "error and nothing is measured",
     "tests": ((ADAPTER, "test_a_slow_failing_overpass_never_stops_a_city_with_a_cached_network"),
               (ADAPTER, "test_no_network_is_fetched_with_under_the_osm_reserve_left"),
               (ADAPTER, "test_a_stale_network_is_not_refreshed_with_under_the_osm_reserve_left"),
               (ADAPTER, "test_a_failed_first_fetch_is_tried_again_on_the_next_tick"))},
    {"case": "out_of_time", "input": "a budget under the reserves", "real": None,
     "expected": "deferred, and the site keeps its place",
     "tests": ((ADAPTER, "test_measuring_is_deferred_when_the_budget_is_nearly_spent"),
               (ADAPTER, "test_a_new_scene_waits_for_the_measuring_reserve_and_is_then_measured_with_a_"
                         "stale_network"),
               (ADAPTER, "test_a_site_past_the_budget_is_not_attempted_at_all"))},
    {"case": "agent_fails", "input": "a malformed answer, a Bedrock error, the deadline, the turn and "
                                     "tool budget, look_again refused for time, an unknown task, no model",
     "real": None, "expected": "artefact, with the stop code that says why",
     "tests": ((TRIAGE, "test_an_unusable_answer_is_an_artefact"),
               (TRIAGE, "test_a_bedrock_failure_is_an_artefact"),
               (TRIAGE, "test_out_of_time_is_an_artefact_before_any_model_call"),
               (TRIAGE, "test_near_the_deadline_web_context_gets_only_what_is_left_and_the_agent_then_"
                        "fails_closed"),
               (TRIAGE, "test_the_tool_budget_and_turn_limit_are_enforced_in_code"),
               (TRIAGE, "test_a_request_past_the_budget_is_refused_and_the_model_still_answers"),
               (TRIAGE, "test_look_again_is_refused_when_the_worker_might_not_answer_in_time"),
               (TRIAGE, "test_look_again_is_refused_under_the_tasks_minimum_wait"),
               (TRIAGE, "test_an_unknown_task_is_an_artefact_before_any_model_call"),
               (TRIAGE, "test_the_trail_says_how_it_stopped_at_every_exit"),
               (ADAPTER, "test_no_model_means_no_finding")),
     "elsewhere": "agent C6-C9 (Q2b)"},
    {"case": "injected_news", "input": "a headline that instructs", "real": None,
     "expected": "artefact: tool text is data, not instructions",
     "tests": ((TRIAGE, "test_the_ships_prompt_is_unchanged_and_the_rail_prompt_says_tool_text_is_data"),
               (TRIAGE, "test_web_context_results_are_cut_and_cleaned")),
     "elsewhere": "agent C5 (Q2b)"},
)  # fmt: skip
# The real-data rows that read no pixel (refused before any read): they run anywhere, CI included.
# The others read the bucket, these scenes, and run with --cache (the unreadable row reads a key
# that is not there, past the cache).
NO_READ_RUNS = ("allowlist", "build")
FAILURE_SCENES = {
    "cloud": ("sydney-2024-01-05", "sydney-2024-02-28"), "nodata": ("sydney-2024-08-21a",),
    "smoke": ("sydney-2019-12-26-smoke",), "outside": ("sydney-2024-08-26",),
    "window": ("sydney-2024-08-26",), "unreadable": (),
}  # fmt: skip
# A clear-enough scene the production search picks that is still under the coverage floor (the
# coverage run measured 0.489; E2 counts five such): the cloud row's gate runs on it.
CLOUD_PICKED = "sydney-2024-02-28"
# A measured coverage agrees with the coverage run's (the manifest's site_coverage) within this.
COVERAGE_AGREES = 0.01
# A polygon moved off the tile (Sydney's tile spans about 1.15 degrees of longitude) ...
OUTSIDE_SHIFT_DEG = 2.0
# ... and widened past the rail task's 3,584 px window (71.7 km at 20 m): about 76 km at -33.9.
WINDOW_EAST_DEG = 0.12


class CollectionFailed(Exception):
    """pytest could not collect the catalogue's test files here (not installed, or a file failed to
    import): the check cannot be made, which is not the same as the tests being missing."""


# pytest's exit statuses that mean it collected: 0 (tests found) and 5 (none found).
COLLECTED_STATUSES = (0, 5)


def collected_tests(files: list[str], run=None) -> set[str]:
    """The node ids `pytest --collect-only -q` lists for `files`, rooted at the repository (its own
    pyproject.toml, as `pytest lambdas/ scripts/` runs: a node id is then `lambdas/tests/...`).
    CollectionFailed, quoting the end of pytest's output, when pytest did not collect."""
    import subprocess

    run = run or subprocess.run
    argv = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
            "--rootdir", str(ROOT), "-c", str(ROOT / "pyproject.toml"), *files]  # fmt: skip
    done = run(argv, cwd=ROOT, capture_output=True, text=True, timeout=600)
    if done.returncode not in COLLECTED_STATUSES:
        output = f"{done.stdout or ''}\n{done.stderr or ''}".splitlines()
        said = [line.strip() for line in output if line.strip()]
        raise CollectionFailed(f"pytest exited {done.returncode}: {' / '.join(said[-3:]) or 'no output'}")
    return {line.strip() for line in done.stdout.splitlines() if "::" in line}


def node_ids(case: dict) -> list[str]:
    """A catalogue case's tests as pytest node ids (`<file>::<name>`)."""
    return [f"{file}::{name}" for file, name in case.get("tests") or ()]


def missing_tests(cases, collected: set[str]) -> list[str]:
    """Every catalogue node id that `collected` does not hold (a parametrised test counts by its
    base id: `name[...]`)."""
    missing = []
    for case in cases:
        for node in node_ids(case):
            if node not in collected and not any(found.startswith(node + "[") for found in collected):
                missing.append(node)
    return missing


def _scene_request(scene: dict, polygon: list, network: dict, *, site_id: str = "eval-failures",
                   backend: str = "opencv", assets: dict | None = None) -> dict:  # fmt: skip
    from common import vision_contract

    return {
        "version": vision_contract.VERSION, "task": "rail_access", "backend": backend,
        "site": {"id": site_id, "polygon": polygon},
        "scene": {"id": scene["scene_id"], "captured_at": scene.get("captured_at") or "",
                  "assets": assets or {band: asset["url"] for band, asset in scene["assets"].items()}},
        "params": {}, "coverage_floor": rail_rule()[0], "image": False, "network": network,
    }  # fmt: skip


def _handled(request: dict, fetch, environ: dict) -> tuple[dict, list]:
    """The worker handler's reply in this process, and every range it read."""
    import vision_worker_handler

    import vision_eval_driver as driver

    reads = []

    def logged(url, start, end):
        reads.append((url, start, end))
        return fetch(url, start, end)

    with driver.environment(environ), contextlib.redirect_stdout(io.StringIO()):
        reply = vision_worker_handler.lambda_handler(request, None, fetch=logged)
    return reply, reads


def failure_row(name: str, manifest: dict, network: dict, fetch) -> dict:
    """One real-data row: what happened, against what was expected (`got` and `pass`)."""
    from vision import cog

    polygon = manifest["sites"]["sydney"]["polygon"]
    by_name = {scene["name"]: scene for scene in manifest["scenes"]}
    stock = {"VISION_BACKEND": "opencv", "COOL_BUILD_SHA256": None}
    if name == "allowlist":
        scene = by_name["sydney-2024-08-26"]
        assets = {band: asset["url"] for band, asset in scene["assets"].items()}
        assets["red"] = "https://example.com/sentinel-s2-l2a-cogs/B04.tif"
        reply, reads = _handled(_scene_request(scene, polygon, network, assets=assets), fetch, stock)
        got = {"code": reply.get("error"), "reads": len(reads)}
        ok = got == {"code": "bad_request", "reads": 0} and "allowed location" in (reply.get("detail") or "")
        return {"got": got, "pass": ok}
    if name == "build":
        scene = by_name["sydney-2024-08-26"]
        mismatch, reads_a = _handled(_scene_request(scene, polygon, network, backend="cool"), fetch, stock)
        unpinned, reads_b = _handled(_scene_request(scene, polygon, network, backend="cool"), fetch,
                                     {"VISION_BACKEND": "cool", "COOL_BUILD_SHA256": None})  # fmt: skip
        got = {"cool_to_stock": mismatch.get("error"), "cool_without_pin": unpinned.get("error"),
               "reads": len(reads_a) + len(reads_b)}  # fmt: skip
        expected = {"cool_to_stock": "backend_mismatch", "cool_without_pin": "not_cool", "reads": 0}
        return {"got": got, "pass": got == expected}
    if name == "cloud":
        return _cloud_row(manifest, network, fetch)
    if name == "nodata":
        scene = by_name["sydney-2024-08-21a"]
        chosen = chosen_by_the_search(scene, polygon)
        reply, _ = _handled(_scene_request(scene, polygon, network), fetch, stock)
        metrics = reply.get("metrics") or {}
        flags, coverage_ = metrics.get("quality_flags") or [], metrics.get("coverage") or 0
        got = {"chosen_by_the_search": chosen, "code": reply.get("error"),
               "coverage": metrics.get("coverage"), "quality_flags": flags}  # fmt: skip
        ok = (not chosen and reply.get("ok") is True and "low_coverage" in flags
              and abs(coverage_ - (scene.get("site_coverage") or 0)) <= COVERAGE_AGREES)  # fmt: skip
        return {"got": got, "pass": ok}
    if name == "smoke":
        scene = by_name["sydney-2019-12-26-smoke"]
        reply, _ = _handled(_scene_request(scene, polygon, network), fetch, stock)
        metrics = reply.get("metrics") or {}
        got = {"code": reply.get("error"), "coverage": metrics.get("coverage"),
               "served_share": metrics.get("served_share"), "aot_mean": scene.get("aot_mean")}  # fmt: skip
        passes = reply.get("ok") is True and (metrics.get("coverage") or 0) >= rail_rule()[0]
        return {"got": got, "pass": passes}
    if name == "outside":
        scene = by_name["sydney-2024-08-26"]
        moved = [[lon + OUTSIDE_SHIFT_DEG, lat] for lon, lat in polygon]
        reply, reads = _handled(_scene_request(scene, moved, network), fetch, stock)
        got = {"code": reply.get("error"), "reads": len(reads)}
        return {"got": got, "pass": reply.get("error") == "site_outside_scene"}
    if name == "window":
        scene = by_name["sydney-2024-08-26"]
        east = max(lon for lon, _ in polygon)
        wide = [[lon + WINDOW_EAST_DEG if lon == east else lon, lat] for lon, lat in polygon]
        reply, reads = _handled(_scene_request(scene, wide, network), fetch, stock)
        tiles = [read for read in reads if read[1] != 0]
        got = {"code": reply.get("error"), "detail": reply.get("detail"), "reads": len(reads),
               "tile_reads": len(tiles)}  # fmt: skip
        return {"got": got, "pass": reply.get("error") == "window_too_large" and not tiles}
    if name == "unreadable":
        scene = by_name["sydney-2024-08-26"]
        folder = scene_folder(scene["scene_id"])
        assets = {band: asset["url"] for band, asset in scene["assets"].items()}
        assets["swir16"] = f"{folder}/B11-missing.tif"
        import vision_worker_handler

        request = _scene_request(scene, polygon, network, assets=assets)
        reply, _ = _handled(request, vision_worker_handler.http_fetch, stock)
        return {"got": {"code": reply.get("error")}, "pass": reply.get("error") == "unreadable_scene"}
    raise cog.CogError(f"no real row {name!r}")


def chosen_by_the_search(scene: dict, polygon: list) -> bool:
    """Whether the production search would pick `scene` for the site: its STAC cloud cover passes
    the query's filter (the adapter's max_cloud_cover; Earth Search applies it, so it is applied
    here), and the adapter's own search keeps the item (vision_sites.search_newest_scene: its
    footprint holds the site's box, every band present)."""
    from common.adapters import rail_access
    from common.vision_sites import search_newest_scene

    import vision_eval_driver as driver

    _floor, max_cloud_cover = rail_rule()
    cloud = (scene.get("item") or {}).get("cloud_cover")
    if cloud is None or cloud > max_cloud_cover:
        return False
    stac = driver.ScriptedStac()
    stac.item = driver.stac_item(scene)
    config = {"lookback_days": rail_access.DEFAULTS["lookback_days"], "max_cloud_cover": max_cloud_cover}
    found = search_newest_scene({"polygon": polygon}, config, driver.tick_time(scene["captured_at"]),
                                http_post=stac, assets=rail_access.ASSETS_RAIL)  # fmt: skip
    return found is not None


def _cloud_row(manifest: dict, network: dict, fetch) -> dict:
    """The cloudy scene measured directly (the worker's flag and its coverage, which the coverage
    run measured too), never chosen by the search; and the adapter's gate on real data: the scene
    of CLOUD_PICKED, which the search does pick under the floor, through the real adapter for one
    tick: below the floor, no model call, never a first observation."""
    import vision_eval_driver as driver

    by_name = {scene["name"]: scene for scene in manifest["scenes"]}
    site = manifest["sites"]["sydney"]
    cloudy, picked = by_name["sydney-2024-01-05"], by_name[CLOUD_PICKED]
    reply, _ = _handled(_scene_request(cloudy, site["polygon"], network), fetch,
                        {"VISION_BACKEND": "opencv", "COOL_BUILD_SHA256": None})  # fmt: skip
    metrics = reply.get("metrics") or {}
    flags, coverage_ = metrics.get("quality_flags") or [], metrics.get("coverage")
    called = []

    class NoModel(driver.ScriptedAgent):
        def converse_for(self, tick, site_id):
            called.append(tick)
            return super().converse_for(tick, site_id)

    topic = driver.topic_config("failures-cloud", "sydney", site["polygon"], site_name="Sydney",
                                params=site.get("params"))  # fmt: skip
    measure = driver.in_process_measure(fetch, {"VISION_BACKEND": "opencv"})
    run_driver = driver.Driver(topic, measure=measure, agent=NoModel(), clock=lambda n: driver.Clock.frozen())
    now = driver.tick_time(picked["captured_at"])
    tick = run_driver.run([driver.TickSpec(scene=picked, now=now, network=network, label="A")])[0]
    run_driver.store.close()
    latest = ((run_driver.state["sites"][run_driver.site_id]).get("history") or [{}])[-1]
    got = {
        "measured": {"code": reply.get("error"), "coverage": coverage_, "quality_flags": flags},
        "chosen_by_the_search": chosen_by_the_search(cloudy, site["polygon"]),
        "gate": {"scene": CLOUD_PICKED, "picked_by_the_search": chosen_by_the_search(picked, site["polygon"]),
                 "codes": tick["codes"], "coverage": latest.get("coverage") if tick["measured"] else None,
                 "model_calls": len(called), "first_observed_at": run_driver.state.get("first_observed_at"),
                 "changed": tick["changed"]},
    }  # fmt: skip
    measured_ok = ("low_coverage" in flags and coverage_ is not None
                   and abs(coverage_ - (cloudy.get("site_coverage") or 0)) <= COVERAGE_AGREES)  # fmt: skip
    gate = got["gate"]
    gate_ok = (gate["picked_by_the_search"] and gate["codes"] == ["below_floor"] and not called
               and gate["first_observed_at"] is None and not gate["changed"])  # fmt: skip
    return {"got": got, "pass": bool(measured_ok and not got["chosen_by_the_search"] and gate_ok)}


def _main_failures(args, manifest: dict) -> int:
    import vision_eval_driver as driver

    started = vision_runs.utc_now()
    cases = [case for case in FAILURE_CASES if not args.only or case["case"] in args.only]
    if args.only and len(cases) != len(set(args.only)):
        known = [case["case"] for case in FAILURE_CASES]
        print(f"no case named {sorted(set(args.only) - set(known))}; the cases are {known}", file=sys.stderr)
        return REFUSED
    collected = None
    if args.collect:
        files = sorted({file for case in cases for file, _name in case.get("tests") or ()})
        try:
            collected = collected_tests(files)
        except CollectionFailed as exc:
            print(f"refused: pytest cannot collect the catalogue's tests here ({exc}): install "
                  "lambdas/requirements-dev.txt (pytest, moto), or pass --no-collect (CI holds that check)",
                  file=sys.stderr)  # fmt: skip
            return REFUSED
    network_path = network_file_for("sydney", args.network)
    if args.network is not None and not network_path.is_file():
        # An explicit file that is not there is refused, as replay and access-baseline refuse it; only
        # the default, uncommitted fixture falls back to an empty network.
        print(f"{NETWORK_FILE_MISSING} ({vision_runs.shown_path(network_path)})", file=sys.stderr)
        return REFUSED
    if network_path.is_file():
        network = driver.load_network_file(network_path)
        network_note = {"file": vision_runs.shown_path(network_path),
                        "sha256": vision_runs.sha256_file(network_path)}  # fmt: skip
    else:
        # The rows' expectations are about the scene, never the network: with no fixture the requests
        # carry an empty one (a valid request; served share is then 0, and no row reads it).
        network = {"ways": [], "stations": [], "pois": [], "fetched_at": None}
        network_note = {"file": None, "sha256": None, "note": "no fixture: an empty network"}
    reading = [case["real"] for case in cases if case.get("real") and case["real"] not in NO_READ_RUNS]
    heads, cache, fetch, scenes_read = {}, None, None, []
    if args.cache is not None and reading:
        by_name = {scene["name"]: scene for scene in manifest["scenes"]}
        names = [name for real in reading for name in FAILURE_SCENES.get(real, ())]
        scenes_read = [by_name[name] for name in dict.fromkeys(names)]
        try:
            heads = head_scenes(scenes_read, manifest)
        except (vision_runs.InputChanged, vision_runs.WrongHost) as exc:
            print(str(exc), file=sys.stderr)
            return REFUSED
        fetch, cache = worker_fetch(args.cache, heads, synthetic=False)
    rows = {}
    for case in cases:
        row = {"input": case["input"], "expected": case["expected"], "tests": node_ids(case),
               "elsewhere": case.get("elsewhere"), "real": case.get("real"), "ran": False, "got": None,
               "pass": None}  # fmt: skip
        if collected is not None:
            row["tests_missing"] = missing_tests([case], collected)
        real = case.get("real")
        if real and (real in NO_READ_RUNS or fetch is not None):
            reads = fetch if fetch is not None else _no_reads
            row.update(failure_row(real, manifest, network, reads), ran=True)
        elif real:
            row["why_not"] = "reads the bucket: run with --cache DIR (the operator's run)"
        rows[case["case"]] = row
    checks = []
    if collected is not None:
        missing = [node for row in rows.values() for node in row.get("tests_missing") or []]
        checks.append({"name": "every test the catalogue names is collected", "expected": [], "got": missing,
                       "pass": not missing})  # fmt: skip
    for name, row in rows.items():
        if row["ran"]:
            checks.append({"name": f"{name}: as expected", "expected": row["expected"], "got": row["got"],
                           "pass": bool(row["pass"])})  # fmt: skip
    summary = {"rows": len(rows), "ran": sum(1 for row in rows.values() if row["ran"]),
               "as_expected": sum(1 for row in rows.values() if row["ran"] and row["pass"]),
               "tests_named": sum(len(row["tests"]) for row in rows.values()),
               "tests_missing": sum(len(row.get("tests_missing") or []) for row in rows.values()),
               }  # fmt: skip
    from vision_benchmark import environment as bench_environment

    env = bench_environment()
    run = vision_runs.new_run(
        "eval-failures", env["cpu"], "opencv", env, tag=args.tag, mode="failures", started=started,
        worker={"kind": "in-process", "region": None, "code_sha256": vision_runs.worker_code_sha256()},
        inputs={"manifest_sha256": vision_runs.sha256_file(args.manifest), "cache": cache is not None,
                "cache_index_sha256": cache.index_sha256() if cache else None,
                "scenes": scene_etags(scenes_read, heads), "networks": [network_note]},
        params={"only": args.only, "collect": args.collect, "cache": cache is not None},
    )  # fmt: skip
    vision_runs.finish(run, rows, checks, summary)
    for name, row in rows.items():
        state = "not run" if not row["ran"] else ("as expected" if row["pass"] else "NOT AS EXPECTED")
        missing = f", {len(row['tests_missing'])} test(s) missing" if row.get("tests_missing") else ""
        print(f"{name}: {state}{missing}")
    print(f"{summary['as_expected']} of {summary['ran']} real rows as expected; "
          f"{summary['tests_named']} tests named, {summary['tests_missing']} missing")  # fmt: skip
    if args.out:
        print(f"wrote {vision_runs.write(run, args.out)}")
    return 0 if all(check["pass"] for check in checks) else 1


def _no_reads(url, start, end):
    raise AssertionError(f"a row that reads nothing read {url}")


# --- access-baseline (E5) ----------------------------------------------------------------------------------

E5_REPEATS = 5
E5_SCENE = "sydney-2024-08-26"


def served_masks(built, stations_px, reach_px: float) -> tuple[dict, dict]:
    """E5's three served masks (built-up pixels within reach of a station) and each one's median
    time over E5_REPEATS: production's distance transform (vision/access.distance_to_stations:
    `DIST_L2` with the 5x5 mask, labels and all), the precise one (`DIST_MASK_PRECISE`), and a
    straight-line buffer (the union of `cv2.circle` discs of `reach_px` at each station: the simple
    form of SDG 11.2.1's reach to rail)."""
    import statistics
    import time

    import cv2
    import numpy as np
    from vision import access

    built = np.asarray(built) > 0
    raster = access.station_raster(built.shape, stations_px)
    not_station = np.where(raster > 0, 0, 1).astype(np.uint8)
    radius = int(round(reach_px))
    centres = [(int(round(s[0])), int(round(s[1]))) for s in stations_px if s is not None]

    def production():
        return access.distance_to_stations(raster)[0] <= reach_px

    def precise():
        if not raster.any():
            return np.zeros(built.shape, bool)
        return cv2.distanceTransform(not_station, cv2.DIST_L2, cv2.DIST_MASK_PRECISE) <= reach_px

    def buffer():
        discs = np.zeros(built.shape, np.uint8)
        for centre in centres:
            cv2.circle(discs, centre, radius, 255, -1)
        return discs > 0

    masks, times = {}, {}
    for name, fn in (("production", production), ("precise", precise), ("buffer", buffer)):
        samples = []
        for _ in range(E5_REPEATS):
            started = time.perf_counter()
            within = fn()
            samples.append((time.perf_counter() - started) * 1000.0)
        masks[name] = built & within
        times[name] = round(statistics.median(samples), 3)
    return masks, times


def access_baseline(analysis, pixel_size_m: float, reach_m: float) -> tuple[dict, dict]:
    """E5 on one measured scene (`analysis`, vision.rail_analyse's): each method's served share, the
    difference from production's, the IoU of its served mask with production's, and the times."""
    import numpy as np

    built = analysis.layers["built"] > 0
    reach_px = reach_m / pixel_size_m
    masks, times = served_masks(built, analysis.layers["stations_px"], reach_px)
    built_px = int(np.count_nonzero(built))
    pixel_km2 = (pixel_size_m / 1000.0) ** 2

    def share(mask):
        return round(int(np.count_nonzero(mask)) / built_px, 6) if built_px else 0.0

    def iou(a, b):
        union = int(np.count_nonzero(a | b))
        return round(int(np.count_nonzero(a & b)) / union, 6) if union else 1.0

    base = masks["production"]
    methods = {name: {"served_share": share(mask),
                      "served_km2": round(int(np.count_nonzero(mask)) * pixel_km2, 4),
                      "iou_vs_production": iou(mask, base), "ms": times[name]}
               for name, mask in masks.items()}  # fmt: skip
    results = {"built_up_px": built_px, "reach_px": reach_px, "pixel_size_m": pixel_size_m,
               "stations": sum(1 for s in analysis.layers["stations_px"] if s is not None),
               "worker_served_share": analysis.metrics.get("served_share"), "methods": methods}  # fmt: skip
    production = methods["production"]["served_share"]
    summary = {
        "delta_vs_precise": round(methods["precise"]["served_share"] - production, 6),
        "delta_vs_buffer": round(methods["buffer"]["served_share"] - production, 6),
        "iou_vs_precise": methods["precise"]["iou_vs_production"],
        "iou_vs_buffer": methods["buffer"]["iou_vs_production"],
        "ms_production": times["production"], "ms_precise": times["precise"], "ms_buffer": times["buffer"],
    }  # fmt: skip
    return results, summary


def scene_analysis(scene: dict, polygon: list, network: dict, fetch, reach_m: float):
    """The worker's own read and analysis of `scene` in this process (vision_worker_handler's rail
    path: the bands on swir16's 20 m grid, no-data folded into SCL), returning the analysis with its
    layers, which the reply never carries."""
    import vision_worker_handler
    from vision import scene as scene_module
    from vision.rail_analyse import RailParams, analyse_rail_access

    assets = {band: asset["url"] for band, asset in scene["assets"].items()}
    site = scene_module.read_site(assets, polygon, fetch, reference=vision_worker_handler.RAIL_REFERENCE_BAND,
                                  max_window_px=vision_worker_handler.RAIL_MAX_WINDOW_PX)  # fmt: skip
    bands = dict(site.bands)
    reference = vision_worker_handler.RAIL_REFERENCE_BAND
    nodata = site.nodata.get(reference)
    if nodata is not None:
        missing = bands[reference] == nodata
        if missing.any():
            scl = bands["scl"].copy()
            scl[missing] = 0
            bands["scl"] = scl
    analysis = analyse_rail_access(bands, [site.polygon_px], site.pixel_size_m, site.transform, site.epsg,
                                   network, RailParams(reach_m=reach_m), rail_rule()[0])  # fmt: skip
    return analysis, site.pixel_size_m


def _main_access_baseline(args, manifest: dict) -> int:
    import vision_eval_driver as driver

    started = vision_runs.utc_now()
    synthetic = args.scene.startswith("synthetic-")
    if synthetic:
        entry = next((e for e in driver.SYNTHETIC_SCENES if e["name"] == args.scene), None)
        if entry is None:
            print(f"no synthetic scene {args.scene!r}", file=sys.stderr)
            return REFUSED
        scene, polygon = driver.synthetic_scene(entry), driver.synthetic_polygon()
    else:
        by_name = {s["name"]: s for s in manifest["scenes"]}
        if args.scene not in by_name:
            print(f"no scene named {args.scene!r} in the manifest", file=sys.stderr)
            return REFUSED
        scene = by_name[args.scene]
        site = manifest["sites"][scene["site"]]
        polygon = site["polygon"]
    network_path = network_file_for("synthetic" if synthetic else scene["site"], args.network)
    try:
        network = driver.load_network_file(network_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"{exc} ({vision_runs.shown_path(network_path)})", file=sys.stderr)
        return REFUSED
    try:
        heads = head_scenes([scene], manifest)
    except (vision_runs.InputChanged, vision_runs.WrongHost) as exc:
        print(str(exc), file=sys.stderr)
        return REFUSED
    fetch, cache = worker_fetch(args.cache, heads, synthetic)
    if fetch is None:
        import vision_worker_handler

        fetch = vision_worker_handler.http_fetch
    reach_m = float(args.reach_m)
    analysis, pixel_size_m = scene_analysis(scene, polygon, network, fetch, reach_m)
    results, summary = access_baseline(analysis, pixel_size_m, reach_m)
    production = results["methods"]["production"]["served_share"]
    worker_share = results["worker_served_share"]
    agrees = worker_share is not None and abs(round(production, 4) - worker_share) <= 1e-9
    checks = [{"name": "production's served share is the worker's", "expected": worker_share,
               "got": round(production, 4), "pass": agrees}]  # fmt: skip
    from vision_benchmark import environment as bench_environment

    env = bench_environment()
    run = vision_runs.new_run(
        "eval-access-baseline", env["cpu"], "opencv", env, tag=args.tag, mode="access-baseline",
        started=started,
        worker={"kind": "in-process", "region": None, "code_sha256": vision_runs.worker_code_sha256()},
        inputs={"manifest_sha256": None if synthetic else vision_runs.sha256_file(args.manifest),
                "cache": cache is not None, "cache_index_sha256": cache.index_sha256() if cache else None,
                "scenes": scene_etags([scene], heads),
                "networks": [{"file": vision_runs.shown_path(network_path),
                              "sha256": vision_runs.sha256_file(network_path)}]},
        params={"scene": args.scene, "reach_m": reach_m, "repeats": E5_REPEATS, "synthetic": synthetic,
                "cache": cache is not None},
    )  # fmt: skip
    vision_runs.finish(run, results, checks, summary)
    m = results["methods"]
    print("| method | served share | vs production | IoU with production | ms |\n|---|---:|---:|---:|---:|")
    for name in ("production", "precise", "buffer"):
        delta = round(m[name]["served_share"] - m["production"]["served_share"], 6)
        row = m[name]
        print(f"| {name} | {row['served_share']} | {delta:+} | {row['iou_vs_production']} | {row['ms']} |")
    if args.out:
        print(f"wrote {vision_runs.write(run, args.out)}")
    return 0 if all(check["pass"] for check in checks) else 1


# --- index (2.9) ------------------------------------------------------------------------------------------

INDEX_SCHEMA = "bloggerbear.vision-index/1"
# A run listed here was measured on code that has changed since in a way that matters to it; the
# reason says why it is still cited ({run_id: reason}). Optional; the index refuses a run id that
# is not in runs/.
OLDER_CODE_FILE = "older-code.json"
# The operator's labels of live decisions (O9): the export's template, filled.
LABEL_COLUMNS = ("decision", "verdict", "figure", "trace", "label", "reason")
LABELS = ("agree", "disagree")
LABEL_REASONS = ("looks_real", "network_edit", "cloud_or_haze", "season", "other_artefact", "unsure")
_DECISION = re.compile(r"^[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_.:-]{1,128}$")
# A kind's table shows at most this many of its summary's fixed codes as columns.
MAX_SUMMARY_COLUMNS = 8
KIND_WORDS = {
    "bench-kernels": "each OpenCV operation the worker spends its time on, on synthetic images",
    "bench-scenes": "the worker handler in-process on the pinned scenes: stage timings, the rail metrics",
    "bench-worker": "the deployed worker, end to end, with the caller's round trip",
    "bench-compare": "two runs of one machine and the same inputs: the speed-up and the metrics' parity",
    "bench-controls": "the COOL guard's negative controls",
    "eval-coverage": "E1 and E2: each pinned scene's coverage of the site, and the year's selection counts",
    "eval-replay": "2.6.4: the real adapter over a pinned sequence, tick by tick, with the agent",
    "eval-failures": "2.5: the failure catalogue's real-data rows and the tests that hold the rest",
    "eval-access-baseline": "E5: the production distance transform against the precise one and a buffer",
    "eval-agent": "2.6.1-2.6.3: the agent's cases, per class, against numeric-only and always-artefact",
    "eval-counterfactual": "2.6.6: only OpenCV's output changed, at the agent and at the coverage gate",
    "eval-stability": "E3: the year set through the production rule on one network: false alarms, spreads",
}


def index_rows(runs_dir: Path) -> list[dict]:
    """One row per run file in `runs_dir`, each read through vision_runs.load (a malformed file or
    an unknown kind is refused: ValueError naming the file), sorted by kind, newest first."""
    rows = []
    for path in sorted(Path(runs_dir).glob("*.json")):
        run = vision_runs.load(path)
        if path.stem != run["run_id"]:
            raise ValueError(f"{path}: named {path.stem!r} but its run_id is {run['run_id']!r}")
        env = run.get("environment") or {}
        build = ((env.get("opencv") or {}).get("build_sha256")) or ""
        checks = run.get("checks") or []
        summary = run.get("summary") or {}
        # A value may be null: vision_benchmark.compare_summary writes None for an interval it could
        # not compute (`--bootstrap 0`, one sample) and a ratio over a median of 0.
        if not isinstance(summary, dict) or not all(
            v is None or (isinstance(v, int | float) and not isinstance(v, bool)) for v in summary.values()
        ):
            raise ValueError(f"{path}: summary must map codes to numbers (or null)")
        rows.append({
            "run_id": run["run_id"], "kind": run["kind"], "date": (run.get("started_at") or "")[:10],
            "cpu": env.get("cpu"), "backend": run.get("backend"), "build12": build[:12] or None,
            "git_sha": (run.get("code") or {}).get("git_sha"), "file": f"runs/{path.name}",
            "checks_failed": sum(1 for check in checks if not check.get("pass")), "summary": summary,
        })  # fmt: skip
    order = {kind: n for n, kind in enumerate(vision_runs.KINDS)}
    rows.sort(key=lambda row: row["run_id"], reverse=True)
    rows.sort(key=lambda row: row["date"], reverse=True)
    rows.sort(key=lambda row: order[row["kind"]])
    return rows


def older_code(evidence_dir: Path, rows: list[dict]) -> dict[str, str]:
    """`older-code.json`'s reasons ({} when there is none); ValueError for a run id not indexed or a
    reason that is not text."""
    path = Path(evidence_dir) / OLDER_CODE_FILE
    if not path.is_file():
        return {}
    reasons = json.loads(path.read_text(encoding="utf-8"))
    known = {row["run_id"] for row in rows}
    if not isinstance(reasons, dict):
        raise ValueError(f"{path}: not an object of run id to reason")
    for run_id, reason in reasons.items():
        if run_id not in known:
            raise ValueError(f"{path}: {run_id!r} is not a run file in runs/")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{path}: {run_id!r} needs its reason")
    return reasons


def _number(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def results_markdown(rows: list[dict], reasons: dict[str, str]) -> str:
    """RESULTS.md: one table per kind, newest first, each row linking its run file."""
    lines = [
        "# Results",
        "",
        "Generated by `python scripts/vision_eval.py index` from the run files in `runs/`; do not edit it "
        "by hand. `index --check` fails when a run file was added without re-indexing: run it before "
        "committing run files. CI runs it too (scripts/tests/test_vision_eval.py), on every pull "
        "request that touches `docs/hackathon/`, `scripts/` or `lambdas/`.",
        "Every number in the report cites a run file listed here. A run marked **measured on older code** "
        "was made on code that has changed since; the note says why it is still cited "
        f"(`{OLDER_CODE_FILE}`).",
        "",
    ]  # fmt: skip
    if not rows:
        lines += ["No run files yet.", ""]
    for kind in vision_runs.KINDS:
        found = [row for row in rows if row["kind"] == kind]
        if not found:
            continue
        codes = []
        for row in found:
            codes += [key for key in row["summary"] if "." not in key and key not in codes]
        shown = codes[:MAX_SUMMARY_COLUMNS]
        runs = f"{len(found)} run{'s' if len(found) != 1 else ''}"
        lines += [f"## {kind} ({runs})", "", KIND_WORDS[kind] + ".", ""]
        header = ["Run", "Date", "CPU", "Backend", "Build", "Code", *shown, "Checks", "Note"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|---|---|---|---|---|---|" + "---:|" * len(shown) + "---|---|")
        for row in found:
            more = len(row["summary"]) - sum(1 for key in shown if key in row["summary"])
            note = []
            if row["run_id"] in reasons:
                note.append(f"measured on older code: {reasons[row['run_id']]}")
            if more:
                note.append(f"{more} more values in the run file")
            checks = "pass" if not row["checks_failed"] else f"{row['checks_failed']} failed"
            cells = [f"[{row['run_id']}]({row['file']})", row["date"], row["cpu"] or "", row["backend"] or "",
                     f"`{row['build12']}`" if row["build12"] else "", f"`{(row['git_sha'] or '')[:12]}`",
                     *(_number(row["summary"][key]) if key in row["summary"] else "" for key in shown),
                     checks, "; ".join(note)]  # fmt: skip
            lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in cells) + " |")
        lines.append("")
    return "\n".join(lines)


def label_problems(evidence_dir: Path) -> tuple[list[str], int]:
    """Every problem in the operator's labels (`live/labels.csv`, `live/*/labels.csv`): the
    columns, `agree` or `disagree`, the reason codes, the decision's shape; and how many rows were
    read. None of the files need exist yet."""
    import csv

    evidence_dir = Path(evidence_dir)
    paths = sorted({*evidence_dir.glob("live/labels.csv"), *evidence_dir.glob("live/*/labels.csv")})
    problems, read = [], 0
    for path in paths:
        name = path.relative_to(evidence_dir).as_posix()
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError:
            problems.append(f"{name}: not UTF-8 text (save it as \"CSV UTF-8\")")
            continue
        text = [line for line in lines if not line.startswith("#")]
        table = list(csv.DictReader(text))
        header = list(table[0].keys()) if table else next(csv.reader(text[:1]), [])
        missing = [column for column in LABEL_COLUMNS if column not in header]
        if missing:
            problems.append(f"{name}: no column {missing}")
            continue
        for n, row in enumerate(table, start=2):
            read += 1
            if not _DECISION.match(row.get("decision") or ""):
                problems.append(f"{name} row {n}: decision is not <topic>/<site>/<scene>")
            if row.get("verdict") not in ("real", "artefact"):
                problems.append(f"{name} row {n}: verdict is not real or artefact")
            if row.get("label") not in LABELS:
                problems.append(f"{name} row {n}: label {row.get('label')!r} is not agree or disagree")
            if row.get("reason") not in LABEL_REASONS:
                reasons = ", ".join(LABEL_REASONS)
                problems.append(f"{name} row {n}: reason {row.get('reason')!r} is not one of {reasons}")
    return problems, read


def _main_index(args) -> int:
    evidence_dir = Path(args.evidence)
    if not evidence_dir.is_dir():
        print(f"refused: --evidence {evidence_dir} is not a directory", file=sys.stderr)
        return REFUSED
    try:
        rows = index_rows(evidence_dir / "runs")
        reasons = older_code(evidence_dir, rows)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    index_text = json.dumps({"schema": INDEX_SCHEMA, "runs": rows}, indent=1) + "\n"
    results_text = results_markdown(rows, reasons)
    problems, labelled = label_problems(evidence_dir)
    for problem in problems:
        print(problem, file=sys.stderr)
    targets = {evidence_dir / "index.json": index_text, evidence_dir / "RESULTS.md": results_text}
    if args.check:
        stale = [path.name for path, text in targets.items()
                 if not path.is_file() or path.read_text(encoding="utf-8") != text]  # fmt: skip
        if stale:
            print(f"stale: {', '.join(stale)}: run `python scripts/vision_eval.py index` and commit them",
                  file=sys.stderr)  # fmt: skip
        print(f"{len(rows)} run files indexed; {labelled} labelled decisions checked")
        return 1 if stale or problems else 0
    for path, text in targets.items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    return 1 if problems else 0


# --- main ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pin", help="HEAD every asset and read every STAC item; print the differences")
    p.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    p.add_argument("--write", action="store_true", help="write the values into the manifest")
    p.add_argument("--list-year", nargs=2, metavar=("TILE", "YEAR"), help="list a tile's items for a year")
    p.add_argument("--site", default="sydney", help="the site the year set is for (with --list-year)")
    c = sub.add_parser("coverage", help="E1 per scene and E2 over the year's listing; a run file")
    c.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    c.add_argument("--only", action="append", metavar="NAME", help="only this scene (repeatable)")
    c.add_argument(
        "--cache", type=vision_runs.cache_dir, nargs="?", const=vision_runs.default_cache_dir(),
        default=None, metavar="DIR", help="answer range reads from DIR (~ expanded), filled on first read",
    )
    c.add_argument("--write", action="store_true", help="put site_coverage and aot_mean into the manifest")
    c.add_argument("--out", type=Path, help="write the run file here: a .json file, or a directory")
    c.add_argument("--tag", help="a suffix for the run id")
    # The replay's sequences are vision_eval_driver.SEQUENCES', checked when it runs: importing this
    # module never imports the driver (it imports the adapter, and so boto3), so `pin` (without
    # --list-year) and `index` run in a venv with only the worker's requirements (the Graviton
    # session's stock venv). `coverage` and `pin --list-year` read the adapter's rule (rail_rule),
    # which imports the adapter: they need lambdas/requirements.txt too.
    r = sub.add_parser("replay", help="2.6.4: the real adapter over a pinned sequence, tick by tick")
    r.add_argument("--sequence", required=True, metavar="NAME", help=f"one of {', '.join(SEQUENCE_NAMES)}")
    r.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    r.add_argument("--network", type=Path, help="the network fixture (default: the site's committed one)")
    r.add_argument("--network-b", type=Path,
                   help=f"sydney-c1's second network (default: {NETWORK_B_DERIVED})")  # fmt: skip
    r.add_argument("--worker", default="in-process", metavar="ARN|in-process",
                   help="the deployed worker's ARN (site ids sent as eval-*), or in-process")  # fmt: skip
    r.add_argument(
        "--cache", type=vision_runs.cache_dir, nargs="?", const=vision_runs.default_cache_dir(),
        default=None, metavar="DIR", help="answer the in-process worker's reads from DIR, filled once",
    )
    r.add_argument("--backend", choices=("opencv", "cool"), default="opencv")
    r.add_argument("--cool-sha", help="the pinned COOL build's sha256 (required with --backend cool)")
    r.add_argument("--agent", choices=("bedrock", "recorded", "scripted"), default="scripted")
    r.add_argument("--from", dest="from_run", type=Path, help="the replay run file --agent recorded replays")
    r.add_argument("--model-id", help="the model or inference profile id (--agent bedrock)")
    r.add_argument("--region", help="Bedrock's Region (--agent bedrock; default: the AWS configuration's)")
    r.add_argument("--thresholds", choices=("default", "demo"), help="the sequence's own by default")
    r.add_argument("--limit", type=int, help="replay only the first N ticks")
    r.add_argument("--out", type=Path, help="write the run file here: a .json file, or a directory")
    r.add_argument("--trace", nargs="?", const=True, default=None, metavar="FILE",
                   help="write the trace as Markdown (default: replay/<sequence>.md)")  # fmt: skip
    r.add_argument("--tag", help="a suffix for the run id (after the sequence and the agent)")
    r.add_argument("--no-figures", dest="figures", action="store_false",
                   help="do not write each tick's figure beside the run file")  # fmt: skip

    def cache_option(parser_):
        parser_.add_argument(
            "--cache", type=vision_runs.cache_dir, nargs="?", const=vision_runs.default_cache_dir(),
            default=None, metavar="DIR", help="answer the in-process worker's reads from DIR, filled once",
        )

    f = sub.add_parser("failures", help="2.5: the failure catalogue's real rows and its tests; a run file")
    f.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    f.add_argument("--network", type=Path, help="the Sydney network fixture (default: the committed one)")
    cache_option(f)
    f.add_argument("--only", action="append", metavar="CASE", help="only this case (repeatable)")
    f.add_argument("--no-collect", dest="collect", action="store_false",
                   help="skip the check that every named test is collected")  # fmt: skip
    f.add_argument("--out", type=Path, help="write the run file here: a .json file, or a directory")
    f.add_argument("--tag", help="a suffix for the run id")
    a = sub.add_parser("access-baseline", help="E5: the distance transform against two references")
    a.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    a.add_argument("--scene", default=E5_SCENE, help=f"a manifest scene, or synthetic-1 (default {E5_SCENE})")
    a.add_argument("--network", type=Path, help="the network fixture (default: the site's committed one)")
    a.add_argument("--reach-m", type=float, default=1000.0, help="the walking reach (default the adapter's)")
    cache_option(a)
    a.add_argument("--out", type=Path, help="write the run file here: a .json file, or a directory")
    a.add_argument("--tag", help="a suffix for the run id")
    i = sub.add_parser("index", help="2.9: index.json and RESULTS.md from the run files")
    i.add_argument("--evidence", type=Path, default=EVIDENCE_DIR, help="the evidence directory")
    i.add_argument("--check", action="store_true", help="fail when either file is stale, or a label is wrong")
    # The agent, counterfactual and stability subcommands (Q2b): their arguments come from a module
    # that does not import the driver; vision_eval_agent (which does) is imported only to run one.
    import vision_eval_agent_cli

    vision_eval_agent_cli.add_parsers(sub, MANIFEST_FILE)
    args = parser.parse_args(argv)

    if args.command == "index":
        return _main_index(args)
    manifest = load_manifest(args.manifest)
    if args.command == "pin":
        return _main_pin(args, manifest)
    if args.command == "replay":
        return _main_replay(args, manifest)
    if args.command == "failures":
        return _main_failures(args, manifest)
    if args.command == "access-baseline":
        return _main_access_baseline(args, manifest)
    if args.command in vision_eval_agent_cli.COMMANDS:
        import vision_eval_agent

        return vision_eval_agent.main(args, manifest)
    return _main_coverage(args, manifest)


def _main_pin(args, manifest: dict) -> int:
    code = 0
    try:
        if args.list_year:
            tile, year = args.list_year[0], int(args.list_year[1])
            if args.site not in manifest["sites"]:
                print(f"no site {args.site!r} in the manifest", file=sys.stderr)
                return 2
            _floor, max_cloud_cover = rail_rule()
            items = list_year(tile, year, None)
            for item in items:
                print(f"{item['scene_id']} cloud {item.get('cloud_cover')} nodata {item.get('nodata_pct')}")
            print(f"{len(items)} items of {tile} in {year}")
            if args.write:
                added = add_year_set(manifest, args.site, tile, year, items, max_cloud_cover)
                print(f"added {len(added)} year scenes: {added}")
        lines, changed = pin(manifest)
    except vision_runs.WrongHost as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    if changed:
        print("input changed: a pinned ETag differs from the bucket's; earlier runs read other bytes",
              file=sys.stderr)  # fmt: skip
        code = 1
    if args.write:
        save_manifest(manifest, args.manifest)
        print(f"wrote {args.manifest} ({len(lines)} values)")
    elif not lines:
        print("the manifest matches the bucket")
    return code


def _main_coverage(args, manifest: dict) -> int:
    scenes = manifest["scenes"]
    if args.only:
        names = {scene["name"] for scene in scenes}
        missing = [name for name in args.only if name not in names]
        if missing:
            print(f"no scene named {missing} in the manifest", file=sys.stderr)
            return 2
        scenes = [scene for scene in scenes if scene["name"] in args.only]
    urls = [scene["assets"]["scl"]["url"] for scene in scenes]
    urls += [aot_url(scene["scene_id"]) for scene in scenes]
    started = vision_runs.utc_now()  # before the HEADs and the reads: the run's own start
    try:
        heads = vision_runs.head_assets(urls)
        vision_runs.check_pinned(heads, vision_runs.pinned_etags(manifest))
    except (vision_runs.WrongHost, vision_runs.InputChanged) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    import vision_worker_handler
    from vision import build

    fetch = vision_worker_handler.http_fetch
    cache = None
    if args.cache is not None:
        etags = {url: fields.get("etag") or "" for url, fields in heads.items()}
        cache = fetch = vision_runs.CachedFetch(args.cache, etags, fetch=fetch)
    results, summary = coverage(manifest, scenes, fetch)
    inputs = {
        "manifest_sha256": vision_runs.sha256_file(args.manifest),
        "cache": cache is not None,
        "cache_index_sha256": cache.index_sha256() if cache else None,
        "scenes": [
            {"name": s["name"], "scene_id": s["scene_id"],
             "etag_by_asset": {"scl": heads[s["assets"]["scl"]["url"]].get("etag"),
                               "aot": heads[aot_url(s["scene_id"])].get("etag")}}
            for s in scenes
        ],
        "networks": [],
    }  # fmt: skip
    env = vision_runs.environment(cpu_identity(), build.build_info())
    run = vision_runs.new_run(
        "eval-coverage", env["cpu"], "none", env, inputs=inputs, tag=args.tag, mode="coverage",
        started=started,
        params={"only": args.only, "cache": cache is not None, "floor": rail_rule()[0],
                "max_cloud_cover": rail_rule()[1], "year_set_min_coverage": YEAR_SET_MIN_COVERAGE},
    )  # fmt: skip
    vision_runs.finish(run, results, coverage_checks(scenes, results), summary)
    print(coverage_markdown(run))
    if args.write:
        written = write_coverage(manifest, results)
        manifest["sets"] = year_sets(manifest, rail_rule()[1])
        save_manifest(manifest, args.manifest)
        sizes = ", ".join(f"{name} {len(entry['scenes'])} scenes" for name, entry in manifest["sets"].items())
        print(f"wrote {written} scenes' coverage and the sets ({sizes or 'none'}) into {args.manifest}")
    if args.out:
        path = vision_runs.write(run, args.out)
        print(f"wrote {path}")
    return 0 if all(check["pass"] for check in run["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
