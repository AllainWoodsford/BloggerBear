# The evidence

Every number in the submission's report cites a file in this directory. Nothing here is typed
from memory: a run file is written by the script that made the measurement, with the code,
machine, build and inputs it ran on, and the report quotes it.

## What is where

| Directory | Holds | Written by |
|---|---|---|
| `runs/` | One JSON **run file** per run, `<run_id>.json`, plus the figures a `scenes` run kept beside its file (`<run_id>.<scene>.png`) | `scripts/vision_benchmark.py --out` and `scripts/vision_eval.py --out` |
| `cool/` | What the Graviton session records by hand: `recon.md` (the Marketplace product, its delivery type, Region and price), `cool-buildinfo.txt`, `stock-buildinfo.txt` and `stock4-buildinfo.txt` (each venv's `cv2.getBuildInformation()`, whose sha256 is the build fingerprint), `lscpu.txt`, `session.md` (hours and fees), `perf-dso.txt` if made | the owner, following the [runsheet's COOL section](../../deployment-runsheet.md#the-cool-backend-and-the-benchmark) |

The evaluation's later subcommands add an `index.json` and a generated `RESULTS.md` here, built
from the run files; until then the files themselves are the index.

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
- `code.git_sha` is the commit the run was made on, and `dirty` whether the tree had uncommitted
  changes. A run made before the submission tag is still cited when the code it measured has not
  changed since, or when the index says why the difference does not matter.
- `environment.host` holds five fields of the EC2 instance identity and nothing else (never an
  account id, an instance id or an IP). `environment.libraries` lists, on Linux, the OpenCV shared
  objects mapped into the process during the run, each with its sha256 and the package that owns
  it: with the build fingerprint, that is what shows which binaries did the work.
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

The first run file, `runs/2026-10-16-eval-coverage-intel-xeon-processor-2-80ghz-none.json`, is the
coverage of every scene in `scripts/vision_eval_manifest.json`, measured on 16 October on the
build container (x86, outside us-west-2: the environment says so). Coverage does not depend on the
CPU; the Graviton session's run files are the first measurements of the worker's speed.

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
what OpenCV 5 changed for this workload. It is PyPI's newest 4.x release (28 July 2026), with
`cp37-abi3` aarch64 wheels (`manylinux2014_aarch64.manylinux_2_17_aarch64` and
`manylinux_2_28_aarch64`); on 16 October it was installed into a fresh venv beside
`numpy==2.4.4` and `import cv2; cv2.__version__` gave `4.14.0`. The deployed worker's pin
(`lambdas/requirements-vision.txt`) is unchanged: 5.0.0.93.
