"""Security events Lambda handler: turns the regional WAFs' BLOCK log records into incidents.

Invoked by a CloudWatch Logs subscription filter on each regional WAF log group (public API and
admin API), filtered to `{ $.action = "BLOCK" }`. Each invocation carries a gzipped, base64-encoded
batch of log events from one log group. The records in a batch are grouped by rule, client and
15-minute window first, so a burst of 300 blocked requests is one DynamoDB write, not 300; then
common/security_events.py's record_incident stores (or adds to) each incident, escalates it, and
raises the alarm once for a high-severity one.

The client: behind the API's CloudFront distribution every request comes from an edge address, and
the visitor's own is in `x-viewer-ip`, set by the distribution's function. That header is only
believed when `x-origin-verify` is present too (redacted in the log, but still listed), the same
rule the WAF's own per-visitor rate limits follow; otherwise the connection's address is used.

Never raises unhandled: a failed batch is logged, and CloudWatch Logs does not retry it.
"""

from __future__ import annotations

import base64
import gzip
import json
from datetime import UTC, datetime

from common.security_events import (
    WAF_ADMIN_API,
    WAF_OTHER,
    WAF_PUBLIC_API,
    WINDOW_MINUTES,
    record_incident,
)


def _source(log_group: str) -> str:
    if log_group.endswith("-public-api"):
        return WAF_PUBLIC_API
    if log_group.endswith("-admin"):
        return WAF_ADMIN_API
    return WAF_OTHER


def _headers(request: dict) -> dict[str, str]:
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in request.get("headers") or []
        if isinstance(header, dict)
    }


def parse_waf_record(message: str) -> dict | None:
    """One WAF log record as what an incident needs, or None if it isn't a usable BLOCK record."""
    try:
        record = json.loads(message)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("action") != "BLOCK":
        return None
    rule = str(record.get("terminatingRuleId") or "unknown")
    for group in record.get("ruleGroupList") or []:
        inner = (group or {}).get("terminatingRule") or {}
        if inner.get("ruleId"):
            rule = f"{rule}/{inner['ruleId']}"
            break
    request = record.get("httpRequest") or {}
    headers = _headers(request)
    viewer_ip = headers.get("x-viewer-ip") if "x-origin-verify" in headers else None
    client_ip = viewer_ip or request.get("clientIp")
    matched = " ".join(
        str(piece)
        for detail in record.get("terminatingRuleMatchDetails") or []
        for piece in (detail or {}).get("matchedData") or []
    )
    try:
        at = datetime.fromtimestamp(int(record["timestamp"]) / 1000, UTC)
    except (KeyError, TypeError, ValueError):
        at = datetime.now(UTC)
    return {
        "rule": rule,
        "client_ip": str(client_ip or ""),
        "at": at,
        "method": str(request.get("httpMethod") or ""),
        "path": str(request.get("uri") or ""),
        "country": str(request.get("country") or ""),
        "matched": matched,
    }


def _window_key(parsed: dict) -> tuple:
    at = parsed["at"]
    window = at.replace(minute=at.minute - at.minute % WINDOW_MINUTES, second=0, microsecond=0)
    return (parsed["rule"], parsed["client_ip"], window)


# Not wrapped in track_lambda_duration: that is the Stats page's scheduled-pipeline run time, and
# this runs whenever the WAF blocks something.
def handler(event, context) -> dict:
    try:
        payload = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    except Exception as exc:  # noqa: BLE001 - a malformed batch is logged, never raised
        print(f"security_events_handler: could not read the log batch: {exc!r}")
        return {"status": "error", "error": str(exc)}
    if payload.get("messageType") != "DATA_MESSAGE":
        return {"status": "skipped", "reason": payload.get("messageType")}

    source = _source(str(payload.get("logGroup", "")))
    groups: dict[tuple, dict] = {}
    for log_event in payload.get("logEvents") or []:
        parsed = parse_waf_record(log_event.get("message", ""))
        if parsed is None:
            continue
        group = groups.setdefault(_window_key(parsed), {**parsed, "count": 0, "first_at": parsed["at"]})
        group["count"] += 1
        group["at"] = max(group["at"], parsed["at"])
        group["first_at"] = min(group["first_at"], parsed["at"])

    recorded = 0
    for group in groups.values():
        if record_incident(
            source=source,
            rule=group["rule"],
            client_ip=group["client_ip"],
            at=group["at"],
            first_at=group["first_at"],
            count=group["count"],
            method=group["method"],
            path=group["path"],
            country=group["country"],
            matched=group["matched"],
        ):
            recorded += 1
    return {"status": "recorded", "source": source, "incidents": recorded, "groups": len(groups)}
