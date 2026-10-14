# Agentic vision: what was built, how it works, and how to run it

**Date:** 2026-10-06, the rail task added 2026-10-10 to 2026-10-14 · **For:** the
[AWS OpenCV AI Competition 2026](https://opencv26.devpost.com) (deadline 26 October 2026,
11:45 pm PDT) · **Status:** built, on the integration branch (#285, its pull request to `dev`),
not yet deployed (`VISION_ENABLED` is off) · **Tasks:** `ships` (the `satellite_vision` adapter)
and `rail_access`, the Rail Access Monitor (the `rail_access` adapter).

This is the documentation for the whole vision workstream in one place: the corrections to the
original strategy note, the design, every component and setting, what was measured on real data,
what went wrong along the way, and what an operator has to do to turn it on. The design rationale in
more depth is [opencv-agentic-vision-enhancement.md](opencv-agentic-vision-enhancement.md); the
rail task's review, angle and per-PR specifications are
[rail-access-monitor.md](rail-access-monitor.md); the pipeline step by step, with the rail concepts
in plain words, is [../architecture/blogger-vision.md](../architecture/blogger-vision.md). The
earlier topic ideas are [supply-chain-tracker-enhancement.md](supply-chain-tracker-enhancement.md)
and [../risks/opencv-bushfire-watch-01.md](../risks/opencv-bushfire-watch-01.md).

---

## 1. In one paragraph

A topic using the new `satellite_vision` adapter watches fixed sites (lon/lat polygons) in
Sentinel-2 satellite imagery. On each research tick it finds the newest scene over each site and
asks a **vision worker** (an `arm64` Lambda running **OpenCV 5**, deployed in us-west-2 beside the
imagery) to count bright, elongated objects on water. The count is compared with the site's own
recent baseline. Only a real numeric change goes to a **Claude triage agent**, which looks at the
annotated figure, may re-measure the scene with other settings or compare the previous one, and
answers "real" or "artefact". Only "real" becomes a Finding; the daily cycle then drafts an article
that **a person must approve**. A **COOL** path (OpenCV's Graviton-tuned build) runs the same code,
with a benchmark that measures it against stock OpenCV.

**The worker now has two tasks**, chosen by the request's `task`. `ships` is the one above: NIR,
green and the scene classification at 10 m, a count of vessels at a fixed site. `rail_access`, the
**Rail Access Monitor**, takes a whole city that fits inside one Sentinel-2 tile: from red, NIR,
SWIR and the scene classification on a 20 m grid it measures where the city is built up (a
built-up density "heat", a proxy, not population), how much of that is within walking reach of a
rail station (the served share) and what is not (transit deserts), and, from the OpenStreetMap
rail network the adapter fetches and sends with the request, the network as a graph: degree,
betweenness, hubs, distance to a hub, an isolation weight, three kinds of flag and two kinds of
simulated link. The rail adapter diffs the served share, the desert area and the station count
against the city's own baseline, and the same agent (with a news tool added) and the same person
stand between a number and an article, which carries the map.

```
ap-southeast-2 (home: all state)                      us-west-2 (beside the imagery)
────────────────────────────────                      ──────────────────────────────
research tick ── satellite_vision.fetch_state
                  │ Earth Search STAC: newest scene per site
                  │ vision_client.measure ───────────────► vision worker (arm64, OpenCV 5)
                  │                                        range-reads only the tiles under the site
                  │ ◄─────── metrics + small PNG ───────── masks → detect → count → annotate
                  │ figure → content bucket; history → running state
              material_diff: count vs median of earlier clear scenes
                  │ no change → stop (no model call)
                  ▼
              triage agent (Bedrock Converse, image + tools, bounded, fail closed)
                  │ artefact → kept in state, no Finding
                  ▼
              Finding → daily cycle → draft → compliance review → held for a person
```

The rail task follows the same path with two additions: the adapter fetches the city's
OpenStreetMap network through the Overpass API and keeps it in the running state, and the Finding
carries the figure to the article page. Its diagram is in
[rail-access-monitor.md §3](rail-access-monitor.md#3-architecture) and
[blogger-vision.md](../architecture/blogger-vision.md#part-one-the-pipeline).

---

## 2. The strategy note, corrected

| The note said | Verdict | What is true |
|---|---|---|
| Agentic Vision Award: 30% OpenCV 5 + agent integration, 25% orchestration/autonomy | Right | Full rubric: 30% integration, 25% orchestration and appropriate autonomy, 20% task effectiveness and evaluation, 15% failure handling / observability / security / human control, 10% UX, docs and demo. |
| Best Use of COOL: 30% for the Graviton/Arm component | Half right | The 30% row is *verified COOL integration* on Graviton. Then 25% architecture, **20% measured value against a baseline**, 15% innovation, 10% reproducibility. |
| Deploy the vision Lambdas for `arm64` and you score on COOL | **Wrong** | COOL is the **Cloud-Optimized OpenCV Library**, OpenCV's own OpenCV 5 build for Graviton3/4 (KleidiCV), on AWS Marketplace as AMIs and Docker images. Stock OpenCV on an `arm64` Lambda is not COOL. |
| BloggerBear's compute is all Lambda | Mostly | Plus Step Functions, Scheduler and SQS. And every Lambda was x86_64. |
| Extend the ops assistant's agent and MCP server | **Wrong place** | That assistant is read-only on its own narrow role. The vision agent lives in the pipeline. |
| Region restrictions on "Sentinel/Agent features" | **Wrong cause** | Sentinel-2 is ESA imagery, not an AWS feature. The **data** is in US regions (`sentinel-cogs` us-west-2, `noaa-himawari9` us-east-1). The model is fine: Claude Haiku 4.5 reads images through the AU profile in ap-southeast-2. |
| Don't move finished articles between regions | Right | All state stays in the home region; the worker is stateless. |
| A "diff-first, stateless" pipeline | Half right | Diff-first yes; it keeps state between ticks (and now more of it, §6.4). |
| New sources are adapters, not core branches | Right | Rule 5. Everything here is an adapter, a module, or a generic opt-in. |

Also settled: `opencv-python-headless==5.0.0.93` **does** ship Linux aarch64 wheels (an open
question in `docs/friction.md` 9.4).

The competition pages are blocked from the build container, so the rubric was read through search
results. **Re-check it against the Devpost rules before submitting.**

---

## 3. The pull requests

The ship stack (#240 to #254) was merged onto `dev` as one integration branch, whose pull request
to `dev` is #285; the rail task was built on it as pull requests into that branch, each reviewed
and merged there. #285 merges into `dev` as a whole. **Merging deploys nothing new** until the
`VISION_ENABLED` repository variable is `true` (§9).

The ship stack:

| PR | What it adds |
|---|---|
| [#240](https://github.com/AllainWoodsford/BloggerBear/pull/240) | The design doc and progress log; `friction.md` 9.4 settled |
| [#241](https://github.com/AllainWoodsford/BloggerBear/pull/241) | Vision core (`lambdas/vision/`): masks, detection, metrics, figure, build fingerprint |
| [#242](https://github.com/AllainWoodsford/BloggerBear/pull/242) | Reading Sentinel-2 windows without GDAL; lon/lat → UTM |
| [#244](https://github.com/AllainWoodsford/BloggerBear/pull/244) | Worker handler, request/reply contract, cross-region client |
| [#245](https://github.com/AllainWoodsford/BloggerBear/pull/245) | `satellite_vision` adapter; `keeps_running_state` in the research tick |
| [#246](https://github.com/AllainWoodsford/BloggerBear/pull/246) | Terraform: `vision-worker` module, bootstrap grants, gated by `vision_enabled` |
| [#248](https://github.com/AllainWoodsford/BloggerBear/pull/248) | Triage agent, `force_manual_review`, `vision_triage` on Stats, admin API float fix |
| [#251](https://github.com/AllainWoodsford/BloggerBear/pull/251) | COOL benchmark and COOL worker image recipe |
| [#254](https://github.com/AllainWoodsford/BloggerBear/pull/254) | This document, first version |

The integration branch and the Rail Access Monitor (the letters are the rail document's, §4):

| PR | Rail PR | What it adds |
|---|---|---|
| [#285](https://github.com/AllainWoodsford/BloggerBear/pull/285) | 0 | The integration branch: current `dev`, the stack and #240's docs, three append/append conflicts resolved. Its review fixes: a request whose `min_length_m` exceeds `max_length_m` is `bad_request`, not `internal` (`DEFAULT_LENGTH_M` in the contract), and the triage agent has a deadline (`triage_deadline_seconds`); Checkov's two findings on the artifacts bucket met |
| [#286](https://github.com/AllainWoodsford/BloggerBear/pull/286) | G | The deployment switch: `VISION_ENABLED`, a repository variable read as a switch, fed to both apply workflows as `vision_enabled` |
| [#287](https://github.com/AllainWoodsford/BloggerBear/pull/287) | B | `common/osm.py`: the OpenStreetMap Overpass client (two allow-listed endpoints, one fallback hop, decimation to the 20 m grid, caps, a streamed body under a size cap, coded errors, cache rules) |
| [#288](https://github.com/AllainWoodsford/BloggerBear/pull/288) | C | `common/figures.py` and the figures path: `Adapter.figures`, the list on the Finding and the article, one server-side copy per PNG to `articles/figures/<article>/<n>.png`, the `<figure>` on the static page and in the SPA |
| [#289](https://github.com/AllainWoodsford/BloggerBear/pull/289) | A | The rail vision core: `urban.py`, `access.py`, `network.py`, `rail_analyse.py`, `rail_annotate.py`; COG overviews in the reader and the UTM inverse |
| [#290](https://github.com/AllainWoodsford/BloggerBear/pull/290) | F1 | The stable docs: the pipeline page, the README section and data-source rows, the configuration and deployment sections, the submission draft |
| [#291](https://github.com/AllainWoodsford/BloggerBear/pull/291) | D | Worker tasks: `task` in the contract, the rail dispatch, overview reads, the network caps, bounded betweenness and graph size, the benchmark's rail kernels and `fetch-network` |
| [#292](https://github.com/AllainWoodsford/BloggerBear/pull/292) | E | The `rail_access` adapter, the shared `common/vision_sites.py`, task profiles in the triage agent (`web_context`), first observation reported once, after three review rounds |
| [#293](https://github.com/AllainWoodsford/BloggerBear/pull/293) | H | The rail figure: flags, isolation weight and simulated links drawn, the caption and a legend in a strip under the map, a byte budget |
| [#294](https://github.com/AllainWoodsford/BloggerBear/pull/294) | I | Planner notes (`common/rail_notes.py`): the flags and simulated links as sentences, in the summary, the review evidence and the Finding |
| [#295](https://github.com/AllainWoodsford/BloggerBear/pull/295) | J | Linear station adjacency, ghost-line walk and run lengths; the rail window cap (`RAIL_MAX_WINDOW_PX`) |
| this PR | F2 | This document for two tasks, the city topic templates, the runsheet's rail section, progress and friction |

---

## 4. Components

| Piece | Path | What it does |
|---|---|---|
| Masks | `lambdas/vision/masks.py` | Site polygon (`fillPoly`); cloud, shadow and no data from the SCL band; NDWI water that keeps ships inside it (small non-water patches are taken back as water). |
| Detection | `lambdas/vision/detect.py` | 8-bit stretch; masked areas painted with median water; `adaptiveThreshold` (Gaussian); blobs beside anything unmeasured dropped (`edge_buffer_px`); `minAreaRect` length and elongation filters; a tally of why candidates were dropped. |
| Per-site analysis | `lambdas/vision/analyse.py` | Count, coverage, clear-water km², density, size histogram, map coordinates, quality flags, the parameters used. |
| Figure | `lambdas/vision/annotate.py` | Contrast-stretched NIR, unmeasured areas tinted, rotated boxes, a burned-in "Processed imagery" caption; PNG, ≤ 1024 px. |
| Build record | `lambdas/vision/build.py` | OpenCV version, SHA-256 of `getBuildInformation()`, CPU baseline, KleidiCV; `matches()` against a pinned fingerprint. |
| GeoTIFF reader | `lambdas/vision/cog.py` | Range-reads only the tiles under a window: classic/BigTIFF, either byte order, DEFLATE + horizontal predictor, GeoTIFF scale/tiepoint/EPSG, nodata. Follows the IFD chain to the COG's overviews (`CogInfo.levels`, `level_for_pixel_size`); a mask IFD or an unreadable level is skipped. |
| Projection | `lambdas/vision/geo.py` | Lon/lat → WGS 84 UTM (Snyder's series; under 1 mm from PROJ), polygon → pixels, clipped windows; the inverse (`utm_to_lonlat`, `pixel_to_lonlat`) for the rail task's lon/lat outputs. |
| Scene reading | `lambdas/vision/scene.py` | Reads all bands over the site aligned to a reference band's grid: NIR at 10 m for ships, SWIR16 at 20 m for rail. A band with a COG overview at the reference's pixel size is read from it; else a finer band is read and reduced with `INTER_AREA`; SCL is never averaged (nearest `warpAffine`). `levels_used` says which level each band came from. Window cap 4096 px (rail: 3584 px, §4 contract). |
| Built-up heat | `lambdas/vision/urban.py` | NDVI and NDBI; `built_up_mask` (NDBI above a threshold, NDVI below a cap, not SCL water, not unusable, a 3×3 opening); `heat_map`: the mask blurred with a Gaussian of a walking distance, the local share of built-up ground, never rescaled. |
| Station reach | `lambdas/vision/access.py` | One `distanceTransformWithLabels` from the station raster: served area, `desert_mask`, desert clusters (`connectedComponentsWithStats`), each station's catchment activity and catchment coverage, intermodal counts. |
| Rail network | `lambdas/vision/network.py` | OpenStreetMap ways rasterised (`polylines`), thinned with `MORPH_HITMISS` (core OpenCV; no `ximgproc`), junctions and ends (`filter2D`), stations snapped (`DIST_LABEL_PIXEL`), the skeleton read back as a networkx graph with lengths, corridor visibility and tunnels; `station_adjacency`. |
| Rail analysis | `lambdas/vision/rail_analyse.py` | `RailParams`; the whole pass in order (masks, heat, network projected, reach, deserts, catchments, graph, measures, flags, simulated links); every metric rounded, JSON-clean and with its parameters echoed. |
| Rail figure | `lambdas/vision/rail_annotate.py` | The heat map over the red band, the analysis drawn on it, the caption and legend in a strip under the map; PNG level 9 within a 2 MiB budget (§6.6). |
| Worker | `lambdas/vision_worker_handler.py` | Validates, reads, analyses, annotates; replies with metrics, figure, build record, I/O and per-stage timings. Dispatches on `task`. Stateless. |
| Contract | `lambdas/common/vision_contract.py` | Request/reply schema shared by both sides; `TASKS`; asset URL allowlist; parameter bounds per task; the rail network's shape and caps (`validate_network`, `NETWORK_LIMITS`); error codes. |
| Client | `lambdas/common/vision_client.py` | Invokes the worker in the region its ARN names; sends the task and network; validates every reply; every failure is `VisionError`. |
| OpenStreetMap | `lambdas/common/osm.py` | The Overpass query, two allow-listed endpoints and one fallback hop, decimation to 20 m, caps, a streamed body under 20 MB, coded errors (`OsmError`), `is_stale`, the credit (`OSM_SOURCE`). No numpy or OpenCV. |
| Shared site machinery | `lambdas/common/vision_sites.py` | What both vision adapters share: config checks and bounds, the STAC search and choice of scene, the figure store, clear entries, the time `Budget`, the site order, `site_key`, the first-observation rule, the credits. |
| Ship adapter | `lambdas/common/adapters/satellite_vision.py` | Sites, history, the count diff, triage hook, attribution, summary prompt, review evidence. |
| Rail adapter | `lambdas/common/adapters/rail_access.py` | One city per site: scene search, the cached network, the worker call, history, the served-share, desert and station-count diff, triage, figures, source references, summary prompt, review evidence, planner notes. |
| Planner notes | `lambdas/common/rail_notes.py` | The rail flags and simulated links as one generated sentence each (§6.8). |
| Figures path | `lambdas/common/figures.py` | Shapes and checks `[{"key", "caption", "alt"}]` from adapter to Finding to article; the site key `articles/figures/<article>/<n>.png`; `static_pages.publish_figures` does the copy. |
| Triage agent | `lambdas/common/vision_triage.py` | Converse with the figure and the task profile's tools (three for ships, four for rail); bounded; a deadline; fail closed; trail returned. |
| Terraform | `infra/modules/vision-worker/` | The worker, its role, log group and artifacts bucket in `var.region`. |
| Benchmark | `scripts/vision_benchmark.py` | `kernels`, `scenes`, `worker` and `fetch-network` modes; reports CPU generation and build fingerprint. |
| COOL image | `docker/vision-cool/Dockerfile` | The same handler on OpenCV's COOL image. |

### The worker contract

A request names its `task`: `"ships"` when absent, so older callers are unchanged, or
`"rail_access"`. `VERSION` stays 1. A ships request:

```json
{"version": 1, "task": "ships", "backend": "opencv",
 "site": {"id": "botany-bay", "polygon": [[151.20, -33.97], [151.26, -33.97], [151.26, -34.02]]},
 "scene": {"id": "S2A_56HLH_20240105_0_L2A", "captured_at": "2024-01-05",
           "assets": {"nir": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/…/B08.tif",
                      "green": "…/B03.tif", "scl": "…/SCL.tif"}},
 "params": {"offset": 25}, "coverage_floor": 0.7, "image": true}
```

Reply: `{"version": 1, "ok": true, "task", "backend", "site_id", "scene_id", "captured_at", "metrics",
"epsg", "window", "image_png_b64", "build", "io", "timings_ms"}`, or
`{"version": 1, "ok": false, "error", "detail"}` with `error` one of `bad_request`,
`backend_mismatch`, `not_cool`, `site_outside_scene`, `window_too_large`, `unreadable_scene`,
`internal`. `io` is `{"requests", "bytes", "levels_used"}` (the COG level each band was read from,
0 the full image); `timings_ms` is `read`, `analyse`, `annotate` and `total`, in milliseconds.

Detector parameters a request may set (bounds in `PARAM_LIMITS`): `stretch_max`, `block_size`
(odd), `offset`, `min_length_m`, `max_length_m`, `min_elongation`, `edge_buffer_px`.

What each error means, for both tasks: a request that does not match the contract, an unknown or
non-string `task`, or a parameter combination the task's parameter dataclass refuses
(`DetectParams`, `RailParams`, built before any byte is read) is `bad_request`; any other failure
inside the worker, a `ValueError` from the analysis included, is `internal` and logged. A
`window_too_large` or `site_outside_scene` is decided from the reference band's header alone.

### The rail task's request

```json
{"version": 1, "task": "rail_access", "backend": "opencv",
 "site": {"id": "sydney", "polygon": [[150.86, -33.58], [151.56, -33.58], [151.56, -34.16], [150.86, -34.16]]},
 "scene": {"id": "S2A_56HLH_20240105_0_L2A", "captured_at": "2024-01-05",
           "assets": {"red": "…/B04.tif", "nir": "…/B08.tif", "swir16": "…/B11.tif", "scl": "…/SCL.tif"}},
 "params": {"reach_m": 1000}, "coverage_floor": 0.6, "image": true,
 "network": {"ways": [{"id": "w123", "points": [[151.2, -33.89], …], "tunnel": false, "name": "…"}],
             "stations": [{"id": "n456", "name": "Redfern", "lon": 151.198, "lat": -33.892, "kind": "station"}],
             "pois": [{"id": "n789", "lon": 151.2, "lat": -33.89, "kind": "bus_stop"}],
             "fetched_at": "2026-10-10T03:00:00+00:00"}}
```

- **Assets.** `red`, `nir`, `swir16` and `scl`, all four required and no others (`TASKS`). The
  analysis grid is SWIR16's 20 m (the worker's `RAIL_REFERENCE_BAND`); the 10 m red and NIR are
  read from the COG overview whose pixel is 20 m, a quarter of the bytes, and SCL (20 m already)
  is never averaged. SWIR16's no-data pixels are folded into SCL's no-data class.
- **Window.** The rail task has its own cap, `RAIL_MAX_WINDOW_PX` = 3,584 px a side (71.7 km at
  20 m), under the reader's 4096 px for ships: its compute grows with the window and the worker
  has one vCPU (§7, §8). The four starting cities need at most 3,327 × 3,306 px (Sydney) with the
  reader's 16 px pad. A larger site is `window_too_large` once the reference band's header is read,
  before any tile.
- **`network`.** Required for rail and refused for ships: the shape `common/osm.py` returns, checked
  by `validate_network` against `NETWORK_LIMITS` (60,000 way points in all, 5,000 ways, 2,000
  stations, 5,000 points of interest, names cut at 80 characters), every lon/lat checked as a
  polygon's points are, ids of at most `MAX_NETWORK_ID_CHARS` (32), `fetched_at` a string of at
  most `MAX_FETCHED_AT_CHARS` (64). Only `ways`, `stations`, `pois` and `fetched_at` are sent; the
  adapter keeps the rest. The client sends the request as UTF-8 (names are not escaped): a city's
  network at the caps is about 2.6 to 3 MB of JSON, under Lambda's 6 MB synchronous payload.

**Rail parameters** a request may set: every field of `vision.rail_analyse.RailParams`, within the
contract's `RAIL_PARAM_LIMITS` (a test holds the two equal). The adapter always sends `reach_m`
from the topic's config, with the topic's `params` laid over it.

| Parameter | Default | Limits | Meaning |
|---|---|---|---|
| `ndbi_threshold` | 0.0 | −0.5 to 0.5 | NDBI above this is built up (0 is SWIR brighter than NIR). |
| `ndvi_max` | 0.3 | 0 to 1 | NDVI at or above this is vegetation, built up or not. |
| `reach_m` | 1000 | 200 to 5000 | Walking reach of a station, straight line. |
| `heat_sigma_m` | 500 | 100 to 3000 | The heat map's Gaussian sigma: the neighbourhood whose built-up share a pixel holds. |
| `snap_m` | 300 | 20 to 1000 | How far a station may sit from the drawn line and still be on it. |
| `hub_count` | 5 | 1 to 20, whole | How many stations, by betweenness among the busier half, are hubs. |
| `intermodal_near_m` | 300 | 50 to 1000 | Bus stops and bus stations within this of a station are counted. |
| `intermodal_far_m` | 500 | 100 to 2000, at least `intermodal_near_m` | Ferry terminals and park-and-rides within this are counted. |
| `min_desert_km2` | 0.5 | 0.01 to 100 | A built-up patch beyond reach is reported as a desert from this size up. |
| `visibility_ndvi_max` | 0.35 | 0 to 1 | A corridor pixel with NDVI below this shows bare ground or track, not canopy. |
| `max_orbital_km` | 5.0 | 1 to 20 | Orbital links are simulated only between stations this close in a straight line. |

**The rail reply** has the same envelope as above, with `"task": "rail_access"`, `timings_ms` that
adds `graph` (the rasterise, thin, snap and build stage, part of `analyse`), and these `metrics`
(floats to 4 decimal places, coordinates to 5, never NaN or infinity):

| Key | What it is |
|---|---|
| `task`, `coverage` | `"rail_access"`; the share of the site's pixels the scene could see. |
| `built_up_km2`, `served_km2`, `desert_km2`, `served_share` | Built-up area, the part within reach of a station, the part beyond it, and served over built up (0 when nothing is built up). |
| `station_count`, `snapped_station_count` | Stations in the network, and those snapped to a drawn line within `snap_m`. |
| `node_count`, `edge_count`, `components` | The graph's size and its connected pieces. |
| `hubs`, `interchanges` | Station node ids (`s:<osm id>`): the top `hub_count` by betweenness among stations with activity at or above the median; stations of degree 3 or more. |
| `stations[]` | At most 2,000 rows: `id`, `name`, `kind`, `lon`, `lat`, `snapped`, `degree`, `betweenness`, `activity` (null when the catchment was not seen), `catchment_coverage`, `d_hub_m` (null with no path to a hub), `isolation_weight`, `intermodal` (`bus_stop_near`, `bus_station_near`, `ferry_terminal_far`, `park_ride_far`). |
| `flags[]` | `isolated_high_demand` (`station`, `name`, `activity`, `degree`, `isolation_weight`), `single_point_of_failure` (`station`, `name`, `ratio`, null when the second station's betweenness is 0), `ghost_line` (`stations`, `names`). |
| `suggestions[]` | `orbital_link` (`from`, `to`, `from_name`, `to_name`, `straight_km`, `delta_top_hub_betweenness`, `delta_mean_d_hub_m`), at most three; `feeder_corridor` (`station`, `name`, `hub`, `hub_name`, `length_m`), at most three. |
| `deserts[]` | The ten largest: `lon`, `lat`, `area_km2`, `nearest_station`, `nearest_station_m` (both null when no station lies in the window). |
| `edges[]`, `truncated` | At most 5,000 edges (`from`, `to`, `length_m`, `betweenness`, `corridor_visibility`, null where over half is tunnel, `tunnel`), kept by betweenness; how many were cut. |
| `quality_flags` | Any of `empty_site`, `low_coverage`, `no_network`, `few_stations_snapped` (under half), `graph_fragmented` (more than max(3, stations / 10) components), `graph_too_large` (no graph was built: over `MAX_GRAPH_NODES`), `betweenness_sampled` (below). |
| `warnings` | Elements skipped (coordinates not on Earth, repeated ids), stations merged, odd runs. |
| `network` | What was received: `ways`, `stations`, `pois`, `points`, `fetched_at`. |
| `params` | `pixel_size_m`, `coverage_floor` and every `RailParams` field, as measured with. |

Two bounds keep a contract-valid but pathological network inside the worker's time.
`MAX_GRAPH_NODES` (10,000) is checked on the skeleton's node clusters and runs before any networkx
object exists: over it, no graph is built, the stations are reported without degree, betweenness or
hub distance, hubs, flags and suggestions are empty, and `graph_too_large` says so.
`BETWEENNESS_WORK` (600,000) bounds sources × (nodes + edges) for each betweenness call: a graph
within it is exact; a larger one samples `BETWEENNESS_WORK // (nodes + edges)` seeded source nodes
(at least 20, at most 200), and `betweenness_sampled` says so. A simulated link is measured with the
base graph's estimator, so its before and after come from the same sample.

---

## 5. Configuration

### A `satellite_vision` topic

Create it with the admin CLI (`topics create`) or the admin API. `force_manual_review` is set on it
automatically and cannot be unset.

| `adapter_config` key | Default | Meaning |
|---|---|---|
| `sites` | required | 1–10 of `{"id", "name", "polygon": [[lon, lat], …]}`; polygons of 3–64 points. Names are used in articles exactly as given. |
| `object_noun` | `"objects"` | What articles call what is counted, e.g. `"large vessels"`. |
| `backend` | `"opencv"` | `"cool"` once a COOL worker exists. |
| `params` | `{}` | Detector overrides (§4). |
| `max_cloud_cover` | 60 | Scene-level cloud filter in the search, %. |
| `lookback_days` | 10 | How far back to search for a scene. |
| `coverage_floor` | 0.7 | Below it, a count is never material and never part of a baseline. |
| `history_size` | 8 | Scenes kept per site. |
| `min_baseline` | 2 | Earlier clear scenes needed before a change can be material. |
| `relative_threshold` | 0.35 | Both this … |
| `absolute_threshold` | 5 | … and this must be crossed against the baseline median. |
| `max_sites_per_tick` | 5 | Bound on worker calls per tick. |
| `time_budget_seconds` | 60 | Stop measuring new sites after this (the research tick times out at 120 s). |
| `triage` | true | Run the agent on a numeric change. |
| `triage_max_tool_calls` | 3 | The agent's tool budget. |
| `triage_deadline_seconds` | 85 | Seconds after the tick started by which the agent must have answered. |

### A `rail_access` topic

Every key, its default and its bounds are in one table, in
[configuration.md, A `rail_access` topic](../configuration.md#a-rail_access-topic), read from the
adapter's `DEFAULTS` (`lambdas/common/adapters/rail_access.py`); the rail parameters `params` may
override are the table in §4. Four ready topic bodies, one per starting city, are
`scripts/rail_topics.example.json`, and the runsheet's
[rail_access topics](../deployment-runsheet.md#rail_access-topics) creates them. What differs from a
ship topic, in short: at most three sites (one city per topic is the design), one city measured per
tick by default, `reach_m` and the network cache's `osm_ttl_days`, the agent's `web_context`, and
three material rules (served share, desert area, station count, §6.2) in place of the count's two
thresholds.

### Bounds every vision topic shares

`common/vision_sites.parse_site_config` refuses a config the tick could not keep to, before any
call: numbers must be finite; `time_budget_seconds` in (0, 80] (the tick has 120 s);
`triage_deadline_seconds` in (0, 90]; `triage_max_tool_calls` 0 to 5; `history_size` 2 to 20;
`min_baseline` at least 1 and under `history_size`; `max_sites_per_tick` 1 to the adapter's site cap;
`coverage_floor` in [0, 1]; no negative `*_threshold`; `triage` and `web_context` true or false; a
site id of letters, digits, `_` and `-` (at most 64, since it becomes part of an S3 key), unique;
a site name of 1 to 80 characters of plain text. The admin API stores `adapter_config` as given;
a config the adapter refuses fails the topic's research tick with the reason in its log.

### Terraform

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `vision_enabled` | dev and production roots, from CI (`VISION_ENABLED`) | `false` | Create the worker and give the research tick its ARN and invoke right. |
| `vision_region` | dev and production | `"us-west-2"` | Where the worker runs. Must equal the bootstrap's. |
| `vision_region` | `infra/bootstrap` | `"us-west-2"` | Where the deploy roles may create `<prefix>-*-vision-*` Lambdas and log groups. |
| `memory_size`, `timeout` | module | 2048 MB, 60 s | The worker's size; the timeout stays under the client's 90 s. |

The full list, with the switch, is [configuration.md, Vision and rail access](../configuration.md#vision-and-rail-access).

### Environment variables

| Variable | Set on | Meaning |
|---|---|---|
| `VISION_WORKER_ARN` | research tick | The stock worker; empty when off, which reads as "not configured". |
| `VISION_COOL_WORKER_ARN` | research tick | The COOL worker, once it exists. |
| `VISION_BACKEND` | worker | `opencv` or `cool`: what this deployment is. |
| `COOL_BUILD_SHA256` | COOL worker | The pinned COOL build fingerprint. |
| `VISION_ALLOWED_URL_PREFIXES` | worker, research tick | Optional; defaults to the `sentinel-cogs` bucket. |

The rail task adds none: Earth Search, the `sentinel-cogs` bucket, Overpass and GDELT need no key.

### Topic flag: `force_manual_review`

Any topic may set it; `satellite_vision` and `rail_access` topics always have it (the admin API
forces it for every adapter in `MANUAL_REVIEW_ADAPTERS`, on create and on update). The daily cycle
adds "this topic's articles are always reviewed by a person before publishing" to the hold
reasons, so nothing auto-publishes. Unlike `is_financial`, drafting is unchanged and the
compliance review still runs, so the moderator sees its verdict. Stored only when true.

### Where things are stored

| What | Where |
|---|---|
| Per-site history, `last_error`, `last_attempt_at`, triage trails | `snapshots/<topic>/running-state.json` in the content bucket, and each Finding's snapshot (`snapshots/<topic>/<captured_at>.json`) |
| The rail task's cached OpenStreetMap network, per site, with its polygon's `site_key`, `network_error` after a failed refresh, and `failed_scene` after a refusal | The same running state (never in a prompt or the review evidence) |
| When the topic's first observation was reported | `first_observed_at` in the running state |
| Figures, as the worker drew them | `vision/<topic>/<site>/<scene>.png` in the content bucket (private) |
| A Finding's figures and planner notes | `figures` (`[{"key", "caption", "alt"}]`) and, for rail, `planner_notes` (at most 7) on the Finding |
| A published article's figures | `articles/figures/<article>/<n>.png` in the site bucket (copied at publish, removed on unpublish and take-down), and `figures` on the article row |
| Triage spend | Stats page, category `vision_triage` |
| Worker package | `<prefix>-<env>-vision-artifacts-<account>-<region>` bucket, `vision-worker/<md5>.zip`, 14-day expiry |

A rail history entry keeps, per scene: `scene_id`, `captured_at`, `scene_cloud_cover`, `coverage`,
`built_up_km2`, `served_km2`, `desert_km2`, `served_share`, `station_count`, `edge_count`, `hubs`,
`flag_count`, `quality_flags`, `backend`, `build_sha256`, `timings_ms`, `image_key`, `assets` (for
`look_again`), `network_fetched_at`, `network_stale`, `site_key`, `params` (as the worker echoed
them) and, once judged, `triage` (`verdict`, `reason`). Only the newest entry also keeps the lists
(`NEWEST_ONLY_KEYS`: `stations`, cut to ten fields and 2,000 rows, `flags`, `suggestions`,
`deserts`): the figure, the review evidence, the agent's brief and the planner notes read them from
it alone, and dropping them from older entries is what keeps three cities' state near 10 MB
(§8).

---

## 6. How the decisions are made

### 6.1 The rules this keeps

- **Rule 2, diff-first:** OpenCV measures every new scene; Bedrock is only called after a numeric
  material change (the triage), and then for the summary.
- **Rule 3 and the risks doc's item 9:** no vision article publishes without a person.
- **Rule 5, adapters:** the handlers learned nothing about imagery; the one research-tick change
  is a generic opt-in, and the figures path and planner notes are generic hooks too.
- **Rule 6, Terraform:** everything is behind `vision_enabled`; the bootstrap change is applied
  by hand.
- **Rule 7, security:** Trivy scans now cover `requirements-vision.txt` and
  `requirements-vision-cool.txt`; nothing was weakened.

### 6.2 Material change

A ship site is material when its new scene's coverage is at least the floor, there are at least
`min_baseline` earlier clear scenes, and the count differs from their median by at least
`absolute_threshold` **and** `relative_threshold`.

A rail site is material when its new scene's coverage is at least the floor, there are at least
`min_baseline` earlier clear scenes **measured the same way** (the same polygon's `site_key` and
the same echoed parameters, so an edited reach or polygon starts a new baseline, reported as
"baseline still building", rather than reading as a change in the city), and any of: the served
share moved by at least `served_share_threshold` from their median; the desert area moved by at
least `desert_relative_threshold` times the larger of its median and 1 km²; the station count
differs from that of the previous scene measured the same way (clear or not: a station count does
not depend on cloud). Values are compared as stored, to 4 decimal places.

**The first observation, both adapters** (`vision_sites.first_observation`): the first tick that
measures a scene at or above the coverage floor is the topic's first observation, material and
reported once; the tick records when (`first_observed_at`), so trimming the history or raising the
floor never makes a later scene "first" again. A tick before that, that measured nothing or only
scenes below the floor, is not material and calls no model. The first observation is not triaged:
there is no baseline to compare, and every article of these topics is held for a person anyway.

A site that could not be searched or measured keeps its history and records `last_error`: **not
measured is never "nothing there"**.

### 6.3 The triage agent

Given the metrics, the baseline, the detector's rejection tally (ships) and the figure, Claude may
call:

- `look_again(settings)`: re-measure the same scene with other parameters (the task's contract
  limits are the tool's schema);
- `previous_scene()`: the previous scene's metrics and figure;
- `site_history()`: every stored scene's date, key values and coverage;
- `web_context()` (rail only, when the topic's `web_context` is true): at most five news
  headlines from the last 60 days for "<city> rail station opening closure line", through the
  existing GDELT search (`common/web_search.search_web`), within 10 s.

Bounds are in code: at most `triage_max_tool_calls` tool calls, at most that plus two model turns,
a deadline (`triage_deadline_seconds` after the tick started) under which no model turn starts with
less than 10 s left, and a strict JSON answer `{"verdict": "real" | "artefact", "reason": "…"}`.
`look_again` waits on the worker for at most the client's read timeout, cut to the deadline less
the 10 s answer turn and the 5 s connect allowance, rounded down to 5 s; under 10 s (ships) or 30 s
(rail: a city's run takes 20 to 40 s and a worker call that cannot finish is still billed) it is
refused and the model told to answer. A malformed answer, an unknown verdict, a Bedrock error, no
model, the turn limit or the deadline all mean **artefact**: an unsure agent never makes a Finding.

What the agent reads from outside is data, not instructions: the rail system prompt says so;
news results are cut (title 200, snippet 300, source 100 characters) and made one plain line each,
a link kept only when it is http(s) and at most 500 characters, a date only when it is one; the
brief carries at most 20 flags with `flag_count`, and never the network or the station list. Its
reason is one plain line of at most 300 characters, and a summary quotes it as
`the vision agent's note (not a measurement): "…"`. The trail (tools, inputs, outcome, verdict,
reason, model, tokens) is stored in the state under `triage`, and the spend on the Stats page.

### 6.4 Running state

The research tick used to keep state only when it wrote a Finding, so a baseline would have held
only reported scenes, and every tick would have re-measured the same scene. Adapters can now set
`keeps_running_state`: the tick also writes the newest state to
`snapshots/<topic>/running-state.json` after every tick, reads it first next time, and falls back to
the last Finding's snapshot if it is gone (it expires with the `snapshots/` lifecycle).

### 6.5 What articles may say

The ship summary prompt allows observations only: site names as given, counts, the change against
the baseline, the capture date. It forbids naming any vessel, owner, company or person, causes,
predictions, prices or trading views, saying any area is safe or clear, and inventing places or
numbers.

The rail summary prompt says what the numbers are (built-up density from Sentinel-2 at 20 m,
straight-line reach, network measures from OpenStreetMap; none is ridership, patronage or journey
time) and asks for 2 to 4 plain sentences of observations only: names exactly as given, the served
share, the built-up area beyond reach, the capture date, and what was not measured (ridership,
service frequency, planned works). It forbids causes, forecasts, political, planning or investment
views, saying any area is well or badly served in absolute terms, and inventing names or numbers.
With planner notes in the diff (§6.8), it allows 3 to 6 sentences and adds one paragraph before
that last rule: end with at most three of the notes, each keeping its numbers, names and the words
"flags" or "simulation", never called recommendations, with no cost, timetable, cause or opinion
on whether they should be built, and names in quotation marks used as names only.

### 6.6 Attribution

Every `satellite_vision` and `rail_access` article and topic page credits *"Contains modified
Copernicus Sentinel data"* (the Copernicus legal notice's wording) and *"Sentinel-2 Cloud-Optimized
GeoTIFFs accessed from the Registry of Open Data on AWS"*; a `rail_access` one also credits *"Map
data © OpenStreetMap contributors, available under the Open Database Licence"*. All three are on
the About page. Each figure is labelled as processed imagery and its caption carries the year.

The rail figure's map carries only the analysis: the heat (inferno colours) over the stretched red
band where the scene was usable and a slate tint where it was not; deserts outlined in amber; the
graph's edges in pale cyan, thicker the more paths run through them; stations as white rings, larger
the busier the catchment, with a thicker ring in the top quarter of isolation weight; hubs as green
diamonds; a flagged station with a second, red ring; a ghost line as a red line through its
stations; simulated links dashed in green. Its words are in a dark strip under the map, so they
never cover a station: three caption lines ("Processed imagery - BloggerBear", "Contains modified
Copernicus Sentinel data {year}", "Map data (c) OpenStreetMap contributors"; Hershey fonts have no
©), a 1 px rule, and a legend in one to three columns by width, on every figure at least 256 px
wide. The PNG is written at zlib's level 9 and kept within a 2 MiB budget by drawing it again at 0.8
times the size, down to a 512 px map (the client refuses a figure over 3 MiB). The page caption (at
most 400 characters, `figures.MAX_CAPTION_CHARS`) is:

> Rail access in {name}, Sentinel-2 {date}: built-up density (warm colours), areas beyond {reach}
> of a station (outlined), OpenStreetMap rail lines and stations, flagged stations (red rings) and
> simulated links (dashed). Processed imagery; contains modified Copernicus Sentinel data {year};
> map data © OpenStreetMap contributors.

and its alt text "Processed satellite map of {name} showing rail lines, stations, flagged stations,
simulated links and built-up areas beyond walking reach of a station". A long site name is cut
(and marked) so the credits always fit.

### 6.7 Rail access: what is measured and what is not

- **Built-up density is a proxy, not population.** A pixel is built up when its NDBI is above a
  threshold, its NDVI below a cap, and the scene classification calls it neither water nor cloud,
  shadow or no data; the heat is that mask blurred over a walking distance. A warehouse district
  and a block of flats are both roofs and asphalt; a park is cold however many people cross it.
  The heat is never rescaled, so two scenes of one city compare directly.
- **The topology comes from OpenStreetMap, and the imagery verifies it.** Ways, stations and
  intermodal points are mapped data (ODbL, credited). The imagery supplies the density and scores
  each surface edge's corridor visibility; it does not draw the network. Where the map is wrong or
  stale, the graph is; a low visibility score is a question, not a correction. Grade-separated
  crossings read as junctions.
- **Reach is a straight line.** A built-up pixel within `reach_m` of a station is served; one
  beyond it is in a **transit desert**, reported from `min_desert_km2` up with its nearest station.
  A river, a motorway or a rail yard between a home and a station is not seen.
- **Flags and suggestions are hypotheses.** A flag is a pattern in a density proxy and a map
  (isolated high demand, a single point of failure, a possible ghost line); a suggestion is an edge
  added to a graph of drawn lines and the change it makes to distances and betweenness. Neither
  knows ridership, timetables, journey times, cost, land or engineering, and neither is a
  recommendation.
- **Unmeasured is never "nothing there".** Coverage says how much of the city the scene saw; a
  station whose catchment was under cloud has no activity (null), not a quiet one; unsnapped
  stations, graph components, truncated lists and the quality flags are reported; the figure tints
  what was not measured; a site that could not be read keeps its history and records `last_error`.

### 6.8 Planner notes: the flags and simulations in words

`common/rail_notes.py` turns the newest entry's flags and simulated links into one sentence each,
written by code from the measured values. Up to four flags (single point of failure, then isolated
high demand by isolation weight, then ghost lines longest first) and three simulations (orbital
links by the fall in distance to a hub, then feeder corridors by isolation weight), in that fixed
order. The templates, with every station name OpenStreetMap's, in double quotation marks, one line
without controls, markup or bidi overrides:

- "{name} lies on {ratio} times as many shortest paths through the mapped rail network as any
  other station: the pipeline flags it as a single point of failure, where a disruption would cut
  more routes than anywhere else on the map." (Without a ratio: "lies on more shortest paths …
  than any other station".)
- "{name} has one of the busiest catchments in the city by built-up density (activity {a}, top
  quarter), {n} track directions and {d} km by rail to the nearest hub: the pipeline flags it as
  isolated high demand."
- "{n} connected stations, among them {first} and {last}, sit in the quietest quarter of built-up
  density along the network: the pipeline flags the stretch as a possible ghost line, where track
  runs through little built-up area."
- "Simulation: a {km} km link between {a} and {b} (the straight line plus 20 %) would bring the
  mapped network's stations {m} m closer to a hub on average by rail, and change the busiest hub's
  share of shortest paths by {±p} %."
- "Simulation: {name} is among the most isolated stations for the size of its catchment (isolation
  weight {w}); a feeder corridor, such as an express bus, to {hub}, {d} km away by rail today, is
  the link the pipeline's rules point to."

The honesty rules, which the tests hold: every sentence comes from measured values and says what
the measure is; flags are called flags and simulations simulations, hypotheses from proxies
(built-up density, straight-line reach, OpenStreetMap's topology), never recommendations, and no
note gives a cost, a cause or a timetable; a value that was not measured, or that rounds to nothing
as printed, drops the clause that needs it (a simulation whose one claim rounds to nothing is not
written); a figure read from sampled betweenness says "about". The notes follow the summary in the
diff (introduced as "Rule-based flags and simulations (proxies, not recommendations):"), so the
summary prompt and the article may use them; they are in the review evidence (`planner_notes`, per
site); and the Finding keeps a structured copy (`planner_notes`, at most seven, each with its site,
kind, type, stations and text), which leaves out a scene the agent judged an artefact. The figure
draws the same flags and links (§6.6).

---

## 7. Measured

On a real Sentinel-2 scene, Botany Bay (`S2A_56HLH_20240105_0_L2A`, B08 + B03 + SCL), from the
build container (outside us-west-2):

| | Value |
|---|---|
| Header + tiles read | 9 range requests, 5.5 MB (of files far larger) |
| Read | 2.4–3.1 s (crossing to us-west-2; should be much less in-region) |
| OpenCV analysis | 18–33 ms |
| Figure | 50–56 ms |
| Detections, first version | 93 (mostly shoreline and cloud-edge pixels) |
| Detections, with `edge_buffer_px = 2` | 15 (the remainder mostly thin cloud; scene flagged `low_coverage`, 0.69) |

**The rail task on Sydney** (PR D's smoke run, the worker handler in-process in the build
container: the same scene, B04, B08, B11 and SCL, the Sydney box of
[rail-access-monitor.md §2](rail-access-monitor.md#2-the-angle-rail-access-not-rail-yards), and a
nine-station network built by hand, since Overpass is unreachable from the container):

| | Value |
|---|---|
| Window on the 20 m grid | 3,327 × 3,306 px (the box plus the 16 px pad; 66.5 × 66.1 km) |
| Read | 192 range requests, 54 MB; 27.1 s through this container's proxy |
| Analyse (masks, reach, deserts, catchments, graph, measures) | 12.1 s, of which the graph stage 6.6 s |
| Figure | 0.5 s |
| Total | 39.7 s |
| Coverage | 0.147: the 2024-01-05 scene is mostly cloud over the city, so `low_coverage` is flagged and the scene could never be material |

The read is a different number in us-west-2, beside the bucket, and the graph stage says nothing
about a city's real network of a few thousand nodes (friction 12.12); the deployed run with the
Overpass network is still to be made (§9).

Kernel benchmark, stock OpenCV 5.0.0 on x86 (1024 px): adaptive threshold 4.8 ms, connected
components 2.4 ms, contours 0.4 ms, whole `analyse_site` 35.6 ms.

The benchmark with the rail kernels (`python scripts/vision_benchmark.py kernels`, its defaults:
2048 px synthetic images, 20 runs, medians) on **this build container's CPU, not Graviton**: an
Intel Xeon at 2.10 GHz with 4 vCPUs, stock `opencv-python-headless` 5.0.0 (build `0ef08003c311`),
Python 3.11, on the integration branch with PR J's code; then pinned to one CPU (`taskset -c 0`),
as the worker's one vCPU is. The machine is shared, and two runs differed by up to 40 % on some
rows:

| Operation | 4 vCPUs, ms | 1 CPU, ms |
|---|---:|---:|
| `distance_transform_l2` (with labels) | 48.8 | 57.0 |
| `thinning_hitmiss` | 51.2 | 44.2 |
| `polylines` | 0.2 | 0.2 |
| `apply_colormap` | 1.8 | 4.0 |
| `connected_components_stats_desert` | 11.4 | 24.2 |
| `analyse_rail_access` (a 2048 px city, 8 lines, 40 stations) | 1,138 | 1,303 |
| `adaptive_threshold_gaussian_31` | 38.2 | 35.5 |
| `connected_components_stats` | 9.6 | 24.1 |
| `analyse_site` (ships, 2048 px) | 191.6 | 224.6 |

**The COOL and Graviton rows are still to be measured** (§9). The worst cases the worker must
survive were measured separately on one core (§8, lesson 13).

Tests: the full suite passed at 4412 on the stack's last PR, and 4448 with the whole stack merged
into the latest `dev`; each rail PR added its own, with the full suite green before every push.

---

## 8. What went wrong, and what it taught

1. **GDAL doesn't fit.** rasterio + OpenCV + numpy for arm64 is 278 MB unpacked, 254 MB stripped,
   over Lambda's 250 MB. A small reader for the TIFF subset Sentinel-2 uses (§4) replaced it, and
   was checked against a real file.
2. **The package is at the direct-upload limit.** OpenCV + numpy zip to ~51 MB, against Lambda's
   50 MB direct upload; the package goes through an S3 bucket in the worker's own region.
3. **numpy 2.4 needs glibc 2.27+.** Lambda's python3.11 runs on Amazon Linux 2 (glibc 2.26), so the
   worker uses python3.12, which also matches COOL's Python 3.10–3.12 images.
4. **The research tick dropped no-change state** (§6.4).
5. **The admin API refused floats.** Creating any topic whose `adapter_config` held a decimal (a
   polygon) returned 500; floats are now stored as `Decimal`.
6. **Real imagery is noisier than synthetic.** Mixed pixels along shores and cloud edges were most
   of the first detections; the edge buffer and the rejection tally came from that.
7. **The tool budget left no turn to answer.** With a tool budget of 2, the agent's third
   request was refused but it never got a turn to answer; one final turn was added.
8. **Repo-wide tests caught the wiring.** Every resource-naming module must be wired in both
   environments; the ops assistant's architecture catalogue must list every Lambda; every
   requirements file must be in the Trivy scan; the CLI guide lists every adapter. Each failed
   first, and was fixed in the PR.
9. **The build container's limits.** The Terraform registry, Earth Search STAC and Overpass are
   blocked from it: providers came from a local mirror of releases.hashicorp.com (lock files left
   untouched), and the STAC search and the Overpass client are tested with fakes only.
   `sentinel-cogs` itself was reachable, which is what made the real-data checks possible.
10. **No `ximgproc` in the headless wheel.** The usual `cv2.ximgproc.thinning` is opencv-contrib,
    which `opencv-python-headless` does not carry, and COOL's module list is not published. The
    skeleton is made with core OpenCV instead: sequential hit-or-miss thinning
    (`cv2.morphologyEx(MORPH_HITMISS)`) over the Golay L elements and their rotations, each hit
    removed before the next element, on the drawing's bounding box, at most 32 passes. One-pixel
    `polylines` are nearly thin already, so a real network needs a few passes.
11. **Overview reads to fit a city in the window.** A 60 km city at 10 m is 6,000 px a side, past
    the reader's 4096 px window and far past the worker's time. The rail task works on SWIR16's
    20 m grid instead, and reads the 10 m red and NIR from the COG overview whose pixel is 20 m:
    the reader now follows the IFD chain, keeps each reduced image as a level, and
    `level_for_pixel_size` picks it; a file without one is read at full resolution and reduced with
    `INTER_AREA`. On Sydney that is 54 MB in 192 range requests for a 66 km box.
12. **The tick's 120 s, shared between Overpass, the worker and the agent.** The research tick has
    120 s and 512 MB for everything: the STAC search, a network fetch, the worker, the agent, the
    summary call and the stores. The split, in code: one city per tick (`max_sites_per_tick` 1); a
    70 s measuring budget (`time_budget_seconds`, never over 80); the network fetched only with
    30 s of it left and finished within them, both Overpass hops included (`OSM_RESERVE_SECONDS`,
    25 s per hop); the worker asked only with 45 s left (`MEASURE_RESERVE_SECONDS`) and waited on
    for 58 s (`WORKER_READ_TIMEOUT`, under its own 60 s timeout; the client's default 90 s would
    run the tick out); the agent's answer due 85 s after the tick started
    (`triage_deadline_seconds`, never over 90), no model turn with under 10 s left, `look_again`
    cut to the time left and refused under 30 s, `web_context` within 10 s; at least the last 30 s
    for the summary call and the stores. And the order of work keeps a slow Overpass from costing a
    measurement: the scene comes first; a new scene is measured with the cached network even when
    it is past its TTL (`network_stale`); a stale network is refreshed only on a tick with nothing
    new to measure, and after a failed refresh not again for 6 hours (`NETWORK_RETRY_HOURS`).
13. **One core, not four.** The first worst-case timings of the rail analysis were taken on the
    build container's four cores, where OpenCV threads: 20 to 21 s at 4096 px, comfortably inside
    the worker's 60 s. The worker has one vCPU at 2048 MB. Pinned to one core (`taskset`,
    `cv2.setNumThreads(1)`), the worst contract-valid networks took 28 to 32 s at 4096 px, about
    42 s at an assumed 1.3 times slower arm64 core, which with a 10 to 30 s read runs past the
    client's 58 s whenever the read takes over 16 s. So the rail task got its own window cap,
    `RAIL_MAX_WINDOW_PX` = 3,584 px (71.7 km at 20 m; Sydney needs 3,327): there the same inputs
    take at most 20.7 s at 1.24 GB peak, about 27 s on arm64, 35 to 50 s with the read. Station
    adjacency, the ghost-line walk and the run lengths were made linear first (PR J): for 2,000
    stations off one junction grid, adjacency took 14.6 s and takes 1.0 s. The lesson: time a
    bound on the cores the target has, not the ones the build machine has.
14. **The state a city costs.** A city's network at the caps is 2.6 to 3 MB of JSON, and the
    first state kept every scene's station list: three cities, twenty scenes each, made a 54 MB
    state and a 390 MB peak in the 512 MB tick. Only the newest entry now keeps the lists
    (`NEWEST_ONLY_KEYS`), every entry keeps `flag_count`, and a topic has at most three cities
    (`MAX_RAIL_SITES`): about 10 to 11 MB of state (23 MB with long CJK names) and a 146 to 181 MB
    peak.
15. **The stack merged onto a moved `dev`.** `dev` had moved 86 commits since the stack branched;
    three files had both sides appending at the end, and a careless "keep both" duplicated six
    lines of a test file, which ruff caught. Each was resolved as dev's version plus the stack's
    hunk, and the checks `dev` runs now (a Checkov job the stack never saw) were run on the merged
    tree (friction 12.1, 12.4).

---

## 9. What the operator has to do

None of this is automatic.

1. **Merge** #285 into `dev` (it carries the stack and every rail PR).
2. **Re-apply `infra/bootstrap` by hand**, with `vision_region` (default us-west-2). Until then
   an apply with the worker turned on is refused.
3. **Set the `VISION_ENABLED` repository variable to `true`** and run a deploy (the
   [runsheet](../deployment-runsheet.md#vision-optional) has the steps; nothing in `terraform.tfvars`
   changes).
4. **Create the topics.** The four rail cities from `scripts/rail_topics.example.json`
   ([rail_access topics](../deployment-runsheet.md#rail_access-topics)), and a ship topic if
   wanted: `adapter: "satellite_vision"`, its `sites`, an `object_noun`, and editorial goals. Check
   `docs/risks/opencv-bushfire-watch-01.md` against it.
5. **Run Sydney for real:** a tick with the Overpass network on the deployed worker, for the
   measurements the submission needs (its table in `docs/hackathon/opencv-2026-submission.md`).
6. **COOL:** subscribe to *Cloud Optimized OpenCV For AWS Graviton4* on Marketplace (7-day trial,
   then usage pricing); on a `c8g` from the COOL AMI run
   `python3 scripts/vision_benchmark.py kernels --out cool-g4.json`, and the same with a stock
   OpenCV venv; pin the COOL `build_sha256` as `COOL_BUILD_SHA256`; terminate the instance and
   unsubscribe if not kept. `docs/deployment-runsheet.md` has the steps.
7. **For the submission:** the benchmark table (x86, Graviton stock, Graviton COOL), a run of the
   loop on dev showing an artefact rejected and a real change held for review, the architecture
   diagram (§1), and the video.

---

## 10. Still open

1. Does the COOL image run on Lambda's `arm64` CPUs, or does the COOL backend need ECS on a
   Graviton4 instance? That decides its Terraform.
2. Are COOL and Graviton4 (`c8g`) offered in us-west-2 and ap-southeast-2?
3. COOL's price after the trial.
4. Inter-region transfer cost of the worker's replies against reading `sentinel-cogs` from Sydney:
   measure both on dev.
5. Lambda `arm64`'s CPU generation: adding the benchmark's `cpu_identity` to the worker's reply
   would answer it from one invoke.
6. Whether "Sentinel models" in the original brief meant the Sentinel-2 data (assumed here) or a
   geospatial model trained on it; the latter would be another worker backend.
7. **The COOL run on Graviton**, by the operator (§9): the benchmark's Graviton stock and COOL
   rows, and the 1.3 times slower arm64 core that the rail window cap assumes, are not measured.
8. **Grade-separated crossings read as junctions.** Two lines crossing on a bridge are crossing
   pixels; degree and betweenness inherit it. OpenStreetMap's `bridge` and `layer` tags could keep
   them apart.
9. **SCL is the only water mask.** The scene classification's water class decides what is water
   for the built-up mask; a flooded field or a misclassified shadow moves the mask at its edges.
10. **No ridership data.** Activity is built-up density in a catchment; a ridership or timetable
    source would turn the flags from hypotheses into measurements, and none is open for all four
    cities.
11. **Overpass availability.** Two public endpoints, rate-limited and sometimes slow. The cached
    network and the retry back-off mean a city is still measured, but a first fetch that keeps
    failing leaves a city unmeasured; a self-hosted or mirrored extract would remove the
    dependency.
