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
