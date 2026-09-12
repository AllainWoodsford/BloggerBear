---
doc: progress-tracker
schema_version: 1
last_updated: 2026-09-12
source_of_truth: docs/project-plan.md
phases:
  - id: phase-0
    name: Foundations
    status: in_progress
  - id: phase-1
    name: First adapter + manual pipeline
    status: not_started
  - id: phase-2
    name: Admin console
    status: not_started
  - id: phase-3
    name: Automation (scheduler + Step Functions)
    status: not_started
  - id: phase-4
    name: Public frontend polish
    status: not_started
  - id: phase-5
    name: Feedback loop
    status: not_started
  - id: phase-6
    name: Observability & hardening
    status: not_started
  - id: phase-7
    name: Second & third adapters
    status: not_started
  - id: phase-8
    name: Stretch
    status: not_started
---

# BloggerBear — Progress Tracker

This is a living file. Edit it directly whenever the roadmap changes — add
items under a phase, split a phase into more phases, rewrite acceptance
criteria, add a new section entirely. It is not generated from anything else;
this file *is* the plan-in-progress.

**Status values** (used in the YAML front matter above, one per phase):
`not_started` · `in_progress` · `blocked` · `done`

**How to use this with an agent (Claude or Copilot):**
1. Pick the next unchecked phase or item below.
2. Ask Claude (this Project) to turn it into a build spec if one doesn't
   already exist under `docs/specs/`.
3. Hand the spec to Copilot as an issue/PR brief.
4. When Copilot's PR merges, tick the corresponding `- [ ]` boxes below to
   `- [x]` **in that same PR**, and update the phase's `status` in the YAML
   front matter if the phase is now fully/partially done. This keeps the
   human-readable checklist and the machine-readable front matter in sync as
   one commit, so neither drifts from the actual repo state.

Full rationale, architecture, and constraints for everything below live in
`docs/project-plan.md` — treat that as the source of truth; this file only
tracks *what's built vs. not*.

---

## ⚠️ Known issues (fix before/with Phase 0)

- [ ] **Branch mismatch**: `.github/workflows/terraform.yml` and
  `security.yml` both gate on `refs/heads/main`, but the repo's default
  branch is `master`. As written, the `apply` job will never run. Rename the
  default branch to `main` (GitHub: Settings → Branches → rename, one click,
  PRs auto-update) — simplest fix, don't edit the workflows to chase `master`.
- [ ] **GitHub Environment missing**: `terraform.yml`'s apply job targets the
  `production` environment for its approval gate. That environment doesn't
  exist yet in the repo's Settings → Environments — until it's created (with
  at least one required reviewer, ideally you), the apply job either fails
  or, worse, has no real approval gate at all.
- [ ] `docs/project-plan.md` in this repo is a condensed rewrite that has
  drifted from the full spec (this Project's copy) — missing the financial-
  topic compliance detail (§4.4), the feedback PII-scrub mechanics (§7.2),
  and phases 3–8. Reconcile these into one document before Copilot leans on
  the condensed version for anything past Phase 1.

---

## Prerequisites (manual, one-time, blocking)

- [ ] AWS root user MFA enabled; root no longer used day-to-day
- [ ] IAM (or IAM Identity Center) admin user created for this project, MFA on
- [ ] AWS Budget alarm set (e.g. $10 / $25 / $50) — **before** anything deploys
- [ ] Bedrock model access requested in-console for the Claude models needed
  (manual, per-model, per-region, can sit pending — start this early)
- [ ] AWS region chosen (plan suggests `us-east-1` for broadest Bedrock access
  if latency isn't a constraint)
- [ ] AWS CLI v2 installed locally, `aws configure` / `aws configure sso` run

---

## Phase 0 — Foundations

Spec: `docs/specs/phase-0-foundations.md`

- [ ] Known issues above resolved (branch rename, `production` environment)
- [ ] `infra/bootstrap`: Terraform state S3 bucket + DynamoDB lock table,
  applied manually/locally (never through CI)
- [ ] `infra/environments/production`: placeholder static site (S3 +
  CloudFront + Origin Access Control)
- [ ] WAF Web ACL attached to CloudFront: rate-based rule + AWS Managed Rule
  Groups (Core Rule Set, Known Bad Inputs, IP Reputation List)
- [ ] Route 53 hosted zone + ACM certificate (requested in `us-east-1`
  regardless of deployment region) + custom domain wired to CloudFront
- [ ] `terraform fmt/validate/plan` clean in CI for the new environment
- [ ] `terraform apply` succeeds end-to-end; placeholder site reachable over
  HTTPS at the custom domain

## Phase 1 — First adapter + manual pipeline

- [ ] Adapter contract implemented: `fetch_state`, `material_diff`,
  `source_refs` (per §6 of the plan)
- [ ] First adapter built — simplest data source first (GitHub Trending is
  the obvious pick: no auth, no rate-limit pain, no compliance sensitivity)
- [ ] Hourly research-tick Lambda: diff-first, only calls Bedrock on material
  change, writes to `Findings` (rolling TTL 7–14 days)
- [ ] DynamoDB tables: `Topics`, `Findings`, `CandidateIdeas`, `Articles`
- [ ] S3 storage for article bodies + raw source snapshots
- [ ] Daily cycle (manually triggered for now): ideation → selection → draft
  → compliance review → publish
- [ ] At least one manually-triggered end-to-end run produces an article
  you'd actually be willing to publish

## Phase 2 — Admin console

- [ ] Admin API, separate from public API, authenticated (Cognito or IAM +
  WAF IP allowlist)
- [ ] Topic CRUD UI, including adapter selection
- [ ] Manual "trigger a run" button per topic
- [ ] Moderation queue UI: approve / reject flagged drafts
- [ ] "Candidates considered but not published" view (from `CandidateIdeas`)

## Phase 3 — Automation

- [ ] EventBridge Scheduler per topic: hourly tick + daily cycle, each on its
  own configured cadence
- [ ] Step Functions state machine: Research → Draft → Review → Publish, with
  retries and a DLQ
- [ ] Manual-trigger-only dependency removed — topics run unattended

## Phase 4 — Public frontend polish

- [ ] Per-topic nav entries, auto-updating as topics are added
- [ ] Article pages with sources footer (URL + title + accessed date)
- [ ] Public, anonymous view counters per article
- [ ] Site-wide RSS feed

## Phase 5 — Feedback loop

- [ ] Thumbs up/down on articles, no identity attached
- [ ] Optional free-text comment on a vote
- [ ] PII-scrub Lambda: regex pass, then Bedrock pass, before anything is
  stored; raw text never persisted, even transiently
- [ ] `Feedback` table with no requester identifier of any kind (IP logging,
  if any, stays in infra logs only — never joined to app data)
- [ ] Weekly reflection job proposes prompt edits into `PromptRefinements`
  (status `pending`) — admin must approve before they take effect
- [ ] Highly-upvoted articles reusable as few-shot examples in future drafts

## Phase 6 — Observability & hardening

- [ ] CloudWatch dashboards/alarms for pipeline health
- [ ] Cost/budget alarms specifically watching Bedrock spend
- [ ] WAF rule tuning based on real traffic patterns
- [ ] Prompt iteration on the compliance-review step based on what's actually
  been flagged so far

## Phase 7 — Second & third adapters

- [ ] Second adapter added (whichever domain wasn't picked for Phase 1)
- [ ] Third adapter added — crypto, with the stricter compliance rubric from
  §4.4: no recommendation language, standing "not financial advice"
  disclaimer, always routed to manual moderation regardless of confidence
- [ ] Confirms the adapter pattern actually required zero changes to core
  pipeline logic — the thing worth saying out loud in an interview

## Phase 8 — Stretch

- [ ] Cross-topic "trending everywhere" digest
- [ ] Public read API / RSS so other tools can consume output via API
  instead of scraping it

---

## Backlog / not yet scheduled

*(Freeform — drop ideas here as they occur to you; promote them into a
phase above, or a new phase, whenever you're ready to schedule them.)*

-
