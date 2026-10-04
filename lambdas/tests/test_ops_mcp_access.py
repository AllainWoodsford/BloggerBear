"""The `assistant_access` switch (ops_mcp/access.py): the rule, the middleware, and the real app.

Three layers, each against what it would really meet: `decide` with plain values; the middleware
around a tiny web app that is nothing to do with MCP (it has to serve a second app later), with
the setting in a moto config table; and the MCP server's own app, where a refused request must
never reach a tool.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from starlette.testclient import TestClient
from table_schemas import create_table

from common.dynamo import put_pipeline_config
from ops_mcp import access, server

REGION = "ap-southeast-2"
HOST = "ops.example.test"
HOME_V4 = "203.0.113.7"
HOME_V6 = "2001:db8:aaaa::17"
ELSEWHERE_V4 = "198.51.100.23"
ELSEWHERE_V6 = "2001:db8:bbbb::1"
ALLOWLIST = ["203.0.113.7", "192.0.2.0/24", "2001:db8:aaaa::/48"]


# --- decide: the rule ----------------------------------------------------------------------


@pytest.mark.parametrize("setting", [None, "open"])
@pytest.mark.parametrize("source_ip", [HOME_V4, ELSEWHERE_V4, ELSEWHERE_V6, None, "not an address"])
def test_no_setting_and_open_admit_any_address(setting, source_ip):
    assert access.decide(setting, source_ip, []) == (True, access.OPEN)
    assert access.decide(setting, source_ip, ALLOWLIST) == (True, access.OPEN)


@pytest.mark.parametrize(
    "source_ip",
    [
        HOME_V4,  # a single IPv4 address
        "192.0.2.200",  # inside an IPv4 block
        HOME_V6,  # inside an IPv6 block
        "2001:DB8:AAAA:0:0:0:0:17",  # the same address, written differently
        "::ffff:203.0.113.7",  # the IPv4 address, written the IPv6 way
        f" {HOME_V4} ",
    ],
)
def test_allowlist_admits_a_listed_address(source_ip):
    assert access.decide("allowlist", source_ip, ALLOWLIST) == (True, access.ADDRESS_LISTED)


def test_allowlist_admits_a_single_ipv6_address_and_a_block_written_from_inside_it():
    assert access.decide("allowlist", HOME_V6, [HOME_V6])[0] is True
    assert access.decide("allowlist", "192.0.2.9", ["192.0.2.77/24"])[0] is True


@pytest.mark.parametrize("source_ip", [ELSEWHERE_V4, ELSEWHERE_V6, "203.0.113.8", "192.0.3.1"])
def test_allowlist_refuses_an_address_that_is_not_listed(source_ip):
    assert access.decide("allowlist", source_ip, ALLOWLIST) == (False, access.ADDRESS_NOT_LISTED)


def test_an_ipv4_address_is_not_in_an_ipv6_block_or_the_reverse():
    assert access.decide("allowlist", HOME_V4, ["::/0"]) == (False, access.ADDRESS_NOT_LISTED)
    assert access.decide("allowlist", HOME_V6, ["0.0.0.0/0"]) == (False, access.ADDRESS_NOT_LISTED)


@pytest.mark.parametrize("allowed", [[], None, [""], ["  ", ""]])
def test_allowlist_with_nothing_listed_refuses(allowed):
    assert access.decide("allowlist", HOME_V4, allowed) == (False, access.ALLOWLIST_EMPTY)


@pytest.mark.parametrize(
    "allowed",
    [["home"], [HOME_V4, "203.0.113.0/33"], [HOME_V4, "10.0.0.0/8 or so"], [HOME_V4, None], HOME_V4],
)
def test_one_entry_that_is_not_an_address_spoils_the_whole_allowlist(allowed):
    """Even when the caller's own address is on it: a list half read is a mistake to surface."""
    assert access.decide("allowlist", HOME_V4, allowed) == (False, access.ALLOWLIST_UNPARSEABLE)


@pytest.mark.parametrize("source_ip", [None, "", "  ", "unknown", "203.0.113.7, 198.51.100.23", 7])
def test_allowlist_refuses_a_caller_whose_address_is_not_known(source_ip):
    assert access.decide("allowlist", source_ip, ALLOWLIST) == (False, access.ADDRESS_UNKNOWN)


@pytest.mark.parametrize("source_ip", [HOME_V4, HOME_V6, ELSEWHERE_V4, None])
def test_off_refuses_everything(source_ip):
    assert access.decide("off", source_ip, ALLOWLIST) == (False, access.SWITCHED_OFF)


@pytest.mark.parametrize("setting", ["", "Open", "OPEN", "allow-list", "on", "closed", True, 1, ["open"]])
def test_a_setting_that_is_not_one_of_the_three_refuses(setting):
    """Never the default: the default is the most permissive value."""
    assert access.decide(setting, HOME_V4, ALLOWLIST) == (False, access.UNKNOWN_SETTING)


# --- where the address comes from ----------------------------------------------------------


def context_header(source_ip, *, section="identity") -> dict:
    """The header the Lambda Web Adapter adds: API Gateway's request context, as JSON."""
    return {"x-amzn-request-context": json.dumps({"requestId": "r-1", section: {"sourceIp": source_ip}})}


def as_asgi(headers: dict) -> list[tuple[bytes, bytes]]:
    return [(name.lower().encode(), value.encode()) for name, value in headers.items()]


def test_the_address_is_read_from_a_rest_api_or_an_http_api_request_context():
    assert access.source_ip_from_headers(as_asgi(context_header(HOME_V4))) == HOME_V4
    assert access.source_ip_from_headers(as_asgi(context_header(HOME_V6, section="http"))) == HOME_V6


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-forwarded-for": HOME_V4, "forwarded": f"for={HOME_V4}", "x-real-ip": HOME_V4},
        {"x-amzn-request-context": "not json"},
        {"x-amzn-request-context": json.dumps(HOME_V4)},
        {"x-amzn-request-context": json.dumps({"identity": HOME_V4})},
        {"x-amzn-request-context": json.dumps({"identity": {"sourceIp": None}})},
        {"x-amzn-request-context": json.dumps({"sourceIp": HOME_V4})},
    ],
)
def test_without_a_usable_request_context_the_address_is_unknown(headers):
    assert access.source_ip_from_headers(as_asgi(headers)) is None


def test_two_request_context_headers_are_trusted_no_more_than_none():
    one = (b"x-amzn-request-context", json.dumps({"identity": {"sourceIp": HOME_V4}}).encode())

    assert access.source_ip_from_headers([one, one]) is None


def test_the_allowlist_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(access.ALLOWED_CIDRS_ENV, f" {HOME_V4} , 192.0.2.0/24,, 2001:db8:aaaa::/48 ,")
    assert access.allowed_cidrs_from_env() == ALLOWLIST

    monkeypatch.delenv(access.ALLOWED_CIDRS_ENV)
    assert access.allowed_cidrs_from_env() == []


# --- the middleware, around an app that is nothing to do with MCP --------------------------


@pytest.fixture
def config_table(monkeypatch):
    """The config table, empty: no `pipeline` row, so every setting takes its default."""
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MODEL_CONFIG_TABLE": "ModelConfig",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(access.ALLOWED_CIDRS_ENV, ",".join(ALLOWLIST))
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        create_table(
            boto3.client("dynamodb", region_name=REGION),
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig")
    dynamo_module._dynamodb_resource = None


class Inner:
    """A web app that answers 200 and counts the requests that reached it."""

    def __init__(self):
        self.reached = 0

    async def __call__(self, scope, receive, send):
        assert scope["type"] == "http"
        self.reached += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inside"})


@pytest.fixture
def guarded(config_table):
    inner = Inner()
    return inner, TestClient(access.AccessMiddleware(inner, label="test"))


def test_with_no_setting_stored_any_address_is_admitted(guarded):
    inner, client = guarded

    assert client.get("/", headers=context_header(ELSEWHERE_V4)).status_code == 200
    assert client.get("/").status_code == 200  # and a caller with no address at all
    assert inner.reached == 2


def test_open_admits_any_address_and_costs_one_read_of_the_setting(config_table, monkeypatch):
    put_pipeline_config(assistant_access="open")
    monkeypatch.delenv(access.ALLOWED_CIDRS_ENV)  # open needs no allowlist
    reads = []

    def read_config():
        reads.append(1)
        return access.get_pipeline_config()

    inner = Inner()
    client = TestClient(access.AccessMiddleware(inner, read_config=read_config))

    with patch.object(access, "decide", side_effect=AssertionError("open looks at nothing else")):
        assert client.get("/", headers=context_header(ELSEWHERE_V6)).status_code == 200

    assert inner.reached == 1 and len(reads) == 1


def test_allowlist_admits_listed_addresses_and_refuses_others(guarded):
    inner, client = guarded
    put_pipeline_config(assistant_access="allowlist")

    for listed in (HOME_V4, "192.0.2.200", HOME_V6):
        assert client.get("/", headers=context_header(listed)).status_code == 200
    assert client.get("/", headers=context_header(HOME_V6, section="http")).status_code == 200
    assert inner.reached == 4

    for other in (ELSEWHERE_V4, ELSEWHERE_V6):
        assert client.get("/", headers=context_header(other)).status_code == 403
    assert inner.reached == 4


@pytest.mark.parametrize("cidrs", [None, "", " , ", "home", f"{HOME_V4},nonsense"])
def test_allowlist_with_an_empty_or_unreadable_list_refuses_the_operator_too(guarded, monkeypatch, cidrs):
    inner, client = guarded
    put_pipeline_config(assistant_access="allowlist")
    if cidrs is None:
        monkeypatch.delenv(access.ALLOWED_CIDRS_ENV)
    else:
        monkeypatch.setenv(access.ALLOWED_CIDRS_ENV, cidrs)

    assert client.get("/", headers=context_header(HOME_V4)).status_code == 403
    assert inner.reached == 0


def test_allowlist_refuses_when_the_address_is_unknown(guarded):
    inner, client = guarded
    put_pipeline_config(assistant_access="allowlist")

    assert client.get("/").status_code == 403  # not behind the adapter: no request context
    assert client.get("/", headers={"x-amzn-request-context": "{"}).status_code == 403
    assert client.get("/", headers=context_header("somewhere")).status_code == 403
    assert inner.reached == 0


def test_a_spoofed_forwarded_for_does_not_help(guarded):
    inner, client = guarded
    put_pipeline_config(assistant_access="allowlist")
    spoof = {"X-Forwarded-For": HOME_V4, "Forwarded": f"for={HOME_V4}", "X-Real-IP": HOME_V4}

    assert client.get("/", headers=spoof).status_code == 403
    assert client.get("/", headers={**spoof, **context_header(ELSEWHERE_V4)}).status_code == 403
    assert inner.reached == 0
    # And it does no harm to the operator, whose real address is the request context's.
    lying_about_home = {"X-Forwarded-For": ELSEWHERE_V4, **context_header(HOME_V4)}
    assert client.get("/", headers=lying_about_home).status_code == 200


def test_off_refuses_every_request(guarded):
    inner, client = guarded
    put_pipeline_config(assistant_access="off")

    for headers in (context_header(HOME_V4), context_header(HOME_V6), {}):
        assert client.get("/", headers=headers).status_code == 403
        assert client.post("/anything", headers=headers, content="{}").status_code == 403
    assert inner.reached == 0


@pytest.mark.parametrize("stored", ["Open", "allow-list", "", "on", 1, True, ["open"], {"mode": "open"}])
def test_a_stored_value_that_is_not_one_of_the_three_refuses(guarded, config_table, stored):
    """Written straight to the table, since the admin API would never store it."""
    inner, client = guarded
    config_table.put_item(Item={"config_id": "pipeline", "assistant_access": stored})

    assert client.get("/", headers=context_header(HOME_V4)).status_code == 403
    assert inner.reached == 0


def test_a_config_read_that_fails_refuses(config_table, capsys):
    def read_config():
        raise RuntimeError(f"could not reach the table from {HOME_V4}")

    inner = Inner()
    client = TestClient(access.AccessMiddleware(inner, read_config=read_config))

    response = client.get("/", headers=context_header(HOME_V4))

    assert response.status_code == 403
    assert inner.reached == 0
    logged = capsys.readouterr().out
    assert access.CONFIG_UNREADABLE in logged and HOME_V4 not in logged


def test_a_missing_config_table_refuses(guarded, monkeypatch):
    """The real read, failing the way a deployment without the table or the permission would."""
    inner, client = guarded
    monkeypatch.setenv("MODEL_CONFIG_TABLE", "NoSuchTable")
    assert client.get("/", headers=context_header(HOME_V4)).status_code == 403

    monkeypatch.delenv("MODEL_CONFIG_TABLE")
    assert client.get("/", headers=context_header(HOME_V4)).status_code == 403
    assert inner.reached == 0


def test_a_change_of_setting_applies_to_the_very_next_request(guarded):
    """Nothing is remembered between requests, so a lock-down does not wait for anything."""
    inner, client = guarded
    assert client.get("/", headers=context_header(ELSEWHERE_V4)).status_code == 200

    put_pipeline_config(assistant_access="off")
    assert client.get("/", headers=context_header(ELSEWHERE_V4)).status_code == 403

    put_pipeline_config(assistant_access=None)  # cleared: back to the default
    assert client.get("/", headers=context_header(ELSEWHERE_V4)).status_code == 200
    assert inner.reached == 2


def test_a_refusal_says_the_same_thing_whatever_the_reason_and_never_the_address(
    guarded, monkeypatch, capsys
):
    inner, client = guarded
    refusals = []

    put_pipeline_config(assistant_access="off")
    refusals.append((client.get("/", headers=context_header(HOME_V4)), access.SWITCHED_OFF))
    put_pipeline_config(assistant_access="allowlist")
    refusals.append((client.get("/", headers=context_header(ELSEWHERE_V4)), access.ADDRESS_NOT_LISTED))
    refusals.append((client.get("/"), access.ADDRESS_UNKNOWN))
    monkeypatch.setenv(access.ALLOWED_CIDRS_ENV, "")
    refusals.append((client.get("/", headers=context_header(HOME_V4)), access.ALLOWLIST_EMPTY))

    logged = capsys.readouterr().out.splitlines()
    assert len(logged) == len(refusals)  # one line each
    for (response, reason), line in zip(refusals, logged, strict=True):
        assert response.status_code == 403
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"error": "forbidden"}
        assert reason in line and "test" in line
        for told in (HOME_V4, ELSEWHERE_V4, "203.0.113", "allowlist", "off", reason):
            assert told not in response.text
        for address in (HOME_V4, ELSEWHERE_V4):
            assert address not in line


def test_an_admitted_request_logs_nothing(guarded, capsys):
    _, client = guarded

    assert client.get("/", headers=context_header(HOME_V4)).status_code == 200
    assert capsys.readouterr().out == ""


# --- the real app: a refused request never reaches a tool ----------------------------------

MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "access-test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture
def mcp_client(config_table, monkeypatch):
    monkeypatch.setenv("OPS_MCP_ALLOWED_HOSTS", HOST)
    with TestClient(server.create_app(), base_url=f"http://{HOST}") as test_client:
        yield test_client


def call_inbox(client, headers=None):
    """A tools/call of admin_inbox, with the tool itself replaced so its being reached shows."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "admin_inbox", "arguments": {}, "_meta": META},
    }
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "admin_inbox",
        **(headers or {}),
    }
    answer = {"spoken": "Nothing is waiting in the inbox.", "findings": [], "waiting": 0, "items": []}
    with patch("ops_mcp.tools.admin_inbox", return_value=answer) as tool:
        response = client.post(server.MCP_PATH, content=json.dumps(body), headers=request_headers)
    return response, tool


def test_through_the_real_app_an_open_setting_reaches_the_tool(mcp_client):
    response, tool = call_inbox(mcp_client, context_header(ELSEWHERE_V4))

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    tool.assert_called_once()


def test_through_the_real_app_a_refused_request_never_reaches_a_tool(mcp_client):
    put_pipeline_config(assistant_access="off")
    response, tool = call_inbox(mcp_client, context_header(HOME_V4))
    assert response.status_code == 403 and response.json() == {"error": "forbidden"}
    tool.assert_not_called()

    put_pipeline_config(assistant_access="allowlist")
    response, tool = call_inbox(mcp_client, {"X-Forwarded-For": HOME_V4, **context_header(ELSEWHERE_V4)})
    assert response.status_code == 403 and ELSEWHERE_V4 not in response.text
    tool.assert_not_called()

    response, tool = call_inbox(mcp_client, context_header(HOME_V4))
    assert response.status_code == 200
    tool.assert_called_once()


def test_through_the_real_app_every_path_and_method_is_refused_when_off(mcp_client):
    put_pipeline_config(assistant_access="off")

    assert mcp_client.get(server.MCP_PATH).status_code == 403
    assert mcp_client.get("/").status_code == 403
    assert mcp_client.delete(server.MCP_PATH).status_code == 403
    # Before the Host check too: a refusal does not depend on anything else about the request.
    assert mcp_client.post(server.MCP_PATH, headers={"Host": "elsewhere.example"}).status_code == 403


def test_through_the_real_app_an_unreadable_setting_refuses(mcp_client, monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG_TABLE", "NoSuchTable")

    response, tool = call_inbox(mcp_client, context_header(HOME_V4))

    assert response.status_code == 403
    tool.assert_not_called()
