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
| Topics | `topic_id` | Topic config, cadence, adapter, optional `editorial_goals` |
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

Optional hooks (defaults on `Adapter` keep existing adapters unchanged):
- `build_summary_prompt(topic, diff_summary, new_state) -> str | None` — supply a
  domain-specific research-summary prompt instead of the generic one.
- `uses_previous_state = True` — the research tick then calls
  `fetch_state(topic_config, previous_state=...)`, letting an adapter reuse
  slow-changing data it already fetched earlier in the day.

Reusable capabilities live in `common/` rather than in one adapter:
`web_search.py` (keyless web/news search behind a provider interface) and
`http_retry.py` (exponential backoff for rate-limited APIs). The generic
`web_search` adapter turns any topic into "watch these search queries".

Editorial goals (`common/editorial_resolver.py`) say what a topic's research and
writing are *for*, resolved through a fallback tree so a new topic needs no code:
topic-specific (`editorial_goals.primary_focus` on the Topic) -> adapter-specific
(`ADAPTER_DEFAULTS`) -> global default (independent web research and synthesis of
5-10 relevant items). The Topic's `editorial_goals.exclusion_criteria` layer on
top of whichever applies. A topic created without an adapter defaults to
`web_search`, which searches on the topic's name when no query is configured.
The resolved goal is mirrored into the research summary (P1), ideation (P2) and
drafting (P3) prompts. It is separate from, and layered with, the crypto feed's
daily goal (`common/editorial_goals.py`: one of four -- altcoin deep-dive, crypto
news aggregator, trend inventor, general market news -- drawn at random each UTC
day, stable within the day), and never relaxes the relevance guardrails below or
the financial-topic safety rules. On a market-news day the crypto adapter's own
standing goal is skipped, since it would contradict a non-crypto day.

Topic relevance (`common/relevance.py`) is enforced at three points, always
keyed to the *active* topic's name, never a hardcoded topic:
- Collection: hacker_news / github_trending accept `adapter_config.keywords`
  (whole-word match, trailing `*` = prefix) to drop off-topic items before they
  reach a summary; `web_search` filters titles on `title_keywords`, defaulting
  to each query's own terms (`[]` disables). An adapter left with nothing
  relevant reports "no material change" instead of spending a model call.
- Research summary (P1): the generic prompt carries a relevance rule.
- Writing (P2/P3): ideation and drafting carry a critical relevance rule/
  boundary that forces off-topic findings to be ignored or reframed through
  the topic. Editorial goals (e.g. crypto) are applied inside that boundary.

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

Enhancements proposed here, each carrying its own status. Where one has
shipped, its entry records what shipped and what was decided rather than the
original proposal; where it hasn't, it is a design to be picked up in a
dedicated PR when explicitly requested.

### Static article publishing

**Status: implemented** (`common/static_pages.py`; every publish path renders the
page, and topic pages show a "researching" placeholder for unpublished work).

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

**Status: not done.** The module and production wiring exist, but `domain_name` and
`hosted_zone_id` are still empty in `infra/environments/production/terraform.tfvars`;
the remaining steps below are manual.

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

### Lineage cost fixes and the research tally

**Status: implemented.** Found from the site: most articles showed "No data" for
models/tokens/cost. Two causes. (1) Articles drafted before lineage tracking merged
have no token counts and cannot be recovered; they read "No data" honestly. (2) Every
*new* article's cost was blank too: the Models registry was never seeded, and the
model id recorded was the full inference-profile ARN, which could not match a registry
row keyed by profile id.

- *Canonical ids.* `common/model_pricing.py` reduces an ARN to the profile id it names.
  The model is still invoked by the configured id; the recorded id and price key are
  canonical. Lineage also stores `model_labels` (id -> readable name) for display.
- *Fallback prices.* A small built-in table (keyed by base model id, so every geo profile
  of a model matches) prices a model when the registry has no row; the registry always
  wins, so a price is corrected without a deploy. A model with neither is logged and
  left unpriced -- never guessed.
- *Research is a running tally, bundled into the article.* Each Finding records the
  Bedrock call behind its summary (`research_call`: model, tokens). When an article is
  written, the daily cycle sums the calls of every Finding in its window (before any
  goal filtering, so nothing is dropped) into `lineage.research` -- findings, tokens,
  models, per-call detail, cost -- and `total_cost_aud` = authoring + research.
  Windows are disjoint (they start at the topic's `last_article_at`), so each research
  call lands in one article. Findings written before this have no call and are counted
  as `untracked_findings`, with a note, rather than passing as free. The digest carries
  no research block (it summarises other topics' findings, which their own articles
  already count). The article page and Stats page show research tokens/cost and the total.
- *Audit and backfill.* `lineage audit` reports gaps; `lineage backfill [--apply]`
  recomputes cost from stored tokens and canonicalises old ARN ids.
- *Not done:* the research tick still uses `BEDROCK_MODEL_ID` directly rather than the
  per-topic/global model resolution the daily cycle uses (unchanged behaviour; the
  recorded model is whatever it actually called).

### Rolling research, whole-window articles, and a fresh-data review before publish

**Status: shipped, except (C) the fresh-data review: its shadow mode is built
(records only, changes nothing); enforcement is designed below and not started.** Written up from a review of how the research and authoring
pipelines behaved, checked against the code and the dev environment.

**Design constraint: topic-agnostic.** Topics can be about anything; the first
one built out happened to be crypto. Everything here lives in the generic
pipeline or behind the adapter contract, so every topic gets the
research-quality improvement and no domain's rules or vocabulary reach
another's. Domain knowledge (what an "item" is, what a day's editorial goal is,
which coins to look at) stays inside each adapter; the handlers only ask the
adapter.

**What shipped**
- *Daily cycle at 9 AM Sydney* (#69). `daily_timezone` (default
  `Australia/Sydney`) is stored on the topic and passed to EventBridge
  Scheduler as `ScheduleExpressionTimezone`, so `cron(0 9 * * ? *)` follows
  daylight saving. Only *new* topics get the new default; an older topic stays
  on UTC until moved on purpose (`admin_cli topics update <id> --daily-cadence
  "cron(0 9 * * ? *)" --daily-timezone Australia/Sydney`), so an unrelated edit
  never shifts its run.
- *No thresholds on novelty* (#69). Any item new to the topic makes a tick
  material. "Leaving a list" is no longer a trigger; a jump in a known item's
  score/stars still is, since that is a new fact.
- *"New" is judged against everything already reported* (#69). Every stored
  snapshot carries `_seen` (item key -> first-seen date, 7-day retention,
  2000-key cap), built from the adapter's `item_keys`, so an item that drops
  out of a feed and returns is not reported twice. This replaced the proposed
  Observations table: a tick with nothing new stores nothing and calls no
  model, and a tick with something new stores a Finding, so there was nothing
  for a separate store to hold.
- *Summaries cover only what is new and may not invent* (#69).
- *Articles read the whole window* (#69, #70). Every finding since the topic's
  previous article, at most 24h back (cap 48, 40k characters, oldest dropped
  first). The topic records `last_article_at` (the run's *start*, written only
  after an article exists and never allowed to fail the run, since a retry
  would write a duplicate), so a manual run followed by the scheduled one
  writes nothing the second time. `topics trigger --pipeline daily_cycle
  --force` ignores it for an intentional regenerate.
- *Research spend as a running tally* (#72). Each Finding stores the Bedrock
  call behind its summary; the daily cycle sums the whole window's calls into
  the article's lineage (`research`, `total_cost_aud`). See "Lineage cost fixes
  and the research tally" above.
- *Research interval from DynamoDB* (#73). The per-topic schedule stays a
  fixed hourly heartbeat and each tick asks whether it is due, against
  `research_interval_hours` on the topic, else the `pipeline` row in the
  ModelConfig table, else 1 hour. Whole hours (1-168); a manual trigger always
  runs. `admin_cli topics update --research-interval-hours N`, `pipeline-config
  set`. (Editing the schedule itself from DynamoDB was rejected: EventBridge
  only learns a schedule when the Admin API writes it, and rewriting schedules
  from a DynamoDB stream adds a stream, a Lambda and IAM for exact-cron control
  nobody needs.)
- *A fresh random coin pool every tick, and headlines on analysis days* (#74;
  crypto adapter only). The pool is an unseeded draw each tick, skipping coins
  already analysed that UTC day, so each tick has new information to report.
  A newly sampled coin counts as new. Only the day's *goal* stays date-seeded.
  Cost of this: history is fetched for each tick's pool (set
  `COINGECKO_API_KEY`), and analysis days now produce a Finding per tick -- the
  research interval above is the dial.

**Decisions on the earlier open questions**
- *Article window and cron time:* decided (9 AM Sydney = 22:00/23:00 UTC the
  day before, the end of the UTC day whose findings and goal the article uses;
  window = since `last_article_at`).
- *Observation storage:* none needed (see `_seen`).
- *Feed the previous summary into the next tick's summary prompt:* **decided
  against.** The delta-only prompt plus `_seen` already deliver "build on what we
  knew", and feeding old summaries back in works against "report only what is
  new" and invites the model to restate or embellish old material.

**Deferred**
- Article-body fetching for news. Needs a search provider that returns text, or
  page fetching with paywall and robots handling; a spike after (C).

---

#### (C) Fresh-data adversarial review before publish -- design

**Status.** PR 1 (shadow mode) is built: `common/fresh_review.py`, the adapter hook
(`Adapter.review_evidence`, with a crypto override), `common/adapters/registry.py`,
the record on the article (`review`) and on the moderation item (`review_notes`), the
`adversarial_review` lineage stage, and `pipeline-config set --review-mode off|shadow`
(default `shadow`). Where it differs from the design below: `review_mode` accepts only
`off` and `shadow` -- `enforce` is added with PR 2, so a setting can never claim more than
the code does; an adapter that opts out yields a `skipped` record; the daily-cycle
Lambda timeout is now 300s. PR 2 (the revision pass and routing to moderation) is not
started, and waits on about a week of shadow data from dev.

**Problem.** Nothing re-checks an article against reality before it goes out.
Drafts are written from finding *summaries*, hours old by publish time, and
the compliance review checks safety (PII/harm, or a deterministic route to
manual moderation for financial topics), not whether the claims are still
true. The moderation queue already shows the symptom: non-financial drafts are
flagged for figures like "gained over 170 stars" stated without support.

**Where it sits.** In `daily_cycle_handler._run_daily_cycle`, after the draft and
title and before the disclaimer and compliance review, so compliance sees the
final text:

`draft -> fresh-data review -> (revision) -> disclaimer -> compliance -> publish | moderation`

**1. Get fresh evidence through the adapter contract (no domain code in the
handler).**
- Move `ADAPTER_REGISTRY` out of `research_tick_handler` into
  `common/adapters/registry.py` so both handlers use it (a pure refactor, own
  commit).
- New optional adapter hook `review_evidence(topic_config, latest_state) -> str |
  None`. `latest_state` is the topic's most recent stored snapshot, so the
  adapter re-checks *what it was looking at*. Default: `fetch_state(topic_config)`
  rendered as compact text with internal keys (`_seen`, ...) removed and a
  length cap. Returning None opts the topic out.
- Crypto overrides it, because its pool is random per tick and a plain re-fetch
  would look at *different coins*: it reads the coin ids from the snapshot's
  `analyzed_today`, makes the one markets call (top 200: current price and
  24h/7d/30d change for every coin the article could mention, plus BTC/ETH), and
  the latest headlines. No per-coin history calls.
- GitHub Trending / Hacker News use the default (current trending list, current
  stars/scores).
- Time-boxed. A failed or slow fetch is `unavailable`, never a silent pass.

**2. The reviewer.** One tracked Bedrock call (stage `adversarial_review`) given the
draft, the window's finding summaries, and the fresh evidence, told to use only
that material and to return JSON only:
`{"claims": [{"claim", "problem": "stale|contradicted|unsupported", "evidence",
"severity": "minor|major"}]}`. Guidance: flag a claim only if it states a number,
rank, direction or fact as *current* and the material shows otherwise; ignore
ordinary short-term movement the draft doesn't present as current; when unsure,
flag nothing. `major` = central to the article's point; `minor` = peripheral.
Unparseable output is `unavailable`, not `clean`.

**3. Outcomes** (one record per article, `review` on the Articles item, never
public):

| Result | Non-financial topic | Financial topic (always moderated) |
|---|---|---|
| clean | publish path as today | as today |
| minor only | one revision pass (stage `revision`): fix the listed claims using only the fresh evidence and findings, add no new claims; then compliance | same, notes attached |
| any major | route to moderation with the notes as reasons | notes attached to the queue item |
| unavailable | route to moderation ("fresh-data review unavailable") | note attached |

Notes go on the ModerationQueue item, so `moderation list` shows the operator
*why*. Both stages appear in lineage (`calls`), so their cost is in the article
footer and on the Stats page.

**4. Safety.** Fetched text (headlines, page titles) is untrusted: it is passed
to the model as delimited data, the reviewer can only return the JSON schema
(it cannot act, publish or change routing except through a `severity`), the
revision prompt forbids facts not in the supplied evidence, and evidence is
length-capped.

**5. Rollout, in two PRs.**
- *PR 1 -- shadow mode.* Registry extraction, the adapter hook (default + crypto),
  the reviewer, storage on the article, lineage stages, and a global
  `review_mode` (`off | shadow | enforce`) on the `pipeline` config row
  (`pipeline-config set --review-mode`), defaulting to `shadow`: the review runs
  and is recorded but changes no outcome. Also raise the daily-cycle Lambda
  timeout from 120s (it already makes four Bedrock calls; this adds two and a
  fetch) and add a `review` summary to `moderation list`.
- *Run in shadow on dev for about a week.* Watch the share of articles flagged,
  the severity split, and sample the notes for false positives; tune the
  reviewer prompt and, if needed, per-adapter tolerance.
- *PR 2 -- enforce.* The revision pass and moderation routing, behind
  `review_mode = enforce`, then turn it on. Now scoped in full, with two
  prerequisite steps that fell out of reading the code, under "(C) Enforcement --
  scoped enhancement" at the end of this section.

**6. Cost.** +1 model call per article, +1 if it revises: a few cents a day per
topic at Haiku prices, and visible in the lineage.

**7. Tests.** Adapter hook (default, crypto reading `analyzed_today`, opt-out);
reviewer JSON parsing (fenced, invalid, empty claims); each outcome in each mode;
fetch failure and timeout are `unavailable`; the revision keeps structure and adds
no new claims (mocked model); evidence is delimited and capped; a non-crypto topic
never sees crypto code; lineage carries both stages.

**Limitations to state honestly.** A model re-reading its own draft cannot detect
real-world drift; the fresh fetch is what makes the review meaningful. News
review is headline-level until a provider with article text exists. The review
catches stale or unsupported figures, not subtle framing errors.

**Still open (decide from the shadow-mode data).** The severity threshold that
sends a non-financial article to moderation (starting point: any `major`);
whether a flaky fetch should force moderation or publish with a note; which
future adapters should opt in.


---

#### (C) Enforcement -- scoped enhancement (not started)

**Status: scoped, not started.** This is the second half of (C): shadow mode records a
review and changes nothing; enforcement makes the review *act*. It is deliberately
gated on real shadow data, so it is broken into three steps in order, the first two of
which are useful whatever is decided about the third.

**What reading the code found (these shape the scope).**
1. *Drafts are being cut off.* The draft, ideation and title calls all use
   `invoke_model_tracked`'s default `max_tokens=1024`. The tokenized-gold article's draft
   recorded exactly 1,024 output tokens, and its stored body stops mid-word ("...deep
   institutional liqu") just before the appended disclaimer. `invoke_model_tracked` does
   not surface Bedrock's `stopReason`, so nothing notices. This is a defect in its own
   right (a truncated article can be published), and it also sets the budget any revision
   pass needs.
2. *The title is never reviewed.* The title comes from a separate call that sees only the
   chosen angle, not the body, and the shadow review's input is the body. A headline
   such as "...While Bitcoin Crashed 30%..." is exactly the kind of claim the review
   exists to catch, and today it cannot.
3. *Financial and non-financial topics have different stakes.* Financial articles are
   always held for manual moderation, so for them enforcement adds *notes*, not routing.
   For everything else the compliance review only checks safety (PII/harm), so a stale
   figure is currently a straight publish.
4. *The digest is not reviewed.* It summarises other topics' findings; out of scope here.

**Step 0 -- fix truncated drafts (small; prerequisite, useful on its own).**
- `invoke_model_tracked` returns `stop_reason`, and records it on the lineage call.
- Give the draft call a budget that fits the article it asks for (order of 4,096 tokens),
  and treat a `max_tokens` stop as a problem: retry once with a larger budget, and if it
  still truncates, route the article to moderation with the reason "draft truncated"
  rather than publish half an article.
- Tests: the stop reason is surfaced; a truncated draft is retried, then held; a normal
  draft is untouched.

**Step 1 -- a review report (small; lets the decision be made from data).**
- `GET /review/report` and `admin_cli review report`: aggregate the `review` records on
  articles. Counts by status (`reviewed` / `unavailable` / `skipped`), by outcome, by
  topic; claims by `problem` and `severity`; the reasons reviews were unavailable; and
  the two numbers that matter for the decision, computed as *what enforcement would have
  done*: the share of non-financial articles it would have **held** (any `major`, or
  `unavailable`) and **revised** (`minor` only). A sample of recent flagged claims with
  their article ids (no article content), for eyeballing precision.
- A pure function over the articles the caller already fetched, like `lineage_tools`, plus
  a route (added to Terraform for dev and production) and a CLI command. No new data.

**Step 2 -- enforcement (the main change).**

*Modes and scope.*
- `review_mode` gains `enforce` (`off | shadow | enforce`). It stays `shadow` by default.
- A per-topic override: `review_mode` on the Topic (`topics update --review-mode`),
  resolved topic -> pipeline row -> `shadow`, the same pattern as the research interval.
  This lets one non-financial topic go first while the rest stay in shadow.

*The reviewer also sees the title.* The draft block becomes the title plus the body, so a
claim in a headline is reviewable. (This changes the review's input slightly from shadow
mode; note it when comparing.)

*Outcomes.*

| Review result | Non-financial topic | Financial topic (always moderated) |
|---|---|---|
| `clean` | as today (compliance, then publish or moderate) | as today |
| `minor` only | one revision pass, then compliance | same; notes attached |
| any `major` | **held**: routed to moderation, notes attached | notes attached |
| `unavailable` | per `review_on_unavailable` (below); default held | note attached |
| `skipped` | as today | as today |

*The revision pass* (a tracked call, lineage stage `revision`).
- Input: title, draft, the flagged claims with their evidence, the fresh evidence and the
  findings, all in the same delimited, defanged data blocks as the reviewer. Instruction:
  correct or remove only the listed claims using only facts present in the evidence or
  findings, add no other claims, keep structure, tone and length. Reply as JSON
  `{"title", "body"}`; anything else means the revision is rejected.
- Its `max_tokens` is at least the draft's (see Step 0).
- *Deterministic guards, no model involved* -- the model's word is not trusted to have
  added nothing: the revised text may not contain a number or URL that appears in none of
  the original draft, the findings and the evidence; its length must stay within about
  +/-35% of the original; and its heading count must not change. A revision that breaks a
  guard is *rejected* and the article is **held** with a note saying so.
- The original body is kept privately (`articles/<id>.original.md`, `body_original_s3_key`
  on the article) so a moderator can compare. One pass only; there is no second review of
  the revision (the guards bound what it can add, and it would double the cost).

*Holding.* The article follows the existing moderation path: the review contributes a
reason ("fresh-data review: 2 major claims") and the plain-words `review_notes`; nothing
about approve/reject changes. `compliant` becomes "compliance says fine *and* the review
did not hold it".

*When the review is unavailable.* A pipeline setting `review_on_unavailable`:
`hold` (default -- never a silent pass) or `note` (publish, and record that the review
could not run). The knob exists because a flaky source could otherwise flood the queue;
the report (Step 1) shows how often it would.

*Recording.* `review` on the article gains `revised`, `revision_rejected` (with why) and
`held`. Optional and for you to decide: a "Checked against current data" row in the public
lineage footer (clean / revised / held-then-approved), so readers can see an article was
checked.

*Data model and API changes.* Topic: `review_mode` (optional). Pipeline row:
`review_on_unavailable`. Article: `body_original_s3_key`, the extra `review` fields.
`PUT /pipeline-config` and `PUT /topics/{id}` accept the new settings; CLI flags to match.
No new table, no IAM change.

**Go / no-go from the shadow data** (starting points, to be argued with once there is
data; the report in Step 1 produces every number):
- at least about a week and 20 non-financial articles reviewed, across at least two topics;
- `unavailable` on no more than about 10% of reviews (otherwise fix the fetch first);
- would-hold no more than about 25% of non-financial articles (otherwise the queue
  becomes unmanageable, which is the thing this project cannot afford);
- on a hand-checked sample of at least 20 flagged claims, at least about 70% are real.
If any fails: tune the reviewer prompt or the per-adapter evidence and stay in shadow.

**Rollout and rollback.** Merge with the default still `shadow`. Turn `enforce` on for one
non-financial topic first, watch the queue and the report for a few days, then the rest;
crypto (already always moderated) last. Rollback is one setting, per topic or globally:
`review_mode` back to `shadow`. Articles already revised or held are unaffected.

**Cost and latency.** +1 call when a review is `minor` (the revision); nothing extra for
`clean` or `major`. Worst case (fetch 45s, review, revision, compliance) stays well inside
the 300s Lambda timeout. The report shows the real revision rate.

**Risks and what covers them.**

| Risk | Covered by |
|---|---|
| The reviewer cries wolf and floods the queue | shadow first; go/no-go on the would-hold rate; per-topic rollout; one-setting rollback |
| A revision quietly adds a claim | deterministic number/URL/length/heading guards; a broken guard holds the article |
| A flaky source holds every article | `review_on_unavailable`; the unavailable-rate gate |
| Web text tries to steer the reviewer or reviser | delimited data blocks, defanged tags, fixed JSON schema, the guards above |
| A truncated draft is reviewed and published | Step 0 |

**Tests.** Step 0: stop reason surfaced, retry then hold. Step 1: each aggregate, a topic
with no reviews, records of every status. Step 2: every cell of the outcomes table in each
mode; per-topic override and precedence; the title is in the reviewer's input; the revision
prompt and its JSON parse; each guard rejecting (new number, new URL, length, headings) and
the clean path accepting; original body stored; `review_on_unavailable` both ways;
`held`/`revised` recorded; a failing revision call holds rather than publishes; a
non-crypto topic never sees crypto code; shadow mode is byte-for-byte unchanged.

**Out of scope.** Reviewing the digest; article-body fetching for news; a second review of
a revision; anything that publishes without a human on a financial topic.

**Effort.** Step 0 and Step 1 are each a small PR. Step 2 is the largest single change in
(C): about a day of work plus the shadow-data wait, which is the real gate.

**Decisions needed from you** (recommendations in brackets): (1) unavailable review holds
or notes [hold]; (2) a per-topic `review_mode` override [yes]; (3) a public "checked against
current data" footer row [yes, it is honest about what the check is]; (4) re-review a
revised draft [no, rely on the guards].
