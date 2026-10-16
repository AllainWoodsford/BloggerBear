#!/usr/bin/env python3
"""The evaluation's inputs: the pinned Sentinel-2 scenes (`pin`) and what each one shows of the
site (`coverage`). One run file per run, as scripts/vision_runs.py writes it.

    python scripts/vision_eval.py pin           # HEAD every asset, read every STAC item; print the changes
    python scripts/vision_eval.py pin --write   # ... and write them into the manifest
    python scripts/vision_eval.py pin --list-year 56HLH 2024 --write   # add a tile's year (the year set)
    python scripts/vision_eval.py coverage --out docs/hackathon/evidence/runs --write   # E1, E2, the run file

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
into the manifest; `--cache DIR` answers the reads from the cache after the first.

Runs on Windows as well as Linux and macOS (nothing here pins a CPU or reads `resource`). Needs
lambdas/requirements-vision.txt and scripts/requirements.txt installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import vision_runs
from vision_benchmark import cpu_identity

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
    c.add_argument("--cache", type=Path, nargs="?", const=vision_runs.default_cache_dir(), default=None,
                   metavar="DIR", help="answer range reads from DIR, filled on first read")  # fmt: skip
    c.add_argument("--write", action="store_true", help="put site_coverage and aot_mean into the manifest")
    c.add_argument("--out", type=Path, help="write the run file here: a .json file, or a directory")
    c.add_argument("--tag", help="a suffix for the run id")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    if args.command == "pin":
        return _main_pin(args, manifest)
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
        params={"only": args.only, "cache": cache is not None, "floor": rail_rule()[0],
                "max_cloud_cover": rail_rule()[1], "year_set_min_coverage": YEAR_SET_MIN_COVERAGE},
    )  # fmt: skip
    vision_runs.finish(run, results, coverage_checks(scenes, results), summary)
    print(coverage_markdown(run))
    if args.write:
        written = write_coverage(manifest, results)
        save_manifest(manifest, args.manifest)
        print(f"wrote {written} scenes' coverage into {args.manifest}")
    if args.out:
        path = vision_runs.write(run, args.out)
        print(f"wrote {path}")
    return 0 if all(check["pass"] for check in run["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
