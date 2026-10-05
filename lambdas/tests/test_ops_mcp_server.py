"""The ops MCP server's wire behaviour (ops_mcp/server.py): the contract a client relies on.

Run against the real SDK through its own web app, in process. What is held here is what the
hackathon's rules and the MCP spec ask of the server, and what the deployment relies on: the
protocol versions it answers, plain JSON with no session, and the Host and Origin checks.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from starlette.testclient import TestClient
from table_schemas import create_table

from ops_mcp import architecture, server

HOST = "ops.example.test"
MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "contract-test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture(autouse=True)
def config_table(monkeypatch):
    """The config table, with nothing in it. Every request first reads the operator's
    `assistant_access` setting from it (ops_mcp/access.py) and is refused if it can't; no
    setting stored means open, which is what these tests are about. The switch itself is held
    in test_ops_mcp_access.py."""
    for key, value in {
        "AWS_DEFAULT_REGION": "ap-southeast-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MODEL_CONFIG_TABLE": "ModelConfig",
    }.items():
        monkeypatch.setenv(key, value)
    import common.dynamo as dynamo_module

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


def test_the_alexa_plus_sequence_works_as_standalone_posts_with_no_session(client):
    """The Alexa+ toolkit speaks 2025-11-25 (docs/enhancements/alexa-plus.md, section 2): the
    handshake, the `initialized` notification, then `tools/list` and `tools/call`. Behind a
    Lambda every one of those is its own request with nothing kept between them, so each must
    be answered as it stands, with no session id given or asked for."""
    legacy = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def post(method, params=None, *, request_id=1, version_header=True):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            body["id"] = request_id
        headers = dict(legacy)
        if version_header:
            headers["MCP-Protocol-Version"] = "2025-11-25"
        return client.post(server.MCP_PATH, content=json.dumps(body), headers=headers)

    opened = post(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "alexa-like", "version": "0"},
        },
        version_header=False,  # the handshake is what agrees the version
    )
    assert opened.status_code == 200
    assert opened.json()["result"]["protocolVersion"] == "2025-11-25"
    assert "tools" in opened.json()["result"]["capabilities"]
    assert "mcp-session-id" not in opened.headers

    noted = post("notifications/initialized", request_id=None)
    assert noted.status_code == 202

    listed = post("tools/list", request_id=2)
    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"pipeline_health", "admin_inbox"} <= names

    quiet = {"spoken": "Nothing waiting.", "findings": []}
    with patch.object(server.tools, "admin_inbox", return_value=quiet):
        called = post("tools/call", {"name": "admin_inbox", "arguments": {}}, request_id=3)
    assert called.status_code == 200
    result = called.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["spoken"] == "Nothing waiting."


def test_a_version_it_does_not_speak_is_refused_with_the_ones_it_does(client):
    response = call(client, "tools/list", headers={"MCP-Protocol-Version": "1999-01-01"})

    assert response.status_code == 400


# The tools that look at the pipeline, and the ones that keep the assistant's own list (memory.py).
PIPELINE_TOOLS = {"pipeline_health", "admin_inbox", "content_checks", "security_events", "alarms", "spend"}
MEMORY_TOOLS = {"follow_up", "dismiss", "watch", "unwatch", "watch_list"}
# The guide to the Admin CLI (cli_guide.py): help, how-to commands, and the topics as a table.
GUIDE_TOOLS = {"cli_reference", "cli_help", "cli_guides", "cli_command", "topics_overview"}
# The architecture expert (architecture.py, runsheets.py): answered from the package's catalogue.
EXPERT_TOOLS = {"architecture", "investigate"}


def test_the_tools_are_listed_with_what_they_change_and_structured_output(client):
    tools = {tool["name"]: tool for tool in call(client, "tools/list").json()["result"]["tools"]}

    assert set(tools) == PIPELINE_TOOLS | MEMORY_TOOLS | GUIDE_TOOLS | EXPERT_TOOLS
    for name, tool in tools.items():
        # Only the memory tools say they write, and each says what: its own list and nothing else.
        assert tool["annotations"]["readOnlyHint"] is (name not in MEMORY_TOOLS)
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["outputSchema"]["type"] == "object"
        assert tool["description"]
        if name in MEMORY_TOOLS:
            assert "changes only the assistant's own" in " ".join(tool["description"].split())
        # The SDK's Context is injected: it is not an argument a client can send.
        assert "ctx" not in tool["inputSchema"].get("properties", {})


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


def test_the_only_thing_any_tool_can_write_is_the_assistants_own_table():
    """Every tool is one of the read-only functions in tools.py, content.py and account.py, or one
    of memory.py's, by name. The whole package writes through one function, memory._write, and
    deletes through one, memory._delete, both on OPERATOR_SUGGESTIONS_TABLE; no other module has
    a write call in it. (test_ops_mcp_memory.py holds that no other table changes when they run,
    and the Terraform tests that the role could not write one if they tried.)"""
    import asyncio
    import pathlib
    import re

    registered = asyncio.run(server.build_server().list_tools())

    assert {tool.name for tool in registered} == PIPELINE_TOOLS | MEMORY_TOOLS | GUIDE_TOOLS | EXPERT_TOOLS

    package = pathlib.Path(server.__file__).parent
    writes = re.compile(r"\.(put_item|update_item|delete_item|batch_writer|put_object|delete_object)\(")
    found = {
        path.name: writes.findall(path.read_text(encoding="utf-8")) for path in sorted(package.glob("*.py"))
    }
    assert {name: calls for name, calls in found.items() if calls} == {
        "memory.py": ["update_item", "delete_item"],
        # The latest briefing per user (briefings.py): its own table, OPS_BRIEFINGS_TABLE, which
        # the MCP server marks as started and the agent writes. Tools listed only where it is
        # configured, which it is not here; test_ops_briefings.py holds those.
        "briefings.py": ["update_item", "update_item", "put_item", "update_item"],
    }
    memory_source = (package / "memory.py").read_text(encoding="utf-8")
    assert memory_source.count("os.environ[") == 1 and "os.environ[TABLE_ENV]" in memory_source


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


# --- the guide to the Admin CLI, over the wire ---------------------------------------------------


def tool_call(client, name, arguments):
    response = call(client, "tools/call", {"name": name, "arguments": arguments}, name=name)
    result = response.json()["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]


def test_the_guide_tools_say_when_to_use_each_and_never_to_write_a_command(client):
    tools = {tool["name"]: tool for tool in call(client, "tools/list").json()["result"]["tools"]}
    described = {name: " ".join(tools[name]["description"].split()) for name in GUIDE_TOOLS}

    # Help first; the exact command second, and only from the operator's own values.
    assert "first step" in described["cli_help"] and "never read the help aloud" in described["cli_help"]
    assert "second step" in described["cli_command"]
    assert "only when the operator has given the values" in described["cli_command"]
    assert "never invent a value" in described["cli_command"]
    assert "never write one in your answer" in described["cli_command"]
    assert "Use it first" in described["cli_guides"] and "editorial-goals" in described["cli_guides"]
    assert "list topics" in described["topics_overview"]
    # The model picks command paths from the description: every one is in it.
    from ops_mcp import cli_guide

    for path in cli_guide.command_paths():
        assert path in described["cli_help"], path
    assert tools["cli_command"]["inputSchema"]["required"] == ["command"]
    assert tools["cli_help"]["inputSchema"]["properties"]["commands"]["type"] == "array"
    assert tools["topics_overview"]["inputSchema"]["properties"]["limit"]["default"] == 5


def test_a_how_to_goes_over_the_wire_as_help_and_then_a_built_command(client):
    helped = tool_call(client, "cli_help", {"commands": ["topics update"]})
    assert helped["findings"][0]["help"].startswith("usage: admin_cli.py topics update")
    assert helped["findings"][0]["suggestion"]["command"].endswith("topics update --help")

    made = tool_call(
        client,
        "cli_command",
        {"command": "topics update", "options": {"topic_id": "crypto", "research_interval_hours": 3}},
    )
    assert made["findings"][0]["suggestion"]["command"] == (
        "python scripts/admin_cli.py topics update crypto --research-interval-hours 3"
    )

    delete = {"command": "topics delete", "options": {"topic_id": "crypto"}}
    template = tool_call(client, "cli_command", delete)
    assert template["findings"][0]["destructive"] is True
    assert template["findings"][0]["suggestion"]["command"].endswith("topics delete <topic_id>")

    assert tool_call(client, "cli_reference", {})["commands"]
    assert tool_call(client, "cli_guides", {"topic": "gear"})["guide"]["id"] == "gear"


def test_the_guide_tools_are_not_passed_through_the_memory(client):
    """A how-to is not a suggestion to follow up: nothing is read from or written to the list."""
    with patch("ops_mcp.memory.remember") as mock_remember:
        tool_call(client, "cli_help", {"commands": ["topics update"]})
        tool_call(client, "cli_command", {"command": "inbox"})

    mock_remember.assert_not_called()


def test_the_overview_passes_its_arguments_to_its_function(client):
    answer = {"spoken": "There are no topics yet.", "findings": []}
    with patch("ops_mcp.cli_guide.topics_overview", return_value=answer) as mock_overview:
        assert tool_call(client, "topics_overview", {"limit": 3}) == answer
        tool_call(client, "topics_overview", {})

    assert mock_overview.call_args_list[0].args == (3, None)
    assert mock_overview.call_args_list[1].args == (5, None)


# --- the architecture expert, over the wire ------------------------------------------------------


def test_the_architecture_tools_take_what_the_operator_pasted_and_answer_for_this_environment(
    client, monkeypatch
):
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")
    tools = {tool["name"]: tool for tool in call(client, "tools/list").json()["result"]["tools"]}
    kinds = tools["architecture"]["inputSchema"]["properties"]["kind"]
    assert set(json.dumps(kinds).split('"')) >= set(architecture.KINDS)

    answer = tool_call(client, "architecture", {"name": "bloggerbear-prod-candidate-ideas"})
    assert answer["matches"][0]["name"] == "bloggerbear-dev-candidate-ideas"
    assert answer["rewritten"] is True and answer["data_allowed"] is True

    runsheet = tool_call(client, "investigate", {"symptom": "any 400 errors in the logs?", "status": 400})
    assert runsheet["runsheet"]["id"] == "api-errors"
    assert all(card["kind"] == "how_to" for card in runsheet["findings"])


def test_the_architecture_tools_are_not_passed_through_the_memory(client):
    with patch("ops_mcp.memory.remember") as mock_remember:
        tool_call(client, "architecture", {"name": "topics"})
        tool_call(client, "investigate", {"symptom": "api-errors"})

    mock_remember.assert_not_called()
