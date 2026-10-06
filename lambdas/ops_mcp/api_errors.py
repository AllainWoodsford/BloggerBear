"""`api_errors`: what the APIs answered with an error, who answered, and why.

"Any API failures?", "I'm investigating 400s on the public API between 1 and 3 this morning". The
assistant reads the APIs' access logs itself (one JSON line per request, written by API Gateway:
infra/modules/rest-api and ops-assistant/main.tf), through the name and tag check in logs.py.

**What an access log line says.** status, method, the route's template (resourcePath, ours: the
path a caller sent is not logged), errorType (who answered: the firewall, the throttle, the
authorizer, the integration) and integrationLatency ("-" when the request never reached the
Lambda). No address, no user agent, no token. So the root cause of each error is decided in code
from status and errorType (`cause`), and none of it is text a caller chose.

**Three fixed queries**, over the APIs' access logs for the window (logs.window), with at most an
HTTP status (a whole number from 100 to 599) put into them:

    BREAKDOWN  errors counted by status, errorType, route and method (the biggest groups)
    TOTAL      every request, for the error rate
    TIMELINE   errors per hour: when they started, and the error total (not cut short, unlike
               the breakdown)

**What to do next is said, not done.** A 5XX the Lambda answered is in the Lambda's own log:
the finding says so and names `log_review` with that function, for the agent to follow (or the
operator to ask). Firewall blocks in production point at `firewall_review`. The words are fixed
here; the suggestions are the catalogue's (`api_<cause>`).

**Check it yourself**: the same queries as `how_to` cards, for the same log groups and window, with
a console link: the runsheet for exactly what was looked at.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ops_mcp import architecture, logs, redact
from ops_mcp.suggestions import finding

HOW_TO = "how_to"
API_CHOICES = ("public", "admin", "assistant")
FINDINGS_MAX = 6
ROUTES_SHOWN = 25

FIX_WORDS = {
    "settings": "a settings change",
    "code": "a code fix",
    "transient": "a problem on AWS's side that usually passes",
    "caller": "the caller sending something wrong, not a bug here",
    "security": "the firewall doing its job",
    "benign": "nothing to fix",
    "unknown": "something I couldn't classify",
}


@dataclass(frozen=True)
class Api:
    key: str  # what the operator calls it: public, admin, assistant
    name: str  # the REST API's name template: this deployment's prefix (common/naming.py), then {env}
    function: str  # the catalogue key of the Lambda behind it, for log_review
    words: str


APIS = {
    "public": Api("public", f"{architecture.PREFIX}{{env}}-public-api", "public-api", "the public API"),
    "admin": Api("admin", f"{architecture.PREFIX}{{env}}-admin-api", "admin-api", "the admin API"),
    "assistant": Api("assistant", f"{architecture.PREFIX}{{env}}-ops-mcp", "ops-mcp", "the assistant's API"),
}


@dataclass(frozen=True)
class Cause:
    key: str
    label: str  # for speech: "400s the handler chose itself"
    fix_type: str
    advice: str


CAUSES = {
    "firewall_blocked": Cause(
        "firewall_blocked",
        "requests the firewall blocked",
        "security",
        "The firewall (WAF) refused them before they reached the API. In production, ask about the "
        "firewall for which rule and how many addresses; a burst from one address is someone "
        "probing, a steady trickle across many is the managed rules doing their job.",
    ),
    "throttled": Cause(
        "throttled",
        "requests over the API's rate limit",
        "settings",
        "API Gateway's throttle answered 429. If they are real users, raise the stage's "
        "throttling_rate_limit and throttling_burst_limit in Terraform; if one client is looping, "
        "that client needs fixing.",
    ),
    "auth_refused": Cause(
        "auth_refused",
        "requests refused for their sign-in or signature",
        "caller",
        "The token or IAM signature was missing, expired or not allowed. For the admin API that is "
        "usually expired operator credentials; for the assistant, a sign-in that needs renewing.",
    ),
    "no_such_route": Cause(
        "no_such_route",
        "requests for paths the API doesn't have",
        "benign",
        "API Gateway answers 403 Missing Authentication Token for a path it does not have. Usually "
        "scanners; a real page calling a wrong path would show up as one route over and over.",
    ),
    "bad_request": Cause(
        "bad_request",
        "requests API Gateway refused as malformed",
        "caller",
        "The body or parameters did not match what the route accepts, so API Gateway refused it "
        "before the Lambda ran.",
    ),
    "handler_4xx": Cause(
        "handler_4xx",
        "4XXs the handler chose itself",
        "caller",
        "The Lambda ran and answered 4XX on purpose: bad input, or something that does not exist "
        "(a 404 for an article that is gone). Not a bug, unless the site itself is sending it: "
        "then the route on screen is where to look.",
    ),
    "integration_timeout": Cause(
        "integration_timeout",
        "requests where the Lambda took longer than 29 seconds",
        "code",
        "API Gateway gives up after 29 seconds and answers 504. The Lambda is too slow for this "
        "route: read its log for what it waited on, and make the slow part asynchronous or faster.",
    ),
    "lambda_failed": Cause(
        "lambda_failed",
        "5XXs from the Lambda failing",
        "code",
        "The Lambda raised an error or returned something API Gateway could not use. Its own log "
        "has the traceback: I can read it with log_review for that function and the same window.",
    ),
    "aws_5xx": Cause(
        "aws_5xx",
        "5XXs from API Gateway itself",
        "transient",
        "API Gateway answered 5XX without the Lambda failing. Usually passes; if it lasts, check the "
        "AWS Health Dashboard.",
    ),
    "other": Cause(
        "other", "errors I couldn't classify", "unknown", "The breakdown on screen has the detail."
    ),
}

_AUTH = {
    "UNAUTHORIZED",
    "ACCESS_DENIED",
    "EXPIRED_TOKEN",
    "INVALID_SIGNATURE",
    "INVALID_API_KEY",
    "AUTHORIZER_FAILURE",
}
_BAD_REQUEST = {"BAD_REQUEST_BODY", "BAD_REQUEST_PARAMETERS", "REQUEST_TOO_LARGE", "UNSUPPORTED_MEDIA_TYPE"}
_LAMBDA = {"INTEGRATION_FAILURE", "API_CONFIGURATION_ERROR", "AUTHORIZER_CONFIGURATION_ERROR"}


def cause(status: int, error_type: str) -> Cause:
    """The root cause of one kind of error, from what API Gateway logged about it. An error API
    Gateway answered itself carries an errorType; one with none was the Lambda's own answer."""
    kind = (error_type or "").strip().upper()
    if kind in ("-", "NULL"):
        kind = ""
    if kind == "WAF_FILTERED":
        return CAUSES["firewall_blocked"]
    if kind in ("THROTTLED", "QUOTA_EXCEEDED") or status == 429:
        return CAUSES["throttled"]
    if kind == "MISSING_AUTHENTICATION_TOKEN":
        return CAUSES["no_such_route"]
    if kind in _AUTH or status == 401:
        return CAUSES["auth_refused"]
    if kind in _BAD_REQUEST:
        return CAUSES["bad_request"]
    if kind == "INTEGRATION_TIMEOUT" or status == 504:
        return CAUSES["integration_timeout"]
    if kind in _LAMBDA or status == 502:
        return CAUSES["lambda_failed"]
    if 400 <= status < 500:
        return CAUSES["handler_4xx"] if not kind else CAUSES["other"]
    if status >= 500:
        return CAUSES["lambda_failed"] if not kind else CAUSES["aws_5xx"]
    return CAUSES["other"]


# --- the queries -----------------------------------------------------------------------------------


def clean_status(status) -> int | None:
    """An HTTP status the operator named, as a whole number from 100 to 599, or None."""
    if isinstance(status, bool):
        return None
    try:
        value = int(status)
    except (TypeError, ValueError):
        return None
    return value if 100 <= value <= 599 else None


def queries(status: int | None) -> dict[str, str]:
    """The three queries. The one value put into them is `status`, which clean_status made a whole
    number from 100 to 599 (None: every 4XX and 5XX)."""
    if status is not None and clean_status(status) != status:
        raise ValueError("not a status this module puts in a query")
    errors = f"status = {status}" if status is not None else "status >= 400"
    return {
        "breakdown": (
            f"filter {errors}\n"
            "| stats count(*) as requests by status, errorType, resourcePath, httpMethod\n"
            f"| sort requests desc\n| limit {ROUTES_SHOWN * 2}"
        ),
        # Every request by its status code, the 200s as well as the errors: how the API is
        # doing is read off this, not off whether the Lambda behind it finished its run.
        "total": "stats count(*) as requests by status\n| sort status asc",
        "timeline": f"filter {errors}\n| stats count(*) as errors by bin(1h)\n| sort bin(1h) asc",
    }


# --- reading and working it out --------------------------------------------------------------------


def _groups(chosen: list[Api], env: str) -> dict[str, Api]:
    return {f"/aws/apigateway/{api.name.format(env=env)}-access": api for api in chosen}


def api_errors(
    api: str | None = None,
    status: int | None = None,
    hours: int | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    now: datetime | None = None,
    run: Callable[[list], dict] | None = None,
    readable: Callable[[list[str]], tuple[list[str], list[dict]]] | None = None,
    wait_seconds: float = logs.WAIT_SECONDS,
) -> dict:
    env = architecture.environment()
    if env is None:
        return _refusal("I don't know which environment I'm in, so I won't read any logs.")
    if api is not None and api not in API_CHOICES:
        return _refusal("I can look at the public API, the admin API or the assistant's API.")
    chosen = [APIS[api]] if api else [APIS["public"], APIS["admin"], APIS["assistant"]]
    status = clean_status(status)
    when = logs.window(hours, start, end, now=now)
    region = logs.home_region()
    by_group = _groups(chosen, env)
    allowed, refused = (readable or (lambda names: logs.readable(names, region=region)))(list(by_group))
    if not allowed:
        return _refusal(
            "I'm not allowed to read those access logs: "
            + (refused[0]["why"] if refused else "none to read."),
            refused=refused,
        )

    text = queries(status)
    jobs = []
    for group in allowed:
        key = by_group[group].key
        jobs += [((key, name), region, group, query, when.start, when.end) for name, query in text.items()]
    # `wait_seconds` is shorter when a caller reads several logs in one request (memory.py).
    if run is None:
        run = lambda batch: logs.run_queries(batch, label="ops_api_errors", wait_seconds=wait_seconds)  # noqa: E731
    results = run(jobs)

    rows, findings, complete = [], [], True
    totals = {"requests": 0, "errors": 0}
    for group in allowed:
        target = by_group[group]
        breakdown = results.get((target.key, "breakdown"))
        total = results.get((target.key, "total"))
        timeline = results.get((target.key, "timeline"))
        complete = complete and all(part is not None for part in (breakdown, total, timeline))
        row = _work_out(target, breakdown or [], total or [], timeline or [])
        rows.append(row)
        totals["requests"] += row["requests"]
        totals["errors"] += row["errors"]
        findings += _findings(target, row, when, env)
    findings.sort(key=lambda item: -item["root_cause"]["count"])
    findings = findings[:FINDINGS_MAX]

    spoken = _spoken(rows, chosen, status, when, totals, complete, refused, env)
    for row in rows:
        del row["_by_cause"]
    return {
        "spoken": spoken,
        "findings": findings + _check_yourself(allowed, text, when, region),
        "environment": env,
        "apis": [api.key for api in chosen],
        "status": status,
        "window": when.as_dict(),
        "requests": totals["requests"],
        "errors": totals["errors"],
        "by_api": rows,
        "refused": refused,
        "complete": complete,
        "table": _table(rows, when),
        "as_of": when.end.isoformat(),
    }


def _refusal(spoken: str, **more) -> dict:
    return {"spoken": spoken, "findings": [], "requests": 0, "errors": 0, "by_api": [], **more}


def _route(value) -> str:
    """A route as logged: API Gateway's template for the route (ours), or "-" for a path it does
    not have. Scrubbed anyway: it is still read from a log."""
    return redact.scrub(value, 120) or "-"


def _work_out(api: Api, breakdown: list[dict], total: list[dict], timeline: list[dict]) -> dict:
    by_cause: Counter = Counter()
    by_status: Counter = Counter()
    routes: dict[tuple, int] = defaultdict(int)
    for line in breakdown:
        count = logs.count(line.get("requests"))
        status = logs.count(line.get("status"))
        found = cause(status, str(line.get("errorType") or ""))
        by_cause[found.key] += count
        by_status[status] += count
        route = (
            status,
            _route(line.get("resourcePath")),
            redact.scrub(line.get("httpMethod"), 10),
            found.key,
        )
        routes[route] += count
    requests = sum(logs.count(line.get("requests")) for line in total)
    status_codes: Counter = Counter()
    for line in total:
        code = clean_status(line.get("status"))
        if code is not None:
            status_codes[code] += logs.count(line.get("requests"))
    hours = [(redact.scrub(line.get("bin(1h)"), 40), logs.count(line.get("errors"))) for line in timeline]
    # The timeline counts every error; the breakdown only its biggest groups.
    errors = max(sum(by_cause.values()), sum(count for _, count in hours))
    started = next((when for when, count in hours if count), None)
    peak = max(hours, key=lambda item: item[1]) if hours else None
    return {
        "api": api.key,
        "requests": requests,
        "errors": errors,
        "error_rate": round(errors / requests, 4) if requests else None,
        # Every status the API answered with and how often (200s included), then the errors alone.
        "status_codes": {str(code): count for code, count in sorted(status_codes.items())},
        "by_status": {str(code): count for code, count in sorted(by_status.items())},
        "causes": [
            {"cause": key, "count": count, "fix_type": CAUSES[key].fix_type}
            for key, count in by_cause.most_common()
        ],
        "routes": [
            {"status": s, "route": r, "method": m, "cause": c, "requests": n}
            for (s, r, m, c), n in sorted(routes.items(), key=lambda item: -item[1])[:ROUTES_SHOWN]
        ],
        "first_error_hour": started,
        "peak_hour": {"hour": peak[0], "errors": peak[1]} if peak and peak[1] else None,
        "by_hour": hours,
        "_by_cause": by_cause,
    }


def _findings(api: Api, row: dict, when: logs.Window, env: str) -> list[dict]:
    out = []
    for key, count in row["_by_cause"].most_common():
        if not count:
            continue
        found = CAUSES[key]
        item = finding(
            f"api_{key}",
            f"{api.words.capitalize()}: {count} {found.label} in {when.words()}",
            api.function,
            api=api.key,
        )
        follow = None
        if key in ("lambda_failed", "integration_timeout"):
            follow = {
                "tool": "log_review",
                "function": api.function,
                "start": when.start.isoformat(),
                "end": when.end.isoformat(),
            }
        elif key == "firewall_blocked":
            follow = {"tool": "firewall_review"} if env == "production" else None
        item["root_cause"] = {
            "cause": key,
            "fix_type": found.fix_type,
            "needs": FIX_WORDS[found.fix_type],
            "advice": found.advice,
            "count": count,
            "next": follow,
        }
        out.append(item)
    return out


def _check_yourself(
    groups: list[str], text: dict[str, str], when: logs.Window, region: str | None
) -> list[dict]:
    where = {"log_groups": ", ".join(groups), "from": when.start.isoformat(), "to": when.end.isoformat()}
    if len(groups) == 1:
        link = architecture.console_url("log_group", groups[0], region)
        if link:
            where["open"] = link
    titles = {
        "total": "every request by status code",
        "breakdown": "the errors by status, who answered and route",
        "timeline": "errors per hour",
    }
    return [
        {
            "kind": HOW_TO,
            "id": f"api-errors-{name}",
            "noticed": f"Check it yourself: {title}",
            "where": where,
            "suggestion": {
                "action": "Open CloudWatch > Logs Insights, select the access log groups above, set the "
                "time range to the one above, paste this and run it",
                "command": text[name],
                "what_it_does": "Reads the access logs and changes nothing. errorType says who answered: "
                "WAF_FILTERED the firewall, THROTTLED the rate limit, empty with a latency the Lambda.",
            },
        }
        for name, title in titles.items()
    ]


def _status_words(code: int) -> str:
    """What a status code that is not an error means, for the table."""
    if 200 <= code < 300:
        return "OK"
    if 300 <= code < 400:
        return "redirect or not modified"
    return "informational"


def _table(rows: list[dict], when: logs.Window) -> dict:
    """Per API: each status code it answered with and how often, the successful ones first as one
    row per code, then the errors by route with their root cause."""
    table_rows = []
    for row in rows:
        for code, count in row["status_codes"].items():
            if int(code) < 400:
                meaning = _status_words(int(code))
                table_rows.append([row["api"], int(code), "", "(all routes)", count, meaning, ""])
        for route in row["routes"]:
            found = CAUSES[route["cause"]]
            table_rows.append(
                [
                    row["api"],
                    route["status"],
                    route["method"],
                    route["route"],
                    route["requests"],
                    found.label,
                    FIX_WORDS[found.fix_type],
                ]
            )
    return {
        "title": f"API calls by status code, {when.words()}",
        "columns": ["API", "Status", "Method", "Route", "Requests", "Root cause", "Needs"],
        "rows": table_rows,
    }


STATUS_CODES_SPOKEN = 4


def _by_code_words(rows: list[dict]) -> str:
    """The status codes the APIs answered with, most frequent first: "By status code: 1890 were
    200, 80 were 400 and 15 were 502." Empty when the logs gave no codes."""
    merged: Counter = Counter()
    for row in rows:
        merged.update({int(code): count for code, count in row["status_codes"].items()})
    if not merged:
        return ""
    ranked = merged.most_common()
    said = [
        f"{count} {'were' if count != 1 else 'was'} {code}" for code, count in ranked[:STATUS_CODES_SPOKEN]
    ]
    rest = sum(count for _, count in ranked[STATUS_CODES_SPOKEN:])
    if rest:
        said.append(f"{rest} other")
    return f"By status code: {_join(said)}."


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _spoken(rows, chosen, status, when, totals, complete, refused, env) -> str:
    which = chosen[0].words if len(chosen) == 1 else "the APIs"
    what = f"{status} responses" if status else "errors"
    words = []
    if totals["errors"] == 0:
        served = f" out of {totals['requests']} requests" if totals["requests"] else ""
        words.append(f"In {when.words()}, {which} answered no {what}{served}.")
        words.append(_by_code_words(rows))
    else:
        rate = ""
        if totals["requests"]:
            percent = round(100 * totals["errors"] / totals["requests"], 1)
            rate = f" out of {totals['requests']} requests ({percent}%)"
        words.append(f"In {when.words()}, {which} answered {totals['errors']} {what}{rate}.")
        words.append(_by_code_words(rows))
        merged: Counter = Counter()
        for row in rows:
            merged.update(row["_by_cause"])
        ranked = merged.most_common(2)
        for index, (key, count) in enumerate(ranked):
            found = CAUSES[key]
            lead = "Most were" if index == 0 else "Then"
            words.append(f"{lead} {count} {found.label}: that looks like {FIX_WORDS[found.fix_type]}.")
        if any(key in merged for key in ("lambda_failed", "integration_timeout")):
            words.append("The Lambda's own log has the cause of the 5XXs; I can read it next.")
        if "firewall_blocked" in merged and env == "production":
            words.append("I can look at the firewall for which rule blocked them.")
        started = [row["first_error_hour"] for row in rows if row["first_error_hour"]]
        if started and when.hours > 2:
            words.append(f"The first came in the hour from {min(started)} UTC.")
    if not complete:
        words.append("Some of the logs didn't answer in time, so the numbers may be low.")
    if refused:
        words.append(f"I wasn't allowed to read {len(refused)} of the access logs.")
    words.append("The breakdown, and how to check it yourself, is on screen.")
    return " ".join(words)
