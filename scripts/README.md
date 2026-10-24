# BloggerBear admin CLI

A local operator CLI for the Admin API. This is a plain script you
run on your own machine -- it is **not** deployed to Lambda or anywhere
else.

**New here, or just need the export commands?** See [QUICKSTART.md](QUICKSTART.md).

## First-time setup of your repository: `setup_repo.py`

Deploying your own copy? This one comes before the admin CLI. It asks for the GitHub secrets and
variables a deployment needs (the AWS region and the name prefix first, then alert emails, the AWS account IDs and
deploy role ARNs, the state bucket names and `PII_DENYLIST`), checks each answer,
shows what is already set, and sets what is missing. It also tells you how to turn on the git hook
that stops personal data being committed.

The region question is optional: Enter keeps the default, `ap-southeast-2`. Answer with another
region and the script puts it in the bootstrap command it prints, and lists the few things you
must then change by hand (the Bedrock model profile for your part of the world, above all).

```bash
python scripts/setup_repo.py --dry-run     # step through it all; changes nothing
python scripts/setup_repo.py               # the real thing; asks before it sets anything
python scripts/setup_repo.py --help
```

It needs only Python 3.11+ and the GitHub CLI (`gh auth login`): no AWS credentials, no
`pip install`. Secret values are never printed and never passed on a command line. Nothing is set
until you have seen a summary and confirmed; if a write then fails part-way, it stops and lists
what was and was not set. See
[docs/deployment-runsheet.md](../docs/deployment-runsheet.md#quick-start-the-setup-script).

## Who may reach the admin API: `admin_allowlist.py`

The admin API answers only your addresses. They are one SSM parameter per environment, which this
script writes and then copies into what enforces it (the admin WAF IP set and the API's resource
policy, deploying the stage), so a new IP needs no apply and no secret:

```bash
python scripts/admin_allowlist.py set --env dev --my-ip    # after the first deploy, and when your IP changes
python scripts/admin_allowlist.py show --env dev           # what each place holds; only reads
python scripts/admin_allowlist.py sync --env dev           # copy the stored list out again
```

It reads no environment variable for its names: it assumes the prefix `bloggerbear` and the region
`ap-southeast-2` unless you pass `--prefix <your UNIQUE_NAME_PREFIX>` and `--region <your region>`.
They go before the command: `python scripts/admin_allowlist.py --prefix <yours> --region <yours> set --env dev --my-ip`.
A fork that leaves `--prefix` out writes the original deployment's parameter name and is refused.
It needs boto3 (`scripts/requirements.txt`) and your own AWS credentials. Why, and what it touches:
[configuration.md](../docs/configuration.md#the-admin-apis-address-allowlist).

## Why a CLI, not a web page

The Admin API requires AWS SigV4 (IAM) auth and is additionally restricted
to the admin's addresses by an allowlist (the API's resource policy, plus a WAF IP set when the
admin web ACL is on). A browser page can't safely hold
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
that your IP is on the target environment's admin allowlist (an SSM parameter that
`scripts/admin_allowlist.py` writes, not anything in Terraform).
`python scripts/admin_allowlist.py show --env <env>` says whether this machine is listed;
`set --env <env> --my-ip` puts it there (see above). `set` replaces the whole stored list, so
name any other ranges you want to keep in the same command.

## Configuration

The CLI needs two things:

1. **Admin API base URL** -- not known until `infra/` has been applied,
   since API Gateway assigns it. After applying, get it with:

   ```bash
   terraform -chdir=infra/environments/<dev-or-production> output admin_api_url
   ```

   Then either export it:

   ```bash
   export BLOGGERBEAR_ADMIN_API_URL="https://<api-id>.execute-api.<region>.amazonaws.com/<dev-or-production>"
   ```

   or pass `--api-url` on every invocation. Copy the `admin_api_url` output exactly as it is: the
   admin API is a REST API, so its URL ends with the stage name (`/dev` or `/production`), and the
   CLI only appends paths such as `/topics` to it.

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
# Several goals taken in turn (2 to 7, each {"name", "primary_focus"}; the topic's
# exclusion_criteria apply to all): new findings get the next goal, and with nothing new the
# last findings are revisited under a goal not yet used on them. Replaces the whole list; '[]'
# clears it. Also on `topics create` (docs/configuration.md; rail-cities: docs/RailVisionSeeding.md section 7).
python scripts/admin_cli.py topics update rail-cities --goal-rotation-json "$(cat rail_goal_rotation.json)"
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
# How far back the daily article reads findings: everything since the topic's last article,
# but never more than its findings window, 24 hours by default. A topic that does not write
# every day (one day of a weekly rotation, a weekly topic) widens it so a finding made on a day
# it does not write is still there on the day it does. Whole hours, 1-336 (findings expire after
# 14 days); '' clears it. Also on `topics create`. Editing `findings_window_hours` in DynamoDB works too.
python scripts/admin_cli.py topics update rail-sydney --findings-window-hours 192
python scripts/admin_cli.py topics update rail-sydney --findings-window-hours ""
# A topic that rotates through its subjects, one a day (docs/enhancements/archive/dailysubject_hook.md;
# read by an adapter that names a day subject: rail_access, whose sites are the subjects). Three
# keys of its adapter_config (--config-json on create, --adapter-config-json on update, each the
# whole config: read it with `topics get`, edit it, send all of it back):
#   "rotation": the seven weekdays each naming a subject, every subject at least once,
#       {"MON": "sydney", "TUE": "melbourne", "WED": "singapore", "THU": "boston",
#        "FRI": "sydney", "SAT": "melbourne", "SUN": "singapore"},
#     or a list of subjects cycled one a day: ["sydney", "melbourne", "singapore", "boston"]
#   "rotation_timezone": the IANA zone whose calendar day decides, e.g. "Australia/Sydney"
#     (unset: the topic's daily_timezone when its adapter passes it, else UTC)
#   "rotation_period": with a list only, "day" (the default) or "week": each subject in turn has a
#     whole calendar week, Monday to Sunday (rail-cities: docs/RailVisionSeeding.md section 7)
# Set --findings-window-hours to the longest gap between one subject's days, plus a day (24 h a
# day). Give a rota to a new topic, not to one that has published: each subject's first window
# starts from the findings window, not from the topic's last article, so it would draft again
# Findings the last article used (docs/configuration.md).
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
# admin API's allowlist parameter, which admin_allowlist.py sets); `off` refuses every request;
# '' clears the setting (back to open). The assistant reads it on every request, so a
# change applies from the next one, with no deploy. `pipeline-config get` shows it.
python scripts/admin_cli.py pipeline-config set --assistant-access allowlist
python scripts/admin_cli.py pipeline-config set --assistant-access off
python scripts/admin_cli.py pipeline-config set --assistant-access ""
python scripts/admin_cli.py topics delete github-trending
python scripts/admin_cli.py topics trigger github-trending --pipeline research_tick   # a manual run always runs now, whatever the interval
python scripts/admin_cli.py topics trigger github-trending --pipeline daily_cycle --no-wait
# daily_cycle only writes from findings newer than the topic's last article, so a
# repeat run with nothing new is a no-op. --force rewrites from the topic's whole findings
# window (the last 24 hours unless it sets --findings-window-hours):
python scripts/admin_cli.py topics trigger github-trending --pipeline daily_cycle --force
python scripts/admin_cli.py topics candidates github-trending
python scripts/admin_cli.py topics findings github-trending
# A topic's research Findings, newest first (the raw items, with how many the window holds).
# Expired ones are left out, except with --next-article, which lists exactly what the next
# daily article will be written from: since the topic's last article, within its findings
# window, at most 48. --limit is 1 to 20 (default 5); --days 1 to 14; --since an ISO timestamp.
# With --next-article, --subject KEY is for a topic that rotates through subjects: what its next
# article on that subject reads (from that subject's last article, its Findings only).
# A topic with a goal rotation and nothing new revisits its last set of Findings instead (not
# listed here; `topics get` shows `goal_rotation_state`).
# The ops assistant's topic_findings tool and the topic-findings Claude Code skill
# (.claude/skills/) show the same window.
python scripts/admin_cli.py findings list rail-sydney
python scripts/admin_cli.py findings list rail-sydney --next-article
python scripts/admin_cli.py findings list crypto --days 2 --limit 10

python scripts/admin_cli.py moderation list
python scripts/admin_cli.py moderation approve <queue_id>
python scripts/admin_cli.py moderation reject <queue_id>
# Read-only counts of the moderation queue, to tune the compliance prompt from; it changes nothing.
# It prints `total_flagged`, counts `by_status` and `by_topic`, `reason_counts` and `recent` (the 20
# newest items with their full reasons). It counts every queue item and every reason on it, not only
# compliance flags, and a Re-Write leaves the old item `rewritten` beside the new one, so one article
# can count more than once. Reasons are counted exactly as written, so the fixed ones add up:
# financial topic, "always reviewed by a person" (a topic with `force_manual_review`, which every
# satellite_vision and rail_access topic has), sent back for a rewrite, truncated draft, implausible
# title, held by Professor Grizzlewick. The compliance model's own reasons rarely match, so read
# those in `recent`. Its help says "across all history", but a rejected item (and a failed rewrite
# of a published article) expires 7 days later, so those drop out after about a week. Pending,
# approved and rewritten items stay (docs/observability.md); unpublishing an article marks its item
# rejected, so that one expires too.
python scripts/admin_cli.py moderation stats

python scripts/admin_cli.py articles get <article_id>       # any status, with its full text
python scripts/admin_cli.py articles checked <article_id>   # you checked a financial article the reviewer bear approved
python scripts/admin_cli.py articles publish <article_id>
python scripts/admin_cli.py articles unpublish <article_id>
python scripts/admin_cli.py articles rewrite <article_id> --instructions "the second section confuses staking with lending"
python scripts/admin_cli.py articles rewrite <article_id> -i "too long; cut the history section" --model <model_id>
python scripts/admin_cli.py articles rewrite <article_id> -i "names the wrong company" --force   # take it down now

python scripts/admin_cli.py failed-executions list

# Sign-ins to the operator's assistant. Every attempt and success is logged; five failed
# attempts in fifteen minutes lock the user, raise a high-severity security incident and send
# the alert email. `unlock` lets them in again at once and does not change the password.
python scripts/admin_cli.py sign-ins list
python scripts/admin_cli.py sign-ins list --days 30
python scripts/admin_cli.py sign-ins unlock <username>

# Security incidents: what the firewalls blocked, attack-shaped comments, sign-in lockouts, and
# the two trends (dropped feedback comments per day: 10 low, 50 medium, 100 high; the admin API's
# 4xx answers per hour: 20, 50, 100). High severity sends the alert email. `open` records
# something you noticed that nothing else did; high severity there emails too.
python scripts/admin_cli.py security list
python scripts/admin_cli.py security list --status acknowledged
python scripts/admin_cli.py security open --severity medium --summary "odd requests to the admin API overnight"
python scripts/admin_cli.py security acknowledge <event_id>   # you are looking into it
python scripts/admin_cli.py security resolve <event_id>       # dealt with
python scripts/admin_cli.py security reopen <event_id>

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
# The model catalog keeps this list and its prices current by itself (docs/models.md):
# - discover: every second day (odd days of the month) at 02:00 Sydney it adds up to 5 models
#   the account can call, from every provider, switched off;
# - reprice: the same days at 03:00 it reprices up to 50. A changed price is used at once and
#   checked 1 and 2 hours later; one a check cannot confirm goes back to the old price (a
#   failed-executions row);
# - cycle: on Fridays at 04:00 it refreshes the topics' rotation lists (`model-catalog set
#   --cycle off` pauses it).
# `models add` with the same id switches a discovered model on; --lock-price keeps the reprice
# off a price you set on purpose.
python scripts/admin_cli.py models add --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --display-name "Claude Haiku 4.5" --provider anthropic \
    --input-price 0.0011 --output-price 0.0055 --lock-price
python scripts/admin_cli.py models reprice     # now, even when paused, with its own two checks
python scripts/admin_cli.py models discover    # now, even when paused
python scripts/admin_cli.py model-catalog get  # paused or not, what each job last did, any run under review
python scripts/admin_cli.py model-catalog set --reprice off   # pause the scheduled reprice (every second day, 03:00 Sydney; '' or on resumes)
python scripts/admin_cli.py model-catalog set --discover off  # pause the scheduled discover (every second day, 02:00 Sydney)
python scripts/admin_cli.py model-catalog set --review-after-hours "1,2" --review-tolerance-pct 5 \
    --max-change-factor 10 --max-web-lookups 10   # the defaults; '' clears one
python scripts/admin_cli.py model-config get
python scripts/admin_cli.py model-config set --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0"
# The shared rotation pool: after an article the daily cycle publishes by itself, or a run whose
# model proposed no usable angle, the topic's model_id becomes one of these at random. Each must be
# a registered, enabled, callable (not `access: denied`) text model. `set` replaces the whole pool;
# '' clears it.
python scripts/admin_cli.py model-rotation get
python scripts/admin_cli.py model-rotation set --candidates "model-id-a,model-id-b"
python scripts/admin_cli.py model-rotation set --candidates ""

# Per-topic: pin one model, set a fallback, or rotate between several
# (one picked at random per daily run). '' clears a value.
python scripts/admin_cli.py topics update github-trending     --model-candidates "model-id-a,model-id-b" --fallback-model-id "model-id-a"
python scripts/admin_cli.py topics update github-trending --model-candidates ""
# Whether Professor Grizzlewick, the reviewer bear, may approve this topic's held articles himself
# ('' falls back to his `may_approve` setting, on by default):
python scripts/admin_cli.py topics update finance-crypto-investing --reviewer-may-approve off

# His settings, read at the start of each run, so no deploy (see `reviewer set --help`):
python scripts/admin_cli.py reviewer get
python scripts/admin_cli.py reviewer set --enabled off                  # pause his scheduled runs
python scripts/admin_cli.py reviewer set --may-approve off              # he reviews, but never approves
python scripts/admin_cli.py reviewer set --defang-after-days 14 --max-rewrites 2
python scripts/admin_cli.py reviewer set --review-published ""          # back to its default
```

**The Rail Access Monitor's topic.** One `rail_access` topic, `rail-cities`, over the four
starting cities (Sydney, Melbourne, Singapore and Boston, each with its box; the reach, an editorial
goal that names no city with its four rotating angles (`editorial_goal_rotation`), and a `rotation`
that writes about one city a day, with a 192-hour `findings_window_hours`, so each city's article
drafts every Finding about it since its last one)
is a complete admin API topic body in [`rail_topics.example.json`](rail_topics.example.json).
`topics create` takes a topic as flags, not a file:
[docs/vision-runsheet.md, `rail_access` topics](../docs/vision-runsheet.md#rail_access-topics)
has the lines that read each field out of the file, and how to add a city and check its box.
The file gives each city weekdays; the live `rail-cities` was switched to a city a week
(Melbourne, Singapore, Boston, Sydney) with a weekly reading and four goals taken in turn:
[docs/RailVisionSeeding.md §7](../docs/RailVisionSeeding.md#7-a-city-a-week-written-up-from-three-sides).

Which model a call uses, rotation, the model catalog's discovery and repricing, and setting the
registry up on a new deployment, with a sample `model-config` item: [docs/models.md](../docs/models.md).

`models`/`model-config` back the AI lineage/cost-tracking enhancement's
DynamoDB-backed model registry (docs/project-plan.md §11) -- adding a
model or changing the global default/fallback never needs a Terraform
apply. Resolution order (`common/model_routing.py`'s `resolve_model`):
a topic's `model_id_candidates` rotation list (one picked at random per
run, so all of one article's Bedrock calls use the same model and its
lineage stays coherent), if set → the topic's own `model_id` override →
the global `model-config` default → the Terraform-set `BEDROCK_MODEL_ID`
env var. `fallback_model_id` resolves separately: the topic's, else the global model-config's,
else `BEDROCK_MODEL_ID` (unless that is the model already chosen).

`articles publish` force-sets an article's status to `published` regardless
of its current state -- unlike `moderation approve`, which only acts on an
item still sitting `pending` in the moderation queue. Use it to publish
something that was never routed to moderation in the first place, or to
override a stuck/undesired status. If a moderation queue item exists for
the article and is still `pending`, it's marked `approved` too so the two
records don't disagree.

**Refreshing already-published pages** (for example to add the source credit line, which pages
rendered before it existed do not have). A static article page is written when the article is
published, and only a re-publish or a `backup restore` renders it again. The single-page view
(`/#/article/<id>`), the topic pages and the RSS feed need no refresh: they read the credit from
the API on every load. For one static page (`/articles/<id>.html`) the path today is
`articles publish <article_id>`, which renders the page again from the stored article, credit
included. It is a re-publish, not a plain re-render, so on an article that is already published it also:

- sets `published_at` to now, so the article's date changes and it moves to the top of its topic
  list and the RSS feed;
- sets "approved by" to Humans, whatever it was;
- writes another musing about the article (one more model call).

That is acceptable for a handful of pages you care about (the crypto articles, for CoinGecko's
attribution) and wrong for a mass refresh.

`backup restore` is no refresh tool either. It does render every published page again, with the
same renderer, keeping each article's stored date and approver and calling no model. But it renders
them only as part of a full restore, after it has:

- written every row and object back from the snapshot, which puts every row the snapshot holds
  back as it was when the snapshot was taken;
- left those rows without their pointers to the pre-review original and to the texts that
  rewrites replaced;
- rewritten every topic's schedules, unless you pass `--no-schedules`.

There is no command that only re-renders. The smallest follow-up is an
`articles rerender <article_id>` route that calls the same renderer with the article's stored
`published_at` and `published_by` and does nothing else.

`articles unpublish` is the inverse: it deletes the article's static page (and
its figures, if it has any), marks the article and its moderation-queue item
`rejected` (so it leaves every public listing), removes the musings written
about it, and asks CloudFront to drop its cached page (and, only for an article
with figures, `/articles/figures/<id>/*`). The markdown body is kept, so
`articles publish` can bring it back. An article still waiting in moderation is
refused (use `moderation reject`). If it fails part-way, run it again -- every step is
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

The list also holds a row for each model catalog review that put one or more prices back
(`kind: model_pricing`, no topic; see `lambdas/common/model_catalog.py`). The error's `Cause`
names each model and why, and the raw message holds the pricing run's id and the full list under
`reverted`. Such a row has no topic to retry: if the old price is wrong too, set it with
`models add ... --lock-price` ([docs/models.md](../docs/models.md)). A price put back quietly,
because no review could look it up, writes no row.

## Schedules, the stagger and pausing: `topics schedule`, `reschedule`, `pause`, `resume`

Topics take turns so that no two call their sources or Bedrock at the same minute
(docs/project-plan.md §11, "Staggered schedules by slot"). Every topic has a **slot** (1, 2,
3, ...; the next free one when it is created) and the pipeline has one **stagger**, 5 minutes by
default: slot 1's research heartbeat runs at :05 past the hour it would run anyway, slot 2's at
:10, and every daily cycle starts after the hour's heartbeats, one slot apart (four topics: the
last heartbeat is at :20, so 09:25, 09:30, 09:35 and 09:40 for a 9 AM cadence). The slot sets the minute; the cadence
still sets the hour pattern, the day and the time zone. What was written is on the topic's row
in DynamoDB (`stagger_slot`, `research_schedule`, `daily_schedule`, `paused`) and printed here:

```bash
python scripts/admin_cli.py topics schedule                 # every topic: slot, state, both schedules in words and as written
python scripts/admin_cli.py topics schedule --json
python scripts/admin_cli.py pipeline-config set --stagger-minutes 5    # the gap (0 turns staggering off; '' back to 5)
python scripts/admin_cli.py topics reschedule --dry-run      # what would be written: slots for older topics, every schedule
python scripts/admin_cli.py topics reschedule                # write it: after a stagger change, a topic added or deleted
python scripts/admin_cli.py topics update wow-forever --stagger-slot 3    # move a topic to another turn ('' = next free)
python scripts/admin_cli.py topics create --topic-id ... --name ... --stagger-slot 2
```

`topics schedule` prints, for each topic:

```
github-trending  (slot 2, running)
  research: every 4 hours at :10 past
            cron(10 */4 * * ? *)  UTC
  daily:    daily at 09:30  Australia/Sydney
            cron(30 9 * * ? *)
  real research every 4 h · last researched 2026-10-15T04:10:12+00:00 · last article 2026-10-14T23:30:03+00:00
```

Twelve five-minute slots fill an hour; past that, shorten the stagger or read
docs/enhancements/dispatcher-sqs-enhancement.md.

**Pausing** a topic stops everything of it until it is resumed: no research tick, no candidate
ideas, no daily article. The Topics row's `paused` is what the handlers obey, whatever invokes
them; the two schedules are disabled as well so the heartbeats cost nothing, `topics trigger`
refuses the topic, the topic page's "Articles in the Pipeline" says Paused, and the bear muses
about it (a reflective one on a pause, an excited one on a resume). Pausing twice is a no-op.

```bash
python scripts/admin_cli.py topics pause wow-forever
python scripts/admin_cli.py topics resume wow-forever
```

## What is waiting for you: `inbox` and `approve`

Two things need a person, and both are covered:

1. **Articles in the moderation queue.** Financial topics (crypto) always land here, by design.
   Others land here when the compliance or fresh-data review held them, with the reasons shown.
   (Every article that needs a decision has a queue item, so this is all of them.)
   - An article that has waited two days or more may be decided for you by Professor Grizzlewick,
     the reviewer bear, financial ones included. Each financial article he approves is flagged for
     you to check ([docs/enhancements/archive/reviewer-bear.md](../docs/enhancements/archive/reviewer-bear.md)).
   - `reviewer set --may-approve off` (or, for one topic,
     `topics update <topic_id> --reviewer-may-approve off`) stops only his approvals. A topic set
     to `--reviewer-may-approve on` still lets him approve. He still sends a held article with a
     real problem, or a fault in the draft, for a rewrite, and rejects it once it has had three
     rewrites (`max_rewrites`). To make every decision yourself, also run
     `reviewer set --review-held off`. That switch covers the whole project; there is none per
     topic.
   - Topics with `force_manual_review` (every vision topic) always wait for you.
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
  an error or a dropped connection never loses progress. A reject or a Re-Write is sent the moment you
  press the key, not when the batch is finished: by the time you reach the tenth item, the rewrite of the
  first is already running. A failure is shown and you stay on that item to retry, skip or quit.
  Something already handled elsewhere is noted and passed.
- **It ends by saying what was done**, however it ends (the last item, `q`, Ctrl+C even in the middle of
  a request, or an error):

  ```
  Session summary: reviewed 2 of 14 items.
    2 approved, 0 rejected, 0 skipped.
  Cancelled out of the session with 12 left: nothing you already decided is lost.
  ```

  "Reviewed" counts every item you settled: approved, rejected, sent for a Re-Write, skipped, or
  found already handled elsewhere. `q` reads "You stopped early"; Ctrl+C reads "Cancelled".
- **Skipped items are hidden from your next runs** for 24 hours (`--skip-hours`; a small local file,
  `~/.bloggerbear/review-skips.json`, or `BLOGGERBEAR_REVIEW_STATE`; never sent anywhere), so a rerun gives
  you the next batch instead of the same ones. `--include-skipped` shows them again, `--reset-skipped`
  forgets them.
- **Held for a reason?** If the review found something (a fabricated claim, stale figures, advice), the
  reasons and notes are on the card, and approving asks "Approve anyway? [y/N]". Rejecting never asks.
  A routine financial article (held only because it is a financial topic) does not ask.
- **Crypto and finance figures are approximate on purpose.** Prices move between the research, the
  draft, each review and a Re-Write, so an exact figure was "stale" by the time anything checked it and
  every article came back flagged. The crypto adapter now declares a tolerance of 7%
  (`figure_tolerance_percent` in `lambdas/common/adapters/crypto_feed.py`): its articles are asked to
  write figures loosely ("up more than 30% at the time of checking", not "up 36.2%"), the fresh-data
  review does not flag a figure within 7% of the current one (a note on the card says how many it let
  pass), and a correction or Re-Write may state an approximate figure without being refused. A figure
  that is further off, points the wrong way, or matches nothing in the sources is flagged as before,
  and the article still always waits for you. Its disclaimer now also says the figures may be
  inaccurate or out of date. Those articles end with an "At a glance" table and "Key takeaways".
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
python scripts/admin_cli.py equipment equip TOPIC VERSION         # by its stored scope: armor in its suggested slot (else the first empty one), anything else a ring for its topic
python scripts/admin_cli.py equipment equip TOPIC VERSION --replace OTHER_TOPIC OTHER_VERSION
python scripts/admin_cli.py equipment unequip TOPIC VERSION       # into the backpack, no longer used
python scripts/admin_cli.py refinements approve TOPIC VERSION --scope backpack   # approve, do not wear
python scripts/admin_cli.py refinements list                      # every proposal and piece of gear, any status
python scripts/admin_cli.py refinements list --topic-id github-trending --status pending
python scripts/admin_cli.py refinements reject TOPIC VERSION      # turn a pending proposal down outside the `approve` review session
```

`TOPIC VERSION` names one proposal or piece of gear in every `refinements` and `equipment`
command. Armor made with `equipment create` has TOPIC `global`; armor that began as a
weekly-reflection proposal (approved or equipped with `--scope global`) keeps the topic it was
proposed for, such as `github-trending`. VERSION is the ISO-8601 time the weekly reflection or
`equipment create` wrote it, such as `2026-10-19T03:00:12.345678+00:00`. Copy both from
`refinements list` or `equipment list`; the CLI percent-encodes VERSION for the URL, so pass it as
printed.

- `--status` is matched exactly against the stored status: `pending`, `approved` or `rejected`.
  The CLI offers no choices, so a typo lists nothing.
- `refinements approve` and `refinements reject` act only on a pending proposal, and answer 409
  for any other.
- A rejected proposal is kept for 7 days, then expires
  ([what is kept](../docs/observability.md#tables)).

Wearing is not the same as using. The topic's rings are used in every article, but the bear takes in only
*some* of its worn armor each time: a random number of pieces, anywhere from none to all of it, chosen at
random, and not the same set twice running when another is possible. The article records which pieces it
used.

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

`--scope global` with no `--slot` takes the armor slot the bear suggested, if it is empty, else the
first empty armor slot. Approving with no choice made wears it
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
per piece; an empty list means none). Reader feedback wears exactly that gear (a downvote costs each piece one
point of durability, an upvote gives one back: `lambdas/common/wear.py`), and the article's page shows it as a
small "Equipment used" record beside the lineage footer.

## Fixing a musing: `musings`

A musing is written once, when its article is published. One that went out blank (a mood and a link, then
no text) stays blank until you fix it here, or until Professor Grizzlewick's next run (every second
day, 05:00 Sydney), which writes blank musings from the last week again. He never touches the
articles of a topic with `force_manual_review` (every vision topic), so a blank musing on one of
those stays blank until you fix it here. None of these commands touch the article: no rewrite, no
re-publish, no new date.

```bash
python scripts/admin_cli.py musings list                       # the newest 20, with their ids
python scripts/admin_cli.py musings list --blank               # only the ones with no text
python scripts/admin_cli.py musings list --article <article_id>
# Have the model write it again, in the mood it was published with:
python scripts/admin_cli.py musings regenerate --article <article_id>   # every blank one about that article
python scripts/admin_cli.py musings regenerate <musing_id>              # that one, blank or not
# Or put your own words on it (at most 280 characters):
python scripts/admin_cli.py musings edit <musing_id> --text "Fresh from the den: a new one is out!"
```

- `regenerate` costs one model call per musing and works for musings about **published articles**
  only (the title is what it is written from). Each one in the reply has `written_by`: `model`, or
  `plain` when the model failed or answered with nothing and the plain sentence was used instead,
  so a regenerate never leaves a musing blank. Run it again, or use `edit`, if you want better.
- `--article` only ever touches blank musings; it answers 409 if none is blank.
- `edit` works on any kind of musing (loot drops, feedback and rejection musings too). Those kinds
  cannot be regenerated: what they were written from (a vote count, a piece of gear) is gone.
- Only the text changes. The mood, the date and the link stay, and the item gets an `edited_at`.
- The public feed is cached for a short while, so the change takes a moment to show on the site.

## When the site takes feedback: `feedback-config`

How much reader feedback the site takes is a few settings you change from here, with no deploy.
`get` shows the stored settings, the ones in force and today's usage. `set` changes any of them: a
setting you do not send is left as it is, and `''` clears one back to its default.

```bash
python scripts/admin_cli.py feedback-config get
python scripts/admin_cli.py feedback-config set --locked-down true --lockdown-reason "Back soon"   # no feedback anywhere; the page says so
python scripts/admin_cli.py feedback-config set --locked-down false
python scripts/admin_cli.py feedback-config set --daily-limit 200 --screening-limit ""
python scripts/admin_cli.py articles feedback-lock <article_id>                    # stop feedback on one article
python scripts/admin_cli.py articles feedback-unlock <article_id> --reset-count    # take it again
```

The defaults, and the flag that sets each:

- At most 20 pieces of feedback per 5-minute window (`--rate-limit`, `--rate-window-minutes`).
- At most 100 a day (`--daily-limit`). The day starts in `--daily-timezone`, Australia/Sydney.
- At most 50 on one article, then it is locked (`--article-limit`). An article locked because it
  reached its limit stays at the limit unless `feedback-unlock` is given `--reset-count`.
- At most 300 comments a day sent to the model check (`--screening-limit`). Rejected feedback does
  not count against the other limits, so this one bounds its cost.
- Every submission needs the token from the feedback-status call. `--verification-required false`
  switches that off, in an emergency.
- A token becomes valid at a random time 500 to 2000 ms after it is issued
  (`--token-delay-min-ms`, `--token-delay-max-ms`).
- Once the site is 70% of the way to its daily limit, its rate limit or its daily model-check
  budget, tokens need proof of work (`--pow-threshold-percent`). `--pow-difficulty-bits`, 16, sets
  how much, and 0 never asks.

`--lockdown-reason` is at most 100 characters. Every setting, the checks in order and the limits at
the edge that this does not set:
[docs/architecture/site-and-feedback.md](../docs/architecture/site-and-feedback.md).

## Backups, restores and article exports: `backup`

Terraform builds the skeleton; a **snapshot** is the data loaded into it. It is one file,
`backup/<prefix>-<env>/snapshots/<UTC time>-<manual|schedule>.jsonl.gz`, in the bootstrap state
bucket (outside Terraform state, and kept when an environment is destroyed). It holds:

- **articles**: every row, with its metadata (lineage and cost, sources, review, figures, rewrite
  history) and its **final** text, plus the figure PNGs a page shows. Not the research snapshots,
  the pre-review `.original.md` or the texts a rewrite replaced; the row's pointers to those are
  dropped, so nothing restored points at a missing object;
- **costing**: StatsCurrent, StatsHistory and ViewCounts;
- **gear and refinements** (PromptRefinements), **musings**, reader feedback and the moderation queue;
- **configuration**: Topics, Models and ModelConfig (default model, pipeline and feedback settings).

Not Findings, candidate ideas, failed executions, security events or sign-ins, nor rows already set
to expire, nor the feedback-signing key (a new deployment makes its own).

A snapshot runs **every Sunday at 3am Sydney** (the newest 8 scheduled ones are kept; ones you
take by hand are never pruned). Every job runs inside the Admin API's Lambda in ~20-second steps,
saving where it got to after each, so closing the terminal does not stop it. The command follows
it, printing progress, unless `--no-wait`:

```bash
python scripts/admin_cli.py backup snapshot                 # take one now; prints its key at the end
python scripts/admin_cli.py backup list                     # snapshot files and recent jobs
python scripts/admin_cli.py backup status <job_id> --wait   # follow a job again
python scripts/admin_cli.py backup resume <job_id>          # restart one shown as stalled

# Load a snapshot into this deployment: every row and article written back under its own key
# (so it can be run again safely), then each topic's schedules written as `topics create` writes
# them and every published page rendered again. No model is called. Asks first unless --yes.
python scripts/admin_cli.py backup restore backup/bloggerbear-dev/snapshots/20261017T160000Z-schedule.jsonl.gz
#   The name is the job's start in UTC: Sunday 3am in Sydney is Saturday 16:00 UTC (17:00 when Sydney is on standard time).
#   --no-pages / --no-schedules skip those steps; --bucket reads it from another allowed bucket.

# Copy every article (its row as JSON, its final text and figures) to a bucket, with manifest.json.
python scripts/admin_cli.py backup export                                   # into the backup bucket's exports/
python scripts/admin_cli.py backup export --bucket my-archive --prefix dev-2026-10
```

Each job saves where it got to in the bootstrap state bucket, even an export or restore given
`--bucket`, as `backup/<prefix>-<env>/jobs/<job_id>.json`.
`backup list` shows the snapshot files and the 10 most recent jobs.

A running job whose saved state has not changed for 180 seconds is shown as `stalled` by
`backup status` and `backup list`, and the progress line adds ``[STALLED: run `backup resume`]``.
It means the job's next step was lost, or ran out of retries. `backup resume <job_id>` starts that
step again, from where the job got to. It answers 409 for a job that has already succeeded or
failed, or is still moving, and 404 for an unknown id. How the steps run:
[admin-cli.md, Backup jobs](../docs/architecture/admin-cli.md#backup-jobs).

A restore can take another environment's snapshot (production's into dev), or one copied into this
deployment's state bucket from another region or account: copy the file to the same key under
`backup/`, and restore it. `--bucket` and `export --bucket` only work with a bucket named in the
Terraform variable `backup_export_buckets` (the Lambda role is granted exactly those). Tearing down
and rebuilding dev with a snapshot: [the deployment runsheet](../docs/deployment-runsheet.md#keeping-devs-content-across-a-teardown).

## The one-time stats catch-up: `stats`

```bash
python scripts/admin_cli.py stats backfill-articles           # what the totals would be; writes nothing
python scripts/admin_cli.py stats backfill-articles --apply   # write them
```

`stats backfill-articles` folds every existing article's already-recorded cost into StatsHistory's
all-time total. It was the catch-up for articles drafted before the weekly `articles` category
existed, but it has no date cut-off. Every article drafted since then was already counted when it
was drafted, so `--apply` would count those articles a second time. On a deployment made since
then, a fork's included, that means every article. It is a dry run unless `--apply`, and it refuses
to run twice, but that only stops a second run: the first run is not safe.
What the Stats page counts, its rows, the weekly rollover and the daily cost poll:
[docs/architecture/stats-and-costs.md](../docs/architecture/stats-and-costs.md).

## A week the stats rollover missed: `repair_stats_week.py`

Rarely needed. If the weekly stats rollover does not run one Monday, the next one files two weeks
of counters under the first week's date, the second week has no row in the stats history table,
and its AWS bill is never recorded. The `<prefix>-<env>-schedule-invocations-dropped` alarm is
what tells you a scheduled run was dropped.

```bash
python scripts/repair_stats_week.py --env production --missing-week 2026-09-28           # shows only
python scripts/repair_stats_week.py --env production --missing-week 2026-09-28 --apply
```

It makes a placeholder row for the missing week (the next daily cost poll fills in its AWS bill)
and notes on the week before that its counters cover both. It does not split the counters:
nothing recorded them by day. It talks to DynamoDB directly, not the admin API, so it needs AWS
credentials for that environment's account; pass `--prefix` and `--region` if yours are not the
defaults. Run it within six weeks of the missing one, which is as far back as the cost poll reads.

## The Alexa+ add-on: `alexa_addon_values.py`

Putting the operator's assistant on Alexa+ is a one-time bootstrap per environment, written up in
[alexa/README.md](../alexa/README.md). This script prints the values it asks for, from one
environment's Terraform outputs, and checks they are https and all that environment's own:

```bash
python scripts/alexa_addon_values.py dev
python scripts/alexa_addon_values.py dev --write-manifest ~/bloggerbear-addon-dev/addon-package/addon.json
```

It never prints the Alexa client secret unless you pass `--show-secret`, and it will not replace an
existing manifest without `--force`. It needs Terraform and read access to that environment's state.

## Timing the vision worker: `vision_benchmark.py`

The vision worker (`lambdas/vision_worker_handler.py`; [docs/enhancements/archive/vision.md](../docs/enhancements/archive/vision.md))
is benchmarked with one script, run the same way on every machine so the rows compare like with
like: stock OpenCV 5 against OpenCV's COOL build, x86 against Graviton, OpenCV 5 against 4. It
needs `lambdas/requirements-vision.txt` installed (`worker` mode also needs boto3 and AWS
credentials) and reads only public data.

```bash
python scripts/vision_benchmark.py kernels --out docs/hackathon/evidence/runs     # the OpenCV operations alone, on synthetic images
python scripts/vision_benchmark.py scenes --cache ~/cogs --out docs/hackathon/evidence/runs   # the pinned scenes through the real handler
python scripts/vision_benchmark.py worker --arn <function ARN>                     # a deployed worker, end to end
python scripts/vision_benchmark.py compare A.json B.json --out docs/hackathon/evidence/runs   # two runs: speed-up and parity
python scripts/vision_benchmark.py controls --cool-sha <hex> --out docs/hackathon/evidence/runs   # the COOL guard's negative controls
python scripts/vision_benchmark.py fetch-network --scene sydney-rail-2024-08-26    # a rail scene's OpenStreetMap network
```

`kernels` times every operation the worker spends its CPU on, for both of its tasks: the ship pass
(resize, blur, adaptive threshold, morphology, components, contours, the warp, `analyse_site`) and
the rail access pass (the distance transform, hit-or-miss thinning, polylines, the colour map, the
desert components, `analyse_rail_access`, with that analysis's own stage timings so the split
between masks, access, graph and metrics is measured), on fixed synthetic images, so network and
data never enter it. `scenes` runs the worker handler in-process on the scenes pinned in
`vision_benchmark_scenes.json` (add scenes; never change one, or old reports stop being
comparable) and records, per scene, the handler's stage timings with every sample, the rail
metrics it measured (served share, areas, counts, hubs, flags, the top suggestions) and their
digest, the figure's sha256 and shape (the PNG is written beside the run file), and the peak
memory. `worker` invokes a deployed worker, every site id prefixed `bench-` so its lines never
pass for production's, and adds the round trip; its run file names the worker by Region and by
the function's `CodeSha256` (null when the caller may not `lambda:GetFunctionConfiguration`),
never by its ARN, which holds the account id. `compare` takes two run files of one kind from
one machine and the same inputs (it refuses anything else: "not the same machine", "not the same
inputs") and reports the speed-up of B over A as the ratio of medians with a seeded bootstrap 95 %
interval, per case or stage, the headline on the compute stages (`analyse` and `annotate`; `graph`
is inside `analyse`, the read is reported apart), each stage's share of the total, and for scenes
the parity of the rail metrics (an equal digest, or each scalar within its tolerance and the hubs,
flags and suggestions identical; a scene measured in one run and not the other, or no scene
measured in both, fails it too; exit 1 when it fails) and the share of figure pixels that differ.
`controls` runs the COOL guard's three negative controls in the current interpreter on one real
scene: `cool` with the pin (`ok` only on the pinned build, else `not_cool`), `cool` with no pin
(`not_cool`), an `opencv` request to the `cool` backend (`backend_mismatch`).

Every run records the OpenCV build fingerprint and the CPU, and with `--out` writes a
`bloggerbear.vision-eval/1` run file ([docs/hackathon/evidence/README.md](../docs/hackathon/evidence/README.md)
has the schema): the code's git commit, the environment (cores, affinity, threads, library
versions, where it ran, and on Linux the OpenCV shared objects mapped into the process with their
sha256 and package, plus the build lines that tell a vendor build apart), the inputs' ETags, the
parameters, the results and a flat summary. `--out` takes a `.json` path or a directory (then
`<run_id>.json`, the run id being `<UTC date>-<kind>-<cpu slug>-<backend>`, with `--tag` as a
suffix for several runs of one kind a day). A Markdown table is always printed.

The options, as `--help` gives them: `kernels`, `scenes`, `worker` and `controls` take
`--repeats` (timed runs per case; 20, 3 and 5 when left out), `--out`, `--tag`, `--imds` (the EC2
instance identity through IMDSv2 with 1 s timeouts: five fields are kept, the account id and the
rest are dropped; `host` is null off EC2) and `--label k=v` (repeatable; `cool_product`,
`cool_version`, `cool_delivery` as `ami` or `container`, `image_digest`, `price`; any other key is
an error); `kernels`, `scenes` and `controls` take `--threads N` (`cv2.setNumThreads`, recorded)
and `--pin-cpu K` (one core, through `sched_setaffinity`; Linux only, refused with a message
elsewhere: use the OS's own tool, the affinity is recorded either way, as `taskset`'s is);
`kernels` takes `--size` (the synthetic images' side, 2048 px); `scenes`, `worker` and `controls`
take `--scenes-file`, `--only NAME` (repeatable) and `--backend opencv|cool`, where `cool` needs
`--cool-sha <hex>`, the pinned COOL build's fingerprint (the in-process modes set the handler's
`VISION_BACKEND` and `COOL_BUILD_SHA256` from these before calling it, so a `cool` run on any other
build is answered `not_cool` by the handler itself, and `kernels --backend cool` refuses to run on
any other build); `scenes`, `controls` and `worker` take `--manifest` (the evaluation's manifest,
whose ETags pin the scenes they share: a changed ETag refuses the run before any read or
invocation, "input changed", and the run file records the ETags HEAD found); `scenes` and
`controls` take `--cache [DIR]` (every range read answered from `DIR` after the first, keyed by
URL, ETag and range, so repeats and both backends read identical bytes; the default is
`~/.cache/bloggerbear-cogs`, `%LOCALAPPDATA%\bloggerbear-cogs` on Windows, never inside the
repository); `scenes` takes `--isolate` (each scene in a fresh interpreter, so its peak memory is
its own, and the run's cache counts and mapped libraries are those of the processes the scenes ran
in); `worker` needs
`--arn`; `compare` takes two run files, `--bootstrap` (2000) and `--seed` (0); `fetch-network`
takes `--scene NAME` (required), `--scenes-file` and `--out`. The worker runs on one vCPU, so a
CPU's rows are closest to it with the run pinned to one core.

A `rail_access` scene also needs its OpenStreetMap network, which the worker is sent with the
request. The scenes file names `vision_benchmark_networks/sydney.json` for both Sydney scenes. The
four cities' networks (`sydney`, `melbourne`, `singapore` and `boston`) were committed on 17 October
(#349), and the directory's `NOTICE.md` carries the Open Database Licence attribution and each
file's row: where and when it was fetched, and its sha256. A fresh capture is step 5 of
[the COOL benchmark runsheet](../docs/cool-benchmark-runsheet.md): from the deployed city's network
object, or `fetch-network`, which asks Overpass for the scene's polygon through
`lambdas/common/osm.py`, the same client the adapter uses, and writes what its `fetch_network`
returned: the ways, stations and points of interest, the `stops` layer (every bus and tram stop in
the polygon as bare `[lon, lat]` points per mode, thinned to the 20 m grid and capped at 40,000),
`counts` (with `stops` and `stops_by_mode`) and `bytes`, the Overpass body's size; the run prints
`wrote <path>: {counts} bytes=N`, so a city's reply can be read against the client's 20 MB cap.
The four committed networks predate the stops layer and carry no `stops`, which the worker
reports as the quality flag `no_stops_layer` (every stop measure null, no transit-reach figure);
they keep working, and a refetch gives them the layer. Without the file, the rail scenes' rows
read `error: network_file missing`. Overpass is not reachable from the build containers, so a new
file is captured on a machine that can reach it, checked with `pii_denylist_check.py --staged`, and
committed, so later reports say (`fetched_at`, and the file's sha256 in `inputs.networks`) which
network they measured.

## The evaluation's inputs: `vision_eval.py`

The evaluation reads pinned scenes, so every run file names the bytes it read.
`vision_eval_manifest.json` (`bloggerbear.vision-manifest/1`) holds the Sydney site and 46
Sentinel-2 L2A scenes on AWS Open Data by id, each with its four assets' URL, ETag and size, the
STAC item's capture time, cloud cover, no-data share and footprint, its role (`clear`, `season`,
`cloud`, `nodata`, `smoke`, `baseline`, `year`), and what `coverage` measured. Add a scene; never
change one.

```bash
python scripts/vision_eval.py pin                                # HEAD every asset, read every STAC item; print what differs
python scripts/vision_eval.py pin --write                        # ... and write it into the manifest
python scripts/vision_eval.py pin --list-year 56HLH 2024 --write # add a tile's year by the production rule (the year set)
python scripts/vision_eval.py coverage --out docs/hackathon/evidence/runs --write   # E1 and E2; the run file; the manifest's coverage
```

`pin` HEADs each asset for its ETag and Content-Length and reads the scene's STAC item from the
same bucket folder (`<scene>/<scene>.json`: no Earth Search call), prints the differences from
the manifest, writes them with `--write`, and exits 1 when a pinned ETag changed (earlier runs read
other bytes). `--list-year TILE YEAR` lists the bucket month by month (`list-type=2` with a
delimiter) and, with `--write`, keeps the whole listing under `listings` and adds every item that
passes the production rule as a `year` scene: STAC cloud cover at most the rail adapter's
`max_cloud_cover` (40) and a footprint that holds the site box (the same rule as the deployed
search), one per date, the others kept as `duplicate_of` the one chosen (a Sentinel-2C tandem item,
more tile no-data, or a later processing sequence) so they are measured but outside the set. It
reads `sentinel-cogs.s3.us-west-2.amazonaws.com` only, with 10 s timeouts; a URL at any other host
is refused before any request.

`coverage` reads each scene's SCL inside the site polygon with the repository's own COG reader
(`lambdas/vision/cog.py`, `geo.py`) and reports the share of every scene-classification class,
the coverage as the worker defines it (the share outside `vision/masks.SCL_UNUSABLE`), whether
it is under the adapter's floor (0.6, imported, never retyped) and the dominant cause when it is,
and the mean aerosol optical thickness over the site from the scene's `AOT` band at its coarsest
overview (smoke that SCL does not flag); about 60 range requests and 0.7 MB a scene. From the
manifest's listing it counts the selection: the items, those passing the cloud filter, those of
them passing the footprint rule, and those of them still under the floor (worker calls the search
would make for nothing); the year set is the scenes that pass the rule with site coverage 0.95 or
more. It writes an `eval-coverage` run file (`--out`, `--tag`), fills `site_coverage` and
`aot_mean` into the manifest with `--write`, and records each year set there by name, in time
order (`sets`, `year-2024`: a `year` role marks a candidate of the listing, and a candidate under
0.95 or another's duplicate is outside the set, so a run of the set reads `sets`, never the
roles), takes `--only NAME` and `--cache [DIR]`, HEADs each asset first and refuses a changed
ETag ("input changed"), and exits 1 when a scene's `expected` (`low_coverage` for the cloud and
no-data cases) was not met. Both subcommands run on Windows as well as Linux and macOS (`py -3.11 scripts\vision_eval.py ...`); they need
`lambdas/requirements-vision.txt` and `scripts/requirements.txt`.

## The evaluation proper: `vision_eval.py replay`, `failures`, `access-baseline`, `index`

`replay`, `failures` and `access-baseline` each write one run file (`--out DIR`, `--tag`) with the
code, machine, build and inputs it ran on, and run in CI on a synthetic city with scripted parts;
on real data they are the operator's runs (the runsheet's [evaluation runs](../docs/evaluation-runsheet.md#the-evaluations-real-data-runs),
bash and PowerShell). Every real-data run HEADs its scenes first and refuses one whose pinned ETag
moved ("input changed"); `--cache [DIR]` answers the worker's reads from DIR after the first.
`index` writes no run file and takes no `--out` or `--tag`: it rebuilds `index.json` and
`RESULTS.md` in `--evidence DIR` (default `docs/hackathon/evidence`) from the run files already in
its `runs/`.

```bash
python scripts/vision_eval.py replay --sequence sydney-demo --worker <ARN> --agent bedrock --model-id <id> --out docs/hackathon/evidence/runs --trace
python scripts/vision_eval.py replay --sequence sydney-demo --agent recorded --from docs/hackathon/evidence/runs/<run>.json --cache ~/cogs
python scripts/vision_eval.py replay --sequence synthetic-city                       # CI's: the synthetic city, scripted agent
python scripts/vision_eval.py failures --cache ~/cogs --out docs/hackathon/evidence/runs
python scripts/vision_eval.py access-baseline --cache ~/cogs --out docs/hackathon/evidence/runs   # E5; --scene synthetic-1 in CI
python scripts/vision_eval.py index            # docs/hackathon/evidence/index.json and RESULTS.md
python scripts/vision_eval.py index --check    # change nothing; exit 1 if either is stale or a label is wrong
```

**`replay`** (2.6.4) drives the real `RailAccessAdapter` through `fetch_state` then `material_diff`,
tick by tick, over a sequence of the manifest's scenes (`sydney-winter`, `sydney-demo` with the demo
topic's thresholds of 0, `sydney-c1` with network B on its last tick, and `synthetic-city`), as a
one-site topic like `trains-sydney`, each tick six hours after its scene. The worker is in this
process (`--worker in-process`, the default) or the deployed one (`--worker <ARN>`, without
`--cache`; every site id sent is `eval-sydney`, and the run file names the Region, never the ARN).
The agent is Bedrock (`--agent bedrock --model-id <id> [--region R]`: every Converse response
recorded; an application inference profile's ARN reaches Converse alone, and the run file and the
trace name no model for it), a recording (`--agent recorded --from <run file>`: the responses, the
ticks' times and the clock's readings replayed, every measurement recomputed in-process and held to
the recording: the ring's code-derived fields exactly, measurements within 2.7.1's tolerances, the
fields left out listed in the run file under `excluded`) or scripted (`--agent scripted`, the
default: a `look_again` at 800 m, the news, then `real` exactly when the station count moved). The
news the agent may read is the sequence's own (sydney-c1's is constructed and says so), never a live
search. A recorded replay is refused when a pinned ETag or the network file moved, or when a
recorded response calls a tool the profile no longer offers (naming the tick and the step); an
in-process worker under the real model's deadline is refused without `--cache` off an EC2 instance
in us-west-2 (its `look_again` would re-read the scene inside the agent's 85 s; when the instance is
what allowed it, the run file's `environment.host` says so). `--trace` renders the ticks' decisions
to `docs/hackathon/evidence/replay/<sequence>.md` through `vision_decisions.trace` and the
operator's assistant's own table; `--limit N` replays the first N ticks; each tick's figure is
written beside the run file (`--no-figures` to skip).
`scripts/tests/fixtures/replay-synthetic-city-scripted.json` is a scripted recording CI replays with
`--agent recorded`.

**`failures`** (2.5) is the constructed failure catalogue: fourteen cases, each with its input, what
must happen and the tests that hold it, named by pytest node id and checked to be collected
(`--no-collect` skips that; without pytest and moto, `lambdas/requirements-dev.txt`, it refuses
rather than call the tests missing). The rows that read nothing (an asset off the allowlist; a cool
request to a stock worker and a cool worker without its pin) run anywhere; with `--cache` the
real-data rows run through the worker handler in this process, and the cloudy row also runs the real
adapter for one tick on a scene the search does pick under the floor. `--only CASE` picks rows.

**`access-baseline`** (E5) measures one scene in this process with the worker's own read and
analysis, then compares the production distance transform (`DIST_L2`, the 5x5 mask, as
`vision/access.py` runs it) with `DIST_MASK_PRECISE` and with a straight-line buffer (the union
of `cv2.circle` discs of the reach at each station, SDG 11.2.1's simple method): each method's
served share, its difference from production's, the IoU of the served masks and the median time
of five runs; it checks production's share is the worker's own.

**`index`** (2.9) reads every run file in `docs/hackathon/evidence/runs/` (a malformed one, or one
of an unknown kind, is refused by name) and writes `index.json` and `RESULTS.md` (one table per
kind, newest first; a run listed in `older-code.json` is marked "measured on older code" with its
reason). `--check` fails when a run file was added without re-indexing, and checks the operator's
labels (`live/*/labels.csv`) when there are any. `scripts/tests/test_vision_eval.py` runs it in CI,
on every pull request that touches `docs/hackathon/`, `scripts/` or `lambdas/`; run it before
committing run files too.

All four run on Windows (`python scripts\vision_eval.py ...` in the activated venv: `py -3.11`
would run the global 3.11, without the venv's packages). The driver they share,
`scripts/vision_eval_driver.py`, documents in its docstring the interface the agent evaluation,
the counterfactuals and the stability run build on.

## The agent's evaluation: `vision_eval.py agent`, `counterfactual`, `stability`

Built on the same driver, each writes one run file (`--out DIR`, `--tag`) and runs in CI with
`--synthetic` (the driver's synthetic city, the worker in this process, the model scripted, no
network); on real data they are the operator's runs (the runsheet's
[evaluation runs](../docs/evaluation-runsheet.md#the-evaluations-real-data-runs), steps 6 to 8,
bash and PowerShell).

```bash
python scripts/vision_eval.py agent --worker <ARN> --agent bedrock --model-id <id> --region ap-southeast-2 --repeats 5 --out docs/hackathon/evidence/runs
python scripts/vision_eval.py agent --synthetic --repeats 2              # CI's: every case, scripted
python scripts/vision_eval.py counterfactual --worker <ARN> --cache ~/cogs --agent bedrock --model-id <id> --out docs/hackathon/evidence/runs
python scripts/vision_eval.py stability --cache ~/cogs --out docs/hackathon/evidence/runs   # E3; --synthetic in CI
```

**`agent`** (2.6.1-2.6.3) reads the cases in `vision_eval_cases/<case>/case.json` (C1-C10 with
C1c, C1d and C1e: the scenes, the network each was measured with, the thresholds, the news, what
is expected and why, which input moved; `--case NAME`, repeatable, or `--cases A,B` picks some)
and builds each through the real `RailAccessAdapter`: its history ticks with triage off (each
scene measured once a run, however many cases share it), then its newest tick `--repeats` times
(5) from that same state, with the real `vision_triage.triage`. Fixture A is `--network FILE` (default
`vision_benchmark_networks/sydney.json`); every other network is made from it by
`vision_eval_networks.py` (a line on the baseline's largest desert, the replay's own; a three-station
extension toward the deserts; a line removed; an infill station; three stations mapped twice).
The model is Bedrock (`--agent bedrock --model-id <id> [--region R]`) or each case's script
(`--agent scripted`, the default; `--model-id` without `--agent bedrock` is refused, never dropped
unsaid); C7 and C9 always run their scripts, C8 is asked with an invalid model id, C6's
`look_again` is refused with `unreadable_scene`, C7's clock is past the deadline.
The worker is the deployed one (`--worker <ARN>`, site ids `eval-sydney`) or this process
(`--worker in-process`), refused without `--cache` off an EC2 instance in us-west-2 (naming
`--cache`). The scores (`vision_eval_scoring.py`) are per class: recall with its Wilson interval,
balanced accuracy, the confusion matrix with failed-closed runs in their own column, against
numeric-only and always-artefact; stability over repeats, fail-closed, tool use, latency, tokens
and cost in USD and AUD (the built-in price table). Every Converse response is kept in the run file
in the driver's recorded form. It exits 1 when an unconditional case never reached the agent, a
failure case did not fail closed as it must, or the worker did not measure a case's newest scene
(that repeat keeps the worker's code, is scored nowhere, and the next repeat runs).

**`counterfactual`** (2.6.6) takes C1 and C10 (C4 in C10's place when C10 does not pass the gate).
Arm (a): the newest tick's repeats twice over, `look_again` answered with the newest scene's own
re-measurement (`measured`) or the same with the moved measures at the baseline median
(`baseline`), paired by repeat; the scripted model re-measures and answers by what came back.
Arm (b): the same scene in this process with SCL clouded inside the site to coverage 0.62 and 0.58
(`--cache` needed off us-west-2, or `--arms a`); its checks hold that both sides were measured at
their coverage (only such a pair is counted) and that no model was asked below the floor. When C4
stands in, arm (b) runs for C4 alone.

**`stability`** (E3) runs the manifest's `--set` (`year-2024`) in time order through the production
rule, triage off, `history_size` 6, the default thresholds, on one network, in this process by
default (`--worker in-process`; it keeps no deadline, so an uncached read is only slow) so the
worker's built-up masks are compared scene to scene and with the first; `--worker <ARN>` leaves the
IoU unmeasured. `--synthetic --scenes N` runs N synthetic scenes.

All three run on Windows (`python scripts\vision_eval.py ...` in the activated venv: `py -3.11`
would run the global 3.11, without the venv's packages). `vision_eval.py` builds every
subcommand's arguments on each run, so these three keep theirs in `vision_eval_agent_cli.py`, which
does not import the driver (the driver imports the adapter, and so boto3). That is why `pin`
(without `--list-year`) and `index` still run in a venv without boto3. `vision_eval_agent.py`,
which does import the driver, is imported only to run one of the three.

## The reference check: `vision_eval.py builtup-check` (E8)

```bash
python scripts/vision_eval.py builtup-check --cache ~/cogs --out docs/hackathon/evidence/runs
python scripts/vision_eval.py builtup-check --scene synthetic-1 --out DIR     # CI's: synthetic inputs
```

**`builtup-check`** (E8, `scripts/vision_eval_reference.py`) asks how far the worker's built-up
proxy (NDBI and NDVI from Sentinel-2) agrees with an independent map: ESA WorldCover 2021 v200, class
50 "Built-up" (10 m, CC BY 4.0, the `esa-worldcover` bucket in eu-central-1). It reads
`sydney-2024-08-26` (`--scene`) and analyses it in this process as the worker does, reads the
WorldCover tile under the site (`S36E150` for Sydney) over the site's box with the repository's own
COG reader, and resamples it onto the scene's 20 m UTM grid by nearest neighbour (`cv2.remap`).
Over the pixels both saw it reports `iou`, `precision`, `recall`, and the served share over each
mask with the same stations and reach (`served_share_ours`, `served_share_reference`; the network
fixture gives the stations, `--network FILE` otherwise), with each WorldCover class's share of our
built-up pixels. The tile's ETag and size are pinned (`REFERENCE_PINS`): a HEAD first, and a moved
object is refused ("input changed"); only the bucket's map folder is ever read, in this script, never
through the worker. `--cache` keeps the reference's ranges beside the scene's. The run file's kind is
`eval-builtup-check`. On Windows: `python scripts\vision_eval.py builtup-check --cache $HOME\cogs
--out docs\hackathon\evidence\runs` in the activated venv.

## The deployed system's evidence: `evidence_export.py`

```bash
python scripts/evidence_export.py export --env dev --networks --figures 12    # live/<UTC date>/
python scripts/evidence_export.py trace --topic rail-cities --site sydney --scene latest --from docs/hackathon/evidence/live/<date>
python scripts/evidence_export.py trace --env dev --topic rail-sydney-demo --scene latest   # live, writes nothing
python scripts/evidence_export.py agreement --from docs/hackathon/evidence/live/<date>     # after labels.csv
python scripts/pii_denylist_check.py --files <path> ...    # what the export runs on every file it writes
```

On Windows, in the activated venv:

```powershell
python scripts\evidence_export.py export --env dev --networks --figures 12
python scripts\evidence_export.py trace --topic rail-cities --site sydney --scene latest --from docs\hackathon\evidence\live\<date>
python scripts\evidence_export.py trace --env dev --topic rail-sydney-demo --scene latest
python scripts\evidence_export.py agreement --from docs\hackathon\evidence\live\<date>
python scripts\pii_denylist_check.py --files <path> ...
```

**`export`** reads, read-only, what the deployed system measured, decided and did into
`docs/hackathon/evidence/live/<UTC date>/` (the [evidence README](../docs/hackathon/evidence/README.md#the-live-evidence-live)
lists the files and their fields): each vision topic's running state and its decision ring; the
research tick's, the vision worker's (us-west-2) and the admin API's evidence lines and Lambda's
REPORT lines through Logs Insights; the Articles and Findings tables; the three functions' code
identity and the deploy that produced it (`gh run list`, null with the reason when `gh` is missing
or signed out). Topics are every topic whose adapter keeps decision evidence (`--topics a,b`
otherwise), the demo topics (the tracker's `Demo topics:` line) counted apart; the window is the
logs' 90 days (`--since ISO` otherwise). The queries are the operator's assistant's own
(`ops_mcp/vision_evidence.py`), a few at a time, each batch waited for at most `--logs-wait` (120 s);
one cut at 1,000 rows is split by time and asked again, and one that did not finish is said as not
checked in `summary.json`, never as zero. Only schema fields are written; `--with-notes` adds the
agent's reasons, the news hosts and the Finding summaries, through `redact.scrub`, labelled as
unverified. A backstop replaces any ARN and any 12-digit number left, beside a hyphen too, but not
one inside a longer hex token or a UUID, nor a code-made 12-hex `run`, `build` or `reason_sha256`
(`summary.json`'s `redacted` says how many, 0 expected, and `redacted_at` where), and
`pii_denylist_check.py --files` reads every file written. Its list is `PII_DENYLIST` if that is
set, else your `.pii-denylist` at the repository's root. Git ignores that file, so a fresh checkout
has none: put your list there before you export. With no list, or an empty one, the export is
refused only at the end, after the whole read, and moved aside as if a term had been found. To
write it unchecked anyway, pass `--no-denylist` (`agreement` takes it too); the command then prints
that the files were not checked. A hit exits 2, naming the file and line,
puts back every network file it wrote and moves the export to `live/<date>.refused/`, which git
ignores (the operator's `labels.csv` stays). `--networks` also writes each city's
`scripts/vision_benchmark_networks/<site>.json` from its network object, in the fixture's shape: a
file that already holds the same network is left byte for byte (run files pin its sha256), and one
that holds another is kept unless `--replace-networks`. `--figures N` copies at most N figures (the
triaged decisions' first, whatever the agent's verdict, then the reported ones). An export already in
the directory is refused without `--replace`, which removes only the export's own files (never
`labels.csv`); a directory that holds anything else is refused either way, and `live/README.md` is
written only beside the exports in `live/`. Resource names come from
`--prefix` (`bloggerbear`) and `--env`; the Regions are Terraform's defaults unless given
(`--region`, `--vision-region`).

**`trace`** prints one decision's chain (`vision_decisions.trace`, as the assistant's scene view
renders it) as Markdown, from an export (`--from`) or live (`--env`): `--scene` is a scene id or
`latest`, and `--site` is needed when the topic has several sites and the scene does not say which
(the rotating `rail-cities`). **`agreement`** recounts `summary.json`'s agreement once the
operator has filled `labels.csv` in (O9). The runsheet's
[evidence export](../docs/evaluation-runsheet.md#the-evidence-export-the-trace-and-the-labels-o9)
gives every command in bash and PowerShell, and what the credentials must allow.

## After changing the CLI: `generate_cli_reference.py`

The operator's assistant answers "how do I ...?" by showing a command's own `--help` and, when you
give it the values, the exact command (`lambdas/ops_mcp/cli_guide.py`). It runs in a Lambda that
does not contain this folder, so it reads a generated copy of the CLI's reference:
`lambdas/ops_mcp/cli_reference.json`, every command, flag and help text, written from
`admin_cli.build_parser()`.

**Run this whenever you add, rename or remove a command or flag, or change any help text, and commit
the result with the change:**

```bash
python scripts/generate_cli_reference.py          # rewrite lambdas/ops_mcp/cli_reference.json
python scripts/generate_cli_reference.py --check  # change nothing; exit 1 if it is out of date
```

`scripts/tests/test_generate_cli_reference.py` makes the same check, so CI fails with this command
in the message if you forget. Two things the generator decides, both read from the parser:

- **Which commands the assistant never fills in.** A command whose last word deletes or takes
  something down (`delete`, `unpublish`, `reject`, and a few more in `DESTRUCTIVE_VERBS`) and any
  `--force` flag are marked destructive; the assistant shows those as a template with
  `<placeholders>`. A new command of that kind under another name needs its word added to
  `DESTRUCTIVE_VERBS`; the test lists the marked commands so the change is seen in review.
- **The help text**, from argparse's `format_help()` at a fixed width. argparse's layout differs a
  little between Python releases, so the file records which release wrote it: the check compares
  the help exactly on that release, and by content (every flag and help string) on another.

The assistant's hand-written guides (costs, gear, editorial goals, vision topics, findings, a first
topic, security, setting a topic up, reviewing, musings and model prices) are in `cli_guide.py`;
a test builds every command they mention with the real parser, so a guide that names a flag you
removed fails too.

### Testing the DLQ consumer manually

`dlq_handler.py` is only exercised for real when a `daily_cycle` execution
actually fails both of its Step Functions retries -- to verify it works
without waiting for (or forcing) a real failure, send it a synthetic
message shaped like what the state machine's `Catch` state produces:

```bash
QUEUE_URL=$(terraform -chdir=infra/environments/dev output -raw pipeline_dlq_url)
aws sqs send-message \
  --queue-url "$QUEUE_URL" \
  --message-body '{"topic_id": "test-topic", "execution": {"id": "synthetic"}, "error": {"Error": "States.TaskFailed", "Cause": "synthetic test message"}}'
```

Then confirm it landed:

```bash
python scripts/admin_cli.py failed-executions list
```

and check `aws logs tail /aws/lambda/bloggerbear-dev-dlq-handler --since 5m`
for the `dlq_handler: daily_cycle failed for topic_id=test-topic: ...` log
line. Without Terraform, get the queue URL with
`aws sqs get-queue-url --queue-name bloggerbear-dev-pipeline-dlq`. (Both names start with
your `UNIQUE_NAME_PREFIX`; `bloggerbear` is the default.)

Responses are pretty-printed JSON on stdout. A non-2xx response prints the
error body to stderr and exits non-zero.

`topics trigger`'s target Lambda invocation is fire-and-forget (async), so
by default the command polls afterward and only returns once it sees actual
new output -- a new Finding for `research_tick`, a new candidate idea for
`daily_cycle` -- or times out (30s / 90s respectively) and says so. Pass
`--no-wait` to skip this and get the old immediate-return behavior back.
This also means running `daily_cycle` right after `research_tick` for the
same topic is safe without a manual delay in between, for a tick that ends
within those 30s; running it beforehand still isn't -- `daily_cycle` needs a
Finding to already exist. A vision topic's tick (`satellite_vision`,
`rail_access`) that measures a new scene, the one that can write a Finding,
can run well past 30s: the scene search, a network fetch, up to 58s of the
vision worker, the triage agent and the summary, inside the function's 120s.
For those, "No new Finding after 30s" is expected and does not mean the
tick failed: wait about two minutes, or until `topics findings <id>` shows
the new Finding, before `daily_cycle`. A daily cycle started while the tick
still runs drafts without that Finding, and if it writes an article from
other Findings, no later window holds it
([docs/vision-runsheet.md, How the cadence works](../docs/vision-runsheet.md#how-the-cadence-works),
"One Finding can still be missed").

## Before submitting to the competition: `submission_check.py`

The OpenCV AI Competition 2026's pre-submit check
([the Devpost checklist](../docs/hackathon/devpost-checklist.md#the-pre-submit-check)), offline:
it reads the checkout and asks local git, and fetches nothing.

```bash
python scripts/submission_check.py --video-seconds 287             # at the tag, opencv-2026-submission
python scripts/submission_check.py --ref HEAD --video-seconds 287  # before the tag exists
```

PowerShell: `py -3.11 scripts\submission_check.py --video-seconds 287`. Five checks, each listing
every failure: no placeholder (`[R2: ...]`, any bracketed text that is not a link, or `<R2: ...>`
inside code), unclosed code fence or stray backtick left in the report, the reproduction guide, the
dependency audit, the Devpost text or the checklist; every repository-relative
link in them resolves (external ones are listed to open by
hand); no `missing` row in the rubric tracker but O7, O10 and O11 (the form itself, the optional
production release, keeping the site up) and any passed with `--accept ID`, all listed; every run
file under `docs/hackathon/evidence/runs/` measured on vision code equal to the checked commit's,
from a clean checkout, or listed in `docs/hackathon/evidence/RESULTS.md` as "measured on older
code" with its reason; and
the video's length, entered by hand, at most 300 seconds. Exit 0 when all pass, 1 otherwise.
