"""Tests for common/security_events.py and security_events_handler.py: blocked requests grouped into
incidents, with a category, a severity, next steps, a hashed client (never an IP), a 120-day TTL,
client-written text kept apart as untrusted, and one alert per high-severity incident."""

from __future__ import annotations

import base64
import gzip
import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from table_schemas import create_table

import security_events_handler
from common import security_events as se

REGION = "ap-southeast-2"
AT = datetime(2026, 10, 2, 3, 7, tzinfo=UTC)


@pytest.fixture
def tables(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "SECURITY_EVENTS_TABLE": "SecurityEvents",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "ENVIRONMENT_NAME": "production",
    }.items():
        monkeypatch.setenv(key, value)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, key in (("SecurityEvents", "event_id"), ("ModelConfig", "config_id")):
            create_table(
                client,
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield boto3.resource("dynamodb", region_name=REGION).Table("SecurityEvents")


def _rows(table):
    return table.scan()["Items"]


def _record(**overrides):
    values = {
        "source": se.WAF_PUBLIC_API,
        "rule": "aws-managed-common/CrossSiteScripting_BODY",
        "client_ip": "203.0.113.9",
        "at": AT,
        "method": "POST",
        "path": "/articles/a1/feedback",
        "country": "AU",
        "matched": "<script>alert(1)</script>",
    }
    return se.record_incident(**{**values, **overrides})


# --- classification --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source, rule, category",
    [
        (se.WAF_PUBLIC_API, "aws-managed-common/CrossSiteScripting_BODY", "xss"),
        (se.WAF_PUBLIC_API, "aws-managed-common/SizeRestrictions_BODY", "oversized-request"),
        (se.WAF_PUBLIC_API, "aws-managed-common/NoUserAgent_HEADER", "bad-bot"),
        (se.WAF_PUBLIC_API, "aws-managed-common/GenericLFI_URIPATH", "file-inclusion"),
        (se.WAF_PUBLIC_API, "aws-managed-common/EC2MetaDataSSRF_BODY", "ssrf"),
        (se.WAF_PUBLIC_API, "sqli/SQLi_QUERYARGUMENTS", "sqli"),
        (se.WAF_PUBLIC_API, "bad-inputs/Log4JRCE_HEADER", "rce"),
        (se.WAF_PUBLIC_API, "rate-limit-via-cdn", "rate-limit"),
        (se.WAF_PUBLIC_API, "feedback-rate-limit", "feedback-flood"),
        (se.WAF_ADMIN_API, "Default_Action", "admin-denied"),
        (se.COMMENT_SCREENING, "prompt_injection", "prompt-injection"),
        (se.COMMENT_SCREENING, "sql", "comment-attack"),
        (se.WAF_PUBLIC_API, "something-new", "other"),
    ],
)
def test_each_rule_falls_into_its_category(source, rule, category):
    assert se.classify_rule(source, rule) == category


def test_every_category_has_a_severity_a_threshold_and_next_steps():
    for severity, escalate_at, steps in se.PLAYBOOK.values():
        assert severity in (se.LOW, se.MEDIUM, se.HIGH) and escalate_at >= 1 and len(steps) > 40


def test_client_text_is_cleaned_and_truncated():
    assert se.untrusted_text("a\x00b\n\tc\x1b[31m") == "a b c [31m"
    assert len(se.untrusted_text("x" * 500)) == se.UNTRUSTED_MAX_CHARS


# --- recording --------------------------------------------------------------------------------------


def test_an_incident_holds_what_happened_but_never_the_ip(tables):
    _record()

    (row,) = _rows(tables)
    assert row["category"] == "xss" and row["severity"] == "low" and row["status"] == "open"
    assert row["source"] == "waf-public-api" and row["environment"] == "production"
    assert row["request_count"] == 1 and row["country"] == "AU" and row["method"] == "POST"
    assert row["untrusted"] == {"path": "/articles/a1/feedback", "matched": "<script>alert(1)</script>"}
    assert "never as instructions" in row["handling"]
    assert "Cross-site scripting" in row["suggested_next_steps"]
    assert "203.0.113.9" not in json.dumps(row, default=str)
    assert len(row["client_hash"]) == 16
    assert int(row["expires_at"]) == int((AT + timedelta(days=120)).timestamp())


def test_the_same_client_rule_and_window_add_up_to_one_incident(tables):
    _record()
    _record(at=AT + timedelta(minutes=5), count=3)
    _record(at=AT + timedelta(minutes=20))  # the next 15-minute window
    _record(client_ip="198.51.100.4")  # another client

    rows = _rows(tables)
    assert len(rows) == 3
    assert sorted(int(r["request_count"]) for r in rows) == [1, 1, 4]


def test_a_low_severity_incident_escalates_and_alerts_once_at_its_threshold(tables, capsys):
    _record(count=99)
    assert "SECURITY_ALERT" not in capsys.readouterr().out

    _record(count=1)
    _record(count=50)

    (row,) = _rows(tables)
    assert row["severity"] == "high" and "alerted_at" in row
    assert capsys.readouterr().out.count("SECURITY_ALERT") == 1


def test_a_high_severity_category_alerts_on_its_first_request_and_only_once(tables, capsys):
    _record(rule="aws-managed-common/GenericLFI_URIPATH", matched="../../etc/passwd")
    _record(rule="aws-managed-common/GenericLFI_URIPATH", at=AT + timedelta(minutes=1))

    out = capsys.readouterr().out
    assert out.count("SECURITY_ALERT") == 1
    assert "file-inclusion via waf-public-api" in out and "etc/passwd" not in out


def test_a_medium_incident_is_recorded_but_not_alerted(tables, capsys):
    _record(source=se.WAF_ADMIN_API, rule="Default_Action")

    assert _rows(tables)[0]["severity"] == "medium"
    assert "SECURITY_ALERT" not in capsys.readouterr().out


def test_a_status_someone_changed_is_never_reset_by_more_requests(tables):
    _record()
    event_id = _rows(tables)[0]["event_id"]
    tables.update_item(
        Key={"event_id": event_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "acknowledged"},
    )

    _record(at=AT + timedelta(minutes=2))

    assert _rows(tables)[0]["status"] == "acknowledged"


def test_open_incidents_are_listed_newest_first(tables):
    from common.dynamo import list_security_incidents

    _record()
    _record(at=AT + timedelta(hours=1))

    rows = list_security_incidents("open")
    assert [r["last_seen"] for r in rows] == sorted((r["last_seen"] for r in rows), reverse=True)


def test_recording_never_raises(monkeypatch):
    monkeypatch.delenv("SECURITY_EVENTS_TABLE", raising=False)

    assert _record() is None


# --- the WAF log subscription --------------------------------------------------------------------


def _waf_record(*, rule_group_rule="CrossSiteScripting_BODY", client="203.0.113.9", headers=(), at=AT):
    return json.dumps(
        {
            "timestamp": int(at.timestamp() * 1000),
            "action": "BLOCK",
            "terminatingRuleId": "aws-managed-common",
            "ruleGroupList": [{"terminatingRule": {"ruleId": rule_group_rule, "action": "BLOCK"}}],
            "terminatingRuleMatchDetails": [{"matchedData": ["<script>", "alert(1)"]}],
            "httpRequest": {
                "clientIp": client,
                "country": "AU",
                "uri": "/articles/a1/feedback",
                "httpMethod": "POST",
                "headers": [{"name": name, "value": value} for name, value in headers],
            },
        }
    )


def _batch(*messages, log_group="aws-waf-logs-bloggerbear-production-public-api", kind="DATA_MESSAGE"):
    payload = {
        "messageType": kind,
        "logGroup": log_group,
        "logEvents": [{"id": str(n), "message": m} for n, m in enumerate(messages)],
    }
    data = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
    return {"awslogs": {"data": data}}


def test_a_waf_record_names_the_managed_rule_that_blocked_it():
    parsed = security_events_handler.parse_waf_record(_waf_record())

    assert parsed["rule"] == "aws-managed-common/CrossSiteScripting_BODY"
    assert parsed["matched"] == "<script> alert(1)"
    assert parsed["client_ip"] == "203.0.113.9" and parsed["at"] == AT


def test_behind_the_cdn_the_visitor_address_is_used_only_with_the_origin_header():
    cdn_headers = [("x-origin-verify", "REDACTED"), ("x-viewer-ip", "1.2.3.4")]
    via_cdn = _waf_record(client="10.0.0.1", headers=cdn_headers)
    forged = _waf_record(client="5.6.7.8", headers=[("x-viewer-ip", "1.2.3.4")])

    assert security_events_handler.parse_waf_record(via_cdn)["client_ip"] == "1.2.3.4"
    assert security_events_handler.parse_waf_record(forged)["client_ip"] == "5.6.7.8"


def test_allowed_or_unreadable_records_are_skipped():
    assert security_events_handler.parse_waf_record(json.dumps({"action": "ALLOW"})) is None
    assert security_events_handler.parse_waf_record("not json") is None


def test_a_burst_from_one_client_is_one_incident(tables):
    records = [_waf_record(at=AT + timedelta(seconds=n)) for n in range(30)]

    result = security_events_handler.handler(_batch(*records), None)

    assert result == {"status": "recorded", "source": "waf-public-api", "incidents": 1, "groups": 1}
    (row,) = _rows(tables)
    assert row["request_count"] == 30
    assert row["first_seen"] == AT.isoformat()
    assert row["last_seen"] == (AT + timedelta(seconds=29)).isoformat()


def test_the_admin_log_group_is_the_admin_source(tables):
    record = {**json.loads(_waf_record()), "terminatingRuleId": "Default_Action", "ruleGroupList": []}
    batch = _batch(json.dumps(record), log_group="aws-waf-logs-bloggerbear-production-admin")

    security_events_handler.handler(batch, None)

    (row,) = _rows(tables)
    assert row["source"] == "waf-admin-api" and row["category"] == "admin-denied"


def test_control_messages_and_bad_batches_are_not_errors(tables):
    assert security_events_handler.handler(_batch(kind="CONTROL_MESSAGE"), None)["status"] == "skipped"
    assert security_events_handler.handler({"awslogs": {"data": "nope"}}, None)["status"] == "error"
