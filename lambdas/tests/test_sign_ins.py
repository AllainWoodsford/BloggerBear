"""Sign-ins to the operator's assistant: the log, the lockout, the incident it raises, and how a
person and the assistant read it (common/sign_ins.py, sign_in_events_handler.py,
ops_mcp/sign_in_tool.py, the Admin API's /sign-ins routes and admin_cli's `sign-ins`).

The user pool's triggers are driven here as Cognito drives them: one event before the password is
checked, one more only if the user got in.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from table_schemas import create_table

import admin_api_handler
import ops_agent_handler
import sign_in_events_handler as handler_module
from common import security_events as se
from common import sign_ins
from ops_mcp import sign_in_tool, suggestions

ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-southeast-2"
NOW = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
PAGE_CLIENT = "pageclient123"


@pytest.fixture
def tables(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "SIGN_INS_TABLE": "SignIns",
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
            TableName="SignIns",
            KeySchema=[
                {"AttributeName": "username", "KeyType": "HASH"},
                {"AttributeName": "at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "username", "AttributeType": "S"},
                {"AttributeName": "at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for name, key in (("SecurityEvents", "event_id"), ("ModelConfig", "config_id")):
            create_table(
                client,
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {"sign_ins": resource.Table("SignIns"), "incidents": resource.Table("SecurityEvents")}


class Clock:
    """The handler's clock, moved by hand: each event a few seconds after the last."""

    def __init__(self, monkeypatch, start=NOW):
        self.now = start

        class _Datetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return self.now

        monkeypatch.setattr(handler_module, "datetime", _Datetime)

    def tick(self, **delta):
        self.now = self.now + timedelta(**(delta or {"seconds": 5}))


def _event(source: str, username: str = "operator") -> dict:
    return {
        "version": "1",
        "triggerSource": source,
        "userPoolId": "ap-southeast-2_test",
        "userName": username,
        "callerContext": {"clientId": PAGE_CLIENT, "awsSdkVersion": "test"},
        "request": {"userAttributes": {}},
        "response": {},
    }


def wrong_password(clock, username="operator"):
    """What a failed sign-in looks like from here: the first trigger, and no second."""
    result = handler_module.handler(_event("PreAuthentication_Authentication", username), None)
    clock.tick()
    return result


def right_password(clock, username="operator"):
    handler_module.handler(_event("PreAuthentication_Authentication", username), None)
    clock.tick(seconds=2)
    result = handler_module.handler(_event("PostAuthentication_Authentication", username), None)
    clock.tick()
    return result


def _events(tables, username="operator"):
    rows = tables["sign_ins"].scan()["Items"]
    return [row["event"] for row in sorted(rows, key=lambda row: row["at"]) if row["username"] == username]


def _incidents(tables):
    return sorted(tables["incidents"].scan()["Items"], key=lambda row: row["category"])


# --- the log ------------------------------------------------------------------------------------


def test_a_sign_in_is_two_rows_and_the_event_is_handed_back(tables, monkeypatch):
    clock = Clock(monkeypatch)
    event = _event("PreAuthentication_Authentication")

    assert handler_module.handler(event, None) is event  # handing it back lets the sign-in go on
    clock.tick()
    handler_module.handler(_event("PostAuthentication_Authentication"), None)

    rows = sorted(tables["sign_ins"].scan()["Items"], key=lambda row: row["at"])
    assert [row["event"] for row in rows] == ["attempt", "success"]
    assert {row["username"] for row in rows} == {"operator"}
    assert {row["client_id"] for row in rows} == {PAGE_CLIENT}
    # When, to the microsecond, and a suffix so two events at once are both kept.
    assert all(re.fullmatch(r"2026-10-05T02:00:\d\d(\.\d+)?\+00:00#[0-9a-f]{6}", row["at"]) for row in rows)
    # Kept 120 days, then gone.
    expires = {int(row["expires_at"]) for row in rows}
    assert min(expires) == int((NOW + timedelta(days=120)).timestamp())
    # Nothing else is stored: no address (the trigger is given none), no attributes.
    assert set(rows[0]) == {"username", "at", "event", "client_id", "expires_at"}
    assert _incidents(tables) == []


def test_a_failure_is_an_attempt_with_no_success_after_it():
    kinds = ("attempt", "success", "unlocked", "refused")
    attempt, success, unlocked, refused = ({"event": kind} for kind in kinds)

    assert sign_ins.outstanding_failures([]) == 0
    assert sign_ins.outstanding_failures([attempt, attempt, attempt]) == 3
    assert sign_ins.outstanding_failures([attempt, attempt, success]) == 0
    assert sign_ins.outstanding_failures([attempt, success, attempt, attempt]) == 2
    assert sign_ins.outstanding_failures([attempt, attempt, unlocked, attempt]) == 1
    # A refusal is not another failure: being turned away must not extend the lock.
    assert sign_ins.outstanding_failures([attempt, refused, refused]) == 1


# --- the lockout --------------------------------------------------------------------------------


def test_the_sixth_attempt_after_five_failures_is_refused_and_raises_one_high_incident(
    tables, monkeypatch, capsys
):
    clock = Clock(monkeypatch)
    for _ in range(sign_ins.LOCKOUT_FAILURES):
        wrong_password(clock, "mallory")

    with pytest.raises(handler_module.SignInRefused) as refused:
        wrong_password(clock, "mallory")
    assert "Too many failed sign-in attempts" in str(refused.value)
    assert "15 minutes" in str(refused.value)
    with pytest.raises(handler_module.SignInRefused):
        wrong_password(clock, "mallory")  # and again: still locked

    assert _events(tables, "mallory") == ["attempt"] * 5 + ["refused"] * 2

    lockout = next(row for row in _incidents(tables) if row["category"] == "sign-in-lockout")
    assert lockout["severity"] == "high" and lockout["status"] == "open"
    assert lockout["source"] == "sign-in" and lockout["rule"] == "lockout"
    assert int(lockout["request_count"]) == 2  # both refusals, one incident
    # The user's name is not in the incident: a keyed hash of it, as for a client address.
    assert "mallory" not in json.dumps(lockout, default=str)
    assert re.fullmatch(r"[0-9a-f]{16}", lockout["client_hash"])
    # One alert line for the incident, however many times the user is refused: this line is what
    # the high-severity alarm counts, and the alarm is what emails the operator.
    out = capsys.readouterr().out
    assert out.count(se.ALERT_MARKER) == 1
    assert "sign-in-lockout via sign-in" in out
    assert "mallory" not in out


def test_repeated_failures_are_a_low_incident_before_the_lockout(tables, monkeypatch, capsys):
    clock = Clock(monkeypatch)
    wrong_password(clock)
    wrong_password(clock)
    assert _incidents(tables) == []  # one slip, then another: nothing yet

    wrong_password(clock)  # the third attempt shows the first two failed
    wrong_password(clock)

    (incident,) = _incidents(tables)
    assert incident["category"] == "sign-in-failures" and incident["severity"] == "low"
    assert int(incident["request_count"]) == 2
    assert se.ALERT_MARKER not in capsys.readouterr().out


def test_getting_in_clears_the_count(tables, monkeypatch):
    clock = Clock(monkeypatch)
    for _ in range(4):
        wrong_password(clock)
    right_password(clock)
    for _ in range(4):
        wrong_password(clock)

    assert sign_ins.recent_failures("operator", clock.now) == 4  # not 8: never locked
    right_password(clock)
    assert "refused" not in _events(tables)


def test_the_lock_lifts_by_itself_as_the_attempts_age_out(tables, monkeypatch):
    clock = Clock(monkeypatch)
    for _ in range(sign_ins.LOCKOUT_FAILURES):
        wrong_password(clock)
    with pytest.raises(handler_module.SignInRefused):
        wrong_password(clock)

    clock.tick(minutes=sign_ins.WINDOW_MINUTES + 1)

    right_password(clock)
    assert _events(tables)[-2:] == ["attempt", "success"]


def test_one_users_failures_do_not_lock_another(tables, monkeypatch):
    clock = Clock(monkeypatch)
    for _ in range(sign_ins.LOCKOUT_FAILURES):
        wrong_password(clock, "judge")

    right_password(clock, "operator")
    with pytest.raises(handler_module.SignInRefused):
        wrong_password(clock, "judge")


def test_an_unlock_lets_the_user_in_at_once(tables, monkeypatch):
    clock = Clock(monkeypatch)
    for _ in range(sign_ins.LOCKOUT_FAILURES):
        wrong_password(clock)

    result = sign_ins.unlock("operator", clock.now)
    clock.tick()

    assert result == {"username": "operator", "was_locked": True, "failures_cleared": 5}
    right_password(clock)
    assert _events(tables)[-3:] == ["unlocked", "attempt", "success"]
    # Unlocking someone who is not locked is harmless.
    assert sign_ins.unlock("nobody", clock.now)["was_locked"] is False


def test_a_fault_never_stops_a_sign_in(tables, monkeypatch, capsys):
    """The table being unreachable must not lock the operator out of their own assistant."""
    Clock(monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("table unreachable")

    monkeypatch.setattr(sign_ins, "recent_failures", broken)
    monkeypatch.setattr(sign_ins, "record", broken)

    for source in ("PreAuthentication_Authentication", "PostAuthentication_Authentication"):
        event = _event(source)
        assert handler_module.handler(event, None) is event
    assert capsys.readouterr().out.count("could not record") == 2


def test_an_event_with_no_user_or_an_unknown_trigger_is_let_through(tables, monkeypatch):
    Clock(monkeypatch)

    nameless = _event("PreAuthentication_Authentication", "")
    assert handler_module.handler(nameless, None) is nameless
    other = _event("PreTokenGeneration_Authentication")
    assert handler_module.handler(other, None) is other
    assert tables["sign_ins"].scan()["Items"] == []


# --- reading it: the report, the assistant's tool ---------------------------------------------------


def _history(tables, monkeypatch):
    """operator: in twice, one slip. judge: locked right now. guest: three failures two days ago."""
    clock = Clock(monkeypatch, NOW - timedelta(days=2))
    for _ in range(3):
        wrong_password(clock, "guest")
    clock.now = NOW - timedelta(hours=3)
    right_password(clock, "operator")
    wrong_password(clock, "operator")
    right_password(clock, "operator")
    clock.now = NOW - timedelta(minutes=5)
    for _ in range(sign_ins.LOCKOUT_FAILURES):
        wrong_password(clock, "judge")
    with pytest.raises(handler_module.SignInRefused):
        wrong_password(clock, "judge")
    return NOW


def test_the_report_counts_each_user_and_says_who_is_locked(tables, monkeypatch):
    now = _history(tables, monkeypatch)

    report = sign_ins.report(7, now)

    users = {user["username"]: user for user in report["users"]}
    assert [user["username"] for user in report["users"]] == ["judge", "guest", "operator"]  # worst first
    assert users["operator"] | {"last_success": None, "last_event": None} == {
        "username": "operator", "attempts": 3, "successes": 2, "failed": 1, "refused": 0,
        "unlocks": 0, "locked": False, "last_success": None, "last_event": None,
    }
    assert users["operator"]["last_success"].startswith("2026-10-04T23:00")
    assert users["judge"]["locked"] is True and users["judge"]["refused"] == 1
    assert users["judge"]["failed"] == 5 and users["judge"]["last_success"] is None
    assert users["guest"]["failed"] == 3 and users["guest"]["locked"] is False  # two days old
    assert report["totals"] == {"attempts": 11, "successes": 2, "failed": 9, "refused": 1}
    assert report["lockout"] == {"failures": 5, "window_minutes": 15}
    # The period is held to 1..30 days, whatever is asked.
    assert sign_ins.report(999, now)["days"] == 30 and sign_ins.report(0, now)["days"] == 1
    assert sign_ins.clamp_days("nonsense") == 7
    # One day back does not reach the guest's failures.
    assert {user["username"] for user in sign_ins.report(1, now)["users"]} == {"judge", "operator"}


def test_the_tool_reports_a_lock_with_its_unlock_command_and_failures_with_the_list(tables, monkeypatch):
    now = _history(tables, monkeypatch)

    result = sign_in_tool.sign_ins(7, now=now)

    kinds = {finding["kind"]: finding for finding in result["findings"]}
    assert set(kinds) == {"sign_in_locked", "sign_in_failures"}  # the operator's one slip is not a finding
    locked = kinds["sign_in_locked"]
    assert locked["id"] == "judge"
    assert locked["noticed"] == (
        "User judge is locked out of the assistant after 5 failed sign-ins in 15 minutes"
    )
    assert locked["suggestion"]["command"] == "python scripts/admin_cli.py sign-ins unlock judge"
    failed = kinds["sign_in_failures"]
    assert failed["noticed"] == "User guest failed to sign in 3 times in the last 7 days"
    assert failed["suggestion"]["command"] == "python scripts/admin_cli.py sign-ins list"
    assert result["spoken"] == (
        "In the last 7 days: 2 sign-ins by 3 users, 9 failed attempts, 1 refusal. "
        "User judge is locked out right now."
    )
    assert result["totals"]["successes"] == 2 and len(result["users"]) == 3


def test_the_tool_says_so_when_nothing_is_unusual_or_nobody_signed_in(tables, monkeypatch):
    assert sign_in_tool.sign_ins(7, now=NOW) == {
        "spoken": "Nobody has tried to sign in to the assistant in the last 7 days.",
        "findings": [],
        "days": 7,
        "lockout": {"failures": 5, "window_minutes": 15},
        "totals": {"attempts": 0, "successes": 0, "failed": 0, "refused": 0},
        "users": [],
        "as_of": NOW.isoformat(),
    }

    clock = Clock(monkeypatch, NOW - timedelta(hours=1))
    right_password(clock)
    quiet = sign_in_tool.sign_ins(1, now=NOW)
    assert quiet["findings"] == []
    assert quiet["spoken"] == "In the last day: 1 sign-in by 1 user, 0 failed attempts. Nothing unusual."


def test_a_name_that_is_not_a_plain_id_is_never_said_or_put_in_a_command(tables, monkeypatch):
    clock = Clock(monkeypatch, NOW - timedelta(minutes=5))
    odd = "someone; rm -rf /"
    for _ in range(sign_ins.LOCKOUT_FAILURES):
        wrong_password(clock, odd)

    result = sign_in_tool.sign_ins(1, now=NOW)

    (finding,) = result["findings"]
    assert finding["kind"] == "sign_in_locked" and finding["id"] is None
    assert finding["noticed"].startswith("A user is locked out")
    assert finding["suggestion"] is None  # no command built from a name we do not trust
    assert odd not in json.dumps(result)


def test_the_assistant_does_not_keep_these_findings_and_a_briefing_may_look():
    """A lock lifts by itself in minutes, so a remembered "unlock" would be noise: the server
    hands this tool's result back as it is, where the others go through its memory."""
    server = (ROOT / "lambdas" / "ops_mcp" / "server.py").read_text(encoding="utf-8")
    tool = server[server.index("def sign_ins(") :]
    tool = tool[: tool.index("@server.tool")]

    assert "return sign_in_tool.sign_ins(days)" in tool and "remembered(" not in tool
    assert "sign_ins" in ops_agent_handler._BRIEFING_TOOLS
    for kind in ("sign_in_locked", "sign_in_failures"):
        assert suggestions.CATALOGUE[kind].arguments.startswith("sign-ins ")


# --- the Admin API and the CLI ------------------------------------------------------------------------


def _api(route: str, path: dict | None = None, query: dict | None = None) -> dict:
    return admin_api_handler.handler(
        {
            "requestContext": {"routeKey": route},
            "routeKey": route,
            "httpMethod": route.split()[0],
            "resource": route.split()[1],
            "pathParameters": path,
            "queryStringParameters": query,
        },
        None,
    )


def test_the_admin_api_lists_sign_ins_and_unlocks_a_user(tables, monkeypatch):
    clock = Clock(monkeypatch, datetime.now(UTC) - timedelta(minutes=2))
    for _ in range(sign_ins.LOCKOUT_FAILURES):
        wrong_password(clock, "judge")

    listed = _api("GET /sign-ins", query={"days": "1"})
    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert body["days"] == 1 and body["users"][0]["username"] == "judge"
    assert body["users"][0]["locked"] is True

    unlocked = _api("POST /sign-ins/{username}/unlock", path={"username": "judge"})
    assert unlocked["statusCode"] == 200
    assert json.loads(unlocked["body"]) == {"username": "judge", "was_locked": True, "failures_cleared": 5}
    assert json.loads(_api("GET /sign-ins")["body"])["users"][0]["locked"] is False

    assert _api("POST /sign-ins/{username}/unlock", path={"username": " "})["statusCode"] == 400
    assert _api("POST /sign-ins/{username}/unlock", path={"username": "x" * 129})["statusCode"] == 400


def test_the_cli_has_both_commands_and_encodes_the_name(monkeypatch):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import admin_cli

    sent = []
    monkeypatch.setattr(
        admin_cli, "_do_request", lambda args, method, path, body=None: sent.append((method, path))
    )
    parser = admin_cli.build_parser()

    for argv in (["sign-ins", "list"], ["sign-ins", "list", "--days", "30"], ["sign-ins", "unlock", "a b/c"]):
        args = parser.parse_args(argv)
        args.func(args)

    assert sent == [
        ("GET", "/sign-ins?days=7"),
        ("GET", "/sign-ins?days=30"),
        ("POST", "/sign-ins/a%20b%2Fc/unlock"),
    ]


# --- Terraform ------------------------------------------------------------------------------------


def _tf(*parts: str) -> str:
    return (ROOT / "infra").joinpath(*parts).read_text(encoding="utf-8")


def test_the_pool_calls_the_function_before_and_after_every_sign_in_and_only_this_pool_may():
    module = _tf("modules", "ops-assistant", "main.tf")
    pool = module[module.index('resource "aws_cognito_user_pool" "this" {') :]
    pool = pool[: pool.index("\n}\n")]

    assert re.search(r"lambda_config \{\s*pre_authentication\s*=\s*var\.sign_in_trigger_function_arn", pool)
    assert re.search(r"post_authentication\s*=\s*var\.sign_in_trigger_function_arn", pool)
    permission = module[module.index('resource "aws_lambda_permission" "sign_in_trigger" {') :]
    permission = permission[: permission.index("\n}\n")]
    assert 'principal     = "cognito-idp.amazonaws.com"' in permission
    assert "source_arn    = aws_cognito_user_pool.this.arn" in permission


@pytest.mark.parametrize("env", ["dev", "production"])
def test_each_environment_has_the_function_its_table_and_its_routes(env):
    text = _tf("environments", env, "main.tf")
    function = text[text.index('resource "aws_lambda_function" "sign_in_events" {') :]
    function = function[: function.index("\n}\n")]

    assert f'function_name = "${{var.unique_name_prefix}}-{env}-sign-in-events"' in function
    assert 'handler       = "sign_in_events_handler.handler"' in function
    assert re.search(r"timeout\s+= 5\b", function)  # all Cognito gives a trigger
    # Its own settings: the shared map names the site, the site names the pool, the pool names
    # this function, so using the map here would be a cycle Terraform refuses.
    assert "variables = local.lambda_env_variables" not in function
    for name in ("SIGN_INS_TABLE", "SECURITY_EVENTS_TABLE", "MODEL_CONFIG_TABLE", "NAME_PREFIX"):
        assert re.search(rf"^\s+{name}\s+=", function, re.M), name
    assert re.search(rf'ENVIRONMENT_NAME\s+= "{env}"', function)
    # Handed to the assistant's module, which sets it on the pool.
    assert "sign_in_trigger_function_arn  = aws_lambda_function.sign_in_events.arn" in text
    # A lockout is high severity: its alert line must be in a log group the alarm's filter reads.
    alert_groups = text[text.index("security_alert_log_groups = [") :]
    alert_groups = alert_groups[: alert_groups.index("\n  ]")]
    assert "aws_lambda_function.sign_in_events.function_name" in alert_groups
    # The Admin API routes the CLI calls, and the table's name for the Admin API's function.
    assert '"GET /sign-ins",' in text and '"POST /sign-ins/{username}/unlock",' in text
    assert re.search(r"^\s+SIGN_INS_TABLE\s+= module\.app_data\.sign_ins_table_name$", text, re.M)


def test_the_table_is_keyed_by_user_then_time_expires_and_is_one_the_shared_role_reaches():
    tables = _tf("modules", "app-data", "main.tf")
    table = tables[tables.index('resource "aws_dynamodb_table" "sign_ins" {') :]

    assert '= "${var.unique_name_prefix}-${var.environment_name}-sign-ins"' in table
    assert re.search(r'hash_key\s+= "username"', table) and re.search(r'range_key\s+= "at"', table)
    assert 'attribute_name = "expires_at"' in table
    assert "aws_dynamodb_table.sign_ins.arn," in _tf("modules", "app-data", "outputs.tf")
