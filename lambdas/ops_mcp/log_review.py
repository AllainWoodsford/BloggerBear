"""`log_review`: what went wrong in the Lambdas' logs, why, and what to do about it.

"Any errors in the logs?", "why is crypto failing?", "what happened to research-tick between 1 and
3 this morning?". The assistant reads the logs itself (logs.py says which it may), works out the
root cause of each error in code, says whether it needs a code fix, a settings change, or just
time, and puts on screen how the operator can check it themselves.

**What is read.** One function's log (`function`, by any name architecture.py understands), a
topic's (`topic`: the functions that run for it, research-tick and daily-cycle, narrowed to lines
naming the topic or its adapter), or every function this environment has. Over the last `hours`,
or between `start` and `end` (logs.window clamps both). Four fixed Logs Insights queries, one call
each over all the groups at once:

    SAMPLE    the newest error lines (up to SAMPLE_LINES), for classifying
    TOTALS    how many error lines per function, first and last seen
    RUNS      invocations, duration and memory per function, from Lambda's REPORT lines
    BASELINE  error lines per function per day over the BASELINE_DAYS before the window

**Root cause is decided here, not by the model.** Each sampled line is matched against CAUSES, in
order, and the first match is its cause: a Lambda timeout, out of memory, a model throttled, a
source's rate limit (CoinGecko's 429), a source down, missing data from the source (coins dropped
from the crypto pool), a permission refused, a packaging error, a code error. Each cause carries
its fix type (`code`, `settings`, `transient`, `data`, `permissions`, `packaging`) and fixed advice.
A function's count per cause is the share of its sampled lines, scaled to its total when there
were more lines than the sample (and then said to be about). Two causes get help from the REPORT
numbers: a timeout on a function whose longest run is near its limit, and memory use near its size.

**Nothing a log says reaches speech or a command.** `spoken` is built here from counts, function
names (ours) and fixed words. Example lines go under `untrusted`, each through redact.scrub
(personal data and secrets out, addresses masked, a line that reads like instructions withheld
whole). The suggestions are the catalogue's (suggestions.py), by cause.

**Check it yourself.** The result carries a table of what was found and `how_to` cards with the
same queries, for the same log groups and window, to paste into Logs Insights, plus a console link
for each log group: the runsheet, for exactly what was looked at.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from common.adapters.registry import ADAPTER_REGISTRY
from ops_mcp import architecture, logs, redact, topic_match
from ops_mcp.suggestions import ID_PATTERN, finding
from ops_mcp.tools import _join, _topic_label

SAMPLE_LINES = 500
EXAMPLES_PER_CAUSE = 2
FINDINGS_MAX = 6
BASELINE_DAYS = 7
UNUSUAL_TIMES = 2
UNUSUAL_MIN = 5
NEAR_LIMIT = 0.9  # a run at 90% of the timeout or of memory is "at the limit"
HOW_TO = "how_to"

# What counts as an error line. Fixed text. Wide on purpose: classifying is done in code, and a
# line that is not really an error is classified `other` and said to be unclassified.
ERROR_PATTERN = (
    r"/(?i)(error|exception|traceback|timed out|timeout|throttl|denied|not authorized|\b429\b|"
    r"too many|rate limit|killed|out of memory|could not|failed|unable to|dropping|no usable|"
    r"missing anchor|refused)/"
)
# Lambda's own lines that are not errors even when a word above appears in them.
_NOT_ERRORS = r"/^(START|END|INIT_START) /"

# What each topic's adapter writes at the start of its log lines (common/adapters/*.py and
# common/web_search.py print "<prefix>: ..."). A key from ADAPTER_REGISTRY, so a topic can only
# narrow the search to a prefix that is ours.
ADAPTER_PREFIXES = {
    "crypto_feed": "crypto_feed:",
    "github_trending": "github_trending:",
    "hacker_news": "hacker_news:",
    "web_search": "web_search:",
}
# The functions that run per topic.
TOPIC_FUNCTIONS = ("research-tick", "daily-cycle")

FIX_WORDS = {
    "settings": "a settings change",
    "code": "a code fix",
    "transient": "a problem on the other side that usually passes by itself",
    "data": "a problem with the data the source sent",
    "permissions": "a permissions fix in Terraform",
    "packaging": "a packaging fix",
    "benign": "nothing to fix: the code expects it",
    "unknown": "something I couldn't classify",
}


@dataclass(frozen=True)
class Cause:
    key: str
    pattern: re.Pattern
    label: str  # for speech, after the function's name: "research-tick timed out"
    fix_type: str
    advice: str  # what to do: fixed words, shown and offered as the suggestion
    where: str  # where the change would be made


def _cause(key, pattern, label, fix_type, advice, where) -> Cause:
    return Cause(key, re.compile(pattern, re.I), label, fix_type, advice, where)


# In order: the first that matches a line is its cause. Specific before general.
CAUSES: tuple[Cause, ...] = (
    _cause(
        "lambda_timeout",
        r"Task timed out|Status: timeout",
        "hit its time limit",
        "settings",
        "Raise the function's timeout, or find what it waits on. If most runs are close to the "
        "limit (the longest run is on screen), it needs more time; if only a few are, a slow "
        "model call or source is holding it up.",
        "timeout on the function's aws_lambda_function in infra/environments/<env>/main.tf",
    ),
    _cause(
        "out_of_memory",
        r"Runtime exited with error: signal: killed|MemoryError|Runtime\.OutOfMemory|out of memory",
        "ran out of memory",
        "settings",
        "Raise the function's memory (it also gets more CPU), or look for a step that loads too "
        "much at once.",
        "memory_size on the function's aws_lambda_function in infra/environments/<env>/main.tf",
    ),
    _cause(
        "packaging",
        r"Runtime\.ImportModuleError|ModuleNotFoundError|ImportError|Unable to import module",
        "could not start: a module is missing",
        "packaging",
        "A dependency is missing from the deployment package. Add it to lambdas/requirements.txt "
        "(or the function's own requirements file) and redeploy.",
        "lambdas/requirements*.txt, then the package build in infra/",
    ),
    _cause(
        "access_denied",
        r"AccessDenied|not authorized to perform|UnauthorizedOperation|is not authorized",
        "was refused permission",
        "permissions",
        "The function's role is missing an action or a resource. Add it to the role's policy in "
        "Terraform (read the denied action in the example on screen).",
        "the function's IAM role policy in infra/",
    ),
    _cause(
        "model_throttled",
        r"ThrottlingException|ServiceQuotaExceeded|TooManyRequestsException|Too many tokens",
        "was throttled by AWS",
        "transient",
        "Calls were refused for being too many at once (Bedrock or DynamoDB). It passes, but if it "
        "keeps happening spread the topics' schedules out or ask AWS for a higher quota.",
        "topics' research intervals and daily cadences (admin_cli topics update), or Service Quotas",
    ),
    _cause(
        "token_limit",
        r"token limit|max_tokens|MaxTokensReached|was cut off|draft truncated",
        "had a model reply cut off at its token limit",
        "settings",
        "The model's reply was longer than its token limit. Raise the limit for that step, or "
        "tighten the prompt so the reply is shorter.",
        "the model settings (admin_cli models / pipeline-config) or the prompt in lambdas/common",
    ),
    _cause(
        "source_rate_limited",
        r"\b429\b|Too Many Requests|rate.?limit",
        "was rate-limited by its data source",
        "settings",
        "The source (CoinGecko, GitHub, web search) refused calls for being too many. Research the "
        "topic less often, or give it an API key with a higher limit.",
        "the topic's research interval (admin_cli topics update), or the source's key in SSM",
    ),
    _cause(
        "source_down",
        r"\b50[0-4]\b|Bad Gateway|Service Unavailable|Gateway Timeout|Internal Server Error",
        "got errors from its data source",
        "transient",
        "The source answered with a server error. That is on their side and usually passes; the "
        "next run retries.",
        "nothing to change unless it lasts; then the source's status page",
    ),
    _cause(
        "source_timeout",
        r"ReadTimeout|ConnectTimeout|ConnectionError|EndpointConnectionError|timed out",
        "timed out waiting for a source",
        "transient",
        "A call to a source or an AWS service took too long. Usually passes; if it keeps "
        "happening, the source is slow and the call's timeout or retries need changing.",
        "the call's timeout in lambdas/common/http_retry.py or the adapter",
    ),
    _cause(
        "source_data",
        r"no usable history|from the pool|missing anchor|returned no .{0,40}items|no headlines|"
        r"history fetch failed|no results",
        "got incomplete data from its source",
        "data",
        "The source sent less than expected (for crypto: coins dropped from the pool for having no "
        "usable price history). A few is normal; many means the source is struggling or the "
        "adapter's expectations need loosening.",
        "the topic's adapter in lambdas/common/adapters/",
    ),
    _cause(
        "configuration",
        r"could not read the .{0,40}(config|key|token)|ParameterNotFound|unknown adapter|unknown .{0,20}plan",
        "could not read part of its configuration",
        "settings",
        "A setting or a key it needs is missing or unreadable, so it fell back to a default. Set it.",
        "the pipeline config (admin_cli pipeline-config), the topic, or the key in SSM",
    ),
    _cause(
        "conflict",
        r"ConditionalCheckFailed|TransactionCanceled",
        "lost a race to write the same row",
        "benign",
        "Two writes raced and the second was refused, as the code intends. Nothing to do unless "
        "something is visibly missing.",
        "nothing to change",
    ),
    _cause(
        "code_error",
        r"Traceback|KeyError|TypeError|ValueError|AttributeError|IndexError|ValidationException|"
        r"ParamValidationError|unhandled exception|NameError|ZeroDivision",
        "raised an error in its own code",
        "code",
        "The code hit a case it does not handle. The example on screen names the error; it needs a "
        "code fix and a test for that case.",
        "the function's handler or what it calls in lambdas/",
    ),
)
OTHER = _cause(
    "other",
    r"$^",
    "logged errors I couldn't classify",
    "unknown",
    "Read the examples on screen; if they matter, the function's log has the full lines.",
    "the function's log group",
)
BY_KEY = {cause.key: cause for cause in (*CAUSES, OTHER)}


def classify(message: str) -> Cause:
    """A log line's root cause: the first of CAUSES that matches, or OTHER."""
    text = str(message or "")
    return next((cause for cause in CAUSES if cause.pattern.search(text)), OTHER)


# --- the queries -----------------------------------------------------------------------------------


def _topic_filter(topic_id: str | None, prefix: str | None) -> str:
    """A filter narrowing a query to one topic's lines: those naming the topic, or written by its
    adapter. Both are checked before they get here: the id against ID_PATTERN (letters, digits,
    "-" and "_"), the prefix from ADAPTER_PREFIXES. Neither can end the regex or add a command."""
    if topic_id is None:
        return ""
    if not ID_PATTERN.match(topic_id) or (prefix is not None and prefix not in ADAPTER_PREFIXES.values()):
        raise ValueError("not a topic filter this module builds")
    words = [re.escape(topic_id)] + ([re.escape(prefix)] if prefix else [])
    return f"\n| filter @message like /{'|'.join(words)}/"


def queries(topic_id: str | None = None, prefix: str | None = None) -> dict[str, str]:
    """The four queries, as text. The only thing put into them is the topic filter above."""
    narrow = _topic_filter(topic_id, prefix)
    errors = f"filter @message like {ERROR_PATTERN} and @message not like {_NOT_ERRORS}{narrow}"
    return {
        "sample": (
            f"fields @timestamp, @log, @message\n| {errors}\n| sort @timestamp desc\n| limit {SAMPLE_LINES}"
        ),
        "totals": (
            f"{errors}\n| stats count(*) as errors, min(@timestamp) as first_seen, "
            "max(@timestamp) as last_seen by @log"
        ),
        "runs": (
            'filter @type = "REPORT"\n'
            "| stats count(*) as runs, avg(@duration) as avg_ms, max(@duration) as max_ms,\n"
            "  max(@maxMemoryUsed / 1000 / 1000) as max_memory_mb,\n"
            "  max(@memorySize / 1000 / 1000) as memory_mb\n"
            "  by @log"
        ),
        "baseline": f"{errors}\n| stats count(*) as errors by @log, bin(1d)",
    }


# --- what to read ----------------------------------------------------------------------------------


class Refused(Exception):
    """The request names something this tool will not read. The message is fixed text, for speech."""


@dataclass
class Scope:
    functions: list[architecture.Component]
    topic_id: str | None = None
    topic_name: str | None = None
    adapter: str | None = None
    prefix: str | None = None
    note: str | None = None  # something to say about how the name was taken

    def words(self) -> str:
        if self.topic_name:
            return f"{self.topic_name}'s runs"
        if len(self.functions) == 1:
            return self.functions[0].key
        return "every function"


def _functions(env: str) -> list[architecture.Component]:
    return [c for c in architecture.CATALOGUE if c.kind == "function" and architecture.exists_in(c, env)]


def scope(function: str | None, topic: str | None, env: str) -> Scope:
    """Which functions to read, from the names the model passed on. Everything read is a catalogue
    function of this environment; nothing the caller typed becomes a log group's name."""
    if topic is not None:
        # The topic as the operator said it (topic_match.py); a guess is said, a doubt is asked.
        item, matched, refusal = topic_match.pick(topic)
        if item is None:
            raise Refused(refusal)
        topic = item.get("topic_id")
        if not isinstance(topic, str) or not ID_PATTERN.match(topic):
            raise Refused("That topic's id isn't one I can look up.")
        adapter = item.get("adapter") if item.get("adapter") in ADAPTER_REGISTRY else None
        functions = [architecture.by_key("function", key) for key in TOPIC_FUNCTIONS]
        return Scope(
            [c for c in functions if architecture.exists_in(c, env)],
            topic_id=topic,
            topic_name=_topic_label(item, topic),
            adapter=adapter,
            prefix=ADAPTER_PREFIXES.get(adapter or ""),
            note=topic_match.took(matched).strip() or None,
        )
    if function is not None:
        # A near miss ("reserch tick") is taken and said; several close ones are asked about.
        resolved, took_note, did_you_mean = architecture.forgiving(function, "function")
        found = [c for c in resolved.matches if c.kind == "function" and architecture.exists_in(c, env)]
        if not found:
            raise Refused(did_you_mean or "I don't know a function by that name in this environment.")
        if resolved.asked_env is not None and resolved.asked_env not in architecture.ENVIRONMENTS:
            raise Refused("That name is for an environment that isn't this one, so I won't read its logs.")
        note = took_note
        if resolved.asked_env is not None and resolved.asked_env != env:
            note = f"You named {resolved.asked_env}'s function; I can only read {env}'s, so this is {env}'s."
        return Scope(found[:1], note=note)
    return Scope(_functions(env))


def _lambda_group(component: architecture.Component, env: str) -> str:
    return architecture.fill(f"/aws/lambda/{component.name}", env)


# --- reading and working it out --------------------------------------------------------------------


def _function_key(group: str, env: str) -> str:
    return group.removeprefix(f"/aws/lambda/{architecture.PREFIX}{env}-")


_TIMED_OUT_AFTER = re.compile(r"Task timed out after ([\d.]+) seconds")


def review(
    function: str | None = None,
    topic: str | None = None,
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
    try:
        chosen = scope(function, topic, env)
    except Refused as exc:
        return _refusal(str(exc))
    when = logs.window(hours, start, end, now=now)
    region = logs.home_region()
    wanted = [_lambda_group(c, env) for c in chosen.functions]
    allowed, refused = (readable or (lambda names: logs.readable(names, region=region)))(wanted)
    if not allowed:
        return _refusal(
            "I'm not allowed to read those logs: "
            + (refused[0]["why"] if refused else "no log group to read."),
            refused=refused,
        )

    text = queries(chosen.topic_id, chosen.prefix)
    groups = tuple(allowed)
    baseline_start = when.start - timedelta(days=BASELINE_DAYS)
    jobs = [
        (("all", "sample"), region, groups, text["sample"], when.start, when.end),
        (("all", "totals"), region, groups, text["totals"], when.start, when.end),
        (("all", "runs"), region, groups, text["runs"], when.start, when.end),
        (("all", "baseline"), region, groups, text["baseline"], baseline_start, when.start),
    ]
    # `wait_seconds` is shorter when a caller reads several logs in one request (memory.py).
    if run is None:
        run = lambda batch: logs.run_queries(batch, label="ops_log_review", wait_seconds=wait_seconds)  # noqa: E731
    results = run(jobs)
    sample = results.get(("all", "sample"))
    totals = results.get(("all", "totals"))
    runs = results.get(("all", "runs"))
    baseline = results.get(("all", "baseline"))
    complete = all(part is not None for part in (sample, totals, runs, baseline))

    per_function = _work_out(sample or [], totals or [], runs or [], baseline, env, when)
    findings = _findings(per_function, chosen, when)
    total = sum(row["errors"] for row in per_function.values())
    examples_withheld = sum(row["withheld"] for row in per_function.values())

    return {
        "spoken": _spoken(per_function, chosen, when, total, complete, refused),
        "findings": findings + _check_yourself(allowed, text, when, region),
        "environment": env,
        "scope": {
            "functions": [c.key for c in chosen.functions],
            "topic_id": chosen.topic_id,
            "adapter": chosen.adapter,
            "note": chosen.note,
        },
        "window": when.as_dict(),
        "errors": total,
        "functions": [_public_row(key, row) for key, row in sorted(per_function.items())],
        "refused": refused,
        "complete": complete,
        "withheld_lines": examples_withheld,
        "table": _table(per_function, when),
        "as_of": when.end.isoformat(),
    }


def _refusal(spoken: str, **more) -> dict:
    return {"spoken": spoken, "findings": [], "errors": 0, "functions": [], **more}


def _work_out(sample, totals, runs, baseline, env, when) -> dict[str, dict]:
    """Per function: error lines, the share of each cause (scaled to the total), examples
    (scrubbed), the REPORT numbers, and whether errors are unusual against the baseline."""
    rows: dict[str, dict] = {}

    def row(group: str) -> dict:
        key = _function_key(group, env)
        return rows.setdefault(
            key,
            {
                "errors": 0,
                "sampled": Counter(),
                "examples": {},
                "withheld": 0,
                "first_seen": None,
                "last_seen": None,
                "runs": None,
                "typical": None,
                "timeout_seconds": None,
            },
        )

    for line in totals:
        target = row(logs.group_of(line.get("@log")))
        target["errors"] += logs.count(line.get("errors"))
        target["first_seen"] = line.get("first_seen")
        target["last_seen"] = line.get("last_seen")
    for line in sample:
        target = row(logs.group_of(line.get("@log")))
        message = line.get("@message") or ""
        cause = classify(message)
        target["sampled"][cause.key] += 1
        seconds = _TIMED_OUT_AFTER.search(message)
        if seconds:
            target["timeout_seconds"] = float(seconds.group(1))
        shown = target["examples"].setdefault(cause.key, [])
        if len(shown) < EXAMPLES_PER_CAUSE:
            scrubbed = redact.scrub(message)
            if scrubbed == redact.WITHHELD:
                target["withheld"] += 1
            shown.append({"at": redact.scrub(line.get("@timestamp"), 40), "line": scrubbed})
    for line in runs:
        target = row(logs.group_of(line.get("@log")))
        target["runs"] = {
            "runs": logs.count(line.get("runs")),
            "avg_ms": logs.count(line.get("avg_ms")),
            "max_ms": logs.count(line.get("max_ms")),
            "max_memory_mb": logs.count(line.get("max_memory_mb")),
            "memory_mb": logs.count(line.get("memory_mb")),
        }
    if baseline is not None:
        daily: dict[str, list[int]] = {}
        for line in baseline:
            key = _function_key(logs.group_of(line.get("@log")), env)
            daily.setdefault(key, []).append(logs.count(line.get("errors")))
        for key, row_ in rows.items():
            days = daily.get(key, [])
            days = days + [0] * max(0, BASELINE_DAYS - len(days))
            row_["typical"] = round(median(days) * when.hours / 24, 1)

    for row_ in rows.values():
        sampled = sum(row_["sampled"].values())
        total = max(row_["errors"], sampled)
        row_["errors"] = total
        row_["estimated"] = total > sampled
        row_["causes"] = {
            key: (round(total * n / sampled) if sampled else 0) for key, n in row_["sampled"].most_common()
        }
        typical = row_["typical"]
        row_["unusual"] = (
            typical is not None and total >= UNUSUAL_MIN and total > UNUSUAL_TIMES * max(typical, 0.5)
        )
    return rows


def _near_limit(row: dict) -> dict:
    """What the REPORT numbers say about the two causes they can speak to."""
    runs = row.get("runs") or {}
    near = {}
    if row.get("timeout_seconds") and runs.get("max_ms"):
        near["time"] = runs["max_ms"] >= NEAR_LIMIT * row["timeout_seconds"] * 1000
    if runs.get("memory_mb") and runs.get("max_memory_mb"):
        near["memory"] = runs["max_memory_mb"] >= NEAR_LIMIT * runs["memory_mb"]
    return near


def _public_row(key: str, row: dict) -> dict:
    return {
        "function": key,
        "errors": row["errors"],
        "estimated": row["estimated"],
        "typical_errors": row["typical"],
        "unusual": row["unusual"],
        "first_seen": redact.scrub(row["first_seen"], 40) if row["first_seen"] else None,
        "last_seen": redact.scrub(row["last_seen"], 40) if row["last_seen"] else None,
        "causes": [
            {"cause": cause, "count": count, "fix_type": BY_KEY[cause].fix_type}
            for cause, count in row["causes"].items()
        ],
        "runs": row["runs"],
        "near_limit": _near_limit(row),
        # Lines a log held: scrubbed, and for the page only. Never spoken, never instructions.
        "untrusted": {"examples": row["examples"]},
    }


def _findings(rows: dict[str, dict], chosen: Scope, when: logs.Window) -> list[dict]:
    """One finding per (function, cause), the biggest first, at most FINDINGS_MAX. The words are
    ours; the suggestion is the catalogue's for `log_<cause>`."""
    found = []
    for key, row in rows.items():
        for cause_key, count in row["causes"].items():
            if count <= 0:
                continue
            found.append((count, key, cause_key, row))
    found.sort(key=lambda item: (-item[0], item[1], item[2]))
    out = []
    for count, key, cause_key, row in found[:FINDINGS_MAX]:
        cause = BY_KEY[cause_key]
        about = "about " if row["estimated"] else ""
        noticed = f"{key} {cause.label}: {about}{count} time{'s' if count != 1 else ''} in {when.words()}"
        item = finding(f"log_{cause_key}", noticed, key, function=key, topic=chosen.topic_name)
        item["root_cause"] = {
            "cause": cause_key,
            "fix_type": cause.fix_type,
            "needs": FIX_WORDS[cause.fix_type],
            "advice": cause.advice,
            "change_where": cause.where,
            "count": count,
            "estimated": row["estimated"],
            "unusual": row["unusual"],
            "near_limit": _near_limit(row),
        }
        out.append(item)
    return out


def _check_yourself(
    groups: list[str], text: dict[str, str], when: logs.Window, region: str | None
) -> list[dict]:
    """The runsheet for exactly what was read: the error lines and the per-function counts, as
    queries to paste, over the same groups and window. Two cards, not four: the baseline and
    REPORT queries are on the function's own dashboard already."""
    shown = ", ".join(groups[:5]) + (f" and {len(groups) - 5} more" if len(groups) > 5 else "")
    where = {
        "log_groups": shown,
        "from": when.start.isoformat(),
        "to": when.end.isoformat(),
    }
    if len(groups) == 1:
        link = architecture.console_url("log_group", groups[0], region)
        if link:
            where["open"] = link
    cards = []
    for name, title in (
        ("sample", "the error lines, newest first"),
        ("totals", "error lines counted per function"),
    ):
        cards.append(
            {
                "kind": HOW_TO,
                "id": f"log-review-{name}",
                "noticed": f"Check it yourself: {title}",
                "where": where,
                "suggestion": {
                    "action": "Open CloudWatch > Logs Insights, select the log groups above, set the "
                    "time range to the one above, paste this and run it",
                    "command": text[name],
                    "what_it_does": "Reads the logs and changes nothing. Logs Insights bills per GB "
                    "scanned, so keep the time range to when it happened.",
                },
            }
        )
    return cards


def _table(rows: dict[str, dict], when: logs.Window) -> dict:
    table_rows = []
    for key, row in sorted(rows.items(), key=lambda item: -item[1]["errors"]):
        if not row["errors"]:
            continue
        for cause_key, count in row["causes"].items():
            cause = BY_KEY[cause_key]
            table_rows.append(
                [
                    key,
                    cause_key.replace("_", " "),
                    count,
                    FIX_WORDS[cause.fix_type],
                    cause.advice,
                    cause.where,
                ]
            )
    return {
        "title": f"Errors in the Lambdas' logs, {when.words()}",
        "columns": ["Function", "Root cause", "Count", "Needs", "What to do", "Where"],
        "rows": table_rows,
    }


def _spoken(rows, chosen: Scope, when: logs.Window, total: int, complete: bool, refused: list) -> str:
    words = []
    if chosen.note:
        words.append(chosen.note)
    if total == 0:
        ran = sum((row.get("runs") or {}).get("runs", 0) for row in rows.values())
        words.append(f"I found no errors in {chosen.words()} in {when.words()}.")
        if ran:
            words.append(f"They ran {ran} times.")
    else:
        erroring = [key for key, row in rows.items() if row["errors"]]
        about = "about " if any(row["estimated"] for row in rows.values()) else ""
        words.append(
            f"In {when.words()} I saw {about}{total} error line{'s' if total != 1 else ''} in "
            f"{chosen.words()}, from {len(erroring)} function{'s' if len(erroring) != 1 else ''}."
        )
        ranked = sorted(
            (
                (count, key, cause)
                for key, row in rows.items()
                for cause, count in row["causes"].items()
                if count
            ),
            reverse=True,
        )
        for index, (count, key, cause_key) in enumerate(ranked[:2]):
            cause = BY_KEY[cause_key]
            lead = "Most came from" if index == 0 else "Then"
            words.append(
                f"{lead} {key}, which {cause.label}, {count} times: "
                f"that looks like {FIX_WORDS[cause.fix_type]}."
            )
        unusual = [key for key, row in rows.items() if row["unusual"]]
        if unusual:
            words.append(f"That is more than usual for {_join(sorted(unusual))}.")
        withheld = sum(row["withheld"] for row in rows.values())
        if withheld:
            words.append(
                f"I held back {withheld} log line{'s' if withheld != 1 else ''} that read like instructions "
                "to me; that can mean someone is probing."
            )
    if chosen.topic_id and chosen.adapter:
        words.append(f"I narrowed it to lines naming the topic or written by its {chosen.adapter} adapter.")
    if not complete:
        words.append("Some of the logs didn't answer in time, so the numbers may be low.")
    if refused:
        words.append(f"I wasn't allowed to read {len(refused)} of the logs.")
    words.append("The detail, and how to check it yourself, is on screen.")
    return " ".join(words)
