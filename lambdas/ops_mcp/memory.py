"""The assistant's memory: the list of what it has suggested, and what it was asked to watch.

One table, OPERATOR_SUGGESTIONS_TABLE (hash key `user_id`, range key `item`), and the only thing
in the account the assistant's role may write to (infra/modules/ops-assistant/memory.tf). The
design is docs/enhancements/alexa-plus-operator-assistant-enhancement.md, section 4.

**Rows hold kinds, ids, booleans and timestamps. Never text a model wrote, never a command.**

    suggestion#<kind>#<id>  kind, target_id, first_suggested_at, last_mentioned_at, dismissed,
                            expires_at
    watch#<kind>#<id>       kind, target_id, watched_since, expires_at

A kind is a key of the catalogue (suggestions.CATALOGUE) or one of WATCH_KINDS; an id passes
ID_PATTERN; the timestamps are ours. Everything written goes through `_write`, which checks each
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

**Every row expires** 30 days after it was last mentioned (DynamoDB TTL on `expires_at`).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
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
from ops_mcp import account, content, tools
from ops_mcp.access import REQUEST_CONTEXT_HEADER
from ops_mcp.suggestions import CATALOGUE, ID_PATTERN, finding

TABLE_ENV = "OPERATOR_SUGGESTIONS_TABLE"
RETENTION = timedelta(days=30)
FOLLOW_UP_SPOKEN_LINES = 5

SUGGESTION = "suggestion"
WATCH = "watch"
WATCH_KINDS = ("topic", "function", "incident", "spend")
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
SELF_CLEARING_KINDS = frozenset({"research_overdue", "no_article_today", "run_failed"})


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
    """A finding whose suggestion has a command, about an id. The command is asked of the
    catalogue, not read from the finding."""
    return _has_key(found) and CATALOGUE[found["kind"]].arguments is not None


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
        kept, written = [], set()
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
                _mention(user_id, *key, now)
                written.add(key)
        if len(kept) == len(result["findings"]):
            return result
        return {**result, "findings": kept, "findings_dismissed": len(result["findings"]) - len(kept)}
    except Exception as exc:  # noqa: BLE001 - the tool's answer matters more than remembering it
        print(f"ops_memory: could not record findings ({type(exc).__name__})")
        return result


def _mention(user_id: str, kind: str, target_id: str, now: datetime, *, only_if_exists: bool = False) -> None:
    _write(
        user_id,
        SUGGESTION,
        kind,
        target_id,
        always={"last_mentioned_at": now.isoformat(), "expires_at": _expires_at(now)},
        if_absent={"first_suggested_at": now.isoformat(), "dismissed": False},
        only_if_exists=only_if_exists,
    )


# --- follow_up: is what was suggested still true? ------------------------------------------------


class Check(NamedTuple):
    state: str  # FIXED, OPEN or GONE
    finding: dict | None = None  # for OPEN: the finding as its tool would return it now
    topic: str | None = None  # the topic's name, for speech


class _Sources:
    """What the checkers read, each read once per follow_up however many rows need it."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self._read: dict[str, object] = {}

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


# Kind -> its check. Every catalogue kind that has a command and is about an id has one
# (tests/test_ops_mcp_memory.py holds that), so a new kind without one fails the build.
CHECKERS: dict[str, Callable[[str, _Sources], Check]] = {
    "draft_truncated": _check_draft_truncated,
    "research_overdue": _topic_checker("research_overdue"),
    "no_article_today": _topic_checker("no_article_today"),
    "run_failed": _topic_checker("run_failed"),
    "musing_no_text": _content_checker("musing_no_text"),
    "title_markup": _content_checker("title_markup"),
    "body_code_fence": _content_checker("body_code_fence"),
    "title_markup_and_body_code_fence": _content_checker("title_markup_and_body_code_fence"),
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
                _mention(user_id, kind, target_id, now, only_if_exists=True)
            else:
                _delete(user_id, SUGGESTION, kind, target_id)
        except Exception as exc:  # noqa: BLE001 - the answer is still right; the row is tried again next time
            print(f"ops_memory: could not update a {kind} suggestion ({type(exc).__name__})")
        if check.state == FIXED:
            (cleared if kind in SELF_CLEARING_KINDS else fixed).append(entry)
        elif check.state == OPEN:
            still_open.append({**entry, "waiting": tools._age(since, now)})
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


def _follow_up_spoken(fixed: list[dict], still_open: list[dict], cleared: list[dict] | None = None) -> str:
    """Fixed first, then what cleared, then what is waiting and for how long. Counts and topic
    names only. "You fixed" is kept for what only a person could have changed."""
    cleared = cleared or []
    if not fixed and not still_open and not cleared:
        return "I have no open suggestions to follow up."
    sentences = []
    if fixed:
        sentences.append(f"You fixed {_things(len(fixed))} I suggested{_about(fixed)}.")
    if cleared:
        count = len(cleared)
        sentences.append(
            f"{_things(count).capitalize()} I flagged {'have' if count != 1 else 'has'} "
            f"cleared{_about(cleared)}."
        )
    if still_open:
        count = len(still_open)
        lines = [
            f"{entry['topic'] or 'one'} for {entry['waiting']}"
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


def _watchable(kind: str, target_id) -> str | None:
    """Why `target_id` cannot be watched as a `kind`, in words for speech, or None if it can.
    Checked against what exists where one read answers it."""
    if kind not in WATCH_KINDS:
        return "I can watch a topic, a function, an incident or spend."
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
    return None  # a function: no list of them is one read away, so its id only has to look like one


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


def _watched_state(kind: str, target_id: str, now: datetime) -> tuple[dict, str]:
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
    # A function: the tool that would read its log (log_review) is not built yet.
    return {"state": "not_checked"}, "a function I can't check yet"


def watch_list(user_id: str | None, *, now: datetime | None = None) -> dict:
    """What the caller asked to have watched, and how each is now. Reading the list keeps it:
    each item's expiry moves 30 days out again."""
    if user_id is None:
        return _needs_user()
    now = tools._now(now)
    items, words = [], []
    for row in sorted(_rows(user_id, WATCH), key=lambda row: row["item"]):
        kind, target_id = row["kind"], row["target_id"]
        try:
            state, said = _watched_state(kind, target_id, now)
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
