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
- **`index`**: after any of the above, and `index --check` before committing.

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
