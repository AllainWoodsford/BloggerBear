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
  are kept 90 days in both Regions, and Logs Insights reads them in the console (the
  queries are in the [deployment runsheet](../../deployment-runsheet.md#evidence-what-the-pipeline-measured-and-decided));
  the operator's assistant and Alexa+ read them with `vision_evidence`
  ([Asking the assistant](#asking-the-assistant)).
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

## Asking the assistant

The owner's question, and the submission round's first-deploy check of it (T1 in the rubric
tracker), is

> Alexa, ask BloggerBear what the vision agent did for rail Sydney this week.

Alexa+ and the ask page both call `vision_evidence` (`lambdas/ops_mcp/vision_evidence.py`) with
`topic` as the operator said it, here `"rail Sydney"` and `days` 7, and say its `spoken`:

> Rail access: Sydney made 3 decisions in the last 7 days: 1 first observation, 1 within the usual
> range, and 1 change checked by the agent. The agent judged 1 real and 0 artefacts; it re-measured
> with OpenCV once and read the news once. 2 findings were written; 1 article is waiting for
> review, 0 were approved and 0 rejected. The decisions are on screen.

Since the rotating topic, `rail-cities`, ask for it by its name, "rail access": a topic is matched
by its exact id or name first, then by the closest words (`lambdas/ops_mcp/topic_match.py`), and
"rail Sydney" is closer to the demo topic, "Rail access: Sydney (demo)", which the assistant would
take instead. The submission's check (T1) and its video ask for "rail access".

Of the gate's counts (first observation, too cloudy to count, still building a baseline, within
the usual range) only those above zero are said, so a quiet week is not a list of zeros; the
agent's count is always said, as it is what the question is about, and the findings sentence says
its zeros, except when the topic's Articles read was skipped or failed: then it says only "1 finding
was written.", never "0 articles are waiting for review", and "What people decided wasn't checked
this time." follows. A topic with no decision says "made no decisions in the last 7 days: no new
clear scene was measured." and nothing more, since there is nothing on screen; "The decisions are on
screen." closes an answer only when the table has a row. When the attempts query was cut at its
1,000 rows and every decision of a topic was left out as unknown, the topic is said as "For Rail
access: Sydney, I couldn't tell which of its decisions in the last 7 days were kept.", followed by
"Only the newest 1000 log lines were read.", never as "no new clear scene was measured"; when some
of its decisions are known to come from failed ticks, the sentence keeps their count ("...were
kept; 3 came from ticks that failed, and were measured again."). An all-topics answer too long for one
sentence a topic says as many short ones as fit, and then the rest by what the table holds for them:
"4 more topics are on screen." only for topics with a row, "6 more topics made no decisions." and
"For 2 more topics, I couldn't tell which decisions were kept." for the others.

The page shows one row per decision: the topic, the site, the scene, how clear it was, OpenCV's
measures, which measures moved and from which input (the imagery, OpenStreetMap, or both), the
agent's verdict, what OpenCV re-measured for it, whether a finding was written, what became of its
article, and who measured it (backend, build, CPU). The "OpenCV re-measured" cell of the look_again
decision above reads, exactly as the code renders it:

> reach 800 m: 93% clear, built-up area served 64.1% (70.0% at the topic's settings)

It says "at the topic's settings", not the topic's own reach in metres, because that reach is in
the topic's adapter configuration, which this tool does not read (rule 5: a domain's settings
belong to its adapter, and the core names none of them); the evidence line carries only the
re-measurement's settings. The number beside it is the same scene measured at the topic's
settings, never the baseline. It adds the re-measurement's own coverage ("93% clear") because every `measured_` number
the look_again step returned is shown, and the coverage says the re-measurement read the same clear
part of the scene as the decision did, so the two shares are of the same pixels. With no topic, every topic that keeps decision evidence gets its
own sentence, the demo topic included, and no number is summed across topics.

**The result's shape.** `spoken`, `table` (12 columns), `rows` (one summary per decision),
`topics` (one entry per topic: `topic_id`, `label`, `decisions`, and its own `counts`, the 17 keys,
and `spend_aud`), `window`, `queries` (each `complete`, `cut` or `not_checked`), `dropped`,
`spend_note` and `as_of`. With a `topic`, the top-level `counts` and `spend_aud` are that topic's.
With no `topic` they are null: per-topic numbers are never summed, so a reader of the all-topics
answer reads each topic's from `topics[].counts` and `topics[].spend_aud`. Both are also null
when the decisions or attempts query did not finish. A topic whose Articles read was skipped or
failed has `articles_held`, `articles_approved` and `articles_rejected` null in its counts: not
checked, never zero. A scene answer adds `decision` (the summary)
and `other_sites` (below), and its `rows` are the trace's.

**One decision, step by step.** "Walk me through the latest Sydney scene" passes `scene` as
`"latest"` (or a scene id the table showed). The page shows the trace of
[the decision ring](#the-decision-ring), one row per step (what was measured, how it compared with
the baseline and what the gate did, each tool the agent called, its verdict, what the pipeline did,
what a person did), and Alexa+ hears it in fixed sentences:

> That Rail access: Sydney scene was 93% clear. OpenCV measured built-up area served 70.0%,
> stations 180. The served share and the station count moved past the topic's thresholds, from the
> imagery and the OpenStreetMap network, so the agent was asked. It re-measured the scene with
> OpenCV once, read the news once, and judged the change real. A finding was written. Its article
> was approved. The steps are on screen.

Facts only, as the trace states them: a re-measurement is said beside the same scene's numbers at
the topic's settings and never compared with the baseline. A measure is said only when it is
plausible (below a million once scaled; `SPOKEN_MEASURE_MAX`), otherwise "OpenCV measured it.";
a decision line whose coverage is outside 0 to 1 is dropped and counted, as any other line that
fails a check. A scene id decided for several sites of the topic (a tick decides every site in one
Sentinel-2 tile) shows the newest one's steps, ends with "That scene was decided for 2 sites; the
newest one's steps are on screen.", and lists the other sites' ids under `other_sites` for the page.

**How it reads the evidence.**

- **From the logs alone**: three fixed Logs Insights queries over the research-tick and admin-api
  log groups in the home Region, started together and waited for within one budget of 8 seconds
  (`WAIT_SECONDS`): the topic's `vision_decision` and `vision_triage` lines, the `research_tick`
  lines of the attempts that made a decision, and the `moderation_decision` lines. Then one short
  Query on the Articles table's topic index per topic with a finding written
  (`dynamo.list_articles_for_topic`: at most 200 articles, never a Scan, and only the fields the
  join uses: the id, status, who published it and when, and the lineage) for the article each
  finding fed, found by its lineage's `captured_at`. The one-topic answer makes one such read; the
  all-topics answer one per topic with a finding. A read skipped or stopped by the call's limit
  (below) is said as a failed one is: that topic's "what a person did" and its article counts are
  not checked (never "no article yet", never a count of what one page held). The `vision_site`
  lines are never read
  (24 a day per site would fill the 1,000-row cap within weeks), and no `stats` aggregate is used
  (it would count a failed attempt's lines twice).
- **One limit for the whole call** (`CALL_LIMIT_SECONDS`, 12 seconds from its start): Alexa+ gives
  up after roughly 15 seconds, and the tool answers through API Gateway, which gives up after 29
  (on the ask page the agent's own model calls share those 29), inside the ops-mcp function's 30.
  Every AWS call the tool makes is held to the time left before it: the Topics read, the access
  check (the two log groups' tags, and the account id their ARNs need), starting the queries,
  polling them, stopping the ones still running, and the Articles reads, whose own deadline
  (`DEADLINE_SECONDS`) is the same 12 seconds since they come last. Each request goes through a
  client built for it with a one-second connect, the time left as its read timeout (at least
  0.1 s) and one attempt, so a request that stalls is never sent twice; a request that would start
  at or after the limit is not made. A request Logs throttles is asked again above botocore instead
  (`logs.run_queries`' `recover_throttles`, which only this tool passes): a poll counts as still
  running and is asked again next round, within the wait, and a start is tried once more after one
  pause, each a new request with the time left, so the sum below still holds. The shared clients
  keep longer bounds (the Logs client's 3-second connect, 8-second read and two attempts; the
  pipeline's DynamoDB resource's 60-second timeouts and ten attempts), which no limit could hold.
  A step out of time ends as when it fails, never as a partial count or an exception: the access
  check refuses ("I couldn't reach CloudWatch Logs"), a query is not checked ("The decision logs
  didn't answer in time"), a stop is skipped (a query left running is cheap and ends by itself),
  an Articles read is not checked, and a Topics read that fails says "I couldn't read the topics
  just now". Normally the steps before the 8-second Logs wait take well under a second and the
  Articles reads have the rest of the 12 seconds. The worst case for a request that stalls is a
  sum: it starts before the limit and ends by its connect, its read timeout and its client's
  build, which comes after the time left is read: 12 + 1 + 0.1 + 0.2 = 13.3 seconds (0.2 s is the
  build measured for the first client of a service in a process; about 10 ms after). The name
  lookup each request makes before it connects is outside these timeouts, and each request makes
  its own (a client per request reuses no connection). A read timeout is an idle bound, the
  longest wait between two reads of the socket, so a request whose bytes keep trickling in is
  bounded only by the function's 30-second timeout; a hard end per request would need a thread,
  which a DynamoDB Query of at most 200 small items does not warrant.
- **By equality, never a pattern**: the only values put into a query are a topic id that passed
  `ID_PATTERN` and a scene id that passed `^[A-Za-z0-9_.-]{1,100}$`, each a quoted string compared
  with `=`, so `rail-sydney` never reads `rail-sydney-demo`'s lines.
- **Parsed, never trusted**: every line goes through `evidence.parse`, and a line is dropped and
  counted (`dropped`) when its topic is not the one asked for, its event not the query's, its
  decision code, stop, verdict, tool or moved measure not one the pipeline writes (the triage
  profiles' tools, the union of the adapters' `decision_vocabulary()`), its coverage is outside 0
  to 1, or it lacks the ids it is joined by.
- **Kept, reported and written**, as above: a decision of an attempt with `stored: false` or no
  `research_tick` line is not kept, counted under `not_kept` and never as a decision; a scene
  decided twice is shown once, the kept copy first.
- **Said when incomplete**: a query that returned the full 1,000 rows was cut ("only the newest
  1000 log lines were read", `complete: false`); a query that did not finish is "not checked this
  time", and the decision logs not answering in time is said as such, never as "no decisions".
- **Spend** is the triages' `cost_usd`, a failed attempt's included, in AUD at the project's
  fixed rate (`costing.USD_TO_AUD_RATE`), the currency of the Stats page.
- **Everything is code-derived**: codes, ids, times and numbers, with words from fixed tables and
  the adapters' vocabularies. There is no "(unverified)" column, so Alexa+ gets what the page
  gets. `spoken` stays under 120 words and never says a site's name or id, a scene id, the agent's
  reason, a news host or a build's hex.
- **The 90-day window** is a named exception to the log reader's 30-day lookback, local to this
  tool (`EVIDENCE_LOOKBACK_DAYS`): its queries are fixed, filter one topic by equality and read two
  groups, and the logs keep 90 days. `days` outside 1 to 90 is clamped, and an unreadable one is
  the default 7, as `log_review` treats its hours.
- **Nothing new to grant**: both log groups are among those the assistant may already query, and
  the Articles table and its indexes are among its tables; `infra/modules/ops-assistant/logs.tf`
  is unchanged, and a test pins it. The tool writes nothing.

**After the deploy**, ask the T1 question once on the page and once through Alexa+: both answers
should say the same counts, the page should show the decisions, and "walk me through the latest
Sydney scene" should put the trace on screen. If the answer is "The decision logs didn't answer in
time", run the runsheet's query (a) for the topic in the console to see whether Logs Insights
answers at all.

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
| The assistant and Alexa+ read the evidence from the logs (`vision_evidence`) | done; the T1 check to be run once after the deploy | the submission round's P2a pull request |
| The evidence from the running state, the agent's note on screen, the worker's runs | later | after the assistant's vision round |
| The export into git | later | the evaluation pull requests |
