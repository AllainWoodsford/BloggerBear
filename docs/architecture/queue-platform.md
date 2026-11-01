# The queue platform: the dispatcher, the work queue and the cutover

How a topic's research heartbeat and daily article are started once it no longer has schedules of
its own: one dispatcher a minute reads every topic's cadence, claims the work that is due on the
topic's row, sends research jobs to a FIFO work queue and starts the daily runs itself. Then how a
topic is watched in shadow mode before it moves, how it moves (the cutover and its pre-flight),
how it moves back, and what a manual run, a pause and a restore do to a topic on the queue.

It exists because two EventBridge Scheduler schedules per topic, each at a hand-set stagger slot,
stop fitting an hour past about twelve topics, and nothing capped how many research ticks ran at
once. The queue platform makes the number of topics a matter of rows, not schedules: one schedule
for the dispatcher, one queue, and one knob (`worker_concurrency`) for how many research jobs run
at once however many topics there are. The two schedules stay as the permanent undo: a topic is
moved one at a time, after shadow mode has watched it, and can be moved back in one command.

The settings and their defaults are in
[configuration.md](../configuration.md#the-queue-platform-prompts-and-evals); moving topics in
production, and back, is in the
[production runsheet](../production-runsheet.md#moving-topics-to-the-queue-and-back); the alarms
are in [observability.md](../observability.md#the-queue-platforms-and-evals-alarms). The design,
and the reasons behind it, are in
[model-ranch-prompt-assets-evals.md](../enhancements/archive/model-ranch-prompt-assets-evals.md)
(sections 4.7, 4.8 and 5.1), which builds on
[dispatcher-sqs-enhancement.md](../enhancements/archive/dispatcher-sqs-enhancement.md). This page does not
repeat them.

**In the assistant.** This page has no `feature` entry yet. Ask about its parts by name instead:
`architecture(name="dispatcher")` (the function, or with `kind="schedule"` its schedule), `"work"`
and `"work-dlq"` (the queues), `"daily-cycle"` (the state machine) or `"topic-schedules"` (the two
schedules a topic on them keeps), or about the layer that holds it (`orchestration`). Where evals
are on, `evals status` shows the work queue's depth and the age of its oldest job, and how many
topics are in each dispatch mode.

Resource names below use `<prefix>` (your `unique_name_prefix`) and `<env>` (`dev` or
`production`). Both environments build it from one module,
[`infra/modules/work-queue/`](../../infra/modules/work-queue/), always instantiated.

## Contents

- [The steps](#the-steps)
- [The tick](#the-tick)
- [Reading a cadence: the cron matcher](#reading-a-cadence-the-cron-matcher)
- [Claims](#claims)
- [A research job on work.fifo](#a-research-job-on-workfifo)
- [A daily run](#a-daily-run)
- [Shadow mode, as built](#shadow-mode-as-built)
- [The cutover and its pre-flight](#the-cutover-and-its-pre-flight)
- [Cutover day](#cutover-day)
- [The way back](#the-way-back)
- [Manual runs, pausing, and a restore](#manual-runs-pausing-and-a-restore)
- [Flows](#flows)
- [When something fails](#when-something-fails)
- [Where state lives](#where-state-lives)
- [Permissions](#permissions)
- [Where in the code](#where-in-the-code)
- [Design history](#design-history)

```mermaid
flowchart LR
    operator(["Operator<br/>admin CLI"])
    subgraph tickbox["1-4. Every minute"]
        sched["EventBridge Scheduler<br/>dispatcher, rate(1 minute)"]
        disp["Lambda dispatcher<br/>cron matcher, claims"]
        config[("model-config<br/>pipeline row")]
        topics[("topics<br/>dispatch_mode, claims,<br/>shadow record")]
    end
    subgraph researchbox["5-6. Research jobs"]
        work["SQS work.fifo<br/>one group per topic"]
        tick["Lambda research-tick"]
        wdlq["SQS work-dlq.fifo"]
        dlqfn["Lambda dlq-handler"]
        failed[("failed-executions")]
    end
    subgraph dailybox["7. Daily runs"]
        sfn["Step Functions<br/>daily-cycle state machine"]
        cycle["Lambda daily-cycle"]
    end
    old["EventBridge Scheduler<br/>two schedules per topic<br/>(schedule mode)"]
    admin["Lambda admin-api<br/>9-11. cutover, manual runs,<br/>the way back"]

    sched --> disp
    disp <--> config
    disp <--> topics
    disp -- "research: SendMessage" --> work
    work -- "one job at a time,<br/>worker_concurrency at most" --> tick
    work -- "3 receives" --> wdlq --> dlqfn --> failed
    disp -- "daily: StartExecution" --> sfn --> cycle
    disp -. "shadow: ListExecutions" .-> sfn
    old -. "a topic not yet moved" .-> tick
    old -.-> sfn
    operator --> admin
    admin --> topics & old & work & sfn
```

## The steps

| # | Step | What happens | Where in the code / infra |
|---|---|---|---|
| 1 | **The tick** | Every minute a schedule invokes the dispatcher. It reads the `pipeline` row and every topic, skips a paused topic, and considers each topic on the queue, and each topic still on its schedules while shadow mode is on ([in detail](#the-tick)). | [`lambdas/dispatcher_handler.py`](../../lambdas/dispatcher_handler.py); `Dispatcher.tick` in [`lambdas/common/dispatch.py`](../../lambdas/common/dispatch.py); `aws_scheduler_schedule.dispatcher` (`<prefix>-<env>-dispatcher`) |
| 2 | **Reading the cadence** | The topic's two expressions, as the stagger writes them for the Scheduler, are parsed by the cron matcher. One it cannot read is logged and the topic is skipped, never guessed at ([in detail](#reading-a-cadence-the-cron-matcher)). | `effective_cadences`, `parse_cadences` in `dispatch.py`; [`lambdas/common/cron_match.py`](../../lambdas/common/cron_match.py); `effective_schedules` in [`common/stagger.py`](../../lambdas/common/stagger.py) |
| 3 | **Research is due** | The research cadence has a turn since the last tick and the topic is due by the research tick's own rule. The topic is claimed on its row, then one job goes to `work.fifo` ([in detail](#claims)). | `research_turn` in `cron_match.py`; `is_due` in [`common/research_schedule.py`](../../lambdas/common/research_schedule.py); `claim_research`, `research_message`, `research_dedup_id` in `dispatch.py` |
| 4 | **The daily run is due** | Today's first daily turn has passed in the topic's zone and the topic has not been dispatched today. The day is claimed, then the daily-cycle state machine is started under a name made from the topic, the date and the claim number, at most `daily_starts_per_tick` a minute ([in detail](#a-daily-run)). | `daily_turn_passed` in `cron_match.py`; `claim_daily`, `execution_name`, `_start_daily` in `dispatch.py` |
| 5 | **The research job** | An event source mapping hands the research tick one job at a time, at most `worker_concurrency` at once. The tick runs at the minute the dispatcher found the topic due, not the minute it left the queue ([in detail](#a-research-job-on-workfifo)). | `aws_lambda_event_source_mapping.research_tick`; `_queue_record`, `_stamp` in [`lambdas/research_tick_handler.py`](../../lambdas/research_tick_handler.py) |
| 6 | **Dead letters** | A job received three times without finishing goes to `work-dlq.fifo`, whose consumer writes a `research_job` row to FailedExecutions ([in detail](#when-something-fails)). | `aws_sqs_queue.work_dlq`, `aws_lambda_event_source_mapping.work_dlq`; [`lambdas/dlq_handler.py`](../../lambdas/dlq_handler.py) |
| 7 | **The daily run** | The state machine runs as it does from a schedule: `NameTheRun` adds the execution's id, the daily cycle uses it as its run id, and Step Functions' retries and the pipeline dead-letter queue are unchanged. | `aws_sfn_state_machine.daily_cycle` in each environment's `main.tf`; `_run_id` in [`lambdas/daily_cycle_handler.py`](../../lambdas/daily_cycle_handler.py) |
| 8 | **Shadow mode** | A topic still on its schedules is never sent or started. The tick records the turns it would have dispatched on the row and counts where the schedules did otherwise ([in detail](#shadow-mode-as-built)). | `_shadow_research`, `_judge_research`, `_shadow_daily`, `_daily_ran` in `dispatch.py` |
| 9 | **The cutover** | `topics update <id> --dispatch-mode queue`: a pre-flight, a confirmation, the two schedules deleted, then the row written with `dispatch_mode: queue` and, after today's turn, today's daily claim ([in detail](#the-cutover-and-its-pre-flight)). | `_move_to_queue`, `_update_topic` in [`lambdas/admin_api_handler.py`](../../lambdas/admin_api_handler.py); `cutover_refusals`, `cutover_day` in `dispatch.py` |
| 10 | **Manual runs** | `topics trigger` on a topic on the queue sends a research job of its own to `work.fifo`, or starts the state machine under a manual name, rather than invoking a Lambda ([in detail](#manual-runs-pausing-and-a-restore)). | `_trigger_on_the_queue` in `admin_api_handler.py`; `manual_research_dedup_id`, `manual_execution_name` in `dispatch.py` |
| 11 | **The way back** | `topics update <id> --dispatch-mode schedule`, or `topics reschedule --dispatch-mode schedule` for every topic on the queue: the row says schedule first, then its two schedules are written ([in detail](#the-way-back)). | `_update_topic`, `_reschedule_topics`, `_apply_topic_schedules` in `admin_api_handler.py` |
| 12 | **The tick's record** | Each tick prints one `dispatcher_tick` evidence line with what it did, and stores its time as `last_tick` for the next tick's window. The heartbeat alarm counts the lines that say `ok`. | `handler` in `dispatcher_handler.py`; `dispatcher_tick` in [`common/evidence.py`](../../lambdas/common/evidence.py); `aws_cloudwatch_metric_alarm.dispatcher_heartbeat` |

## The tick

One Lambda, `<prefix>-<env>-dispatcher` (60 seconds), invoked once a minute by
`<prefix>-<env>-dispatcher` (`rate(1 minute)`). A tick:

1. reads the `pipeline` row of `<prefix>-<env>-model-config` and scans `<prefix>-<env>-topics`;
2. goes through the topics in topic-id order. A paused topic is skipped. A topic whose
   `dispatch_mode` is `queue` is dispatched for real. A topic on its schedules (`dispatch_mode`
   unset or `schedule`) is only ever shadowed, and only while `dispatch_shadow` is on;
3. for each topic considered, works out its research turn and its daily turn (below), and sends,
   starts or records accordingly;
4. writes `last_tick` (this tick's time) on the `pipeline` row;
5. prints `dispatcher_tick` with `considered`, `dispatched` (research jobs sent), `started` (daily
   runs started) and `skipped_unparsable`, its `outcome` (`ok` or `error`) and how long it took.

Two settings on the `pipeline` row, changed with `pipeline-config set` and read every tick:

| Setting | Default | What it does |
|---|---|---|
| `dispatch_shadow` | `true` | Whether topics still on their schedules are shadowed. Off, the tick considers only topics on the queue. |
| `daily_starts_per_tick` | 3 (1 to 10) | How many daily runs one tick may start. Once the count is reached, a later topic whose turn has passed waits for the next minute: its turn stays passed and its day unclaimed. |

A value that is not valid is ignored for its default, with a log line, so a hand edit never stops
the dispatcher.

**It raises,** unlike the pipeline's other handlers. A tick that could not do its job (the Topics
scan, a send to the queue, a throttled `StartExecution`, or the `last_tick` write) is printed and
raised again, so the function's errors alarm fires. One topic's own trouble (a zone the runtime
does not know, a field missing) is printed as `dispatcher: topic_id=... skipped: ...` and the tick
goes on to the next topic.

## Reading a cadence: the cron matcher

The dispatcher reads exactly what the stagger would write to the Scheduler for the topic
(`stagger.effective_schedules`, from the topic's cadences and slot, the pipeline row's
`stagger_minutes` and how many topics there are), so a topic fires at the minute its schedule
would have. [`cron_match.py`](../../lambdas/common/cron_match.py) accepts only the shapes the
stagger emits or passes through:

| Field | Accepted |
|---|---|
| Minutes | a whole number, `*/N`, or a list (`0,30`) |
| Hours | `*`, a whole number, `*/N`, `a/N`, a list (`9,21`), a range (`9-17`, or `22-2` across midnight), a range with a step |
| Day of month | `*`, `?`, a whole number, or `1/N` |
| Month, year | `*` |
| Day of week | `*`, `?`, a list of names (`MON,WED,FRI`) or a range of names (`MON-FRI`) |
| A rate | `rate(N minutes|hours|days)` |

Anything else (`at(...)`, a month or a year, `#`, `L` or `W`, a weekday range that wraps, both day
fields set) is refused. The tick logs
`dispatcher: cadence unparsable topic_id=... expression=...`, counts it in `skipped_unparsable` and
moves on. The admin API refuses such a cadence for a topic on the queue, and the pre-flight
refuses to move a topic that has one.

**Turns.**

- **Research, a cron.** The minutes the cron names, matched over the window since the last tick,
  `(last_tick, now]`, at most a day back. A tick that failed costs nothing: the next one looks back
  to the last one that finished.
- **Research, a rate.** Every minute is a turn, and the research tick's own due rule (`is_due`:
  the topic's research interval since `last_research_at`, less 5 minutes) decides.
- **Daily.** A daily `rate(...)` is refused. A cron's daily turn has *passed* once today's clock,
  as an `(hour, minute)` pair in the topic's `daily_timezone`, has reached the first turn it names
  for today. Comparing clock pairs is what makes daylight saving behave: on the spring-forward
  morning a 02:30 turn, which does not exist, fires at 03:00; on the fall-back morning 02:30 comes
  round twice, and the day's claim stops the second.
- **A daily cron with several hours** (`9,21`, `*/4`) dispatches **once a day**, at the first
  turn. The Scheduler fired at each; this is a change of behaviour, on purpose.

## Claims

A claim is a conditional write on the topic's row that stops two overlapping ticks queueing the
same work twice. The expressions are constants in `dispatch.py`, and tests hold them word for word
to the design.

| Claim | Update | Only when |
|---|---|---|
| Daily | `SET last_daily_dispatched_on = :today ADD daily_claim_n :one` | `last_daily_dispatched_on` is missing or is not today (the topic's local date) |
| Research | `SET research_claimed_at = :now` | `research_claimed_at` is missing or older than one research interval, less the same 5 minutes `is_due` allows, so a topic due at :05 is also claimable at :05 |

Both also require the row to exist, so a claim never recreates a deleted topic. A send or start
that fails after its claim **releases** it (the field put back as it was, or removed), but only
while the row still holds this tick's value. `daily_claim_n` is never put back: the name it
numbered may already have reached Step Functions, and an execution name is never reused.

## A research job on work.fifo

`<prefix>-<env>-work.fifo` is a FIFO queue with content-based deduplication off:

- **The body:** `{"topic_id": ..., "job": "research", "force": false, "due_checked_at": <the
  tick's time>, "dispatched_at": <the same>}`.
- **The group:** the topic id, so one topic's jobs run one at a time and in order.
- **The deduplication id:** `<topic_id>:research:<yyyy-mm-ddTHH:MM>`, SQS's own guard against the
  same minute sent twice.
- **Visibility:** 720 seconds, six times the research tick's 120-second timeout. Three receives
  without finishing send a job to `<prefix>-<env>-work-dlq.fifo`.

The event source mapping hands the research tick one job per invocation (`batch_size` 1), with
at most `worker_concurrency` jobs running at once (the Terraform variable, default 2, the least
SQS accepts). The research tick reads the record's body as it reads a direct invoke's, ignores
`job`, and runs **at `due_checked_at`**: that time decides its own due check and is the
`last_research_at` a successful fetch stamps, never the time it ran. The stamp never moves
backwards (the later of the stored value and `due_checked_at`). Otherwise a topic behind a busy
queue would be stamped late, found not due at its next turn, and researched only every second
heartbeat. A stale job is harmless for the same reason. A record whose `due_checked_at` cannot be
read runs at now, as a direct invoke does.

The research tick never raises, so a job that fails cleanly is answered and deleted like any
other; only a timeout or a crash leaves it to be received again.

## A daily run

There is no message for a daily run. The dispatcher claims the day, then calls `StartExecution` on
the daily-cycle state machine with:

- **the name** `<slug>-<local date>-<n>`: the slug is the topic id lower-cased, every character
  outside `[a-z0-9-]` replaced by `-`, at most 40 characters; the date is today in the topic's zone;
  `n` is the topic's claim count (`daily_claim_n` after the claim);
- **the input** `{"topic_id": ...}`.

The name is what makes a repeat harmless: Step Functions refuses a second execution of the same
name (`ExecutionAlreadyExists`), which the dispatcher counts as consumed, with a log line and no
retry. The same name while the first is still running, with the same input, is answered as a
success. From there the run is the one a schedule starts: `NameTheRun` adds `$$.Execution.Id`, the
daily cycle's `already_written` check and the state machine's retries work unchanged
([article research](article-research.md#runs-that-write-nothing-and-runs-that-fail)).

A throttled start (`ThrottlingException`, `TooManyRequestsException`, `ExecutionLimitExceeded`)
releases the claim and raises, so the tick fails and the next tick tries again.

## Shadow mode, as built

While `dispatch_shadow` is on, every unpaused topic still on its schedules is shadowed: the tick
decides as it would for a topic on the queue, sends and starts nothing, and keeps a record on the
row that the cutover's pre-flight reads. It never changes what the topic's schedules do.

**Research.** At a research turn, the turn is recorded as `shadow_last_research_dispatch_at` when
the live path would have claimed it (the topic due, and no record younger than the claim's
expiry), or when the schedule's own research tick, firing in the same minute, has already stamped
`last_research_at` within 5 minutes of now. A recorded turn is judged once, **5 minutes after the
turn**, by the tick whose window holds that moment: a mismatch when `last_research_at` is missing
or more than 5 minutes from the turn. A stamp later than that is a run-now made since the turn,
which replaced the turn's evidence, so the turn is left unjudged rather than counted. A research
cadence that is a `rate(...)` is recorded, once an interval, but **never judged**: the Scheduler's
firing minute for a rate cannot be worked out from the expression.

A heartbeat that drifts more than 5 minutes off its computed minute is counted once, at the first
turn after the drift began. From then on the topic is not due at the computed minute, so those
turns are neither recorded nor judged. One mismatch is enough to hold the cutover, and a drift that
began before shadow mode did leaves no research turn recorded at all, which the pre-flight refuses
too.

**Daily.** Once a day, when the daily turn passes, the turn is recorded as
`shadow_last_daily_dispatch_at`, and the previous recorded day is checked: did an execution of the
daily-cycle state machine with this topic's id in its input start on that local date? The check
reads the executions newest first (`ListExecutions`, then `DescribeExecution` for each one that
started that day), up to 10 pages of 100, and stops at the first execution older than the day.

- It ran: the day counts in `shadow_checked_days`.
- It did not: the day counts in `shadow_checked_days` and adds one to `shadow_mismatches`.
- The check could not answer (Step Functions refused, or the pages ran out): the day counts in
  `shadow_unchecked`, never as a match, so a missing grant can never pass as zero mismatches.

The first shadow day has nothing before it to compare, so it adds to no count.

**The counts are whole-history.** `shadow_mismatches`, `shadow_unchecked` and
`shadow_checked_days` only ever grow: nothing in the code resets them, so one old mismatch, or one
from before a cadence change, holds a topic's cutover until it is cleared on the row by hand.
`topics schedule` shows the record under each topic as
`N mismatches · N unchecked days · N checked days`, with the last turns recorded.

## The cutover and its pre-flight

`topics update <id> --dispatch-mode queue` moves one topic. Moving to the queue is never done in
bulk: each topic has its own pre-flight and its own confirmation.

1. **The pre-flight** (`dispatch.cutover_refusals`). The admin API answers 400 with every reason it
   finds, in plain words, before anything is deleted:
   - the topic is paused (shadow mode does not watch a paused topic);
   - the dispatcher cannot read its research or daily cadence, judged both as the stagger writes
     it now and as stored (the dispatcher falls back to the stored text whenever the topic's
     offset comes to 0, so a daily `rate(...)` is refused whatever the stagger is now), or its
     zone;
   - its slug has no letter or digit, or another topic has the same slug (their daily runs would
     take each other's names);
   - its id cannot be the work queue's message group id (letters, digits and punctuation, short
     enough for the longest deduplication id);
   - shadow mode has counted a mismatch, has a day it could not check, has not yet checked a full
     day (its first check runs at its second shadow daily turn), or, for a cron research cadence,
     has recorded none of its research turns. A topic whose research cadence is a `rate(...)` is
     judged by its daily record alone.
2. **The confirmation.** Without `confirm: true` the API answers 409 `confirm_required`, naming the
   two schedules it will delete. The CLI asks first, unless `--yes`.
3. **The schedules are deleted,** before the row says queue, so a failure in between leaves the
   topic running nowhere, and says so, but never on both paths at once (two articles in a day). A
   delete that fails answers 502 and the topic stays on its schedules.
4. **The row** is written with `dispatch_mode: queue` and, when today's turn has passed, today's
   daily claim ([cutover day](#cutover-day)). The schedules are then deleted once more as the row
   is applied, and none are found.

From the next minute the dispatcher runs the topic. A topic on the queue keeps its cadences and
its slot, and its row keeps recording the expressions the dispatcher reads (`research_schedule`,
`daily_schedule`). An update to a topic on the queue is checked against the matcher, with the
same 400 as the pre-flight's cadence reason.

The API also takes `dispatch_mode: queue` when a topic is created (the CLI has no flag for it).
The same checks apply, bar the shadow record, which a new topic cannot have, and the topic is
given no schedules.

## Cutover day

The Scheduler may already have run today's daily cycle when a topic moves. Two things stop a
second run:

- **The cutover-day claim.** When today's daily turn has already passed in the topic's zone, the
  write that sets `dispatch_mode` also sets `last_daily_dispatched_on` to today
  (`dispatch.cutover_day`). `daily_claim_n` is left as it is, so tomorrow's run takes the next
  number: a topic moved after its turn starts nothing today and starts `<slug>-<tomorrow>-<n>`
  tomorrow. A topic created on the queue after today's turn is given the same claim.
- **A same-day shadow record counts as dispatched.** A live topic never writes a shadow field, so a
  `shadow_last_daily_dispatch_at` dated today can only mean the Scheduler owned today; the live
  path treats it as today's dispatch.

## The way back

Schedule mode is the permanent undo, for one topic or all of them:

- **One topic:** `topics update <id> --dispatch-mode schedule`.
- **Every topic on the queue:** `topics reschedule --dispatch-mode schedule`, which answers
  `moved_to_schedule` with the topics it moved (`--dry-run` shows them and writes nothing).

Either way, the row says `schedule` **before** the two schedules are written, so the dispatcher has
let go of the topic first and it never runs on both paths. The same write drops the topic's
`shadow_last_daily_dispatch_at`, which dates from before the cutover: kept, its first shadow daily
check would ask about a day too far back for the pages it reads, and count an unchecked day that
would hold the next cutover for good. A topic whose schedules then fail to be written is off the
queue with nothing running it: the answer says so (502 for one topic, an error on its row for the
bulk undo), and the same command run again writes them.

## Manual runs, pausing, and a restore

**A manual run** (`topics trigger`) on a topic on the queue goes the way the dispatcher's would, so
it waits its turn behind the topic's other work rather than racing it:

- **research_tick:** a research job on `work.fifo` with `force: true` ("check now"), in the
  topic's group, with a deduplication id of its own, `<topic_id>:research:manual:<token>`, so SQS
  never drops it as the dispatcher's job of the same minute or the last manual one;
- **daily_cycle:** the state machine started as `<slug>-<local date>-manual-<HHMMSS>` (the topic's
  zone), with `force` passed on when asked for. Two in the same second share a name: the second is
  refused with a 409.

Neither touches the dispatcher's claims. The topic's own turn still comes, and the daily cycle's
already-written check holds. `network_prefetch` still invokes the research tick directly. With no
work queue in the deployment (`WORK_QUEUE_URL` empty), a manual research job answers 500.

**Pausing.** The dispatcher skips a paused topic, and a paused topic's handlers refuse it whatever
invoked them. A topic on the queue has no schedules to disable. A queue topic resumed after its
daily turn has passed starts today's run at the next tick, if today is not already claimed: the
Scheduler's disabled schedule, by contrast, would have missed the day.

**A restore** (`backup restore`) writes each topic's row as the snapshot holds it, then, in its
schedules phase, deletes the schedules of every restored row that says queue (it may have gone
back to them after the snapshot was taken). The row's `last_daily_dispatched_on` is the
snapshot's, so a snapshot from before today's run lets the dispatcher start a second run today.

## Flows

**Research on the queue.** Dispatcher tick → `cron_match` finds a turn in `(last_tick, now]` →
`is_due` → claim `research_claimed_at` → `SendMessage` to `work.fifo` (group the topic id,
deduplicated by the minute) → event source mapping (one job, at most `worker_concurrency` at once)
→ research tick at `due_checked_at` → `no_change` or a Finding, `last_research_at` stamped no
earlier than before. Loss points: a failed send releases the claim and fails the tick (alarm); a
job that times out three times reaches `work-dlq.fifo` → `dlq_handler` → a `research_job` row in
FailedExecutions.

**A daily article on the queue.** Dispatcher tick → the daily turn has passed in the topic's zone →
not dispatched today → under `daily_starts_per_tick` → claim `last_daily_dispatched_on`,
`daily_claim_n` → `StartExecution` named `<slug>-<date>-<n>` → `NameTheRun` adds the execution id →
daily cycle (`_run_id` the execution id; `already_written` check) → the article
([article research](article-research.md)). Loss points: `ExecutionAlreadyExists` (consumed, no
retry); a throttled start releases the claim and fails the tick; a run that fails is retried by the
state machine and then reaches the pipeline dead-letter queue, as from a schedule.

**A shadowed topic.** Dispatcher tick → research turn recorded, judged 5 minutes later against
`last_research_at` → daily turn recorded, the previous recorded day checked against the state
machine's executions → counts on the row → `topics schedule` → the pre-flight.

**The cutover.** `topics update <id> --dispatch-mode queue` → pre-flight (400) → 409
`confirm_required` → the CLI asks → the schedules deleted → the row written with
`dispatch_mode: queue` and the cutover-day claim → the next tick runs it.

## When something fails

| What failed | What you see | What to do |
|---|---|---|
| The dispatcher has stopped, or fails every minute | `<prefix>-<env>-dispatcher-heartbeat`: fewer than one `ok` tick in each of three 5-minute periods (missing data counts as breaching); the function's errors alarm for a tick that raised | Read `/aws/lambda/<prefix>-<env>-dispatcher` for the tick's error. A topic on the queue is not run meanwhile. To move every topic back: `topics reschedule --dispatch-mode schedule` |
| Jobs wait too long | `<prefix>-<env>-work-queue-age`: the oldest job in `work.fifo` more than 900 seconds old | Look for the research tick's errors or throttles; raise `worker_concurrency` if the queue is simply long |
| A research job could not finish | `<prefix>-<env>-work-dlq-messages` and `-work-dlq-received`; a `research_job` row in `failed-executions list`; the DLQ handler's log line `research job for topic_id=... failed` | Read the research tick's log for the timeout or crash. Nothing replays the job: the topic's next turn sends a new one |
| A daily run failed | As from a schedule: the pipeline dead-letter queue, `failed-executions list`, the daily cycle's alarms | [Article research](article-research.md#runs-that-write-nothing-and-runs-that-fail) |
| A cadence the dispatcher cannot read | `dispatcher: cadence unparsable topic_id=...` every minute; `skipped_unparsable` in `dispatcher_tick` | Change the cadence (`topics update`), or move the topic back to its schedules |

The dispatcher's errors and throttles alarms come from the observability module, like every
pipeline Lambda's; the other four are the work-queue module's own. Each emails the alerts topic
([observability.md](../observability.md#the-queue-platforms-and-evals-alarms)).

## Where state lives

| What | Where |
|---|---|
| Whether a topic is on the queue | `dispatch_mode` on its row of `<prefix>-<env>-topics` (unset means its schedules) |
| The claims | `last_daily_dispatched_on`, `daily_claim_n` and `research_claimed_at` on the topic's row |
| The shadow record | `shadow_last_research_dispatch_at`, `shadow_last_daily_dispatch_at`, `shadow_mismatches`, `shadow_unchecked` and `shadow_checked_days` on the topic's row |
| The expressions the dispatcher reads | `research_schedule` and `daily_schedule` on the topic's row, as the stagger writes them |
| The settings and the last tick | `dispatch_shadow`, `daily_starts_per_tick` and `last_tick` on the `pipeline` row of `<prefix>-<env>-model-config` |
| Jobs waiting | `<prefix>-<env>-work.fifo`; those that could not finish in `<prefix>-<env>-work-dlq.fifo`, then a row in `<prefix>-<env>-failed-executions` |
| Daily runs | the daily-cycle state machine's executions, named `<slug>-<date>-<n>` or `<slug>-<date>-manual-<HHMMSS>` |
| The schedules of a topic not yet moved | EventBridge Scheduler, group `default`: `<prefix>-<env>-<topic_id>-research-tick` and `-daily-cycle` |
| Logs | `/aws/lambda/<prefix>-<env>-dispatcher`, 90 days |

## Permissions

The dispatcher runs as the shared `lambda_exec` role. The work-queue module adds two inline
policies to that role, so every Lambda on it holds these rights (accepted; the admin API needs the
first two for manual runs):

| Policy | What it allows |
|---|---|
| `<prefix>-<env>-lambda-dispatch` | `sqs:SendMessage` on `work.fifo`; `states:StartExecution` and `states:ListExecutions` on the daily-cycle state machine; `states:DescribeExecution` on its executions (the last two for shadow mode's daily check) |
| `<prefix>-<env>-lambda-consume-work` | `sqs:ReceiveMessage`, `DeleteMessage` and `GetQueueAttributes` on `work.fifo` (the research tick's mapping) and `work-dlq.fifo` (the DLQ handler's) |

The dispatcher's schedule runs as a role of its own, `<prefix>-<env>-dispatcher-scheduler-invoke`,
which may invoke the dispatcher and nothing else.

## Where in the code

| Part | Code | Infra |
|---|---|---|
| The tick and its decisions | `lambdas/dispatcher_handler.py`; `lambdas/common/dispatch.py` (`Dispatcher`, the claims, `SETTINGS`, `validate_setting`, `effective_settings`) | `aws_lambda_function.dispatcher`, `aws_scheduler_schedule.dispatcher` in [`infra/modules/work-queue/main.tf`](../../infra/modules/work-queue/main.tf) |
| The cron matcher | `lambdas/common/cron_match.py` (`parse`, `research_turn`, `daily_turn_passed`) | none |
| The queues and their consumers | `_queue_record`, `_stamp` in `lambdas/research_tick_handler.py`; `lambdas/dlq_handler.py` | `aws_sqs_queue.work`, `aws_sqs_queue.work_dlq`, both event source mappings; `worker_concurrency` in each environment's `variables.tf` |
| The cutover, the way back, manual runs | `_update_topic`, `_move_to_queue`, `_reschedule_topics`, `_apply_topic_schedules`, `_trigger_on_the_queue`, `_schedule_view` in `lambdas/admin_api_handler.py`; `cutover_refusals`, `cutover_day`, `cadence_problem`, `slug_problem`, `queue_id_problem` in `dispatch.py` | the admin API's `PUT /topics/{topic_id}`, `POST /topics/reschedule`, `POST /topics/{topic_id}/trigger` |
| The commands | `scripts/admin_cli.py`: `topics update --dispatch-mode`, `topics reschedule --dispatch-mode schedule`, `topics schedule`, `topics trigger`, `pipeline-config set --dispatch-shadow --daily-starts-per-tick` | none |
| Alarms | none | `work_queue_age`, `work_dlq_messages`, `work_dlq_received`, `dispatcher_heartbeat` (with the `dispatcher_ticks` metric filter) in the module; errors and throttles through `local.pipeline_lambda_function_names` |

## Design history

- [model-ranch-prompt-assets-evals.md](../enhancements/archive/model-ranch-prompt-assets-evals.md): the
  design, built as phase Q (Q0 the matcher and the dispatcher's decisions, Q1 the queue platform in
  shadow mode, Q2 the per-topic cutover), with the decisions the reviews and the bug sweep made in
  its section 7.1.
- [dispatcher-sqs-enhancement.md](../enhancements/archive/dispatcher-sqs-enhancement.md): the proposal it
  builds on. Its step 3, removing per-topic scheduling, is superseded: schedule mode stays as the
  undo.
- [Article research](article-research.md): what the research tick and the daily cycle do once a
  job or a run reaches them.
