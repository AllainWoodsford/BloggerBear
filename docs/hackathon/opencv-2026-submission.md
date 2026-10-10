# OpenCV AI Competition 2026: submission draft

**Status:** draft, to be filled from the Sydney run and re-checked against the Devpost rules
before submitting · **Deadline:** 26 October 2026, 11:45 pm PDT · **Entry:** BloggerBear's
Rail Access Monitor · **Design:** [vision.md](../enhancements/vision.md) and
[rail-access-monitor.md](../enhancements/rail-access-monitor.md) · **How it works:**
[docs/architecture/blogger-vision.md](../architecture/blogger-vision.md)

Everything here describes what is on the integration branch or specified in the rail document;
where a part is specified but not yet merged, it is marked *(specified)*. Numbers in square
brackets are placeholders for the Sydney run. The competition pages were read through search
results from the build container, so the rubric headings below are to be re-checked against the
Devpost page by hand.

## Title

**Rail Access Monitor: an autonomous publisher that watches a city's rail network from space**

## One-line pitch

A research-and-publishing pipeline that measures a city's built-up density, the walking reach of
its stations and its rail network as a graph from free satellite imagery and OpenStreetMap, with
OpenCV 5 on Graviton, and writes an article only when a bounded agent has checked that a change
is real and a person has approved it.

## The problem

Where a city builds and where its trains reach drift apart slowly and quietly. The numbers that
would show it (where the roofs are, how far each neighbourhood is from a station, which stations
everything depends on) exist in open data that nobody looks at every week: Sentinel-2 revisits
every city every few days, and OpenStreetMap carries every line and station. Looking takes a
pipeline that measures the same thing the same way each time, says when something moved, is
honest about what it could not see, and never publishes a number without a person reading it.

BloggerBear is that pipeline for text feeds already: topics, a diff-first research tick, a daily
authoring cycle, reviews, a moderation queue, a static site. The entry adds eyes.

## What it does

### Technical execution

- **Perception with OpenCV 5, in a stateless worker on arm64.** The worker reads only the tiles
  under a site from the public Sentinel-2 Cloud-Optimized GeoTIFFs with HTTP range requests and
  a small reader for the TIFF subset they use (no GDAL: rasterio for arm64 did not fit Lambda's
  250 MB). The rail task works on a 20 m grid read from the COG overviews *(specified)* and uses
  NDBI and NDVI thresholds and the scene classification for the built-up mask, a Gaussian blur
  for the heat map, one distance transform with labels for served area, deserts and catchments,
  `polylines` and hit-or-miss thinning in core OpenCV to turn OpenStreetMap ways into a one-pixel
  skeleton, `filter2D` for junctions and ends, connected components for the segments, and
  networkx (in the worker only) for betweenness, hubs, distance to a hub and the simulated links.
  The ship task, the first on the same worker, uses an NDWI water mask, an adaptive threshold
  and `minAreaRect` filters.
- **A contract, not a convention.** Every request is validated (version, asset URL allowlist,
  parameter bounds, polygon size; the task's assets and the network shape *(specified)*); every
  failure is a code; every reply echoes the parameters it was measured with, the OpenCV build
  record and per-stage timings, so a result is reproducible and a COOL result is provable.
- **Tested without the network.** 128 new tests in the stack and more per rail PR, on
  synthetic scenes built in the tests; moto for S3 and DynamoDB; scripted fakes for the worker,
  the model and the sources. CI is green on every pull request.

### Innovation

- **Rail access from what a 10 m satellite can see.** Counting rail cars needs sub-metre imagery
  that is not free; the questions a city asks about access are measurable at Sentinel-2's
  resolution. The imagery supplies density and verifies the map; the map supplies topology.
- **The imagery scores the map.** Each mapped edge gets a corridor-visibility score from the
  imagery, tunnels apart: where a line is drawn but not seen, the pipeline says so rather than
  trusting either source.
- **Active perception as an agent tool.** The triage agent can re-measure the same scene with
  other parameters, compare the previous scene, read the site's history and (for rail) check
  the news *(specified)*, and must answer in a bounded number of turns before a deadline.
- **A pixel-to-graph step that takes any mask**, so a learned track segmentation could replace
  the rasterised ways later and nothing after it changes.

### Real-world impact

- Reports, for any city that fits in one Sentinel-2 tile, the served share of its built-up area,
  its transit deserts with their nearest station, its hubs and interchanges, the busy stations
  the network reaches least well, the station every journey depends on, the lines through
  little, and simulated orbital links and feeder corridors ranked by how much they bring the
  city closer to a hub.
- Says what it did not measure: coverage under cloud, stations that did not snap to a line, a
  fragmented graph, a map disagreeing with the imagery.
- Honest by construction about what the numbers are: built-up density is a proxy from
  reflectance, not population; reach is straight-line; a suggestion is a simulation on a graph.
  The summary prompt forbids causes, forecasts and planning or investment views.

### User experience

- The figure is on the page: the heat map over the scene, deserts outlined, lines drawn thicker
  the more paths run through them, stations larger the busier their catchment, hubs marked,
  unmeasured areas tinted, the credits burned in. The static page and the single-page app show
  it under the article with its caption.
- The article is two to four plain sentences with the station and line names exactly as
  mapped, the served share, the area beyond reach, the capture date, and what was not measured.
- Adding a city is adding a topic with the admin CLI; the operator's assistant reports what
  needs attention.

### Documentation

- [docs/architecture/blogger-vision.md](../architecture/blogger-vision.md): the pipeline end to
  end and the rail concepts for a reader who is not a planner.
- [docs/enhancements/vision.md](../enhancements/vision.md): the design, every component and
  setting, what was measured, what went wrong.
- [docs/enhancements/rail-access-monitor.md](../enhancements/rail-access-monitor.md): the
  review of the stack, the choice of angle, the architecture, one specification per PR, the
  risks and what bounds them.
- [docs/configuration.md](../configuration.md#vision-and-rail-access), the
  [deployment runsheet](../deployment-runsheet.md#vision-optional) and the
  [README](../../README.md#vision-satellite-imagery-and-the-rail-access-monitor).
- [docs/friction.md](../friction.md) §12: what went wrong and what it taught, entry by entry.

### Cloud delivery

- Everything is Terraform applied by GitHub Actions through OIDC, after security scans, lint and
  tests in the same run. The worker is an arm64, python3.12 Lambda in us-west-2, beside the
  `sentinel-cogs` bucket, packaged through its own artifacts bucket (the OpenCV zip is at the
  direct-upload limit), with a role that may write its own log group and nothing else.
- All state stays in the home region: the worker returns a few KB of metrics and one small PNG,
  and the adapter stores them. The vision region can be torn down at any time.
- Deployment is opt-in: `VISION_ENABLED` as a repository variable, off by default; merging the
  work deploys nothing.

## The special awards

### Agentic Vision

The loop is **perception → diff → agent → person**, and each arrow is bounded in code:

1. **Perception.** The worker measures every new scene with OpenCV. No model is involved.
2. **Diff.** The adapter compares the measurement with the site's own baseline (the median of
   earlier clear scenes; low coverage never counts). Nothing moved: the tick stops, and no model
   is called. That is the repository's rule 2, and the vision work keeps it.
3. **Agent.** A numeric change is a candidate, not a Finding. The agent sees the figure as an
   image and the metrics, may call `look_again` (re-measure with other settings), `previous_scene`,
   `site_history` and, for rail, `web_context` *(specified)*, within a tool budget, that plus
   two turns, and a deadline 85 s from the tick's start. The answer must be a strict JSON
   verdict. A malformed answer, an error, the turn limit or the deadline is "artefact": the
   agent fails closed. Its trail is stored and its spend lands on the Stats page.
4. **Person.** `force_manual_review` is forced on every vision topic; no vision article is
   published without someone approving it in the review inbox.

Observability: every reply carries timings, the build record and the quality flags; every site
records `last_error` rather than a quiet zero; the agent's trail can be replayed from the state.

### Best use of COOL

The same handler runs on two backends: stock `opencv-python-headless` and OpenCV's COOL build
for Graviton. `VISION_BACKEND` says which a deployment is, a request for the other is refused,
and a `cool` worker whose build fingerprint (`vision/build.py`, the SHA-256 of
`cv2.getBuildInformation()` plus the KleidiCV line) does not match the pinned `COOL_BUILD_SHA256`
refuses every request. `scripts/vision_benchmark.py` times the kernels the worker spends its CPU
on, the whole per-site analysis, and a deployed worker end to end, and every report names the
CPU generation and the build fingerprint, so a COOL row is a COOL row. The rail kernels (the
distance transform, hit-or-miss thinning, `polylines`, the colour map, connected components and
the whole rail analysis) join the benchmark in the worker-tasks PR *(specified)*.

## Architecture

```
home region (all state)                                   us-west-2 (beside the imagery)
───────────────────────────────────────────────           ──────────────────────────────
topic: rail_access, sites (city polygons), reach_m, daily_cadence, force_manual_review
   │ research tick (120 s)
   ▼
adapter ── Earth Search STAC: the newest clear scene over the whole site
   │    ── Overpass: ways, stations, intermodal points (cached 30 days, decimated, capped)
   │  request {task, assets {red, nir, swir16, scl}, network, params}
   ├────────────────────── cross-region invoke ──────────► vision worker (arm64, OpenCV 5
   │                                                          or COOL, networkx)
   │                                                        overviews at 20 m → masks → heat →
   │                                                        distances → deserts → rasterise,
   │ ◄── metrics (served_share, desert_km2, stations[],       thin, graph → metrics, figure
   │     flags[], suggestions[]), the figure, build, timings
   │ history and baseline → material? → triage agent (look_again, previous_scene,
   │                                     site_history, web_context; deadline; fails closed)
   ▼
Finding {summary, figures} → daily cycle → draft → reviews → held (force_manual_review)
   → a person approves → the page: articles/figures/<article>/<n>.png, with the Copernicus,
     AWS Open Data and OpenStreetMap credits
```

## Measurements

To be filled from the Sydney run (the Botany Bay tile, 56HLH, whose scene
`S2A_56HLH_20240105_0_L2A` is already verified for the ship task). Every row comes from a stored
reply or a benchmark report, never typed from memory.

| Measurement | Value | Source |
|---|---|---|
| Site box, UTM zone, pixels on the 20 m grid | 150.86–151.56, −34.16 to −33.58, 56S; [w × h] | the request and `window` in the reply |
| Bytes read, range requests | [MB], [n] | the reply's `io` |
| Worker time: read, analyse (masks, access, graph, metrics), annotate | [ms each] | the reply's `timings_ms` |
| Coverage, scene cloud cover | [0.xx], [%] | `metrics.coverage`, the STAC item |
| Built-up km², served km², desert km², served share | [ ], [ ], [ ], [0.xx] | `metrics` |
| Stations, snapped, nodes, edges, components | [n], [n], [n], [n], [n] | `metrics` |
| Hubs and interchanges, by name | [names] | `metrics.hubs`, `metrics.interchanges` |
| Flags raised | [list] | `metrics.flags` |
| Top suggestion, and its fall in mean distance to a hub | [from–to], [m] | `metrics.suggestions` |
| Quality flags, warnings | [list] | `metrics.quality_flags`, `metrics.warnings` |
| Network payload | [MB]; ways [n], points [n] | `metrics.network`, the request size |
| Research tick wall time, with and without the agent | [s], [s] | the tick's log |
| Agent: tool calls, turns, verdict, tokens, cost | [n], [n], [verdict], [tokens], [USD] | the trail in the running state; Stats `vision_triage` |
| Kernel benchmark, x86 stock | [ms per kernel, whole analysis] | `vision_benchmark.py kernels` |
| Kernel benchmark, Graviton stock | [ms] | the same, on a `c8g` |
| Kernel benchmark, Graviton COOL | [ms], fingerprint [sha256] | the same, inside the COOL image |
| Deployed worker end to end | [ms] | `vision_benchmark.py worker --arn` |

## Licences

| Data | Licence and credit |
|---|---|
| Sentinel-2 | Copernicus Sentinel data, free, full and open; "Contains modified Copernicus Sentinel data {year}" on every figure and page |
| The AWS copy | Registry of Open Data on AWS; "Sentinel-2 Cloud-Optimized GeoTIFFs accessed from the Registry of Open Data on AWS" |
| OpenStreetMap | ODbL; "Map data © OpenStreetMap contributors" burned into every rail figure, in its caption and under each rail article *(the adapter's credit is specified)* |
| GDELT | Free with a citation and a link, as the project already gives |
| COOL | AWS Marketplace subscription (a trial, then usage pricing); a manual benchmark run, not Terraform |
| This repository | Apache License 2.0 |

No person, vehicle or company is identified; nothing finer than 10 m is read; the only data
kept is numbers and one map per scene.

## Limitations

- **Proxies.** Built-up density is from reflectance (NDBI, NDVI and the scene classification),
  not population or jobs; a warehouse is as built up as a tower block. Catchment activity is the
  mean of that proxy.
- **No ridership, no service.** Nothing here knows patronage, frequency, journey time or fares.
  A hub is central and busy in the proxy's terms; a ghost line runs through little built-up
  ground, not through few passengers.
- **Straight-line reach.** A river, a motorway or a yard between a home and a station is not
  seen.
- **Grade-separated crossings read as junctions.** Two lines crossing on a bridge are two lines
  crossing in pixels. Betweenness and degree inherit that.
- **One water mask.** Water is the scene classification's open-water class and nothing else;
  a flooded field or a misclassified shadow can move the built-up mask at the edges.
- **One Sentinel-2 tile per site.** The scene search keeps only scenes whose footprint holds the
  whole site, so a city must fit inside one 100 km tile, with the box placed to avoid its grid
  lines.
- **Cloud.** A scene under the coverage floor is kept but never material; a station under cloud
  is unmeasured, not quiet; in a cloudy season the baseline builds slowly.
- **The map is the topology.** Where OpenStreetMap is wrong or stale, the graph is; the
  visibility score and the news tool are checks, not corrections. A station that does not snap
  within 300 m has no place in the graph.
- **A suggestion is a simulation.** An orbital link is an edge on a graph, 1.2 times the
  straight line, with no cost, land or engineering behind it.
- **The agent is bounded, not omniscient.** Three tool calls, a deadline and a strict answer:
  when unsure it says "artefact", so a real change can wait for the next clear scene.

## Reproduction

1. **Deploy with the switch.** Re-apply `infra/bootstrap` by hand with `vision_region`
   (`us-west-2`), set the `VISION_ENABLED` repository variable to `true`, and merge or dispatch
   a deploy. The worker, its bucket and log group appear in us-west-2; nothing else changes.
   ([runsheet](../deployment-runsheet.md#vision-optional))
2. **Create a topic.** With the admin CLI, `topics create` with `adapter: "rail_access"`
   *(specified; the adapter is PR E)* and the Sydney box from the rail document's §2 as its one
   site, `reach_m` 1000, or `adapter: "satellite_vision"` with a Botany Bay polygon and
   `object_noun: "large vessels"` for the ship task that is on the branch. `force_manual_review`
   is set for you.
3. **Force a tick.** `python scripts/admin_cli.py topics trigger <id> --pipeline research_tick`.
   The first observation is always material: a Finding with its figure appears, and the running
   state at `snapshots/<topic>/running-state.json` holds the history, the reply's metrics and
   timings and the agent's trail (from the second clear scene on).
4. **Run the daily cycle** (`--pipeline daily_cycle`) and open the inbox
   (`python scripts/review_inbox.py inbox`): the article is held for you with its figure.
   `approve` publishes it; the page shows the map at `articles/figures/<article>/1.png` with the
   credits.
5. **The benchmark.** `python scripts/vision_benchmark.py kernels --out stock-x86.json` on any
   machine with `requirements-vision.txt`; the same on a `c8g` from the COOL AMI and in a stock
   venv beside it; `worker --arn <VISION_WORKER_ARN>` for the deployed worker.

## Video shot list

About three minutes, screen and voice, no faces.

1. **The question** (15 s): a Sentinel-2 true-colour view of the city, then the OpenStreetMap
   rail lines over it. "Where is this city built, and how far is that from a train?"
2. **The topic** (20 s): the admin CLI creating the topic: one polygon, one reach, one
   adapter. `force_manual_review` appearing in the response without being asked for.
3. **The tick** (30 s): the research-tick log: the STAC search, the Overpass fetch (or the
   cache hit), the cross-region call, the worker's timings and the quality flags coming back.
4. **The figure** (30 s): the PNG from the content bucket: the heat map, the outlined deserts,
   the lines by betweenness, the stations by activity, the hubs, the tinted cloud, the three
   credit lines. Point at one desert and its nearest station in the metrics.
5. **The diff and the agent** (40 s): a second scene; "within the usual range", no model call.
   Then a scene with a change: the agent's trail in the running state, a `look_again` call with
   other parameters, the verdict and reason. Show an "artefact" verdict holding a Finding back.
6. **The person** (20 s): the review inbox with the held article and its figure; approve; the
   page with the map and the credits under the text.
7. **COOL** (25 s): the benchmark table: x86 stock, Graviton stock, Graviton COOL, with the
   build fingerprints; the `not_cool` refusal from a worker whose build does not match.
8. **What it does not say** (10 s): the article's last sentence: what was not measured.
