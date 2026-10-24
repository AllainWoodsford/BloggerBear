# The admin CLI and the admin API

How a decision the operator types becomes a change in the pipeline: the local script, how it
finds its environment and signs each request, the allowlist and IAM in front of the API, the one
Lambda behind it, and what that Lambda writes: topic rows and their schedules, decisions on
articles, models, settings and backups. Then what each group of commands does, the backup jobs
that run inside that Lambda, and how the CLI and the operator's assistant divide the work.

It exists because the admin side is deliberately hard to reach. There is no admin web page. The
admin API takes only IAM-signed requests, from the operator's addresses only, and a browser
cannot hold long-lived IAM credentials safely. A script on the operator's own machine can: it
signs with whatever AWS credentials that machine already has. Topics are runtime data, not
Terraform, so creating, pausing or rescheduling one is a command, not a deploy.

How to set it up and every command's options are in
[scripts/QUICKSTART.md](../../scripts/QUICKSTART.md) and
[scripts/README.md](../../scripts/README.md); every setting the CLI changes, and where it is
stored, is in [configuration.md](../configuration.md#dynamodb-settings-changed-with-no-deploy).
This page does not repeat them.

**In the assistant.** This page has no `feature` entry yet. Ask about its parts by name instead,
`architecture(name="admin-api")`, `"admin-acl"`, `"waf-admin"`, `"admin-api-access"` or
`"topic-schedules"`, or about the layer that holds it (`api`). "Why are my admin calls failing?"
is `api_errors(api="admin")`, and `investigate(symptom="api-errors", api="admin")` says where to
look yourself.

Resource names below use `<prefix>` (your `unique_name_prefix`) and `<env>` (`dev` or
`production`). Both environments build the same API from one module,
[`infra/modules/rest-api/`](../../infra/modules/rest-api/), each with its own URL, Lambda and
allowlist.

```mermaid
flowchart LR
    operator(["Operator<br/>a terminal"])
    subgraph local["1-2. The operator's machine"]
        cli["scripts/admin_cli.py<br/>review_inbox.py, gear_create.py"]
        creds["AWS credentials<br/>profile, SSO or environment"]
        skips[("review-skips.json<br/>what approve skipped")]
    end
    subgraph edge["3-4. The admin API"]
        acl["WAF web ACL<br/>the allowlist, default block"]
        apigw["API Gateway REST API<br/>&lt;prefix&gt;-&lt;env&gt;-admin-api<br/>resource policy, AWS_IAM on every route"]
    end
    subgraph handler["5. One Lambda"]
        fn["Lambda admin-api<br/>_ROUTES, checks, evidence lines"]
    end
    subgraph writes["6-8. What it writes"]
        tables[("topics, articles, moderation-queue,<br/>prompt-refinements, models,<br/>model-config, musings, ...")]
        sched["EventBridge Scheduler<br/>two schedules per topic"]
        site[("S3 site bucket<br/>articles/&lt;id&gt;.html")]
        state[("S3 state bucket<br/>backup/&lt;prefix&gt;-&lt;env&gt;/")]
    end
    weekly["Schedule weekly-backup<br/>Sundays 3 AM Sydney"]
    pipeline["Lambdas research-tick<br/>and daily-cycle"]
    bedrock["Amazon Bedrock<br/>musings, gear names"]
    assistant(["The operator's assistant<br/>reads, suggests commands"])

    operator --> cli
    cli -. "signs with" .-> creds
    cli --> skips
    cli -- "SigV4 over HTTPS" --> acl --> apigw --> fn
    fn --> tables
    fn --> sched
    fn -- "approve, publish" --> site
    fn -- "trigger, Re-Write (async)" --> pipeline
    fn --> bedrock
    fn -- "backup jobs" --> state
    weekly -- "backup_job" --> fn
    fn -- "next backup step (async)" --> fn
    sched -- "heartbeat; daily run via Step Functions" --> pipeline
    assistant -. "reads" .-> tables
    assistant -. "a command to copy" .-> operator
```

## The steps

| # | Step | What happens | Where in the code / infra |
|---|---|---|---|
| 1 | **The command** | A plain Python script on the operator's own machine; it is not deployed anywhere. Twenty-three groups of commands (`topics`, `approve`, `models`, `model-catalog`, `pipeline-config`, `backup`, ...), each with its own `--help`. It needs two things: the API's URL, from `BLOGGERBEAR_ADMIN_API_URL` or `--api-url` (Terraform's `admin_api_url` output, which ends in the stage name), and the Region, from `--region`, `AWS_REGION` or `AWS_DEFAULT_REGION`. Without either it stops with an error before any request. | [`scripts/admin_cli.py`](../../scripts/admin_cli.py) (`build_parser`, `_resolve_api_url`, `_resolve_region`); [`scripts/requirements.txt`](../../scripts/requirements.txt); [your own machine](../configuration.md#your-own-machine) |
| 2 | **Signing** | Each request is signed with SigV4 for `execute-api` in that Region, with the credentials botocore's default chain finds: an `aws configure` profile, `AWS_PROFILE`, SSO or environment variables, the same chain the AWS CLI uses. No credentials, no request. It is sent with `requests`, a JSON body where there is one, and a 30 s timeout. | `signed_request` in `admin_cli.py`; `review_inbox.Api` wraps the same function for `inbox`, `approve`, `equipment create` and `equipment delete` |
| 3 | **The allowlist** | Only the operator's addresses (the allowlist parameter, which `scripts/admin_allowlist.py` writes and copies out) get in. The API's own **resource policy** denies `execute-api:Invoke` from any other address; it is always on, costs nothing, and until the script has run it denies every call. The regional **WAF web ACL** is a second layer over the same list, with a default action of block; it exists while `WAF_REGIONAL_ENABLED` and `WAF_ADMIN_ALLOWLIST` are on (the default) and logs every request unless `WAF_LOG_ALL_REQUESTS` is off. A refused caller gets a 403 and no Lambda runs. A request the firewall blocked is counted into an `admin-denied` security incident; with the firewall switched off, the resource policy's 403s are counted only with the 4xx answers in step 9. | `local.admin_api_resource_policy`, `aws_wafv2_ip_set.admin_allowlist`, `aws_wafv2_web_acl.admin` in `infra/environments/*/main.tf`; [`admin_allowlist.py`](../../scripts/admin_allowlist.py); `aws_api_gateway_rest_api_policy.this` in [`rest-api/main.tf`](../../infra/modules/rest-api/main.tf); [the allowlist is always on](../cost-saving.md#the-admin-apis-allowlist-is-always-on) |
| 4 | **IAM and the routes** | One regional REST API, `<prefix>-<env>-admin-api`, its stage named after the environment. Every method has `AWS_IAM` authorization: before the Lambda runs, API Gateway checks the signature, and the caller, an IAM identity in this account, must be allowed the call by its own IAM policy. It has 71 routes, each listed in Terraform, and all proxied to one Lambda. The stage is throttled at 10 requests a second, bursts of 20, which one operator never reaches; it caps what a leaked credential or a runaway script could do. Its access log holds what happened to each request, never an address or user agent. | `module "admin_api"` in `infra/environments/*/main.tf` (`routes`, `authorization = "AWS_IAM"`); [`infra/modules/rest-api/`](../../infra/modules/rest-api/); `/aws/apigateway/<prefix>-<env>-admin-api-access` |
| 5 | **The handler** | `<prefix>-<env>-admin-api` (Python 3.11, 30 s, 256 MB) is built from the pipeline's shared package and runs as its shared execution role. An event with a `backup_job` key is not a request: it comes from the weekly backup schedule or from the function itself, and goes to `_run_backup_job` before any route is looked up ([backup jobs](#backup-jobs)). For a request, it builds a route key from the method and resource (`"POST /topics"`) and looks it up in `_ROUTES`: one plain function per route. Each checks the request first, and answers 400, 404 or 409 with a reason. A few write in steps, so a later step can still fail: a topic's row is written before its schedules, so a time zone EventBridge Scheduler refuses (the API checks only its shape) answers 500 and leaves the row, perhaps with one schedule; a Re-Write that cannot be started answers 502, and the item goes back to the inbox or the published article stays up as it was. A route never raises: anything unexpected is logged and answered with a plain 500. A route that decides about an article, or creates, changes, pauses or resumes a topic, also prints one evidence line, `moderation_decision` or `topic_changed`: what changed and when, never who. | [`lambdas/admin_api_handler.py`](../../lambdas/admin_api_handler.py) (`handler`, `_route_key`, `_ROUTES`, `_run_backup_job`); [`lambdas/common/backup.py`](../../lambdas/common/backup.py); [`lambdas/common/dynamo.py`](../../lambdas/common/dynamo.py); [`lambdas/common/evidence.py`](../../lambdas/common/evidence.py) |
| 6 | **Topics and their schedules** | `topics create` writes the topic's row, with defaults where nothing was given (the `web_search` adapter, an hourly research heartbeat, the daily article at 9 AM `Australia/Sydney`) and the next free stagger slot. Two settings cannot be turned off: a `crypto_feed` topic is always financial, and a `satellite_vision` or `rail_access` topic always goes to a person (`force_manual_review`). Then two EventBridge Scheduler schedules are written in the `default` group: `<prefix>-<env>-<topic_id>-research-tick`, which invokes the research tick (read in UTC), and `-daily-cycle`, which starts the daily cycle's state machine at the topic's daily cadence in its own time zone, each at the minute its slot gives. Both pass `{"topic_id": ...}` and run as the scheduler's own role. The expressions as written go back on the row. `update` rewrites both; `pause` and `resume` set them DISABLED or ENABLED; `delete` removes both and the row. | `_create_topic`, `_update_topic`, `_apply_topic_schedules`, `_set_topic_paused` in `admin_api_handler.py`; [`lambdas/common/scheduler.py`](../../lambdas/common/scheduler.py), [`common/stagger.py`](../../lambdas/common/stagger.py); `aws_iam_role_policy.scheduler_manage` (only `<prefix>-<env>-*` schedules, and PassRole on `scheduler_invoke` alone) |
| 7 | **Running a topic by hand** | `topics trigger` invokes the research tick or the daily cycle's Lambda directly and asynchronously, so the API answers 202 at once. A manual research tick ignores the topic's research interval. A manual daily cycle skips the state machine, so a run that times out or crashes is not retried, by the state machine or by Lambda (the function's async retries are off), and never reaches the dead-letter queue or `failed-executions`. `--force` (daily cycle only) writes from the topic's whole findings window, not only from what is newer than its last article. `network_prefetch` asks a `rail_access` topic's research tick only to fetch its rail networks. A paused topic is refused with a 409. The CLI then polls the API every 3 s: for a new Finding (up to 30 s) or new candidate ideas (up to 90 s), unless `--no-wait`. It never waits for `network_prefetch`, whose result is a line per site in the research tick's log. | `_trigger_topic`; `_cmd_topics_trigger` in `admin_cli.py`; `aws_iam_role_policy.lambda_invoke_pipeline` (the research tick, the daily cycle and the model catalog, plus the admin-api Lambda itself for backup steps); `aws_lambda_function_event_invoke_config.daily_cycle` |
| 8 | **Decisions on articles** | `inbox` counts what is waiting, and `approve` goes through it one key at a time: the moderation queue's pending articles, then the pending prompt changes, each oldest first, up to 30 at a time (`--limit`), with each article's text read from `GET /articles/{article_id}`. Approving an article marks it published by a person, renders its static page into the site bucket (its figures too), writes a musing, and refreshes the Stats snapshot. Rejecting keeps it private and writes a musing that names only the topic. A Re-Write claims the queue item and hands it to the daily-cycle Lambda asynchronously; the result comes back to the inbox as a new item. `articles unpublish` deletes the page, marks the article rejected (its text is kept, so it can be published again), removes its musings and asks CloudFront to drop it. Each choice is sent the moment the key is pressed. A skip changes nothing in AWS: it is kept in a local file. | [`scripts/review_inbox.py`](../../scripts/review_inbox.py); `_resolve_moderation_item`, `_render_published_page`, `_publish_article`, `_unpublish_article`, `_rewrite_article`, `_rewrite_moderation_item`; [`common/static_pages.py`](../../lambdas/common/static_pages.py), [`common/rewrite.py`](../../lambdas/common/rewrite.py), [`common/musings.py`](../../lambdas/common/musings.py) |
| 9 | **The answer** | The API answers JSON, and the CLI prints it indented. A non-2xx answer is printed to stderr and the command exits 1; `inbox` and `approve` say what failed and carry on. `topics schedule` prints a table instead, and `approve` ends with a summary of what was decided, however it ended. The Lambda's log holds each unhandled error and the evidence lines; the access log's 4xx answers, less those the firewall refused, are counted per hour into an `admin-api-errors` incident (20 opens it, 50 makes it medium, 100 high). | `_print_response_and_exit_on_error`, `_print_schedule_table`; `/aws/lambda/<prefix>-<env>-admin-api`; [`lambdas/common/security_events.py`](../../lambdas/common/security_events.py) |

## The commands

Each command makes signed calls to the routes above (`--mock` makes none); nothing in the CLI
reaches DynamoDB or S3 directly. The options are in [scripts/README.md](../../scripts/README.md).

| Commands | What they do | What they write |
|---|---|---|
| `topics` (list, get, create, update, delete, pause, resume, schedule, reschedule, trigger, candidates, findings) | A topic's settings: adapter and config, whether it is financial, cadences and time zone, research interval, findings window, editorial goals and goal rotation, review mode, its own model, its stagger slot, and whether the reviewer bear may approve its articles (`update --reviewer-may-approve`). `schedule` shows every topic's schedules in words and as written; `reschedule` rewrites them all after the stagger changes or a topic is added. | `<prefix>-<env>-topics`; EventBridge Scheduler; `musings` (a pause or a resume) |
| `findings list` | A topic's research Findings, newest first; with `--next-article`, exactly what its next article will be written from. | Nothing |
| `inbox`, `approve`, `moderation` | What is waiting for a person, and the decision on each held article and prompt change. `moderation stats` sums up what compliance review has flagged. | `articles`, `moderation-queue`, `prompt-refinements`, `musings`; the site bucket; the Stats snapshot in `stats-history` |
| `articles` (get, checked, publish, unpublish, rewrite, feedback-lock, feedback-unlock) | `get` shows one article in any status, with its text; `checked` clears the reviewer bear's flag on a financial article he approved, once you have read it. A person's override: publish whatever the status, take a published article down, rewrite one to fix what the operator says is wrong, stop or restart feedback on one. | `articles`, `moderation-queue`, `musings`; the site bucket; a CloudFront invalidation; the Stats snapshot in `stats-history` |
| `models`, `model-config`, `model-rotation` | The model registry (each model's name, provider, price per 1,000 tokens, and whether it is enabled), the global default and fallback model, and the shared rotation pool: the models a topic's `model_id` moves to after an article the daily cycle publishes by itself, or a run with no usable angle. Each model in the pool must be a registered, enabled, callable (not `access: denied`) text model, and `model-rotation set` replaces the whole list. A topic's own model is `topics update --model-id`, `--fallback-model-id` or `--model-candidates`. A registered price wins over the built-in one when a call's cost is worked out ([`common/costing.py`](../../lambdas/common/costing.py)). Which model a call uses, and rotation: [models.md](../models.md). | `<prefix>-<env>-models`; the `default` and `model-rotation` rows of `<prefix>-<env>-model-config` |
| `models discover`, `models reprice`, `model-catalog` | The model catalog ([models.md](../models.md#the-model-catalog-discovery-prices-and-the-weekly-cycle); design: [model-catalog-refresh.md](../enhancements/archive/model-catalog-refresh.md)). `models discover` and `models reprice` each run their job now, even when it is paused. Discover adds up to 5 models the account can call, from every provider, switched off. Reprice looks up as many as 50 prices again; each change is checked 1 and 2 hours later and put back if a check cannot confirm it. Both commands start the model-catalog Lambda in the background and answer at once; `models reprice` is refused (409) while a pricing run is still under way. `model-catalog get` and `set` cover the catalog's three pause flags (discover, reprice and the weekly rotation cycle), its sizes, pruning and how a change is checked. `models add --lock-price` keeps the reprice off a price. | `model-catalog set`: the `model-catalog` row of `model-config`. The rest write nothing directly; the model-catalog Lambda writes `models` (discover adds rows, reprice changes prices, pruning deletes rows), the `model-catalog-state`, `pricing-run#<id>`, `model-archive` (a pruned model's price) and `model-access-denied` (a model a probe found it cannot call) rows of `model-config`, each topic's rotation list (`model_id_candidates` in `topics`, from the weekly cycle), each pricing run's one-shot review and tick schedules in EventBridge Scheduler, `failed-executions` (a price put back) and `pricing/` in the content bucket |
| `pipeline-config` | The pipeline-wide settings: the research interval, the fresh-data review's mode and what it does when it cannot run, who may reach the assistant (`assistant_access`), and the stagger. Each is read where it is used, with no deploy; a new stagger also needs `topics reschedule`. | the `pipeline` row of `model-config` |
| `feedback-config` | When the site takes reader feedback: lockdown, rate, daily and per-article limits, the model check's daily limit, verification tokens and proof of work. | the `feedback` row of `model-config` |
| `refinements`, `equipment` | Approving or rejecting a prompt change, and the gear: where each piece is worn, making one by hand, raising its rarity, repairing it, deleting it, and the loot drop on the Musings. | `prompt-refinements`, `musings` |
| `musings` | List the musings, put your own words on one, or have one written again. | `musings` |
| `backup` (list, snapshot, restore, export, status, resume) | `snapshot` writes one snapshot file of the content and configuration; a schedule also takes one every Sunday at 3 AM `Australia/Sydney`. `restore` loads one into a deployment, and asks you to type `restore` first unless `--yes`. `export` copies every article (its row, text and figures) to a bucket. `list` shows the snapshot files and the 10 most recent jobs. A snapshot, restore or export runs as a job inside the admin-api Lambda: each step does about 20 seconds of work, saves where it got to and invokes the function again for the next, as a `backup_job` event, not a route ([backup jobs](#backup-jobs)). The CLI follows the job unless `--no-wait`; `status --wait` picks it up again, and `resume` restarts a stalled one. How to use them: [scripts/README.md](../../scripts/README.md#backups-restores-and-article-exports-backup). | `backup/<prefix>-<env>/` in the backup bucket, the bootstrap's Terraform state bucket (snapshots, job state, and an export given no `--bucket`). A restore writes every row in the file back to its table, the articles' text and figures to the content bucket, the topics' schedules (unless `--no-schedules`), the published pages to the site bucket (unless `--no-pages`) and the Stats snapshot in `stats-history`. An export with `--bucket` writes that bucket, which must be one of the buckets in the `backup_export_buckets` Terraform variable ([configuration.md](../configuration.md#environment-terraform-variables)) |
| `reviewer` (get, set) | The reviewer bear's tasks, limits and pause flag, and whether he may approve held articles himself (a topic's own setting wins). Each is read when he runs, with no deploy. | the `reviewer` row of `model-config` |
| `security`, `sign-ins` | Security incidents, moved from open to acknowledged to resolved, or opened by hand; the assistant's sign-in log, and clearing a lockout. | `security-events`, `sign-ins` |
| `review report`, `lineage`, `stats backfill-articles`, `failed-executions list` | How the fresh-data review is doing; articles with no lineage or cost, and their repair; a one-off fold of past costs into the Stats history; the daily runs the dead-letter queue caught. | `lineage backfill --apply`: `articles` and the Stats snapshot; `stats backfill-articles --apply`: `stats-history`; the rest nothing |

The Lambda's own model calls are few: a musing (on a publish, a reject, a pause or a
resume, a loot drop, or `musings regenerate`) and a name for hand-made gear. A Re-Write's model
calls are made by the daily-cycle Lambda, not by this one.

## Backup jobs

The admin-api Lambda is invoked two more ways, both for `backup` and neither through API Gateway.
Each event carries a `backup_job` key, and `handler` sends it to `_run_backup_job` before it looks
for a route. If you change this function's timeout, its IAM or the start of `handler`, keep both
working.

**The weekly snapshot.** Every Sunday at 3 AM `Australia/Sydney`, the schedule
`<prefix>-<env>-weekly-backup` (`aws_scheduler_schedule.weekly_backup`) invokes the function with
`{"backup_job": {"start": "snapshot", "trigger": "schedule"}}`. It runs as the scheduler's own role
(statement `InvokeWeeklyBackup`). This starts a snapshot job, the same as `backup snapshot`. Once
the file is written, all but the newest 8 scheduled snapshots are deleted.

**Each step.** A job can need more than one 30-second invocation, so it runs in steps. A step
works for about 20 seconds and saves the job to `backup/<prefix>-<env>/jobs/<job_id>.json` in the
state bucket. Then it invokes the function again, asynchronously, with
`{"backup_job": {"job_id": ..., "step": n}}` (`invoke_step` in
[`lambdas/common/backup.py`](../../lambdas/common/backup.py)). The shared execution role may do this
for this one function only (statement `InvokeAdminApiBackupSteps` in `lambda_invoke_pipeline`).
Each step prints one progress line in `/aws/lambda/<prefix>-<env>-admin-api`.

**When a step fails.** A `backup_job` event is not a request, so an error is raised, not answered
with a 500. Lambda's own retries for an asynchronous call then run the step again. A step whose
number is not the job's current one is a duplicate, and does nothing. A fault a retry cannot fix,
such as a snapshot file cut off before its end line, marks the job `failed`, with the reason.

**A stalled job.** `backup status` and `backup list` mark a running job `"stalled": true` once its
saved state is more than 180 seconds old; its `status` stays `running`. While a job is being
followed (`backup status <job_id> --wait`, or a snapshot, restore, export or resume run without
`--no-wait`), its progress line, still `[running] ...`, ends with ``[STALLED: run `backup resume`]``.
That means its next step was lost, ran out of retries, or is failing and still has a retry to come.

A failing step has usually not used up its retries at 180 seconds. This function sets no retry
config of its own, so Lambda's defaults apply: two retries, about 1 minute and then about 2 more
minutes after each failure. The last retry can start up to about 4 minutes after the saved state.
Before you run `backup resume <job_id>`, look for the step's error in
`/aws/lambda/<prefix>-<env>-admin-api`: three errors (the first run and both retries) mean its
retries are spent. Otherwise wait until the saved state is about 5 minutes old. A resume while a
retry is still to come can run the same step twice at once. `backup resume` starts the current step
again, from where the job got to. It answers 404 for an unknown job, and 409 for one that has
already succeeded or failed, or is still moving.

| Limit | Value | Set in |
|---|---|---|
| The Lambda's timeout | 30 s | `aws_lambda_function.admin_api` in `infra/environments/*/main.tf` |
| A step stops starting new work | when less than 10 s is left; a snapshot step also stops once it holds 24 MB of lines | `_RESERVE_MS`, `_MAX_STEP_BYTES` in `backup.py` |
| A running job counts as stalled | after 180 s with no change | `STALLED_AFTER_SECONDS` in `backup.py` |
| Scheduled snapshots kept | the newest 8; one taken by hand is never deleted | `KEEP_SCHEDULED` in `backup.py` |
| Jobs `backup list` shows | the 10 most recent | `list_jobs` in `backup.py` |

## Where state lives

| What | Where |
|---|---|
| The API's address and Region | The operator's shell (`BLOGGERBEAR_ADMIN_API_URL`, `AWS_REGION` or `AWS_DEFAULT_REGION`), or `--api-url` and `--region` on each command |
| The credentials | The operator's own AWS configuration; nothing in the repo |
| What `approve` skipped | `~/.bloggerbear/review-skips.json` (or `BLOGGERBEAR_REVIEW_STATE`): each skip hides its item for 24 hours by default. Practice mode (`--mock`) keeps its own file. Never sent anywhere |
| Topics, and the schedules as written | `<prefix>-<env>-topics`: `stagger_slot`, `paused`, `research_schedule`, `daily_schedule` on each row |
| The schedules themselves | EventBridge Scheduler, group `default`; made by the admin API, not by Terraform |
| Pipeline settings, the default model, the shared rotation pool, feedback limits | `<prefix>-<env>-model-config`, rows `pipeline`, `default`, `model-rotation` and `feedback` |
| The model catalog's pause flags and checks, what it last did, and the pricing run under review | `<prefix>-<env>-model-config`, rows `model-catalog`, `model-catalog-state` and `pricing-run#<id>`; the job schedules are Terraform's (`model_catalog_discover` and `model_catalog_reprice` every second day, `model_catalog_cycle` weekly on Fridays); each run's review and tick schedules are the Lambda's own (`<prefix>-<env>-price-review-*` and `<prefix>-<env>-price-tick-*`, deleted once fired) |
| Backups: snapshots, job state and exports | The bootstrap state bucket, under `backup/<prefix>-<env>/`: `snapshots/<UTC time>-<trigger>.jsonl.gz` (the trigger is `manual` or `schedule`), `jobs/<job_id>.json` (a snapshot's parts wait in `jobs/<job_id>/` until its last step joins them), and `exports/<UTC time>/` for an export given no `--bucket`. Outside Terraform state, so destroying the environment leaves them. Only scheduled snapshots are ever deleted ([what is kept](../observability.md#buckets)) |
| The routes | `_ROUTES` in `admin_api_handler.py`, and `routes` in `module "admin_api"` in each environment's `main.tf` |
| Logs | `/aws/lambda/<prefix>-<env>-admin-api`, 90 days; the access log, 30 days; the firewall's `aws-waf-logs-<prefix>-<env>-admin`, 30 days |

## The CLI and the assistant

The [operator's assistant](ops-mcp-agent.md) and the CLI split the work: **the assistant reads,
the CLI writes.** The assistant reads the same tables through its own read-only role and changes
nothing in the pipeline; the changes a person makes go through this API, signed from the
operator's own terminal.

- **A fix on screen.** Each problem the assistant reports carries a suggestion from a fixed
  catalogue: most are an `admin_cli` command with only a checked id filled in, and the rest say
  only where to look. Nothing in it deletes anything. A test parses every command in it with the CLI's
  own parser, so a renamed command or flag fails the build
  ([`suggestions.py`](../../lambdas/ops_mcp/suggestions.py)).
- **"How do I ...?"** The assistant's MCP server cannot import `scripts/`, so it reads
  [`cli_reference.json`](../../lambdas/ops_mcp/cli_reference.json), every command, flag and help
  text, written from `build_parser()` by
  [`generate_cli_reference.py`](../../scripts/generate_cli_reference.py). `cli_help` shows a
  command's own `--help`, `cli_guides` has eleven short guides (a first topic, gear, costs, vision,
  model prices, ...), and `cli_command` builds one exact command in code, each value one quoted shell word
  ([`cli_guide.py`](../../lambdas/ops_mcp/cli_guide.py)). A command that deletes or takes something
  down (`topics delete`, `articles unpublish`, the rejects, `equipment delete`), or uses `--force`,
  comes back only as a template with `<placeholders>`, and a vision topic's sites config is never
  filled in.
- **Settings for the assistant are made here.** `pipeline-config set --assistant-access` decides
  who may reach it. `sign-ins list` shows each user's sign-ins, failures, refusals and lockouts,
  and `sign-ins unlock` clears a lock.
- **The same views.** The assistant's `topics_overview` puts the topics' settings on screen as a
  table, and `topic_findings` the same window `findings list` prints. The `topic-findings` Claude
  Code skill runs those `findings list` commands itself, read-only.

## Adding a command

1. Write the route as a plain function in `admin_api_handler.py` and add it to `_ROUTES`. Check
   the whole request before writing anything, and answer with `_response` or `_error`.
2. Add the route to `routes` in `module "admin_api"` in both `infra/environments/dev/main.tf` and
   `infra/environments/production/main.tf`: API Gateway serves only the routes listed there. A
   path deeper than four segments also needs a `level5` resource block in the module.
3. If the route needs a new AWS permission, add it to the shared execution role in both
   environments' `main.tf`, scoped to the resources it needs and never to `*`.
4. Add the command to `build_parser()` in `admin_cli.py`, then run
   `python scripts/generate_cli_reference.py` and commit `cli_reference.json` with it; a test
   fails until it is regenerated. If the command deletes or takes something down and its last
   word is not already in `DESTRUCTIVE_VERBS` (in `generate_cli_reference.py`), add the word
   there, so the assistant never fills it in.
5. Describe it in [scripts/README.md](../../scripts/README.md), and in
   [configuration.md](../configuration.md#dynamodb-settings-changed-with-no-deploy) if it changes
   a setting.

## Design history

- [project-plan.md](../project-plan.md) §10 and §11: the admin console API and CLI in the original
  build, then "The review inbox", "Equipment", "Re-Write", "AI lineage, cost tracking, pluggable
  model routing" (the model registry), "Feedback limits" and "Staggered schedules by slot, pausing
  a topic, and the digest's steering".
- [cost-saving.md](../cost-saving.md): the firewall switches, and why the allowlist is also the
  API's resource policy.
- [dailysubject_hook.md](../enhancements/archive/dailysubject_hook.md): the `rotation` keys a topic's
  config carries, set with `topics create` or `topics update`.
- [dispatcher-sqs-enhancement.md](../enhancements/dispatcher-sqs-enhancement.md): proposed, not
  built. One dispatcher and a work queue in place of two schedules per topic, for when twelve
  slots no longer fit an hour.
