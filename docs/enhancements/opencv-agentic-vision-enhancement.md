# Enhancement: agentic vision scaffolding for the OpenCV AI Competition 2026

**Status:** scaffolding built (PRs 1–8 open, CI green); waiting on the operator steps in §9 · **Date:** 2026-10-06 · **Deadline:**
[AWS OpenCV AI Competition 2026](https://opencv26.devpost.com), **26 October 2026, 11:45 pm PDT**.
**Topic:** not decided. This pass builds what every candidate topic needs (the vision worker, the
adapter, the cross-region plumbing, the COOL path and the vision-model triage) so that choosing a
topic later is configuration plus a prompt, not new architecture.
**Builds on:** [supply-chain-tracker-enhancement.md](supply-chain-tracker-enhancement.md) (the
ship-counting design, still the leading topic) and
[docs/risks/opencv-bushfire-watch-01.md](../risks/opencv-bushfire-watch-01.md) (licences and safety).

This document started as a review of an outside strategy note ("BloggerBear x OpenCV AI Competition
2026 Strategy"). Section 1 records what in that note was right, what was wrong and what changed as a
result; the rest is the design that replaced it and the build log.

---

## 1. Review of the strategy note

| # | The note said | Verdict | What is true |
|---|---|---|---|
| 1 | Agentic Vision Award: 30% "OpenCV 5 + agent integration", 25% "orchestration/autonomy" | **Right** | The full rubric: substantive OpenCV 5 and agent integration 30%, orchestration and appropriate autonomy 25%, task effectiveness and evaluation 20%, failure handling / observability / security / human control 15%, UX / docs / demo 10%. |
| 2 | Best Use of COOL: "30% on AWS Graviton / ARM component" | **Half right** | The 30% row is *"verified **COOL** integration on AWS Graviton, or on the Arm component of a documented hybrid architecture"*. The rest: architecture and technical quality 25%, **measured** performance / cost / reliability / productivity 20%, innovation 15%, reproducibility and demo 10%. |
| 3 | "Deploy the vision Lambdas for `arm64` and you automatically score heavily" on COOL | **Wrong** | COOL is the **Cloud-Optimized OpenCV Library**: OpenCV's own Graviton-tuned build of OpenCV 5 (KleidiCV), sold on AWS Marketplace as Ubuntu 24.04 AMIs for **Graviton3** and **Graviton4**, with Docker images for Python 3.10–3.12. An `arm64` Lambda running the stock `opencv-python-headless` wheel is Graviton but is **not COOL** and scores nothing in that row. The award needs COOL itself running the core image workload, plus a measured comparison against a baseline. See §4. |
| 4 | "BloggerBear's compute is entirely AWS Lambda" | **Mostly right** | Lambda plus Step Functions, EventBridge Scheduler and SQS. But every Lambda today is **x86_64** (`infra/modules/ops-assistant/main.tf` says so, and the deploy role's layer grant is x86-only), so nothing is on Graviton yet. |
| 5 | Extend the ops assistant's Strands agent and MCP server to orchestrate the visual tasks | **Wrong place** | The ops assistant answers the *operator*, on its own narrowly scoped, read-only role (docs/friction.md 10.5). Giving it tools that start compute and write Findings undoes that. The agent that decides what the pipeline does lives in the pipeline (§5). The MCP server may gain **read-only** vision tools later, so the operator can ask "what did the vision agent see?". |
| 6 | "Regional restrictions on Sentinel/Agent features in us-east-1 / us-west-2" | **Wrong cause** | Sentinel-2 is ESA satellite imagery, not an AWS feature. What *is* in US regions is the **data**: `sentinel-cogs` (Sentinel-2 L2A) in us-west-2, `noaa-himawari9` in us-east-1. The **model** is not the problem: the pipeline's default model is Claude Haiku 4.5 through the AU inference profile, which takes images, in ap-southeast-2. COOL's Marketplace listings and Graviton4 capacity are per-region and still to be checked for ap-southeast-2 (§3). |
| 7 | Don't migrate finished articles between S3 buckets across regions | **Right** | Agreed, and for one more reason than the note gives: articles, Findings and lineage must stay in the one region the site, the review path and the Stats page read from. |
| 8 | BloggerBear is a "diff-first, **stateless**" pipeline | **Half right** | Diff-first, yes (rule 2). Not stateless: every tick diffs against the last stored snapshot, and adapters with `uses_previous_state` carry history forward in it. The vision adapter relies on exactly that for its baselines. |
| 9 | "Constraint #5: new data sources are adapters, never branches in the core pipeline" | **Right** (paraphrased) | The rule's wording is *"Add new domains through adapters, not core branching logic"* (.github/copilot-instructions.md rule 5; docs/project-plan.md §2). |
| 10 | Treat the vision pipeline as a "Remote Research Adapter" | **Right** | That is the design below: a thin adapter in the home region calls a vision worker that may run in another region, next to the data. |

**Also corrected while reviewing:** docs/friction.md 9.4 said an OpenCV 5 wheel for Linux aarch64 was
unconfirmed. It exists: `opencv-python-headless==5.0.0.93` ships `manylinux2014_aarch64` and
`manylinux_2_28_aarch64` wheels (checked on PyPI, 2026-10-06).

**Sources:** [Devpost](https://opencv26.devpost.com) ·
[OpenCV competition page](https://opencv.org/opencv-ai-competition-2026/) ·
[COOL](https://opencv.org/cool/) ·
[COOL for Graviton4 (Marketplace)](https://aws.amazon.com/marketplace/pp/prodview-fdvbfiewzuehs) ·
[COOL for Graviton3 (Marketplace)](https://aws.amazon.com/marketplace/pp/prodview-5b2boxpyztidw) ·
[AWS blog: the COOL framework](https://aws.amazon.com/blogs/physical-ai/accelerating-opencv-on-graviton-the-cool-framework/) ·
[OpenCV 2026 meeting notes](https://github.com/opencv/opencv/wiki/2026) (COOL Docker images
released for Python 3.10–3.12; ~1.2× average speed-up over stock OpenCV 5.0). The competition pages
are blocked from the build container, so the rubric rows above were read through search results;
re-check them against the Devpost rules page before the submission.

---

## 2. Architecture

```
ap-southeast-2 (home region: everything that holds state)        vision region (next to the data)
─────────────────────────────────────────────────────────        ─────────────────────────────────
research tick ── SatelliteVisionAdapter.fetch_state
                   │ 1. STAC search per site (Earth Search, free)
                   │ 2. VisionClient.analyse(site, scene) ───────────► vision worker (arm64)
                   │                                                    backend = "opencv"  (Lambda, stock OpenCV 5)
                   │                                                    backend = "cool"    (COOL on Graviton3/4)
                   │                                                    windowed COG read → masks → detect → count
                   │ ◄──── metrics JSON (+ annotated PNG, small) ──────  annotate; no state kept
                   │ 3. append to the site's history in the snapshot
               material_diff: count vs rolling baseline, coverage floor
                   │ not material → stop (no Bedrock: rule 2)
                   ▼
               VisionTriage (Bedrock Converse, image + metrics + tools)
                   │ tools: look_again(site, params) · previous_scene(site) · site_history(site)
                   │ "artefact" / malformed → recorded, no Finding (fail closed)
                   ▼
               Finding ─► daily cycle ─► article ─► compliance ─► manual moderation (force_manual_review)
```

### Rules the design keeps

- **Adapters, not branches (rule 5).** One generic adapter, `satellite_vision`, configured per topic
  through `adapter_config` (sites, detector, thresholds). No handler learns what a satellite is.
- **Diff-first (rule 2).** OpenCV runs on every new scene; Bedrock runs only after a material diff.
  The vision worker is not Bedrock, so measuring is allowed before the diff; it is what the diff is
  computed from.
- **State stays home.** The vision worker is stateless: it returns metrics and a small annotated PNG
  in its response. The adapter writes the image to the home region's content bucket and the history
  to the snapshot. No bucket or table exists in the vision region, so nothing needs migrating and a
  vision region can be torn down at any time.
- **No publish without review (rule 3), and no auto-publish at all** for vision topics:
  `force_manual_review` on the topic holds every draft (the risks doc's item 9).
- **Terraform through CI only (rule 6).** Every new resource is behind `vision_enabled` (default
  `false`), so merging scaffolding deploys nothing until the operator turns it on in tfvars.

## 3. Regions

| Piece | Region | Why |
|---|---|---|
| Adapter, snapshots, Findings, images, triage call | ap-southeast-2 | everything with state, and the model, is already here |
| Vision worker (stock OpenCV) | `var.vision_region`, default **us-west-2** | `sentinel-cogs` is there: windowed range reads stay in-region and free of inter-region transfer; only a few KB of metrics and one small PNG cross back |
| COOL worker | `var.vision_region`, if COOL is listed there | Graviton3/4 capacity and the Marketplace listing are per region: **check** ap-southeast-2 and us-west-2 before choosing |

Cross-region is one `lambda:InvokeFunction` from the home region's execution role to one named
function. Terraform gets a second, aliased `aws` provider for the vision region. The CI deploy role
(infra/bootstrap) today may only touch Lambdas in the home region and has no ECR or ECS rights, so
the vision PRs add those grants to the bootstrap policy, and the operator applies the bootstrap by
hand (it is the one manual apply rule 6 allows) **before** setting `vision_enabled = true`.

Setting `vision_region` to the home region also works: the worker then reads across regions instead,
which is slower and may cost transfer, but needs no second provider. That is the fallback if the
operator would rather not grant the deploy role a second region.

## 4. COOL: the path that scores, and the baseline that proves it

The worker has one interface and two backends, chosen per call:

1. **`opencv`** — an `arm64` Lambda with `opencv-python-headless==5.0.0.93` (zip package, no
   container). Cheap, always on, the default. This is also the **baseline** the COOL award's 20%
   "measured value" row asks for.
2. **`cool`** — the COOL build of OpenCV 5 on Graviton3 or Graviton4, from OpenCV's Marketplace
   Docker image. Same Python code, same inputs; only the `cv2` underneath changes.

What has to be settled first (the first COOL PR does this, before any infrastructure):

- **Which compute runs the COOL image.** Lambda's `arm64` has been Graviton2. COOL is built for
  Graviton3/4 and may use instructions Graviton2 lacks, so COOL-in-Lambda is unproven. The safe
  path is ECS on an EC2 Graviton4 instance (c8g) or AWS Batch, started on demand per tick. If the
  COOL image runs correctly on Lambda `arm64`, that is simpler and is preferred.
- **Proving COOL is in use.** The worker reports `cv2.__version__` and a hash of
  `cv2.getBuildInformation()`, and the build information's KleidiCV line, with every result, so the
  logs and the Stats page show which build did the work. A `cool` call that comes back from a
  non-COOL build fails rather than being counted.
- **Marketplace subscription** is a console step (like Cost Explorer, docs/friction.md 1.10) with a
  7-day trial and then usage pricing. It goes in the deployment runsheet, not in Terraform, and its
  cost goes in the costs section once known.
- **The benchmark** (`scripts/vision_benchmark.py`): the same scenes through stock OpenCV 5 on
  x86_64, stock on arm64, and COOL on Graviton, reporting per-stage latency (read, mask, detect,
  annotate), total, memory and cost per scene. Reproducible from a pinned scene list. Its table goes
  in the technical report.

## 5. The agent (Agentic Vision Award)

The award weights orchestration and autonomy at 25% and failure handling and human control at 15%.
The design turns the supply-chain doc's one-shot triage into a bounded tool-using agent:

- **Perception:** the vision worker (OpenCV 5, or COOL).
- **Decision:** `VisionTriage` gets the metrics, the site's baseline and the annotated crop as an
  image block through Bedrock Converse (Claude Haiku 4.5 reads images; same AU inference profile as
  the rest of the pipeline, tracked with `invoke_model_tracked` so the cost lands in lineage and
  Stats). It may call three tools before answering:
  - `look_again(site, params)` re-runs the worker on the same scene with different parameters
    (a tighter water mask, a different size band): **active perception**, the award's own example;
  - `previous_scene(site)` analyses the site's last scene, so it can compare like with like;
  - `site_history(site)` returns the stored counts.
- **Action:** a Finding (then the normal article path) or a recorded "artefact" with its reason.
- **Bounds, in code not prompt:** at most N tool calls (default 3) and a per-tick cost cap; a
  malformed answer, a tool failure or the cap being hit is `"artefact"`, never `"real"` (fail
  closed); every tool call and verdict is logged and stored on the snapshot, so the demo can replay
  the agent's reasoning trail.
- **Human control:** `force_manual_review` on the topic. No vision article is published without a
  person approving it in the review inbox.

The agent is written against Bedrock Converse's tool use directly, not Strands, to keep the
pipeline's shared zip free of an agent framework (the reason `requirements-ops-agent.txt` exists).

## 6. Components and where they live

| Component | Path | Notes |
|---|---|---|
| Vision core (pure OpenCV + numpy) | `lambdas/vision/` | masks, detection, filtering, counting, annotation, build info; no AWS |
| Scene reading | `lambdas/vision/cog.py`, `geo.py`, `scene.py` | windowed GeoTIFF reads over HTTP ranges, lon/lat → UTM pixels, bands aligned to one grid; **no GDAL** (§7, PR 3) |
| Vision worker handler | `lambdas/vision_worker_handler.py` | thin: validate, read the window, call the core, reply with metrics, figure, build and timings |
| Worker dependencies | `lambdas/requirements-vision.txt` | `opencv-python-headless==5.0.0.93`, `numpy==2.4.4`; packaged for `arm64`, `python3.12`, separately from the shared zip |
| Contract | `lambdas/common/vision_contract.py` | request/reply schema shared by both sides; asset URL allowlist |
| Vision client | `lambdas/common/vision_client.py` | invokes the worker in the region its ARN names; checks the reply |
| Adapter | `lambdas/common/adapters/satellite_vision.py` | registered as `satellite_vision`; sites, STAC search, history, diff, attribution |
| Triage agent | `lambdas/common/vision_triage.py` | Converse with image + tools, bounded, fail closed |
| Manual-review flag | `force_manual_review` on the Topic | holds every draft; not `is_financial` |
| Terraform | `infra/modules/vision-worker/` | aliased provider, `arm64` Lambda, its own role, gated by `vision_enabled` |
| Deploy-role grants | `infra/bootstrap/main.tf` | Lambda (and later ECR/ECS) in the vision region; manual bootstrap apply |
| Benchmark | `scripts/vision_benchmark.py` | stock vs COOL, x86 vs arm64 |

## 7. Build plan and progress

Small PRs, each green on its own, merged into `dev`. PRs 2 to 4 are stacked (each based on the
one before) so each shows only its own diff; GitHub retargets each to `dev` as the one below it
merges. Ticked here as they are opened and green; "merged" is noted when it happens.

- [x] **PR 1 — this document** (AllainWoodsford/BloggerBear#240): review of the strategy note,
  architecture, regions, COOL, agent; friction.md 9.4 corrected.
- [x] **PR 2 — vision core** (#241, CI green): `lambdas/vision/` masks (site polygon, SCL cloud and
  no data, NDWI water that keeps ships inside it), adaptive Gaussian detection, `minAreaRect`
  length/elongation filters, per-site metrics, the annotated figure, the build fingerprint;
  `requirements-vision.txt`, added to the Trivy scans. 19 synthetic-image tests.
- [x] **PR 3 — scene reading without GDAL** (#242, CI green). *Changed from the plan:* rasterio
  (GDAL) + OpenCV + numpy for `arm64` is 278 MB unzipped, 254 MB stripped, over Lambda's 250 MB zip
  limit, and a container image would put ECR and an arm64 Docker build on the baseline's critical
  path. `vision/cog.py` reads the TIFF subset `sentinel-cogs` uses (tiled, single band, DEFLATE +
  horizontal predictor, GeoTIFF scale/tiepoint/EPSG) with one range request for the header and
  one per tile; `vision/geo.py` projects lon/lat to UTM with Snyder's series. **Checked on real
  data:** Botany Bay from tile 56HLH in 3 requests (~2.7 MB); projection within 1 mm of PROJ.
- [x] **PR 4 — worker handler, contract, client** (#244). The worker refuses the backend it isn't,
  and a `cool` worker whose `cv2` isn't the pinned COOL build refuses everything (`not_cool`). The
  client calls the worker in the region its ARN names and validates every reply. **End to end on
  real data** (Botany Bay, `S2A_56HLH_20240105_0_L2A`, B08 + B03 + SCL): 2.5 s, 9 range requests,
  5.5 MB read, 28 ms of OpenCV. *Tuned from it:* 93 raw detections were mostly mixed pixels along
  shore and cloud edges; `edge_buffer_px` (drop blobs beside anything unmeasured) took it to 15,
  and every result now tallies why candidates were dropped, for the triage agent to read.
- [x] **PR 5 — `satellite_vision` adapter** (#245, CI green). Sites, `object_noun`, backend,
  params and thresholds are all `adapter_config`. Material only when *both* thresholds are crossed
  against the median of earlier **clear** scenes; low coverage is never material and never in the
  baseline; a site that can't be searched or measured keeps its history (`last_error`). *Found
  while building it:* the research tick only kept state with a Finding, so a baseline would have
  held only reported scenes. Adapters can now opt in to `keeps_running_state` (generic, like
  `uses_previous_state`): the tick keeps the newest state at `snapshots/<topic>/running-state.json`.
- [x] **PR 6 — Terraform** (#246, CI green). `infra/modules/vision-worker`: arm64, python3.12,
  in `vision_region` (us-west-2) through the provider's per-resource `region` (no aliased
  provider), its own log-only role, and an artifacts bucket (the arm64 zip is ~51 MB, at the
  direct-upload limit). Both environments call it with `count = var.vision_enabled` (**false**);
  the bootstrap gains `<prefix>-*-vision-*` Lambda and log rights in the vision Region.
- [x] **PR 7 — triage agent and `force_manual_review`** (#248, CI green). Converse with the
  figure as an image and three tools (`look_again`, `previous_scene`, `site_history`); a tool
  budget and `budget + 2` turns, enforced in code; anything unsure is an artefact; the trail is
  stored; spend on Stats as `vision_triage`. `force_manual_review` holds every draft for a person
  (the compliance review still runs) and is forced on for `satellite_vision` topics. *Bug found:*
  the admin API returned 500 for any `adapter_config` holding a float (a polygon); fixed.
- [x] **PR 8 — COOL benchmark and image** (#251). `scripts/vision_benchmark.py` (`kernels`,
  `scenes`, `worker`; each report names the Graviton generation and the OpenCV build fingerprint)
  and `docker/vision-cool/Dockerfile` (the same handler on OpenCV's COOL image, never its own cv2).
  COOL compute in Terraform waits on question 1 in §8.
- [ ] **Then:** the operator steps in §9; pick the topic and write its editorial goals and prompt;
  run it on dev; the benchmark table; the technical report, diagram and video.

### What the first real scene taught

- Thin cloud that SCL doesn't flag still makes bright, elongated wisps: the detector can't tell
  every one from a ship. That is by design the triage agent's call (§5), with the figure and the
  rejection tally in front of it, and a scene under the coverage floor is never material anyway.
- The read dominates: 2.5 s of the 2.8 s total was fetching tiles from Sydney to us-west-2. In the
  vision region it should be a fraction of that, which is the case for running the worker there
  (§3). The benchmark (PR 8) will measure both.

### Where the agent sits, as built

The decision step runs inside the adapter's `material_diff`, after the numeric thresholds, on the
same adapter instance that fetched (so it knows the topic's model and the sites). The research
tick stays topic-agnostic; it stores the state after the diff, so the agent's trail is kept
whether or not a Finding is written. A triage costs at most `budget + 2` Converse calls plus up to
`budget` worker calls; with the defaults that fits inside the research tick's 120 s beside the
60 s fetch budget, but a topic with many sites should lower `max_sites_per_tick`.

## 8. Open questions

1. Does the COOL Docker image run on Lambda `arm64`? Decides between Lambda and ECS for the COOL
   backend.
2. Is COOL listed, and is Graviton4 (c8g) offered, in ap-southeast-2 and us-west-2?
3. COOL's price after the 7-day trial.
4. The inter-region transfer charge for the worker's response (small) versus a same-region worker
   reading `sentinel-cogs` across regions (larger). Measure both on dev.
5. Lambda arm64's CPU generation today. The benchmark's `worker` mode reports nothing about the
   worker's CPU (it runs remotely); adding `cpu_identity` to the worker's reply would settle it from
   one invoke.
6. Whether "Sentinel models" in the original brief meant the Sentinel-2 data (assumed here) or a
   geospatial foundation model trained on it; the latter would be a third worker backend, not a new
   architecture.

## 9. What the operator has to do (nothing here is automatic)

1. **Review and merge** #240, then #241 → #251 in order (each is based on the one before;
   GitHub retargets the next to `dev` as each merges). Merging deploys nothing new:
   `vision_enabled` is false.
2. **Re-apply `infra/bootstrap` by hand** (rule 6), with `vision_region` (default us-west-2).
3. **Set `vision_enabled = true`** in `infra/environments/dev/terraform.tfvars` and merge.
4. **Create a topic**: `adapter: "satellite_vision"`, `sites` in `adapter_config`, an
   `object_noun`, and editorial goals (docs/deployment-runsheet.md, "The vision worker").
5. **COOL**: subscribe, measure, pin the fingerprint (runsheet, "The COOL backend and the
   benchmark"); unsubscribe after the trial if it isn't kept.

