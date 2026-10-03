# Enhancement: supply-chain tracker — counting ships at choke points with OpenCV 5

**Status:** proposed, not started · **Date:** 2026-10-03 · **Do it when:** now, if entering the
[AWS OpenCV AI Competition 2026](https://opencv26.devpost.com) (deadline **26 October 2026,
11:45 pm PDT**).
**Background:** chosen over a bushfire topic as the lower-stakes entry; the legal and data-licence
findings in [docs/risks/opencv-bushfire-watch-01.md](../risks/opencv-bushfire-watch-01.md) (items
1, 3, 4, 5 and 8) apply here too.

## The idea

A new topic, **Shipping Choke Points**, watches the anchorages outside a handful of the world's
busiest straits and canals. OpenCV 5 counts the large vessels waiting in each one on every new
Sentinel-2 scene. When a queue grows or shrinks materially against its own recent baseline, Claude
decides whether the change is real and worth reporting, and the daily cycle writes an article
describing what the queues show.

It reports **observations, not market calls**: "the waiting fleet off Suez is about twice its
recent level", never "freight rates will rise".

### Why this fits the competition

- **OpenCV 5 does the substantive work** (30% of the score is "correctness and depth of the
  OpenCV 5 implementation"): water and cloud masking, detection, filtering and counting, plus an
  annotated image for every count.
- **Agentic Vision Award:** visual analysis directly decides what the agent does next. No change:
  stop. A change: Claude triages it, and only a confirmed change becomes a Finding and an article.
- **Meaningful AWS workload:** Lambda (container image), S3, DynamoDB, Bedrock and Step Functions,
  reading data from AWS Open Data.
- **Best Use of COOL Award** (a second special award): the vision Lambda runs on Graviton
  (`arm64`).

### Why ships and not containers

A shipping container is about 2.4 m wide; Sentinel-2's best resolution is 10 m, so containers
cannot be counted without paid high-resolution imagery. Large vessels (roughly 100–400 m long) are
10–40 pixels long and stand out sharply against water in the near-infrared band.

## Today

The research tick (`lambdas/research_tick_handler.py`) loads a topic's adapter from
`ADAPTER_REGISTRY` and calls the contract in `lambdas/common/adapters/base.py`:

- `fetch_state(topic_config)` returns a normalised snapshot. An adapter that sets
  `uses_previous_state = True` also receives the last snapshot, so it can carry history forward.
- `material_diff(old_state, new_state)` returns `(changed, diff_summary)`. The first tick is always
  material.
- `review_evidence(topic_config, latest_state)` returns text for the fresh-data review.

If the diff is material, the tick summarises it with Bedrock and stores a Finding. Every Lambda
ships from one shared zip package with one execution role. Nothing in the package does image
processing.

## Proposal

```
Research tick (existing, every 24 h for this topic)
   └─ ShippingChokePointsAdapter.fetch_state
         │  for each site: newest Sentinel-2 scene since the last one? (STAC search)
         v
   vision Lambda (NEW: container image, arm64, OpenCV 5)
         │  windowed read of the site's bands  ->  masks  ->  detect  ->  count
         │  writes annotated PNG to the content bucket, returns metrics
         v
   material_diff: count vs the site's rolling baseline
         │  no change / no usable scene  ->  stop
         v
   Claude triage (NEW: Bedrock, sees metrics + annotated crop)
         │  "real change" / "artefact (cloud, haze, partial scene)"
         │  artefact  ->  recorded, no Finding
         v
   Finding  ->  daily cycle  ->  article  ->  manual moderation
```

### 1. Sites

A fixed list in `adapter_config`. Each site is a small polygon covering an anchorage (where ships
wait), drawn to **exclude** berths, port infrastructure and land. Starting set:

| Site | Why |
|---|---|
| Singapore Strait, eastern anchorages | busiest transshipment hub |
| Gulf of Suez, southern approach | Suez Canal queue |
| Panama Canal, Pacific and Atlantic anchorages | sensitive to drought-limited transits |
| Strait of Hormuz approaches | oil tankers |
| One Australian port anchorage (e.g. Port Botany or Port Hedland) | local interest for the Sydney audience |

Keeping each polygon small keeps the reads, the compute and false detections down.

### 2. Data: Sentinel-2 L2A from AWS Open Data

- **Find scenes** with the Earth Search STAC API (Element 84) by site bounding box, date and
  scene cloud cover.
- **Read only the site's window** of the Cloud-Optimized GeoTIFFs in `sentinel-cogs` (us-west-2)
  with range requests: about 1–4 bands of a few thousand pixels square per site, a few tens of MB
  at most. Never download whole tiles.
- **Bands:** B08 (NIR, 10 m) for detection; B03 (green) for the water mask; the SCL scene
  classification layer for cloud and cloud-shadow masking.
- **Revisit:** about every 5 days per site, so with five sites most days bring at least one new
  scene. A site whose newest scene is too cloudy is skipped until the next pass.
- **Attribution:** every article and image carries *"Contains modified Copernicus Sentinel data
  [year]"* and is labelled as processed imagery (risks doc, item 1).

### 3. The vision Lambda (OpenCV 5)

A separate function, because OpenCV, numpy and a GeoTIFF reader will not fit in the shared zip
package and do not belong in every other Lambda.

- **Packaging:** container image on ECR, `arm64` (Graviton), 2–3 GB memory, dependencies pinned
  (the submission needs a reproducible build). Use `rasterio` (GDAL) only for windowed reads and
  georeferencing; everything after the read is OpenCV.
- **Pipeline per site:**
  1. **Masks:** water from NDWI (`(B03 − B08) / (B03 + B08)` above a threshold, cleaned with
     `cv2.morphologyEx`). Cloud and shadow from SCL. Intersect both with the site polygon
     (`cv2.fillPoly`). Pixels that are not clear water are excluded, and their share is
     reported as `coverage`.
  2. **Detect:** ships are bright in NIR against dark water. Use a local adaptive threshold
     (`cv2.adaptiveThreshold`, or a background estimate from a large median blur subtracted from
     the image) so haze and sun glint don't move one global cut-off.
  3. **Filter:** `cv2.connectedComponentsWithStats` gives each blob's area. Then
     `cv2.minAreaRect` gives its length and elongation: keep blobs of roughly 8–60 pixels long
     (80–600 m) that are elongated. This rejects noise, small boats, wakes and leftover cloud.
  4. **Count and describe:** vessel count, density per clear km², size distribution, and the
     centroid list (pixel → lat/lon via the scene's transform).
  5. **Annotate:** draw each detection's rotated box (`cv2.drawContours`) on a contrast-stretched
     crop. Write the PNG to the content bucket; it becomes the article's figure.
- **Output:** a JSON metrics record per site: `scene_id`, `captured_at`, `count`, `density`,
  `coverage`, `size_histogram`, `annotated_image_key`, `quality_flags`.

### 4. The adapter

`common/adapters/shipping_choke_points.py`, registered as `shipping_choke_points`. A thin
adapter: it searches STAC, invokes the vision Lambda, and holds the history. It needs no OpenCV
itself.

- `uses_previous_state = True`: the snapshot carries each site's last N counts (say 8 scenes, about
  6 weeks), so the baseline needs no extra table.
- `fetch_state`: for each site with a scene newer than the last one counted, invoke the vision
  Lambda and append the result to that site's history.
- `material_diff`: material when any site's new count differs from its baseline (the median of
  the previous scenes) by at least both a relative (e.g. ±35%) and an absolute (e.g. ±8 vessels)
  threshold, **and** `coverage` is above a floor (e.g. 70%). Low coverage is never material, so a
  half-cloudy scene can't look like a vanished queue. The summary names the sites and figures.
- `review_evidence`: the latest metrics per site, so the fresh-data review can check every number
  and every site an article mentions against what was measured.

### 5. Claude triage: the agent's decision

Between a material diff and a Finding, one tracked Bedrock call (`invoke_model_tracked`, so its
cost lands in lineage and Stats) gets the metrics, the baseline and the annotated crop, and returns
`{"verdict": "real" | "artefact", "reason": "..."}`. Artefacts (cloud edges counted as ships, a
glint streak, a scene clipped at the swath edge) are recorded on the snapshot with the reason but
create no Finding. This makes the loop explicit for the judges: **perception** (OpenCV) →
**decision** (diff, then triage) → **action** (Finding and article, or stop).

**Stretch:** give the triage call tools through Bedrock Converse (`get_site_history`,
`get_previous_crop`), so Claude can compare against an earlier scene before deciding. Do this only
if the core loop is finished early.

### 6. Editorial and review

- **Observations, not advice.** The topic's editorial goals and the compliance review ban price
  predictions, trading suggestions and claims about specific ships, owners or companies. Without
  AIS data no vessel can be identified, so none should be named.
- **Locations come from data, not the model:** site names come from `adapter_config`, never
  inferred by Claude (risks doc, item 8).
- **Manual moderation for every article.** Add a topic flag, `force_manual_review`, that holds
  every draft for review. Do **not** reuse `is_financial`: that also folds financial guidance into
  the draft, which is the opposite of what this topic wants. The same flag covers item 9 of the
  bushfire risks doc, should that topic ever be built.
- **Each article shows** the annotated image(s), capture time, coverage and the attribution line.

### 7. Infrastructure

- ECR repository and a container-image Lambda (`arm64`), tagged like everything else
  (`default_tags`).
- The shared execution role gains `lambda:InvokeFunction` on the vision Lambda only. The vision
  Lambda gets its own role: read on `sentinel-cogs`, write to its own prefix in the content
  bucket.
- CI builds and pushes the image on changes under a new `vision/` directory, and pins it by
  digest, not by tag.
- Region: stay in ap-southeast-2 and read across regions. The windowed reads are small, but
  confirm the data transfer charge for reading `sentinel-cogs` from Sydney before relying on it.
  Moving the vision Lambda to us-west-2, beside the data, would break the project's
  single-region rule for a saving of cents.

## Costs

Per day, at five sites and a scene roughly every five days per site: about one vision run (say
30 s at 3 GB on arm64), one triage call, plus the normal tick and cycle. That is cents a month in
Lambda, a few cents in Bedrock, and a small amount of cross-region transfer. STAC search is free.

## Risks and open questions

- **OpenCV 5 packaging:** confirm an `opencv-python-headless` 5.x wheel exists for Linux aarch64,
  or build from source in the image. Settle this in the first two days.
- **False counts:** fixed platforms, buoys and rigs inside a polygon count as ships. Keep a
  per-site list of known static objects (blobs that appear in every scene at the same place) and
  subtract them.
- **Quiet stretches:** queues may barely move during the build window. For the demo, the history
  can be backfilled from the archive (it goes back years), so a past event shows the loop firing,
  e.g. the Suez queue after the 2021 *Ever Given* blockage or the Red Sea diversions in 2024.
  Label any backfilled article clearly as historical.
- **Ground truth:** for the technical report, hand-count ships in a sample of crops and report
  precision and recall. Judges score correctness, so evidence beats claims.

## Submission checklist (OpenCV AI Competition)

- Technical report: the pipeline, the thresholds and why, the hand-counted accuracy, the costs.
- Architecture diagram: the flow above, with AWS services named.
- Video, 5 minutes at most, showing the loop end to end **and** the safeguards: the coverage floor,
  the artefact triage, manual moderation and attribution (the rules ask to show the entry operates
  safely, legally and responsibly).
- Repository with pinned dependencies and build instructions for the image. It can stay private
  for this competition.

## Plan (3 October → 26 October)

1. **Days 1–3:** OpenCV 5 on arm64 in a container image; a windowed Sentinel-2 read for one site;
   detection working on a saved scene in a notebook or script.
2. **Days 4–8:** the vision Lambda and its Terraform; the adapter, `material_diff` and baseline;
   annotated images in the content bucket.
3. **Days 9–12:** Claude triage, `force_manual_review`, editorial goals and compliance rules,
   attribution in the static page template.
4. **Days 13–16:** all five sites, the static-object list, a historical backfill for the demo,
   and the hand-counted accuracy sample.
5. **Days 17–21:** report, diagram, video. Leave days 22–23 as buffer.

## Tests to write

- Vision: synthetic water images with planted bright elongated blobs (counted), round blobs and
  specks (rejected), a cloud mask covering half the site (coverage reported, ships under cloud not
  counted), land inside the polygon (excluded).
- Adapter: first tick is material; a change below either threshold is not; low coverage is never
  material; history is capped at N scenes; a site with no new scene keeps its last count.
- Triage: an "artefact" verdict creates no Finding; malformed model output is treated as
  "artefact" (fail closed, never as a pass).
- Review: `force_manual_review` holds an article that passed compliance; the attribution line is
  on the rendered page.
- Terraform wiring: the vision Lambda is `arm64` and a container image pinned by digest; the
  shared role can invoke only that function.
