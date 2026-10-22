# Models: which one runs, rotation, discovery and prices

A fresh deployment needs nothing on this page. Every call uses the model Terraform gives the
Lambdas (`BEDROCK_MODEL_ID`, by default the AU Claude Haiku 4.5 inference profile), priced from a
built-in entry for that one model, and the model catalog quietly fills the registry around it.

Read on when you want to change the model without a deploy, give it a fallback, try several models
on one topic, or understand why a topic is suddenly on a model you never chose. Every command here
is the admin CLI ([scripts/QUICKSTART.md](../scripts/QUICKSTART.md) sets it up), and every change
takes effect on the next run, with no deploy. The design, with every field and edge case, is
[enhancements/model-catalog-refresh.md](enhancements/model-catalog-refresh.md).

## Contents

- [Which model a call uses](#which-model-a-call-uses)
  - [The fallback](#the-fallback)
  - [What follows the routing, and what does not](#what-follows-the-routing-and-what-does-not)
- [Seeding the registry](#seeding-the-registry)
- [Rotation: trying several models](#rotation-trying-several-models)
  - [A topic's own rotation list](#a-topics-own-rotation-list)
  - [The shared rotation pool](#the-shared-rotation-pool)
  - [When a model in use is switched off](#when-a-model-in-use-is-switched-off)
- [The model catalog: discovery, prices and the weekly cycle](#the-model-catalog-discovery-prices-and-the-weekly-cycle)
  - [When each job runs](#when-each-job-runs)
  - [Discover](#discover)
  - [Reprice, and the reviews that check it](#reprice-and-the-reviews-that-check-it)
  - [The weekly cycle](#the-weekly-cycle)
  - [Pruning, once the registry is full](#pruning-once-the-registry-is-full)
  - [Commands](#commands)
  - [Settings](#settings)
- [How a cost is worked out](#how-a-cost-is-worked-out)
- [Where it all lives](#where-it-all-lives)
- [When something looks wrong](#when-something-looks-wrong)

## Which model a call uses

First match wins (`resolve_model` in `lambdas/common/model_routing.py`):

1. **The topic's rotation list** (`model_id_candidates`): one is picked at random.
2. **The topic's own `model_id`.** The [shared rotation pool](#the-shared-rotation-pool), when you
   set one up, rewrites this after each article.
3. **The global default**: the `default` row of the `<prefix>-<env>-model-config` table
   (`model-config set`).
4. **`BEDROCK_MODEL_ID`**, from Terraform. Always set, so a call never has no model.

A run picks once and keeps it. The daily cycle resolves the model at the start of the run, so the
ideation, draft, title and reviews that make one article all use the same model, and the article's
lineage records it. Each research tick resolves its own model, so the Findings an article is drafted
from may have been summarised by other models. Each Finding records its own.

A model the registry marks switched off (`enabled: false`) is routed around: the call goes to the
fallback, else the default, else a random active, priced text model from the registry, which then
takes the inactive model's place on the topic so the next run starts there. A model with no
registry row counts as active, and a registry that cannot be read routes as if it were empty.

### The fallback

The fallback resolves separately: the topic's `fallback_model_id`, else the default row's
`fallback_model_id`, else `BEDROCK_MODEL_ID` (unless that is the model already chosen). When a call
to the chosen model raises any error, it is retried once with the fallback, and the lineage records
`used_fallback`. So a model Bedrock retires, or that your account was never granted, costs one
failed call, not the day's article.

A reply cut off at its token limit is a different thing: Bedrock reports it as a normal reply, so
it never reaches the fallback by itself. Where a caller asks for it (the research summary and the
daily cycle's ideation do), it is retried once on the same model with a larger limit, and the tokens
of both attempts are counted. Ideation also tries the fallback when a reply comes back empty at its
limit.

### What follows the routing, and what does not

| Uses the routing above | Always uses `BEDROCK_MODEL_ID` |
|---|---|
| The research tick's summary | The bear's musings (on publish, on a rejection, on a pause, on new gear, and a regenerated one) |
| The daily cycle: ideation, draft, title, compliance and fresh-data reviews | The weekly reflection and the names it gives new gear |
| Re-Write from the review inbox | Screening a reader's comment |
| Professor Grizzlewick, the reviewer bear | The operator's assistant (`OPS_AGENT_MODEL_ID`, which Terraform sets to the same value) |
| The vision triage agent (the topic's model; it has no fallback, and fails closed) | |
| The model catalog's reading of a web price (the global default, no topic) | |

To move the right-hand column to another model you change Terraform: `bedrock_model_id`, or
`bedrock_inference_profile_id` outside Australia
([Deploying to another region](deployment-runsheet.md#deploying-to-another-region)).

## Seeding the registry

The registry (the `<prefix>-<env>-models` table) is what the Stats page and each article's cost are
priced from, what rotation and pruning choose among, and where a model is switched on or off. The
catalog adds models to it by itself ([Discover](#discover)), switched off and unpriced; you can also
add one by hand, which switches it on.

**1. Register each model you mean to use, with its price.** Prices are USD per 1,000 tokens. Check
them against the Bedrock price page for your region, because a geographic inference profile can
cost more than the provider's list price. `--lock-price` stops the catalog's reprice from ever
changing a price you set on purpose.

```bash
python scripts/admin_cli.py models add --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --display-name "Claude Haiku 4.5" --provider anthropic \
    --input-price 0.001 --output-price 0.005
python scripts/admin_cli.py models add --model-id "au.anthropic.claude-sonnet-4-5-20250929-v1:0" \
    --display-name "Claude Sonnet 4.5" --provider anthropic \
    --input-price 0.003 --output-price 0.015
python scripts/admin_cli.py models list
```

Switching a model on probes it first (one Converse call of one output token). A model your account
cannot call is still saved, with a warning, and routing and rotation skip it.

**2. Set the global default and fallback.**

```bash
python scripts/admin_cli.py model-config set \
    --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --fallback-model-id "au.anthropic.claude-sonnet-4-5-20250929-v1:0"
python scripts/admin_cli.py model-config get
```

That writes one item to the model-config table, which you can also create or edit in the DynamoDB
console:

```json
{
  "config_id": { "S": "default" },
  "model_id": { "S": "au.anthropic.claude-haiku-4-5-20251001-v1:0" },
  "fallback_model_id": { "S": "au.anthropic.claude-sonnet-4-5-20250929-v1:0" }
}
```

The same table holds the other rows on this page ([Where it all lives](#where-it-all-lives)), and
the `pipeline` settings row. None overwrites another.

**3. Pin a model, or a fallback, on one topic (optional).**

```bash
python scripts/admin_cli.py topics update github-trending \
    --model-id "au.anthropic.claude-sonnet-4-5-20250929-v1:0" \
    --fallback-model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0"
python scripts/admin_cli.py topics update github-trending --model-id ""   # back to the default
```

Before you add a model anywhere:

- **Check the id exists in your region.** `aws bedrock list-inference-profiles --region
  ap-southeast-2` lists the profiles on offer. In Sydney, Anthropic's models are reached through an
  inference profile (the `au.` or `apac.` prefix), not by the bare model id. The Sonnet id above is
  an example: use what the list shows.
- **Check your account can call it**, with the `aws bedrock-runtime converse` command under
  [Before you start](deployment-runsheet.md#before-you-start). A wrong id saved on a topic or as the
  default shows up as a failed call and a fallback, not as an error when you save it.
- Any provider's model works. Every call goes through Bedrock's Converse API.
- The registry and the settings are per environment. Do it again for production.

## Rotation: trying several models

There are two ways to rotate, and they do different jobs.

| | A topic's rotation list | The shared rotation pool |
|---|---|---|
| What it is | `model_id_candidates` on one topic | the `model-rotation` row: one list for every topic |
| When it moves | every run picks at random from the list | after each article, and after a run whose model proposed no usable angle, the topic's `model_id` is set to a random pick from the pool |
| Set with | `topics update --model-candidates` | by hand in DynamoDB (no CLI command yet) |
| Kept fresh by | [the weekly cycle](#the-weekly-cycle) | only you |
| Good for | comparing models on one topic | moving every topic on from a model that stopped working for it |

A topic with its own list ignores the pool: the list is step 1 of the routing, and the pool only
ever writes `model_id`, step 2.

### A topic's own rotation list

```bash
python scripts/admin_cli.py topics update github-trending \
    --model-candidates "au.anthropic.claude-haiku-4-5-20251001-v1:0,au.anthropic.claude-sonnet-4-5-20250929-v1:0" \
    --fallback-model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0"
python scripts/admin_cli.py topics update github-trending --model-candidates ""    # stop rotating
```

Each article's lineage records which model wrote it (`articles get <article_id>`), and the Stats
page breaks the cost down by model, so you can compare.
Once a topic has a list, [the weekly cycle](#the-weekly-cycle) maintains it: it removes models
that are switched off or that the account cannot call, and adds one new model a week until the list
holds `rotation_max` (10). Pause the cycle (`model-catalog set --cycle off`) if you want the list
left exactly as you wrote it.

### The shared rotation pool

The pool is one row in the model-config table. Nothing creates it; until you do, nothing rotates
this way. To set it:

```bash
aws dynamodb put-item --region ap-southeast-2 --table-name <prefix>-dev-model-config --item '{
  "config_id": {"S": "model-rotation"},
  "candidates": {"L": [
    {"S": "au.anthropic.claude-haiku-4-5-20251001-v1:0"},
    {"S": "au.anthropic.claude-sonnet-4-5-20250929-v1:0"}
  ]}
}'
```

To stop it, delete the item, or set `candidates` to an empty list. Topics keep whatever `model_id`
the pool last gave them: clear it with `topics update <topic_id> --model-id ""`.

Why the move after a failed run: a model that cannot propose an angle for a topic would otherwise
keep the topic for good, because the pool used to move a topic only after a publish
([friction log, 12. The silent topic](friction.md#12-the-silent-topic-2026-10-15)).

### When a model in use is switched off

`models disable --model-id <id>` (or `models add ... --disabled`) takes the model out of every
topic's rotation list (putting the cycle's next pick in its place, when there is one), clears it
where it is a topic's `model_id` or `fallback_model_id`, and removes it from the shared pool. A
topic left without a model falls through to the default. If the model is the application's default
or fallback, the CLI asks first (`--yes` skips the question).

## The model catalog: discovery, prices and the weekly cycle

The `<prefix>-<env>-model-catalog` Lambda keeps the registry's list and prices current
(`lambdas/common/model_catalog.py` and `model_fleet.py`). It is on by default and needs no setup.
Each of its three jobs can be paused, and every size and limit changed, with no deploy.

### When each job runs

Sydney time, from the schedules in each environment's `main.tf`:

| Job | When | What it does |
|---|---|---|
| **discover** | 02:00 on odd days of the month (every second day) | adds up to 5 models the account can call and the registry lacks, switched off |
| **reprice** | 03:00 on the same days | looks up to 50 prices again, 5 every 5 minutes, and applies the ones that changed |
| price reviews | 1 and 2 hours after a reprice that changed something | look each changed price up again; one that does not hold goes back |
| **cycle** | 04:00 on Fridays | refreshes every topic's rotation list |

"Odd days" is the closest EventBridge's cron comes to every second day, so the 31st and the 1st run
back to back. 03:00 is when no topic's daily cycle runs.

### Discover

Discover lists every model the account can call in the deployment's region, from every provider,
with Bedrock's `ListFoundationModels` and the system inference profiles (`au.`, `apac.`,
`global.`, ...). It adds at most `discover_max_added` (5) that are not in the registry yet, text
models first, each **switched off and unpriced**: discovery never puts a model in use.

Before it adds a text model it probes it with one 1-token Converse call. A model the account cannot
call (access never granted, a provider agreement not accepted) is not added, and is not tried again
for 30 days. Probes left over from the run's budget (`access_checks_per_run`, 10) check registry
models whose access is not known yet. Image, video, speech and embedding models are listed as
`pricing_unit: other` and never priced.

A model the fleet pruned is never added back. Once the registry holds `max_models` (50), discover
prunes instead of adding ([Pruning](#pruning-once-the-registry-is-full)). Finding nothing new is a
normal result.

To switch a discovered model on, register it with `models add` and its price (the same id); or let
the next reprice price it first and then `models add` it.

### Reprice, and the reviews that check it

Reprice queues up to `reprice_max_queued` (50) token-priced models that are not price-locked, the
one looked up longest ago first (so a newly discovered one leads), and checks them 5 at a time on
one-shot schedules 5 minutes apart. Each price comes from the AWS Price List API first (no model
call), then from an AgentCore web search read by the default model. The web is skipped when the
search results are the same as last time, and a run makes at most `max_web_lookups` (10) searches.
A source that cannot price a model leaves it alone.

A changed price is **used at once**, and stays on probation. Each review (`review_after_hours`,
1 and 2 hours by default) looks the price up again on its own:

- **Both reviews agree** (within `review_tolerance_pct`, 5 %): the new price stays, and the old
  prices are kept in the content bucket at `pricing/<date>-<run id>.json`.
- **A review disagrees**, or the price is out of range or moved more than `max_change_factor` (10)
  times: that model goes back to its old price, and a pricing failure is written to the
  FailedExecutions table. `failed-executions list` and the assistant's pipeline health show it.
- **A review could not look it up** (Bedrock or the search unavailable): that is not a failure. If
  no review could look it up at all, it goes back quietly and the next reprice tries again.

Only one pricing run is under way at a time. A tick or review whose schedule was lost is run by the
next job, so nothing can hold repricing up for good.

### The weekly cycle

For every topic that has a rotation list: remove the models that are switched off or that a probe
found the account cannot call, then, while the list holds fewer than `rotation_max` (10), add one
model it does not have. The model added is an active, callable, priced text model that is nobody's
fallback: the cheapest in `value_for_money` mode (the default), the most expensive in `frontier`
mode (`prune_mode`). A topic with no list is left alone, and so is a model with no registry row.

### Pruning, once the registry is full

When discover finds the registry at `max_models`, it removes models until the next discover has
room again, at most `prune_max_per_run` (20) a run. It takes, in order: models the account cannot
call, switched-off models, unpriced ones, then the ones `prune_mode` values least
(`value_for_money` removes the most expensive; `frontier` the cheapest).

It never removes a model in use: the default and fallback (the `default` row and
`BEDROCK_MODEL_ID`), any topic's model, fallback or rotation entry, anything in the shared pool, or
a price-locked row. A pruned model's price is kept in the `model-archive` row, so old articles still
cost correctly.

### Commands

```bash
python scripts/admin_cli.py models discover          # run discover now, even if paused (a minute or two)
python scripts/admin_cli.py models reprice           # run reprice now, even if paused
python scripts/admin_cli.py model-catalog get        # settings, what each job last did, any run under review
python scripts/admin_cli.py models list              # the registry: enabled, price, source, access
python scripts/admin_cli.py model-catalog set --reprice off    # pause a scheduled job ('' or on resumes it)
python scripts/admin_cli.py model-catalog set --prune-mode frontier --rotation-max 5
python scripts/admin_cli.py failed-executions list   # pricing failures, beside failed daily cycles
```

The operator's assistant answers "is the model catalog running?" with the same summary
(`model_catalog`), and reports a price put back in its pipeline health.

### Settings

The `model-catalog` row of the model-config table, read on every run. Set them with
`model-catalog set` (`--help` lists the flags) or in DynamoDB; a setting left unset, or invalid,
takes its default.

| Setting | Default | What it does |
|---|---|---|
| `discover_enabled`, `reprice_enabled`, `cycle_enabled` | `true` | `false` pauses that job's schedule. Running a job from the CLI ignores the pause |
| `max_models` | 50 | once the registry holds this many, discover prunes instead of adding (1 to 500) |
| `discover_max_added` | 5 | models one discover may add (1 to 5) |
| `access_checks_per_run` | 10 | access probes one discover may make (0 to 50) |
| `reprice_max_queued` | 50 | models one reprice may queue (1 to 50) |
| `max_web_lookups` | 10 | paid web searches one pricing run may make (0 to 100) |
| `review_after_hours` | `[1, 2]` | one review per entry, that many hours after the run (1 to 4 entries) |
| `review_tolerance_pct` | 5 | how far a review's price may be from the applied one (0 to 100) |
| `max_change_factor` | 10 | a new price more than this many times the old, or less than 1/this, fails review |
| `prune_mode` | `value_for_money` | what pruning keeps and the cycle adds: `value_for_money` or `frontier` |
| `prune_max_per_run` | 20 | models one pruning run may remove (1 to 100) |
| `rotation_max` | 10 | the cycle adds to a topic's list until it holds this many (1 to 50) |

`MODEL_CATALOG_FUNCTION_NAME` is not a setting: it only tells the admin API which Lambda to start,
and Terraform sets it ([configuration.md](configuration.md#the-model-catalogs-environment-variables)).

## How a cost is worked out

Every model call records its model and tokens on the Finding or the article (its lineage). The
cost is priced from, first match wins (`pricing_for` in `lambdas/common/costing.py`):

1. the model's registry row, by the id as given, then by its canonical id (an inference-profile
   ARN reduces to the profile id it names);
2. the archived price of a model the fleet pruned;
3. the built-in price list (`DEFAULT_MODEL_PRICES` in `lambdas/common/model_pricing.py`), which
   holds Claude Haiku 4.5 only, keyed so that every geographic profile of it (`au.`, `apac.`,
   `us.`, ...) matches.

A call nothing prices leaves the whole article **unpriced**, never part-summed. Costs are shown in
AUD, at a fixed rate of 1.50 per US dollar (`USD_TO_AUD_RATE` in `costing.py`). An article's cost
includes the research that fed it.

## Where it all lives

| What | Where |
|---|---|
| The registry: each model, its price, `enabled`, `price_locked`, `access`, where the price came from | `<prefix>-<env>-models` table |
| The global default and fallback | `default` row of `<prefix>-<env>-model-config` |
| The shared rotation pool | `model-rotation` row, same table |
| The catalog's settings / what it last did / a pricing run | `model-catalog` / `model-catalog-state` / `pricing-run#<id>` rows (a run's row expires 14 days after it ends) |
| Pruned models' prices | `model-archive` row |
| Models a probe found the account cannot call | `model-access-denied` row (each rechecked after 30 days) |
| A topic's model, fallback and rotation list | `model_id`, `fallback_model_id`, `model_id_candidates` on its Topics row |
| Old prices a pricing run replaced | `pricing/<date>-<run id>.json` in the content bucket (kept) |
| A price put back | a `model_pricing` row in FailedExecutions (7 days) |
| The Terraform model | `bedrock_model_id` / `bedrock_inference_profile_id` in each environment's `terraform.tfvars` |

## When something looks wrong

- **A topic is on a model you never set.** The shared pool moved it (after an article, or after a
  run with no usable angle), or routing moved it off a switched-off model and wrote the
  replacement onto the topic. `topics get <topic_id>` shows `model_id` and
  `model_id_candidates`; the daily-cycle and research-tick logs say `model_routing: ... moves to`.
- **Every article says `used_fallback`.** The chosen model fails every call: check its id with
  `aws bedrock list-inference-profiles`, and your account's access with the `converse` command
  under [Before you start](deployment-runsheet.md#before-you-start).
- **An article's cost is blank.** One of its calls used a model nothing prices: `models add` it with
  its price. A model `models list` shows as unpriced gets a price at the next reprice, if the Price
  List or the web has one.
- **A price looks wrong.** `models add` it with the right price and `--lock-price`. A locked price
  is never repriced or pruned.
- **Discover adds nothing.** Normal when nothing new is on offer. Otherwise `model-catalog get`
  shows its last result: `full` means it pruned instead; `denied_ids` are models the account cannot
  call (grant access in the Bedrock console, then wait out the 30 days or `models add` it).
- **A model shows `access: denied`.** Grant it under Bedrock → Model access (some providers need an
  agreement or a use-case form), then `models add` it again: switching it on probes it afresh.
