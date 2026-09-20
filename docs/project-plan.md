# BloggerBear Project Plan

## 1) Purpose
BloggerBear is an autonomous, multi-domain research-and-publishing platform. Topics are configured by admins and run two cadences:
- **Hourly research tick** to refresh a rolling knowledge base
- **Daily authoring cycle** to ideate, select, draft, review, and publish one article

## 2) Hard Constraints
1. No PII is collected or persisted.
2. Research ticks must do structural diffing before any Bedrock call.
3. Drafts must pass compliance review before publish.
4. Financial/investment-adjacent topics always route to manual moderation and disallow recommendation language.
5. New domains are implemented as adapters, not core pipeline branches.
6. Terraform apply is never manual/ad hoc — see §8 for the branch/release model that governs when it runs.
7. Security checks block merges on HIGH/CRITICAL findings.

## 3) Target Architecture
> ⚠️ Every `us-east-1` mention below (and elsewhere in this repo's docs) is
> intentional — the CloudFront-scope WAF Web ACL and ACM certificate only,
> per AWS's own requirement. Do not change these to `ap-southeast-2`.
- **Region**: `ap-southeast-2` (Sydney) for everything, except the
  CloudFront-scope WAF Web ACL and ACM certificate, which AWS requires in
  `us-east-1` regardless of hosting region
- **Compute**: Python 3.11 AWS Lambda functions
- **AI**: Amazon Bedrock (Claude) — confirm which model IDs are directly
  invokable in `ap-southeast-2` vs. need a cross-region inference profile
  before Phase 1 locks in a model choice
- **Storage**: DynamoDB + S3
- **Frontend**: Static site (S3 + CloudFront)
- **Security**: CloudFront + WAF + Shield Standard
- **IaC**: Terraform (≥1.10, for native S3 state locking — no DynamoDB lock
  table) with GitHub Actions deployment

## 4) Pipelines
### Hourly Research Tick
1. Load topic + adapter config
2. Fetch current source state via adapter
3. Diff against prior structural snapshot
4. If no material change: stop
5. If changed: summarize with Bedrock and store findings

### Daily Authoring Cycle
1. Generate candidate angles
2. Select one angle
3. Draft article
4. Run compliance review
5. Publish if compliant; else moderation queue

## 5) Data Model (DynamoDB)
| Table | PK/SK | Purpose |
|---|---|---|
| Topics | `topic_id` | Topic config, cadence, adapter list |
| Findings | `topic_id` / `captured_at` | Research summaries and source hashes |
| CandidateIdeas | `topic_id` / `created_at` | Daily generated article ideas |
| Articles | `article_id` | Draft/compliance/publish lifecycle |
| ViewCounters | `article_id` | Aggregate read counters |
| Feedback | `article_id` / `feedback_id` | Scrubbed public feedback |
| PromptRefinements | `topic_id` / `version` | Prompt iterations and rationale |
| ModerationQueue | `queue_id` | Manual review tasks |

## 6) Adapter Contract
Each new domain provides an adapter implementing the same contract:
- `fetch_state(topic_config) -> normalized_state`
- `material_diff(old_state, new_state) -> bool, diff_summary`
- `source_refs(new_state) -> list[SourceRef]`

Core pipeline code must remain topic-agnostic.

## 7) Compliance and Safety
- Run regex-based redaction then Bedrock redaction review before writing feedback.
- Never persist raw, unredacted comment text.
- Financial topics must avoid advice language and always queue for manual moderation.

## 8) CI/CD and Infrastructure Rules

Two long-lived branches, two environments:

- `dev` (default branch) — all work lands here via PR from a task branch,
  never a direct push. Merging to `dev` **auto-applies**
  `infra/environments/dev`. No approval gate — dev is disposable and can be
  torn down and rebuilt at any time via a manual `workflow_dispatch`
  "destroy dev" job.
- `prod` — promoted from `dev` via PR when a set of changes is ready to
  ship. Merging into `prod` does **not** deploy by itself.
- A production deploy happens only when a GitHub Release is published from
  a commit on `prod` (tagged with semver, e.g. `v0.1.0`). That workflow
  applies `infra/environments/production`, gated by the `production`
  GitHub Environment's required-reviewer approval. Because every release
  targets the same Terraform-managed production state, a new release
  replaces whatever was previously deployed rather than running alongside
  it.
- Both `dev` and `prod` require PRs and passing `terraform`/`security`
  checks via branch protection — no direct pushes to either.
- `infra/bootstrap` (the Terraform state backend itself — a single S3
  bucket, no DynamoDB table; locking is native to S3 via `use_lockfile`,
  Terraform ≥1.10) is the one exception to all of this: it's applied once,
  manually, locally, and is never wired into CI.
- One shared WAF Web ACL is associated with both the dev and production
  CloudFront distributions, rather than one each, to avoid paying its flat
  fee twice.

Full detail: `docs/specs/phase-0-foundations.md`.

## 9) Validation Commands
- Terraform: `terraform fmt -check`, `terraform validate`, `terraform plan`
- Python: `ruff check .`, `pytest`
- Security: `trivy config infra/`, `trivy fs --scanners vuln,secret lambdas/`, `bandit -r lambdas/ -ll`

## 10) Build Phases
### Phase 0 — Foundations (current)
- Establish CI workflows (Terraform + security)
- Set up the `dev`/`prod` branch and release model (§8)
- Create initial infra and lambda scaffolding
- Capture and enforce non-negotiable guardrails

### Phase 1 — Core Pipeline (current scope)
- Implement topic model and one adapter path
- Implement hourly diff-first research tick
- Implement daily ideation/selection/draft/compliance/publish chain
- Route uncertain/financial outputs to moderation queue

### Phase 2 — Refinement (out of scope until Phase 1 is stable)
- Additional adapters/domains
- Prompt refinement automation
- Frontend polish and analytics improvements

See `docs/PROGRESS.md` for the full phase 0–8 breakdown and live status —
this section is intentionally a summary, not the tracker.

## 11) Proposed Enhancements

Not yet scheduled or scoped for implementation — captured here so the
idea isn't lost, to be picked up in a dedicated follow-up PR when
explicitly requested.

### Static article publishing

**Problem**: every published article is currently read through
`public_api_handler`'s `GET /articles/{article_id}` — a Lambda invocation
plus a DynamoDB read plus an S3 read on every single page view, for
content that, once published, never changes. There's also no distinction
today between "not yet published" and "doesn't exist" (the public API
treats both as a 404, per its own doc comment), so a reader has no way to
see that BloggerBear is actively researching/drafting something for a
topic.

**Proposed shape**:
- At publish time (`daily_cycle_handler._publish_or_moderate`, and both
  places an article can be force-published after the fact — the
  moderation-approve flow and the force-publish admin route added in the
  low-hanging-fruit PR), render the article as a static HTML page and
  write it into the content S3 bucket alongside the existing markdown
  body, styled consistently with `frontend/`. Serve it directly from
  S3/CloudFront — no Lambda or API Gateway round-trip to read a published
  article.
- Keep topic listing and anything not yet published dynamic, via the
  existing public API — shown as a "BloggerBear is researching this"
  placeholder rather than a 404, using the existing Findings/CandidateIdeas
  visibility the admin API already has.
- Feedback (thumbs up/down + comment) and the view counter stay API-backed
  regardless — they're inherently interactive, not something a static page
  can serve on its own.

**Key risk to design around**: three different code paths can change an
article's status (`daily_cycle_handler`'s own publish/moderate branch, the
moderation-approve route, and the force-publish route) — all three must
regenerate the static page, not just flip the `Articles` table's `status`
field, or the static page and the table's authoritative status will drift
out of sync.

### Custom domain (bloggerbear.com) via Route 53 + ACM

**Problem**: the production site is only ever reachable at its
`*.cloudfront.net` default domain. A real domain, `bloggerbear.com`, has
now been purchased through GoDaddy but isn't wired up to anything yet.

**Good news, not a gap**: `infra/modules/static-site` already has the
entire custom-domain path built and switched on for production —
`infra/environments/production/main.tf`'s `module "static_site"` block
already sets `enable_custom_domain = true`, and the module already
contains the ACM certificate (DNS-validated, requested in `us-east-1` per
AWS's CloudFront requirement), the Route 53 validation/alias records, and
the `aliases` entry on the CloudFront distribution itself. This has been
a known, explicitly-flagged open item since Phase 0
(`infra/environments/production/variables.tf`'s `domain_name`/
`hosted_zone_id` are documented as "Required (non-empty) before the first
production apply") — it was simply waiting on a real, purchased domain,
which now exists.

**No CSR, no manual certificate handling.** GoDaddy's traditional
"download a CSR, submit it to a CA, install the signed cert" flow does
not apply here at all — ACM issues and auto-renews the certificate
entirely via DNS validation, which Terraform already automates end to
end (it writes the validation record into Route 53 itself and waits for
ACM to see it).

**Proposed shape / remaining steps**:
1. Create a Route 53 Hosted Zone for `bloggerbear.com` (one-time,
   arguably belongs in `infra/bootstrap` alongside the state bucket and
   OIDC role, matching that file's existing "applied once, manually,
   locally" pattern for foundational resources — or created directly via
   `aws route53 create-hosted-zone`/console, since a hosted zone is
   rarely-changing infrastructure not worth re-creating per environment).
2. In GoDaddy's dashboard, change `bloggerbear.com`'s nameservers from
   GoDaddy's defaults to the 4 NS values that hosted zone generates
   (Domain Settings → Nameservers → Custom). GoDaddy remains the
   registrar of record; Route 53 becomes the authoritative DNS host.
   DNS propagation is usually fast but can take up to ~48h.
3. Set `domain_name = "bloggerbear.com"` and `hosted_zone_id = "<the new
   zone's ID>"` in `infra/environments/production/terraform.tfvars` (or
   as CI-supplied variables) — neither value is sensitive the way
   `admin_allowed_cidrs`/`alert_email` are, so `terraform.tfvars` is fine.
4. Run the normal production release process (§8) — Terraform requests
   the ACM cert, validates it via Route 53, attaches it to the
   distribution, and creates the alias records, with no other code
   changes needed.

**Open question**: the module's `aliases` list currently takes exactly
one domain name (the bare root domain) — whether `www.bloggerbear.com`
should also resolve (and if so, whether as a second alias + redirect, or
left unsupported) hasn't been decided and would need scoping if wanted.

### AI lineage, cost tracking, pluggable model routing, and a public Stats page

**Problem**: today there's exactly one model (`var.bedrock_model_id`, a
Terraform variable) for every Bedrock call across every handler, changing
it needs a full apply, and nothing records which model(s) or how many
tokens actually went into producing a given article, or who/what
ultimately approved it. This is a large, multi-part enhancement — broken
into four pieces below, any of which could be scoped/built independently.

**(A) Per-article lineage and cost metadata**
- Track total token spend across *every* Bedrock call that contributed to
  one finished article, not just the final draft -- currently that's
  ideation (`_ideate`), drafting (`_draft_article`), title generation
  (`_draft_title`), and compliance review (`compliance.review_draft`),
  per `daily_cycle_handler.py` (and the equivalent calls in
  `trending_digest_handler.py`'s synthesis path). `common/bedrock.py`'s
  `invoke_claude` doesn't currently return token-usage data at all --
  it'd need to start surfacing the Bedrock response's usage block
  (input/output token counts) for this to be possible.
- Record which model(s) were used (e.g. "all Claude Haiku 4.5") as
  lineage metadata on the `Articles` item -- if every call for an article
  used the same model, that's simple; once (C) below exists, an article
  could genuinely span more than one model and the lineage needs to
  reflect that per-model breakdown, not just a single value.
- Compute an approximate cost in **AUD** from the token counts (needs a
  per-model USD/AUD pricing table and a currency-conversion figure kept
  somewhere -- likely hardcoded/updated periodically rather than calling
  a live FX API, given this project's cost-consciousness elsewhere).
- Missing/blank lineage data (e.g. every article published before this
  feature existed) must render as an explicit "no data" in the UI, not a
  blank space or an error.
- **Open question**: does musing generation (`common/musings.py`, itself
  a Bedrock call) count toward the *article's* lineage/cost, or does it
  get tracked as its own separate cost line? Leaning toward separate,
  since a musing isn't part of producing the article itself, but not
  decided.

**(B) Where this shows up**
- A **footer block** on every article (both the static S3-rendered page
  and the DynamoDB-backed dynamic view) with the fuller lineage stats --
  the static page must have this **hard-baked into its HTML at render
  time** (`common/static_pages.py`), not fetched via an API call, per
  this project's whole reason for static pages existing in the first
  place (docs/project-plan.md's own "Static article publishing" entry
  above). The dynamic view (admin/API-backed) reads the same metadata
  from wherever it's stored.
- A **compact one-line summary** near the existing gray-text published
  date (both on the topic screen's article list and the article detail
  page): something like `models [...] · tokens [by model] · approved by
  [Humans | Humans & AI | AI only] · approx. $X.XX AUD`.

**(C) Pluggable AI model adapters, with fallback and per-topic routing**
- A "supported models" registry, most likely a new DynamoDB table rather
  than a Terraform variable, so adding/switching models doesn't need an
  apply -- e.g. `topic_id`/global default → model ID, editable via the
  admin API/CLI the same way everything else in this project is admin-
  managed.
- Different model families need different Bedrock Converse API request
  shapes (Claude vs. Amazon Nova, etc.) -- `common/bedrock.py` would need
  to grow into a small adapter layer, one adapter per model family,
  behind a single common "call this model" interface every handler
  already uses via `invoke_claude`, so the handlers themselves don't need
  to know which model family they're talking to.
- **Fallback**: if the selected/primary model's call fails, try a
  configured fallback model instead (try/catch), and record in the
  lineage which one actually ended up producing the content, including
  whether a fallback occurred.
- **Rotation**: the daily cycle might deliberately vary the model used
  for a topic run over time -- e.g. "run the next few days' articles for
  this topic on a different model" -- whether that's an admin-set,
  time-bounded override on the topic, or genuinely random per-run
  selection from an allowed set, isn't decided; either way it needs to
  land in that run's article lineage.

**(D) Published-by attribution**
- Lineage should also record who/what approved and published each
  article: `Humans` (moderation-approve or force-publish by an operator),
  `AI only` (the fully-automatic compliant `daily_cycle_handler` path,
  today's only automatic path), with `Humans & AI` reserved for a future
  state once an AI reviewer/approver for the moderation queue exists
  (explicitly a later, separate piece of work, not part of this
  enhancement) -- the schema should have room for that third value now
  even though nothing produces it yet.

**(E) A new public Stats page**
- A public-facing dashboard (new "Stats" page/route in `frontend/`)
  charting costing data over time -- broken down by AWS service and/or
  by content produced and storage. Explicitly the least-scoped part of
  this enhancement (the user's own framing was "if that would be
  interesting") -- worth a dedicated design pass of its own (what it
  actually charts, where the underlying numbers come from -- Cost
  Explorer API vs. the per-article lineage data summed up vs. both --
  before implementation, rather than guessing at a shape here.

**Status (implemented in five PRs)**
- (A)/(C): #56 -- `Models`/`ModelConfig` DynamoDB registry (no Terraform
  apply to change models), `resolve_model` precedence, and
  `invoke_model_tracked` with token capture and fallback. No per-family
  adapter classes were needed: `common/bedrock.py` already used Bedrock's
  Converse API, which normalizes the request/response shape across
  providers.
- (A)/(D): #57 -- per-article `lineage` (every Bedrock call that went into
  an article) with AUD cost, and `published_by` (`ai_only` / `humans`;
  `humans_and_ai` reserved) across all four publish paths.
- (B): #59 -- lineage footer baked into static pages, compact one-line
  summary on article pages and lists, explicit "No data" fallback.
- (C) rotation: #60 -- per-topic `model_id_candidates`, one picked at
  random per run.
- (E): first pass, this PR -- a public `#/stats` page fed by
  `GET /stats`, aggregated purely from article lineage priced against the
  current Models registry (estimated AI spend, per day / model / topic).
  Chosen over AWS Cost Explorer for this pass: it needs no extra IAM or
  paid API calls, and it answers "what has the AI cost" directly.

**Deferred**
- Per-AWS-service spend and storage costs (Cost Explorer) -- the Stats
  page labels itself as an AI-spend estimate, not billing.
- Date-range filters on the Stats page (fixed last-30-days chart for now).
- A separate cost tracker for musings (`common/musings.py` calls are
  deliberately not part of an article's lineage, and are currently
  untracked).
- An AI reviewer for the moderation queue (would produce
  `humans_and_ai`).
