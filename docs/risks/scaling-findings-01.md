# Scaling findings 01: where the architecture gives first

**Date:** 2026-10-03 · **Status:** open, for reference · **Scope:** what breaks first if BloggerBear
had to grow well past today (four topics, one operator, modest traffic).

Nothing here is a problem at today's size. This is the order things would start to hurt, why, and
what fixes each. Items 1, 2 and 7 are being worked on (DynamoDB indexes; sharded/batched counters;
API caching and throttling, with API Gateway and WAF dashboards to watch them). Item 3 has a design:
[docs/enhancements/dispatcher-sqs-enhancement.md](../enhancements/dispatcher-sqs-enhancement.md).

| # | Finding | Hurts at | Fix | Effort |
|---|---|---|---|---|
| 1 | Tables are read with full scans | thousands of articles, or real traffic | GSIs + `Query` | low |
| 2 | Hot keys: one Stats row, a write per page view | ~1,000 writes/s on one item, or a viral article | shard or batch counters | low–medium |
| 3 | One pair of schedules per topic, no concurrency limit | ~15–20 topics | dispatcher + SQS | medium |
| 4 | Bedrock quota and spend have no hard ceiling | more topics x more rewrites | in-code token budget, inference profiles | medium |
| 5 | Long work inside one 300 s Lambda | longer articles, slower models | per-stage Step Functions states | medium |
| 6 | One person reviews everything | more financial/held topics | risk-based routing, more reviewers | medium |
| 7 | No API throttling or response caching | a bot crawl | API Gateway throttling + CloudFront caching | low |
| 8 | Single region, all-at-once deploys, CLI-only admin | a team, or an outage | multi-region fallback, staged deploys, real identity | high |

---

## 1. DynamoDB is used like a small relational database

**What.** The 13 tables in `infra/modules/app-data` have no secondary indexes, and
`lambdas/common/dynamo.py` makes about 25 scans. `list_articles_by_status` reads the whole Articles
table and filters it; the public API calls it to list published articles, so every listing and
RSS request reads every article ever written. The moderation queue is the same:
`get_moderation_item_by_article_id` and `list_pending_moderation` scan the whole queue.

**Why it matters.** A scan's cost and latency grow with the table, not with the answer. At 50
articles it is free; at 50,000 each page view reads all 50,000 items (and DynamoDB returns at most
1 MB per page, so the request also gets slower page by page).

**Fix.** Add GSIs for the questions actually asked (`status` + `published_at`; `topic_id` +
`created_at`; ModerationQueue `status` + `created_at` and `article_id` + `created_at`) and switch
those reads to `Query` with pagination. DynamoDB creates one GSI per table update, so adding several
to an existing table is applied in steps.

## 2. Hot keys

**What.** Every tracked Bedrock call increments the same all-time Stats row. Every article view
increments `view_count` on the article's own item (`increment_view_count`, called by the public
API on each view).

**Why it matters.** One DynamoDB item takes about 1,000 writes a second; contended updates throttle
before that. A popular article turns its item into a hot partition, and every view is a billed
write.

**Fix.** Split each counter into N shard items (write to a random shard, sum on read), or buffer
increments (SQS/Kinesis) and roll them up on a schedule. For views, counting from CloudFront logs
removes the per-request write altogether.

## 3. Each topic is its own set of scheduled jobs

**What.** Each topic owns two EventBridge Scheduler schedules (research heartbeat, daily cycle),
created by the Admin API. Production staggers them by hand: daily cycles at 09:00, 09:01, 09:02 and
09:03 Sydney; research at :01 to :04 past every fourth hour.

**Why it matters.** Staggering spreads start times but limits nothing. A daily cycle can run for up
to 5 minutes, so one-minute offsets already overlap; at 40 topics about five run at once, and a slow
Bedrock day makes runs longer and the pile-up worse. All of them then share the Lambda concurrency
limit (1,000 per account per region by default), the Bedrock tokens-per-minute quota, and the
GDELT/CoinGecko rate limits, with nothing coordinating them. Every new topic also means choosing a
free slot by hand, and two AWS resources kept in step with a DynamoDB row.

**Fix.** One dispatcher schedule that enqueues only what is due, and workers that drain the queue
at a fixed concurrency. See the enhancement doc linked above. Worth doing at about 15–20 topics, or
as soon as Bedrock starts throttling.

## 4. Bedrock quota and cost are the real ceiling

**What.** Each article costs several model calls (ideation, draft, revision, fresh-data review,
compliance, musing), and each Re-Write adds a rewrite and two reviews. Cost grows with topics x
rewrites. Spend is tracked per article and per week (lineage, the Stats page, Cost Explorer), and an
AWS Budget emails at 80%/100%.

**Why it matters.** Tracking is not control: nothing stops a run when a budget is exceeded, and the
Bedrock quota in one region is a hard wall that every topic hits together.

**Fix.** A per-topic and global daily token budget checked in code before each call (fail closed:
skip the run, alert); cross-region inference profiles or provisioned throughput for headroom.

## 5. Long work runs inside Lambda limits

**What.** The daily cycle has a 300 s timeout, and a Re-Write runs in the same Lambda. Step
Functions wraps the daily cycle as a single task.

**Why it matters.** Longer articles, more research or slower models push against 300 s. Because the
state machine has one step, a failure in compliance retries the whole cycle, draft included, and
pays for it again. "Release a stale rewrite after 15 minutes" exists to work around exactly this.

**Fix.** Split draft, revise, review and publish into separate Step Functions states, each saving
its output, so a retry resumes at the failed stage.

## 6. The human is the bottleneck

**What.** Financial topics, held articles and every rewrite wait for one person in a CLI inbox
(`admin_cli approve`).

**Why it matters.** Double the topics and the inbox doubles. Steered rewrites make each decision
better, not faster.

**Fix.** Route by risk (auto-publish low-risk articles, spot-check a sample), more than one reviewer
with assignment, and an age at which an item escalates or expires.

## 7. The public edge has no throttling

**What.** The REST APIs have no API Gateway throttling or usage plan; WAF rate-based rules are the
only limit. Listings are not cached.

**Why it matters.** With items 1 and 2, a bot crawl is a bill: each request is a scan and each view
a write.

**Fix.** Stage/method throttling on the public API, CloudFront caching of listings and RSS (even
60 s collapses a crawl into one origin request per minute), and RSS as a static file regenerated on
publish. Watch it on an API Gateway dashboard (requests, 4XX/5XX by status, 429s, latency, cache hit
rate) and a WAF dashboard (allowed/blocked by rule, for both the regional and CloudFront WAFs).

## 8. Operations are built for one person

- **Single region, one account per environment.** A Bedrock outage in `ap-southeast-2` stops all
  writing; the fallback model is in the same region.
- **All-or-nothing deploys.** One Terraform root per environment; no canary or gradual rollout for
  the Lambdas (aliases + CodeDeploy would give one).
- **Admin is a SigV4-signed CLI behind an IP allowlist.** Right for one operator; a team needs real
  identity (Cognito or IAM Identity Center), per-person audit, and roles.

**Fix.** Only at real scale: a second-region Bedrock fallback, Lambda aliases with traffic
shifting, and an identity provider for admin.

---

## Suggested order

1. Indexes instead of scans (1): cheap, and removes the curve everyone notices first.
2. API caching and throttling, counters (7, 2): protect the bill from traffic.
3. The dispatcher queue (3) when topics reach the mid-teens.
4. Budgets in code (4) before adding many more topics.
5. The rest when the project has more than one operator or real traffic.
