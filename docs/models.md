# Models: which one runs, rotation, discovery and prices

A fresh deployment needs nothing on this page. Every call uses the model Terraform gives the
Lambdas (`BEDROCK_MODEL_ID`, by default the AU Claude Haiku 4.5 inference profile), priced from a
built-in entry for that one model, and the model catalog quietly fills the registry around it.

Read on when you want to change the model without a deploy, give it a fallback, try several models
on one topic, or understand why a topic is suddenly on a model you never chose. How the catalog
works across the layers is [architecture/model-catalog.md](architecture/model-catalog.md); the
design, with every field and edge case, is
[enhancements/archive/model-catalog-refresh.md](enhancements/archive/model-catalog-refresh.md).

Every model also has a purpose (text, image, embedding, ...), and only text models are used
([What a model is for](#what-a-model-is-for-purpose-and-suitability)). With continuous evals on
(`EVALS_ENABLED`), each model earns a score as it works, and the Model Ranch keeps a model whose
scores fall below a gate out of the candidates, ranks the models a Re-Write may use, and proposes
the worst for culling ([The Model Ranch](#the-model-ranch-scores-gates-and-culling)). With evals
off, none of that happens and models are chosen as they always were.

Most commands here are the admin CLI ([scripts/QUICKSTART.md](../scripts/QUICKSTART.md) sets it
up). A change takes effect on the next run, with no deploy.

The one exception is the model Terraform gives the Lambdas: `BEDROCK_MODEL_ID`, and the operator's
assistant's `OPS_AGENT_MODEL_ID`, which Terraform sets to the same value. Moving it, and with it
every call that always uses it, takes a Terraform change and a deploy
([below](#what-follows-the-routing-and-what-does-not)).

## Contents

- [Which model a call uses](#which-model-a-call-uses)
  - [The fallback](#the-fallback)
  - [What follows the routing, and what does not](#what-follows-the-routing-and-what-does-not)
- [Seeding the registry](#seeding-the-registry)
- [What a model is for: purpose and suitability](#what-a-model-is-for-purpose-and-suitability)
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
- [The Model Ranch: scores, gates and culling](#the-model-ranch-scores-gates-and-culling)
  - [Where a model's score comes from](#where-a-models-score-comes-from)
  - [The windows](#the-windows)
  - [Gates and `min_events`](#gates-and-min_events)
  - [The rotation guard](#the-rotation-guard)
  - [Never gated: the default, the fallback and the judge](#never-gated-the-default-the-fallback-and-the-judge)
  - [Holding a gate open: `ungated_until`](#holding-a-gate-open-ungated_until)
  - [The rewrite picker](#the-rewrite-picker)
  - [Forgive and punish](#forgive-and-punish)
  - [Culling: a proposal, then an apply](#culling-a-proposal-then-an-apply)
  - [Putting a model back: `models restore`](#putting-a-model-back-models-restore)
- [How a cost is worked out](#how-a-cost-is-worked-out)
- [Where it all lives](#where-it-all-lives)
- [When something looks wrong](#when-something-looks-wrong)

## Which model a call uses

First match wins (`resolve_model` in `lambdas/common/model_routing.py`):

1. **The topic's rotation list** (`model_id_candidates`): one is picked at random. While evals are
   on, the pick is among the list's models the Model Ranch has not gated; when every one is gated,
   the topic's `model_id` is used, else the default ([the rotation guard](#the-rotation-guard)).
2. **The topic's own `model_id`.** The [shared rotation pool](#the-shared-rotation-pool), when you
   set one up, rewrites this after each article the daily cycle publishes by itself, and after a
   run whose model proposed no usable angle. An article held for moderation does not move it, not
   even once it is approved.
3. **The global default**: the `default` row of the `<prefix>-<env>-model-config` table
   (`model-config set`).
4. **`BEDROCK_MODEL_ID`**, from Terraform. Always set, so a call never has no model.

A run picks once and keeps it. The daily cycle resolves the model at the start of the run, so the
ideation, draft, title and reviews that make one article all use the same model, and the article's
lineage records it. Each research tick resolves its own model, so the Findings an article is drafted
from may have been summarised by other models. Each Finding records its own.

Only a topic's own model (its rotation pick, or its `model_id`) is checked against the registry.
When the registry marks that model switched off (`enabled: false`), or says it cannot do the work
(it is not a text model, or, for the vision triage, it takes no image:
[purpose](#what-a-model-is-for-purpose-and-suitability)), the call is routed around it, to the
first of these that is fit:

1. the fallback, unless it is switched off or unfit too;
2. the default, unless it is switched off or unfit too;
3. a random active model from the registry that can do the work, leaving out any that a probe found
   the account cannot call and, while evals are on, any the Model Ranch has gated.

Only the third takes the model's place on the topic (in its rotation list, or as its `model_id`),
so the next run starts there, and only when the model is switched off or not a text model at all.
A text model that only lacks an image input stays on the topic, and only the triage's call goes
elsewhere. A model with no registry row counts as active and fit, and a registry that cannot be
read routes as if it were empty.

Nothing else is checked. A call that starts from the global default or `BEDROCK_MODEL_ID` uses
that model even when it is switched off. That happens when the topic has no model of its own, or
the call has no topic. The one exception is the vision triage: a default that takes no image is
passed over for that call (nothing is written). A switched-off fallback is also still tried when a
call fails. A fallback that is not a text model is not: `BEDROCK_MODEL_ID` takes its place.

`models disable` does not change the `default` row. So before you switch off the application's
default or fallback, point the row at another model with `model-config set`. It rewrites the whole
row, and a flag you leave out is cleared. A cleared default or fallback falls through to
`BEDROCK_MODEL_ID`. So pass both `--model-id` and `--fallback-model-id`, repeating the one you are
keeping (`model-config get` shows it). To move off `BEDROCK_MODEL_ID` itself, change Terraform
([below](#what-follows-the-routing-and-what-does-not)).

### The fallback

The fallback resolves separately: the topic's `fallback_model_id`, else the default row's
`fallback_model_id`, else `BEDROCK_MODEL_ID` (unless that is the model already chosen). When a call
to the chosen model raises any error, it is retried once with the fallback, and the lineage records
`used_fallback`. So a model Bedrock retires, or that your account was never granted, costs one
failed call, not the day's article.

A reply cut off at its token limit is a different thing: Bedrock reports it as a normal reply, so
it never reaches the fallback by itself. Where a caller asks for it (the research summary, and the
daily cycle's ideation and draft do), it is retried once on the same model with a larger limit, and
the tokens of both attempts are counted. Ideation also tries the fallback when a reply comes back
empty at its limit.

### What follows the routing, and what does not

| Uses the routing above | Always uses `BEDROCK_MODEL_ID` |
|---|---|
| The research tick's summary | The bear's musings on an article a person or Grizzlewick publishes (an approval or a force-publish), on a rejection, on a pause, on new gear, on readers' feedback (every 4 days), and a regenerated one |
| The daily cycle: ideation, draft, title, compliance and fresh-data reviews, and the bear's musing on an article it publishes by itself (with no fallback) | The weekly reflection, the names it gives new gear, and the name the bear gives gear you make by hand when you give it no theme |
| Re-Write from the review inbox | Screening a reader's comment |
| Professor Grizzlewick, the reviewer bear | The operator's assistant (`OPS_AGENT_MODEL_ID`, which Terraform sets to the same value) |
| The vision triage agent (the topic's model; it has no fallback, and fails closed) | |
| The model catalog's reading of a web price (the global default, no topic) | |

Three more calls have no fallback either. Each runs only on the model it was given (the routed
one, or the model you picked for a Re-Write), and an error is not retried on the fallback:

- Professor Grizzlewick's own calls (his reads, judgements and musings). On an error he leaves the
  article for a person or his next run.
- The Re-Write's rewriting call. On an error the Re-Write is marked failed. Its compliance and
  fresh-data reviews do fall back.
- The model catalog's reading of a web price. On an error it counts as a lookup that could not be
  made.

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
cost more than the provider's list price.

`--lock-price` stops the catalog's reprice from ever changing a price you set on purpose. Pass it
again every time you `models add` that model: a `models add` without it clears the lock, even one
with `--disabled`. `models disable` keeps it.

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
cannot call is still saved, switched on, with a warning. The weekly cycle (Fridays) never adds such
a model to a rotation and drops it from rotation lists at its next run, and the shared pool moves
no topic to it, but nothing else moves it: until then a rotation can still pick it, and a topic's
`model_id` or the global default that names it keeps using it, each call failing over to the
fallback where there is one. Change those yourself.

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
- It must be a text model. A topic's model, fallback and rotation list, the default and fallback
  (`model-config set`), the shared pool, the judge (`evals set --judge-model-id`) and a Re-Write's
  model are refused (400) when the registry says the model writes something else
  ([purpose](#what-a-model-is-for-purpose-and-suitability)). A model with no registry row is not
  checked.
- The registry and the settings are per environment. Do it again for production.

`models add` on a model already in the registry keeps what the catalog and the Model Ranch wrote on
its row: where it was found, its access, its purpose and inputs, its scores and any
`ungated_until`. If another writer changes the row while it is written (the hourly rollup, an
`evals ungate`), it reads the row again and retries once; a second change answers 409, and nothing
is written: send it again.

## What a model is for: purpose and suitability

Every registry row records what its model writes, `model_purpose`, and what it takes in,
`model_inputs`, both from Bedrock's listing. The purpose is `text` when the model's outputs include
text, else the first of `image`, `embedding`, `video` and `speech` it outputs, else `other`; and
`unknown` once Bedrock no longer lists the model. Discover records both for every model it adds.
While evals are on, the hourly rollup fills them in for any row that has none. `models classify`
does it for every row now.

What reads them (`suitable` in `lambdas/common/model_fleet.py`):

- **Text work** (everything the pipeline writes) needs a text model. Routing passes over a model
  that is not one, the weekly cycle drops it from rotation lists and never adds one, the shared pool
  never moves a topic to one, the rewrite picker never offers one, the judge and offline scoring
  runs never use one, and the admin API refuses one wherever a model is named
  ([above](#seeding-the-registry)).
- **The vision triage** needs a text model that also takes an image (`IMAGE` in `model_inputs`).
- **A row from before purposes were recorded** (`models list` shows `unclassified`) is judged by
  the old rule: a token-priced model is a text model, and its inputs are not held to the image rule.
  So nothing the pipeline uses today stops until `models classify` says otherwise.

```bash
python scripts/admin_cli.py models classify   # record every model's purpose and inputs now (a minute or so)
python scripts/admin_cli.py models list       # the PURPOSE column
```

Classify runs in the background in the model-catalog Lambda, and writes only the rows that change,
so it can be run any number of times. It reads Bedrock's whole listing in the deployment's region:
every foundation model, whatever its lifecycle (a `LEGACY` one keeps its purpose) and however it is
called, the system inference profiles (`au.`, `apac.`, `global.`, ...), and the application
inference profiles this account made, by their ARN, each judged by the model under it. A model in
none of them is marked `unknown`. A custom, provisioned or imported model's ARN is left as it is and
counted as unchanged: Bedrock lists those through calls the catalog does not make. `model-catalog
get` shows the result (`last_classify_result`: how many classified, how many `unknown`, with their
ids).

## Rotation: trying several models

There are two ways to rotate, and they do different jobs.

| | A topic's rotation list | The shared rotation pool |
|---|---|---|
| What it is | `model_id_candidates` on one topic | the `model-rotation` row: one list for every topic |
| When it moves | every run picks at random from the list | after each article the daily cycle publishes by itself (not one held for moderation, nor one a moderator approves later), and after a run whose model proposed no usable angle, the topic's `model_id` is set to a random pick from the pool |
| Set with | `topics update --model-candidates` | `model-rotation set --candidates` |
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
that are switched off, are not text models, or that the account cannot call, and adds one new model
a week until the list holds `rotation_max` (10). A gated model stays in the list while it is gated:
the draw passes it over until its scores recover ([the rotation guard](#the-rotation-guard)).
Pause the cycle (`model-catalog set --cycle off`) if you want the list left exactly as you wrote
it.

### The shared rotation pool

The pool is one row in the model-config table. Nothing creates it; until you set it, nothing
rotates this way. To set it:

```bash
python scripts/admin_cli.py model-rotation set --candidates "<model id>,<model id>"
python scripts/admin_cli.py model-rotation get    # the pool as stored; [] when there is none
```

`set` replaces the whole pool. Each model must be a registered, enabled, callable (not
`access: denied`) text model (`models list`), named once, and the pool holds at most 50. These are
the models the weekly cycle and routing move a topic to. Anything else is refused with the reason,
and the pool stays as it was. Like the registry, the pool is per environment: set it in each.

The check runs only when you set the pool. If a later probe finds that the account cannot call a
pooled model (`access: denied`), or `models classify` finds it is not a text model, it stays in the
pool until you set the pool again without it, but topics are not moved to it. While evals are on, a
pooled model the Model Ranch has gated is passed over the same way, until its scores recover. A
pooled model with no registry row is kept as a choice, and every pooled model is when the registry
cannot be read.

To stop it, clear the pool with `model-rotation set --candidates ""` (deleting the row in the
DynamoDB console does the same). Topics keep whatever `model_id` the pool last gave them: clear it
with `topics update <topic_id> --model-id ""`.

Why the move after a failed run: a model that cannot propose an angle for a topic would otherwise
keep the topic for good, because the pool used to move a topic only after a publish
([friction log, 12. The silent topic](friction.md#12-the-silent-topic-2026-10-15)).

### When a model in use is switched off

`models disable --model-id <id>` (or `models add ... --disabled`) does three things:

- It takes the model out of every topic's rotation list. In its place it puts the model
  [the weekly cycle](#the-weekly-cycle) would add next, if there is one.
- It clears the model where it is a topic's `model_id` or `fallback_model_id`.
- It removes the model from the shared pool.

A topic left without a model falls through to the default. If the model is the application's default
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

Discover lists every active model Bedrock offers in the deployment's region, from every provider:
the on-demand models from Bedrock's `ListFoundationModels`, and the system inference profiles
(`au.`, `apac.`, `global.`, ...). It adds at most `discover_max_added` (5) that are not in the
registry yet, text models first, each **switched off and unpriced**: discovery never puts a model
in use.

Being listed does not mean the account can call a model. So before it adds a text model, discover
probes it with one 1-token Converse call. A model the account cannot call (access never granted, a
provider agreement not accepted) is not added, and is not tried again for 30 days.

Probes left over from the run's budget (`access_checks_per_run`, 10) check registry text models not
yet known to be callable: never-probed ones first, then ones a probe could not decide, then ones
found denied, the longest ago first in each group. Image, video, speech and embedding models are
listed as `pricing_unit: other` and never priced. Every model discover adds records its purpose and
inputs ([What a model is for](#what-a-model-is-for-purpose-and-suitability)).

A model the fleet pruned is never added back. Once the registry holds `max_models` (50), discover
prunes instead of adding ([Pruning](#pruning-once-the-registry-is-full)). Finding nothing new is a
normal result.

To switch a discovered model on, register it with `models add` and its price (the same id); or let
the next reprice price it first and then `models add` it.

### Reprice, and the reviews that check it

Reprice queues up to `reprice_max_queued` (50) token-priced models that are not price-locked, the
one looked up longest ago first (so a newly discovered one leads). It checks them 5 at a time. Each
batch is a **tick**: the first runs at once, and each later one on a one-shot schedule 5 minutes
after the one before.

Each price comes from the AWS Price List API first (no model call), then from an AgentCore web
search read by the default model. In the reprice, a model neither source can price keeps the price
it has. A review treats that as a failure (below).

Web searches are capped for the reprice, but not for its reviews:

- A reprice makes at most `max_web_lookups` (10) searches, across all its ticks.
- When the search results are the same as last time, the model does not read them and the price
  stays as it is. The search itself is still made, and it counts toward the cap.
- Its reviews have no cap and do not count against it. Each review that runs searches again for
  every price still on probation (see below) that the Price List cannot give.

A changed price is **used at once**, and stays on probation. Each review (`review_after_hours`,
1 and 2 hours by default) looks the price up again on its own.

One exception: a review that would check half of `reprice_max_queued` or more (25 by default) is
skipped, unless it is the run's last. So a run that changed that many prices has them checked only
once, at its last review.

What the reviews decide:

- **Every review that could look it up agrees** (within `review_tolerance_pct`, 5 %): the new
  price stays, and the old prices are kept in the content bucket at `pricing/<date>-<run id>.json`.
- **A review disagrees, or finds no price at all** (the Price List has none, and the search finds
  nothing usable or the model reading it gives no price), or the price is out of range (the input
  or output price not above zero, or over 0.50 USD per 1,000 tokens) or moved more than
  `max_change_factor` (10) times, up or down: that model goes back to its old price (or to none, if
  it had none), and a pricing failure is written to the FailedExecutions table.
  `failed-executions list` and the assistant's pipeline health show it.
- **A review could not look it up** (Bedrock or the search unavailable): that is not a failure. If
  no review could look it up at all, it goes back quietly and the next reprice tries again.

Only one pricing run is under way at a time. A tick or review whose schedule was lost is run by the
next job, so nothing can hold repricing up for good.

### The weekly cycle

For every topic that has a rotation list: remove the models that are switched off, that are not
text models, or that a probe found the account cannot call. Then, if the list holds fewer than
`rotation_max` (10), add one model it does not have, so the cycle grows a list by at most one model
a week. The model added is an active, callable, priced text model that is nobody's fallback and,
while evals are on, that the Model Ranch has not gated: the cheapest in `value_for_money` mode (the
default), the most expensive in `frontier` mode (`prune_mode`). A gated model is left out before
that order is applied, and the order itself is unchanged. A topic with no list is left alone, and so
is a model with no registry row.

After the rotations, while evals are on, the same run writes the week's cull proposal
([Culling](#culling-a-proposal-then-an-apply)). A cycle paused with `model-catalog set --cycle off`
writes none.

### Pruning, once the registry is full

When discover finds the registry at `max_models`, it removes models until the next discover has
room again, at most `prune_max_per_run` (20) a run. It takes, in order: models the account cannot
call, switched-off models, unpriced ones, then the ones `prune_mode` values least
(`value_for_money` removes the most expensive; `frontier` the cheapest).

It never removes a model in use: the default and fallback (the `default` row and
`BEDROCK_MODEL_ID`), any topic's model, fallback or rotation entry, anything in the shared pool, the
evals judge named by `judge_model_id`, or a price-locked row. A pruned model's price is kept in the
`model-archive` row, so old articles still cost correctly. Pruning never looks at the Model Ranch's
scores; culling is the part that does ([Culling](#culling-a-proposal-then-an-apply)).

### Commands

```bash
python scripts/admin_cli.py models discover          # run discover now, even if paused (a minute or two)
python scripts/admin_cli.py models reprice           # run reprice now, even if paused
python scripts/admin_cli.py models classify          # record every model's purpose and inputs now
python scripts/admin_cli.py model-catalog get        # settings, what each job last did, any run under review
python scripts/admin_cli.py models list              # the registry: purpose, enabled, access, prices (--json: every field)
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
| `access_checks_per_run` | 10 | access probes one discover may make (0 to 50). Each new text model is probed before it is added, and the first one left without a probe ends that run's adding (text models come first, so the models after it wait too). At 0 it adds nothing while a new text model is on offer |
| `reprice_max_queued` | 50 | models one reprice may queue (1 to 50) |
| `max_web_lookups` | 10 | paid web searches one reprice may make (0 to 100); its reviews search without a cap |
| `review_after_hours` | `[1, 2]` | one review per entry, that many hours after the run (1 to 4 entries) |
| `review_tolerance_pct` | 5 | how far a review's price may be from the applied one (0 to 100) |
| `max_change_factor` | 10 | a new price more than this many times the old, or less than 1/this, fails review |
| `prune_mode` | `value_for_money` | what pruning keeps and the cycle adds: `value_for_money` or `frontier` |
| `prune_max_per_run` | 20 | models one pruning run may remove (1 to 100) |
| `rotation_max` | 10 | the cycle adds to a topic's list until it holds this many (1 to 50) |

`MODEL_CATALOG_FUNCTION_NAME` is not a setting: it only tells the admin API which Lambda to start,
and Terraform sets it ([configuration.md](configuration.md#the-model-catalogs-environment-variables)).

## The Model Ranch: scores, gates and culling

With continuous evals on (`EVALS_ENABLED`), every model gets a running score from what happens to
its work, and the score decides three things: whether the model may be drawn as a candidate (the
gates), which model a Re-Write is offered first (the picker), and which models are proposed for
deletion (culling). The scores are kept on each model's registry row. The settings are the `evals`
row of the model-config table ([configuration.md](configuration.md#the-evals-settings)), and how
the events are written, added up and checked, the judge included, is in
[the evaluation runsheet](evaluation-runsheet.md#continuous-evals). With evals off, nothing here
reads or filters anything, and the scores already kept stay as they are.

```bash
python scripts/admin_cli.py evals status          # gated models, the cull proposal, the judge, the rollup
python scripts/admin_cli.py evals digest          # each model's day totals over the last 7 days
python scripts/admin_cli.py models list --json    # each model's windows, as the rollup last wrote them
```

The operator's assistant answers "how are the models doing?" and "why is this model not picked?"
with the same scores (`model_ranch`, [alexa/README.md](../alexa/README.md#5-write-the-manifest-deploy-and-test)).

### Where a model's score comes from

| What happened | Score | Counts towards the gates |
|---|---|---|
| The daily cycle stored an article, held or published | +2 | yes |
| A person approved or rejected it in the inbox | +2 or −2 | yes |
| A person sent it back to be rewritten (`articles rewrite`, `[w]` in `approve`) | −2 | yes |
| A person marked a research Finding good or bad (`approve --source findings`) | +2 or −2 | yes |
| The judge reviewed a Finding or an article | +2 or −2 | only while the judge is calibrated |
| An offline scoring run made with `--record` | +2 or −2 | only while the judge is calibrated |
| A reader voted | +1 or −1, +3 or −3 with a comment the screening kept | never |
| An operator's `evals forgive` or `evals punish` | +N or −N (1 to 100; more with `--allow-cull`) | the total only |

An article counts once for its model, however many prompts went into it. Its model is the one whose
text the article is now: its latest finished Re-Write's, else its draft's. Professor Grizzlewick's
approvals, rejections and rewrites are an AI reviewer's, not a person's, and score nothing. The
judge is calibrated while it agrees with people often enough
([calibrating the judge](evaluation-runsheet.md#4-the-judge-and-calibrating-it)); until then its
verdicts are kept but do not gate.

### The windows

The hourly rollup writes these on each model's registry row. It writes only to a row that is there,
so a culled or deleted model is never written back.

| Field | What it adds up |
|---|---|
| `daily_perf` | today, in the evals `timezone` (Sydney by default) |
| `weekly_perf` | the last 7 days, today included |
| `monthly_perf` | the calendar month so far |
| `total_perf` | every day the evals table holds, plus the model's forgives and punishes |
| `perf_events` | the events behind each window (`daily`, `weekly`, `monthly`, `total`); a forgive or punish is not an event |
| `perf_good`, `perf_bad` | per window, the events above and below zero, readers' votes and every judge's verdict included, stored articles not: what the rewrite picker ranks by |
| `model_roles` | what the model is used for now (`default`, `fallback`, `topic_model`, `topic_fallback`, `rotation`, `pool`, `price_locked`, `judge`), for display only: culling works the roles out again |
| `perf_updated_at` | when the rollup last wrote them |

The four `*_perf` windows and `perf_events` hold only what may gate: the rows marked yes above, and
the judge's and offline runs' verdicts only while the judge is calibrated. Every run adds whole days
up again from their events, so a window never counts anything twice.

### Gates and `min_events`

A model is **gated** while one of three windows is below its gate with at least `min_events` (10)
events behind that window:

| Window | Its gate (default) |
|---|---|
| `daily_perf` | `daily_gate` (−50) |
| `weekly_perf` | `weekly_gate` (−100) |
| `total_perf` | `total_gate` (−1000) |

`min_events` keeps a model with little history from being gated on a handful of events. The month
has no gate. These four, and how well the judge must agree with people before its verdicts count
(`judge_min_agreement`, 0.7, over at least `judge_min_pairs`, 20), are policy: no CLI flag sets
them and the admin API refuses them. Change them by editing the `evals` row in the DynamoDB console.

A gate lifts by itself when its window recovers: the next day for the daily gate, as the bad days
leave the week for the weekly one. A model's row whose scores cannot be read is never gated, and
nothing is gated while the `evals` row or the model-config default row cannot be read: a gate never
holds a model back on a guess. `evals status` lists the gated models with the windows that gate
each.

### The rotation guard

While evals are on, a gated model is left out of:

- the rotation draw (step 1 of [the routing](#which-model-a-call-uses)). When every model in a
  topic's list is gated, the topic's own `model_id` is used, else the default, never the whole list
  again;
- the registry pick that routing moves a topic to when its model is switched off or unfit, so no
  gated model is ever written onto a topic;
- the model [the weekly cycle](#the-weekly-cycle) adds to a rotation list;
- the shared pool's pick after a publish;
- the [rewrite picker](#the-rewrite-picker). A Re-Write asked for on a gated model is refused (400),
  with the windows that gate it.

Nothing is taken out of a rotation list or the pool: a model whose windows recover is a candidate
again at the next draw. Each call that routes reads the `evals` row and lists the registry once,
and only while evals are on.

### Never gated: the default, the fallback and the judge

These are served whatever their scores:

- the default: the model-config `default` row's model, and `BEDROCK_MODEL_ID`;
- the fallback: that row's `fallback_model_id`;
- the judge in effect: `judge_model_id`, else that fallback, else `BEDROCK_MODEL_ID`.

A topic's own single `model_id` and its `fallback_model_id` are not gated either: they are the
operator's choice, and only lists are filtered. Each time one of these is served while the gates
would have held it back, a `model_gated_protected` evidence line in the Lambda's log names the
model, the role that keeps it and the windows. `evals status` lists those models under `protected`,
apart from the gated ones.

### Holding a gate open: `ungated_until`

```bash
python scripts/admin_cli.py evals ungate --model <model id> --hours 48   # 1 to 720 hours from now
python scripts/admin_cli.py evals ungate --model <model id> --hours 0    # end the override now
```

This sets `ungated_until` on the model's registry row: until then no gate holds the model back,
whatever its scores, and no cull proposes it. Its scores are not changed. A value there that is not
a time is read as holding the gates open too. `evals status` lists the models an override holds open
under `held_open`. `models restore` sets it for 7 days. It is the failure-day step for a model gated
by mistake ([the production runsheet](production-runsheet.md#failure-day-recipes)).

### The rewrite picker

`approve`'s Re-Write (`w`) offers, and `articles rewrite` without `--model` takes the first of,
these models:

1. the enabled text models the account can call (not `access: denied`) that the Model Ranch has not
   gated;
2. while evals are on, those with at least `min_events` good and bad events in their whole record
   (`perf_good` plus `perf_bad`, all time) first, ranked by the lower bound of the 95 % Wilson
   interval of good / (good + bad), then by price (input plus output), then by id;
3. then the rest, cheapest first.

Each model in the picker shows its score, n and interval, or "too few to rank, by price", or "no
scores yet". With evals off, nothing is held back and every model comes cheapest first. `articles
rewrite` without `--model` uses the top model only while one is ranked; otherwise (evals off, no
model with enough events yet, or the models could not be read) it says so and the topic's own model
writes the Re-Write, as it always has.

### Forgive and punish

```bash
python scripts/admin_cli.py evals forgive --model <model id> --amount 50
python scripts/admin_cli.py evals punish --model <model id> --amount 50
python scripts/admin_cli.py evals punish --model <model id> --amount 500 --allow-cull
```

Each moves the model's `total_perf` by N at the next hourly rollup, and no window: a forgive cannot
hide a bad day or week from the daily and weekly gates, and a punish cannot put a model under them.
So it reaches the total gate and culling only. N is 1 to 100; more needs `--allow-cull` (a punish
that large can take a model below the total gate, where it is gated and may be culled). The answer
shows the model's total before and after, as the next rollup would put it, beside `total_gate`. Each
is an `adjust#model#<id>` row on the evals table and an Admin Forgive or Admin Punish event.

`--prompt <prompt id> --version <version>` records one for a prompt version instead (`v2`, or
`vanilla:v1` for the copy in the code). Nothing adds those up yet, so no total of a prompt version
moves. The prompt must be one the code registers: a code-only prompt takes only a `vanilla:`
version, and a `vanilla:` version cannot be later than the code's own. Forgive and punish are
refused (409) while evals are off.

### Culling: a proposal, then an apply

Culling deletes models whose total has stayed bad. It takes two steps, and only the second deletes.

**The proposal.** On Fridays at 04:00 Sydney time, after it refreshes the rotations, [the weekly
cycle](#the-weekly-cycle) proposes every registry model that:

- has a `total_perf` below `total_gate` (−1000), with at least `min_events` events behind it;
- plays no role as things stand now: not the default, the fallback or the judge, no topic's model,
  fallback or rotation entry, not in the shared pool (worked out again from the topics and the
  settings, not read from the row's `model_roles`);
- has no locked price;
- has no `ungated_until` holding its gates open.

It writes them, worst total first, each with its total, its events and why, as `cull_proposal` on
the `model-catalog-state` row, expiring at the next cycle. An empty proposal is written too, so
"nothing to cull" can be told from "never looked". Nothing is deleted. A proposal is made only while
evals are on and the cycle runs (`model-catalog set --cycle on`), and whatever `cull_enabled` says,
so you can read a few weeks of proposals before anything can be deleted.

```bash
python scripts/admin_cli.py evals cull                # the proposal in place and the last cull applied
python scripts/admin_cli.py evals set --cull on       # let an apply delete (cull_enabled, off by default)
python scripts/admin_cli.py evals cull --apply        # shows the proposal and asks first; --yes skips both
python scripts/admin_cli.py evals set --cull off
```

**The apply.** `evals cull --apply` takes up to `cull_max_per_run` (3) of the proposal's models,
worst total first. It is refused (409) while `cull_enabled` is off, while evals are off, when there
is no proposal, and once the proposal has expired. Each model is checked again against the rules as
they are now, so one that has since been given a role, had its price locked or its gates held open,
recovered above the gate, or left the registry is kept, and the answer says why. A culled model
goes the way a pruned one does: its price into the `model-archive` row (old articles still cost
correctly), then its registry row deleted, so discover never adds it back. Its archive entry also
keeps its row, less its scores, for `models restore`. The proposal stays as it was, so another apply
takes the next ones, checked again. What was done (when, the ids culled, those kept and why, those
left for another apply) is `last_cull_apply` on the state row, which `evals cull` and `evals status`
show.

### Putting a model back: `models restore`

```bash
python scripts/admin_cli.py models restore <model id>
```

This puts a model the archive holds back in the registry. A culled model comes back with the row
the archive kept; a pruned one with its price and names only, switched off, for `models add` to
switch on. Its `ungated_until` is set 7 days ahead, so no gate holds it back and no cull proposes it
while its scores settle. When its total is below zero, an Admin Forgive brings it to zero; the
total moves at the next hourly rollup. Last, the archive entry is deleted, so a restore that failed
part-way can be sent again. It works whatever `cull_enabled` says, and is refused (409) while evals
are off. It is the failure-day step for a model culled by mistake.

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
| A model's purpose and inputs | `model_purpose`, `model_inputs` on its registry row |
| A model's scores and gate override | `daily_perf`, `weekly_perf`, `monthly_perf`, `total_perf`, `perf_events`, `perf_good`, `perf_bad`, `model_roles`, `perf_updated_at` and `ungated_until` on its registry row |
| The day totals, forgives and punishes behind the scores | the `<prefix>-<env>-evals` table: `model#<id>` day rows, `adjust#model#<id>` rows |
| The Model Ranch's settings: the gates, `min_events`, culling, the judge | `evals` row of `<prefix>-<env>-model-config` ([configuration.md](configuration.md#the-evals-settings)) |
| The catalog's settings / what it last did / a pricing run | `model-catalog` / `model-catalog-state` / `pricing-run#<id>` rows (a run's row expires 14 days after it ends) |
| The cull proposal / the last cull applied | `cull_proposal` / `last_cull_apply` on the `model-catalog-state` row |
| Pruned and culled models' prices, and a culled model's row for `models restore` | `model-archive` row |
| Models a probe found the account cannot call | `model-access-denied` row (each rechecked after 30 days) |
| A topic's model, fallback and rotation list | `model_id`, `fallback_model_id`, `model_id_candidates` on its Topics row |
| Old prices a pricing run replaced | `pricing/<date>-<run id>.json` in the content bucket (kept) |
| A price put back | a `model_pricing` row in FailedExecutions (7 days) |
| The Terraform model | `bedrock_model_id` (default empty) and `bedrock_inference_profile_id` (default the AU Claude Haiku 4.5 profile), in each environment's `variables.tf`. When `bedrock_model_id` is empty, `local.bedrock_model_id` in `main.tf` builds the profile's ARN. To change the model, set one in that environment's `terraform.tfvars`: a `bedrock_model_id` you set is used as given, and the profile is ignored |

## When something looks wrong

- **A topic is on a model you never set.** One of these put it there:
  - the shared pool, which rewrites `model_id` after an article the daily cycle published by
    itself, or after a run with no usable angle;
  - routing, which moved the topic off a switched-off model, or one that is not a text model, and
    wrote the replacement onto it;
  - [the weekly cycle](#the-weekly-cycle) (Fridays), which adds one model a week to a topic's
    rotation list until it holds `rotation_max` (10);
  - `models disable` (or `models add ... --disabled`), which put the cycle's next pick in the
    disabled model's place in a rotation list.

  `topics get <topic_id>` shows `model_id` and `model_id_candidates`, and `model-rotation get`
  the pool. Routing logs `model_routing: ... moves to` in the daily-cycle and research-tick logs.
  `model-catalog get` shows the cycle's last run (`last_cycle_result`, with `topics_changed`).
- **Every article says `used_fallback`.** The chosen model fails every call: check its id with
  `aws bedrock list-inference-profiles`, and your account's access with the `converse` command
  under [Before you start](deployment-runsheet.md#before-you-start).
- **An article's cost is blank.** One of its calls used a model nothing prices. To fix it:
  1. `models add` it with its price. (A model `models list` shows as unpriced also gets a price at
     the next reprice, if the Price List or the web has one.)
  2. Run `lineage backfill --apply`. Without `--apply` it only reports what would change.

  The backfill is needed either way. An article's cost is worked out once, when it is written, so a
  price added later fills in no cost already stored until the backfill recomputes it from the
  recorded tokens. The article's static page keeps the figure it was rendered with.

  The Stats page reprices every call each time it is rebuilt: after the next article is written or
  moderated, and after a backfill that changed something.
- **A price looks wrong.** `models add` it with the right price and `--lock-price`. `models add`
  switches the model on unless you also pass `--disabled`, so add that flag for a model that is
  switched off and should stay off. A locked price is never repriced or pruned.
- **Discover adds nothing.** Normal when nothing new is on offer. Otherwise `model-catalog get`
  shows its last result: `full` means it pruned instead; `denied_ids` are models the account cannot
  call (grant access in the Bedrock console, then wait out the 30 days or `models add` it).
- **A model shows `access: denied`.** Grant it under Bedrock → Model access (some providers need an
  agreement or a use-case form), then `models add` it again: switching it on probes it afresh.
- **A model is refused with "does not write text".** Its purpose says it is not a text model:
  `models list` shows which. `unknown` means Bedrock no longer lists it. A row that shows
  `unclassified` is judged as a text model until `models classify` records its purpose.
- **A rotation never picks a model, or a Re-Write refuses it as gated.** The Model Ranch has gated
  it: `evals status` lists it under `gated_models`, with the windows below their gate. The gate
  lifts by itself when the windows recover. If its scores are wrong, or you need it now, `evals
  ungate --model <model id> --hours 48`.
- **A model has gone from `models list`.** Discover pruned it (the registry was full; `model-catalog
  get` shows the last discover's `prune`, with `pruned_ids`) or an `evals cull --apply` culled it
  (`evals cull` shows the last cull applied). `models restore <model id>` puts either back.
- **The scores do not move.** Evals are off, or the rollup has stopped: `evals status` shows the
  rollup's watermark and last run, and `evals rollup` runs it now.
