# Agentic vision: what was built, how it works, and how to run it

**Date:** 2026-10-06 · **For:** the [AWS OpenCV AI Competition 2026](https://opencv26.devpost.com)
(deadline 26 October 2026, 11:45 pm PDT) · **Status:** scaffolding built, in pull requests, not yet
deployed (`vision_enabled` is off) · **Topic:** not chosen yet.

This is the documentation for the whole vision workstream in one place: the corrections to the
original strategy note, the design, every component and setting, what was measured on real data,
what went wrong along the way, and what an operator has to do to turn it on. The design rationale in
more depth is [opencv-agentic-vision-enhancement.md](opencv-agentic-vision-enhancement.md); the
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

Merge in this order; each after #240 is based on the one before it, and GitHub retargets the next
to `dev` as each merges. **Merging deploys nothing new** until `vision_enabled` is set.

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
| this PR | This document |

---

## 4. Components

| Piece | Path | What it does |
|---|---|---|
| Masks | `lambdas/vision/masks.py` | Site polygon (`fillPoly`); cloud, shadow and no data from the SCL band; NDWI water that keeps ships inside it (small non-water patches are taken back as water). |
| Detection | `lambdas/vision/detect.py` | 8-bit stretch; masked areas painted with median water; `adaptiveThreshold` (Gaussian); blobs beside anything unmeasured dropped (`edge_buffer_px`); `minAreaRect` length and elongation filters; a tally of why candidates were dropped. |
| Per-site analysis | `lambdas/vision/analyse.py` | Count, coverage, clear-water km², density, size histogram, map coordinates, quality flags, the parameters used. |
| Figure | `lambdas/vision/annotate.py` | Contrast-stretched NIR, unmeasured areas tinted, rotated boxes, a burned-in "Processed imagery" caption; PNG, ≤ 1024 px. |
| Build record | `lambdas/vision/build.py` | OpenCV version, SHA-256 of `getBuildInformation()`, CPU baseline, KleidiCV; `matches()` against a pinned fingerprint. |
| GeoTIFF reader | `lambdas/vision/cog.py` | Range-reads only the tiles under a window: classic/BigTIFF, either byte order, DEFLATE + horizontal predictor, GeoTIFF scale/tiepoint/EPSG, nodata. |
| Projection | `lambdas/vision/geo.py` | Lon/lat → WGS 84 UTM (Snyder's series; under 1 mm from PROJ), polygon → pixels, clipped windows. |
| Scene reading | `lambdas/vision/scene.py` | Reads all bands over the site aligned to the 10 m NIR grid (20 m SCL resampled with `warpAffine`, nearest); 4096 px window cap. |
| Worker | `lambdas/vision_worker_handler.py` | Validates, reads, analyses, annotates; replies with metrics, figure, build record, I/O and per-stage timings. Stateless. |
| Contract | `lambdas/common/vision_contract.py` | Request/reply schema shared by both sides; asset URL allowlist; parameter bounds; error codes. |
| Client | `lambdas/common/vision_client.py` | Invokes the worker in the region its ARN names; validates every reply; every failure is `VisionError`. |
| Adapter | `lambdas/common/adapters/satellite_vision.py` | Sites, STAC search, history, diff, triage hook, attribution, summary prompt, review evidence. |
| Triage agent | `lambdas/common/vision_triage.py` | Converse with the figure and three tools; bounded; fail closed; trail returned. |
| Terraform | `infra/modules/vision-worker/` | The worker, its role, log group and artifacts bucket in `var.region`. |
| Benchmark | `scripts/vision_benchmark.py` | `kernels`, `scenes`, `worker` modes; reports CPU generation and build fingerprint. |
| COOL image | `docker/vision-cool/Dockerfile` | The same handler on OpenCV's COOL image. |

### The worker contract

Request:

```json
{"version": 1, "backend": "opencv",
 "site": {"id": "botany-bay", "polygon": [[151.20, -33.97], [151.26, -33.97], [151.26, -34.02]]},
 "scene": {"id": "S2A_56HLH_20240105_0_L2A", "captured_at": "2024-01-05",
           "assets": {"nir": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/…/B08.tif",
                      "green": "…/B03.tif", "scl": "…/SCL.tif"}},
 "params": {"offset": 25}, "coverage_floor": 0.7, "image": true}
```

Reply: `{"version": 1, "ok": true, "backend", "site_id", "scene_id", "captured_at", "metrics",
"epsg", "window", "image_png_b64", "build", "io", "timings_ms"}`, or
`{"version": 1, "ok": false, "error", "detail"}` with `error` one of `bad_request`,
`backend_mismatch`, `not_cool`, `site_outside_scene`, `window_too_large`, `unreadable_scene`,
`internal`.

Detector parameters a request may set (bounds in `PARAM_LIMITS`): `stretch_max`, `block_size`
(odd), `offset`, `min_length_m`, `max_length_m`, `min_elongation`, `edge_buffer_px`.

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

### Terraform

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `vision_enabled` | dev and production tfvars | `false` | Create the worker and give the research tick its ARN and invoke right. |
| `vision_region` | dev and production | `"us-west-2"` | Where the worker runs. Must equal the bootstrap's. |
| `vision_region` | `infra/bootstrap` | `"us-west-2"` | Where the deploy roles may create `<prefix>-*-vision-*` Lambdas and log groups. |
| `memory_size`, `timeout` | module | 2048 MB, 60 s | The worker's size; the timeout stays under the client's 90 s. |

### Environment variables

| Variable | Set on | Meaning |
|---|---|---|
| `VISION_WORKER_ARN` | research tick | The stock worker; empty when off, which reads as "not configured". |
| `VISION_COOL_WORKER_ARN` | research tick | The COOL worker, once it exists. |
| `VISION_BACKEND` | worker | `opencv` or `cool`: what this deployment is. |
| `COOL_BUILD_SHA256` | COOL worker | The pinned COOL build fingerprint. |
| `VISION_ALLOWED_URL_PREFIXES` | worker, research tick | Optional; defaults to the `sentinel-cogs` bucket. |

### Topic flag: `force_manual_review`

Any topic may set it; `satellite_vision` topics always have it. The daily cycle adds "this topic's
articles are always reviewed by a person before publishing" to the hold reasons, so nothing
auto-publishes. Unlike `is_financial`, drafting is unchanged and the compliance review still runs,
so the moderator sees its verdict. Stored only when true.

### Where things are stored

| What | Where |
|---|---|
| Per-site history, triage trails | `snapshots/<topic>/running-state.json` in the content bucket, and each Finding's snapshot |
| Figures | `vision/<topic>/<site>/<scene>.png` in the content bucket (private) |
| Triage spend | Stats page, category `vision_triage` |
| Worker package | `<prefix>-<env>-vision-artifacts-<account>-<region>` bucket, `vision-worker/<md5>.zip`, 14-day expiry |

---

## 6. How the decisions are made

### 6.1 The rules this keeps

- **Rule 2, diff-first:** OpenCV measures every new scene; Bedrock is only called after a numeric
  material change (the triage), and then for the summary.
- **Rule 3 and the risks doc's item 9:** no vision article publishes without a person.
- **Rule 5, adapters:** the handlers learned nothing about imagery; the one research-tick change
  is a generic opt-in.
- **Rule 6, Terraform:** everything is behind `vision_enabled`; the bootstrap change is applied
  by hand.
- **Rule 7, security:** Trivy scans now cover `requirements-vision.txt` and
  `requirements-vision-cool.txt`; nothing was weakened.

### 6.2 Material change

A site is material when its new scene's coverage is at least the floor, there are at least
`min_baseline` earlier clear scenes, and the count differs from their median by at least
`absolute_threshold` **and** `relative_threshold`. The first tick of a topic is always material
(the adapter contract). A site that could not be searched or measured keeps its history and
records `last_error`: **not measured is never "nothing there"**.

### 6.3 The triage agent

Given the metrics, the baseline, the rejection tally and the figure, Claude may call:

- `look_again(settings)`: re-measure the same scene with other detector settings;
- `previous_scene()`: the previous scene's metrics and figure;
- `site_history()`: every stored scene's date, count and coverage.

Bounds are in code: at most `triage_max_tool_calls` tool calls, at most that plus two model turns,
and a strict JSON answer `{"verdict": "real" | "artefact", "reason": "…"}`. A malformed answer, an
unknown verdict, a Bedrock error, no model, or the turn limit all mean **artefact**: an unsure agent
never makes a Finding. The trail is stored and the reason of a "real" verdict goes into the research
summary.

### 6.4 Running state

The research tick used to keep state only when it wrote a Finding, so a baseline would have held
only reported scenes, and every tick would have re-measured the same scene. Adapters can now set
`keeps_running_state`: the tick also writes the newest state to
`snapshots/<topic>/running-state.json` after every tick, reads it first next time, and falls back to
the last Finding's snapshot if it is gone (it expires with the `snapshots/` lifecycle).

### 6.5 What articles may say

The summary prompt allows observations only: site names as given, counts, the change against the
baseline, the capture date. It forbids naming any vessel, owner, company or person, causes,
predictions, prices or trading views, saying any area is safe or clear, and inventing places or
numbers.

### 6.6 Attribution

Every `satellite_vision` article and topic page credits *"Contains modified Copernicus Sentinel
data"* (the Copernicus legal notice's wording) and *"Sentinel-2 Cloud-Optimized GeoTIFFs accessed
from the Registry of Open Data on AWS"*, and both are on the About page. Each figure is labelled as
processed imagery; the capture year belongs in the figure caption.

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

Kernel benchmark, stock OpenCV 5.0.0 on x86 (1024 px): adaptive threshold 4.8 ms, connected
components 2.4 ms, contours 0.4 ms, whole `analyse_site` 35.6 ms. **The COOL and Graviton rows are
still to be measured** (§9).

Tests: the full suite passed at 4412 on the last PR, and 4448 with the whole stack merged into the
latest `dev`. CI is green on every PR in the stack.

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
9. **The build container's limits.** The Terraform registry and Earth Search STAC are blocked
   from it: providers came from a local mirror of releases.hashicorp.com (lock files left
   untouched), and the STAC search is tested with fakes only. `sentinel-cogs` itself was reachable,
   which is what made the real-data checks possible.

---

## 9. What the operator has to do

None of this is automatic.

1. **Merge** #240, then #241 → #251 in order, then this PR.
2. **Re-apply `infra/bootstrap` by hand**, with `vision_region` (default us-west-2). Until then
   an apply with `vision_enabled = true` is refused.
3. **Set `vision_enabled = true`** in `infra/environments/dev/terraform.tfvars`, and merge.
4. **Choose the topic** and create it: `adapter: "satellite_vision"`, its `sites`, an
   `object_noun`, and editorial goals. Check `docs/risks/opencv-bushfire-watch-01.md` against it.
5. **COOL:** subscribe to *Cloud Optimized OpenCV For AWS Graviton4* on Marketplace (7-day trial,
   then usage pricing); on a `c8g` from the COOL AMI run
   `python3 scripts/vision_benchmark.py kernels --out cool-g4.json`, and the same with a stock
   OpenCV venv; pin the COOL `build_sha256` as `COOL_BUILD_SHA256`; terminate the instance and
   unsubscribe if not kept. `docs/deployment-runsheet.md` has the steps.
6. **For the submission:** the benchmark table (x86, Graviton stock, Graviton COOL), a run of the
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
