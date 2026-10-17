# The evidence

Every number in the submission's report cites a file in this directory. Nothing here is typed
from memory: a run file is written by the script that made the measurement, with the code,
machine, build and inputs it ran on, and the report quotes it.

## What is where

| Directory | Holds | Written by |
|---|---|---|
| `runs/` | One JSON **run file** per run, `<run_id>.json`, plus the figures a `scenes` run kept beside its file (`<run_id>.<scene>.png`) | `scripts/vision_benchmark.py --out` and `scripts/vision_eval.py --out` |
| `replay/` | One Markdown trace per replayed sequence, `<sequence>.md`: each tick's decision as the operator's assistant shows it (the six steps of `vision_decisions.trace`), generated from a `replay` run file, never edited by hand | `scripts/vision_eval.py replay --trace` |
| `index.json`, `RESULTS.md` | One row per run file (its id, kind, date, CPU, backend, build, commit and `summary`), and the same as one table per kind, newest first, each row linking its file | `scripts/vision_eval.py index`; `index --check` fails when a run file was added without re-indexing. Run it before committing run files; CI runs it too, on every pull request that touches `docs/hackathon/`, `scripts/` or `lambdas/` |
| `older-code.json` (when needed) | `{run_id: reason}` for a run made on code that has changed since in a way that matters to it: RESULTS.md marks it "measured on older code" with the reason | by hand, then `index` |
| `live/` (from the export) | One directory per export, `live/<UTC date>/`: what the deployed system measured, decided and did, with `provenance.json` and `summary.json` ([below](#the-live-evidence-live)); and the operator's labels, `live/<date>/labels.csv` (`decision, verdict, figure, trace, label, reason`; `agree`/`disagree`; the reason codes) | `scripts/evidence_export.py export` and the owner; `index --check` validates the labels |
| `cool/` | What the COOL reconnaissance and the Graviton session record by hand. From the reconnaissance of 16 October: [`recon.md`](cool/recon.md) (the Marketplace product, its delivery type, Region, price and layout) and [`cool-buildinfo.txt`](cool/cool-buildinfo.txt) (COOL's `cv2.getBuildInformation()`, byte for byte; its sha256 is the fingerprint, checked by `scripts/tests/test_cool_evidence.py`). From the session: `stock-buildinfo.txt` and `stock4-buildinfo.txt` (each stock venv's build information), `lscpu.txt`, `session.md` (hours and fees), `perf-dso.txt` if made | the owner, following the [COOL benchmark runsheet](../../cool-benchmark-runsheet.md) |

[`RESULTS.md`](RESULTS.md) is the index a reader starts from; `index.json` is the same for code
(the operator's assistant copies its rows). Both are generated: after adding run files, run
`python scripts/vision_eval.py index` (PowerShell: `py -3.11 scripts\vision_eval.py index`) and
commit the two files with them.

## The run file

Schema `bloggerbear.vision-eval/1` (`scripts/vision_runs.py` writes and reads it):

```
{"schema": "bloggerbear.vision-eval/1", "run_id", "kind", "mode", "backend", "started_at", "finished_at",
 "code": {"git_sha", "dirty"},
 "environment": {"cpu", "machine", "cpu_count", "affinity", "threads", "python", "numpy", "networkx",
                 "opencv": build_info() + {"cv2_path"},
                 "where": "ec2" | "wsl" | "windows" | "macos" | "linux" | "ci",
                 "host": {"instance_type", "ami_id", "region", "architecture", "marketplace_product_codes"} | null,
                 "cool": {"product", "version", "delivery", "image_digest", "price"} | null,
                 "libraries": [{"path", "sha256", "bytes", "package"}] | null,
                 "build_lines": [...]},
 "worker": {"kind": "in-process" | "deployed", "region", "code_sha256"},
 "inputs": {"manifest_sha256", "cache", "scenes": [{"name", "scene_id", "etag_by_asset"}], "networks": [{"file", "sha256"}]},
 "params": {...}, "results": {...}, "checks": [{"name", "expected", "got", "pass"}],
 "agent": null | {"mode": "bedrock" | "recorded" | "scripted", "model_id", "responses": [{"tick", "site_id", "turns"}],
                  "input_tokens", "output_tokens", "cost_usd", "cost_aud", "price_source"},   # a replay's
 "summary": {<fixed code>: <number>}}
```

- `run_id` is `<UTC date>-<kind>-<cpu slug>-<backend>`, with a `-<tag>` suffix when several runs
  of one kind are made in a day (`1t`, `4t-r1`); the file is named after it.
- `code.git_sha` is the commit the run was made on, and `dirty` whether a tracked file had
  uncommitted changes (untracked files, such as the session's own run files before they are
  committed, do not count). A copy without git history, the COOL container image, names its
  commit through `BLOGGERBEAR_GIT_SHA` (the image's `GIT_SHA` build argument), with `dirty` null.
  A run made before the submission tag is still cited when the code it measured has not changed
  since, or when the index says why the difference does not matter.
- `environment.host` holds five fields of the EC2 instance identity and nothing else (never an
  account id, an instance id or an IP). `environment.libraries` lists, on Linux, the OpenCV shared
  objects mapped into the process during the run (with `--isolate`, into the processes the scenes
  ran in), each with its sha256 and the package that owns it: with the build fingerprint, that is
  what shows which binaries did the work.
- `worker` never holds an ARN (it carries the account id): a deployed worker is named by its
  Region and its `CodeSha256`. Nor does a replay's `agent.model_id` (or `params.model_id`): it is
  the id the decision ring keeps, so a system inference profile's ARN is recorded as its id and
  an application inference profile's as null. Paths (`cv2_path`, a library's `path`, the
  parameters' files) are recorded relative to the repository, or as `~/...` under the home
  directory, so no run file names the user who made it.
- `inputs.scenes` carries every asset's ETag as read. A run refuses to start when a pinned ETag
  has changed ("input changed"), so two run files that cite the same ETags read the same bytes.
- `results` is each script's own shape (the benchmark's old report shape is kept there, so its
  Markdown table and tests are unchanged); `summary` is a flat map of fixed codes to numbers,
  which the index and the operator's assistant read.

## The kinds that exist now

| Kind | Script | What it holds |
|---|---|---|
| `bench-kernels` | `vision_benchmark.py kernels` | each OpenCV operation's samples and summary on synthetic images, with `analyse_rail_access`'s own stage timings |
| `bench-scenes` | `vision_benchmark.py scenes` | per pinned scene: the handler's stage timings and samples, the rail metrics and their digest, the figure's sha256 and shape, the peak memory |
| `bench-worker` | `vision_benchmark.py worker` | the deployed worker, end to end, with the caller's round trip |
| `bench-compare` | `vision_benchmark.py compare` | two runs of one machine and the same inputs: the speed-up of B over A with a bootstrap interval, per stage and on the compute stages, and the parity of the rail metrics |
| `bench-controls` | `vision_benchmark.py controls` | the COOL guard's three negative controls and whether each answered as expected |
| `eval-coverage` | `vision_eval.py coverage` | per pinned scene the SCL class shares inside the site, the coverage as the worker defines it and the mean aerosol optical thickness; and the year's selection counts |
| `eval-replay` | `vision_eval.py replay` | the real rail adapter over a pinned sequence, tick by tick (2.6.4): each tick's evidence lines as printed, the decision ring's entries, every worker call with its metrics digest, the clock's readings, and under `agent.responses` every Converse response (Bedrock or scripted), so `--agent recorded` replays it; with `--agent recorded`, the checks that it reproduced its recording |
| `eval-failures` | `vision_eval.py failures` | the failure catalogue (2.5): per case its input, what must happen, the tests that hold it (pytest node ids, each checked to be collected) and, for the real-data rows, what the worker and the adapter did |
| `eval-access-baseline` | `vision_eval.py access-baseline` | E5 on one scene: the production distance transform against the precise one and a straight-line buffer, each served share, the IoU of the served masks and the times |
| `eval-agent` | `vision_eval.py agent` | the agent's evaluation (2.6.1-2.6.3): per case its history's codes, its networks (each by digest, with what the derivation added and removed), whether it passed the gate, and per repeat the verdict, stop, every step, the reason, time, tokens and cost in USD and AUD; the scores (per-class recall with Wilson intervals, balanced accuracy, the confusion matrix with failed-closed runs in their own column) for the agent and both baselines; and under `agent.responses` every Converse response |
| `eval-counterfactual` | `vision_eval.py counterfactual` | 2.6.6: arm (a), each case's repeats with `look_again` answered by the newest scene's own re-measurement or the same with the moved measures at the baseline median, paired by repeat; arm (b), the same scene with SCL clouded to coverage 0.62 and 0.58: the code, whether the agent was called, whether a Finding would be written |
| `eval-stability` | `vision_eval.py stability` | E3: the year set through the production rule on one network, each scene's code and measures, the false-alarm rate over the decisions with its Wilson 95 % interval and by month, and the spreads (served share, desert and built-up area, the built-up mask's IoU, the Jaccard of hubs and flags) |
| `eval-builtup-check` | `vision_eval.py builtup-check` | E8: the worker's built-up mask on one scene against ESA WorldCover 2021's class 50, resampled onto the scene's 20 m grid: the IoU, precision and recall over the pixels both saw, each WorldCover class's share of our built-up pixels, and the served share over each mask with the same stations and reach; `inputs.reference` names the tile, its ETag and size, and its licence |

The first run file, `runs/2026-10-16-eval-coverage-intel-xeon-processor-2-80ghz-none.json`, is the
coverage of every scene in `scripts/vision_eval_manifest.json`, measured on 16 October on the
build container (x86, outside us-west-2: the environment says so). Coverage does not depend on the
CPU; the Graviton session's run files are the first measurements of the worker's speed.

## Running the evaluation for real

Every subcommand below is in CI on the synthetic city or with scripted parts; on real data they
are the operator's runs, and only their run files are evidence. They read the pinned scenes over
HTTPS from `sentinel-cogs` (us-west-2): give them a cache (`--cache ~/cogs`; PowerShell
`--cache $HOME\cogs`), which a first run fills (about 54 MB a scene) and every later run reads
from. They need the Sydney network fixture, `scripts/vision_benchmark_networks/sydney.json`
(committed on 17 October from the deployed `vision/rail-cities/sydney/network.json`, #349;
`--network <file>` takes another copy), `lambdas/requirements-vision.txt`, `lambdas/requirements.txt` and
`scripts/requirements.txt`, and for `failures`' collection check pytest and moto
(`lambdas/requirements-dev.txt`, which holds all three `lambdas/` files). The exact session,
with each command's PowerShell line, is the runsheet's
[evaluation runs](../../deployment-runsheet.md#the-evaluations-real-data-runs).

- **`replay --sequence sydney-winter | sydney-demo | sydney-c1`** (2.6.4): the real adapter over
  06-27, 08-21b, 08-26 and 08-31, through `fetch_state` then `material_diff`, one tick per scene,
  each six hours after its capture. `--worker <the deployed worker's ARN>` (site ids sent as
  `eval-sydney`) or `--worker in-process --cache DIR`; `--agent bedrock --model-id <id>` (every
  Converse response recorded), `--agent recorded --from runs/<id>.json` (no AWS: the responses
  replayed, every measurement recomputed in-process and held to the recording within 2.7.1's
  tolerances, its excluded fields listed in the run file) or `--agent scripted`. `--trace` writes
  `replay/<sequence>.md`. sydney-demo runs with the demo topic's thresholds of 0, disclosed in its
  trace; sydney-c1's last tick reads network B, the fixture plus a constructed five-station line
  on the previous scene's largest area beyond reach (or `--network-b FILE`), with a constructed
  news item. The replay stops at the adapter's decision: "a Finding would be written", never a
  Finding.
- **`failures`** (2.5): the catalogue's tests are checked to be collected; the rows that read
  nothing run anywhere; with `--cache` the real-data rows run through the worker handler in this
  process (the cloudy and no-data scenes, the smoke scene, a site off the tile, a window past the
  rail cap, a missing key), and the cloudy row also runs the real adapter for one tick on a scene
  the search does pick under the floor.
- **`access-baseline`** (E5): on `sydney-2024-08-26` by default (`--scene`), with the network.
- **`agent`** (2.6.1-2.6.3): every case in `scripts/vision_eval_cases/` (below), each case's
  history built through the real adapter, then its newest tick asked of the real agent
  `--repeats` times (5) from the same state: `--worker <the deployed worker's ARN>` (production's
  in-Region read and timing; site ids `eval-sydney`) or `--worker in-process --cache DIR`, which is
  refused without the cache off an EC2 instance in us-west-2 (an uncached re-measurement would run
  the case out of its 85 s for the harness's reasons); `--agent bedrock --model-id <id> --region
  <Bedrock's Region>`. C7 and C9 run their scripts even then; C8 is asked with an invalid model id;
  C6's re-measurements are all refused. A run without Bedrock access ends every case
  `bedrock_error`, counted as failed closed, never dropped. About 55 live runs of up to five turns
  (65 in all: C7's and C9's ten run their scripts, with no Bedrock call): a few US dollars of model
  calls (the run file totals them in USD and AUD) and cents of worker time. A repeat whose newest
  scene the worker did not measure keeps the worker's code, is scored nowhere and fails the run's
  check. `--case C1` (repeatable, or `--cases C1,C2`) runs some; `--model-id` without `--agent
  bedrock` is refused.
- **`counterfactual`** (2.6.6): C1 and C10 (C4 in C10's place when C10 does not pass the gate),
  the same worker and model as `agent`, `--repeats` 5 per arm; arm (b) clouds SCL in this process,
  so it needs `--cache` too (or `--arms a`). About 22 runs. Arm (b) counts a pair only when both
  sides were measured at their coverage; a stand-in's pair replaces the case's, never adds to it.
- **`stability`** (E3): the year set (`--set year-2024`, the manifest's 17 scenes in time order)
  in this process (`--worker in-process`, the default; with a cache, read once), so the built-up
  masks can be compared; with `--worker <ARN>` the masks' IoU is not measured, and said.
- **`builtup-check`** (E8): `sydney-2024-08-26` (`--scene`) in this process against ESA WorldCover
  2021's tile `S36E150` (eu-central-1), read in the script over the site's box (about 10 MB of a
  20 MB object, kept in the cache), its ETag and size checked against the pins first; it needs the
  network fixture for the served shares. Its run is the operator's: on 17 October the build
  container read both buckets for a validation run with no network (served shares not measured), not
  committed, which found the worker's mask far stricter than WorldCover's class: precision 0.88,
  recall 0.10, IoU 0.10 over 4,143 km² compared (the report's number comes from the operator's run).
- **`index`**: after any of the above, and `index --check` before committing.

The three live runs, with `W` the deployed worker's ARN and `M` the rail topic's model id (the
runsheet's step 4 sets both; steps 6 to 8 say what each run checks):

```sh
python scripts/vision_eval.py agent --worker "$W" --agent bedrock --model-id "$M" --region ap-southeast-2 --repeats 5 --out docs/hackathon/evidence/runs
python scripts/vision_eval.py counterfactual --worker "$W" --cache ~/cogs --agent bedrock --model-id "$M" --region ap-southeast-2 --repeats 5 --out docs/hackathon/evidence/runs
python scripts/vision_eval.py stability --cache ~/cogs --out docs/hackathon/evidence/runs
```

```powershell
py -3.11 scripts\vision_eval.py agent --worker $W --agent bedrock --model-id $M --region ap-southeast-2 --repeats 5 --out docs\hackathon\evidence\runs
py -3.11 scripts\vision_eval.py counterfactual --worker $W --cache $HOME\cogs --agent bedrock --model-id $M --region ap-southeast-2 --repeats 5 --out docs\hackathon\evidence\runs
py -3.11 scripts\vision_eval.py stability --cache $HOME\cogs --out docs\hackathon\evidence\runs
```

## The agent's cases

`scripts/vision_eval_cases/<case>/case.json` (`bloggerbear.vision-case/1`), one directory a case:
the history (manifest scenes, each with the network it was measured with), the newest scene, the
thresholds (the adapter's defaults), the news `web_context` replays (`null` would withhold the
tool), what is expected and why, and which input moved (`driven_by`). A case may build on another
(`base`). Every real case is network-driven: a true growth of the city within the window could not
be built honestly from imagery.

| Case | Class | Built as | Driven by |
|---|---|---|---|
| C1 | real | three clear winter scenes with fixture A; the newest with fixture B, A plus a five-station line laid on the largest desert of the baseline's newest measurement (the replay's own `sydney-c1` line); news of its opening | network |
| C1c | real | as C1, with three stations extending a line from its end nearest the baseline's deserts, toward them | network |
| C1d | real | as C1, with A less one line (the smallest named line with three stations of its own) and its stations; news of its closure | network |
| C1e | real | as C1, with one infill station between two stations of one line, on ground both already serve | network |
| C2 | artefact | as C1, with B': three stations mapped twice under new ids at the same places; no news | network |
| C3 | artefact | a summer scene against the winter baseline, one network; included only if it passes the gate | imagery |
| C4 | artefact | a winter scene with coverage 0.667 (`sydney-2024-08-11`, a third of the city under cloud or its shadow) against clear ones | imagery |
| C5 | artefact | C2, plus a headline whose title is an instruction to the model | network |
| C10 | artefact | the 2019 bushfire smoke scene (aerosol optical thickness 0.44, coverage 0.995) against the clear scenes before it (2019-11-01, -11-06 and -12-21, coverage above 0.99; the two November scenes at 0.70 and 0.71 are left out); included only if it passes the gate | imagery |
| C6 | failure | C1, every `look_again` refused (`unreadable_scene`), live: reported as observed | - |
| C7 | failure | C1, the clock already past the deadline: `artefact`, `deadline`, no model call | - |
| C8 | failure | C1, live with an invalid model id: `artefact`, `bedrock_error` | - |
| C9 | failure | C1, a scripted answer with no JSON: `artefact`, `malformed` | - |

Fixture A is the Sydney network file; every other network is made from it by code
(`scripts/vision_eval_networks.py`), never a hand-edited copy, so the cases read the same on
whichever copy of the map the owner captures; the run file records each network's digest and what
was added and removed, by id. Added stations and ways have ids starting `eval-`; a new line's and
the infill station's names say they are the evaluation's, while an extension keeps its line's
name and a duplicate its station's, as the map itself would. The news is constructed: each
headline is on the reserved `news.example.com` domain under a placeholder source, so no real
outlet is quoted; a model may weigh it less for that, and the report says so.

**Scoring** (`scripts/vision_eval_scoring.py`, pure functions, so a summary can be re-made from
its `results`): a run that failed closed (Bedrock error, deadline, turn limit, malformed answer)
scores for neither class and has its own column in the confusion matrix; balanced accuracy is the
mean of the per-class recalls, so numeric-only (every candidate reported) and always-artefact each
score 0.5 whatever the class mix; every share carries its Wilson 95 % interval. A case that never
reached the agent is reported and left out of the scores. A summary holds numbers only: what could
not be measured is left out and listed under `results.not_measured`, never written as zero.

## The live evidence (`live/`)

`scripts/evidence_export.py export --env dev` writes what the deployed system measured, decided and
did into `live/<UTC date>/`. It is read-only against AWS; the owner runs it on Saturday 24 October
(O9) and again by Thursday 29, with the commands and the credentials the runsheet's
[evidence export](../../deployment-runsheet.md#the-evidence-export-the-trace-and-the-labels-o9) gives.
Every file holds only schema fields: codes, ids, times and numbers, each through the evidence
writer's own kinds (`lambdas/common/evidence.py`); never a log's `@log` (it names the account),
`@ptr` or raw message, a site's or a station's name, a news title or URL, a summary or the agent's
reason. Run with `--with-notes`, the agent's reasons, the news hosts it read and the Findings'
summaries are added, through `redact.scrub` and labelled "unverified, model text" (or third-party
text); the report quotes notes from the evaluation's run files by default. Before the export
finishes, every ARN and every 12-digit number left is replaced with `[redacted]`: a run of exactly
twelve digits, beside a hyphen too, but not one inside a longer hex token (a log stream's id, an
ETag), a UUID's last group, or a `run`, `build` or `reason_sha256` value that is the code's own
12-hex id. `summary.json`'s `redacted` counts them (0 is expected) and `redacted_at` names each
file, line and field. A line's log stream and time are written only in Lambda's and Logs Insights'
own shapes. `scripts/pii_denylist_check.py --files` then reads every file written; a hit refuses the
export (exit 2): the directory is moved to `live/<date>.refused/`, which git ignores, the operator's
`labels.csv` left where it was, and every network file it wrote put back as it was.

`--replace` writes over an export: it removes only the export's own files and folders, never the
operator's `labels.csv`, and refuses a directory that holds anything else. `live/README.md` is
written only beside the exports in `live/`. `--networks` leaves a network file that already holds
the same network byte for byte (the evaluation's run files pin its sha256), and keeps one that holds
another network unless `--replace-networks` is given.

| File | From | Holds |
|---|---|---|
| `decisions.jsonl` | each vision topic's running state (its decision ring, `snapshots/<topic>/running-state.json`) and the research tick's `vision_decision` and `vision_triage` lines | one line per decision, `(topic_id, site_id, scene_id)` once: the ring's entries first (`source: state`, kept), then the decisions only the logs still hold (`source: logs`, `kept` from their attempt's `research_tick` line); each with its fields of the ring's schema, the agent's part without its reason or hosts, who measured, `finding_written` (from its attempt's line, else the Findings and the articles, else null) and its not-kept copies under `not_kept` (an attempt that failed: `stored_false`, or `no_attempt_line`) |
| `triage.jsonl` | the research tick's group | every `vision_triage` line of the topics: the verdict, the stop, every step with its result, tokens and cost in USD |
| `worker.jsonl` | the worker's group, us-west-2 | every `vision_measure` line, with its REPORT line's billed duration and memory joined by the request id, and its `caller` from the site id (`production`, `demo`, `eval`, `bench`); then every REPORT with no line, `ended_without_a_reply` with its `cause` (`timeout`, `out_of_memory`, `error`, `other`), or `ended_without_a_reply: null` with no cause when it was logged where the `vision_measure` query did not read (whether it replied is not known) |
| `ticks.jsonl` | the research tick's group | every `research_tick` line of the topics: outcome, `stored`, decisions, triages, the Finding's time, the tick's own ms |
| `people.jsonl` | the admin API's group | every `moderation_decision` and `topic_changed` line of the topics; never who |
| `articles.json` | the Articles table (a Query on `by_topic_created_at`) | per topic since the window's start: `article_id`, `status`, `published_by`, `created_at`, `published_at` and the lineage's research `captured_at`s; never the body or the title |
| `findings.json` | the Findings table (a Query on the topic) | `captured_at`, `expires_at`, the figures' keys, planner-note counts by type, `research_call` (its model id, tokens), the subjects (site ids); the summary only with `--with-notes` |
| `history.json` | the running states | per topic its times and thresholds; per site its first observation, network times and counts, the last error's code and time (never its detail), and each history entry's scalars (never stations, flags, suggestions, deserts, hubs or the network) |
| `figures/` (`--figures N`) | the content bucket | at most N figures of kept decisions that were triaged (whatever the agent's verdict, an artefact's too) or reported, the triaged first, for the labels: not every one shows a reported change. They carry their credits in the strip |
| `traces/` | the above | one Markdown trace per triaged decision (`vision_decisions.trace`, as the assistant's scene view shows it), for the labels |
| `labels-template.csv` | the triaged decisions | one row per triaged decision: `decision` (`<topic>/<site>/<scene>`), `verdict`, `figure`, `trace` (file names), and empty `label` and `reason`; the reason codes in its header. Filled in and saved as `labels.csv` (O9) |
| `provenance.json` | everything read | where each line came from: the log group the query named, the stream, the time, the query (its Logs Insights id, window and run time, under `queries`); each state's key, ETag and time; each function's `CodeSha256` and `LastModified` (never its ARN or role) and the deploy that produced it: the head commit of the newest `terraform.yml` run on `dev` created before `LastModified` (`gh run list`; null, with the reason, without `gh`); the exporter's own commit |
| `summary.json` | all of the above | the numbers below, the cities and the demo topics apart (the tracker's `Demo topics:` line), and how each log read ended (`complete`, `cut`, or `not_checked` with the window missing: a count from a read not checked is said as partial, never as zero) |

`summary.json` holds:

- **the decisions** by code, zero rows included; triaged, the verdicts, reported, the Findings
  written, the not-kept copies, and the decisions whose attempt could not be read (`unknown_kept`);
- **the failures observed in the deployment**, zero rows included: the sites' turns by outcome and
  by error code; the worker's refusals by code, production's and the demo's; the worker runs that
  ended without a reply (from REPORT lines: a worker timeout never writes its line; a REPORT logged
  where the `vision_measure` query did not read is `not_checked`, never counted); the attempts
  not kept (`stored: false`, or no `research_tick` line) and the research tick's timeouts; the
  OpenStreetMap failures (site turns with `osm`, each site's network error now); the triage stops
  other than `answered`;
- **E11**, the research tick's own time with the agent, measured without it, and with no decision
  (n, p50, p95, slowest);
- **the worker** by caller, build and CPU: runs, refusals, total and billed ms (p50, p95, slowest),
  peak memory, and the GB-seconds billed;
- **human control and observability** (2.6.5): each group's articles, those published and by whom
  (`held_share` is published by people over published: expected 1.0), the people's decisions with
  the hours from the Finding; the operator's agreement on live decisions (`k` of `n` with its
  Wilson 95 % interval, from every `labels.csv` under `live/`; `evidence_export.py agreement`
  recounts it once the labels are filled in); completeness over kept attempts (every kept
  decision and triage line with its ring entry, or the ring rolled over; every worker request id of
  a kept attempt with its line, or a REPORT that ended without a reply, or one the caller gave up
  on; anything else listed as `unmatched`, with where it came from); the share of production's
  worker lines that carry the build and the CPU;
- **the spend**: the agent's (every triage line's `cost_usd`, a failed attempt's included) and the
  research summaries' (the Findings' calls, at the built-in price table), in AUD at the project's
  fixed rate with USD beside it.

`scripts/evidence_export.py trace --topic <topic> [--site <site>] --scene <id>|latest --from
live/<date>` prints one decision's chain from an export (or live, with `--env dev`); the report and
the video quote it. `live/README.md` carries the OpenStreetMap attribution and the Copernicus credit.

## How to add a run

1. Run the script with `--out docs/hackathon/evidence/runs` (a directory: the file is named after
   the run id) and, when several runs of one kind are made in a day, `--tag`.
2. Commit the run file and any figure written beside it. Nothing in a run file names a person, an
   account or a host beyond the five instance-identity fields.
3. Cite it from the report by file name; the number in the report must be the one in the file's
   `summary` or `results`.

## Licences of the inputs

- **Sentinel-2** imagery is Copernicus Sentinel data, processed by ESA and distributed on AWS
  Open Data (`sentinel-cogs`, us-west-2) under the Copernicus Sentinel data terms and conditions
  (free, full and open access, with attribution: "contains modified Copernicus Sentinel data
  [year]"). The manifest pins each scene's assets by ETag; the run files cite them.
- **OpenStreetMap** data (the rail network fixtures under `scripts/vision_benchmark_networks/`,
  and every network the worker is sent) is © OpenStreetMap contributors, under the Open Database
  Licence 1.0; the directory's `NOTICE.md` carries the attribution, the extract date and the
  query, and says that this repository's Apache-2.0 licence does not cover those files.
- **ESA WorldCover** 2021 v200 (the built-up reference E8, `builtup-check`, compares against;
  the `esa-worldcover` bucket on AWS Open Data, eu-central-1) is under CC BY 4.0, with the
  attribution "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021)
  processed by ESA WorldCover consortium". A run that uses it records the product, the tile, the
  object's ETag and the licence under `inputs.reference`; nothing of it is committed but those
  numbers.

| Input | Licence | Credit | Where the repository uses it |
|---|---|---|---|
| Sentinel-2 L2A (Copernicus) | Copernicus Sentinel data terms (free, full and open, with attribution) | "Contains modified Copernicus Sentinel data [year]" | every scene the worker reads; the figures; `live/` |
| OpenStreetMap | ODbL 1.0 | "© OpenStreetMap contributors" | the networks the worker is sent; `scripts/vision_benchmark_networks/` (its `NOTICE.md`); station ids in `live/` |
| ESA WorldCover 2021 v200 | CC BY 4.0 | "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium" | E8 (`builtup-check`) only, read in the script |

## The 4.x pin

The Graviton session's third venv runs `opencv-python-headless==4.14.0.94`, so the report can say
what OpenCV 5 changed for this workload. It is PyPI's newest 4.x release (its wheels uploaded on
28 and 29 July 2026; `https://pypi.org/pypi/opencv-python-headless/json`, read on 16 October),
with `cp37-abi3` aarch64 wheels (`manylinux2014_aarch64.manylinux_2_17_aarch64` and
`manylinux_2_28_aarch64`); on 16 October it was installed into a fresh Python 3.11 venv beside
`numpy==2.4.4`, `requests==2.34.2` and `networkx==3.4.2` (the COOL runsheet's stock4 line), `import
cv2; cv2.__version__` gave `4.14.0`, and `vision_benchmark.py kernels` ran on it (x86, so only
that the code runs on 4.x is shown, not a timing). The deployed worker's pin
(`lambdas/requirements-vision.txt`) is unchanged: 5.0.0.93.
