"""E8, the built-up check: the worker's built-up mask against ESA WorldCover 2021's "Built-up" class.

    python scripts/vision_eval.py builtup-check --cache ~/cogs --out docs/hackathon/evidence/runs
    python scripts/vision_eval.py builtup-check --scene synthetic-1 --out DIR    # CI: synthetic inputs

The worker calls a pixel built-up from Sentinel-2 alone (vision/urban.built_up_mask: NDBI above a
threshold, NDVI below a ceiling, SCL's water and unusable classes out), and every access number the
pipeline reports is a share of that mask. E8 asks how far that proxy agrees with an independent map
(the submission round's 2.4.1): ESA WorldCover 2021 v200 (10 m, CC BY 4.0, the AWS Registry of Open
Data's `esa-worldcover` bucket, eu-central-1), class 50 "Built-up".

**Method.** One clear Sydney scene (sydney-2024-08-26) is read and analysed in this process exactly
as the worker does (vision_eval.scene_site_analysis): its built-up mask, its usable pixels, its
stations. The reference tile under the site (3 x 3 degrees, named by its lower-left corner:
`S36E150` for Sydney) is read with the repository's own COG reader over the site's box only.
WorldCover is in EPSG:4326; vision/geo.py converts between lon/lat and UTM but reads pixels only
through UTM transforms (pixel_to_lonlat and lonlat_polygon_to_pixels take a UTM EPSG code), and
vision/cog.py reads a projected CRS's GeoKey only, so this module checks the GeoKey directory itself
(GeographicTypeGeoKey 4326) and maps lon/lat to the tile's pixels with its affine transform
(geo.map_to_pixel, which is CRS-agnostic). Each 20 m scene pixel's centre is taken to lon/lat with
geo.utm_to_lonlat on a grid of nodes GRID_STEP_PX apart, interpolated between them (the projection is
smooth: well under a metre over 640 m), and the reference is resampled onto the scene's grid by
nearest neighbour with cv2.remap. Compared are the pixels inside the site that the scene saw (its
usable mask) and the reference classifies (not its no-data 0). Reported: the IoU, precision and
recall of our mask against class 50, and the served share (the production distance transform, the
same stations and reach) computed over each mask: `served_share_ours`, `served_share_reference`.

**Inputs pinned.** The reference object's ETag and size, checked with one HEAD from the build
container on 17 October 2026 (the bucket answered in eu-central-1), are REFERENCE_PINS: a HEAD
before the run refuses it ("input changed") when either differs, and the run file records both. The
reference is read in this script, never through the worker, whose URL allowlist stays the Sentinel-2
bucket's; this script reads only REFERENCE_HOST (and, synthetic, the synthetic city's prefix).

**Synthetic (CI).** `--scene synthetic-1` runs the same code on the synthetic city of
vision_eval_driver.py, against a synthetic reference in EPSG:4326: the city's built district by
construction (its roofs, its lake as water, canopy elsewhere), written at 10 m in lon/lat. It tests
the geometry and the arithmetic, not the proxy: the run file says so.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import vision_runs

ROOT = Path(__file__).resolve().parents[1]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

KIND = "eval-builtup-check"
E8_SCENE = "sydney-2024-08-26"
REFERENCE_HOST = "esa-worldcover.s3.eu-central-1.amazonaws.com"
REFERENCE_REGION = "eu-central-1"
REFERENCE_PREFIX = f"https://{REFERENCE_HOST}/v200/2021/map/"
REFERENCE_FILE = "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
REFERENCE_PRODUCT = "ESA WorldCover 10 m 2021 v200"
REFERENCE_LICENCE = "CC BY 4.0"
REFERENCE_CREDIT = "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021)"
# One HEAD from the build container on 17 October 2026: 200, the bucket in eu-central-1
# (x-amz-bucket-region), Last-Modified 26 October 2022.
REFERENCE_PINS = {"S36E150": {"etag": "b80441256064dfdeacf0e397722cd017-3", "bytes": 20060792}}
# WorldCover's tiles are 3 x 3 degrees, named by their lower-left corner.
TILE_DEGREES = 3
# The classes of the WorldCover 2021 product (its manual's legend); 0 is no data.
CLASSES = {
    10: "tree_cover",
    20: "shrubland",
    30: "grassland",
    40: "cropland",
    50: "built_up",
    60: "bare_sparse",
    70: "snow_ice",
    80: "water",
    90: "wetland",
    95: "mangroves",
    100: "moss_lichen",
}
BUILT_UP, NO_DATA = 50, 0
# The GeoKey directory's GeographicTypeGeoKey, and the EPSG code WorldCover is in.
GEOGRAPHIC_TYPE_KEY, WGS84 = 2048, 4326
# A degree-sized pixel: WorldCover's is 1/12000 of a degree (about 9 m at Sydney's latitude); a pixel
# this large in "degrees" would be metres read as degrees.
MAX_DEGREE_PIXEL = 0.01
# The scene's pixel centres are taken to lon/lat on nodes this far apart and interpolated between.
GRID_STEP_PX = 32
# Rows interpolated at a time (bounds the memory of the lon/lat grids).
CHUNK_ROWS = 256
# The reference read around the site's box, in reference pixels: the nearest neighbour of an edge
# pixel's centre must be inside the window.
WINDOW_MARGIN_PX = 4
# The share of the site that must be compared for the numbers to stand for it.
MIN_COMPARED_SHARE = 0.5
HTTP_TIMEOUT_SECONDS = 30.0
SYNTHETIC_SCENE = "synthetic-1"
REFUSED = 2


class ReferenceRefused(Exception):
    """A reference this check will not read: another host, another CRS, more than one tile, or an
    object that is not the one pinned."""


# --- the reference tile --------------------------------------------------------------------------------


def tile_name(lon: float, lat: float) -> str:
    """The 3-degree tile holding (lon, lat), by its lower-left corner: S36E150 for Sydney."""
    lat0 = math.floor(lat / TILE_DEGREES) * TILE_DEGREES
    lon0 = math.floor(lon / TILE_DEGREES) * TILE_DEGREES
    return f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"


def polygon_box(polygon: list) -> tuple[float, float, float, float]:
    lons, lats = [float(p[0]) for p in polygon], [float(p[1]) for p in polygon]
    return min(lons), min(lats), max(lons), max(lats)


def tile_for(polygon: list) -> str:
    """The one tile the site's box lies in; ReferenceRefused naming both when it spans two."""
    west, south, east, north = polygon_box(polygon)
    corners = {tile_name(west, south), tile_name(east, north)}
    if len(corners) != 1:
        raise ReferenceRefused(
            f"the site spans reference tiles {', '.join(sorted(corners))}: one tile is read"
        )
    return corners.pop()


def reference_url(tile: str, prefix: str = REFERENCE_PREFIX) -> str:
    return prefix + REFERENCE_FILE.format(tile=tile)


def check_reference_url(url: str, allowed: str = REFERENCE_PREFIX) -> str:
    """`url` when it is under the reference bucket's map folder (or the synthetic prefix a CI run
    passes); ReferenceRefused before any request otherwise."""
    if not isinstance(url, str) or not url.startswith(allowed):
        raise ReferenceRefused(f"refused: not under {allowed}: {url!r}")
    return url


def head_reference(url: str, head=None) -> dict:
    """One HEAD of the reference object: its ETag (unquoted) and size."""
    if head is None:
        import requests

        response = requests.head(
            check_reference_url(url),
            timeout=(HTTP_TIMEOUT_SECONDS, HTTP_TIMEOUT_SECONDS),
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise vision_runs.InputChanged(f"HEAD {url}: HTTP {response.status_code}")
        return vision_runs.head_fields(response.headers)
    return head(check_reference_url(url))


def check_pin(tile: str, found: dict) -> None:
    """InputChanged when the object is not the one pinned (its ETag or its size)."""
    pin = REFERENCE_PINS.get(tile)
    if pin is None:
        raise vision_runs.InputChanged(
            f"no pin for reference tile {tile}: HEAD it and add it to REFERENCE_PINS"
        )
    if (found.get("etag"), found.get("bytes")) != (pin["etag"], pin["bytes"]):
        raise vision_runs.InputChanged(
            f"input changed: reference tile {tile} (pinned ETag {pin['etag']} and "
            f"{pin['bytes']} bytes, now {found.get('etag')} and {found.get('bytes')})"
        )


def reference_fetch(url: str, start: int, end: int) -> bytes:
    """One range read of the reference, with timeouts, no redirect."""
    import requests

    response = requests.get(
        check_reference_url(url),
        headers={"Range": f"bytes={start}-{end}"},
        timeout=(HTTP_TIMEOUT_SECONDS, HTTP_TIMEOUT_SECONDS),
        allow_redirects=False,
    )
    if response.status_code != 206:
        raise ReferenceRefused(f"GET {url} bytes {start}-{end}: HTTP {response.status_code}")
    return response.content


def geographic_epsg(source) -> int | None:
    """The EPSG code under the first image's GeographicTypeGeoKey (2048), from the GeoKey directory
    vision/cog.py parses but reads only the projected key of; None when there is none."""
    import struct

    from vision import cog

    head = source.head
    order = {b"II": "<", b"MM": ">"}.get(head[:2])
    magic = struct.unpack(order + "H", head[2:4])[0]
    big = magic == 43
    first = struct.unpack(order + ("Q" if big else "I"), head[8:16] if big else head[4:8])[0]
    tags, _next = cog._parse_ifd(source, first, order, big)
    keys = tags.get(34735) or ()
    for k in range(4, len(keys) - 3, 4):
        key_id, location, _count, value = keys[k : k + 4]
        if key_id == GEOGRAPHIC_TYPE_KEY and location == 0:
            return int(value)
    return None


def read_reference(url: str, polygon: list, fetch):
    """The reference's pixels over the site's box (a margin around it), their lon/lat transform and
    the file's geographic EPSG code; ReferenceRefused unless it is EPSG:4326 with degree-sized pixels
    and the box is inside it."""
    from vision import cog, geo

    info, source = cog.read_info(url, fetch)
    epsg = geographic_epsg(source)
    if info.epsg is not None or epsg != WGS84:
        raise ReferenceRefused(
            f"the reference is not EPSG:{WGS84} (projected {info.epsg}, geographic {epsg})"
        )
    if not 0 < info.pixel_size < MAX_DEGREE_PIXEL:
        raise ReferenceRefused(f"the reference's pixel is {info.pixel_size}, not a fraction of a degree")
    west, south, east, north = polygon_box(polygon)
    col0, row0 = geo.map_to_pixel(info.transform, west, north)
    col1, row1 = geo.map_to_pixel(info.transform, east, south)
    window = (
        math.floor(col0) - WINDOW_MARGIN_PX,
        math.floor(row0) - WINDOW_MARGIN_PX,
        math.ceil(col1) + WINDOW_MARGIN_PX,
        math.ceil(row1) + WINDOW_MARGIN_PX,
    )
    if window[0] < 0 or window[1] < 0 or window[2] > info.width or window[3] > info.height:
        raise ReferenceRefused("the site's box is not inside the reference tile")
    data, _ = cog.read_window(url, window, fetch, (info, source))
    return data, cog.window_transform(info, window), epsg, window


# --- onto the scene's grid -----------------------------------------------------------------------------


def _axis(nodes, values):
    import numpy as np

    k = np.clip(np.searchsorted(nodes, values, side="right") - 1, 0, len(nodes) - 2)
    span = nodes[k + 1] - nodes[k]
    return k, (values - nodes[k]) / span


def scene_lonlat(shape: tuple[int, int], transform, epsg: int, step: int = GRID_STEP_PX):
    """(lon, lat) of every pixel centre of a UTM grid (`transform`, `epsg`), as float64 arrays:
    geo.utm_to_lonlat at nodes `step` pixels apart (and at the last row and column), bilinear between
    them, CHUNK_ROWS rows at a time."""
    import numpy as np
    from vision import geo

    h, w = shape
    if h < 2 or w < 2:
        raise ValueError("a grid of at least 2 x 2 pixels")
    zone, south = geo.utm_zone_from_epsg(epsg)
    c, a, _b, f, _d, e = transform
    rows = np.unique(np.append(np.arange(0, h, step), h - 1)).astype(float)
    cols = np.unique(np.append(np.arange(0, w, step), w - 1)).astype(float)
    lon_n, lat_n = np.empty((len(rows), len(cols))), np.empty((len(rows), len(cols)))
    for i, r in enumerate(rows):
        for j, col in enumerate(cols):
            lon_n[i, j], lat_n[i, j] = geo.utm_to_lonlat(c + a * (col + 0.5), f + e * (r + 0.5), zone, south)
    kj, tj = _axis(cols, np.arange(w, dtype=float))
    lon, lat = np.empty(shape), np.empty(shape)
    for top in range(0, h, CHUNK_ROWS):
        ki, ti = _axis(rows, np.arange(top, min(h, top + CHUNK_ROWS), dtype=float))
        for nodes, out in ((lon_n, lon), (lat_n, lat)):
            g00, g01 = nodes[np.ix_(ki, kj)], nodes[np.ix_(ki, kj + 1)]
            g10, g11 = nodes[np.ix_(ki + 1, kj)], nodes[np.ix_(ki + 1, kj + 1)]
            upper = g00 + (g01 - g00) * tj[None, :]
            lower = g10 + (g11 - g10) * tj[None, :]
            out[top : top + len(ki)] = upper + (lower - upper) * ti[:, None]
    return lon, lat


def resample(reference, reference_transform, lon, lat):
    """The reference on the scene's grid by nearest neighbour (cv2.remap): each scene pixel takes
    the class of the reference pixel its centre falls in; outside the window, no data."""
    import cv2
    import numpy as np

    c, a, _b, f, _d, e = reference_transform
    map_x = ((lon - c) / a - 0.5).astype(np.float32)
    map_y = ((lat - f) / e - 0.5).astype(np.float32)
    return cv2.remap(
        np.ascontiguousarray(reference),
        map_x,
        map_y,
        cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=NO_DATA,
    )


# --- the comparison ------------------------------------------------------------------------------------


def compare(built, usable, reference, stations_px, reach_px: float, pixel_size_m: float) -> tuple[dict, dict]:
    """E8 over the compared pixels (usable in the scene, classified in the reference): the IoU,
    precision and recall of our mask against class 50, the served share over each mask with the same
    stations and reach (the production distance transform), and the classes the reference gives our
    built-up pixels."""
    import numpy as np
    from vision import access

    usable = np.asarray(usable) > 0
    reference = np.asarray(reference)
    compared = usable & (reference != NO_DATA)
    ours = (np.asarray(built) > 0) & compared
    theirs = (reference == BUILT_UP) & compared
    both = ours & theirs
    tp, fp, fn = (int(np.count_nonzero(m)) for m in (both, ours & ~theirs, theirs & ~ours))
    union = tp + fp + fn
    within = access.distance_to_stations(access.station_raster(ours.shape, stations_px))[0] <= reach_px

    def share(mask):
        total = int(np.count_nonzero(mask))
        return round(int(np.count_nonzero(mask & within)) / total, 6) if total else None

    pixel_km2 = (pixel_size_m / 1000.0) ** 2
    classes = reference[ours]
    ours_px = int(np.count_nonzero(ours))
    by_class = (
        {
            CLASSES.get(int(code), str(int(code))): round(int(n) / ours_px, 6)
            for code, n in zip(*np.unique(classes, return_counts=True), strict=True)
        }
        if ours_px
        else {}
    )
    in_site = reference[compared]
    site_classes = {
        CLASSES.get(int(code), str(int(code))): round(int(n) / max(1, in_site.size), 6)
        for code, n in zip(*np.unique(in_site, return_counts=True), strict=True)
    }
    results = {
        "pixel_size_m": pixel_size_m,
        "reach_px": reach_px,
        "usable_px": int(np.count_nonzero(usable)),
        "compared_px": int(np.count_nonzero(compared)),
        "compared_km2": round(int(np.count_nonzero(compared)) * pixel_km2, 4),
        "ours_km2": round(ours_px * pixel_km2, 4),
        "reference_km2": round((tp + fn) * pixel_km2, 4),
        "true_positive_px": tp,
        "false_positive_px": fp,
        "false_negative_px": fn,
        "ours_by_reference_class": by_class,
        "reference_classes_compared": site_classes,
        "stations": sum(1 for s in stations_px if s is not None),
    }
    summary = {
        "iou": round(tp / union, 6) if union else None,
        "precision": round(tp / (tp + fp), 6) if tp + fp else None,
        "recall": round(tp / (tp + fn), 6) if tp + fn else None,
        "served_share_ours": share(ours),
        "served_share_reference": share(theirs),
    }
    return results, summary


# --- the synthetic reference (CI) ----------------------------------------------------------------------


def synthetic_reference(
    size: int | None = None, shift_deg: tuple[float, float] = (0.0, 0.0)
) -> tuple[str, bytes]:
    """The synthetic city's reference: a 10 m lon/lat raster in EPSG:4326 over the city's box, class
    50 where the city is built by construction (its roofs: red reflectance of 1500 against 600 for
    canopy), 80 on its lake (SCL 6), 10 (tree cover) elsewhere. (URL, GeoTIFF bytes). `shift_deg`
    writes it misregistered by that much (lon, lat), for the test that the check sees it."""
    import numpy as np
    from vision import geo

    import vision_eval_driver as driver
    from vision_benchmark import CITY_EPSG, synthetic_city

    size = size or driver.SYNTHETIC_SIZE
    seed = next(e["seed"] for e in driver.SYNTHETIC_SCENES if e["name"] == SYNTHETIC_SCENE)
    bands = synthetic_city(size, seed=seed)
    truth = np.full((size, size), 10, np.uint8)
    truth[bands["red"] > 1050] = BUILT_UP
    truth[bands["scl"] == 6] = 80
    west, south, east, north = driver.synthetic_bbox(size)
    step = 1.0 / 12000.0  # WorldCover's pixel
    width, height = int(math.ceil((east - west) / step)) + 8, int(math.ceil((north - south) / step)) + 8
    origin = (west - 4 * step, north + 4 * step)
    zone, south_hemisphere = geo.utm_zone_from_epsg(CITY_EPSG)
    city = driver.synthetic_transform()
    raster = np.zeros((height, width), np.uint8)
    for row in range(height):
        lat = origin[1] - (row + 0.5) * step
        for col in range(width):
            lon = origin[0] + (col + 0.5) * step
            x, y = geo.lonlat_to_utm(lon, lat, zone, south_hemisphere)
            c, r = geo.map_to_pixel(city, x, y)
            if 0 <= c < size and 0 <= r < size:
                raster[row, col] = truth[int(r), int(c)]
    url = reference_url(tile_name(west, south), prefix=synthetic_prefix())
    written = (
        origin[0] + shift_deg[0],
        step,
        0.0,
        origin[1] + shift_deg[1],
        0.0,
        -step,
    )  # misregistered by shift_deg
    blob = driver.geotiff(raster, written, WGS84, nodata="0", geokey=driver.GEOGRAPHIC_TYPE_KEY)
    return url, blob


def synthetic_prefix() -> str:
    import vision_eval_driver as driver

    return driver.SYNTHETIC_PREFIX + "worldcover/"


# --- the subcommand ------------------------------------------------------------------------------------


def add_parser(sub, manifest_file: Path, cache_option) -> None:
    b = sub.add_parser("builtup-check", help="E8: the built-up mask against ESA WorldCover 2021 (class 50)")
    b.add_argument("--manifest", type=Path, default=manifest_file)
    b.add_argument(
        "--scene", default=E8_SCENE, help=f"a manifest scene, or {SYNTHETIC_SCENE} (default {E8_SCENE})"
    )
    b.add_argument("--network", type=Path, help="the network fixture (default: the site's committed one)")
    b.add_argument("--reach-m", type=float, default=1000.0, help="the walking reach (default the adapter's)")
    cache_option(b)
    b.add_argument("--out", type=Path, help="write the run file here: a .json file, or a directory")
    b.add_argument("--tag", help="a suffix for the run id")


def main(args, manifest: dict, *, head=None, fetch=None, reference=None) -> int:
    """`builtup-check`: E8 on one scene, one run file. `head`, `fetch` (the scene's reads) and
    `reference` ((url, fetch) of the reference) are the tests' seams."""
    import vision_eval
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
        polygon = manifest["sites"][scene["site"]]["polygon"]
    network_path = vision_eval.network_file_for("synthetic" if synthetic else scene["site"], args.network)
    try:
        network = driver.load_network_file(network_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"{exc} ({vision_runs.shown_path(network_path)})", file=sys.stderr)
        return REFUSED
    try:
        tile = tile_for(polygon)
        heads = vision_eval.head_scenes([scene], manifest, head=head)
        if synthetic:
            url, blob = reference or synthetic_reference()
            ref_fetch = blob if callable(blob) else (lambda u, s, e, b=blob: b[s : e + 1])
            check_reference_url(url, synthetic_prefix())
            found = {"etag": None, "bytes": None}
        else:
            url = reference_url(tile)
            found = head_reference(url, head=head)
            check_pin(tile, found)
            ref_fetch = (reference or (None, reference_fetch))[1]
    except (vision_runs.InputChanged, vision_runs.WrongHost, ReferenceRefused) as exc:
        print(str(exc), file=sys.stderr)
        return REFUSED
    scene_fetch, cache = vision_eval.worker_fetch(args.cache, heads, synthetic, fetch=fetch)
    if scene_fetch is None:
        import vision_worker_handler

        scene_fetch = vision_worker_handler.http_fetch
    reach_m = float(args.reach_m)
    try:
        analysis, site = vision_eval.scene_site_analysis(scene, polygon, network, scene_fetch, reach_m)
        if cache is not None and not synthetic:
            # The same cache directory, opened after the scene's reads so its index keeps them too.
            ref_fetch = vision_runs.CachedFetch(cache.directory, etags={url: found["etag"]}, fetch=ref_fetch)
        data, transform, epsg, window = read_reference(url, polygon, ref_fetch)
    except ReferenceRefused as exc:
        print(str(exc), file=sys.stderr)
        return REFUSED
    built = analysis.layers["built"]
    lon, lat = scene_lonlat(built.shape, site.transform, site.epsg)
    on_grid = resample(data, transform, lon, lat)
    results, summary = compare(
        built,
        analysis.layers["usable"],
        on_grid,
        analysis.layers["stations_px"],
        reach_m / site.pixel_size_m,
        site.pixel_size_m,
    )
    usable_px = results["usable_px"]
    compared_share = round(results["compared_px"] / usable_px, 6) if usable_px else 0.0
    results.update(
        {
            "worker_served_share": analysis.metrics.get("served_share"),
            "worker_built_up_km2": analysis.metrics.get("built_up_km2"),
            "compared_share_of_usable": compared_share,
            "reference": {
                "tile": tile,
                "crs": f"EPSG:{epsg}",
                "window_px": list(window),
                "pixel_deg": abs(transform[1]),
                "synthetic": synthetic,
            },
        }
    )
    checks = [
        {"name": "the reference is in EPSG:4326", "expected": WGS84, "got": epsg, "pass": epsg == WGS84},
        {
            "name": "most of the site's usable pixels were compared",
            "expected": f">= {MIN_COMPARED_SHARE}",
            "got": compared_share,
            "pass": compared_share >= MIN_COMPARED_SHARE,
        },
    ]
    from vision_benchmark import environment as bench_environment

    env = bench_environment()
    run = vision_runs.new_run(
        KIND,
        env["cpu"],
        "opencv",
        env,
        tag=args.tag,
        mode="builtup-check",
        started=started,
        worker={"kind": "in-process", "region": None, "code_sha256": vision_runs.worker_code_sha256()},
        inputs={
            "manifest_sha256": None if synthetic else vision_runs.sha256_file(args.manifest),
            "cache": cache is not None,
            "cache_index_sha256": cache.index_sha256() if cache else None,
            "scenes": vision_eval.scene_etags([scene], heads),
            "networks": [
                {
                    "file": vision_runs.shown_path(network_path),
                    "sha256": vision_runs.sha256_file(network_path),
                }
            ],
            "reference": {
                "product": REFERENCE_PRODUCT,
                "licence": REFERENCE_LICENCE,
                "credit": REFERENCE_CREDIT,
                "url": url,
                "region": None if synthetic else REFERENCE_REGION,
                "tile": tile,
                "etag": found.get("etag"),
                "bytes": found.get("bytes"),
                "synthetic": synthetic,
            },
        },
        params={
            "scene": args.scene,
            "reach_m": reach_m,
            "built_up_class": BUILT_UP,
            "grid_step_px": GRID_STEP_PX,
            "resampling": "nearest neighbour (cv2.remap) onto the scene's 20 m UTM grid",
            "synthetic": synthetic,
        },
    )
    vision_runs.finish(run, results, checks, summary)
    print("| measure | value |\n|---|---:|")
    for key, value in summary.items():
        print(f"| {key} | {value} |")
    if args.out:
        print(f"wrote {vision_runs.write(run, args.out)}")
    return 0 if all(check["pass"] for check in checks) else 1
