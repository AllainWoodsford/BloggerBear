# Observability: alarms, dashboards, logs and what is kept

What tells you something went wrong, where to look next, and how long each record lasts. It is
written for the person running the deployment. Everything here is made by Terraform: most alarms
and the pipeline-health dashboard by `infra/modules/observability`, the queue platform's alarms by
`infra/modules/work-queue`, continuous evals' alarms, log group and ranch-and-prompts dashboard by
`infra/modules/evals`, and the other logs and the tables by each environment's `main.tf` and the
modules it calls.

`<prefix>` and `<env>` are as in [configuration.md](configuration.md): every name is
`<prefix>-<env>-<resource>`, with `<env>` either `dev` or `production`. The security incidents,
which raise one of these alarms, are in
[architecture/security-incidents.md](architecture/security-incidents.md). The switches that
change what the firewall logs are in [cost-saving.md](cost-saving.md).

## Contents

- [The alert email](#the-alert-email)
- [Alarms](#alarms)
- [The queue platform's and evals' alarms](#the-queue-platforms-and-evals-alarms)
- [The Bedrock budget](#the-bedrock-budget)
- [Dashboards](#dashboards)
- [Logs](#logs)
- [What the visitor-facing logs record](#what-the-visitor-facing-logs-record)
- [The evals log group](#the-evals-log-group)
- [The evidence lines the queue platform, prompts and evals add](#the-evidence-lines-the-queue-platform-prompts-and-evals-add)
- [What is kept, and for how long](#what-is-kept-and-for-how-long)
- [Where the code is](#where-the-code-is)

## The alert email

Each environment has one SNS topic, `<prefix>-<env>-alerts` (`aws_sns_topic.alerts`). Every
alarm below publishes to it.

| What | How it works |
|---|---|
| The address | `ALERT_EMAIL_DEV` (a repository secret) for dev, `ALERT_EMAIL_PROD` (on the `production` environment) for production ([GitHub secrets and variables](configuration.md#github-secrets-and-variables)). CI passes it to Terraform as `alert_email`, which subscribes it to the topic (`aws_sns_topic_subscription.alerts_email`). |
| Unset | No subscription is made. The alarms still fire and publish to the topic, but nobody is told. |
| Confirming it | After the first apply with an address, AWS sends one confirmation email to it. Nothing is delivered until you click the link. Each environment's address is confirmed on its own. |
| Changing it | Change the secret and re-run the deploy. The new address gets its own confirmation email. |
| When an email goes out | When an alarm **changes state**, not for each event. An alarm emails when it goes into ALARM, and all but one also email when it goes back to OK. While it stays in ALARM, more errors send nothing more. |
| What it says | CloudWatch's alarm notification: the alarm's name, its description (the "Email says" column below) and why its state changed. |

So a Lambda that fails once usually sends two emails: ALARM, then OK a few minutes later, once a
5-minute period has passed with no error. A function that keeps failing sends one ALARM email and nothing
more until it recovers.

The Bedrock budget does not use this topic. It emails its own address
([below](#the-bedrock-budget)).

One email on this topic is not an alarm: the week's evals digest. While the evals setting
`digest_email` is on (`admin_cli.py evals set --digest-email on`; off by default), eval-jobs
publishes it on Fridays, after the first scheduled hourly rollup from 08:00 in the evals
settings' `timezone` (`Australia/Sydney` by default), once a day. It is the `evals digest` answer
written out as plain lines: model ids, prompt ids and versions, and numbers. A publish that fails
is tried again the next hour.

## Alarms

**Every alarm here is in both environments**, dev and production, with the same threshold. The
alarms in this first table are in `infra/modules/observability/main.tf`. Each looks at one period,
and treats a period with no data as fine, so a function that did not run never alarms. The queue
platform's and continuous evals' own alarms are
[in their own table](#the-queue-platforms-and-evals-alarms); two of those are heartbeats, which
treat no data as a failure.

| Alarm | Watches | Fires when | Email says | OK email too |
|---|---|---|---|---|
| `<prefix>-<env>-<prefix>-<env>-<function>-errors` (`lambda_errors`, one per function listed below). The module puts `<prefix>-<env>-` in front of the function's full name, which already starts with it, so the prefix appears twice, e.g. `<prefix>-production-<prefix>-production-research-tick-errors` | `AWS/Lambda` `Errors` for that function | at least 1 error in 5 minutes | the function had at least one invocation error in the last 5 minutes | yes |
| `<prefix>-<env>-<prefix>-<env>-<function>-throttles` (`lambda_throttles`, one per function, named the same way) | `AWS/Lambda` `Throttles` for that function | at least 1 throttle in 5 minutes | the function was throttled in the last 5 minutes | yes |
| `<prefix>-<env>-pipeline-dlq-messages` | messages waiting on `<prefix>-<env>-pipeline-dlq` (the highest count in the period) | more than 0 in 5 minutes | the daily-cycle dead-letter queue has a failed execution waiting on it | yes |
| `<prefix>-<env>-daily-cycle-executions-failed` | `AWS/States` `ExecutionsFailed` on the `<prefix>-<env>-daily-cycle` state machine | at least 1 in 5 minutes | the daily-cycle state machine had a failed execution | yes |
| `<prefix>-<env>-schedule-invocations-dropped` | `AWS/Scheduler` `InvocationDroppedCount` for the `default` schedule group | at least 1 in 5 minutes | the scheduler dropped a scheduled run, so a job did not run | yes |
| `<prefix>-<env>-feedback-rejections-spike` | `FeedbackRejected`: the public-api Lambda's `rejected a feedback submission` log lines (the honeypot or comment screening) | **150 or more in one hour** (`feedback_rejections_alarm_threshold`, default 150) | that many submissions were rejected in the last hour, likely comment spam; check the public API's WAF log for the source | yes |
| `<prefix>-<env>-feedback-screening-budget-used-up` | `FeedbackScreeningBudgetUsedUp`: the `rejected a feedback submission (screening_budget)` log line | at least 1 in 5 minutes | today's comment model checks are used up, so every comment is rejected until the day resets (Australia/Sydney) | yes |
| `<prefix>-<env>-security-high-severity` | `SecurityHighSeverityIncidents`: `SECURITY_ALERT` lines in the security-events, public-api, sign-in-events and admin-api Lambdas' logs | at least 1 in 5 minutes | a high-severity security incident was recorded; open incidents are in the SecurityEvents table | **no** |
| `<prefix>-<env>-prompt-fallback` | `PromptFallback`: `prompt_fallback` evidence lines in the log of every app Lambda (`aws_cloudwatch_log_group.lambda`) and, while evals are on, eval-jobs' | **at least 1 in one hour** | a Lambda fell back from a prompt asset to the copy in its code in the last hour; its log's `prompt_fallback` lines name the prompt, the version and why | yes |

The custom metrics are in the `BloggerBear/<env>` namespace. The log lines they count hold a
reason code or a tag, never a comment's text or a prompt's.

**The functions with errors and throttles alarms** are `local.pipeline_lambda_function_names` in
each environment's `main.tf`: `research-tick`, `daily-cycle`, `admin-api`, `public-api`,
`weekly-reflection`, `dlq-handler`, `musing-feedback`, `stats-rollover`, `cost-explorer-poll`,
`model-catalog`, `security-events` and `sign-in-events`, plus `reviewer` while the reviewer bear
is deployed (`REVIEWER_ENABLED`), the queue platform's `dispatcher` (always), and `eval-jobs`
while evals are on (`EVALS_ENABLED`). The operator's assistant (`ops-mcp`, `ops-agent`) and the
vision worker have **no alarm**: a failure there shows only in its own log.

**The prompt-fallback alarm stays quiet while prompts are not served as assets.** With
`PROMPT_AS_ASSET_ENABLED` off, every Lambda uses the copies in its code and writes no
`prompt_fallback` line. Once assets are on, a line means an asset was not used: see
[What to do when one fires](#what-to-do-when-one-fires). The run-time kill switch is not the
same: with `prompts set --assets off`, every render of an asset prompt writes an `assets_off`
line, so the alarm is in ALARM in each hour with such a line and back to OK, with an email, in an
hour with none (a research tick with nothing new calls no model; the daily cycle runs once a day),
until the next render fires it again. Both emails are expected while the switch is on, and an OK
email does not mean the fallbacks have stopped. While it is in ALARM, another fallback in the same
stretch sends no email of its own. After `prompts set --assets on`, it goes back to OK once an hour
passes with no line.

**The security alarm sends no OK email.** A `SECURITY_ALERT` line is logged once per incident,
when it is or becomes high severity, so each email is one incident. Two incidents that go high in
the same few minutes give one email: check the table for every open one. Lower severities are
recorded, never emailed.

**Where dev and production share an account,** they share the `default` schedule group too, so a
dropped run fires both environments' `schedule-invocations-dropped` alarms.

### What to do when one fires

| Alarm | What it usually means | What to do |
|---|---|---|
| `-errors` | An exception the function did not catch, or a timeout. The research tick and the daily cycle catch their own errors and return `"status": "error"`, which raises no alarm ([why](architecture/article-research.md#runs-that-write-nothing-and-runs-that-fail)). Two functions raise on purpose so that this alarm fires. The dispatcher raises when a whole tick fails (the Topics scan, the send to the work queue, Step Functions throttling a start), and when Step Functions refused a daily start for a reason no one topic causes (anything but `InvalidName`, `InvalidExecutionInput` and `ValidationException`: the role, the state machine, the service); that one it raises only after the tick has run every other topic and written `last_tick`, so their research and starts still go out. eval-jobs raises when its rollup cannot read the evals log or its tables. | Open pipeline health's recent-errors table, then the function's log, `/aws/lambda/<prefix>-<env>-<function>`. A dispatcher tick that failed needs no replay: the next one looks back to the last tick that finished (`last_tick` on the pipeline settings row). A refused daily start gives its claim back, its number (`daily_claim_n`) included, so every tick tries it again under the same execution name until the refusal is mended; its `dispatcher: topic_id=... start refused` line names the topic and Step Functions' error. A start refused for the topic's own name or input raises nothing: it is a `skipped` line in the dispatcher's log, tried again each tick. A rollup that failed is caught up by the next one, which reads from its watermark. |
| `-throttles` | The account ran out of concurrent Lambda executions. | Check the account's quota ([AWS account settings](configuration.md#aws-account-settings)): new accounts often allow only 10, shared by every function. |
| `pipeline-dlq-messages` | The `dlq-handler` Lambda takes each message off the queue as it arrives and writes it to FailedExecutions, so a message is usually gone before this sees it. It mostly fires when the handler cannot take messages off the queue. While evals are on, the queue also takes eval-work's judge and scoring jobs that were received three times without finishing; their records' `kind` is `judge_job` or `score_job`. eval-jobs answers every job and the mapping deletes it, with one exception: a scoring job whose run's header it could not read, or one that failed and then could not be counted finished, it names among its answer's `batchItemFailures` (the eval-work mapping reports batch item failures), so eval-work delivers it again after its 90-minute visibility timeout. So a job gets here after three receives that each timed out, crashed or were answered that way. Such an answer is not an invocation error, so only a timeout or a crash also fires eval-jobs' `-errors` alarm. | Look at `dlq-handler`'s errors alarm and log. The failed runs are in `python scripts/admin_cli.py failed-executions list`. A failed scoring job with no result is sent again by `evals runs resume <run_id>`; one whose result was written but never counted leaves its run running with nothing left to resume, and `evals runs cancel <run_id>` marks it cancelled, keeping what was scored. A judge item that failed stays unjudged, and a later judge run picks it again. |
| `daily-cycle-executions-failed` | The state machine retries a failed daily cycle up to twice, then sends it to the dead-letter queue, and that execution ends as succeeded. A timeout after the article was stored is the exception: the next attempt writes nothing, returns `already_written` and ends the execution as succeeded, so nothing reaches the queue and only the function's `-errors` alarm shows the timeout ([why](architecture/article-research.md#runs-that-write-nothing-and-runs-that-fail)). This fires only for an execution that fails anyway, such as when the send to the queue itself fails. | Open the execution in the Step Functions console. A failed daily cycle normally shows as a FailedExecutions record instead, unless it timed out after it stored its article. There is no automatic replay: run `topics trigger <topic_id> --pipeline daily_cycle` again once it is fixed ([scripts/README.md](../scripts/README.md#usage)). |
| `schedule-invocations-dropped` | A schedule's target refused it, or its retries ran out. The function was never invoked, so it logged nothing. | The Scheduler console's metrics say which schedule. A missed stats rollover needs repair: [When a week goes missing](architecture/stats-and-costs.md#when-a-week-goes-missing). |
| `feedback-rejections-spike` | Comment spam, most likely. | Check the public API's WAF log for where it comes from. The limits and settings are in [site-and-feedback.md](architecture/site-and-feedback.md#when-the-site-takes-feedback-limits-and-settings). |
| `feedback-screening-budget-used-up` | No comment can be kept until the day resets. | `python scripts/admin_cli.py feedback-config get` shows today's usage. |
| `security-high-severity` | An exploit attempt, a sustained attack, a sign-in lockout, or an incident opened by hand at high severity. | `python scripts/admin_cli.py security list`. What each kind is and what to do: [security-incidents.md](architecture/security-incidents.md). |
| `prompt-fallback` | A Lambda rendered a prompt from the copy in its code instead of its asset, and carried on. The line's `code` says why: the asset could not be read (`s3_error`, `decode_error`), there was none or it had no version tag (`no_files`, `no_version_tag`), its hash row was not approved or could not be read (`hash_unapproved`, `hash_lookup_failed`), it failed a template check (`too_large`, `missing_placeholder`, `unknown_placeholder`, `locked_missing`, `locked_order`), the version drawn is paused (`paused`), or the `prompts` settings row says assets are off (`assets_off`). A pause or the kill switch therefore puts this alarm in ALARM, by design, in every hour with an affected render: every render that meets it writes a line. An hour with none sends it back to OK, with an email, and the next affected render fires it again, so both emails are expected until it is lifted (a pause on one arm of a split goes back to OK more often, since runs that draw the other arms write nothing). An `assets_off` you did not set is a new container that could not read the settings row: it serves the code's copies until it can (a container that has read the row keeps what it last read). | In Logs Insights on the Lambda's log, `filter event = "prompt_fallback"` lists the prompt, version and code. `admin_cli.py prompts list` shows each file under `latest/` with its version, release, hash status and pause state; `evals status` counts the last 24 hours' fallbacks. While evals are on, the assistant's `prompt_metrics` raises a `prompt_fallback` card for each prompt that fell back in the last three days (today and the two before, from the rollup's Prompt Fallback counts, so up to an hour late), and a watch on that card reads as fixed only once three days pass with none: the reviewer bear runs every second day, so a shorter window would clear his prompts between runs. A bad version live: `prompts pause <prompt_id> <version>`, or `prompts set --assets off` for every prompt; either takes effect within five minutes, as each container's cache runs out. `prompts resume` and `prompts set --assets on` undo them. |

## The queue platform's and evals' alarms

`infra/modules/work-queue` makes four alarms, always: the dispatcher runs every minute from the
first apply, in shadow mode until a topic moves to the queue, so its heartbeat is watched from the
start. `infra/modules/evals` makes two, only while evals are on (`EVALS_ENABLED`); with evals off
they do not exist. All six email on ALARM and on OK. The dispatcher's and eval-jobs' own errors
and throttles alarms are the per-function ones [above](#alarms).

| Alarm | Made by | Watches | Fires when | Email says |
|---|---|---|---|---|
| `<prefix>-<env>-dispatcher-heartbeat` | work-queue | `DispatcherTicks`: the dispatcher's `dispatcher_tick` lines whose `outcome` is `ok`, one per tick that finished without raising | fewer than 1 in each of three 5-minute periods in a row. **No data counts as a failure** (`treat_missing_data = breaching`) | the dispatcher has not finished a tick in 15 minutes, so no research job or daily cycle is being dispatched |
| `<prefix>-<env>-work-queue-age` | work-queue | `AWS/SQS` `ApproximateAgeOfOldestMessage` on `<prefix>-<env>-work.fifo` (the highest in the period) | more than 900 seconds (15 minutes), in 5 minutes | a job on the work queue has waited more than 15 minutes |
| `<prefix>-<env>-work-dlq-messages` | work-queue | messages waiting on `<prefix>-<env>-work-dlq.fifo` (the highest count in the period) | more than 0 in 5 minutes | the work queue's dead-letter queue has a job waiting that dlq-handler has not recorded |
| `<prefix>-<env>-work-dlq-received` | work-queue | `AWS/SQS` `NumberOfMessagesReceived` on the same queue | more than 0 in 5 minutes | a queue job failed three times and reached the work queue's dead-letter queue (see `failed-executions list`) |
| `<prefix>-<env>-eval-rollup-heartbeat` | evals, while on | `EvalRollups`: eval-jobs' `rollup_done` lines, one per rollup that finished | fewer than 1 in each of three one-hour periods in a row. **No data counts as a failure** | the evals rollup has not finished in 3 hours, so the evals table and the models' scores are going stale |
| `<prefix>-<env>-eval-work-age` | evals, while on | `AWS/SQS` `ApproximateAgeOfOldestMessage` on `<prefix>-<env>-eval-work` | more than 7,200 seconds (2 hours), in 5 minutes | a job on the eval-work queue has waited more than 2 hours |

A heartbeat is what notices a job that is not being invoked at all (its schedule or the
schedule's role gone): such a job has no errors to alarm on. So a heartbeat can fire with no
`-errors` alarm beside it, and an `-errors` alarm usually fires first when the job is invoked and
failing.

| Alarm | What it usually means | What to do |
|---|---|---|
| `dispatcher-heartbeat` | No tick has finished for 15 minutes: the dispatcher fails every tick (its `-errors` alarm fires too), or nothing invokes it. Topics on the queue get no research and no daily cycle meanwhile; topics still on their two schedules are not touched (the dispatcher only shadows them). One exception: a tick that raised only because Step Functions refused a daily start (above) still sent the other topics' research and starts, but its line says `error`, so a refusal that lasts 15 minutes fires this alarm too. | The dispatcher's log, `/aws/lambda/<prefix>-<env>-dispatcher`, and the Scheduler console for `<prefix>-<env>-dispatcher`. A missed minute needs no replay: the next tick looks back to the last one that finished, and a daily cycle whose turn passed meanwhile is started then, if it is still that day in the topic's time zone. If it stays down, `topics reschedule --dispatch-mode schedule` puts every queue topic back on its two schedules. |
| `work-queue-age` | A research job has waited 15 minutes: the research tick is failing or throttled, or `WORKER_CONCURRENCY` (2 by default) is too few for the topics' research turns. The queue runs one job per topic at a time (FIFO, grouped by topic id), so a topic whose job is stuck holds back only its own. | The research tick's `-errors` and `-throttles` alarms and its log. `evals status` shows `work.fifo`'s depth and its oldest job's age, whether or not evals are on. More workers: raise `WORKER_CONCURRENCY` and re-run the deploy ([the setting](configuration.md#github-secrets-and-variables)). To take the queue out of the path: `topics reschedule --dispatch-mode schedule`. |
| `work-dlq-messages`, `work-dlq-received` | A research job was received three times without finishing. The research tick never raises (it returns an error result instead), so the job timed out or crashed the runtime. `dlq-handler` records it in FailedExecutions with `kind` `research_job` within seconds, so `work-dlq-received` is the one that normally fires; `work-dlq-messages` means the handler could not take it. | `failed-executions list`, then the research tick's log at that time (`Task timed out`). Nothing replays the job: the topic's next research turn queues a new one, or run `topics trigger <topic_id> --pipeline research_tick`. With `work-dlq-messages`, look at `dlq-handler`'s `-errors` alarm and log. |
| `eval-rollup-heartbeat` | No rollup has finished for three hours: eval-jobs fails (its `-errors` alarm fires first when the rollup cannot read the evals log or its tables), or its hourly schedule is not invoking it. Events are still written to the evals log; the day totals, the models' scores and so the rotation guard's gates are going stale. | `evals status` (the rollup's watermark and last run, and late events); eval-jobs' log, `/aws/lambda/<prefix>-<env>-eval-jobs`; `evals rollup` to run one now. A rollup reads from its watermark, so missed hours cost only lateness. |
| `eval-work-age` | A judge or scoring job has waited two hours: eval-jobs fails or is throttled, or a large offline scoring run is ahead of the judge's jobs (eval-jobs takes two jobs at a time). | `evals status` (eval-work's depth and age), `evals runs list` and `evals runs get <run_id>`, eval-jobs' `-errors` and `-throttles` alarms and its log. `evals runs cancel <run_id>` drops a run's remaining jobs as they come up. |

## The Bedrock budget

The last email you can get is not an alarm either, and does not use the alerts topic. It is an AWS
budget, `<prefix>-bedrock-spend` (`aws_budgets_budget.bedrock_spend` in
`infra/bootstrap/main.tf`).

| What | Value |
|---|---|
| Created | Only when bootstrap's `budget_alert_email` is set. Once per account, since bootstrap is applied per account. |
| Watches | The month's spend on Amazon Bedrock alone. |
| Limit | `bedrock_budget_limit_usd`, default US$20 a month ([Bootstrap variables](configuration.md#bootstrap-variables)). |
| Emails | `budget_alert_email`, directly, with no confirmation to click. |
| When | Once **actual** spend passes 80% of the limit, and once the **forecast** for the month passes 100%. |

## Dashboards

CloudWatch → Dashboards. Each environment has two: pipeline health, always, and the Model Ranch
and prompts dashboard while evals are on. They replaced the pipeline, lambda-runs and edge
dashboards. Every panel reads the deployment's home region, except the shared CloudFront ACL's,
which reads `us-east-1`: CloudFront publishes its metrics there only.

| Dashboard | Environment | Opens on | Shows |
|---|---|---|---|
| `<prefix>-<env>-pipeline-health` (`aws_cloudwatch_dashboard.pipeline_health`, in `infra/modules/observability/pipeline_health.tf`) | both, always | the last 7 days, one point per hour | Every pipeline Lambda, the daily cycle, feedback, both APIs and the web ACLs, the model catalog's counters, 30 days of trend and the latest errors: [panel by panel](#pipeline-health-panel-by-panel). |
| `<prefix>-<env>-ranch-and-prompts` (`aws_cloudwatch_dashboard.ranch_and_prompts`, in `infra/modules/evals/dashboard.tf`) | both, **only while evals are on** (`EVALS_ENABLED`) | the last 7 days | The scored events in the evals log group as they arrive, before any rollup, and the prompt fallbacks: [panel by panel](#ranch-and-prompts-panel-by-panel). |

**What they cost.** CloudWatch's free tier is three dashboards an account, of up to 50 metrics
each; each dashboard past those is about US$3 a month
([costs](production-runsheet.md#what-it-costs-list-prices-roughly)). Where dev and production
share an account and evals are on in both, that is four dashboards, so at least one is paid.
Whether pipeline health counts as one of the free three **has not been checked yet**. Its
per-Lambda panels are SEARCH expressions, each one entry in a widget's metrics, and counted that
way the dashboard stays within 50 (a module test holds it there). If CloudWatch counts every line
a search draws instead, each environment's pipeline-health dashboard is past the free tier's 50
metrics and is paid whatever else the account has: about US$6 a month for the two. The owner is to
settle it from the bill or Cost Explorer after a dev apply (usage type `DashboardsUsageHour-Basic`
is the free tier, `DashboardsUsageHour` the paid one). Until then, plan on the higher figure.

**Panels that read logs cost money when opened.** Pipeline health's recent-errors table and every
ranch-and-prompts panel but the fallback chart are Logs Insights queries, billed per GB scanned
each time the dashboard loads them. The evals log group holds one short JSON line per event.

**Some API Gateway and firewall detail is no longer drawn.** Pipeline health keeps each API's
requests, errors and p95 latency and each web ACL's blocks per rule. Latency against integration
latency at p50, p90 and p99, the responses by status and the 4XX/5XX by route from the access
logs, the public API's cache, the allowed and counted requests per rule, and the top blocked
rule, address and path from the WAF logs are not on any dashboard now. Their widgets are kept as
locals in `infra/modules/observability/api_waf_dashboards.tf`, for a dashboard of their own should
one be wanted again (about 45 metrics for production alone, so another paid dashboard).
Meanwhile each of those is a Logs Insights query on the access logs or the WAF logs
([below](#logs)), or a metric in the CloudWatch console.

### Pipeline health, panel by panel

The tiles add up the **whole** range picked at the top right. The charts draw one point per hour,
and the trend panel always reads 30 days.

| Part | Panels |
|---|---|
| Operator tiles | Four totals: dispatcher ticks that finished, evals rollups that finished, prompt fallbacks, and high-severity security incidents. They are the metrics behind the two heartbeats, the prompt-fallback alarm and the security alarm. A tile with nothing is a switch that is off (evals) or a quiet week. The dispatcher finishes about 60 ticks an hour, so fewer than that over the range means it missed some. |
| Model catalog | Totals from the model-catalog Lambda's `model_catalog_job` lines: models discovered, pruned, classified (`models classify`), denied, repriced, reverted (prices a review failed, plus prices the last review put back because no review could look them up) and dropped (prices someone else changed meanwhile). The rollup's own classifying of new models is on its `rollup_done` line, not here. |
| Every pipeline Lambda | Errors per hour and throttles per hour, one line per function, and each function's p95 duration as a share of its own timeout (100% is timing out). The functions are the ones with errors and throttles alarms [above](#alarms). A function that has not run in the last two weeks draws no line: the panels find the functions by a metric search, which sees only metrics reported in that time. |
| The daily cycle | The state machine's executions started, succeeded, failed and timed out per hour, beside the messages waiting on its dead-letter queue. |
| Feedback | Totals: accepted, and of them comments kept; rejected, and of them dropped by the screening model and rejected because the day's model checks were used up. From the public-api Lambda's tag-only log lines. |
| Each API | The public and the admin API's requests, 4XX and 5XX per hour, and one chart of both APIs' p95 latency. |
| Each web ACL that exists | Blocked requests per rule, rate limits included. The shared CloudFront ACL's reads `us-east-1`, on dev's dashboard too, since dev uses the same ACL; with two accounts, dev's panel for it stays empty ([why](deployment-separate-accounts.md#logs-alarms-and-dashboards)). An ACL a cost-saving switch turned off is left off ([cost-saving.md](cost-saving.md)). WAF only reports a number above zero, so a rule that blocked nothing draws no line. |
| The last 30 days | Daily runs succeeded and failed, and feedback accepted, per day. |
| Recent errors | The latest 50 lines matching `unhandled exception`, `Traceback`, `ERROR` or `Task timed out` across every listed Lambda's log. |

### Ranch and prompts, panel by panel

Every panel but the first is a Logs Insights query over the evals log group (`<prefix>-<env>-evals`),
so it shows the events as they were written, before the hourly rollup: a rating here is the plain
sum of the events' `rating_delta`. The rolled-up windows, the gates and the judge's calibration are
`admin_cli.py evals status`. The events hold codes, ids and numbers only, so no panel can show a
reader's comment or a model's reply.

| Panel | Shows |
|---|---|
| Prompt fallbacks per hour | The `PromptFallback` metric the prompt-fallback alarm reads, counted from every app Lambda's log and eval-jobs'. |
| Events and rating per hour | How many events were written, and the sum of their deltas. |
| Models | Events and rating per model, lowest rating first (50 rows). |
| Prompt versions | Events and rating per prompt id and version (50 rows). |
| Model x prompt cells | Events and rating per model, prompt and version, lowest rating first (25 rows). |
| Events by reason and source | Per reason (Article Completion, User Feedback, Research Review and so on) and the emitting Lambda's code. |
| Fallbacks recorded as events | `Prompt Fallback` events by prompt version and source (the Lambda that fell back). They name no model and score nothing: the rollup counts each on its prompt version alone. A fallback whose event could not be written is on the chart above only. |
| Latest bad events | The 25 newest events with a negative delta: when, the model, the prompt and version, the reason, the delta and the source. |

## Logs

CloudWatch → Log groups. Terraform makes each of these groups with its retention set, so none of
them grows for ever.

| Log group | Environment (region) | Kept | What it holds |
|---|---|---|---|
| `/aws/lambda/<prefix>-<env>-<function>`, one per app Lambda (`aws_cloudwatch_log_group.lambda`) | both | 90 days | What the function prints. The public-api Lambda's feedback lines hold a reason code or a tag, never a comment. |
| `/aws/lambda/<prefix>-<env>-ops-mcp` and `-ops-agent` (`infra/modules/ops-assistant`, `log_retention_days`) | both | 90 days | The assistant. The agent logs which turn it was, which tools were called and how many findings came back: never the question, the answer or the token. |
| `/aws/lambda/<prefix>-<env>-vision-worker` (`infra/modules/vision-worker`) | where the worker is on (`vision_region`) | 90 days | The vision worker. |
| `/aws/lambda/<prefix>-<env>-dispatcher` (`infra/modules/work-queue`) | both | 90 days | The queue platform's dispatcher: one `dispatcher_tick` line a minute, and a line for each topic it could not consider (`dispatcher: cadence unparsable topic_id=... expression=...` among them). |
| `/aws/lambda/<prefix>-<env>-eval-jobs` (`infra/modules/evals`) | both, while evals are on | 90 days | The rollup, the judge and offline scoring runs: `rollup_done`, `judge_skipped` and `judge_tripwire` lines, the run and item ids they worked on, and a `prompt_fallback` line when an offline run's draft of the live version falls back (this log feeds the prompt-fallback alarm too). |
| `<prefix>-<env>-evals` (`infra/modules/evals`) | both, whether or not evals are on | **120 days** | Continuous evals' scored events, one JSON line each, codes, ids and numbers only ([below](#the-evals-log-group)). |
| `/aws/apigateway/<prefix>-<env>-public-api-access` | both | **14 days** | One line per request to the public API, with no visitor details ([below](#what-the-visitor-facing-logs-record)). |
| `/aws/apigateway/<prefix>-<env>-admin-api-access` | both | 30 days | The same fields, for the admin API. The security-events Lambda reads its 4xx lines. |
| `/aws/apigateway/<prefix>-<env>-ops-mcp-access` | both | 30 days | The same fields without `wafStatus`, for the assistant's API. Which signed-in user called is not logged. |
| `aws-waf-logs-<prefix>-<env>-public-api` | both (home region) | **14 days** | The public API's web ACL: only what a rule blocked or counted, with browser headers redacted. |
| `aws-waf-logs-<prefix>-<env>-admin` | both (home region) | 30 days | The admin API's web ACL: every request by default, an audit trail of your own calls. Only blocked and counted ones with `WAF_LOG_ALL_REQUESTS=false` ([the switch](cost-saving.md#waf_log_all_requests)). No headers redacted. |
| `aws-waf-logs-<prefix>-shared` | production only (`us-east-1`) | **14 days** | The shared CloudFront web ACL, in front of the site and the public API's CDN: only what a rule blocked or counted, with browser headers redacted. |

Not logged at all: API Gateway's execution logs (`logging_level = "OFF"` on all three APIs),
CloudFront access logs (no distribution sets any) and the daily-cycle state machine, whose
execution history Step Functions keeps itself, for 90 days.

A WAF log exists only while its web ACL does. `WAF_REGIONAL_ENABLED=false` removes the two
regional ACLs and their logs, `WAF_ADMIN_ALLOWLIST=false` the admin one, and
`WAF_SITE_ENABLED=false` the shared one ([cost-saving.md](cost-saving.md)). No switch makes an
ACL log more.

The security-events Lambda reads the `BLOCK` records of the public API's and the admin API's WAF
logs as they arrive, and turns them into security incidents
([security-incidents.md](architecture/security-incidents.md)). The shared ACL's log, in
`us-east-1`, is not read that way.

### What the visitor-facing logs record

Three logs see anonymous visitors: the public API's WAF log, the shared CloudFront ACL's log and
the public API's access log. Dev's public API logs are set up exactly as production's; the shared
ACL, and so its log, is production's alone.

| Log | What it records | What it leaves out | Kept |
|---|---|---|---|
| The two visitor WAF logs (`aws_wafv2_web_acl_logging_configuration.public_api` and `.shared`) | Only requests a rule **blocked or counted** (`logging_filter` drops the rest). For each: the time, the source IP address, the address asked for, the rule that acted, and `matchedData`, the small part of the request that tripped the rule, which can be a fragment of a comment. Through the CDN, the visitor's address is also in the `x-viewer-ip` header. | Allowed requests, so ordinary browsing and accepted feedback. 18 browser-fingerprinting headers and the CDN's `x-origin-verify` secret, redacted (`local.waf_log_redacted_headers`): `user-agent`, `referer`, `accept`, `accept-language`, `accept-encoding`, `cookie`, `x-forwarded-for`, `dnt`, `sec-gpc` and the nine `sec-ch-ua` client hints. WAF cannot redact the source IP or `matchedData`. | 14 days (`local.waf_visitor_log_retention_days`) |
| The public API's access log (`access_log_settings` in `infra/modules/rest-api/main.tf`) | Per request: the request id, the time, the method, the resource path, the status, the response and integration latency, the response length, the error type and the WAF status. | Any IP address, user agent or other detail about who asked. | 14 days (`access_log_retention_days` on `module "public_api"`) |

AWS WAF also keeps a small sample of recent requests, allowed and blocked, for up to 3 hours
(`sampled_requests_enabled`). AWS holds that sample, and it expires on its own.

**The Privacy Policy describes exactly this.** Section 5 of `frontend/privacy.html`,
[Firewall and security logs](../frontend/privacy.html#firewall-and-security-logs), tells
visitors what these logs hold, where they are and that they are kept 14 days. The Terraform
comment beside `local.waf_log_redacted_headers` says the two must change together. If you change
the filter, the redacted headers or a retention, change section 5 in the same pull request. The
section also names the regions (Sydney for the API, US East for the site's firewall), so a
deployment in another region changes that wording too.

### The evals log group

`<prefix>-<env>-evals` is where every scored event goes: an article stored, a person's approve,
reject or rewrite, a reader's vote, a judge's or a person's review, an offline scoring verdict, an
operator's forgive or punish, a prompt's fallback. It is made whatever `EVALS_ENABLED` says, so
turning evals off loses no history, and nothing is written to it while they are off. Each
container writes one stream of its own a day, `<function>/<yyyy-mm-dd>/<container id>`.

- **What a line holds.** One event (`schema` `evals/1`): its `event_id`, the time, the model and
  the prompt id and version it scores, `good`, `bad` or `neutral` with its `rating_delta`, the
  reason, the subject (an article id, or `<topic_id>#<captured_at>` for a Finding), the topic id,
  the emitting Lambda's code and, for a review, who gave it (`human`, `judge` or `system`). Every
  field has a fixed shape, and an event with a field that fails is not written at all, so a
  comment, a reply or any other text has nowhere to go (`lambdas/common/evals.py`).
- **Who writes it.** The daily cycle (one Article Completion per prompt in a stored article's
  lineage, its research included, leaving out the calls of a failed rewrite and research its
  editorial goal set aside), the
  admin API (a person's decisions, Findings reviews, forgives and punishes), the public API (a
  reader's vote, after its answer is built), eval-jobs (the judge's and offline runs' verdicts),
  and any Lambda whose prompt fell back. A write that fails is lost, with one line in the writer's
  log; it never fails the work.
- **Who reads it.** eval-jobs' hourly rollup, which adds the events up into the evals table
  (`<prefix>-<env>-evals`, kept for good); the ranch-and-prompts dashboard; and two metric filters
  (`EvalRatingDelta` by `Performance`, and `EvalEvents`), while evals are on.
- **Why 120 days.** The rollup reads only from its watermark, a day or two back, and the day totals
  outlive the lines. Adding days up again (`evals rollup --from <day>`, or `evals revoke`, which
  re-adds the days that hold the revoked events) reaches 118 days back at most.

### The evidence lines the queue platform, prompts and evals add

An evidence line is one JSON object a line, codes, ids and numbers only, printed into the
function's own log (`EVENTS` in `lambdas/common/evidence.py`). Logs Insights finds one kind with
`filter event = "<name>"`. These are the features' own:

| Event | Printed by | When, and what it carries | What reads it |
|---|---|---|---|
| `dispatcher_tick` | the dispatcher | Once a tick, every minute: the topics considered, research jobs sent, daily cycles started, cadences skipped as unparsable, how long it took, `outcome` `ok` or `error` and the error's type. | The dispatcher's heartbeat alarm and pipeline health's tile (the `ok` lines). |
| `prompt_fallback` | any Lambda that renders a prompt asset | Each time it used the code's copy instead: the Lambda and prompt names, the asset's version when one was read, and the `code` saying why. | The prompt-fallback alarm and both dashboards. |
| `model_catalog_job` | the model-catalog Lambda | At the end of each discover, prune, cycle, reprice, review or classify job, with its counts. | Pipeline health's model-catalog row. |
| `rollup_done` | eval-jobs | At the end of every rollup that finished: the days it added up, the events read, duplicates, unreadable, excluded and late ones, the models updated, its classifying of new models, and the judge's agreement with people and whether it is calibrated. | The rollup heartbeat alarm and pipeline health's tile. |
| `judge_skipped` | eval-jobs | An item the judge passed over, and why: `author_only` (every model it may use wrote it), `no_judge` (no model fit to judge), `no_author` or `no_source` (its snapshot or body could not be read). | People. |
| `judge_tripwire` | eval-jobs | A judge run marked more than 80% of at least 10 items bad, so the judge turned itself off (`judge_paused_reason` on the evals settings row says why). No email is sent: `evals status` shows it, and `evals set --judge on` turns the judge back on and clears the reason. | People. |
| `model_gated_protected` | any Lambda that picks a model (routing, the rewrite picker) | A model the gates would hold back was served all the same, because it is the default, the fallback, the judge or a topic's own model: the model id, its role, the slot and the windows that gate it. | People. |
| `model_cull` | the model-catalog Lambda (the weekly proposal) and the admin API (`evals cull --apply`, `models restore`) | A cull proposed (or not, while evals are off), applied, or undone: counts, and for a restore the model and its forgive. | People. |

## What is kept, and for how long

### Tables

Every table that expires rows does it with DynamoDB TTL on an `expires_at` attribute. Terraform
turns TTL on; the code sets `expires_at` when it writes the row. A row with no `expires_at` is
kept for good.

TTL deletion is lazy: a row can stay up to about two days past its `expires_at`. Reads that care,
such as the Findings for the next article, skip rows whose time has passed.

| Table (`<prefix>-<env>-…`) | What expires | When | Set by |
|---|---|---|---|
| `findings` | every Finding | 14 days after it is written | `FINDING_TTL_DAYS` in `lambdas/research_tick_handler.py` |
| `candidate-ideas` | every candidate idea | 7 days after it is written | `CLEANUP_TTL_DAYS` in `lambdas/common/dynamo.py` (`put_candidate_idea`) |
| `moderation-queue` | an item once it is rejected, or once its rewrite failed with the article still published (`rewrite_failed`) | 7 days after that | `CLEANUP_TTL_DAYS` (`update_moderation_status`, `put_moderation_item`, `finish_moderation_rewrite`). Pending, approved and rewritten items never expire. |
| `prompt-refinements` | a version once it is rejected | 7 days after that | `CLEANUP_TTL_DAYS` (`update_prompt_refinement_status`). Pending and approved versions never expire. |
| `failed-executions` | every record: failed daily cycles from the dead-letter queue, queue jobs that failed three times (`kind` `research_job`, `judge_job` or `score_job`), and prices the model catalog put back (`kind: model_pricing`) | 7 days after it is written | `CLEANUP_TTL_DAYS` (`put_failed_execution`) |
| `model-config` | the feedback limiter's counters (`feedback-window#…`, `feedback-day#…`, `feedback-screen#…`) | 2 days after their window or day ends | `_ROW_TTL_SLACK` in `lambdas/common/feedback_limits.py` |
| `model-config` | a used feedback token (`nonce#…`) | an hour after the token expires | `lambdas/common/feedback_verification.py` |
| `model-config` | a model catalog pricing run (`pricing-run#…`) | 14 days after it ends | `RUN_ROW_DAYS` in `lambdas/common/model_catalog.py` |
| `security-events` | every incident | 120 days after its last request | `RETENTION_DAYS` in `lambdas/common/security_events.py` |
| `sign-ins` | every sign-in event | 120 days after it happened | `RETENTION_DAYS` in `lambdas/common/sign_ins.py` |
| `operator-suggestions` | every row of the assistant's memory | 30 days after it was last mentioned | `RETENTION` in `lambdas/ops_mcp/memory.py` |
| `ops-briefings` | each user's latest briefing (each new one overwrites it) | 2 days after it is written | `RETENTION` in `lambdas/ops_mcp/briefings.py` |

**Never expire:** `topics`, `articles`, `feedback`, `musings`, `models`, `stats-current`,
`stats-history`, `view-counts`, the settings rows in `model-config`, `evals` (the day totals,
operator adjustments, offline runs, judge and human pairs and the judge's daily spend) and
`prompt-hashes` (every prompt version ever published). Stored feedback is kept until you delete
it.

In production, every app table also has point-in-time recovery: any second of the last 35 days
can be restored (`protect_data` in `infra/modules/app-data`), and so do the evals and
prompt-hashes tables (`protect_data` in `infra/modules/evals` and `infra/modules/prompts`). Dev's
tables have none.

### Buckets

| Bucket | Rule | What it does | Set in |
|---|---|---|---|
| `<prefix>-dev-content` | `expire-old-snapshots` | Deletes the raw source snapshots (`snapshots/`) 21 days after they are written. | `aws_s3_bucket_lifecycle_configuration.content`, `infra/environments/dev/main.tf` |
| `<prefix>-production-content` | `expire-old-snapshots` | Retires the snapshots (`snapshots/`) after 21 days. The bucket is versioned, so this leaves an old version, which the next rule removes. | `aws_s3_bucket_lifecycle_configuration.content`, `infra/environments/production/main.tf` |
| `<prefix>-production-content` | `expire-old-versions` | Deletes any replaced or deleted version 30 days after it stopped being current, and any unfinished multipart upload after 7 days. Article bodies and figures are kept for good. | the same |
| `<prefix>-<env>-content` | `expire-call-traces` | Deletes call traces (`traces/`) 14 days after they are written. In production, where the bucket is versioned, the noncurrent copy that leaves behind is deleted a day later, so a trace is gone within 15 days. | `aws_s3_bucket_lifecycle_configuration.content` in each environment's `main.tf` |
| `<prefix>-<env>-prompts` | `expire-old-versions` | Deletes a replaced `latest/` file or `releases/current.json` 90 days after it was replaced, and any unfinished upload after 7 days. The files under `versions/` are written once and kept for good. | `infra/modules/prompts` |
| `<prefix>-<env>-site` | none | Not versioned. Each deploy uploads the site again, and an article's page stays until it is unpublished. | `infra/modules/static-site` |
| `<prefix>-<env>-vision-artifacts-<account id>-<vision_region>` | `expire-old-packages`, `abort-incomplete-uploads` | Deletes worker packages (`vision-worker/`) after 14 days, and unfinished uploads after 7. | `infra/modules/vision-worker` |
| The bootstrap state bucket | none | Versioned, and every version is kept: it is the state's rollback history. | `infra/bootstrap/main.tf` |

The snapshots outlive the 14-day Findings by a week on purpose. TTL can delete a Finding up to two
days late, and the daily cycle may still read its snapshot until then.

**Call traces** are written only while the evals setting `trace_calls` is on: on by default in
dev, off in production (`TRACE_CALLS_DEFAULT` in each root; `evals set --trace-calls on|off`
changes it). Each is one model call's whole prompt and reply, at `traces/<subject>/<stage>.json` in
the content bucket, which has no public path, with the id of the run that stored it as the
object's metadata (a Re-Write's is its `rewrite_id`). Only the admin API reads them (`evals trace
<article_id>`), and it marks `superseded` a Re-Write stage left over from an earlier attempt. Two
values are stored as their sha256, never their text: the gear's guidance in the daily cycle's
ideation and draft, which readers' comments shaped, and the topic's operator note in the
fresh-data review. Everything else a prompt held (the findings, the drafts, the fresh data) is
stored as it was sent.

**Backups** are in the state bucket, under `backup/<prefix>-<env>/snapshots/`. The weekly schedule
keeps its newest 8 snapshots and deletes older ones after each run (`KEEP_SCHEDULED` in
`lambdas/common/backup.py`). A snapshot you take by hand is never deleted. A deleted one can still
be recovered, because the bucket is versioned. A snapshot holds the evals table too; it leaves out
the prompt-hashes table, the prompts bucket and every log group. What a snapshot holds, and leaves
out: [scripts/README.md](../scripts/README.md#backups-restores-and-article-exports-backup).

### What that means for you

- **A failed daily cycle or queue job drops out of `failed-executions list` after 7 days**, and
  nothing replays it. Look within the week.
- **`moderation stats` sees rejected items from the last 7 days only.** Pending and approved ones
  are counted from the start.
- **A Finding is gone after 14 days**, which is why a topic's findings window is at most 336 hours.
- **A security incident is gone 120 days after its last request.** Note anything you need to keep
  before then.
- **A scored event is gone from the evals log after 120 days**, but its day's totals stay in the
  evals table for good. A day older than 118 days can no longer be added up again, so an
  `evals revoke` that reaches further back leaves those days' totals as they are.
- **A call trace is gone after 14 days** (15 at most in production). Read it with `evals trace`
  within the fortnight.
- **None of the expiring rows is in a backup.** A snapshot leaves out Findings, candidate ideas,
  failed executions, security events, sign-ins and any row already set to expire.
- **A scan by hand can still show expired rows**, for up to about two days.

## Where the code is

- The alert topic and its alarms: `infra/modules/observability/main.tf`. Pipeline health:
  `pipeline_health.tf` beside it, which takes its API and firewall panels from the widgets in
  `api_waf_dashboards.tf`. The thresholds and switches: its `variables.tf`.
- The queue platform's alarms and the dispatcher's log group: `infra/modules/work-queue/main.tf`.
  Continuous evals' alarms, metric filters, the evals log group and table, and eval-jobs' log
  group: `infra/modules/evals/main.tf`; ranch and prompts: `dashboard.tf` beside it.
- Which functions are watched and which logs feed the security and prompt-fallback alarms:
  `local.pipeline_lambda_function_names`, `security_alert_log_groups`, `prompt_fallback_log_groups`
  (eval-jobs' through `module.evals.function_log_group_names`) and `module "observability"` in
  `infra/environments/{dev,production}/main.tf`.
- The evidence lines: `EVENTS` in `lambdas/common/evidence.py`. The scored events:
  `lambdas/common/evals.py`.
- The Bedrock budget: `aws_budgets_budget.bedrock_spend` in `infra/bootstrap/main.tf`.
- The Lambda log groups: `aws_cloudwatch_log_group.lambda` and
  `local.lambda_log_group_function_names` in each environment's `main.tf`; the assistant's and
  the worker's in their modules.
- The WAF logs, their filter and redaction: `local.waf_log_redacted_headers`,
  `local.waf_visitor_log_retention_days`, the `aws_cloudwatch_log_group.waf_*` groups and the
  `aws_wafv2_web_acl_logging_configuration` resources in each environment's `main.tf`.
- The access logs: `access_log_settings` and `aws_cloudwatch_log_group.access` in
  `infra/modules/rest-api/main.tf` and `infra/modules/ops-assistant/main.tf`.
- The TTLs: the `ttl` blocks in `infra/modules/app-data/main.tf`,
  `infra/modules/ops-assistant/memory.tf` and `briefings.tf`, and the code in the table above.
- The bucket rules: the `aws_s3_bucket_lifecycle_configuration` resources named in the table above.
