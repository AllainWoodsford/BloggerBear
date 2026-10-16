# The evidence trail: what the pipeline measured, decided and did

## The ask

The owner's words, reviewing the OpenCV AI Competition 2026 rubric:

> I need to be able to track the results of this and make sure Alexa+ and the MCP can interrogate
> the evidence, i.e. via logs if necessary.

The rubric wants the chain shown: OpenCV's output changing a later decision, a tool call, an
action, and a person's approval. Until this round none of it was kept. The agent's trail was
overwritten on the next due tick (`fetch_state` rebuilds the top level of the running state, and
`triage` lived there), its tools' results were never stored (only the model saw them), and the
success path logged nothing: a Finding said what was summarised, not what was measured, compared
or decided. Evidence that is not captured cannot be recovered later, so this is the submission
round's first pull request, ahead of the first live triage.

The evidence has three homes:

- **One JSON line per step, in the logs** (`lambdas/common/evidence.py`): the research tick, the
  two vision adapters, the vision worker and the admin API each print one line per step. The logs
  are kept 90 days in both Regions, and Logs Insights reads them in the console today (the
  queries are in the [deployment runsheet](../deployment-runsheet.md#evidence-what-the-pipeline-measured-and-decided));
  the operator's assistant and Alexa+ read them in the next pull request.
- **A decision ring in the running state** (`lambdas/common/vision_decisions.py`): per site, the
  last twelve decisions with the agent's whole trail, each tool's result included.
- **An export into git**, later in the round, which makes the code-derived part permanent.

## The chain, and the ids that join it

```
research tick ── tick = its Lambda request id; run = new per attempt ── research_tick {outcome, stored,
  │                                                                                    decisions, finding}
  adapter, per configured site, right after its turn ──────────────── vision_site {site_id, outcome, code,
  │                                                                                 scene_id, worker_request_id}
  │   └─ worker in us-west-2 (request_id = worker_request_id) ──────── vision_measure {build, cpu, timings, io,
  │                                                                                    coverage, metrics, rss}
  adapter, per measured scene ─────────────────────────────────────── vision_decision {site_id, scene_id, at, code,
  │                                                                                     moved, network_changed,
  │                                                                                     thresholds, reported}
  │   └─ the agent, when the numbers moved ─────────────────────────── vision_triage {verdict, stop, steps[...],
  │         └─ look_again → worker (its own request id) ────────────                   worker_request_ids}
  Finding (topic_id, captured_at = at) ────────────────────────────── research_tick.finding = at, stored = true
daily cycle ─ article (lineage.research.calls[].captured_at = at) ─── the Articles table: status, created_at,
                                                                      published_by, published_at
admin API ─ a person approves, rejects, unpublishes or sends back ─── moderation_decision {queue_id, article_id,
                                                                                         decision, from_status}
admin API ─ a topic is created, changed, paused or resumed ────────── topic_changed {fields, config_keys, ...}
```

The join keys:

- **`(topic_id, at)`**: `at` is the tick's `fetched_at`, the Finding's `captured_at` and the
  article lineage's `captured_at`.
- **`worker_request_id`**: the Lambda request id of the worker invoke, read by the client from the
  invoke's response and carried on every result and every failure after the invoke answered. It is
  the worker's own `request_id` in its `vision_measure` line, and the `@requestId` of its `REPORT`
  line (billed duration, peak memory).
- **`tick` and `run`**: `tick` is the research tick's request id, which also keys Lambda's `START`,
  `REPORT` and "Task timed out" lines for that invocation. The tick is invoked asynchronously, so
  a timed-out one is retried by Lambda, believed to be under the same request id; `run` is a new
  12-hex id per attempt, so two attempts are told apart whatever the retry does.
- **`article_id`** and **`queue_id`**.

**Kept, reported and written are three different facts.** A decision is *reported* when the
adapter put its site in the diff summary (`vision_decision.reported`). An attempt's decisions are
*kept* when its `research_tick` line says `stored: true`: the running state, with its ring, was
written. *A Finding was written* only when that line's `finding` equals the decision's `at`. The
state is stored after the summary model call, so a tick whose summary call fails has printed its
`vision_decision` and `vision_triage` lines and then says `outcome: error, stored: false`; a tick
that times out leaves no `research_tick` line at all. Either way the next tick measures the same
scene again. Every reader marks such decisions "not kept: the tick failed and the scene is
measured again" and de-duplicates by `(topic_id, site_id, scene_id)`, keeping the kept copy.

## What is recorded where

| Step | Log line | Running state | Durable copy |
|---|---|---|---|
| A tick ran | `research_tick`, research-tick group, home Region | - | the export |
| A site's turn | `vision_site`, same group, at the branch the loop took | `last_error`, `failed_scene`, as before | the export |
| OpenCV measured | `vision_measure`, the worker's group, us-west-2; Lambda's `REPORT` line for every run, the ones killed by the timeout included | the history entry, as before; the ring entry's `measured_by` | the export; the figure PNG |
| The numbers were compared | `vision_decision`, research-tick group | a ring entry per measured scene | the export |
| The agent decided | `vision_triage`, research-tick group | the ring entry's `triage`, with each tool's result, the news hosts and the agent's reason | the export (the reason only on request) |
| A Finding was written | `research_tick.finding`, with `stored` | - | the Findings table (14 days); the export |
| An article was drafted and held | - | - | the Articles table (`status`, `created_at`, the lineage's `captured_at`s) |
| A person decided | `moderation_decision`, admin-api group (with the time, which the Articles table does not keep for a rejection) | - | the Articles table (`status`, `published_by`, `published_at`); the export |
| A topic was created, changed, paused or resumed | `topic_changed`, admin-api group | - | the Topics table (the current value only); the export |

The daily cycle emits nothing: the Articles table already keeps the status, `created_at` and the
lineage's `captured_at`s, and `published_by` tells a held-then-approved article from any other.

## The events

Every line carries `evidence` (`bloggerbear/1`), `event`, and the bound `tick`, `run` and
`topic_id` (null when not bound; the admin API binds `tick` and `api_request_id` and passes the
topic explicitly). The schema is declared in `evidence.EVENTS`, not inferred: a field is kept only
when its value passes its kind's check (`code`, `id`, `iso`, `int`, `num`, `bool`, `hex12`,
`model`, `cpu`, `numbers`, `version`, `codes`, `ids`, `steps`); a value that fails is dropped and
its *name* goes under `dropped`, so schema drift can be read off the log; a list is cut, never
dropped whole (over its cap it keeps its first items; a step that is not a step is left out and the
others stay), and named under `cut`.

| Event | Emitted by | Fields |
|---|---|---|
| `research_tick` | `research_tick_handler.handler`, once per invocation, in a `finally` | `adapter`, `outcome` (`not_due`, `paused`, `no_change`, `material_change`, `error`, `unknown_topic`, `unknown_adapter`), `stored`, `decisions`, `triages`, `finding` (iso), `force`, `ms`, `error_type` |
| `vision_site` | each vision adapter's `fetch_state`, right after each site's turn, and for each site the loop passed over | `site_id`, `outcome` (`measured`, `deferred`, `not_reached`, `none`; ships also `no_scene`), `code` (the error this tick recorded for the site), `scene_id`, `worker_request_id`, `worker_ms`, `budget_left_s` |
| `vision_decision` | each adapter's `material_diff`, once per verdict of `assess()`, with the ring entry | `site_id`, `scene_id`, `at`, `captured_at`, `coverage`, `measure`, `baseline`, `delta`, `relative`, `baseline_scenes`, `thresholds`, `code`, `moved`, `network_changed`, `triaged`, `verdict`, `stop`, `reported`, `backend`, `build`, `cpu`, `worker_request_id` |
| `vision_triage` | each adapter's `_triage`, right after each site's outcome, so a timeout later in the tick still leaves it | `site_id`, `scene_id`, `verdict`, `stop`, `model_id`, `model_calls`, `input_tokens`, `output_tokens`, `cost_usd`, `elapsed_ms`, `deadline_left_s`, `figure`, `tools` (in call order), `tools_total`, `look_again`, `budget_refusals`, `steps`, `steps_total`, `worker_request_ids`, `reason_sha256` (the reason's SHA-256 prefix, so a reason quoted from the ring or the export can be checked against the log without logging it) |
| `vision_measure` | `vision_worker_handler.lambda_handler`, every request that returns, success or refusal | `request_id`, `task`, `backend`, `ok`, `code`, `site_id`, `scene_id`, `build`, `opencv`, `kleidicv`, `cpu`, `cpu_count`, `arch`, `window_px`, `io_requests`, `io_bytes`, `levels`, `read_ms`, `analyse_ms`, `graph_ms`, `annotate_ms`, `total_ms`, `coverage`, `quality_flags`, `stations`, `snapped`, `nodes`, `edges`, `components` (rail), `count` (ships), `rss_peak_mb` |
| `moderation_decision` | the admin API, after `update_moderation_status` or `update_article_status` | `topic_id`, `queue_id`, `article_id`, `decision` (`approved`, `rejected`, `force_published`, `unpublished`, `sent_back`), `from_status`, `by` (always `human`) |
| `topic_changed` | the admin API, after `put_topic` or `set_topic_fields` | `topic_id`, `action` (`created`, `updated`, `paused`, `resumed`), `fields`, `config_keys`, `config_numbers`, `config_on`, `config_off`, `force_manual_review` |

**The worker writes its line with its own serialiser.** Its package ships only
`common/vision_contract.py`, so it cannot import `common/evidence`; a test holds its prefix equal
to `evidence.PREFIX` and its line byte for byte equal to what `evidence.line` would write. The
line is printed after the reply is built, inside a guard, and never changes the reply. A run
killed by the 60 s timeout or by running out of memory never reaches it: Lambda's `REPORT` line is
its only trace, and query (g) of the runsheet counts those, so a missing `vision_measure` line is
never read as a logging gap.

**The step results** (`vision_triage.steps`; `evidence.STEP_RESULT`): one entry per tool call, in
order, the budget refusals included (`ok: false`, `result: {"code": "budget"}`), each with the
tool's code-derived result built from the body the model was given: for `look_again` the settings
sent, the measures re-measured (rail: `coverage, served_share, desert_km2, built_up_km2,
station_count, flag_count`; ships: `coverage, count, density_per_km2`), the quality flags and the
worker call's request id, build and wall time (kept on failure too, with the worker's error code);
for `previous_scene` the scene id and whether it had a figure; for `site_history` how many scenes;
for `web_context` how many results and how many were dated. A tool the model invented is `other`
with `unknown_tool`. The trail keeps every step; the line and the ring keep the first eight, with
`steps_total` beside them.

## The decision ring

`state["sites"][site_id]["decisions"]` is a list of the last twelve decisions (`DECISIONS_KEPT`:
Sentinel-2 revisits a tile every 2 to 5 days, so twelve scene decisions cover 4 to 8 weeks; the
logs hold 90 days and the export keeps the rest). It lives in the site's record because both
adapters' `fetch_state` deep-copy each record from the previous state and rebuild only the top
level, so it is carried forward with no change; a state written before this round has no
`decisions` key and the ring starts. One entry per verdict of `assess()`, written on every return
path of `material_diff` and never twice for the same scene and tick:

```
{"v": 1, "at": <the tick's fetched_at>, "tick", "run", "scene_id", "captured_at", "coverage",
 "measure": {...}, "baseline": {...} | null, "delta": {...} | null, "relative": {...} | null,
 "baseline_scenes", "thresholds": {...},
 "code": first_observation | below_floor | baseline_building | within_range | reported | artefact,
 "moved": [codes], "network_changed": bool | null,
 "triage": null | {verdict, stop, model_id, model_calls, input_tokens, output_tokens, cost_usd,
                   elapsed_ms, deadline_left_s, figure, steps, steps_total, budget_refusals,
                   hosts, reason},
 "reported": bool,
 "measured_by": {backend, build, cpu, worker_request_id, worker_ms}}
```

- **The code** (`vision_decisions.decision_code`) reads only fields `assess()` and `_triage` set
  and compares no measure with a threshold, so it cannot drift from the adapter's rule; it equals
  the word `vision_sites.verdict_status` speaks for the newest scene, and a test holds the two
  equal over both adapters' fixtures.
- **`moved`** is written by each adapter's `_judge` itself, from the same conditions that write
  its reason phrases (rail: `served_share`, `desert_km2`, `station_count`; ships: `count`): one
  rule, by construction, pinned by a test. It is never recomputed from the stored delta and the
  thresholds, which would be a second copy of the rule that disagrees at the boundary.
- **`network_changed`** (rail only) says whether the newest scene was measured against another
  copy of the OpenStreetMap network than its baseline's newest scene: with the same copy on both
  sides every moved measure came from the imagery. Each adapter's `decision_vocabulary()` declares
  which input each measure depends on (the served share and the desert area on the imagery and the
  network both, the station count on the network alone; the ships' count on the imagery alone), so
  a reader can say which input moved without re-implementing a rule.
- **The ring alone keeps two kinds of third-party text**: the agent's reason, and the host names
  of the news results it read (lower-case ASCII, at most five). They are shown only in
  "(unverified)" cells on the page, never spoken, never logged, never in a result's rows.
- **Size.** About 3.4 KB an entry at the caps (eight `look_again` steps, five hosts, a
  300-character reason): some 40 KB a site, 125 KB for three, against a rail state of about 10 MB.

**The trace** (`vision_decisions.trace`) is the one rendering of a decision's six steps for every
reader, so they cannot disagree: `measured` (coverage, the measure, who measured), `compared` (the
code, the baseline, the delta, the thresholds, what the gate did: `kept_out_cloud`,
`kept_out_baseline`, `kept_out_range`, `sent_to_agent`, `reported_first`, `reported_untriaged`,
and for each moved measure its source), one `tool` row per step, `verdict` (`real`, `artefact`, or
`failed_closed` with the stop), `action` (`finding_written`, `no_finding`, `not_kept`,
`finding_lost`, `finding_would_be_written`) and `person` (`held`, `approved`, `rejected`,
`unpublished`, `published_unreviewed`, `no_article_yet`, `finding_expired`). It returns rows of
codes and numbers; the words come from `TRACE_WORDS` and the adapter's vocabulary. **Facts only**:
a `look_again` row states what was re-measured at which settings beside the same scene's numbers
at the topic's settings, and never compares a re-measurement with the baseline. `look_again`
re-measures only the newest scene at other settings, the baseline is the median of earlier scenes
at the topic's settings, and the adapter itself refuses to compare scenes measured with different
parameters; so no row says "the change held" or "fell back inside the usual range". Causal
evidence comes from the evaluation's counterfactual experiments, not from the trace.

## What is never recorded, and why readers parse lines

A log line holds only code-derived values: codes, ids (topic, site, scene, request, run, article,
queue), times, numbers, hex build prefixes and canonical model ids. It never holds a site name, a
station name or any other OpenStreetMap text; an error's detail or an exception's message; a news
title, URL or host; a summary, or the agent's reason (its SHA-256 prefix only). That is not a
filter applied to text but the schema itself: `evidence.line` keeps a field only when its value
passes its kind, and a test feeds every hostile value (an e-mail address, an IPv4 address, the
instruction-like variants `redact.scrub` withholds, a URL, a host, a 12-digit account id, an ARN,
a name with a bidi override, a value with an embedded newline) into every field of every event and
asserts none appears in any line. An application-inference-profile ARN carries the account id, so
the `model` kind drops it.

A line in a log group is only as trustworthy as every `print` in that group. In the Text log
format the Python runtime writes each newline-terminated line of stdout as its own CloudWatch
event, so a print of `str(exc)` whose message carried `\n{"evidence": "bloggerbear/1", ...}` would
have made a well-formed evidence line. The three prints that interpolated an exception as text
now use `!r`, an AST test (`tests/test_no_raw_exception_prints.py`) holds every print in the
handlers and adapters to it, and the evidence writer's `ensure_ascii` keeps its own lines single.
Beyond that, **every reader parses, never trusts**: `evidence.parse` takes a line back through the
same kinds, returns nothing for a line that is not an evidence line of this version with a known
event, drops every field that fails its kind, and never raises. Readers ask Logs Insights for
`@timestamp` and `@message` only, filter by field equality (never a substring), and report what
they cannot match. `log_review` leaves the evidence lines out of its error search by their fixed
prefix (their codes include `timeout`, `failed` and `bedrock_error`), and no field is named
`tool_calls` (the trail's field is `tools`), so nothing in a line reads like instructions.

## Retention

- Logs: 90 days in both Regions, unchanged.
- The ring: while the topic runs. Dev deletes the running state 21 days after its last write, with
  the snapshots; production versions it.
- The export (later in the round) makes the code-derived part permanent in git. Raw lines pulled
  from CloudWatch by hand go under `evidence-raw/`, which git ignores.
- Volume: a rail topic writes about four lines a tick (one site): `research_tick`, `vision_site`,
  `vision_decision`, `vision_triage`; hourly, about 100 lines and well under 100 KB a day per
  topic. The worker adds one line per invoke. The CloudWatch cost is negligible.

## A pipeline change, made for a new reason

The assistant's vision round decided against a status file written by the research tick: reading
the running state directly kept that round read-only. This round reverses it in spirit, for a new
reason: the owner asked to track the results, and the rubric's agentic rows cannot be evidenced
from a state the next tick overwrites. The ring is a key in the state the tick already writes, not
a side file; the lines are prints in functions that already print; nothing changes for a topic
that is not a vision topic (the regression probe over the four non-vision adapters shows
byte-identical Findings, articles and prompts, with one `research_tick` line more in the log).

## What this round does not change

The research tick's decisions (what is material, the Finding's shape and TTL, `put_finding`, the
summary prompt, every return value); `assess()` in either adapter beyond the `moved` and
`network_changed` fields `_judge` adds; the worker contract and the reply's content (`build` gains
`cpu` and `cpu_count`, which `validate_reply` allows); `tool_calls`, `parse_verdict`, `_run_tool`'s
signature and the model's input; the history entry's shape; `stats_tracking`; moderation, article
and topic writes (only prints are added); Terraform. `maximum_retry_attempts = 0` on the research
tick is the owner's call, not this round's.

## Status

| Part | State | Where |
|---|---|---|
| `common/evidence.py`: the events, the kinds, the caps, the parser | done | this pull request |
| The agent's trail keeps each tool's result, its stop, its time and spend | done | this pull request |
| The decision ring, `decision_vocabulary`, the trace | done | this pull request |
| The worker's `vision_measure` line and `build.cpu` | done | this pull request |
| The client carries the worker's request id on every result and failure | done | this pull request |
| The research tick's and the admin API's lines; the `!r` prints and their AST guard | done | this pull request |
| `log_review` leaves the lines out; the topic filter matches a whole id | done | this pull request |
| The runsheet's Logs Insights queries and the first-deploy check | done; to be run once on dev after the deploy | this pull request |
| The assistant and Alexa+ read the evidence from the logs (`vision_evidence`) | next | the following pull request |
| The evidence from the running state, the agent's note on screen, the worker's runs | later | after the assistant's vision round |
| The export into git | later | the evaluation pull requests |
