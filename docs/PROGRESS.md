---
doc: progress-tracker
schema_version: 1
last_updated: 2026-10-09
source_of_truth: docs/project-plan.md
phases:
  - id: phase-0
    name: Foundations
    status: in_progress
  - id: phase-1
    name: First adapter + manual pipeline
    status: done
  - id: phase-2
    name: Admin console
    status: done
  - id: phase-3
    name: Automation (scheduler + Step Functions)
    status: done
  - id: phase-4
    name: Public frontend polish
    status: done
  - id: phase-5
    name: Feedback loop
    status: done
  - id: phase-6
    name: Observability & hardening
    status: in_progress
  - id: phase-7
    name: Second & third adapters
    status: done
  - id: phase-8
    name: Stretch
    status: done
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

## Current status (2026-10-09)

- **Live:** production at bloggerbear.com with four topics (GitHub Trending,
  crypto, tech market news, World of Warcraft) plus the daily Trending
  Everywhere digest, all running unattended on their schedules. Dev is
  deployed and runs the same pipeline.
- **Phases 1–5, 7 and 8: done** — built, deployed, and in daily use.
- **Phase 0: in progress** — the code and workflows are done; what's open is
  GitHub/AWS configuration: branch protection, a required reviewer on the
  `production` environment, a working destroy/rebuild of dev, and sharing
  one WAF ACL across both distributions (details under Known issues and
  Phase 0 below).
- **Phase 6: in progress** — one item open: WAF rule tuning from real traffic.
- **Since phase 8**, work has continued as enhancements; see "Enhancements
  since Phase 8" below, and `docs/project-plan.md` §11 for the detail.

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

- [x] Rename default branch `master` → `dev`; create `prod` from its tip —
  `dev` is the default branch and `prod` exists
- [ ] Branch protection on `dev` and `prod`: PR required, `terraform`,
  `security` and `checkov` checks required (Checkov is its own check since
  #264), no direct pushes — **blocked:** GitHub offers
  branch protection and rulesets on a private repository only with GitHub
  Pro (the API answers "Upgrade to GitHub Pro", checked 2026-09-27). Until
  then, the PR-only rule is a convention, not something GitHub enforces
- [ ] GitHub Environments: `dev` (no protection) and `production` (required
  reviewer) — **partly done:** the `production` environment exists but has
  no protection rules (required reviewers on a private repo are also a paid
  GitHub feature), so a published release deploys without an approval step
- [x] `docs/project-plan.md` reconciled (2026-09-27): it now covers the
  financial-topic rules (§2, §7), feedback screening (§11), all phases
  (§10), and every shipped enhancement (§11)

---

## Prerequisites (manual, one-time, blocking)

- [ ] AWS root user MFA enabled; root no longer used day-to-day — not
  verifiable from the repo; confirm in the AWS console
- [ ] IAM (or IAM Identity Center) admin user created for this project, MFA on
  — not verifiable from the repo; confirm in the AWS console
- [x] AWS Budget alarm set — the account has AWS's "My Zero-Spend Budget"
  plus `bloggerbear-bedrock-spend` from `infra/bootstrap` (checked
  2026-09-27). Consider a total-spend budget with real thresholds (e.g.
  $10 / $25 / $50), since zero-spend alerts on any charge at all
- [x] Bedrock model access requested in-console for the Claude models needed,
  **in `ap-southeast-2`** (manual, per-model, per-region, can sit pending —
  start this early; before Phase 1, confirm which specific model IDs are
  directly invokable there vs. need a cross-region inference profile) —
  done: models run through Australian cross-region inference profiles and
  are chosen at runtime from the Models registry
- [x] AWS region decided: **`ap-southeast-2` (Sydney)** — fixed, not still
  open. Exception: the CloudFront-scope WAF ACL and ACM certificate must
  still be created in `us-east-1` (AWS platform constraint, see
  `docs/specs/phase-0-foundations.md` → "Region")
- [x] AWS CLI v2 installed locally, `aws configure` / `aws configure sso` run

---

## Phase 0 — Foundations

Spec: `docs/specs/phase-0-foundations.md`

- [ ] Known issues above resolved (branches, protections, environments) —
  branches done; branch protection and the production reviewer are blocked
  on GitHub Pro (see Known issues)
- [x] CI's pinned Terraform version bumped from `1.9.8` to ≥1.10.0 (required
  for native S3 locking below) — bumped to `1.16.2`
- [x] `infra/bootstrap`: Terraform state S3 bucket **only** — no DynamoDB
  table; locking is native S3 (`use_lockfile = true`) — code complete; the
  one-time local apply is done by hand (last applied 2026-09-27, adding the
  AgentCore deploy permissions). Keep its variables in the git-ignored
  `infra/bootstrap/terraform.tfvars`: a plan without `domain_name` and
  `budget_alert_email` proposes destroying the DNS zone and budget
  (`prevent_destroy` blocks it)
- [x] `infra/modules/static-site`: reusable module (S3 + CloudFront + OAC),
  parameterized for `enable_custom_domain` and `force_destroy`
- [x] `infra/environments/dev`: region `ap-southeast-2`, no custom domain,
  dev bucket `force_destroy = true`
- [x] `infra/environments/production`: region `ap-southeast-2`, custom
  domain via Route 53 + ACM (ACM cert in `us-east-1` — CloudFront
  requirement, not a region change) — live at bloggerbear.com; the hosted
  zone lives in `infra/bootstrap` so it outlives production
- [ ] One WAF Web ACL (created in `us-east-1`), associated with **both**
  distributions (not two ACLs) — production creates the ACL; wiring dev's
  distribution to the same ACL ARN is a manual `terraform.tfvars` edit
  after production's first apply (see `infra/environments/dev/variables.tf`)
  — still open: dev's `web_acl_arn` is empty (checked 2026-09-27)
- [x] Dev workflow: auto-apply on push to `dev`, no approval
- [x] Production workflow: apply only on Release published from `prod`,
  gated by `production` environment approval — workflow is wired up and
  in use; the `production` environment exists but has no required reviewer
  yet (see Known issues)
- [x] Concurrency groups on both apply paths (queue, don't race)
- [x] `workflow_dispatch` "destroy dev" workflow
- [ ] Round-trip proven: destroy dev, rebuild it via a push, confirm it
  comes back clean — not yet: all four runs of the destroy-dev workflow
  (2026-09-19) failed, so the destroy half needs fixing first

## Phase 1 — First adapter + manual pipeline

- [x] Adapter contract implemented: `fetch_state`, `material_diff`,
  `source_refs` (per §6 of the plan) — `lambdas/common/adapters/base.py`
- [x] First adapter built — simplest data source first (GitHub Trending is
  the obvious pick: no auth, no rate-limit pain, no compliance sensitivity)
  — `lambdas/common/adapters/github_trending.py`
- [x] Hourly research-tick Lambda: diff-first, only calls Bedrock on material
  change, writes to `Findings` (rolling TTL 7–14 days) —
  `lambdas/research_tick_handler.py`; deployed. The research tick now runs
  on a heartbeat + interval (see project-plan §4) rather than strictly
  hourly
- [x] DynamoDB tables: `Topics`, `Findings`, `CandidateIdeas`, `Articles` —
  plus `ModerationQueue` (required by the "route to moderation" scope line
  below, per the data model in §5) — `infra/modules/app-data/`
- [x] S3 storage for article bodies + raw source snapshots — one private
  `bloggerbear-<env>-content` bucket, `articles/` and `snapshots/` prefixes
- [x] Daily cycle (manually triggered for now): ideation → selection → draft
  → compliance review → publish — `lambdas/daily_cycle_handler.py`;
  deployed and scheduled (Phase 3). A fresh-data review now runs before
  compliance
- [x] At least one manually-triggered end-to-end run produces an article
  you'd actually be willing to publish — production publishes (or holds for
  review) articles daily across four topics

## Phase 2 — Admin console

- [x] Admin API, separate from public API, authenticated (Cognito or IAM +
  WAF IP allowlist) — went with **IAM (SigV4) + a regional WAF IP allowlist**
  rather than Cognito (simpler/cheaper for a single-operator project, and
  explicitly sanctioned as an equal alternative here); deployed, with the
  operator's IP in `admin_allowed_cidrs` (fails closed when empty)
- [x] Topic CRUD UI, including adapter selection — implemented as a local
  operator CLI (`scripts/admin_cli.py`), not a browser app, since IAM auth
  from a browser would otherwise need Cognito Identity Pool federation
  anyway; `topics create/get/update/delete` cover CRUD, `adapter`/
  `adapter_config` are free-form CLI args so any adapter key works
- [x] Manual "trigger a run" button per topic — `admin_cli.py topics
  trigger <id> --pipeline {research_tick,daily_cycle}`, async Lambda invoke
- [x] Moderation queue UI: approve / reject flagged drafts —
  `admin_cli.py moderation list/approve/reject`, and since then the review
  inbox (`admin_cli.py inbox` / `approve`: one keystroke per item, plus
  Re-Write of a held article)
- [x] "Candidates considered but not published" view (from `CandidateIdeas`)
  — `admin_cli.py topics candidates <id>`, returns every candidate
  regardless of status so rejected/unselected angles are visible too

## Phase 3 — Automation

- [x] EventBridge Scheduler per topic: hourly tick + daily cycle, each on its
  own configured cadence — created/updated/deleted dynamically by
  `admin_api_handler.py` (`lambdas/common/scheduler.py`) at topic
  create/update/delete time, since topics are runtime data Terraform can't
  enumerate; deployed. Production topics research every 4 hours and write
  at 09:00–09:03 Sydney, staggered a minute apart
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
  redeploy; deployed
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
  not originally scoped) — Terms of Service and Privacy Policy, first as
  `#/terms` and `#/privacy` hash routes and since moved to static
  `frontend/terms.html` / `privacy.html` (no JavaScript needed, own URLs,
  a linkable id per section, a "Last updated" date; the old routes
  redirect), grounded in what the codebase actually
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
- [x] `robots.txt`, security response headers, a standalone `error.html`,
  a full favicon set, further ARIA work, and a sticky header/footer
  layout (second ad hoc follow-up) — `robots.txt` allows all crawling
  (this site's own public API + RSS exist specifically so it can be
  consumed, per Phase 8's design) with a courtesy `Crawl-delay`, no
  disallowed paths (the admin console isn't a page on this site at
  all, so listing one to "hide" it would only advertise it). No
  `.htaccess`: this stack is S3 + CloudFront, no Apache anywhere, so
  an `.htaccess` file would be silently inert if deployed — the real
  equivalent is `infra/modules/static-site/main.tf`'s new
  `aws_cloudfront_response_headers_policy` (HSTS, X-Content-Type-
  Options, X-Frame-Options: DENY, a real Content-Security-Policy,
  Referrer-Policy, Permissions-Policy), applied to every response the
  distribution serves — verified it doesn't introduce any new
  `trivy config` findings. A standalone `error.html` (deliberately no
  `app.js`/inline `<style>` — the CSP's `style-src 'self'` has no
  `'unsafe-inline'`, so an inline safety-net style would just be
  blocked) wired into the distribution's `custom_error_response` for
  403 (S3/OAC's actual response for a missing key) and 404, both
  mapped to one friendly page instead of leaking S3's raw XML error
  body. A full favicon set generated from `logo.svg` (`favicon.ico`,
  `apple-touch-icon.png`) plus a `logo.webp` variant wired in via
  `<picture>` (WebP primary, SVG fallback). Further ARIA: `aria-live`
  on the feedback status region (async submit/error state changes are
  now actually announced to screen readers, not just visually
  updated), `aria-label`s on the vote buttons/dismiss button/RSS
  link/external source links, and a visible-plus-`aria-describedby`
  reminder on the comment box not to include personal information —
  tied directly to the Privacy Policy's own request. Tab order:
  audited for zero positive `tabindex` values (an accepted anti-
  pattern) and confirmed the skip link is first in DOM order via an
  automated check, rather than by inspection. Sticky layout: `body` is
  now a column flexbox (`min-height: 100vh`) with `main` as the one
  flexible child (`flex: 1 0 auto`), which is what actually glues
  `.site-footer` to the bottom of short pages rather than letting it
  float up under sparse content; `header` is `position: sticky; top:
  0` so it stays visible while scrolling instead of only "not
  scrolling away because the page happened to be short." Verified
  with an extended real functional test (jsdom again) — 29/29 new
  checks plus the prior 22/22 and 6/6 regression checks, none broken —
  though the visual layout itself (spacing, how the sticky header
  actually looks while scrolling) wasn't eyeballed in a live browser
  this session either.
- [x] Homepage hero + About page, header only sticky on mobile, a
  "Back to top" link (third ad hoc follow-up) — `header`'s
  `position: sticky` is now inside a `@media (max-width: 640px)`
  block instead of applying unconditionally: on a phone, a scrolled-
  away header is a longer, more deliberate gesture to get back to, so
  staying docked earns its keep; on desktop/tablet it now scrolls
  away like the rest of the page, trading a permanent chunk of every
  page's vertical space for a `.back-to-top` link (`#top`, a plain
  fragment link to `header`'s own `id="top"`, no JS) that solves the
  same "get back to the nav" problem without the permanent cost. The
  home route gained a hero section — an `h1` tagline + a "Find out
  more" CTA to the new standalone `frontend/about.html` — with
  "Topics" demoted to an `h2` to keep exactly one `h1` per page.
  `about.html` (deliberately JS-free, like `error.html`, for the same
  "loads fast and reliably even for someone following a resume link"
  reason) is an interview-pitch-style architecture walkthrough: what
  the project is, the two pipeline cadences, why compliance is a
  deterministic gate rather than a model request, and the AWS/IaC/
  CI-CD stack at a glance. Caught and fixed a second instance of the
  same dark-mode contrast bug from the prior PR (white text on
  `var(--accent)`, ~2.4:1 against WCAG AA's 4.5:1 minimum) on the new
  CTA's hover state before shipping, and manually re-checked that
  `background: var(--accent)` doesn't appear anywhere else in
  styles.css so a third instance doesn't slip through unnoticed (no
  automated test enforces this -- it's not wired into CI, just a
  check run during this PR). Verified with a third extended manual
  pass — 24/24 new checks plus the prior 29/22/6 regression checks,
  all still passing.

## Phase 5 — Feedback loop

- [x] Thumbs up/down on articles, no identity attached —
  `frontend/app.js`'s `renderFeedback` posts `{vote: "up"|"down"}` to
  `POST /articles/{article_id}/feedback` (public API,
  `public_api_handler.py`'s `_submit_feedback`); deployed
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

All six items above: deployed (originally shipped with 143 lambda tests;
the suite is now over 2,300). Fixed post-first-pass: `admin_api_routes` /
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
  `production` as `module.observability`; deployed. Alerts reach a person
  only once `var.alert_email` is set and the SNS subscription email is
  confirmed
- [x] Cost/budget alarms specifically watching Bedrock spend —
  `infra/bootstrap`'s new `aws_budgets_budget.bedrock_spend`
  (account-level, so it lives in bootstrap alongside the other one-time
  resources, not per-environment), filtered to the "Amazon Bedrock"
  service, notifying at 80% actual / 100% forecasted. Gated on
  `var.budget_alert_email` (empty by default → no budget resource is
  created at all, matching this project's fail-closed-by-omission
  pattern) — created (checked 2026-09-27). This is in addition
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
  deployed. Since narrowed for privacy: the public API and shared
  CloudFront ACLs now log only BLOCK/COUNT requests, with browser-
  fingerprinting headers redacted, kept 14 days (the Privacy Policy's
  section 5 describes exactly this). Tuning from allowed traffic now
  relies on the ACLs' CloudWatch metrics and WAF's 3-hour sampled
  requests; the admin ACL still logs everything
- [x] Prompt iteration on the compliance-review step based on what's
  actually been flagged so far — done: real flagged drafts showed the
  reviewer calling figures from the findings "invented", so the review now
  sees the source material and plain code decides which nominated items
  stand (project-plan §11, "The compliance review sees the sources"). The
  visibility that made it possible: added
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
  route every other topic already uses. Deployed, daily at 07:00 UTC
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

## Enhancements since Phase 8

All shipped and deployed; the design and decisions for each are in
`docs/project-plan.md` §11. The vision entries merged into `dev` on 15 October (#285); the
submission-round entries after them are docs and tests, with nothing to deploy.

- [x] Static article publishing — every publish path renders a static page
- [x] Custom domain (bloggerbear.com) via Route 53 + ACM
- [x] AI lineage and cost tracking, pluggable model routing (registry,
  fallback, rotation after publish), and a public Stats page
- [x] Lineage cost fixes and the research tally
- [x] Rolling research, whole-window articles, and the fresh-data review
  before publish (shadow by default; enforce mode available per topic)
- [x] The compliance review sees the sources
- [x] Feedback: comment screening, limits and lockdown, verification
- [x] Musings (the bear's moods) and the tummy toy
- [x] The review inbox (`admin_cli inbox` / `approve`)
- [x] Equipment: approved prompt changes as gear the bear wears
- [x] Observability: StatsCurrent/StatsHistory, Lambda timing, Cost
  Explorer poll
- [x] Cleanup: TTLs, expiring snapshots, 90-day log retention
- [x] Refusal guard for angles and titles; recent titles fed to ideation
- [x] Re-Write of a held article from the inbox (#130)
- [x] GDELT time budgets; CoinGecko key passed to the daily cycle (#129)
- [x] AgentCore Web Search as the fallback when GDELT fails (#132)
- [x] Web search usage and spend on Stats: counted (#134) and read from the
  AWS bill (#135)
- [x] Stats: the assistant's spend, total infrastructure cost and a total overall cost
  that is the AWS bill alone (Bedrock counted once), with the assistant's tile in
  the all-time and weekly sections (superseded by the next line)
- [x] Stats page, one source and one period per section (#268, #270, #271, and this
  round's docs): Total Stats from the history table only, Weekly Stats from the
  current table only, the per-article detail in an Articles section of its own. The
  bill is four tiles that add up; the rolling-30-day and calendar-month tiles and the
  assistant's tiles are gone
- [x] Stats page's Articles section (the 30-day table, by model, by topic) is built after
  each publishing run and stored, not scanned from the Articles table on every view
- [x] A missed stats rollover: an alarm on dropped scheduled runs, and
  `scripts/repair_stats_week.py`, run for both environments (#265)
- [x] Crypto research looks up 2 to 5 altcoins a tick, not 5 to 10 (#267)
- [x] Each deploy role is refused the other environment's resources, by a Deny-only
  policy beside the shared one (bootstrap; needs a hand apply)
- [x] Cost saving: four firewall switches, every one on by default (`WAF_REGIONAL_ENABLED`,
  `WAF_ADMIN_ALLOWLIST`, `WAF_SITE_ENABLED`, `WAF_LOG_ALL_REQUESTS`, each `_DEV`/`_PROD` first),
  with the admin API's address allowlist always on as its resource policy and `moved` blocks so
  the default plan changes nothing (docs/cost-saving.md; project-plan §11)
- [x] Staggered research and authoring schedules (production and dev)
- [x] Staggered schedules by slot: `stagger_minutes` on the pipeline row, `stagger_slot` on each
  topic, the effective schedules written at the slot's minute and recorded on the row, `topics
  schedule` / `topics reschedule`; `topics pause` / `resume` (schedules disabled, the handlers
  refuse, "Paused" on the topic page, a musing each way); the digest kept general with no
  figures and no longer held for a person on a financial contributor alone (project-plan §11;
  the dispatcher design revised for it, docs/enhancements/dispatcher-sqs-enhancement.md)
- [x] Render-blocking CSS kept on purpose; async preload reverted (#127)
- [x] GitHub Trending no longer scrapes `github.com/trending`: it calls the
  official REST Search API (most-starred repos created in the last week),
  with an optional token in SSM at `/bloggerbear/<env>/github-api-token`
- [x] The operator's assistant (Alexa+ track): ops MCP server, Strands agent, memory, the Admin
  CLI guide, `ask.html` (#185–#192)
- [x] Alexa+ plan: the voice over the Strands agent, the add-on, the async briefing (#195;
  `docs/enhancements/alexa-plus.md`)
- [x] The page's voice fixed: tap to talk, per-error messages, speech that finishes, Test voice
  (#198)
- [x] OAuth discovery for Alexa+ account linking: PRM, AS metadata, `WWW-Authenticate`, an Alexa
  app client per environment (#200)
- [x] The Alexa+ add-on runbook, manifest template and values helper (#201)
- [x] Async briefings: Alexa starts the agent and reads its answer back; opt-in keep-warm (#202)
- [x] `firewall_review`, production only, gated three ways (#203)
- [x] Fix: `TRENDING_URL` collision between #197 and #199 broke the adapter's import on dev (#205)
- [x] The operator's assistant in production: MFA, account-wide data, the firewall (#206)
- [x] The operator's assistant reads the logs: root causes, check-it-yourself, written down and
  followed up, watch a function or table, masked addresses, a suggested command under the CLI help
  (#218, #220, #221, #225, #226, #227, #228; docs/enhancements/ops-assistant-log-reader.md)
- [x] The operator's assistant, second round: forgiving topic names with "did you mean" (#232),
  success rates and the Lambdas listed with what they do (#234), a suggested command for any CLI
  question and a topic-setup mock-up (#235), push to talk that does not let go while held (#238)
- [x] The operator's assistant, third round (dev against production): API calls by status code and
  no Lambda success rate (#249), no "I'm read-only" pushback and no false "someone is probing"
  (#250), log queries with their `SOURCE` log groups in an editable box (#253), the architecture
  in layers (#255), a wake call at sign-in (#256), a Dismiss button and "it looks like you already
  fixed this" (#257); docs/enhancements/ops-assistant-log-reader.md, "Third round"
- [x] Shorter listings: Musings 15 a page and a topic's articles 10 a page, with a pager
  (`?page=N` on the API and the route); "Articles in the Pipeline" lists at most 3 pending
  articles, then a plain "...more" (#276, #277, #278; project-plan §11, "Paged listings")
- [x] Architecture by feature: `architecture` with `feature` walks through article research step by
  step across the layers (adapters, keys in SSM, research tick, findings, candidate ideas,
  drafting, reviews, the article in S3, where the agents are) (#262; docs/architecture/)
- [x] Agentic vision, for the OpenCV AI Competition 2026 (#240–#254, merged into the integration
  branch, #285, its pull request to `dev`, with the review's fixes): the vision core in
  `lambdas/vision/`, the GeoTIFF reader and UTM without GDAL, the stateless OpenCV 5 worker on
  arm64 in us-west-2 with its contract and cross-region client, the `satellite_vision` adapter
  with running state, the bounded triage agent with a deadline, `force_manual_review`, the COOL
  benchmark and image recipe; nothing deployed until switched on (docs/enhancements/vision.md,
  docs/enhancements/rail-access-monitor.md §1)
- [x] The vision worker is opt-in through the `VISION_ENABLED` repository variable, fed to both
  apply workflows as `vision_enabled`; the setup script lists it as deliberately not asked (#286)
- [x] The OpenStreetMap Overpass client for rail networks: two allow-listed endpoints, one
  fallback hop, decimation to the 20 m grid, caps, a streamed body under a size cap, coded
  errors, cache rules (#287)
- [x] Figures from Findings to articles: `Adapter.figures`, the list on the Finding and the
  article, one server-side copy per PNG to `articles/figures/<article>/<n>.png`, the `<figure>`
  on the static page and in the SPA, removed on take-down (#288)
- [x] The rail access vision core: built-up heat, station reach and deserts, the rail network
  graph from OpenStreetMap ways, flags and simulated links, the figure; COG overviews and the
  UTM inverse (#289; `lambdas/vision/urban.py`, `access.py`, `network.py`, `rail_analyse.py`,
  `rail_annotate.py`)
- [x] Worker tasks: `rail_access` beside `ships` in the contract, the handler, the scene reader's
  overview reads, the network caps, bounded betweenness and graph size, and the benchmark's rail
  kernels and `fetch-network` (#291; `lambdas/vision_worker_handler.py`,
  `lambdas/common/vision_contract.py`, `scripts/vision_benchmark.py`)
- [x] The `rail_access` adapter, the shared `vision_sites` module and task-aware triage with
  `web_context`; the first observation reported once; refused scenes and failed network refreshes
  not retried every tick; only the newest history entry keeps the lists (#292;
  `lambdas/common/adapters/rail_access.py`, `lambdas/common/vision_sites.py`,
  `lambdas/common/vision_triage.py`)
- [x] The rail figure: flags in red rings, isolation weight as ring thickness, ghost lines and
  dashed simulated links, the caption and a legend in a strip under the map, PNG level 9 within a
  2 MiB budget (#293; `lambdas/vision/rail_annotate.py`)
- [x] Planner notes: the rail network's flags and simulated links as generated sentences in the
  diff summary, the review evidence and the Finding (`planner_notes`), never recommendations
  (#294; `lambdas/common/rail_notes.py`; docs/enhancements/vision.md §6.8)
- [x] The rail analysis's time bounds: linear station adjacency, ghost-line walk and run lengths,
  thinning only in the box round what the last pass removed, the rail window cap
  `RAIL_MAX_WINDOW_PX` = 3,584 px from one-core measurements, and speed guards that count the work
  (#295; `lambdas/vision/network.py`, `rail_analyse.py`, `scene.py`,
  `lambdas/vision_worker_handler.py`)
- [x] A topic's findings window: `findings_window_hours` (1 to 336 hours, 24 by default, never past
  the last article) through the admin API and CLI, read by the daily cycle and by Re-Write, so a
  topic that writes on some days only, like a city of the rail rotation, drafts every Finding since
  its last article (#297; `lambdas/common/findings_window.py`, `lambdas/daily_cycle_handler.py`,
  `lambdas/common/rewrite.py`, `scripts/admin_cli.py`)
- [x] Fixes from the review of the whole integration branch: the daily trending digest leaves out
  every topic whose articles always wait for a person, so vision text never reaches an
  auto-published page; a rail Finding carries only the maps of the sites its summary reports; the
  SPA removes a figure whose image fails to load; one measuring rule for both vision adapters
  (`time_budget_seconds` 45 to 80); the worker follows no redirect and reads only its byte range;
  one parser for `VISION_ALLOWED_URL_PREFIXES`; editorial defaults for both vision adapters; the
  compliance review's verdicts restored for every topic; the ops assistant's log review leaves the
  worker out of its default sweep; a test runs the gitleaks email rule over the Python sources
  (#298, rail PR L; `lambdas/trending_digest_handler.py`, `lambdas/common/vision_sites.py`,
  `lambdas/vision_worker_handler.py`, `lambdas/common/vision_contract.py`,
  `lambdas/common/editorial_resolver.py`, `lambdas/common/compliance.py`, `frontend/app.js`)
- [x] Documentation: the vision pipeline page (docs/architecture/blogger-vision.md), the README
  section and data-source rows, the configuration and deployment sections, the submission draft
  (#290, rail PR F1)
- [x] Documentation: vision.md for two tasks, the four city topics rotating one a day with a
  192-hour findings window (`scripts/rail_topics.example.json`) and the runsheet's
  `rail_access topics` section, the friction entries, and the earlier docs brought in line with
  the merged code, PR L's fixes included: the documentation cut-off for the vision pipeline
  (#296, rail PR F2)
- [x] The assistant's vision round: the operator's assistant and Alexa+ see the vision pipeline,
  read-only and only where vision is on. `vision_status` (is the worker on and where, which sites
  were measured and why the others were not, a card per problem), vision topics in
  `pipeline_health` and the topic watch, the worker's log read in `vision_region`, the
  `vision-pipeline` feature, the `vision` guide and runsheet; site names and error text only in
  "(unverified)" columns for the page, never to a voice caller; no list change and no echo after
  text written outside the project, held in code; `redact.scrub` withholds instructions however
  they are spaced or spelt; a sites config is never filled in (#300, on the integration branch
  for the assistant's work after #285; `lambdas/ops_mcp/vision_status.py`,
  `lambdas/common/adapters/base.py`, `lambdas/ops_agent/policy.py`,
  `infra/modules/ops-assistant/vision.tf`; [design](enhancements/ops-assistant-vision.md))
- [x] The submission round's evidence trail: one JSON line of codes and numbers per step (a tick
  ran, a site's turn, OpenCV measured, the numbers were compared, the agent decided, a person
  decided, a topic changed), a decision ring of the last 12 decisions per site in the running
  state with the agent's trail and each tool's result, the worker's request id on every result,
  the worker's own line with its CPU, the research tick's and the admin API's lines, `log_review`
  leaving the lines out and its topic filter matching a whole id, and the runsheet's Logs Insights
  queries (on the integration branch after the assistant's vision round;
  `lambdas/common/evidence.py`, `lambdas/common/vision_decisions.py`,
  `lambdas/common/vision_triage.py`, `lambdas/vision_worker_handler.py`;
  [design](enhancements/vision-evidence.md))
- [x] Interrogating a topic's Findings, three ways: the assistant's `topic_findings` (a topic's
  research Findings newest first as a table, with `next_article` for exactly what the next daily
  run reads; counts and ages spoken, the summary scrubbed and on the page only, nothing to a voice
  caller), `GET /topics/{topic_id}/findings` and `admin_cli findings list` (the raw items, with
  `--days`, `--since` and `--next-article`), and the repository's first Claude Code skill,
  `topic-findings`, with its rules for Finding text in a developer's session; the findings
  window's start moved to `common/findings_window.window_start`, shared by the daily cycle, the
  tool and the route; Findings joins the assistant's readable tables (the Findings round, #307,
  after #300 on the integration branch; `lambdas/ops_mcp/topic_findings.py`,
  `.claude/skills/topic-findings/SKILL.md`, `scripts/tests/test_claude_skills.py`;
  [design](enhancements/ops-assistant-vision.md#topic-findings))
- [x] The submission round, for the OpenCV AI Competition 2026: the rubric tracker
  (`docs/hackathon/rubric-tracker.md`), one row per rubric line, the owner's ask and each owner
  step, with its status, what it needs, the evidence, the gap, what closes it and what the
  assistant may say, updated by the PR that closes a row; a format test that fails on a
  malformed row, run by the Python CI on a pull request that also touches `lambdas/` or
  `scripts/` and by hand on a docs-only one until `python-ci.yml` lists `docs/hackathon/**`
  (#303, submission PR P0; `scripts/tests/test_hackathon_docs.py`)
- [x] The submission round's benchmark and evaluation inputs (Q1): the benchmark writes run files
  with the code, machine, build, inputs' ETags and every sample, records the rail metrics, their
  digest, the figure's hash, the peak memory and the OpenCV libraries mapped during a run, gains
  `--cache`, `--threads`, `--pin-cpu`, `--isolate`, `--only`, `--imds`, `--label`, `compare` (a
  seeded bootstrap speed-up with parity) and `controls` (the COOL guard's negative controls); the
  Sydney scene manifest with `vision_eval.py pin` and `coverage` (the first run file, under
  `docs/hackathon/evidence/runs/`); the network fixture directory's ODbL notice; the COOL runsheet
  corrected into the Graviton session's script, with the 4.x pin (`opencv-python-headless==4.14.0.94`)
  (#308, submission PR Q1)
- [x] A city's first OpenStreetMap network gets the tick's measuring time: with none cached for its
  polygon, the fetch may take the rest of the budget less 10 s (about 60 s at the default 70 s,
  70 s at the 80 s cap) instead of 30 s, each Overpass hop reading for as long as that leaves and
  its query's `[timeout:]` the same; the city is measured on the next tick, and a refresh of a
  cached network keeps 30 s. The third of the first live tick's defects, after #305 (the content
  bucket's ListBucket) and #310 (the Overpass User-Agent) (#312, rail PR U;
  `lambdas/common/adapters/rail_access.py`, `lambdas/common/osm.py`)

## Backlog / not yet scheduled

*(Freeform — drop ideas here as they occur to you; promote them into a
phase above, or a new phase, whenever you're ready to schedule them.)*

- **Fix production's CoinGecko key (operator):** the deployed
  `COINGECKO_API_KEY` is rejected by CoinGecko (HTTP 401, "API Key
  Missing"; it doesn't have the `CG-` prefix real keys use). Crypto research
  falls back to the keyless API and fails on altcoin days. Update the
  `production` environment secret (and the plan, if it's a Pro key), then
  run a release
- **Confirm the AgentCore Cost Explorer service name:** #135 assumed
  `"Amazon Bedrock AgentCore"`. Once AgentCore spend has been billed, check
  that `agentcore_cost_aud_30d` on the stats-current row is non-zero (it is
  no longer a tile on the Stats page); if not, look the name up
  with `aws ce get-dimension-values --dimension SERVICE`
- **Confirm the AgentCore per-query price** ($0.007 USD assumed from the
  launch announcement) against the pricing page
- **Fix the destroy-dev workflow**, then prove the destroy/rebuild round trip
- **Share one WAF ACL** across the dev and production distributions (set
  dev's `web_acl_arn`)
- **Branch protection and a production reviewer:** needs GitHub Pro, or a
  public repository
- **WAF rule tuning** from real traffic (Phase 6)
- **Look at the Stats page in a browser** — `test_frontend_stats.py` now runs the real
  script under Node and reads back every section, tile and table, but nothing checks how
  the three sections lay out at desktop and phone width
- **A true all-time AWS bill** — the all-time figure starts at the first week that has a
  history row. A one-off backfill from Cost Explorer would cover the weeks before it
- **Stop carrying the rolling readings onto the history rows** — the firewall's, API
  Gateway's and AgentCore's 30-day and monthly readings are still copied to each week's
  row and the all-time row at rollover, though nothing reads them there now
- **Alexa+ bootstrap (operator):** needs Alexa+ toolkit access (US, partner-gated); then
  `alexa/README.md` for dev, and separately for production. Settle whether Cognito accepts the
  RFC 8707 `resource` parameter Alexa sends (friction 10.19-10.20)
- **A real-microphone check of the voice** on dev and production, in Chrome and Edge (Test voice)
- **Three Checkov findings skipped with reasons in #261, worth fixing:** the public API's
  CloudFront distribution has no response headers policy (`CKV2_AWS_32`; the site's has one),
  the daily-cycle state machine has no CloudWatch execution logging (`CKV_AWS_285`), and
  dev's content bucket is not versioned (`CKV_AWS_21`; production's is)
- **Decide whether the deploy roles should also trust the plain OIDC subject:** bootstrap's
  trust policies match only GitHub's immutable form (`repo:<owner>@<id>/<repo>@<id>:...`), so a
  fork whose tokens carry `repo:<owner>/<repo>:...` is refused (#263; the setup guide's
  troubleshooting shows how to tell)
- **Consider `adapter_config.provider = "agentcore"` for wow-forever:** the
  fallback triggers only when GDELT fails, not when it answers with nothing
