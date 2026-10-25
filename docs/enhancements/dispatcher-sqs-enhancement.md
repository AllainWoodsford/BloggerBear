# Enhancement: one dispatcher and an SQS work queue instead of per-topic schedules

**Status:** being built as phase Q of [the Model Ranch spec](model-ranch-prompt-assets-evals.md) (2026-10-25); its step 3, removing per-topic scheduling, is superseded: schedule mode stays as the permanent undo · **First draft:** 2026-10-03 · **Revised:** 2026-10-15, for
the stagger, the pause and the vision stack · **Do it when:** about 15–20 topics (twelve
five-minute slots fill an hour), or as soon as Bedrock (or GDELT/CoinGecko) starts throttling
scheduled runs.
**Background:** the schedule limit in [project-plan.md, "Still open at scale"](../project-plan.md#still-open-at-scale).

This revision keeps the shape of the first draft and changes what the dispatcher reads and what
the worker runs, so that it builds on what is now in the Topics table and does not break the
vision adapters on the integration branch (#285, the stack #240–#254). Nothing here is built
yet; the **stagger and the pause it reads are** (the "Today" section).

## Today

Each topic owns two EventBridge Scheduler schedules, created and deleted by the Admin API
(`lambdas/common/scheduler.py`):

- `<prefix>-<env>-<topic>-research-tick`: a **heartbeat** that invokes `research_tick` directly.
  The tick already decides whether it is *due* (`lambdas/common/research_schedule.py`: the
  topic's `research_interval_hours`, else the pipeline config's, else 1 hour, measured from
  `last_research_at`).
- `<prefix>-<env>-<topic>-daily-cycle`: a `cron(...)` in the topic's timezone that starts the
  `daily-cycle` Step Functions state machine (retries, then the pipeline DLQ).

Since the stagger landed (docs/project-plan.md §11, "Staggered schedules by slot"), the minute
each schedule runs at is no longer hand-kept. Every topic has a **slot** (`stagger_slot`, from
1), the pipeline has one gap (`stagger_minutes`, 5 by default, in the `pipeline` config row),
and the Admin API writes the schedules at the slot's minute: the research heartbeat of slot *k*
at `k × stagger` past its hour, the daily cycle of every topic delayed past the hour's
heartbeats (`topics × stagger`) and then one slot apart. What was written is on the row
(`research_schedule`, `daily_schedule`), listed by `topics schedule`, and rewritten for every
topic by `topics reschedule`. A topic can also be **paused** (`paused`, `paused_at`): both
schedules are set DISABLED, and the research tick and the daily cycle refuse the topic whatever
invoked them.

| Topic (production, after `topics reschedule` with four topics) | Slot | Research (UTC) | Daily (Australia/Sydney) |
|---|---|---|---|
| finance-crypto-investing | 1 | :05 past every 4th hour | 09:25 |
| github-trending | 2 | :10 | 09:30 |
| tech-market-news | 3 | :15 | 09:35 |
| wow-forever | 4 | :20 | 09:40 |

That is fine at four topics, and good to about twelve. It stops working because:

- **Nothing limits concurrency.** A daily cycle can run up to 300 s, so five-minute offsets stop
  separating runs once a cycle is slow, and nothing pushes back on Bedrock's tokens-per-minute
  quota, the Lambda concurrency limit or the outside APIs' rate limits.
- **An hour has twelve five-minute slots.** Slot 13 lands on the same minute as slot 1. The gap
  can be shortened (`stagger_minutes` 3 gives twenty), but the minutes run out.
- **Two AWS resources per topic** must stay in step with a DynamoDB row; a failed Scheduler call
  leaves them out of step (`topics reschedule` repairs it, by hand).

## What the vision stack needs from this design

The integration branch (#285) changes the research tick in ways a dispatcher must respect. None
of them is a reason to change the tick; they are constraints on what runs it and how.

- **One research run per topic at a time.** A `keeps_running_state` adapter (`satellite_vision`,
  `rail_access`) stores its newest state at one fixed S3 key per topic on every tick, material or
  not, and reads it back as `previous_state`. Two ticks of the same topic running at once would
  race on that key. Per-topic schedules give this for free (one schedule, one invocation); a
  queue must give it explicitly: an **SQS FIFO queue with `MessageGroupId = topic_id`**, which
  delivers one message per group at a time.
- **The tick's 120 s, and its deadline.** The vision adapters budget their worker calls and the
  triage agent against the tick's start (`triage_deadline_seconds`, 85 s). The worker must give
  a research job the same 120 s the tick has today, and must not add its own waiting in front:
  invoke the research tick Lambda **synchronously** (`RequestResponse`) from the worker rather
  than re-implement the tick in the worker, so the environment the vision client needs
  (`VISION_WORKER_ARN`, `VISION_COOL_WORKER_ARN`, `VISION_ALLOWED_URL_PREFIXES`) and the IAM
  grant to invoke the worker across Regions stay on the one function Terraform already sets
  them on. The worker's own timeout is then the tick's plus a margin (150 s), and the queue's
  visibility timeout six times that, as AWS advises for Lambda consumers.
- **`force_manual_review` is the tick's and the cycle's business, not the dispatcher's.** The
  daily cycle holds those articles. The dispatcher only
  decides *when*, never *whether to publish*.
- **Figures and planner notes are on the Finding.** Nothing in the dispatch path touches
  Findings, so the figures path (#288) and the article page are unaffected.
- **The event shapes stay.** `{"topic_id": ...}` for the research tick (with `"force": true`
  only from an operator), `{"topic_id": ...}` for the state machine. A worker that passes those
  through unchanged breaks neither handler, whichever branch merges first.

## Proposal

```
EventBridge Scheduler (one schedule, every minute)
        |
        v
  dispatcher Lambda ── reads Topics, decides what is due this minute,
        |               claims it with a conditional write
        v
  SQS FIFO: <prefix>-<env>-work.fifo  ──(redrive after N tries)──>  pipeline DLQ (existing)
        |      MessageGroupId = topic_id: one job per topic at a time
        v  event source mapping, maximum concurrency = 2..3
  worker Lambda
        ├─ job "research" -> invoke research_tick synchronously (120 s, its own environment)
        └─ job "daily"    -> StartExecution on the daily-cycle state machine
```

### 1. One dispatcher schedule, every minute

A single Terraform-managed schedule invokes a new `dispatcher` Lambda every minute (about 43,000
invocations a month, inside the free tier; a 5-minute dispatcher would lose the slot minutes).
All per-topic schedules are deleted once the migration below is done.

### 2. The dispatcher reads the slot, the pause and the due rules

For each topic that is **not paused** it decides, from fields already on the Topic row:

- **its turn:** this minute is the topic's. The research turn is `(slot × stagger) mod 60`;
  the daily turn is its `daily_cadence` hour in `daily_timezone` plus `(topics + slot) ×
  stagger` minutes: exactly the minutes `common/stagger.py` writes into the schedules today, so
  `topics schedule` keeps meaning what it means (the dispatcher calls `effective_schedules` and
  compares the minute, rather than keeping a second rule).
- **research:** the existing rule, unchanged: `research_schedule.is_due(now, last_research_at,
  interval)`. With `stagger_minutes` 0 every minute is a turn and only `is_due` decides.
- **daily:** the daily turn has passed today **and** `last_daily_dispatched_on` is not today's
  local date.

For each due job it first **claims** it with a conditional write on the Topic row (for the
daily job `SET last_daily_dispatched_on = :today IF last_daily_dispatched_on <> :today`; for
research `SET research_claimed_at = :now IF research_claimed_at < :now - interval`), then sends
`{"topic_id": "...", "job": "research" | "daily", "dispatched_at": "..."}` to the queue with
`MessageGroupId = topic_id` and `MessageDeduplicationId = <topic_id>:<job>:<minute>`. The claim
is what stops two overlapping dispatcher runs from queueing the same work twice; the
deduplication id is SQS's own second guard. If the send fails after the claim, release the claim
so the next minute retries it.

A paused topic is skipped before any of this, and the handlers refuse it anyway (belt and
braces, as today). The dispatcher reads Topics with a scan; at hundreds of topics that is still
small, but it can become a `Query` on an `enabled` index once finding 1 (indexes) lands.

### 3. Workers drain the queue at a fixed concurrency

An SQS event source mapping with `scaling_config.maximum_concurrency` (2–3 to start) runs the
worker. **That one number becomes the knob that protects Bedrock and the source APIs**, however
many topics there are. The FIFO group keeps one job per topic in flight, so a slow vision tick
never overlaps with its own next heartbeat. Batch size 1 keeps a failure scoped to one job.

- **research:** invoke the existing research tick Lambda synchronously with the job's event and
  return when it does. The tick's own result (`paused`, `not_due`, `no_change`,
  `material_change`, `error`) is logged by the worker; `error` is not a retry (the tick never
  raises, by design), so the message is consumed.
- **daily:** `StartExecution` on the existing state machine with an execution name
  `<topic>-<local-date>`: Step Functions rejects a duplicate name, which makes a redelivered
  message harmless. The machine's retries and DLQ path are unchanged.

### 4. Failures

- SQS visibility timeout longer than the worker's timeout; `maxReceiveCount` 3, then the
  **existing** pipeline DLQ, so `dlq_handler` and `admin_cli failed-executions list` keep
  working. Its message shape will differ from a Step Functions failure; `dlq_handler` recognises
  both by the presence of `job`.
- Alarms: queue age (`ApproximateAgeOfOldestMessage`) as the "falling behind" signal, plus the
  existing DLQ depth alarm, in `infra/modules/observability`.

### 5. Admin API and CLI

- Creating, updating or deleting a topic becomes a DynamoDB write only: no Scheduler calls.
  `stagger_slot`, `stagger_minutes`, `paused` and the `topics schedule` listing keep their
  meaning; `research_schedule` and `daily_schedule` on the row become what the dispatcher
  computes rather than what was written to Scheduler, and `topics reschedule` only reassigns
  slots.
- `topics pause` and `topics resume` become a row write only; the dispatcher reads `paused`.
- `topics trigger` enqueues a job (same path as scheduled work) for "run it in turn", or keeps
  invoking directly for "run it now", as it does today.
- `research_cadence` / `daily_cadence` become `research_interval_hours` (exists) and the daily
  hour (`daily_cadence`'s hour field, which `common/stagger.py` already parses).

## Timing

- A slot's minute is kept exactly: slot 3 is dispatched at :15, and runs as soon as a worker is
  free, so "9:30" becomes "9:30 plus queue wait". With a concurrency of 3, four topics all start
  within their own minute.
- Beyond twelve topics the slots wrap; the queue then serialises what the minutes cannot
  separate, which is the point of the change.

## Duplicates and idempotency

SQS FIFO is exactly-once within its deduplication window and at-least-once beyond it:

- **research:** safe. The due check uses `last_research_at`, diff-first means a repeat with no
  change calls no model, and the FIFO group means a repeat never runs beside the original (the
  running-state key is never written by two ticks at once).
- **daily:** the Step Functions execution name (above) rejects a second start for the same day.
  The cycle itself also only writes from findings newer than the topic's last article.

## Migration plan (non-breaking, one step at a time)

1. Add the queue, the worker and the dispatcher, with the dispatcher in **shadow mode**: it logs
   what it would enqueue (topic, job, slot minute) and sends nothing. Compare its decisions with
   the real schedules for a few days, with `topics schedule` beside the log.
2. Turn the dispatcher on for one topic (a `dispatch_mode = "queue"` flag on the Topic row, read
   by the dispatcher and by the Admin API's schedule writer) and delete that topic's two
   schedules. Pick a plain feed topic first, then a vision topic once #285 is in, to see the FIFO
   group hold.
3. Move the rest, then delete `common/scheduler.py`'s topic CRUD and the Scheduler permissions
   the Admin API no longer needs.
4. Keep the Terraform-managed fixed jobs (digest, reflection, stats rollover, Cost Explorer poll,
   musing feedback) as they are: they are single, global and already serialised.

Each step leaves the previous one working, and the handlers are unchanged throughout: the pause
flag and the due check live in them, not in whatever invokes them.

## Costs and trade-offs

- **Adds:** one Lambda that wakes every minute, one FIFO queue, one event source mapping. Cents a
  month (the dispatcher is inside the free tier; FIFO requests are priced like standard ones at
  this volume).
- **Removes:** two Scheduler resources per topic, per-topic Scheduler CRUD in the Admin API, and
  the twelve-slot ceiling.
- **Gives up:** nothing a reader sees; start times move from the slot's minute to the slot's
  minute plus queue wait. Gains backpressure, one concurrency knob, per-message retries, one job
  per topic at a time, and a queue age that shows when the pipeline is falling behind.

## Tests to write

- Dispatcher: a topic's turn from its slot and `stagger_minutes` matches `common/stagger.py`'s
  schedules; due/not-due for research and daily across timezones and DST changes; a paused topic
  is never enqueued; a claimed job is not enqueued twice; a failed send releases its claim.
- Worker: each job type; a research job's event reaches the tick unchanged; a duplicate daily
  message does not start a second execution; two messages for one topic are never in flight
  together (the FIFO group).
- Terraform wiring: one dispatcher schedule, no per-topic schedules, `maximum_concurrency` set,
  a FIFO queue with content-based deduplication off (the id is explicit), the redrive policy
  pointing at the existing DLQ, and the worker's environment carrying what the vision client
  needs only if the tick is ever run in-process (it is not, in this design).
