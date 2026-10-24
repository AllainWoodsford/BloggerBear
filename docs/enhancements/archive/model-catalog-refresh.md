# Enhancement: keep the model registry's list and prices up to date by themselves

**Status:** built · **First draft:** 2026-10-19 · **Throttled:** 2026-10-20 (see
[Throttling](#throttling-2026-10-20))
**Background:** the Models registry and its prices ([project-plan.md §11](../../project-plan.md),
"AI lineage/cost tracking"), which until now were only filled in by hand (`admin_cli.py models add`).

## What it does

Two jobs look after the **Models** table (`<prefix>-<env>-models`), every second day, discover
first:

| Job | Default | What it does |
|---|---|---|
| **discover** | odd days of the month, 02:00 Australia/Sydney | Lists the models this account can call in the deployment's region, **from every provider** (Amazon, Anthropic, Meta, Mistral, Cohere, AI21, DeepSeek, Stability, ...), with Bedrock `ListFoundationModels` and `ListInferenceProfiles`, and adds **at most `discover_max_added` (5)** that are not in the registry yet, with **`enabled: false`** and no price, text models first. Once the registry holds **`max_models` (50)** it adds nothing and does not even list. Finding nothing new is a normal result. Image, video, speech and embedding models are listed with `pricing_unit: other` and never priced (the cost code only prices tokens). It makes no model call. |
| **reprice** | odd days of the month, 03:00 Australia/Sydney | Queues **at most `reprice_max_queued` (50)** token-priced models, enabled or not, the one looked up longest ago first (a model never looked up, like one discover just added, before any other), and checks them **5 at a time, 5 minutes apart**. Writes the prices that changed. Runs that each check only some of the models still reach every one in turn. |

The days are odd days of the month (`cron(0 2 1/2 * ? *)` and `cron(0 3 1/2 * ? *)`), the simplest
exact "every second day" EventBridge cron can say: the 31st and the 1st run back to back.

A price that changed is applied **straight away**, but stays on probation. Two **reviews** run
later, **1 hour and 2 hours** after the run's queue is done by default, each on a one-shot
schedule the run creates. Each one looks the price up again, on its own, and checks it, 5 models
at a time like the reprice:

- **Both pass:** the new price stays. A snapshot of the *old* prices, with the date, is written to
  the content bucket at `pricing/<date>-<run id>.json`.
- **A review fails** (the price can't be confirmed, or looks wrong): that model goes **back to its old
  price**. A **pricing failure** is written to FailedExecutions. The operator's assistant reports it
  in `pipeline_health`, and `admin_cli.py failed-executions list` shows it.
- **A review's lookup errors** (Bedrock or the web search unavailable, throttled, timed out): that
  says nothing about the price, so it is **not a failure**. The model stays on probation for the
  next review. If no review could look it up at all, it goes back to its old price **quietly** (no
  pricing failure; the run row lists it under `unconfirmed`), and the next reprice tries again.
  One that an earlier review confirmed keeps its new price.
- **Nothing changed:** there is nothing to review; the run is closed at once (`no_change`) and no
  snapshot is written.

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
  | `discover_enabled` | `true` | `false` pauses discover's schedule |
  | `reprice_enabled` | `true` | `false` pauses reprice's schedule |
  | `cycle_enabled` | `true` | `false` pauses the weekly rotation cycle |
  | `max_models` | `50` | once the registry holds this many, discover prunes instead of adding (1–500) |
  | `prune_mode` | `value_for_money` | what pruning keeps and the cycle adds: `value_for_money` (cheaper models) or `frontier` (high-end models) |
  | `prune_max_per_run` | `20` | models one pruning run may remove (1–100) |
  | `access_checks_per_run` | `10` | access probes (1-token Converse calls) one discover may make (0–50) |
  | `rotation_max` | `10` | the cycle adds models to a topic's rotation until it holds this many (1–50) |
  | `discover_max_added` | `5` | models one discover may add (1–5) |
  | `reprice_max_queued` | `50` | models one reprice may queue for a price check (1–50) |
  | `review_after_hours` | `[1, 2]` | one review per entry, that many hours after the run (1 to 4 entries, each 1–168) |
  | `review_tolerance_pct` | `5` | how far a review's price may be from the applied one |
  | `max_change_factor` | `10` | a new price more than this many times the old (or less than 1/this) fails review |
  | `max_web_lookups` | `10` | web searches one run may make (each is a paid AgentCore query) |

- **`model-catalog-state`** is written only by the job: `last_discover_at`, `last_reprice_at`,
  what each last did, and `active_run_id`. It is a separate row so that saving the settings can
  never wipe it.
- **`pricing-run#<run id>`** is one per reprice. It holds the **queue** still to check (`pass`:
  which pass, the models left, the next tick and when it is due), each changed model's **old** and
  **new** prices, when each review is due, and what each review found. These are the "rows that
  get pruned". When a run ends (passed, reverted, or `no_change`), its row gets `expires_at` =
  14 days, and the table's TTL removes it.

**Models table**: new fields on a row. Every one is optional, so rows written by hand keep working.

| Field | Meaning |
|---|---|
| `region` | the region it was discovered in |
| `discovered_at` | when discovery added it |
| `pricing_unit` | `tokens` (priced per 1K input/output tokens, the only kind priced automatically) or `other` |
| `price_source` | `price-list` (AWS Price List API), `web-search`, or absent (set by hand) |
| `price_checked_at` | when a lookup last confirmed or set the price |
| `price_attempted_at` | when a reprice last looked the price up, whatever it found; the reprice queues the oldest first |
| `price_evidence_hash` | a hash of the web search results the price was read from. A reprice skips the model call when it hasn't changed (diff-first) |
| `price_locked` | `true` means the jobs never change this row's price. Use it for a price you set on purpose, e.g. a geo-profile premium |

**Content bucket**: `pricing/<YYYY-MM-DD>-<run id>.json` holds the old prices a passed run replaced:
`{"written_at", "run_id", "applied_at", "models": [{"model_id", "old", "new"}]}`. Unlike
`snapshots/`, there is no lifecycle rule on `pricing/`, so they are kept.

**FailedExecutions**: a pricing failure is an item with `kind: "model_pricing"`, `topic_id: null`,
`error: {"Error": "ModelPricingReverted", "Cause": "<models and why>"}` and the reverted models in
`raw_message`. There is one per review that put a price back. It has the table's usual 7-day TTL
(`CLEANUP_TTL_DAYS`).

## How it runs: two schedules, and ticks and reviews that schedule themselves

One new Lambda, **`model-catalog`** (`lambdas/model_catalog_handler.py`), is invoked four ways:

| What invokes it | Event | Does |
|---|---|---|
| `aws_scheduler_schedule.model_catalog_discover`, `cron(0 2 1/2 * ? *)` Australia/Sydney | `{"job": "discover", "trigger": "schedule"}` | discover, unless `discover_enabled` is `false` |
| `aws_scheduler_schedule.model_catalog_reprice`, `cron(0 3 1/2 * ? *)` Australia/Sydney | `{"job": "reprice", "trigger": "schedule"}` | reprice, unless `reprice_enabled` is `false` |
| `admin_cli.py models discover` / `models reprice` (`POST /models/discover`, `/models/reprice`) | `{"job": ...}` | that job now, **even when it is paused** (an asynchronous invoke; the API answers `202`) |
| a run's review schedules | `{"job": "review", "run_id": ...}` | that run's next review |
| a run's tick schedules | `{"job": "tick", "run_id": ..., "pass": "p"\|"r<n>", "tick": n}` | the next 5 models of that pass |

03:00 Sydney is quiet: no topic's daily cycle runs then. Discover runs an hour earlier because it
makes no model call; the reprice it leads into prices what it added.

**Ticks.** A reprice, and each review, is a **pass** over a queue of models. The job (or the
review's schedule) runs the first tick at once; each tick checks `CHECKS_PER_TICK` (5) models and,
while any are left, creates a one-shot schedule for the next one `TICK_MINUTES` (5) later:
`<prefix>-<env>-price-tick-<run id>-<p|r<n>>-<n>`, deleted once fired. A full queue of 50 is 10
ticks, about 45 minutes. A tick that fires for a pass that has moved on, or more than a minute
early, does nothing. The reviews are scheduled from the end of the reprice's pass; a review that
falls due while the previous one is still ticking starts at the next tick after it.

**A saturated review is skipped.** Before a review queues anything it counts the models still on
probation. At half of `reprice_max_queued` or more (25 by default) it is skipped and recorded in
`reviews_done` as `{"skipped": <count>}`, **unless it is the run's last review**, which always
runs. So a run that changed many prices is checked once, at its last review, instead of at
every review. A model that is put back or confirmed there is settled as usual.

**Reviews.** A run that changed a price creates one EventBridge Scheduler schedule per review:
`at(<time>)` in UTC, named `<prefix>-<env>-price-review-<run id>-<n>`, with
`ActionAfterCompletion: DELETE`, so it removes itself once it has fired. A rerun from the CLI
creates its own two, so it is checked exactly like a scheduled one. A review whose schedule
fires for a run that is no longer under review, or before its time, does nothing.

**Nothing can hold repricing up for good.** Before a job runs:
- a tick or review more than 30 minutes overdue (its schedule was lost, or could not be created)
  is run then and there, in turn, until the run's next step is no longer overdue;
- a slot claimed by a run that died before writing its row is freed after an hour.

**One run under way at a time.** `reprice` does not start while another run is ticking or under
review, so two runs never hold different "old" prices for one model. On its schedule it records
`review_pending` and runs again two days later; `models reprice` is refused (`409`) unless the
run's next step is overdue, in which case the job runs that step first. `discover` never prices
and never opens a run.

**Pausing** stops a job's schedule only. The ticks and reviews of a run that has started still
finish, so pausing never strands an applied-but-unchecked price.

**SQS was considered and not used.** The ticks are a queue drained at a fixed pace by one-shot
schedules, which this Lambda already creates for its reviews under an existing grant: no new
queue, event source mapping, DLQ or IAM. If the dispatcher work queue
([dispatcher-sqs-enhancement.md](../dispatcher-sqs-enhancement.md)) is built, the ticks can become
delayed messages on it.

`admin_cli.py model-catalog get` shows the run under way: its queue left, its next tick, and when
its next review is due.

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
   - At most `max_web_lookups` searches are made per run, across all its ticks.

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
  A lookup that **errored** is not "can't confirm": it defers the model to the next review (see
  above).

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
([configuration.md](../../configuration.md#the-model-catalogs-environment-variables)):
`MODEL_CATALOG_FUNCTION_NAME` tells the Admin API which Lambda to start (Terraform sets it; unset,
it is `<prefix>-<env>-model-catalog`), and `MODEL_CATALOG_FUNCTION_ARN` would point the review
schedules elsewhere (unset, they invoke the Lambda's own ARN, from its context). The switches are
the DynamoDB settings `discover_enabled` and `reprice_enabled`: on unless set to `false`.

## Pruning and cycling (2026-10-20)

`common/model_fleet.py` keeps the registry under its cap for cost profiling, and the topics'
model rotations working and fresh.

**Pruning.** When discover finds the registry already holds `max_models`, it prunes instead of
adding: down to `max_models - discover_max_added`, so the next discover has room, and at most
`prune_max_per_run` models a run. The order:

1. inactive models (`enabled: false`) before active ones;
2. within each, unpriced models first (including image, video and embedding models, which are
   never priced), so a breached cap goes for those;
3. then by `prune_mode`: `value_for_money` prunes the most expensive (input plus output per 1K
   tokens) and keeps the cheaper ones; `frontier` prunes the cheapest and keeps the high-end ones.

**Never pruned:** the application's default and fallback (the ModelConfig `default` row and
`BEDROCK_MODEL_ID`), every topic's `model_id`, `fallback_model_id` and `model_id_candidates`,
the shared `model-rotation` pool, and any `price_locked` row. A run that cannot reach its target
for that reason says so (`short_by`).

**A pruned model's price is archived** in the `model-archive` config row before its row is
deleted. `costing.pricing_for` and the Stats page (`dynamo.list_priced_models`) fall back to it,
so the articles that used it keep their cost, and discover never adds an archived model back
(`models add` does, by hand).

**Which models the account can call.** `ListFoundationModels` and `ListInferenceProfiles` list
what Bedrock offers in the region, not what this account may call: model access never granted, a
provider's agreement or use-case form not accepted, or a Marketplace subscription missing all
still list. So discover **probes** each text model before adding it: one Converse call of one
output token (`model_catalog.probe_access`), which is also exactly how the pipeline calls a model.
Its tokens are tallied under **Model pricing** on the Stats page.

- **Answered:** `access: granted`.
- **`AccessDeniedException`, `ResourceNotFoundException`** (e.g. a use-case form not submitted)
  **or `ValidationException`** (e.g. not callable on demand through Converse): **denied**. The
  model is not added, and is not probed or added again for 30 days (the `model-access-denied`
  config row).
- **Anything else** (throttling, an outage, a timeout): `access: unknown`; the model is added and
  checked again later.

The probes left over from the run's `access_checks_per_run` re-check registry models whose access
is not known to be granted: never checked first, then unknown, then denied, oldest first. Each row
records `access`, `access_checked_at` and, if it failed, `access_error`. A denied model is pruned
before anything else, never joins a rotation, is dropped from one by the cycle, and is never the
random pick when routing around an inactive model. Enabling a model through `models add` probes it
there and then (unless it is already known to be callable) and warns if the account cannot call it.

The probe is a real model call, not an entitlement lookup: Bedrock may offer an availability API,
but the botocore pinned here (1.34) and the local AWS CLI have no such operation, so nothing relies
on one. The Lambda role's `bedrock:InvokeModel` already covers every foundation model and the
account's inference profiles, so a denial is the account's, not IAM's; and the role has no
Marketplace permissions, so a probe can never subscribe the account to a model.

**Routing around an inactive model** (`common/model_routing.py`). When a topic's rotation, or its
own `model_id`, gives a model the registry marks `enabled: false`, the call goes to the fallback if
one is configured, else the default. If those are inactive too, it goes to a random active text
model from the registry, which replaces the inactive one in the topic's rotation (or as its
`model_id`). A model with no registry row counts as active, and every lookup fails open.

**The cycle** (`{"job": "cycle"}`, `aws_scheduler_schedule.model_catalog_cycle`,
`cron(0 4 ? * FRI *)` Sydney, paused by `cycle_enabled: false`). For every topic with a rotation:
models known to be inactive leave it, and while it holds fewer than `rotation_max` it gains one
model: active, priced, a text model, not already in it, and nobody's fallback (the application's
or any topic's). The cheapest in `value_for_money` mode, the most expensive in `frontier`. It
draws on the models discover found once the operator has enabled them (discovered models start
inactive).

**Disabling a model** (`admin_cli.py models disable --model-id <id>`, or `models add ...
--disabled`, both `POST /models` with `enabled: false`). Disabling the application's default or
fallback is refused (`409`, `confirm_required`) until confirmed: the CLI asks "are you sure?"
(`--yes` skips the question) and resends with `confirm: true`. Once disabled, the model leaves
every topic's rotation (the cycle's pick takes its place, when there is one), `model_id` and
`fallback_model_id` (a topic left without falls through to the default), and the shared rotation
pool. The response lists the topics updated (`rotations`).

## Throttling (2026-10-20)

The first scheduled discover in dev (2026-10-19) added **84 models** at once and priced them in
the same invocation. An hour later its first review asked the default model to read seven web
prices back to back. Every call failed with `ServiceUnavailableException` after four retries, so
all seven read as "can't confirm", were put back, and a `model_pricing` failure was written, which
the assistant reported. Five changes:

- **Smaller jobs:** discover adds at most 5 models and none once the registry holds 50; reprice
  checks at most 50 models a run, oldest first.
- **Paced:** a run's models are checked 5 at a time, 5 minutes apart, instead of all in one
  invocation; reviews too.
- **No pile-up of reviews:** a review that would queue 25 or more models is skipped, unless it
  is the last.
- **Errors are not doubts:** an errored lookup defers a model instead of failing it.
- **Every second day:** discover, then reprice an hour later, instead of weekly on separate days.

The dev registry already held 89 rows from that first discover. Discover prunes it instead
(see [Pruning and cycling](#pruning-and-cycling-2026-10-20)), 20 a run, until the next discover
has room.

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
- **Pruned models are removed from the registry**, not just disabled: the cap is on rows.
  Their prices stay in the `model-archive` row, which nothing prunes; old articles' lineage and
  the Stats page price against it.
