# Enhancement: agentic vision scaffolding for the OpenCV AI Competition 2026

**Status:** in progress (scaffolding) · **Date:** 2026-10-06 · **Deadline:**
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
| Vision worker handler | `lambdas/vision_worker_handler.py` | thin: parse request, read the window, call the core, return metrics |
| Worker dependencies | `lambdas/requirements-vision.txt` | `opencv-python-headless==5.0.0.93`, `numpy`; packaged for `arm64` separately from the shared zip |
| Vision client | `lambdas/common/vision_client.py` | cross-region invoke, timeouts, response validation, backend choice |
| Adapter | `lambdas/common/adapters/satellite_vision.py` | registered as `satellite_vision`; sites, STAC search, history, diff, attribution |
| Triage agent | `lambdas/common/vision_triage.py` | Converse with image + tools, bounded, fail closed |
| Manual-review flag | `force_manual_review` on the Topic | holds every draft; not `is_financial` |
| Terraform | `infra/modules/vision-worker/` | aliased provider, `arm64` Lambda, its own role, gated by `vision_enabled` |
| Deploy-role grants | `infra/bootstrap/main.tf` | Lambda (and later ECR/ECS) in the vision region; manual bootstrap apply |
| Benchmark | `scripts/vision_benchmark.py` | stock vs COOL, x86 vs arm64 |

## 7. Build plan and progress

Small PRs, each green on its own, merged into `dev`. Ticked here as they land.

- [x] **PR 1 — this document**: review of the strategy note, architecture, regions, COOL, agent;
  friction.md 9.4 corrected.
- [ ] **PR 2 — vision core**: `lambdas/vision/` (water and cloud masks, adaptive detection,
  component filtering by length and elongation, counting, coverage, annotation, build info) with
  synthetic-image tests; `requirements-vision.txt`.
- [ ] **PR 3 — vision worker handler and client**: request/response schema, backend field,
  cross-region invoke with a validated response, fakes for tests.
- [ ] **PR 4 — `satellite_vision` adapter**: topic-agnostic sites in `adapter_config`, STAC
  search, history in the snapshot, baseline diff with coverage floor, attribution, registered.
- [ ] **PR 5 — Terraform**: `vision-worker` module (aliased provider, `arm64`, own role, gated by
  `vision_enabled`), deploy-role grants, wiring tests.
- [ ] **PR 6 — triage agent and `force_manual_review`**: Converse with image and tools, bounds,
  fail closed, stored trail.
- [ ] **PR 7 — COOL backend**: build-info check, benchmark script, runsheet for the Marketplace
  subscription; then the COOL compute (Lambda if the image runs there, else ECS on Graviton4).
- [ ] **Then:** pick the topic and write its editorial goals and prompt; run it on dev; the
  technical report, diagram and video.

## 8. Open questions

1. Does the COOL Docker image run on Lambda `arm64`? Decides between Lambda and ECS for the COOL
   backend.
2. Is COOL listed, and is Graviton4 (c8g) offered, in ap-southeast-2 and us-west-2?
3. COOL's price after the 7-day trial.
4. The inter-region transfer charge for the worker's response (small) versus a same-region worker
   reading `sentinel-cogs` across regions (larger). Measure both on dev.
5. Whether "Sentinel models" in the original brief meant the Sentinel-2 data (assumed here) or a
   geospatial foundation model trained on it; the latter would be a third worker backend, not a new
   architecture.
