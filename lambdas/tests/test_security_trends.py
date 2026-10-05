"""Security incidents that are not one client's doing, and what a person can do with an incident.

* Trends (common/security_events.py's record_trend): dropped feedback comments, counted per day
  across every reader, and the admin API's 4xx answers, counted per hour. Each is a row that is
  not an incident until its first threshold, then rises through low, medium and high.
* By hand: `admin_cli security open`, and moving an incident along (acknowledge, resolve, reopen).
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from table_schemas import create_table

import admin_api_handler
import security_events_handler
from common import security_events as se
from common.dynamo import list_security_incidents
from ops_mcp import memory, suggestions

ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-southeast-2"
AT = datetime(2026, 10, 5, 3, 7, tzinfo=UTC)
ACCESS_LOG = "/aws/apigateway/bloggerbear-dev-admin-api-access"


@pytest.fixture
def table(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "SECURITY_EVENTS_TABLE": "SecurityEvents",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "ENVIRONMENT_NAME": "dev",
    }.items():
        monkeypatch.setenv(key, value)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        create_table(
            client,
            TableName="SecurityEvents",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "last_seen", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by_status_last_seen",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "last_seen", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        create_table(
            client,
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table("SecurityEvents")


def _rows(table):
    return table.scan()["Items"]


def _drop(times: int, at: datetime = AT) -> dict:
    row = None
    for n in range(times):
        row = se.record_trend("feedback-drops", at + timedelta(seconds=n))
    return row


# --- the ladder ----------------------------------------------------------------------------------


def test_the_thresholds_are_the_ones_asked_for():
    assert se.TRENDS["feedback-drops"].tiers == ((10, "low"), (50, "medium"), (100, "high"))
    assert se.TRENDS["feedback-drops"].period == "day"
    assert se.TRENDS["admin-api-errors"].tiers == ((20, "low"), (50, "medium"), (100, "high"))
    assert se.TRENDS["admin-api-errors"].period == "hour"
    for count, severity in ((0, None), (9, None), (10, "low"), (49, "low"), (50, "medium"), (99, "medium"),
                            (100, "high"), (5000, "high")):
        assert se.trend_severity("feedback-drops", count) == severity, count
    # Every trend has next steps to show, like any other category.
    assert set(se.TRENDS) <= set(se.PLAYBOOK)


def test_nine_drops_are_not_an_incident_and_the_tenth_opens_a_low_one(table, capsys):
    row = _drop(9)

    assert row["status"] == "counting" and int(row["request_count"]) == 9
    assert list_security_incidents("open") == []  # nothing for a person or the assistant to see

    row = _drop(1, AT + timedelta(minutes=5))

    assert row["status"] == "open" and row["severity"] == "low"
    (incident,) = list_security_incidents("open")
    assert incident["category"] == "feedback-drops" and incident["source"] == "comment-screening"
    assert int(incident["request_count"]) == 10
    assert incident["client_hash"] == "all"  # every reader together: no one address
    assert incident["window_start"] == "2026-10-05T00:00:00+00:00" and incident["period"] == "day"
    assert "feedback-config set --locked-down" in incident["suggested_next_steps"]
    assert se.ALERT_MARKER not in capsys.readouterr().out


def test_fifty_is_medium_and_a_hundred_is_high_and_alerts_once(table, capsys):
    assert _drop(49)["severity"] == "low"
    assert _drop(1, AT + timedelta(hours=1))["severity"] == "medium"
    assert _drop(49, AT + timedelta(hours=2))["severity"] == "medium"
    assert se.ALERT_MARKER not in capsys.readouterr().out

    assert _drop(1, AT + timedelta(hours=3))["severity"] == "high"
    _drop(25, AT + timedelta(hours=4))

    (incident,) = _rows(table)  # one row for the whole day
    assert incident["severity"] == "high" and int(incident["request_count"]) == 125
    out = capsys.readouterr().out
    assert out.count(se.ALERT_MARKER) == 1  # the line the alarm counts, and so one email
    assert "feedback-drops via comment-screening" in out


def test_each_day_starts_again(table):
    _drop(9, AT)
    _drop(9, AT + timedelta(days=1))

    assert sorted(int(row["request_count"]) for row in _rows(table)) == [9, 9]
    assert {row["status"] for row in _rows(table)} == {"counting"}


def test_an_acknowledged_trend_stays_acknowledged_but_still_rises_and_alerts(table, capsys):
    event_id = _drop(10)["event_id"]
    assert se.change_status(event_id, "acknowledged", AT)["status"] == "acknowledged"

    row = _drop(90, AT + timedelta(hours=2))

    assert row["status"] == "acknowledged" and row["severity"] == "high"
    assert list_security_incidents("open") == []
    assert capsys.readouterr().out.count(se.ALERT_MARKER) == 1


def test_a_fault_while_counting_never_reaches_the_caller(table, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("table unreachable")

    monkeypatch.setattr(se, "upsert_security_incident", broken)

    assert se.record_trend("feedback-drops", AT) is None
    assert "could not record a trend" in capsys.readouterr().out


def test_every_dropped_comment_counts_except_the_ones_that_are_not_the_comments_doing():
    code = (ROOT / "lambdas" / "public_api_handler.py").read_text(encoding="utf-8")

    assert "_NOT_THE_COMMENTS_DOING = frozenset({MODEL_BUDGET, MODEL_ERROR})" in code
    drop = code[code.index('if screened["dropped_because"]:') :]
    drop = drop[: drop.index("return _response(422")]
    assert 'if screened["dropped_because"] not in _NOT_THE_COMMENTS_DOING:' in drop
    assert 'security_events.record_trend("feedback-drops", datetime.now(UTC))' in drop
    # The attack incident, per client, is still recorded as before.
    assert "security_events.record_incident(" in drop


# --- the admin API's own errors -------------------------------------------------------------------


def _line(status: int, error_type: str = "-", route: str = "/topics") -> str:
    return json.dumps(
        {
            "requestId": "r",
            "httpMethod": "GET",
            "resourcePath": route,
            "status": status,
            "errorType": error_type,
        }
    )


def _batch(lines: list[str], at: datetime = AT, log_group: str = ACCESS_LOG) -> dict:
    payload = {
        "messageType": "DATA_MESSAGE",
        "logGroup": log_group,
        "logEvents": [
            {"id": str(n), "timestamp": int((at + timedelta(seconds=n)).timestamp() * 1000), "message": line}
            for n, line in enumerate(lines)
        ],
    }
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}}


def test_only_4xx_the_firewall_did_not_answer_counts():
    assert security_events_handler.counts_as_admin_error(_line(400))
    assert security_events_handler.counts_as_admin_error(_line(403, "MISSING_AUTHENTICATION_TOKEN"))
    assert security_events_handler.counts_as_admin_error(_line(404))
    assert security_events_handler.counts_as_admin_error(_line(429, "THROTTLED"))
    # Already an incident from the firewall's own log.
    assert not security_events_handler.counts_as_admin_error(_line(403, "WAF_FILTERED"))
    # A fault, not a security matter: the alarms cover it.
    assert not security_events_handler.counts_as_admin_error(_line(502))
    assert not security_events_handler.counts_as_admin_error(_line(200))
    assert not security_events_handler.counts_as_admin_error("not json")
    assert not security_events_handler.counts_as_admin_error('{"status": "-"}')

    assert security_events_handler.is_access_log(ACCESS_LOG)
    assert not security_events_handler.is_access_log("aws-waf-logs-bloggerbear-dev-admin")


def test_twenty_errors_in_an_hour_open_an_incident_and_a_hundred_alert(table, capsys):
    lines = [_line(403, "INVALID_SIGNATURE")] * 15 + [_line(200)] * 30 + [_line(403, "WAF_FILTERED")] * 30

    result = security_events_handler.handler(_batch(lines), None)

    assert result == {"status": "recorded", "source": "admin-api-access", "errors": 15, "hours": 1}
    assert list_security_incidents("open") == []  # fifteen: still counting

    security_events_handler.handler(_batch([_line(404)] * 5, AT + timedelta(minutes=10)), None)

    (incident,) = list_security_incidents("open")
    assert incident["category"] == "admin-api-errors" and incident["source"] == "admin-api-access"
    assert incident["severity"] == "low" and int(incident["request_count"]) == 20
    assert incident["window_start"] == "2026-10-05T03:00:00+00:00" and incident["period"] == "hour"

    security_events_handler.handler(_batch([_line(400)] * 80, AT + timedelta(minutes=20)), None)

    (incident,) = list_security_incidents("open")
    assert incident["severity"] == "high" and int(incident["request_count"]) == 100
    assert capsys.readouterr().out.count(se.ALERT_MARKER) == 1


def test_a_batch_across_two_hours_is_counted_into_each(table):
    early = [_line(400)] * 3
    just_before_four = datetime(2026, 10, 5, 3, 59, 59, tzinfo=UTC)
    result = security_events_handler.handler(_batch(early, just_before_four), None)

    assert result["errors"] == 3 and result["hours"] == 2
    assert sorted((row["window_start"], int(row["request_count"])) for row in _rows(table)) == [
        ("2026-10-05T03:00:00+00:00", 1),
        ("2026-10-05T04:00:00+00:00", 2),
    ]


def test_a_firewall_batch_is_still_grouped_per_client(table):
    block = json.dumps(
        {
            "timestamp": int(AT.timestamp() * 1000),
            "action": "BLOCK",
            "terminatingRuleId": "Default_Action",
            "httpRequest": {
                "clientIp": "203.0.113.9",
                "uri": "/topics",
                "httpMethod": "GET",
                "country": "ZZ",
            },
        }
    )

    waf_log = "aws-waf-logs-bloggerbear-dev-admin"
    result = security_events_handler.handler(_batch([block], log_group=waf_log), None)

    assert result["status"] == "recorded" and result["source"] == "waf-admin-api"
    (incident,) = _rows(table)
    assert incident["category"] == "admin-denied" and incident["status"] == "open"


# --- by hand: open, acknowledge, resolve ---------------------------------------------------------------


def test_an_incident_opened_by_hand_is_listed_and_a_high_one_alerts(table, capsys):
    incident = se.open_manual_incident("medium", "  odd sign-ins from\x00 a new place  ", AT)

    assert incident["status"] == "open" and incident["severity"] == "medium"
    assert incident["category"] == "manual-report" and incident["source"] == "manual"
    assert incident["summary"] == "odd sign-ins from a new place"  # cleaned, as stored text always is
    assert re.fullmatch(r"[0-9a-f]{32}", incident["event_id"])
    assert se.ALERT_MARKER not in capsys.readouterr().out

    high = se.open_manual_incident("high", "x" * 1000, AT)

    assert len(high["summary"]) == se.SUMMARY_MAX_CHARS
    out = capsys.readouterr().out
    assert out.count(se.ALERT_MARKER) == 1 and "manual-report via manual" in out
    assert "xxx" not in out  # the summary is in the table, not the log
    assert len(list_security_incidents("open")) == 2

    for severity, summary in (("urgent", "x"), ("low", "   "), ("low", "")):
        with pytest.raises(ValueError):
            se.open_manual_incident(severity, summary, AT)


def test_an_incident_is_moved_along_and_back(table):
    event_id = se.open_manual_incident("low", "a thing", AT)["event_id"]

    seen = se.change_status(event_id, "acknowledged", AT + timedelta(minutes=1))
    assert seen["status"] == "acknowledged" and seen["status_changed_by"] == "admin"
    assert seen["status_changed_at"] == "2026-10-05T03:08:00+00:00"
    assert list_security_incidents("open") == []
    assert [row["event_id"] for row in list_security_incidents("acknowledged")] == [event_id]

    assert se.change_status(event_id, "resolved", AT)["status"] == "resolved"
    assert se.change_status(event_id, "open", AT)["status"] == "open"

    assert se.change_status("no-such-incident", "resolved", AT) is None
    with pytest.raises(ValueError):
        se.change_status(event_id, "counting", AT)  # not a status a person may set
    # A trend below its first threshold is not an incident yet: nothing to move.
    counting = _drop(3)["event_id"]
    assert se.change_status(counting, "resolved", AT) is None


def _api(route: str, path: dict | None = None, query: dict | None = None, body: dict | None = None) -> dict:
    return admin_api_handler.handler(
        {
            "requestContext": {"routeKey": route},
            "routeKey": route,
            "httpMethod": route.split()[0],
            "resource": route.split()[1],
            "pathParameters": path,
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
        },
        None,
    )


def test_the_admin_api_lists_opens_and_moves_incidents(table):
    opened = _api("POST /security-incidents", body={"severity": "low", "summary": "a strange request"})
    assert opened["statusCode"] == 201
    event_id = json.loads(opened["body"])["event_id"]

    listed = json.loads(_api("GET /security-incidents")["body"])
    assert listed["status"] == "open" and listed["count"] == 1
    assert listed["items"][0]["summary"] == "a strange request"

    route = "PUT /security-incidents/{event_id}/status"
    moved = _api(route, path={"event_id": event_id}, body={"status": "resolved"})
    assert moved["statusCode"] == 200 and json.loads(moved["body"])["status"] == "resolved"
    assert json.loads(_api("GET /security-incidents")["body"])["count"] == 0
    assert json.loads(_api("GET /security-incidents", query={"status": "resolved"})["body"])["count"] == 1

    assert _api("POST /security-incidents", body={"severity": "dire", "summary": "x"})["statusCode"] == 400
    assert _api("POST /security-incidents", body={"severity": "low"})["statusCode"] == 400
    assert _api("GET /security-incidents", query={"status": "counting"})["statusCode"] == 400
    assert _api(route, path={"event_id": event_id}, body={"status": "counting"})["statusCode"] == 400
    assert _api(route, path={"event_id": "nope"}, body={"status": "resolved"})["statusCode"] == 404


def test_the_cli_has_the_security_commands(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import admin_cli

    sent = []
    monkeypatch.setattr(
        admin_cli, "_do_request", lambda args, method, path, body=None: sent.append((method, path, body))
    )
    parser = admin_cli.build_parser()

    for argv in (
        ["security", "list"],
        ["security", "list", "--status", "resolved"],
        ["security", "open", "--severity", "high", "--summary", "someone is in"],
        ["security", "acknowledge", "abc123"],
        ["security", "resolve", "abc123"],
        ["security", "reopen", "abc123"],
    ):
        args = parser.parse_args(argv)
        args.func(args)

    assert sent == [
        ("GET", "/security-incidents?status=open", None),
        ("GET", "/security-incidents?status=resolved", None),
        ("POST", "/security-incidents", {"severity": "high", "summary": "someone is in"}),
        ("PUT", "/security-incidents/abc123/status", {"status": "acknowledged"}),
        ("PUT", "/security-incidents/abc123/status", {"status": "resolved"}),
        ("PUT", "/security-incidents/abc123/status", {"status": "open"}),
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(["security", "open", "--severity", "dire", "--summary", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["security", "open", "--severity", "low"])  # no summary


# --- what the assistant suggests ------------------------------------------------------------------------


def test_the_assistant_suggests_the_commands_and_forgets_an_incident_once_it_is_seen():
    assert suggestions.suggest("security_incident", "abc123")["command"] == (
        "python scripts/admin_cli.py security acknowledge abc123"
    )
    spike = suggestions.suggest("firewall_spike", "aws-waf-logs-bloggerbear-shared")
    assert spike["command"].startswith(
        "python scripts/admin_cli.py security open --severity medium --summary "
    )
    assert "blocks nobody" in spike["what_it_does"]
    # Both are remembered, so each needs a way to be checked again.
    assert {"security_incident", "firewall_spike"} <= set(memory.CHECKERS)
    assert memory.CHECKERS["firewall_spike"]("any-log-group", None).state == memory.GONE


# --- Terraform --------------------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_each_environment_sends_the_admin_apis_4xx_to_the_security_function(env):
    text = (ROOT / "infra" / "environments" / env / "main.tf").read_text(encoding="utf-8")
    start = 'resource "aws_cloudwatch_log_subscription_filter" "security_events_admin_api_errors" {'
    block = text[text.index(start) :]
    block = block[: block.index("\n}\n")]

    assert "log_group_name  = module.admin_api.access_log_group_name" in block
    assert 'filter_pattern  = "{ $.status >= 400 && $.status < 500 }"' in block
    assert "destination_arn = aws_lambda_function.security_events.arn" in block
    assert f'"${{var.unique_name_prefix}}-{env}-security-events-admin-api-errors"' in block
    start = 'resource "aws_lambda_permission" "security_events_from_admin_access_log" {'
    permission = text[text.index(start) :]
    permission = permission[: permission.index("\n}\n")]
    assert 'principal     = "logs.amazonaws.com"' in permission
    assert 'source_arn    = "${local.admin_api_access_log_group_arn}:*"' in permission
    # The routes the CLI calls.
    for route in (
        "GET /security-incidents",
        "POST /security-incidents",
        "PUT /security-incidents/{event_id}/status",
    ):
        assert f'"{route}",' in text, route
    # An incident opened by hand at high severity logs the alert line from the Admin API's function.
    alert_groups = text[text.index("security_alert_log_groups = [") :]
    alert_groups = alert_groups[: alert_groups.index("\n  ]")]
    assert "aws_lambda_function.admin_api.function_name" in alert_groups
