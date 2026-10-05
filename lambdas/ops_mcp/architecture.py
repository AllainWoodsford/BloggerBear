"""The assistant as an expert on the project's own architecture: what each AWS resource is for,
what it is called in this environment, and where to look when something goes wrong with it.

    architecture   one resource described (a table, a function, an API, a dashboard, a log group,
                   ...), or every resource of a kind, as a table on screen

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
    (for a table: its rows), which test_ops_mcp_architecture.py holds to the Terraform."""

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
        "daily cadence, model, review mode, editorial goals, and when it was last researched and "
        "last written about.",
        ("topic_id",),
        details=(
            (
                "Written by",
                "the Admin API (admin_cli topics ...); research-tick and daily-cycle "
                "stamp last_research_at and last_article_at",
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
            ("Written by", "research-tick, once per topic per research interval"),
            ("Read by", "daily-cycle, trending-digest, the public API, the Admin API"),
            ("Expires", "14 days after it was captured (TTL)"),
            ("Healthy looks like", "a new row per topic at about each research interval"),
        ),
        aliases=("finding", "research"),
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
                "daily-cycle and trending-digest; the Admin API on approve, reject, rewrite and unpublish",
            ),
            ("Read by", "the public API (home, topic pages, RSS), the Admin API, the assistant"),
            ("Bodies", "in the content bucket under articles/, not in the table"),
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
                "daily-cycle and trending-digest (held articles); the Admin API (approve, reject, rewrite)",
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
        "queue: the topic and the error.",
        ("failure_id",),
        ttl="expires_at",
        details=(
            ("Written by", "dlq-handler, one row per dead-letter message"),
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
            ("Read by", "the public API, the assistant's content checks"),
        ),
        aliases=("musing",),
        assistant_reads=True,
    ),
    _table(
        "models",
        "The registry of Bedrock models the pipeline may use, with their pricing, so a model "
        "can be added or switched without a Terraform apply.",
        ("model_id",),
        details=(
            ("Written by", "the Admin API (admin_cli models ...)"),
            ("Read by", "every function that calls Bedrock, the public API"),
        ),
        aliases=("model-registry",),
    ),
    _table(
        "model-config",
        'Settings rows: the default model ("default"), the pipeline\'s settings including the '
        'assistant_access switch ("pipeline"), feedback limits ("feedback"), and the '
        "feedback rate limiter's short-lived counters.",
        ("config_id",),
        ttl="expires_at",
        details=(
            (
                "Written by",
                "the Admin API (model-config, pipeline-config, feedback-config set); "
                "the public API (rate-limit counters)",
            ),
            ("Read by", "every pipeline function, the assistant (its access switch)"),
            ("Expires", "only the rate-limit counters, by TTL"),
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
        "per topic, on the topic's own EventBridge schedule (its research interval)",
        details=(
            ("Writes", "findings; topics.last_research_at"),
            ("Schedules", f"{PREFIX}{ENV}-<topic_id>-research-tick"),
        ),
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
                "candidate-ideas, articles, moderation-queue, the content bucket; topics.last_article_at",
            ),
            ("Schedules", f"{PREFIX}{ENV}-<topic_id>-daily-cycle"),
        ),
        alarms=(f"{PREFIX}{ENV}-daily-cycle-executions-failed", f"{PREFIX}{ENV}-pipeline-dlq-messages"),
    ),
    _lambda(
        "admin-api",
        "The Admin API behind admin_cli: topics, review, models, settings, rewrites.",
        "on each request to the Admin API (IAM-signed, behind an address allowlist)",
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
        "Turns each message on the pipeline's dead-letter queue into a failed-executions row.",
        f"on each message on {PREFIX}{ENV}-pipeline-dlq",
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
        "trending-digest",
        'Writes the cross-topic "trending everywhere" digest article from every topic\'s latest finding.',
        "daily at 07:00 UTC (EventBridge Scheduler)",
    ),
    _lambda(
        "musing-feedback",
        "Writes a musing reflecting on the last four days of reader feedback.",
        "every 4 days (EventBridge Scheduler)",
    ),
    _lambda(
        "security-events",
        "Turns the regional firewalls' block records into security incidents.",
        "on each batch of BLOCK records from the public and admin APIs' WAF logs (a log subscription)",
        alarms=(f"{PREFIX}{ENV}-security-high-severity",),
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
        purpose="Two schedules per topic, made by the Admin API when a topic is created or "
        "changed (not by Terraform): research-tick at the topic's interval, daily-cycle at its "
        "daily cadence and timezone.",
        details=(("Where", 'EventBridge Scheduler, schedule group "default"'),),
        aliases=("schedules", "scheduler", "eventbridge"),
    ),
    Component(
        kind="bucket",
        key="content",
        name=f"{PREFIX}{ENV}-content",
        purpose="Article bodies (articles/) and the raw source snapshots research works from.",
        details=(("Name", "unique across all of AWS, which is why each deployment has its own prefix"),),
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


# --- The environment ------------------------------------------------------------------------------


def environment() -> str | None:
    """This assistant's environment, or None when the function was not told (or was told
    something that is not a name)."""
    name = os.environ.get(ENVIRONMENT_ENV, "")
    return name if _ENVIRONMENT_NAME.fullmatch(name) else None


def region() -> str | None:
    """The region this runs in, which is where everything but CloudFront lives."""
    value = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    return value if re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d+", value) else None


def fill(template: str, env: str | None) -> str:
    """A name template for this environment: "bloggerbear-{env}-topics" -> "bloggerbear-dev-topics".
    With no environment known, the placeholder stays readable: "bloggerbear-<environment>-topics"."""
    return template.replace(ENV, env or "<environment>")


def exists_in(component: Component, env: str | None) -> bool:
    return not component.only_in or env in component.only_in


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
    """Where a log group lives: the shared CloudFront firewall's is in us-east-1, everything else
    in this region."""
    # A CLOUDFRONT-scope web ACL, with its log group, can only be created in us-east-1.
    return "us-east-1" if name == SHARED_WAF_LOG_GROUP else region()


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
    if not present:
        rows.append(["In this environment", f"no: only in {', '.join(component.only_in)}"])
    url = console_url(component.kind, name)
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


def _overview(kind: str | None, env: str | None) -> dict:
    components = [c for c in CATALOGUE if kind is None or c.kind == kind]
    rows = [
        [
            fill(c.name, env),
            KIND_LABELS[c.kind],
            c.purpose if exists_in(c, env) else f"(only in {', '.join(c.only_in)}) {c.purpose}",
        ]
        for c in components
    ][:OVERVIEW_MAX_ROWS]
    what = KIND_LABELS[kind] + "s" if kind else "resources"
    counted = f"{len(components)} {what.lower() if kind else what}"
    return {
        "spoken": f"There are {counted} in the catalogue for {env or 'this environment'}. They're on screen.",
        "findings": [],
        "environment": env,
        "components": [{"kind": c.kind, "key": c.key, "name": fill(c.name, env)} for c in components],
        "table": {
            "title": f"BloggerBear {env or ''} architecture: {what}".replace("  ", " "),
            "columns": ["Name", "Kind", "What it's for"],
            "rows": rows,
        },
    }


def architecture(name: str | None = None, kind: str | None = None) -> dict:
    """What a resource is for, in this environment. With `name`: the matching resources, each
    described (a name can mean several, like a function and its state machine). With only
    `kind`: every resource of that kind. With neither: everything."""
    env = environment()
    if kind is not None and kind not in KINDS:
        return {
            "spoken": f"I don't know that kind. The kinds are: {', '.join(KINDS)}.",
            "findings": [],
            "kinds": list(KINDS),
        }
    if not isinstance(name, str) or not name.strip():
        return _overview(kind, env)

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
