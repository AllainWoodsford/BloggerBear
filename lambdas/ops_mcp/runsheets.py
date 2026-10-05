"""Where to look when the assistant cannot look itself: `investigate`, a runsheet per symptom.

The assistant reads this environment's logs itself (log_review, api_errors), but not metrics or
dashboards, and the operator often wants to look for themselves: "how can I check these myself?".
It knows the architecture (architecture.py), so it answers like someone who knows the system: which
dashboards to open, which log groups to query and with what query, which AWS console pages show it,
and which of its own tools to ask first. That is a runsheet.

**Every word and every query is ours.** A runsheet is chosen by its id or by matching the
operator's words against fixed keywords; nothing they said is put into a step. Names come from the
catalogue, filled in with this environment's name, and the one value a caller may pass into a query
is an HTTP status, which must be a whole number from 100 to 599.

**What reaches the page.** The steps as a table, and each Logs Insights query as a `how_to` card
with a Copy button (the page already shows those; cli_guide.py makes the same kind). A query reads
a log and changes nothing. `how_to` is not in the suggestion catalogue, so memory.py never records
one, and server.py does not pass this tool through it.

**Per environment.** A step about something only one environment has (the edge dashboard is only
created in production) is replaced by what to do instead in the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ops_mcp.architecture import ENV, PREFIX, console_url, environment, fill, log_group_region

HOW_TO = "how_to"
QUERIES_MAX = 4
API_CHOICES = ("public", "admin", "assistant")

_ERRORS_QUERY = (
    "fields @timestamp, @log, @message\n"
    "| filter @message like /unhandled exception|Traceback|ERROR|Task timed out/\n"
    "| sort @timestamp desc\n"
    "| limit 50"
)
_DURATION_QUERY = (
    'filter @type = "REPORT"\n'
    "| stats count(*) as runs, avg(@duration) as avg_ms, max(@duration) as max_ms,\n"
    "  max(@maxMemoryUsed / 1000 / 1000) as max_memory_mb by bin(1h)\n"
    "| sort bin(1h) desc"
)
# The WAF log's own fields. The client address is left out on purpose: the question is which rule
# and which path, and an address is personal data the operator rarely needs to see.
_WAF_BLOCKED_QUERY = (
    'filter action = "BLOCK"\n'
    "| stats count(*) as blocked by terminatingRuleId, httpRequest.uri\n"
    "| sort blocked desc\n"
    "| limit 25"
)


def _access_query(status: int | None) -> str:
    """The access log's requests at a status (or every 4XX and 5XX), newest first. The fields are
    the ones the stage's access log writes (rest-api/main.tf): errorType says who answered (the
    Lambda, the firewall, throttling), and an integrationLatency of "-" means the request never
    reached the Lambda."""
    condition = f"status = {status}" if status is not None else "status >= 400"
    return (
        "fields @timestamp, status, httpMethod, resourcePath, errorType, wafStatus, integrationLatency\n"
        f"| filter {condition}\n"
        "| sort @timestamp desc\n"
        "| limit 50"
    )


def _access_summary_query(status: int | None) -> str:
    condition = f"status = {status}" if status is not None else "status >= 400"
    return (
        f"filter {condition}\n"
        "| stats count(*) as requests by status, httpMethod, resourcePath, errorType\n"
        "| sort requests desc\n"
        "| limit 25"
    )


@dataclass(frozen=True)
class Step:
    """One place to look. `where` is a name template ({env}); `kind` says what it is, which picks
    the console link (architecture.console_url). `query` is a Logs Insights query for a log group.
    `only_in` and `instead`: a step about something not every environment has, and what to do
    where it is missing."""

    where: str
    kind: str
    look_for: str
    query: str | None = None
    only_in: tuple[str, ...] = ()
    instead: str | None = None


@dataclass(frozen=True)
class Runsheet:
    id: str
    title: str
    words: tuple[str, ...]  # what the operator might say: matched whole, case-insensitively
    summary: str  # one sentence, spoken
    assistant_tools: tuple[str, ...]  # what the assistant can check itself, first
    steps: tuple[Step, ...] = field(default=())


def _api_steps(api: str, status: int | None) -> tuple[Step, ...]:
    """The steps for one REST API's errors, from the outside in: the firewall, API Gateway, the
    Lambda."""
    if api == "assistant":
        name, function, waf = f"{PREFIX}{ENV}-ops-mcp", None, None
        lambdas = (f"/aws/lambda/{PREFIX}{ENV}-ops-agent", f"/aws/lambda/{PREFIX}{ENV}-ops-mcp")
    else:
        name = f"{PREFIX}{ENV}-{api}-api"
        function = name
        waf = f"aws-waf-logs-{PREFIX}{ENV}-{'admin' if api == 'admin' else 'public-api'}"
        lambdas = (f"/aws/lambda/{name}",)
    access = f"/aws/apigateway/{name}-access"
    steps = [
        Step(
            f"API Gateway console > APIs > {name} > Dashboard",
            "console",
            "AWS's own graphs for the stage: Count, 4XXError, 5XXError, Latency and Integration "
            "latency. Shows when errors started and whether they are 4XX or 5XX; not which status.",
        ),
        Step(
            access,
            "log_group",
            "Which status, method and path, and errorType: who answered. WAF_FILTERED is the "
            "firewall, THROTTLED is the rate limit, an empty errorType with integrationLatency set "
            "is the Lambda's own answer (a 400 the handler chose is bad input, not a bug).",
            query=_access_query(status),
        ),
        Step(
            access,
            "log_group",
            "The same errors counted by route, to see whether one path accounts for most of them.",
            query=_access_summary_query(status),
        ),
    ]
    for group in lambdas:
        steps.append(
            Step(
                group,
                "log_group",
                "Tracebacks and timeouts at the same time as the errors. A 5XX should have one; a "
                "4XX the handler chose usually logs nothing.",
                query=_ERRORS_QUERY,
            )
        )
    if function:
        steps.append(
            Step(
                function,
                "function",
                "Lambda console > Monitor: invocations, errors, duration and throttles, with a "
                "link to its recent log streams.",
            )
        )
    if waf:
        steps.append(
            Step(
                waf,
                "log_group",
                "When the access log says wafStatus 403 / WAF_FILTERED: which rule blocked it and "
                "on which path.",
                query=_WAF_BLOCKED_QUERY,
            )
        )
    return tuple(steps)


_EDGE = Step(
    f"{PREFIX}{ENV}-edge",
    "dashboard",
    "API Gateway half: requests and 4XX/5XX per API, responses by status, errors by route with "
    "errorType, 429s; then the firewall half.",
    only_in=("production",),
    instead=(
        "The edge dashboard is not created in dev (each dashboard past the account's first three "
        "costs US$3 a month). Use the API Gateway console and access log steps below, or set "
        "edge_dashboard_enabled = true in infra/environments/dev/main.tf for a while."
    ),
)
_PIPELINE = Step(
    f"{PREFIX}{ENV}-pipeline",
    "dashboard",
    "Per Lambda runs, errors, throttles and duration; the daily cycle's failed runs and the "
    "dead-letter queue; the Recent errors table at the bottom.",
)
_RUNS = Step(
    f"{PREFIX}{ENV}-lambda-runs",
    "dashboard",
    "How many times each Lambda ran over the span picked, and runs per day: a gap is a schedule "
    "that did not fire.",
)
_ALARMS = Step(
    f"CloudWatch console > Alarms > filter {PREFIX}{ENV}-",
    "console",
    "Which alarms are firing or fired recently, and their history.",
)


def _runsheets(api: str | None, status: int | None) -> tuple[Runsheet, ...]:
    apis = (api,) if api else ("public", "admin")
    return (
        Runsheet(
            "api-errors",
            "API errors (4XX and 5XX, such as 400, 403, 404, 429, 500, 502, 504)",
            (
                "400",
                "401",
                "403",
                "404",
                "429",
                "4xx",
                "500",
                "502",
                "503",
                "504",
                "5xx",
                "error",
                "errors",
                "status",
                "api",
                "gateway",
                "request",
                "requests",
                "http",
                "bad",
                "throttled",
                "forbidden",
                "logs",
                "log",
            ),
            "API errors are in API Gateway's access logs, which say which status and who answered.",
            ("api_errors", "alarms", "security_events"),
            (_EDGE, *[step for one in apis for step in _api_steps(one, status)]),
        ),
        Runsheet(
            "pipeline-failed",
            "A topic's daily run failed, or published nothing",
            (
                "failed",
                "failure",
                "daily",
                "cycle",
                "run",
                "article",
                "publish",
                "published",
                "dlq",
                "dead",
                "retries",
                "step",
                "functions",
                "nothing",
            ),
            "A failed daily run shows in the state machine's executions, the dead-letter queue and "
            "the daily cycle's log.",
            ("pipeline_health", "admin_inbox", "log_review", "alarms"),
            (
                _PIPELINE,
                Step(
                    f"Step Functions console > State machines > {PREFIX}{ENV}-daily-cycle > "
                    "Executions, filter Failed",
                    "console",
                    "Each failed run's graph, its input (the topic_id) and the error of each "
                    "attempt. Two retries come before the dead-letter queue.",
                ),
                Step(
                    f"/aws/lambda/{PREFIX}{ENV}-daily-cycle",
                    "log_group",
                    "The traceback, or Task timed out, at the time of the failed execution.",
                    query=_ERRORS_QUERY,
                ),
                Step(
                    f"SQS console > {PREFIX}{ENV}-pipeline-dlq",
                    "console",
                    "Messages waiting: dlq-handler should drain it into failed-executions "
                    "within seconds. Messages that stay mean dlq-handler is failing too.",
                ),
                Step(
                    "python scripts/admin_cli.py failed-executions list",
                    "console",
                    "The recorded failures, with topic and error (the failed-executions table).",
                ),
            ),
        ),
        Runsheet(
            "research-late",
            "Research is late, or a topic is not being researched",
            (
                "research",
                "late",
                "overdue",
                "tick",
                "findings",
                "schedule",
                "scheduler",
                "stale",
                "eventbridge",
            ),
            "Research runs on a per-topic EventBridge schedule; a late topic is a schedule that "
            "did not fire or a research-tick run that failed.",
            ("pipeline_health", "log_review"),
            (
                Step(
                    f"EventBridge console > Scheduler > Schedules > {PREFIX}{ENV}-<topic_id>-research-tick",
                    "console",
                    "That the schedule exists, is enabled and has the expected rate. It is made "
                    "by the Admin API when the topic is saved, not by Terraform.",
                ),
                _RUNS,
                Step(
                    f"/aws/lambda/{PREFIX}{ENV}-research-tick",
                    "log_group",
                    "Errors from the topic's adapter or web search.",
                    query=_ERRORS_QUERY,
                ),
                Step(
                    f"{PREFIX}{ENV}-research-tick",
                    "function",
                    "Lambda console > Monitor: invocations per hour, errors and throttles.",
                ),
            ),
        ),
        Runsheet(
            "lambda-errors",
            "A Lambda is erroring, timing out, slow or throttled",
            (
                "lambda",
                "function",
                "error",
                "errors",
                "timeout",
                "timed",
                "slow",
                "duration",
                "throttle",
                "throttles",
                "memory",
                "exception",
                "traceback",
                "crash",
            ),
            "Each Lambda has an errors and a throttles alarm, a widget on the pipeline dashboard "
            "and a log group named after it.",
            ("log_review", "alarms", "pipeline_health"),
            (
                _PIPELINE,
                _ALARMS,
                Step(
                    "CloudWatch console > Dashboards > Automatic dashboards > Lambda",
                    "console",
                    "AWS's generated dashboard across every function: errors, duration, "
                    "concurrent executions and throttles side by side.",
                ),
                Step(
                    f"/aws/lambda/{PREFIX}{ENV}-<function>",
                    "log_group",
                    "Run against the function's log group (or several at once in Logs Insights) "
                    "for tracebacks and timeouts.",
                    query=_ERRORS_QUERY,
                ),
                Step(
                    f"/aws/lambda/{PREFIX}{ENV}-<function>",
                    "log_group",
                    "Duration and memory per hour, from Lambda's own REPORT lines: a run near "
                    "its timeout or memory size is the next one to fail.",
                    query=_DURATION_QUERY,
                ),
            ),
        ),
        Runsheet(
            "security",
            "Blocked requests, attacks and the firewall",
            (
                "security",
                "attack",
                "attacks",
                "blocked",
                "waf",
                "firewall",
                "bot",
                "bots",
                "injection",
                "incident",
                "incidents",
                "ddos",
                "scan",
            ),
            "Blocked requests are grouped into incidents, which I can read; the raw requests are "
            "in the firewalls' logs.",
            ("security_events", "alarms"),
            (
                _EDGE,
                Step(
                    f"WAF & Shield console > Web ACLs > {PREFIX}{ENV}-public-api > Traffic overview",
                    "console",
                    "AWS's generated dashboard for the ACL: allowed against blocked, top rules, "
                    "countries and bots.",
                ),
                Step(
                    f"aws-waf-logs-{PREFIX}{ENV}-public-api",
                    "log_group",
                    "Which rules blocked what, by path.",
                    query=_WAF_BLOCKED_QUERY,
                ),
                Step(
                    f"aws-waf-logs-{PREFIX}{ENV}-admin",
                    "log_group",
                    "The admin API's allowlist at work: anything blocked here came from outside "
                    "the operator's addresses.",
                    query=_WAF_BLOCKED_QUERY,
                ),
            ),
        ),
        Runsheet(
            "feedback",
            "Reader feedback rejected, or screening used up",
            ("feedback", "comment", "comments", "vote", "votes", "screening", "rejected", "readers"),
            "Feedback is counted on the lambda-runs dashboard and has two alarms of its own.",
            ("alarms",),
            (
                _RUNS,
                Step(
                    f"CloudWatch console > Metrics > BloggerBear/{ENV}",
                    "console",
                    "FeedbackAccepted, FeedbackRejected, FeedbackCommentKept, FeedbackModelDropped "
                    "and FeedbackScreeningBudgetUsedUp over time.",
                ),
                Step(
                    f"/aws/lambda/{PREFIX}{ENV}-public-api",
                    "log_group",
                    "The feedback route's errors.",
                    query=_ERRORS_QUERY,
                ),
            ),
        ),
        Runsheet(
            "costs",
            "Spend: AI or AWS costs higher than expected",
            ("cost", "costs", "spend", "bill", "billing", "money", "expensive", "bedrock", "budget"),
            "I can read the week's spend; the AWS bill's detail is in Cost Explorer.",
            ("spend",),
            (
                Step("The site's Stats page", "console", "This week's AI spend against earlier weeks."),
                Step(
                    "Billing console > Cost Explorer, grouped by Service, daily",
                    "console",
                    "Which service grew, and from which day.",
                ),
                Step(
                    f"/aws/lambda/{PREFIX}{ENV}-cost-explorer-poll",
                    "log_group",
                    "Whether the daily cost poll ran: when it fails, the stats tables stop "
                    "getting AWS costs.",
                    query=_ERRORS_QUERY,
                ),
            ),
        ),
        Runsheet(
            "assistant",
            "The assistant itself: sign-in, 401, 403, 429 or slow answers",
            (
                "assistant",
                "agent",
                "mcp",
                "cognito",
                "sign",
                "signing",
                "signin",
                "login",
                "token",
                "ask",
                "401",
                "403",
                "429",
            ),
            "The assistant's API has its own access log, and the agent and MCP server their own log groups.",
            (),
            (
                *_api_steps("assistant", status),
                Step(
                    "The pipeline-config row's assistant_access setting",
                    "console",
                    '"off" refuses everyone and "allowlist" only the operator\'s addresses: '
                    "a 403 from the MCP server with no error logged is usually this.",
                ),
            ),
        ),
    )


def _clean_status(status) -> int | None:
    if status is None or isinstance(status, bool):
        return None
    try:
        value = int(status)
    except (TypeError, ValueError):
        return None
    return value if 100 <= value <= 599 else None


_WORD = re.compile(r"[a-z0-9]+")


def _choose(sheets: tuple[Runsheet, ...], symptom: str) -> Runsheet | None:
    """The runsheet whose id is `symptom`, or the one whose keywords the most words of it match."""
    text = symptom.strip().lower()
    for sheet in sheets:
        if text == sheet.id:
            return sheet
    words = set(_WORD.findall(text))
    scored = [(len(words & set(sheet.words)), -index, sheet) for index, sheet in enumerate(sheets)]
    best = max(scored, key=lambda item: item[:2])
    return best[2] if best[0] > 0 else None


def _render(step: Step, env: str | None) -> dict:
    present = not step.only_in or env in step.only_in
    where = fill(step.where, env)
    look_for = step.look_for if present else (step.instead or f"Not in {env}.")
    url = console_url(step.kind, where, log_group_region(where) if step.kind == "log_group" else None)
    return {
        "where": where,
        "kind": step.kind,
        "look_for": look_for,
        "query": step.query if present else None,
        "console_url": url,
        "here": present,
    }


def _query_card(sheet: Runsheet, number: int, step: dict, env: str | None) -> dict:
    region = log_group_region(step["where"])
    where = {"log_group": step["where"], "region": region}
    if step["console_url"]:
        where["open"] = step["console_url"]
    return {
        "kind": HOW_TO,
        "id": f"{sheet.id}-step-{number}",
        "noticed": f"Step {number}: a Logs Insights query for {step['where']}",
        "where": {key: value for key, value in where.items() if value},
        "suggestion": {
            "action": "Open CloudWatch > Logs Insights, pick the log group above and the time range, "
            "paste this and run it",
            "command": step["query"],
            "what_it_does": "Reads the log and changes nothing. Logs Insights bills per GB scanned, "
            "so keep the time range to when it happened.",
        },
    }


def investigate(symptom: str | None = None, status: int | None = None, api: str | None = None) -> dict:
    """A runsheet for a symptom: what the assistant can check itself, then where to look in this
    environment, in order. `symptom` is a runsheet id or the operator's words; `status` an HTTP
    status to put in the queries; `api` one of public, admin or assistant. With no symptom, or
    words that match none: the runsheets there are."""
    env = environment()
    status = _clean_status(status)
    api = api if api in API_CHOICES else None
    sheets = _runsheets(api, status)
    sheet = _choose(sheets, symptom) if isinstance(symptom, str) and symptom.strip() else None
    if sheet is None:
        return {
            "spoken": "I have runsheets for: "
            + "; ".join(s.title for s in sheets)
            + ". Ask about one of those.",
            "findings": [],
            "environment": env,
            "runsheets": [{"id": s.id, "title": s.title} for s in sheets],
        }

    steps = [_render(step, env) for step in sheet.steps]
    rows: list[list] = []
    if sheet.assistant_tools:
        rows.append([0, "Ask me", "I can check " + _say_tools(sheet.assistant_tools) + " myself.", ""])
    findings: list[dict] = []
    for number, step in enumerate(steps, start=1):
        look = step["look_for"]
        if step["query"]:
            look += " (query on a card below)" if len(findings) < QUERIES_MAX else ""
        rows.append([number, step["where"], look, step["console_url"] or ""])
        if step["query"] and len(findings) < QUERIES_MAX:
            findings.append(_query_card(sheet, number, step, env))

    spoken = (
        f"{sheet.summary} To check it yourself, a runsheet for {env or 'this environment'} "
        f"is on screen: {len(steps)} places to look"
    )
    spoken += f", with {len(findings)} queries to copy." if findings else "."
    if sheet.assistant_tools:
        spoken += " I can check " + _say_tools(sheet.assistant_tools) + " for you first."
    return {
        "spoken": spoken,
        "findings": findings,
        "environment": env,
        "runsheet": {"id": sheet.id, "title": sheet.title},
        "assistant_tools": list(sheet.assistant_tools),
        "status": status,
        "steps": steps,
        "table": {
            "title": f"Runsheet ({env or 'this environment'}): {sheet.title}",
            "columns": ["Step", "Where", "Look for", "Open"],
            "rows": rows,
        },
    }


def _say_tools(names: tuple[str, ...]) -> str:
    """Tool names as words for speech: "pipeline_health" -> "pipeline health"."""
    words = [name.replace("_", " ") for name in names]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]
