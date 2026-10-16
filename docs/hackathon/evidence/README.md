# The evidence

Every number in the submission's report cites a file in this directory. Nothing here is typed
from memory: a run file is written by the script that made the measurement, with the code,
machine, build and inputs it ran on, and the report quotes it.

## What is where

| Directory | Holds | Written by |
|---|---|---|
| `runs/` | One JSON **run file** per run, `<run_id>.json`, plus the figures a `scenes` run kept beside its file (`<run_id>.<scene>.png`) | `scripts/vision_benchmark.py --out` and `scripts/vision_eval.py --out` |
| `replay/` | One Markdown trace per replayed sequence, `<sequence>.md`: each tick's decision as the operator's assistant shows it (the six steps of `vision_decisions.trace`), generated from a `replay` run file, never edited by hand | `scripts/vision_eval.py replay --trace` |
| `index.json`, `RESULTS.md` | One row per run file (its id, kind, date, CPU, backend, build, commit and `summary`), and the same as one table per kind, newest first, each row linking its file | `scripts/vision_eval.py index`; CI's `index --check` fails when a run file was added without re-indexing |
| `older-code.json` (when needed) | `{run_id: reason}` for a run made on code that has changed since in a way that matters to it: RESULTS.md marks it "measured on older code" with the reason | by hand, then `index` |
| `live/` (from the export) | The deployed system's evidence and the operator's labels, `live/<date>/labels.csv` (`decision, verdict, figure, trace, label, reason`; `agree`/`disagree`; the reason codes) | `scripts/evidence_export.py` (Q3) and the owner; `index --check` validates the labels |
| `cool/` | What the Graviton session records by hand: `recon.md` (the Marketplace product, its delivery type, Region and price), `cool-buildinfo.txt`, `stock-buildinfo.txt` and `stock4-buildinfo.txt` (each venv's `cv2.getBuildInformation()`, whose sha256 is the build fingerprint), `lscpu.txt`, `session.md` (hours and fees), `perf-dso.txt` if made | the owner, following the [runsheet's COOL section](../../deployment-runsheet.md#the-cool-backend-and-the-benchmark) |

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
 "params": {...}, "results": {...}, "agent": null, "checks": [{"name", "expected", "got", "pass"}],
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
  Region and its `CodeSha256`. Paths (`cv2_path`, a library's `path`, the parameters' files) are
  recorded relative to the repository, or as `~/...` under the home directory, so no run file
  names the user who made it.
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
(the owner captures it from the deployed `vision/rail-cities/sydney/network.json`; until then
`--network <file>`), `lambdas/requirements-vision.txt`, `lambdas/requirements.txt` and
`scripts/requirements.txt`. The exact session, with each command's PowerShell line, is the
runsheet's [evaluation runs](../../deployment-runsheet.md#the-evaluations-real-data-runs).

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
  `bedrock_error`, counted as failed closed, never dropped. About 65 runs of up to five turns: a
  few US dollars of model calls (the run file totals them in USD and AUD) and cents of worker time.
- **`counterfactual`** (2.6.6): C1 and C10 (C4 in C10's place when C10 does not pass the gate),
  the same worker and model as `agent`, `--repeats` 5 per arm; arm (b) clouds SCL in this process,
  so it needs `--cache` too (or `--arms a`). About 22 runs.
- **`stability`** (E3): the year set (`--set year-2024`, the manifest's 17 scenes in time order)
  in this process (`--worker in-process`, the default; with a cache, read once), so the built-up
  masks can be compared; with `--worker <ARN>` the masks' IoU is not measured, and said.
- **`index`**: after any of the above, and `index --check` before committing.

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
| C10 | artefact | the 2019 bushfire smoke scene (aerosol optical thickness 0.44, coverage 0.995) against November 2019; included only if it passes the gate | imagery |
| C6 | failure | C1, every `look_again` refused (`unreadable_scene`), live: reported as observed | - |
| C7 | failure | C1, the clock already past the deadline: `artefact`, `deadline`, no model call | - |
| C8 | failure | C1, live with an invalid model id: `artefact`, `bedrock_error` | - |
| C9 | failure | C1, a scripted answer with no JSON: `artefact`, `malformed` | - |

Fixture A is the Sydney network file; every other network is made from it by code
(`scripts/vision_eval_networks.py`), never a hand-edited copy, so the cases read the same on
whichever copy of the map the owner captures; the run file records each network's digest and what
was added and removed, by id. Added stations and ways have ids starting `eval-` and names saying
they are the evaluation's. The news is constructed: each headline is on the reserved
`news.example.com` domain under a placeholder source, so no real outlet is quoted; a model may
weigh it less for that, and the report says so.

**Scoring** (`scripts/vision_eval_scoring.py`, pure functions, so a summary can be re-made from
its `results`): a run that failed closed (Bedrock error, deadline, turn limit, malformed answer)
scores for neither class and has its own column in the confusion matrix; balanced accuracy is the
mean of the per-class recalls, so numeric-only (every candidate reported) and always-artefact each
score 0.5 whatever the class mix; every share carries its Wilson 95 % interval. A case that never
reached the agent is reported and left out of the scores. A summary holds numbers only: what could
not be measured is left out and listed under `results.not_measured`, never written as zero.

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
- **ESA WorldCover** (the built-up reference the evaluation's later check compares against) is
  © ESA WorldCover project, under CC BY 4.0; a run that uses it cites the product and year.

## The 4.x pin

The Graviton session's third venv runs `opencv-python-headless==4.14.0.94`, so the report can say
what OpenCV 5 changed for this workload. It is PyPI's newest 4.x release (its wheels uploaded on
28 and 29 July 2026; `https://pypi.org/pypi/opencv-python-headless/json`, read on 16 October),
with `cp37-abi3` aarch64 wheels (`manylinux2014_aarch64.manylinux_2_17_aarch64` and
`manylinux_2_28_aarch64`); on 16 October it was installed into a fresh Python 3.11 venv beside
`numpy==2.4.4`, `requests==2.34.2` and `networkx==3.4.2` (the runsheet's stock4 line), `import
cv2; cv2.__version__` gave `4.14.0`, and `vision_benchmark.py kernels` ran on it (x86, so only
that the code runs on 4.x is shown, not a timing). The deployed worker's pin
(`lambdas/requirements-vision.txt`) is unchanged: 5.0.0.93.
