# BloggerBear Project Plan

## 1) Purpose
BloggerBear is an autonomous, multi-domain research-and-publishing platform. Topics are configured by admins and run two cadences:
- **Research tick** to refresh a rolling knowledge base -- a fixed heartbeat schedule, doing real work only
  when the topic's research interval has passed (see §4)
- **Daily authoring cycle** to ideate, select, draft, fact-check, review, and publish (or hold) one article

**Current state (September 2026):** phases 1–8 are built and running in production at bloggerbear.com with
four topics (GitHub Trending, crypto, tech market news, World of Warcraft) plus the cross-topic Trending
digest. Recent work: an operator-requested Re-Write of held articles, AgentCore web search as a fallback
when GDELT fails, web search usage and spend on the Stats page, and staggered research/authoring schedules.
The §11 entries below record each enhancement as it shipped. Still open: some Phase 0 GitHub/AWS
settings (branch protection, a production reviewer, a shared WAF ACL, a working destroy-dev) and Phase 6's
WAF tuning -- `docs/PROGRESS.md` tracks them.

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
  `us-east-1` regardless of hosting region, and the AgentCore web search
  gateway, which lives in `ap-northeast-1` (Tokyo) because the Web Search
  Tool connector isn't offered in Sydney (see §11, "Web search")
- **Compute**: Python 3.11 AWS Lambda functions
- **AI**: Amazon Bedrock (Claude, via Australian cross-region inference
  profiles). Models are chosen at runtime from a DynamoDB model registry
  (per-topic model, fallback model, and rotation candidates), not fixed in
  Terraform
- **Search**: GDELT (keyless, the default) with Amazon Bedrock AgentCore's
  Web Search Tool as the fallback
- **Storage**: DynamoDB + S3
- **Frontend**: Static site (S3 + CloudFront)
- **Security**: CloudFront + WAF + Shield Standard
- **IaC**: Terraform (≥1.10, for native S3 state locking — no DynamoDB lock
  table) with GitHub Actions deployment

## 4) Pipelines
### Research Tick
0. Heartbeat: each topic has its own EventBridge schedule (`research_cadence`). The tick
   does real work only when `research_interval_hours` (topic, else pipeline default) has
   passed since the last run, with 5 minutes' slack for scheduler jitter
   (`common/research_schedule.py`); otherwise it logs "not due" and stops
1. Load topic + adapter config
2. Fetch current source state via adapter (web searches go to GDELT, falling back to
   AgentCore web search when GDELT fails)
3. Diff against prior structural snapshot
4. If no material change: stop
5. If changed: summarize with Bedrock and store findings

Current schedules (runtime data, set through the Admin API, not Terraform):
- **Production:** every topic researches every 4 hours on a fixed UTC cron, staggered a
  minute apart (github-trending `:01`, crypto `:02`, tech-market-news `:03`, wow-forever
  `:04`, at 00/04/08/12/16/20 UTC), with a 4-hour interval.
- **Dev:** an hourly heartbeat at each topic's own minute (`:01`, `:02`, `:03`) with a
  10-hour interval, so each topic researches every 10 hours. A `cron(M 0/10 …)` heartbeat
  would *not* do this: it resets at midnight, giving 10h/10h/4h gaps.

### Daily Authoring Cycle
1. Generate candidate angles
2. Select one angle
3. Draft article and title
4. Fresh-data review: re-read current data and check the draft's claims (shadow or
   enforce mode; enforce may apply one guarded revision or hold the article)
5. Run compliance review (against the research the draft was written from)
6. Publish if compliant; else moderation queue

Production's daily articles run at 09:00–09:03 Sydney time, one topic per minute.

### Operator Re-Write (on demand)
From the review inbox, a held article can be sent for a Re-Write with a chosen model; it
runs in the background and comes back to the inbox for approval (see §11, "Re-Write").

## 5) Data Model (DynamoDB)
| Table | PK/SK | Purpose |
|---|---|---|
| Topics | `topic_id` | Topic config, cadences, research interval, adapter, models, optional `editorial_goals` |
| Findings | `topic_id` / `captured_at` | Research summaries, source hashes, the research call's tokens |
| CandidateIdeas | `topic_id` / `created_at` | Daily generated article ideas |
| Articles | `article_id`; indexes `status`/`created_at`, `topic_id`/`created_at` | Draft/review/publish lifecycle, lineage (tokens, models, cost), view count, rewrite history |
| Feedback | `article_id` / `feedback_id` | Scrubbed public feedback |
| PromptRefinements | `topic_id` / `version` | Prompt iterations and rationale; approved ones are "equipment" |
| ModerationQueue | `queue_id`; indexes `status`/`created_at`, `article_id`/`created_at` | Manual review tasks: `pending` → `approved`/`rejected`, or `rewriting` → `rewritten` |
| FailedExecutions | `failure_id` | Daily-cycle runs that exhausted their retries (dead-letter queue) |
| Musings | `musing_id` | The bear's short posts about articles, feedback and loot |
| Models / ModelConfig | `model_id` / `config_id` | Model registry and prices; single rows for the default model, pipeline config and feedback config |
| StatsCurrent / StatsHistory | `stats_id` / `week_start` | This week's counters (Bedrock spend, Lambda time, web search queries/spend, feedback) and past weeks plus an all-time row |

View counts live on the Articles item (`view_count`); there is no separate counters table.

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
`web_search.py` (web/news search behind a provider interface: GDELT by default,
AgentCore web search as the fallback, with an optional time budget) and
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
  applies `infra/environments/production`, meant to be gated by the
  `production` GitHub Environment's required-reviewer approval. **Not yet
  enforced:** the environment exists without a required reviewer (a paid
  GitHub feature on a private repository), so a published release deploys
  straight away. Because every release
  targets the same Terraform-managed production state, a new release
  replaces whatever was previously deployed rather than running alongside
  it.
- Both `dev` and `prod` are meant to require PRs and passing
  `terraform`/`security` checks via branch protection — no direct pushes to
  either. **Not yet enforced:** branch protection on a private repository
  needs GitHub Pro, so this is currently a convention.
- `infra/bootstrap` (the Terraform state backend itself — a single S3
  bucket, no DynamoDB table; locking is native to S3 via `use_lockfile`,
  Terraform ≥1.10) is the one exception to all of this: it's applied once,
  manually, locally, and is never wired into CI.
- One shared WAF Web ACL is meant to be associated with both the dev and production
  CloudFront distributions, rather than one each, to avoid paying its flat
  fee twice.

Full detail: `docs/specs/phase-0-foundations.md`.

## 9) Validation Commands
- Terraform: `terraform fmt -check`, `terraform validate`, `terraform plan`
- Python: `ruff check .`, `pytest`
- Security: `trivy config infra/`, `trivy fs --scanners vuln,secret lambdas/`, `bandit -r lambdas/ -ll`

## 10) Build Phases
Phases 1–8 are built and deployed; Phase 0's code is done, with a few GitHub/AWS settings still open.
In summary:

### Phase 0 — Foundations (code done; some settings open)
- CI workflows (Terraform + security), the `dev`/`prod` branch and release model (§8)
- Initial infra and lambda scaffolding, and the non-negotiable guardrails

### Phase 1 — Core Pipeline (done)
- Topic model and adapter path; diff-first research tick
- Daily ideation/selection/draft/compliance/publish chain
- Uncertain/financial outputs routed to the moderation queue

### Phases 2–8 — Refinement (done)
- Admin console API and CLI, scheduling automation, the public frontend
- The anonymous feedback loop and prompt-refinement automation (equipment)
- Observability (Stats, lineage and cost), hardening, and further adapters
  (crypto, web search, Hacker News, GitHub Trending)

Since then, work has continued as the §11 enhancements. See `docs/PROGRESS.md`
for the original phase 0–8 breakdown and acceptance criteria.

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

**Status: built, not yet connected.** The whole path is in Terraform and tested, `domain_name` is set
(`bloggerbear.com`), and `www.` is served and redirected to the bare domain. What is left is manual and in order:
create the Route 53 zone (in `infra/bootstrap`), point GoDaddy's nameservers at it, put the zone ID in
`infra/environments/production/terraform.tfvars`, and release. **The step-by-step, with checks, is
[docs/production-runsheet.md](docs/production-runsheet.md)**; `python scripts/domain_check.py` shows where the
domain stands at any time. The design notes below still describe why it is shaped this way.

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

**Decided: `www` redirects to the bare domain.** Once DNS leaves GoDaddy nothing else can forward it, so
production now also serves `www.<domain>`: the certificate lists it, CloudFront has it as a second alias,
Route 53 has its A/AAAA records, and a small CloudFront Function (`infra/modules/static-site/www_redirect.js.tftpl`)
answers it with a 301 to the bare domain, keeping the path and query string. It only acts on that exact host
and its target is fixed, so it cannot be used as an open redirect. The hosted zone was moved out of the
per-environment path into `infra/bootstrap` (with `prevent_destroy`) so its name servers survive a production
rebuild.

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

#### (C) Enforcement -- built, default still shadow

**Status: built; nothing changes until you turn it on.** This is the second half of (C):
shadow mode records a review and changes nothing; enforcement makes the review *act*. It
was scoped as three steps, gated on real shadow data. All three are built: Step 0 (#78,
truncated drafts), Step 1 (#79, `review report`) and Step 2 (the enforcement change). The
default `review_mode` is still `shadow`; turning `enforce` on is a decision to make from
`admin_cli review report`, against the go/no-go criteria below.

**How the decisions came out.** (1) An unavailable review **holds** the article
(`review_on_unavailable`, default `hold`, `note` to publish and record the gap). (2) There is
a **per-topic** `review_mode` override (`topics update --review-mode`), topic then pipeline
then `shadow`. (3) A public **"Fact check"** line appears in the article footer *only* when
the review was enforced (`common/fact_check.py`); a shadow-mode review changed nothing, so
claiming a check would overstate it, and the review record itself stays private. (4) A
revised draft is **not** re-reviewed; the deterministic guards bound what it can add.

**Built as scoped, with these details.** The reviewer also sees the title, and the
revision returns a corrected title and body as JSON. The guards are plain code
(`revision_violations`): no number that is in none of the original draft, the findings and
the fresh evidence (a source figure rounded to its own precision is allowed); no new link;
body length within 65-135% of the original; the heading count unchanged; a single short
title within 40-250% of the original length. A broken guard, a cut-off or unparseable
revision, or a failed call *holds* the article instead. The original body is kept at
`articles/<id>.original.md` (`body_original_s3_key`), private. An unexpected error while
enforcing holds the article; it never lets an enforced article through unchecked.

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

### The compliance review sees the sources

**Problem.** The compliance reviewer saw only the draft, so a figure taken straight from the
findings ("over 170 stars", "16,660 in total") read as an invented claim and nearly every
non-financial draft was held for a person. The reviewer wasn't wrong that it couldn't verify
the claim; it just couldn't see what the claim came from.

**What we learned.** Showing the model the sources made it *stricter* (0 of 6 real drafts
passed, against 2 of 6 before): it cross-checked rounded figures against every snapshot, read
"positioning security as a core capability" as investment advice, and called ordinary
enthusiasm a tone problem. It also changed its mind between calls on the same draft.

**What it does now** (`common/compliance.py`, `review_draft(..., source_material=...)`; the
daily cycle passes the findings block the draft was written from). The model only
*nominates*: each item is a labelled line with an exact quote, and plain code decides which
stand, the way the enforce-mode revision guards work.

| Item | Holds the article only if |
|---|---|
| Any figure in the draft (code, model not asked) | it is within 3% of no figure in the sources. Counts up to 20 and years are exempt |
| `FABRICATED` (model) | its quote holds a number or capitalised name found nowhere in the sources |
| `ADVICE` (model) | its quote holds a recommendation word (buy, sell, invest, price target...) |
| `PII` (model) | its quote holds a contact detail the redaction pass caught, or a street address |
| `NOTE`, tone, a name on its own, a loose or rounded figure | never; kept as a minor concern |

An empty answer, or one with no recognisable shape, still fails closed. A financial topic
still never calls the model and always goes to a person. A review with no source material
(the trending digest) is exactly what it was.

**A deliberate loosening.** Tone and "interpretation stated as fact" no longer hold an
article on their own. Whether a claim is *true and current* is the fresh-data review's job
(`review_mode: enforce`), which is now the check that matters for accuracy.

**Replay on real data** (six non-financial drafts, three runs each, Haiku 4.5): old prompt 2
of 6 passed; this design 18 of 18 runs. Three deliberately bad drafts (an invented funding
round, "you should invest your savings", a named person's address) were held 3 of 3 times.

### Feedback comments: screening, and what protects the votes

**Comments (built).** A comment is optional and is only kept if it is a civil, genuine
piece of feedback on the article. Anything else is **dropped: not stored, not
redacted-and-stored, not logged, not echoed back**, and neither is the submission it came with: the
vote is not recorded and nothing is counted against the feedback limits below. The response is a bare
`422`, never why (`common/comment_screening.py`). The reader can resubmit without the comment.

| Layer | What it catches | Model? |
|---|---|---|
| Length | more than 1,000 characters (also the textarea's `maxlength`), and anything that isn't a string | no |
| Text | control characters | no |
| Attacks | prompt injection ("ignore previous instructions", "system prompt", chat-role markers, model control tokens, our own prompt delimiters); SQL (`DROP TABLE`, `UNION SELECT`, `' OR 1=1`, `;--`); script/markup; shell | no |
| Links | any URL | no |
| Personal information | the existing regex pass (email, card, US-shaped phone), plus Australian and `+<country>` phone numbers. If it would redact anything, the comment is dropped instead | no |
| Everything else | a name, an address, hate, harassment, threats, rudeness, spam, off-topic, gibberish, anything unlawful or against a site's terms, instructions aimed at an AI or database | one call: `KEEP` or `DROP` |

Only the exact word `KEEP` keeps a comment; anything else, and any model error, drops it (an
optional comment is cheap to lose). The comment and article title are delimited as data, so an
instruction in them has nowhere to go; a hostile comment never reaches the model at all.

**SQL is not an actual risk here.** Comments go into DynamoDB as an attribute value through the
parameterised API (no SQL string is ever built), and are never rendered as HTML. They are
dropped anyway: no legitimate feedback looks like that, and a stored comment is later shown to
a model (the weekly reflection, which now also treats comments as delimited data).

**Replay against the dev model** (three runs each): 20 hostile or unwanted samples (SQL,
injection including "note to the reviewing AI", PII, hate, insults, a threat, illegal offers,
piracy, spam, gibberish, off-topic, oversize, control characters) dropped 60 of 60 times; the
four real comments left so far, and four ordinary ones, kept 23 of 24 (one blunt but civil
comment about confusing sources was dropped on one run: the model is strict by design).

**Votes (as they were before the feedback limits below).** What existed: the WAF rate limit of 500 requests per 5 minutes
per IP across the whole public API, plus the managed common rule set. What doesn't: any limit
per article, per visitor or per vote. Anonymous by design (the privacy policy promises no IP,
fingerprint or identifier is stored with feedback), so a visitor can vote as often as the rate
limit allows, and someone with several IPs without limit. What that can affect:

- `net_votes` picks the **few-shot example** article for a topic's future drafts, and feeds the
  weekly reflection's up/down tally. A prompt refinement still needs your approval.
- It cannot change what is published, and votes are not shown publicly.
- **Cost:** every comment that passes the code layer is one model call. At the WAF ceiling that
  is up to 500 per 5 minutes per IP. At about 500 tokens a call on Haiku 4.5 that is
  roughly $50 to $100 a day for one IP that sustained the ceiling with clean-looking comments (a
  hostile-looking one never reaches the model). This is the real exposure, and the reason to tighten
  option (1) below.

Options, none built yet: (1) a tighter WAF rate rule on the feedback route only (for example 20
per 5 minutes per IP), which costs nothing and stores nothing; (2) one vote per article per
browser, kept in `localStorage`, which stops casual repeats but not a script; (3) a salted,
expiring hash of the IP per article, which stops repeats properly but stores a derived
identifier, so the privacy policy would have to say so.

### Feedback limits: when BloggerBear is not taking feedback

Every **accepted** submission (a vote, with or without a comment) is one piece of feedback. Whether
BloggerBear takes it is decided by four checks, in this order; the first that is closed is the reason
the reader sees (`common/feedback_limits.py`).

| # | Check | Default | Reopens |
|---|---|---|---|
| 1 | **The article**: `feedback_locked` (true/false) on its Articles row, or `feedback_count` at `article_limit` | 50 per article | when you set `feedback_locked` false (and, if it was full, reset `feedback_count` or raise the limit) |
| 2 | **Site lockdown**: `locked_down` (boolean), optional public `lockdown_reason` | off | when you turn it off |
| 3 | **Daily limit**: `daily_limit` per day | 100 | the start of the next day in `daily_timezone` (Australia/Sydney) |
| 4 | **Rate limit**: `rate_limit_count` per `rate_limit_window_minutes` | 20 per 5 minutes | when the window ends |

**The article lock supersedes everything**: a locked article says "This article is locked" whatever
the site-wide state is. An article that reaches its limit gets `feedback_locked` set to true, so the
flag in the table always says so. It is a plain boolean on the article: flip it in DynamoDB, or use
`articles feedback-lock <id>` / `feedback-unlock <id> [--reset-count]`. A hand-typed `"true"` (as text)
also locks: a typo must not leave feedback open.

**What the reader sees.** Where the form was, `Hold your Paws! 🐾 / BloggerBear is not taking
feedback right now. / Reason: Rate limit / Try again in about 4 minutes.` The reasons are: "This
article is locked", "This article has reached its feedback limit", "Feedback is paused" (or your
`lockdown_reason`), "Daily limit reached", "Rate limit", and "Feedback is unavailable right now". The
form is never shown for a closed article (the SPA shows nothing until `GET
/articles/{id}/feedback-status` answers; a static article page starts with its buttons hidden). A
submission is refused server-side too: 423 for a lock or pause, 429 for a limit, 503 if the limiter
itself can't be read (it fails closed, because these limits are what stop a flood of comments running
up a model bill). A refused submission is refused before anything is screened or stored, so a closed
site costs no model call.

**Rejected feedback counts for nothing.** The order of a submission is: (1) is feedback open, a read
only; (2) screen the comment, if there is one; (3) only if it is kept, count it against the article,
the day and the rate limit, and store it. A comment that is rejected, by the rules or by the model,
rejects the whole submission: nothing stored, the vote not recorded, nothing counted, so junk cannot
use up the room real feedback needs (a hundred rude comments do not lock the day).

**But rejected comments still cost a model check**, and the limits above no longer bound that, so there
is a separate budget: `screening_limit` (default 300 a day, resetting with the day). A comment is only
sent to the model while checks are left; after that it is rejected unchecked, and a vote with no
comment still works. A comment stopped by the rules (SQL, injection, PII, links, oversize) costs no
check. At the default that is at most 300 model calls a day, a few cents.

**Where it lives.** The settings are the `feedback` row of the config table
(`bloggerbear-<env>-model-config`, beside `pipeline`): `locked_down`, `lockdown_reason`,
`rate_limit_count`, `rate_limit_window_minutes`, `daily_limit`, `article_limit`, `screening_limit`,
`daily_timezone`. Change them with `admin_cli feedback-config set --rate-limit 20 --rate-window-minutes
5 --daily-limit 100 --article-limit 50 --screening-limit 300 --locked-down true --lockdown-reason "..."` (`get` shows them, the defaults in
force, and today's and this window's usage), or edit the row in DynamoDB; a missing or invalid value
takes its default. The counters are rows in the same table (`feedback-window#...`, `feedback-day#...`, `feedback-screen#...`)
with an `expires_at` the table's TTL now clears.

**How it is enforced.** Each count is a conditional DynamoDB update ("add one, only if still under the
limit"), so two submissions at the edge cannot both get in, and a submission refused by a later check
gives back the counts it took. The windows are fixed, not sliding: a burst straddling two windows can
briefly reach twice the limit.

**Worth knowing.** The limits are site-wide, so they protect the bill (at most 300 model checks a day,
a few cents) but not the feature: anyone can send 100 *acceptable* votes and use up the day, and
rejected feedback can no longer do that. A refused submission costs nothing. If the first becomes a
problem the answer is a per-visitor limit, which needs a stored identifier the privacy policy
currently promises not to keep.

### Feedback verification: making automated submissions cost more

**Why not a game.** A "spec" for gamified CAPTCHAs (drag the honey to the bear, hold the button, tap in
order) was assessed and mostly not taken up. Each is bypassable by a script that fakes human timing or
by a vision model; drag, hold and order-tapping fail WCAG 2.2 (2.5.7 Dragging Movements, 2.1.1
Keyboard, 1.1.1 for SVG-only numbers); the "jitter and speed arc" checks are behavioural telemetry, which
sits badly with a privacy policy that promises no identifier or fingerprint; a random 15% trigger adds no
security; and the honeypot markup it gave uses an inline `style`, which our CSP (`style-src 'self'`)
blocks. What was taken from it: a signed token, a minimum time, escalation when it matters, and a
honeypot. Third-party CAPTCHAs (Turnstile, hCaptcha) stay a last resort: they need a CSP change and a
privacy-page change.

**What a submission now carries** (`common/feedback_verification.py`, `frontend/verify.js`):

| Layer | What it does | What it costs a person |
|---|---|---|
| **Signed token** | `GET .../feedback-status` hands out an HMAC-signed token for that article; `POST .../feedback` must send it back. A blind POST, a forged or edited token, a token for another article, an expired one, or a reused one is refused (`403`). Single use: the random value inside is recorded once when used, and expires with the token | nothing |
| **Not before** | The token is issued instantly but is valid only from a random moment 0.5 to 2 seconds later (`token_delay_min_ms`/`token_delay_max_ms`), enforced by the server. Nothing sleeps: a script that fetches and posts at once is refused, a person (who took far longer than 2 seconds) never notices. A fast client is told how long to wait and quietly retries | nothing |
| **Proof of work, when busy** | When the site is at or past `pow_threshold_percent` (70) of its daily limit, rate limit or daily model-check budget (`screening_limit`, which rejected comments use up), tokens need a number so that SHA-256(token + ":" + number) starts with `pow_difficulty_bits` (16) zero bits: about a second or two of browser CPU. Triggered by the site's own counters, never by watching a visitor | nothing to read or click; nothing for a screen reader or a switch user to do |
| **Honeypot** | A decoy field that looks like any other optional field (`referral_code`, labelled "Referral code (optional)"): no class, no telling name, and a label that says nothing about its purpose. Its wrapper carries the standard `hidden` attribute (which `normalize.css` already hides, so no rule of ours points at it, and it stays hidden if our stylesheet fails to load) and `aria-hidden="true"` (which removes the whole subtree from a screen reader's view); the input has `tabindex="-1"` and `autocomplete="off"`. A script that fills every input fills it: it is told it worked, and nothing is stored, counted, or spent | nothing |
| **WAF rate rule** | At most 20 submissions per 5 minutes per IP on the feedback route only, on top of the general 500. WAF counts the address and forgets it | nothing unless one address sends 20 in 5 minutes |

**Where it sits in a submission**: honeypot, then "is feedback open" (a closed site says why with or
without a token), then the token, then screening the comment, then counting and storing. A refused token
costs no model call and counts for nothing. A rejected comment spends its token, so the page fetches a
fresh one to retry.

**The key.** The signing key is created on first use and kept in the config table (row
`verification-secret`), readable only by whoever can read that table. Deleting the row rotates it; tokens
already out stop working (they live two hours at most). `verification_required` (default true) switches the
whole thing off in an emergency: `admin_cli feedback-config set --verification-required false`. If the key
or the used-token record cannot be reached, verification fails closed.

**What none of it stops.** Anyone can read the API, fetch a token, wait a second, and post; and one person
with many addresses can still use up the daily 100. These layers make each automated submission cost a
request, a wait, and (when busy) CPU, and remove the cheap attacks. Only a per-visitor identifier fixes
the rest, and the privacy policy rules that out.

**Settings** (the `feedback` row, with the limits above): `verification_required`, `token_delay_min_ms`,
`token_delay_max_ms`, `pow_threshold_percent`, `pow_difficulty_bits` (0 = never ask for work).

### Musings: BloggerBear's moods, and a bear for each

Each musing carries a mood, and the Musings page shows it: a bear feeling that mood where a bullet
point used to be, and a line of grey text after the musing, "BloggerBear was feeling proud".

**BloggerBear has five moods** (`common/musings.py`, `MOODS`), each set by real signal, not chosen at
random:

| Mood | When |
|---|---|
| **proud** | an article sailed through compliance on the first pass |
| **thoughtful** | an article needed a person first (moderation-approve or a force-publish) |
| **pleased** | the periodic feedback musing, when net votes are positive |
| **reflective** | the same, when votes are net negative or tied |
| **curious** | the same, when there was no feedback at all |

**The pictures** are `frontend/bears/<mood>.svg`, plus `default.svg` for a mood with no art of its own
and for an older musing with no mood. They are placeholders in a consistent style: to use your own,
**replace the file with the same name** (any square SVG; it is shown at 56px, 44px on a phone). The
pictures are served with `Cache-Control: no-cache`, so a replacement shows up at once. The picture is
decorative (no alt text): the grey line says the same in words.

**Adding a mood** means adding it to `MOODS` in `musings.py` and `frontend/moods.js`, a
`bears/<mood>.svg`, and its line in both environments' `frontend_files`. A test fails until all of
those agree, so a mood can't ship with a broken image. Until its art exists it shows the plain bear.

A mood word that isn't plain letters is never shown as text or used to build a file name.

### Scratch BloggerBear's tummy: a toy, never a gate

A small inline toy around the feedback moments, for the fun of it. It is **decoration only**: it has no
say in whether feedback is accepted (the token and proof of work do that, invisibly), it measures
nothing about how you play, and nothing about it is sent anywhere or stored (a test forbids `fetch`,
storage and cookies in it).

**Where it appears** (`frontend/tummy.js`):
- under **"Thanks for your feedback!"**, about one time in three (`OFFER_CHANCE`);
- always on the **"Hold your Paws!"** panel while feedback is closed, except when the site is simply
  broken ("Feedback is unavailable right now"), where a toy would be flippant.

Both the site and the static article pages get it. It is an inline card, not a modal: nothing traps
focus or blocks reading.

**Playing.** Press the bear (a real button: Enter, Space and a tap all work), or rub it with a mouse
(about 40px of movement is a scratch; a finger just taps, so it never fights page scrolling). Five
scratches and the bear purrs and swaps to its happy picture.

**Accessibility.** The button is named "Scratch BloggerBear's tummy"; the picture is decorative. The
words sit in a polite live region that changes at only four points (after 1, 3, 5 and 12 scratches; it
starts as the invitation), so a screen reader hears a few lines, not one per click. The wiggle exists only under
`prefers-reduced-motion: no-preference` (a test checks it), so with reduced motion the bear just swaps
picture and the words change. There is a visible keyboard focus ring.

**Your art:** `frontend/bears/tummy.svg` (idle) and `tummy-happy.svg` (purring). Replace them with the
same names; like the mood bears they are served no-cache, so a replacement shows up at once. (Two
full-body pictures rather than the mood faces, because the tummy has to be in the picture.)

**Not built, deliberately:** anything that scores how human the scratching looks, and any use of the
game to decide whether feedback goes through. Both were rejected in the anti-bot assessment.

### The review inbox: what needs a person, and one way to clear it

Two things wait for a person: **articles in the moderation queue** and **prompt-change proposals**. Both
are in `admin_cli inbox` (a summary, plus heads-ups if feedback is locked down or verification is off)
and `admin_cli approve` (`scripts/review_inbox.py`): one item at a time, one keystroke each (y approve,
r reject, z skip, v read it all, q quit, and w Re-Write for an article held for a reason -- see "Re-Write"
below), up to 30 a time, oldest first.

It goes through the Admin API, never straight to DynamoDB or S3, because approving an article does more
than flip a flag (it renders the page, publishes it, writes a musing). It needed one new admin-only route,
`GET /articles/{article_id}`, because the moderation queue lists items but not the article's title or text.

Each item is a `ContentSource` (`ModerationSource`, `RefinementSource`, and a `MockSource` for practice), so a
new kind of waiting thing is one small class. Skipped items are hidden from later runs for 24 hours in a local
file so a rerun gives the next batch; nothing skipped is changed. An item held for a reason asks for a second
yes before approving. Errors are shown per item and never end the run.

The spec this came from assumed a pending-posts table and a pending-images bucket; neither exists here, so
the sources are the two above.

### Equipment: approved prompt changes as gear the bear wears

**Status: all four PRs (the model, the injection, gear identity, wear, and the Stats-page paper doll).** An approved prompt refinement is *worn* rather than
merely approved; only worn gear is injected into the ideation and drafting prompts. `common/equipment.py` is
the pure rules; the state lives on the PromptRefinements items (`equipped`, `slot`, `scope`, `equipped_at`,
`unequipped_at`).

- **Armor** (helmet, chest, gloves, boots, sword, shield): *global* guidance, one item per slot. Equipping
  into a taken slot benches the old item (it stays approved and can be worn again).
- **Rings**: *topic* guidance for the item's own topic, at most 5 in all. When full, equipping needs a ring
  to replace, or the item waits in the backpack.
- **Wearing is not using.** A topic's rings are used in every article, but the bear takes in only some of its
  worn armor each time: a random number of pieces (at least one), chosen at random (`pick_armor`). What it used
  is recorded on the article, and that record is what later wear is tied to.
- **Backpack**: approved and not worn. Nothing there is injected, and it will only ever be a count in public.
- **Compatibility.** An item approved before this has no `equipped` field. It is "legacy": the latest per
  topic keeps being injected until the topic has a ring. Once an item has been equipped or benched it has the
  field and is never legacy again. Approving with no choice wears a ring for the topic, so nothing changes for
  the old workflow, except that guidance now stacks (all of a topic's rings plus the armor, capped at
  4,000 characters, dropping whatever does not fit).
- **What was worn is recorded.** `Articles.equipment_used` lists `{topic_id, version, slot}` per piece used;
  (only the armor actually taken in) `[]` means no gear, an absent field means written before gear existed. Never in the public projection.
  This is the data an effectiveness measure and wear-out would need; neither is built.
- **Loot drops.** The first time a piece of gear is worn, BloggerBear posts a "loot drop" musing (a new
  `kind`, `"loot"`, and a new mood, `excited`): a short model-written announcement in the same voice as
  every other musing, screened the same way a comment is (fails closed to a plain, always-accurate post),
  carrying a snapshot of the gear (name, rarity, slot, description, topic) so the post still makes sense
  if the gear is deleted later. `loot_announced_at` on the item stops a repaired-and-re-equipped piece
  announcing twice. `--no-announce` on approve/equip/create opts out; `POST .../announce` (`admin_cli
  equipment announce`) posts it later or again. A failure to announce never stops the equip.
- **Made by hand.** `POST /equipment` (`admin_cli equipment create`, guided or with flags) creates gear you
  wrote, approved from the start, optionally with a chosen rarity and name, and puts it on; `DELETE
  /prompt-refinements/{topic}/{version}` (`equipment delete`) removes it. Armor made this way is filed under
  the reserved pseudo-topic `global`. All checks (scope, topic, slot, rarity, name, room for another ring) run
  before anything is written.
- **Admin.** `GET /equipment`, `POST /prompt-refinements/{topic_id}/{version}/equip|unequip`, and an optional
  `{scope, slot, replace}` body on approve. The displaced item is benched first, so a failure part way leaves
  a slot empty rather than two items fighting over it. `admin_cli equipment ...`, and `approve` asks where
  the bear wears a prompt change.

**PR 2 of 4 (identity): names, rarity, durability, the bear's slot suggestion, the admin bump.**
`common/gear.py`. The weekly reflection names each proposal as it writes it, so the person approving sees
what the bear found.

- **Rarity is rolled by code**, weighted (common 50, uncommon 28, rare 14, epic 6, legendary 2; tunable in
  `RARITY_WEIGHTS`). The model never picks it, so it cannot be argued into a better one. Only an admin can
  raise it, and only up.
- **Durability** is the most wear an item can take, rolled once in its rarity's range: common 6-10, uncommon
  10-15, rare 15-20, epic 21-30, legendary 40-50. It starts full and never exceeds the maximum. A bump
  re-rolls the maximum in the new range (never below the old one) and adds the extra room without repairing.
- **The name is `<slot noun> of <theme>`.** The model writes only a short theme ("Plain Speaking"); the noun
  comes from the slot (Helm, Breastplate, Gauntlets, Boots, Blade, Shield, Ring), so moving an item renames it.
  The theme is public and derives from guidance that derives from anonymous comments, so it is screened like
  a comment: the same code rules (links, personal information, injection, SQL, markup), a character
  allow-list and a length cap, then a one-word model check that fails closed. Anything else, or any error,
  falls back to a theme built from the topic ("Github Trending Lore"). A proposal is never lost to its name.
- **The bear suggests a slot** (armor by what the guidance is about, a ring if it only fits its topic). With
  no slot named, equipping takes the suggestion if empty, else the first empty armor slot; the CLI's Enter does
  the same. The server's default on a bare approve is still a ring, so nothing widens by accident.
- **Older proposals** have no identity; one is rolled, once and stored, the first time an item is approved,
  worn or bumped (no model is called from the admin API).
- `POST /prompt-refinements/{topic_id}/{version}/rarity`, `admin_cli equipment bump`.

**PR 3 of 4 (wear): durability is the performance record.** `common/wear.py`, called from the public API's
feedback path right after a submission is stored.

- A stored **downvote costs each piece the article used 1 durability; an upvote gives 1 back**, never above
  its maximum. "Used" is `Articles.equipment_used`: the topic's rings and only the armor the bear took in that
  time, so gear it left out is neither blamed nor rewarded.
- **Only feedback that is kept counts.** A submission with a comment screened out, a bad token, a hit limit or
  a filled honeypot is turned away before anything is stored, so it wears nothing. (Bare thumbs count too;
  downvotes are still cheap to send, but the existing per-article and rate limits, the token and the honeypot
  already bound how many can land.)
- **Only worn gear is touched.** A retired or benched piece is not revived by an upvote on an old article; only
  an admin repairs it. The change is one conditional write (`durability > 0` / `durability < max_durability`,
  `equipped = true`), so concurrent feedback can neither push it below 0 nor above the maximum, and exactly one
  submission sees it reach 0.
- **At 0 the piece is taken off** (`unequipped_reason = worn_out`) and a spare may take its place. Every way an
  item gets to the backpack records why: `parked` (approved when there was no room), `shelved` (approved to
  the backpack by choice), `benched` (an admin took it off), `displaced` (something else took its slot),
  `worn_out`. **Only `parked` spares are put on automatically**, and only the same kind (a ring for the same
  topic, or global armor for an armor slot), never past the ring cap and never displacing anything, choosing
  the one with the most durability left. Today parking happens for topic items (a default approve with every
  ring worn), so in practice this replaces rings; armor is re-equipped by an admin.
- **Admin repair**: `POST /prompt-refinements/{topic_id}/{version}/repair` (`admin_cli equipment repair`),
  all of it or `--amount`, never above the maximum; the item stays where it is. Gear at 0 cannot be worn until
  repaired. Worn-out gear that is repaired goes back on by an admin's choice, which is the rotation the owner
  wants.
- The wear step never raises into the feedback path, and says nothing to the reader.

**PR 4 of 4 (the Stats page): what BloggerBear is wearing.** A section of `#/stats`, from a new public
`GET /equipment` (cached 60 seconds).

- **The paper doll.** Six armor slots down either side of the bear (empty ones drawn dashed), the worn rings in
  a row beneath (they appear as they are worn, at most five), and the backpack as **a count only**: the endpoint
  never returns what is in it, or anything about a proposal beyond what is shown on the gear.
- **Hover, focus or tap a piece** for its tooltip: **name, rarity, what the guidance says, its slot, the topic
  it is tied to (rings) or "Every topic" (armor), and its durability as a percentage with a bar.** The tooltip
  and the slot's outline are colour-coded like a game: orange legendary, purple epic, blue rare, green uncommon,
  grey common. Durability runs green, yellow, orange, red as it falls (75%+, 50%, 25%, below), and the words
  ("Good condition" ... "About to break") always say it too, so colour is never the only signal.
- **Accessible.** Each piece is a real `<button>` whose `aria-describedby` is its tooltip, so a screen reader
  hears the same thing without opening anything. The tooltip follows WCAG 1.4.13: it appears on hover, keyboard
  focus and tap, stays while the pointer is over it, and Escape dismisses it (a click holds it open). On a phone
  it is a panel along the bottom of the screen. The same facts are in "View the gear as a list". Rarity and
  condition colours are held to contrast minimums in light mode, dark mode and the always-dark tooltip by tests.
- **Art.** `frontend/gear.js` describes each picture as plain data (path, circle, rect; class names only, so no
  style attribute the CSP would block): a silhouette per slot, and rarity adds to it (nothing for common, a gem
  for uncommon, a ring around it for rare, sparkles for epic, rays and a double ring for legendary). Colours all
  come from `styles.css`. Replace the pictures by editing the specs; the bear in the middle is
  `bears/default.svg`.
- **What is shown is screened again.** The guidance text is a person-approved but anonymous-comment-derived
  string, so before it goes public it gets the same code rules as a comment (links, personal information,
  injection, SQL, markup) and is withheld ("The details of this guidance are not shown") if any applies; it is
  capped at 280 characters. The theme in the name is re-checked the same way.
- The gear loads separately from the numbers, so a failure there never hides the rest of the Stats page.

Settled with the owner: "equip only in part" means the bear takes in a random subset of its worn armor for each
article (PR 1), and wear comes from a stored downvote (1 point), repaired by a stored upvote.

**Built beyond the original four PRs.**

- **Make and delete gear by hand.** `POST /equipment` and `DELETE /prompt-refinements/{topic}/{version}`
  (`admin_cli equipment create`, guided or with flags, and `equipment delete`), covered above under PR 1's
  "Made by hand" and the loot-drop bullet.
- **The "Equipment used" record on a static article page.** `common/static_pages.py`: when an article's
  `equipment_used` names gear, a second footer sits beside Lineage (side by side on a wide screen, stacked
  below it on a narrow one via `.article-footers`, a `flex-wrap` container) -- the gear's name, rarity, slot
  and what it says. It is a **snapshot taken once**, at the moment the page is rendered (`get_prompt_refinement`
  read there and nowhere else), and baked into the static HTML: no script, no API call, and deliberately never
  re-fetched, so it stays exactly as it was even if that gear is later deleted, repaired, worn out, or bumped
  in rarity -- the same "record what happened, not what is true right now" choice as the loot-drop snapshot and
  as Lineage itself (`published_by`/cost are fixed at draft/approval time too). Present only when the article
  actually used gear; an article with none, or from before this existed, shows no gap. Never touches the
  Lineage code -- a separate function, its own footer, joined only by the shared wrapper's layout.

### Observability: a Stats table, a Historic table, and everything Bedrock spends that isn't on an article

**Status: all five PRs of an owner-scoped series done** (recording what was previously untracked; the
weekly rollover job and Lambda billed-duration tracking; API Gateway cost via Cost Explorer; the
Stats-page UI for all of it; a one-time backfill of pre-existing article spend). Prompted by the owner
asking how tokens are calculated and finding two real gaps: no dedicated Stats table existed
(per-article `lineage`, aggregated live by `common/stats.py`, was the whole story), and four real
Bedrock-calling code paths were never tracked at
all.

**Two new DynamoDB tables** (`infra/modules/app-data`): `StatsCurrent`, one row (`stats_id = "current"`),
updated in place all week with `ADD` expressions -- the same pattern the ModelConfig table's rate-limit
counters already use (`common/dynamo.py`'s `consume_feedback_counter`), so the row and every attribute
on it come into existence on first use, nothing has to create it first. `StatsHistory`, one row per
completed week (hash key `week_start`, the Monday it covers), the same shape, written once by a rollover
job this PR does not build yet -- empty until it exists. Not everything on either row is meant to reach
the public Stats page; some of it (see below) is for the owner's own troubleshooting.

**Four previously-untracked call sites now go through `common/stats_tracking.py`'s `tracked_claude`**
(same signature and return as `common.bedrock.invoke_claude`, so each call site's own diff is one line)
instead of the plain, untracked `invoke_claude`:

- `musings` -- every article, loot-drop and feedback musing (`common/musings.py`)
- `weekly_reflection` -- a topic's rationale and suggested change (`weekly_reflection_handler.py`)
- `gear_identity` -- naming a new piece of gear, and its name-safety check (`common/gear.py`)
- `comment_screening` -- KEEP/DROP on a reader's comment (`common/comment_screening.py`)

`tracked_claude` prices the call the same way per-article cost already is (`common/costing.py`'s
`pricing_for`/`call_cost_usd`), and tallies calls/tokens/cost onto `StatsCurrent` under that category --
an unpriced model is counted in `{category}_unpriced_calls`, never silently costed at zero, same rule
`common/stats.py` already holds itself to. Deliberately **not** done: splitting ideation out as its own
category (it stays bundled into whichever article gets published, as it always has -- owner's call).
Recording a tally never loses the model's actual answer: a DynamoDB failure here is logged and
swallowed, the generated text is still returned, the same "bookkeeping must never break the real
work" rule as every other cross-cutting concern in this codebase.

**Reader-activity counters, also on `StatsCurrent`:** `feedback_given` (a stored submission --
`public_api_handler.py`'s `_submit_feedback`) and `feedback_rejected_comment` (a whole submission thrown
away because its comment failed the content screen -- narrower than "every way a submission didn't
succeed": a closed site never let the reader try, and a bad token or a filled-in honeypot is a caught
bot, neither is what a person reads as "my feedback was rejected"). `loot_drops` (an activity count, not
a cost figure -- `common/musings.py`, the moment a loot-drop musing is actually written).

**PR 2 -- built:** the weekly rollover job (`stats_rollover_handler.py`, a static EventBridge Scheduler
job like `weekly_reflection`'s, `cron(15 9 ? * MON *)` -- 15 minutes after `weekly_reflection`'s own
Monday run, so that Monday's reflection cost lands in the week it is reflecting on, not the new week
just starting). It copies `StatsCurrent` into a new `StatsHistory` row keyed by the week it covers
(`common/dynamo.py`'s `put_stats_history_row`, a conditional write -- a retried or duplicated invocation
never overwrites an already-rolled-over week), then clears `StatsCurrent` (`delete_current_stats`) so
the next Bedrock call or reader-activity event starts the new week's row fresh. "Total Stats" on the
public page will sum `StatsHistory` and must say it excludes the current week, still in `StatsCurrent`,
per the owner's steer.

Also in PR 2: Lambda billed-duration tracking, self-timed (`common/lambda_timing.py`'s
`track_lambda_duration` decorator, `time.perf_counter()` around each handler's own body -- close to but
not identical to AWS's billed figure, chosen over pulling CloudWatch's own `REPORT` lines/metrics after
the fact, for build quality over exactness, the owner's explicit call). Applied to every scheduled
pipeline handler (`research_tick`, `daily_cycle`, `weekly_reflection`, `musing_feedback`,
`trending_digest`, `dlq_handler`, `stats_rollover` itself) and deliberately **not** to `admin_api`/
`public_api`, so live reader traffic never takes an extra write. One wrinkle worth knowing: because
`stats_rollover_handler.handler` is itself decorated, its own duration is recorded *after* `_roll_over()`
has already cleared `StatsCurrent` -- so a fresh row for the new week reappears immediately, seeded with
nothing but that run's own `lambda_ms_stats_rollover`. Correct and intentional: the rollover job's own
cost belongs to the week it actually ran in, not the one it just archived.

Found and fixed while building PR 2's tests: a real bug in PR 1's `increment_current_stats` (already
merged into `dev`) -- its DynamoDB `ADD` expression referenced each field's *resolved* attribute name
instead of the literal `#f{n}` placeholder token, which raised a `ValidationException` on every call.
Fixed in the same PR 2 commit rather than a separate hotfix; called out here in case anyone finds it
independently.

**PR 3 -- built:** API Gateway cost via a daily Cost Explorer poll (`cost_explorer_poll_handler.py`,
`common/cost_explorer.py`). `ce:GetCostAndUsage`, filtered to `SERVICE = Amazon API Gateway`, summed
over a rolling 30 days ending *yesterday* (Cost Explorer's own data lags real spend by roughly a day, so
today's figure would be partial and understate the true cost -- the query's exclusive `End` is always
today, never tomorrow). Queried at `DAILY` granularity and summed here, not requested as `MONTHLY`:
Cost Explorer's `MONTHLY` granularity requires the queried period to align to calendar-month boundaries,
which an arbitrary rolling 30-day window does not. Chosen over hand-maintaining AWS's per-request price
ourselves, the owner's explicit "least complex to troubleshoot / manage" steer, even at the cost of that
~24h lag and `GetCostAndUsage`'s own small per-call charge (~$0.01/request -- why this polls once a day,
not more often). The reading is written straight onto `StatsCurrent` as a refreshed snapshot (`SET`, via
`common/dynamo.py`'s new `set_current_stats_fields`), not accumulated (`ADD`) like every other field on
that row -- a repeat poll overwrites the previous reading rather than compounding onto it. `ce` has no
regional API of its own; queried via `us-east-1` regardless of the stack's own `ap-southeast-2`. New IAM
grant (`ce:GetCostAndUsage`, `resources = ["*"]` -- Cost Explorer does not support resource-level
permissions, so this is the correct, narrowest scope, not an oversight). **Requires a one-time manual
enablement of Cost Explorer in the AWS console before `GetCostAndUsage` returns real data** -- cannot be
done via Terraform or the CLI. Until it's turned on in both accounts, `cost_explorer_poll_handler.py`'s
own top-level try/except means a rejected call is logged and reported as `{"status": "error"}` rather
than crashing, the same as any other scheduled handler's real failure -- it is not silently masked as a
$0 reading.

**PR 4 -- built:** the Stats-page UI. `record_article_lineage` (`common/stats_tracking.py`) folds each
drafted article's own already-tracked cost into the same weekly row too, under a fifth category,
`articles` -- the owner's call ("everything is therefore per week"): Weekly/Total Stats would otherwise
leave out the largest share of AI spend just because it wasn't tracked the same way musings/
weekly_reflection/etc. are. Unlike those four, an article is tallied per-article, not per Bedrock call
within it (per-model/per-topic/per-day detail stays `common/stats.py`'s job, read live off Articles).

`StatsHistory` also gained a permanent running-total row (`week_start = "all-time"`, a sentinel that
can never collide with a real Monday date) -- the owner's steer, once they realised "Total Stats" would
otherwise mean a scan-and-sum over every week there has ever been. `stats_rollover_handler.py` now
folds each just-completed week onto it too (`common/dynamo.py`'s `increment_stats_totals`/
`set_stats_totals_fields`, `common/stats_tracking.py`'s `split_for_rollover` deciding which fields are
additive counters versus the API Gateway reading, a rolling-30-day snapshot kept as the latest value,
never summed). "Total Stats" is therefore one `get_item` away, same as "Weekly Stats" (`StatsCurrent`),
never a scan.

The public `/stats` route now returns `weekly` and `historic` alongside the original per-article
`totals`/`by_model`/`by_topic`/`daily` (`common/stats_tracking.py`'s `public_view`, applied to
`StatsCurrent` and to `StatsHistory`'s all-time row -- the same shape either way). Not everything on
those rows reaches the page: the per-function Lambda breakdown stays internal (only one combined
pipeline-run-time figure is public, the owner's call), and `api_gateway_cost_as_of` never leaves
`common/stats_tracking.py` at all (owner-only troubleshooting).

On the page itself (`frontend/app.js`): a Quick Links nav under the title jumps to Total Stats, Weekly
Stats and Gear. Total Stats now contains the original per-article detail (always fully live, unwindowed)
*plus* the historic all-time categories/feedback/loot/pipeline-hours/API-Gateway figures, explicitly
labelled as excluding the current week. Weekly Stats is the same category/feedback/loot/pipeline-hours/
API-Gateway shape, just from `StatsCurrent`. Gear moved to the bottom of the page, superseding the
earlier fix/stats-gear-first order, now that there's real financial data above it to lead with. The
"Estimated spend per day" heading and its "View as table" twin now say how many days they cover.

**PR 5 -- built:** a one-time catch-up, `POST /stats/backfill-articles` (`admin_api_handler.py`'s
`_stats_backfill_articles`, `admin_cli stats backfill-articles [--apply]`) -- run once, after PR 4
deploys, before the separate Cleanup PR touches anything: article spend was never folded into
`StatsCurrent`/`StatsHistory` before `record_article_lineage` existed, so every article drafted before
that deploy would otherwise be invisible to Weekly/Total Stats even though its cost is sitting right
there on the Articles table. `common/stats_tracking.py`'s `plan_articles_backfill` sums every existing
article's lineage into one totals dict (the exact shape `record_article_lineage` would have tallied,
via a shared `_lineage_tally` the two now split out between them) -- pure and read-only, same
"caller already fetched the data" shape as `lineage_tools.py`'s own `plan_backfill`. A dry run unless
`{"apply": true}`; applying it folds the totals onto `StatsHistory`'s all-time row in one shot
(`to_stats_updates` Decimal-wraps only at that write boundary) and writes a reserved marker row
(`week_start = "articles-backfill"`, via the same conditional `put_stats_history_row` a real week's
row already uses) so a second run -- retried, duplicated, or just run again on purpose -- can never
fold these articles in twice; it reports `already_run: true` and touches nothing.

**Deliberately not backfilled, and never can be:** the four other categories (`musings`,
`weekly_reflection`, `gear_identity`, `comment_screening`) and Lambda billed duration. None of them
were ever tracked before PRs 1/2 existed -- there is no stored token count or duration anywhere to sum,
so inventing one would be a guess dressed up as data, the same principle the existing lineage-backfill
CLI already holds itself to for an article drafted before *that* tracking existed. API Gateway cost is
technically recoverable from Cost Explorer's own history (it answers for past date ranges, not just
"now"), but was left out of this PR as the lowest-value figure to chase, not a limitation of the
approach -- a candidate for a later, separate follow-up if it turns out to matter.

One thing worth knowing before running any of this against production data: Findings already expire via
DynamoDB TTL after `FINDING_TTL_DAYS = 14` (`research_tick_handler.py`, unrelated to this series) --
older research-cost data has been quietly aging out of reach this whole time, regardless of this PR or
the separate Cleanup PR's own retention proposals. Articles themselves carry no TTL and are kept
indefinitely, so this backfill's own numbers are not racing against anything.

### Cleanup: TTLs, an expiring snapshot prefix, and 90-day log retention

**Status: built.** Prompted by the owner's own brain-dump review (checked, item by item, in an earlier
review pass before any of the Observability PRs above): most items ended up bounded-TTL additions to
existing DynamoDB tables rather than a new scheduled sweep job -- once an item carries its own
`expires_at`, DynamoDB clears it on its own; nothing needed to run and decide "is this old enough yet."
The one place that isn't true is the S3 lifecycle rule below, which is AWS's own equivalent for objects,
not DynamoDB TTL -- so there is, in the end, no new Lambda anywhere in this PR at all.

**CandidateIdeas and FailedExecutions get a TTL** (`ttl { attribute_name = "expires_at" }`,
`infra/modules/app-data/main.tf`), `common/dynamo.py`'s `put_candidate_idea`/`put_failed_execution`
setting `expires_at` to `CLEANUP_TTL_DAYS` (7) from write time on every item, unconditionally -- both are
pure operational lists (a topic's own ideation scratchpad; an admin-visibility DLQ record), nothing
aggregates either across history. `FailedExecutions` originally had a comment saying the opposite
("meant to persist for later review, not expire like Findings") -- revisited here since, unlike
ModerationQueue below, nothing actually reads it back historically either.

**ModerationQueue and PromptRefinements get a *conditional* TTL** -- `common/dynamo.py`'s
`update_moderation_status`/`update_prompt_refinement_status` only set `expires_at` when the new status is
"rejected", never on "approved"/"pending"/equipped, so real, adopted history (an approved article's
queue record; an equipped prompt refinement) persists exactly as before. For PromptRefinements this is a
completely free cleanup -- nothing reads a rejected version back historically. For ModerationQueue it is
**not**: `admin_api_handler.py`'s `_moderation_queue_stats` reads rejected items' reasons "across all
history" to inform compliance-prompt iteration (Phase 6), and that history now only reaches back as far
as `CLEANUP_TTL_DAYS`. Flagged clearly rather than quietly shipped -- that endpoint's own docstring now
says so -- but implemented rather than left out, since dropping the cleanup item entirely was the only
other option and the owner had not ruled either way by the time this PR was built.

**Raw source snapshots expire via an S3 lifecycle rule**, not a DynamoDB TTL: a new rule on the content
bucket scoped to the `snapshots/` prefix (`expiration { days = 21 }` in dev; the same, but only retiring
the *current* version, in production's already-versioned bucket -- the existing 30-day noncurrent-version
rule then clears that copy too, same as it already does for articles). 21 days, not the Findings table's
own 14-day `FINDING_TTL_DAYS`: `daily_cycle_handler.py`'s fresh-data review reads a Finding's
`raw_snapshot_s3_key` at draft time, and DynamoDB TTL deletion can lag up to roughly 48 hours past a
Finding's actual expiry -- expiring the snapshot at exactly 14 days risked deleting one a
still-technically-alive Finding could still be pointed at. The extra week is a safety margin over that
lag, not a second independent retention decision, and it was accepted as such ("okay snapshots already
deleting, I'm fine with that") in place of the original flat 7-day ask. This same rule also covers
"delete orphaned snapshots once their topic is deleted" as the same case, not a separate mechanism: a
deleted topic's Findings simply age out on their own normal clock like any other, dragging their
snapshots down with them regardless of whether the topic that made them still exists.

**Every pipeline/API Lambda's own log group gets `retention_in_days = 90`** (`aws_cloudwatch_log_group.lambda`,
one `for_each` per environment over the same `local.pipeline_lambda_function_names` list
`module.observability`'s alarms/dashboard already used -- extracted out of that module call so both share
it and can never drift apart when a function is added or removed). Lambda creates a function's log group
itself on first invocation with no retention at all, so this was growing forever; IAM for Terraform to
manage it was already provisioned ahead of time in `infra/bootstrap/main.tf`'s `LambdaLogGroups`
statement, unexercised until now. Since every one of these functions has almost certainly already run at
least once in both environments, its log group already exists in AWS -- the first apply after this change
needs each one imported first (`terraform import 'aws_cloudwatch_log_group.lambda["<function_name>"]'
/aws/lambda/<function_name>`, once per function per environment), or it fails with
`ResourceAlreadyExistsException`.

**Left exactly as they were, on purpose:** Findings' own TTL stays at 14 days, not lowered to 7 --
shortening it risked a topic whose article cadence runs slower than a week losing research cost before an
article ever got to bill it. `ModelConfig`'s "tokens issued" (the feedback verification nonces / rate-limit
counters) already had their own `expires_at` and self-clear via TTL, from before this PR existed -- the
"tokens" in that table's `verification-secret` row is an unrelated, deliberately permanent HMAC signing
key, not a rate-limit counter, and was never a cleanup candidate. Articles are kept indefinitely, per the
owner's explicit call.

### Bugfix: a refusal parsed as an angle or a title (two real production incidents)

**Status: fixed.** Two things the owner found live on bloggerbear.com in the same week: two consecutive
`github-trending` articles both wrote about the same repo (Google's Ax) back to back, and one
`tech-market-news` article published with its **title** literally reading *"I'd be happy to help you write
an article title, but I'm not sure I understand your request. You've indicated that you cannot propose
article angles based on certain findings, but you haven't shared: 1. What those findings are..."* --
Bedrock's own confused reply to a nonsensical prompt, stored and served as-is.

Root cause, confirmed against the actual production `CandidateIdeas` row for that article: ideation
refused outright that day ("I cannot propose article angles based on these findings... the findings
consist almost entirely of..."), because it judged the day's research didn't support the topic's editorial
mandate. `daily_cycle_handler.py`'s `_ideate` parser had no way to tell that refusal's own sentences apart
from real proposed angles -- it just harvested whatever non-empty lines came out after stripping list
markers, and (Phase 1's deterministic "pick candidate zero, no scoring model yet") the refusal's own first
sentence became *the angle*, fed into both the draft and title calls. Drafting recovered by improvising a
topic from the raw findings; title-writing had nothing to improvise around, and answered the nonsensical
"angle" as literally and earnestly as a confused person would -- and `_draft_title` had zero validation on
what came back, so that answer became the title.

**`_is_plausible_reply(text, max_words)`** is the one guard behind both fixes: rejects multi-line text,
text over the word cap, and text containing a short list of refusal/clarifying-question phrases (`"i
cannot"`, `"i'm not sure"`, `"could you provide"`, `"as an ai"`, etc. -- not exhaustive, just what the two
real incidents actually said). Applied at both ends of the pipe:

- **`_ideate`** filters every candidate line through it (`_MAX_ANGLE_WORDS = 40`) before treating it as a
  real angle; the old unconditional "use the whole raw reply as one angle" fallback -- the thing that let a
  multi-paragraph refusal through in the first place -- now only fires when that whole reply itself passes
  the same check. If nothing survives, `handler()` bails out cleanly (`{"status": "no_usable_angles", ...}`),
  the same shape as the existing "nothing new since last run" bail-out, rather than drafting from a
  candidate that was never really an angle.
- **`_draft_title`** (`_MAX_TITLE_WORDS = 20`) checks its own reply, and gets one automatic retry with a
  firmer prompt on the same model if it fails -- tokens from the discarded first attempt are still summed
  into the lineage call, same "a wasted attempt still cost money" principle `invoke_model_tracked`'s own
  `max_tokens` retry already follows. If the retry still fails, the (still bad) text is kept, not discarded
  -- so a person reviewing it in moderation, where the article is now held (`IMPLAUSIBLE_TITLE_REASON`,
  alongside `TRUNCATED_DRAFT_REASON` in the same `hold_reasons` list), sees exactly what the model actually
  said, never something invented in its place.
- The title prompt itself also now explicitly states the word cap and says not to ask clarifying questions
  -- the original prompt never told the model either of those things, which is arguably why a
  clarification felt like a reasonable reply to make in the first place.

**Separately, the duplicate-topic problem**: `_ideate` only ever saw that day's research findings, with no
memory of what the topic already published. A GitHub repo that stays trending for several consecutive days
(Ax's star count kept climbing) looks like fresh material every single day, because the numbers genuinely
are new -- the story just isn't. `common/dynamo.py`'s new `list_recent_article_titles(topic_id, limit=5)`
(same Scan + FilterExpression shape as the existing `get_top_voted_articles`, sorted by `created_at`
instead of `net_votes`) feeds the topic's own last few published titles into the ideation prompt under an
"Already covered recently -- propose a genuinely different angle" header, only when there's something to
show; a topic's first article, or one with nothing published recently, sees the exact prompt as before.

The bad title already live in production was fixed by hand (DynamoDB `title` update, then re-rendering the
static page and invalidating its CloudFront cache -- the same three steps every other publish path already
does, just driven manually since there was no admin route for "fix just the title" at the time).

### Re-Write: fixing a held article without writing it by hand

**Status: implemented** (#130; `lambdas/common/rewrite.py`, `scripts/review_inbox.py`'s `w` key).

A held article can be rewritten from the review inbox: press `w`, pick one of the registered, enabled
models, and the inbox moves straight on. `POST /moderation-queue/{queue_id}/rewrite` claims the queue item
(`pending` → `rewriting`, a conditional write, so a second trigger gets 409) and invokes the daily-cycle
Lambda asynchronously with `{"action": "rewrite"}`. It runs there, not in a Lambda of its own, because it
needs exactly that Lambda's timeout, permissions and CoinGecko key.

- **Input to the model:** the article, the reasons it was held (minus the routine "financial topic"
  routing reason) and its review notes, the findings it was written from, and fresh data from the adapter.
- **Guards:** the same plain-code checks as the automatic revision (`revision_violations`: no figure or
  link that appears in none of the sources, a sensible title), with a looser length bound and no heading
  check, since fixing "investment advice" can mean removing a section. Then the fresh-data review and the
  compliance review run again on the new text.
- **Always back to a person:** the old item becomes `rewritten`, and a new `pending` item carries the
  rewritten article back to the inbox with the model and cost (`Re-Write #n by <model> (~$x AUD)`). A
  rewrite never publishes anything.
- **Nothing lost:** the replaced text is kept at `articles/<id>.before-rewrite-<n>.md`. Any failure puts
  the original item back to `pending` with `last_rewrite_error`; a rewrite still `rewriting` after 15
  minutes is released the next time the queue is listed.
- **Cost:** every call (stage `rewrite`, plus the review calls) is added to the article's lineage and the
  week's Stats, rejected attempts included.

Routing it through a "draft" status and the hourly tick was considered and rejected: it mixes unrelated
jobs into the research tick, adds up to an hour's wait, and would need a new article status handled
everywhere.

### Web search: GDELT with an AgentCore fallback

**Status: implemented** (#129, #132; `lambdas/common/web_search.py`, `infra/modules/web-search`).

GDELT, the keyless default, proved unreliable from Lambda: connection timeouts and 429s, with one search
taking 80s+ with retries. The crypto topic's fresh-data review timed out daily, and `wow-forever` never
produced a finding. Our own volume (tens of searches a day) was far below GDELT's published limit, and
the same failures reproduced from a home connection, so the cause was GDELT's own throttling, not load.

- **Time budgets (#129):** `search_web` takes an optional `deadline` that caps a whole search, retries
  included. The crypto review's headlines get 15s of the review's 45s, and the review goes on with prices
  alone if they run out.
- **AgentCore fallback (#132):** Amazon Bedrock AgentCore's managed Web Search Tool, through a gateway in
  `ap-northeast-1` (the connector's closest region to Sydney), with IAM (SigV4) inbound auth and connector
  version 1.2.0 for the published-date filter. `AgentCoreProvider` makes one signed MCP `tools/call` per
  search, rewrites GDELT syntax into a plain query of at most 200 characters, and returns snippets, which
  GDELT never did. When the gateway is configured, GDELT gets at most 20s before the fallback runs. A
  topic can also use it directly (`adapter_config.provider = "agentcore"`).
- **Why fallback, not default:** AgentCore is priced per query (about $7 / 1,000 at launch), so it's paid
  for only when GDELT fails.
- **Alternatives considered:** Bedrock's built-in Web Search tool works only with OpenAI GPT models in US
  regions; Knowledge Base web crawlers and Kendra crawl fixed sites rather than search the news; Google
  News RSS is free and fast but unofficial.
- **Deploy permissions:** the bootstrap deploy roles may manage `bedrock-agentcore:*` only in
  `ap-northeast-1`, plus the `*-agentcore-gateway` role. Bootstrap is applied by hand, and a plan run
  without its usual `-var`s (`domain_name`, `budget_alert_email`) proposes destroying the DNS zone and the
  budget alarm -- `prevent_destroy` stops it. Keep them in the git-ignored `infra/bootstrap/terraform.tfvars`.

Also fixed alongside: the CoinGecko key reached only the research-tick Lambda, so the review (which runs
in the daily-cycle Lambda) called CoinGecko keyless. Both Lambdas now share `local.coingecko_env_variables`.

### Web search usage and spend on the Stats page

**Status: implemented** (#134, #135).

- **Counted as it happens (#134):** each AgentCore query that gets an HTTP 2xx from the gateway adds to
  the week's `web_search_agentcore_queries` and `web_search_agentcore_cost_aud` (a fixed $0.007 USD per
  query, `AGENTCORE_WEB_SEARCH_USD_PER_QUERY`, to confirm against the pricing page). Each GDELT failure
  that falls back adds to `web_search_gdelt_fallbacks`. These are weekly counters that roll into the
  all-time totals. The Stats page shows "Web searches (AgentCore)" and "Web search spend".
- **Checked against the bill (#135):** the daily Cost Explorer poll reads AgentCore's actual 30-day spend
  in the same single `GetCostAndUsage` call as API Gateway's (`agentcore_cost_usd_30d`,
  `agentcore_cost_aud_30d`; snapshots, never summed). The Stats page shows it as "Web search spend
  (actual)", labelled as coming from the AWS bill with about a day's lag.
- **Open item:** the Cost Explorer service name (`"Amazon Bedrock AgentCore"`) was assumed, because no
  AgentCore spend had been billed yet. If the actual stays at $0 while the estimate grows, look up the
  real name with `aws ce get-dimension-values --dimension SERVICE` and fix `AGENTCORE_SERVICE`.
- **Not per article:** a search belongs to a research run, not to one article, so search spend is not
  added to article lineage.

### Staggered schedules

**Status: applied in production and dev** (runtime data via the Admin API, not code; see §4).

Every production topic researches every 4 hours on a fixed UTC cron a minute apart, and the daily articles
run a minute apart from 09:00 Sydney, so no two topics start their external calls at the same moment. The
system-wide jobs (the digest at 07:00 UTC, the weekly reflection and stats rollover on Mondays, the cost
poll at 10:00 UTC) are Terraform-managed, don't call GDELT or CoinGecko, and don't overlap, so they were
left alone. Interval settings are whole hours with 5 minutes' slack, so "4 hours 1 minute" can't be
expressed and wouldn't stagger anything: the minute a topic runs comes from its heartbeat schedule.

### Frontend: render-blocking CSS kept, on purpose

**Status: decided** (#125 tried it, #127 reverted it).

Lighthouse flags the two stylesheets as render-blocking (~440ms). An async `preload` + swap script (#125)
raced: a stylesheet that finished loading before the script attached its `load` listener was never
switched on, leaving pages unstyled. The site's CSP forbids the inline `onload` handler the usual pattern
needs. Plain `<link rel="stylesheet">` is back. Both files are about 5KB over the wire minified and gzipped
(CloudFront compresses them), so the cost is one round trip, not size. Inlining was rejected: the CSP
(`style-src 'self'`) would need a hash regenerated on every CSS change, and already-published article
pages are static HTML that would need regenerating. `preload-styles.js` stays deployed as a legacy shim
(it now switches the links on immediately) for article pages published while #125 was live.

### Observability, security and content fixes (October 2026 batch)

**Status: in progress** (four PRs, in this order; designs agreed 2026-10-03). PR 1 (#145) and PR 2 (#146)
merged; PR 3 (#147) and PR 4 in review.

Found while planning this batch, and the reason PR 1 goes first: the production `stats-rollover` and
`cost-explorer-poll` schedules have never invoked their Lambdas. Both schedules exist, but
`data.aws_iam_policy_document.scheduler_invoke` (dev and production) has no statement for either function,
so EventBridge Scheduler fails every attempt and retries (about 76 failed attempts a day since 2026-09-29;
zero invocations of `cost-explorer-poll` in the last 7 days). That is why the Stats page shows `null` for
the actual API Gateway and AgentCore spend, and why the Historic totals are all zero.

#### PR 1 -- Schedules, dashboards, deploy fixes and release checks

- **Scheduler fix:** add `InvokeStatsRollover` and `InvokeCostExplorerPoll` to `scheduler_invoke` in both
  environments, with a test that every `aws_scheduler_schedule` target has a matching invoke statement, so
  a new scheduled Lambda can't miss it again.
- **Pipeline dashboard (`bloggerbear-<env>-pipeline`):** the metrics have data (24 research-tick runs in a
  day), but the dashboard sets no time range or period, so it opens on CloudWatch's 3-hour default and
  shows a few dots; and each widget draws Duration (thousands of ms) on the same axis as the counts,
  flattening them. Set a 7-day default with hourly periods, move Duration to the right axis, and add text
  headers.
- **New Lambda runs dashboard:** invocation counts per Lambda (research ticks, daily cycle, digest, musings,
  weekly reflection, rollover, cost poll, the two APIs), plus feedback -- accepted, comments kept,
  rejected, dropped by the screening model, model checks used up -- over whatever span the dashboard's
  time picker sets. The public API now logs a tag-only `accepted a feedback submission (comment kept|vote
  only)` line next to its rejection line, and three more metric filters count them. The pipeline
  dashboard also gets a "recent errors" log table across every Lambda.
- **A Lambda's log group before the Lambda:** dev's rebuild after a destroy (2026-10-02) failed with
  `ResourceAlreadyExistsException` on `/aws/lambda/bloggerbear-dev-public-api`. The public API was created
  and deployed, its first request arrived at 14:58:57, and Lambda made the log group itself (no retention)
  nine seconds before Terraform tried to. Every `aws_lambda_function` now `depends_on` the log groups, whose
  `for_each` uses a literal name list (a function that depends on them can't also name them) checked
  against the functions by a test. That also orders destroy, so nothing recreates a group after Terraform
  deletes it. The orphaned dev group needs a one-off `terraform import`.
- **Destroy of dev (`destroy-dev.yml`):** the 2026-09-29 run deleted the DLQ event source mapping, then
  failed waiting for the delete because `GetEventSourceMapping` on a mapping that no longer exists is
  authorized against `"*"`. That left the stale mapping in state, which broke every later dev apply too.
  Fixed by #144's read-only `LambdaEventSourceMappingReads` bootstrap statement (applied 2026-10-02); the
  next dev apply recreated the mapping.
- **Release checks (moved here from PR 3):** the production release workflow runs no checks today; Trivy,
  Bandit, ruff and pytest run only on PRs and pushes to `dev`. `security.yml` and `python-ci.yml` become
  reusable (`workflow_call`, checking out the release tag), and `terraform-production-release.yml`'s apply
  job `needs` both, so a failing scan or test blocks the deploy before anything changes.

#### PR 2 -- WAF spend on the Stats page

WAF is the largest line on the bill: US$10.90 of about US$17 over the 30 days to 2026-10-03 (Haiku was
US$4.24). It joins the existing daily Cost Explorer poll as a third service (`"AWS WAF"`, confirmed from
billing data) in the same single `GetCostAndUsage` call, so it costs nothing extra to fetch.

- **Stored:** SET snapshots on the week's StatsCurrent row, like API Gateway's: the rolling 30 days, this
  week so far (from the row's own Monday, so each week's history row keeps that week's WAF spend up to the
  last poll before its rollover -- normally Monday to Saturday, given the ~24h lag), this calendar month so
  far, and last month in full, with `waf_cost_month` / `waf_cost_previous_month` labels. One call still
  serves everything: `fetch_costs` asks for daily buckets from whichever is earliest (30 days ago, the 1st
  of last month, this Monday) to yesterday and counts each day into every window it falls in.
- **One site-wide figure:** dev and production can't be told apart without cost-allocation tags, and the
  CloudFront ACL is shared by both anyway, so it is reported as the site's cost.
- **Stats page:** "Firewall (WAF) spend" tiles beside the other actual (from the bill) figures, labelled
  with the ~24h lag: last 30 days, this week (Weekly Stats only), this month so far, and last month.
- Depends on PR 1: the poll has to run before there is anything to show.

#### PR 3 -- The CoinGecko key in SSM

- **CoinGecko key in SSM Parameter Store** (SecureString, chosen over Secrets Manager: same job, no
  monthly charge) at `/bloggerbear/<env>/coingecko-api-key`. Terraform does **not** create the
  parameter -- a managed SecureString's value is read back into state on every refresh -- it only
  grants `ssm:GetParameter` on that one name (the default `aws/ssm` key needs no KMS grant) and tells
  the two crypto Lambdas (research tick, daily cycle) where it is, via `COINGECKO_API_KEY_PARAMETER`.
  The operator creates it once with `aws ssm put-parameter` (README.md step 3). `crypto_feed` reads it
  once per cold start, keeps a definite answer (the key, or "no such parameter") for the container's
  life and retries a failed read next run; a plain `COINGECKO_API_KEY` still wins, for local runs. No
  parameter means keyless, exactly as with no key before. `var.coingecko_api_key`,
  `TF_VAR_coingecko_api_key` and the `COINGECKO_API_KEY_DEV`/`_PROD` secrets are retired (delete the
  secrets once the parameters exist). The Lambdas share one exec role, so any of them *could* read the
  parameter; only the two crypto ones are told its name.

#### PR 4 -- Content

- **Digest layout:** the Trending Everywhere body is whatever shape the model picks that day -- usually one
  long paragraph per topic behind an inline bold label, no headings, and the title repeated as a body
  heading. The layout moves into code (`_compose_digest`): the model now replies in a fixed line format
  (`OVERVIEW: ...`, then `1: ...`, `2: ...`, one per numbered topic) and the code builds the body: the
  overview paragraph, then a `## <Topic name>` section per topic, in order, holding the model's line for
  it. A topic the model skipped gets the start of its own finding summary (plain prose, cut at a sentence
  end within 400 characters), so an unformatted reply still yields every section. Bold, heading and
  bullet marks are stripped, any preamble is ignored, and the title never appears in the body.
- **A musing when an article is rejected:** a new `shocked` mood with its own bear
  (`frontend/bears/shocked.svg`, in the existing bears' style), written when moderation rejects an article.
  It names the topic only: no title and no link, because a rejected article isn't public and may contain
  exactly what got it rejected. Checked like the loot musing: the text must name the topic and pass the
  comment rules, or a plain fixed line is posted instead.

### Scaling A: DynamoDB indexes instead of Scans

**Status: built** (one of three scaling PRs; hot-key counters and the public API's caching, throttling
and dashboards are the other two).

**Problem:** no table had a secondary index, so every read that wasn't by primary key was a Scan of the
whole table plus a filter. The worst was `list_articles_by_status`, behind the home page, every topic page
and the RSS feed: each request read every article ever drafted, in every status. The review inbox, a topic
page's "pending review" count, the stuck-rewrite sweep and an article's moderation lookup each scanned the
whole moderation queue, history included.

**What changed:**
- **Articles** gets `by_status_created_at` (status / created_at) and `by_topic_created_at`
  (topic_id / created_at), both `ALL` projection. Published listings Query the first; a topic's articles
  (topic page, recent titles, the top-voted example) Query the second and keep the published ones.
  `created_at`, not `published_at`, is the sort key: `put_article` stores `published_at` as an explicit
  null until publish, and DynamoDB rejects a write whose index key is null. Callers already sorted for
  themselves.
- **ModerationQueue** gets `by_status_created_at` (`ALL`) for the pending/rewriting lists (a topic's pending
  items are filtered from the pending ones: few, and not worth a third index) and `by_article_created_at`
  (`KEYS_ONLY`). An article's live item is the newest by `created_at`; its full item is then read with a
  strongly consistent GetItem, because index reads can trail the table and the caller acts on `status`.
- `list_prompt_refinements(topic_id=...)` Queries the table's own hash key instead of scanning.
- The Lambda role may `dynamodb:Query` on `<table>/index/*` (a separate statement: nothing else runs
  against an index). The deploy role's `dynamodb:*` on `table/bloggerbear-*` already covers creating
  indexes, so no bootstrap apply is needed.
- Function signatures and return shapes are unchanged. Test fixtures create tables through
  `lambdas/tests/table_schemas.py`, which holds the same index definitions; a wiring test fails if it and
  Terraform drift apart.

**Applying it:** DynamoDB creates one index per UpdateTable call. The AWS provider (checked in v6.64.0's
`table.go`) sends one call per new index and waits for each to become ACTIVE before the next, so both
indexes per table land in a single apply; it just takes a few minutes longer while they backfill.

**Left as Scans, on purpose:** `list_all_articles` (wants every status; backs the Stats page and admin
lineage/backfill tools), `list_all_moderation_items` (admin stats over all history), `list_topics`,
`list_models`, `list_failed_executions` (tiny tables), `list_feedback_since` (the weekly and 4-day jobs),
`list_prompt_refinements` without a topic (small table, filtered on status), and the Musings table
(`list_musings`, `delete_musings_for_article`): it has no attribute every item shares to index on, so it
would need a new attribute and a backfill. Its public feed and the Stats page are better served by caching
their responses.

### Scaling C: the public API's cache and throttling, and two dashboards

**Status: built** (the last of three scaling PRs). **Needs `infra/bootstrap` applied before it merges**:
see "Applying it" below.

**Problem:** the public API had no cache and no API Gateway throttling. Every listing, article and RSS
request invoked the Lambda and read DynamoDB, the per-IP WAF rules were the only ceiling, and there was no
dashboard for API Gateway or WAF at all.

**Caching: CloudFront, in its own distribution** (`infra/modules/api-cdn`). The frontend now calls the API
through it (`config.js`'s `PUBLIC_API_URL`, added to the site's CSP `connect-src`). The execute-api URL
keeps working for anything already pointed at it, such as RSS readers.
- **Not a behaviour on the site's distribution:** that distribution maps every 403 and 404 to
  `/error.html`, and `custom_error_response` applies to the whole distribution. It would replace the API's
  JSON errors with HTML, and the feedback form reads a 403's body to retry its verification. A second
  distribution has no monthly fee.
- **The API decides what is cached.** The cache policy's default TTL is 0, so only responses with a
  `max-age` are kept:
  - 60 s: topics, topic activity, article listings, an article, musings, equipment
  - 300 s: RSS and stats
  
  Everything else is sent `no-store` and never cached: the view counter, feedback, `feedback-status` (a
  fresh verification token each time) and every error. CloudFront never caches POSTs. The query string is
  in the cache key; headers and cookies are not.
- **API Gateway's own cache was rejected on cost.** It bills hourly whether used or not, about US$20 a month
  for the smallest per stage, which is more than the whole site costs.

**Per-visitor WAF limits behind the CDN.** Through CloudFront the regional WAF only sees edge addresses,
which many visitors share, so the existing per-IP limits would have started blocking whole edges. The fix:
- A CloudFront Function writes the visitor's address into `x-viewer-ip`, overwriting whatever the visitor
  sent.
- The distribution sends a secret `x-origin-verify` header (`random_password`, redacted from WAF logs).
- `rate-limit` and `feedback-rate-limit` now apply only to requests **without** the secret, by source IP,
  as before.
- Two new rules, `rate-limit-via-cdn` and `feedback-rate-limit-via-cdn`, apply the same limits to requests
  **with** it, aggregated on `x-viewer-ip` (`FORWARDED_IP`, `NO_MATCH` fallback).
- A direct caller can't forge the visitor header: without the secret, its own IP is what's counted.
- Cost: two more WAF rules per regional ACL, about US$1 each a month.
- In production the API distribution also gets the shared CloudFront ACL (the 2000-per-IP edge limit and
  managed rules); in dev it gets that ACL once `web_acl_arn` is set, the same as the site.

**Throttling** (`aws_api_gateway_method_settings`):
- Public API stage: 25 requests a second, burst 50. The feedback POST, which can cost a model call, gets
  2 a second, burst 5.
- Admin API: 10, burst 20.

Anything above these gets a 429 before the Lambda runs. These are whole-API ceilings; per-visitor limits
stay with WAF. Detailed per-method metrics and execution logging stay off, since they bill as custom
metrics.

**Access logs:** one JSON line per request (time, method, path, status, latency, integration latency,
error type, WAF status). They hold **no IP or user agent** and are kept 14 days for the public API and 30
for admin. The privacy policy now says so. API Gateway can only write them once the account names a
CloudWatch Logs role for it, which is an account-wide singleton, so it lives in `infra/bootstrap`
(`aws_api_gateway_account`), together with the deploy role's permission for `/aws/apigateway/bloggerbear-*`.

**Dashboards** (`infra/modules/observability/api_waf_dashboards.tf`). Like the pipeline dashboard, both open
on 7 days of hourly points.
- **API Gateway**, per API:
  - requests, 4XX and 5XX
  - latency against integration latency at p50, p90 and p99
  - from the access logs: a responses-by-status table (the 400/403/429/500/502/504 split; REST APIs publish
    no per-status metric), 429s per hour, and 4XX/5XX by route and error type
  - for the public API, CDN requests next to the requests that still reached API Gateway (the gap is the
    cache), and CDN error rates
  
  CloudFront's `CacheHitRate` widget appears only if `enable_additional_metrics` is on (about US$2.40 a
  month, off by default).
- **WAF:**
  - for each regional ACL: allowed, blocked and counted, blocked per rule (rate limits included) and
    allowed/counted per rule, all by metric search so a new rule shows up by itself, plus a Logs Insights
    table of the top blocked rule, address and path (the address is `x-viewer-ip` when the request came
    through the CDN)
  - the same for the shared CloudFront ACL, in us-east-1. Its metrics carry no `Region` dimension, so the
    widgets search both dimension shapes; CloudFront's own metrics use `Region = Global`.
  
  Logs Insights widgets are billed per GB scanned, only when someone opens the dashboard. A metric filter
  per status would cost a custom metric each, every month.

**Applying it:**
1. **Apply `infra/bootstrap` first.** It adds the API Gateway CloudWatch Logs role and account setting, and
   the deploy role's `ApiAccessLogGroups` statement. Without them the stage update fails ("CloudWatch Logs
   role ARN must be set in account settings") or the log group creation is denied.
2. Then merge. The first apply creates the distribution, which takes a few minutes. `config.js` switches
   the site to it, and an open page picks the change up on its next load (`config.js` is `no-cache`).
3. The `random` provider is new (lock files updated).
