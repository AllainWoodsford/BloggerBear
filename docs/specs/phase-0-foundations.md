# Phase 0 — Foundations: Build Spec

Status: tracked in `docs/PROGRESS.md` under "Phase 0 — Foundations."
Source of truth for rationale/constraints: `docs/project-plan.md`.

**Revision history:**
- 2026-09-12: superseded the original "rename master → main, one production
  environment" plan with a two-branch, release-gated model. See "Branch &
  Release Model."
- 2026-09-12 (later): removed the DynamoDB lock table — Terraform ≥1.10's
  native S3 locking replaces it — and fixed the hosting region to
  `ap-southeast-2`. See "Region" and the updated "State backend" section.

## Objective

Stand up the deployable skeleton — state backend, two environments (`dev`,
`production`), edge security, DNS/TLS, and a branch/release workflow that
lets you iterate freely on a disposable dev environment and only touch
production through a deliberate, approved release — with **zero**
application logic (no Lambda business logic, no Bedrock calls, no
DynamoDB app tables). Everything below should be provable end-to-end before
Phase 1 writes a single Lambda.

## Region

Everything deploys to **`ap-southeast-2` (Sydney)** — this is fixed, not an
open question.

Two AWS-mandated exceptions, which are platform constraints, not a
contradiction of that choice: the CloudFront-scope **WAF Web ACL** and the
**ACM certificate** used by CloudFront both must be created in `us-east-1`,
because CloudFront only reads global-scope WAF ACLs and viewer certificates
from that region, regardless of where the distribution's origin or
everything else lives. Both resources still protect/serve the
`ap-southeast-2` deployment — they just have to be declared in a different
region block in Terraform (a second `aws` provider alias for `us-east-1`,
used only for these two resources).

One thing to verify before Phase 1 locks in a model choice (not a Phase 0
blocker): confirm which specific Claude model IDs are directly invokable in
`ap-southeast-2` versus which require routing through a cross-region
inference profile (AWS publishes an APAC inference-profile grouping for
exactly this) — Bedrock's model availability by region varies and is worth
checking in the console rather than assumed.

## Branch & Release Model

This is the core design decision for Phase 0's CI/CD, not an afterthought —
get this right now, because Phase 1 onward just rides on top of it.

**Branches:**
- `dev` — the default branch. All feature/task branches fork from here and
  merge back via PR (never a direct push). Merging into `dev` **auto-applies**
  `infra/environments/dev` — no approval gate. It's meant to be broken,
  rebuilt, and torn down freely.
- `prod` — a second long-lived branch, promoted from `dev` via PR when
  you're happy with what's on `dev`. Merging into `prod` does **not** deploy
  anything by itself — it just marks "this is what I intend to ship."

**What actually triggers a production deploy:** publishing a GitHub Release
(a tag, e.g. `v0.1.0`) from a commit on `prod`. That's the deliberate
"release control" moment — cutting a release is a distinct act from merging
code, so production only ever moves when you explicitly say so. The release
workflow applies `infra/environments/production`, gated behind the
`production` GitHub Environment's required-reviewer approval (you approve
your own release before it applies — an extra deliberate pause, not
redundant with the tag itself, since a tag can be pushed accidentally but an
environment approval can't).

**Why a new release "replaces" what's running:** every release targets the
same `infra/environments/production` Terraform state. There's no blue/green
split here — Terraform just reconciles production to match whatever the new
release's config says, which naturally supersedes the prior deployment. For
a single-admin portfolio project this is the right amount of complexity;
don't build parallel environments or traffic-shifting for this.

**Concurrency:** add a `concurrency` block per environment (e.g. `group:
deploy-dev` / `group: deploy-production`, `cancel-in-progress: false`) to
whichever workflow(s) do the applying, so two triggers close together queue
instead of racing.

**Branch protection:** both `dev` and `prod` require a PR (no direct
pushes) and require the `terraform plan` + `security` checks to pass before
merge. This is what actually enforces "always branch, always PR" as policy
rather than convention Copilot might forget under pressure.

**Versioning convention:** semver tags (`v0.1.0`, `v0.2.0`, …) for
production releases — small detail, but "versioned, gated production
releases" is a better line in an interview than "pushed to main."

## Fix first / set up first

1. Rename the repo's default branch from `master` to `dev` (GitHub Settings
   → Branches → rename; PRs auto-update).
2. Create the `prod` branch from `dev`'s current tip.
3. Branch protection rules on both `dev` and `prod`: require PR, require the
   `terraform` and `security` status checks.
4. GitHub Environments: `dev` (no protection rules — auto-deploy) and
   `production` (required reviewer = you).
5. Bump the Terraform version pinned in both `.github/workflows/*.yml` files
   (currently `1.9.8`) to a current stable ≥1.10.0 — 1.16.2 as of this
   writing, but pin whatever the actual current stable release is when this
   PR is built. 1.9.8 predates native S3 state locking; `use_lockfile` is
   silently unavailable on it.
6. Confirm the **prerequisites** in `docs/PROGRESS.md` are done — root MFA,
   admin IAM user, AWS Budget alarm, Bedrock model access requested for
   `ap-southeast-2`. Terraform should not be applied, even to dev, before
   the Budget alarm exists.

## Scope

**In scope:**
- `infra/bootstrap` — Terraform state backend (a single S3 bucket — no
  DynamoDB), applied manually/locally, one time, never through CI
- `infra/environments/dev` and `infra/environments/production` — both
  environments, sharing the reusable module below but with different inputs
- Placeholder static site (S3 + CloudFront + Origin Access Control) in both
  environments
- WAF Web ACL — **one** ACL, associated with both distributions (see Cost
  notes) — not two
- Route 53 + ACM + custom domain for **production only** (see Cost notes on
  why dev skips this)
- Two workflows (or one workflow with two trigger paths — Copilot's call,
  but two separate files is probably clearer): dev auto-apply on push, and a
  release-triggered, environment-gated production apply
- A `workflow_dispatch`-triggered "destroy dev" workflow

**Explicitly out of scope (Phase 1+):**
- Any Lambda function with real logic (research tick, ideation, draft,
  compliance review, publish, admin API)
- `Topics` / `Findings` / `CandidateIdeas` / `Articles` / any app DynamoDB
  table (unrelated to the state-locking DynamoDB removed above — if/when
  these app tables get built in Phase 1, that's a separate, deliberate use
  of DynamoDB)
- Bedrock IAM permissions or model invocation of any kind
- Cognito / admin auth
- Anything under `frontend/` beyond a single static placeholder page

## Directory layout

```
infra/
├── bootstrap/                  # one-time, applied locally, not via CI
│   ├── main.tf                 # S3 state bucket only — native locking, no DynamoDB
│   ├── variables.tf
│   └── outputs.tf               # bucket name, for backend blocks below
├── modules/
│   └── static-site/            # reusable: S3 + CloudFront + OAC (+ WAF/DNS optionally)
│       ├── main.tf
│       ├── variables.tf         # include a bool like `enable_custom_domain`
│       └── outputs.tf
└── environments/
    ├── dev/
    │   ├── main.tf              # backend key: "dev/terraform.tfstate"; enable_custom_domain=false
    │   ├── variables.tf
    │   └── terraform.tfvars     # force_destroy=true on the site bucket here
    └── production/
        ├── main.tf              # backend key: "production/terraform.tfstate"; enable_custom_domain=true
        ├── variables.tf
        └── terraform.tfvars
```

One shared bootstrap bucket, two state file keys (`dev/terraform.tfstate`,
`production/terraform.tfstate`). Locking is handled natively by S3
(`use_lockfile = true`, Terraform ≥1.10) — no DynamoDB table at all. Each
state key gets its own `.tflock` companion object, so dev and production can
never contend for the same lock.

## Component detail

**State backend (`infra/bootstrap`)** — a single S3 bucket: versioned,
encrypted (SSE-S3 or SSE-KMS), block-all-public-access, no lifecycle
deletion of state. No DynamoDB table — Terraform ≥1.10's native S3 locking
(`use_lockfile = true` in each environment's backend block) replaces it
entirely; HashiCorp has deprecated DynamoDB-based locking for the S3
backend. Whatever applies Terraform (your local CLI, and the CI role) needs
`s3:GetObject`/`PutObject`/`DeleteObject` on both the state object and its
`.tflock` companion (e.g. `.../dev/terraform.tfstate` and
`.../dev/terraform.tfstate.tflock`).

Each environment's backend block looks like:

```hcl
terraform {
  backend "s3" {
    bucket       = "<bootstrap bucket name>"
    key          = "dev/terraform.tfstate"   # or "production/terraform.tfstate"
    region       = "ap-southeast-2"
    encrypt      = true
    use_lockfile = true
  }
}
```

**Static site module** — parameterize so `dev` and `production` can diverge
cleanly:
- S3 bucket, OAC-only access (no public bucket policy). `force_destroy =
  true` for dev (so `terraform destroy` doesn't choke on a non-empty
  bucket — the standard Terraform footgun here); leave it `false` (default)
  for production, so an accidental destroy can't silently delete real
  content.
- CloudFront distribution, HTTPS-only viewer policy.
- `enable_custom_domain` (bool): when false (dev), skip Route 53 + ACM
  entirely and just use the distribution's default `*.cloudfront.net`
  domain. When true (production), request the ACM cert (in `us-east-1`,
  via a provider alias — see "Region") and wire the alias record.

**WAF** — create **one** Web ACL, in `us-east-1` (required for CloudFront
scope, regardless of the `ap-southeast-2` hosting region), with a
rate-based rule + AWS Managed Rule Groups, and associate it with **both**
distributions. A CloudFront-scope WAF ACL can be associated with multiple
distributions — there's no reason to pay its flat monthly fee twice for a
project with one operator and no real dev traffic.

**DNS/TLS (production only)** — Route 53 hosted zone + ACM cert requested
in `us-east-1` (via a provider alias — see "Region") + alias record. Dev
deliberately has none of this, which also makes dev faster to destroy and
rebuild (no DNS propagation or cert re-validation in the loop).

## CI/CD workflow requirements

| Trigger | Target | Approval | Notes |
|---|---|---|---|
| PR into `dev` or `prod` touching `infra/**` | `terraform plan` against whichever env dirs have changed (the existing loop over `infra/environments/*` already generalizes to both once `dev/` exists) | none (informational) | must pass before merge per branch protection |
| Push (merge) to `dev` | `terraform apply` on `infra/environments/dev` | none | fast iteration; disposable |
| GitHub Release published, tagged from `prod` | `terraform apply` on `infra/environments/production` | `production` environment required reviewer | the actual "ship it" moment |
| Manual (`workflow_dispatch`) | `terraform destroy` on `infra/environments/dev` | none | your on-demand teardown button |

Add a `concurrency` group per environment on whichever job(s) apply, so
overlapping triggers queue rather than race. Optionally, have the release
workflow verify the tagged commit is actually reachable from `prod`'s tip,
as a guardrail against cutting a release from the wrong branch. Bump the
Terraform version installed by these workflows from `1.9.8` to a current
stable ≥1.10.0 (see "Fix first," item 5) — without this, `use_lockfile`
doesn't work.

## Cost & teardown notes (portfolio project, cost-sensitive)

- The plan's own cost section already flags WAF's flat ~$5–10/mo fee as the
  main "always-on" line item — sharing one ACL across dev+production
  instead of two roughly halves that. Removing the DynamoDB lock table
  entirely also drops a (small, but non-zero) line item versus the original
  design.
- Dev skipping Route 53/ACM removes another small fixed cost (hosted zone
  is ~$0.50/mo) and, more importantly, removes DNS propagation/cert
  re-validation from the destroy/rebuild loop, so tearing dev down and
  rebuilding it stays fast.
- CloudFront distributions take some time to disable before they can be
  deleted — a `terraform destroy` on dev may take 10–20 minutes, not
  instant; that's normal, not a stuck job.
- If you later decide to taper rather than fully kill the project: disabling
  the EventBridge schedules (Phase 3+) is reversible and free, versus
  destroying infra — worth remembering once there's something scheduled to
  pause. Nothing to build for this now; noting it here so it's not
  forgotten later.
- If you decide to kill the project outright: `terraform destroy` on dev,
  then on production, in that order. Decide up front whether to release the
  domain/hosted zone or keep it parked — that's the one piece that isn't
  free to walk away from and re-acquire later if you change your mind.

## Acceptance criteria (Definition of Done)

- [ ] `dev` is the default branch; `prod` exists; both have branch
  protection requiring PR + passing `terraform`/`security` checks
- [ ] GitHub Environments `dev` (no gate) and `production` (required
  reviewer) exist
- [ ] CI's pinned Terraform version is ≥1.10.0 in both workflow files
- [ ] `terraform apply` in `infra/bootstrap` has been run once, locally,
  creating only an S3 bucket — no DynamoDB table anywhere
- [ ] `infra/environments/dev` and `infra/environments/production` both have
  working `backend "s3"` blocks, region `ap-southeast-2`, distinct state
  keys, `use_lockfile = true`, and no `dynamodb_table` argument
- [ ] A PR touching `infra/**` produces a clean `terraform plan` for every
  changed environment
- [ ] Merging to `dev` auto-applies `infra/environments/dev`; the site is
  reachable at its `*.cloudfront.net` domain
- [ ] Publishing a Release from `prod` applies `infra/environments/production`
  only after the `production` environment's approval; the site is reachable
  at the custom domain over HTTPS
- [ ] One Web ACL (created in `us-east-1`) is associated with both
  distributions (verify in console — not two ACLs)
- [ ] The ACM certificate for production is confirmed issued from
  `us-east-1`, all other resources from `ap-southeast-2`
- [ ] The manual "destroy dev" workflow successfully tears dev down, and a
  subsequent push to `dev` rebuilds it cleanly (round-trip test — this is
  the real proof that "tear down whenever" actually works)
- [ ] No Bedrock, Lambda-with-logic, Cognito, or app-data DynamoDB resources
  exist anywhere in this PR's diff

## Open questions (need your answer before or during this PR)

1. Domain name and registrar — already registered, and where (Route 53 or
   external)?
2. Single AWS account for everything, or a separate account/OU?
3. Are you fine with dev living only at its `*.cloudfront.net` URL (no
   subdomain), or would you rather it get something like `dev.yourdomain.com`
   for a closer-to-production feel? (Recommendation above assumes no
   subdomain, for simplicity and destroy/rebuild speed.)

## Suggested brief to hand Copilot

See the standalone prompt below — this file is what it points back to for
detail.
