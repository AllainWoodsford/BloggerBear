# BloggerBear admin CLI

A local operator CLI for the Phase 2 Admin API. This is a plain script you
run on your own machine -- it is **not** deployed to Lambda or anywhere
else.

**New here, or just need the export commands?** See [QUICKSTART.md](QUICKSTART.md).

## First-time setup of your repository: `setup_repo.py`

Deploying your own copy? This one comes before the admin CLI. It asks for the GitHub secrets and
variables a deployment needs (your allowed address ranges, alert emails, the AWS account IDs and
deploy role ARNs, the state bucket names, the name suffix and `PII_DENYLIST`), checks each answer,
shows what is already set, and sets what is missing. It also tells you how to turn on the git hook
that stops personal data being committed.

```bash
python scripts/setup_repo.py --dry-run     # step through it all; changes nothing
python scripts/setup_repo.py               # the real thing; asks before it sets anything
python scripts/setup_repo.py --help
```

It needs only Python 3.11+ and the GitHub CLI (`gh auth login`): no AWS credentials, no
`pip install`. Secret values are never printed and never passed on a command line. Nothing is set
until you have seen a summary and confirmed; if a write then fails part-way, it stops and lists
what was and was not set. See
[docs/deploying-your-own.md](../docs/deploying-your-own.md#quick-start-the-setup-script).

## Why a CLI, not a web page

The Admin API requires AWS SigV4 (IAM) auth and is additionally restricted
to the admin's IP by a WAF allowlist. A browser page can't safely hold
long-lived IAM credentials to sign requests with, so this CLI signs
requests using whatever AWS credentials your local environment already has
configured (the same default credential chain `boto3`/`aws` CLI use --
`aws configure`, SSO, environment variables, etc).

## Setup

```bash
pip install -r scripts/requirements.txt
```

Make sure you have AWS credentials configured locally with permission to
call the Admin API (`aws configure`, `aws sso login`, or equivalent), and
that your IP is on the WAF allowlist Terraform configured for the
environment you're targeting.

## Configuration

The CLI needs two things:

1. **Admin API base URL** -- not known until `infra/` has been applied,
   since API Gateway assigns it. After applying, get it with:

   ```bash
   terraform -chdir=infra/environments/<dev-or-production> output admin_api_url
   ```

   Then either export it:

   ```bash
   export BLOGGERBEAR_ADMIN_API_URL="https://<api-id>.execute-api.<region>.amazonaws.com"
   ```

   or pass `--api-url` on every invocation.

2. **AWS region** -- read from `--region`, or falls back to `AWS_REGION` /
   `AWS_DEFAULT_REGION` (already set if you've run `aws configure`).

## Usage

```bash
python scripts/admin_cli.py topics list
python scripts/admin_cli.py topics get github-trending
python scripts/admin_cli.py topics create --topic-id github-trending \
    --name "GitHub Trending" --adapter github_trending \
    --config-json '{"language": "python"}'
# --adapter is optional: a topic with none does independent web research
# (web_search, searching on the topic's name). Give it a topic-specific goal:
python scripts/admin_cli.py topics create --topic-id security-trends \
    --name "Cybersecurity & Infrastructure Threats" \
    --config-json '{"query": "zero-day vulnerabilities exploit wild 2026", "max_results": 15}' \
    --editorial-goals-json '{"primary_focus": "Identify unpatched zero-day exploits actively observed in production.", "exclusion_criteria": "Ignore marketing press releases or compliance frameworks."}'
# replaces the whole editorial_goals block; '{}' clears it (back to the default goal)
python scripts/admin_cli.py topics update security-trends --editorial-goals-json '{}'
python scripts/admin_cli.py topics update github-trending --name "Renamed" --financial
# New topics run their daily article at 9 AM Australia/Sydney (follows daylight
# saving). A topic created before --daily-timezone existed is still on UTC until
# you move it -- an unrelated update never changes its run time:
python scripts/admin_cli.py topics update github-trending \
    --daily-cadence "cron(0 9 * * ? *)" --daily-timezone Australia/Sydney
# How often research really runs. `--research-cadence` is only the heartbeat (hourly by
# default); each heartbeat checks whether the topic is due. A topic's own interval wins,
# else the pipeline-wide one, else 1 hour. Whole hours, 1-168; '' clears it. Applies from
# the next heartbeat. Editing `research_interval_hours` straight in DynamoDB works too.
python scripts/admin_cli.py topics update github-trending --research-interval-hours 3
python scripts/admin_cli.py topics update github-trending --research-interval-hours ""
python scripts/admin_cli.py pipeline-config get
python scripts/admin_cli.py pipeline-config set --research-interval-hours 2   # save cost
python scripts/admin_cli.py pipeline-config set --research-interval-hours ""
# Fresh-data review of each draft before it is checked and published. `shadow` (the
# default) runs it and records the result on the article and, if it goes to moderation,
# on its queue item (`review_notes`, shown by `moderation list`) without changing any
# outcome; `off` skips it; '' clears the setting. Costs about one extra model call per article.
python scripts/admin_cli.py pipeline-config set --review-mode off
# `enforce` makes the review ACT: a minor problem is corrected by one revision pass (checked by
# plain code -- no new figures or links, similar length -- and the original body is kept); a
# major problem, a correction that can't be trusted, or a review that couldn't run holds the
# article for a person, with the reasons in `moderation list`. Decide from `review report`.
python scripts/admin_cli.py pipeline-config set --review-mode enforce
# If a review can't run in enforce mode: hold the article (default) or publish and note it.
python scripts/admin_cli.py pipeline-config set --review-on-unavailable note
# One topic can differ from the pipeline-wide mode ('' clears it back to inheriting):
python scripts/admin_cli.py topics update github-trending --review-mode enforce
python scripts/admin_cli.py topics update github-trending --review-mode ""
# How the review is doing, from the records it leaves on articles: counts by outcome and
# topic, unavailable reasons, what enforcement WOULD have held and revised (the number that
# decides whether turning it on is safe), readiness against the plan's starting thresholds,
# and a sample of recent flagged claims to check by eye.
python scripts/admin_cli.py review report
python scripts/admin_cli.py review report --sample 25
python scripts/admin_cli.py pipeline-config set --review-mode shadow
python scripts/admin_cli.py pipeline-config set --research-interval-hours 2 --review-mode shadow
# Who may reach the operator's assistant (the ops MCP server). `open` (the default) is any
# signed-in caller from anywhere; `allowlist` is only from the operator's addresses (the
# assistant's OPS_ASSISTANT_ALLOWED_CIDRS, set by Terraform); `off` refuses every request;
# '' clears the setting (back to open). The assistant reads it on every request, so a
# change applies from the next one, with no deploy. `pipeline-config get` shows it.
python scripts/admin_cli.py pipeline-config set --assistant-access allowlist
python scripts/admin_cli.py pipeline-config set --assistant-access off
python scripts/admin_cli.py pipeline-config set --assistant-access ""
python scripts/admin_cli.py topics delete github-trending
python scripts/admin_cli.py topics trigger github-trending --pipeline research_tick   # a manual run always runs now, whatever the interval
python scripts/admin_cli.py topics trigger github-trending --pipeline daily_cycle --no-wait
# daily_cycle only writes from findings newer than the topic's last article, so a
# repeat run with nothing new is a no-op. --force rewrites from the whole last-24h window:
python scripts/admin_cli.py topics trigger github-trending --pipeline daily_cycle --force
python scripts/admin_cli.py topics candidates github-trending
python scripts/admin_cli.py topics findings github-trending

python scripts/admin_cli.py moderation list
python scripts/admin_cli.py moderation approve <queue_id>
python scripts/admin_cli.py moderation reject <queue_id>

python scripts/admin_cli.py articles publish <article_id>
python scripts/admin_cli.py articles unpublish <article_id>
python scripts/admin_cli.py articles rewrite <article_id> --instructions "the second section confuses staking with lending"
python scripts/admin_cli.py articles rewrite <article_id> -i "too long; cut the history section" --model <model_id>
python scripts/admin_cli.py articles rewrite <article_id> -i "names the wrong company" --force   # take it down now

python scripts/admin_cli.py failed-executions list

# Lineage/cost data: where it is missing, and repair. `backfill` is a dry run
# unless --apply; it only rewrites `lineage` (tokens are kept), and is safe to repeat.
python scripts/admin_cli.py lineage audit
python scripts/admin_cli.py lineage backfill
python scripts/admin_cli.py lineage backfill --apply

python scripts/admin_cli.py models list
python scripts/admin_cli.py models add --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --display-name "Claude Haiku 4.5" --provider anthropic \
    --input-price 0.001 --output-price 0.005
# (USD per 1K tokens. Confirm against the Bedrock price page for your region -- a
# geographic inference profile can cost more than the provider's list price.)
python scripts/admin_cli.py model-config get
python scripts/admin_cli.py model-config set --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0"

# Per-topic: pin one model, set a fallback, or rotate between several
# (one picked at random per daily run). '' clears a value.
python scripts/admin_cli.py topics update github-trending     --model-candidates "model-id-a,model-id-b" --fallback-model-id "model-id-a"
python scripts/admin_cli.py topics update github-trending --model-candidates ""
```

`models`/`model-config` back the AI lineage/cost-tracking enhancement's
DynamoDB-backed model registry (docs/project-plan.md §11) -- adding a
model or changing the global default/fallback never needs a Terraform
apply. Resolution order (`common/model_routing.py`'s `resolve_model`):
a topic's `model_id_candidates` rotation list (one picked at random per
run, so all of one article's Bedrock calls use the same model and its
lineage stays coherent), if set → the topic's own `model_id` override →
the global `model-config` default → the Terraform-set `BEDROCK_MODEL_ID`
env var. `fallback_model_id` resolves independently (topic → global).

`articles publish` force-sets an article's status to `published` regardless
of its current state -- unlike `moderation approve`, which only acts on an
item still sitting `pending` in the moderation queue. Use it to publish
something that was never routed to moderation in the first place, or to
override a stuck/undesired status. If a moderation queue item exists for
the article and is still `pending`, it's marked `approved` too so the two
records don't disagree.

`articles unpublish` is the inverse: it deletes the article's static page,
marks the article and its moderation-queue item `rejected` (so it leaves every
public listing), removes the musings written about it, and asks CloudFront to
drop its cached page. The markdown body is kept, so `articles publish` can
bring it back. An article still waiting in moderation is refused (use
`moderation reject`). If it fails part-way, run it again -- every step is
safe to repeat. `cache_invalidated: false` in the response means CloudFront
wasn't asked (or the request failed); a cached copy can then linger until the
CDN's TTL expires.

`articles rewrite` sends an article back to be rewritten, with `--instructions` saying what is
wrong with it. A **published** article stays up, untouched, while it is rewritten: it is taken
down (page, musings and CDN cache, as with `unpublish`) and set back to waiting for review only
once the rewrite is ready to take its place in the inbox. If that rewrite fails, the article is
still published exactly as it was, and `inbox` and `moderation list` say so, with the reason
(`failed_rewrites`). `--force` takes it down first and then rewrites, for an article that must not
stay up meanwhile. A **rejected** article is brought back to waiting for review; one already
**waiting in the inbox** is rewritten in place. The rewrite runs in the background
(`lambdas/common/rewrite.py`) with your note as the main thing to fix, alongside anything the
reviews flagged. It gets the same guards as any rewrite (no figure or link that is in none of the
sources), goes through the fresh-data and compliance reviews again, and comes back to the inbox
for you to approve, reject or rewrite again; approving it publishes it. `--model` picks a
registered model (`models list`); by default it uses the model the topic writes with today. If the
rewrite of an article that was not published (or was taken down with `--force`) fails, the article
waits in the inbox with the reason, and `[w]` there retries with the same note. Approving it writes a fresh musing about the new version, in place of the ones the
take-down removed.

`lineage audit` lists articles with no lineage, articles whose cost is blank, and
models in use that nothing can price. `lineage backfill` recomputes each article's
cost from the token counts it recorded, at today's prices (registry first, then the
built-in fallback in `common/model_pricing.py`), and rewrites older ARN-form model
ids to their canonical ids. An article drafted before lineage tracking has no token
counts and can't be recovered -- the audit lists those rather than guessing.

`failed-executions list` shows every daily_cycle Step Functions execution
that exhausted its retries and landed on the pipeline dead-letter queue
(see `lambdas/dlq_handler.py`) -- each item records the topic, the error,
and the raw message, for manual follow-up. There's no automatic replay; to
retry a topic after fixing whatever caused the failure, use
`topics trigger <topic_id> --pipeline daily_cycle` again.

## What is waiting for you: `inbox` and `approve`

Two things need a person, and both are covered:

1. **Articles in the moderation queue.** Financial topics (crypto, the trending digest) always wait for
   you, by design. Others land here when the compliance or fresh-data review held them, and the reasons
   are shown. (Every article that needs a decision has a queue item, so this is all of them.)
2. **Prompt-change proposals** from the weekly reflection. Approving one changes how future articles on
   that topic are written.

```
python scripts/admin_cli.py inbox                 # what is waiting, at a glance
python scripts/admin_cli.py approve               # go through it, one keystroke each
python scripts/admin_cli.py approve --mock        # practise the keys on made-up items: no AWS
```

`approve` shows one item at a time (where it is from, its title, how long it has waited, why it needs
you, a preview) and waits for one key, no Enter:

| Key | Does |
|---|---|
| `y` | Approve. An article is published (page rendered, musing written); a prompt change goes live for future drafts |
| `r` | Reject. An article stays private; a prompt change is dropped |
| `w` | Re-Write (articles held for a reason only). Pick a model; the article is rewritten in the background to fix what it was held for, and you move on |
| `z` | Skip: leave it exactly as it is |
| `v` | Read the whole text, then choose |
| `q` | Quit. Nothing you already decided is lost |

- **Re-Write** (`w`) sends the article, its hold reasons, its research and current data to the model
  you pick (any enabled model from `admin_cli models`). The result is checked like the automatic
  revision (no figure or link that is in none of the sources), reviewed again, and comes back to
  this inbox as a new item showing `Re-Write #n by <model> (~$cost)` and the old title, to approve,
  reject or rewrite again. The article is never published by a rewrite, and the text it replaced is
  kept in S3 (`articles/<id>.before-rewrite-<n>.md`). If it fails, the original comes back with the
  reason; one that never finishes is put back after 15 minutes. Its tokens and cost go into the
  article's lineage (stage `rewrite`) and the week's Stats.
- **Up to 30 a time**, oldest first, articles before prompt changes. Run it again for the next batch
  (`--limit` changes the size; `--source moderation` or `--source refinements` picks one kind).
- **Each choice is applied at once**, through the same signed Admin API as every other command, so a quit,
  an error or a dropped connection never loses progress. A failure is shown and you stay on that item to
  retry, skip or quit. Something already handled elsewhere is noted and passed.
- **Skipped items are hidden from your next runs** for 24 hours (`--skip-hours`; a small local file,
  `~/.bloggerbear/review-skips.json`, or `BLOGGERBEAR_REVIEW_STATE`; never sent anywhere), so a rerun gives
  you the next batch instead of the same ones. `--include-skipped` shows them again, `--reset-skipped`
  forgets them.
- **Held for a reason?** If the review found something (a fabricated claim, stale figures, advice), the
  reasons and notes are on the card, and approving asks "Approve anyway? [y/N]". Rejecting never asks.
  A routine financial article (held only because it is a financial topic) does not ask.
- `--dry-run` goes through the motions and changes nothing. `--mock` (or `BLOGGERBEAR_REVIEW_MOCK=1`)
  uses made-up items and its own skip file, needs no credentials, and sends nothing anywhere.
- **Approving a prompt change asks where the bear wears it** (see the next section): `t` (or Enter) a ring
  for its topic, `g` an armor slot (it lists what is worn, and says what would be replaced), `b` the
  backpack. `c` backs out and leaves it pending.

## What the bear wears: `equipment`

An approved prompt change is *gear*. Only worn gear is injected into the drafting prompts:

- **Armor** (helmet, chest, gloves, boots, sword, shield) is *global* guidance, used for every topic. One
  item per slot; equipping into a taken slot sends the old item to the backpack.
- **Rings** are *topic* guidance, used only for the topic the change was proposed for. Five at most; when
  they are all worn, equipping another means naming the ring it replaces.
- **The backpack** is approved gear that is not worn. Nothing in it is used.

```
python scripts/admin_cli.py equipment list                        # every slot, the rings, the backpack
python scripts/admin_cli.py equipment equip TOPIC VERSION --scope global --slot helmet
python scripts/admin_cli.py equipment equip TOPIC VERSION         # a ring for its topic
python scripts/admin_cli.py equipment equip TOPIC VERSION --replace OTHER_TOPIC OTHER_VERSION
python scripts/admin_cli.py equipment unequip TOPIC VERSION       # into the backpack, no longer used
python scripts/admin_cli.py refinements approve TOPIC VERSION --scope backpack   # approve, do not wear
```

Wearing is not the same as using. The topic's rings are used in every article, but the bear takes in only
*some* of its worn armor each time: a random number of pieces, at least one, chosen at random. The article
records which pieces it used.

**Make your own gear.** You do not have to wait for readers: write the guidance yourself and it becomes gear at
once (approved, named, given a rarity and durability) and, by default, is put on.

```
python scripts/admin_cli.py equipment create                     # guided: it asks what it needs, one question at a time
python scripts/admin_cli.py equipment create --text "Lead with the most useful fact." --slot helmet
python scripts/admin_cli.py equipment create --text "Name the repository." --topic-id github-trending --rarity epic
python scripts/admin_cli.py equipment create --text "..." --no-equip     # into the backpack, not worn
python scripts/admin_cli.py equipment delete TOPIC VERSION               # for good; it asks first (--yes skips)
```

Every choice has a default: the bear names the gear (or pass `--theme "Plain Speaking"`), suggests the slot, and
the rarity is rolled like a drop (or pass `--rarity legendary`; the durability then follows the rarity). Armor
is filed under the reserved topic `global`, and `global` cannot be used as a real topic's id. Checks run
before anything is written, so a refusal (a sixth ring, an unknown topic, a bad name) creates nothing. Deleting
removes it entirely; articles already written with it keep their own record of it.

**Loot drops.** The first time a piece of gear is worn, BloggerBear posts about it on the Musings page: a
short, excited, tweet-like post naming the gear and thanking readers, next to a card with its name, rarity,
slot and what it does -- coloured by rarity, same as the Stats page. It is written by the model in the
same voice as every other musing and screened the same way a comment is; if the model fails or the reply
does not pass, a plain accurate post is used instead, so a drop is always announced. It only ever
announces once per piece: taking it off and putting it back on (a repair, say) does not post again.

Every command that can put something on takes `--no-announce` to stay quiet (`refinements approve`,
`equipment equip`, `equipment create`), and the guided `equipment create` asks. To post the announcement
for a piece later, or again:

```
python scripts/admin_cli.py equipment announce TOPIC VERSION
```

`--scope global` with no `--slot` takes the first empty armor slot. Approving with no choice made wears it
as a ring for its topic (what approving always meant), or leaves it in the backpack if all five rings are
worn. Unlike before, guidance now *stacks*: several rings for one topic, plus the armor, are all used
together. A change approved before this existed keeps working exactly as it did (the latest one per topic)
until that topic has a ring of its own.

**Every piece of gear has a name, a rarity and a durability.** The weekly reflection names each proposal
when it writes it, so the card in `approve` already shows what the bear found (for example *Breastplate of
Plain Speaking, rare, durability 17/17*, and which slot the bear suggests). Rarity is rolled by code, at
random, never chosen by the model: common 50%, uncommon 28%, rare 14%, epic 6%, legendary 2%. The most wear an
item can take is rolled within its rarity's range (common 6-10, uncommon 10-15, rare 15-20, epic 21-30,
legendary 40-50), and durability starts full and can never go over that maximum. The name is
`<slot noun> of <theme>` (Helm, Breastplate, Gauntlets, Boots, Blade, Shield, Ring), so an item moved to
another slot is renamed with it. When the bear suggests armor, Enter in the placement question takes its
slot (or the first empty one if that is taken).

```
python scripts/admin_cli.py equipment bump TOPIC VERSION               # one rarity step up
python scripts/admin_cli.py equipment bump TOPIC VERSION --to epic     # or straight to one
```

A bump only goes up. It re-rolls the maximum in the new rarity's range (never lower than it was) and adds the
extra room to the item's durability, so making a battered item rarer does not repair it. An item proposed or
approved before this has no rarity: it is given one, once, the first time it is approved, worn or bumped.

**Wear.** Durability is how gear's record shows. When feedback is stored, each piece of gear the article
*used* takes it: a downvote costs 1 durability, an upvote gives 1 back (never above the maximum). Feedback
that is turned away (a rejected comment, a bad token, a limit, the honeypot) never counts. Only gear that is
being *worn* is affected, so an upvote on some old article does not revive gear that has been retired.

- At 0 the piece is taken off (into the backpack, marked `worn_out`).
- If a **parked** spare is waiting (an item approved when there was no room for it: a ring for the same
  topic, or global armor for armor), the one with the most durability left takes its place. Anything you
  benched, took off, displaced or shelved is never put back automatically, and a worn-out piece stays worn
  out until you repair it.
- `equipment repair TOPIC VERSION [--amount N]` restores durability (all of it by default), never above the
  maximum, and leaves the item where it is. A repaired worn-out piece is in the backpack until you
  `equipment equip` it. Gear at 0 durability can't be equipped until it is repaired.

Each article records which gear was in the prompts that wrote it (`equipment_used`: topic, version and slot
per piece; an empty list means none). Nothing reads it yet: it is there so a later change can measure whether
gear helps, and share out wear. It is never shown publicly.

### Testing the DLQ consumer manually

`dlq_handler.py` is only exercised for real when a `daily_cycle` execution
actually fails both of its Step Functions retries -- to verify it works
without waiting for (or forcing) a real failure, send it a synthetic
message shaped like what the state machine's `Catch` state produces:

```bash
QUEUE_URL=$(terraform -chdir=infra/environments/dev output -raw pipeline_dlq_url)
aws sqs send-message \
  --queue-url "$QUEUE_URL" \
  --message-body '{"topic_id": "test-topic", "error": {"Error": "States.TaskFailed", "Cause": "synthetic test message"}}'
```

Then confirm it landed:

```bash
python scripts/admin_cli.py failed-executions list
```

and check `aws logs tail /aws/lambda/bloggerbear-dev-dlq-handler --since 5m`
for the `dlq_handler: daily_cycle failed for topic_id=test-topic: ...` log
line. If `pipeline_dlq_url` isn't an existing Terraform output yet, get the
queue URL instead with
`aws sqs get-queue-url --queue-name bloggerbear-dev-pipeline-dlq`.

Responses are pretty-printed JSON on stdout. A non-2xx response prints the
error body to stderr and exits non-zero.

`topics trigger`'s target Lambda invocation is fire-and-forget (async), so
by default the command polls afterward and only returns once it sees actual
new output -- a new Finding for `research_tick`, a new candidate idea for
`daily_cycle` -- or times out (30s / 90s respectively) and says so. Pass
`--no-wait` to skip this and get the old immediate-return behavior back.
This also means running `daily_cycle` right after `research_tick` for the
same topic is safe without a manual delay in between; running it beforehand
still isn't -- `daily_cycle` needs a Finding to already exist.
