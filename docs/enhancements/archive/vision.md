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
in plain words, is [../architecture/blogger-vision.md](../../architecture/blogger-vision.md). The
earlier topic idea is [supply-chain-tracker-enhancement.md](supply-chain-tracker-enhancement.md); a
bushfire watch came before it and was dropped as too risky (friction 9.2).

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
OpenStreetMap network through the Overpass API and keeps it in an object of its own in the content
bucket, and the Finding carries the figure to the article page. Its diagram is in
[rail-access-monitor.md §3](rail-access-monitor.md#3-architecture) and
[blogger-vision.md](../../architecture/blogger-vision.md#part-one-the-pipeline).

---

## 2. The strategy note, corrected

| The note said | Verdict | What is true |
|---|---|---|
| Agentic Vision Award: 30% OpenCV 5 + agent integration, 25% orchestration/autonomy | Right | Full rubric: 30% integration, 25% orchestration and appropriate autonomy, 20% task effectiveness and evaluation, 15% failure handling / observability / security / human control, 10% UX, docs and demo. |
| Best Use of COOL: 30% for the Graviton/Arm component | Half right | The 30% row is *verified COOL integration* on Graviton. Then 25% architecture, **20% measured value against a baseline**, 15% innovation, 10% reproducibility. |
| Deploy the vision Lambdas for `arm64` and you score on COOL | **Wrong** | COOL is the **Cloud-Optimized OpenCV Library**, OpenCV's own OpenCV 5 build for Graviton3/4 (KleidiCV), on AWS Marketplace as AMIs and Docker images. Stock OpenCV on an `arm64` Lambda is not COOL. |
| BloggerBear's compute is all Lambda | Mostly | Plus Step Functions, Scheduler and SQS. And every Lambda was x86_64. |
| Extend the ops assistant's agent and MCP server | **Wrong place** | This is about where the triage agent runs. That assistant is read-only on its own narrow role, so the vision agent lives in the pipeline. The assistant's read-only view of the pipeline (`vision_status`, `topic_findings`, the worker's log) is [ops-assistant-vision.md](ops-assistant-vision.md). |
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
| [#295](https://github.com/AllainWoodsford/BloggerBear/pull/295) | J | Linear station adjacency, ghost-line walk and run lengths; incremental thinning (`THINNING_REACH_PX`) and speed guards that count the work; the rail window cap (`RAIL_MAX_WINDOW_PX`) |
| [#297](https://github.com/AllainWoodsford/BloggerBear/pull/297) | K | A topic's `findings_window_hours` (1 to 336 h, default 24) through the admin API and CLI, read by the daily cycle and Re-Write (`common/findings_window.py`) |
| [#298](https://github.com/AllainWoodsford/BloggerBear/pull/298) | L | The integration review's fixes: the daily trending digest leaves out every topic for which `compliance.requires_manual_review` is true; a rail Finding carries only the maps of the sites its summary reports; the SPA removes a figure whose image fails to load; one measuring rule for both vision adapters (`vision_sites.time_to_measure`; `time_budget_seconds` 45 to 80); the worker follows no redirect and reads only its byte range; one parser for `VISION_ALLOWED_URL_PREFIXES` (`vision_contract.allowed_prefixes`); editorial defaults for both vision adapters; the compliance review's longer quote readings only when the draft holds them; the ops assistant's log review leaves the worker out of its default sweep; desert areas to one decimal, "simulated link" in the legend, figure invalidation only for an article with figures |
| [#296](https://github.com/AllainWoodsford/BloggerBear/pull/296) | F2 | This document for two tasks, the city topic templates, the runsheet's rail section, progress and friction, and PR L documented: the documentation cut-off for the vision pipeline |

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
| Rail network | `lambdas/vision/network.py` | OpenStreetMap ways rasterised (`polylines`), thinned with `MORPH_HITMISS` (core OpenCV; no `ximgproc`; after the first pass only within the bounding box of what the last pass removed, grown by `THINNING_REACH_PX`, 8 px), junctions and ends (`filter2D`), stations snapped (`DIST_LABEL_PIXEL`), the skeleton read back as a networkx graph with lengths, corridor visibility and tunnels; `station_adjacency`. |
| Rail analysis | `lambdas/vision/rail_analyse.py` | `RailParams`; the whole pass in order (masks, heat, network projected, reach, deserts, catchments, graph, measures, flags, simulated links); every metric rounded, JSON-clean and with its parameters echoed. |
| Rail figure | `lambdas/vision/rail_annotate.py` | The heat map over the red band, the analysis drawn on it, the caption and legend in a strip under the map; PNG level 9 within a 2 MiB budget (§6.6). |
| Worker | `lambdas/vision_worker_handler.py` | Validates, reads, analyses, annotates; replies with metrics, figure, build record, I/O and per-stage timings. Dispatches on `task`. Stateless. Never follows a redirect: any 3xx is `unreadable_scene`, its body unread and its `Location` unparsed (`_NoRedirectSession`); a body is streamed and read no further than the end of the byte range asked for (at most one 64 KB chunk past it, `_READ_CHUNK_BYTES`), even from a server that ignores `Range` and answers 200 with the whole file. |
| Contract | `lambdas/common/vision_contract.py` | Request/reply schema shared by both sides; `TASKS`; asset URL allowlist (`allowed_prefixes`, the one parser of `VISION_ALLOWED_URL_PREFIXES` for the worker and the client); parameter bounds per task; the rail network's shape and caps (`validate_network`, `NETWORK_LIMITS`); error codes. |
| Client | `lambdas/common/vision_client.py` | Invokes the worker in the region its ARN names; sends the task and network; validates every reply; every failure is `VisionError`. |
| OpenStreetMap | `lambdas/common/osm.py` | The Overpass query, two allow-listed endpoints and one fallback hop, decimation to 20 m, caps, a streamed body under 20 MB, coded errors (`OsmError`), `is_stale`, the credit (`OSM_SOURCE`). No numpy or OpenCV. |
| Shared site machinery | `lambdas/common/vision_sites.py` | What both vision adapters share: config checks and bounds, the STAC search and choice of scene, the figure store, clear entries, the time `Budget` and when a tick may still ask the worker (`time_to_measure`: `MEASURE_RESERVE_SECONDS`, 45 s, of the budget left; waited on for at most `WORKER_READ_TIMEOUT`, 58 s), the site order, `site_key`, the first-observation rule, the credits. |
| Ship adapter | `lambdas/common/adapters/satellite_vision.py` | Sites, history, the count diff, triage hook, attribution, summary prompt, review evidence. |
| Rail adapter | `lambdas/common/adapters/rail_access.py` | One city per site, one city a day by the topic's rota: scene search, the city's network object, the worker call, history, the served-share, desert and station-count diff, triage, figures and planner notes (of the sites its summary reports, `_reported_scenes`), source references, summary prompt, review evidence. |
| Planner notes | `lambdas/common/rail_notes.py` | The rail flags and simulated links as one generated sentence each (§6.8). |
| Figures path | `lambdas/common/figures.py` | Shapes and checks `[{"key", "caption", "alt"}]` from adapter to Finding to article; the site key `articles/figures/<article>/<n>.png`; `static_pages.publish_figures` does the copy and leaves a failed copy off the static page; the SPA (`frontend/app.js`) removes a `<figure>` whose image fails to load, so it shows what the static page shows. |
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
  has one vCPU. On one core, through `lambda_handler`, the worst contract-valid networks (ways
  reaching the window's edges) take up to 37.5 s at 4096 px, about 49 s on arm64 and 59 to 79 s
  with the read, past the client's 58 s; at 3,584 px up to 25.5 s and up to 1.32 GB (two
  different networks), about 33 s on arm64 and 41 to 56 s with the read, within it (§7, §8
  lesson 13). The four starting cities need at most 3,327 × 3,306 px (Sydney) with the reader's
  16 px pad. A larger site is `window_too_large` once the reference band's header is read, before
  any tile.
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
| `max_sites_per_tick` | 5 | Bound on worker calls per tick; the time budget usually allows fewer (next row). |
| `time_budget_seconds` | 60 | The measuring budget, 45 to 80 s (the research tick times out at 120 s). The worker is asked only with 45 s of it left (`vision_sites.time_to_measure`), so with the default 60 a tick asks it only in about its first 15 s: only the sites whose new scene is found by then are measured (how many depends on the search's and the worker's times; often fewer than `max_sites_per_tick`). The rest rotate to later ticks and keep their place. |
| `triage` | true | Run the agent on a numeric change. |
| `triage_max_tool_calls` | 3 | The agent's tool budget. |
| `triage_deadline_seconds` | 85 | Seconds after the tick started by which the agent must have answered. |

### A `rail_access` topic

Every key, its default and its bounds are in one table, in
[configuration.md, A `rail_access` topic](../../configuration.md#a-rail_access-topic), read from the
adapter's `DEFAULTS` (`lambdas/common/adapters/rail_access.py`); the rail parameters `params` may
override are the table in §4. One ready topic body, `rail-cities`, is
`scripts/rail_topics.example.json`: the four starting cities and a `rotation` that gives each city
its days (the day-subject round, [dailysubject_hook.md](dailysubject_hook.md)), a daily run at
09:05 Sydney time that writes about the day's city alone, and `findings_window_hours` 192 (8 days,
more than Boston's 7 between its Thursdays), so each article drafts every Finding about its city
since that city's last. The runsheet's
[rail_access topics](../../deployment-runsheet.md#rail_access-topics) creates it and says how the
window works. What differs from a ship topic, in short: at most eight sites (`MAX_RAIL_SITES`), an
optional rota, one city measured per tick by default, `reach_m` and each network's `osm_ttl_days`,
the agent's `web_context`, a first observation per city, and three material rules (served share,
desert area, station count, §6.2) in place of the count's two thresholds.

**A city's first network takes the measuring time.** With no network stored for the city's
polygon, the fetch starts when `OSM_RESERVE_SECONDS` (30 s) of the budget remain, like a refresh,
but may take the rest of the budget less `FIRST_FETCH_MARGIN_SECONDS` (10 s, for the parse and the
stores: a reply still arriving at the deadline is shut, so the transfer is inside it), never less
than 30 s: about 60 s at the default `time_budget_seconds` of 70, about 70 s at the cap of 80. Each
Overpass hop reads for up to `FIRST_FETCH_TIMEOUT_SECONDS` (75 s) within that deadline, and its
query declares the same runtime to the server (`[timeout:N]`, the hop's read timeout rounded down),
so Overpass neither stops a query the client still waits for nor runs one the client has left.
A refresh of a stored network keeps 30 s and 25 s a hop
(`osm.TIMEOUT_SECONDS`): the stored copy serves meanwhile. A first fetch that takes most of the
minute leaves under 45 s, so the city is measured on its next turn, against the network now
stored; a failed one is tried again on the next tick (no 6-hour wait: nothing serves meanwhile).
Raise `time_budget_seconds` to 80 for a heavy city; the runsheet's
[rail_access topics](../../deployment-runsheet.md#rail_access-topics) says when (§8 lesson 16).

### Bounds every vision topic shares

`common/vision_sites.parse_site_config` refuses a config the tick could not keep to, before any
call: numbers must be finite; `time_budget_seconds` in [45, 80] (the tick has 120 s, and under
`MEASURE_RESERVE_SECONDS`, 45 s, no tick could ever ask the worker);
`triage_deadline_seconds` in (0, 90]; `triage_max_tool_calls` 0 to 5; `history_size` 2 to 20;
`min_baseline` at least 1 and under `history_size`; `max_sites_per_tick` 1 to the adapter's site cap;
`coverage_floor` in [0, 1]; no negative `*_threshold`; `triage` and `web_context` true or false; a
site id of letters, digits, `_` and `-` (at most 64, since it becomes part of an S3 key), unique;
a site name of 1 to 80 characters of plain text. The admin API stores `adapter_config` as given;
a config the adapter refuses fails the topic's research tick with the reason in its log.

### Terraform

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `vision_enabled` | dev and production roots, from CI (`VISION_ENABLED`); never set in `terraform.tfvars` | `false` | Create the worker, give the research tick its ARN, and give the pipeline's shared execution role (`lambda_exec`) the right to invoke it. CI hands the variable to Terraform as `TF_VAR_vision_enabled`, and a value in `terraform.tfvars` would beat it, so the switch would no longer switch. The steps: [the runsheet's Vision (optional)](../../deployment-runsheet.md#vision-optional). |
| `vision_region` | dev and production | `"us-west-2"` | Where the worker runs. Must equal the bootstrap's. In production it and the prefix must fit the worker's bucket name, 21 characters together ([configuration.md](../../configuration.md#environment-terraform-variables)). |
| `vision_region` | `infra/bootstrap` | `"us-west-2"` | Where the deploy roles may create `<prefix>-*-vision-*` Lambdas and log groups. |
| `memory_size`, `timeout` | module | 2048 MB, 60 s | The worker's size; the timeout stays under the client's default 90 s. Both vision adapters give up first, at 58 s (`vision_sites.WORKER_READ_TIMEOUT`), by design. |

The full list, with the switch, is [configuration.md, Vision and rail access](../../configuration.md#vision-and-rail-access).

### Environment variables

| Variable | Set on | Meaning |
|---|---|---|
| `VISION_WORKER_ARN` | research tick | The stock worker; empty when off, which reads as "not configured". |
| `VISION_COOL_WORKER_ARN` | research tick | The COOL worker, once it exists. |
| `VISION_BACKEND` | worker | `opencv` or `cool`: what this deployment is. |
| `COOL_BUILD_SHA256` | COOL worker | The pinned COOL build fingerprint. |
| `VISION_ALLOWED_URL_PREFIXES` | worker, research tick | Optional, comma-separated; defaults to the `sentinel-cogs` bucket. Each entry must be `https://<host>/` and a path; any other entry is skipped, with a printed line, and if every entry is skipped the default applies (`vision_contract.allowed_prefixes`, read by both). No Terraform variable sets it, and Terraform owns both functions' environment, so a value set by hand lasts only until the next apply (§10). |

The rail task adds none: Earth Search, the `sentinel-cogs` bucket, Overpass and GDELT need no key.

### Topic flag: `force_manual_review`

Any topic may set it; `satellite_vision` and `rail_access` topics always have it (the admin API
forces it for every adapter in `MANUAL_REVIEW_ADAPTERS`, on create and on update). The daily cycle
adds "this topic's articles are always reviewed by a person before publishing" to the hold
reasons, and the daily trending digest, which publishes with no person, leaves out every topic
for which `compliance.requires_manual_review` is true (#298), so no text of such a topic is
published without a person on any publish path. Unlike `is_financial`, drafting is unchanged and
the compliance review still runs, so the moderator sees its verdict. Stored only when true.

### Editorial goal

A topic created without `editorial_goals` (or without a `primary_focus` in them) is ideated and
drafted under its adapter's default goal (`ADAPTER_DEFAULTS` in `common/editorial_resolver.py`,
#298), not the global one's "randomly sampling 5 to 10 highly relevant articles". Both vision
defaults restate their summary prompt's limits: report only what was measured, with the capture
dates, and use no number or place name that is not in the findings; the rail one gives no
ridership, patronage, journey time, cost, cause, forecast or recommendation, and the ship one names
no ship, vessel, owner, company or person, gives no cause, forecast, price, market or trading view,
and never calls an area safe, clear or unaffected. A topic's own `primary_focus` wins over it.

### Where things are stored

| What | Where |
|---|---|
| Per-site history, `last_error`, `last_attempt_at`, triage trails | `snapshots/<topic>/running-state.json` in the content bucket, and each Finding's snapshot (`snapshots/<topic>/<captured_at>.json`) |
| Each site's decision ring: the last 12 decisions, each with the numbers it was made from, its code, what moved, the agent's trail with each tool's result, who measured, and whether it was reported ([the evidence trail](vision-evidence.md)) | `decisions` on the site's record in the same running state; one `vision_decision` line per entry in the research tick's log |
| The rail task's OpenStreetMap network, per site, with its polygon's `site_key`: its only copy, written by the tick's fetches and by the operator's `network_prefetch` alike | `vision/<topic>/<site>/network.json` in the content bucket, outside `snapshots/` (never in the running state, a prompt or the review evidence) |
| What the site's record knows of its network without holding it (`network_key`, `network_fetched_at`, `network_site_key`, `network_counts`), `network_error` after a failed refresh, and `failed_scene` after a refusal | The running state |
| When the topic's first observation was reported, and each city's (rail) | `first_observed_at` in the running state, and on each rail site's record |
| Figures, as the worker drew them | `vision/<topic>/<site>/<scene>.png` in the content bucket (private) |
| A Finding's figures and planner notes | `figures` (`[{"key", "caption", "alt"}]`) and, for rail, `planner_notes` (at most 7) on the Finding, both only for the sites its summary reports |
| A published article's figures | `articles/figures/<article>/<n>.png` in the site bucket (copied at publish; removed on unpublish and take-down, which also ask the CDN to drop `/articles/figures/<article>/*` for an article with figures), and `figures` on the article row |
| Triage spend | Stats page, category `vision_triage` |
| Worker package | `<prefix>-<env>-vision-artifacts-<account>-<region>` bucket, `vision-worker/<md5>.zip`, 14-day expiry |

A rail history entry keeps, per scene: `scene_id`, `captured_at`, `scene_cloud_cover`, `coverage`,
`built_up_km2`, `served_km2`, `desert_km2`, `served_share`, `station_count`, `edge_count`, `hubs`,
`flag_count`, `quality_flags`, `backend`, `build_sha256`, `timings_ms`, `image_key`, `assets` (for
`look_again`), `network_fetched_at`, `network_stale`, `site_key`, `params` (as the worker echoed
them) and, once judged, `triage` (`verdict`, `reason`). Only the newest entry also keeps the lists
(`NEWEST_ONLY_KEYS`: `stations`, cut to ten fields and 2,000 rows, `flags`, `suggestions`,
`deserts`): the figure, the review evidence, the agent's brief and the planner notes read them from
it alone, and dropping them from older entries, with each network in its own object, is what keeps
eight cities' state near 7 MB (§8).

A scene the worker failed on leaves `failed_scene` (`id`, `code`, `at`, `site_key`, `params`) on
the site beside its `last_error`. One refused for what it is (`window_too_large`,
`site_outside_scene` or `bad_request`, the adapter's `REFUSED_SCENE_CODES`) would be refused again,
so it is not sent again until a newer scene appears, 6 hours pass (`NETWORK_RETRY_HOURS`), or the
site's polygon or parameters change: an edited box or reach is a new request, sent on the next
tick. `unreadable_scene` (which also covers a timed-out or throttled read) and every other failure
are tried again on the next tick. A measurement clears both.

---

## 6. How the decisions are made

### 6.1 The rules this keeps

- **Rule 2, diff-first:** OpenCV measures every new scene; Bedrock is only called after a numeric
  material change (the triage), and then for the summary.
- **Rule 3 and the risks doc's item 9:** no vision article publishes without a person, on any
  publish path: the daily cycle holds every one, the daily trending digest leaves these topics out
  (#298), and approving or force-publishing is a person's act.
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
`min_baseline` earlier clear scenes **measured the same way** (the same polygon's `site_key` and the
same echoed parameters, so an edited reach or polygon starts a new baseline, reported as "baseline
still building", rather than reading as a change in the city), and any of: the served share moved by
at least `served_share_threshold` from their median; the desert area moved by at least
`desert_relative_threshold` times the larger of its median and 1 km²; the station count differs from
that of the previous scene measured the same way (clear or not: a station count does not depend on
cloud). Values are compared as stored, to 4 decimal places. The diff summary prints shares as whole
percentages and desert areas to one decimal at every size (`_area`, #298: "12.3 km² beyond 1 km of a
station against 10.7 km² (+15%)"), since whole square kilometres could print a material move as no
move. One decimal shows any move of more than 0.1 km², and the default threshold (0.10 of a median
of at least 1 km²) is never under 0.1 km²; a topic with a lower `desert_relative_threshold` can
still see one area printed twice, with the percentage beside it.

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
  existing news search (`common/web_search.search_web`): GDELT, with the AgentCore web-search
  gateway as its fallback when GDELT fails; within 10 s.

Bounds are in code: at most `triage_max_tool_calls` tool calls, at most that plus two model turns,
a deadline (`triage_deadline_seconds` after the tick started) under which no model turn starts with
less than 10 s left, and a strict JSON answer `{"verdict": "real" | "artefact", "reason": "…"}`.
`look_again` waits on the worker for at most the adapters' read timeout (58 s,
`vision_sites.WORKER_READ_TIMEOUT`, for both since #298), cut to the deadline less
the 10 s answer turn and the 5 s connect allowance, rounded down to 5 s; under 10 s (ships) or 30 s
(rail: a city's run takes 20 to 40 s and a worker call that cannot finish is still billed) it is
refused and the model told to answer. A malformed answer, an unknown verdict, a Bedrock error, no
model, the turn limit or the deadline all mean **artefact**: an unsure agent never makes a Finding.

What the agent reads from outside is data, not instructions: the rail system prompt says so;
news results are cut (title 200, snippet 300, source 100 characters) and made one plain line each,
a link kept only when it is http(s) and at most 500 characters, a date only when it is one; the
brief carries at most 20 flags with `flag_count`, and never the network or the station list. Its
reason is one plain line of at most 300 characters, and a summary quotes it as
`the vision agent's note (not a measurement): "…"`. The trail, with each tool's result (what
`look_again` measured at which settings, the previous scene's id, how many scenes or news results
the other tools gave), how the loop stopped, its time and its spend, is kept in each site's
decision ring in the running state (the last 12 decisions, `common/vision_decisions.py`) and
logged as one `vision_triage` line of codes and numbers ([the evidence trail](vision-evidence.md));
the spend lands on the Stats page.

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
wide. The legend's entries (`LEGEND_ENTRIES`): "station, larger = busier catchment, thick ring =
high isolation weight", "hub", "flagged (isolated high demand, single point of failure, ghost
line)", "simulated link" (#298; "simulated", as the caption and the planner notes say, never
"suggested") and "beyond walking reach". The PNG is written at zlib's level 9 and kept within a
2 MiB budget by drawing it again at 0.8 times the size, down to a 512 px map (the client refuses a
figure over 3 MiB). The page caption (at most 400 characters, `figures.MAX_CAPTION_CHARS`) is:

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
kind, type, stations and text), which leaves out a scene the agent judged an artefact. The
Finding's figures take the same rule (`_reported_scenes`, #298): a map only for a site its summary
reports, never for one under the coverage floor or one the agent doubted. The figure draws the
same flags and links (§6.6).

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
as the worker's one vCPU is. **Indicative only:** the machine is a shared container, two runs
differed by up to 40 % on some rows, and no row here is a COOL ratio (COOL is measured against stock
on one Graviton4 instance, §9):

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

**The COOL and Graviton rows are still to be measured** (§9).

**Evaluation.** From the submission round on, every measurement a judge reads is a run file
under `docs/hackathon/evidence/runs/` ([its README](../../hackathon/evidence/README.md) has the
schema and the kinds): the benchmark writes one per run (`--out`), with the code's commit, the
CPU, the build fingerprint, the inputs' ETags, every sample, the rail metrics and their digest,
and on Linux the OpenCV libraries mapped during the run; `compare` puts two runs of one machine
in a ratio with a bootstrap interval and checks they measured the same thing; `controls` shows
the COOL guard refusing what it must. The evaluation's inputs are pinned in
`scripts/vision_eval_manifest.json` (46 Sydney scenes: clear, season, cloud, no-data, smoke, the
2019 baseline, the 2024 year set), and its first run file is `vision_eval.py coverage` over all
of them (16 October, x86): the share of each scene the worker can measure, why a scene is
unusable, and how many of the year's items the production search would pick only to find them
under the floor. The tables above stay as the first indicative numbers; the report cites the
files.

**Evaluation results: [`RESULTS.md`](../../hackathon/evidence/RESULTS.md)**, the index of every run
file, one table per kind, written by `vision_eval.py index` and checked in CI. The evaluation's
first half (Q2a) adds `replay` (the real adapter over pinned Sydney scenes, tick by tick, with the
deployed worker and the real model, or a recording a judge re-runs without AWS: the A2 trace,
rendered to `docs/hackathon/evidence/replay/`), `failures` (the constructed failure catalogue, its
tests checked to be collected, its real-data rows through the worker) and `access-baseline` (E5:
the production distance transform against the precise one and SDG 11.2.1's straight-line buffer).
The second half (Q2b) adds `agent` (thirteen cases, balanced between the classes, scored per class
against numeric-only and always-artefact, with the failure cases C6-C9), `counterfactual` (only
OpenCV's output changed, at the agent's re-measurement and at the coverage gate) and `stability`
(E3: the year set through the production rule on one network; the false-alarm rate with its
interval). Their real-data runs are the operator's, in the
[runsheet](../../deployment-runsheet.md#the-evaluations-real-data-runs); until they are committed,
RESULTS.md holds the coverage run alone.

The worst cases the worker must survive were measured separately (PR J; the figures are in the
comments on `RAIL_MAX_WINDOW_PX` in `vision_worker_handler.py`, `MAX_WINDOW_PX` in
`vision/scene.py` and `MAX_GRAPH_NODES` in `vision/rail_analyse.py`): on one core of the same
Xeon (`taskset`, `cv2.setNumThreads(1)`, two runs each), through `lambda_handler` (the bands read
from in-memory COGs, the analysis, the figure, the reply), on contract-valid networks built to be
slow, under both graph caps, with ways that reach the window's edges as clipped OpenStreetMap ways
do:

| Window | Worst compute, peak RSS | Taking arm64 as 1.3 times slower | With a 10 to 30 s read | Against the client's 58 s read timeout |
|---|---|---|---|---|
| 4096 px | up to 37.5 s; up to 1.69 GB | about 49 s | 59 to 79 s | past it |
| 3,584 px (the rail cap) | up to 25.5 s; up to 1.32 GB | about 33 s | 41 to 56 s (the read covers 77 % of the pixels) | within it |

The 3,584 px budget assumes the read scales with the window's pixels; a full 30 s read on top of
the worst case would not fit. That worst case is constructed: two 1.3 km-wide solid bars of track
along both edges of the window, which no real network looks like. At 3,584 px, the reviewer's
edge-reaching inputs with at most one bar stay within 21.9 s. No real OpenStreetMap network has
been timed: the "about 9 s" for a city is PR J's synthetic city, 180 stations and 221 nodes, 9.2
to 9.6 s at 4096 px on four cores (the analysis and the figure, not pinned to one core). The
deployed Sydney run with the Overpass network (§9) will give the real figure. A worker that times
out is an `invoke` error the adapter retries on its next tick. The 1.3 factor is an assumption
until the Graviton run (§10).

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
    removed before the next element, on the drawing's bounding box, at most 32 passes; after the
    first pass, only within the bounding box of what the last pass removed, grown by
    `THINNING_REACH_PX` (8 px) on every side, with the same skeleton pixel for pixel (PR J;
    friction 12.22). One-pixel `polylines` are nearly thin already, so a real network needs a few
    passes.
11. **Overview reads to fit a city in the window.** A 60 km city at 10 m is 6,000 px a side, past
    the reader's 4096 px window and far past the worker's time. The rail task works on SWIR16's
    20 m grid instead, and reads the 10 m red and NIR from the COG overview whose pixel is 20 m:
    the reader now follows the IFD chain, keeps each reduced image as a level, and
    `level_for_pixel_size` picks it; a file without one is read at full resolution and reduced with
    `INTER_AREA`. On Sydney that is 54 MB in 192 range requests for a 66 km box.
12. **The tick's 120 s, shared between Overpass, the worker and the agent.** The research tick has
    120 s and 512 MB for everything: the STAC search, a network fetch, the worker, the agent, the
    summary call and the stores. The split, in code: one city per tick (`max_sites_per_tick` 1); a
    70 s measuring budget (`time_budget_seconds`, 45 to 80); the network fetched only with
    30 s of it left and, for a refresh of a cached one, finished within them, both Overpass hops
    included (`OSM_RESERVE_SECONDS`, 25 s per hop), while a city's first network may take the rest
    of the budget less 10 s (lesson 16); the worker asked only with 45 s left
    (`MEASURE_RESERVE_SECONDS`) and waited on
    for 58 s (`WORKER_READ_TIMEOUT`, under its own 60 s timeout; the client's default 90 s would
    run the tick out), a rule the ship adapter lacked until the integration review found it could
    run the tick out of time, so both adapters now keep it in one place
    (`vision_sites.time_to_measure`, #298) and a budget under 45 s is refused; the agent's answer
    due 85 s after the tick started
    (`triage_deadline_seconds`, never over 90), no model turn with under 10 s left, `look_again`
    cut to the time left and refused under 30 s, `web_context` within 10 s; at least the last 30 s
    for the summary call and the stores. And the order of work keeps a slow Overpass from costing a
    measurement: the scene comes first; a new scene is measured with the cached network even when
    it is past its TTL (`network_stale`); a stale network is refreshed only on a tick with nothing
    new to measure, and after a failed refresh not again for 6 hours (`NETWORK_RETRY_HOURS`).
13. **One core, not four.** The first worst-case timings of the rail analysis were taken on the
    build container's four cores, where OpenCV threads: 20 to 21 s at 4096 px, comfortably inside
    the worker's 60 s. The worker has one vCPU at 2048 MB. Pinned to one core (`taskset`,
    `cv2.setNumThreads(1)`) and run through `lambda_handler` with ways that reach the window's
    edges, the worst contract-valid networks take up to 37.5 s at 4096 px, about 49 s at an assumed
    1.3 times slower arm64 core, which with a 10 to 30 s read is 59 to 79 s, past the client's
    58 s. So the rail task got its own window cap, `RAIL_MAX_WINDOW_PX` = 3,584 px (71.7 km at
    20 m; Sydney needs 3,327): there the same inputs take at most 25.5 s (about 33 s on arm64,
    41 to 56 s with the read) and peak at 1.32 GB at most, on another network (§7). Station
    adjacency, the ghost-line walk and the run lengths were made linear first (PR J): for 2,000
    stations off one junction grid, the old adjacency walk asked a node for its neighbours
    4,052,000 times, the new pass asks 4,025 times and takes up to 1.0 s; and the thinning works
    only in the box round what its last pass removed (lesson 10). The lesson: time a bound on the
    cores the target has, not the ones the build machine has.
14. **The state a city costs.** A city's network at the caps is 2.6 to 3 MB of JSON, and the
    first state kept every scene's station list: three cities, twenty scenes each, made a 54 MB
    state and a 390 MB peak in the 512 MB tick. Only the newest entry now keeps the lists
    (`NEWEST_ONLY_KEYS`), every entry keeps `flag_count`, and a topic has at most three cities
    (`MAX_RAIL_SITES`): about 10 to 11 MB of state (23 MB with long CJK names) and a 146 to 181 MB
    peak. The day-subject round then moved each network out of the state into its own object, the
    one the out-of-band prefetch (#315) already wrote: eight cities with full decision rings make a
    7.3 MB state (16.4 MB with CJK names) and a 107 MB peak (128 MB) for a tick measuring one, so
    a topic may now hold eight (`MAX_RAIL_SITES`'s comment has the measurements).
15. **The stack merged onto a moved `dev`.** `dev` had moved 86 commits since the stack branched;
    three files had both sides appending at the end, and a careless "keep both" duplicated six
    lines of a test file, which ruff caught. Each was resolved as dev's version plus the stack's
    hunk, and the checks `dev` runs now (a Checkov job the stack never saw) were run on the merged
    tree (friction 12.1, 12.4).
16. **The first live tick found three defects no test could.** The first rail topic in dev,
    `trains-sydney` (the Sydney box of `scripts/rail_topics.example.json`), was created on 16
    October and measured nothing that day; the owner found and fixed three defects in turn, each
    hidden behind the one before. (a) Every tick failed before the adapter ran: the research tick
    reads a running state with GetObject and expects `NoSuchKey` on a first tick, but S3 answers
    a missing key with 403 `AccessDenied` to a caller without `s3:ListBucket` on the bucket, and
    the Lambda role had none (#305 grants it, with a wiring test). (b) Then every tick recorded
    `osm` with "HTTP 406 from https://overpass-api.de/api/interpreter": the Overpass POST went out
    with no User-Agent, as python-requests, which overpass-api.de refuses with a 406 (not
    transient, so the mirror was never asked; the mirror asks for a User-Agent with a 429). #310
    sends the bot's name on every hop. (c) Then the fetch itself could not succeed: the first
    network had 30 s (`OSM_RESERVE_SECONDS`), each hop 25 s, and the query `[timeout:25]`, while
    Sydney's query took 54 s from a workstation (5,925 elements, 5.5 MB); a run through
    `fetch_network` with the 30 s deadline hit the 25 s read timeout, and both endpoints then
    answered 502 and 500 for several minutes. #312 gives the first fetch the measuring time (§5): the
    rest of the budget less 10 s, each hop's read and the query's `[timeout:]` following it, the
    measurement on the next tick; the network is then cached for up to `osm_ttl_days`, so the cost
    is paid once per city. The tests had moto for S3 and fakes for Overpass, and neither models
    what failed: moto does not model IAM's effect on GetObject's error code, and a fake models
    neither a third party's User-Agent policy nor its real latency. The lesson: a first deploy
    needs one real tick per adapter, read in its log, before anything depends on it (friction
    12.25).

---

## 9. What the operator has to do

None of this is automatic.

1. **Merge** #285 into `dev` (it carries the stack and every rail PR).
2. **Re-apply `infra/bootstrap` by hand**, with `vision_region` (default us-west-2). Until then
   an apply with the worker turned on is refused.
3. **Set the `VISION_ENABLED` repository variable to `true`** and run a deploy (the
   [runsheet](../../deployment-runsheet.md#vision-optional) has the steps; nothing in `terraform.tfvars`
   changes). It also turns the worker on in production at the next release, unless the
   `production` environment sets `VISION_ENABLED` to `false` until the dev trial is done.
4. **Create the topics.** The rail topic over the four cities from `scripts/rail_topics.example.json`
   ([rail_access topics](../../deployment-runsheet.md#rail_access-topics)), and a ship topic if
   wanted: `adapter: "satellite_vision"`, its `sites`, an `object_noun`, and editorial goals if the
   adapter's default (§5, "Editorial goal") is not what you want. Before it goes live, check the
   five things the bushfire watch's risk review asked of any imagery topic: the Copernicus and
   "processed imagery" credits are on the figure and the page; the articles say what was seen,
   never a cause or who is to blame; each figure carries the scene's capture time and the article
   says the imagery is days old; no private property, and nothing finer than Sentinel-2's 10 m;
   place names come from the data, never guessed.
5. **Run Sydney for real:** a tick with the Overpass network on the deployed worker, for the
   measurements the submission needs (the live run in
   [the technical report](../../hackathon/technical-report.md#6-evaluation) §6).
6. **COOL:** the Graviton session, exactly as the
   [COOL benchmark runsheet](../../cool-benchmark-runsheet.md) gives
   it: the reconnaissance (done on 16 October, `docs/hackathon/evidence/cool/recon.md`: an AMI,
   COOL 3.1 with `cv2` 5.1.0-dev, us-west-2, Python 3.12), a `c8g.xlarge` (or `c8g.2xlarge`) from the COOL AMI with
   IMDSv2 and SSM, COOL's interpreter on the vendor's own activated environment (a wrapper, with
   the benchmark's packages beside it; never opencv or numpy in it), stock 5.0.0.93 and 4.14.0.94 venvs
   beside it, the interleaved `kernels`, `scenes` (`--isolate`, `--cache`, pinned to one core),
   `compare` and `controls` runs of its step 8 written to `docs/hackathon/evidence/runs/`, and the
   proof checklist it ends with; pin the COOL `build_sha256` as `COOL_BUILD_SHA256`; terminate the
   instance and unsubscribe if not kept.
7. **For the submission:** the benchmark table (x86, Graviton stock, Graviton COOL), a run of the
   loop on dev showing an artefact rejected and a real change held for review, and the video. The
   package is in `docs/hackathon/`: the [technical report](../../hackathon/technical-report.md), the
   system and agent [diagrams](../../hackathon/diagrams/README.md), the
   [reproduction guide](../../hackathon/REPRODUCE.md) and the [video script](../../hackathon/video-script.md).

---

## 10. Still open

1. Does the COOL image run on Lambda's `arm64` CPUs, or does the COOL backend need ECS on a
   Graviton4 instance? That decides its Terraform.
2. Are COOL and Graviton4 (`c8g`) offered in us-west-2 and ap-southeast-2? **Answered on
   16 October** (`docs/hackathon/evidence/cool/recon.md`): the listing names a COOL 3.1 AMI in both
   (26 Regions in all), and a `c8g.large` ran it in us-west-2.
3. COOL's price: the listing's table on 16 October gave USD 0.01 an hour on `c8g.large` and 0.02 on
   `c8g.2xlarge`, the software fee only (recon.md); the session's hours and fees go in `session.md`.
4. Inter-region transfer cost of the worker's replies against reading `sentinel-cogs` from Sydney:
   measure both on dev.
5. Lambda `arm64`'s CPU generation: answered from the first invoke once deployed, by the worker's
   `build.cpu` (`common/vision_contract.cpu_identity`, shared with the benchmark) in its reply and
   its `vision_measure` line; the runsheet's query (d) groups the worker's runs by build and CPU.
6. Whether "Sentinel models" in the original brief meant the Sentinel-2 data (assumed here) or a
   geospatial model trained on it; the latter would be another worker backend.
7. **The COOL run on Graviton**, by the operator (§9): the benchmark's Graviton stock and COOL
   rows, and the 1.3 times slower arm64 core that the rail window cap assumes, are not measured.
   The benchmark now records what that session needs (the run files, the rail metrics and their
   parity, the mapped libraries, the negative controls, the 4.x row) and the
   [COOL benchmark runsheet](../../cool-benchmark-runsheet.md) is the session's script; what remains is to run it. The Sydney network fixture the
   rail scenes need is captured after the deploy's first tick (the COOL runsheet's step 5), into
   `scripts/vision_benchmark_networks/`, whose `NOTICE.md` is in place.
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
12. **A Finding still being written when the daily cycle starts can be missed.** The research
    tick stores the adapter's `fetched_at`, taken during its fetch (at its start for the vision
    adapters), before the summary call and the write, as the Finding's `captured_at`, and writes
    the Finding at the tick's end. A Finding whose tick is still running when its topic's daily
    cycle starts is not there for that run, and is dated before the `last_article_at` that run
    stamps, so once that run writes an article no later window holds it. Older than PR K, and for
    every topic; a code follow-up.
13. **The COOL Dockerfile is not scanned.** `docker/vision-cool/Dockerfile` is covered by no IaC or
    misconfiguration scan: Trivy `config` and Checkov scan `infra/` only.
14. **Two stale size comments.** The comments on `NETWORK_LIMITS` (`common/vision_contract.py`) and
    in `common/osm.py` still say a network is "near 1.5 MB"; the adapter measured 2.6 to 3 MB.
15. **`common/osm.py` reads a redirect before refusing it.** It passes `allow_redirects=False` to
    `requests.post`, but `requests` still reads a 3xx's body and parses its `Location` before the
    status reaches `_request`, the behaviour the worker now avoids with `_NoRedirectSession` (#298).
16. **The daily cycle never prints the error its top-level guard catches.** `_handle` returns it as
    `{"status": "error", "error": ...}` with no log line, so it shows only in the Step Functions
    execution's output, not in the function's log.
17. **The worker's bucket name can pass S3's 63 characters in production.** It is
    `<prefix>-<env>-vision-artifacts-<account id>-<vision_region>`
    (`infra/modules/vision-worker/main.tf`), so production needs the prefix and `vision_region` 21
    characters together: a 13- or 14-character prefix, which the prefix rule allows, or a
    home-Region `ap-southeast-2` with the default prefix, fails the apply. The docs state the limit
    ([configuration.md](../../configuration.md#environment-terraform-variables)); a shorter name (no
    Region, or a short hash) and the module's bucket in
    `test_the_longest_names_still_fit_with_the_longest_prefix` would remove it.
18. **The admin CLI stops waiting for a research tick after 30 s** (`_RESEARCH_TICK_TIMEOUT_SECONDS`
    in `scripts/admin_cli.py`), and a vision tick that measures a scene can run longer (a rail
    city's Overpass fetch, up to 58 s of worker, triage, the summary; the function has 120 s).
    The runsheet tells the operator to wait; a wait that covers the tick's 120 s would make "No
    new Finding after 30s" mean what it says.
19. **`VISION_ALLOWED_URL_PREFIXES` has no Terraform variable.** Terraform owns the worker's and the
    research tick's whole environment, so a value set in the console lasts until the next apply,
    and both then fall back to the `sentinel-cogs` default. Keeping one needs a variable passed to
    both functions.
20. **Fixed platforms, buoys and rigs inside a ship site are counted as vessels.** The `ships`
    task subtracts no known static objects; a per-site list of them was proposed
    ([supply-chain-tracker-enhancement.md](supply-chain-tracker-enhancement.md)) and not built.
