# Observability: alarms, dashboards, logs and what is kept

What tells you something went wrong, where to look next, and how long each record lasts. It is
written for the person running the deployment. Everything here is made by Terraform: the alarms
and dashboards by `infra/modules/observability`, the logs and the tables by each environment's
`main.tf` and the modules it calls.

`<prefix>` and `<env>` are as in [configuration.md](configuration.md): every name is
`<prefix>-<env>-<resource>`, with `<env>` either `dev` or `production`. The security incidents,
which raise one of these alarms, are in
[architecture/security-incidents.md](architecture/security-incidents.md). The switches that
change what the firewall logs are in [cost-saving.md](cost-saving.md).

## Contents

- [The alert email](#the-alert-email)
- [Alarms](#alarms)
- [The Bedrock budget](#the-bedrock-budget)
- [Dashboards](#dashboards)
- [Logs](#logs)
- [What the visitor-facing logs record](#what-the-visitor-facing-logs-record)
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

## Alarms

**Every alarm here is in both environments**, dev and production, with the same threshold. Each
looks at one period, and treats a period with no data as fine, so a function that did not run
never alarms. All are in `infra/modules/observability/main.tf`.

| Alarm | Watches | Fires when | Email says | OK email too |
|---|---|---|---|---|
| `<prefix>-<env>-<prefix>-<env>-<function>-errors` (`lambda_errors`, one per function listed below). The module puts `<prefix>-<env>-` in front of the function's full name, which already starts with it, so the prefix appears twice, e.g. `bloggerbear-production-bloggerbear-production-research-tick-errors` | `AWS/Lambda` `Errors` for that function | at least 1 error in 5 minutes | the function had at least one invocation error in the last 5 minutes | yes |
| `<prefix>-<env>-<prefix>-<env>-<function>-throttles` (`lambda_throttles`, one per function, named the same way) | `AWS/Lambda` `Throttles` for that function | at least 1 throttle in 5 minutes | the function was throttled in the last 5 minutes | yes |
| `<prefix>-<env>-pipeline-dlq-messages` | messages waiting on `<prefix>-<env>-pipeline-dlq` (the highest count in the period) | more than 0 in 5 minutes | the daily-cycle dead-letter queue has a failed execution waiting on it | yes |
| `<prefix>-<env>-daily-cycle-executions-failed` | `AWS/States` `ExecutionsFailed` on the `<prefix>-<env>-daily-cycle` state machine | at least 1 in 5 minutes | the daily-cycle state machine had a failed execution | yes |
| `<prefix>-<env>-schedule-invocations-dropped` | `AWS/Scheduler` `InvocationDroppedCount` for the `default` schedule group | at least 1 in 5 minutes | the scheduler dropped a scheduled run, so a job did not run | yes |
| `<prefix>-<env>-feedback-rejections-spike` | `FeedbackRejected`: the public-api Lambda's `rejected a feedback submission` log lines (the honeypot or comment screening) | **150 or more in one hour** (`feedback_rejections_alarm_threshold`, default 150) | that many submissions were rejected in the last hour, likely comment spam; check the public API's WAF log for the source | yes |
| `<prefix>-<env>-feedback-screening-budget-used-up` | `FeedbackScreeningBudgetUsedUp`: the `rejected a feedback submission (screening_budget)` log line | at least 1 in 5 minutes | today's comment model checks are used up, so every comment is rejected until the day resets (Australia/Sydney) | yes |
| `<prefix>-<env>-security-high-severity` | `SecurityHighSeverityIncidents`: `SECURITY_ALERT` lines in the security-events, public-api, sign-in-events and admin-api Lambdas' logs | at least 1 in 5 minutes | a high-severity security incident was recorded; open incidents are in the SecurityEvents table | **no** |

The custom metrics are in the `BloggerBear/<env>` namespace. The log lines they count hold a
reason code or a tag, never a comment's text.

**The functions with errors and throttles alarms** are `local.pipeline_lambda_function_names` in
each environment's `main.tf`: `research-tick`, `daily-cycle`, `admin-api`, `public-api`,
`weekly-reflection`, `dlq-handler`, `musing-feedback`, `stats-rollover`, `cost-explorer-poll`,
`model-catalog`, `security-events` and `sign-in-events`, plus `reviewer` while the reviewer bear
is deployed (`REVIEWER_ENABLED`). The operator's assistant (`ops-mcp`, `ops-agent`) and the vision
worker have **no alarm**: a failure there shows only in its own log.

**The security alarm sends no OK email.** A `SECURITY_ALERT` line is logged once per incident,
when it is or becomes high severity, so each email is one incident. Two incidents that go high in
the same few minutes give one email: check the table for every open one. Lower severities are
recorded, never emailed.

**Where dev and production share an account,** they share the `default` schedule group too, so a
dropped run fires both environments' `schedule-invocations-dropped` alarms.

### What to do when one fires

| Alarm | What it usually means | What to do |
|---|---|---|
| `-errors` | An exception the function did not catch, or a timeout. The research tick and the daily cycle catch their own errors and return `"status": "error"`, which raises no alarm ([why](architecture/article-research.md#runs-that-write-nothing-and-runs-that-fail)). | Open the pipeline dashboard's recent-errors table, then the function's log, `/aws/lambda/<prefix>-<env>-<function>`. |
| `-throttles` | The account ran out of concurrent Lambda executions. | Check the account's quota ([AWS account settings](configuration.md#aws-account-settings)): new accounts often allow only 10, shared by every function. |
| `pipeline-dlq-messages` | The `dlq-handler` Lambda takes each message off the queue as it arrives and writes it to FailedExecutions, so a message is usually gone before this sees it. It mostly fires when the handler cannot take messages off the queue. | Look at `dlq-handler`'s errors alarm and log. The failed runs are in `python scripts/admin_cli.py failed-executions list`. |
| `daily-cycle-executions-failed` | The state machine retries a failed daily cycle twice, then sends it to the dead-letter queue, and that execution ends as succeeded. A retry of a run that had already stored its article writes nothing and returns `already_written` ([why](architecture/article-research.md#runs-that-write-nothing-and-runs-that-fail)). This fires only for an execution that fails anyway, such as when the send to the queue itself fails. | Open the execution in the Step Functions console. A failed daily cycle normally shows as a FailedExecutions record instead. There is no automatic replay: run `topics trigger <topic_id> --pipeline daily_cycle` again once it is fixed ([scripts/README.md](../scripts/README.md#usage)). |
| `schedule-invocations-dropped` | A schedule's target refused it, or its retries ran out. The function was never invoked, so it logged nothing. | The Scheduler console's metrics say which schedule. A missed stats rollover needs repair: [When a week goes missing](architecture/stats-and-costs.md#when-a-week-goes-missing). |
| `feedback-rejections-spike` | Comment spam, most likely. | Check the public API's WAF log for where it comes from. The limits and settings are in [site-and-feedback.md](architecture/site-and-feedback.md#when-the-site-takes-feedback-limits-and-settings). |
| `feedback-screening-budget-used-up` | No comment can be kept until the day resets. | `python scripts/admin_cli.py feedback-config get` shows today's usage. |
| `security-high-severity` | An exploit attempt, a sustained attack, a sign-in lockout, or an incident opened by hand at high severity. | `python scripts/admin_cli.py security list`. What each kind is and what to do: [security-incidents.md](architecture/security-incidents.md). |

## The Bedrock budget

The other email you can get is not an alarm. It is an AWS budget, `<prefix>-bedrock-spend`
(`aws_budgets_budget.bedrock_spend` in `infra/bootstrap/main.tf`).

| What | Value |
|---|---|
| Created | Only when bootstrap's `budget_alert_email` is set. Once per account, since bootstrap is applied per account. |
| Watches | The month's spend on Amazon Bedrock alone. |
| Limit | `bedrock_budget_limit_usd`, default US$20 a month ([Bootstrap variables](configuration.md#bootstrap-variables)). |
| Emails | `budget_alert_email`, directly, with no confirmation to click. |
| When | Once **actual** spend passes 80% of the limit, and once the **forecast** for the month passes 100%. |

## Dashboards

CloudWatch → Dashboards. Every panel reads the deployment's home region, except the CloudFront
ones, which read `us-east-1`: CloudFront publishes its metrics there only.

| Dashboard | Environment | Opens on | Shows |
|---|---|---|---|
| `<prefix>-<env>-pipeline` (`aws_cloudwatch_dashboard.pipeline`) | both | the last 7 days, one point per hour | Per Lambda in the list above: runs, errors and throttles, with average duration in ms on its own axis. The daily-cycle state machine's succeeded and failed executions, next to how many messages wait on the dead-letter queue. A table of the latest 50 lines matching `unhandled exception`, `Traceback`, `ERROR` or `Task timed out` across every listed Lambda's log. |
| `<prefix>-<env>-lambda-runs` (`aws_cloudwatch_dashboard.lambda_runs`) | both | the last 24 hours | Number tiles that add up the **whole** range picked at the top right: each Lambda's runs, and feedback accepted, comments kept, rejected, dropped by the screening model, and rejected because the day's model checks were used up. Below them, a stacked chart of runs per day. |
| `<prefix>-production-edge` (`aws_cloudwatch_dashboard.edge`, in `api_waf_dashboards.tf`) | **production only** | the last 7 days, one point per hour | API Gateway and the firewall: the [panels are listed below](#the-edge-dashboard-panel-by-panel). |

**Dev has no edge dashboard.** It is created only where `edge_dashboard_enabled` is true, and only
production's `module "observability"` sets it. CloudWatch bills about US$3 a month for every
dashboard past an account's first three ([costs](production-runsheet.md#what-it-costs-list-prices-roughly)),
and dev's traffic is mostly yours. To debug dev's edge for a while, set
`edge_dashboard_enabled = true` in dev's `module "observability"` block, by pull request. With two
accounts, its panels for the shared web ACL stay empty
([why](deployment-separate-accounts.md#logs-alarms-and-dashboards)).

**Panels that read logs cost money when opened.** The recent-errors table and the edge
dashboard's access-log and WAF-log panels are Logs Insights queries, billed per GB scanned each
time the dashboard loads them.

### The edge dashboard, panel by panel

| Part | Panels |
|---|---|
| Each API (the public API and the admin API) | Requests, 4XX and 5XX per hour. Latency against integration latency (the Lambda's share), at p50, p90 and p99. From the access log: 429s per hour, responses by status, and 4XX/5XX by route with `errorType` (who answered: `THROTTLED`, `WAF_FILTERED` and so on). A row of totals for the selected range sits above them. |
| The public API's cache | Requests at the CDN against requests that still reached API Gateway: the gap is what the cache answered. The CDN's 4xx and 5xx error rates. The cache hit rate, only when the distribution's additional metrics are on. These read `us-east-1`. |
| Each web ACL that exists | Allowed, blocked and counted requests. Blocked per rule, rate limits included. Allowed and counted per rule. The top blocked rule, address and path, from the WAF log. The address is the `x-viewer-ip` header for a request that came through the CDN, else the client address. |
| The shared CloudFront ACL | Allowed, blocked and counted requests. Blocked per rule, rate limits included. The top blocked rule, address and path, from the WAF log. All read in `us-east-1`. There is no allowed-and-counted-per-rule panel for it. Its numbers are the whole site's, every distribution it is attached to. |

WAF only reports a number above zero, so a rule that blocked nothing draws no line. A web ACL
that a cost-saving switch turned off is left off the dashboard
([cost-saving.md](cost-saving.md)).

## Logs

CloudWatch → Log groups. Terraform makes each of these groups with its retention set, so none of
them grows for ever.

| Log group | Environment (region) | Kept | What it holds |
|---|---|---|---|
| `/aws/lambda/<prefix>-<env>-<function>`, one per app Lambda (`aws_cloudwatch_log_group.lambda`) | both | 90 days | What the function prints. The public-api Lambda's feedback lines hold a reason code or a tag, never a comment. |
| `/aws/lambda/<prefix>-<env>-ops-mcp` and `-ops-agent` (`infra/modules/ops-assistant`, `log_retention_days`) | both | 90 days | The assistant. The agent logs which turn it was, which tools were called and how many findings came back: never the question, the answer or the token. |
| `/aws/lambda/<prefix>-<env>-vision-worker` (`infra/modules/vision-worker`) | where the worker is on (`vision_region`) | 90 days | The vision worker. |
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
| `failed-executions` | every record: failed daily cycles from the dead-letter queue, and prices the model catalog put back (`kind: model_pricing`) | 7 days after it is written | `CLEANUP_TTL_DAYS` (`put_failed_execution`) |
| `model-config` | the feedback limiter's counters (`feedback-window#…`, `feedback-day#…`, `feedback-screen#…`) | 2 days after their window or day ends | `_ROW_TTL_SLACK` in `lambdas/common/feedback_limits.py` |
| `model-config` | a used feedback token (`nonce#…`) | an hour after the token expires | `lambdas/common/feedback_verification.py` |
| `model-config` | a model catalog pricing run (`pricing-run#…`) | 14 days after it ends | `RUN_ROW_DAYS` in `lambdas/common/model_catalog.py` |
| `security-events` | every incident | 120 days after its last request | `RETENTION_DAYS` in `lambdas/common/security_events.py` |
| `sign-ins` | every sign-in event | 120 days after it happened | `RETENTION_DAYS` in `lambdas/common/sign_ins.py` |
| `operator-suggestions` | every row of the assistant's memory | 30 days after it was last mentioned | `RETENTION` in `lambdas/ops_mcp/memory.py` |
| `ops-briefings` | each user's latest briefing (each new one overwrites it) | 2 days after it is written | `RETENTION` in `lambdas/ops_mcp/briefings.py` |

**Never expire:** `topics`, `articles`, `feedback`, `musings`, `models`, `stats-current`,
`stats-history`, `view-counts`, and the settings rows in `model-config`. Stored feedback is kept
until you delete it.

In production, every app table also has point-in-time recovery: any second of the last 35 days
can be restored (`protect_data` in `infra/modules/app-data`). Dev's tables have none.

### Buckets

| Bucket | Rule | What it does | Set in |
|---|---|---|---|
| `<prefix>-dev-content` | `expire-old-snapshots` | Deletes the raw source snapshots (`snapshots/`) 21 days after they are written. | `aws_s3_bucket_lifecycle_configuration.content`, `infra/environments/dev/main.tf` |
| `<prefix>-production-content` | `expire-old-snapshots` | Retires the snapshots (`snapshots/`) after 21 days. The bucket is versioned, so this leaves an old version, which the next rule removes. | `aws_s3_bucket_lifecycle_configuration.content`, `infra/environments/production/main.tf` |
| `<prefix>-production-content` | `expire-old-versions` | Deletes any replaced or deleted version 30 days after it stopped being current, and any unfinished multipart upload after 7 days. Article bodies and figures are kept for good. | the same |
| `<prefix>-<env>-site` | none | Not versioned. Each deploy uploads the site again, and an article's page stays until it is unpublished. | `infra/modules/static-site` |
| `<prefix>-<env>-vision-artifacts-<account id>-<vision_region>` | `expire-old-packages`, `abort-incomplete-uploads` | Deletes worker packages (`vision-worker/`) after 14 days, and unfinished uploads after 7. | `infra/modules/vision-worker` |
| The bootstrap state bucket | none | Versioned, and every version is kept: it is the state's rollback history. | `infra/bootstrap/main.tf` |

The snapshots outlive the 14-day Findings by a week on purpose. TTL can delete a Finding up to two
days late, and the daily cycle may still read its snapshot until then.

**Backups** are in the state bucket, under `backup/<prefix>-<env>/snapshots/`. The weekly schedule
keeps its newest 8 snapshots and deletes older ones after each run (`KEEP_SCHEDULED` in
`lambdas/common/backup.py`). A snapshot you take by hand is never deleted. A deleted one can still
be recovered, because the bucket is versioned. What a snapshot holds, and leaves out:
[scripts/README.md](../scripts/README.md#backups-restores-and-article-exports-backup).

### What that means for you

- **A failed daily cycle drops out of `failed-executions list` after 7 days**, and nothing replays
  it. Look within the week.
- **`moderation stats` sees rejected items from the last 7 days only.** Pending and approved ones
  are counted from the start.
- **A Finding is gone after 14 days**, which is why a topic's findings window is at most 336 hours.
- **A security incident is gone 120 days after its last request.** Note anything you need to keep
  before then.
- **None of the expiring rows is in a backup.** A snapshot leaves out Findings, candidate ideas,
  failed executions, security events, sign-ins and any row already set to expire.
- **A scan by hand can still show expired rows**, for up to about two days.

## Where the code is

- The alert topic, the alarms and the pipeline and lambda-runs dashboards:
  `infra/modules/observability/main.tf`. The edge dashboard: `api_waf_dashboards.tf` beside it.
  The thresholds and switches: its `variables.tf`.
- Which functions are watched and which logs feed the security alarm:
  `local.pipeline_lambda_function_names` and `module "observability"` in
  `infra/environments/{dev,production}/main.tf`.
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
