"""Ops agent Lambda handler: `POST /ask`, the operator's question in and a spoken answer out.

Behind an API Gateway REST API whose Cognito authorizer has already checked the caller's token
before this code runs (the design's section 5). The request:

    {"question": "Anything need my attention?",
     "history": [{"role": "user", "text": "..."}, {"role": "assistant", "text": "..."}]}

`history` is the last few turns of the conversation, which the browser tab holds and sends with
each question; with none, this is a briefing (ops_agent/policy.py). The answer:

    {"answer": "...",                       spoken by the page
     "tool_calls": [{"name", "arguments"}], what the agent called, in order, for the page to show
     "findings": [...],                     the tools' findings, each with its suggestion card
     "turn": "briefing" | "follow_up"}

**The caller's token is passed on, not used.** The `Authorization` header goes to the MCP server
as it arrived, so the server's own authorizer sees the same caller. This code never decodes it.

**Nothing is stored, and the logs hold no words.** A log line records which turn it was, which
tools were called and how many findings came back: never the question, the answer or the token.
A failure is logged by the class of the error alone, since its message could quote any of them.

**What is refused, and how.** A body that is not what is described above is a 400 with a plain
reason. A model or MCP failure is a 502 that says nothing about why. Like the other handlers,
`handler` never raises.

The page is served from another origin (the site's own), so responses carry CORS headers and
OPTIONS is answered here, as public_api_handler.py does. Only the origin named in
OPS_AGENT_ALLOWED_ORIGIN may read a response; left unset, no browser can.
"""

from __future__ import annotations

import base64
import json
import os

from ops_agent import agent

QUESTION_MAX_CHARS = 500
HISTORY_MAX_TURNS = 6
TURN_MAX_CHARS = 1000
_ROLES = ("user", "assistant")

_UNAVAILABLE = "The assistant could not answer just now. Try again in a moment."


class _Invalid(Exception):
    """The request is not one this endpoint takes. The message is sent to the caller, so it
    never quotes what they sent."""


def _cors_headers() -> dict:
    origin = os.environ.get("OPS_AGENT_ALLOWED_ORIGIN", "").strip()
    if not origin:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Access-Control-Allow-Headers": "authorization,content-type",
        "Vary": "Origin",
    }


def _response(status_code: int, payload: dict) -> dict:
    return {
        "statusCode": status_code,
        # no-store: an answer is about the operator's own pipeline, and is nobody else's to cache.
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store", **_cors_headers()},
        "body": json.dumps(payload),
    }


def _error(status_code: int, message: str) -> dict:
    return _response(status_code, {"error": message})


def _authorization(event: dict) -> str | None:
    """The caller's `Authorization` header if it is a bearer token, else None. Header names
    arrive in whatever case the client used."""
    for name, value in (event.get("headers") or {}).items():
        if name.lower() == "authorization" and isinstance(value, str):
            scheme, _, token = value.strip().partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return f"Bearer {token.strip()}"
    return None


def _body(event: dict) -> dict:
    raw = event.get("body") or ""
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        body = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise _Invalid("the body must be JSON") from exc
    if not isinstance(body, dict):
        raise _Invalid("the body must be a JSON object")
    return body


def _validated(body: dict) -> tuple[str, list[dict]]:
    """The question and the history, checked. Everything the model is sent from the caller
    passes through here, so its size is known before a token is spent."""
    unknown = set(body) - {"question", "history"}
    if unknown:
        raise _Invalid("the body may hold only 'question' and 'history'")

    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise _Invalid("'question' is required and must be text")
    question = question.strip()
    if len(question) > QUESTION_MAX_CHARS:
        raise _Invalid(f"'question' may be at most {QUESTION_MAX_CHARS} characters")

    history = body.get("history")
    if history is None:
        history = []
    if not isinstance(history, list):
        raise _Invalid("'history' must be a list of turns")
    if len(history) > HISTORY_MAX_TURNS:
        raise _Invalid(f"'history' may hold at most {HISTORY_MAX_TURNS} turns")
    turns = []
    for turn in history:
        if not isinstance(turn, dict) or set(turn) != {"role", "text"}:
            raise _Invalid("each turn in 'history' must hold exactly 'role' and 'text'")
        role, text = turn["role"], turn["text"]
        if role not in _ROLES:
            raise _Invalid("a turn's 'role' must be 'user' or 'assistant'")
        if not isinstance(text, str) or not text.strip():
            raise _Invalid("a turn's 'text' is required and must be text")
        if len(text) > TURN_MAX_CHARS:
            raise _Invalid(f"a turn's 'text' may be at most {TURN_MAX_CHARS} characters")
        turns.append({"role": role, "text": text.strip()})
    return question, turns


def _ask(event: dict) -> dict:
    authorization = _authorization(event)
    if authorization is None:
        # API Gateway's authorizer turns such a request away before it gets here; this is for
        # the day the route is deployed without one.
        return _error(401, "a bearer token is required")
    try:
        question, history = _validated(_body(event))
    except _Invalid as exc:
        return _error(400, str(exc))

    try:
        result = agent.answer(question, history, authorization)
    except agent.AgentError as exc:
        print(f"ops_agent: failed error={exc}")  # the error's class name, nothing it said
        return _error(502, _UNAVAILABLE)

    names = ",".join(call["name"] for call in result["tool_calls"])
    print(
        f"ops_agent: turn={result['turn']} tool_calls={len(result['tool_calls'])} "
        f"tools={names or '-'} findings={len(result['findings'])}"
    )
    return _response(200, result)


def _route_key(event: dict) -> str:
    """ "METHOD /path": routeKey if present, else httpMethod + resource (REST API's proxy event),
    as the other API handlers build it."""
    return event.get("routeKey") or f"{event.get('httpMethod')} {event.get('resource') or event.get('path')}"


def handler(event, context) -> dict:
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 204, "headers": _cors_headers(), "body": ""}
    if _route_key(event) != "POST /ask":
        return _error(404, "no such route")
    try:
        return _ask(event)
    except Exception as exc:  # noqa: BLE001 - never raise out of the handler
        print(f"ops_agent: failed error={type(exc).__name__}")
        return _error(502, _UNAVAILABLE)
