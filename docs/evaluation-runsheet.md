# Evaluation runsheet: continuous evals, the real-data runs and the evidence export

Two kinds of evaluation are run from here.

**Continuous evals** score the pipeline's own models and prompts while it runs, once
`EVALS_ENABLED` is on ([Continuous evals](#continuous-evals)). They run by themselves. Your part is
calibrating the judge, offline scoring runs, reading `evals status` and the digest, and taking a
source's events back out when they are wrong.

**Two by-hand sessions for the OpenCV competition**, after the vision worker is running on dev
([vision-runsheet.md](vision-runsheet.md)). Neither writes to AWS.

- **The evaluation's real-data runs** run `scripts/vision_eval.py` against real scenes, the deployed
  worker and the real model, and commit the run files the report cites.
- **The evidence export** copies what the deployed system measured, decided and did into git with
  `scripts/evidence_export.py`, and writes the trace the report and the video quote.

The third session, the Graviton4 timing of COOL against stock OpenCV 5, is the
[COOL benchmark runsheet](cool-benchmark-runsheet.md). The evidence these leave behind, and its
schema, is in [the evidence README](hackathon/evidence/README.md).

The labels in the competition sessions' headings and steps are cross-references:

- E3, E5 and E8 are experiments in the
  [technical report's evaluation](hackathon/technical-report.md#6-evaluation).
- A2, A3, S7 and O9 are rows of the [rubric tracker](hackathon/rubric-tracker.md), which the
  commit steps say to update.
- Numbers such as 2.5, 2.6.4 or 2.7.1, in brackets or not, are sections of the evaluation's
  specification. The scripts' docstrings cite it by the same numbers; it is not in the repository.

## Contents

- [Continuous evals](#continuous-evals)
  - [1. How a score is made, end to end](#1-how-a-score-is-made-end-to-end)
  - [2. Reading `evals status`](#2-reading-evals-status)
  - [3. The digest, and its Friday email](#3-the-digest-and-its-friday-email)
  - [4. The judge, and calibrating it](#4-the-judge-and-calibrating-it)
  - [5. Offline scoring runs](#5-offline-scoring-runs)
  - [6. Call traces](#6-call-traces)
  - [7. Taking a source's events back out](#7-taking-a-sources-events-back-out)
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

## Continuous evals

With `EVALS_ENABLED` on, the pipeline writes a scored event whenever something says a model or a
prompt did well or badly, eval-jobs adds the events up every hour, and the Model Ranch acts on the
totals: it gates models, ranks the models a Re-Write may use and proposes culls
([models.md](models.md#the-model-ranch-scores-gates-and-culling)). Turning it on is step 3 of the
production runsheet's [enable order](production-runsheet.md#the-enable-order), and every setting is
in [configuration.md](configuration.md#the-evals-settings). Every command below is
`python scripts/admin_cli.py ...` against the environment `BLOGGERBEAR_ADMIN_API_URL` points at
([scripts/README.md](../scripts/README.md#continuous-evals-and-the-model-ranch-evals)).

### 1. How a score is made, end to end

1. **An event.** The code that knows something went well or badly writes one JSON line to the
   `<prefix>-<env>-evals` log group, which keeps 120 days. An event holds ids, codes and numbers
   only, never text, and has an `event_id`, so a write that is retried counts once.
2. **The rollup.** eval-jobs adds the log up every hour, at five past, into day rows on the
   `<prefix>-<env>-evals` table: one per model, one per prompt version, and one per model and prompt
   version together (a *cell*).
3. **The models row.** It then writes each model's windows on its registry row
   ([models.md, the windows](models.md#the-windows)), and records the purpose of any model that has
   none yet.
4. **What reads them.** The rotation guard, the rewrite picker, the weekly cull proposal,
   `evals status`, `evals digest` and the operator's assistant (`model_ranch`, `prompt_metrics`).

| Reason | Score | Written by | What it scores | Gates |
|---|---|---|---|---|
| Article Completion | +2 | the daily cycle, when it stores an article (held or published) | the article's model, and each prompt in its lineage | yes |
| Admin Review | +2 approved, −2 rejected | a person's approve or reject in the inbox (never Professor Grizzlewick's) | the model, and each prompt | yes |
| Article Rewrite | −2 | a person's `articles rewrite` or `[w]` (never his) | the model whose text it was | yes |
| User Feedback | ±1, ±3 with a comment the screening kept | the public API, once a reader's vote is stored | the model, and the cells of the article's prompts | never |
| Research Review, Article Review | ±2 | the judge ([4](#4-the-judge-and-calibrating-it)) | the model and the prompt that wrote the Finding or the article | only while the judge is calibrated |
| Human Review | ±2 | a person's verdict on a Finding (`approve --source findings`) | the model and the prompt that wrote the Finding | yes |
| Offline Review | ±2 | an `evals score --record` run ([5](#5-offline-scoring-runs)) | the model and prompt version scored | only while the judge is calibrated |
| Admin Forgive, Admin Punish | ±N | `evals forgive`, `evals punish`, `models restore` | a model's total only; a prompt version's is recorded only | the total only |
| Prompt Fallback | 0 | a Lambda that served the code's copy of a prompt in place of its asset | the prompt version, counted | never |

- **An article's prompts** are every prompt its lineage names: its own calls (ideation, draft,
  title, the reviews, a revision, a finished Re-Write and its reviews) and the research calls of the
  Findings it was written from. Two kinds of call are in a lineage for their cost alone and score
  nothing: every call of a Re-Write that failed, whose text was thrown away, and the research of a
  Finding that was in the article's window but that its editorial goal set aside. A daily-cycle
  revision the guards turned down still counts: it is part of the run that wrote the text. A lineage
  stored before these marks existed carries none, so all its calls count. Each prompt gets one event
  per article, at the version its most recent call used.
- **An article's model** is the one whose text it is now: its latest finished Re-Write's, else its
  draft's. Every event of the article names that model, and the model counts the article once, not
  once per prompt.
- **A reader's vote** names no prompt. The rollup joins it to the article's Article Completion
  events, so it reaches the cells of the article's prompts and never a prompt's own totals.
- **A Prompt Fallback** is written beside each `prompt_fallback` log line. It names the prompt
  version that fell back and no model, and is counted on that version's day row only. Its `source`
  is the code of the Lambda that fell back.

**When an event counts.** The rollup reads the log from the start of its watermark day
(`last_rolled_day`, the newest final day) to now, and adds every day after the watermark up again
from all its events. A day becomes final two hours after the midnight that ends it, in the evals
`timezone`. An event written after its day became final is counted in `late_dropped`, which `evals
status` shows, and in no day row. To run a rollup now:

```bash
python scripts/admin_cli.py evals rollup                                     # from the watermark, in the background
python scripts/admin_cli.py evals rollup --from 2026-10-20 --to 2026-10-27   # add those days up again (118 days back at most)
```

A rollup run twice, or two at once, write the same rows.

**While evals are off** nothing is written, eval-jobs and its queue are not deployed, and the evals
table and log group keep what they hold. `evals rollup`, `forgive`, `punish`, `cull --apply`,
`score` and `models restore` are refused (409); `evals get`, `set`, `status` and `digest` still
answer.

### 2. Reading `evals status`

```bash
python scripts/admin_cli.py evals status
```

One answer in parts, each read on its own. A part that cannot be read says "could not check" and
the others still answer, so one missing grant never hides the rest. Within two hours of turning
evals on, no part should say it.

| Part | What it says | When to act |
|---|---|---|
| `switches` | `evals_enabled` in this deployment, and the `judge_enabled`, `cull_enabled` and `trace_calls` settings | `judge_enabled` false when you did not turn it off: the tripwire did ([4](#4-the-judge-and-calibrating-it)) |
| `prompt_release` | the prompts release in place, and the pinned one during a rollback; `off` with no prompts bucket | |
| `prompt_fallbacks` | how many prompts fell back to the code's copy in the last 24 hours, in how many hours, and the latest; `off` while this deployment does not read assets | any fallback: `prompts list`, then the Lambda's `prompt_fallback` lines say why. While the kill switch is on (`prompts set --assets off`) every call falls back, so this counts them all and the `<prefix>-<env>-prompt-fallback` alarm stays in ALARM, as expected, until it is cleared |
| `rollup` | the watermark (`last_rolled_day`), when the last run finished, and what it did | a last run more than an hour or two old: `evals rollup`, then eval-jobs' log |
| `late_dropped` | events that reached the log after their day was final, and so are counted nowhere (a rollup of those days leaves them out too) | it should stay near 0; one that keeps growing means events reach the log hours after the time they carry |
| `judge` | the judge's switch and model, its agreement with people over how many pairs, whether that makes it calibrated, the thresholds, `last_judge_at`, and `judge_paused_reason` while the tripwire holds it off | not calibrated: [calibrate it](#4-the-judge-and-calibrating-it) |
| `gated_models` | the models below a gate, in three lists: `gated` (held back), `protected` (the default, fallback or judge, served all the same) and `held_open` (an `ungated_until` holds the gate open), with the gates in force | a model gated by mistake: `evals ungate --model <model id> --hours 48` |
| `cull` | the cull proposal in place and whether it has expired, and the last `evals cull --apply` | a proposal you agree with: [culling](models.md#culling-a-proposal-then-an-apply) |
| `queue_depth`, `queue_age` | for eval-work (the judge's and offline runs' jobs) and `work.fifo` (the topics' research jobs): waiting, in flight and delayed, and the oldest job's age | eval-work older than two hours: its alarm emails too |
| `topics_by_dispatch_mode` | how many topics run from their schedules and how many from the queue | |

### 3. The digest, and its Friday email

```bash
python scripts/admin_cli.py evals digest                     # the last 7 days, today included
python scripts/admin_cli.py evals digest --since 2026-10-01  # from a day (or an ISO time), 120 days back at most
```

For each model and each prompt version with events in those days: `perf`, `events`, `good` and
`bad` (events above and below zero, stored articles and fallbacks left out) and the 95 % Wilson
interval of good / (good + bad). Each model also shows the windows that are below their gate now,
and where it stands: `gated` while the rotation guard holds it back, else why it is served all the
same (kept as the default, the fallback or the judge, or held open by an override). Beside them, the
judge's agreement and the rollup's watermark.

With `evals set --digest-email on` (off by default), eval-jobs emails the same digest to the alerts
topic, the one the alarms email through, once a Friday: from 08:00 in the evals `timezone`, after
the first scheduled hourly rollup that runs then. The email says "gated" only for a model the
rotation guard holds back, and "below gate", with the reason it is served, for the rest. The day is
claimed by `last_digest_on` on the evals table's index row, so a retry or the next hour's run sends
nothing more; a send that fails gives the claim back, and the next hour tries again. It holds only
ids, codes and numbers, never a topic's text or a reader's comment.

### 4. The judge, and calibrating it

**What the judge does.** A model reads another model's work, a research Finding's summary (with an
excerpt of the source data it was written from) or an article, and says good or bad. It runs in
eval-jobs:

- **The selector**, every hour, runs only while `judge_enabled` is on and once `judge_interval_hours`
  (4) have passed since its last run (`last_judge_at`). It picks up to `judge_max_items` (10) items
  no judge has reviewed: Findings of the last two days, and articles of the last seven, held,
  published or rejected alike (a person's reject does not take an article out of its reach), taken
  in turn, an article then a Finding. The Findings go one topic after another, and each run starts
  with the topic after the one its last Finding came from (`last_judge_topic` on the evals table's
  index row), so no topic is passed over. It sends one job per item to eval-work, never more than
  the day's spend cap has left.
- **Each job** gives the item to the judge's model with its rubric (`eval_judge/research_review` or
  `eval_judge/article_review`, which live only in the code) and the topic's operator note, if it has
  one. The reply must be exactly `{"verdict": "good" or "bad", "reason_code": ...}`; anything else
  is ignored, with no verdict and no event (its cost still counts). A verdict writes a Research
  Review or Article Review event (+2 or −2) for the model and prompt that wrote the item, marks the
  Finding or the article as judged, and writes the judge's half of a pair (below).

**The judge's model** is the first of `judge_model_id`, model-config's fallback model and
`BEDROCK_MODEL_ID` that is an enabled text model and did not write the item: the judge never judges
its own work. Models are compared by their base model, so the model that wrote the item reached
through another inference profile, or named by its ARN, counts as its author too. An item every
option wrote is skipped, with a `judge_skipped` line; so is a Finding from before research recorded
its model, as there is nothing for a verdict to score.

**Calibration.** The judge's verdicts count towards the gates only while it agrees with people
often enough: at least `judge_min_agreement` (0.7) over at least `judge_min_pairs` (20) subjects
both a person and the judge decided, on the same text. The rollup works the agreement out each hour from the evals
table's `pair#<subject>` rows. Until it is calibrated, every verdict is still written and kept, and
counts in the rewrite picker's good and bad, but not in any window a gate reads. A person's verdict
comes from two places:

- **Research Findings you mark yourself**, the quickest way to calibrate it:

  ```bash
  python scripts/admin_cli.py approve --source findings          # y good, r bad, z skip, v read it all, q quit
  python scripts/admin_cli.py approve --source findings --mock   # practise the keys: no AWS
  ```

  The Findings the judge has already reviewed come first (only those make a pair), then the rest,
  oldest first; each card says when the judge has reviewed it, never what it said. Each verdict is a
  Human Review event for the model that wrote the summary and the person's half of the pair. Marking
  a Finding twice writes nothing more, and the other verdict the second time is refused. The queue
  is read a page at a time (`GET /evals/findings?count=&offset=`, whose `more` says whether anything
  is left after the page), so the Findings you skipped, which only your machine remembers, cannot
  hide the ones behind them. `--source findings` is never part of the default `approve` run.
- **Articles you approve or reject in the inbox.** Each decision is also your verdict on the
  article (good for approve, bad for reject), and the latest one is kept. Both halves of an
  article's pair record which text they were given on (`rewrite_number`: how many Re-Writes were
  saved before it), and the rollup pairs only verdicts on the same text. A decision made after a
  Re-Write was saved on top of the text the judge marked is not kept as your half at all (an
  earlier decision's row stays). A decision, then a Re-Write, then the judge's verdict on the new
  text leaves your half unpaired until you decide the text the judge saw. A row from before the
  number was kept counts as the first text.

`evals status` (its `judge` part) shows the agreement, the pairs and whether the judge is
calibrated, and the assistant's `judge_uncalibrated` card suggests `approve --source findings`.

**The tripwire.** A run that ends having marked more than 80% of at least 10 items bad turns the
judge off (`judge_enabled` false, with `judge_paused_reason` saying why) and prints a
`judge_tripwire` line: a judge that finds nearly everything bad is more likely broken than right.
Look at what it marked (`evals runs list`, `evals runs get <run id>`), then:

```bash
python scripts/admin_cli.py evals revoke --source judge --since 2026-10-27T00:00:00Z   # leave its verdicts out
python scripts/admin_cli.py evals set --judge on                                      # turn it back on
```

Setting `--judge` (on or off) clears `judge_paused_reason`.

**What it spends.** Each call is added to the day's spend on the evals table (its `spend` rows, the
day in the evals `timezone`), to the run, and to the Stats page's `evals` category.
`evals_daily_spend_cap_aud` (A$2) bounds the judge and offline runs together: the selector sends no
more items than the cap has left, and each job holds what its calls can cost on the day's `spend`
row before making them (`held_aud`, counted in `spent_aud` too, in one conditional write) and gives
it back once their cost is counted. So two jobs run at once (eval-work runs two) can never both take
the last of the cap. When the hold does not fit, the run is marked `paused: spend cap` and its items
wait on the queue, 15 minutes at a time, until a new day's cap allows. A job that dies holding, or a
hold that cannot be given back, stays counted until the day ends: the day spends less, never more
than its cap. A model with no price is counted at A$0.05 a call, never as free.

### 5. Offline scoring runs

An offline run asks how other models, or other versions of a prompt, would have written past
articles, and has the judge compare each draft with what the live version writes. It touches no
prompt asset and serves nothing: it only drafts, judges and records on the evals table. One prompt
can be scored today, `daily_cycle/draft`, the one that writes an article.

```bash
python scripts/admin_cli.py evals score --judge <judge model id> \
    --models <model id>,<model id> --prompt daily_cycle/draft=v2,vanilla --sample 20
```

- `--models`: 1 to 5 models, each registered, enabled and a text model, none of them the judge
  (compared by base model, as [the judge's own](#4-the-judge-and-calibrating-it) are).
- `--prompt`: the prompt and 1 to 5 versions. A version is any this deployment published, live or
  retired since, so a version a later one replaced can still be scored, or `vanilla`, the copy in
  the code.
- Each model is scored with each version, at most 10 combinations.
- `--sample`: 1 to 50 past articles of the last 14 days (10 by default), the newest that can be
  rebuilt (their Findings still there, the model that drafted them recorded and not the judge),
  taken one topic at a time so a busy topic does not fill the sample.

**For each article and combination**, the article's draft prompt is rebuilt exactly as the daily
cycle built it. The live version drafts once per article, on the model that drafted it; the
candidate drafts on its model; and the judge, which is neither, says which of the two drafts is
better, shown in an order drawn per article. The candidate's is good (+2), the live one's bad (−2).

**The estimate and the cap.** First the CLI asks for the plan without starting anything, and prints
the environment, the judge, the combinations, the items, the messages and the estimate in Australian
dollars beside what today's cap leaves. The estimate is an estimate, not a bound: it counts the live
version's draft once per combination, though it is made once per article, and it leaves out the one
retry, with a larger limit, of a draft cut off at its limit (that draft then costs about two and a
half times its estimate). The hold each job takes on the day's cap before its calls does price that
retry. The run is refused when the estimate is more than today's cap leaves; over
`score_max_cost_aud` (A$5) it needs `--yes`; otherwise the CLI asks before it starts.

**A message no day can pay for.** The plan, its dry run included, is refused (409) when one of the
run's messages can cost more than the whole daily cap: its candidate's draft and, until its
article's live draft is kept, the live one's, each with its retry, and the judge's verdict. The
answer gives that most as `message_ceiling_aud`. It turns on the models, not on how many messages
there are, so raise the cap (`evals set --daily-spend-cap <AUD>`) or score cheaper models.

**Following it.** The CLI then follows the run, printing a line whenever its progress changes, for
up to 30 minutes (`--no-wait` returns at once). eval-work runs two jobs at a time, so a few items
take minutes and 50 items with several combinations can take an hour.

```bash
python scripts/admin_cli.py evals runs list                 # the newest runs, judge and scoring, with their status
python scripts/admin_cli.py evals runs get <run id> --wait  # follow one again; without --wait, its progress and results now
python scripts/admin_cli.py evals runs cancel <run id>      # its jobs still on the queue are dropped
python scripts/admin_cli.py evals runs resume <run id>      # send again whatever has no result
```

`evals runs get` reads the run's progress from its item rows and gives each combination's results.
A run stopped by the spend cap shows `paused: spend cap` and goes on by itself once a new day's cap
allows; to go on today, raise the cap (`evals set --daily-spend-cap <AUD>`). `resume` sends again
every item of a scoring run that has no result and is not being scored: a paused or cancelled run's,
one whose send failed, one whose model call failed (a failed call is recorded as the item's
`error`, so the run still closes), one whose job failed before it claimed its item, and one that met
a cap lowered since below what it can cost (those two are counted finished with no row, so the run
still closes too). It runs under the same cap checks as a new run, and is refused (409) when every
message has its result or is being scored, when what is left costs more than today's cap leaves,
and when one of the messages it would send can cost more than the whole cap (raise the cap first).

A scoring job eval-jobs cannot place in its run (the run's header could not be read), or cannot
count finished after a failure, is not deleted: eval-jobs names it among the answer's
`batchItemFailures`, eval-work delivers it again, and the copy finishes it. After three receives it
goes to the pipeline's dead-letter queue, which `failed-executions list` shows as a `score_job`.

**`--record`** (it needs `--yes`) also writes each verdict as an Offline Review event for the
candidate model and prompt version, which counts towards the gates only while the judge is
calibrated. Without it, a run is item rows on the evals table and nothing else. To take a recorded
run's events back out: `evals revoke --source run:<run id>`.

### 6. Call traces

With `trace_calls` on (off by default, and on by default in dev: `evals set --trace-calls on|off`),
every model call that is recorded in a lineage is also stored whole, for reading back what a prompt
version was given and what it wrote:

```bash
python scripts/admin_cli.py evals trace <article id>
```

- **What one holds:** the prompt id and version, the model, the prompt as rendered, the reply and
  the tokens, at `traces/<subject>/<stage>.json` in the content bucket: an article's id for the
  daily cycle's calls and a Re-Write's, `<topic_id>#<captured_at>` for the research call behind a
  Finding. `evals trace` shows an article's and those of the research it was written from. A
  Re-Write's reviews are its own stages (`rewrite_adversarial_review`, `rewrite_compliance_review`),
  so they never overwrite the reviews of the first text.
- **Which run stored it:** each trace carries the id of the run that stored it, a Re-Write's
  `rewrite_id`. A later Re-Write overwrites only the stages it reaches, so `evals trace` marks
  `superseded` every Re-Write stage whose run is not that of the `rewrite` stage beside it, rather
  than show an earlier attempt's review as the later one's.
- **What is hashed:** only the gear's guidance (written from reader comments) and the topic's
  operator note are stored as their sha256 in place of their text, wherever they appear in the
  prompt or the reply. Everything else a prompt is given is the pipeline's own material and is
  stored as sent.
- **Where they live:** in the private content bucket, which has no public path, for 14 days; its
  lifecycle drops `traces/` after that in both environments. Only the admin API reads them.
- **What is never traced:** comment screening, and the judge's own calls (they are recorded in no
  lineage).

### 7. Taking a source's events back out

When a source's events are wrong (a runaway judge, a bad offline run, a Lambda that misbehaved), the
rollup can leave them out:

```bash
python scripts/admin_cli.py evals revoke --source judge --since 2026-10-27T00:00:00Z
python scripts/admin_cli.py evals revoke --source run:<run id>
python scripts/admin_cli.py evals revoke --source daily_cycle
```

`--source` is who decided (`human`, `judge` or `system`), an offline run (`run:<run id>`), or the
code of the Lambda that wrote the events. `--since` leaves out only the events from that time on;
without it, all of them. Each revoke adds one entry to `excluded_sources` (at most 20). The days
already final would keep those events, so the revoke starts a rollup of the days from the one
`--since` falls on (without it, as far back as a rollup reaches) to today, or, when it cannot, its
answer gives the `evals rollup --from --to` command that does it. `evals get` shows the list;
`evals set --excluded-sources` replaces the whole list, and `''` clears it. The events stay in the
log, so a source let back in is counted again by a rollup of those days.

A wrong score on one model is not a reason to revoke a source: `evals forgive` or `evals punish`
moves its total, and `evals ungate` holds its gates open
([models.md](models.md#holding-a-gate-open-ungated_until)).

## The evaluation's real-data runs

The evaluation's subcommands (`scripts/vision_eval.py`; options in
[scripts/README.md](../scripts/README.md#the-evaluation-proper-vision_evalpy-replay-failures-access-baseline-index))
run in CI on a synthetic city with scripted parts. These are the runs on real data: the
operator's, and their run files are the evidence the report cites
([the evidence README](hackathon/evidence/README.md#running-the-evaluation-for-real)).

Run them from a checkout of the code being evaluated, in a venv. They use the committed Sydney
network fixture (`scripts/vision_benchmark_networks/sydney.json`, #349) unless given
`--network <file>`. Nothing here writes to AWS.

They read the pinned scenes from `sentinel-cogs` in us-west-2 through a cache. The first run fills
it, about 54 MB a scene, and later runs read from it. The runs of steps 2, 3, 5 and 7 read eleven
scenes, and step 8 adds the rest of the 17-scene year set. Filling the cache is fast on the
Graviton instance of the [COOL benchmark runsheet](cool-benchmark-runsheet.md) (that runsheet's
step 8 sets the same `$C` as step 1 below) or on any machine in us-west-2. Elsewhere it is a few
minutes a scene through a slow link.

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
scene, a site off the tile, a window past the rail cap, a missing key; five scenes read).

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

You need credentials that may invoke two things:

- the worker: `lambda:InvokeFunction` on `<prefix>-dev-vision-worker` in us-west-2 (`<prefix>`
  is `bloggerbear`, Terraform's default `unique_name_prefix`, unless your deployment set another);
- the topics' model: `bedrock:InvokeModel` on the inference profile the topics use, in the home
  Region (`ap-southeast-2`, as the commands pass it).

`W` is the worker's ARN, which the first command looks up. `M` is that profile's id
(`python scripts/admin_cli.py model-config get` shows it). For an application inference profile,
`M` is its ARN, which Converse needs. The run file and the trace then name no model, because that
ARN carries the account id (a system profile's ARN is recorded as its id).

Every site id the worker is sent is `eval-sydney`, so these runs never pass for production's in
its logs. The run file names the worker's Region and code, never its ARN. Each sequence costs
about 10 worker calls and a few cents of model calls.

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

With the same `W` and `M`, the command below runs the thirteen cases in
`scripts/vision_eval_cases/` (the [evidence README](hackathon/evidence/README.md#the-agents-cases)
lists them) against the deployed worker. Each case's history goes through the real adapter. Then
its newest tick is asked of the real model five times from the same state. Some cases differ:

- C7 and C9 run their scripts.
- C8 is asked with an invalid model id on purpose.
- C3 and C10 count only if their newest scene passes the gate and so reaches the agent; otherwise
  they are left out of the scores.

That is about 55 live runs of up to five turns: a few US dollars of model calls (the run file
totals them in USD and AUD, by the built-in price table) and cents of worker time.

```sh
python scripts/vision_eval.py agent --worker "$W" --agent bedrock --model-id "$M" --region ap-southeast-2 --repeats 5 --out $E
```

```powershell
python scripts\vision_eval.py agent --worker $W --agent bedrock --model-id $M --region ap-southeast-2 --repeats 5 --out docs\hackathon\evidence\runs
```

It exits 1 when:

- an unconditional case never reached the agent;
- a failure case did not fail closed as it must; or
- the worker did not measure a case's newest scene (the run keeps its code and scores that repeat
  nowhere).

To re-run one case, add `--case C1` (repeatable).

Without Bedrock access every case ends `bedrock_error`, counted, never dropped. Check the summary's
`failed_closed_task_runs` before committing a run that only measured that.

To run the worker in this process instead, add `$C` and pass `--worker in-process`. Without a cache
it is refused off an EC2 instance in us-west-2.

### 7. The counterfactuals (2.6.6)

This runs C1 and C10 (C4 in C10's place if C10 does not pass the gate) in two arms, about 22
model runs in all:

- **Arm (a), the agent:** each case's newest tick, five repeats in each of its two arms,
  `measured` and `baseline`, which differ only in `look_again`'s numbers.
- **Arm (b), the gate:** the same newest scene clouded to coverage 0.62 and 0.58, either side of
  the 0.6 floor, in this process. This arm needs the cache.

```sh
python scripts/vision_eval.py counterfactual --worker "$W" $C --agent bedrock --model-id "$M" --region ap-southeast-2 --repeats 5 --out $E
```

```powershell
python scripts\vision_eval.py counterfactual --worker $W --cache $HOME\cogs --agent bedrock --model-id $M --region ap-southeast-2 --repeats 5 --out docs\hackathon\evidence\runs
```

Its checks hold that both sides were measured at their coverage and that no model was asked
below the floor. Arm (a)'s `baseline` arm is a constructed tool output, and the report says so.

### 8. E3, stability over the year

This step runs the manifest's `year-2024` set (17 scenes) through the production rule, with
triage off, on one network. It runs in this process so the built-up masks can be compared. It
reads about 1 GB once into the cache, so run it on the Graviton instance or another machine in
us-west-2. It calls no model and invokes no worker.

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
(`REFERENCE_PINS` in `scripts/vision_eval_reference.py`). The message gives the pinned ETag and
size and the ones found, not the URL. Check it with
`curl -sI https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_S36E150_Map.tif`,
and pin the new ETag (without curl's quotes) and size in a commit of its own that says why,
before the run.

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
`RESULTS.md`, and update the A2, A3 and S7 rows of [the rubric tracker](hackathon/rubric-tracker.md).

If a run file was made before the submission tag, on code that has changed since in a way that
matters to it, add its run id to `docs/hackathon/evidence/older-code.json` with the reason, then
run `index` again.

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
- Your `.pii-denylist` at the repository's root (or `PII_DENYLIST`). Put it in place before you
  start. The export runs the check on every file it writes, and refuses without a list, since
  nothing would then check the files before they are committed.
  - It checks only at the end. Without a list it still reads everything and writes every file,
    then refuses (exit 2, `refused: PII_DENYLIST is unset and <repo>/.pii-denylist is missing`).
  - The refused export is moved to `live/<date>.refused/`, your `labels.csv` is left where it
    was, and every network file it wrote is put back as it was.
  - `--no-denylist` writes unchecked, and says so.

The logs are read through Logs Insights, a few queries at a time, each batch waited for at most
`--logs-wait` seconds (120); a 90-day scan of the three groups is a few GB at most, cents.

### 1. Export

```sh
python scripts/evidence_export.py export --env dev --networks --figures 12
```

```powershell
python scripts\evidence_export.py export --env dev --networks --figures 12
```

It writes `docs/hackathon/evidence/live/<UTC date>/`. With `--networks` it also writes each city's
network as `scripts/vision_benchmark_networks/<site>.json`:

- Fill each new file's row in that directory's `NOTICE.md` from its `fetched_at`, `endpoint` and
  `query_url`, and its sha256, which `provenance.json` gives under `networks`.
- A committed fixture that holds the same network is left byte for byte (its `kept` says
  `unchanged`). One that holds another network is kept, with a line saying so.
- Do not pass `--replace-networks` once the evaluation's runs are made:
  [the evaluation's real-data runs](#the-evaluations-real-data-runs) pin each fixture's sha256,
  and `replay --agent recorded` refuses a file that changed.

It prints any log read that did not complete. "not_checked" means a query did not finish within
`--logs-wait`: run it again with `--logs-wait 300 --replace`. `--replace` removes only the
export's own files: your `labels.csv` stays.

Exit 2 with `pii-denylist: <file>:<line> contains denylist entry #n` means a personal string
reached a file. The export is moved to `live/<date>.refused/` (git ignores it), your `labels.csv`
is left where it was, and every network file it wrote is put back as it was. Find where the
string came from, then export again.

Leave out `--with-notes` for the committed export unless you have read every note it adds (model
and third-party text, scrubbed and labelled, but quoted).

### 2. Read it before committing

Check these in `summary.json`:

- `logs`: each one's `state` is `complete`.
- `redacted`: 0 expected. If it is anything else, `redacted_at` names the file, line and field of
  each (the first 100): find out why.
- `observed_failures.worker.not_checked`: worker runs whose own line was not read. It is 0 when
  every log read completed.
- `human_control.completeness.unmatched`: each entry is a line nothing corroborates. The report
  says so.

Then read the traces under `traces/`.

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

The second reads live (the logs and the running state, now) and writes nothing. In the video, run
query (c) from [the vision runsheet's queries](vision-runsheet.md#the-queries) on
`/aws/lambda/<prefix>-dev-research-tick` in the home Region, with the same tick id the committed
trace shows.

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
scripts\pii_denylist_check.py --staged`). Update the O9, S7 and A3 rows of
[the rubric tracker](hackathon/rubric-tracker.md).
