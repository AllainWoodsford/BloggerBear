# OpenCV bushfire watch 01: legal and safety risks

**Date:** 2026-10-03 · **Status:** open, brainstorm — nothing built · **Scope:** the legal and safety
risks of the proposed entry to the [AWS OpenCV AI Competition 2026](https://opencv26.devpost.com):
a satellite imagery adapter feeding an "Australian Bushfire & Smoke Watch" topic. Not legal advice.

## The entry, in brief

The competition requires OpenCV 5 for substantive image analysis and a meaningful part of the
workload on AWS. The target is the **Agentic Vision Award** (a perception → decision → action loop),
with an Overall Award as the stretch goal. Deadline: **26 October 2026, 11:45 pm PDT**.

BloggerBear's research tick already has that loop shape: fetch the current state, diff it against
the prior snapshot, decide whether it materially changed. The entry adds an imagery adapter where
the diff is OpenCV 5:

- **Source:** Himawari-9 (JMA's geostationary satellite over Australia; ~10 min cadence) and
  Sentinel-2 (10 m, ~5-day revisit, for burn-scar before/after), both on AWS Open Data. Not GOES,
  which covers the Americas.
- **Perception:** OpenCV registers frames, masks cloud, detects smoke plumes and hot spots, and
  measures burn-scar area.
- **Decision:** Claude on Bedrock gets the metrics and crops, and decides whether this is new or
  growing against the last snapshot. Yes: store a Finding. No: stop.
- **Action:** the daily cycle writes an explainer through the existing review path.

Late October is the start of the bushfire season, so the demo should have real material.

## Why these risks matter to the competition, not just the site

The judging asks for a video that shows the entry operates **safely, legally and responsibly**. The
rubric also weights **real-world impact** (20%). A fire-reporting agent that misleads or falsely
reassures loses on both. The safeguards below are part of the entry, and the video should show them.

| # | Risk | Kind | Fix | Effort |
|---|---|---|---|---|
| 1 | Data licence terms (Himawari, Sentinel-2) | legal | checked 2026-10-03: both allow it; attribution and "processed" labels on every page | low |
| 2 | Looking like an official emergency warning | legal / safety | banned terms and styling; "not a warning service" banner | low |
| 3 | Defamation: guessing at cause or blame | legal | physical observations only; compliance rule | low |
| 4 | Misleading conduct | legal | disclaimer, capture timestamps, accurate wording | low |
| 5 | Privacy | legal | coarse imagery only; no private properties | low |
| 6 | Staleness: findings are up to a day old by publish time | safety | explainer framing; capture time up front | low |
| 7 | False reassurance from missed detections | safety | never say an area is safe or clear; compliance rule | low–medium |
| 8 | Hallucinated place names | safety | deterministic geocoding; fresh-data review checks locations | medium |
| 9 | Publishing without a human looking | safety | force manual moderation, like financial topics | low |
| 10 | Sensational tone; speculation on losses | safety | prompt and compliance rules | low |

---

## 1. Data licences

**Checked 2026-10-03.** Both datasets allow this use, commercial included, with attribution.

- **Himawari-9** ([AWS registry](https://registry.opendata.aws/noaa-himawari/)): produced by JMA,
  distributed by NOAA under its Open Data Dissemination program. NOAA's conditions: attribute NOAA
  and JMA; never state or imply endorsement by or affiliation with either; and if you modify the
  data, never present it as original, unaltered data. JMA's own
  [terms of use](https://www.jma.go.jp/jma/en/copyright.html) are compatible with CC BY 4.0. They
  allow commercial use, and ask edited content to be credited as e.g. *"Based on Himawari-9 data
  (Japan Meteorological Agency)"* together with a statement that it was edited.
- **Sentinel-2** ([AWS registry](https://registry.opendata.aws/sentinel-2-l2a-cogs/)): free, full and
  open under the Copernicus
  [Sentinel data legal notice](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice),
  which asks derived products to carry *"Contains modified Copernicus Sentinel data [year]"*.
  The registry also gives a citation for the AWS copy: *"Sentinel-2 Cloud-Optimized GeoTIFFs was
  accessed on [DATE] from https://registry.opendata.aws/sentinel-2-l2a-cogs"*.
- **Landsat:** US public domain.

**Why it matters.** The competition requires rights to everything in the entry. Every image the
site publishes is modified: registered, cloud-masked and annotated by OpenCV. That is exactly the
case both NOAA and JMA attach conditions to.

**Fix.**
- Every article and image carries: *"Processed imagery based on Himawari-9 data (Japan
  Meteorological Agency, distributed by NOAA) and/or contains modified Copernicus Sentinel data
  [year]. Edited by BloggerBear; not endorsed by JMA, NOAA or ESA."*
- Label annotated images as processed, for example in the caption, so none can pass as raw
  satellite imagery.
- Record each dataset's licence in the adapter's docstring, and add the attribution to the static
  page template rather than leaving it to the model.
- The submission's technical report cites both datasets, using the registry citation for
  Sentinel-2.

**Side finding: the data is in US regions.** `noaa-himawari9` is in us-east-1 and `sentinel-cogs`
is in us-west-2; the rest of BloggerBear is in ap-southeast-2. Reading across regions adds latency,
may add data transfer cost (check before relying on it), and full-disk Himawari files are large.
Fetch only the bands and segments covering Australia, and read Sentinel-2's Cloud-Optimized
GeoTIFFs with range requests rather than whole files.

## 2. Looking like an official warning

**What.** Australia's emergency services use the Australian Warning System: the levels "Advice",
"Watch and Act" and "Emergency Warning", with set colours and icons.

**Why it matters.** Mimicking them confuses readers during a real emergency, and could read as
impersonating an emergency agency.

**Fix.** Never use those terms, colours or icons. Every article carries a banner, "BloggerBear is not
a warning service", linking to the official sources (NSW RFS *Fires Near Me*, VicEmergency, QFES and
the other state services). Add the banned terms to the compliance review.

## 3. Defamation

**What.** A model asked to explain a fire may guess at its cause: "arson", "a landowner's burn-off",
"a powerline fault".

**Why it matters.** Attributing a fire to a person or business is the classic defamation risk.

**Fix.** Articles describe only what the imagery shows: location, extent, growth, smoke direction.
No causes, no named people or businesses. Add this as a compliance rule.

## 4. Misleading conduct

**What.** Australian Consumer Law's misleading-conduct rules apply to conduct in trade or commerce.
Whether a hobby site is "in trade" is unclear, especially if the site earns money in any way.

**Fix.** The disclaimer (item 2), capture timestamps (item 6) and the no-reassurance rule (item 7)
cover it in practice.

## 5. Privacy

**What.** Himawari pixels are kilometres wide and Sentinel-2's are 10 m, so no individual can be
identified.

**Fix.** Keep it that way. Use no high-resolution commercial imagery and name no private
properties. This keeps the project's no-PII constraint intact.

## 6. Staleness

**What.** The research tick runs hourly and the daily cycle at 9 AM in the topic's zone, so a
finding can be up to a day old by the time its article is published. Fires move much faster.

**Why it matters.** A reader could take a day-old picture for the current situation.

**Fix.** Frame every article as a look-back explainer, never "the current situation". Put the
imagery capture time (in AEST/AEDT) at the top of the article, not just the publish time.

## 7. False reassurance

**What.** Cloud cover hides smoke, and night or thin smoke can defeat the detector. OpenCV will
miss some fires.

**Why it matters.** This is the worst failure mode. "No new activity near X" read by someone
deciding whether to leave is far more dangerous than an over-alert.

**Fix.** An article must never say or imply an area is safe, clear or unaffected. Add this as a
compliance rule that fails the review. Where cloud masking removed a large share of a region,
say coverage was limited.

## 8. Hallucinated place names

**What.** Given pixel coordinates, Claude may name the nearest town it can think of, which is not
necessarily the nearest town.

**Fix.** Turn coordinates into place names in code (a fixed gazetteer, such as the Australian
Gazetteer, looked up by distance) and pass the names to Claude as data. The fresh-data review
checks every place named in the draft against the finding's metrics, and fails the draft on any
place it cannot match.

## 9. No human in the loop

**What.** Non-financial topics can auto-publish when the Bedrock compliance review passes.

**Fix.** Add a safety-sensitive flag that forces every article to manual moderation, the same way
`crypto_feed` topics are forced to `is_financial = True` and so always held for review. This is the
strongest single point for the video's "responsible operation" requirement.

## 10. Tone

**What.** Disaster coverage invites sensational writing and guesses about casualties and property
losses.

**Fix.** Prompt and compliance rules: plain, factual tone; no speculation on lives, homes or losses;
no dramatic headlines.

---

## Fallback topics

If fire activity is quiet, or the safety work proves too heavy for the time available, the same
adapter and change detection work on **flood extent** (for example the Murray-Darling) or
**reservoir surface area** (for example Warragamba Dam) from Sentinel-2. Items 1, 3, 4, 5 and 8
still apply. Items 2, 6, 7 and 9 matter far less, because nobody makes evacuation decisions from a
dam-level article.
