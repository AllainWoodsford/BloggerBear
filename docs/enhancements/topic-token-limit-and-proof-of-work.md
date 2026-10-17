# Per-topic limits, richer vision articles, and a proof of work for the reviewer

**Status:** proposed, not built · **Date:** 2026-10-16 · **Builds on:** [vision.md](vision.md) (the
vision stack as merged) and [rail-access-monitor.md](rail-access-monitor.md) (the rail task, PRs
#285 to #298)

## Why

The first rail topic seeded in dev, `trains-sydney`, asks for an audit of Sydney's network: where
dense built-up ground is served and where it lies in a transit desert, what is fragile, which
simulated link would help most, and what moved since the last scene. The pipeline measures all of
that (the heat map, the served share, the deserts, the graph, the flags, the simulations, the
figure), but the article that comes out is thin, and three things in the code keep it so:

1. **The Finding is written under hard-coded limits.** The rail summary prompt
   (`lambdas/common/adapters/rail_access.py`, `build_summary_prompt`) says "Write 2-4 plain
   sentences" (3 to 6 with planner notes) and "Give no cause, forecast, political, planning or
   investment view; do not say any area is well or badly served in absolute terms". The ship
   adapter's prompt has the same shape. The topic's editorial goal never reaches either: both
   adapters return their own prompt, and `resolve_editorial_goals` is only called for the generic
   research prompt. So a topic's `primary_focus` governs the article, and a different, fixed rule
   governs the evidence the article is drafted from.
2. **The draft sees only summaries.** `daily_cycle_handler._format_findings_summaries` emits
   `- {summary}` per Finding. The planner notes, the figure captions and the per-site metrics
   (served share, desert area, station count, the deserts, the flags, the suggestions) are stored
   on the Finding or in its snapshot and never put in front of the drafting model. The base
   class says of planner notes: "nothing renders them".
3. **The caps are constants.** The research summary gets 2,048 output tokens, the draft 4,096,
   the findings block 40,000 characters, and the three reviewers (compliance, the fresh-data
   review, Re-Write) each see 12,000 characters of source. A topic cannot ask for more. The
   compliance code check (`invented_figures`, which holds any number over 20 that is not within
   3 % of a source number) runs on the truncated 12,000 characters while the draft was written
   from 40,000, so a real figure past the cut is held as fabricated.

And when the article is held, which every vision article is, the reviewer gets little to check it
against. The inbox card shows the title, a 700-character preview, the hold reasons, the fresh-data
notes, a count of sources and the cost. It shows no figure, no planner note, no Finding, and the
article stores no Finding key: the only link back is `lineage.research.calls[].captured_at`, which
covers the whole window rather than what reached the prompt.

What the operator asked for, in order:

- **Limits per topic.** The summary, draft and review caps are fields on the Topic, set with
  `topics create|update`, with defaults that apply when unset and are documented in
  [configuration.md](../configuration.md). A topic seeded without them gets the defaults; the
  elevated limits are configured on the topic.
- **Both stages lifted.** The content rules (no forecast, no recommendation) stop being hard-coded
  in the adapter prompts and move into the editorial goal, which then governs the Finding summary
  as well as the article. The adapter default goal stays conservative, so an unconfigured topic
  reads the same rules it does today.
- **No population layer.** Built-up density stays the proxy. Out of scope.
- **A proof of work.** For a topic with `topic_has_proof_of_work`, each held article gets a plain
  text file in S3 that links the candidate angles and the Findings (and their snapshots and
  figures) it was drafted from, the limits in force and the reviews' verdicts. The admin CLI and
  the inbox say where it is. It is deleted when the article is approved, rejected or re-written.
- **Both vision adapters**, `rail_access` and `satellite_vision`.

The non-negotiable rules hold: no publish without the compliance review, and no vision text
published without a person (`force_manual_review` is unchanged).

## What it is

Four pieces, each a pull request, each leaving an unconfigured topic as it is today.

### 1. Per-topic limits: `lambdas/common/topic_limits.py`

Flat fields on the Topic item, not a nested block, so they ride the existing `_update_topic`
field loop, the one-flag-per-field CLI pattern and the ops assistant's argparse-driven CLI guide
exactly as `findings_window_hours` does.

| Field | Default | Range | Replaces |
|---|---|---|---|
| `summary_max_tokens` | 2048 | 256 to 8192 | `research_tick_handler.RESEARCH_MAX_TOKENS` (retry at twice the value) |
| `summary_max_sentences` | 4 | 1 to 60 | the vision prompts' "Write 2-4 plain sentences" / "3-6". At the default the literal strings are kept; any other value renders "Write up to {n} plain sentences, as many as the measurements warrant" |
| `draft_max_tokens` | 4096 | 1024 to 16384 | `daily_cycle_handler.DRAFT_MAX_TOKENS` (retry at twice). Above the default the draft prompt's "(a few paragraphs)" becomes "(as detailed as the findings support)" |
| `summaries_max_chars` | 40000 | 4000 to 200000 | `SUMMARIES_MAX_CHARS`, the findings block the ideation, draft and reviews receive |
| `review_source_max_chars` | 12000 | 4000 to 200000 | one knob for the three reviewer caps: `compliance.MAX_SOURCE_CHARS`, `fresh_review.FINDINGS_MAX_CHARS`, `rewrite.FINDINGS_MAX_CHARS` |
| `max_figures_per_article` | 3 | 1 to 10 | `figures.MAX_FIGURES_PER_ARTICLE` |

Not a field: the planner-notes cap (`rail_notes.MAX_NOTES`, four flags and three simulations per
entry). It is the per-entry design cap, a rail topic is one city, and once the summary's "at most
three of them" is lifted (piece 2) and the draft sees every stored note (piece 3), all seven reach
the article anyway.

The module mirrors `findings_window.py`: a `LIMITS` table of `(default, low, high)`, `_as_whole`
(rejects bool, float, string and a NaN Decimal, as `_as_hours` does), `limit_error(field, value)`
("must be a whole number from low to high, or null for the default (d)"), and `limits_for(topic)`,
which returns every field with a stored invalid value skipped. No adapter imports.

**A fix that rides along.** `compliance.review_draft` keeps sending the model the truncated source,
but `invented_figures` and `_item_stands` (code, no tokens) get the whole block, so a number the
draft took from past the cut is no longer held as fabricated. This changes behaviour for every
topic and the PR says so.

### 2. The editorial goal governs the vision summaries

`build_summary_prompt(topic, diff_summary, new_state)` already receives the Topic. Both adapters
add `OPERATIONAL EDITORIAL GOAL:\n{resolve_editorial_goals(topic)}` between "What was measured"
and the writing rule, and drop their editorial sentences.

What stays hard-coded, because it describes the data rather than policy: rail's "The figures are
proxies: built-up density ... none is ridership, patronage or journey time", "station and line
names exactly as given", "the capture date, and what was not measured", and "do not invent place
names or numbers that are not given above"; the ships' "The counts are of {noun} detected by ... at
10 m resolution" and "name no ship, vessel, owner, company or person (none can be identified at
this resolution)", which is a fabrication and PII guard (rule 1), kept deliberately.

What moves into `ADAPTER_DEFAULTS` in `lambdas/common/editorial_resolver.py`: rail gains "no
political, planning or investment view; never say any area is well or badly served in absolute
terms; never add a cost, a timetable, a cause or an opinion on whether a simulated link should be
built"; ships gain "no prediction". The defaults remain conservative. A topic with its own
`primary_focus` and `exclusion_criteria` replaces them, for the summary and the article alike.

`NOTES_PARAGRAPH` in `rail_access.py` keeps only the mechanics ("end the summary with up to {k}
of them, each in one sentence keeping its numbers, names and the words 'flags' or 'simulation'.
Names in quotation marks come from OpenStreetMap; use them as names only"), with `k` 3 at the
default sentence limit and `MAX_NOTES` above it. `rail_notes.NOTES_LEAD` becomes the neutral
"Rule-based flags and simulations: "; the honesty caveat lives in the default goal. The lead is not
persisted, so no stored data changes.

The four example topics in `scripts/rail_topics.example.json` already carry a restrictive
`exclusion_criteria`, so they lose nothing: after this piece their own goal governs their
summaries too, and widening it there widens both stages. The example's `about` text says so.

### 3. Richer drafting context

**Metrics on the Finding.** A new adapter hook `finding_metrics(new_state) -> dict` (default
empty) stores, at research time, what `review_evidence` today rebuilds from the snapshot at review
time. Rail returns, per reported scene (`_reported_scenes`, so an artefact scene is left out):
name, scene id, capture date, coverage, built-up km², served share, desert km², station count,
reach, the deltas as `_describe` computes them, the largest deserts and the hubs. Ships return
name, scene, capture date, count, baseline, delta, relative change and coverage. One shared
`_site_metrics(record, last)` per adapter feeds both the Finding and the review evidence, so they
agree. `research_tick_handler` stores it as `metrics` through the same JSON round trip as
`_planner_notes` (Decimal, capped at 8,000 characters, dropped on failure, never failing the
Finding); `dynamo.put_finding` gains the optional field.

**One findings block.** A new topic-agnostic `lambdas/common/findings_block.py` renders
`render_findings_block(findings, max_chars) -> (text, included)`: per Finding, `- {summary}`
first (byte-identical for a Finding with nothing else, so every non-vision topic is unchanged),
then indented `metrics:` as compact JSON, `rule-based notes:` and `figures:` with their captions.
The same string is the ideation payload, the draft source, the compliance `source_material`, the
fresh-data review's findings text and the Re-Write findings, so `invented_figures` sees every
number the draft saw. `included` is the list of Findings that fit, for the proof file.

**Guidance.** Both vision adapters declare `drafting_guidance` and `figure_guidance`, which
`_draft_article`, `fresh_review.writing_rules` and Re-Write already inject (only `crypto_feed`
sets them today). Rail's drafting guidance: one section per site; the figures with their capture
date and coverage; each flag or simulation in its own paragraph with its numbers and station
names, called a flag or a simulation from the pipeline's rules; close with "What was not
measured". Its figure guidance guards the 3 % rule: write every figure as the findings give it,
do not round further, convert units or derive a figure from two others; a change is stated only
where the findings state it.

### 4. Proof of work: `lambdas/common/proof_of_work.py`

**The flag.** `topic_has_proof_of_work`, a boolean on the Topic. Defaulted, not forced: when
unset it is true for the vision adapters (`MANUAL_REVIEW_ADAPTERS`) and false otherwise;
`_create_topic` stores the explicit `true` for a vision topic so `topics get` shows it, as it does
`force_manual_review`. Unlike `force_manual_review`, which is a safety property, the proof is a
reviewer aid an operator may turn off for a noisy dev topic, or on for any other topic.

**The file.** `proof/{article_id}.txt` in the content bucket, beside `articles/{id}.md`. Plain
text, in sections:

- the topic and article ids, the run's times, the title;
- the resolved editorial goal, as the prompts saw it;
- the three candidate angles from ideation, with the chosen one marked;
- for each Finding that reached the prompt: its key (`topic_id` and `captured_at`; Findings have
  no id), its summary, its planner notes, its metrics, its figures as `s3://bucket/key`, its
  snapshot `s3://bucket/snapshots/...`, its source references (title, URL, accessed);
- the six limits in force, each marked default or set;
- the compliance verdict and every reason, including those "flagged but not held";
- the fresh-data review: status, outcome, claims, evidence date, held or revised;
- the hold reasons;
- a footer: Findings expire 14 days after capture and snapshots 21 days after; figure PNGs stay.
  Links past that are dead.

**When it is written.** In `_publish_or_moderate`, after the body is put and before
`put_article`, when the topic has proof of work and the article is going to moderation. The
article row stores `proof_of_work_s3_key`; the moderation item stores the same key so listing
needs no join. A failed write is logged and the article proceeds without a key: the proof is
never a reason to lose an article. The daily cycle threads the angles, the chosen angle, the
resolved goal, the limits and the included Findings into `_publish_or_moderate` for this.

**When it is deleted.** On approve and reject in `_resolve_moderation_item`, on force publish in
`_publish_article`, and on Re-Write in `rewrite._save`, which deletes the old file and writes the
rewrite's own (its angles section says ideation was not re-run and lists the issues and the
editor's note instead). `dynamo.set_article_proof_of_work(article_id, key | None)` sets or
removes the field so a dead link is never shown. A 30-day lifecycle rule on `proof/` is the
backstop for an orphan (a `put_article` failure after the write, a topic deleted with pending
articles).

**Where it is shown.** `GET /articles/{id}` and `GET /moderation-queue` carry `proof_of_work` as
an `s3://` URI when the key is set; `moderation list` and `topics get` print it through the JSON
they already print; a new `articles get <article_id>` CLI verb reads the article endpoint, which
the CLI has no verb for today; the inbox card adds "Proof of work: s3://..." to its facts line.

**Infrastructure.** The shared Lambda role (`aws_iam_role.lambda_exec`, dev and production) may
get and put on the content bucket but not delete. One statement allows `s3:DeleteObject` on
`${content bucket}/proof/*` only, beside the new lifecycle rule, in both environments.

### 5. CLI and API

- `--proof-of-work` / `--no-proof-of-work` on `topics create` and `topics update`, the
  `--financial` / `--no-financial` pair's style.
- `--summary-max-tokens`, `--summary-max-sentences`, `--draft-max-tokens`,
  `--summaries-max-chars`, `--review-source-max-chars`, `--max-figures-per-article`; an empty
  string clears a field back to the default, through the existing `_parse_whole`.
- `_create_topic` validates each field with `limit_error` and stores only what was sent;
  `_update_topic` adds the fields to its loop with the null-deletes pattern;
  `topic_has_proof_of_work` is validated as a boolean like `force_manual_review`.
- The ops assistant's CLI guide derives option names from `admin_cli.build_parser()`, so the new
  flags need no guide entry.

## The pull requests

Each leaves unconfigured topics as they are (the compliance code-check fix excepted, on purpose).

**PR 1, per-topic limits.** `topic_limits.py`; validation and storage in `_create_topic` and
`_update_topic`; the CLI flags; the consumers (`research_tick_handler`, `daily_cycle_handler`,
`compliance.review_draft`'s `max_source_chars` and full-block code check, `fresh_review`'s
`run_review`, `run_revision` and `build_review_prompt`, `figures_for_findings(findings, limit)`,
`rewrite.run_rewrite`). No prompt wording changes. Tests: a new `test_topic_limits.py`; the
admin API and CLI tests modelled on the `findings_window_hours` ones; a compliance test that a
number past 12,000 characters of source is no longer held. Docs: the topic table in
configuration.md, one row per field; the CLI examples in scripts/README.md.

**PR 2, the goal governs the summaries.** `ADAPTER_DEFAULTS`; both `build_summary_prompt`s;
`NOTES_PARAGRAPH`, `_FINAL_RULE` and `_with_notes_rule`; `NOTES_LEAD`. Tests assert that the
resolved goal is in the prompt, that an unconfigured topic's prompt still holds every conservative
rule (now via the goal), that a topic with its own focus has no "Give no cause, forecast" sentence,
and that the default sentence strings are unchanged while `summary_max_sentences=12` renders the
open rule. Docs: vision.md §5 "Editorial goal", §6.5, §6.8; the runsheet's `rail_access` topics;
the rail section of configuration.md; the example file's `about`.

**PR 3, richer drafting context.** `finding_metrics` on the base class and both adapters with the
`_site_metrics` refactor; `metrics` on the Finding; `findings_block.py` used by the daily cycle and
Re-Write; `drafting_guidance` and `figure_guidance` on both adapters. Tests: metrics for reported
scenes only and equal to the review evidence; the block byte-identical for plain Findings, Decimal
rendered, oldest dropped first; compliance and the fresh review receive the enriched block; the
guidance in the draft and rewrite prompts. Docs: vision.md §6.5; the storage table.

**PR 4, proof of work.** `proof_of_work.py`; the article and queue fields; the write in
`_publish_or_moderate`; the deletes on approve, reject, force publish and Re-Write; the API
payloads; the CLI flag and `articles get`; the inbox fact; the Terraform statement and lifecycle
rule in both environments. Tests with the `s3_bucket` fixture: a rail topic's pending article has
its file with the chosen angle, the Finding keys and the compliance reasons; a GitHub topic has
none; a non-vision topic with the flag on gets one when held; a failed write still stores the
article; approve and reject remove the object and the field; a rewrite replaces the file. Docs:
configuration.md (the flag's row and a "Proof of work" paragraph), vision.md §5 and §10, the
runsheet, scripts/README.md, and the PROGRESS.md entry in the same PR.

## Verification

Unit tests in the two halves (the `.venv` lacks `mcp` and `strands`): the lambdas suite without
the ops-assistant tests, plus `scripts/tests`, in the venv; the ops half in the environment that
has them. `ruff check --fix lambdas/ scripts/` with the pinned ruff, then `bandit`, `trivy config`
and `checkov`.

In dev after PR 4's auto-apply:

1. `topics create --topic-id rail-proof-test --adapter rail_access ... --proof-of-work
   --summary-max-sentences 12 --draft-max-tokens 8192 --review-source-max-chars 40000`;
   `topics get` shows the flag, `force_manual_review` and the three limits; `topics update
   rail-proof-test --summary-max-tokens 10` is refused with the bounds message.
2. `topics trigger rail-proof-test --pipeline research_tick`; `topics findings` shows `metrics`
   and a summary longer than four sentences.
3. `topics trigger rail-proof-test --pipeline daily_cycle` returns `pending_moderation`;
   `moderation list` shows `proof_of_work: s3://<prefix>-dev-content/proof/<article_id>.txt`;
   `aws s3 cp` of it shows the goal, the angles, the Finding key, the snapshot and figure keys,
   the limits, the compliance verdict and the fresh review.
4. `articles get <article_id>` shows the same URI; the inbox card shows "Proof of work".
5. Approve from the inbox; `aws s3 ls` of the key returns nothing and `articles get` no longer
   carries it. Re-Write a second article: a new URI, the old object gone; reject it and confirm.
6. A pre-existing GitHub topic's daily cycle behaves as before: no proof, the same block.

## Risks and what bounds them

- **The 3 % rule against richer numbers.** A draft given metrics may derive a difference or a
  percentage the source never states. The figure guidance forbids derived figures, the adapter's
  own deltas are in `metrics` so the likely derivations are in the source, and the code check now
  sees the whole block. A residual hold goes to the reviewer with the proof file in hand, which
  for a vision topic is where every article goes anyway.
- **Prompt size.** The goal adds at most about 2,000 characters of input; output is what
  `summary_max_tokens` governs. Twelve sentences fit 2,048 tokens; a topic raising sentences much
  higher should raise tokens too, and configuration.md says so.
- **Link decay.** Findings expire after 14 days (`FINDING_TTL_DAYS`) and snapshots after 21; the
  footer says so, and the Finding key plus snapshot key are the link since Findings have no id.
  Figures do not expire.
- **Decimal.** Finding `metrics` and `planner_notes` come back from DynamoDB as `Decimal`; the
  block renderer and the proof text convert them, not through `dynamo._decimal_to_number`, which
  turns every non-cost value into an int.
- **Ordering.** The proof is written before `put_article` so the row carries the key, and a write
  failure never raises.
- **The ship guard.** "Name no ship, vessel, owner, company or person" stays hard-coded. If it
  should be governed by the goal too, it is one line in PR 2.

## Still open

- Whether the proof file should also be written for an article that publishes without a hold on
  a non-vision topic with the flag on. The design writes it only while a person has a decision to
  make; an "always" mode is a one-condition change.
- A trend chart of served share and desert area across the kept scenes, and a before-and-after
  figure pair on a material change, were asked for as visuals. Both are figure-generation changes
  in the worker or adapter, outside these four PRs.
