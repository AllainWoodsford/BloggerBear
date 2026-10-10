# Vision: satellite imagery and the Rail Access Monitor

How a topic that watches satellite imagery becomes an article with a map on it: where the
imagery and the map data come from, what the research tick asks the vision worker, what the
worker measures with OpenCV, how the triage agent decides whether a change is real, why a person
approves every article, and how the figure reaches the page. Then, in plain words, what the rail
task measures and what its numbers do and do not mean.

The design is in [vision.md](../enhancements/vision.md) (the stack: ship counting, the worker,
the agent, COOL) and [rail-access-monitor.md](../enhancements/rail-access-monitor.md) (the rail
task, and one specification per pull request). This page describes what is on the integration
branch today; where a step is specified but not yet merged, it says so. Unlike
[article research](article-research.md), the operator's assistant has no `feature` entry for it
yet.

The rules this follows are the README's: research is diff-first, nothing is published without a
review, and new sources are adapters. One more is the vision work's own: **not measured is never
"nothing there"**. A site that could not be read keeps its history and records an error, and a
figure tints what was not measured.

Resource names below use `<prefix>` (your `unique_name_prefix`) and `<env>` (`dev` or
`production`).

## Part one: the pipeline

```
home region (ap-southeast-2 by default): everything that holds state
──────────────────────────────────────────────────────────────────────
topic: adapter satellite_vision (or rail_access), sites, thresholds, force_manual_review
   │ research tick, 120 s
   ▼
adapter.fetch_state ── Earth Search STAC ──► the newest clear Sentinel-2 scene over each site
   │                ── Overpass (rail) ────► rail ways, stations, intermodal points, cached in
   │                                         the running state and sent inline to the worker
   │ a scene the site already has? nothing new
   │ vision_client.measure: one request, across regions   us-west-2, beside the imagery
   ├─────────────────────────────────────────────────────► vision worker (arm64, OpenCV 5)
   │                                                        range-reads only the tiles under
   │ ◄──── metrics, one small PNG, build record, timings ── the site from sentinel-cogs;
   │                                                        keeps nothing, writes nothing
   │ history → running state; the PNG → content bucket
adapter.material_diff: this scene against the site's own baseline
   │ within the usual range → stop; no model call
   ▼
triage agent: the figure and the metrics, tools, a budget, a deadline; unsure → "artefact"
   │ artefact → kept in the state, no Finding
   ▼
Finding {summary, figures: [{key, caption, alt}]}
   ▼
daily cycle → draft → fresh-data and compliance reviews → held: force_manual_review
   ▼
a person approves → the article page, with the figure at articles/figures/<article>/<n>.png
                    and the Copernicus, AWS Open Data and OpenStreetMap credits
```

### The steps

| # | Step | What happens | Where in the code / infra |
|---|---|---|---|
| 1 | **Topic and adapter** | A vision topic is a row of settings like any other: its adapter, its sites as lon/lat polygons, its thresholds and its schedule, all in `adapter_config`. `force_manual_review` is forced on when the topic is created and cannot be unset. The `satellite_vision` adapter (the `ships` task) is on the branch; the `rail_access` adapter is specified (PR E) and shares the site, history and budget machinery with it. | `topics` table; `lambdas/common/adapters/satellite_vision.py`; `admin_api_handler.py` forces the flag |
| 2 | **Sources** | Three public sources and no key for any of them. **Sentinel-2** scenes are found through the Earth Search STAC API (the newest scene over the whole site, under a cloud-cover cap, within a lookback window) and read from the public `sentinel-cogs` bucket as Cloud-Optimized GeoTIFFs. **OpenStreetMap** rail ways, stations and intermodal points come through the Overpass API: two allow-listed endpoints, one fallback hop, points decimated to the 20 m grid and capped, a coded error on failure, and a stale-after-TTL rule the adapter will apply. **GDELT**, through the existing news search, is the agent's `web_context` tool for the rail task (specified). | `STAC_SEARCH_URL` in `satellite_vision.py`; `DEFAULT_ALLOWED_URL_PREFIXES` in `common/vision_contract.py`; `lambdas/common/osm.py`; `lambdas/common/web_search.py` |
| 3 | **The research tick: diff first, with running state** | The tick is the same Lambda every topic uses. The adapter visits its sites inside a **time budget** (`time_budget_seconds`, well under the tick's 120 s), skips a scene it has already measured, and appends each new measurement to the site's history. The adapter sets `keeps_running_state`, so the tick keeps the newest state after every tick, not only when it writes a Finding: a baseline includes every scene measured, and a scene is never measured twice. `material_diff` then compares the new scene with the median of the site's earlier clear scenes. | `lambdas/research_tick_handler.py`; `snapshots/<topic_id>/running-state.json` in the `<prefix>-<env>-content` bucket |
| 4 | **The stateless worker** | One cross-region `lambda:InvokeFunction`. The worker validates the request against the **contract** (version 1; every asset URL must start with an allowed prefix, the `sentinel-cogs` bucket by default; parameter bounds; a polygon of 3 to 64 points), reads only the tiles under the site with HTTP range requests (no GDAL: a small reader for the TIFF subset Sentinel-2 uses), measures with OpenCV, draws the figure, and replies with the metrics, the PNG, the **build record** and per-stage timings. Every failure is a code (`bad_request`, `backend_mismatch`, `not_cool`, `site_outside_scene`, `window_too_large`, `unreadable_scene`, `internal`). **Two backends** share the handler: `VISION_BACKEND` says which one a deployment is, a request for the other is refused, and a `cool` worker whose OpenCV build does not match the pinned `COOL_BUILD_SHA256` fingerprint refuses everything, so a result can never claim a build it did not run on. | `lambdas/vision_worker_handler.py`; `lambdas/vision/`; `lambdas/common/vision_contract.py`; `lambdas/common/vision_client.py`; `infra/modules/vision-worker/` (`<prefix>-<env>-vision-worker`, arm64, python3.12, 2048 MB, 60 s, in `vision_region`) |
| 5 | **The bounded triage agent** | A numeric change is only a candidate. Before it becomes a Finding, the agent is given the metrics, the baseline, the detector's rejection tally and the figure as an image, and may call `look_again` (re-measure the same scene with other settings: active perception), `previous_scene` and `site_history`; the rail profile adds `web_context` (specified). The bounds are in code, not in the prompt: at most `triage_max_tool_calls` tool calls and that plus two model turns, and a **deadline** (`triage_deadline_seconds`, 85 s from the tick's start) under which no model turn starts with under 10 s left and `look_again` is refused with under 30 s. The answer must be `{"verdict": "real" or "artefact", "reason"}`; a malformed answer, a model error, the turn limit or running out of time is **artefact**. It fails closed: an unsure agent never makes a Finding. The trail is stored in the state and the spend lands on the Stats page as `vision_triage`. | `lambdas/common/vision_triage.py`, called from the adapter's `material_diff` |
| 6 | **The person** | The daily cycle drafts and reviews the article as for any topic, then holds it: `force_manual_review` adds "this topic's articles are always reviewed by a person before publishing" to the hold reasons, whatever the compliance review said. The article waits in the review inbox until `admin_cli approve`. | `lambdas/daily_cycle_handler.py`; `moderation-queue`; `scripts/review_inbox.py` |
| 7 | **The page: the figures path** | An adapter that draws something returns `[{"key", "caption", "alt"}]` from `figures()`; the tick stores the list on the Finding; the daily cycle gathers the figures of the findings an article was written from (newest first, de-duplicated, at most three); publishing copies each PNG from the private content bucket to the site bucket as `articles/figures/<article_id>/<n>.png` (one server-side copy, stored as `image/png`), and the static page and the SPA show each as a `<figure>` with its caption. Unpublishing and take-down remove the copies; invalidation covers the prefix. On the integration branch the path is generic and the ship adapter does not yet attach its figure (the base adapter's default is an empty list); the rail adapter attaches one per measured site (specified). | `lambdas/common/figures.py`; `publish_figures` in `lambdas/common/static_pages.py`; `frontend/app.js`; the site bucket under `articles/` |

### Where state lives

Everything with state is in the home region. The worker's region holds the function, its log
group and the bucket its package is uploaded through, and nothing of ours to migrate.

| What | Where |
|---|---|
| Per-site history, `last_error`, the agent's trails, the cached OpenStreetMap network (rail) | `snapshots/<topic>/running-state.json` in the content bucket, and each Finding's snapshot |
| The worker's PNG for each scene | `vision/<topic>/<site>/<scene>.png` in the content bucket (private) |
| A published article's figures | `articles/figures/<article>/<n>.png` in the site bucket, behind CloudFront |
| The Finding's and the article's figure lists | `figures` on the Finding and on the article row |
| Triage spend | the Stats page, category `vision_triage` |
| The worker package | `<prefix>-<env>-vision-artifacts-<account>-<region>`, `vision-worker/<md5>.zip`, 14-day expiry |

### What is deployed

**With `VISION_ENABLED` unset or anything but `true`: nothing.** Both environments call the
module with `count = var.vision_enabled ? 1 : 0`; the research tick's `VISION_WORKER_ARN` is
empty, which the client reads as "not configured", and the invoke policy does not exist. A
vision topic created anyway runs, records `last_error` on every site, and writes no Finding.

**With it `true`:** in `vision_region` (us-west-2 by default), the worker, its role (its own log
group and nothing else), its log group and the artifacts bucket; in the home region, one policy
on the pipeline's execution role allowing `lambda:InvokeFunction` on that one function, and the
ARN in the research tick's environment. The switch is a GitHub repository variable fed to
Terraform as `vision_enabled` (`TF_VAR_vision_enabled` in both apply workflows), never a tfvars
edit; the bootstrap must have been re-applied with `vision_region` first. The steps are in the
[deployment runsheet](../deployment-runsheet.md#vision-optional) and the settings in
[configuration.md](../configuration.md#vision-and-rail-access).

### The two tasks

| | `ships` | `rail_access` |
|---|---|---|
| What it watches | fixed sites on water: an anchorage, a port approach | a city, inside one Sentinel-2 tile |
| Bands | NIR, green and the scene classification at 10 m | red, NIR, SWIR and the scene classification on a 20 m grid, read from the COG overviews |
| Extra input | none | the OpenStreetMap network, fetched by the adapter and sent inline |
| What OpenCV does | NDWI water mask, adaptive threshold, `minAreaRect` length and elongation filters, a buffer beside anything unmeasured, a tally of why candidates were dropped | NDBI and NDVI thresholds, a Gaussian heat map, a distance transform with labels, connected components, `polylines`, hit-or-miss thinning, `filter2D` junctions, a networkx graph |
| What is diffed | the count against the median of earlier clear scenes, past both an absolute and a relative threshold | the served share, the desert area and the station count against the baseline |
| Agent tools | `look_again`, `previous_scene`, `site_history` | the same, and `web_context` |
| Where it stands | on the integration branch | the vision core is in review ([#289](https://github.com/AllainWoodsford/BloggerBear/pull/289)); the worker task (PR D) and the adapter (PR E) are specified |

## Part two: the trains explainer

What the Rail Access Monitor measures, for a reader who is not a planner. The exact rules,
constants and bounds are in [the specification](../enhancements/rail-access-monitor.md#4-specifications-one-per-pull-request)
(PR A for the vision core, PR E for the adapter); the paragraphs here describe the concepts as
the vision core implements them.

**One scene, one city.** A Sentinel-2 scene is a set of images, one per band of light, with a
pixel of 10 or 20 m. The rail task works on a 20 m grid (so a 60 km city fits the worker's
window) and reads four bands: red, near-infrared, short-wave infrared, and the scene
classification, which says for every pixel whether it is cloud, shadow, water or nothing at all.

### Built-up density: the "heat"

Built-up ground is brighter in short-wave infrared than in near-infrared (a high **NDBI**) and
is not vegetation (a low **NDVI**). A pixel is built up when both hold and it is neither water
(the classification's open-water class, the only water mask used) nor cloud, shadow or no data.
That mask is then blurred with a Gaussian whose width is a walking distance (500 m by default),
so each pixel's value becomes the share of built-up ground around it, between 0 and 1. The
result is the **heat map**: warm colours on the figure.

It is deliberately not rescaled, so two scenes of one city compare directly. And it is a
**built-up density proxy from imagery, not population**: a warehouse district and a block of
flats are both roofs and asphalt; a park is cold however many people cross it. The articles are
told so, and say so.

### Walking reach and transit deserts

Every station is a point on the grid. One distance transform gives each pixel its straight-line
distance to the nearest station and which station that is. Within the **reach** (1,000 m by
default) a built-up pixel is *served*; the **served share** is the served built-up area over all
the built-up area. Reach is straight-line distance, not a walking route: a river, a motorway or
a rail yard in between is not seen.

A built-up patch beyond reach is a **transit desert**. Patches of at least half a square
kilometre are reported, the ten largest with their centre, their area and the nearest station.
The figure outlines them.

Each station also gets a **catchment activity**: the mean heat of the ground nearest to it,
within reach, counted over the pixels the scene could see. A station under cloud is reported as
unmeasured (with how much of its catchment was visible), never as quiet.

### The station graph

The network's geometry comes from OpenStreetMap, not from the imagery: rail, light-rail and
subway ways (yards, sidings and industrial lines excluded; trams not fetched), stations and
halts, and the intermodal points (bus stations, ferry terminals, park-and-rides, and bus stops
within 400 m of a station). The imagery verifies and scores it. The graph is built in pixels:

1. **Rasterise.** The ways are drawn into the 20 m grid as one-pixel lines, tunnels into a
   second mask.
2. **Thin.** The drawing is thinned to a one-pixel skeleton with hit-or-miss morphology in core
   OpenCV (the headless wheel has no `ximgproc`), which keeps it connected.
3. **Junctions and ends.** A skeleton pixel with three or more skeleton neighbours is a junction;
   one with a single neighbour is a line end.
4. **Snap.** OpenStreetMap often puts the station on the platform and the line on the track, so
   each station is snapped to the nearest skeleton pixel within 300 m. A station that does not
   snap keeps its catchment and intermodal counts, but has no place in the graph (degree 0, no
   distance to a hub), and a scene where fewer than half the stations snap is flagged.
5. **Vectorise.** Junctions, ends and snapped stations are the nodes; the skeleton runs between
   them are the edges, each with its length along the track.

Because the pixel-to-graph step takes any mask, a learned segmentation of the tracks could
replace the drawn ways later and nothing after it would change. One known limit: two lines that
cross on a bridge are drawn as crossing pixels, so a grade-separated crossing reads as a
junction.

### Degree, betweenness, hubs and interchanges

- **Degree** is how many edges meet at a node: a station in the middle of one line has 2, a
  line's end has 1, a station where lines meet has 3 or more. Stations of degree 3 or more are
  the **interchanges**.
- **Betweenness** is the share of all shortest paths (by track length) between pairs of nodes
  that pass through a node. A station every cross-city journey must pass through has a high
  betweenness; a station at the end of a branch has none. On a graph of more than 800 nodes it
  is sampled rather than exact, and the metrics say so (specified).
- **Hubs** are the top five stations by betweenness among those whose catchment activity is at
  or above the median: busy *and* central. The figure marks them.

### Distance to a hub and the isolation weight

Every station's **distance to the nearest hub** is measured along the track, not as the crow
flies; a station with no path to any hub has none. The **isolation weight** is

```
W = d_hub_km / max(activity, 0.05)
```

that distance in kilometres divided by the station's catchment activity (floored at 0.05, so an
empty catchment far from a hub is "very isolated" rather than infinitely so). A busy station a
long way from any hub scores high; a quiet station next to one scores near zero.

### The three flags

Each flag is a pattern in the numbers, reported with the station names exactly as mapped:

- **Isolated high demand:** a station in the top quarter by activity, on a plain line (degree
  at most 2), and in the top quarter by isolation weight. The busy places the network reaches
  least well.
- **Single point of failure:** the station whose betweenness is at least twice the next one's.
  Most journeys pass through it, and the graph has no second way round.
- **Ghost line:** three or more consecutive stations along a line, each in the bottom quarter by
  activity. A line through ground the imagery shows as little built up.

A quarter only exists where the values spread: when every station scores the same, nothing
stands out and nothing is flagged.

### The two suggestion types

Both are **simulations on the graph**, deterministic for a given scene and map:

- **Orbital link:** among pairs of stations that are both in the farthest quarter from a hub,
  within 5 km of each other in a straight line and not already neighbours, the twelve pairs with
  the largest product of isolation weights are each tried on a copy of the graph with a new edge
  1.2 times the straight line long. The three that most reduce the mean distance to a hub are
  reported, with that reduction and the change in the top hub's betweenness. A link that brings
  nobody closer is not suggested.
- **Feeder corridor:** the three stations with the largest isolation weight, each with the path
  to its nearest hub along the existing track and that path's length.

**What none of this means.** There is no ridership or patronage, no timetable or service
frequency, no journey time, no cost, no land, no engineering: a suggestion is an edge added to a
graph of drawn lines, and a flag is a pattern in a density proxy and a map. The summary prompt
forbids causes, forecasts, political, planning or investment views, and saying any area is well
or badly served in absolute terms.

### Corridor visibility and tunnels

For each edge the imagery is asked whether the mapped line is visible: the share of the pixels
in a three-pixel swath along it that have a low NDVI (bare ground or track, not canopy) and are
neither water nor cloud. An edge more than half in tunnel has no visibility score, and the
tunnel is drawn apart; in a city whose metro is mostly underground only the surface edges are
scored. Low visibility on a surface edge means the map and the imagery disagree, which is a
question, not a verdict.

### Cloud and coverage

Cloud, shadow and no-data pixels (from the scene classification) are unusable. **Coverage** is
the share of the site's pixels that were usable. A scene under the coverage floor (0.6 for the
rail task) is measured and stored but is never material and never part of a baseline; the
figure tints everything unmeasured so a cloud never looks like an empty suburb. The scene search
itself takes only scenes under a cloud-cover cap, and only those whose footprint holds the whole
site, which is why a city must sit inside one Sentinel-2 tile. The quality flags
(`empty_site`, `low_coverage`, `no_network`, `few_stations_snapped`, `graph_fragmented`) travel
with every reply and into the history.

### What is diffed, and what the agent checks

The adapter (specified) diffs the served share, the desert area and the station count against
the site's baseline; the first observation is always reported. The agent sees the figure and may
`look_again` with other parameters, compare the `previous_scene`, read the `site_history`, or
ask `web_context` whether a station or line recently opened or closed. It is told what an
artefact looks like here: cloud, haze or seasonal vegetation shifting the built-up mask, a
scene edge, a map edit, a tagging change.

### Licences and credits

| Data | Licence | The credit, and where it appears |
|---|---|---|
| Sentinel-2 imagery | Free, full and open under the Copernicus Sentinel data legal notice | "Contains modified Copernicus Sentinel data {year}": burned into every figure, in the page caption, under each article and on the About page |
| The AWS copy of it | The Registry of Open Data on AWS asks to be cited as the source | "Sentinel-2 Cloud-Optimized GeoTIFFs accessed from the Registry of Open Data on AWS": under each article and on the About page |
| OpenStreetMap | Open Database Licence (ODbL), which asks for the credit wherever the data is shown | "Map data © OpenStreetMap contributors": burned into every rail figure (as `(c)`, since the Hershey fonts have no ©), in the page caption with the ©, and under each rail article and on the About page with the rail adapter |

Nothing here identifies a person, a vehicle or a company, and the only data kept is a few
hundred kilobytes of numbers and one map per scene. The README's
[data sources and attribution](../../README.md#data-sources-and-attribution) table has the links.
