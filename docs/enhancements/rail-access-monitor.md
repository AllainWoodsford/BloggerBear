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
