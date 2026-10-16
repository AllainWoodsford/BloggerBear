# OpenCV AI Competition 2026: the Devpost form's text

**What this is:** the text the owner pastes into the Devpost form, field by field
([the checklist](devpost-checklist.md) says where each field's file or link comes from). Everything
else is in the [technical report](technical-report.md), which this points to rather than repeats:
the problem and the users, the architecture and both diagrams, the OpenCV 5 implementation, the AWS
deployment, the evaluation, the limitations and responsible use. **Deadline:** 26 October 2026,
11:45 pm PDT. The competition's pages were read through search results from the build container,
so the form's fields are re-checked against the Devpost page by hand before submitting.

## Title

**Rail Access Monitor: an autonomous publisher that watches a city's rail network from space**

## One-line pitch

A research-and-publishing pipeline that measures a city's built-up density, the walking reach of
its stations and its rail network as a graph from free satellite imagery and OpenStreetMap, with
OpenCV 5 in a worker beside the imagery, and writes an article only when the numbers moved, a
bounded agent has checked that the change is real, and a person has approved it.

## The description

Where a city builds and where its trains reach drift apart slowly and quietly, and the numbers that
would show it are in open data nobody reads every week. The Rail Access Monitor reads them. One
topic watches Sydney, Melbourne, Singapore and Boston, measuring each city's newest clear
Sentinel-2 scene as it arrives and writing about one city a day by a fixed weekly rota.

- **Perception with OpenCV 5, beside the imagery.** A stateless worker, an arm64 Lambda in
  us-west-2 next to the public Sentinel-2 bucket, range-reads only the tiles under a city (no GDAL),
  builds a built-up density map from NDBI and NDVI, measures every pixel's distance to the nearest
  station with one labelled distance transform, outlines the built-up areas beyond walking reach,
  turns OpenStreetMap's rail lines into a graph with hit-or-miss thinning and `filter2D`, and draws
  the map the article carries.
- **Diff first, then a bounded agent, then a person.** No model is called unless a number moved
  against the city's own baseline. Then a triage agent on Bedrock, with at most three tool calls and
  a deadline, may re-measure the scene with OpenCV at other settings, compare earlier scenes and
  read the news; anything unsure is an artefact and makes no Finding. A person approves every
  article.
- **Honest about what it measures.** Built-up area, not people; a straight line, not the walking
  route; flags and simulated links are hypotheses from proxies, never recommendations; every
  article says what was not measured, and a cloud never reads as an empty suburb.
- **Every step on the record.** Each measurement, comparison, agent decision and human decision
  leaves one JSON line of codes and numbers, kept 90 days, and the operator can ask about it on the
  assistant's page or through Alexa+.

The whole story, with every number cited to a run file, is the
[technical report](technical-report.md); how to build and test it is [REPRODUCE.md](REPRODUCE.md).

## Agentic Vision

The loop is **perception, diff, agent, person**, and each arrow is bounded in code
([the workflow diagram and the trace](technical-report.md#appendix-b-agentic-vision)):

1. **Perception.** The worker measures every new scene with OpenCV. No model is involved.
2. **Diff.** The adapter compares the measurement with the city's own baseline (the median of
   earlier clear scenes measured the same way; a cloudy scene never counts). Nothing moved: the tick
   stops, and no model is called.
3. **Agent.** A numeric change is a candidate, not a Finding. The agent sees the map and the
   numbers and may call `look_again` (OpenCV re-measures the same scene at other settings: active
   perception), `previous_scene`, `site_history` and `web_context` (recent news, from GDELT with the
   AgentCore web-search gateway as its fallback), within a tool budget, that plus two turns, and a
   deadline 85 s after the tick started. The answer must be a strict JSON verdict; a malformed
   answer, a Bedrock error, no model, the turn limit or the deadline is "artefact". What the tools
   return is data, not instructions, and the agent's reason reaches a summary only quoted, as its
   note, not a measurement.
4. **Person.** Every vision article is held for a person; the daily digest, which publishes with
   no person, leaves these topics out.

**What is recorded.** The last 12 decisions per city, with each tool's result, are in the running
state, and every decision is in the logs for 90 days, from the scene to the person's approval; the
evaluation's replay re-runs a recorded trace on a judge's machine, and its counterfactuals change
only OpenCV's output, to test whether that alone changes the decision.
[R2: the headline result, from the report's Appendix B: the trace, the counterfactual's "k of n",
and the agent's balanced accuracy]

## Best Use of COOL

The same worker handler runs on two backends: stock `opencv-python-headless` 5.0.0.93 (the
deployed worker) and OpenCV's COOL build for Graviton4. A COOL worker whose build fingerprint, the
SHA-256 of `cv2.getBuildInformation()`, does not match the pinned one refuses every request, and the
KleidiCV line is reported beside it. `scripts/vision_benchmark.py` times the OpenCV operations the
worker spends its time on and the whole handler on real Sydney scenes, from a cache so every run
reads the same bytes; every run file records the CPU, the build fingerprint and the OpenCV libraries
mapped during the run, so a COOL row is provably a COOL row; and `compare` gives the speed-up with a
bootstrap interval and checks the two backends measured the same thing. COOL is benchmarked on
Graviton4, not deployed ([Appendix A](technical-report.md#appendix-a-best-use-of-cool)).
[R2: the headline speed-up on one core with its interval, and "parity: pass", from the report's
Appendix A]

## Links

| What | Where |
|---|---|
| The technical report | [technical-report.md](technical-report.md), and its PDF at the tag |
| The code | [R2: the repository's URL and the tag `opencv-2026-submission`, or the archive (owner step O8)] |
| The video | [R2: the unlisted video's link] |
| The working endpoint | [R2: the approved rail article's URL] |
| Reproduce it | [REPRODUCE.md](REPRODUCE.md) |
| The diagrams | [the system](diagrams/architecture.svg) and [the agent's workflow](diagrams/agent-workflow.svg) |
