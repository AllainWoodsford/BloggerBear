# Phase 0 — Foundations: Build Spec

Status: tracked in `docs/PROGRESS.md` under "Phase 0 — Foundations."
Source of truth for rationale/constraints: `docs/project-plan.md`.

## Objective

Stand up the deployable skeleton — state backend, a placeholder static site,
edge security, and DNS/TLS — with **zero** application logic (no Lambda
business logic, no Bedrock calls, no DynamoDB app tables). The goal is a
`terraform apply` that succeeds end-to-end and a domain that serves a
placeholder page over HTTPS, so every IAM/networking/CI problem gets solved
now, in isolation, rather than later while also debugging a first Bedrock
call.

## Fix first (blocking, not part of the Terraform work itself)

1. **Default branch**: rename `master` → `main` in GitHub repo settings.
   `terraform.yml`'s apply job and both workflows' push triggers already
   assume `main`; as of today that ref doesn't exist, so the apply job can
   never fire. Do this before merging any Phase 0 PR, or the PR will merge
   into `master` and CI still won't apply anything.
2. **GitHub Environment**: create a `production` environment in repo
   Settings → Environments, with at least one required reviewer (you). This
   is what actually gates `terraform apply` on a human approval — without it
   existing, the workflow's `environment: production` reference either fails
   or (depending on GitHub's defaults) provides no real gate at all. Given
   this project's Bedrock/AWS spend risk, this gate should not be skipped.
3. Confirm the **prerequisites** in `docs/PROGRESS.md` are done — root MFA,
   admin IAM user, AWS Budget alarm, Bedrock model access requested, region
   picked. None of these are Terraform's job; Terraform should not be applied
   before the Budget alarm exists.

## Scope

**In scope:**
- `infra/bootstrap` — Terraform state backend (S3 bucket + DynamoDB lock
  table), applied manually/locally, one time, never through CI
- `infra/environments/production` — the first (and for now only) environment
- Placeholder static site: S3 bucket (private, OAC-only access) + CloudFront
  distribution
- WAF Web ACL attached to the CloudFront distribution
- Route 53 hosted zone (or use of an existing one) + ACM certificate + domain
  wired to CloudFront
- Confirming the existing `terraform.yml` / `security.yml` workflows actually
  exercise this new code (they already look at `infra/**`, so no workflow
  changes should be needed beyond the branch rename above)

**Explicitly out of scope (Phase 1+):**
- Any Lambda function with real logic (research tick, ideation, draft,
  compliance review, publish, admin API)
- `Topics` / `Findings` / `CandidateIdeas` / `Articles` / any app DynamoDB
  table
- Bedrock IAM permissions or model invocation of any kind
- Cognito / admin auth
- Anything under `frontend/` beyond a single static placeholder page

## Directory layout

```
infra/
├── bootstrap/              # one-time, applied locally, not via CI
│   ├── main.tf             # S3 state bucket + DynamoDB lock table
│   ├── variables.tf
│   └── outputs.tf           # bucket name / table name, for the backend block below
├── modules/
│   └── static-site/        # reusable: S3 + CloudFront + OAC + WAF + ACM + Route53
│       ├── main.tf
│       ├── variables.tf
│       └── outputs.tf
└── environments/
    └── production/
        ├── main.tf          # backend "s3" {...} pointing at bootstrap's outputs
        ├── main.tf          # calls modules/static-site with prod-specific vars
        ├── variables.tf
        └── terraform.tfvars # domain name, region, etc. — no secrets
```

Keep `infra/modules/static-site` genuinely reusable (parameterize domain name,
bucket name, WAF rule set) — Phase 4's frontend polish and any future
non-production environment should be able to reuse it without edits.

## Component detail

**State backend (`infra/bootstrap`)**
- S3 bucket: versioning enabled, default encryption (SSE-S3 or SSE-KMS),
  block-all-public-access, no lifecycle deletion of state.
- DynamoDB table: single string key (`LockID`), on-demand billing (no
  provisioned throughput to manage for something this low-traffic).
- Output the bucket name and table name so `infra/environments/production`'s
  `backend "s3" { ... }` block can reference them (Terraform doesn't allow
  variables in a backend block, so these get hardcoded into that file after
  bootstrap runs — note that explicitly in the PR so it isn't mistaken for
  an oversight).

**Static site**
- S3 bucket for site content: private, no public access, read access granted
  only via CloudFront's Origin Access Control (OAC — not the older OAI,
  which is deprecated).
- A single placeholder `index.html` (can be a literal "BloggerBear — coming
  soon" page) is enough; Phase 4 replaces this with the real frontend.

**CloudFront + WAF**
- CloudFront distribution in front of the S3 origin, HTTPS-only viewer
  policy, using the ACM certificate below.
- WAF Web ACL (must be created in `us-east-1` — CloudFront-scoped WAF is
  global-only there regardless of your chosen deployment region) with:
  - a rate-based rule (block or challenge IPs exceeding N requests / 5 min —
    pick a starting N generously high; tighten later per Phase 6)
  - AWS Managed Rule Groups: Core Rule Set, Known Bad Inputs, IP Reputation
    List
- This automatically brings AWS Shield Standard along at no extra cost
  (it's implicit with CloudFront, not a separate resource to create).

**DNS/TLS**
- Route 53 hosted zone for the domain (create if it doesn't already exist;
  if the domain's registered elsewhere, just the hosted zone + NS delegation
  is needed here).
- ACM certificate for the domain, **requested in `us-east-1` specifically**
  — CloudFront only accepts viewer certificates from that region regardless
  of where everything else is deployed. This is the single most common
  mistake in this exact setup; call it out in the PR description so it isn't
  silently gotten wrong.
- Route 53 alias record pointing the domain at the CloudFront distribution.

## CI expectations

- `terraform.yml`'s `plan` job should run clean (`fmt -check`, `validate`,
  `plan`) against `infra/environments/production` on every PR touching
  `infra/**`.
- `terraform.yml`'s `apply` job should only fire on push to `main` (post
  branch-rename) and should sit behind the `production` environment's
  required-reviewer gate.
- `security.yml`'s Trivy IaC scan already targets `infra/` (excluding
  `infra/bootstrap`) — confirm this Phase 0 code passes at HIGH/CRITICAL
  severity with no exceptions needed. If something has to be excepted,
  say so explicitly in the PR rather than quietly loosening the workflow.

## Acceptance criteria (Definition of Done)

- [ ] `terraform apply` in `infra/bootstrap` has been run once, locally, and
  its state bucket/lock table exist
- [ ] `infra/environments/production` has a working `backend "s3"` block
  pointing at that bucket/table
- [ ] A PR touching `infra/**` produces a clean `terraform plan` in CI
- [ ] Merging to `main` triggers the `apply` job, which pauses for the
  `production` environment's approval, then applies successfully
- [ ] The custom domain resolves and serves the placeholder page over HTTPS
  with a valid certificate
- [ ] WAF Web ACL is attached and visible in the AWS console against the
  CloudFront distribution
- [ ] No Bedrock, Lambda-with-logic, Cognito, or app-data DynamoDB resources
  exist anywhere in this PR's diff

## Open questions (need your answer before or during this PR)

1. Domain name and registrar — is it already registered, and where (Route 53
   or external)?
2. Region for everything non-CloudFront/non-WAF-global (the plan suggests
   `us-east-1` for broadest Bedrock model availability — confirming this now
   avoids a re-deploy later when Phase 1 adds Bedrock calls).
3. Single AWS account for everything, or a separate account/OU for this
   project (affects nothing in Phase 0 itself, but affects how `aws
   configure` / CI credentials get set up).

## Suggested brief to hand Copilot

> Implement Phase 0 per `docs/specs/phase-0-foundations.md`. Scope is
> `infra/bootstrap` (state backend, applied manually — do not wire into CI)
> and `infra/environments/production` (placeholder S3+CloudFront static
> site, WAF Web ACL, Route 53 + ACM). No Lambda logic, no Bedrock, no
> DynamoDB app tables — those are Phase 1, tracked separately in
> `docs/PROGRESS.md`. Follow `docs/project-plan.md` §2 constraints. Flag
> explicitly in the PR description: the ACM certificate must be requested in
> `us-east-1` regardless of the deployment region, and the `infra/bootstrap`
> outputs must be hand-copied into the `production` environment's backend
> block since Terraform backend blocks can't take variables.
