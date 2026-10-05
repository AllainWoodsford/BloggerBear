"""The assistant's memory: the list of what it has suggested, and what it was asked to watch.

One table, OPERATOR_SUGGESTIONS_TABLE (hash key `user_id`, range key `item`), and the only thing
in the account the assistant's role may write to (infra/modules/ops-assistant/memory.tf). The
design is docs/enhancements/alexa-plus-operator-assistant-enhancement.md, section 4.

**Rows hold kinds, ids, booleans and timestamps. Never text a model wrote, never a command.**

    suggestion#<kind>#<id>  kind, target_id, first_suggested_at, last_mentioned_at, dismissed,
                            expires_at; for what the logs showed, also first_count and last_count
                            (how many times it was seen) and fix_type (code, settings, ...)
    watch#<kind>#<id>       kind, target_id, watched_since, expires_at

A kind is a key of the catalogue (suggestions.CATALOGUE) or one of WATCH_KINDS; an id passes
ID_PATTERN; the timestamps are ours; a count is a whole number; a fix type is one of FIX_TYPES.
Everything written goes through `_write`, which checks each
value against that list and refuses anything else, so there is nowhere for a title, a review note
or a log line to go. Hostile text that reaches a tool's result cannot get itself remembered. A
finding's words and its command are rebuilt from code and the catalogue every time.

**Who is asking.** Rows are per user: `user_id` is the Cognito subject (`sub`) of the caller. It
is taken from the claims API Gateway's Cognito authorizer verified before the Lambda was invoked,
which reach this app in the request context the Lambda Web Adapter forwards as JSON in the
`x-amzn-request-context` header (access.py reads the caller's address from the same header and
explains why a caller cannot set it: the adapter inserts it, replacing one sent under that name):

    https://github.com/awslabs/aws-lambda-web-adapter docs/guide/src/features/request-context.md
        the adapter forwards the event's requestContext in `x-amzn-request-context`
    https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-mapping-template-reference.html
        `$context.authorizer.claims.property`: "A property of the claims returned from the Amazon
        Cognito user pool after the method caller is successfully authenticated"
    https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html
        an HTTP API's JWT authorizer puts them at `authorizer.jwt.claims` (read too, as access.py
        reads both shapes of the address)

So for the REST API this is deployed behind, the subject is `authorizer.claims.sub`. The bearer
token is never decoded here: it was verified once, by API Gateway, and a second reading of it in
code would be a second place to get that wrong. A subject must look like one (Cognito's is a
UUID) before it is used as a key. With no user id (a local run, a request that did not come
through the adapter) the memory tools say they need a signed-in user and nothing is recorded;
the other tools work as before.

**Recording** (`remember`) happens when a tool returns findings: server.py passes every tool's
result through it. A finding whose suggestion has a command, about an id, gets a row: one per
(kind, id), so finding it again moves `last_mentioned_at` and `expires_at` and adds nothing. A
finding whose row is dismissed is taken out of the result. Findings with no command (alarms,
incidents, spend, a dangling musing) and the one with no id (articles awaiting review) are read
fresh each time and never recorded. A failure here never fails the tool: one log line, and the
result goes back as the tool made it.

**Following up** (`follow_up`) re-runs each open suggestion's check, in code, with the same
functions the tools use (CHECKERS). Fixed: said so, and the row is deleted. Still true: said so,
with how long, and the finding goes back with its suggestion. Gone (the article or topic no
longer exists): the row is deleted and nothing is said. A kind with no checker, or a check that
fails, is reported as still open and is never deleted.

**What the logs showed is remembered too** (LOGGED_KINDS: log_review's `log_<cause>` and
api_errors' `api_<cause>`, about a function). They have no command, but they are the root causes the
operator was told about, so they are "written to the suggestions table": the kind, the function,
how many times it was seen and the fix type. Never a log line. `follow_up` reads that function's
log again over the time since the row was last mentioned and says whether it is still happening
(with the count then and now) or has calmed down, which is a kind that clears by itself.

**Watching a function or a table.** A watched function's log is read (log_review) and the row it
has in the suggestions table, if any, is named: "you asked me to watch research-tick; I flagged it
for timeouts; it's still happening". A watched table (candidate ideas, findings) is sampled
(samples.table_sample) for whether each topic's newest row is on time. The id is the catalogue's
key for it, whatever name it was asked with.

**Every row expires** 30 days after it was last mentioned (DynamoDB TTL on `expires_at`).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from boto3.dynamodb.conditions import Key

from common.dynamo import (
    get_article,
    get_pipeline_config,
    get_table,
    get_topic,
    list_failed_executions,
    list_musings,
    list_pending_moderation,
)
from ops_mcp import account, api_errors, architecture, content, log_review, samples, tools
from ops_mcp.access import REQUEST_CONTEXT_HEADER
from ops_mcp.suggestions import CATALOGUE, ID_PATTERN, finding

TABLE_ENV = "OPERATOR_SUGGESTIONS_TABLE"
RETENTION = timedelta(days=30)
FOLLOW_UP_SPOKEN_LINES = 5

SUGGESTION = "suggestion"
WATCH = "watch"
WATCH_KINDS = ("topic", "function", "incident", "spend", "table")
# What a spend watch can be about: the two figures account.spend reports.
SPEND_IDS = ("ai", "aws")

# A Cognito subject is a UUID. Anything else in the claim is not used as a key.
USER_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

NEEDS_USER = "I need a signed-in user to remember anything, and I can't tell who is asking."
_REQUEST_CONTEXT = REQUEST_CONTEXT_HEADER.decode()

FIXED, OPEN, GONE = "fixed", "open", "gone"

# Kinds that stop being true without anyone doing anything: a failed run drops out of the last
# day, the next scheduled research or daily run happens. follow_up cannot tell that from the
# operator running the suggested command, so it does not say "you fixed": it says they cleared.
# The other kinds only change when a person acts on the article.
# What the logs showed (log_review, api_errors): recorded although it has no command, and it clears by
# itself when the errors stop.
LOGGED_KINDS = frozenset(kind for kind in CATALOGUE if kind.startswith(("log_", "api_")))
FIX_TYPES = frozenset({*log_review.FIX_WORDS, *api_errors.FIX_WORDS})
COUNT_MAX = 10**9
# The longest window follow_up reads a function's log over: logs.MAX_HOURS, a week.
SINCE_MAX_HOURS = 168
# One follow_up or watch_list is one request to a function that stops at 30 seconds, behind an API
# that gives up at 29. Each log read waits for Logs Insights, so a call reads at most LOG_READS_MAX
# logs, each waiting at most LOG_READ_WAIT_SECONDS; past that a row is left open, unchecked this
# time, and checked first next time (the oldest rows are checked first).
LOG_READS_MAX = 2
LOG_READ_WAIT_SECONDS = 8

SELF_CLEARING_KINDS = frozenset({"research_overdue", "no_article_today", "run_failed"}) | LOGGED_KINDS


# --- who is asking -------------------------------------------------------------------------------


def user_id_from_headers(headers) -> str | None:
    """The caller's Cognito subject from the request context the adapter forwards, or None.

    `headers` is what the SDK's Context gives a tool (`ctx.headers`): the HTTP request's headers,
    names in lower case, or None when there is no HTTP request. The header must appear exactly
    once, as in access.py: the adapter sets one, so two means something else built the request.
    """
    if headers is None:
        return None
    getlist = getattr(headers, "getlist", None)
    if getlist is not None:
        values = list(getlist(_REQUEST_CONTEXT))
    else:
        values = [value for name, value in headers.items() if str(name).lower() == _REQUEST_CONTEXT]
    if len(values) != 1:
        return None
    try:
        context = json.loads(values[0])
    except ValueError:
        return None
    authorizer = context.get("authorizer") if isinstance(context, dict) else None
    if not isinstance(authorizer, dict):
        return None
    jwt = authorizer.get("jwt") if isinstance(authorizer.get("jwt"), dict) else {}
    for claims in (authorizer.get("claims"), jwt.get("claims")):  # REST API, then HTTP API
        subject = claims.get("sub") if isinstance(claims, dict) else None
        if isinstance(subject, str) and USER_ID_PATTERN.match(subject):
            return subject
    return None


def _needs_user() -> dict:
    return {"spoken": NEEDS_USER, "findings": []}


# --- the table -----------------------------------------------------------------------------------


def _table():
    return get_table(os.environ[TABLE_ENV])


def _item_key(prefix: str, kind: str, target_id: str) -> str:
    return f"{prefix}#{kind}#{target_id}"


def _expires_at(now: datetime) -> int:
    return int((now + RETENTION).timestamp())


def _is_timestamp(value) -> bool:
    return isinstance(value, str) and tools._parse(value) is not None and len(value) <= 40


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= COUNT_MAX


def _stored_count(value) -> int | None:
    """A count as read back: DynamoDB returns numbers as Decimal. None for anything else."""
    if isinstance(value, Decimal) and value == value.to_integral_value():
        value = int(value)
    return value if _is_count(value) else None


# What each attribute may hold. `_write` refuses a value that fails its check, and an attribute
# that is not here: the rule in the module docstring, enforced where the write happens.
_ALLOWED: dict[str, Callable[[object], bool]] = {
    "kind": lambda value: value in CATALOGUE or value in WATCH_KINDS,
    "target_id": lambda value: isinstance(value, str) and bool(ID_PATTERN.match(value)),
    "first_suggested_at": _is_timestamp,
    "last_mentioned_at": _is_timestamp,
    "watched_since": _is_timestamp,
    "dismissed": lambda value: isinstance(value, bool),
    "expires_at": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "first_count": lambda value: _is_count(value),
    "last_count": lambda value: _is_count(value),
    "fix_type": lambda value: value in FIX_TYPES,
}


def _write(
    user_id: str,
    prefix: str,
    kind: str,
    target_id: str,
    *,
    always: dict,
    if_absent: dict | None = None,
    only_if_exists: bool = False,
) -> None:
    """The one place a row is written: an upsert of (user, prefix, kind, id). `always` is set
    every time, `if_absent` only when the attribute is not there yet (so the first write decides
    it). With `only_if_exists` a row that has gone is not brought back."""
    if not USER_ID_PATTERN.match(user_id) or not ID_PATTERN.match(target_id):
        raise ValueError("not a key the memory table takes")
    if prefix not in (SUGGESTION, WATCH):
        raise ValueError("not a kind of row the memory table holds")
    if kind not in (CATALOGUE if prefix == SUGGESTION else WATCH_KINDS):
        raise ValueError("not a kind the memory table holds")
    always = {"kind": kind, "target_id": target_id, **always}
    for name, value in {**always, **(if_absent or {})}.items():
        if name not in _ALLOWED or not _ALLOWED[name](value):
            raise ValueError(f"the memory table does not hold {name!r} like that")

    names, values, sets = {}, {}, []
    for name, value in always.items():
        names[f"#{name}"], values[f":{name}"] = name, value
        sets.append(f"#{name} = :{name}")
    for name, value in (if_absent or {}).items():
        names[f"#{name}"], values[f":{name}"] = name, value
        sets.append(f"#{name} = if_not_exists(#{name}, :{name})")
    arguments = {
        "Key": {"user_id": user_id, "item": _item_key(prefix, kind, target_id)},
        "UpdateExpression": "SET " + ", ".join(sets),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    if only_if_exists:
        arguments["ConditionExpression"] = "attribute_exists(#item)"
        names["#item"] = "item"
    _table().update_item(**arguments)


def _rows(user_id: str, prefix: str) -> list[dict]:
    """The caller's rows of one sort, and only the caller's: the Query is on their hash key.
    Rows whose kind or id is not one we would write are left out (nothing here writes one)."""
    table, rows, start = _table(), [], None
    while True:
        arguments = {
            "KeyConditionExpression": Key("user_id").eq(user_id) & Key("item").begins_with(f"{prefix}#"),
            "ConsistentRead": True,
        }
        if start:
            arguments["ExclusiveStartKey"] = start
        page = table.query(**arguments)
        rows.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    kinds = CATALOGUE if prefix == SUGGESTION else WATCH_KINDS
    return [
        row
        for row in rows
        if row.get("kind") in kinds
        and _ALLOWED["target_id"](row.get("target_id"))
        and row.get("item") == _item_key(prefix, row["kind"], row["target_id"])
    ]


def _delete(user_id: str, prefix: str, kind: str, target_id: str) -> None:
    _table().delete_item(Key={"user_id": user_id, "item": _item_key(prefix, kind, target_id)})


# --- recording what a tool found -----------------------------------------------------------------


def _has_key(found) -> bool:
    """A finding that can have a row at all: a catalogue kind, about an id we trust."""
    return (
        isinstance(found, dict)
        and isinstance(found.get("kind"), str)
        and found["kind"] in CATALOGUE
        and isinstance(found.get("id"), str)
        and bool(ID_PATTERN.match(found["id"]))
    )


def _recordable(found) -> bool:
    """A finding whose suggestion has a command, about an id, or a root cause the logs showed
    (LOGGED_KINDS). The command is asked of the catalogue, not read from the finding."""
    if not _has_key(found):
        return False
    return CATALOGUE[found["kind"]].arguments is not None or found["kind"] in LOGGED_KINDS


def _seen(found) -> tuple[int | None, str | None]:
    """How many times a logged finding was seen, and its fix type, if the finding says and both are
    shaped as the table holds them. Anything else is left out, not written."""
    cause = found.get("root_cause") if isinstance(found, dict) else None
    if not isinstance(cause, dict) or found.get("kind") not in LOGGED_KINDS:
        return None, None
    count = cause.get("count")
    count = count if _is_count(count) else None
    fix_type = cause.get("fix_type") if cause.get("fix_type") in FIX_TYPES else None
    return count, fix_type


def remember(user_id: str | None, result, *, now: datetime | None = None):
    """A tool's result on its way out: its findings recorded, and the dismissed ones removed.
    Never raises, and with no user id does nothing."""
    if user_id is None or not isinstance(result, dict) or not result.get("findings"):
        return result
    try:
        now = tools._now(now)
        dismissed = {
            (row["kind"], row["target_id"]) for row in _rows(user_id, SUGGESTION) if row.get("dismissed")
        }
        kept, written, noted = [], set(), 0
        for found in result["findings"]:
            key = (found["kind"], found["id"]) if _has_key(found) else None
            if key in dismissed:
                # Still true, so the dismissal is kept alive; it is not mentioned, so only its
                # expiry moves.
                if key not in written:
                    _write(user_id, SUGGESTION, *key, always={"expires_at": _expires_at(now)})
                    written.add(key)
                continue
            kept.append(found)
            if key is not None and key not in written and _recordable(found):
                count, fix_type = _seen(found)
                _mention(user_id, *key, now, count=count, fix_type=fix_type)
                written.add(key)
                noted += 1
        out = result if len(kept) == len(result["findings"]) else {
            **result,
            "findings": kept,
            "findings_dismissed": len(result["findings"]) - len(kept),
        }
        # Said so the assistant can tell the operator its findings are written down for next time.
        return {**out, "remembered": noted} if noted else out
    except Exception as exc:  # noqa: BLE001 - the tool's answer matters more than remembering it
        print(f"ops_memory: could not record findings ({type(exc).__name__})")
        return result


def _mention(
    user_id: str,
    kind: str,
    target_id: str,
    now: datetime,
    *,
    only_if_exists: bool = False,
    count: int | None = None,
    fix_type: str | None = None,
) -> None:
    always = {"last_mentioned_at": now.isoformat(), "expires_at": _expires_at(now)}
    if_absent = {"first_suggested_at": now.isoformat(), "dismissed": False}
    if count is not None:
        always["last_count"] = count
        if_absent["first_count"] = count
    if fix_type is not None:
        always["fix_type"] = fix_type
    _write(
        user_id,
        SUGGESTION,
        kind,
        target_id,
        always=always,
        if_absent=if_absent,
        only_if_exists=only_if_exists,
    )


# --- follow_up: is what was suggested still true? ------------------------------------------------


class Check(NamedTuple):
    state: str  # FIXED, OPEN or GONE
    finding: dict | None = None  # for OPEN: the finding as its tool would return it now
    topic: str | None = None  # the topic's name, for speech
    count: int | None = None  # for a logged kind: how many times it was seen in the window read


class _Sources:
    """What the checkers read, each read once per follow_up however many rows need it."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self._read: dict[str, object] = {}
        # The row being checked: the logged kinds read the log since it was last mentioned.
        self.row: dict = {}

    def _once(self, name: str, read: Callable[[], object]):
        if name not in self._read:
            self._read[name] = read()
        return self._read[name]

    @property
    def config(self):
        return self._once("config", get_pipeline_config)

    @property
    def failures(self):
        return self._once("failures", list_failed_executions)

    @property
    def pending(self):
        return self._once("pending", list_pending_moderation)

    @property
    def musings(self):
        return self._once("musings", lambda: list_musings(content.CONTENT_MAX_MUSINGS))

    def hours_since_mentioned(self) -> int:
        """The window a logged kind is read over: since its row was last mentioned, from one hour
        to SINCE_MAX_HOURS."""
        since = tools._parse(self.row.get("last_mentioned_at"))
        if since is None:
            return 24
        hours = int((self.now - since).total_seconds() // 3600) + 1
        return max(1, min(SINCE_MAX_HOURS, hours))

    def _log_read(self, name: str, read: Callable[[], dict]) -> dict:
        """A log read, at most LOG_READS_MAX per call (see it); one over the budget raises, and
        the row it was for is left open, unchecked this time."""
        if name not in self._read:
            reads = self._read.setdefault("#log-reads", [0])
            if reads[0] >= LOG_READS_MAX:
                raise RuntimeError("the log read budget for this call is spent")
            reads[0] += 1
        return self._once(name, read)

    def function_log(self, function_key: str, hours: int) -> dict:
        return self._log_read(
            f"log#{function_key}#{hours}",
            lambda: log_review.review(
                function=function_key, hours=hours, now=self.now, wait_seconds=LOG_READ_WAIT_SECONDS
            ),
        )

    def api_log(self, api_key: str, hours: int) -> dict:
        return self._log_read(
            f"api#{api_key}#{hours}",
            lambda: api_errors.api_errors(
                api=api_key, hours=hours, now=self.now, wait_seconds=LOG_READ_WAIT_SECONDS
            ),
        )


def _article_topic(article: dict) -> str:
    topic_id = article.get("topic_id")
    return tools._topic_label(get_topic(topic_id), topic_id) if topic_id else "an unknown topic"


def _topic_checker(kind: str) -> Callable[[str, _Sources], Check]:
    """For the kinds pipeline_health finds: run its check for the one topic again."""

    def check(topic_id: str, sources: _Sources) -> Check:
        topic = get_topic(topic_id)
        if topic is None:
            return Check(GONE)
        row, findings = tools._topic_check(topic, sources.config, sources.failures, sources.now)
        still = next((found for found in findings if found["kind"] == kind), None)
        return Check(OPEN if still else FIXED, still, row["name"])

    return check


def _check_draft_truncated(article_id: str, sources: _Sources) -> Check:
    """Still open while the article is in the inbox, held because its draft was cut short."""
    article = get_article(article_id)
    if article is None:
        return Check(GONE)
    name = _article_topic(article)
    held = any(
        item.get("article_id") == article_id and "draft_truncated" in tools._hold_kinds(item)
        for item in sources.pending
    )
    return Check(OPEN, tools._truncated_finding(name, article_id), name) if held else Check(FIXED, None, name)


def _content_checker(kind: str) -> Callable[[str, _Sources], Check]:
    """For the kinds content_checks finds. An article that is no longer published is fixed: the
    suggested rewrite takes it down, with its musings, when the rewrite is ready."""

    def check(article_id: str, sources: _Sources) -> Check:
        article = get_article(article_id)
        if article is None:
            return Check(GONE)
        name = _article_topic(article)
        if article.get("status") != "published":
            return Check(FIXED, None, name)
        if kind == "musing_no_text":
            still = any(
                musing.get("article_id") == article_id and content._musing_has_no_text(musing)
                for musing in sources.musings
            )
        else:
            # Raises if the body cannot be read, which follow_up reports as still open: a check
            # that could not be made is never a fix.
            still = content._title_and_body_problem(article) == kind
        return (
            Check(OPEN, content._article_finding(kind, name, article_id), name)
            if still
            else Check(FIXED, None, name)
        )

    return check


def _read_complete(result: dict) -> dict:
    """A log read follow_up can judge from, or an error: a refusal or an incomplete read is never
    taken for "it has calmed down"."""
    if not isinstance(result, dict) or result.get("complete") is not True or result.get("refused"):
        raise RuntimeError("the log could not be read completely")
    return result


def _still(kind: str, function_key: str, count: int, before, label: str) -> dict:
    """The finding for a logged kind that is still happening: fixed words, the catalogue's
    suggestion, and the count then and now."""
    counted = isinstance(before, int) and not isinstance(before, bool)
    earlier = f" ({before} when I flagged it)" if counted else ""
    if kind.startswith("api_"):
        what = f"{count} {label} on {function_key}'s API since I last looked"
    else:
        what = f"{function_key} {label}, {count} time{'s' if count != 1 else ''} since I last looked"
    item = finding(kind, f"Still happening: {what}{earlier}", function_key, function=function_key)
    item["root_cause"] = {"count": count, "count_before": before if earlier else None}
    return item


def _log_checker(kind: str) -> Callable[[str, _Sources], Check]:
    """For log_review's kinds: read the function's log since the row was last mentioned, and look
    for the same root cause."""
    cause = log_review.BY_KEY[kind.removeprefix("log_")]

    def check(function_key: str, sources: _Sources) -> Check:
        try:
            architecture.by_key("function", function_key)
        except KeyError:
            return Check(GONE)
        result = _read_complete(sources.function_log(function_key, sources.hours_since_mentioned()))
        row = next((f for f in result.get("functions") or [] if f.get("function") == function_key), None)
        count = next((c["count"] for c in (row or {}).get("causes") or [] if c.get("cause") == cause.key), 0)
        if count:
            before = _stored_count(sources.row.get("last_count"))
            still = _still(kind, function_key, count, before, cause.label)
            return Check(OPEN, still, function_key, count)
        return Check(FIXED, None, function_key, 0)

    return check


def _api_checker(kind: str) -> Callable[[str, _Sources], Check]:
    """For api_errors' kinds: read the API's access log since the row was last mentioned. The row's
    id is the Lambda behind the API (api_errors.APIS)."""
    cause = api_errors.CAUSES[kind.removeprefix("api_")]

    def check(function_key: str, sources: _Sources) -> Check:
        api = next((a for a in api_errors.APIS.values() if a.function == function_key), None)
        if api is None:
            return Check(GONE)
        result = _read_complete(sources.api_log(api.key, sources.hours_since_mentioned()))
        row = next((r for r in result.get("by_api") or [] if r.get("api") == api.key), None)
        count = next((c["count"] for c in (row or {}).get("causes") or [] if c.get("cause") == cause.key), 0)
        if count:
            before = _stored_count(sources.row.get("last_count"))
            still = _still(kind, function_key, count, before, cause.label)
            return Check(OPEN, still, function_key, count)
        return Check(FIXED, None, function_key, 0)

    return check


# Kind -> its check. Every catalogue kind that has a command and is about an id has one
# (tests/test_ops_mcp_memory.py holds that), so a new kind without one fails the build.
def _check_security_incident(event_id: str, sources: _Sources) -> Check:
    """Fixed once the incident is no longer open: acknowledged or resolved (the suggested
    command), or aged out."""
    row = _incident(event_id, now=sources.now)
    if row is None:
        return Check(FIXED)
    return Check(
        OPEN,
        finding(
            "security_incident",
            f"A security incident is still open: {account._incident_words(row)}",
            event_id,
            category=row["category"],
            source=row["source"],
        ),
    )


def _check_firewall_spike(log_group: str, sources: _Sources) -> Check:
    """A spike is a moment, and whether it deserved an incident was the operator's call: it is
    forgotten, not chased. If the firewall is still busy, the next briefing says so afresh."""
    return Check(GONE)


CHECKERS: dict[str, Callable[[str, _Sources], Check]] = {
    "draft_truncated": _check_draft_truncated,
    "security_incident": _check_security_incident,
    "firewall_spike": _check_firewall_spike,
    "research_overdue": _topic_checker("research_overdue"),
    "no_article_today": _topic_checker("no_article_today"),
    "run_failed": _topic_checker("run_failed"),
    "musing_no_text": _content_checker("musing_no_text"),
    "title_markup": _content_checker("title_markup"),
    "body_code_fence": _content_checker("body_code_fence"),
    "title_markup_and_body_code_fence": _content_checker("title_markup_and_body_code_fence"),
    **{kind: _log_checker(kind) for kind in LOGGED_KINDS if kind.startswith("log_")},
    **{kind: _api_checker(kind) for kind in LOGGED_KINDS if kind.startswith("api_")},
}


def _unchecked(kind: str, target_id: str) -> Check:
    """Still open, as far as anyone knows: the words are fixed, the suggestion is the catalogue's."""
    return Check(OPEN, finding(kind, "Something I suggested earlier has not been checked again", target_id))


def follow_up(user_id: str | None, *, now: datetime | None = None) -> dict:
    """What became of the caller's open suggestions: which are fixed or have cleared (and are
    forgotten), which are still waiting and for how long."""
    if user_id is None:
        return _needs_user()
    now = tools._now(now)
    sources = _Sources(now)
    fixed, cleared, still_open, findings = [], [], [], []

    rows = [row for row in _rows(user_id, SUGGESTION) if not row.get("dismissed")]
    rows.sort(key=lambda row: str(row.get("first_suggested_at") or ""))  # the oldest first
    for row in rows:
        kind, target_id = row["kind"], row["target_id"]
        checker = CHECKERS.get(kind)
        sources.row = row
        try:
            check = checker(target_id, sources) if checker else _unchecked(kind, target_id)
        except Exception as exc:  # noqa: BLE001 - a check that fails leaves the suggestion open
            print(f"ops_memory: could not re-check a {kind} suggestion ({type(exc).__name__})")
            check = _unchecked(kind, target_id)

        since = tools._parse(row.get("first_suggested_at"))
        entry = {
            "kind": kind,
            "id": target_id,
            "topic": check.topic,
            "first_suggested_at": since.isoformat() if since else None,
        }
        try:
            if check.state == OPEN:
                _mention(user_id, kind, target_id, now, only_if_exists=True, count=check.count)
            else:
                _delete(user_id, SUGGESTION, kind, target_id)
        except Exception as exc:  # noqa: BLE001 - the answer is still right; the row is tried again next time
            print(f"ops_memory: could not update a {kind} suggestion ({type(exc).__name__})")
        if check.state == FIXED:
            (cleared if kind in SELF_CLEARING_KINDS else fixed).append(entry)
        elif check.state == OPEN:
            counted = {}
            if kind in LOGGED_KINDS and check.count is not None:
                counted = {"count_now": check.count, "count_before": _stored_count(row.get("last_count"))}
            still_open.append({**entry, "waiting": tools._age(since, now), **counted})
            findings.append(check.finding)

    return {
        "spoken": _follow_up_spoken(fixed, still_open, cleared),
        "findings": findings,
        "fixed": fixed,
        "cleared": cleared,
        "open": still_open,
        "as_of": now.isoformat(),
    }


def _things(count: int) -> str:
    return f"{count} thing{'s' if count != 1 else ''}"


def _about(entries: list[dict]) -> str:
    names = list(dict.fromkeys(entry["topic"] for entry in entries if entry["topic"]))
    return f", for {tools._join(names[:FOLLOW_UP_SPOKEN_LINES])}" if names else ""


def _trend(entry: dict) -> str:
    """For something the logs showed and still show: whether it is easing or getting worse, from
    the count when it was flagged and the count since. Empty for anything else."""
    now, before = entry.get("count_now"), entry.get("count_before")
    if not isinstance(now, int) or not isinstance(before, int) or isinstance(before, bool) or before <= 0:
        return ""
    if now > before * 1.5:
        return ", and it's getting worse"
    if now * 2 < before:
        return ", though it's easing off"
    return ", about as often as before"


def _follow_up_spoken(fixed: list[dict], still_open: list[dict], cleared: list[dict] | None = None) -> str:
    """Fixed first, then what cleared, then what is waiting and for how long. Counts and topic
    names only. "You fixed" is kept for what only a person could have changed."""
    cleared = cleared or []
    if not fixed and not still_open and not cleared:
        return "I have no open suggestions to follow up."

    sentences = []
    if fixed:
        sentences.append(f"You fixed {_things(len(fixed))} I suggested{_about(fixed)}.")
    calmed = [entry for entry in cleared if entry["kind"] in LOGGED_KINDS]
    cleared = [entry for entry in cleared if entry["kind"] not in LOGGED_KINDS]
    if cleared:
        count = len(cleared)
        sentences.append(
            f"{_things(count).capitalize()} I flagged {'have' if count != 1 else 'has'} "
            f"cleared{_about(cleared)}."
        )
    if calmed:
        count = len(calmed)
        sentences.append(
            f"{_things(count).capitalize()} I saw in the logs {'have' if count != 1 else 'has'} "
            f"calmed down{_about(calmed)}."
        )
    if still_open:
        count = len(still_open)
        lines = [
            f"{entry['topic'] or 'one'} for {entry['waiting']}{_trend(entry)}"
            for entry in still_open[:FOLLOW_UP_SPOKEN_LINES]
        ]
        rest = count - len(lines)
        more = f", and {rest} more" if rest > 0 else ""
        sentences.append(
            f"{_things(count)} I suggested {'are' if count != 1 else 'is'} still waiting: "
            f"{tools._join(lines)}{more}. The fixes are on screen again."
        )
    else:
        sentences.append("Nothing else I suggested is waiting.")
    return " ".join(sentences)


# --- dismiss -------------------------------------------------------------------------------------


def dismiss(user_id: str | None, kind: str, target_id: str, *, now: datetime | None = None) -> dict:
    """ "Leave that one": the suggestion is not raised again. The row is kept, marked dismissed
    (deleting it would only have the finding suggested afresh at the next check), and made if it
    is not there."""
    if user_id is None:
        return _needs_user()
    if kind not in CATALOGUE or not isinstance(target_id, str) or not ID_PATTERN.match(target_id):
        return {"spoken": "That isn't a suggestion I can set aside.", "findings": [], "dismissed": False}
    now = tools._now(now)
    _write(
        user_id,
        SUGGESTION,
        kind,
        target_id,
        always={"dismissed": True, "expires_at": _expires_at(now)},
        if_absent={"first_suggested_at": now.isoformat(), "last_mentioned_at": now.isoformat()},
    )
    return {
        "spoken": "Done. I won't raise that one again.",
        "findings": [],
        "dismissed": True,
        "kind": kind,
        "id": target_id,
    }


# --- watch items ---------------------------------------------------------------------------------


def _catalogue_key(kind: str, name: str) -> str | None:
    """A function's or a table's key in the architecture catalogue (research-tick, candidate-ideas),
    from any name architecture.py understands, if it is deployed in this environment."""
    resolved = architecture.resolve(name, kind=kind)
    if resolved.asked_env is not None and resolved.asked_env not in architecture.ENVIRONMENTS:
        return None
    env = architecture.environment()
    found = [c for c in resolved.matches if c.kind == kind and architecture.exists_in(c, env)]
    return found[0].key if found else None


def _normalized(kind: str, target_id) -> str:
    """The id a watch is kept under: a function's or a table's catalogue key; anything else as
    given."""
    if kind in ("function", "table") and isinstance(target_id, str):
        return _catalogue_key(kind, target_id) or target_id
    return target_id


def _watchable(kind: str, target_id) -> str | None:
    """Why `target_id` cannot be watched as a `kind`, in words for speech, or None if it can.
    Checked against what exists where one read answers it."""
    if kind not in WATCH_KINDS:
        return "I can watch a topic, a function, an incident, spend or a table."
    if kind in ("function", "table") and isinstance(target_id, str) and len(target_id) <= 200:
        if _catalogue_key(kind, target_id) is None:
            return f"I don't know a {kind} by that name in this environment."
        if kind == "table" and _catalogue_key(kind, target_id) in samples.NEVER_SAMPLED:
            return "That table is one I never read."
        return None
    if not isinstance(target_id, str) or not ID_PATTERN.match(target_id):
        return "That isn't an id I can watch."
    if kind == "topic" and get_topic(target_id) is None:
        return "I can't find a topic with that id."
    if kind == "spend" and target_id not in SPEND_IDS:
        return "For spend I can watch ai or aws."
    # The AWS bill is the whole account's, and only an assistant told it may report account-wide
    # figures has it (account.account_wide_data). Refused here, so nobody is told "I'll keep an
    # eye on it" about a figure this environment's assistant will never read.
    if kind == "spend" and target_id == "aws" and not account.account_wide_data():
        return f"{account.BILL_NOT_AVAILABLE} For spend I can watch ai."
    if kind == "incident" and _incident(target_id) is None:
        return "I can't find an open incident with that id."
    return None


def _incident(event_id: str, *, now: datetime | None = None) -> dict | None:
    """The open incident with this id, as security_events returns it, or None."""
    rows = account.security_events(account.SECURITY_MAX_DAYS, now=now).get("incidents") or []
    return next((row for row in rows if row.get("event_id") == event_id), None)


def watch(user_id: str | None, kind: str, target_id: str, *, now: datetime | None = None) -> dict:
    if user_id is None:
        return _needs_user()
    refusal = _watchable(kind, target_id)
    if refusal:
        return {"spoken": refusal, "findings": [], "watching": False}
    target_id = _normalized(kind, target_id)
    now = tools._now(now)
    _write(
        user_id,
        WATCH,
        kind,
        target_id,
        always={"expires_at": _expires_at(now)},
        if_absent={"watched_since": now.isoformat()},
    )
    return {
        "spoken": "I'll keep an eye on it.",
        "findings": [],
        "watching": True,
        "kind": kind,
        "id": target_id,
    }


def unwatch(user_id: str | None, kind: str, target_id: str) -> dict:
    if user_id is None:
        return _needs_user()
    target_id = _normalized(kind, target_id) if kind in WATCH_KINDS else target_id
    if kind not in WATCH_KINDS or not isinstance(target_id, str) or not ID_PATTERN.match(target_id):
        return {"spoken": "That isn't something I could have been watching.", "findings": []}
    _delete(user_id, WATCH, kind, target_id)
    return {
        "spoken": "I've stopped watching it.",
        "findings": [],
        "watching": False,
        "kind": kind,
        "id": target_id,
    }


def _flagged_words(flagged: list[dict], now_causes: dict[str, int], now: datetime) -> str:
    """What the suggestions table says was wrong with a watched function, and whether it still is:
    "; I flagged it for hitting its time limit 2 days ago, and it's still happening"."""
    if not flagged:
        return ""
    row = max(flagged, key=lambda r: str(r.get("last_mentioned_at") or ""))
    kind = row["kind"]
    label = (
        log_review.BY_KEY[kind.removeprefix("log_")].label
        if kind.startswith("log_")
        else api_errors.CAUSES[kind.removeprefix("api_")].label
    )
    since = tools._parse(row.get("first_suggested_at"))
    still = now_causes.get(kind, 0)
    when = tools._age(since, now) if since else "a while"
    if kind.startswith("api_"):
        # Its API's errors are not in the function's own log: said as flagged, not re-judged here.
        return f"; {when} ago I flagged {label} on its API"
    tail = "and it's still happening" if still else "and that has calmed down"
    return f"; I flagged that it {label}, {when} ago, {tail}"


def _watched_function(target_id: str, now: datetime, flagged: list[dict]) -> tuple[dict, str]:
    result = log_review.review(function=target_id, hours=24, now=now, wait_seconds=LOG_READ_WAIT_SECONDS)
    if result.get("complete") is not True or result.get("refused"):
        return {"state": "unreadable"}, f"{target_id}, whose log I couldn't read completely"
    row = next((f for f in result.get("functions") or [] if f.get("function") == target_id), None) or {}
    errors = int(row.get("errors") or 0)
    causes = {f"log_{c['cause']}": c["count"] for c in row.get("causes") or []}
    state = {
        "state": "read",
        "errors": errors,
        "causes": row.get("causes") or [],
        "unusual": row.get("unusual"),
    }
    if errors:
        top = log_review.BY_KEY[(row.get("causes") or [{"cause": "other"}])[0]["cause"]]
        lines = f"{errors} error line{'s' if errors != 1 else ''}"
        words = f"{target_id} had {lines} in the last day, mostly that it {top.label}"
    else:
        words = f"{target_id} had no errors in the last day"
    return state, words + _flagged_words(flagged, causes, now)


def _watched_table(target_id: str, now: datetime) -> tuple[dict, str]:
    result = samples.table_sample(target_id, now=now)
    if not result.get("read"):
        return {"state": "unreadable"}, f"the {target_id} table, which I couldn't read"
    fresh = result.get("freshness") or []
    late = [entry for entry in fresh if not entry.get("on_time")]
    state = {"state": "read", "freshness": fresh, "rows_shown": result.get("rows_shown")}
    if not fresh:
        return state, f"the {target_id} table is readable; it has no on-time rule to check"
    words = f"{target_id} is written on time for {len(fresh) - len(late)} of {len(fresh)} topics"
    if late:
        words += f", and late for {tools._join([entry['topic'] for entry in late[:3]])}"
    return state, words


def _watched_state(
    kind: str, target_id: str, now: datetime, flagged: list[dict] | None = None
) -> tuple[dict, str]:
    """A watched item's state now, from the tool that reads it, and a few words for speech."""
    if kind == "topic":
        rows = tools.pipeline_health(target_id, now=now).get("topics") or []
        if not rows:
            return {"state": "gone"}, "a topic that no longer exists"
        row = rows[0]
        research = "research is late" if row["research"]["state"] == "late" else "research is on time"
        article = {
            "published": "published",
            "held": "has an article held for review",
            "rejected": "had its article rejected",
            "failed": "failed its daily run",
            "none": "has no article in the last day",
        }[row["article"]["state"]]
        state = {"state": "read", "research": row["research"], "article": row["article"]}
        return state, f"{row['name']} {article}, and {research}"
    if kind == "spend":
        # A watch on the bill kept from before this environment stopped reporting it: said to be
        # unavailable, never "not unusual", which nothing here could know.
        if target_id == "aws" and not account.account_wide_data():
            return {"state": "not_available"}, "the AWS bill, which is not available from this environment"
        figures = account.spend("week", now=now).get(target_id) or {}
        label = "AI spend" if target_id == "ai" else "the AWS bill"
        unusual = bool(figures.get("unusual"))
        words = "is more than twice a typical week" if unusual else "is not unusual this week"
        return {"state": "read", **figures}, f"{label} {words}"
    if kind == "incident":
        row = _incident(target_id, now=now)
        if row is None:
            return {"state": "not_open"}, "an incident that is no longer open"
        state = {key: row[key] for key in ("category", "severity", "source", "requests", "last_seen")}
        return {"state": "read", **state}, f"a {row['severity']}-severity incident, still open"
    if kind == "table":
        return _watched_table(target_id, now)
    return _watched_function(target_id, now, flagged or [])


def watch_list(user_id: str | None, *, now: datetime | None = None) -> dict:
    """What the caller asked to have watched, and how each is now. Reading the list keeps it:
    each item's expiry moves 30 days out again."""
    if user_id is None:
        return _needs_user()
    now = tools._now(now)
    items, words = [], []
    # What the suggestions table holds about each function: what was flagged in its logs.
    flagged: dict[str, list[dict]] = {}
    watched = sorted(_rows(user_id, WATCH), key=lambda row: row["item"])
    if any(row["kind"] == "function" for row in watched):
        for suggestion in _rows(user_id, SUGGESTION):
            if suggestion["kind"] in LOGGED_KINDS and not suggestion.get("dismissed"):
                flagged.setdefault(suggestion["target_id"], []).append(suggestion)
    function_reads = 0
    for row in watched:
        kind, target_id = row["kind"], row["target_id"]
        try:
            if kind == "function":
                function_reads += 1
                if function_reads > LOG_READS_MAX:
                    # Each one is a Logs Insights read: past the budget it is listed, not read.
                    raise RuntimeError("the log read budget for this call is spent")
            state, said = _watched_state(kind, target_id, now, flagged.get(target_id))
        except Exception as exc:  # noqa: BLE001 - one item that can't be read must not hide the rest
            print(f"ops_memory: could not read a watched {kind} ({type(exc).__name__})")
            state, said = {"state": "unreadable"}, f"a {kind} I could not read"
        try:
            _write(
                user_id, WATCH, kind, target_id, always={"expires_at": _expires_at(now)}, only_if_exists=True
            )
        except Exception as exc:  # noqa: BLE001 - the list is still right
            print(f"ops_memory: could not keep a watched {kind} ({type(exc).__name__})")
        since = tools._parse(row.get("watched_since"))
        items.append(
            {
                "kind": kind,
                "id": target_id,
                "watched_since": since.isoformat() if since else None,
                "now": state,
            }
        )
        words.append(said)

    if not items:
        spoken = "You haven't asked me to watch anything."
    else:
        shown = words[:FOLLOW_UP_SPOKEN_LINES]
        rest = len(words) - len(shown)
        more = f" And {rest} more." if rest > 0 else ""
        spoken = f"You asked me to watch {_things(len(items))}. {'. '.join(shown)}.{more}"
    return {"spoken": spoken, "findings": [], "watching": items, "as_of": now.isoformat()}
