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

> ⚠️ **Every `us-east-1` mention anywhere in this file is intentional** — it
> refers only to the CloudFront-scope WAF Web ACL and ACM certificate, which
> AWS requires in `us-east-1` regardless of hosting region. Everything else
> is `ap-southeast-2`. Do not "fix" these.

## Branch & Release Model

Full detail in `docs/specs/phase-0-foundations.md`. Summary, since this
governs how every phase from here on ships:

- `dev` (default branch) — feature branches fork from here, PR back in.
  Merging to `dev` auto-deploys `infra/environments/dev`. No approval gate —
  it's meant to be broken and torn down freely.
- `prod` — promoted from `dev` via PR when you're happy with it. Merging
  into `prod` does **not** deploy by itself.
- A production deploy only happens when you publish a GitHub Release
  (tagged, e.g. `v0.1.0`) from `prod` — gated by the `production`
  environment's required-reviewer approval. A new release replaces whatever
  was previously deployed, since both target the same Terraform state.
- Direct pushes to `dev` or `prod` are blocked by branch protection —
  everything is task branch → PR → review → merge, always.

---

## ⚠️ Known issues (fix with Phase 0)

- [ ] Rename default branch `master` → `dev`; create `prod` from its tip
- [ ] Branch protection on `dev` and `prod`: PR required, `terraform` +
  `security` checks required, no direct pushes
- [ ] GitHub Environments: `dev` (no protection) and `production` (required
  reviewer) — the latter is what actually gates a production release
- [ ] `docs/project-plan.md` in this repo is a condensed rewrite that had
  drifted from the full spec (this Project's copy) — missing the financial-
  topic compliance detail (§4.4), the feedback PII-scrub mechanics (§7.2),
  and phases 3–8. Its CI/CD section (§8) has now been updated to match the
  branch/release model above; the rest of the reconciliation is still open.

---

## Prerequisites (manual, one-time, blocking)

- [ ] AWS root user MFA enabled; root no longer used day-to-day
- [ ] IAM (or IAM Identity Center) admin user created for this project, MFA on
- [ ] AWS Budget alarm set (e.g. $10 / $25 / $50) — **before** anything deploys
- [ ] Bedrock model access requested in-console for the Claude models needed,
  **in `ap-southeast-2`** (manual, per-model, per-region, can sit pending —
  start this early; before Phase 1, confirm which specific model IDs are
  directly invokable there vs. need a cross-region inference profile)
- [x] AWS region decided: **`ap-southeast-2` (Sydney)** — fixed, not still
  open. Exception: the CloudFront-scope WAF ACL and ACM certificate must
  still be created in `us-east-1` (AWS platform constraint, see
  `docs/specs/phase-0-foundations.md` → "Region")
- [ ] AWS CLI v2 installed locally, `aws configure` / `aws configure sso` run

---

## Phase 0 — Foundations

Spec: `docs/specs/phase-0-foundations.md`

- [ ] Known issues above resolved (branches, protections, environments) —
  default branch rename to `dev` is done; `prod` branch, branch protection,
  and GitHub Environments are still manual/pending (see PR #TBD)
- [x] CI's pinned Terraform version bumped from `1.9.8` to ≥1.10.0 (required
  for native S3 locking below) — bumped to `1.16.2`
- [x] `infra/bootstrap`: Terraform state S3 bucket **only** — no DynamoDB
  table; locking is native S3 (`use_lockfile = true`) — code complete; the
  one-time local apply is still a manual step for the human
- [x] `infra/modules/static-site`: reusable module (S3 + CloudFront + OAC),
  parameterized for `enable_custom_domain` and `force_destroy`
- [x] `infra/environments/dev`: region `ap-southeast-2`, no custom domain,
  dev bucket `force_destroy = true`
- [x] `infra/environments/production`: region `ap-southeast-2`, custom
  domain via Route 53 + ACM (ACM cert in `us-east-1` — CloudFront
  requirement, not a region change) — domain/hosted zone values are still
  TODO in `terraform.tfvars` pending the open domain-registrar question
- [ ] One WAF Web ACL (created in `us-east-1`), associated with **both**
  distributions (not two ACLs) — production creates the ACL; wiring dev's
  distribution to the same ACL ARN is a manual `terraform.tfvars` edit
  after production's first apply (see `infra/environments/dev/variables.tf`)
- [x] Dev workflow: auto-apply on push to `dev`, no approval
- [x] Production workflow: apply only on Release published from `prod`,
  gated by `production` environment approval — workflow is wired up;
  actually gating requires the human to create the `production` GitHub
  Environment with a required reviewer (see known issues above)
- [x] Concurrency groups on both apply paths (queue, don't race)
- [x] `workflow_dispatch` "destroy dev" workflow
- [ ] Round-trip proven: destroy dev, rebuild it via a push, confirm it
  comes back clean — requires a real AWS deploy, not achievable from a PR

## Phase 1 — First adapter + manual pipeline

- [x] Adapter contract implemented: `fetch_state`, `material_diff`,
  `source_refs` (per §6 of the plan) — `lambdas/common/adapters/base.py`
- [x] First adapter built — simplest data source first (GitHub Trending is
  the obvious pick: no auth, no rate-limit pain, no compliance sensitivity)
  — `lambdas/common/adapters/github_trending.py`
- [x] Hourly research-tick Lambda: diff-first, only calls Bedrock on material
  change, writes to `Findings` (rolling TTL 7–14 days) —
  `lambdas/research_tick_handler.py`; code complete, not yet deployed (see
  manual follow-ups in the PR)
- [x] DynamoDB tables: `Topics`, `Findings`, `CandidateIdeas`, `Articles` —
  plus `ModerationQueue` (required by the "route to moderation" scope line
  below, per the data model in §5) — `infra/modules/app-data/`
- [x] S3 storage for article bodies + raw source snapshots — one private
  `bloggerbear-<env>-content` bucket, `articles/` and `snapshots/` prefixes
- [x] Daily cycle (manually triggered for now): ideation → selection → draft
  → compliance review → publish — `lambdas/daily_cycle_handler.py`; code
  complete, not yet deployed
- [ ] At least one manually-triggered end-to-end run produces an article
  you'd actually be willing to publish — blocked on real AWS: `infra/bootstrap`
  hasn't been re-applied with Phase 1's IAM changes, `BEDROCK_MODEL_ID` is
  still an empty TODO in both `terraform.tfvars` pending model-access
  confirmation, and no `Topics` item has been seeded yet

## Phase 2 — Admin console

- [x] Admin API, separate from public API, authenticated (Cognito or IAM +
  WAF IP allowlist) — went with **IAM (SigV4) + a regional WAF IP allowlist**
  rather than Cognito (simpler/cheaper for a single-operator project, and
  explicitly sanctioned as an equal alternative here); code complete, not
  yet deployed — `admin_allowed_cidrs` is still an empty-list TODO in both
  `terraform.tfvars`, which fails closed (nothing can reach the API) until
  set to the operator's real IP
- [x] Topic CRUD UI, including adapter selection — implemented as a local
  operator CLI (`scripts/admin_cli.py`), not a browser app, since IAM auth
  from a browser would otherwise need Cognito Identity Pool federation
  anyway; `topics create/get/update/delete` cover CRUD, `adapter`/
  `adapter_config` are free-form CLI args so any adapter key works
- [x] Manual "trigger a run" button per topic — `admin_cli.py topics
  trigger <id> --pipeline {research_tick,daily_cycle}`, async Lambda invoke
- [x] Moderation queue UI: approve / reject flagged drafts —
  `admin_cli.py moderation list/approve/reject`
- [x] "Candidates considered but not published" view (from `CandidateIdeas`)
  — `admin_cli.py topics candidates <id>`, returns every candidate
  regardless of status so rejected/unselected angles are visible too

## Phase 3 — Automation

- [x] EventBridge Scheduler per topic: hourly tick + daily cycle, each on its
  own configured cadence — created/updated/deleted dynamically by
  `admin_api_handler.py` (`lambdas/common/scheduler.py`) at topic
  create/update/delete time, since topics are runtime data Terraform can't
  enumerate; code complete, not yet deployed
- [x] Step Functions state machine: Research → Draft → Review → Publish, with
  retries and a DLQ — **scoped down**: wraps the existing single
  `daily_cycle_handler` Lambda (already the full ideate→select→draft→
  review→publish chain from Phase 1) in one Task state with retries + a
  Catch→SQS dead-letter queue, rather than splitting it into four
  separately-orchestrated Lambdas, since that would be a large rewrite of
  working Phase 1 code for limited benefit on a single-operator project.
  The hourly research tick bypasses Step Functions entirely (EventBridge
  Scheduler invokes it directly) — it's a single self-contained operation
  with nothing to orchestrate.
- [x] Manual-trigger-only dependency removed — topics run unattended — once
  deployed, every topic created via the Admin API gets its own schedules;
  the CLI's manual `trigger` command still exists for on-demand runs but is
  no longer the only path

## Phase 4 — Public frontend polish

- [x] Per-topic nav entries, auto-updating as topics are added — the static
  frontend (`frontend/app.js`) fetches `GET /topics` from a new
  unauthenticated public API at load time rather than baking nav into the
  static build, so a topic created via the admin CLI appears without any
  redeploy; code complete, not yet deployed
- [x] Article pages with sources footer (URL + title + accessed date) —
  `frontend/app.js`'s article view renders `source_refs` as a footer list
- [x] Public, anonymous view counters per article — `POST
  /articles/{id}/view` atomically increments a `view_count` attribute
  (DynamoDB `ADD`, no read-modify-write race); the public API Gateway's
  WAF Web ACL rate-limits (500 req/5min/IP) rather than blocking by
  default, unlike Phase 2's admin ACL, since this one must stay reachable
  by anonymous visitors
- [x] Site-wide RSS feed — `GET /rss.xml` on the same public API, hand-built
  valid RSS 2.0 (50 most recent published articles, XML-escaped)
- [x] Legal pages, accessibility, and CSS resilience (ad hoc follow-up,
  not originally scoped) — `#/terms` and `#/privacy` hash routes
  (static content, no API call), grounded in what the codebase actually
  does rather than boilerplate (confirmed via a real code/config scan:
  no cookies anywhere — the CloudFront distribution explicitly forwards
  none, and the frontend has no cookie-setting code — anonymous view
  counts, and feedback that goes through the Phase 5 two-pass PII
  redaction before storage). A separate `.legal-nav` (footer) links to
  both. An honest, in-flow "site notice" banner (not a cookie-consent
  banner — there's nothing to consent to) tells visitors about the
  anonymous view counting and WAF-level security logging, dismissal
  remembered via `localStorage` (the one and only thing this site
  stores client-side). A placeholder square SVG logo
  (`frontend/logo.svg`). Accessibility: a skip link (WCAG 2.4.1),
  focus moved to `#content` on route change (WCAG 2.4.3), a real WCAG
  AA contrast bug caught and fixed before shipping (white text on the
  dark-mode accent blue was ~2.4:1, well under the 4.5:1 minimum —
  both the skip link and the notice's dismiss button now use the
  already-high-contrast `--fg`/`--bg` pair instead), and a ≥44px
  dismiss-button target size. `normalize.css` (vendored, official
  v8.0.1, unmodified) now loads before `styles.css` as a baseline, so a
  failed `styles.css` load still leaves the page in a consistent,
  readable cross-browser state rather than raw unstyled HTML; `body`'s
  background/color also carry static fallback values ahead of their
  `var()` versions for the same reason. Verified with a real functional
  test (jsdom, actual `index.html`/`app.js`, real HTTP fetches against a
  local static server) rather than just visual inspection — 22/22
  checks covering routing, rendering, focus management, and the
  notice's localStorage persistence across a simulated return visit.

## Phase 5 — Feedback loop

- [x] Thumbs up/down on articles, no identity attached —
  `frontend/app.js`'s `renderFeedback` posts `{vote: "up"|"down"}` to
  `POST /articles/{article_id}/feedback` (public API,
  `public_api_handler.py`'s `_submit_feedback`); code complete, not yet
  deployed
- [x] Optional free-text comment on a vote — same route, `comment` field is
  optional and defaults to `null`
- [x] PII-scrub Lambda: regex pass, then Bedrock pass, before anything is
  stored; raw text never persisted, even transiently —
  `common/compliance.py`'s `regex_redact` (reused from Phase 1) then
  `bedrock_redact_review`, both run in `_submit_feedback` before
  `put_feedback` is ever called; a `REJECT`/ambiguous Bedrock response fails
  closed to `comment = None` rather than storing anything
- [x] `Feedback` table with no requester identifier of any kind (IP logging,
  if any, stays in infra logs only — never joined to app data) —
  `infra/modules/app-data/main.tf`'s `aws_dynamodb_table.feedback`
  (PK `article_id` / SK `feedback_id`); `common/dynamo.py`'s `put_feedback`
  signature has no IP/user-agent/session parameter at all
- [x] Weekly reflection job proposes prompt edits into `PromptRefinements`
  (status `pending`) — admin must approve before they take effect —
  `weekly_reflection_handler.py` on a static weekly EventBridge Scheduler
  cron; `admin_api_handler.py` + `scripts/admin_cli.py`'s `refinements
  approve/reject` gate whether `daily_cycle_handler.py` ever picks one up
  (`get_latest_approved_prompt_refinement`, status must be `approved`)
- [x] Highly-upvoted articles reusable as few-shot examples in future drafts
  — `common/dynamo.py`'s `get_top_voted_articles` (net-positive `net_votes`
  only) feeds a short excerpt into `daily_cycle_handler.py`'s draft prompt

All six items above: code complete (143 lambda tests passing, ruff clean,
`bandit -r lambdas/ --severity-level high --confidence-level high` clean,
`terraform fmt`/`validate` clean on bootstrap/dev/production), not yet
deployed to real AWS. Fixed post-first-pass: `admin_api_routes` /
`public_api_routes` in both `infra/environments/dev/main.tf` and
`infra/environments/production/main.tf` were missing the new
`/prompt-refinements` and `/articles/{id}/feedback` routes, which would
have 404'd at the API Gateway layer despite the Lambda handlers supporting
them.

## Phase 6 — Observability & hardening

- [x] CloudWatch dashboards/alarms for pipeline health —
  `infra/modules/observability` (new, reusable like app-data/static-site):
  an Errors + a Throttles alarm per pipeline Lambda (all 5, via
  `for_each`), a DLQ-depth alarm, a Step Functions `ExecutionsFailed`
  alarm, one SNS topic all of them publish to, and a dashboard
  summarizing all of it. Wired into both `infra/environments/dev` and
  `production` as `module.observability`; code complete, not yet
  deployed. A human still needs to set `var.alert_email` in
  `terraform.tfvars` and confirm the SNS subscription email before
  anyone actually gets paged — alarms fire either way, but silently,
  until then
- [x] Cost/budget alarms specifically watching Bedrock spend —
  `infra/bootstrap`'s new `aws_budgets_budget.bedrock_spend`
  (account-level, so it lives in bootstrap alongside the other one-time
  resources, not per-environment), filtered to the "Amazon Bedrock"
  service, notifying at 80% actual / 100% forecasted. Gated on
  `var.budget_alert_email` (empty by default → no budget resource is
  created at all, matching this project's fail-closed-by-omission
  pattern) — set it before the next bootstrap apply. This is in addition
  to the general account-wide AWS Budget alarm already listed as a manual
  prerequisite above
- [ ] WAF rule tuning based on real traffic patterns — the tuning itself
  (adjusting thresholds/rules from observed traffic) can't be done
  without real traffic and stays open until some exists. What's done:
  added the AWS Managed Common Rule Set to both public API WAF ACLs
  (dev + production, previously rate-limit-only) and enabled WAF logging
  to CloudWatch Logs on every ACL in both environments (admin, public
  API, and production's shared CLOUDFRONT-scope ACL), so the data needed
  to actually tune the rate-limit threshold and rule set will exist once
  deployed
- [ ] Prompt iteration on the compliance-review step based on what's
  actually been flagged so far — the prompt edit itself needs real
  flagged data and stays open. What's done: added
  `GET /moderation-queue/stats` (admin API, IAM-authenticated) and
  `admin_cli.py moderation stats`, which summarize ModerationQueue
  history (`common/dynamo.py`'s `list_all_moderation_items`) — total
  flagged, breakdown by status/topic, a `reason_counts` tally, and the 20
  most recent flagged items with their full reasons. This is the
  visibility a human needs to responsibly edit
  `common/compliance.py`'s `_REVIEW_PROMPT_TEMPLATE`; the actual prompt
  edit stays manual and requires real flagged data, same as the WAF item
  above

## Phase 7 — Second & third adapters

- [x] Second adapter added (whichever domain wasn't picked for Phase 1) —
  Hacker News top stories via the official public Firebase-backed API
  (no auth, no compliance sensitivity, same low-risk profile as Phase 1's
  GitHub Trending pick but a different fetch shape — many small JSON
  requests instead of one HTML page, which is the point: it proves the
  adapter contract isn't secretly HTML-scrape-shaped) —
  `lambdas/common/adapters/hacker_news.py`, registered as `hacker_news`
- [x] Third adapter added — crypto, with the stricter compliance rubric:
  no recommendation language, standing "not financial advice" disclaimer,
  always routed to manual moderation regardless of confidence —
  `lambdas/common/adapters/crypto_feed.py` (public CoinGecko market-data
  API, no auth), registered as `crypto_feed`. The rubric itself lives
  entirely outside the adapter (by design, see the module's docstring):
  `admin_api_handler.py` now forces `is_financial = True` on any topic
  using this adapter, on both create and update, so the flag can never be
  forgotten or unset by mistake; `common/compliance.py` gained
  `FINANCIAL_DRAFTING_GUIDANCE` (folded into `daily_cycle_handler.py`'s
  ideation/draft prompts for any financial topic) and
  `append_financial_disclaimer` (deterministically appended to the stored
  draft body, not left to the model to remember). The unconditional
  manual-moderation routing itself was already in place since Phase 1
  (`compliance.review_draft`'s `is_financial_topic` short-circuit) — this
  phase adds the drafting-side guidance/disclaimer on top of it
- [x] Confirms the adapter pattern actually required zero changes to core
  pipeline logic — the thing worth saying out loud in an interview.
  `research_tick_handler.py`'s flow (`handler`/`_run_daily_cycle`-style
  logic) is untouched; adding both new domains was exactly "one new
  adapter module + one new `ADAPTER_REGISTRY` line" each, now asserted
  directly by `test_adapter_registry_has_all_three_phase_7_adapters`.
  (The financial-topic guidance/disclaimer additions above touch
  `daily_cycle_handler.py`, but that's the pre-existing, adapter-agnostic
  `is_financial` flag mechanism — not a per-adapter branch — so it
  doesn't count against this claim)

## Phase 8 — Stretch

- [x] Cross-topic "trending everywhere" digest —
  `lambdas/trending_digest_handler.py`, a sixth pipeline Lambda on its
  own static daily EventBridge Scheduler cron (same "one global job, not
  per-topic" pattern as Phase 5's weekly reflection — see
  `aws_scheduler_schedule.trending_digest`). Pulls every topic's latest
  Finding (skipping any older than 48h — nothing to contribute right
  now), asks Bedrock to synthesize one short digest across all of them
  (calling out genuine cross-topic connections where they exist), then
  runs it through the exact same `compliance.review_draft` gate as any
  other draft — routed to manual moderation unconditionally if ANY
  contributing topic is financial, same "regardless of confidence" rule
  a financial-topic draft gets. Published as a normal Articles item under
  a synthetic `topic_id="digest"`, so it shows up through the *existing*
  public API/RSS/frontend (`GET /articles?topic_id=digest`, `GET
  /rss.xml`) with zero new API routes. `frontend/app.js`/`styles.css`
  gained a static "Trending Everywhere" nav link (the digest isn't a real
  Topic — no adapter, no cadence — so it never comes back from `GET
  /topics` and needs its own entry) pointing at the same `#/topic/{id}`
  route every other topic already uses. Code complete, not yet deployed
- [x] Public read API / RSS so other tools can consume output via API
  instead of scraping it — already fully satisfied by Phase 4's public
  API: `GET /topics`, `GET /articles`, `GET /articles/{article_id}`, and
  `GET /rss.xml` are all public, unauthenticated, and structured
  (JSON/RSS 2.0) specifically so other tools can consume them
  programmatically instead of scraping the site. No new work needed here
  — noting it explicitly since this phase's own wording could otherwise
  read as a duplicate ask

---

## Cost control & tapering

Called out separately because it cuts across every phase, not just one:

- AWS Budget alarm (see Prerequisites) is the tripwire — set it before
  anything deploys, not after.
- WAF's flat monthly fee is the main "always-on" cost per the plan's own
  cost section (§9) — Phase 0 shares one Web ACL across dev and production
  rather than paying for two.
- Dev has no custom domain/DNS, only the `*.cloudfront.net` URL — smaller
  footprint, faster to destroy and rebuild, one less thing to pay for.
- **If it needs to taper rather than stop outright:** once Phase 3's
  schedules exist, disabling an EventBridge rule (or a single topic) is
  free and instantly reversible — a much smaller step than tearing anything
  down. Cutting the priciest topic (crypto/financial ones likely call
  Bedrock most, per §9) or lowering a topic's research/publish cadence are
  both good middle-ground moves before a full kill.
- **If it needs to stop outright:** `terraform destroy` on dev, then
  production, in that order (the `workflow_dispatch` destroy job from
  Phase 0 covers dev; production would need the equivalent run manually or
  a similar dispatch job added when you're ready to consider it). Decide
  up front whether to release the domain/hosted zone or keep it parked —
  that's the one piece that isn't free to walk away from and re-acquire.

---

## Backlog / not yet scheduled

*(Freeform — drop ideas here as they occur to you; promote them into a
phase above, or a new phase, whenever you're ready to schedule them.)*

-
