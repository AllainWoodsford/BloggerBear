"""The assistant as an expert on the project's own architecture: what each AWS resource is for,
what it is called in this environment, and where to look when something goes wrong with it.

    architecture   one resource described (a table, a function, an API, a dashboard, a log group,
                   ...), or every resource of a kind, as a table on screen; the layers; or a
                   feature (article research, the reviewer bear, ...) step by step across them

runsheets.py builds on the same catalogue for `investigate`: where to look when the assistant cannot
look itself.

**Where the knowledge comes from.** CATALOGUE below, written by hand from infra/ and checked in.
The Lambda's package holds no infra/, so nothing here reads Terraform at run time:
tests/test_ops_mcp_architecture.py reads it instead, and fails when a table, function, dashboard,
alarm or schedule in infra/ is missing from the catalogue, or named, keyed or indexed differently.
The catalogue cannot drift from what is deployed without the build saying so.

**Fast by construction.** The catalogue is a module constant and its lookup index is built once,
at import: answering a question about a resource reads no table, no file and no AWS API. It costs
nothing to ask and works when everything else is down, which is when it is most needed.

**One deployment's names.** Nothing here assumes what the resources are called: every name starts
with this deployment's prefix (common/naming.py, the NAME_PREFIX variable Terraform sets on the
function), which is "bloggerbear" in the original deployment and in the examples below.

**One environment, whatever name is pasted.** Every name is a template, "bloggerbear-{env}-...",
filled in with this assistant's own environment (ENVIRONMENT_NAME, as account.py reads it). A name
from the other environment is answered for this one: asked "bloggerbear-prod-candidate-ideas" in
dev, the answer is about bloggerbear-dev-candidate-ideas, and says so. A name whose environment is
neither (a typo, "staging") is still described, for this environment, but `data_allowed` comes back
false: a tool that reads rows (a later one) must not guess which table was meant and then read it.

**Read-only, and nothing here is untrusted.** Every word below is ours. The operator's own input is
only ever matched against the catalogue, never echoed into `spoken`.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import quote

from common.naming import NAME_PREFIX

# This assistant's environment. The module sets it on the function (ops-assistant/main.tf).
ENVIRONMENT_ENV = "ENVIRONMENT_NAME"
_ENVIRONMENT_NAME = re.compile(r"[a-z][a-z0-9]{1,31}")
# What every name starts with, hyphen included: "bloggerbear-" unless this deployment has its own.
PREFIX = f"{NAME_PREFIX}-"
# The prefix as the words a pasted name is split into (a prefix may itself hold a hyphen).
_PREFIX_WORDS = NAME_PREFIX.split("-")
# The CloudFront firewall's log group: one for the deployment, not one per environment.
SHARED_WAF_LOG_GROUP = f"aws-waf-logs-{PREFIX}shared"
ENV = "{env}"

# The environments the project deploys, and the words people use for them. "prod" is what the
# operator says; "production" is what Terraform names things.
ENVIRONMENTS = ("dev", "production")
ENVIRONMENT_ALIASES = {
    "dev": "dev",
    "development": "dev",
    "production": "production",
    "prod": "production",
    "prd": "production",
}

KINDS = (
    "table",
    "function",
    "api",
    "state_machine",
    "queue",
    "topic",
    "dashboard",
    "log_group",
    "schedule",
    "bucket",
    "firewall",
)
KIND_LABELS = {
    "table": "DynamoDB table",
    "function": "Lambda function",
    "api": "API Gateway REST API",
    "state_machine": "Step Functions state machine",
    "queue": "SQS queue",
    "topic": "SNS topic",
    "dashboard": "CloudWatch dashboard",
    "log_group": "CloudWatch log group",
    "schedule": "EventBridge Scheduler schedule",
    "bucket": "S3 bucket",
    "firewall": "WAF web ACL",
}

OVERVIEW_MAX_ROWS = 50
_NAME_MAX_CHARS = 200


@dataclass(frozen=True)
class Component:
    """One resource. `name` is a template with {env} where the environment goes. `details` are
    (label, text) pairs for the page, in the order shown. `log_groups`, `dashboards` and `alarms`
    are templates too: where to look for this resource. `only_in` lists the environments that have
    it, when not every one does. `assistant_reads` is whether the assistant's own role may read it
    (for a table: its rows), which test_ops_mcp_architecture.py holds to the Terraform.
    `region_env` names the environment variable that holds the Region this component lives in, for
    one outside the stack's home Region (the vision worker: OPS_VISION_REGION). Such a component
    exists only while that variable holds a Region (component_region), and its console links and
    log reads go to that Region."""

    kind: str
    key: str
    name: str
    purpose: str
    details: tuple[tuple[str, str], ...] = ()
    aliases: tuple[str, ...] = ()
    log_groups: tuple[str, ...] = ()
    dashboards: tuple[str, ...] = ()
    alarms: tuple[str, ...] = ()
    only_in: tuple[str, ...] = ()
    assistant_reads: bool = False
    region_env: str | None = None
    keys: tuple[str, ...] = field(default=())  # a table's hash key, then its range key if any
    indexes: tuple[str, ...] = ()
    ttl: str | None = None  # a table's TTL attribute


def _lambda(key: str, purpose: str, trigger: str, *, alarmed: bool = True, **more) -> Component:
    """A Lambda function: its log group is always /aws/lambda/<name>, and the pipeline's own
    functions each have an errors and a throttles alarm (observability/main.tf, which names them
    "<prefix>-<env>-" + the function's own name, so the prefix appears twice)."""
    name = f"{PREFIX}{ENV}-{key}"
    alarms = (f"{PREFIX}{ENV}-{name}-errors", f"{PREFIX}{ENV}-{name}-throttles") if alarmed else ()
    dashboards = (f"{PREFIX}{ENV}-pipeline", f"{PREFIX}{ENV}-lambda-runs") if alarmed else ()
    return Component(
        kind="function",
        key=key,
        name=name,
        purpose=purpose,
        details=(("Runs", trigger), *more.pop("details", ())),
        log_groups=(f"/aws/lambda/{name}", *more.pop("log_groups", ())),
        dashboards=(*dashboards, *more.pop("dashboards", ())),
        alarms=(*alarms, *more.pop("alarms", ())),
        **more,
    )


def _table(key: str, purpose: str, keys: tuple[str, ...], **more) -> Component:
    return Component(kind="table", key=key, name=f"{PREFIX}{ENV}-{key}", purpose=purpose, keys=keys, **more)


_PIPELINE_DASHBOARD = f"{PREFIX}{ENV}-pipeline"
_EDGE_DASHBOARD = f"{PREFIX}{ENV}-edge"

# Every resource the assistant can explain, by kind. Purposes are one sentence, for speech; the
# rest is for the page. Written from the Terraform's own comments (infra/modules/app-data,
# observability, rest-api, ops-assistant and infra/environments/*/main.tf).
CATALOGUE: tuple[Component, ...] = (
    # --- DynamoDB ------------------------------------------------------------------------------
    _table(
        "topics",
        "One row per topic the blog writes about, with its settings: adapter, research interval, "
        "daily cadence, findings window, model, review mode, editorial goals, and when it was last "
        "researched and last written about.",
        ("topic_id",),
        details=(
            (
                "Written by",
                "the Admin API (admin_cli topics ...); research-tick and daily-cycle "
                "stamp last_research_at and last_article_at, with last_article_run (the run that wrote "
                "the article, so a retry of that run writes nothing), and, for a topic that rotates "
                "through subjects, last_article_by_subject (each subject's last article)",
            ),
            ("Read by", "every pipeline function, the public API, the assistant"),
        ),
        aliases=("topic",),
        assistant_reads=True,
    ),
    _table(
        "findings",
        "What each research tick found for a topic: a summary of the source data, where the raw "
        "snapshot is in the content bucket, and the sources. The daily cycle writes from these.",
        ("topic_id", "captured_at"),
        ttl="expires_at",
        details=(
            (
                "Holds",
                "topic_id, captured_at, expires_at, summary (written by a model from the source "
                "data), raw_snapshot_s3_key, source_refs, research_call (model, tokens, fallback, "
                "stop reason), figures (maps, from the vision adapters), planner_notes (rail: flags "
                "and simulations), subjects (the subject keys it reports, for a topic that rotates "
                "through subjects)",
            ),
            (
                "Written by",
                "research-tick, only when a tick's diff is material (diff-first): a quiet source "
                "writes none",
            ),
            (
                "Read by",
                "daily-cycle, the public API, the Admin API, the assistant (topic_findings)",
            ),
            ("Expires", "14 days after it was captured (TTL)"),
            (
                "Healthy looks like",
                "research on time for the topic (pipeline_health); Findings as often as its source "
                "changes",
            ),
        ),
        aliases=("finding", "research"),
        assistant_reads=True,
    ),
    _table(
        "candidate-ideas",
        "The daily cycle's scratchpad: the article angles it considered for a topic, and which "
        "one it selected to write.",
        ("topic_id", "created_at"),
        ttl="expires_at",
        details=(
            ("Holds", "topic_id, created_at, angle, status (considered or selected), expires_at"),
            ("Written by", "daily-cycle, each time it runs for a topic"),
            ("Read by", 'the Admin API\'s "candidates considered but not published" view'),
            ("Expires", "7 days after it was written (TTL)"),
            (
                "Healthy looks like",
                "new rows for each topic at about its daily cadence, with one "
                "selected per run that wrote an article",
            ),
        ),
        aliases=("candidates", "candidate-idea", "ideas", "angles"),
    ),
    _table(
        "articles",
        "One row per article: its topic, title, status (published, pending_moderation, "
        "rejected), where its body is in the content bucket, its lineage and cost, and votes.",
        ("article_id",),
        indexes=("by_status_created_at", "by_topic_created_at"),
        details=(
            (
                "Written by",
                "daily-cycle; the Admin API on approve, reject, rewrite and unpublish",
            ),
            ("Read by", "the public API (home, topic pages, RSS), the Admin API, the assistant"),
            ("Bodies", "in the content bucket under articles/, not in the table"),
            (
                "Day subject",
                "subject: on an article of a topic that rotates through subjects, the key of the "
                "subject it was written about",
            ),
        ),
        aliases=("article", "posts"),
        assistant_reads=True,
    ),
    _table(
        "moderation-queue",
        "Articles held for a person to approve or reject, with why each was held. Approved and "
        "rejected items stay as history.",
        ("queue_id",),
        indexes=("by_status_created_at", "by_article_created_at"),
        ttl="expires_at",
        details=(
            (
                "Written by",
                "daily-cycle (held articles); the Admin API (approve, reject, rewrite)",
            ),
            ("Read by", "the Admin API's inbox, the public API's pending counts, the assistant"),
            ("Expires", "only rejected items, by TTL; pending and approved ones never"),
        ),
        aliases=("moderation", "inbox", "review-queue", "queue-items"),
        assistant_reads=True,
    ),
    _table(
        "feedback",
        "Readers' thumbs up or down, with an optional comment, on published articles.",
        ("article_id", "feedback_id"),
        details=(
            ("Written by", "the public API (POST /articles/{article_id}/feedback), after screening"),
            ("Read by", "weekly-reflection and musing-feedback"),
            ("Why the assistant cannot read it", "reader comments are the public's words"),
        ),
        aliases=("votes", "reader-feedback"),
    ),
    _table(
        "prompt-refinements",
        "Versioned prompt changes per topic, proposed by the weekly reflection from reader "
        "feedback, then approved, equipped or rejected by the operator.",
        ("topic_id", "version"),
        ttl="expires_at",
        details=(
            ("Written by", "weekly-reflection (proposals); the Admin API (approve, equip, reject)"),
            ("Read by", "daily-cycle (the equipped version), the public API, the Admin API"),
            ("Expires", "only rejected versions, by TTL"),
        ),
        aliases=("refinements", "prompts", "gear"),
    ),
    _table(
        "failed-executions",
        "One row per daily-cycle run that ran out of retries and landed on the dead-letter "
        "queue (the topic and the error), and one per model price check that put a price back "
        '(kind "model_pricing").',
        ("failure_id",),
        ttl="expires_at",
        details=(
            (
                "Written by",
                "dlq-handler, one row per dead-letter message; model-catalog, per failed price check",
            ),
            ("Read by", "the Admin API (admin_cli failed-executions list), the assistant"),
        ),
        aliases=("failures", "failed-runs", "dlq-records"),
        assistant_reads=True,
    ),
    _table(
        "musings",
        "The musings feed: a short note on each article as it is published, and one every four "
        "days reflecting on reader feedback.",
        ("musing_id",),
        details=(
            ("Written by", "every publish path (common/musings.py) and musing-feedback"),
            ("Changed by", "the Admin API (admin_cli musings edit and musings regenerate)"),
            ("Read by", "the public API, the assistant's content checks, admin_cli musings list"),
        ),
        aliases=("musing",),
        assistant_reads=True,
    ),
    _table(
        "models",
        "The registry of Bedrock models the pipeline may use, with their pricing, so a model "
        "can be added or switched without a Terraform apply. Every model the account can call is "
        "added here by the model catalog, switched off, and its price kept current.",
        ("model_id",),
        details=(
            (
                "Written by",
                "the Admin API (admin_cli models ...); model-catalog (discover adds rows, reprice "
                "and its checks change prices)",
            ),
            ("Read by", "every function that calls Bedrock, the public API"),
        ),
        aliases=("model-registry",),
    ),
    _table(
        "model-config",
        'Settings rows: the default model ("default"), the pipeline\'s settings including the '
        'assistant_access switch ("pipeline"), feedback limits ("feedback"), the model '
        'catalog\'s pause flags and checks ("model-catalog"), what it last did '
        '("model-catalog-state"), one row per pricing run while it is checked '
        '("pricing-run#<id>"), and the feedback rate limiter\'s short-lived counters.',
        ("config_id",),
        ttl="expires_at",
        details=(
            (
                "Written by",
                "the Admin API (model-config, pipeline-config, feedback-config, model-catalog set); "
                "the public API (rate-limit counters); model-catalog (its state and pricing runs)",
            ),
            ("Read by", "every pipeline function, the assistant (its access switch, model_catalog)"),
            ("Expires", "the rate-limit counters, and a pricing run 14 days after it closes, by TTL"),
        ),
        aliases=("config", "pipeline-config", "settings"),
        assistant_reads=True,
    ),
    _table(
        "stats-current",
        "This week's running totals of AI spend and reader activity that are not part of any one "
        "article (musings, reflection, screening, feedback counts). One row, updated in place.",
        ("stats_id",),
        details=(
            (
                "Written by",
                "every function that calls Bedrock outside an article "
                "(common/stats_tracking.py); reset each Monday by stats-rollover",
            ),
            ("Read by", "the public Stats page, the assistant's spend tool"),
        ),
        aliases=("stats",),
        assistant_reads=True,
    ),
    _table(
        "stats-history",
        "One row per completed week, copied from stats-current by the weekly rollover and never "
        "changed after.",
        ("week_start",),
        details=(
            ("Written by", "stats-rollover, Mondays 13:15 Sydney time; cost-explorer-poll adds AWS costs"),
            ("Read by", "the public Stats page, the Admin API, the assistant's spend tool"),
        ),
        aliases=("weekly-stats",),
        assistant_reads=True,
    ),
    _table(
        "view-counts",
        "Article view counts, split over a few counter rows per article so one popular article "
        "is not one hot item. A read adds them up.",
        ("counter_id",),
        details=(("Written by", "the public API, on every page view"), ("Read by", "the public API")),
        aliases=("views",),
    ),
    _table(
        "security-events",
        "Security incidents: blocked requests grouped by source, rule, client and 15-minute "
        "window, each with a category, severity, next steps and status. No IP address, only a "
        "keyed hash.",
        ("event_id",),
        indexes=("by_status_last_seen",),
        ttl="expires_at",
        details=(
            (
                "Written by",
                "security-events (from the regional firewalls' logs) and the public API's comment screening",
            ),
            ("Read by", "the assistant's security_events tool, the Admin API"),
            ("Expires", "120 days after an incident was last seen (TTL)"),
        ),
        aliases=("incidents", "security"),
        assistant_reads=True,
        dashboards=(_EDGE_DASHBOARD,),
        alarms=(f"{PREFIX}{ENV}-security-high-severity",),
    ),
    _table(
        "sign-ins",
        "Sign-ins to the operator's assistant: one row per attempt, success, refusal and unlock, "
        "by user and time. It is what the lockout counts from. No address is kept.",
        ("username", "at"),
        ttl="expires_at",
        details=(
            ("Written by", "sign-in-events (the user pool's triggers) and the Admin API's unlock"),
            ("Read by", "the assistant's sign_ins tool, the Admin API"),
            ("Expires", "120 days after the event (TTL)"),
        ),
        aliases=("logins", "sign-in log", "signins"),
        assistant_reads=True,
        alarms=(f"{PREFIX}{ENV}-security-high-severity",),
    ),
    _table(
        "operator-suggestions",
        "The assistant's own memory: the fixes it suggested and what the operator asked it to "
        "watch, per signed-in user. The one table the assistant writes to.",
        ("user_id", "item"),
        ttl="expires_at",
        details=(
            ("Written by", "the assistant (follow_up, dismiss, watch, unwatch)"),
            ("Expires", "30 days after a row was last mentioned (TTL)"),
        ),
        aliases=("suggestions", "assistant-memory", "memory"),
        assistant_reads=True,
    ),
    _table(
        "ops-briefings",
        "The latest briefing per signed-in user: what the assistant last said needs attention, "
        "written by the agent and read back by latest_briefing, for clients like Alexa+ that "
        "cannot wait for the agent.",
        ("user_id",),
        ttl="expires_at",
        details=(
            (
                "Written by",
                "the agent (every briefing); the MCP server's start_briefing marks one as started",
            ),
            ("Read by", "the MCP server's latest_briefing"),
            ("Expires", "two days after it was written (TTL)"),
            ("Never shown to the agent", "it holds what the agent wrote after reading hostile text"),
        ),
        aliases=("briefings", "briefing"),
    ),
    # --- Lambda --------------------------------------------------------------------------------
    _lambda(
        "research-tick",
        "Checks one topic's source (its adapter) for anything new and writes a finding.",
        "per topic, on the topic's own EventBridge schedule (its research interval); for a topic "
        f"on the queue (dispatch_mode queue), on each research job on {PREFIX}{ENV}-work.fifo, "
        "at most worker_concurrency at once",
        details=(
            ("Writes", "findings; topics.last_research_at"),
            ("Schedules", f"{PREFIX}{ENV}-<topic_id>-research-tick"),
        ),
    ),
    _lambda(
        "dispatcher",
        "Every minute, works out which topics are due for research or their daily cycle and, for a "
        "topic on the queue, sends the research job or starts the cycle; for a topic still on its "
        "schedules (every topic until the cutover) it only records what it would have done.",
        f"every minute ({PREFIX}{ENV}-dispatcher, EventBridge Scheduler)",
        details=(
            (
                "Writes",
                "topics.research_claimed_at, last_daily_dispatched_on and daily_claim_n; in shadow mode "
                "topics.shadow_last_research_dispatch_at, shadow_last_daily_dispatch_at, "
                "shadow_mismatches, shadow_unchecked and shadow_checked_days; last_tick on the "
                "model-config pipeline row",
            ),
            (
                "Sends",
                f"research jobs to {PREFIX}{ENV}-work.fifo; starts {PREFIX}{ENV}-daily-cycle "
                "executions named <topic>-<date>-<n>",
            ),
            (
                "Settings",
                "dispatch_shadow and daily_starts_per_tick on the pipeline row (pipeline-config set)",
            ),
            (
                "Logs",
                "one dispatcher_tick evidence line a tick: considered, dispatched, started, "
                "skipped_unparsable",
            ),
        ),
        alarms=(f"{PREFIX}{ENV}-dispatcher-heartbeat",),
        aliases=("dispatch", "queue-dispatcher"),
    ),
    _lambda(
        "daily-cycle",
        "Writes one topic's article from its recent findings, reviews it, and publishes it or "
        "holds it for review.",
        "per topic, once a day, through the daily-cycle state machine (two retries, then the "
        "dead-letter queue)",
        details=(
            (
                "Writes",
                "candidate-ideas, articles, moderation-queue, the content bucket; topics.last_article_at "
                "with topics.last_article_run (the run that wrote the article: a retry of that run "
                "writes nothing) and, for a topic that rotates through subjects, "
                "topics.last_article_by_subject",
            ),
            ("Schedules", f"{PREFIX}{ENV}-<topic_id>-daily-cycle"),
        ),
        alarms=(f"{PREFIX}{ENV}-daily-cycle-executions-failed", f"{PREFIX}{ENV}-pipeline-dlq-messages"),
    ),
    _lambda(
        "admin-api",
        "The Admin API behind admin_cli: topics, review, models, settings, rewrites.",
        "on each request to the Admin API (IAM-signed, behind an address allowlist)",
        details=(
            (
                "Sends",
                f"for a topic on the queue, topics trigger sends a research job to {PREFIX}{ENV}-work.fifo "
                f"or starts {PREFIX}{ENV}-daily-cycle with an execution named "
                "<topic>-<date>-manual-<HHMMSS>",
            ),
        ),
        dashboards=(_EDGE_DASHBOARD,),
    ),
    _lambda(
        "public-api",
        "The public API the site calls: topics, articles, RSS, stats, views and reader feedback.",
        "on each request to the public API (through its CloudFront cache)",
        dashboards=(_EDGE_DASHBOARD,),
        alarms=(
            f"{PREFIX}{ENV}-feedback-rejections-spike",
            f"{PREFIX}{ENV}-feedback-screening-budget-used-up",
            f"{PREFIX}{ENV}-security-high-severity",
        ),
    ),
    _lambda(
        "dlq-handler",
        "Turns each message on the pipeline's and the work queue's dead-letter queues into a "
        "failed-executions row.",
        f"on each message on {PREFIX}{ENV}-pipeline-dlq and {PREFIX}{ENV}-work-dlq.fifo",
    ),
    _lambda(
        "weekly-reflection",
        "Reads a week of reader feedback and proposes prompt refinements per topic.",
        "Mondays 13:00 Sydney time (EventBridge Scheduler)",
    ),
    _lambda(
        "stats-rollover",
        "Copies the week's stats-current row into stats-history and starts a new week.",
        "Mondays 13:15 Sydney time (EventBridge Scheduler)",
    ),
    _lambda(
        "cost-explorer-poll",
        "Reads the AWS bill from Cost Explorer (API Gateway, web search, WAF, every service) "
        "into the stats tables.",
        "daily at 10:00 UTC (EventBridge Scheduler)",
    ),
    _lambda(
        "model-catalog",
        "Keeps the models table's list and prices up to date: adds up to 5 new models (disabled), "
        "or at 50 prunes the ones its prune mode values least (never a default, fallback or "
        "rotation model); reprices up to 50, 5 every 5 minutes, reviewing each changed price 1 "
        "and 2 hours later; and weekly refreshes every topic's model rotation.",
        "every second day, discover at 02:00 and reprice at 03:00 Sydney time, the rotation cycle "
        "Fridays 04:00 Sydney time (EventBridge Scheduler), each tick and review on a one-shot "
        "schedule of its own, and on demand (models discover, models reprice)",
    ),
    _lambda(
        "reviewer",
        "Professor Grizzlewick, the reviewer bear: fixes content findings, reviews held articles "
        "(approves, rewrites, or rejects after three rewrites; at ten days publishes them with the "
        "flagged sentences cut, or rejects), reviews the newest published articles, takes down "
        "financial ones he approved that readers downvoted, and writes to the musings feed. "
        "Settings in the model-config table's reviewer row.",
        "every second day at 05:00 Sydney time (EventBridge Scheduler), each tick on a one-shot "
        "schedule of its own",
    ),
    _lambda(
        "musing-feedback",
        "Writes a musing reflecting on the last four days of reader feedback.",
        "every 4 days (EventBridge Scheduler)",
    ),
    _lambda(
        "security-events",
        "Turns the regional firewalls' block records into security incidents, and counts the "
        "admin API's 4xx answers toward the hour's admin-api-errors incident.",
        "on each batch of BLOCK records from the public and admin APIs' WAF logs, and of 4xx "
        "lines from the admin API's access log (log subscriptions)",
        alarms=(f"{PREFIX}{ENV}-security-high-severity",),
    ),
    _lambda(
        "sign-in-events",
        "Logs every sign-in to the assistant, and refuses a user after too many failed attempts.",
        "before and after every sign-in to the assistant's user pool (its Cognito triggers)",
        alarms=(f"{PREFIX}{ENV}-security-high-severity",),
        aliases=("sign-ins", "lockout"),
    ),
    _lambda(
        "ops-mcp",
        "The assistant's MCP server: the read-only tools the assistant answers with.",
        "on each POST /mcp to the assistant's API (Cognito sign-in)",
        alarmed=False,
        aliases=("mcp", "mcp-server"),
    ),
    _lambda(
        "ops-agent",
        "The assistant itself: takes the operator's question, calls the MCP server's tools "
        "through Bedrock, and answers.",
        "on each POST /ask to the assistant's API (Cognito sign-in)",
        alarmed=False,
        aliases=("agent", "assistant"),
    ),
    _lambda(
        "vision-worker",
        "Measures one site in one Sentinel-2 scene with OpenCV 5 (arm64) for the satellite_vision "
        "and rail_access adapters (the ships and rail_access tasks), and returns the metrics and a "
        "figure. Stores nothing.",
        "on each call from the research tick of a satellite_vision or rail_access topic, across Regions",
        alarmed=False,
        details=(
            (
                "Region",
                "the vision Region (OPS_VISION_REGION; us-west-2 unless the roots set vision_region), "
                "beside the imagery",
            ),
            ("Exists", "only when the environment sets vision_enabled"),
        ),
        aliases=("vision", "opencv"),
        # It lives in the vision Region, and only when vision is on (the deployment sets
        # OPS_VISION_REGION then, and "" otherwise): its log is read there, and with vision off it
        # is not deployed here at all, so a log review neither reads nor refuses it.
        region_env="OPS_VISION_REGION",
    ),
    # --- API Gateway ---------------------------------------------------------------------------
    Component(
        kind="api",
        key="public-api",
        name=f"{PREFIX}{ENV}-public-api",
        purpose="The site's API, open to everyone, behind CloudFront and a rate-limiting firewall.",
        details=(
            ("Stage", "dev in dev, production in production"),
            (
                "Access log fields",
                "requestId, requestTime, httpMethod, resourcePath, status, "
                "responseLatency, integrationLatency, responseLength, errorType, wafStatus",
            ),
            (
                "Metrics",
                "AWS/ApiGateway Count, 4XXError, 5XXError, Latency, IntegrationLatency "
                "(by ApiName and Stage); no per-status metric, so 400 vs 403 vs 429 is in the "
                "access log",
            ),
        ),
        log_groups=(
            f"/aws/apigateway/{PREFIX}{ENV}-public-api-access",
            f"/aws/lambda/{PREFIX}{ENV}-public-api",
            f"aws-waf-logs-{PREFIX}{ENV}-public-api",
        ),
        dashboards=(_EDGE_DASHBOARD,),
        aliases=("public", "site-api"),
    ),
    Component(
        kind="api",
        key="admin-api",
        name=f"{PREFIX}{ENV}-admin-api",
        purpose="The operator's API, called by admin_cli with IAM-signed requests, behind a "
        "firewall that admits only the operator's addresses.",
        details=(
            ("Stage", "dev in dev, production in production"),
            ("Access log fields", "the same as the public API's"),
            (
                "A 403 here",
                "usually the address allowlist (wafStatus 403) or a request not "
                "signed with the right IAM credentials",
            ),
        ),
        log_groups=(
            f"/aws/apigateway/{PREFIX}{ENV}-admin-api-access",
            f"/aws/lambda/{PREFIX}{ENV}-admin-api",
            f"aws-waf-logs-{PREFIX}{ENV}-admin",
        ),
        dashboards=(_EDGE_DASHBOARD,),
        aliases=("admin",),
    ),
    Component(
        kind="api",
        key="ops-mcp",
        name=f"{PREFIX}{ENV}-ops-mcp",
        purpose="The assistant's API: POST /ask (the agent) and POST /mcp (the MCP server), "
        "behind a Cognito sign-in and a low rate limit.",
        details=(("Throttling", "5 requests a second, bursts of 10; beyond that 429"),),
        log_groups=(
            f"/aws/apigateway/{PREFIX}{ENV}-ops-mcp-access",
            f"/aws/lambda/{PREFIX}{ENV}-ops-agent",
            f"/aws/lambda/{PREFIX}{ENV}-ops-mcp",
        ),
        aliases=("assistant-api", "ops-api", "ask"),
    ),
    # --- Orchestration -------------------------------------------------------------------------
    Component(
        kind="state_machine",
        key="daily-cycle",
        name=f"{PREFIX}{ENV}-daily-cycle",
        purpose="Wraps each topic's daily-cycle run: two retries 30 and 60 seconds apart, then "
        "the run goes to the dead-letter queue.",
        log_groups=(f"/aws/lambda/{PREFIX}{ENV}-daily-cycle",),
        dashboards=(_PIPELINE_DASHBOARD,),
        alarms=(f"{PREFIX}{ENV}-daily-cycle-executions-failed",),
        aliases=("step-functions", "sfn", "state-machine"),
    ),
    Component(
        kind="queue",
        key="pipeline-dlq",
        name=f"{PREFIX}{ENV}-pipeline-dlq",
        purpose="Where a daily-cycle run lands after its retries ran out; dlq-handler records "
        "each one in failed-executions.",
        log_groups=(f"/aws/lambda/{PREFIX}{ENV}-dlq-handler",),
        dashboards=(_PIPELINE_DASHBOARD,),
        alarms=(f"{PREFIX}{ENV}-pipeline-dlq-messages",),
        aliases=("dlq", "dead-letter-queue"),
    ),
    Component(
        kind="queue",
        key="work",
        name=f"{PREFIX}{ENV}-work.fifo",
        purpose="The FIFO work queue: the dispatcher's research jobs, and an operator's manual "
        "research runs of a topic on the queue (from the Admin API), one topic at a time, which the "
        "research tick takes one by one, at most worker_concurrency at once.",
        details=(
            (
                "Message",
                "topic_id, job, force (true only for a manual run), due_checked_at, dispatched_at; "
                "grouped by topic_id",
            ),
            (
                "Deduplicated by",
                "<topic_id>:research:<minute> (the dispatcher's); <topic_id>:research:manual:<hex> "
                "(a manual run, new each time)",
            ),
            ("Empty until", "a topic's dispatch_mode is queue; shadow mode sends nothing"),
            ("After three receives", f"the job goes to {PREFIX}{ENV}-work-dlq.fifo"),
        ),
        log_groups=(
            f"/aws/lambda/{PREFIX}{ENV}-dispatcher",
            f"/aws/lambda/{PREFIX}{ENV}-research-tick",
            f"/aws/lambda/{PREFIX}{ENV}-admin-api",
        ),
        alarms=(f"{PREFIX}{ENV}-work-queue-age",),
        aliases=("work-queue", "fifo"),
    ),
    Component(
        kind="queue",
        key="work-dlq",
        name=f"{PREFIX}{ENV}-work-dlq.fifo",
        purpose="Where a queue job lands after three receives without finishing (it timed out or "
        "crashed); dlq-handler records each one in failed-executions.",
        log_groups=(f"/aws/lambda/{PREFIX}{ENV}-dlq-handler",),
        alarms=(f"{PREFIX}{ENV}-work-dlq-messages", f"{PREFIX}{ENV}-work-dlq-received"),
        aliases=("work-dead-letter-queue",),
    ),
    Component(
        kind="topic",
        key="alerts",
        name=f"{PREFIX}{ENV}-alerts",
        purpose="Every alarm in this environment notifies this topic; it emails the operator "
        "when an alert address is set.",
        aliases=("sns", "alarms-topic"),
    ),
    # --- Dashboards ----------------------------------------------------------------------------
    Component(
        kind="dashboard",
        key="pipeline",
        name=_PIPELINE_DASHBOARD,
        purpose="Pipeline health: per Lambda runs, errors, throttles and duration; the daily "
        "cycle's failed and succeeded runs and the dead-letter queue; recent errors from every "
        "Lambda's log.",
        details=(("Opens on", "the last 7 days, hourly"),),
        aliases=("pipeline-dashboard", "health-dashboard"),
    ),
    Component(
        kind="dashboard",
        key="lambda-runs",
        name=f"{PREFIX}{ENV}-lambda-runs",
        purpose="How often each Lambda ran and what happened to reader feedback, over whatever "
        "span is picked.",
        details=(("Opens on", "the last 24 hours"),),
        aliases=("runs", "runs-dashboard"),
    ),
    Component(
        kind="dashboard",
        key="edge",
        name=_EDGE_DASHBOARD,
        purpose="The edge: both REST APIs' requests, 4XX and 5XX, latency, responses by status and "
        "errors by route from the access logs; the public API's CDN; every firewall's allowed, "
        "blocked and top blocked rules, addresses and paths.",
        details=(
            ("Opens on", "the last 7 days, hourly"),
            (
                "Only in production",
                "dev leaves it off (edge_dashboard_enabled): past the "
                "account's first three dashboards each costs US$3 a month. Set it to true in "
                "infra/environments/dev/main.tf for a while to debug dev's edge.",
            ),
        ),
        only_in=("production",),
        aliases=("edge-dashboard", "api-dashboard", "waf-dashboard", "firewall-dashboard"),
    ),
    # --- Log groups that belong to no one function ---------------------------------------------
    Component(
        kind="log_group",
        key="waf-public-api",
        name=f"aws-waf-logs-{PREFIX}{ENV}-public-api",
        purpose="The public API firewall's log: every request it looked at, with the rule that "
        "matched and the action. Visitor-identifying headers are redacted.",
        details=(("Kept for", "14 days"),),
        aliases=("public-api-waf-log",),
    ),
    Component(
        kind="log_group",
        key="waf-admin",
        name=f"aws-waf-logs-{PREFIX}{ENV}-admin",
        purpose="The admin API firewall's log: requests from outside the operator's addresses "
        "show up here as blocked.",
        details=(("Kept for", "30 days"),),
        aliases=("admin-waf-log",),
    ),
    Component(
        kind="log_group",
        key="waf-shared",
        name=SHARED_WAF_LOG_GROUP,
        purpose="The site's CloudFront firewall log, shared by both environments; in us-east-1, "
        "where CloudFront's firewall lives.",
        only_in=("production",),
        aliases=("cloudfront-waf-log", "site-waf-log"),
    ),
    Component(
        kind="log_group",
        key="public-api-access",
        name=f"/aws/apigateway/{PREFIX}{ENV}-public-api-access",
        purpose="One JSON line per public API request: method, path, status, latency, errorType "
        "and the firewall's status. No address or user agent.",
        aliases=("public-api-access-log",),
    ),
    Component(
        kind="log_group",
        key="admin-api-access",
        name=f"/aws/apigateway/{PREFIX}{ENV}-admin-api-access",
        purpose="One JSON line per admin API request, in the same shape as the public API's.",
        aliases=("admin-api-access-log",),
    ),
    Component(
        kind="log_group",
        key="ops-mcp-access",
        name=f"/aws/apigateway/{PREFIX}{ENV}-ops-mcp-access",
        purpose="One JSON line per request to the assistant's API (/ask and /mcp).",
        aliases=("assistant-access-log",),
    ),
    # --- Schedules, buckets, firewalls ---------------------------------------------------------
    Component(
        kind="schedule",
        key="topic-schedules",
        name=f"{PREFIX}{ENV}-<topic_id>-research-tick / -daily-cycle",
        purpose="Two schedules per topic on its schedules (dispatch_mode schedule), made by the "
        "Admin API when a topic is created or changed (not by Terraform): research-tick at the "
        "topic's interval, daily-cycle at its daily cadence and timezone. A topic on the queue has "
        "none.",
        details=(
            ("Where", 'EventBridge Scheduler, schedule group "default"'),
            (
                "On the queue",
                "topics update <id> --dispatch-mode queue deletes both once the operator confirms; "
                "--dispatch-mode schedule, or topics reschedule --dispatch-mode schedule for every "
                "topic, writes them again",
            ),
            (
                "A dropped run",
                "the alarm is for the whole group, the fixed schedules (the weekly rollover, the "
                "cost poll) as well as the topics'; Scheduler's own metrics say which",
            ),
        ),
        # A run Scheduler gave up on is silent: the function is never invoked, so it has no error.
        alarms=(f"{PREFIX}{ENV}-schedule-invocations-dropped",),
        aliases=("schedules", "scheduler", "eventbridge"),
    ),
    Component(
        kind="schedule",
        key="dispatcher",
        name=f"{PREFIX}{ENV}-dispatcher",
        purpose="Invokes the dispatcher every minute (made by Terraform, never retried: the next "
        "tick looks back to the last one that finished).",
        details=(("Expression", "rate(1 minute)"),),
        log_groups=(f"/aws/lambda/{PREFIX}{ENV}-dispatcher",),
        # A dispatcher that is not invoked has no error to alarm on; the heartbeat notices.
        alarms=(f"{PREFIX}{ENV}-dispatcher-heartbeat", f"{PREFIX}{ENV}-schedule-invocations-dropped"),
        aliases=("dispatcher-schedule",),
    ),
    Component(
        kind="bucket",
        key="content",
        name=f"{PREFIX}{ENV}-content",
        purpose=(
            "Article bodies (articles/), the raw source snapshots research works from (snapshots/, "
            "expiring after 21 days), each vision topic's running state "
            "(snapshots/<topic>/running-state.json), the vision figures "
            "(vision/<topic>/<site>/<scene>.png) and each rail city's network "
            "(vision/<topic>/<site>/network.json)."
        ),
        details=(
            ("Name", "unique across all of AWS, which is why each deployment has its own prefix"),
            ("Also", "pricing/<date>-<run>.json: old model prices a reprice replaced, once checked"),
        ),
        aliases=("content-bucket", "s3"),
    ),
    Component(
        kind="bucket",
        key="site",
        name=f"{PREFIX}{ENV}-site",
        purpose="The static site CloudFront serves: HTML, scripts and the pre-built pages.",
        details=(("Name", "unique across all of AWS, which is why each deployment has its own prefix"),),
        aliases=("site-bucket", "frontend"),
    ),
    Component(
        kind="firewall",
        key="public-api-acl",
        name=f"{PREFIX}{ENV}-public-api",
        purpose="The public API's regional WAF web ACL: rate limits (general and on feedback) and "
        "AWS's common rule set.",
        log_groups=(f"aws-waf-logs-{PREFIX}{ENV}-public-api",),
        dashboards=(_EDGE_DASHBOARD,),
        aliases=("public-api-waf", "waf"),
    ),
    Component(
        kind="firewall",
        key="admin-acl",
        name=f"{PREFIX}{ENV}-admin-api",
        purpose="The admin API's regional WAF web ACL: admits only the operator's addresses.",
        log_groups=(f"aws-waf-logs-{PREFIX}{ENV}-admin",),
        dashboards=(_EDGE_DASHBOARD,),
        aliases=("admin-waf", "allowlist"),
    ),
)


# --- The layers -----------------------------------------------------------------------------------
#
# The owner's ask: "how does the project work?" was answered with one table of fifty resources,
# too much to listen to or read. The architecture is now told in layers: asked with nothing, the
# tool names the layers and what each does and asks which one; `layer` gives one of them (its AWS
# services, then our resources in it); "everything" is the old full table, for when it is wanted.
#
# A layer's `services` are (name, what it does here) and are plain facts about this project, each
# from the Terraform or the code named beside it. A resource of the catalogue belongs to exactly
# one layer, by its kind (`kinds`): tests/test_ops_mcp_architecture.py holds that every kind has
# one. Three layers hold no catalogue resource (the pages, identity, AI): what they are made of is
# not a named resource the assistant reads, so they are services only.
EVERYTHING = "everything"


@dataclass(frozen=True)
class Layer:
    key: str
    title: str  # as shown: "Edge (delivery and security)"
    said: str  # as spoken in a list: "the edge"
    summary: str
    services: tuple[tuple[str, str], ...]
    kinds: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


LAYERS: tuple[Layer, ...] = (
    Layer(
        key="edge",
        title="Edge (delivery and security)",
        said="the edge",
        summary="The gatekeeper in front of everything public: it caches the site, fronts the "
        "public API and turns away abusive traffic before any of our code runs.",
        services=(
            # infra/modules/static-site and infra/modules/api-cdn: one distribution each.
            (
                "Amazon CloudFront",
                "The CDN: one distribution serves the static site, another fronts the public API.",
            ),
            # The catalogue's firewalls, and SHARED_WAF_LOG_GROUP for the CloudFront-scope ACL.
            (
                "AWS WAF",
                "Rate limits and AWS's common rule set on the public API, an address allowlist on "
                "the admin API, and a shared web ACL on the CloudFront distributions. What it "
                "blocks becomes security incidents.",
            ),
            # Not in the Terraform: AWS applies it to CloudFront by itself.
            (
                "AWS Shield Standard",
                "AWS's own DDoS protection, on by default; nothing of ours to configure.",
            ),
        ),
        kinds=("firewall",),
        aliases=("edge layer", "delivery", "security", "waf", "firewall", "firewalls", "cloudfront", "cdn"),
    ),
    Layer(
        key="presentation",
        title="Presentation (the site and the operator's page)",
        said="the presentation layer",
        summary="Plain HTML, CSS and JavaScript with no framework, served from the site bucket: "
        "the readers' site and the operator's own page.",
        services=(
            # frontend/index.html and app.js: hash routes (#/article/<id>).
            (
                "The public site (index.html)",
                "A small hash-routed single-page app: topics, published articles, the musings, "
                "stats and reader feedback.",
            ),
            # frontend/ask.html and ask.js.
            (
                "The operator's page (ask.html)",
                "This assistant's page: push to talk, the answer read aloud by the browser, and "
                "the tables and suggestion cards.",
            ),
            # frontend/about.html, terms.html, privacy.html; common/static_pages.py for articles.
            (
                "Static pages",
                "About, terms and privacy, and a pre-built page per published article, cached at the CDN.",
            ),
        ),
        aliases=("frontend", "front end", "ui", "site", "pages", "spa", "website"),
    ),
    Layer(
        key="api",
        title="API gateways (routing and who may call)",
        said="the API gateways",
        summary="Three REST APIs keep public reading apart from privileged operations, each with "
        "its own way of deciding who may call.",
        services=(
            (
                "Public REST API",
                "No sign-in, throttled: topics, published articles, views, the RSS feed, feedback.",
            ),
            # scripts/admin_cli.py signs with SigV4; the admin ACL admits the operator's addresses.
            (
                "Admin REST API",
                "IAM-signed (SigV4) and limited to the operator's addresses: what the Admin CLI calls.",
            ),
            # infra/modules/ops-assistant: /ask and /mcp behind the Cognito authorizer.
            ("Assistant's REST API", "The assistant's own routes (/ask and /mcp), behind a sign-in."),
        ),
        kinds=("api",),
        aliases=("api gateway", "api gateways", "apis", "gateway", "gateways", "routing", "api layer"),
    ),
    Layer(
        key="identity",
        title="Identity and access",
        said="identity and access",
        summary="Who and what may act: no long-lived keys in the repository, a role per job, and "
        "a sign-in for the assistant.",
        services=(
            # infra/bootstrap: the GitHub OIDC provider and the deploy roles.
            (
                "AWS IAM with GitHub OIDC",
                "GitHub Actions assumes a deploy role through OIDC federation, so the repository "
                "holds no AWS keys. Each Lambda runs under a role limited to what it needs.",
            ),
            # infra/modules/ops-assistant: the user pool; MFA per environment.
            (
                "Amazon Cognito",
                "The assistant's user pool: sign-in (with MFA where the environment requires it) "
                "and the token every request to the assistant carries.",
            ),
        ),
        aliases=(
            "identity", "iam", "auth", "authentication", "access",
            "cognito", "sign in", "sign-in", "login",
        ),
    ),
    Layer(
        key="orchestration",
        title="Orchestration (schedules and workflows)",
        said="orchestration",
        summary="What makes things happen on time and what catches them when they fail: "
        "per-topic schedules, a workflow around the daily article, and a dead-letter queue.",
        services=(
            (
                "Amazon EventBridge Scheduler",
                "Two schedules per topic, made when a topic is created, and the fixed jobs.",
            ),
            (
                "AWS Step Functions",
                "Wraps the daily article cycle so it retries, and has one place to fail to.",
            ),
            (
                "Amazon SQS (dead-letter queue)",
                "Takes a run that ran out of retries; a Lambda records it for a person.",
            ),
        ),
        kinds=("schedule", "state_machine", "queue"),
        aliases=(
            "scheduling", "schedules", "scheduler", "workflows",
            "workflow", "step functions", "eventbridge",
        ),
    ),
    Layer(
        key="compute",
        title="Compute (the Lambdas)",
        said="compute",
        summary="All the code, as Lambda functions built from one package, each under its own role.",
        services=(
            (
                "Pipeline Lambdas",
                "Research, the daily article, the weekly reflection.",
            ),
            ("API Lambdas", "Answer the public API and the admin API."),
            ("Housekeeping Lambdas", "Stats, the daily cost read, failed runs, firewall and sign-in events."),
            ("Assistant Lambdas", "The assistant's agent and its MCP tool server."),
        ),
        kinds=("function",),
        # "The research pipeline" is what the owner calls the Lambdas that do the work.
        aliases=(
            "lambda", "lambdas", "functions", "serverless",
            "code", "compute layer", "pipeline", "research",
        ),
    ),
    Layer(
        key="ai",
        title="AI (models and agents)",
        said="the AI layer",
        summary="Where a model is called. Every call goes through one place in the code, which "
        "counts its tokens and cost.",
        services=(
            # common/bedrock.py: the Converse API, with a fallback model.
            (
                "Amazon Bedrock (Converse API)",
                "Research summaries, ideas, drafts, the reviews, rewrites, musings and comment screening.",
            ),
            # infra/modules/web-search; common/web_search.py's provider.
            ("Amazon Bedrock AgentCore", "A managed web search for news, used when the free source fails."),
            # lambdas/ops_mcp/server.py.
            (
                "MCP server",
                "This assistant's tools, read-only, as Model Context Protocol tools the agent calls. "
                "It runs in the ops-mcp Lambda.",
            ),
        ),
        aliases=("ai", "intelligence", "inference", "bedrock", "models", "agents", "agent", "mcp", "llm"),
    ),
    Layer(
        key="data",
        title="Data and storage",
        said="data and storage",
        summary="Structured records in DynamoDB, files in S3, secrets in Parameter Store, and "
        "nothing personal stored anywhere.",
        services=(
            ("Amazon DynamoDB", "Topics, findings, articles, the moderation queue, feedback, models, stats."),
            ("Site S3 bucket", "The static site: HTML, scripts, styles and the pre-built article pages."),
            ("Content S3 bucket (private)", "Article bodies and the raw snapshots research works from."),
            # COINGECKO_API_KEY_PARAMETER and friends: SecureStrings read at cold start.
            ("AWS Systems Manager Parameter Store", "API keys for the data sources, as SecureStrings."),
        ),
        kinds=("table", "bucket"),
        aliases=(
            "storage", "data", "database", "dynamodb", "tables",
            "s3", "buckets", "ssm", "secrets", "persistence",
        ),
    ),
    Layer(
        key="observability",
        title="Observability (monitoring and cost)",
        said="observability",
        summary="How anyone, this assistant included, sees what happened and what it cost.",
        services=(
            ("Amazon CloudWatch", "Every function's and API's logs, the dashboards, and the alarms."),
            ("Amazon SNS", "The alerts topic every alarm and high-severity incident is sent to."),
            (
                "Cost tracking",
                "Tokens and cost per article and per week in the stats tables, and the AWS bill read daily.",
            ),
        ),
        kinds=("dashboard", "log_group", "topic"),
        aliases=(
            "monitoring", "cloudwatch", "logs", "dashboards", "alarms",
            "cost", "costs", "costing", "metrics",
        ),
    ),
)
LAYER_KEYS = tuple(layer.key for layer in LAYERS)
_EVERYTHING_WORDS = frozenset({
    "everything", "all", "full", "whole",
    "all of it", "in detail", "detail", "the lot",
})


def layer_of(component: Component) -> Layer:
    """The one layer a catalogue resource belongs to, by its kind."""
    return next(layer for layer in LAYERS if component.kind in layer.kinds)


def resolve_layer(raw) -> str | None:
    """A layer's key, or EVERYTHING, from the operator's words for it ("storage", "the edge layer",
    "lambdas", "all of it"); None when it names neither."""
    if not isinstance(raw, str):
        return None
    words = re.sub(r"[^a-z0-9 -]", " ", raw.lower())
    words = re.sub(r"\b(the|layer|layers|tier|and|please|about)\b", " ", words)
    words = " ".join(words.split())
    if not words:
        return None
    if words in _EVERYTHING_WORDS or words.split()[0] in ("everything", "all"):
        return EVERYTHING
    for layer in LAYERS:
        names = {layer.key, layer.said, layer.title.split(" (")[0].lower(), *layer.aliases}
        cleaned = {" ".join(re.sub(r"\b(the|layer|and)\b", " ", name).split()) for name in names}
        if words in cleaned:
            return layer.key
    for layer in LAYERS:
        if any(word in {layer.key, *layer.aliases} for word in words.split()):
            return layer.key
    return None


# --- The features ---------------------------------------------------------------------------------
#
# The owner's ask: a layer answers "what is this made of?", not "how does this one thing work?".
# A feature is a smaller, logical grouping that cuts across the layers: the parts that together
# make one thing the project does, told in the order the work flows through them. There are seven,
# each told from its page under docs/architecture/: article research (from a topic's data source to
# an article in S3), the vision pipeline (from a satellite site to a map in an article,
# blogger-vision.md), the site and reader feedback, stats and costs, security incidents, the
# reviewer bear and the model catalog. The other three pages there (the admin CLI, this assistant
# and Alexa+) have no entry: they name the parts to ask about instead. More can be added the same
# way ("prompts as assets" is the next one the owner has in mind) by adding a Feature below, as
# docs/architecture/README.md's "Adding a feature" says; resolve_feature picks the feature with the
# longest alias the words hold.
#
# A step's `parts` are catalogue resources as "kind:key" (tests/test_ops_mcp_architecture.py
# holds that each exists, so a feature cannot name a resource the catalogue lost) and, for what is
# not a named resource of ours (Bedrock, an adapter, a third-party API), plain words. Every step is
# a plain fact about this project, from the code named beside it.


@dataclass(frozen=True)
class Step:
    title: str  # "Research tick: diff first"
    what: str  # what happens, one or two sentences
    parts: tuple[str, ...]  # "kind:key" for a catalogue resource, or a name in plain words


@dataclass(frozen=True)
class Feature:
    key: str
    title: str  # as shown: "Article research (from a data source to a published article)"
    said: str  # as spoken in a list: "article research"
    summary: str
    steps: tuple[Step, ...]
    aliases: tuple[str, ...] = ()


FEATURES: tuple[Feature, ...] = (
    Feature(
        key="article-research",
        title="Article research (from a data source to a published article)",
        said="article research",
        summary="How a topic's data source becomes a published article: the research tick finds "
        "what is new, the daily cycle turns it into candidate ideas, a draft and two reviews, and "
        "the article lands in S3.",
        steps=(
            # common/adapters/: base.py (the contract), registry.py, one module per source.
            Step(
                "Topic and adapter",
                "A topic is a row of settings, made with the Admin CLI: which adapter reads its "
                "source, how often to research, its model and review mode. The adapter is the only "
                "code that knows the source: GitHub, Hacker News, CoinGecko or web search, or "
                "satellite imagery (see the vision pipeline).",
                ("table:topics", "Adapters (common/adapters/)"),
            ),
            # common/scheduler.py, from admin_api_handler.py on topic create, update and delete
            # (research_cadence, rate(1 hour) by default); common/research_schedule.py says when due.
            Step(
                "Schedules",
                "Creating a topic makes its two EventBridge schedules: a research heartbeat (hourly "
                "by default), and the daily cycle at its time and timezone. The heartbeat is not the "
                "research interval: each tick decides whether the topic is due.",
                ("schedule:topic-schedules", "function:admin-api"),
            ),
            # crypto_feed.py COINGECKO_API_KEY_PARAMETER, github_trending.py GITHUB_API_TOKEN_PARAMETER,
            # common/web_search.py (GDELT, then AgentCore).
            Step(
                "Third-party sources and their keys",
                "API keys are SecureStrings in SSM Parameter Store, named by an environment "
                "variable Terraform sets and read at cold start, never stored in code or tables; "
                "both are optional. News search uses GDELT, which needs no key, and falls back to "
                "Bedrock AgentCore web search, which the function's IAM role authorises.",
                (
                    "SSM Parameter Store (CoinGecko key, GitHub token)",
                    "GDELT",
                    "Bedrock AgentCore web search",
                ),
            ),
            # research_tick_handler.py: diff-first (rule 2) against the last Finding's snapshot and
            # its seen-set (_merge_seen, SEEN_KEY); _not_due skips a topic that is not due.
            Step(
                "Research tick: diff first",
                "Unless the topic is paused or not due, the adapter fetches the source now and "
                "compares it with the snapshot of the topic's last finding in the content bucket, "
                "and with the items already reported (the seen-set), so an item that comes back is "
                "not new again. Nothing new: it stops, and no model is called.",
                ("function:research-tick", "bucket:content"),
            ),
            # research_tick_handler.py: the summary first, then snapshots/{topic_id}/{captured_at}.json,
            # then the Finding.
            Step(
                "Findings",
                "Bedrock summarises only what is new. Only once the summary is back is the raw "
                "snapshot stored under snapshots/ in the content bucket, and the summary written as "
                "a finding with where its snapshot is and its sources. Findings expire after 14 "
                "days, snapshots after 21.",
                ("table:findings", "Amazon Bedrock (Converse API)"),
            ),
            # daily_cycle_handler.py steps 2-4 (MAX_WINDOW_FINDINGS), common/findings_window.py. The
            # state machine retries twice, then the DLQ, but the handler catches its own errors, so
            # only a timeout or a crash is retried and reaches the DLQ.
            Step(
                "Candidate ideas",
                "Once a day, through the daily-cycle state machine, the daily cycle reads the "
                "topic's findings since its last article, but only within its findings window "
                "(findings_window_hours: a day unless the topic sets a wider one, up to 14 days), "
                "at most 48 of them, newest first. It asks Bedrock for three candidate angles, "
                "stores them as candidate ideas and selects the first. Ideas expire after 7 days.",
                (
                    "state_machine:daily-cycle",
                    "function:daily-cycle",
                    "table:candidate-ideas",
                ),
            ),
            # daily_cycle_handler._draft_article and _draft_title; common/model_routing.py,
            # equipment.py (the gear worn), compliance.py (financial guidance); the few-shot excerpt.
            Step(
                "Drafting",
                "Bedrock drafts the article with the topic's model from the model registry, folding "
                "in the topic's approved prompt refinements (the bear's gear), a top-voted past "
                "excerpt, and financial guidance where it applies. The title is a separate call, "
                "after the body; a title that is not one, or a draft cut off, holds the article.",
                (
                    "table:models",
                    "table:model-config",
                    "table:prompt-refinements",
                    "Amazon Bedrock (Converse API)",
                ),
            ),
            # common/fresh_review.py, common/compliance.py (rule 3 and rule 4); common/reviewer.py.
            Step(
                "Reviews",
                "A fresh-data review checks the draft's claims against what the source says now "
                "(only recorded in shadow mode, the default; enforce mode revises or holds); then a "
                "compliance review decides publish or hold. A financial topic's article is always "
                "held when drafted, whatever the review says; the reviewer bear may approve it "
                "after 48 hours, and flags it for a person to check.",
                ("Fresh-data review (common/fresh_review.py)", "Compliance review (common/compliance.py)"),
            ),
            # daily_cycle_handler._publish_or_moderate; common/static_pages.py; stats_tracking.py;
            # common/reviewer.py (stale_after_hours, 48 by default).
            Step(
                "The article in S3",
                "The body is written to the content bucket as articles/<id>.md and the article's "
                "row records its lineage, tokens and cost. Published, a static page goes to the "
                "site bucket; held, it waits in the moderation queue for a person (admin_cli "
                "approve) or, after 48 hours, the reviewer bear.",
                (
                    "bucket:content",
                    "table:articles",
                    "bucket:site",
                    "table:moderation-queue",
                    "table:stats-current",
                ),
            ),
            # The ops assistant: ops_agent/ (Strands) and ops_mcp/ (the MCP server), read-only. The
            # vision triage agent: common/vision_triage.py.
            Step(
                "Where the AI agents are",
                "Every model call above is one Bedrock request from a Lambda, not an agent. One "
                "agent is this assistant: a Strands agent calling the MCP server's read-only tools, "
                "which read the same topics, findings, ideas and articles, and change none of them. "
                "The other is the vision triage agent, inside a vision topic's research tick.",
                ("function:ops-agent", "function:ops-mcp"),
            ),
        ),
        aliases=(
            "article research", "research", "articles", "article pipeline",
            "research pipeline", "writing pipeline", "authoring", "how articles are written",
            "how an article is written", "how is an article written", "how are articles written",
            "how is an article researched", "how are articles researched", "findings",
            "candidate ideas", "reviews", "draft review", "draft reviews", "compliance review",
            "fresh data review",
        ),
    ),
    # docs/architecture/blogger-vision.md's steps, as the assistant tells them.
    Feature(
        key="vision-pipeline",
        title="The vision pipeline (from a satellite site to a map in an article)",
        said="the vision pipeline",
        summary="How a topic that watches fixed sites in satellite imagery becomes an article with a "
        "map: a worker in the vision Region measures each new scene, an agent judges a change, and "
        "a person approves.",
        steps=(
            # common/adapters/satellite_vision.py, rail_access.py; MANUAL_REVIEW_ADAPTERS.
            Step(
                "Topic, sites and the switch",
                "A vision topic's sites are polygons written by hand in its config, measured only "
                "where the deployment sets VISION_ENABLED. Every vision article waits for a person "
                "(force_manual_review).",
                (
                    "table:topics",
                    "common/adapters/satellite_vision.py and rail_access.py",
                    "VISION_ENABLED",
                ),
            ),
            # common/vision_sites.py: order_sites, Budget, time_to_measure.
            Step(
                "Research tick: sites take turns, inside a time budget",
                "The tick finds the newest clear Sentinel-2 scene over each site, the longest "
                "waiting site first; a site left without time waits for the next tick.",
                ("function:research-tick", "Earth Search STAC (Sentinel-2 L2A)"),
            ),
            # common/osm.py; rail_access.py's network objects (vision_sites.network_key).
            Step(
                "Map data (rail)",
                "A rail topic reads each city's rail network from OpenStreetMap through the "
                "Overpass API and keeps it in its own object in the content bucket for osm_ttl_days.",
                ("OpenStreetMap Overpass API",),
            ),
            # vision_worker_handler.py, lambdas/vision/, common/vision_client.py.
            Step(
                "The worker measures",
                "A stateless worker in the vision Region range-reads only the tiles under the site "
                "and measures them with OpenCV, across Regions: it returns metrics, a figure and its "
                "build, and keeps nothing.",
                (
                    "function:vision-worker",
                    "Sentinel-2 COGs (sentinel-cogs, us-west-2)",
                    "OpenCV 5 on arm64",
                ),
            ),
            # research_tick_handler._running_state_key; common/vision_sites.store_image.
            Step(
                "Running state and figures",
                "Every tick keeps the topic's running state, with each site's history and last "
                "error, and the figures are PNGs under vision/ in the content bucket.",
                ("bucket:content",),
            ),
            # material_diff, then common/vision_triage.py; the spend is vision_triage.
            Step(
                "Diff first, then the triage agent",
                "A change against the site's usual range is only a candidate until the triage agent "
                "judges it real; unsure is an artefact, and no change calls no model.",
                ("Amazon Bedrock (the triage agent's Converse loop)", "table:stats-current"),
            ),
            Step(
                "A Finding",
                "A real change, or the first clear scene, is written as a Finding with its figures "
                "and, for rail, its planner notes.",
                ("table:findings",),
            ),
            # daily_cycle_handler.py; common/static_pages.publish_figures.
            Step(
                "The article waits for a person",
                "The daily cycle drafts and reviews the article and holds it; once a person "
                "approves it, its figures are copied to articles/figures/ in the site bucket.",
                ("function:daily-cycle", "table:moderation-queue", "bucket:site"),
            ),
            # ops_mcp/vision_status.py, log_review.py, vision_evidence.py.
            Step(
                "Where to look",
                "This assistant's vision_status reads each site's running state, log_review "
                "reads the worker's log in the vision Region, and vision_evidence reads what the "
                "pipeline decided from its evidence lines.",
                ("function:ops-mcp",),
            ),
        ),
        aliases=(
            "vision", "vision pipeline", "vision research", "vision research pipeline",
            "vision findings", "vision finding", "vision review", "vision reviews", "satellite",
            "satellite vision", "satellite imagery", "satellite research", "rail", "rail access",
            "rail access monitor", "rail findings", "rail review", "rail reviews", "opencv",
            "sentinel", "ships",
        ),
    ),
    # docs/architecture/site-and-feedback.md's steps, as the assistant tells them: its twelve, with
    # the token's three (status, the wait, the decoy) made one and drafting with gear and wear one.
    Feature(
        key="site-and-feedback",
        title="The site and reader feedback (from a vote to the bear's gear and musings)",
        said="the site and reader feedback",
        summary="How a reader's visit and vote become the bear's gear and musings: every vote passes "
        "cheap checks before it is kept, and a week of feedback becomes a proposal that the bear "
        "wears only once you approve it.",
        steps=(
            # frontend/index.html and app.js (the hash routes); render_and_publish_article_page in
            # common/static_pages.py.
            Step(
                "The site",
                "A static single-page app served from the site bucket, with pages for the topics, an "
                "article, the musings and the stats. Every published article also gets a static page "
                "of its own, rendered when it is published.",
                ("bucket:site", "frontend/ (index.html, app.js, article-widgets.js)"),
            ),
            # public_api_handler._ROUTES and _response (a max-age only where the route sets one);
            # infra/modules/api-cdn/; aws_wafv2_web_acl.public_api's rate rules.
            Step(
                "The public API",
                "One anonymous REST API with eleven routes, all answered by one Lambda. Its CDN caches "
                "only what the Lambda marks cacheable, and the regional firewall limits each visitor to "
                "500 requests and 10 feedback posts in 5 minutes.",
                ("api:public-api", "function:public-api", "firewall:public-api-acl"),
            ),
            # public_api_handler._feedback_status and _submit_feedback (HONEYPOT_FIELD);
            # common/feedback_verification.py (issue, verify); frontend/verify.js.
            Step(
                "Feedback status and the token",
                "Before the form shows, the page asks whether feedback is open and, if it is, gets a "
                "signed one-use token, valid after a random 0.5 to 2 seconds, with proof of work when "
                "the site is busy. A post that fills the hidden decoy field is told it worked, and "
                "nothing is kept.",
                ("table:model-config", "frontend/verify.js"),
            ),
            # common/comment_screening.py (screen_comment); feedback_limits.take_screening_slot;
            # security_events.record_incident and record_trend, from _submit_feedback.
            Step(
                "The comment screen",
                "A comment is checked by code, then by one model call that must answer KEEP; once "
                "today's model-check budget is used up, every comment is dropped. A dropped comment "
                "refuses the whole submission, vote and all, and an attack-shaped one also opens a "
                "security incident.",
                (
                    "Comment screening (common/comment_screening.py)",
                    "Amazon Bedrock (Converse API)",
                    "table:security-events",
                ),
            ),
            # feedback_limits.acquire; put_feedback and update_article_net_votes in common/dynamo.py;
            # stats_tracking.record_feedback_given.
            Step(
                "Counted and stored",
                "Only then is it counted against the article, the day and the rate window. The vote and "
                "any comment are stored with no identifier of any kind, the article's net votes move by "
                "one, and the week's feedback count goes up.",
                ("table:feedback", "table:articles", "table:stats-current"),
            ),
            # weekly_reflection_handler.py (_LOOKBACK_DAYS); gear.generate_identity and roll_rarity.
            Step(
                "The weekly reflection",
                "Mondays at 13:00 Sydney time it reads the last 7 days of feedback and, for each topic "
                "with some, asks a model for a change, the comments passed as data. Each is stored as a "
                "pending proposal, already named as gear, with a rarity code rolls.",
                (
                    "function:weekly-reflection",
                    "table:prompt-refinements",
                    "Amazon Bedrock (Converse API)",
                ),
            ),
            # admin_api_handler._resolve_prompt_refinement and _announce_drop; equipment.plan_equip.
            Step(
                "Your decision",
                "A proposal changes nothing until you approve it with the Admin CLI. With no choice made "
                "it is worn as a ring for its topic, or parked in the backpack when all five rings are "
                "worn, and the first time a piece is put on, the bear posts a loot drop unless you skip "
                "it.",
                ("function:admin-api", "table:prompt-refinements", "table:musings"),
            ),
            # daily_cycle_handler._get_approved_guidance; equipment.guidance_for and pick_armor
            # (MAX_GUIDANCE_CHARS); wear.apply_feedback.
            Step(
                "Gear: drafting and wear",
                "Each draft wears its topic's rings and a random part of the worn armor, up to about "
                "4,000 characters, recorded on the article. A downvote costs each of those pieces one "
                "durability and an upvote gives one back; a piece at none is taken off.",
                ("function:daily-cycle", "table:articles", "table:prompt-refinements"),
            ),
            # common/musings.py (_MAX_MUSING_CHARS); musing_feedback_handler.py;
            # public_api_handler._list_musings; frontend/moods.js.
            Step(
                "Musings",
                "Short posts in the bear's voice, 280 characters at most, each with a mood: when an "
                "article is published or rejected, a topic is paused or resumed or gear drops, every "
                "4 days about feedback, and from Professor Grizzlewick.",
                ("table:musings", "function:musing-feedback", "function:reviewer"),
            ),
        ),
        aliases=(
            "site and feedback", "site and reader feedback", "site", "public site", "website",
            "public api", "reader feedback", "feedback", "votes", "vote", "voting",
            "comment screening", "comment screen", "honeypot", "decoy field", "proof of work",
            "feedback token", "weekly reflection", "prompt refinements", "refinements", "proposals",
            "gear", "equipment", "armor", "rings", "wear", "loot drops", "mood", "moods", "musings",
            "musing",
        ),
    ),
    # docs/architecture/stats-and-costs.md's steps, as the assistant tells them: its nine, with the
    # API and the page made one.
    Feature(
        key="stats-and-costs",
        title="Stats and costs (from a model call to the Stats page)",
        said="stats and costs",
        summary="How every model call and request becomes a number on the Stats page: an article's "
        "cost is kept on the article, everything else goes onto one weekly row, a Monday rollover "
        "keeps each week, and a daily poll reads the AWS bill.",
        steps=(
            # bedrock.invoke_model_tracked; costing.build_lineage; stats_tracking.record_article_lineage,
            # from daily_cycle_handler.py and common/rewrite.py.
            Step(
                "An article's own cost",
                "Every model call that goes into an article is recorded in its lineage with its tokens "
                "and cost; one call nothing can price leaves the whole article unpriced, never "
                "part-summed. The article's whole cost is also added onto this week's row.",
                ("table:articles", "function:daily-cycle", "common/costing.py"),
            ),
            # stats_tracking.tracked_claude, record_model_usage and record_assistant_run
            # (BEDROCK_CATEGORIES).
            Step(
                "Spend on no article",
                "Each model call outside an article is tracked with a category, such as musings, "
                "comment screening or this assistant, and its tokens and cost are added onto this "
                "week's row. A call with no price is counted as unpriced, never as free.",
                ("table:stats-current", "common/stats_tracking.py (tracked_claude)"),
            ),
            # stats_tracking's record_* counters (AGENTCORE_WEB_SEARCH_USD_PER_QUERY);
            # lambda_timing.track_lambda_duration.
            Step(
                "Activity and run time",
                "Plain counters go onto the same row: feedback kept and rejected, loot drops, and web "
                "searches, each AgentCore one at US$0.007. The pipeline's Lambdas time themselves and "
                "add their milliseconds.",
                ("table:stats-current", "common/lambda_timing.py"),
            ),
            # increment_current_stats and get_current_stats in common/dynamo.py (STATS_CURRENT_SHARDS).
            Step(
                "This week's row",
                "Every increment is added onto one of 8 shard rows picked at random, so no one item is "
                "hot. Reading the week sums them with the base row, which holds the cost poll's "
                "readings.",
                ("table:stats-current",),
            ),
            # stats_rollover_handler.py; stats_tracking.split_for_rollover.
            Step(
                "The weekly rollover",
                "Mondays at 13:15 Sydney time, after the weekly reflection, the week's row is copied "
                "into history under its Monday and added onto the all-time row, then cleared for the "
                "new week.",
                ("function:stats-rollover", "table:stats-history"),
            ),
            # cost_explorer_poll_handler.py; cost_explorer.fetch_costs (COMPLETE_WEEKS, us-east-1);
            # stats_tracking.record_aws_bill.
            Step(
                "The daily cost poll",
                "Every day at 10:00 UTC it reads the AWS bill by service from Cost Explorer, up to "
                "yesterday. This week's bill goes onto this week's row, each of the last six complete "
                "weeks onto its history row, and the all-time bill is added up again.",
                ("function:cost-explorer-poll", "AWS Cost Explorer (us-east-1)", "table:stats-history"),
            ),
            # stats.build_stats; stats_tracking.refresh_articles_snapshot, from daily_cycle_handler.py
            # and the admin API's publish, unpublish and moderation routes.
            Step(
                "The Articles snapshot",
                "The Articles section is built from every article at the model registry's prices and "
                "stored as one row. It is rebuilt when an article is drafted or rewritten, and when one "
                "is published, approved, rejected or taken down.",
                ("table:stats-history", "table:articles", "table:models"),
            ),
            # public_api_handler._stats (_STATS_CACHE_SECONDS); renderStats in frontend/app.js.
            Step(
                "The Stats page",
                "GET /stats reads three rows, the Articles snapshot, this week's and the all-time one, "
                "and may be cached for 5 minutes. The page draws Total Stats, Weekly Stats, Articles "
                "and the bear's gear, and adds no money up itself.",
                ("function:public-api", "bucket:site"),
            ),
        ),
        aliases=(
            "stats", "stats page", "statistics", "costs", "cost", "spend", "spending", "ai spend",
            "article cost", "article costs", "articles cost", "cost tracking", "aws bill", "bill",
            "cost explorer", "cost poll", "daily cost poll", "rollover", "weekly rollover",
            "stats rollover", "lineage",
        ),
    ),
    # docs/architecture/security-incidents.md's steps, as the assistant tells them: its eight, with
    # grouping and recording made one.
    Feature(
        key="security-incidents",
        title="Security incidents (from a blocked request to an incident you work through)",
        said="security incidents",
        summary="How what the firewalls block, an attack-shaped comment and failed sign-ins become "
        "security incidents: grouped by client, rule and 15 minutes, graded, kept with a keyed hash "
        "in place of an address, and emailed to you when high.",
        steps=(
            # aws_wafv2_web_acl.public_api and .admin, with their logging configurations
            # (infra/environments/*/main.tf).
            Step(
                "Blocked by a firewall",
                "The public API's regional firewall blocks a client over a rate limit, or a request "
                "AWS's common rule set catches; the admin API's blocks every address not on the "
                "allowlist. Each logs the block as one record.",
                (
                    "firewall:public-api-acl",
                    "firewall:admin-acl",
                    "log_group:waf-public-api",
                    "log_group:waf-admin",
                ),
            ),
            # aws_cloudwatch_log_subscription_filter.security_events and
            # .security_events_admin_api_errors.
            Step(
                "The block reaches the Lambda",
                "A log subscription on each regional firewall's log sends only its BLOCK records to "
                "the security-events Lambda, and another sends the admin API access log's 4xx lines.",
                ("function:security-events", "log_group:admin-api-access"),
            ),
            # security_events_handler.py (_window_key); common/security_events.py (record_incident,
            # classify_rule, client_hash, PLAYBOOK); dynamo.upsert_security_incident.
            Step(
                "Grouped into incidents",
                "Blocks are grouped by rule, client and 15-minute window into one incident each, with a "
                "category, a severity and fixed next steps; it turns high once it reaches its "
                "category's count. The client is kept only as a keyed hash, never its address.",
                ("table:security-events", "table:model-config"),
            ),
            # public_api_handler._submit_feedback; sign_in_events_handler.py;
            # security_events_handler._record_access_log; record_trend and open_manual_incident.
            Step(
                "The other sources",
                "The public API records an attack-shaped comment and counts dropped comments as a daily "
                "trend; repeated failed sign-ins to this assistant and a lockout are recorded too; the "
                "admin API's errors are an hourly trend; and you can open one by hand.",
                ("function:public-api", "function:sign-in-events", "function:admin-api"),
            ),
            # dynamo.claim_security_alert; the security_high_severity metric filter and alarm in
            # infra/modules/observability/main.tf.
            Step(
                "High severity emails you",
                "The first time an incident is high, the Lambda that wrote it logs one SECURITY_ALERT "
                "line; a metric filter counts it, and the high-severity alarm emails the alert address. "
                "Lower severities are only recorded.",
                ("topic:alerts", "CloudWatch metric filter and alarm (security-high-severity)"),
            ),
            # admin_api_handler._list_security_incidents and _set_security_incident_status;
            # security_events.change_status; security_events in ops_mcp/account.py.
            Step(
                "You work through it",
                "With the Admin CLI you list the open incidents, acknowledge one when you start looking "
                "and resolve it when it is dealt with. This assistant's security_events reads the open "
                "ones.",
                ("function:admin-api", "function:ops-mcp"),
            ),
            # security_events.RETENTION_DAYS; the table's TTL on expires_at (infra/modules/app-data).
            Step(
                "It expires",
                "DynamoDB's TTL removes an incident 120 days after it was last seen.",
                ("table:security-events",),
            ),
        ),
        aliases=(
            "security", "incidents", "incident", "security events", "firewall", "firewalls", "waf",
            "attacks", "attack", "blocked requests", "what was blocked", "failed sign ins",
            "sign in failures", "lockout", "lockouts", "high severity", "security alert",
            "security alarm",
        ),
    ),
    # docs/architecture/reviewer-bear.md's steps, as the assistant tells them, in the page's order:
    # task 5 is planned with task 2, from the same held articles, so it comes after it.
    Feature(
        key="reviewer-bear",
        title="The reviewer bear (from a waiting article to Professor Grizzlewick's decision)",
        said="the reviewer bear",
        summary="How Professor Grizzlewick decides what the admins have left waiting: woken every "
        "second day, he plans with no model call, works through five tasks a few at a time, acts "
        "through the admin API's own paths and reports on the Musings page.",
        steps=(
            # aws_scheduler_schedule.reviewer (cron(0 5 1/2 * ? *)); reviewer_handler.handle.
            Step(
                "Woken",
                "His schedule invokes his Lambda at 05:00 Sydney time on odd days of the month. With "
                "his enabled setting off, the run stops there.",
                ("function:reviewer", "EventBridge Scheduler"),
            ),
            # common/reviewer.py start and plan; the reviewer-state row of model-config (STATE_ID).
            Step(
                "The plan",
                "He reads the tables and queues the run's tasks, with no model call, and saves the "
                "queue. A run still under way is left alone.",
                ("table:model-config", "table:moderation-queue", "table:articles"),
            ),
            # common/reviewer.tick; reviewer_handler._advance and schedule_tick.
            Step(
                "The ticks",
                "Each tick does the next few tasks, two by default, then makes a one-shot schedule for "
                "the next tick, five minutes later by default. The first tick runs straight after the "
                "plan.",
                ("function:reviewer", "One-shot schedules (reviewer-tick-<run id>-<n>)"),
            ),
            # common/reviewer._content; content_checks in ops_mcp/content.py.
            Step(
                "Content findings",
                "A broken title or body that this assistant's content checks found is sent for a "
                "rewrite with a fixed instruction, and a blank musing is written again.",
                ("table:articles", "table:musings"),
            ),
            # common/reviewer._held, held_issues, judge_majors, judge_published, may_approve; a
            # rewrite runs in the daily-cycle Lambda.
            Step(
                "Held articles",
                "An article held two days or more is judged by the topic's model: a real problem sends "
                "it for a rewrite, or after three rewrites rejects it; with none, he reads it and "
                "approves it if he may, flagging a financial one for you.",
                (
                    "table:moderation-queue",
                    "Amazon Bedrock (the topic's model)",
                    "function:daily-cycle",
                ),
            ),
            # common/reviewer._defang, judge_defang, cut_sentences; reviewer_replace_body in
            # admin_api_handler.py.
            Step(
                "The de-fanged publish",
                "An article held ten days or more is published with the sentences that carry its real "
                "problems deleted, approved as it is, or rejected. Its original body is kept beside it "
                "in the content bucket.",
                ("bucket:content", "bucket:site", "Amazon Bedrock (the topic's model)"),
            ),
            # common/reviewer._published and judge_published.
            Step(
                "Recently published",
                "He reads a few of the newest published articles he has not reviewed: a fine one is "
                "tagged, and one that is not is sent for a rewrite.",
                ("table:articles", "Amazon Bedrock (the topic's model)"),
            ),
            # common/reviewer._flag; reviewer_hold in admin_api_handler.py;
            # dynamo.list_reviewer_flagged_articles.
            Step(
                "His flags",
                "He watches each financial article he approved. If a reader has downvoted it with a "
                "comment since, he takes it down and puts it back in the inbox for a person.",
                ("table:feedback", "table:articles", "table:moderation-queue"),
            ),
            # reviewer_handler._report and _converse; common/musings.py.
            Step(
                "His report",
                "When the queue is empty he posts what he did, in counts, on the Musings page. "
                "BloggerBear may reply, and he may answer.",
                ("table:musings",),
            ),
        ),
        aliases=(
            "reviewer", "reviewer bear", "reviewer bears", "ai reviewer", "professor",
            "professor grizzlewick", "grizzlewick", "second bear", "bear review", "bear reviews",
            "held articles", "review inbox", "de fanged publish", "defang", "reviewer musings",
            "reviewer report", "reviewer flags",
        ),
    ),
    # docs/architecture/model-catalog.md's steps, as the assistant tells them: its thirteen, with
    # catching up made part of what invokes it, discover's listing and probe one, looking a price up
    # and applying it one, and the reviews and the run's close one.
    Feature(
        key="model-catalog",
        title="The model catalog (from a newly listed model to a checked price)",
        said="the model catalog",
        summary="How the model registry keeps itself current: one Lambda finds the models the account "
        "can call, looks their prices up again, puts a doubtful price back, keeps the registry "
        "under its cap and keeps the topics' rotations working.",
        steps=(
            # model_catalog_handler.py; model_catalog.handle and settle_overdue (OVERDUE_AFTER); the
            # aws_scheduler_schedule.model_catalog_* in main.tf; _start_model_catalog_job in
            # admin_api_handler.py.
            Step(
                "What invokes it",
                "Fixed schedules start discover and reprice every second day and the rotation cycle on "
                "Fridays, and the admin API starts discover or reprice on demand. Before any job, a "
                "pricing step more than 30 minutes late is run on the spot.",
                ("function:model-catalog", "function:admin-api", "EventBridge Scheduler"),
            ),
            # model_catalog.account_models, discover and probe_access (DENIED_RECHECK);
            # dynamo.add_model_if_missing.
            Step(
                "Discover",
                "Bedrock lists every active model and inference profile, and each new text model is "
                "called once for one token. One the account cannot call is not added, nor tried again "
                "for 30 days; up to 5 others are added, switched off and unpriced.",
                ("table:models", "Amazon Bedrock (the model listings, a one-token probe)"),
            ),
            # model_fleet.prune, prune_order and protected_models; dynamo.archive_models.
            Step(
                "Pruning when full",
                "Once the registry is at its cap, 50 models by default, discover removes models instead "
                "of adding them, never one in use, and archives each one's price first.",
                ("table:models", "table:model-config"),
            ),
            # model_catalog.queue_order, reprice, advance and schedule_tick (CHECKS_PER_TICK,
            # TICK_MINUTES).
            Step(
                "The reprice queue",
                "Reprice claims the one run slot and queues up to 50 models, the one looked up longest "
                "ago first, then checks 5 at a time, 5 minutes apart, on one-shot schedules.",
                ("function:model-catalog", "table:model-config"),
            ),
            # model_catalog.PriceList, WebPrices, PriceLookup and price_models;
            # web_search.AgentCoreProvider.
            Step(
                "Looking a price up",
                "The AWS Price List first, with no model call, then an AgentCore web search read by a "
                "model, skipped when the results have not changed. A different price is written at "
                "once, and is on probation.",
                ("AWS Price List API (us-east-1)", "Bedrock AgentCore web search", "table:models"),
            ),
            # model_catalog.schedule_reviews, review, _check, _revert, _record_failure, _finalise and
            # write_snapshot.
            Step(
                "The reviews",
                "One and two hours after the queue is done, each changed price is looked up again, and "
                "one that fails goes back to its old price, with a failed-executions row. When the run "
                "closes, the old prices of those that kept their new one go to the content bucket.",
                ("table:failed-executions", "bucket:content"),
            ),
            # model_fleet.cycle and rotation_picks.
            Step(
                "The weekly cycle",
                "On Fridays every topic with a rotation list loses the models that are switched off or "
                "that the account cannot call, and gains one new model while it has room.",
                ("table:topics", "table:models"),
            ),
            # _put_model in admin_api_handler.py; model_fleet.deactivated; _route_around in
            # common/model_routing.py.
            Step(
                "Disabling a model in use",
                "Disabling a model takes it out of every topic and the shared rotation pool at once, "
                "and a call whose topic still names a switched-off model is routed to another.",
                ("function:admin-api", "table:topics", "common/model_routing.py"),
            ),
            # costing.pricing_for and calculate_lineage_cost_aud; stats.build_stats.
            Step(
                "Where a price is used",
                "An article's cost is worked out once, when it is written, so a new price reaches only "
                "later articles; the Stats page prices every call again each time it is rebuilt.",
                ("common/costing.py", "table:articles", "table:stats-history"),
            ),
        ),
        aliases=(
            "model catalog", "model catalogue", "models", "model registry", "registry",
            "model prices", "model pricing", "prices", "price", "pricing", "reprice", "repricing",
            "price check", "price checks", "price review", "price reviews", "reprice review",
            "reprice reviews", "changed price", "changed prices", "discover", "discovery",
            "model discovery", "new models", "model rotation", "rotation", "rotations",
            "rotation cycle", "weekly cycle", "pruning", "prune", "model fleet", "fleet",
        ),
    ),
)
FEATURE_KEYS = tuple(feature.key for feature in FEATURES)


def resolve_feature(raw) -> str | None:
    """A feature's key from the operator's words for it ("article research", "how articles are
    written", "the research pipeline"); None when it names none."""
    if not isinstance(raw, str):
        return None
    words = " ".join(re.sub(r"[^a-z0-9 ]", " ", raw.lower().replace("-", " ")).split())
    words = " ".join(w for w in words.split() if w not in ("the", "feature", "please", "about"))
    if not words:
        return None
    for feature in FEATURES:
        names = {feature.key.replace("-", " "), feature.said, *feature.aliases}
        if words in names:
            return feature.key
    # Otherwise every alias is scored, each as whole words in what was said ("the" is gone from
    # the words, so no alias holds it), and the feature with the longest one wins; a tie goes to
    # the earlier feature. "vision research pipeline" so goes to the vision pipeline, though it
    # holds article research's "research pipeline".
    best, length = None, 0
    padded = f" {words} "
    for feature in FEATURES:
        for alias in (feature.said, *feature.aliases):
            if f" {alias} " in padded and len(alias) > length:
                best, length = feature.key, len(alias)
    return best


# --- The environment ------------------------------------------------------------------------------


def environment() -> str | None:
    """This assistant's environment, or None when the function was not told (or was told
    something that is not a name)."""
    name = os.environ.get(ENVIRONMENT_ENV, "")
    return name if _ENVIRONMENT_NAME.fullmatch(name) else None


_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-\d+")


def valid_region(value) -> str | None:
    """`value` when it is shaped like an AWS Region (us-west-2), else None."""
    return value if isinstance(value, str) and _REGION.fullmatch(value) else None


def region() -> str | None:
    """The region this runs in, which is where everything but CloudFront lives."""
    return valid_region(os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "")


def fill(template: str, env: str | None) -> str:
    """A name template for this environment: "bloggerbear-{env}-topics" -> "bloggerbear-dev-topics".
    With no environment known, the placeholder stays readable: "bloggerbear-<environment>-topics"."""
    return template.replace(ENV, env or "<environment>")


def component_region(component: Component) -> str | None:
    """The Region a component lives in: for one with `region_env`, the Region that variable holds
    (None when it holds none: the component is not deployed here); for any other, region()."""
    if component.region_env is not None:
        return valid_region(os.environ.get(component.region_env, ""))
    return region()


def exists_in(component: Component, env: str | None) -> bool:
    return (not component.only_in or env in component.only_in) and (
        component.region_env is None or component_region(component) is not None
    )


def _absent(component: Component, env: str | None) -> str:
    """Why a component is not in this environment, in a few words for a table."""
    if component.only_in and env not in component.only_in:
        return f"only in {', '.join(component.only_in)}"
    return "not deployed here"


# --- Finding a component by whatever name the operator pasted -------------------------------------

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[\s_./:]+")
_ARN_PREFIXES = (
    # arn:aws:dynamodb:<region>:<account>:table/<name>[/index/<index>]
    (re.compile(r"^arn:aws[a-z-]*:dynamodb:[^:]*:[^:]*:table/([^/]+)"), "table"),
    # arn:aws:lambda:<region>:<account>:function:<name>
    (re.compile(r"^arn:aws[a-z-]*:lambda:[^:]*:[^:]*:function:([^:]+)"), "function"),
    # arn:aws:logs:<region>:<account>:log-group:<name>[:*]
    (re.compile(r"^arn:aws[a-z-]*:logs:[^:]*:[^:]*:log-group:([^:]+)"), None),
    # arn:aws:states:<region>:<account>:stateMachine:<name>
    (re.compile(r"^arn:aws[a-z-]*:states:[^:]*:[^:]*:stateMachine:([^:]+)"), "state_machine"),
    # arn:aws:sqs / sns:<region>:<account>:<name>
    (re.compile(r"^arn:aws[a-z-]*:sqs:[^:]*:[^:]*:([^:]+)"), "queue"),
    (re.compile(r"^arn:aws[a-z-]*:sns:[^:]*:[^:]*:([^:]+)"), "topic"),
    (re.compile(r"^arn:aws[a-z-]*:s3:::([^/]+)"), "bucket"),
)
# A log group's prefix says which kind of thing its name is about.
_LOG_PREFIXES = (("/aws/lambda/", "lambda"), ("/aws/apigateway/", "access"), ("aws-waf-logs-", "waf"))


def _slug(text: str) -> str:
    """Lowercase words joined by "-": "CandidateIdeas", "candidate_ideas", "CANDIDATE_IDEAS_TABLE"
    and "candidate ideas" all become "candidate-ideas" (a trailing "table" is dropped)."""
    text = _CAMEL.sub("-", text.strip())
    slug = "-".join(part for part in _SEPARATORS.sub("-", text.lower()).split("-") if part)
    return slug.removesuffix("-table")


@dataclass(frozen=True)
class Resolved:
    """What a pasted name was taken to mean."""

    matches: tuple[Component, ...]
    asked_env: str | None  # the environment the name was for, normalized; None if it named none
    env_word: str | None  # the word in the name that looked like an environment, as written
    suggestions: tuple[str, ...] = ()


# The prefix in any case, as a word of its own: not the same letters inside a longer word.
_PREFIX_ANY_CASE = re.compile(rf"(?<![A-Za-z0-9])(?i:{re.escape(NAME_PREFIX)})(?![a-z0-9])")


def _strip(raw: str) -> tuple[str, str | None]:
    """The name inside an ARN or a log group's path, and a hint: the kind of resource an ARN is
    for, or the kind of log group ("lambda", "access", "waf")."""
    text = raw.strip().strip("'\"`")[:_NAME_MAX_CHARS]
    arn_kind = None
    for pattern, kind in _ARN_PREFIXES:
        found = pattern.match(text)
        if found:
            text, arn_kind = found.group(1), kind
            break
    # The prefix is itself, whatever its case: "BloggerBear" is one word, and the CamelCase split
    # must not make it two.
    text = _PREFIX_ANY_CASE.sub(NAME_PREFIX, text)
    for prefix, hint in _LOG_PREFIXES:
        if text.lower().startswith(prefix):
            return text[len(prefix) :], hint
    return text, arn_kind


def _index() -> dict[str, list[Component]]:
    """slug -> the components it names. A name can mean several things: bloggerbear-dev-daily-cycle
    is a function and a state machine, and the function's log group is named after it."""
    index: dict[str, list[Component]] = {}
    for component in CATALOGUE:
        bare = component.name.replace(f"{PREFIX}{ENV}-", "")
        words = {component.key, *component.aliases}
        if "<" not in bare and "/" not in bare and not bare.startswith("aws-waf-logs-"):
            words.add(bare)
        for word in words:
            entries = index.setdefault(_slug(word), [])
            if component not in entries:
                entries.append(component)
    return index


_INDEX = _index()


def _key(words: list[str], hint: str | None) -> str:
    """The catalogue key for what is left of a name, given the kind of log group it came from: a
    WAF log group's components are keyed "waf-<acl>", an access log's "<api>-access"."""
    key = "-".join(words)
    if hint == "waf" and key:
        return f"waf-{key}"
    return key


def resolve(raw: str, kind: str | None = None) -> Resolved:
    """Take whatever the operator pasted (a name from either environment, an ARN, a log group,
    an environment variable name, a CamelCase name from the docs) to the catalogue's components.

    The environment in the name is noted and taken out: the answer is always about this
    assistant's environment. An environment word that is neither known environment is noted too
    (`asked_env` is then the word itself), so the caller can refuse to read data for it."""
    text, hint = _strip(raw) if isinstance(raw, str) else ("", None)
    words = _slug(text).split("-") if text else []
    words = [word for word in words if word]
    prefixed = words[: len(_PREFIX_WORDS)] == _PREFIX_WORDS
    if prefixed:
        words = words[len(_PREFIX_WORDS) :]
    asked_env = env_word = None
    if words and _key(words, hint) not in _INDEX:
        if words[0] in ENVIRONMENT_ALIASES:
            env_word, asked_env = words[0], ENVIRONMENT_ALIASES[words[0]]
            words = words[1:]
        elif prefixed and len(words) > 1 and _key(words[1:], hint) in _INDEX:
            # Something in the environment's place that is not an environment ("staging"). Only
            # after the prefix: without it, a typo ("candidte-ideas") is a typo.
            env_word = asked_env = words[0]
            words = words[1:]
    key = _key(words, hint)
    matches = list(_INDEX.get(key, ()))
    # A Lambda's log group is about the function, not the state machine that shares its name.
    wanted = kind or ({"lambda": "function"}.get(hint or "") if hint not in KINDS else hint)
    if wanted:
        matches = [component for component in matches if component.kind == wanted] or (
            matches if not kind else []
        )
    suggestions: tuple[str, ...] = ()
    if not matches and key:
        pool = sorted(k for k, found in _INDEX.items() if not kind or any(c.kind == kind for c in found))
        suggestions = tuple(difflib.get_close_matches(key, pool, n=3, cutoff=0.6))
    return Resolved(tuple(matches), asked_env, env_word, suggestions)


# --- Console links ----------------------------------------------------------------------------------


def console_url(kind: str, name: str, where: str | None = None) -> str | None:
    """A link to open the thing in the AWS console, for what has a link that needs no account id:
    a dashboard, a log group, a Lambda function. None for anything else."""
    where = where or region()
    if not where or "<" in name:
        return None
    base = f"https://{where}.console.aws.amazon.com"
    if kind == "dashboard":
        return f"{base}/cloudwatch/home?region={where}#dashboards:name={quote(name, safe='')}"
    if kind == "log_group":
        # The console's own encoding: the name percent-encoded twice, then "%" written as "$".
        encoded = quote(quote(name, safe=""), safe="").replace("%", "$")
        return f"{base}/cloudwatch/home?region={where}#logsV2:log-groups/log-group/{encoded}"
    if kind == "function":
        return f"{base}/lambda/home?region={where}#/functions/{quote(name, safe='')}?tab=monitoring"
    return None


def log_group_region(name: str) -> str | None:
    """Where a log group lives: the shared CloudFront firewall's is in us-east-1; one of a component
    with `region_env` (the vision worker's) in that component's Region; everything else in this
    region."""
    # A CLOUDFRONT-scope web ACL, with its log group, can only be created in us-east-1.
    if name == SHARED_WAF_LOG_GROUP:
        return "us-east-1"
    env = environment()
    for component in CATALOGUE:
        if component.region_env and name in (fill(group, env) for group in component.log_groups):
            return component_region(component)
    return region()


# --- The tool ---------------------------------------------------------------------------------------


def _environment_note(resolved: Resolved, env: str | None) -> tuple[str, bool]:
    """What to say about the environment the name was for, and whether rows may be read for it."""
    if env is None:
        return (
            "This assistant hasn't been told which environment it is for, so names are shown "
            "with a placeholder.",
            False,
        )
    if resolved.asked_env is None or resolved.asked_env == env:
        return ("", True)
    if resolved.asked_env in ENVIRONMENTS:
        return (
            f"That name is {resolved.asked_env}'s. I'm the {env} assistant, so this is {env}'s "
            "version of it.",
            True,
        )
    return (
        f"That name isn't for either environment I know, so this describes {env}'s version, "
        "and I won't read data for it.",
        False,
    )


def describe(component: Component, env: str | None) -> dict:
    """One component as the page and the agent are sent it."""
    name = fill(component.name, env)
    rows: list[list[str]] = [
        ["Name", name],
        ["Kind", KIND_LABELS[component.kind]],
        ["What it's for", component.purpose],
    ]
    if component.keys:
        rows.append(["Key", " + ".join(component.keys)])
    if component.indexes:
        rows.append(["Indexes", ", ".join(component.indexes)])
    if component.ttl:
        rows.append(["TTL attribute", component.ttl])
    rows.extend([label, fill(text, env)] for label, text in component.details)
    log_groups = [fill(group, env) for group in component.log_groups]
    dashboards = [fill(board, env) for board in component.dashboards]
    alarms = [fill(alarm, env) for alarm in component.alarms]
    if log_groups:
        rows.append(["Log groups", ", ".join(log_groups)])
    if dashboards:
        rows.append(["Dashboards", ", ".join(dashboards)])
    if alarms:
        rows.append(["Alarms", ", ".join(alarms)])
    if component.kind == "table":
        rows.append(
            [
                "The assistant can read it",
                "yes: its tools read it, and table_sample shows its newest row"
                if component.assistant_reads
                else "with table_sample, when it carries the default tags and this environment's tag",
            ]
        )
    present = exists_in(component, env)
    if not present and component.region_env and _absent(component, env) == "not deployed here":
        rows.append(["In this environment", "no: only where vision is on (VISION_ENABLED)"])
    elif not present:
        rows.append(["In this environment", f"no: {_absent(component, env)}"])
    url = console_url(component.kind, name, component_region(component)) if present else None
    if url:
        rows.append(["Open", url])
    return {
        "kind": component.kind,
        "key": component.key,
        "name": name,
        "purpose": component.purpose,
        "exists_here": present,
        "assistant_reads": component.assistant_reads,
        "log_groups": log_groups,
        "dashboards": dashboards,
        "alarms": alarms,
        "console_url": url,
        "table": {
            "title": f"{name}: {KIND_LABELS[component.kind]}",
            "columns": ["Detail", "Value"],
            "rows": rows,
        },
    }


def _layers_overview(env: str | None) -> dict:
    """Asked how the project is built, with nothing named: the layers, what each does and what is
    in it, and the question of which to go into. Short enough to listen to."""
    said = [layer.said for layer in LAYERS]
    listed = ", ".join(said[:-1]) + f" and {said[-1]}"
    named_features = [each.said for each in FEATURES]
    features = (
        ", ".join(named_features[:-1]) + f" and {named_features[-1]}"
        if len(named_features) > 1
        else named_features[0]
    )
    rows = [
        [layer.title, layer.summary, ", ".join(name for name, _ in layer.services)] for layer in LAYERS
    ]
    # Every feature is named, and the words are counted (test_ops_mcp_architecture.py): with seven,
    # "step by step across the layers" no longer fits in what can be listened to.
    return {
        "spoken": (
            f"BloggerBear is built in {len(LAYERS)} layers: {listed}. They're on screen with what "
            "each does. Which would you like to hear about, or everything in detail? I can also "
            f"walk through {features}."
        ),
        "findings": [],
        "environment": env,
        "layers": [{"key": layer.key, "title": layer.title, "summary": layer.summary} for layer in LAYERS],
        "features": [{"key": each.key, "title": each.title} for each in FEATURES],
        "ask": "Which layer, or everything in detail?",
        "table": {
            "title": f"BloggerBear {env or ''} architecture: the layers".replace("  ", " "),
            "columns": ["Layer", "What it does", "What it is made of"],
            "rows": rows,
        },
    }


def _layer_detail(layer: Layer, env: str | None) -> dict:
    """One layer: what it does, the services in it, then our own resources in it by name."""
    components = [c for c in CATALOGUE if c.kind in layer.kinds]
    rows = [[name, "Service", what] for name, what in layer.services]
    rows += [
        [
            fill(c.name, env),
            KIND_LABELS[c.kind],
            c.purpose if exists_in(c, env) else f"({_absent(c, env)}) {c.purpose}",
        ]
        for c in components
    ][: OVERVIEW_MAX_ROWS - len(rows)]
    names = [name for name, _ in layer.services]
    made_of = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    spoken = [f"{layer.title.split(' (')[0]}. {layer.summary}", f"It is made of {made_of}."]
    if components:
        plural = "resource" if len(components) == 1 else "resources"
        spoken.append(f"Our {len(components)} {plural} in it are on screen.")
    else:
        spoken.append("The details are on screen.")
    spoken.append("Ask for another layer, or a resource by name.")
    return {
        "spoken": " ".join(spoken),
        "findings": [],
        "environment": env,
        "layer": layer.key,
        "services": [{"name": name, "what": what} for name, what in layer.services],
        "components": [{"kind": c.kind, "key": c.key, "name": fill(c.name, env)} for c in components],
        "table": {
            "title": f"BloggerBear {env or ''} architecture: {layer.title}".replace("  ", " "),
            "columns": ["Part", "Kind", "What it's for"],
            "rows": rows,
        },
    }


def _part_row(part: str, env: str | None) -> tuple[str, str]:
    """A step's part as (name, kind): a catalogue resource by its name here, else the words."""
    kind, _, key = part.partition(":")
    if kind in KINDS and key:
        component = by_key(kind, key)
        return fill(component.name, env), KIND_LABELS[kind]
    return part, "Service or code"


def _feature_detail(feature: Feature, env: str | None) -> dict:
    """One feature: what it does, then its steps in the order the work flows, each with the parts
    it uses. Spoken: the summary and the steps' names, short enough to listen to."""
    rows = []
    for number, step in enumerate(feature.steps, start=1):
        parts = "; ".join(
            name if label == "Service or code" else f"{name} ({label})"
            for name, label in (_part_row(part, env) for part in step.parts)
        )
        rows.append([f"{number}. {step.title}", step.what, parts])
    titles = [_lower_first(step.title.split(":")[0]) for step in feature.steps]
    listed = ", ".join(titles[:-1]) + f" and {titles[-1]}"
    components = [
        {"kind": part.partition(":")[0], "key": part.partition(":")[2], "name": _part_row(part, env)[0]}
        for part in dict.fromkeys(p for step in feature.steps for p in step.parts)
        if part.partition(":")[0] in KINDS
    ]
    return {
        "spoken": (
            f"{feature.title.split(' (')[0]}. {feature.summary} It goes in {len(feature.steps)} "
            f"steps: {listed}. They're on screen. Ask about any part by name, or for a layer."
        ),
        "findings": [],
        "environment": env,
        "feature": feature.key,
        "steps": [{"title": s.title, "what": s.what, "parts": list(s.parts)} for s in feature.steps],
        "components": components,
        "table": {
            "title": f"BloggerBear {env or ''} architecture: {feature.title}".replace("  ", " "),
            "columns": ["Step", "What happens", "What it uses"],
            "rows": rows,
        },
    }


def _overview(kind: str | None, env: str | None) -> dict:
    components = [c for c in CATALOGUE if kind is None or c.kind == kind]
    rows = [
        [
            fill(c.name, env),
            KIND_LABELS[c.kind],
            c.purpose if exists_in(c, env) else f"({_absent(c, env)}) {c.purpose}",
        ]
        for c in components
    ][:OVERVIEW_MAX_ROWS]
    if kind == "function":
        # "List the Lambdas and what they do": when each runs is what the operator asks next.
        runs = {c.key: next((text for label, text in c.details if label == "Runs"), "") for c in components}
        rows = [[row[0], row[2], runs[c.key]] for row, c in zip(rows, components)]
    what = KIND_LABELS[kind] + "s" if kind else "resources"
    counted = f"{len(components)} {what.lower() if kind else what}"
    return {
        "spoken": f"There are {counted} in the catalogue for {env or 'this environment'}. They're on screen.",
        "findings": [],
        "environment": env,
        "components": [{"kind": c.kind, "key": c.key, "name": fill(c.name, env)} for c in components],
        "table": {
            "title": f"BloggerBear {env or ''} architecture: {what}".replace("  ", " "),
            "columns": (
                ["Name", "What it's for", "When it runs"]
                if kind == "function"
                else ["Name", "Kind", "What it's for"]
            ),
            "rows": rows,
        },
    }


def architecture(
    name: str | None = None,
    kind: str | None = None,
    layer: str | None = None,
    feature: str | None = None,
) -> dict:
    """What a resource is for, in this environment. With `name`: the matching resources, each
    described (a name can mean several, like a function and its state machine). With only
    `kind`: every resource of that kind. With `layer`: that layer of the architecture (its
    services and our resources in it), or every resource in one table for "everything". With
    `feature`: one feature, step by step across the layers (FEATURES: article research, the
    reviewer bear, ...). With nothing: the layers, and the question of which to go into."""
    env = environment()
    named = isinstance(name, str) and bool(name.strip())
    if feature is not None and not named and kind is None:
        chosen = resolve_feature(feature)
        if chosen is None:
            said = [each.said for each in FEATURES]
            return {
                "spoken": "I don't know that feature. The features I can walk through are "
                + (", ".join(said[:-1]) + f" and {said[-1]}" if len(said) > 1 else said[0])
                + "; or ask for a layer.",
                "findings": [],
                "environment": env,
                "features": [{"key": each.key, "title": each.title} for each in FEATURES],
            }
        return _feature_detail(next(each for each in FEATURES if each.key == chosen), env)
    if layer is not None and not named and kind is None:
        chosen = resolve_layer(layer)
        if chosen is None:
            said = [each.said for each in LAYERS]
            return {
                "spoken": "I don't know that layer. The layers are "
                + ", ".join(said[:-1])
                + f" and {said[-1]}; or ask for everything.",
                "findings": [],
                "environment": env,
                "layers": [{"key": each.key, "title": each.title} for each in LAYERS],
            }
        if chosen == EVERYTHING:
            return _overview(None, env)
        return _layer_detail(next(each for each in LAYERS if each.key == chosen), env)
    if kind is not None and kind not in KINDS:
        return {
            "spoken": f"I don't know that kind. The kinds are: {', '.join(KINDS)}.",
            "findings": [],
            "kinds": list(KINDS),
        }
    if not named:
        return _overview(kind, env) if kind is not None else _layers_overview(env)

    resolved = resolve(name, kind)
    note, data_allowed = _environment_note(resolved, env)
    if not resolved.matches:
        hint = ""
        if resolved.suggestions:
            hint = " The closest I know: " + ", ".join(resolved.suggestions) + "."
        return {
            "spoken": f"I don't know a resource by that name in BloggerBear's architecture.{hint}",
            "findings": [],
            "environment": env,
            "suggestions": list(resolved.suggestions),
            "data_allowed": False,
        }

    described = [describe(component, env) for component in resolved.matches]
    first = described[0]
    spoken = [f"{first['name']} is the {KIND_LABELS[first['kind']]}: {_lower_first(first['purpose'])}"]
    if len(described) > 1:
        others = ", ".join(f"a {KIND_LABELS[d['kind']]}" for d in described[1:])
        spoken.append(f"The same name is also {others}; all are on screen.")
    if not first["exists_here"]:
        spoken.append(f"It isn't deployed in {env}.")
    if note:
        spoken.append(note)
    return {
        "spoken": " ".join(spoken),
        "findings": [],
        "environment": env,
        "asked_environment": resolved.asked_env,
        "rewritten": bool(env and resolved.asked_env in ENVIRONMENTS and resolved.asked_env != env),
        "data_allowed": data_allowed and bool(env),
        "matches": [{k: v for k, v in d.items() if k != "table"} for d in described],
        # policy.Ledger takes one table per result: the first match's. The rest are in `matches`.
        "table": first["table"],
    }


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def by_key(kind: str, key: str) -> Component:
    """A component the code names itself (runsheets.py): a missing one is a bug, so it raises."""
    for component in CATALOGUE:
        if component.kind == kind and component.key == key:
            return component
    raise KeyError(f"{kind}:{key}")


def as_mapping() -> Mapping[str, Component]:
    """Every component by "kind:key", for the tests."""
    return {f"{c.kind}:{c.key}": c for c in CATALOGUE}


def forgiving(raw, kind: str) -> tuple[Resolved, str | None, str | None]:
    """resolve(), forgiving a near miss the way topics are (topic_match.py): when nothing matches
    and exactly one name of `kind` is close, it is taken, and the sentence to say so comes back;
    when several are close, the "did you mean" question comes back instead. Names are the
    catalogue's own keys, never what was asked."""
    resolved = resolve(raw, kind=kind)
    if resolved.matches or not resolved.suggestions:
        return resolved, None, None
    keys = list(dict.fromkeys(resolved.suggestions))
    if len(keys) == 1:
        again = resolve(keys[0], kind=kind)
        if again.matches:
            taken = Resolved(again.matches, resolved.asked_env, resolved.env_word, ())
            return taken, f"I took that to mean {keys[0]}; tell me if you meant another.", None
    names = ", ".join(keys[:-1]) + " or " + keys[-1] if len(keys) > 1 else keys[0]
    label = KIND_LABELS.get(kind, kind).lower()
    return resolved, None, f"I don't know a {label} by that name. Did you mean {names}?"
