"""What the agent's MCP client puts on the wire, against the real ops MCP server.

The design's day-1 question: does Strands' MCP client send the caller's bearer token, and which
protocol version does it speak to our server? Answered by experiment, and kept as a test so a
dependency upgrade that changes either fails the build.

The server is the real one (`ops_mcp.server.create_app()`), run by uvicorn on a localhost port in
a background thread, as it runs under the Lambda Web Adapter. A wrapper around it, in this test
only, records every request before passing it on unchanged. The client is the agent's own
(`ops_agent.agent.mcp_client`). Only the tables behind the tools are replaced, and the config
table the server's access switch reads is moto's. No AWS, no Bedrock.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from unittest.mock import patch

import boto3
import pytest
import uvicorn
from moto import mock_aws
from ops_agent_fakes import ScriptedModel
from table_schemas import create_table

import common.dynamo as dynamo_module
from common.dynamo import put_pipeline_config
from ops_agent import agent
from ops_mcp import server

TOKEN = "Bearer not-a-real-token.the-caller.for-tests-only"
MODERN = "2026-07-28"
INBOX = {
    "spoken": "One article is waiting: its draft was cut short.",
    "findings": [
        {
            "kind": "draft_truncated",
            "id": "a1",
            "noticed": "A held article's draft was cut short",
            "where": {"topic": "crypto"},
            "suggestion": {
                "action": "Rewrite the article so it is finished",
                "command": 'python scripts/admin_cli.py articles rewrite a1 -i "the draft was cut short"',
                "what_it_does": "Rewrites the article in the background.",
            },
        }
    ],
    "waiting": 1,
    "items": [],
}
HEALTH = {"spoken": "Crypto did not publish.", "findings": [], "topics": []}


class Recorder:
    """ASGI wrapper: notes each HTTP request's headers and JSON-RPC method, then hands the
    request to the real app untouched."""

    def __init__(self, app) -> None:
        self.app = app
        self.requests: list[dict] = []
        self.refuse = False  # True: answer 401 to everything, as an authorizer does a bad token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        entry = {
            "http_method": scope["method"],
            "path": scope["path"],
            "headers": {name.decode().lower(): value.decode() for name, value in scope["headers"]},
            "body": b"",
        }
        self.requests.append(entry)
        if self.refuse:
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b'{"message":"Unauthorized"}'})
            return

        async def recording_receive():
            message = await receive()
            if message["type"] == "http.request":
                entry["body"] += message.get("body", b"")
            return message

        await self.app(scope, recording_receive, send)

    def rpc(self) -> list[dict]:
        """Each request's JSON-RPC body."""
        return [json.loads(request["body"]) for request in self.requests if request["body"]]


@pytest.fixture
def config_table(monkeypatch):
    """The config table, empty, as tests/test_ops_mcp_server.py sets it up: the server reads the
    operator's `assistant_access` setting from it on every request (ops_mcp/access.py) and
    refuses if it can't. Nothing stored means open."""
    for key, value in {
        "AWS_DEFAULT_REGION": "ap-southeast-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MODEL_CONFIG_TABLE": "ModelConfig",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo_module._dynamodb_resource = None
    with mock_aws():
        create_table(
            boto3.client("dynamodb", region_name="ap-southeast-2"),
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
    dynamo_module._dynamodb_resource = None


@pytest.fixture
def ops_server(monkeypatch, config_table):
    """The real server on 127.0.0.1, with its Host check set to match. Yields (url, recorder)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setenv("OPS_MCP_ALLOWED_HOSTS", f"127.0.0.1:{port}")
    monkeypatch.delenv("OPS_MCP_ALLOWED_ORIGINS", raising=False)

    recorder = Recorder(server.create_app())
    web = uvicorn.Server(uvicorn.Config(recorder, log_level="error", lifespan="on"))
    thread = threading.Thread(target=web.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if web.started:
                break
            threading.Event().wait(0.01)
        assert web.started, "the test server did not start"
        yield f"http://127.0.0.1:{port}{server.MCP_PATH}", recorder
    finally:
        web.should_exit = True
        thread.join(timeout=10)
        listener.close()


def test_strands_mcp_client_sends_the_bearer_token_on_every_request_and_speaks_2026_07_28(ops_server):
    url, recorder = ops_server

    with patch("ops_mcp.tools.admin_inbox", return_value=INBOX) as inbox:
        with agent.mcp_client(url, TOKEN) as client:
            tools = agent.list_tools(client)
            result = client.call_tool_sync("call-1", "admin_inbox", {"topic": "crypto", "limit": 3})

    # It is Strands' client, over Streamable HTTP, and it found the server's tools.
    assert type(client).__module__.startswith("strands.tools.mcp")
    registered = asyncio.run(server.build_server().list_tools())
    assert sorted(tool.tool_name for tool in tools) == sorted(tool.name for tool in registered)

    # (b) The caller's token is on every request the client made, not only the first.
    assert len(recorder.requests) >= 3
    for request in recorder.requests:
        assert request["http_method"] == "POST" and request["path"] == server.MCP_PATH
        assert request["headers"]["authorization"] == TOKEN

    # (c) MCP 2026-07-28: one discover, then standalone requests that each carry their version.
    # No `initialize` handshake, no session id, no GET stream.
    methods = [body["method"] for body in recorder.rpc()]
    assert methods == ["server/discover", "tools/list", "tools/call"]
    for request, body in zip(recorder.requests, recorder.rpc(), strict=True):
        assert request["headers"]["mcp-protocol-version"] == MODERN
        assert body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == MODERN
        assert "mcp-session-id" not in request["headers"]
    assert recorder.rpc()[0]["params"]["_meta"]["io.modelcontextprotocol/clientInfo"]["name"] == (
        "bloggerbear-ops-agent"
    )

    # And the tool's structured result arrives whole, which is where findings are read from.
    inbox.assert_called_once_with("crypto", 3)
    assert result["status"] == "success"
    assert result["structuredContent"] == INBOX


def test_a_whole_question_goes_through_the_real_server_as_the_caller(ops_server, monkeypatch):
    """Handler to agent to Strands' MCP client to the server and back, with a scripted model:
    the findings on the page are the server's, and every MCP request carried the token."""
    url, recorder = ops_server
    monkeypatch.setenv("OPS_MCP_URL", url)
    model = ScriptedModel(
        [
            [("pipeline_health", {})],
            [("pipeline_health", {"topic": "crypto"}), ("admin_inbox", {"topic": "crypto"})],
            "Crypto did not publish: its draft was cut short. One suggested fix is on screen.",
        ]
    )
    monkeypatch.setattr(agent, "bedrock_model", lambda: model)

    with (
        patch("ops_mcp.tools.pipeline_health", return_value=HEALTH) as health,
        patch("ops_mcp.tools.admin_inbox", return_value=INBOX) as inbox,
    ):
        result = agent.answer("Anything need my attention?", [], TOKEN)

    assert [call.args for call in health.call_args_list] == [(None,), ("crypto",)]
    inbox.assert_called_once()
    assert inbox.call_args.args[0] == "crypto"
    assert result["turn"] == "briefing"
    assert result["answer"].startswith("Crypto did not publish")
    assert result["findings"] == INBOX["findings"]
    assert result["tool_calls"] == [
        {"name": "pipeline_health", "arguments": {}},
        {"name": "pipeline_health", "arguments": {"topic": "crypto"}},
        {"name": "admin_inbox", "arguments": {"topic": "crypto"}},
    ]
    # The model saw the server's JSON, so it has something to follow a lead from.
    assert "Crypto did not publish." in json.dumps(model.requests[1])

    methods = [body["method"] for body in recorder.rpc()]
    assert methods == ["server/discover", "tools/list", "tools/call", "tools/call", "tools/call"]
    assert {request["headers"].get("authorization") for request in recorder.requests} == {TOKEN}


def test_a_server_that_refuses_the_caller_is_an_agent_error(ops_server, monkeypatch):
    """What a rejected token looks like from here: the server (in production, the authorizer in
    front of it) answers with an error, and the question fails without detail."""
    url, recorder = ops_server
    recorder.refuse = True
    monkeypatch.setenv("OPS_MCP_URL", url)
    model = ScriptedModel(["never asked"])
    monkeypatch.setattr(agent, "bedrock_model", lambda: model)

    with pytest.raises(agent.AgentError) as raised:
        agent.answer("Anything need my attention?", [], TOKEN)

    assert recorder.requests  # it did ask
    assert model.calls == 0  # and with no tools, the model was never called
    assert TOKEN not in str(raised.value)


def test_with_the_assistant_switched_off_the_question_fails_and_the_model_is_never_called(
    ops_server, monkeypatch
):
    """The operator's `assistant_access` switch is enforced by the server (ops_mcp/access.py).
    Off there means no tools here, and with no tools the agent does not go on to Bedrock."""
    url, recorder = ops_server
    put_pipeline_config(assistant_access="off")
    monkeypatch.setenv("OPS_MCP_URL", url)
    model = ScriptedModel(["never asked"])
    monkeypatch.setattr(agent, "bedrock_model", lambda: model)

    with pytest.raises(agent.AgentError):
        agent.answer("Anything need my attention?", [], TOKEN)

    assert recorder.requests and model.calls == 0
