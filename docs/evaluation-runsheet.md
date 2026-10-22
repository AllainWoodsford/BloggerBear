# Evaluation runsheet: the real-data runs and the evidence export

Two by-hand sessions for the OpenCV competition, after the vision worker is running on dev
([vision-runsheet.md](vision-runsheet.md)). Neither writes to AWS.

- **The evaluation's real-data runs** run `scripts/vision_eval.py` against real scenes, the deployed
  worker and the real model, and commit the run files the report cites.
- **The evidence export** copies what the deployed system measured, decided and did into git with
  `scripts/evidence_export.py`, and writes the trace the report and the video quote.

The third session, the Graviton4 timing of COOL against stock OpenCV 5, is the
[COOL benchmark runsheet](cool-benchmark-runsheet.md). The evidence these leave behind, and its
schema, is in [the evidence README](hackathon/evidence/README.md).

## Contents

- [The evaluation's real-data runs](#the-evaluations-real-data-runs)
  - [1. The venv and the names](#1-the-venv-and-the-names)
  - [2. The failure catalogue (2.5)](#2-the-failure-catalogue-25)
  - [3. E5, the access baseline](#3-e5-the-access-baseline)
  - [4. The replay with the deployed worker and the real model (2.6.4)](#4-the-replay-with-the-deployed-worker-and-the-real-model-264)
  - [5. Re-run each recording without AWS](#5-re-run-each-recording-without-aws)
  - [6. The agent's evaluation (2.6.1-2.6.3)](#6-the-agents-evaluation-261-263)
  - [7. The counterfactuals (2.6.6)](#7-the-counterfactuals-266)
  - [8. E3, stability over the year](#8-e3-stability-over-the-year)
  - [9. E8, the built-up check (2.4.1)](#9-e8-the-built-up-check-241)
  - [10. Index and commit](#10-index-and-commit)
- [The evidence export, the trace and the labels (O9)](#the-evidence-export-the-trace-and-the-labels-o9)
  - [What it needs](#what-it-needs)
  - [1. Export](#1-export)
  - [2. Read it before committing](#2-read-it-before-committing)
  - [3. The trace](#3-the-trace)
  - [4. Label (optional; not done for the submission)](#4-label-optional-not-done-for-the-submission)
  - [5. Commit](#5-commit)

## The evaluation's real-data runs

The evaluation's subcommands (`scripts/vision_eval.py`; options in
[scripts/README.md](../scripts/README.md#the-evaluation-proper-vision_evalpy-replay-failures-access-baseline-index))
run in CI on a synthetic city with scripted parts. These are the runs on real data: the
operator's, and their run files are the evidence the report cites
([the evidence README](hackathon/evidence/README.md#running-the-evaluation-for-real)). Run them
from a checkout of the code being evaluated, in a venv; they use the committed Sydney network
fixture (`scripts/vision_benchmark_networks/sydney.json`, #349) unless given
`--network <file>`. They read the pinned scenes from `sentinel-cogs` in us-west-2 through a cache
that the first run fills (about 54 MB a scene, eight scenes in all) and later runs read from; on the
Graviton instance (step 8's `$C` in the [COOL benchmark runsheet](cool-benchmark-runsheet.md)) or any machine in us-west-2 that is fast, elsewhere it is a few
minutes a scene through a slow link. Nothing here writes to AWS.

### 1. The venv and the names

```sh
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r lambdas/requirements-dev.txt -r scripts/requirements.txt
E=docs/hackathon/evidence/runs
C="--cache $HOME/cogs"
```

```powershell
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r lambdas\requirements-dev.txt -r scripts\requirements.txt
```

`lambdas/requirements-dev.txt` is what CI installs: the Lambda and worker requirements, plus
pytest and moto, which `failures` needs to check that pytest collects the catalogue's tests
(without them it refuses rather than call the tests missing). The PowerShell lines below run
`python`, the activated venv's own: `py -3.11` would run the global 3.11, without the venv's
packages. They spell `--cache $HOME\cogs` and `--out docs\hackathon\evidence\runs` out in full.

### 2. The failure catalogue (2.5)

Every test it names is checked to be collected, then the real
rows run through the worker handler in this process (the cloudy and no-data scenes, the smoke
scene, a site off the tile, a window past the rail cap, a missing key; six scenes read).

```sh
python scripts/vision_eval.py failures $C --out $E
```

```powershell
python scripts\vision_eval.py failures --cache $HOME\cogs --out docs\hackathon\evidence\runs
```

It exits 1 when a row did not do what the catalogue says, and the run file says which and how.

### 3. E5, the access baseline

On `sydney-2024-08-26`:

```sh
python scripts/vision_eval.py access-baseline $C --out $E
```

```powershell
python scripts\vision_eval.py access-baseline --cache $HOME\cogs --out docs\hackathon\evidence\runs
```

### 4. The replay with the deployed worker and the real model (2.6.4)

Credentials that may invoke
the worker (`lambda:InvokeFunction` on `<prefix>-dev-vision-worker` in us-west-2) and the
topics' model (`bedrock:InvokeModel` on the inference profile the topics use, in the home
Region); `M` is that profile's id (`python scripts/admin_cli.py model-config get` shows it).
An application inference profile is given by its ARN, which Converse needs; the run file and
the trace then name no model, because that ARN carries the account id (a system profile's ARN
is recorded as its id).
Every site id the worker is sent is `eval-sydney`, so these runs never pass for production's in
its logs; the run file names the worker's Region and code, never its ARN. About 10 worker calls
and a few cents of model calls a sequence.

```sh
W=$(aws lambda get-function --function-name <prefix>-dev-vision-worker --region us-west-2 --query Configuration.FunctionArn --output text)
M=<the topics' inference profile id>
for s in sydney-demo sydney-winter sydney-c1; do
  python scripts/vision_eval.py replay --sequence $s --worker "$W" --agent bedrock --model-id "$M" --region ap-southeast-2 --out $E --trace
done
```

```powershell
$W = aws lambda get-function --function-name <prefix>-dev-vision-worker --region us-west-2 --query Configuration.FunctionArn --output text
$M = "<the topics' inference profile id>"
foreach ($s in "sydney-demo", "sydney-winter", "sydney-c1") {
  python scripts\vision_eval.py replay --sequence $s --worker $W --agent bedrock --model-id $M --region ap-southeast-2 --out docs\hackathon\evidence\runs --trace
}
```

Each writes its run file and figures to `$E` and its trace to
`docs/hackathon/evidence/replay/<sequence>.md`. **sydney-demo is the headline A2 trace**: with
the demo topic's thresholds of 0, every scene after the first goes to the agent, on the same
network fixture throughout, so whatever moved came from the imagery; its trace says the
thresholds are 0. sydney-c1's last tick adds a constructed five-station line and a constructed
news item, and its trace says so.

### 5. Re-run each recording without AWS

Run them the way a judge will (2.6.4's assertions: every
measurement recomputed in this process, held to the recording within 2.7.1's tolerances):

```sh
for s in sydney-demo sydney-winter sydney-c1; do
  python scripts/vision_eval.py replay --sequence $s --agent recorded --from "$(ls $E/*-eval-replay-*-$s-bedrock.json | tail -n 1)" $C --out $E
done
```

```powershell
foreach ($s in "sydney-demo", "sydney-winter", "sydney-c1") {
  $from = (Get-ChildItem "docs\hackathon\evidence\runs\*-eval-replay-*-$s-bedrock.json" | Select-Object -Last 1).FullName
  python scripts\vision_eval.py replay --sequence $s --agent recorded --from $from --cache $HOME\cogs --out docs\hackathon\evidence\runs
}
```

Each exits 0 when it reproduced its recording; a failed check names the tick and the field. A
recording refused before it runs ("input changed", or a recorded response that calls a tool the
profile no longer offers) names what moved.

### 6. The agent's evaluation (2.6.1-2.6.3)

With the same `W` and `M`: the thirteen cases in
`scripts/vision_eval_cases/` (the [evidence README](hackathon/evidence/README.md#the-agents-cases)
lists them), each case's history through the real adapter, then its newest tick asked of the
real model five times from the same state, against the deployed worker. C7 and C9 run their
scripts; C8 is asked with an invalid model id on purpose; C3 and C10 count only if their scene
passes the gate. About 55 live runs of up to five turns: a few US dollars of model calls (the
run file totals them in USD and AUD, by the built-in price table) and cents of worker time.

```sh
python scripts/vision_eval.py agent --worker "$W" --agent bedrock --model-id "$M" --region ap-southeast-2 --repeats 5 --out $E
```

```powershell
python scripts\vision_eval.py agent --worker $W --agent bedrock --model-id $M --region ap-southeast-2 --repeats 5 --out docs\hackathon\evidence\runs
```

It exits 1 when an unconditional case never reached the agent, a failure case did not fail
closed as it must, or the worker did not measure a case's newest scene (the run keeps its code
and scores that repeat nowhere); `--case C1` (repeatable) re-runs one. Without Bedrock access
every case ends `bedrock_error`, counted, never dropped: check the summary's
`failed_closed_task_runs` before committing a run that only measured that. To run the worker
in this process instead, add `$C` and pass `--worker in-process`; without a cache it is refused
off an EC2 instance in us-west-2.

### 7. The counterfactuals (2.6.6)

C1 and C10 (C4 in C10's place if C10 does not pass the gate),
five repeats per arm with only `look_again`'s numbers differing, and the same scene clouded to
coverage 0.62 and 0.58 in this process (arm (b), which needs the cache). About 22 model runs.

```sh
python scripts/vision_eval.py counterfactual --worker "$W" $C --agent bedrock --model-id "$M" --region ap-southeast-2 --repeats 5 --out $E
```

```powershell
python scripts\vision_eval.py counterfactual --worker $W --cache $HOME\cogs --agent bedrock --model-id $M --region ap-southeast-2 --repeats 5 --out docs\hackathon\evidence\runs
```

Its checks hold that both sides were measured at their coverage and that no model was asked
below the floor. Arm (a)'s `baseline` arm is a constructed tool output, and the report says so.

### 8. E3, stability over the year

The manifest's `year-2024` set (17 scenes) through the
production rule, triage off, on one network, in this process so the built-up masks can be
compared: about 1 GB read once into the cache, so run it on the Graviton instance or another
machine in us-west-2. No model, no worker invocation.

```sh
python scripts/vision_eval.py stability $C --out $E
```

```powershell
python scripts\vision_eval.py stability --cache $HOME\cogs --out docs\hackathon\evidence\runs
```

Its summary's `false_material` of `decisions`, with `wilson_low` and `wilson_high`, is the
false-alarm rate a quiet city would call the agent at; `results.false_alarms.by_month` breaks it
down. If a summer scene against winter ones crossed a threshold here, C3 reaches the agent in
step 6; otherwise its gate held, which the report says.

### 9. E8, the built-up check (2.4.1)

The worker's built-up mask on `sydney-2024-08-26` against
ESA WorldCover 2021's class 50, and the served share over each mask with the fixture's stations.
It reads the scene from the cache and the WorldCover tile `S36E150` from the `esa-worldcover`
bucket (eu-central-1, no credentials) over the site's box only, about 10 MB of a 20 MB object,
kept in the cache too; a HEAD first checks the object's pinned ETag and size. No model, no worker
invocation.

```sh
python scripts/vision_eval.py builtup-check $C --out $E
```

```powershell
python scripts\vision_eval.py builtup-check --cache $HOME\cogs --out docs\hackathon\evidence\runs
```

Its summary's `iou`, `precision` and `recall` compare the masks over the pixels both saw;
`results.ours_by_reference_class` says what WorldCover calls the pixels we call built-up. "input
changed: reference tile S36E150" means the object is not the one pinned on 17 October
(`REFERENCE_PINS` in `scripts/vision_eval_reference.py`): check it with `curl -sI <the URL the
message names>`, and pin the new ETag and size in a commit of its own that says why, before
the run.

### 10. Index and commit

```sh
python scripts/vision_eval.py index && python scripts/vision_eval.py index --check
python scripts/pii_denylist_check.py --staged    # after `git add`
```

```powershell
python scripts\vision_eval.py index; python scripts\vision_eval.py index --check
python scripts\pii_denylist_check.py --staged    # after `git add`
```

Run `index --check` here before pushing: CI runs it too, on the pull request (python-ci lists
`docs/hackathon/**`), but a failure there costs a round trip. Run the PII check (your `.pii-denylist`) on what is staged, and read each trace before
committing it: besides scene and station ids, a trace quotes the agent's note under each
decision it was asked about (model text, through `redact.scrub`, of up to 300 characters),
which can name a suburb or a station.

Commit the run files and figures, `docs/hackathon/evidence/replay/`, `index.json` and
`RESULTS.md`, and update the tracker's A2, A3 and S7 rows. A run file made before the submission tag
on code that changed since in a way that matters to it goes in
`docs/hackathon/evidence/older-code.json` with the reason, and `index` again.

## The evidence export, the trace and the labels (O9)

What the deployed system measured, decided and did goes into git with
`scripts/evidence_export.py` ([the live evidence](hackathon/evidence/README.md#the-live-evidence-live)
says what each file holds). Run it on Saturday 24 October, and again by Thursday 29, from a checkout
of the code being submitted, in the venv of [step 1](#1-the-venv-and-the-names) above. It only reads: nothing here writes to AWS.

### What it needs

- Credentials for the dev account (the profile you deploy with will do) that may:
  - `s3:GetObject` on `<prefix>-dev-content` (`snapshots/<topic>/running-state.json`, each city's
    `vision/<topic>/<site>/network.json`, the figures);
  - `dynamodb:Scan` on `<prefix>-dev-topics`, `dynamodb:Query` on `<prefix>-dev-findings` and on
    `<prefix>-dev-articles` (its `by_topic_created_at` index);
  - `logs:StartQuery`, `logs:GetQueryResults` and `logs:StopQuery` on
    `/aws/lambda/<prefix>-dev-research-tick` and `/aws/lambda/<prefix>-dev-admin-api` in the home
    Region, and on `/aws/lambda/<prefix>-dev-vision-worker` in us-west-2;
  - `lambda:GetFunctionConfiguration` on those three functions.
- The Regions: the home one is `ap-southeast-2` and the vision one `us-west-2`, Terraform's defaults
  (`--region` and `--vision-region` otherwise); the prefix is `bloggerbear` (`--prefix` otherwise).
- `gh`, signed in (`gh auth status`), for the commit that deployed each function's code
  (`provenance.json`); without it that commit is null, with the reason, and the export still
  finishes.
- Your `.pii-denylist` at the repository's root (or `PII_DENYLIST`): the export runs the check on
  every file it writes, and refuses to start writing without a list, since nothing would then check
  the files before they are committed. `--no-denylist` writes unchecked, and says so.

The logs are read through Logs Insights, a few queries at a time, each batch waited for at most
`--logs-wait` seconds (120); a 90-day scan of the three groups is a few GB at most, cents.

### 1. Export

```sh
python scripts/evidence_export.py export --env dev --networks --figures 12
```

```powershell
python scripts\evidence_export.py export --env dev --networks --figures 12
```

It writes `docs/hackathon/evidence/live/<UTC date>/`, and with `--networks` each city's network
as `scripts/vision_benchmark_networks/<site>.json` (fill each new file's row in that directory's
`NOTICE.md` from its `fetched_at`, `endpoint` and `query_url` and its sha256, which
`provenance.json` gives under `networks`). A committed fixture is left byte for byte when it
holds the same network (its `kept` says `unchanged`), and kept, with a line saying so, when it
holds another: the evaluation's real-data runs pin its sha256, and `replay --agent recorded`
refuses a file that changed, so do not pass `--replace-networks` once those runs are made. It
prints any log read that did not complete: "not_checked" means a query did not finish within
`--logs-wait`; run it again with `--logs-wait 300 --replace` (`--replace` removes only the
export's own files: your `labels.csv` stays). Exit 2 with `pii-denylist: <file>:<line> contains
denylist entry #n` means a personal string reached a file: the export is moved to
`live/<date>.refused/` (git ignores it), your `labels.csv` is left where it was, and every
network file it wrote is put back as it was; find where the string came from, then export
again. Leave out `--with-notes` for the committed export unless you have read every note it
adds (model and third-party text, scrubbed and labelled, but quoted).

### 2. Read it before committing

`summary.json`'s `logs` (each `complete`), `redacted` (0
expected; anything else, `redacted_at` names each file, line and field: find out why),
`observed_failures.worker.not_checked` (worker runs whose own line was not read: 0 when every
log read completed), `human_control.completeness.unmatched` (a line nothing corroborates: the
report says so), and the traces under `traces/`.

### 3. The trace

The one the report and the video quote (the latest decision of a city, or of the demo
topic; `--scene <scene id>` for one in particular):

```sh
python scripts/evidence_export.py trace --topic rail-cities --site sydney --scene latest --from docs/hackathon/evidence/live/<date>
python scripts/evidence_export.py trace --env dev --topic rail-sydney-demo --scene latest
```

```powershell
python scripts\evidence_export.py trace --topic rail-cities --site sydney --scene latest --from docs\hackathon\evidence\live\<date>
python scripts\evidence_export.py trace --env dev --topic rail-sydney-demo --scene latest
```

The second reads live (the logs and the running state, now) and writes nothing; in the video,
run query (c) on the same tick id the committed trace shows.

### 4. Label (optional; not done for the submission)

The human control is the article inbox,
which the export already measures; labelling is a one-off audit of the agent's live calls. To
do it, copy `labels-template.csv` to `labels.csv` in the same directory and fill each
row's `label` (`agree` or `disagree` with the agent's verdict) and `reason` (one of the codes in
the file's header), reading the trace and the figure the row names. Save it as UTF-8 CSV. Then:

```sh
python scripts/evidence_export.py agreement --from docs/hackathon/evidence/live/<date>
python scripts/vision_eval.py index --check
```

```powershell
python scripts\evidence_export.py agreement --from docs\hackathon\evidence\live\<date>
python scripts\vision_eval.py index --check
```

`agreement` writes the k of n and its Wilson interval into that export's `summary.json`, the
cities and the demo topic apart.

### 5. Commit

Commit the export directory, `docs/hackathon/evidence/live/README.md` and the network files,
after `python scripts/pii_denylist_check.py --staged` (PowerShell: `python
scripts\pii_denylist_check.py --staged`). Update the tracker's O9, S7 and A3 rows.
