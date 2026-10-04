"""The latest briefing per user: how Alexa+ (or any MCP client) gets the Strands agent's work
without waiting for it (docs/enhancements/alexa-plus.md, section 4.3).

Alexa+ asks for an answer in well under a second; a briefing, the agent following leads across the
MCP tools, takes ten to twenty-five. So the two are split:

- `start(...)` (the MCP tool `start_briefing`) marks a briefing as running for the caller and
  invokes the agent's Lambda asynchronously, passing the caller's own bearer token so the agent
  calls the tools as them, exactly as `POST /ask` does. It returns at once.
- `record(...)` (the agent, when it has answered) writes the answer, the findings and the tool
  calls. Every briefing asked on the page is written the same way.
- `latest(...)` (the MCP tool `latest_briefing`) is one read.

**This table holds text, which is why it is not the memory table.** The memory table's rule is
kinds, ids, booleans and timestamps only, so nothing hostile can persist into the agent's later
context (memory.py). A briefing *is* the agent's words, written after reading drafts and logs. It
goes here instead, to a table the agent never reads back: it is handed to the caller (Alexa reads
it aloud), overwritten by the next briefing, and gone after two days.

**One briefing at a time per user.** A start is refused while one started under two minutes ago is
still running (a conditional write, so two starts at once cannot both pass). That bounds what a
stolen token, or a client asking in a loop, can spend on the model. The agent's Lambda gives up
long before two minutes, so a run still marked running after that is reported as not finished.

**Recording never fails the answer.** The agent writes after it has answered; a write that fails is
one log line with the error's class, and the answer goes back as it was.

Used by both Lambdas: the MCP server (ops_mcp/server.py) and the agent (ops_agent_handler.py),
whose package carries this file. Nothing here imports `mcp` or `strands`.

    OPS_BRIEFINGS_TABLE  the table (both functions)
    OPS_AGENT_FUNCTION   the agent's function name or ARN (the MCP server, to start one)
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common.dynamo import get_table

TABLE_ENV = "OPS_BRIEFINGS_TABLE"
AGENT_FUNCTION_ENV = "OPS_AGENT_FUNCTION"

# The event the agent's Lambda is invoked with. Only a direct invoke can carry it: API Gateway's
# events always have an httpMethod, and the handler checks for that too.
EVENT_SOURCE = "ops-briefing"
BRIEFING_QUESTION = "What needs my attention?"

RETENTION = timedelta(days=2)
RUNNING_AT_MOST = timedelta(minutes=2)
FINDINGS_MAX = 20
TOOL_CALLS_MAX = 20
STORED_MAX_CHARS = 200_000  # well under DynamoDB's 400 KB item limit

USER_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
# A bearer token as API Gateway passed it: the scheme, one space, and a JWT's characters.
BEARER_PATTERN = re.compile(r"^Bearer [A-Za-z0-9._~+/=-]{20,8192}$")

RUNNING, READY, FAILED = "running", "ready", "failed"

NEEDS_USER = "I need a signed-in user for a briefing, and I can't tell who is asking."
STARTED = "I've asked the assistant to look into it. Ask me for the briefing in about a minute."
ALREADY_RUNNING = "A briefing is already being put together. Ask me for it in a minute."
COULD_NOT_START = "I couldn't start a briefing just now. Try again in a moment."
NONE_YET = "There's no briefing yet. Ask me to start one."
STILL_RUNNING = "The assistant is still looking. Ask me again in a moment."
NOT_FINISHED = "The last briefing didn't finish. Ask me to start another one."
NOT_AVAILABLE = "Briefings aren't available here."

_LAMBDA_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"})
_lambda_client = None


def configured(*, starting: bool = False) -> bool:
    """Whether this function has the table (and, to start one, the agent) to work with."""
    if not os.environ.get(TABLE_ENV, "").strip():
        return False
    return not starting or bool(os.environ.get(AGENT_FUNCTION_ENV, "").strip())


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _table():
    return get_table(os.environ[TABLE_ENV])


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", config=_LAMBDA_CONFIG)
    return _lambda_client


def _expires_at(now: datetime) -> int:
    return int((now + RETENTION).timestamp())


def _result(spoken: str, status: str, **extra) -> dict:
    return {"spoken": spoken, "status": status, "findings": [], **extra}


def valid_user(user_id) -> bool:
    return isinstance(user_id, str) and bool(USER_ID_PATTERN.match(user_id))


def valid_bearer(authorization) -> bool:
    return isinstance(authorization, str) and bool(BEARER_PATTERN.match(authorization))


def bearer_from_headers(headers) -> str | None:
    """The request's `Authorization` header as a bearer token, or None. It must appear once."""
    if headers is None:
        return None
    getlist = getattr(headers, "getlist", None)
    if getlist is not None:
        values = list(getlist("authorization"))
    else:
        values = [value for name, value in headers.items() if str(name).lower() == "authorization"]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    scheme, _, token = values[0].strip().partition(" ")
    candidate = f"Bearer {token.strip()}"
    return candidate if scheme.lower() == "bearer" and valid_bearer(candidate) else None


# --- starting one (the MCP server) ---------------------------------------------------------------


def start(
    user_id: str | None,
    authorization: str | None,
    *,
    now: datetime | None = None,
    invoke: Callable[[dict], None] | None = None,
) -> dict:
    """Start a briefing for this user, unless one is already running. Returns at once."""
    if not configured(starting=True):
        return _result(NOT_AVAILABLE, "unavailable")
    if not valid_user(user_id) or not valid_bearer(authorization):
        return _result(NEEDS_USER, "unavailable")
    moment = _now(now)
    request_id = uuid.uuid4().hex
    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET #status = :running, started_at = :now, request_id = :rid, expires_at = :expires"
            ),
            ConditionExpression="attribute_not_exists(user_id) OR #status <> :running OR started_at < :stale",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":running": RUNNING,
                ":now": _iso(moment),
                ":rid": request_id,
                ":expires": _expires_at(moment),
                ":stale": _iso(moment - RUNNING_AT_MOST),
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _result(ALREADY_RUNNING, RUNNING, started=False)
        print(f"ops_briefings: start failed error={type(exc).__name__}")
        return _result(COULD_NOT_START, FAILED, started=False)

    event = {
        "source": EVENT_SOURCE,
        "user_id": user_id,
        "request_id": request_id,
        "authorization": authorization,
    }
    try:
        (invoke or _invoke_agent)(event)
    except Exception as exc:  # noqa: BLE001 - whatever failed, the answer is "could not start"
        print(f"ops_briefings: invoke failed error={type(exc).__name__}")
        _mark(user_id, request_id, FAILED, moment)
        return _result(COULD_NOT_START, FAILED, started=False)
    return _result(STARTED, RUNNING, started=True)


def _invoke_agent(event: dict) -> None:
    """Asynchronously: Lambda queues the event and answers 202 before the agent has run."""
    _lambda().invoke(
        FunctionName=os.environ[AGENT_FUNCTION_ENV],
        InvocationType="Event",
        Payload=json.dumps(event).encode("utf-8"),
    )


def _mark(user_id: str, request_id: str, status: str, now: datetime) -> None:
    """Set the status of this run, if it is still the latest. Never raises."""
    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET #status = :status, finished_at = :now",
            ConditionExpression="request_id = :rid",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status, ":now": _iso(now), ":rid": request_id},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ops_briefings: mark failed error={type(exc).__name__}")


# --- writing one (the agent) ---------------------------------------------------------------------


def _stored(result: dict) -> str:
    """What is kept of an answer, as JSON text: the words, and as many findings and tool calls as
    fit. Text so that DynamoDB's number type never reaches the reader."""
    kept = {
        "answer": str(result.get("answer") or ""),
        "findings": [f for f in (result.get("findings") or []) if isinstance(f, dict)][:FINDINGS_MAX],
        "tool_calls": [c for c in (result.get("tool_calls") or []) if isinstance(c, dict)][:TOOL_CALLS_MAX],
    }
    text = json.dumps(kept, default=str)
    if len(text) > STORED_MAX_CHARS:
        kept["findings"] = []
        text = json.dumps(kept, default=str)
    if len(text) > STORED_MAX_CHARS:
        kept["tool_calls"] = []
        kept["answer"] = kept["answer"][:2000]
        text = json.dumps(kept, default=str)
    return text


def record(user_id, result: dict, *, request_id: str | None = None, now: datetime | None = None) -> bool:
    """Write a finished briefing. With `request_id` (a run `start` began), only if that run is
    still the latest, so a slow old run never overwrites a newer one. Never raises."""
    if not configured() or not valid_user(user_id) or not isinstance(result, dict):
        return False
    moment = _now(now)
    item = {
        "user_id": user_id,
        "status": READY,
        "finished_at": _iso(moment),
        "started_at": _iso(moment),
        "result": _stored(result),
        "expires_at": _expires_at(moment),
    }
    try:
        if request_id is None:
            _table().put_item(Item=item)
        else:
            _table().update_item(
                Key={"user_id": user_id},
                UpdateExpression=(
                    "SET #status = :ready, finished_at = :now, #result = :result, expires_at = :expires"
                ),
                ConditionExpression="request_id = :rid",
                ExpressionAttributeNames={"#status": "status", "#result": "result"},
                ExpressionAttributeValues={
                    ":ready": READY,
                    ":now": item["finished_at"],
                    ":result": item["result"],
                    ":expires": item["expires_at"],
                    ":rid": request_id,
                },
            )
    except Exception as exc:  # noqa: BLE001 - recording never fails the answer
        print(f"ops_briefings: record failed error={type(exc).__name__}")
        return False
    return True


def record_failure(user_id, request_id, *, now: datetime | None = None) -> None:
    if (
        configured()
        and valid_user(user_id)
        and isinstance(request_id, str)
        and REQUEST_ID_PATTERN.match(request_id)
    ):
        _mark(user_id, request_id, FAILED, _now(now))


# --- reading one (the MCP server) ----------------------------------------------------------------


def _age(minutes: int) -> str:
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{hours // 24} days ago"


def latest(user_id: str | None, *, now: datetime | None = None) -> dict:
    """The caller's latest briefing: the answer to say, its findings and tool calls, and how old
    it is; or that one is still running, did not finish, or has never been asked for."""
    if not configured():
        return _result(NOT_AVAILABLE, "unavailable")
    if not valid_user(user_id):
        return _result(NEEDS_USER, "unavailable")
    moment = _now(now)
    try:
        item = _table().get_item(Key={"user_id": user_id}).get("Item")
    except Exception as exc:  # noqa: BLE001
        print(f"ops_briefings: read failed error={type(exc).__name__}")
        return _result(COULD_NOT_START.replace("start", "read"), FAILED)
    if not item:
        return _result(NONE_YET, "none")
    status = item.get("status")
    started = _parse(item.get("started_at"))
    if status == RUNNING:
        if started is not None and moment - started < RUNNING_AT_MOST:
            return _result(STILL_RUNNING, RUNNING)
        return _result(NOT_FINISHED, FAILED)
    if status != READY:
        return _result(NOT_FINISHED, FAILED)
    try:
        stored = json.loads(item.get("result") or "{}")
    except ValueError:
        return _result(NOT_FINISHED, FAILED)
    finished = _parse(item.get("finished_at")) or moment
    minutes = max(0, int((moment - finished).total_seconds() // 60))
    answer = str(stored.get("answer") or "").strip()
    findings = [f for f in stored.get("findings") or [] if isinstance(f, dict)]
    return {
        "spoken": f"{answer} (That briefing was from {_age(minutes)}.)" if answer else NOT_FINISHED,
        "status": READY,
        "age_minutes": minutes,
        "finished_at": _iso(finished),
        "findings": findings,
        "tool_calls": [c for c in stored.get("tool_calls") or [] if isinstance(c, dict)],
    }
