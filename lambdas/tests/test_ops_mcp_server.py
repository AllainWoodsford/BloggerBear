"""The ops MCP server's wire behaviour (ops_mcp/server.py): the contract a client relies on.

Run against the real SDK through its own web app, in process. What is held here is what the
hackathon's rules and the MCP spec ask of the server, and what the deployment relies on: the
protocol versions it answers, plain JSON with no session, and the Host and Origin checks.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from ops_mcp import server

HOST = "ops.example.test"
MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "contract-test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_MCP_ALLOWED_HOSTS", HOST)
    monkeypatch.setenv("OPS_MCP_ALLOWED_ORIGINS", "https://bloggerbear.com")
    with TestClient(server.create_app(), base_url=f"http://{HOST}") as test_client:
        yield test_client


def call(client, method, params=None, *, name=None, headers=None):
    """One 2026-07-28 request: the method's own POST, with the headers the spec requires."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), "_meta": META}}
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": method,
    }
    if name:
        request_headers["Mcp-Name"] = name
    request_headers.update(headers or {})
    return client.post(server.MCP_PATH, content=json.dumps(body), headers=request_headers)


def test_it_speaks_2026_07_28_without_a_handshake_or_a_session(client):
    response = call(client, "server/discover")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    result = response.json()["result"]
    assert MODERN in result["supportedVersions"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == server.SERVER_NAME


def test_it_still_answers_a_client_that_opens_with_initialize(client):
    """The rules' minimum is 2025-11-25, and a client on it opens with a handshake."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "legacy", "version": "0"},
        },
    }

    response = client.post(
        server.MCP_PATH,
        content=json.dumps(body),
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-11-25"
    assert "mcp-session-id" not in response.headers  # stateless: nothing to remember between requests


def test_a_version_it_does_not_speak_is_refused_with_the_ones_it_does(client):
    response = call(client, "tools/list", headers={"MCP-Protocol-Version": "1999-01-01"})

    assert response.status_code == 400


def test_the_tools_are_listed_read_only_with_structured_output(client):
    tools = {tool["name"]: tool for tool in call(client, "tools/list").json()["result"]["tools"]}

    assert set(tools) == {
        "pipeline_health",
        "admin_inbox",
        "content_checks",
        "security_events",
        "alarms",
        "spend",
    }
    for tool in tools.values():
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["outputSchema"]["type"] == "object"
        assert tool["description"]


def test_a_tool_call_returns_one_json_object_with_the_structured_result(client):
    answer = {"spoken": "Nothing is waiting in the inbox.", "findings": [], "waiting": 0, "items": []}
    with patch("ops_mcp.tools.admin_inbox", return_value=answer) as mock_inbox:
        response = call(
            client,
            "tools/call",
            {"name": "admin_inbox", "arguments": {"topic": "crypto", "limit": 3}},
            name="admin_inbox",
        )

    mock_inbox.assert_called_once_with("crypto", 3)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")  # not an event stream
    result = response.json()["result"]
    assert result["isError"] is False and result["structuredContent"] == answer


def test_a_tool_that_fails_says_so_without_the_reason(client):
    with patch("ops_mcp.tools.pipeline_health", side_effect=RuntimeError("table bloggerbear-x at 10.1.2.3")):
        response = call(
            client, "tools/call", {"name": "pipeline_health", "arguments": {}}, name="pipeline_health"
        )

    result = response.json()["result"]
    assert result["isError"] is True
    assert "10.1.2.3" not in response.text and "bloggerbear-x" not in response.text


def test_a_header_that_disagrees_with_the_body_is_refused(client):
    response = call(client, "tools/call", {"name": "admin_inbox", "arguments": {}}, name="pipeline_health")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020  # HeaderMismatch


def test_an_origin_that_is_not_allowed_is_refused(client):
    assert call(client, "tools/list", headers={"Origin": "https://evil.example"}).status_code == 403
    assert call(client, "tools/list", headers={"Origin": "https://bloggerbear.com"}).status_code == 200
    assert call(client, "tools/list").status_code == 200  # no Origin: not a browser


def test_a_host_that_is_not_allowed_is_refused(client):
    assert call(client, "tools/list", headers={"Host": "somewhere-else.example"}).status_code == 421


def test_with_no_hosts_configured_everything_is_refused(monkeypatch):
    monkeypatch.delenv("OPS_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("OPS_MCP_ALLOWED_ORIGINS", raising=False)

    with TestClient(server.create_app(), base_url=f"http://{HOST}") as closed:
        assert call(closed, "tools/list").status_code == 421


def test_the_server_registers_no_tool_that_can_change_anything():
    """Every tool is one of the read-only functions in tools.py, content.py and account.py, by
    name."""
    import asyncio

    registered = asyncio.run(server.build_server().list_tools())

    assert sorted(tool.name for tool in registered) == [
        "admin_inbox",
        "alarms",
        "content_checks",
        "pipeline_health",
        "security_events",
        "spend",
    ]


@pytest.mark.parametrize(
    ("name", "arguments", "target", "called_with"),
    [
        ("content_checks", {"days": 3}, "ops_mcp.content.content_checks", (3,)),
        ("security_events", {}, "ops_mcp.account.security_events", (7,)),
        ("alarms", {}, "ops_mcp.account.alarms", ()),
        ("spend", {"period": "month"}, "ops_mcp.account.spend", ("month",)),
    ],
)
def test_each_new_tool_passes_its_arguments_to_its_function(client, name, arguments, target, called_with):
    answer = {"spoken": "Nothing.", "findings": []}
    with patch(target, return_value=answer) as mock_tool:
        response = call(client, "tools/call", {"name": name, "arguments": arguments}, name=name)

    mock_tool.assert_called_once_with(*called_with)
    assert response.json()["result"]["structuredContent"] == answer


def test_spend_takes_a_week_or_a_month_and_nothing_else(client):
    with patch("ops_mcp.account.spend") as mock_spend:
        response = call(
            client, "tools/call", {"name": "spend", "arguments": {"period": "year"}}, name="spend"
        )

    mock_spend.assert_not_called()
    assert response.json()["result"]["isError"] is True
