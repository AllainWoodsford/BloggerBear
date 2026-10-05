"""The `assistant_access` switch (ops_mcp/access.py): the rule, the middleware, and the real app.

Three layers, each against what it would really meet: `decide` with plain values; the middleware
around a tiny web app that is nothing to do with MCP (it has to serve a second app later), with
the setting in a moto config table; and the MCP server's own app, where a refused request must
never reach a tool.
"""

from __future__ import annotations

import asyncio
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


# --- the agent vouching for the operator's address -----------------------------------------------

KEY = "k" * 40
LAMBDAS_OWN = "198.51.100.200"  # where the agent's own request comes from: on nobody's list


def vouched(address, *, key=KEY, own=LAMBDAS_OWN) -> list[tuple[bytes, bytes]]:
    headers = context_header(own)
    if key is not None:
        headers["x-ops-agent-key"] = key
    if address is not None:
        headers["x-ops-caller-address"] = address
    return as_asgi(headers)


def test_a_request_with_the_key_is_judged_by_the_address_it_vouches_for():
    assert access.caller_address(vouched("203.0.113.7"), KEY) == "203.0.113.7"
    assert access.decide("allowlist", access.caller_address(vouched("203.0.113.7"), KEY), ALLOWLIST)[0]
    # The key admits nobody by itself: the address it vouches for must still be on the list.
    assert not access.decide("allowlist", access.caller_address(vouched("198.51.100.9"), KEY), ALLOWLIST)[0]


@pytest.mark.parametrize("offered", ["wrong" * 8, "", "k" * 39, None])
def test_without_the_right_key_the_vouched_address_is_ignored(offered):
    """A signed-in caller who writes their own x-ops-caller-address gets nowhere."""
    assert access.caller_address(vouched("203.0.113.7", key=offered), KEY) == LAMBDAS_OWN


def test_with_no_key_configured_nothing_is_ever_taken_as_the_agents(monkeypatch):
    monkeypatch.delenv("OPS_AGENT_FORWARD_KEY", raising=False)
    assert access.forward_key_from_env() == ""
    assert access.caller_address(vouched("203.0.113.7", key=""), "") == LAMBDAS_OWN
    assert access.forwarding_headers("203.0.113.7") == {}


def test_a_key_too_short_to_be_a_secret_does_not_count(monkeypatch):
    monkeypatch.setenv("OPS_AGENT_FORWARD_KEY", "short")
    assert access.forward_key_from_env() == ""
    assert access.forwarding_headers("203.0.113.7") == {}


def test_the_key_with_no_address_or_two_is_an_unknown_address():
    assert access.caller_address(vouched(None), KEY) is None
    twice = vouched("203.0.113.7") + [(b"x-ops-caller-address", b"192.0.2.1")]
    assert access.caller_address(twice, KEY) is None
    two_keys = vouched("203.0.113.7") + [(b"x-ops-agent-key", KEY.encode())]
    assert access.caller_address(two_keys, KEY) == LAMBDAS_OWN  # two keys: not the agent


def test_the_agents_headers_carry_the_key_and_the_address(monkeypatch):
    monkeypatch.setenv("OPS_AGENT_FORWARD_KEY", KEY)
    assert access.forwarding_headers(" 203.0.113.7 ") == {
        "x-ops-agent-key": KEY,
        "x-ops-caller-address": "203.0.113.7",
    }
    assert access.forwarding_headers(None) == {} and access.forwarding_headers("") == {}


@pytest.mark.parametrize(
    "source_ip",
    [
        "not an address",
        "203.0.113.7, 198.51.100.9",  # a list, as X-Forwarded-For would hold
        "203.0.113.7\r\nx-ops-agent-key: other",  # a line break would start a header of its own
        "203.0.113.0/24",
        "203.0.113.7:443",
        "２０３.0.113.7",  # digits that are not ASCII
        "9" * 5000,
        203,
        ["203.0.113.7"],
    ],
    ids=["words", "two", "line_break", "block", "with_port", "not_ascii", "oversized", "number", "list"],
)
def test_only_an_ip_address_is_ever_sent_as_the_vouched_address(source_ip):
    assert access.forwarding_headers(source_ip, KEY) == {}


def test_the_vouched_address_is_sent_as_ipaddress_writes_it():
    sent = access.forwarding_headers("2001:0DB8:AAAA:0000:0000:0000:0000:0001", KEY)
    assert sent["x-ops-caller-address"] == "2001:db8:aaaa::1"
    # An IPv4 address written the IPv6 way goes as the IPv4 address `decide` would judge it as.
    assert access.forwarding_headers("::ffff:203.0.113.7", KEY)["x-ops-caller-address"] == "203.0.113.7"


@pytest.mark.parametrize(
    "key",
    [
        "k" * 31,
        "k" * 20 + " " + "k" * 20,
        "k" * 40 + "\r\nx-ops-caller-address: 203.0.113.7",
        "k" * 40 + "\n",
        "é" * 40,
        "k" * 40 + "\x00",
    ],
)
def test_a_key_that_could_not_be_a_header_value_is_no_key_on_either_side(key):
    """Short, or holding a space, a line break or anything not printable ASCII: the agent sends
    nothing, and the server takes no request as the agent's, even one offering that same key."""
    assert access.forwarding_headers("203.0.113.7", key) == {}
    offered = context_header(LAMBDAS_OWN) | {"x-ops-caller-address": "203.0.113.7"}
    headers = as_asgi(offered) + [(b"x-ops-agent-key", key.encode())]
    assert access.caller_address(headers, key) == LAMBDAS_OWN


@pytest.mark.parametrize("stored", ["k" * 20 + " " + "k" * 20, "é" * 40, "k" * 40 + "\tx"])
def test_such_a_key_in_the_environment_is_no_key(stored, monkeypatch):
    monkeypatch.setenv("OPS_AGENT_FORWARD_KEY", stored)
    assert access.forward_key_from_env() == ""
    assert access.forwarding_headers("203.0.113.7") == {}


def test_space_around_the_key_in_the_environment_is_not_part_of_it(monkeypatch):
    monkeypatch.setenv("OPS_AGENT_FORWARD_KEY", f"  {KEY}\n")
    assert access.forward_key_from_env() == KEY


@pytest.mark.parametrize(
    "offered",
    [
        b"\xff\xfe\x00 not text at all",
        "é".encode() * 40,
        KEY.encode() + b"\x00",
        KEY.encode() * 2000,  # far longer than any header API Gateway lets through
        b"",
    ],
    ids=["not_text", "not_ascii", "key_then_nul", "oversized", "empty"],
)
def test_a_key_header_of_any_bytes_or_length_is_just_the_wrong_key(offered):
    headers = as_asgi(context_header(LAMBDAS_OWN)) + [
        (b"x-ops-agent-key", offered),
        (b"x-ops-caller-address", b"203.0.113.7"),
    ]
    assert access.caller_address(headers, KEY) == LAMBDAS_OWN


@pytest.mark.parametrize(
    "address",
    [b"\xff\xfe", "２０３.0.113.7".encode(), b"203.0.113.7, 192.0.2.1", b"9" * 100_000, b""],
    ids=["not_text", "not_ascii", "a_list", "oversized", "empty"],
)
def test_with_the_right_key_a_vouched_address_that_is_not_one_is_refused_not_raised(address):
    headers = as_asgi(context_header("203.0.113.7")) + [  # the request's own address is listed
        (b"x-ops-agent-key", KEY.encode()),
        (b"x-ops-caller-address", address),
    ]
    # The key was right, so the request's own address is not looked at: the agent vouched badly.
    assert access.decide("allowlist", access.caller_address(headers, KEY), ALLOWLIST) == (
        False,
        access.ADDRESS_UNKNOWN,
    )


def through_the_middleware(app, headers: list[tuple[bytes, bytes]]) -> int:
    """One GET straight into the ASGI app, with header bytes no HTTP client would agree to send.
    Returns the status."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": headers}
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_through_the_app_allowlist_admits_the_agent_vouching_for_a_listed_address(
    guarded, monkeypatch, capsys
):
    inner, client = guarded
    monkeypatch.setenv("OPS_AGENT_FORWARD_KEY", KEY)
    put_pipeline_config(assistant_access="allowlist")

    def get(address, key=KEY, own=LAMBDAS_OWN):
        headers = {**context_header(own), "x-ops-caller-address": address}
        if key:
            headers["x-ops-agent-key"] = key
        return client.get("/", headers=headers)

    assert get("203.0.113.7").status_code == 200  # the operator, through the agent
    assert inner.reached == 1
    assert capsys.readouterr().out == ""  # admitted: nothing is logged, the key least of all

    refusals = [
        get("198.51.100.9"),  # someone else, through the agent
        get("203.0.113.7", key="wrong" * 8),  # a caller claiming to be the agent
        get("203.0.113.7", key=""),  # a caller who wrote the address header themselves
        get("not an address"),
        get("198.51.100.9", own="203.0.113.7"),  # the key is right: its address is the one judged
    ]

    assert [response.status_code for response in refusals] == [403] * 5
    assert inner.reached == 1
    # Neither the key nor either address comes back or is logged, whichever rule refused.
    printed = capsys.readouterr().out
    assert printed.splitlines() == [
        f"ops_access: test request refused ({access.ADDRESS_NOT_LISTED})",
        f"ops_access: test request refused ({access.ADDRESS_NOT_LISTED})",
        f"ops_access: test request refused ({access.ADDRESS_NOT_LISTED})",
        f"ops_access: test request refused ({access.ADDRESS_UNKNOWN})",
        f"ops_access: test request refused ({access.ADDRESS_NOT_LISTED})",
    ]
    for response in refusals:
        assert response.json() == {"error": "forbidden"}
        assert KEY not in response.text and KEY not in str(response.headers)


def test_through_the_app_the_key_does_nothing_when_the_assistant_is_off(guarded, monkeypatch):
    inner, client = guarded
    monkeypatch.setenv("OPS_AGENT_FORWARD_KEY", KEY)
    put_pipeline_config(assistant_access="off")
    headers = {**context_header(LAMBDAS_OWN), "x-ops-agent-key": KEY, "x-ops-caller-address": "203.0.113.7"}

    assert client.get("/", headers=headers).status_code == 403
    assert inner.reached == 0


@pytest.mark.parametrize(
    "extra",
    [
        [(b"x-ops-agent-key", b"\xff\xfe\x00"), (b"x-ops-caller-address", b"203.0.113.7")],
        [(b"x-ops-agent-key", KEY.encode() * 2000), (b"x-ops-caller-address", b"203.0.113.7")],
        [(b"x-ops-agent-key", KEY.encode()), (b"x-ops-caller-address", b"\xff\xfe")],
        [(b"x-ops-agent-key", KEY.encode()), (b"x-ops-caller-address", b"9" * 100_000)],
        [(b"x-ops-agent-key", "é".encode() * 40), (b"x-ops-caller-address", "é".encode())],
    ],
    ids=["key_not_text", "key_oversized", "address_not_text", "address_oversized", "both_not_ascii"],
)
def test_through_the_app_headers_that_are_not_text_or_are_huge_are_refused_not_raised(
    guarded, monkeypatch, extra
):
    inner, _ = guarded
    monkeypatch.setenv("OPS_AGENT_FORWARD_KEY", KEY)
    put_pipeline_config(assistant_access="allowlist")
    app = access.AccessMiddleware(inner, label="test")

    assert through_the_middleware(app, as_asgi(context_header(LAMBDAS_OWN)) + extra) == 403
    assert inner.reached == 0


def test_a_key_check_that_raises_is_a_refusal(guarded, capsys):
    """Whatever goes wrong while working out whose address to judge, nobody gets in by it."""
    inner, _ = guarded
    put_pipeline_config(assistant_access="allowlist")

    def broken() -> str:
        raise RuntimeError("no key to be had")

    app = access.AccessMiddleware(inner, label="test", forward_key=broken)

    assert through_the_middleware(app, as_asgi(context_header("203.0.113.7"))) == 403
    assert inner.reached == 0
    assert capsys.readouterr().out.strip() == f"ops_access: test request refused ({access.CONFIG_UNREADABLE})"
