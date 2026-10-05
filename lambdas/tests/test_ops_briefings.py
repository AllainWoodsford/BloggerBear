"""The async briefing (ops_mcp/briefings.py and its two callers): how Alexa+ gets the Strands
agent's work without waiting for it (docs/enhancements/alexa-plus.md, section 4.3).

What is held: one briefing at a time per user; a start invokes the agent asynchronously with the
caller's own token and nothing else; a slow old run never overwrites a newer one; the agent's
second entry point is reachable only as a direct invoke, applies the access switch, and records
what it found; the two tools are never offered to the agent itself; and the token is never logged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from starlette.testclient import TestClient
from table_schemas import create_table

import ops_agent_handler
from ops_agent import agent, policy
from ops_mcp import briefings

USER = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
OTHER = "11111111-2222-4333-8444-555555555555"
# Not shaped like a real token on purpose: the secret scan reads test files too.
TOKEN = "Bearer not-a-real-token.plugh-token-marker.for-tests-only"
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
RESULT = {
    "answer": "Crypto didn't publish; its draft is held.",
    "tool_calls": [{"name": "pipeline_health", "arguments": {}}],
    "findings": [{"kind": "draft_truncated", "id": "a1", "suggestion": {"command": "x"}}],
    "turn": "briefing",
}


@pytest.fixture
def table(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": "ap-southeast-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        briefings.TABLE_ENV: "Briefings",
        briefings.AGENT_FUNCTION_ENV: "bloggerbear-test-ops-agent",
    }.items():
        monkeypatch.setenv(key, value)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        create_table(
            boto3.client("dynamodb", region_name="ap-southeast-2"),
            TableName="Briefings",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name="ap-southeast-2").Table("Briefings")
    dynamo_module._dynamodb_resource = None


# --- starting one ---------------------------------------------------------------------------------


def test_a_start_invokes_the_agent_with_the_callers_token_and_returns_at_once(table):
    sent = []

    result = briefings.start(USER, TOKEN, now=NOW, invoke=sent.append)

    assert result["status"] == "running" and result["started"] is True
    assert "about a minute" in result["spoken"]
    assert len(sent) == 1
    event = sent[0]
    assert set(event) == {"source", "user_id", "request_id", "authorization"}
    assert event["source"] == briefings.EVENT_SOURCE
    assert event["user_id"] == USER and event["authorization"] == TOKEN
    assert briefings.REQUEST_ID_PATTERN.match(event["request_id"])
    item = table.get_item(Key={"user_id": USER})["Item"]
    assert item["status"] == "running" and item["request_id"] == event["request_id"]


def test_the_real_invoke_is_asynchronous_and_names_the_agent(table):
    with patch.object(briefings, "_lambda") as client:
        briefings.start(USER, TOKEN, now=NOW)

    kwargs = client.return_value.invoke.call_args.kwargs
    assert kwargs["InvocationType"] == "Event"
    assert kwargs["FunctionName"] == "bloggerbear-test-ops-agent"
    assert json.loads(kwargs["Payload"])["source"] == briefings.EVENT_SOURCE


def test_one_at_a_time_and_a_stale_run_does_not_block_the_next(table):
    sent = []
    briefings.start(USER, TOKEN, now=NOW, invoke=sent.append)

    again = briefings.start(USER, TOKEN, now=NOW + timedelta(seconds=30), invoke=sent.append)
    assert again["started"] is False and again["status"] == "running"
    assert len(sent) == 1  # nothing more was spent

    # Another user is not held up by this one.
    assert briefings.start(OTHER, TOKEN, now=NOW, invoke=sent.append)["started"] is True

    stale = NOW + briefings.RUNNING_AT_MOST + timedelta(seconds=1)
    later = briefings.start(USER, TOKEN, now=stale, invoke=sent.append)
    assert later["started"] is True and len(sent) == 3


def test_an_invoke_that_fails_is_recorded_as_not_finished(table):
    def broken(event):
        raise RuntimeError("throttled")

    result = briefings.start(USER, TOKEN, now=NOW, invoke=broken)

    assert result["started"] is False and result["status"] == "failed"
    assert table.get_item(Key={"user_id": USER})["Item"]["status"] == "failed"
    # And a new start is not blocked by it.
    assert briefings.start(USER, TOKEN, now=NOW, invoke=lambda e: None)["started"] is True


@pytest.mark.parametrize(
    "user_id, token",
    [
        (None, TOKEN),
        ("not-a-subject", TOKEN),
        (USER, None),
        (USER, "Basic dXNlcjpwYXNz"),
        (USER, "Bearer short"),
        (USER, "Bearer has spaces in it and is not a token at all"),
    ],
)
def test_no_user_or_no_bearer_token_starts_nothing(table, user_id, token):
    sent = []

    result = briefings.start(user_id, token, now=NOW, invoke=sent.append)

    assert result["status"] == "unavailable" and sent == []
    assert "Item" not in table.get_item(Key={"user_id": USER})


def test_unconfigured_it_says_so(monkeypatch):
    monkeypatch.delenv(briefings.TABLE_ENV, raising=False)
    assert briefings.start(USER, TOKEN)["status"] == "unavailable"
    assert briefings.latest(USER)["status"] == "unavailable"


def test_the_bearer_is_read_from_exactly_one_authorization_header():
    assert briefings.bearer_from_headers({"authorization": TOKEN}) == TOKEN
    assert briefings.bearer_from_headers({"Authorization": "bearer " + TOKEN.split(" ")[1]}) == TOKEN
    assert briefings.bearer_from_headers({}) is None
    assert briefings.bearer_from_headers(None) is None
    assert briefings.bearer_from_headers({"authorization": "Basic x"}) is None


# --- recording and reading one --------------------------------------------------------------------


def test_latest_says_none_then_running_then_the_answer_with_its_age(table):
    assert briefings.latest(USER, now=NOW)["status"] == "none"

    sent = []
    briefings.start(USER, TOKEN, now=NOW, invoke=sent.append)
    assert briefings.latest(USER, now=NOW + timedelta(seconds=20))["status"] == "running"

    assert briefings.record(USER, RESULT, request_id=sent[0]["request_id"], now=NOW + timedelta(seconds=40))
    found = briefings.latest(USER, now=NOW + timedelta(minutes=5))

    assert found["status"] == "ready"
    assert found["spoken"].startswith(RESULT["answer"])
    assert "4 minutes ago" in found["spoken"]
    assert found["findings"] == RESULT["findings"]
    assert found["tool_calls"] == RESULT["tool_calls"]
    # Another user's briefing is not this one's.
    assert briefings.latest(OTHER, now=NOW)["status"] == "none"


def test_a_run_that_never_finished_is_reported_as_not_finished(table):
    briefings.start(USER, TOKEN, now=NOW, invoke=lambda e: None)

    found = briefings.latest(USER, now=NOW + briefings.RUNNING_AT_MOST + timedelta(seconds=1))

    assert found["status"] == "failed" and "didn't finish" in found["spoken"]


def test_a_slow_old_run_never_overwrites_a_newer_one(table):
    first, second = [], []
    briefings.start(USER, TOKEN, now=NOW, invoke=first.append)
    briefings.start(USER, TOKEN, now=NOW + timedelta(minutes=3), invoke=second.append)

    assert not briefings.record(USER, {**RESULT, "answer": "old"}, request_id=first[0]["request_id"], now=NOW)
    assert briefings.record(USER, RESULT, request_id=second[0]["request_id"], now=NOW + timedelta(minutes=4))
    assert briefings.latest(USER, now=NOW + timedelta(minutes=4))["spoken"].startswith(RESULT["answer"])


def test_a_page_briefing_is_recorded_without_a_run(table):
    assert briefings.record(USER, RESULT, now=NOW)
    assert briefings.latest(USER, now=NOW)["status"] == "ready"


def test_what_is_stored_is_capped_and_never_fails_the_caller(table):
    huge = {"answer": "a" * 10, "findings": [{"text": "x" * 50_000}] * 30, "tool_calls": []}
    assert briefings.record(USER, huge, now=NOW)
    stored = json.loads(table.get_item(Key={"user_id": USER})["Item"]["result"])
    assert len(json.dumps(stored)) <= briefings.STORED_MAX_CHARS

    with patch.object(briefings, "_table", side_effect=RuntimeError("down")):
        assert briefings.record(USER, RESULT, now=NOW) is False


# --- the agent's second entry point ---------------------------------------------------------------


@pytest.fixture
def pipeline_config(monkeypatch):
    row = {}
    monkeypatch.setattr(ops_agent_handler.dynamo, "get_pipeline_config", lambda: dict(row) or None)
    monkeypatch.delenv("OPS_ASSISTANT_ALLOWED_CIDRS", raising=False)
    return row


def _run_event(table_fixture=None, **overrides):
    sent = []
    briefings.start(USER, TOKEN, now=datetime.now(UTC), invoke=sent.append)
    return {**sent[0], **overrides}


def test_a_run_answers_as_the_user_and_records_it(table, pipeline_config, capsys):
    run = _run_event()
    with patch.object(agent, "answer", return_value=dict(RESULT)) as answer:
        assert ops_agent_handler.handler(run, None) == {"ok": True}

    answer.assert_called_once_with(briefings.BRIEFING_QUESTION, [], TOKEN)
    assert briefings.latest(USER)["status"] == "ready"
    out = capsys.readouterr().out
    assert "plugh-token-marker" not in out and RESULT["answer"] not in out


@pytest.mark.parametrize("setting", ["off", "allowlist", "something-else"])
def test_a_run_obeys_the_access_switch_and_has_no_address_to_offer(table, pipeline_config, setting, capsys):
    pipeline_config["assistant_access"] = setting
    run = _run_event()
    with patch.object(agent, "answer") as answer:
        assert ops_agent_handler.handler(run, None) == {"ok": False}

    answer.assert_not_called()
    assert briefings.latest(USER)["status"] == "failed"
    assert "briefing run refused" in capsys.readouterr().out


def test_a_run_that_fails_is_recorded_and_not_raised(table, pipeline_config):
    run = _run_event()
    with patch.object(agent, "answer", side_effect=agent.AgentError("ClientError")):
        assert ops_agent_handler.handler(run, None) == {"ok": False}
    assert briefings.latest(USER)["status"] == "failed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"authorization": "Basic x"},
        {"user_id": "nobody"},
        {"request_id": "not-hex"},
    ],
)
def test_a_malformed_run_is_refused_before_anything_runs(table, pipeline_config, overrides):
    run = _run_event(**overrides)
    with patch.object(agent, "answer") as answer:
        assert ops_agent_handler.handler(run, None) == {"ok": False}
    answer.assert_not_called()


def test_an_api_gateway_event_can_never_be_taken_for_a_run(table, pipeline_config):
    """A caller cannot make POST /ask look like a direct invoke: API Gateway's event always has an
    httpMethod and a request context, whatever the body says."""
    forged = {
        **_run_event(),
        "httpMethod": "POST",
        "resource": "/ask",
        "requestContext": {"identity": {"sourceIp": "203.0.113.9"}},
        "headers": {},
        "body": "{}",
    }
    with patch.object(agent, "answer") as answer:
        response = ops_agent_handler.handler(forged, None)
    assert response["statusCode"] == 401
    answer.assert_not_called()


def test_a_briefing_asked_on_the_page_is_recorded_for_the_signed_in_user(table, pipeline_config):
    page = {
        "httpMethod": "POST",
        "resource": "/ask",
        "headers": {"Authorization": TOKEN},
        "requestContext": {"identity": {"sourceIp": "203.0.113.9"}, "authorizer": {"claims": {"sub": USER}}},
        "body": json.dumps({"question": "What needs my attention?"}),
    }
    with patch.object(agent, "answer", return_value=dict(RESULT)):
        assert ops_agent_handler.handler(page, None)["statusCode"] == 200
    assert briefings.latest(USER)["status"] == "ready"


def test_a_how_to_or_a_follow_up_is_not_recorded_as_a_briefing():
    how_to = {**RESULT, "tool_calls": [{"name": "cli_help", "arguments": {}}]}
    table_answer = {**RESULT, "tables": [{"title": "t"}]}
    follow_up = {**RESULT, "turn": "follow_up"}
    assert ops_agent_handler._is_briefing(RESULT)
    assert not ops_agent_handler._is_briefing(how_to)
    assert not ops_agent_handler._is_briefing(table_answer)
    assert not ops_agent_handler._is_briefing(follow_up)


# --- the agent never gets the two tools; the server lists them only when configured ----------------


def test_the_agent_is_never_offered_the_tools_that_start_or_read_briefings():
    names = ["pipeline_health", "start_briefing", "latest_briefing", "firewall_review"]
    for turn in (policy.BRIEFING, policy.FOLLOW_UP):
        offered = policy.offered(names, turn)
        assert "start_briefing" not in offered and "latest_briefing" not in offered
        ledger = policy.Ledger(turn, {name: object() for name in names})
        assert ledger.admit("start_briefing", {}) is not None
        assert ledger.tool_calls == []


def _tool_names(monkeypatch, configured: bool) -> set[str]:
    from ops_mcp import server

    if configured:
        monkeypatch.setenv(briefings.TABLE_ENV, "Briefings")
        monkeypatch.setenv(briefings.AGENT_FUNCTION_ENV, "agent")
    else:
        monkeypatch.delenv(briefings.TABLE_ENV, raising=False)
        monkeypatch.delenv(briefings.AGENT_FUNCTION_ENV, raising=False)
    built = server.build_server()
    import asyncio

    return {tool.name for tool in asyncio.run(built.list_tools())}


def test_the_server_lists_the_two_tools_only_where_it_can_use_them(monkeypatch):
    assert {"start_briefing", "latest_briefing"} <= _tool_names(monkeypatch, True)
    assert not {"start_briefing", "latest_briefing"} & _tool_names(monkeypatch, False)


def test_a_keep_warm_ping_is_answered_and_nothing_else(table, monkeypatch):
    from ops_mcp import server

    # The access switch is read first, as for every request: nothing stored means open.
    monkeypatch.setenv("MODEL_CONFIG_TABLE", "ModelConfig")
    create_table(
        boto3.client("dynamodb", region_name="ap-southeast-2"),
        TableName="ModelConfig",
        KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setenv("OPS_MCP_ALLOWED_HOSTS", "ops.example.test")
    with TestClient(server.create_app(), base_url="http://ops.example.test") as client:
        response = client.post(server.EVENTS_PATH, json={"source": "bloggerbear.keep-warm"})
    assert response.status_code == 204 and response.content == b""


def test_an_async_invoke_is_never_retried_into_a_second_run():
    """A timed-out async Invoke may still have been queued: retrying it would queue the same run
    again, a second model run for one start_briefing."""
    assert briefings._LAMBDA_CONFIG.retries["total_max_attempts"] == 1
