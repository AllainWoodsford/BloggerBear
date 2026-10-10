# Rail Access Monitor: the second vision task, for the OpenCV AI Competition 2026

**Status:** in progress on the integration branch (PR #285) · **Date:** 2026-10-10 ·
**Deadline:** 26 October 2026, 11:45 pm PDT · **Builds on:** [vision.md](vision.md) (the agentic vision
stack, PRs #240–#254) and [opencv-agentic-vision-enhancement.md](opencv-agentic-vision-enhancement.md)

This document records the review of the vision stack, the choice of competition angle, the
architecture of the rail task, and one specification per pull request. The specifications are
what the implementing sessions build from; the rest is why.

## 1. Review of the vision stack (#240 → #254)

**Verdict: sound; keep it and build on it.** 5,670 lines across 60 files, CI green on every PR,
128 new tests with no network in any of them, nothing deployed until `vision_enabled = true`.
The layering is the right one for this repository and for the rubric:

| Layer | Where | Why it holds up |
|---|---|---|
| Pure vision core, no AWS | `lambdas/vision/{masks,detect,analyse,annotate,build}.py` | tested on synthetic scenes; `build.py` fingerprints the OpenCV build, so a COOL result can be proven |
| COG reader and UTM, no GDAL | `lambdas/vision/{cog,geo,scene}.py` | stays under Lambda's 250 MB; 3 range requests and 2.7 MB per site on a real scene |
| Stateless worker and contract | `vision_worker_handler.py`, `common/vision_contract.py`, `common/vision_client.py` | validated requests, URL allowlist, cross-region invoke, every failure is a code: "not measured" is never "nothing there" |
| Thin adapter | `common/adapters/satellite_vision.py` | configuration decides what is watched; diff-first (rule 2); `keeps_running_state` is a clean, generic addition to the research tick |
| Bounded agent | `common/vision_triage.py` | tool budget and turn cap in code, fails closed to "artefact", trail stored, spend tallied |
| Terraform | `infra/modules/vision-worker/` | arm64 / python3.12 in us-west-2 beside the data, its own role (logs only), `count = vision_enabled ? 1 : 0` |
| COOL | `scripts/vision_benchmark.py`, `docker/vision-cool/Dockerfile` | the benchmark isolates OpenCV kernels; the image never installs its own cv2 |

**Reusable for a second task as is:** the COG reader, UTM (needs the inverse added), the scene
reader (needs a per-task reference band), the build record, the figure helpers, the handler
shell, the contract and client machinery, the adapter's STAC search, history, running state,
figure storage and time budget, the triage loop, the Terraform, the benchmark harness,
`force_manual_review`, the admin API's float fix.

**Ship-only:** the water mask and NDWI, the blob detector and its defaults, the metrics, the
count-against-median rule, the triage prompt ("objects on water"), the summary prompt.

**Findings, and what was done about them**

1. *Figures never reach a reader.* The worker returns a PNG; the adapter stores it in the private
   content bucket; only the triage agent ever loads it. No Finding or Article field carries it,
   `frontend/markdown.js` has no image support, and the site bucket may only be written under
   `articles/`. For a vision entry the map must be on the page. → the figures path (PR C).
2. *The agent could push the research tick past 120 s.* `time_budget_seconds` bounds only the
   measuring loop; triage then ran with up to five model turns and `look_again` worker calls at a
   90 s read timeout. → `triage_deadline_seconds` (85 s from the tick's start); no model turn
   starts with under 10 s left and `look_again` is refused with under 30 s (done, on this branch).
3. *A contract gap.* `min_length_m > max_length_m` passed validation and surfaced as `internal`
   instead of `bad_request`. → checked in the contract against the detector's defaults, which a
   test holds equal (done).
4. *Dead links.* `vision.md` and ten docstrings cite `opencv-agentic-vision-enhancement.md`, which
   lived only on #240's branch. → both docs are on this branch.
5. *Merge drift.* `dev` moved 86 commits; three append/append conflicts (`infra/bootstrap/variables.tf`,
   two test files). → both sides kept.
6. *`cv2.ximgproc` is not in `opencv-python-headless`* (no contrib), and COOL's module list is not
   published. The usual `cv2.ximgproc.thinning` is therefore out; thinning is done with core OpenCV
   (`cv2.morphologyEx` with `MORPH_HITMISS`).

Accepted and documented rather than changed: the first tick is never triaged (every article of
these topics is held for a person anyway); `material_diff` runs the agent, so it has side effects
and reads state set by `fetch_state`; COOL has no Terraform (it is a Marketplace AMI or image for
Graviton EC2, and the award's measurement is a manual run, §9 of vision.md).

## 2. The angle: rail access, not rail yards

**Rail-yard congestion was rejected.** A rail car is about 3 m by 15 m; at Sentinel-2's 10 m it is
below one pixel, and yard tracks 4–5 m apart blur into a texture. Counting cars honestly needs
imagery at a metre or better. The only free sub-metre source on AWS Open Data is NAIP: United
States only, a requester-pays bucket, revisited every two to three years, so "live against
historical" does not exist for free. Nothing in the ship detector transfers.

**Rail access was chosen** because every question it asks is measurable at city scale from what
the stack already reads, plus one free vector source:

| Question | Data | OpenCV 5 in the worker |
|---|---|---|
| Where is the city built up (the "heat")? | Sentinel-2 red, nir, swir16, scl at 20 m | NDBI and NDVI thresholds, SCL masks, Gaussian density → `heat` |
| Where are the transit deserts? | heat and station points | station raster → `distanceTransform`; desert = built-up and farther than `reach_m`; `connectedComponentsWithStats` for the clusters |
| What is the network as a graph? | OpenStreetMap rail ways and stations (Overpass, free) | `polylines` → core thinning → `filter2D` junctions and endpoints → `connectedComponents` segments → graph; stations snapped with `distanceTransformWithLabels` |
| Is the mapped corridor visible in the imagery? | both | a 3-pixel swath per edge: share of pixels low in NDVI and not water or cloud (`corridor_visibility`), tunnels excluded |
| Which stations are isolated, hubs, or single points of failure? | graph and heat | networkx (worker only): degree, betweenness, hubs, distance to the nearest hub, `W = d_hub_km / max(activity, 0.05)`, flags, simulated orbital links |
| Intermodal connectivity | OSM bus stations, ferry terminals, park-and-ride, bus stops within 400 m of a station | counts per station within 300 m and 500 m |
| Did anything change? | this scene against the baseline | adapter diff on `served_share`, `desert_km2` and the station count; the agent with `look_again` and `web_context` (GDELT through `common/web_search.search_web`) |

What stays honest: the heat map is a **built-up density proxy from imagery**, not population. The
topology comes from OpenStreetMap (ODbL, credited) and the imagery verifies and scores it; the
pixel-to-graph code takes any mask, so a learned segmentation can replace the rasterised ways
later without changing what follows. Articles say what was not measured. `force_manual_review`
keeps a person on every article.

The wish to use web search or GDELT maps onto the existing `search_web` as a bounded agent tool,
not as a source of geometry: pages do not yield station coordinates, OpenStreetMap does.

**Cities.** One topic per city, rotating by `daily_cadence`, so adding a city is adding a topic:
Sydney, Melbourne, Singapore and Boston to start. Each site must sit inside one Sentinel-2 tile
(the search takes only scenes whose bounding box contains the whole site). Boxes that stay inside
one 100 km UTM square, computed with `vision/geo.py` and a 2 km margin:

| City | UTM | Box (lon, lat) | Note |
|---|---|---|---|
| Sydney | 56S | 150.86–151.56, −34.16 to −33.58 | the Botany Bay scene (56HLH) is already verified |
| Melbourne | 55S | 144.80–145.30, −37.90 to −37.55 | the CBD is 13 km above a grid line; the box reaches north and east |
| Singapore | 48N | 103.56–104.08, 1.09–1.61 | near the equator; most MRT is in tunnel, so corridor visibility is scored only on surface edges |
| Boston | 19N | −71.30 to −70.90, 42.15–42.42 | the CBD is 8 km below a grid line; the box reaches south and west |

## 3. Architecture

```
topic (rail_access; sites: [city polygon]; reach_m 1000; daily_cadence staggered per city)
   │ research tick (home region, 120 s)
   ▼
rail_access adapter ── Earth Search STAC: newest clear scene over the whole site
   │                 ── Overpass: ways, stations, intermodal points (cached 30 days in the running
   │                    state, decimated to 20 m, capped)
   │  request {task: "rail_access", assets: {red, nir, swir16, scl}, network: {...}, params}
   ▼ cross-region invoke
vision worker (us-west-2, arm64, OpenCV 5 + networkx)
   overviews at 20 m → masks → heat → distances → deserts → rasterise, thin, vectorise → graph
   → metrics (served_share, desert_km2, stations[], flags[], suggestions[]) and the figure
   ▲
   │ adapter: history and baseline → material? → triage agent (look_again, previous_scene,
   │          site_history, web_context; deadline-bounded; fails closed)
   ▼
Finding {summary, figures: [{key, caption, alt}]} → daily cycle → compliance and the
force_manual_review hold → a person approves → the article page shows the figure from
articles/figures/<article>/<n>.png with the Copernicus, AWS Open Data and OSM credits.
```

Design choices, each with its reason:

- **A 20 m analysis grid** (reference band `swir16`), so a 60 km city fits the worker's 4096 px
  window. The 10 m bands are read from the COG overview whose pixel size is 20 m; a file without
  that overview is read at full resolution and reduced with `INTER_AREA`.
- **OpenStreetMap is fetched by the adapter**, which already talks to STAC and runs where
  `requests` lives, and is passed inline to the worker. The worker keeps its "public imagery
  over HTTPS, nothing else" egress and stays stateless. Points are decimated to 20 m spacing and
  capped, so the synchronous invoke payload stays well under 6 MB.
- **networkx only in the worker** (`requirements-vision.txt`). Adapter modules must import without
  numpy or OpenCV, because the registry is imported by every pipeline Lambda.
- **The contract gains `task`** (`"ships"` by default; the version stays 1): per-task assets,
  parameters, extras and reply checks. The ship pipeline keeps working, which is the proof that
  the platform is topic-agnostic (rule 5).
- **A shared `common/vision_sites.py`**, extracted from `satellite_vision.py`: STAC search and
  scene choice, history, running state, figure store and load, the time budget, round-robin site
  order. Two thin adapters sit on it.
- **A generic figures path** from Finding to page, with no vision import in the pipeline.

## 4. Specifications, one per pull request

(Added per PR as each is reviewed. The letters are the branch suffixes:
`claude/vibrant-albattani-rjbsao-<letter>-<slug>`, every PR targeting the integration branch.)

Conventions for every PR: branch from the integration branch; Python 3.11 locally (CI's version);
`ruff check --fix lambdas/ scripts/` with the pinned 0.6.9 before each commit; the test files named
under each PR, then the full suite, green before pushing; no network in tests; adapter modules
import without numpy, OpenCV or networkx; the worker imports nothing from `common/` but
`vision_contract.py` (a test walks its imports); every float in a metric is rounded and every
parameter is echoed back. Signatures below are the contract between the PRs; names may improve,
shapes may not.

### PR 0 — integration branch (done on this branch)

- `origin/dev` + the stack tip + #240's docs, with the three append/append conflicts resolved as
  "dev's file, then the stack's appended hunk".
- Contract: `DEFAULT_LENGTH_M = (60.0, 600.0)` in `common/vision_contract.py`, checked against a
  lone `min_length_m` or `max_length_m`; `test_vision_worker.py` holds it equal to `DetectParams`.
- Triage deadline: `triage(..., deadline: float | None = None, clock=time.monotonic)`;
  `MIN_SECONDS_TO_ANSWER = 10`, `MIN_SECONDS_TO_LOOK_AGAIN = 30`; out of time is "artefact".
  The adapter passes `deadline = started + triage_deadline_seconds` (config key, default 85) and
  its own `clock`.
- Checkov on the artifacts bucket: `CKV_AWS_21` skipped with a reason (packages are keyed by MD5
  and expire); `CKV_AWS_300` met with a bucket-wide `abort_incomplete_multipart_upload` rule.

### Wave 1 (three PRs in parallel)

#### PR A — the rail vision core (`-a-rail-core`)

Title: "Vision core for rail access: built-up heat, station reach, rail network graph, figure;
COG overviews and the UTM inverse". New: `lambdas/vision/{urban,access,network,rail_analyse,rail_annotate}.py`,
`lambdas/tests/test_vision_rail_core.py`, `lambdas/tests/test_vision_network.py`. Modified:
`vision/geo.py`, `vision/cog.py`, `vision/annotate.py` (extract `burn_caption`), `vision/__init__.py`,
`tests/vision_fakes.py`, `tests/test_vision_cog_geo.py`, `requirements-vision.txt` and
`requirements-vision-cool.txt` (+ `networkx==3.4.2`, the `py3-none-any` wheel that serves 3.11, 3.12
and 3.13 alike), `tests/test_vision_terraform.py::test_the_requirements_pin_what_the_worker_needs`
(the set gains `networkx`), `scripts/tests/test_vision_benchmark.py` (the COOL requirements test
also expects `networkx==`).

`geo.py`: `utm_to_lonlat(easting, northing, zone, south) -> (lon, lat)` (Snyder's inverse series,
footpoint latitude from the meridional arc) and `pixel_to_lonlat(transform, epsg, x, y)` (pixel
centres). Test: round-trips of the existing PROJ fixtures to under 1e-7°.

`cog.py` overviews: factor `read_info` into `_parse_ifd(src, offset, order, big) -> (tags, next_offset)`;
follow the IFD chain (at most 8); every IFD whose `NewSubfileType` (254) has bit 1 set and bit 4
clear is a reduced image, kept as a `CogInfo` level with `transform = (c, a·f, 0, f0, 0, e·f)`,
`f = base.width / level.width`, epsg and nodata from the base, its own compression, predictor and
dtype (an unsupported level is skipped, never fails the base). New field
`CogInfo.levels: tuple[CogInfo, ...]`, coarsest last. `level_for_pixel_size(info, pixel_size_m,
tolerance=0.01) -> CogInfo | None`. `read_window` is unchanged: it takes a level's `CogInfo` and a
window in that level's pixels. `vision_fakes.write_tiff(..., overviews=(), mask_ifd=False)` writes
block-mean overviews (tag 254 = 1) and, when asked, a 1-bit mask IFD (254 = 4) the reader must skip.
Tests: scaled transforms and sizes (`ceil`), a window from an overview equals the block mean,
`level_for_pixel_size` picks the base, the level or `None`, mask and unreadable levels are skipped,
a file without overviews has no levels.

`urban.py`: `ndvi(red, nir)`, `ndbi(swir, nir)` (float32, 0 where the sum is 0, as `masks.ndwi`);
`built_up_mask(red, nir, swir, scl, ndbi_threshold=0.0, ndvi_max=0.3) -> (built, unusable, water)`
(uint8 0/255; `built = ndbi > t ∧ ndvi < v ∧ ¬water ∧ ¬unusable`, then a 3×3 `MORPH_OPEN`; water is
SCL 6; unusable is `masks.unusable_mask`); `heat_map(built, sigma_px, usable=None)`:
`cv2.GaussianBlur(built/255 as float32, (0, 0), sigma_px, borderType=BORDER_REPLICATE)`, clipped to
[0, 1], 0 where not usable. **No rescaling by a percentile**: the blurred mask is already the local
share of built-up ground, which is comparable from scene to scene.

`access.py`: `station_raster(shape, stations_px)`; `distance_to_stations(station_mask) ->
(dist_px float32, labels int32)` via `cv2.distanceTransformWithLabels(~mask, DIST_L2, 5,
labelType=DIST_LABEL_CCOMP)`; `label_of_station(labels, stations_px)`; `desert_mask(built, dist_px,
reach_px)`; `desert_clusters(desert, min_px, top=10)` via `connectedComponentsWithStats` (area desc,
`{"x", "y", "area_px", "bbox"}`); `catchment_activity(heat, labels, dist_px, reach_px,
station_labels)` = mean heat of each station's Voronoi cell clipped at reach (`np.bincount`);
`served_stats(built, dist_px, reach_px, pixel_km2) -> {built_up_km2, served_km2, desert_km2,
served_share}` (0 when nothing is built up); catchment activity is the mean over the cell's *usable*
pixels, with `catchment_coverage` (their share) reported per station, so a station under cloud is
said to be unmeasured rather than read as quiet; `intermodal_counts(stations_xy_m, pois_xy_m,
poi_kinds, near_m, far_m) -> list[dict]` in chunked numpy (`bus_stop_near`, `bus_station_near`,
`ferry_terminal_far`, `park_ride_far`).

`network.py` (networkx is imported here and in `rail_analyse.py`, nowhere else): `rasterise_ways(shape, ways_px, thickness=1)`
with `cv2.polylines(..., LINE_8)`; `thin(mask, max_iterations=32)`: sequential thinning with
`cv2.morphologyEx(img, MORPH_HITMISS, k)` over the two Golay L kernels in OpenCV's convention
(1 = foreground, -1 = background, 0 = don't care: `[[-1,-1,-1],[0,1,0],[1,1,1]]` and
`[[0,-1,-1],[1,1,-1],[0,1,0]]`) and their three rotations, each hit removed before the next kernel
(that order is what keeps connectivity), on the bounding box
of the nonzero pixels padded by one, until a pass removes nothing; `neighbour_count(skel01)` with
`cv2.filter2D` and the 3×3 ring kernel; `junctions_and_ends(skel01)` (count ≥ 3, count == 1);
`snap_stations(skel01, stations_px, snap_px) -> list[(x, y) | None]` via
`distanceTransformWithLabels(..., DIST_LABEL_PIXEL)`, whose label is the 1-based scan-order index
of the skeleton pixel (a test proves this on a known skeleton); `build_graph(skel01, stations,
snapped_px, pixel_size_m, visible01, tunnel_mask) -> (nx.Graph, info)`: node clusters =
`connectedComponentsWithStats(junctions ∪ ends ∪ snapped station pixels)` (a cluster holding a
station pixel is that station, id `s:<osm id>`; two stations in one cluster keep the first and
record `merged`; others `j<n>` / `e<n>` at the centroid); segments =
`connectedComponents(skel ∧ ¬dilate(nodes, 3×3))`; adjacency by the eight shifts between segment
and node labels (two nodes → an edge; one → a loop, warned; more → pairwise, warned); the skeleton
pixels the dilated zones swallow between two node clusters two or three pixels apart are
"bridges": labelled on their own, and every pair of nodes a bridge touches gets an edge too, so
close stations and junctions never split the graph; a repeated station id keeps the first and
warns, and no edge joins a node to itself; `length_m` from `cv2.arcLength(contour, True) / 2 +
3 · pixel_size_m` (the three pixels are what the node zones cut from each end); `corridor_visibility` = mean of a 3×3
`cv2.blur` of `visible01` over the segment, `None` where over half the segment is tunnel; edge
attributes `length_m, pixels, corridor_visibility, tunnel`; node attributes `kind, x, y, station`;
`info = {"warnings", "unsnapped", "components"}`. `station_adjacency(G)`: stations reachable through
non-station nodes only.

`rail_analyse.py`: `RailParams` (frozen dataclass, `ValueError` outside bounds, mirrors
`DetectParams`): `ndbi_threshold 0.0 [-0.5, 0.5]`, `ndvi_max 0.3 [0, 1]`, `reach_m 1000 [200, 5000]`,
`heat_sigma_m 500 [100, 3000]`, `snap_m 300 [20, 1000]`, `hub_count 5 [1, 20] whole`,
`intermodal_near_m 300 [50, 1000]`, `intermodal_far_m 500 [100, 2000] ≥ near`, `min_desert_km2 0.5
[0.01, 100]`, `visibility_ndvi_max 0.35 [0, 1]`, `max_orbital_km 5 [1, 20]`. `RailAnalysis(metrics,
layers)` (layers: heat, built, desert, unusable, water, skeleton, graph, node pixels, timings; never
sent). `project_network(network, epsg, transform, shape)`: ways to pixel arrays (tunnels apart),
stations and POIs to pixels and UTM metres. `analyse_rail_access(bands, polygon_px, pixel_size_m,
transform, epsg, network, params=None, coverage_floor=0.6) -> RailAnalysis`, in this order: site
mask → built-up → coverage (visible site pixels / site pixels) → heat (`sigma_px = heat_sigma_m /
pixel_size_m`) → project the network → station raster → distances → served stats, desert mask and
clusters (`min_px = min_desert_km2 / pixel_km2`; centroids to lon/lat; nearest station from the
labels) → catchment activity → rasterise ways (tunnels into a second mask) → thin → snap → graph →
`graph_metrics(G, activity, hub_count)` (degree; betweenness with `weight="length_m"`, normalised,
exact up to 800 nodes and sampled (`k=200, seed=0`) above, with the quality flag `graph_too_large`;
hubs = the top `hub_count` by betweenness among stations with activity at or above the median;
interchanges = degree ≥ 3; `d_hub_m` by `nx.multi_source_dijkstra` from the hubs, `None` when
unreachable; `isolation_weight = (d_hub_m / 1000) / max(activity, 0.05)`) → `intermodal_counts` →
`flag_stations` (the quartile flags only when the values spread, p75 > p25: `isolated_high_demand`:
activity ≥ p75 ∧ degree ≤ 2 ∧ W ≥ p75; `single_point_of_failure`: top betweenness ≥ 2 × second, with
`ratio` null when the second is 0 (a hub with only leaves), which the adapter formats as such;
`ghost_line`: a chain of ≥ 3 consecutive stations, each activity ≤ p25) → `suggest` (`orbital_link`: pairs with both `d_hub` ≥ p75,
straight-line ≤ `max_orbital_km`, not adjacent; up to 12 candidates by `W_a·W_b`; each simulated on a
copy with an edge of 1.2 × the straight line; ranked by `delta_mean_d_hub_m` from Dijkstra alone,
the top 3 kept (a candidate that reduces nothing is dropped, so fewer may be reported) and only
those get `delta_top_hub_betweenness`, so betweenness runs four times, not thirteen; `feeder_corridor`: the three
highest-W stations to their nearest hub). Quality flags: `empty_site`, `low_coverage`, `no_network`,
`few_stations_snapped` (under half), `graph_fragmented` (components > max(3, stations / 10)).

Metrics (exact keys; floats to 4 dp, coordinates to 5 dp; `stations` ≤ 2000, `edges` ≤ 5000 kept
by betweenness with a `truncated` count, `deserts` top 10; a network element whose coordinates
are not finite or not on Earth is skipped with a warning, never a crash):
```json
{"task": "rail_access", "coverage": 0.93, "built_up_km2": 412.3, "served_km2": 301.0, "desert_km2": 111.3,
 "served_share": 0.73, "station_count": 178, "snapped_station_count": 171, "node_count": 240,
 "edge_count": 262, "components": 2, "hubs": ["s:n123"], "interchanges": ["s:n123"],
 "flags": [{"type": "isolated_high_demand", "station": "s:n9", "name": "X", "activity": 0.8, "degree": 1,
            "isolation_weight": 6.1},
           {"type": "single_point_of_failure", "station": "s:n1", "name": "Central", "ratio": 2.4},
           {"type": "ghost_line", "stations": ["s:n4", "s:n5", "s:n6"], "names": ["..."]}],
 "suggestions": [{"type": "orbital_link", "from": "s:n7", "to": "s:n12", "from_name": "...", "to_name": "...",
                  "straight_km": 3.1, "delta_top_hub_betweenness": -0.08, "delta_mean_d_hub_m": -410.0},
                 {"type": "feeder_corridor", "station": "s:n9", "name": "...", "hub": "s:n1", "hub_name": "Central",
                  "length_m": 5200.0}],
 "stations": [{"id": "s:n123", "name": "...", "kind": "station", "lon": 151.2, "lat": -33.87, "snapped": true,
               "degree": 3, "betweenness": 0.31, "activity": 0.72, "d_hub_m": 0.0, "isolation_weight": 0.0,
               "intermodal": {"bus_stop_near": 4, "bus_station_near": 1, "ferry_terminal_far": 0, "park_ride_far": 0}}],
 "deserts": [{"lon": 150.98, "lat": -33.9, "area_km2": 6.2, "nearest_station": "s:n44", "nearest_station_m": 2300.0}],
 (both null when no station lies inside the window; never an infinity: the reply must satisfy json.dumps(allow_nan=False))
 "edges": [{"from": "s:n1", "to": "j3", "length_m": 1840.0, "betweenness": 0.1, "corridor_visibility": 0.62, "tunnel": false}],
 "quality_flags": [], "warnings": [],
 "network": {"ways": 410, "stations": 178, "pois": 900, "points": 23000, "fetched_at": "..."},
 "params": {"pixel_size_m": 20.0, "coverage_floor": 0.6, "...every RailParams field": "..."}}
```

`rail_annotate.py`: `annotate_rail(analysis, red, year, max_side=1024) -> bytes`: `detect.stretch(red,
3000)` to BGR, `cv2.applyColorMap(heat·255, COLORMAP_INFERNO)` blended with `addWeighted(0.45,
0.55)` where usable, `annotate.UNMEASURED_TINT` elsewhere, `resize(INTER_AREA)` to the cap, outlines
of the deserts the metrics report (those of at least `min_desert_km2`) from `findContours` +
`drawContours`, edges as `cv2.line` with thickness
`1 + round(3·betweenness/max)`, stations as circles of radius `2 + round(4·activity)`, hubs with
`drawMarker(MARKER_DIAMOND)`, and the caption lines `"Processed imagery - BloggerBear"`,
`"Contains modified Copernicus Sentinel data {year}"`, `"Map data (c) OpenStreetMap contributors"`
burned with `annotate.burn_caption` (Hershey fonts have no ©; the HTML caption does).

Tests, on a synthetic 400×400 scene at 20 m (`transform=(300000, 20, 0, 6300000, 0, -20)`,
`epsg=32756`): a `city()` helper (vegetated background, a built district, a lake as SCL 6, a cloud
as SCL 9) and a `grid_network()` helper (two crossing lines and a branch, seven stations, POIs near
two of them, one tunnel way; lon/lat produced from pixels with `geo.utm_to_lonlat`, so projection
round-trips are exercised). Cases: built-up excludes water, cloud and vegetation; heat is in [0, 1]
and peaks in the district; distance is 0 at a station and `served_share` in range; desert clusters
are built-up beyond reach with lon/lat centroids; catchment activity is the mean heat of a clipped
Voronoi cell; intermodal counts at both radii; thinning reduces a 3 px line to 1 px and keeps it
connected, converges and is idempotent; junctions and ends on a cross; `DIST_LABEL_PIXEL` indexes
skeleton pixels in scan order; stations snap within `snap_px` and not beyond; nodes, edges and
lengths on a cross with a branch (a diagonal within 10 % of √2·n·20 m); two stations in one cluster
merge with a warning; corridor visibility ignores tunnels and vegetation; betweenness, hubs and
`d_hub` on a star; the three flags; orbital links lower mean `d_hub` and are deterministic across
two runs; feeder corridors target the nearest hub; the analysis is JSON and echoes its params; low
coverage and no network are flagged, not crashes; bad params are refused; the figure is a PNG
within the cap with a caption bar. A `VISION_PERF`-gated test prints `thin()` on a 3000² sparse mask
(target: under 2 s). About 1,000 lines of code and 700 of tests.

#### PR B — the OpenStreetMap client (`-b-osm`)

Title: "OpenStreetMap Overpass client for rail networks: fetch, decimate, cap, cache rules". New:
`lambdas/common/osm.py`, `lambdas/tests/test_osm.py`. Imports only `math`, `re`, `json`,
`urllib.parse`, `datetime`, `requests`; a test reloads the module and asserts that neither numpy,
cv2 nor networkx entered `sys.modules`.

```python
OVERPASS_ENDPOINTS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
TIMEOUT_SECONDS = 25.0; MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_POINTS = 60_000; MAX_WAYS = 5_000; MAX_STATIONS = 2_000; MAX_POIS = 5_000; MIN_SPACING_M = 20.0; MAX_NAME_CHARS = 80
OSM_SOURCE = {"text": "Map data © OpenStreetMap contributors, available under the Open Database Licence",
              "label": "OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"}
class OsmError(RuntimeError): code  # "http" | "rate_limited" | "timeout" | "bad_response" | "too_large"
def overpass_query(polygon) -> str
def query_url(polygon, endpoint=OVERPASS_ENDPOINTS[0]) -> str          # bbox form, ≤ 400 chars, for source_refs
def fetch_network(polygon, *, http_post=None, endpoints=OVERPASS_ENDPOINTS, timeout=TIMEOUT_SECONDS, now=None) -> dict
def parse_elements(elements) -> dict
def decimate(points, min_spacing_m=MIN_SPACING_M) -> list      # keeps the first and last point; equirectangular metres
def cap_network(network, max_points=MAX_POINTS) -> dict         # spacing ×1.5 up to four times, then drop the longest ways; "truncated": true
def is_stale(network, now, ttl_days) -> bool
def clean_name(raw) -> str | None                               # control characters out, whitespace collapsed, MAX_NAME_CHARS
```
The query (exact; `poly` is `"lat lon lat lon …"`):
```
[out:json][timeout:25];
( way["railway"~"^(rail|light_rail|subway)$"]["service"!~"^(yard|siding|spur|crossover)$"]
     ["usage"!~"^(industrial|military|tourism)$"](poly:"{poly}"); ) -> .rail;
( node["railway"~"^(station|halt)$"](poly:"{poly}"); way["railway"~"^(station|halt)$"](poly:"{poly}"); ) -> .stations;
( node["amenity"="bus_station"](poly:"{poly}"); way["amenity"="bus_station"](poly:"{poly}");
  node["amenity"="ferry_terminal"](poly:"{poly}"); way["amenity"="ferry_terminal"](poly:"{poly}");
  node["park_ride"](poly:"{poly}"); way["park_ride"](poly:"{poly}");
  node["highway"="bus_stop"](around.stations:400); ) -> .pois;
.rail out geom; .stations out center; .pois out center;
```
Tram ways are not fetched, so tram stops are not stations either: a station element tagged
`station=tram` is dropped in `parse_elements`. One `requests.post(endpoint, data={"data": query},
timeout=(5, timeout))`; on 429, 504, any 5xx, a timeout or a connection error the next endpoint is
tried once; when all fail, `OsmError` with the last code; non-JSON or no `elements` is
`bad_response`; a body over `MAX_RESPONSE_BYTES` is `too_large`.

The network dict (what the adapter caches and the worker receives):
```json
{"source": "overpass", "endpoint": "https://overpass-api.de/api/interpreter", "fetched_at": "2026-10-10T03:00:00+00:00",
 "query_url": "...",
 "ways": [{"id": "w123", "points": [[lon, lat]], "tunnel": false, "name": "Illawarra Line"}],
 "stations": [{"id": "n456", "name": "Redfern", "lon": 151.198, "lat": -33.892, "kind": "station"}],
 "pois": [{"id": "n789", "lon": 151.2, "lat": -33.89, "kind": "bus_stop"}],
 "counts": {"ways": 410, "stations": 178, "pois": 900, "points": 23000}, "truncated": false}
```
`tunnel` is true for `tunnel=yes|building_passage`; `kind` comes from `railway`; a nameless station
is `"Unnamed station (<id>)"`; POI `kind` from `amenity`, `park_ride` or `highway=bus_stop`; way
elements use their `center`. Tests: the query selects rail, stations and the POI classes; the poly
string is closed lat-lon pairs; parsing builds ways, stations and POIs with tunnels and names, and
drops tram stations; decimation drops close points and keeps the ends; capping reduces spacing then
drops the longest ways and says so; fallback to the mirror on 429, timeout and 5xx; a coded error
when every endpoint fails; oversized and malformed bodies refused; names sanitised and capped;
staleness by TTL and when there is no network; the query URL is short and on an allow-listed
endpoint. About 230 lines of code and 260 of tests. No wiring beyond the file itself.

#### PR C — figures from Findings to articles (`-c-figures`)

Title: "Figures: adapters attach PNGs to Findings; articles show them on the static page and in
the SPA". New: `lambdas/common/figures.py`, `lambdas/tests/test_figures.py`,
`lambdas/tests/test_frontend_figures.py`. Modified: `common/adapters/base.py`, `common/dynamo.py`,
`research_tick_handler.py`, `daily_cycle_handler.py`, `common/static_pages.py`, `common/rewrite.py`,
`admin_api_handler.py`, `public_api_handler.py`, `frontend/app.js`, `frontend/styles.css`, and their
tests. (`trending_digest_handler.py` is left alone: its one render is a fresh digest with no article
dict and no findings; a held digest that is approved is re-rendered by the admin API, which passes
figures.)

```python
# common/adapters/base.py
def figures(self, new_state: dict) -> list[dict]:   # [{"key": <content-bucket PNG key>, "caption": str, "alt": str}]; default []
# common/figures.py (no boto)
MAX_FIGURES_PER_ARTICLE = 3; MAX_CAPTION_CHARS = 300; MAX_ALT_CHARS = 200
_KEY = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9_./-]{1,200}\.png$")
def clean_figures(figures) -> list[dict]                       # drops malformed entries; keeps the three fields
def figures_for_findings(findings) -> list[dict]               # newest first, de-duplicated by key, capped
def figure_key(article_id, index) -> str                       # f"articles/figures/{article_id}/{index}.png", index from 1
def public_figures(article_id, figures) -> list[dict]          # [{"src": "/" + figure_key(...), "caption", "alt"}]
# common/dynamo.py
put_finding(..., research_call=None, figures=None)             # stored only when non-empty
put_article(..., attribution=None, figures=None)               # stored only when non-empty
# common/static_pages.py
def publish_figures(article_id, figures) -> list[dict]         # s3.copy_object content → site articles/figures/<id>/<n>.png,
                                                                #   ContentType image/png, MetadataDirective REPLACE, CacheControl public, max-age=86400;
                                                                #   a failed copy is printed and skipped; returns public_figures of those copied
def remove_article_figures(article_id, count) -> None           # delete_object for 1..count (the role has no ListBucket); errors ignored per key
def remove_article_page(article_id, figure_count=0) -> str     # also removes the figures
def invalidate_article_page(article_id) -> bool                # Items: the page and f"/articles/figures/{article_id}/*"
render_and_publish_article_page(..., attribution=None, figures=None)
```
The page gets, between the attribution and the body, one
`<figure class="article-figure"><img src="/articles/figures/<id>/1.png" alt="…" loading="lazy" /><figcaption>…</figcaption></figure>`
per figure, every value escaped, no `style=` (the CSP forbids inline styles; a test asserts it).
The daily cycle's `_publish_or_moderate` collects `figures_for_findings(findings)` and passes them to
`put_article` and the renderer; the research tick stores `adapter.figures(new_state)` on the Finding;
the admin API's `_render_published_page` and the rewrite path pass `article.get("figures")`; unpublish and take-down call `remove_article_page(article_id,
figure_count=len(article.get("figures") or []))`; the admin `_get_article` returns `figures`; the
public article detail returns `public_figures(...)` (always present, possibly empty). The SPA's
article view appends, after the credit line, a `figure.article-figure` with the image
(`src`, `alt`, `loading="lazy"`) and a `figcaption`, and ignores any `src` outside
`/articles/figures/`. `styles.css`: `.article-figure { margin: 0 0 1.5em }`, the image
`display: block; max-width: 100%; height: auto`, the caption in the muted colour at 0.85rem.
CopyObject needs `s3:GetObject` on the content bucket and `s3:PutObject` under `articles/` in the
site bucket; the role has both, and the CSP already allows `img-src 'self'`. No Terraform.

Tests: `test_figures.py` (the key regex rejects `../`, non-PNG and long captions; de-duplication,
cap and order); `test_static_pages.py` (moto: a PNG in the content bucket is copied to
`articles/figures/a1/1.png` with `ContentType image/png` and the page shows the figure with an
escaped caption and no `style=`; a missing source is skipped; removal deletes the figures it knows;
invalidation covers the figure prefix; no figures, no `<figure>`); `test_daily_cycle_handler.py`
(figures from the window's findings reach the article and the page, capped and de-duplicated);
`test_research_tick_handler.py` (an adapter's figures are stored on the Finding; none by default);
`test_public_api_handler.py` (public figure URLs); `test_admin_api_handler.py` (unpublish removes
them; a re-render re-copies them); `test_rewrite.py` (take-down removes them);
`test_frontend_figures.py` (Node, reusing `test_frontend_attribution`'s harness: the figure element
with its attributes; a foreign `src` is ignored). About 190 lines of code and 380 of tests.

### Wave 2

#### PR D — worker tasks (`-d-worker-tasks`, after A)

Title: "Vision worker tasks: `rail_access` beside `ships` — contract, dispatch, overview reads,
benchmark kernels". Modified: `common/vision_contract.py`, `common/vision_client.py`,
`vision_worker_handler.py`, `vision/scene.py`, `scripts/vision_benchmark.py`,
`scripts/vision_benchmark_scenes.json`, `scripts/README.md`, and `tests/test_vision_worker.py`,
`tests/test_vision_core.py`, `scripts/tests/test_vision_benchmark.py`.

Contract (`VERSION` stays 1; `task` defaults to `"ships"`):
```python
@dataclass(frozen=True)
class TaskSpec: assets_required; assets_optional; param_limits; whole_params; extras; reply_metrics
RAIL_PARAM_LIMITS = {"ndbi_threshold": (-0.5, 0.5), "ndvi_max": (0.0, 1.0), "reach_m": (200.0, 5000.0),
                     "heat_sigma_m": (100.0, 3000.0), "snap_m": (20.0, 1000.0), "hub_count": (1, 20),
                     "intermodal_near_m": (50.0, 1000.0), "intermodal_far_m": (100.0, 2000.0),
                     "min_desert_km2": (0.01, 100.0), "visibility_ndvi_max": (0.0, 1.0), "max_orbital_km": (1.0, 20.0)}
TASKS = {"ships": TaskSpec(("nir", "green"), ("scl",), PARAM_LIMITS, WHOLE_NUMBER_PARAMS, (), ("count", "coverage", "quality_flags")),
         "rail_access": TaskSpec(("red", "nir", "swir16", "scl"), (), RAIL_PARAM_LIMITS, ("hub_count",), ("network",),
                                 ("coverage", "station_count", "served_share", "quality_flags"))}
NETWORK_LIMITS = {"points": 60_000, "ways": 5_000, "stations": 2_000, "pois": 5_000, "name_chars": 80}
def validate_network(network) -> dict     # the PR B shape; lon/lat ranges as for polygons; ids to str; names sanitised; caps → ContractError
```
`validate_request` reads `task` (unknown → `ContractError`), requires the task's assets and allows
only required + optional names, checks params against the task's limits (`intermodal_far_m ≥
intermodal_near_m`; the ship length check stays ships-only), requires and validates `network` for
rail, and returns `task` (and `network`) in the normalised request. `validate_reply` checks the
task's `reply_metrics` (whole numbers for counts, [0, 1] for shares and coverage, a list for
`quality_flags`). `vision_client.measure(..., task="ships", network=None, read_timeout=None)` sends
both and checks `reply.get("task", "ships") == task`; `_client(region, read_timeout=None)` caches
per `(region, read_timeout)`; the triage agent's `look_again` will pass a shorter read timeout.

Handler: `_measure` dispatches on `request["task"]`; `_measure_rail` reads with
`reference="swir16"`, folds `swir16` nodata into SCL 0 as the ship path does for `nir`, builds
`RailParams(**request["params"])`, calls `analyse_rail_access(site.bands, [site.polygon_px],
site.pixel_size_m, site.transform, site.epsg, request["network"], params, coverage_floor)` and
`annotate_rail(analysis, site.bands["red"], year=captured_at[:4])` when `image` is set. The reply
carries `"task"` for both tasks and `timings_ms` gains `"graph"`. A `ValueError` from a params
dataclass becomes `bad_request`: the clause goes after the `cog.CogError` clause, since `CogError`
is a `ValueError`.

`scene.py`: `read_site(..., reference="nir", max_window_px=None, categorical=("scl",))`. For each
non-reference band, `_read_aligned` first asks `cog.level_for_pixel_size(info, target pixel size)`;
with a level it reads the window from that level (no warp when the residual shift is under 1e-3
px); without one, a non-categorical band whose pixel is a whole fraction of the target's is read at
full resolution and reduced with `cv2.resize(INTER_AREA)`; otherwise the existing nearest warp.
`SiteBands.levels_used: dict[str, int]` records the level per band.

Benchmark: `kernels()` adds `distance_transform_l2`, `thinning_hitmiss` (on a synthetic rail
raster), `polylines`, `apply_colormap`, `connected_components_stats_desert` and the whole
`analyse_rail_access` on `synthetic_city(size)` + `synthetic_network(size)` (a grid of lines, 40
stations, bus stops). `request_for(scene, backend)` adds `task` and, for rail, the network from
`scene["network_file"]` (relative to the scenes file; a `fetch-network --scene NAME --out FILE`
subcommand uses `common.osm.fetch_network`; a missing file makes `scenes` mode report that row as
`error: "network_file missing"`). The scenes file gains a `sydney-rail-2024-01-05` entry (task
`rail_access`, the Sydney box from §2, scene `S2A_56HLH_20240105_0_L2A`, assets B04, B08, B11 and
SCL under the existing prefix, `network_file: vision_benchmark_networks/sydney.json`). The network
file is committed only if it can be fetched (Overpass is unreachable from the build container).

Tests: a `rail_files` fixture (512² 10 m `red` and `nir` written with `overviews=(2,)`, 256² 20 m
`swir16` and `scl`, a district and a lake; a network built from pixels with `geo.utm_to_lonlat`);
the rail task measures the city (`task`, metric keys, `station_count`, `served_share` in range, a
PNG); the 10 m bands are read from their overview (the fetch log's ranges fall inside the overview
tiles; `io.bytes` below the full-resolution size; `levels_used == {"red": 1, "nir": 1, "swir16": 0,
"scl": 0}`); area resampling without overviews; rail requires its assets and a network (missing
`swir16`, no `network`, 60,001 points, a station at latitude 95 → `bad_request`); rail param
bounds; reply validation per task; the client passes task and network and checks the task in the
reply; a ships request without `task` still works and replies `ships`; the benchmark times every
kernel the worker uses; pinned scenes are valid requests (a stub network when the file is absent);
scenes mode reports a missing network file. About 420 lines of code and 380 of tests. The contract
still imports nothing from the pipeline; no Terraform change (2048 MB is enough: a 3000² city peaks
near 250 MB).

### Wave 3

#### PR E — the `rail_access` adapter, shared `vision_sites`, task-aware triage (`-e-rail-adapter`, after B, C, D)

Title: "rail_access adapter: OSM-backed rail network monitoring with task-aware triage". New:
`lambdas/common/vision_sites.py`, `lambdas/common/adapters/rail_access.py`,
`lambdas/tests/test_vision_sites.py`, `lambdas/tests/test_rail_access_adapter.py`. Modified:
`common/adapters/satellite_vision.py` (thin; re-exports `STAC_SEARCH_URL`, `ConfigError`, `plain`,
`parse_config`, `search_newest_scene`, `clear_entries` so its tests pass unchanged),
`common/adapters/__init__.py` (`RAIL_ACCESS_ADAPTER_KEY = "rail_access"`, `MANUAL_REVIEW_ADAPTERS`),
`common/adapters/registry.py`, `common/vision_triage.py`, `admin_api_handler.py` (`force_manual_review`
forced for every adapter in `MANUAL_REVIEW_ADAPTERS`, on create and update), `ops_mcp/cli_guide.py`,
`frontend/about.html`, `tests/test_research_tick_handler.py` (the exact registry dict),
`tests/test_admin_api_handler.py`, `tests/test_satellite_vision_adapter.py`, `tests/test_vision_triage.py`.

`vision_sites.py` (moved from `satellite_vision.py`, names kept): the STAC constants and the two
imagery sources, `ConfigError`, `plain`, `parse_site_config(adapter_config, defaults, whole, floats,
max_sites=10)` (shared checks; refuses `time_budget_seconds > 80`), `site_bbox`,
`search_newest_scene(site, config, now, http_post=None, assets=ASSETS_SHIPS)`, `store_image`,
`load_image`, `clear_entries(history, floor, value_key="count")`, `class Budget(clock, seconds)` with
`elapsed()`, `remaining()`, `exhausted()`, `stac_refs(measured, fetched_at)`, and
`order_sites(sites, records)`: the sites a tick visits, those never measured first, then by the
oldest `last_attempt_at`, so a topic with several sites rotates through them (both adapters use it;
record `last_attempt_at` on every attempt).

`rail_access.py`:
```python
ASSETS_RAIL = {"red": "red", "nir": "nir", "swir16": "swir16", "scl": "scl"}
DEFAULTS = {"backend": "opencv", "params": {}, "reach_m": 1000, "osm_ttl_days": 30, "max_cloud_cover": 40,
            "lookback_days": 20, "coverage_floor": 0.6, "history_size": 6, "min_baseline": 1,
            "served_share_threshold": 0.02, "desert_relative_threshold": 0.10, "max_sites_per_tick": 1,
            "time_budget_seconds": 70, "triage": True, "triage_max_tool_calls": 3, "triage_deadline_seconds": 85,
            "web_context": True}
THRESHOLD_KEYS = ("coverage_floor", "min_baseline", "served_share_threshold", "desert_relative_threshold")
OSM_RESERVE_SECONDS = 30.0; MEASURE_RESERVE_SECONDS = 45.0; WORKER_READ_TIMEOUT = 58.0
class RailAccessAdapter(Adapter):
    uses_previous_state = True; keeps_running_state = True
    sources = (COPERNICUS_SOURCE, AWS_OPEN_DATA_SOURCE, osm.OSM_SOURCE)
    http_post = None; measure = staticmethod(vision_client.measure); fetch_network = staticmethod(osm.fetch_network)
    store_image / load_image / triage_agent / choose_model / clock as satellite_vision; web_search = staticmethod(search_web)
    fetch_state(topic_config, previous_state=None); assess(new_state); material_diff(old, new); figures(new_state)
    source_refs(new_state); review_evidence(topic_config, latest_state); build_summary_prompt(topic, diff_summary, new_state)
```
Per site, in `order_sites` order and within `Budget`: (1) `network = record.get("network")`; when
`osm.is_stale(...)` and at least `OSM_RESERVE_SECONDS` remain, `fetch_network(polygon,
http_post=self.http_post)`; an `OsmError` keeps the cached network and records `network_error`
(`at`, `code`, `detail`); no network at all → `last_error {"code": "osm"}` and the site is skipped.
(2) The STAC search with `ASSETS_RAIL`; a known scene is nothing new. (3) Under
`MEASURE_RESERVE_SECONDS` left → `last_error time_budget`, next tick. (4) `measure(site, scene,
backend, params={"reach_m": config["reach_m"], **config["params"]}, coverage_floor,
task="rail_access", network={ways, stations, pois, fetched_at}, read_timeout=WORKER_READ_TIMEOUT)`.
History entries (at most `history_size`): `scene_id, captured_at, scene_cloud_cover, coverage,
built_up_km2, served_km2, desert_km2, served_share, station_count, edge_count, hubs, flags,
suggestions, deserts, stations` (id, name, lon, lat, degree, betweenness, activity, d_hub_m,
isolation_weight, intermodal), `quality_flags, backend, build_sha256, timings_ms, image_key, assets,
network_fetched_at`. State: `{"fetched_at", "task": "rail_access", "thresholds", "sites": {id:
{"name", "network", "network_error"?, "history", "last_error"?, "last_attempt_at"}}, "measured"}`.

`assess`: the latest entry against `clear_entries(earlier, floor, value_key="served_share")`;
material when coverage ≥ floor, at least `min_baseline` clear earlier entries, and `|Δ
served_share| ≥ served_share_threshold`, or `|Δ desert_km2| ≥ desert_relative_threshold ×
max(median desert, 1)`, or the station count differs from the previous entry's; reasons "served
share moved", "desert area moved", "station count changed", "coverage below the floor", "baseline
still building", "within the usual range". The first tick is material: "First observation.
<name>: served share 73 %, 178 stations, 111 km² beyond 1 km of a station (coverage 93 %)".
`figures`: one per measured site with an `image_key`; caption "Rail access heat map of {name} from
the Sentinel-2 scene captured {date}: built-up density (warm colours), areas more than {reach} m
from a station (outlined), rail lines and stations from OpenStreetMap. Processed imagery; contains
modified Copernicus Sentinel data {year}; map data © OpenStreetMap contributors."; alt "Processed
satellite map of {name} showing rail lines, stations and built-up areas beyond walking reach of a
station". `source_refs`: the STAC item plus `{"url": osm.query_url(polygon), "title":
"OpenStreetMap rail data for {name} via the Overpass API", "accessed_at": network["fetched_at"]}`.
`build_summary_prompt` (verbatim rules): "You are summarising a change measured in satellite
imagery and OpenStreetMap data for the topic "{name}".\n\nWhat was measured: {diff_summary}\n\nThe
figures are proxies: built-up density from Sentinel-2 reflectance at 20 m, walking reach as
straight-line distance to the nearest station, and network measures from OpenStreetMap's rail
lines; none is ridership, patronage or journey time. Write 2-4 plain sentences describing only
these observations: station and line names exactly as given, the served share, the built-up area
beyond reach, the capture date, and what was not measured (ridership, service frequency, planned
works). Give no cause, forecast, political, planning or investment view; do not say any area is
well or badly served in absolute terms; do not invent place names or numbers that are not given
above." `review_evidence`: per site name, scene, date, served share, desert km², station count,
flag count and the previous served shares; never the network. `_triage`: `deadline = started +
triage_deadline_seconds`, `triage(..., task="rail_access", network=record["network"],
web_search=self.web_search if config["web_context"] else None, deadline=..., clock=self.clock,
read_timeout=WORKER_READ_TIMEOUT)`.

`vision_triage.py` task profiles: `TaskProfile(task, system_prompt, tools, metric_keys,
look_again_limits, web_query)`; `PROFILES = {"ships": ..., "rail_access": ...}`; `SYSTEM_PROMPT` and
`TOOLS` stay as module constants (existing tests assert them); `triage(*, ..., task="ships",
network=None, web_search=None, deadline=None, clock=time.monotonic, read_timeout=None)`. The rail
system prompt: "You check changes measured by an automated satellite and map pipeline before they
are reported. OpenCV estimated built-up density from a Sentinel-2 scene (20 m pixels) and combined
it with OpenStreetMap's rail lines and stations; the share of built-up area within walking reach of
a station, or the station count, moved against the site's usual level. Decide whether that change
is REAL (the city or its mapped network changed) or an ARTEFACT (cloud, haze or seasonal vegetation
shifting the built-up mask; a scene edge; a map edit; a tagging change). Look at the figure: warm
colours are built-up density, outlines are areas beyond reach, lines and dots are the mapped
network; shaded areas were not measured. Use the tools when they would settle it: web_context says
whether a station or line recently opened or closed. Name no person or company, and do not
speculate about causes beyond what the tools return. When you are not sure, answer "artefact".
Answer with only a JSON object: {"verdict": "real" or "artefact", "reason": "one or two
sentences"}." Rail tools: `look_again` (schema from `RAIL_PARAM_LIMITS`; passes the task, the
network and the read timeout), `previous_scene`, `site_history`, and `web_context` (no input;
`web_search(profile.web_query(site name), max_results=5, max_age_hours=24·60, deadline=min(deadline,
clock() + 10))` → `[{"json": {"results": [{title, url, published_at, source, snippet}]}}]`; any
exception → an error result "News search failed (<type>)"). The rail `web_query` is
`"{name} rail station opening closure line"`.

`about.html` gains the OpenStreetMap credit sentence, verbatim as `OSM_SOURCE["text"]` with the
label linked to the copyright page (use the literal © character, as the attribution test compares
text). `cli_guide.ADAPTERS["rail_access"]`: "maps rail access from satellite imagery and
OpenStreetMap for the cities in its config".

Tests (`test_rail_access_adapter.py`, with `FakeStac`, `FakeOverpass(network | OsmError)`,
`FakeRailWorker`, `FakeAgent`, `FakeSearch` modelled on the ship adapter's tests): registered,
credited and forced to manual review; imports without cv2, numpy or networkx; bad config refused
(no sites, budget over 80, reach outside bounds); the first tick fetches the network then measures
when time allows; a fresh network is reused and refetched after the TTL; an Overpass failure keeps
the cached network and records it; no network means no measurement and a `last_error`; the worker
request carries task, network and reach; measuring is deferred when the budget is nearly spent;
sites rotate (two sites, one per tick, the never-measured first); history entries keep the metrics
and cap the station list; the first tick is material with a plain summary; the served-share,
desert and station-count rules (parametrised); low coverage is never material; figures name the
scene year and OSM; source refs link the scene and the Overpass query; review evidence omits the
network; the summary prompt keeps to proxies; triage gets the rail task, the network and a
deadline; a research-tick running-state round trip with moto. `test_vision_triage.py`: the rail
profile's tools and prompt; `web_context` searches the site and returns titles; its failure is an
error result the model can work around; it is absent when disabled; rail `look_again` uses the rail
limits, task and network. `test_vision_sites.py`: the moved config, search and `clear_entries`
tests plus `order_sites`. About 1,250 lines in all.

### Wave 4

#### PR G — the deployment switch (`-g-deploy-switch`, independent)

Title: "Deploy: the vision worker is opt-in through a repository variable". Deploying the vision
work (worker, bucket, invoke grant) stays optional and off by default. The Terraform variable
`vision_enabled` (dev and production roots, default `false`) is fed from a GitHub repository
variable: `TF_VAR_vision_enabled: ${{ vars.VISION_ENABLED || 'false' }}` in the `apply-dev` job's
`env` of `.github/workflows/terraform.yml` and in the production release workflow's `env`, beside
the other `TF_VAR_*` lines with the same comment style. `docs/configuration.md` gains the row in
the GitHub variables table (`VISION_ENABLED` · variable · repo · "Set to `true` to deploy the
vision worker; the bootstrap must have been re-applied with `vision_region` first; unset or
anything else means off" · `true`) and a short "Vision" subsection under the environment
Terraform variables (`vision_enabled`, `vision_region`), and `docs/deployment-runsheet.md`'s vision
section says the switch is the variable, not a tfvars edit. `test_terraform_wiring.py` already
requires every `vars.X` a workflow reads to be named in `configuration.md`; add one test that both
apply workflows pass `TF_VAR_vision_enabled` from `vars.VISION_ENABLED` with a `false` default.
Nothing else changes: the research tick already reads an empty `VISION_WORKER_ARN` as "not
configured" and the adapters report `last_error` rather than fail.

#### PR F — documentation and the city templates (`-f-docs`, after E)

Title: "Docs: Rail Access Monitor — vision.md for two tasks, city topics, progress, friction,
submission draft". Files: `docs/enhancements/vision.md` (§1 one paragraph for both tasks; §3 the PR
table gains PR 0 and A–F; §4 the new modules; the contract section gains `task`, the rail assets,
`network`, the rail params table and the rail reply keys; §5 a `rail_access` topic table with every
default; "Where things are stored" gains `articles/figures/<article>/<n>.png`; §6 a 6.7 "Rail
access: what is measured and what is not"; §7 whatever was measured on Sydney; §8 and §10 the
thinning, overview and 120 s budget lessons); `docs/deployment-runsheet.md` (a "rail_access topics"
section: creating the four city topics from `scripts/rail_topics.example.json` with the admin CLI,
staggering `daily_cadence` so each day features one city, what the first tick does, where the figure
and state land, how to read the triage trail, and "Adding a city": a box inside one Sentinel-2 tile,
checked with `vision/geo.py` as in §2); `scripts/rail_topics.example.json` (the four cities of §2 with
`reach_m` and a staggered cadence each); `scripts/README.md` (the `fetch-network` subcommand);
`docs/PROGRESS.md` (two `- [x]` lines under "Enhancements since Phase 8", citing the PRs);
`docs/friction.md` (entries for: no `ximgproc` in the headless wheel; overview reads to fit a city;
the 120 s tick against Overpass, worker and agent; merging the stack onto a moved dev);
`docs/architecture/article-research.md` and the README's data-sources table (OpenStreetMap);
`docs/configuration.md` (one sentence: no new secret or variable; Overpass and Earth Search need no
key); `docs/configuration.md` also gains a "Vision and rail access" section (the switch from PR G, the
worker's environment variables, the `rail_access` topic keys and what needs no key); `README.md` gains
a section "Vision: satellite imagery and the Rail Access Monitor" (what it is, the two tasks, the
opt-in deployment, links to the docs) and the OpenStreetMap row in its data-sources table;
`docs/architecture/blogger-vision.md` (new, linked from `docs/architecture/README.md`'s index) is the
architecture-by-feature page: the vision pipeline end to end (sources → adapter → worker → agent →
person → page) and a plain explainer of the rail concepts (built-up heat as a proxy, walking reach
and transit deserts, the station graph, hubs, betweenness and isolation weight, what the flags and
suggestions mean and do not mean); the deployment sheets (`deployment-runsheet.md`,
`deployment-separate-accounts.md`, `production-runsheet.md`) each gain a short "Vision (optional)"
section: off by default, turned on per environment with the repository variable, the bootstrap
step first, how to turn it off again (the variable back to false removes the worker on the next
apply; topics keep their state); `docs/friction.md` §12 keeps growing as things go (it exists on the
integration branch; add the entries of PRs A–F); and `docs/hackathon/opencv-2026-submission.md` (new: pitch, problem, what it does under the
rubric's headings, the two special awards, the loop perception → diff → agent → person, an
architecture diagram, measurement placeholders, licences, limitations — proxies, no ridership,
grade-separated crossings read as junctions, SCL as the only water mask — reproduction steps and a
video shot list). No new tests; every link in the docs must resolve; the suite stays green.

## 5. Risks and what bounds them

1. **The research tick's 120 s** against Overpass (≤ 25 s), the worker (≤ 60 s) and the agent:
   one site per tick, a 70 s measuring budget (hard cap 80), the network fetched only when stale
   and with ≥ 30 s left, measurement only with ≥ 45 s left, a 58 s worker read timeout, the agent's
   deadline at 85 s, `web_context` capped at 10 s. Worst case about 100 s before the summary call.
2. **Payload size.** Decimation to 20 m and caps (60k points, 5k ways, 2k stations, 5k POIs) keep
   the network near 1.5 MB in the request and in the running state; the review evidence and the
   prompts never render it; history keeps at most 2000 stations with ten fields each.
3. **Overpass 429s and mirror drift.** Two allow-listed endpoints, one fallback hop, the cached
   network kept on failure with `network_error`, a 30-day TTL: a healthy site fetches once a month.
4. **Cloud over a whole city.** Cloud cover ≤ 40 % at search, `coverage_floor` 0.6, heat computed
   over usable pixels only, low coverage never material and never in the baseline, unmeasured areas
   tinted in the figure.
5. **Graph fragmentation and false junctions.** Decimation keeps way endpoints, so shared OSM nodes
   land in one pixel; `graph_fragmented` is flagged and `components` reported; grade-separated
   crossings read as junctions is a documented limitation.
6. **Station snapping.** 300 m (15 px) with the `few_stations_snapped` flag; an unsnapped station
   keeps its catchment, activity and intermodal metrics and reports degree 0 and no `d_hub`.
7. **Thinning cost on a 3000² mask.** One-pixel `polylines` are nearly thin already, so two to four
   passes suffice; the pass works on the skeleton's bounding box and stops at 32; a perf-gated test
   and a benchmark kernel measure it.
8. **CI's Python 3.11 against the container's 3.13, and no OpenCV in the build container.** The
   venv used for every PR is 3.11; `networkx==3.4.2` and the abi3 OpenCV wheel serve both;
   `target-version = py311` syntax only; the frontend tests need Node locally.
