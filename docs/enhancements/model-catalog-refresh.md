# Enhancement: keep the model registry's list and prices up to date by themselves

**Status:** built (this PR) · **First draft:** 2026-10-19
**Background:** the Models registry and its prices ([project-plan.md §11](../project-plan.md),
"AI lineage/cost tracking"), which until now were only filled in by hand (`admin_cli.py models add`).

## What it does

Two weekly jobs look after the **Models** table (`<prefix>-<env>-models`):

| Job | Default | What it does |
|---|---|---|
| **discover** | Tuesdays, 03:00 Australia/Sydney | Lists every model this account can call in the deployment's region, **from every provider** (Amazon, Anthropic, Meta, Mistral, Cohere, AI21, DeepSeek, Stability, ...), with Bedrock `ListFoundationModels` and `ListInferenceProfiles`. Every model that is not in the registry yet is added with **`enabled: false`**. The ones that write text are priced like any other; image, video, speech and embedding models are listed with `pricing_unit: other` and left unpriced (the cost code only prices tokens). |
| **reprice** | Wednesdays, 03:00 Australia/Sydney | Looks up the current price of **every** token-priced model in the registry, enabled or not, and writes the prices that changed. |

A price that changed is applied **straight away**, but stays on probation. Two **reviews** run
later, **1 hour and 2 hours** after the run by default, each on a one-shot schedule the run
creates. Each one looks the price up again, on its own, and checks it:

- **Both pass:** the new price stays. A snapshot of the *old* prices, with the date, is written to
  the content bucket at `pricing/<date>-<run id>.json`.
- **A review fails** (the price can't be confirmed, or looks wrong): that model goes **back to its old
  price**. A **pricing failure** is written to FailedExecutions. The operator's assistant reports it
  in `pipeline_health`, and `admin_cli.py failed-executions list` shows it.
- **Nothing changed:** there is nothing to review, and no run or snapshot is created.

Both jobs can be paused, and the review delays and checks changed, without a deploy. The days and
time are the two schedules' cron expressions, in Terraform.

## Where things live

All of it sits in tables and a bucket that already exist. No new tables are needed.

**ModelConfig table**: three kinds of row, next to `default`, `pipeline` and `model-rotation`.

- **`model-catalog`** holds the settings, edited through `admin_cli.py model-catalog get|set`
  (`GET`/`PUT /model-catalog-config`) or straight in DynamoDB. A setting left unset takes its
  default.

  | Setting | Default | Meaning |
  |---|---|---|
  | `discover_enabled` | `true` | `false` pauses discover's weekly schedule |
  | `reprice_enabled` | `true` | `false` pauses reprice's weekly schedule |
  | `review_after_hours` | `[1, 2]` | one review per entry, that many hours after the run (1 to 4 entries, each 1–168) |
  | `review_tolerance_pct` | `5` | how far a review's price may be from the applied one |
  | `max_change_factor` | `10` | a new price more than this many times the old (or less than 1/this) fails review |
  | `max_web_lookups` | `10` | web searches one run may make (each is a paid AgentCore query) |

- **`model-catalog-state`** is written only by the job: `last_discover_at`, `last_reprice_at`,
  what each last did, and `active_run_id`. It is a separate row so that saving the settings can
  never wipe it.
- **`pricing-run#<run id>`** is one per run that changed a price. It holds each changed model's
  **old** and **new** prices, when each review is due, and what each review found. These are
  the "rows that get pruned". When a run ends (passed or reverted), its row gets
  `expires_at` = 14 days, and the table's TTL removes it.

**Models table**: new fields on a row. Every one is optional, so rows written by hand keep working.

| Field | Meaning |
|---|---|
| `region` | the region it was discovered in |
| `discovered_at` | when discovery added it |
| `pricing_unit` | `tokens` (priced per 1K input/output tokens, the only kind priced automatically) or `other` |
| `price_source` | `price-list` (AWS Price List API), `web-search`, or absent (set by hand) |
| `price_checked_at` | when a lookup last confirmed or set the price |
| `price_evidence_hash` | a hash of the web search results the price was read from. A reprice skips the model call when it hasn't changed (diff-first) |
| `price_locked` | `true` means the jobs never change this row's price. Use it for a price you set on purpose, e.g. a geo-profile premium |

**Content bucket**: `pricing/<YYYY-MM-DD>-<run id>.json` holds the old prices a passed run replaced:
`{"written_at", "run_id", "applied_at", "models": [{"model_id", "old", "new"}]}`. Unlike
`snapshots/`, there is no lifecycle rule on `pricing/`, so they are kept.

**FailedExecutions**: a pricing failure is an item with `kind: "model_pricing"`, `topic_id: null`,
`error: {"Error": "ModelPricingReverted", "Cause": "<models and why>"}` and the reverted models in
`raw_message`. There is one per review that put a price back. It has the table's usual 7-day TTL
(`CLEANUP_TTL_DAYS`).

## How it runs: two weekly schedules, and reviews that schedule themselves

One new Lambda, **`model-catalog`** (`lambdas/model_catalog_handler.py`), is invoked three ways:

| What invokes it | Event | Does |
|---|---|---|
| `aws_scheduler_schedule.model_catalog_discover`, `cron(0 3 ? * TUE *)` Australia/Sydney | `{"job": "discover", "trigger": "schedule"}` | discover, unless `discover_enabled` is `false` |
| `aws_scheduler_schedule.model_catalog_reprice`, `cron(0 3 ? * WED *)` Australia/Sydney | `{"job": "reprice", "trigger": "schedule"}` | reprice, unless `reprice_enabled` is `false` |
| `admin_cli.py models discover` / `models reprice` (`POST /models/discover`, `/models/reprice`) | `{"job": ...}` | that job now, **even when it is paused** (an asynchronous invoke; the API answers `202`) |
| a run's review schedules | `{"job": "review", "run_id": ...}` | that run's next review |

03:00 Sydney is quiet: no topic's daily cycle runs then, and a job makes a few API calls.

**Reviews.** A run that changed a price creates one EventBridge Scheduler schedule per review:
`at(<time>)` in UTC, named `<prefix>-<env>-price-review-<run id>-<n>`, with
`ActionAfterCompletion: DELETE`, so it removes itself once it has fired. A rerun from the CLI
creates its own two, so it is checked exactly like a scheduled one. A review whose schedule
fires for a run that is no longer under review, or before its time, does nothing.

**Nothing can hold repricing up for good.** Before a job runs:
- a review more than 30 minutes overdue (its schedule was lost, or could not be created) is run
  then and there, in turn, until the run is closed;
- a slot claimed by a run that died before writing its row is freed after an hour.

**One run under review at a time.** `reprice` does not start while another run is under review,
so two runs never hold different "old" prices for one model. On its schedule it records
`review_pending` and runs again the next week; `models reprice` is refused (`409`) unless the run's
review is overdue, in which case the job runs that review first. `discover` always adds new
models; it prices them only when no run is under review, and otherwise leaves them to the next
reprice.

**Pausing** stops a job's weekly schedule only. The reviews of a run that has started still
finish, so pausing never strands an applied-but-unchecked price.

**SQS was considered and not used.** The jobs run twice a week and make a few API calls. If the
dispatcher work queue ([dispatcher-sqs-enhancement.md](dispatcher-sqs-enhancement.md)) is built,
the schedules can enqueue `{"job": "model-discover"}` instead of invoking the Lambda. Nothing here
would change.

`admin_cli.py model-catalog get` shows the run under review and when its next review is due.

## Where a price comes from

Prices are looked up for every model in the registry (enabled or not, discovered or added by hand)
that is `pricing_unit: tokens` (a row without one counts as tokens) and has no `price_locked`.
Sources are tried in order:

1. **The AWS Price List API** (`pricing:GetProducts` through its us-east-1 endpoint, filtered to the
   deployment's region). Bedrock prices have been reported under more than one service code, so
   `AmazonBedrock`, `AmazonBedrockFoundationModels` and `AmazonBedrockService` are all read; a code
   that does not exist reads as nothing. Which models the Price List covers in this region has not
   been checked against a real account yet: whatever it misses falls through to the web search.
   - Only on-demand input and output token prices are read: batch, cache, provisioned and
     latency-optimised prices are left out.
   - A price per 1M tokens is turned into per 1K.
   - A product is matched to a registry row by its `model` name against the row's
     `display_name`, ignoring case, spaces and punctuation.
   - A model that matches more than one price for the same side is **ambiguous** and not priced
     from here: a wrong price is worse than none.
   - No model call is made.
2. **Web search.** The AgentCore web search gateway (infra/modules/web-search) is asked for the
   model's Bedrock price, and the results are read by a model.
   - The model is the deployment's default (`common/model_routing.resolve_model`). It answers in
     JSON only, and only with prices the results state for that exact model; otherwise `null`.
   - Its tokens are tallied on the Stats page as **Model pricing**.
   - **Diff-first:** when the search results hash the same as last time, the model isn't asked
     again and the price is left as it is.
   - At most `max_web_lookups` searches are made per run.

A source that can't price a model leaves it as it is. A model nobody can price stays unpriced,
which the cost code already handles: it shows as unknown, never as $0.

## What a review checks

For each model the run changed, a review fails it if **any** of these is true:

- **The applied price is out of range:** not above 0, or more than 0.5 USD per 1K tokens
  ($500 per 1M tokens, far above any text model).
- **It moved too far** from the old price: by more than `max_change_factor`, either way.
- **A fresh lookup can't confirm it:** the lookup ignores the evidence hash and the
  `max_web_lookups` budget, and comes back with no price, or one more than `review_tolerance_pct`
  away. "Can't confirm" is a failure on purpose: the rule is to go back when we're not confident.

A model whose row no longer holds the run's new price (someone edited it by hand since) is left
alone: no check and no revert. It is dropped from the run, with a note.

A failed model is reverted at once. Its old price and source are put back; a model that had no
price before has its prices removed again. It also drops out of later reviews, and the review
writes one pricing-failure record listing every model it put back. The models left go on to the
next review. After the last review (or as soon as no model is left on probation), the models still
standing are written to the S3 snapshot and the run is closed.

## What the operator sees

- `admin_cli.py model-catalog get` shows the settings and their effective values, the last
  discover and reprice results, and the run under review.
- `admin_cli.py models list` shows the registry, now with `enabled`, `price_source` and
  `price_checked_at`.
- **The assistant, on the page and through Alexa+** (both use the same MCP tools):
  - `model_catalog` (ops_mcp/model_prices.py) says whether discover and reprice are paused, what
    each last did, and the pricing run under review with when its next check is. Alexa+ hears
    counts and times only; the model ids are on the page.
  - `pipeline_health` adds a `model_pricing_reverted` finding for each pricing failure in the last
    7 days. It suggests `models list` to see the prices now in use and `models reprice` to try
    again. It has no id, so it is read fresh each time and never remembered (ops_mcp/memory.py).
  - `cli_guides` `model-prices` explains the catalog and puts the help of `model-catalog get|set`,
    `models reprice|discover` and `models add` on screen, each with a suggested command.
    `cli_help` and `cli_command` know the new commands from the regenerated `cli_reference.json`.
  - `architecture` describes the model-catalog Lambda, its rows in `model-config`, the reverted
    prices in `failed-executions` and `pricing/` in the content bucket.

## Permissions (Terraform, both environments)

The shared Lambda role gains:
- `bedrock:ListFoundationModels`, `bedrock:ListInferenceProfiles` and `pricing:GetProducts`. None
  of these supports resource-level permissions, so each is on `*`.
- The `lambda_invoke_pipeline` policy gains the model-catalog function, so the Admin API can
  start it.

The scheduler role gains `InvokeModelCatalog`, for the two weekly schedules and every review
schedule. The new Lambda joins the observability function list. Nothing else is new:
- the role already creates and deletes schedules named `<prefix>-<env>-*` and passes the scheduler
  role (`scheduler_manage`, for the topics' schedules), which covers the review schedules;
- it can already `PutObject` to the content bucket, for the snapshot.

Two environment variables, neither a switch and neither needing to be set
([configuration.md](../configuration.md#the-model-catalogs-environment-variables)):
`MODEL_CATALOG_FUNCTION_NAME` tells the Admin API which Lambda to start (Terraform sets it; unset,
it is `<prefix>-<env>-model-catalog`), and `MODEL_CATALOG_FUNCTION_ARN` would point the review
schedules elsewhere (unset, they invoke the Lambda's own ARN, from its context). The switches are
the DynamoDB settings `discover_enabled` and `reprice_enabled`: on unless set to `false`.

## Not done here

- **Non-token models** (images, video, speech, embeddings) are discovered and listed but never
  priced automatically: they are billed per image, second or request, which the registry's two
  per-1K-token prices cannot hold, and the pipeline only calls text models through Converse.
- **Prompt-cache prices** are not stored. The cost code never prices cache tokens separately.
- **Geo-profile premiums:** a price is the region's price for the model. A geo profile billed at
  a premium needs its price set by hand with `price_locked: true`.
- **Enabling a discovered model** is `admin_cli.py models add` with its id. The row keeps what discovery
  recorded (`region`, `discovered_at`, `pricing_unit`). The price given there counts as set by hand:
  add `--lock-price` to keep the reprice off it.
- **Discovered models are never removed.** A model that leaves Bedrock keeps its row (disable it
  by hand), because old articles' lineage still prices against it.
