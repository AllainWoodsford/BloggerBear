# Production runsheet

Everything between "dev works" and "your domain serves the site", in order, with the exact commands.
Each step says who does it and how to tell it worked. Run the steps top to bottom; tick them off as
you go.

It is written for anyone running their own copy. Where the original deployment's values appear
(`bloggerbear.com`, GoDaddy as the registrar), they are examples: use your own domain and whichever
registrar you bought it from. If you have not set up dev yet, do
[deployment-runsheet.md](deployment-runsheet.md) first; this picks up where it stops.

Wherever it says **check it**, `python scripts/domain_check.py` is the read-only tool that tells you where
the domain stands. It reads the domain from production's `terraform.tfvars` (or pass one:
`python scripts/domain_check.py example.com`), needs no AWS login and changes nothing.

## Contents

- [Before you start](#before-you-start)
- [1. Create the DNS zone and the budget alarm (you, locally, once)](#1-create-the-dns-zone-and-the-budget-alarm-you-locally-once)
- [2. Point your registrar at Route 53 (you, at the registrar)](#2-point-your-registrar-at-route-53-you-at-the-registrar)
- [3. Wait for the change to spread, then check it](#3-wait-for-the-change-to-spread-then-check-it)
- [4. Put the domain and zone ID in production (a small PR)](#4-put-the-domain-and-zone-id-in-production-a-small-pr)
- [5. GitHub setup for production (you)](#5-github-setup-for-production-you)
- [6. Release to production (you approve, CI applies)](#6-release-to-production-you-approve-ci-applies)
- [7. After the first apply](#7-after-the-first-apply)
- [The reviewer bear](#the-reviewer-bear)
- [The web search gateway's region](#the-web-search-gateways-region)
- [Vision (optional)](#vision-optional)
- [The queue platform, prompts and evals](#the-queue-platform-prompts-and-evals)
  - [The enable order](#the-enable-order)
  - [The bootstrap: the publisher roles and the evals log group](#the-bootstrap-the-publisher-roles-and-the-evals-log-group)
  - [The `production-prompts` environment](#the-production-prompts-environment)
  - [Moving topics to the queue, and back](#moving-topics-to-the-queue-and-back)
  - [Prompts: publishing and rolling back](#prompts-publishing-and-rolling-back)
  - [Failure-day recipes](#failure-day-recipes)
- [Rolling back](#rolling-back)
- [What it costs (list prices, roughly)](#what-it-costs-list-prices-roughly)
- [Not done, on purpose](#not-done-on-purpose)

## Before you start

| You need | How to tell |
|---|---|
| Dev deployed and working | The `terraform` workflow's last run on `dev` is green, and the dev site loads. |
| Bootstrap applied (state bucket, GitHub OIDC provider, both deploy roles, both prompts publisher roles) | `terraform -chdir=infra/bootstrap output` lists `dev_deploy_role_arn`, `prod_deploy_role_arn`, `dev_prompts_role_arn` and `prod_prompts_role_arn`. A bootstrap without the last two predates the prompts and evals work: [apply it again](#the-bootstrap-the-publisher-roles-and-the-evals-log-group) before the first release. |
| A domain you control, at any registrar | You can sign in to the registrar and change the domain's name servers. |
| The GitHub settings for production | `python scripts/setup_repo.py --dry-run` shows what is set and what is missing. |
| A `prod` branch and a `production` GitHub environment | Step 5 creates both if they are missing. |
| The model available to your account in your region | The `converse` test in [deployment-runsheet.md](deployment-runsheet.md#before-you-start) answers. |
| DynamoDB tag-based access control on, in the production account and region | DynamoDB console > Settings. The assistant's `table_sample` tool needs it: [why](deployment-runsheet.md#dynamodb-tag-based-access-control-abac). |

Two things about the account that are worth knowing before the first release:

- **Lambda concurrency.** A new AWS account often has a quota of only 10 concurrent Lambda executions
  for the whole account, shared by every function in dev and production. The site works within it,
  but nothing can be reserved for one function (Lambda refuses: friction log 10.17), and a burst
  can throttle the pipeline. Ask for more under Service Quotas > AWS Lambda > "Concurrent executions".
- **One account or two.** Dev and production can share an account or have one each. Two is
  recommended if you have them; the original deployment shares one.
  [deployment-runsheet.md](deployment-runsheet.md#3-one-account-or-two) says what differs, and
  [deployment-separate-accounts.md](deployment-separate-accounts.md) is the guide for two.

## 1. Create the DNS zone and the budget alarm (you, locally, once)

Bootstrap is always applied by hand, never by CI. If you gave it your domain and an alert email the
first time, this step is already done: `terraform -chdir=infra/bootstrap output hosted_zone_id`
prints a zone ID. Otherwise, run the `plan` and the `apply` below.

Add the same other variables as your last bootstrap apply to both commands: `aws_account_id`,
`github_repo`, `state_bucket_name`, `unique_name_prefix` and `aws_region`, and `vision_region`,
`web_search_regions` and `separate_environment_permissions` if you set them.

The budget's monthly limit is `bedrock_budget_limit_usd`, US$20 unless you pass another value. The
budget emails you when actual Bedrock spend passes 80% of it, and again when the month's forecast
passes 100% ([Bootstrap variables](configuration.md#bootstrap-variables)).

```bash
cd infra/bootstrap
terraform init
terraform plan  -var domain_name=example.com -var budget_alert_email=you@example.com
```

**Check the plan only adds the zone and the budget.** If it shows anything else being added,
changed or destroyed, stop and find out why. Then:

```bash
terraform apply -var domain_name=example.com -var budget_alert_email=you@example.com
terraform output hosted_zone_id
terraform output hosted_zone_name_servers
```

Keep the four name servers (they look like `ns-123.awsdns-45.org`) for the next step.

The zone lives in bootstrap, not in production, on purpose: its four name servers are what you give
your registrar, and they must not change if production is ever destroyed and rebuilt. The zone has
`prevent_destroy`.

## 2. Point your registrar at Route 53 (you, at the registrar)

Why Route 53 and not the registrar's own DNS: the site is served from CloudFront on the bare domain,
and DNS does not allow a bare domain to be an alias of another name. Route 53 can do it; most
registrars' DNS cannot. Your registrar stays the registrar (you still renew the domain there); only
who answers DNS questions changes.

**Before you change anything**

- **This replaces whatever the domain shows now.** If that is only a parked page, fine. If you pay
  the registrar for a website or email plan on this domain, decide about it first.
- Look through the registrar's DNS records for anything you use (MX for email, TXT for
  verification). Moving the name servers drops all of them. Recreate what you need in the Route 53
  zone afterwards.
- **If DNSSEC is switched on at the registrar, switch it off first.** Leaving it on while changing
  name servers makes the domain unreachable for many resolvers.

**Then** find the domain's name server setting and replace the registrar's own with the four from
step 1, with no trailing dots. Every registrar calls it something like "custom name servers" or
"use my own name servers".

> **Example: GoDaddy.** My Products > the domain > **DNS** > **Nameservers** > **Change
> Nameservers** > **I'll use my own nameservers**, paste the four, save. The screens move around
> over time.
>
> Other registrars (Namecheap, Cloudflare Registrar, Squarespace Domains, Porkbun, and so on) have
> the same setting under the domain's DNS or name server page. If the domain is registered with
> Route 53 itself, set the name servers under Route 53 > Registered domains.

## 3. Wait for the change to spread, then check it

It is usually minutes; allow up to a few hours (rarely up to 48). Nothing is lost while you wait.

```bash
python scripts/domain_check.py --expect-ns <the four from step 1>
```

Re-run it until the first line says `[ OK ] Name servers`. Everything after that is skipped until it
does, on purpose: until then whatever answers on the domain is still the registrar's, and judging
it would only confuse.

**Do not run the production release until this is OK.** The certificate is proved by a DNS record,
and it can only be seen once the name servers have moved. If you run it too early Terraform waits on
the certificate and then fails; that is safe to re-run, just slow.

## 4. Put the domain and zone ID in production (a small PR)

In `infra/environments/production/terraform.tfvars`:

```hcl
domain_name    = "example.com"
hosted_zone_id = "Z0123456789ABC"   # from: terraform -chdir=infra/bootstrap output hosted_zone_id
```

Neither value is sensitive, so it is fine in git. A fork must change both: the file ships with the
original site's values. Open a PR into `dev` and merge it. (Merging to `dev` only applies dev, which
has no custom domain, so nothing changes there.)

`www.<your domain>` is set up too and redirects to the bare domain. If the zone ID is missing, the
plan stops with a plain message instead of an AWS error half-way through.

## 5. GitHub setup for production (you)

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)   # check this is YOUR repository

# the prod branch the release workflow requires (from the merged dev tip)
git fetch origin && git branch prod origin/dev && git push -u origin prod

# the production environment, with you as the required approver
echo "{\"reviewers\":[{\"type\":\"User\",\"id\":$(gh api user --jq .id)}]}" |
  gh api -X PUT "repos/$REPO/environments/production" --input -
```

Then the settings. The setup script asks for each one, checks it, and puts production's on the
`production` environment:

```bash
python scripts/setup_repo.py --dry-run     # asks and checks, sets nothing
python scripts/setup_repo.py
```

For production it sets:

- `AWS_PROD_DEPLOY_ROLE_ARN` (required).
- `TF_STATE_BUCKET_PROD` (required for a fork). It is your state bucket: the same one as dev's if
  you use one account.
- `AWS_PROD_ACCOUNT_ID` and `ALERT_EMAIL_PROD`, if you give them.
- `AWS_PROD_PROMPTS_ROLE_ARN`, if you give it: the prompts publisher's role, needed only once
  prompts as assets are on. This one goes at repository level, not on `production`, because
  production's prompts are also published under a second environment,
  [`production-prompts`](#the-production-prompts-environment).

The full table, and why each is a secret or a variable, is in
[configuration.md](configuration.md#github-secrets-and-variables). Your IP is not among them. You
add it in [step 7](#7-after-the-first-apply), after the first release.

It does not ask where the web search gateway runs: unset, production's is in `ap-northeast-1`
(Tokyo), like dev's. To choose another Region, see
[The web search gateway's region](#the-web-search-gateways-region) below.

Also worth doing while you are in Settings:

- Protect `dev` and `prod` (require a pull request, block force-pushes and deletions). On a
  private repository this needs a paid GitHub plan; the AWS trust policy still gates production
  either way. The commands are in the archived [public-repo-runsheet.md](enhancements/archive/public-repo-runsheet.md).
- Check no credential is stored as a repository **variable**. Variables are shown in plain text and
  are not masked in logs; a credential should only ever be a secret.

## 6. Release to production (you approve, CI applies)

```bash
gh pr create --base prod --head dev --title "Release v0.1.0" --body "First production release"
# merge it, then:
gh release create v0.1.0 --target prod --title v0.1.0 --generate-notes
```

Publishing the release starts `terraform-production-release`. It runs the security scans, lint and
tests on the tag, waits for your approval on the `production` environment, confirms the tagged
commit is on `prod`, then applies. Watch it in the Actions tab. Later releases are the same two
commands with the next version number.

Once the repository's `PROMPT_AS_ASSET_ENABLED` variable is `true`, a release also starts the
`prompts` workflow's `publish-production` job in the `production` environment, which waits for an
approval of its own: every release then needs two, so prompts and code never drift
([Prompts: publishing and rolling back](#prompts-publishing-and-rolling-back)).

Expect 15 to 25 minutes on the first run: a few hundred resources, the certificate has to validate,
and a new CloudFront distribution takes 5 to 15 minutes to deploy. If it stops on the certificate,
see step 3 and re-run the workflow.

**If an apply fails part-way**, read the error, fix the cause in a pull request to `dev`, and
release again. Terraform picks up where it stopped. Some things are only checked by AWS at apply
time, so a release can fail on something dev never exercised: a resource only production creates,
a name AWS refuses, a quota. The friction log (`docs/friction.md`, entries 2.9, 10.16 and 10.17)
has the ones this project has met.

## 7. After the first apply

```bash
terraform -chdir=infra/environments/production output          # the URLs
python scripts/domain_check.py --expect-ns <the four from step 1>   # every line should say OK
```

Then, in order:

1. **Confirm the alert email.** AWS sends one confirmation mail to the `ALERT_EMAIL_PROD` address,
   for the alarms' SNS topic; nothing is delivered until you click it. The budget from step 1 mails
   its address directly and needs no confirmation. What each alarm watches, and what it emails
   you, is in [observability.md](observability.md).
2. **Let yourself in to the admin API.** Nothing can call it until you do
   ([configuration.md](configuration.md#the-admin-apis-address-allowlist)):
   ```bash
   python scripts/admin_allowlist.py --prefix <your prefix> set --env production --my-ip
   ```
   If your region is not `ap-southeast-2`, also put `--region <your region>` before `set`.
3. **Point the CLI at production** (each environment has its own URL; see `scripts/QUICKSTART.md`):
   ```bash
   export BLOGGERBEAR_ADMIN_API_URL=$(terraform -chdir=infra/environments/production output -raw admin_api_url)
   python scripts/admin_cli.py topics list
   ```
4. **Seed the topics** you want
   ([deployment-runsheet.md, step 5](deployment-runsheet.md#5-after-the-first-deploy)). Creating a
   topic creates its schedules; after that it runs unattended. Trigger one by hand first:
   `topics trigger <id> --pipeline research_tick`, then `daily_cycle`.
5. **Optional, but recommended: the API keys and the model registry.** Each is per environment,
   so what you set up for dev is not here yet. Store production's CoinGecko key and GitHub token
   (`/<prefix>/production/coingecko-api-key`, `/<prefix>/production/github-api-token`) and
   seed production's model tables (`<prefix>-production-models`,
   `<prefix>-production-model-config`) the same way as dev's (`<prefix>` is your
   `UNIQUE_NAME_PREFIX`, `bloggerbear` by default):
   [deployment-runsheet.md, "Optional, but recommended"](deployment-runsheet.md#optional-but-recommended).

   The model catalog fills and reprices production's registry by itself from its first run. It is
   on by default and runs every second day: discover at 02:00 Sydney time, reprice at 03:00. To
   run it now instead of waiting, in this order
   ([models.md, the model catalog](models.md#the-model-catalog-discovery-prices-and-the-weekly-cycle)):
   - `models discover` adds models now, disabled and unpriced. It takes a minute or two.
   - `models reprice` prices them. Run it once discover has finished.
   - `model-catalog get` then shows what each job last did.
6. **Smoke-test in a browser**: the home page, an article, the Stats page, `/rss.xml`, a thumbs-up
   on an article (this exercises the feedback token), and `https://www.<your domain>` (should land
   on the bare domain).
7. **Optional:** production creates the shared CloudFront web ACL. In a one-account setup, put
   `terraform output wafv2_web_acl_arn` into `infra/environments/dev/terraform.tfvars` as
   `web_acl_arn` if you want dev to share it. It cannot be shared across accounts.

8. **The operator's assistant** (`/ask.html`; [how it works](architecture/ops-mcp-agent.md)). Its
   user pool has self-sign-up off and MFA required, so make your own user by hand:
   ```bash
   POOL=$(terraform -chdir=infra/environments/production output -raw ops_user_pool_id)
   aws cognito-idp admin-create-user --user-pool-id "$POOL" --username <you> \
     --user-attributes Name=email,Value=<your email> --message-action SUPPRESS
   aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username <you> \
     --password '<a long password>' --permanent
   ```
   Open `https://<your domain>/ask.html`, sign in, and set up the authenticator app it asks for.
   Press **Test voice**, then **What needs my attention?**. "What's happening with the firewall?"
   as a follow-up is production's deep dive. "Any errors in the logs?" and "any API failures?" read
   production's logs (and shared ones) and answer with root causes and the API calls by status code; if they say AWS refused, see
   [CloudWatch Logs tags](deployment-runsheet.md#cloudwatch-logs-tags-the-assistants-log-tools).
   Check `pipeline-config get` shows `assistant_access` as `open` (or absent), unless you mean to
   lock it to your addresses. Five failed sign-ins in fifteen minutes lock a user and email the
   alert address; `sign-ins unlock <you>` clears it
   ([deployment-runsheet.md, step 5](deployment-runsheet.md#5-after-the-first-deploy)).
9. **Optional: Alexa+.** Production's own add-on, linked to production's pool only, is a one-time
   bootstrap: [alexa/README.md](../alexa/README.md). Putting Alexa's redirect URLs in
   `ops_alexa_redirect_uris` also keeps the MCP function warm (about 8,600 invocations a month).
10. **The queue platform, prompts and evals.** Deployed with everything else, and in shadow or off
    until you switch them on: [the enable order](#the-enable-order).

## The reviewer bear

Production gets Professor Grizzlewick too, unless the `production` environment's
`REVIEWER_ENABLED` variable is `false`. **His first run after the release decides every article
that has waited two days or more**: clear what you want to decide yourself first, or turn approvals
off for the release (`python scripts/admin_cli.py reviewer set --may-approve off` against production,
which needs no release, or `topics update <id> --reviewer-may-approve off`). The switches and what to do with his financial
approvals are in [the deployment runsheet](deployment-runsheet.md#the-reviewer-bear-on-by-default).

## The web search gateway's region

Production's AgentCore web search gateway is in `ap-northeast-1` (Tokyo) unless you set
otherwise; AWS offers the Web Search Tool only there, in `us-east-1` and in `eu-west-1`. To move
production's alone:

1. Re-apply the bootstrap by hand with the new Region added to `web_search_regions`
   (`-var='web_search_regions=["ap-northeast-1","us-east-1"]'`, plus your usual variables), in
   production's account if it has its own. Keep `ap-northeast-1` in the list while dev, or the
   gateway being replaced, is still there.
2. Set `WEB_SEARCH_REGION_PROD` (on the `production` environment or the repository) to the new
   Region. Production reads `WEB_SEARCH_REGION_PROD`, then `WEB_SEARCH_REGION` (the `production`
   environment's, else the repository's), then `ap-northeast-1`, so a shared `WEB_SEARCH_REGION`
   set on the repository for dev moves production too at its next release.
3. Publish a release. The apply replaces the gateway in the new Region and points the Lambdas at
   it; it holds no data. An `AccessDenied` on `bedrock-agentcore` means step 1 is missing.

To move back, remove the variable (or set it to `ap-northeast-1`) and release again. Dev's steps,
and why each is needed, are in
[the deployment runsheet](deployment-runsheet.md#the-web-search-gateways-region).

## Vision (optional)

The vision worker and its topics
([vision-runsheet.md](vision-runsheet.md#turning-the-worker-on)) are off in
production by default. **Leave it off unless the dev run was satisfactory:** a vision topic that
ran on dev for a few ticks, with a held article whose figure and numbers you read and approved,
and no `last_error` repeating in its running state. Off by default holds only while the repository
variable is off: the dev runsheet turns the worker on with the `VISION_ENABLED` repository
variable, and the release job runs in the `production` environment, whose `vars.VISION_ENABLED`
falls back to the repository's value, so the next release would turn it on here too (with two
accounts, until step 1 below is done in the production account, that release's apply is refused
instead). To keep production off until the dev trial is done, set `VISION_ENABLED` to `false` on
the `production` environment before you set the repository variable. Then, when the dev run was
satisfactory, in order:

1. Re-apply the bootstrap by hand with `vision_region` (in the production account, if you have
   two), checking the plan touches only the deploy roles' policies. First check the worker's
   bucket name fits: `<prefix>-production-vision-artifacts-<account id>-<vision_region>` must be
   at most 63 characters, so the prefix and `vision_region` together at most 21 (a prefix of 12
   or fewer beside `us-west-2`; a home-Region `ap-southeast-2` fits only a prefix of 7 or fewer).
   Otherwise the release's apply fails on that bucket
   ([configuration.md](configuration.md#environment-terraform-variables)).
2. Set the `VISION_ENABLED` variable to `true` on the `production` environment and publish a
   release. The worker lands in `vision_region`; in the home region the research tick gets the
   worker's ARN (`VISION_WORKER_ARN`) and the pipeline's shared execution role one policy to
   invoke it. The operator's assistant may read the worker's log in `vision_region` and each topic's
   running state. Nothing else in production changes.
3. Create the topic in production with the same `adapter_config` that worked on dev (for the
   rail cities, one topic over the four cities: created from `scripts/rail_topics.example.json` as in
   [RailVisionSeeding.md §4](RailVisionSeeding.md#4-seeding-runsheet-dev), then switched to a city a
   week with three goals, as in [§7](RailVisionSeeding.md#7-a-city-a-week-written-up-from-three-sides)),
   and trigger its first tick by hand (for the rail topic, one tick per city). A large rail city's
   first tick only fetches its OpenStreetMap network and measures
   nothing (Sydney's takes most of a minute: the runsheet's "A large city's first network can take
   a minute"), so trigger a second tick, or wait for the next hourly one, to measure it. The CLI
   waits 30 s for the Finding and a vision tick that measures a scene can run longer, so "No new
   Finding after 30s" is expected: wait about two minutes after the tick that measures, or until
   `topics findings <id>` shows the Finding, before you trigger its daily cycle.

`force_manual_review` is forced on every vision topic: **every vision article waits for a
person** in the review inbox, whatever the compliance review said, and that holds on every publish
path: the daily cycle holds the article, and approving or force-publishing is a person's act. A vision topic created without `editorial_goals` is drafted under its adapter's
default goal (#298), not the global one. Take it down again by setting
the `production` environment's `VISION_ENABLED` to `false` and releasing (removing it falls back to
the repository's value); topics keep their state.

## The queue platform, prompts and evals

Three parts that every deploy carries and that change nothing until you turn them on: the queue
platform (a dispatcher every minute in place of each topic's two schedules), prompts as assets
(prompts published from `prompts/` and read from S3, with the code's copy as the fallback) and
continuous evals (scored events, the Model Ranch's gates and culling, the judge). What each setting
does is in [configuration.md](configuration.md#the-queue-platform-prompts-and-evals). Every
`admin_cli.py` command below runs against the environment `BLOGGERBEAR_ADMIN_API_URL` points at:
dev's first, then production's ([step 7](#7-after-the-first-apply)).

### The enable order

Dev first, then production, one part at a time. An account whose bootstrap was applied before the
prompts and evals work needs [step 1's bootstrap](#the-bootstrap-the-publisher-roles-and-the-evals-log-group)
before the first deploy that carries them, whatever else is on: every deploy creates the evals log
group.

0. **The queue platform.** From the first deploy that has it, the dispatcher watches every topic in
   shadow mode and dispatches none of them. In dev, after a few days, read each topic's shadow line:
   ```bash
   python scripts/admin_cli.py topics schedule
   ```
   A topic is ready when its line has been clean for a few days in a row:
   `0 mismatches · 0 unchecked days`, some checked days, and a last research turn. Move dev's topics
   one at a time ([how, and back](#moving-topics-to-the-queue-and-back)), and watch each one's next
   article arrive on time. Production after its release: a plain feed topic first (not financial,
   not a vision topic, no rota), then the rest.
1. **The bootstrap and GitHub, once.** [Apply the bootstrap again](#the-bootstrap-the-publisher-roles-and-the-evals-log-group)
   if it predates the prompts and evals work, [create `production-prompts`](#the-production-prompts-environment),
   and set the two publisher secrets at repository level:
   ```bash
   python scripts/setup_repo.py --only AWS_DEV_PROMPTS_ROLE_ARN AWS_PROD_PROMPTS_ROLE_ARN
   ```
2. **Prompts in dev.** Keep production off first, then turn dev on, and deploy dev (or merge
   anything under `infra/`, `lambdas/` or `frontend/`):
   ```bash
   gh variable set PROMPT_AS_ASSET_ENABLED --env production --body false
   gh variable set PROMPT_AS_ASSET_ENABLED --body true
   gh workflow run terraform.yml --ref dev
   ```
   Once that apply is green, publish dev's prompts (the next merge to `dev` that touches `prompts/`
   does it too), with a dry run first:
   ```bash
   gh workflow run prompts.yml --ref dev -f environment=dev -f dry_run=true
   gh workflow run prompts.yml --ref dev -f environment=dev
   ```
   **It worked when** `python scripts/admin_cli.py prompts list` shows every prompt's files with
   their hash approved and nothing paused, `prompts releases` shows the release in place, and the
   `<prefix>-dev-prompt-fallback` alarm stays quiet over the next hour.
3. **Evals in dev.** The same shape:
   ```bash
   gh variable set EVALS_ENABLED --env production --body false
   gh variable set EVALS_ENABLED --body true
   gh workflow run terraform.yml --ref dev
   ```
   **It worked when**, within two hours, `python scripts/admin_cli.py evals status` answers every
   part (none says "could not check") and its rollup has run in the last hour, and the
   `<prefix>-dev-eval-rollup-heartbeat` alarm is OK. `evals set --digest-email on` adds the Friday
   digest email, if you want it.
4. **Production.** Every release from step 2 on already publishes production's prompts to its
   bucket (approve the second run; on the first release that creates the bucket, approve it once
   the deploy has finished, or re-run it), and production's Lambdas still read the code's copies.
   Check what is there with `prompts list` against production. Then remove the `production`
   environment's two values, so it takes the repository's `true`, and publish a release
   ([step 6](#6-release-to-production-you-approve-ci-applies)):
   ```bash
   gh variable delete PROMPT_AS_ASSET_ENABLED --env production
   gh variable delete EVALS_ENABLED --env production
   ```
   Approve the deploy and the prompts publish. Check production as for steps 2 and 3. Culling stays
   proposal-only; to cull, `evals set --cull on`, then `evals cull --apply`. The weekly cycle
   (Fridays, 04:00 Sydney) proposes, only while evals are on and the cycle runs
   (`model-catalog set --cycle on`), and `evals cull` shows the proposal.

### The bootstrap: the publisher roles and the evals log group

Once per account, by hand, like every bootstrap apply. A bootstrap first applied from this code
already has both; one applied before the prompts and evals work has neither. The apply adds:

- the two prompts publisher roles, `gha-<prefix>-dev-prompts` (trusts a run on `dev`) and
  `gha-<prefix>-prod-prompts` (trusts runs in the `production-prompts` and `production`
  environments), each allowed its own environment's prompts bucket and prompt-hashes table and
  nothing else;
- the deploy roles' right to the evals log group, `<prefix>-<env>-evals`. Without it the first
  deploy that carries the evals work stops on `logs:CreateLogGroup`, evals on or off, so in such an
  account apply this before merging that work to `dev`, and before the release that brings it to
  production.

Use the same variables as your last bootstrap apply ([step 1](#1-create-the-dns-zone-and-the-budget-alarm-you-locally-once)
lists them):

```bash
cd infra/bootstrap
terraform plan  -var aws_account_id=... -var github_repo=... ...   # your usual variables
terraform apply -var aws_account_id=... -var github_repo=... ...
terraform output dev_prompts_role_arn
terraform output prod_prompts_role_arn
```

**Check the plan** adds the two roles, their policies (`<prefix>-gha-dev-prompts`,
`<prefix>-gha-prod-prompts`) and attachments, changes the deploy policy (its `EvalsLogGroups`
statement), and destroys nothing. Then set the two outputs as the repository secrets
`AWS_DEV_PROMPTS_ROLE_ARN` and `AWS_PROD_PROMPTS_ROLE_ARN` (step 1 of the enable order). The next
dev deploy and the next release hand them to Terraform, and each prompts bucket then refuses every
write by anyone but its publisher. With two accounts, apply it in each and take each role's ARN
from its own account ([separate AWS accounts](deployment-separate-accounts.md#prompts-evals-and-the-queue-platform)).

### The `production-prompts` environment

The GitHub environment the `prompts` workflow publishes production's prompts in between releases.
Created by hand, once, never by a code change. Its required reviewer is you, and it may only be
deployed from `dev`: a dispatch from any other branch is refused.

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)   # check this is YOUR repository
echo "{\"reviewers\":[{\"type\":\"User\",\"id\":$(gh api user --jq .id)}],\"deployment_branch_policy\":{\"protected_branches\":false,\"custom_branch_policies\":true}}" |
  gh api -X PUT "repos/$REPO/environments/production-prompts" --input -
gh api -X POST "repos/$REPO/environments/production-prompts/deployment-branch-policies" \
  -f name=dev -f type=branch
```

**Check it:** Settings → Environments → `production-prompts` shows you as the required reviewer and
`dev` as the only deployment branch. Put no secret or variable on it: everything the publish reads
(`AWS_PROD_PROMPTS_ROLE_ARN`, `PROMPT_AS_ASSET_ENABLED`, `UNIQUE_NAME_PREFIX`, `PII_DENYLIST`) is at
repository level, because the same job also runs in `production` with each release, and an
environment's own settings reach only its own jobs.

### Moving topics to the queue, and back

```bash
python scripts/admin_cli.py topics schedule                                # each topic's mode and shadow record
python scripts/admin_cli.py topics update <topic_id> --dispatch-mode queue  # asks first; --yes does not
python scripts/admin_cli.py topics update <topic_id> --dispatch-mode schedule   # one topic back
python scripts/admin_cli.py topics reschedule --dispatch-mode schedule --dry-run
python scripts/admin_cli.py topics reschedule --dispatch-mode schedule     # every queue topic back
```

- **The move** deletes the topic's two EventBridge schedules, and the dispatcher runs it from the
  next minute. It is refused, with every reason, while the topic is paused, while shadow mode has
  seen a mismatch or a day it could not check, or not yet a checked day or (for a cron research
  cadence) a research turn, and for a cadence, slug or topic id the dispatcher cannot use
  ([what each means](configuration.md#the-queue-platform)). The shadow counts are never reset: a
  topic with an old mismatch whose cause you have fixed needs `shadow_mismatches` removed from its
  item in the console, once it has had a clean day since.
- **On the day of the move**, if today's daily turn has passed the move records today's daily run
  as done, so the topic writes again tomorrow, not twice today.
- **After it:** a daily cadence with several hours runs once a day; `topics trigger` queues a
  research job or starts a manual daily run (`<slug>-<date>-manual-<HHMMSS>`); pausing works as
  before, but a topic resumed after today's daily turn starts today's run; and a backup restored
  over a queue topic keeps the snapshot's `last_daily_dispatched_on`, which can start a second daily
  run that day. Watch the `work-queue-age` and `dispatcher-heartbeat` alarms.
- **The way back** writes the topic's two schedules again from its row, in its stagger slot. Nothing
  else changes, and it can move to the queue again later (the pre-flight runs again).

### Prompts: publishing and rolling back

Each publish makes the bucket hold exactly the repository's `latest/` folders and is one release
(`<env>-<yyyymmddThhmm>-<short sha>`). Nothing is published while the repository's
`PROMPT_AS_ASSET_ENABLED` is not `true`.

| When | What runs | Approval |
|---|---|---|
| A merge to `dev` that touches `prompts/`, the prompts code or the workflow | `publish-dev` | none |
| A release is published | `publish-production`, in `production`, with the release's own code | yours, beside the deploy's |
| `gh workflow run prompts.yml --ref dev -f environment=production` | `publish-production`, in `production-prompts`, publishing dev's tree; any prompt the latest release cannot read yet is left as it is | yours |
| `gh workflow run prompts.yml --ref dev -f environment=dev` | `publish-dev` | none |

Add `-f dry_run=true` to a dispatch to print the plan and write nothing. `prompts releases` lists
each environment's releases, newest first, and the one in place.

**Rolling a prompt back**, fastest first:

1. **Stop serving it**, with no publish: `prompts pause <prompt_id> <version>` (the prompt's other
   files, or the code's copy, are served instead) or `prompts set --assets off` (every prompt's code
   copy). In force within ten minutes; `prompts resume` and `prompts set --assets on` undo them.
2. **Pin an earlier release:** take its id from `prompts releases`, then
   ```bash
   gh workflow run prompts.yml --ref dev -f environment=production -f release_id=<release id>
   ```
   Every later publish, a merge or a release included, publishes the pinned release again until a
   dispatch with `-f clear_pin=true` publishes the tree as a new release.
3. **Revert it in git:** a pull request to `dev` that restores the file, then a publish (dev on
   merge; production by dispatch or with the next release). A version number is never reused for
   other text: the publish refuses a tree that would.

### Failure-day recipes

One step each, as the CLI spells it (`python scripts/admin_cli.py ...`).

| What happened | The step |
|---|---|
| A bad prompt is live | `prompts pause <prompt_id> <version>`, or every prompt at once, `prompts set --assets off`. In force within ten minutes |
| A model was culled by mistake | `models restore <model_id>` |
| A model is gated and should not be | `evals ungate --model <model_id> --hours 48` |
| The judge ran away | `evals revoke --source judge --since <ISO time>`. Its tripwire has already turned it off; `evals set --judge on` turns it back on and clears `judge_paused_reason` |
| The queue is stuck (the `work-queue-age` or `dispatcher-heartbeat` alarm emails) | `topics reschedule --dispatch-mode schedule` puts every queue topic back on its schedules; `failed-executions list` shows what failed |
| An offline scoring run stopped at the spend cap | It carries on by itself once a new day's cap allows; `evals runs resume <run_id>` sends again whatever has no result, a failed model call's items included, or raise the cap with `evals set --daily-spend-cap <AUD>` |
| The rollup has stopped (`eval-rollup-heartbeat` emails) | `evals rollup` runs it now, from its watermark |

## Rolling back

- **Point the domain back**: at the registrar, restore its default name servers (GoDaddy, for
  example: Nameservers > **Reset to GoDaddy default nameservers**). Spreading takes the same
  minutes-to-hours as before; the Route 53 zone can stay.
- **A bad release**: publish a new release from an earlier commit on `prod`. With prompts as assets
  on, approve its prompts publish too, so the prompts match the code again.
- **The queue platform, prompts or evals**: a topic back to its schedules is `topics update <id>
  --dispatch-mode schedule` (every topic: `topics reschedule --dispatch-mode schedule`); a prompt,
  [Prompts: publishing and rolling back](#prompts-publishing-and-rolling-back). To turn prompts as
  assets or evals off in production, set `PROMPT_AS_ASSET_ENABLED` or `EVALS_ENABLED` to `false` on
  the `production` environment and publish a release (`prompts set --assets off` stops the prompts
  within ten minutes, with no release). Evals off removes eval-jobs and its queue; the evals table and log group
  stay, with every total.
- **Tearing production down** is deliberately awkward: tables have deletion protection (in
  `infra/environments/production/main.tf` set `protect_data = false` on `module "app_data"`,
  `module "prompts"`, `module "evals"` and `module "ops_assistant"`, apply, then destroy) and the
  content bucket is not force-destroyed.
  The DNS zone is in bootstrap with `prevent_destroy`, so it survives, and the registrar's settings
  stay valid when you rebuild.
- **An accidental delete or overwrite**: tables can be restored to any second in the last 35 days
  (DynamoDB point-in-time recovery); an overwritten article body can be recovered from the bucket's
  previous version for 30 days.

## What it costs (list prices, roughly)

- Route 53: US$0.50 a month for the zone, plus a few cents of queries.
- WAF is the biggest fixed cost: production has three web ACLs and eight rules, about US$5 per ACL
  and US$1 per rule a month (roughly US$23). Dev has its own.
- CloudWatch dashboards: the first three in an account are free, then about US$3 a month each.
  Each environment has two: pipeline health (`<prefix>-<env>-pipeline-health`) always, and
  `<prefix>-<env>-ranch-and-prompts` while evals are on. With evals on in both environments of one
  account that is four, so one is paid; two accounts pay for none. Whether CloudWatch counts each of
  pipeline health's per-Lambda searches as one metric, or as one per line it draws, has not been
  checked against a bill yet: if the second, pipeline health is over the free tier's 50 metrics, and
  one account with both environments pays for a second dashboard. Cost Explorer's CloudWatch usage
  says which: `DashboardsUsageHour-Basic` is the free kind, `DashboardsUsageHour` the paid.
- The queue platform, prompts and evals: the dispatcher's 43,000 or so runs a month are inside
  Lambda's free tier; the work queue's messages, the prompts bucket and table, the evals table and
  the hourly rollup's Logs Insights queries cost cents. The judge and offline scoring runs are
  Bedrock calls, bounded together by `evals_daily_spend_cap_aud` (A$2 a day unless you change it).
- CloudFront, Lambda, DynamoDB and API Gateway are pay-per-use and small at this traffic; Bedrock
  scales with how much is written, which is what the budget alarm from step 1 is watching.
- ACM certificates are free and renew themselves.

Check the AWS pricing pages before relying on these numbers.

The firewall can be trimmed, at your own risk, with the switches in
[docs/cost-saving.md](cost-saving.md): about US$16 a month per environment for the regional web
ACLs, US$7 for the site's. Every switch is on by default; the admin API's address allowlist stays
on whatever you switch off.

## Not done, on purpose

- **No custom domain for the API.** The site talks to the API on its own AWS address, which is fine
  and needs no change.
- **No email at the domain.** If you want it later, the MX records go in the Route 53 zone.
- **The original deployment keeps both environments in one AWS account.** Names never collide, but
  a quota or a bad permission change touches both. Two accounts are supported:
  see [deployment-separate-accounts.md](deployment-separate-accounts.md).
