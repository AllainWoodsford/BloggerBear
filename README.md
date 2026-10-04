# BloggerBear

An autonomous, multi-domain research-and-publishing platform. Admins
configure "topics" (a data domain + an adapter); each topic runs two
unattended cadences — a research tick (hourly heartbeat, configurable
interval) that watches its source for material change, and a daily
authoring cycle that turns fresh findings into a reviewed, published
article. A small admin console
(CLI, not a web app) manages topics and moderation; a public static site
serves the results, with RSS for anything that wants to consume it
programmatically instead of scraping it.

Built out phase-by-phase as a portfolio project — see `docs/project-plan.md`
(architecture/rules) and `docs/PROGRESS.md` (the live phase-by-phase
tracker) for the full history and rationale behind every decision below.

**Status: live.** Every phase (0 through 8) is built, plus the enhancements in
`docs/project-plan.md` §11. Dev auto-deploys from `dev`; production runs at
**bloggerbear.com** and is deployed by GitHub Releases.
**[docs/production-runsheet.md](docs/production-runsheet.md)** is the production
and domain step-by-step. The "Deploying this" section below is the original
first-time setup run sheet.

## Architecture

- **Region**: `ap-southeast-2` (Sydney) for everything, with two AWS
  platform-mandated exceptions — the CloudFront-scope WAF Web ACL and its
  ACM certificate, which AWS only reads from `us-east-1` regardless of
  hosting region.
- **Compute**: 11 Python 3.11 AWS Lambda functions from one shared
  deployment package (`lambdas/`) and one shared execution role: research
  tick, daily cycle, admin API, public API, DLQ handler, weekly reflection,
  trending digest, musing feedback, Stats rollover, the Cost Explorer poll
  and security events.
- **AI**: Amazon Bedrock through the Converse API, so any provider's model
  works. Every call goes through `lambdas/common/bedrock.py`; tracked calls
  (`invoke_model_tracked`) record tokens and cost into each article's
  lineage and the weekly Stats. Models live in a DynamoDB registry with a
  global default, per-topic overrides and per-topic rotation
  (`common/model_routing.py`). Research falls back from GDELT to AgentCore
  Web Search.
- **Storage**: 15 DynamoDB tables (`infra/modules/app-data`) + a private S3
  bucket for article bodies and raw source snapshots (separate from the
  public site's own S3 bucket, below).
- **Frontend**: a static site (S3 + CloudFront + Origin Access Control) —
  plain HTML/CSS/JS, no framework; JS/CSS are minified at deploy
  (`scripts/minify_frontend.py`). Published articles are also rendered as
  static pages. A public Stats page shows AI and AWS spend.
- **Admin console**: not a web app — a local CLI (`scripts/admin_cli.py`)
  that signs requests with AWS SigV4, talking to an IAM-authenticated,
  IP-allowlisted API. A browser SPA would need Cognito Identity Pool
  federation just to hold credentials safely, which is more
  infrastructure than a single-operator project needs.
- **Public API**: a second, unauthenticated API Gateway — topics,
  articles, an anonymous view counter, anonymous feedback, and an RSS
  feed. Rate-limited by WAF rather than IP-gated, since it must stay
  reachable by anonymous visitors, and throttled at API Gateway. The
  frontend reaches it through its own CloudFront distribution, which
  caches the listings, articles and RSS the API marks cacheable.
- **Security**: CloudFront + WAF (managed rule set + rate limiting +
  logging) + Shield Standard; the admin API additionally sits behind IAM
  auth and an IP allowlist that fails closed (empty allowlist = nothing
  gets in) until an operator IP is configured. Every request the regional
  WAFs block, and every comment screening drops as an attack, is grouped
  into an incident in the SecurityEvents table (category, severity,
  suggested next steps, status; a keyed hash of the client, never the IP;
  kept 120 days), and a high-severity incident emails an alarm
  (`common/security_events.py`).
- **Observability**: CloudWatch alarms (Lambda errors/throttles, DLQ
  depth, Step Functions failures, feedback spam), pipeline and Lambda runs
  dashboards per environment plus an edge dashboard (API Gateway and WAF)
  in production, a daily Cost Explorer poll
  (API Gateway, AgentCore and WAF spend) feeding the Stats page, and an
  AWS Budget alarm scoped to Bedrock spend.
- **IaC**: Terraform ≥1.10 (native S3 state locking — no DynamoDB lock
  table), applied via GitHub Actions using OIDC role federation (no
  long-lived AWS keys anywhere in this repo).

### The pipeline

```
Research tick (heartbeat)            Daily authoring cycle (9 AM, topic's zone)
─────────────────────────            ─────────────────────────────────────────
due yet? (research_interval_hours)   load topic + every finding since its
  no  -> stop                          last article (+ today's editorial goal)
load topic + adapter                 ideate 3 angles -> pick one
fetch current source state           draft article (Bedrock), folding in the
diff vs prior snapshot                 bear's equipped prompt refinements,
  no change?  -> stop                  a top-voted past excerpt, and
  changed?    -> summarize             financial guidance (if applicable)
               (Bedrock) & store     fresh-data review (claims vs. the
               a Finding               source now; shadow or enforce)
                                     compliance review
                                       financial topic? -> always manual
                                       else -> Bedrock review, pass/fail
                                     publish (static page + musing), or
                                       queue for moderation
```

Both cadences are per-topic (EventBridge Scheduler schedules the Admin API
creates, updates and deletes as topics change — topics are runtime data, not
Terraform). The research schedule is a heartbeat; each tick decides from
DynamoDB whether it is due. A Step Functions wrapper gives the daily cycle
retries and a dead-letter queue; the research tick bypasses Step Functions.

Held articles wait in the review inbox (`admin_cli approve`), where they can
be approved, rejected, or **re-written** in the background by a chosen model,
which runs the reviews again.

More jobs run on fixed, Terraform-managed schedules: a **daily cross-topic
digest** ("Trending Everywhere") that publishes through the same review path;
a **weekly reflection** that reads reader feedback and proposes prompt
refinements, which become **gear** the bear wears once approved; **musings**
(BloggerBear's short reflections on articles and feedback); a weekly **Stats
rollover**; and a daily **Cost Explorer poll**.

### Hard constraints (enforced in code, not just documented)

1. No PII is collected or persisted — a public feedback comment is kept
   only if it passes code checks (length, PII shapes, links, injection
   patterns) and then a one-word Bedrock KEEP/DROP screen; anything else
   is dropped, never redacted-and-stored (`common/comment_screening.py`).
2. Research ticks always diff-first — Bedrock is never called unless the
   adapter reports a material change.
3. Drafts always pass compliance review before publish, or they land in
   a moderation queue instead. A Re-Write never publishes; a person does.
4. Financial/investment-adjacent topics (`is_financial`) are always
   routed to manual moderation, regardless of confidence — deterministic
   routing, not something an LLM call could override — plus
   defense-in-depth drafting guidance and a standing "not financial
   advice" disclaimer appended to the draft itself. The one adapter that
   deals in financial data (`crypto_feed`) has `is_financial` forced
   `True` server-side, so this can't be bypassed by an operator forgetting
   the flag.
5. New data sources are adapters (`fetch_state` / `material_diff` /
   `source_refs`), never core-pipeline branches — proven by the fact that
   adding the 2nd and 3rd adapters (Phase 7) touched zero lines of
   `research_tick_handler.py`'s actual flow. A new adapter must also declare
   its data sources (`sources`), which is what credits them under every
   article and topic title; see "Adding an adapter" in
   `docs/project-plan.md` §6.
6. Terraform never applies ad hoc — see the branch/release model below.
7. Security scans (Trivy, Bandit) and lint/tests fail on HIGH/CRITICAL
   findings and must pass before any apply, dev or production, in the same
   workflow run. (They run on PRs too; GitHub branch protection isn't
   available on this repo's plan, so they don't technically block a merge.)

## What's in the repo

```
lambdas/                    Python 3.11, one shared deployment package
  research_tick_handler.py    heartbeat: due? -> diff-first, per-topic
  daily_cycle_handler.py      daily: ideate -> draft -> review -> publish;
                                also runs Re-Writes (async event)
  admin_api_handler.py        IAM-authenticated admin API
  public_api_handler.py       unauthenticated public API + RSS + feedback
  dlq_handler.py              pipeline DLQ -> FailedExecutions records
  weekly_reflection_handler.py   weekly: feedback -> prompt refinements
  trending_digest_handler.py     daily: cross-topic digest
  musing_feedback_handler.py     the bear's musings on reader feedback
  stats_rollover_handler.py      weekly: roll Stats into history
  cost_explorer_poll_handler.py  daily: the AWS bill, every service
  security_events_handler.py     WAF blocks -> SecurityEvents incidents
  common/                     shared modules; the main ones:
    adapters/                  base.py (contract), registry.py, and one
                                module per domain: github_trending.py,
                                hacker_news.py, crypto_feed.py, web_search.py
    bedrock.py                 every Bedrock call (Converse API)
    model_routing.py, costing.py, stats_tracking.py
                                which model, what it cost, weekly totals
    compliance.py               compliance review, financial guidance/disclaimer
    fresh_review.py             the fresh-data review of a draft
    rewrite.py                  the background Re-Write of a held article
    comment_screening.py        keep-or-drop screening of feedback comments
    static_pages.py             rendering published articles to S3
    musings.py, equipment.py, gear.py   the bear's musings and gear
    dynamo.py                   every DynamoDB access, one file
    scheduler.py                per-topic EventBridge Scheduler CRUD
  tests/                      pytest + moto, one file per handler/module

infra/
  bootstrap/                 state bucket + OIDC provider + deploy roles +
                              Route 53 zone -- applied locally, never via CI
  modules/
    app-data/                 the 15 DynamoDB tables
    static-site/               S3 + CloudFront + OAC, reused by both envs
    rest-api/                  the admin and public REST APIs
    observability/             CloudWatch alarms and dashboards, reused
                                by both envs
  environments/
    dev/                       auto-deploys on push to `dev`
    production/                deploys only on a GitHub Release from `prod`

frontend/                   plain HTML/CSS/JS, no framework
  index.html, app.js, styles.css   hash-routed SPA: topics, articles,
                                    feedback, the digest, musings, Stats
  terms.html, privacy.html, about.html   static pages

scripts/
  admin_cli.py               the operator's "admin UI" -- SigV4-signed
                              requests against the admin API
  review_inbox.py            `inbox` / `approve`: the one-keystroke review loop
  minify_frontend.py         builds frontend-dist/ for deploy
  README.md                  the full CLI reference
  tests/                     pytest coverage for the scripts

.github/workflows/
  terraform.yml               on merge to dev: security + lint/test, then
                                apply -- one run
  terraform-production-release.yml   the same checks, then apply to
                                production on Release
  pr-checks.yml                 PRs only: terraform fmt/validate/test, and
                                trufflehog, gitleaks and the personal-data
                                denylist over the PR's commits
  on-demand-scan.yml            by hand or a `security-scan` PR label: every
                                security, secret and personal-data check over
                                the whole repo and history, every severity
                                reported; terraform fmt/validate/test; never
                                deploys
  destroy-dev.yml              manual, typed-confirmation teardown of dev
  python-ci.yml                 pytest + ruff on lambdas/scripts (PRs;
                                called before each apply)
  security.yml                   trivy (config; dependencies + secrets of the
                                whole repo, MEDIUM reported, HIGH+ fails) +
                                bandit on lambdas/ and scripts/ (every PR;
                                called before each apply)

docs/
  project-plan.md              architecture, rules, data model -- source
                                of truth for "why"
  PROGRESS.md                   phase-by-phase tracker -- source of truth
                                for "what's built vs. not," with the full
                                Phase 0-8 checklist and cost/teardown notes
  specs/phase-0-foundations.md   the detailed Phase 0 build spec (branch
                                model, bootstrap, OIDC wiring)
  production-runsheet.md        production + domain, step by step
  risks/                        known weaknesses, e.g. scaling-findings-01.md
  enhancements/                 designs not yet built, e.g. the dispatcher queue
```

### Branch & release model

- `dev` — the default branch. Every change lands here via PR (never a
  direct push). Merging to `dev` auto-applies `infra/environments/dev` —
  no approval gate; it's meant to be broken and rebuilt freely.
- `prod` — promoted from `dev` via PR when a set of changes is ready to
  ship. Merging into `prod` does **not** deploy anything by itself.
- A production deploy happens only when a GitHub Release is published
  from a commit on `prod` (semver tag, e.g. `v0.1.0`), gated by the
  `production` GitHub Environment: it accepts only `v*` tags and waits for
  the required reviewer's approval. A new release replaces whatever was
  previously deployed — one Terraform state, no blue/green.
- There are only these two long-lived branches (the old `master` was retired
  on 2026-10-04). A repository ruleset (`protect-deploy-branches`) covers
  both: changes arrive by PR, and force-pushes and deletions are
  blocked. The repo admin can bypass it (a one-person project must never
  lock itself out); it stops everyone else. On top of that, nothing is
  applied without passing the security scans and lint/tests first.
- Workflows get a read-only `GITHUB_TOKEN` unless they ask for more, and
  workflows from fork PRs wait for approval. See
  [docs/todo/public-repo-runsheet.md](docs/todo/public-repo-runsheet.md)
  for how these settings were applied.

## Deploying this

This is the first-time setup run sheet, in order (steps 0 to 4 are done for
dev; for what is left before production and the domain, use
[docs/production-runsheet.md](docs/production-runsheet.md), which is current and
adds the DNS steps). Each step says who does it (you, locally / GitHub UI / CI).

**Deploying a fork to your own AWS account (or to two, one per environment)?**
Read [docs/deploying-your-own.md](docs/deploying-your-own.md) first: it lists
the GitHub secrets and variables that point the workflows at your account,
your state bucket and your repository, and what still has to be changed by hand.

### 0. Prerequisites (you, one-time)

- [ ] AWS root user MFA enabled; stop using root day-to-day.
- [ ] An IAM (or IAM Identity Center) admin user for this project, MFA on,
  configured locally (`aws configure` or `aws configure sso`).
- [ ] An AWS Budget alarm on the account (e.g. $10/$25/$50) — **before**
  anything below is applied. The Bedrock-specific budget in
  `infra/bootstrap` (step 1) is in *addition* to this, not a replacement.
- [ ] Bedrock model access requested in-console for the Claude model(s)
  you want, **in `ap-southeast-2`** — this can sit pending, but confirm
  which model IDs are directly invokable there vs. need a cross-region
  inference profile before step 4.

### 1. One-time local bootstrap (you, locally)

`infra/bootstrap` creates the Terraform state bucket, the GitHub OIDC
identity provider, and the two deploy roles CI assumes. It has no
backend of its own (it can't depend on the bucket it's creating) and is
**never** applied through CI — always locally, by a human, once.

```bash
cd infra/bootstrap
terraform init
terraform apply \
  -var="budget_alert_email=you@example.com"   # optional but recommended --
                                                # without it, no Bedrock
                                                # budget alarm is created
```

Note the outputs — you'll need them in the next two steps:
`state_bucket_name`, `oidc_provider_arn`, `dev_deploy_role_arn`,
`prod_deploy_role_arn`. If `state_bucket_name` differs from the default
`bloggerbear-terraform-state` (only happens if that name is already
taken globally, which it always is for a fork), set the GitHub Actions
secrets `TF_STATE_BUCKET_DEV` and `TF_STATE_BUCKET_PROD` to it. The
`backend "s3"` blocks can't reference variables, so CI passes the name
to `terraform init` instead
([docs/deploying-your-own.md](docs/deploying-your-own.md)).

### 2. Wire GitHub Actions up to AWS (you, GitHub UI)

`python scripts/setup_repo.py --dry-run` walks through every setting below, checks each answer
and shows what it would set; without `--dry-run` it sets them
([docs/deploying-your-own.md](docs/deploying-your-own.md)). By hand:

- **Settings → Secrets and variables → Actions → Secrets** (repository
  level), all **secrets**, never variables — a variable prints in plain
  text in every step's log, and on a public repo those logs are public:
  - `AWS_DEV_DEPLOY_ROLE_ARN` = the `dev_deploy_role_arn` output. The
    trust policy is what protects the role; a secret just keeps the ARN
    (and its account ID) out of the logs.
  - `ADMIN_ALLOWED_CIDRS_DEV` = your public IP as a Terraform
    list-of-strings literal, e.g. `["203.0.113.7/32"]`. `terraform.yml`'s
    `apply-dev` job passes it through as `TF_VAR_admin_allowed_cidrs`, so
    it never lives in `terraform.tfvars` / git history, and the Terraform
    variable is `sensitive`, so plans print `(sensitive value)` rather
    than the IP (GitHub only masks the secret's exact text).
  - `ALERT_EMAIL_DEV` = where alarm emails go (also `sensitive`).
- **Settings → Environments**: create an environment named `production`,
  add yourself as a required reviewer, and add the **secrets**
  `AWS_PROD_DEPLOY_ROLE_ARN` = the `prod_deploy_role_arn` output,
  `ADMIN_ALLOWED_CIDRS_PROD` and `ALERT_EMAIL_PROD` (same formats and
  reasoning as dev's, environment- or repo-level). The
  required-reviewer gate is what makes a production release a
  deliberate, approved act rather than an accidental push.
- **Settings → Branches**: create the `prod` branch from `dev`'s current
  tip (`git branch prod dev && git push -u origin prod`, or via the
  GitHub UI). Add branch protection to both `dev` and `prod`: require a
  PR, require the `terraform` and `security` status checks to pass.
  (Note: private-repo branch protection needs a paid GitHub plan or a
  public repo — the Terraform/OIDC trust-policy gates still hold either
  way, this is defense-in-depth on top of them.)

### 3. Fill in the required Terraform variables (you, locally)

Both environments already have a `terraform.tfvars` with the fail-closed
defaults checked in — empty values that deliberately leave things
unreachable/unconfigured until you set them:

| File | Variable | Required before | Why it's empty by default |
|---|---|---|---|
| `dev/terraform.tfvars` | `bedrock_model_id` | the pipeline can run | model ID/inference-profile choice is an open question until you confirm it in-console (step 0). Any Bedrock provider's model or inference-profile ID works here, not just Anthropic's — `lambdas/common/bedrock.py` calls the Converse API, which normalizes the request/response shape across providers |
| `dev/terraform.tfvars` | `web_acl_arn` | dev shares production's WAF ACL | doesn't exist until production has been applied once (step 5) |
| `production/terraform.tfvars` | `domain_name`, `hosted_zone_id` | production's first apply succeeds at all | domain/registrar is a decision only you can make |
| `production/terraform.tfvars` | `bedrock_model_id` | same as dev | same as dev |
| either (optional) | `alert_email` | CloudWatch alarms actually notify someone | alarms still fire and publish to SNS either way; this only controls whether you're told |

`admin_allowed_cidrs` is deliberately **not** in this table — it's not
set via `terraform.tfvars` at all, on either environment. A real IP
checked into `terraform.tfvars` would sit in git history permanently,
even after being changed later. It's instead supplied at apply time via
a `TF_VAR_admin_allowed_cidrs` environment variable in CI, sourced from
the `ADMIN_ALLOWED_CIDRS_DEV` / `ADMIN_ALLOWED_CIDRS_PROD` secrets set
up in step 2. Still fails closed — a local apply without that env var
set falls back to the variable's `[]` default, same as before.

**Optional CoinGecko API key** (for the crypto adapter): store it in SSM Parameter Store as a
SecureString, once per environment, from your own machine:

```bash
# Git Bash rewrites arguments that start with "/", hence MSYS_NO_PATHCONV=1 (not needed in PowerShell)
MSYS_NO_PATHCONV=1 aws ssm put-parameter --region ap-southeast-2 --type SecureString --overwrite   --name /bloggerbear/dev/coingecko-api-key --value '<your key>'
MSYS_NO_PATHCONV=1 aws ssm put-parameter --region ap-southeast-2 --type SecureString --overwrite   --name /bloggerbear/production/coingecko-api-key --value '<your key>'
```

Terraform only grants the Lambdas read access to that name and tells the two crypto Lambdas
(research tick, daily cycle) where it is; it never creates the parameter, so the key is never in
Terraform state, a Lambda's environment variables, a GitHub secret or `terraform.tfvars`. The
Lambdas read it once per cold start, so a new value is picked up as containers recycle (or at once,
after any deploy). Set `coingecko_api_plan = "pro"` in `terraform.tfvars` if it's a paid key
(default `"demo"`, the free key). No parameter is fine -- the adapter uses CoinGecko's keyless public
API -- and if a key is rate-limited or rejected at runtime, requests fall back to the public API
automatically. Never put the key in a topic's `adapter_config` (that's stored in DynamoDB).

**Optional GitHub API token** (for the GitHub Trending adapter, which calls GitHub's REST Search
API): create a fine-grained personal access token with **no permissions** (public data only) and
store it the same way:

```bash
MSYS_NO_PATHCONV=1 aws ssm put-parameter --region ap-southeast-2 --type SecureString --overwrite   --name /bloggerbear/dev/github-api-token --value '<your token>'
MSYS_NO_PATHCONV=1 aws ssm put-parameter --region ap-southeast-2 --type SecureString --overwrite   --name /bloggerbear/production/github-api-token --value '<your token>'
```

Without it the adapter searches unauthenticated (10 requests a minute per IP, shared with whatever
else uses that Lambda egress IP); with it, 30 a minute on the token's own budget. A token GitHub
rejects falls back to unauthenticated search automatically.

### 4. First deploy: dev (CI, triggered by you)

Open a PR into `dev` touching anything (or just merge this repo's
current `dev` tip into itself via an empty PR) to exercise the `terraform`
plan check, then merge. Merging to `dev` triggers `terraform.yml`'s
`apply-dev` job automatically — no approval needed. Watch it in the
Actions tab. First run creates everything: DynamoDB tables, the content
bucket, all 11 Lambdas, both API Gateways, the WAF ACLs + logging, the
CloudWatch dashboards, the static site.

If this is truly the first-ever apply, `bedrock_model_id` and
`admin_allowed_cidrs` are still empty (step 3) — that's fine, the stack
comes up but the admin API is unreachable and the pipeline can't call
Bedrock yet. Fill those in, commit, and let `dev` auto-apply again.

### 5. First deploy: production (you approve, CI applies)

Once dev looks right: PR `dev` into `prod`, merge it (this just marks
intent — nothing deploys yet), then publish a GitHub Release from a
commit on `prod` (semver tag, e.g. `v0.1.0`). This triggers
`terraform-production-release.yml`, which verifies the released commit
is actually reachable from `prod`'s tip, then waits for your
required-reviewer approval on the `production` Environment before
applying. This is also where the shared WAF Web ACL gets created — copy
its ARN into `dev/terraform.tfvars`'s `web_acl_arn` afterward so dev
starts sharing it too.

### 6. Seed a topic (you, locally, via the admin CLI)

```bash
pip install -r scripts/requirements.txt
export BLOGGERBEAR_ADMIN_API_URL=$(terraform -chdir=infra/environments/dev output -raw admin_api_url)
export AWS_REGION=ap-southeast-2

python scripts/admin_cli.py topics create \
  --topic-id github-trending --name "GitHub Trending" \
  --adapter github_trending

python scripts/admin_cli.py topics trigger github-trending --pipeline research_tick
python scripts/admin_cli.py topics trigger github-trending --pipeline daily_cycle
python scripts/admin_cli.py moderation list
```

Creating a topic automatically creates its two EventBridge Scheduler
schedules — after this, it runs unattended. See `scripts/README.md` for
the full CLI reference (topics/moderation/refinements subcommands).

### Tearing down / cost control

- Dev: `workflow_dispatch` the `destroy-dev` workflow (type `destroy` to
  confirm). CloudFront distributions take 10–20 minutes to disable before
  they can be deleted — that's normal, not a stuck job.
- To taper rather than stop outright: disable a topic's EventBridge
  schedules, or lower its cadence — free and instantly reversible.
- To stop outright: destroy dev, then production, in that order. Decide
  up front whether to release the domain/hosted zone or keep it parked.
- See `docs/PROGRESS.md`'s "Cost control & tapering" section for the full
  detail.

## Local development

From the repo root (what CI runs):

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r lambdas/requirements.txt -r lambdas/requirements-dev.txt -r scripts/requirements.txt
pytest lambdas/ scripts/     # ~2,450 tests, moto-mocked AWS, no real credentials needed
ruff check lambdas/ scripts/
```

```bash
pip install bandit==1.7.10
bandit -r lambdas/ scripts/ --severity-level high --confidence-level high
```

```bash
terraform fmt -check -recursive infra/
cd infra/environments/dev && terraform init -backend=false && terraform validate
```

### Personal-data and secret checks before you commit

`pr-checks.yml` runs Gitleaks (secrets, email addresses and AWS account IDs, per
`.gitleaks.toml`) and `scripts/pii_denylist_check.py` (your own exact personal strings) on every
PR. Those run after a push, when the content is already on GitHub, so run the same checks before
each commit too:

```bash
git config core.hooksPath .githooks          # once per clone: enables .githooks/pre-commit
winget install Gitleaks.Gitleaks              # or brew install gitleaks / your package manager
```

Then list your own personal strings (a name, a home IP, a personal address), one per line, in
`.pii-denylist` at the repo root. It is gitignored, and the hook refuses to commit it. For CI, put
the same list in the `PII_DENYLIST` repository secret (Settings → Secrets and variables →
Actions). Neither check ever prints what it found, so the logs stay safe to publish.

## License

The code is licensed under the [Apache License 2.0](LICENSE). Security reports: see
[SECURITY.md](SECURITY.md). Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Data sources and attribution

BloggerBear writes from other people's data. The licence above covers this repository's code, not
that data: each source has its own terms, and if you run a copy that uses a source, its terms
apply to you. The credits each source asks for are below; keep them wherever the data is shown.

| Source | Used by | Credit and terms |
|---|---|---|
| **CoinGecko** | The crypto adapter (`lambdas/common/adapters/crypto_feed.py`): prices and market data | Data provided by [CoinGecko](https://www.coingecko.com). Their [attribution guide](https://brand.coingecko.com/resources/attribution-guide) asks for one of a few set phrases, linked to their site, "in a visible location, close to where the data is displayed". An API key is optional (`docs/deploying-your-own.md`); the credit is required either way. |
| **The GDELT Project** | Web search (`lambdas/common/web_search.py`), for topics that search the news | News data from [the GDELT Project](https://www.gdeltproject.org/). Its terms allow any use without fee and say "any use or redistribution of the data must include a citation to the GDELT Project and a link to this website". |
| **GitHub Trending** | The GitHub adapter (`lambdas/common/adapters/github_trending.py`) | Source: [GitHub Trending](https://github.com/trending). There is no API for that page: the adapter reads the public page on each research run, and articles link to the repositories they mention. That is governed by GitHub's [Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) and [Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service), not by a data licence. Read them before you run this adapter yourself. |
| **Hacker News** | The Hacker News adapter (`lambdas/common/adapters/hacker_news.py`) | Stories from [Hacker News](https://news.ycombinator.com/), through its [official API](https://github.com/HackerNews/API). The API's documentation sets no attribution requirement; articles credit and link to the stories they draw on. |
| **Amazon Bedrock AgentCore web search** | Web search, as the fallback and for topics that ask for it | An AWS service, used under the AWS Customer Agreement. Articles link to the pages they cite. |

Articles are written by a language model through Amazon Bedrock, and every article lists the
sources it drew on and says how it was written and reviewed.

Built with, among others: [Requests](https://requests.readthedocs.io/),
[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/),
[Python-Markdown](https://python-markdown.github.io/), the
[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk),
[Strands Agents](https://strandsagents.com/) and the
[AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter). Each is under its own
licence; the pinned versions are in `lambdas/requirements*.txt` and `scripts/requirements.txt`.

BloggerBear is an independent project. It is not affiliated with, sponsored by or endorsed by
CoinGecko, the GDELT Project, GitHub, Y Combinator or Amazon. Their names and marks belong to
their owners and are used here only to say where data comes from and what the project runs on.
