# OpenCV AI Competition 2026: submission draft

**Status:** draft; the measurements of the deployed run are still to be filled, and the whole
re-checked against the Devpost rules before submitting · **Deadline:** 26 October 2026, 11:45 pm PDT · **Entry:** BloggerBear's
Rail Access Monitor · **Design:** [vision.md](../enhancements/vision.md) and
[rail-access-monitor.md](../enhancements/rail-access-monitor.md) · **How it works:**
[docs/architecture/blogger-vision.md](../architecture/blogger-vision.md)

Everything here describes what is on the integration branch (#285), every rail pull request
merged. Numbers in square brackets are placeholders for the deployed run on Sydney and for the
Graviton benchmark, which the operator makes. The competition pages were read through search
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
  250 MB). The rail task works on a 20 m grid read from the COG overviews and uses
  NDBI and NDVI thresholds and the scene classification for the built-up mask, a Gaussian blur
  for the heat map, one distance transform with labels for served area, deserts and catchments,
  `polylines` and hit-or-miss thinning in core OpenCV to turn OpenStreetMap ways into a one-pixel
  skeleton, `filter2D` for junctions and ends, connected components for the segments, and
  networkx (in the worker only) for betweenness, hubs, distance to a hub and the simulated links.
  The ship task, the first on the same worker, uses an NDWI water mask, an adaptive threshold
  and `minAreaRect` filters.
- **Bounded for every valid input, not just real cities.** The graph is not built past 10,000
  junction clusters or runs; betweenness is exact within a work budget and sampled with a fixed
  seed above it (and says so); the station passes are linear, and the thinning works only in the
  box round what its last pass removed; and the rail window is capped at 3,584 px a side (71.7 km
  at 20 m), from timings on one core, the worker's share. There the worst contract-valid network
  built for the test takes up to 25.5 s, about 33 s on arm64 and 41 to 56 s with the read, inside
  the client's 58 s; at 4096 px it took up to 37.5 s, about 49 s on arm64 and 59 to 79 s with the
  read. No real OpenStreetMap network has been timed: the "about 9 s" for a city is PR J's
  synthetic city, 180 stations and 221 nodes, 9.2 to 9.6 s at 4096 px on four cores, and the
  deployed Sydney run with the Overpass network will give the real figure. The speed tests count
  the work (neighbour lookups, comparisons, pixels handed to OpenCV), not the time, so they fail on
  the slow code. And one rule keeps both vision adapters inside the research tick's 120 s: the
  worker is asked only with 45 s of the measuring budget left and waited on for at most 58 s.
- **A contract, not a convention.** Every request is validated (version, asset URL allowlist,
  parameter bounds, polygon size; the task's assets and the network's shape and caps); every
  failure is a code; every reply echoes the parameters it was measured with, the OpenCV build
  record and per-stage timings, so a result is reproducible and a COOL result is provable. The
  worker reads only from allow-listed URLs: it follows no redirect (a 3xx is `unreadable_scene`,
  its body unread), and reads no further than the byte range it asked for, even from a server
  that ignores `Range`.
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
  the news, and must answer in a bounded number of turns before a deadline.
- **A pixel-to-graph step that takes any mask**, so a learned track segmentation could replace
  the rasterised ways later and nothing after it changes.

### Real-world impact

- Reports, for any city that fits in one Sentinel-2 tile, the served share of its built-up area,
  its transit deserts with their nearest station, its hubs and interchanges, the busy stations
  the network reaches least well, the station every journey depends on, the lines through
  little, and simulated orbital links and feeder corridors ranked by how much they bring the
  city closer to a hub.
- Says those flags and simulations in words a planner can check: **planner notes**, one sentence
  each, written by code from the measured values ("{station} lies on {ratio} times as many
  shortest paths through the mapped rail network as any other station: the pipeline flags it as
  a single point of failure …"; "Simulation: a {km} km link between {a} and {b} … would bring the
  mapped network's stations {m} m closer to a hub on average by rail …"; the templates are in
  [vision.md §6.8](../enhancements/vision.md#68-planner-notes-the-flags-and-simulations-in-words)).
  They are called flags and simulations, never
  recommendations, and carry no cost, cause or timetable; up to three end the article, all of
  them go to the reviewer's evidence, and the Finding keeps them as data.
- Says what it did not measure: coverage under cloud, stations that did not snap to a line, a
  fragmented graph, a map disagreeing with the imagery.
- Honest by construction about what the numbers are: built-up density is a proxy from
  reflectance, not population; reach is straight-line; a suggestion is a simulation on a graph.
  The summary prompt forbids causes, forecasts and planning or investment views.

### User experience

- The map on the page: the heat map over the scene (warm colours), unmeasured areas in slate,
  deserts outlined in amber, the network's edges in pale cyan, thicker the more paths run through
  them, stations as white rings, larger the busier their catchment and thicker in the top quarter
  of isolation weight, hubs as green diamonds, flagged stations with a red ring, ghost lines in
  red, simulated links dashed in green. Nothing else is drawn on the map: a dark strip under it
  holds the three caption lines with the credits and a legend of the symbols ("simulated link"
  among them, never "suggested"). The static page and the single-page app show the figure under
  the credit line, above the article text, with a caption that says what the colours and lines are
  and repeats the credits with the year; an article shows the maps of the cities its summary
  reports and no others, and a figure whose image fails to load is removed, not shown broken.
- The article is two to four plain sentences (three to six with planner notes) with the station
  and line names exactly as mapped, the served share, the area beyond reach, the capture date,
  and what was not measured.
- Adding a city is adding a topic with the admin CLI; the operator's assistant reports which
  sites were measured and why the others were not (`vision_status`, from each topic's running
  state), reads the worker's log in its own Region, and walks through the pipeline
  (`architecture(feature="vision-pipeline")`), all read-only
  ([ops-assistant-vision.md](../enhancements/ops-assistant-vision.md)).

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
   `site_history` and, for rail, `web_context` (recent news on the city's stations and lines),
   within a tool budget, that plus two turns, and a deadline 85 s from the tick's start;
   `look_again` is cut to the time left and refused when a worker run could not finish. The
   answer must be a strict JSON verdict. A malformed answer, an error, the turn limit or the
   deadline is "artefact": the agent fails closed. What its tools return is treated as data, and
   its reason reaches a summary only quoted, as the agent's note, not a measurement. Its trail is
   stored and its spend lands on the Stats page. The rule-based planner notes sit beside the
   agent's verdict: the agent decides whether a change is real; code, not a model, writes what the
   network's flags and simulations say.
4. **Person.** `force_manual_review` is forced on every vision topic; no vision article is
   published without someone approving it in the review inbox. That holds on every publish path:
   the daily cross-topic digest, which publishes with no person, leaves these topics out.

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
the whole rail analysis) are in it beside the ship kernels.

## Architecture

```
home region (all state)                                   us-west-2 (beside the imagery)
───────────────────────────────────────────────           ──────────────────────────────
topic: rail_access, sites (city polygons), reach_m, daily_cadence, findings_window_hours,
       force_manual_review
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

Two runs exist so far, and neither is the deployed one. **PR D's smoke run** read the Sydney
scene for real (the Botany Bay tile, 56HLH, scene `S2A_56HLH_20240105_0_L2A`, already verified for
the ship task) with the worker handler in-process in the build container, outside us-west-2, and
a nine-station network made by hand, since Overpass is unreachable from there: its read and window
are real, its graph numbers are not a city's. **The kernel benchmark** ran on the build
container's CPU, not on Graviton. The rows in square brackets wait for the deployed run, with the
Overpass network, and for the operator's Graviton run. Every row comes from a stored reply or a
benchmark report, never typed from memory.

| Measurement | Value | Source |
|---|---|---|
| Site box, UTM zone, pixels on the 20 m grid | 150.86–151.56, −34.16 to −33.58, 56S; 3,327 × 3,306 px with the reader's 16 px pad (the rail window cap is 3,584) | the smoke run's `window`; `geo.py` gives the same |
| Bytes read, range requests | 54 MB in 192 range requests (the 10 m bands from their 20 m overviews) | the smoke run's `io` |
| Worker time: read, analyse, of which graph, annotate | read 27.1 s through the build container's proxy, from outside us-west-2 (beside the bucket it is a different number: [s]); analyse 12.1 s, of which the graph stage 6.6 s on the hand-made network ([s] on the city's); annotate 0.5 s; total 39.7 s | the smoke run's `timings_ms` |
| Coverage, scene cloud cover | 0.147 on 2024-01-05: the scene is mostly cloud over the city, so `low_coverage` is flagged and the scene could never be material or a first observation; [0.xx], [%] on a clear scene | `metrics.coverage`, the STAC item |
| Built-up km², served km², desert km², served share | [ ], [ ], [ ], [0.xx] | `metrics`, deployed run |
| Stations, snapped, nodes, edges, components | [n], [n], [n], [n], [n] | `metrics`, deployed run |
| Hubs and interchanges, by name | [names] | `metrics.hubs`, `metrics.interchanges` |
| Flags raised, and the planner notes written for them | [list] | `metrics.flags`; the Finding's `planner_notes` |
| Top suggestion, and its fall in mean distance to a hub | [from–to], [m] | `metrics.suggestions` |
| Quality flags, warnings | [list] | `metrics.quality_flags`, `metrics.warnings` |
| Network payload | [MB]; ways [n], points [n] (a city at `osm.py`'s caps is 2.6 to 3 MB of JSON) | `metrics.network`, the request size |
| Research tick wall time, with and without the agent | [s], [s] | the tick's log |
| Agent: tool calls, turns, verdict, tokens, cost | [n], [n], [verdict], [tokens], [USD] | the trail in the running state; Stats `vision_triage` |
| Kernel benchmark, x86 stock (this build container's CPU, not Graviton) | Intel Xeon at 2.10 GHz, OpenCV 5.0.0 (`0ef08003c311`), 2048 px synthetic images, medians of 20, on 4 vCPUs / pinned to one: `analyse_rail_access` 1,138 / 1,303 ms (a city of 8 lines and 40 stations), `analyse_site` 192 / 225 ms, `thinning_hitmiss` 51 / 44 ms, `distance_transform_l2` 49 / 57 ms, `adaptive_threshold_gaussian_31` 38 / 36 ms; the full table is in [vision.md §7](../enhancements/vision.md#7-measured) | `vision_benchmark.py kernels`, and the same under `taskset -c 0` |
| Worst-case rail analysis on one core (contract-valid networks built to be slow, with ways reaching the window's edges) | at most 37.5 s and 1.69 GB at a 4096 px window, at most 25.5 s and 1.32 GB at the 3,584 px cap (about 33 s on an arm64 core taken as 1.3 times slower, 41 to 56 s with the read); no real OpenStreetMap network timed yet: PR J's synthetic city (180 stations, 221 nodes) took 9.2 to 9.6 s at 4096 px on four cores, and the deployed Sydney run with the Overpass network will give the real figure | PR J's measurements through `lambda_handler` on one core of the Intel Xeon above, quoted in `vision_worker_handler.py`, `vision/scene.py` and `vision/rail_analyse.py` |
| Kernel benchmark, Graviton stock, one core and four | [ms] | `docs/hackathon/evidence/runs/<date>-bench-kernels-neoverse-v2-graviton4-opencv-1t-r1.json` and `-4t-r1.json`, from the [runsheet's COOL session](../deployment-runsheet.md#the-cool-backend-and-the-benchmark) step 8 |
| Kernel benchmark, Graviton COOL, one core and four | [ms], fingerprint [sha256] | the same, `-cool-1t-r1.json` and `-cool-4t-r1.json`; the fingerprint is `environment.opencv.build_sha256`, equal to `sha256sum cool-buildinfo.txt` |
| The real handler on the clear Sydney scene, stock against COOL, one core | speed-up [x] with its 95 % interval on the compute stages, the read apart; `parity: pass` | `<date>-bench-compare-...-scenes-opencv-1t-vs-cool-1t.json`, from the two `bench-scenes` run files it names (each holds the rail metrics, their digest, the figure's hash, the peak memory and the mapped OpenCV libraries) |
| What OpenCV 5 changed against 4.14 for this workload | speed-up [x] | `<date>-bench-compare-...-scenes-opencv-4x-1t-vs-opencv-1t.json` (`opencv-python-headless==4.14.0.94` against 5.0.0.93, the same venv shape) |
| The COOL guard's negative controls | 3 of 3 as expected under COOL and under stock | `<date>-bench-controls-neoverse-v2-graviton4-cool.json` and `-opencv.json` |
| Site coverage of the pinned scenes, and the year's selection counts | coverage per scene; items, passing the cloud filter, passing the footprint rule, under the floor | `docs/hackathon/evidence/runs/2026-10-16-eval-coverage-intel-xeon-processor-2-80ghz-none.json` (`vision_eval.py coverage`, 16 October) |
| Deployed worker end to end | [ms] | `vision_benchmark.py worker --arn --out docs/hackathon/evidence/runs` |

## Licences

| Data | Licence and credit |
|---|---|
| Sentinel-2 | Copernicus Sentinel data, free, full and open; "Contains modified Copernicus Sentinel data" under each vision article and on the About page; the rail figure carries it with the year in the strip under the map, and its page caption repeats it |
| The AWS copy | Registry of Open Data on AWS; "Sentinel-2 Cloud-Optimized GeoTIFFs accessed from the Registry of Open Data on AWS" |
| OpenStreetMap | ODbL; "Map data © OpenStreetMap contributors" in the rail figure's strip (as "(c)"), in its page caption, under each rail article and on the About page |
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
  lines, and inside the worker's rail window, 71.7 km a side at 20 m.
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
   (`us-west-2`), set the `VISION_ENABLED` repository variable to `true`, and deploy dev: merge a
   change under `infra/`, `lambdas/` or `frontend/` to `dev` (a docs-only merge starts no apply),
   or dispatch the Terraform workflow from `dev`. The worker, its bucket and log group appear in
   us-west-2; in the home region the shared execution role gets one invoke policy and the research
   tick gets the worker's ARN. The repository variable also turns the worker on in production at
   the next release, unless the `production` environment sets `VISION_ENABLED` to `false`.
   ([runsheet](../deployment-runsheet.md#vision-optional))
2. **Create the topics.** The four city topics are complete topic bodies in
   `scripts/rail_topics.example.json` (Sydney, Melbourne, Singapore and Boston, `reach_m` 1000,
   one city drafted a day, each article from every Finding since that city's last, through a
   192-hour `findings_window_hours`); the runsheet's
   [rail_access topics](../deployment-runsheet.md#rail_access-topics) creates them with the admin
   CLI. For the ship task, `adapter: "satellite_vision"` with a Botany Bay polygon and
   `object_noun: "large vessels"`. `force_manual_review` is set for you.
3. **Force a tick.** `python scripts/admin_cli.py topics trigger rail-sydney --pipeline research_tick`.
   The first tick fetches the city's network from Overpass and measures the newest scene; if its
   coverage is at least 0.6, that is the first observation and a Finding appears, carrying the
   map and the planner notes (a cloudy scene reports nothing, and the next clear one is first).
   The figure is in the content bucket under `vision/<topic>/<site>/<scene>.png`, and the running
   state at `snapshots/<topic>/running-state.json` holds the history, the reply's metrics and
   timings, the cached network and, from the second clear scene on, the agent's trail. The CLI
   waits only 30 s for the Finding, and a tick that measures a scene can run longer (the network
   fetch, up to 58 s of worker, then the summary, inside 120 s), so its "No new Finding after 30s"
   is expected: wait about two minutes, or until
   `python scripts/admin_cli.py topics findings rail-sydney` shows it.
4. **Run the daily cycle** (`--pipeline daily_cycle`) once the Finding is there, and open the
   inbox (`python scripts/admin_cli.py inbox`): the article is held for you.
   `python scripts/admin_cli.py approve` goes through what is waiting and publishes it; the page
   shows the map at `articles/figures/<article>/1.png` with its caption and the credits.
5. **The benchmark.** `python scripts/vision_benchmark.py kernels --out docs/hackathon/evidence/runs`
   on any machine with `requirements-vision.txt` writes a run file (the code's commit, the CPU,
   the build fingerprint, every sample); `scenes --cache ~/cogs --out docs/hackathon/evidence/runs`
   runs the real handler on the pinned scenes (`scripts/vision_benchmark_scenes.json`; the rail
   scenes need the Sydney network fixture) and records the rail metrics, their digest and the
   figure's hash beside the timings. The Graviton rows come from the
   [runsheet's COOL session](../deployment-runsheet.md#the-cool-backend-and-the-benchmark): a
   `c8g.xlarge`, three venvs (COOL on the product's own `cv2`, stock 5.0.0.93, stock 4.14.0.94),
   the runs of its step 8 interleaved and pinned to one core, `compare` for each stock-COOL pair
   (the speed-up with its interval, and parity) and `controls` for the guard. Every number in the
   COOL rows above is read from those run files, whose schema is in
   [docs/hackathon/evidence/README.md](evidence/README.md); `worker --arn <VISION_WORKER_ARN>` adds
   the deployed worker end to end.

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
