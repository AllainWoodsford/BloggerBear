"""The `assistant_access` switch, enforced: who may reach the operator's assistant at all.

The setting is one value on the config table's `pipeline` row (common/assistant_access.py),
changed with `pipeline-config set --assistant-access`, so the operator can lock the assistant
to their own addresses, or switch it off, without a deploy:

    open       any caller the authorizer in front has let through, from anywhere (the default)
    allowlist  only from the addresses in OPS_ASSISTANT_ALLOWED_CIDRS
    off        every request is refused

`decide` is the rule, a pure function. `AccessMiddleware` applies it to every request of a web
app. Neither knows anything about MCP: any ASGI app run under the Lambda Web Adapter behind API
Gateway can be wrapped the same way (the agent endpoint will be).

**Everything that goes wrong refuses.** A setting that can't be read, a value that isn't one of
the three, an allowlist that is empty or has an entry that isn't an address, a caller whose
address isn't known: each is a 403. The only way in is a setting that was read and understood.

**Where the caller's address comes from.** API Gateway puts it in the request context of the
event it hands Lambda, and the Lambda Web Adapter forwards that context to the web app as JSON
in the `x-amzn-request-context` header:

    https://github.com/awslabs/aws-lambda-web-adapter
    docs/guide/src/features/request-context.md ("Request Context": "This is forwarded in the
    `x-amzn-request-context` header as a JSON string", read as `requestContext.identity?.sourceIp`)
    src/lib.rs: `req_headers.insert(HeaderName::from_static("x-amzn-request-context"), ...)`

The adapter *inserts* the header, which replaces one a caller sent under the same name, so behind
the adapter a caller cannot choose its own address. `X-Forwarded-For` is never read: a caller
writes whatever it likes there. Not behind the adapter (a local run) the header is absent and the
address is unknown, which `allowlist` refuses. That also means this check is only as good as the
adapter being the one way to reach the app: it must never be served straight to a network.

A REST API puts the address at `identity.sourceIp`; an HTTP API (payload 2.0) puts it at
`http.sourceIp`. Both are read, since both come from the same trusted header.

    OPS_ASSISTANT_ALLOWED_CIDRS  comma-separated addresses and CIDR blocks, IPv4 or IPv6
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from collections.abc import Callable

from common.assistant_access import ACCESS_ALLOWLIST, ACCESS_OFF, ACCESS_OPEN
from common.dynamo import get_pipeline_config

ALLOWED_CIDRS_ENV = "OPS_ASSISTANT_ALLOWED_CIDRS"
REQUEST_CONTEXT_HEADER = b"x-amzn-request-context"

# Why a request was admitted or refused. They go to the log, never to the caller.
OPEN = "open"
ADDRESS_LISTED = "address_listed"
SWITCHED_OFF = "switched_off"
UNKNOWN_SETTING = "unknown_setting"
ALLOWLIST_EMPTY = "allowlist_empty"
ALLOWLIST_UNPARSEABLE = "allowlist_unparseable"
ADDRESS_UNKNOWN = "address_unknown"
ADDRESS_NOT_LISTED = "address_not_listed"
CONFIG_UNREADABLE = "config_unreadable"

# The same body whatever the reason, so a refusal tells a caller nothing about which rule it
# met, what the allowlist holds or what address it was seen from.
_REFUSAL_BODY = json.dumps({"error": "forbidden"}).encode()


def _address(value):
    """`value` as an address, or None. An IPv4 address written the IPv6 way (`::ffff:1.2.3.4`)
    is the IPv4 address, so it meets the IPv4 entries of the list."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    return getattr(address, "ipv4_mapped", None) or address


def _networks(allowed_cidrs) -> list | None:
    """The allowlist as networks, or None if any entry isn't one. One bad entry spoils the list:
    it is a mistake in the deployment, and admitting on the half that could be read would hide
    it. A single address is a network of one; `203.0.113.7/24` means the /24 it is in."""
    networks = []
    for entry in allowed_cidrs:
        if not isinstance(entry, str):
            return None
        try:
            networks.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError:
            return None
    return networks


def decide(setting, source_ip, allowed_cidrs) -> tuple[bool, str]:
    """Whether a request may go on, and why.

    `setting` is the stored `assistant_access` (None when nothing is stored: open), `source_ip`
    the caller's address as text (None when it isn't known), `allowed_cidrs` the allowlist's
    entries as text.
    """
    if setting is None or setting == ACCESS_OPEN:
        return True, OPEN
    if setting == ACCESS_OFF:
        return False, SWITCHED_OFF
    if setting != ACCESS_ALLOWLIST:
        # Not the default: the default is the most permissive value, and a setting somebody
        # mistyped while trying to lock the assistant down must not open it.
        return False, UNKNOWN_SETTING

    # Blank entries are dropped (a trailing comma); anything else that isn't an address is kept,
    # so that it spoils the list below.
    entries = [entry for entry in allowed_cidrs or [] if not isinstance(entry, str) or entry.strip()]
    if not entries:
        return False, ALLOWLIST_EMPTY
    networks = _networks(entries)
    if networks is None:
        return False, ALLOWLIST_UNPARSEABLE
    address = _address(source_ip)
    if address is None:
        return False, ADDRESS_UNKNOWN
    # An IPv4 address is never in an IPv6 network or the reverse; `in` answers False, not an error.
    if any(address in network for network in networks):
        return True, ADDRESS_LISTED
    return False, ADDRESS_NOT_LISTED


def allowed_cidrs_from_env(name: str = ALLOWED_CIDRS_ENV) -> list[str]:
    return [entry.strip() for entry in os.environ.get(name, "").split(",") if entry.strip()]


def source_ip_from_headers(headers) -> str | None:
    """The caller's address from the request context the Lambda Web Adapter forwards, or None.

    `headers` is an ASGI scope's: (name, value) pairs of bytes, names in lower case. The header
    must appear exactly once: the adapter sets one, so two means something else built the request.
    """
    values = [value for name, value in headers if name == REQUEST_CONTEXT_HEADER]
    if len(values) != 1:
        return None
    try:
        context = json.loads(values[0])
    except ValueError:
        return None
    if not isinstance(context, dict):
        return None
    for section in ("identity", "http"):  # REST API, then HTTP API: see the module docstring
        details = context.get(section)
        if isinstance(details, dict) and isinstance(details.get("sourceIp"), str):
            return details["sourceIp"]
    return None


class AccessMiddleware:
    """Refuses a request the `assistant_access` setting doesn't admit, before the app sees it.

    Plain ASGI, so it wraps any web app: `app.add_middleware(AccessMiddleware)` in Starlette, or
    `AccessMiddleware(app)`. `label` names the app in the log line.

    **The setting is read on every request and never cached.** A lock-down therefore applies
    to the very next request: as soon as DynamoDB's ordinary (eventually consistent) read shows
    the write, which is normally well under a second. Remembering the setting for a few seconds
    in each warm Lambda would save one GetItem of one small row, a few milliseconds, on requests
    whose tools go on to make many reads of their own; and it would cost the one thing the
    switch is for, since `off` would then leave each warm copy answering until its own memory
    ran out. One operator asking a few questions a day is not traffic worth that trade.
    """

    def __init__(
        self,
        app,
        *,
        read_config: Callable[[], dict | None] = get_pipeline_config,
        allowed_cidrs: Callable[[], list[str]] = allowed_cidrs_from_env,
        label: str = "assistant",
    ) -> None:
        self.app = app
        self._read_config = read_config
        self._allowed_cidrs = allowed_cidrs
        self._label = label

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":  # the server starting and stopping: nobody is calling
            await self.app(scope, receive, send)
            return

        allowed, reason = await self._check(scope)
        if allowed:
            await self.app(scope, receive, send)
            return

        # One line, with the reason and nothing about the caller: no address, no path, no token.
        print(f"ops_access: {self._label} request refused ({reason})")
        if scope["type"] != "http":  # a websocket: none is served, so close it unanswered
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_REFUSAL_BODY)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _REFUSAL_BODY})

    async def _check(self, scope) -> tuple[bool, str]:
        try:
            # boto3 blocks, so off the event loop it goes (the SDK runs the tools the same way).
            config = await asyncio.to_thread(self._read_config)
            setting = (config or {}).get("assistant_access")
        except Exception:  # noqa: BLE001 - whatever went wrong, the answer is the same
            return False, CONFIG_UNREADABLE
        if setting is None or setting == ACCESS_OPEN:
            return True, OPEN  # nothing else is looked at: open costs the one read above
        try:
            return decide(setting, source_ip_from_headers(scope.get("headers") or []), self._allowed_cidrs())
        except Exception:  # noqa: BLE001 - a check that fails is a refusal, never a way in
            return False, CONFIG_UNREADABLE
