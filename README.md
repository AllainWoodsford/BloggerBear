# BloggerBear

An autonomous, multi-domain research-and-publishing platform. Admins
configure "topics" (a data domain + an adapter); each topic runs two
unattended cadences — an hourly research tick that watches its source for
material change, and a daily authoring cycle that turns fresh findings
into a compliance-reviewed, published article. A small admin console
(CLI, not a web app) manages topics and moderation; a public static site
serves the results, with RSS for anything that wants to consume it
programmatically instead of scraping it.

Built out phase-by-phase as a portfolio project — see `docs/project-plan.md`
(architecture/rules) and `docs/PROGRESS.md` (the live phase-by-phase
tracker) for the full history and rationale behind every decision below.

**Status: code-complete, not yet deployed.** Every phase (0 through 8) is
implemented and tested, but `infra/bootstrap` has never been applied —
there is no Terraform state bucket yet, so nothing in this repo has ever
actually run against real AWS. The "Deploying this" section below is the
run sheet for that first deploy.

## Architecture

- **Region**: `ap-southeast-2` (Sydney) for everything, with two AWS
  platform-mandated exceptions — the CloudFront-scope WAF Web ACL and its
  ACM certificate, which AWS only reads from `us-east-1` regardless of
  hosting region.
- **Compute**: 6 Python 3.11 AWS Lambda functions, one shared deployment
  package (`lambdas/`), one shared execution role.
- **AI**: Amazon Bedrock (Claude) — every Bedrock call goes through
  `lambdas/common/bedrock.py`'s `invoke_claude`.
- **Storage**: 7 DynamoDB tables (`infra/modules/app-data`) + a private S3
  bucket for article bodies and raw source snapshots (separate from the
  public site's own S3 bucket, below).
- **Frontend**: a static site (S3 + CloudFront + Origin Access Control) —
  plain HTML/CSS/JS, no build step, no framework.
- **Admin console**: not a web app — a local CLI (`scripts/admin_cli.py`)
  that signs requests with AWS SigV4, talking to an IAM-authenticated,
  IP-allowlisted API. A browser SPA would need Cognito Identity Pool
  federation just to hold credentials safely, which is more
  infrastructure than a single-operator project needs.
- **Public API**: a second, unauthenticated API Gateway — topics,
  articles, an anonymous view counter, anonymous feedback, and an RSS
  feed. Rate-limited by WAF rather than IP-gated, since it must stay
  reachable by anonymous visitors.
- **Security**: CloudFront + WAF (managed rule set + rate limiting +
  logging) + Shield Standard; the admin API additionally sits behind IAM
  auth and an IP allowlist that fails closed (empty allowlist = nothing
  gets in) until an operator IP is configured.
- **Observability**: CloudWatch alarms (Lambda errors/throttles, DLQ
  depth, Step Functions failures) + a dashboard per environment, plus an
  AWS Budget alarm scoped specifically to Bedrock spend.
- **IaC**: Terraform ≥1.10 (native S3 state locking — no DynamoDB lock
  table), applied via GitHub Actions using OIDC role federation (no
  long-lived AWS keys anywhere in this repo).

### The pipeline

```
Hourly research tick                Daily authoring cycle
─────────────────────                ─────────────────────
load topic + adapter                 load topic + recent findings
fetch current source state           ideate 3 angles -> pick one
diff vs prior snapshot               draft article (Bedrock)
  no change?  -> stop                fold in: admin-approved prompt
  changed?    -> summarize                    refinement (if any),
               (Bedrock) & store              few-shot excerpt from a
               a Finding                      top-voted past article,
                                               financial-topic guidance
                                               (if applicable)
                                      compliance review
                                        financial topic? -> always
                                          manual moderation
                                        else -> Bedrock compliance
                                          review, pass/fail
                                      publish, or queue for
                                        manual moderation
```

Both cadences are per-topic and admin-configurable (EventBridge
Scheduler, created/updated/deleted dynamically as topics change — not
fixed Terraform resources, since topics are runtime data). A Step
Functions wrapper gives the daily cycle retries and a dead-letter queue;
the hourly tick bypasses Step Functions entirely as a single
self-contained operation.

Two more jobs run on fixed, Terraform-managed schedules rather than
per-topic: a **weekly reflection** job that reads a week of reader
feedback and proposes prompt refinements (admin-approved before they
take effect), and a **daily cross-topic digest** ("Trending Everywhere")
that synthesizes what's trending across every topic at once and
publishes through the same compliance/moderation path as any other
article.

### Hard constraints (enforced in code, not just documented)

1. No PII is collected or persisted — public feedback comments go through
   a regex pass, then a second Bedrock redaction-review pass, before
   anything is ever written; raw text is never persisted, even
   transiently.
2. Research ticks always diff-first — Bedrock is never called unless the
   adapter reports a material change.
3. Drafts always pass compliance review before publish, or they land in
   a moderation queue instead.
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
   `research_tick_handler.py`'s actual flow.
6. Terraform never applies ad hoc — see the branch/release model below.
7. Security checks block merges on HIGH/CRITICAL findings.

## What's in the repo

```
lambdas/                    Python 3.11, one shared deployment package
  research_tick_handler.py    hourly: diff-first, per-topic
  daily_cycle_handler.py      daily: ideate -> draft -> review -> publish
  admin_api_handler.py        IAM-authenticated admin API
  public_api_handler.py       unauthenticated public API + RSS
  weekly_reflection_handler.py   weekly: feedback -> prompt refinements
  trending_digest_handler.py     daily: cross-topic digest
  common/
    adapters/                  base.py (contract) + one module per domain:
                                github_trending.py, hacker_news.py,
                                crypto_feed.py
    bedrock.py                 the one place invoke_model is called
    compliance.py               PII redaction, compliance review,
                                financial-topic guidance/disclaimer
    dynamo.py                   every DynamoDB access, one file
    scheduler.py                per-topic EventBridge Scheduler CRUD
  tests/                      pytest + moto, one file per handler/adapter

infra/
  bootstrap/                 state bucket + OIDC provider + deploy roles
                              -- applied once, locally, never via CI
  modules/
    app-data/                 the 7 DynamoDB tables
    static-site/               S3 + CloudFront + OAC, reused by both envs
    observability/             CloudWatch alarms/dashboard, reused by
                                both envs
  environments/
    dev/                       auto-deploys on push to `dev`
    production/                deploys only on a GitHub Release from `prod`

frontend/                   plain HTML/CSS/JS, no build step
  index.html, app.js, styles.css   hash-routed SPA: topics, articles,
                                    feedback, the cross-topic digest

scripts/
  admin_cli.py               the operator's "admin UI" -- SigV4-signed
                              requests against the admin API
  tests/                     pytest coverage for the CLI itself

.github/workflows/
  terraform.yml               plan on PR, auto-apply to dev on merge
  terraform-production-release.yml   apply to production on Release
  destroy-dev.yml              manual, typed-confirmation teardown of dev
  python-ci.yml                 pytest + ruff on lambdas/scripts
  security.yml                   trivy (config + fs/secrets) + bandit
  dev-gatekeeper.yml             additional adversarial review gate on
                                  PRs into dev

docs/
  project-plan.md              architecture, rules, data model -- source
                                of truth for "why"
  PROGRESS.md                   phase-by-phase tracker -- source of truth
                                for "what's built vs. not," with the full
                                Phase 0-8 checklist and cost/teardown notes
  specs/phase-0-foundations.md   the detailed Phase 0 build spec (branch
                                model, bootstrap, OIDC wiring)
```

### Branch & release model

- `dev` — the default branch. Every change lands here via PR (never a
  direct push). Merging to `dev` auto-applies `infra/environments/dev` —
  no approval gate; it's meant to be broken and rebuilt freely.
- `prod` — promoted from `dev` via PR when a set of changes is ready to
  ship. Merging into `prod` does **not** deploy anything by itself.
- A production deploy happens only when a GitHub Release is published
  from a commit on `prod` (semver tag, e.g. `v0.1.0`), gated by the
  `production` GitHub Environment's required-reviewer approval. A new
  release replaces whatever was previously deployed — one Terraform
  state, no blue/green.
- Both branches require a PR and passing `terraform`/`security` checks
  (branch protection) — no direct pushes to either.

## Deploying this

Nothing has been deployed yet, so this is a first-time setup run sheet,
in order. Each step says who does it (you, locally / GitHub UI / CI).

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
taken globally), update the literal `bucket = "..."` string in the
`backend "s3"` blocks in both `infra/environments/dev/main.tf` and
`infra/environments/production/main.tf` — those blocks can't reference
variables.

### 2. Wire GitHub Actions up to AWS (you, GitHub UI)

- **Settings → Secrets and variables → Actions → Variables** (repository
  level): add `AWS_DEV_DEPLOY_ROLE_ARN` = the `dev_deploy_role_arn`
  output. This is a variable, not a secret — the ARN itself isn't
  sensitive, the IAM trust policy is what actually protects it.
- **Settings → Secrets and variables → Actions → Secrets** (repository
  level): add `ADMIN_ALLOWED_CIDRS_DEV` = your public IP as a Terraform
  list-of-strings literal, e.g. `["203.0.113.7/32"]`. This is a secret,
  not a variable — unlike the role ARN above, this is a real IP address,
  and a secret is masked in Actions logs. `terraform.yml`'s `apply-dev`
  job passes it through as the `TF_VAR_admin_allowed_cidrs` environment
  variable, so it never needs to live in `terraform.tfvars` / git history.
- **Settings → Environments**: create an environment named `production`,
  add yourself as a required reviewer, add an environment-scoped variable
  `AWS_PROD_DEPLOY_ROLE_ARN` = the `prod_deploy_role_arn` output, and an
  environment-scoped **secret** `ADMIN_ALLOWED_CIDRS_PROD` (same format
  as the dev one above). The variable/secret split and the reasoning are
  the same as dev's, just Environment-scoped instead of repo-level. The
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

### 4. First deploy: dev (CI, triggered by you)

Open a PR into `dev` touching anything (or just merge this repo's
current `dev` tip into itself via an empty PR) to exercise the `terraform`
plan check, then merge. Merging to `dev` triggers `terraform.yml`'s
`apply-dev` job automatically — no approval needed. Watch it in the
Actions tab. First run creates everything: DynamoDB tables, the content
bucket, all 6 Lambdas, both API Gateways, the WAF ACLs + logging, the
CloudWatch dashboard, the static site.

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

```bash
cd lambdas
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                       # 172 tests, moto-mocked AWS, no real credentials needed
ruff check .
```

```bash
cd scripts
pip install -r requirements.txt pytest
pytest                       # 26 tests for the admin CLI itself
```

```bash
pip install bandit==1.7.10
bandit -r lambdas/ scripts/ --severity-level high --confidence-level high
```

```bash
terraform fmt -check -recursive infra/
cd infra/environments/dev && terraform init -backend=false && terraform validate
```
