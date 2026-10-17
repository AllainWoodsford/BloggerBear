# Reproduce the Rail Access Monitor

For the OpenCV AI Competition 2026's judges: how to build, test and run what the
[technical report](technical-report.md) describes, in four tiers, from "any machine with Python"
to "a Graviton4 instance with COOL". Each tier says what it needs, how long it takes, and what
output to check. Every command has its PowerShell line beside the bash one.

| Tier | What | Needs | Time |
|---|---|---|---|
| [1a](#tier-1a-the-ci-commands) | Lint and the whole test suite, exactly as CI runs them | Python 3.11; internet for the install only | about 10 minutes |
| [1b](#tier-1b-real-data-runs-each-with-an-output-to-check) | The real-data runs: coverage of the pinned Sentinel-2 scenes, the OpenCV kernels, the replay | internet (the public `sentinel-cogs` bucket); no AWS account | about 30 minutes |
| [2](#tier-2-deploy-it-on-aws) | Deploy it on AWS and watch a city measured, triaged and held for a person | an AWS account, a GitHub fork, Bedrock model access | an afternoon, then days of real scenes |
| [3](#tier-3-cool-on-graviton4) | COOL against stock OpenCV on Graviton4 | an AWS account and the COOL Marketplace subscription | about 3 hours |

Clone the repository at the submission tag first ([R2: the repository's URL, or the archive's,
from owner step O8; the same URL goes in both commands below, or, if an archive is submitted,
its download-and-unpack commands replace them]):

```bash
git clone <R2: the repository URL> BloggerBear && cd BloggerBear && git checkout opencv-2026-submission
```

```powershell
git clone <R2: the repository URL> BloggerBear; cd BloggerBear; git checkout opencv-2026-submission
```

## Tier 1a: the CI commands

These are the steps of `.github/workflows/python-ci.yml` (install, ruff, pytest), plus the judges'
constraints file, `constraints-py311.txt`, which pins every package CI installed, transitive ones
included ([dependencies.md](dependencies.md)). `scripts/tests/test_hackathon_docs.py` checks that
this block installs the same requirements files and runs the same `ruff` and `pytest` commands as
the workflow, with only `-c constraints-py311.txt` added, so the two cannot drift apart unseen. No
AWS account; nothing touches the network after the install.

```bash
python3.11 -m venv .venv && . .venv/bin/activate
python -m pip install -c constraints-py311.txt --upgrade pip
pip install -c constraints-py311.txt -r lambdas/requirements.txt
pip install -c constraints-py311.txt -r lambdas/requirements-dev.txt
pip install -c constraints-py311.txt -r scripts/requirements.txt
ruff check lambdas/ scripts/
pytest lambdas/ scripts/
```

```powershell
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
python -m pip install -c constraints-py311.txt --upgrade pip
pip install -c constraints-py311.txt -r lambdas\requirements.txt
pip install -c constraints-py311.txt -r lambdas\requirements-dev.txt
pip install -c constraints-py311.txt -r scripts\requirements.txt
ruff check lambdas\ scripts\
pytest lambdas\ scripts\
```

**What to check.** `ruff` prints `All checks passed!`; `pytest` ends with [R2: "N passed, M
skipped", from a python-ci run of R2's pull request whose tests are the tag's, with the run's URL;
nothing runs CI on the tag itself]. CI runs this on Ubuntu with Python 3.11; on
Windows the same commands install and run, but only Linux is checked by CI, so WSL2 gives the
like-for-like result. The constraints file is Linux CI's own `pip freeze --all`, so on Windows or
macOS pip also resolves the few packages that exist only there (`colorama` for `click`, for one)
without a pin.

## Tier 1b: real-data runs, each with an output to check

None of these is in CI. They read public data over the internet, about 0.7 MB a scene for
coverage and about 54 MB a scene for a whole-city measurement, and need no AWS account. Tier 1a's
environment is all they need: `lambdas/requirements-dev.txt` includes the worker's own packages
(`lambdas/requirements-vision.txt`: OpenCV 5, numpy, networkx).

**1. The pinned scenes have not changed.** `vision_eval.py pin` asks the bucket for every asset's
ETag and size and compares them with `scripts/vision_eval_manifest.json`.

```bash
python scripts/vision_eval.py pin
```

```powershell
python scripts\vision_eval.py pin
```

Check: it prints that the manifest matches the bucket. A changed ETag is named and exits 1; every
run then refuses that scene ("input changed").

**2. Coverage, from the imagery itself (E1 and E2).** The share of each pinned scene the worker can
measure, why the rest is unusable, and the year's scene-selection counts, recomputed on your
machine and compared with the committed run file. `--only` takes a few scenes; without it, all 46
take a few minutes.

```bash
python scripts/vision_eval.py coverage --only sydney-2024-01-05 --only sydney-2019-12-26-smoke --out my-coverage.json
python -c "import json, sys; a, b = (json.load(open(p, encoding='utf-8'))['summary'] for p in sys.argv[1:3]); keys = (a.keys() & b.keys()) - {'scenes', 'explained'}; d = {k: (a[k], b[k]) for k in sorted(keys) if a[k] != b[k]}; print(d or f'{len(keys)} values, all the same')" docs/hackathon/evidence/runs/2026-10-16-eval-coverage-intel-xeon-processor-2-80ghz-none.json my-coverage.json
```

```powershell
python scripts\vision_eval.py coverage --only sydney-2024-01-05 --only sydney-2019-12-26-smoke --out my-coverage.json
python -c "import json, sys; a, b = (json.load(open(p, encoding='utf-8'))['summary'] for p in sys.argv[1:3]); keys = (a.keys() & b.keys()) - {'scenes', 'explained'}; d = {k: (a[k], b[k]) for k in sorted(keys) if a[k] != b[k]}; print(d or f'{len(keys)} values, all the same')" docs\hackathon\evidence\runs\2026-10-16-eval-coverage-intel-xeon-processor-2-80ghz-none.json my-coverage.json
```

Check: the second command prints `10 values, all the same` for those two scenes (each scene's
coverage and mean aerosol optical thickness, and the six scene-selection counts). The first prints
a table in which the cloudy scene of 5 January 2024 has a coverage of 0.1467 and the smoke scene of
26 December 2019 0.9949 with a mean aerosol optical thickness of 0.4358, as the report says.
Coverage does not depend on the CPU, so the numbers are equal, not close.

**3. The OpenCV kernels, timed on your machine.**

```bash
python scripts/vision_benchmark.py kernels --repeats 5 --out my-kernels.json
```

```powershell
python scripts\vision_benchmark.py kernels --repeats 5 --out my-kernels.json
```

Check: a table of each OpenCV operation the worker spends its time on, with `analyse_rail_access`
(the whole rail analysis on a synthetic city) among them, labelled with your CPU and OpenCV's build
fingerprint. The timings are yours; the committed runs say which CPU theirs came from, and
`compare` refuses a ratio across machines.

**4. The whole worker on a real city.** [R2: once the Sydney network fixture
(`scripts/vision_benchmark_networks/sydney.json`) and the Graviton session's `bench-scenes` run
files are committed: `python scripts/vision_benchmark.py scenes --only sydney-rail-2024-08-26
--cache --repeats 1 --out my-scenes.json`, its PowerShell line, and how to check its rail metrics
digest against the committed run's: equal on the same CPU family, within the stated tolerances
otherwise]

**5. The agent's trace, replayed without credentials.** [R2: once the evaluation's replay is
merged: `python scripts/vision_eval.py replay --sequence sydney-demo --agent recorded --from
docs/hackathon/evidence/runs/<the committed Bedrock replay>.json --worker in-process --cache`, its
PowerShell line, and what it asserts: the real model's recorded answers replayed, every OpenCV
measurement recomputed on your machine, each `look_again`'s numbers equal to the recorded ones
within the parity tolerances, and the decision ring's code-derived fields equal]

With AWS credentials and Bedrock access, the same replay runs the live model:
`--agent bedrock --model-id <id>`.

## Tier 2: deploy it on AWS

**Prerequisites.** An AWS account (two is better: dev and production); a fork of the repository
with GitHub Actions; Terraform, the AWS CLI, Python 3.11 and the GitHub CLI; Bedrock model access
in the home Region for the topics' model. The steps are the existing runsheets, by reference: they
are what the deployed system was built from, and copying them here would let them drift. Their
commands are bash; on Windows run them in Git Bash ([setup guide](../setup-guide.md)) or WSL2.

1. **The first deploy:** the README's [Quick start](../../README.md#quick-start) (the bootstrap,
   the setup script, a merge into `dev`), or the [setup guide](../setup-guide.md) from the start.
2. **Turn the vision worker on:** the runsheet's [Vision (optional)](../deployment-runsheet.md#vision-optional)
   (re-apply the bootstrap with `vision_region`, set the `VISION_ENABLED` repository variable,
   deploy).
3. **Create the rail topic and run one tick per city:** the runsheet's
   [`rail_access` topics](../deployment-runsheet.md#rail_access-topics), "Create the topic" and
   "The first-deploy check". A large city's first tick fetches its network and the next one
   measures it; each city's first clear scene is its first observation, a Finding, and then an
   article held for you on that city's day of the rota.
4. **Read what it did:** the runsheet's
   [evidence queries](../deployment-runsheet.md#evidence-what-the-pipeline-measured-and-decided)
   in CloudWatch Logs Insights, or ask the operator's assistant "what did the vision agent do for
   rail access this week?" (`vision_evidence`; [the evidence trail](../enhancements/vision-evidence.md#asking-the-assistant)).
5. **See the agent decide on a known day:** the runsheet's demo topic, "The demo topic's two-tick
   procedure" in the same section. Its thresholds of 0 send its second clear scene to the agent;
   reject its articles.
6. **Approve an article:** `python scripts/admin_cli.py inbox`, then `approve`. The page shows the
   map with its caption and credits.

## Tier 3: COOL on Graviton4

The runsheet's [COOL section](../deployment-runsheet.md#the-cool-backend-and-the-benchmark), steps 0
to 12, is the session's script: the reconnaissance and its go or no-go table, a `c8g.xlarge` in
us-west-2, a COOL venv on the product's own `cv2`, stock OpenCV 5.0.0.93 and 4.14.0.94 venvs beside
it, the interleaved `kernels`, `scenes`, `compare` and `controls` runs, and the checklist of what
the session must leave behind. Its run files are the only source of the report's COOL numbers
([Appendix A](technical-report.md#appendix-a-best-use-of-cool)).

## What the tests cover

`pytest lambdas/ scripts/` runs every handler's and module's tests, the vision pipeline's among
them, with no network: moto for S3 and DynamoDB, scripted fakes for the vision worker, the model,
Overpass, STAC and the news search, and synthetic scenes built in the tests. At 4a3d1b8 (16
October) the 20 vision test files collect 1,432 tests:

| Files | What they hold |
|---|---|
| `lambdas/tests/test_vision_core.py`, `test_vision_rail_core.py`, `test_vision_network.py`, `test_vision_cog_geo.py` | the OpenCV core on synthetic scenes: masks, the heat, reach and deserts, thinning and the graph, the figure, the COG reader and the projection, with speed guards that count the work |
| `lambdas/tests/test_vision_worker.py` | the worker's contract: every error code, the redirect and byte-range rules, the rail window cap, the build fingerprint guard |
| `lambdas/tests/test_rail_access_adapter.py`, `test_satellite_vision_adapter.py`, `test_vision_sites.py` | the adapters: the scene search, the network's object, time budgets, the first observation per city, the diff, the rota |
| `lambdas/tests/test_vision_triage.py`, `test_vision_decisions.py`, `test_evidence.py` | the agent's bounds and every fail-closed stop; the decision ring and the trace; the evidence lines, fed every hostile value |
| `lambdas/tests/test_osm.py`, `test_rail_notes.py`, `test_figures.py`, `test_frontend_figures.py` | Overpass, the planner notes, the figures path to the page |
| `lambdas/tests/test_ops_mcp_vision_status.py`, `test_ops_mcp_vision_evidence.py` | the assistant's vision tools |
| `lambdas/tests/test_vision_terraform.py` | the worker's packaging and IAM |
| `scripts/tests/test_vision_benchmark.py`, `test_vision_eval.py` | the benchmark, the run files, the manifest and coverage |

[R2: the count at the tag]
