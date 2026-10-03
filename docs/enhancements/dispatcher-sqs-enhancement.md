# Enhancement: one dispatcher and an SQS work queue instead of per-topic schedules

**Status:** proposed, not started · **Date:** 2026-10-03 · **Do it when:** about 15–20 topics, or as
soon as Bedrock (or GDELT/CoinGecko) starts throttling scheduled runs.
**Background:** finding 3 in [docs/risks/scaling-findings-01.md](../risks/scaling-findings-01.md).

## Today

Each topic owns two EventBridge Scheduler schedules, created and deleted by the Admin API
(`lambdas/common/scheduler.py`):

- `<env>-<topic>-research-tick`: a **heartbeat** that invokes `research_tick` directly. The tick
  already decides whether it is *due* (`lambdas/common/research_schedule.py`: the topic's
  `research_interval_hours`, else the pipeline config's, else 1 hour, measured from
  `last_research_at`).
- `<env>-<topic>-daily-cycle`: a `cron(...)` in the topic's timezone that starts the
  `daily-cycle` Step Functions state machine (retries, then the pipeline DLQ).

Production staggers them by hand so they don't start together:

| Topic | Daily cycle (Australia/Sydney) | Research (UTC) |
|---|---|---|
| github-trending | 09:00 | :01 past every 4th hour |
| finance-crypto-investing | 09:01 | :02 |
| tech-market-news | 09:02 | :03 |
| wow-forever | 09:03 | :04 |

That is fine at four topics. It stops working because:

- **Nothing limits concurrency.** A daily cycle can run up to 300 s, so one-minute offsets already
  overlap. At 40 topics about five cycles run at once, all sharing the Bedrock tokens-per-minute
  quota, the Lambda concurrency limit and the outside APIs' rate limits. A slow day makes runs
  longer and the overlap worse, and nothing pushes back.
- **The offsets are hand-kept.** Each new topic needs a free slot chosen by a person.
- **Two AWS resources per topic** must stay in step with a DynamoDB row; a failed Scheduler call
  leaves them out of step.

## Proposal

```
EventBridge Scheduler (one schedule, every 5 min)
        |
        v
  dispatcher Lambda ── reads Topics, decides what is due,
        |               claims it with a conditional write
        v
  SQS: bloggerbear-<env>-work  ──(redrive after N tries)──>  pipeline DLQ (existing)
        |
        v  event source mapping, maximum concurrency = 2..3
  worker Lambda
        ├─ job "research" -> research_tick logic (in-process)
        └─ job "daily"    -> StartExecution on the daily-cycle state machine
```

### 1. One dispatcher schedule

A single Terraform-managed schedule (every 5 minutes; see "Timing" below) invokes a new
`dispatcher` Lambda. All per-topic schedules are deleted.

### 2. The dispatcher only enqueues what is due

For each enabled topic it decides, from fields on the Topic row:

- **research:** the existing rule, unchanged: `research_schedule.is_due(now, last_research_at,
  interval)`.
- **daily:** the topic's local time (new fields `daily_time`, e.g. `"09:00"`, and
  `daily_timezone`, already stored) has passed today **and** `last_daily_dispatched_on` is not
  today's local date.

For each due job it first **claims** it with a conditional write on the Topic row (e.g.
`SET last_daily_dispatched_on = :today IF last_daily_dispatched_on <> :today`), then sends
`{"topic_id": "...", "job": "research" | "daily", "dispatched_at": "..."}` to SQS. The claim is
what stops two overlapping dispatcher runs from queueing the same work twice. If the send fails
after the claim, release the claim so the next dispatch retries it.

The dispatcher reads Topics with a scan; at hundreds of topics that is still small, but it can
become a `Query` on an `enabled` index once finding 1 (indexes) lands.

### 3. Workers drain the queue at a fixed concurrency

An SQS event source mapping with `scaling_config.maximum_concurrency` (2–3 to start) runs the
worker. **That one number becomes the knob that protects Bedrock and the source APIs**, however
many topics there are. Batch size 1 keeps a failure scoped to one job.

- **research:** call the research tick's logic in-process (or invoke the existing Lambda
  synchronously).
- **daily:** `StartExecution` on the existing state machine, so its retries and DLQ path are
  unchanged. Use an execution name like `<topic>-<local-date>`: Step Functions rejects a duplicate
  name, which makes a redelivered message harmless.

### 4. Failures

- SQS visibility timeout longer than the worker's timeout; `maxReceiveCount` 3, then the
  **existing** pipeline DLQ, so `dlq_handler` and `admin_cli failed-executions list` keep working.
  Its message shape will differ from a Step Functions failure; `dlq_handler` should recognise both.
- Alarms: queue age (`ApproximateAgeOfOldestMessage`) as the "falling behind" signal, plus the
  existing DLQ depth alarm.

### 5. Admin API and CLI

- Creating, updating or deleting a topic becomes a DynamoDB write only: no Scheduler calls.
- `topics trigger` can enqueue a job (same path as scheduled work) or keep invoking directly for
  "run it now".
- `research_cadence` / `daily_cadence` become `research_interval_hours` (exists) and `daily_time`
  (new). Migrate by parsing each topic's current `cron(m h ...)` into `daily_time`.

## Timing

- 9:00 becomes "within one dispatcher interval after 9:00, plus queue wait". With a 5-minute
  dispatcher and a concurrency of 3, four topics all start by about 9:05.
- An **hourly** dispatcher is possible and cheaper, but a topic set for 9:00 could then publish
  close to 10:00. 5 minutes costs about 8,600 invocations a month: well inside the free tier.

## Duplicates and idempotency

SQS is at-least-once, so a job can arrive twice:

- **research:** already safe. The due check uses `last_research_at`, and diff-first means a repeat
  with no change calls no model.
- **daily:** the Step Functions execution name (above) rejects a second start for the same day.
  The cycle itself also only writes from findings newer than the topic's last article.

## Migration plan

1. Add the queue, the worker and the dispatcher, with the dispatcher in **shadow mode**: it logs
   what it would enqueue and sends nothing. Compare its decisions with the real schedules for a few
   days.
2. Turn the dispatcher on for one topic (a `dispatch_mode = "queue"` flag on the Topic row) and
   delete that topic's two schedules.
3. Move the rest, then delete `common/scheduler.py`'s topic CRUD and the Scheduler permissions the
   Admin API no longer needs.
4. Keep the Terraform-managed fixed jobs (digest, reflection, stats rollover, Cost Explorer poll,
   musing feedback) as they are: they are single, global and already serialised.

## Costs and trade-offs

- **Adds:** one Lambda that wakes every 5 minutes, one queue, one event source mapping. Cents a
  month.
- **Removes:** two Scheduler resources per topic, per-topic Scheduler CRUD in the Admin API, and
  hand-kept offsets.
- **Gives up:** start times to the second. Gains backpressure, one concurrency knob, per-message
  retries, and a queue age that shows when the pipeline is falling behind.

## Tests to write

- Dispatcher: due/not-due for research and daily across timezones and DST changes; a claimed job
  is not enqueued twice; a failed send releases its claim; disabled topics are skipped.
- Worker: each job type; a duplicate daily message does not start a second execution.
- Terraform wiring: one dispatcher schedule, no per-topic schedules, `maximum_concurrency` set,
  the redrive policy points at the existing DLQ.
