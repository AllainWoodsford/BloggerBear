"""The admin API's evals routes (admin_api_handler.py; docs/enhancements/model-ranch-prompt-assets-evals.md,
PR13): GET/PUT /evals-config, POST /evals/rollup, GET /evals/status, GET /evals/digest, the perf
fields on GET /models, and the routes' place in both environments' Terraform.

What is held: the settings route sets and clears what the CLI may, `timezone` (a DynamoDB reserved
word) included, and refuses the DynamoDB-only settings; a rollup starts in eval-jobs, or is refused
while evals are off; status answers every part, each queue on its own, and a part that cannot be
read says "could not check" without hiding the others; the digest adds the day rows up from a day,
in the settings' time zone; GET /models shows what the rollup put on a row.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws
from table_schemas import KEYS
from test_evals_rollup import FakeBedrock, FakeLogs, completion, sydney

import admin_api_handler
from common import dynamo, evals_rollup, evals_settings, evals_status
from common.naming import environment_prefix

REGION = "ap-southeast-2"
ALPHA = "maker.alpha-v1:0"
QUEUE_URL = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/acme-dev-eval-work"
WORK_QUEUE_URL = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/acme-dev-work.fifo"
ROUTES = {
    "GET /evals-config", "PUT /evals-config", "POST /evals/rollup", "GET /evals/status", "GET /evals/digest",
}  # fmt: skip


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MODELS_TABLE": "Models",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "TOPICS_TABLE": "Topics",
        "EVALS_TABLE": "Evals",
        "EVALS_LOG_GROUP": "acme-dev-evals",
        "EVALS_ENABLED": "true",
        "EVAL_WORK_QUEUE_URL": QUEUE_URL,
        # The queue platform is not deployed with evals alone (Q0 sets the key empty in both roots).
        "WORK_QUEUE_URL": "",
        "ENVIRONMENT_NAME": "dev",
        "BEDROCK_MODEL_ID": ALPHA,
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    admin_api_handler._lambda_client = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        hash_key, range_key = KEYS["Evals"]
        client.create_table(
            TableName="Evals",
            KeySchema=[
                {"AttributeName": hash_key, "KeyType": "HASH"},
                {"AttributeName": range_key, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": hash_key, "AttributeType": "S"},
                {"AttributeName": range_key, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for name, key in {"Models": "model_id", "ModelConfig": "config_id", "Topics": "topic_id"}.items():
            client.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        resource = boto3.resource("dynamodb", region_name=REGION)
        resource.Table("Models").put_item(
            Item={"model_id": ALPHA, "display_name": "Alpha", "provider": "maker", "model_purpose": "text"}
        )
        yield resource
    dynamo._dynamodb_resource = None


@pytest.fixture
def queue():
    """A queue as SQS and CloudWatch answer for it: eval-work, and work.fifo once it is there."""
    sqs, cloudwatch = MagicMock(), MagicMock()
    sqs.get_queue_attributes.return_value = {
        "Attributes": {
            "ApproximateNumberOfMessages": "3",
            "ApproximateNumberOfMessagesNotVisible": "1",
            "ApproximateNumberOfMessagesDelayed": "2",
        }
    }
    cloudwatch.get_metric_statistics.return_value = {
        "Datapoints": [
            {"Timestamp": datetime(2026, 10, 27, 2, 50, tzinfo=UTC), "Maximum": 600.0},
            {"Timestamp": datetime(2026, 10, 27, 2, 55, tzinfo=UTC), "Maximum": 900.0},
        ]
    }
    with patch.object(evals_status, "_sqs_client", return_value=sqs), patch.object(
        evals_status, "_cloudwatch_client", return_value=cloudwatch
    ):
        yield sqs, cloudwatch


def call(route, body=None, query=None):
    event = {"routeKey": route}
    if body is not None:
        event["body"] = json.dumps(body)
    if query is not None:
        event["queryStringParameters"] = query
    result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


# --- settings -------------------------------------------------------------------------------------


def test_get_shows_the_defaults_and_the_judges_model_when_nothing_is_set(aws):
    status, body = call("GET /evals-config")

    assert status == 200
    assert body["effective"] == evals_settings.SETTINGS
    assert body["judge_model"] == ALPHA
    assert body["dynamodb_only"] == sorted(evals_settings.DYNAMODB_ONLY)


def test_put_sets_and_clears_settings_and_leaves_the_rest(aws):
    settings = {"judge_enabled": False, "excluded_sources": ["judge"], "score_max_cost_aud": 2.5}
    status, body = call("PUT /evals-config", settings)
    assert status == 200
    assert body["effective"]["judge_enabled"] is False and body["effective"]["excluded_sources"] == ["judge"]
    assert body["effective"]["score_max_cost_aud"] == 2.5

    status, body = call("PUT /evals-config", {"judge_enabled": None})
    assert body["effective"]["judge_enabled"] is True
    assert body["settings"]["excluded_sources"] == ["judge"]


def test_put_sets_and_clears_the_timezone_though_it_is_a_dynamodb_reserved_word(aws):
    status, body = call("PUT /evals-config", {"timezone": "UTC"})
    assert status == 200
    assert body["settings"]["timezone"] == body["effective"]["timezone"] == "UTC"

    # Cleared alongside another setting: neither is lost to a refused update.
    status, body = call("PUT /evals-config", {"timezone": None, "judge_enabled": False})
    assert status == 200
    assert body["settings"]["timezone"] is None
    assert body["effective"]["timezone"] == "Australia/Sydney"
    assert body["effective"]["judge_enabled"] is False


def test_a_config_row_and_a_models_row_take_attributes_named_by_reserved_words(aws):
    assert dynamo.update_config_row("evals", {"timezone": "UTC"})["timezone"] == "UTC"
    assert "timezone" not in dynamo.update_config_row("evals", {"timezone": None})
    # A field set, and a field the write is conditional on, may be the same reserved word.
    assert dynamo.set_model_fields(ALPHA, {"timezone": "UTC"}, expected={"timezone": None})
    assert not dynamo.set_model_fields(ALPHA, {"timezone": "GMT"}, expected={"timezone": None})
    assert dynamo.set_model_fields(ALPHA, {"timezone": None}, expected={"timezone": "UTC"})
    assert "timezone" not in aws.Table("Models").get_item(Key={"model_id": ALPHA})["Item"]


@pytest.mark.parametrize(
    "body",
    [{}, ["not", "a", "dict"], {"nonsense": 1}, {"judge_interval_hours": 25}, {"timezone": "Mars/Olympus"}],
)
def test_put_refuses_what_is_not_a_valid_setting(aws, body):
    assert call("PUT /evals-config", body)[0] == 400


@pytest.mark.parametrize("name", sorted(evals_settings.DYNAMODB_ONLY))
def test_put_refuses_the_settings_kept_to_dynamodb(aws, name):
    status, body = call("PUT /evals-config", {name: evals_settings.SETTINGS[name]})

    assert status == 400 and "DynamoDB only" in body["error"]
    assert dynamo.get_config_row("evals") is None


def test_put_refuses_a_judge_that_does_not_write_text(aws):
    aws.Table("Models").put_item(Item={"model_id": "maker.embed-v1:0", "model_purpose": "embedding"})

    status, body = call("PUT /evals-config", {"judge_model_id": "maker.embed-v1:0"})

    assert status == 400 and "maker.embed-v1:0" in body["error"]
    assert call("PUT /evals-config", {"judge_model_id": ALPHA})[0] == 200


# --- the rollup -----------------------------------------------------------------------------------


def test_a_rollup_starts_in_eval_jobs_in_the_background(aws):
    client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=client):
        assert call("POST /evals/rollup", {}) == (202, {"triggered": "rollup"})
        assert call("POST /evals/rollup") == (202, {"triggered": "rollup"})

    kwargs = client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == f"{environment_prefix('dev')}eval-jobs"
    assert kwargs["InvocationType"] == "Event"
    assert json.loads(kwargs["Payload"]) == {"job": "rollup", "trigger": "admin"}


def test_a_rollup_for_days_sends_them_checked(aws):
    client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=client):
        status, body = call("POST /evals/rollup", {"from": "2026-10-01", "to": "2026-10-03"})

    assert (status, body) == (202, {"triggered": "rollup", "from": "2026-10-01", "to": "2026-10-03"})
    assert json.loads(client.invoke.call_args.kwargs["Payload"]) == {
        "job": "rollup", "trigger": "admin", "from": "2026-10-01", "to": "2026-10-03",
    }  # fmt: skip


@pytest.mark.parametrize("body", [{"from": "October"}, {"to": "2026-10-03"}, {"from": "2020-01-01"}, ["x"]])
def test_a_rollup_for_days_that_are_not_a_range_is_refused(aws, body):
    client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=client):
        assert call("POST /evals/rollup", body)[0] == 400
    client.invoke.assert_not_called()


def test_a_rollup_is_refused_while_evals_are_off(aws, monkeypatch):
    monkeypatch.setenv("EVALS_ENABLED", "false")
    client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=client):
        status, body = call("POST /evals/rollup", {})

    assert status == 409 and "evals are off" in body["error"]
    client.invoke.assert_not_called()


# --- status ---------------------------------------------------------------------------------------


def test_status_answers_every_part(aws, queue):
    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "judge_min_pairs": 2})
    aws.Table("Evals").put_item(
        Item={
            **evals_rollup.INDEX_KEY, "last_rolled_day": "2026-10-26", "judge_agreement": Decimal("0.75"),
            "judge_pairs": 4, "late_dropped": 3, "rolled_up_at": "2026-10-27T02:05:00+00:00",
            "last_run": {"trigger": "schedule", "events": 12},
        }
    )  # fmt: skip
    aws.Table("Models").put_item(
        Item={
            "model_id": "maker.poor-v1:0", "daily_perf": -60, "weekly_perf": -60, "total_perf": -60,
            "perf_events": {"daily": 12, "weekly": 12, "monthly": 12, "total": 12},
        }
    )  # fmt: skip
    aws.Table("Models").put_item(
        Item={
            "model_id": "maker.forgiven-v1:0", "daily_perf": -60, "weekly_perf": 0, "total_perf": 0,
            "perf_events": {"daily": 12, "weekly": 12, "monthly": 12, "total": 12},
            "ungated_until": "2099-01-01T00:00:00Z",
        }
    )  # fmt: skip
    for topic_id, mode in (("t1", None), ("t2", "queue"), ("t3", "schedule"), ("t4", "Some Thing")):
        aws.Table("Topics").put_item(Item={"topic_id": topic_id, **({"dispatch_mode": mode} if mode else {})})

    status, body = call("GET /evals/status")

    assert status == 200
    assert body["switches"] == {
        "evals_enabled": True, "judge_enabled": True, "cull_enabled": False, "trace_calls": False,
    }  # fmt: skip
    assert body["rollup"] == {
        "last_rolled_day": "2026-10-26", "rolled_up_at": "2026-10-27T02:05:00+00:00",
        "last_run": {"trigger": "schedule", "events": 12},
    }  # fmt: skip
    assert body["late_dropped"] == {"late_dropped": 3}
    assert body["judge"] == {
        "judge_enabled": True, "judge_model": ALPHA, "agreement": 0.75, "pairs": 4, "calibrated": True,
        "min_agreement": 0.7, "min_pairs": 2,
    }  # fmt: skip
    gated = body["gated_models"]
    assert [model["model_id"] for model in gated["gated"]] == ["maker.poor-v1:0"]
    # Under the daily gate with 12 events; at -60 the week and the total are above theirs.
    assert gated["gated"][0]["windows"] == ["daily"]
    assert [model["windows"] for model in gated["held_open"]] == [["daily"]]
    assert gated["gates"] == {"daily_gate": -50, "weekly_gate": -100, "total_gate": -1000, "min_events": 10}
    assert body["cull"] == {"proposal": None, "detail": "no cull proposal yet"}
    assert body["queue_depth"]["eval-work"] == {
        "queue": "acme-dev-eval-work", "waiting": 3, "in_flight": 1, "delayed": 2,
    }  # fmt: skip
    assert body["queue_age"]["eval-work"] == {
        "queue": "acme-dev-eval-work", "oldest_seconds": 900, "at": "2026-10-27T02:55:00+00:00",
    }  # fmt: skip
    # The queue platform is not deployed here: WORK_QUEUE_URL is empty.
    assert body["queue_depth"]["work.fifo"]["status"] == body["queue_age"]["work.fifo"]["status"] == "off"
    assert body["topics_by_dispatch_mode"] == {"schedule": 2, "queue": 1, "other": 1}


def test_status_says_the_queue_is_off_while_evals_are_and_asks_sqs_nothing(aws, queue, monkeypatch):
    monkeypatch.setenv("EVAL_WORK_QUEUE_URL", "")
    monkeypatch.setenv("EVALS_ENABLED", "false")

    _, body = call("GET /evals/status")

    assert body["queue_depth"]["eval-work"]["status"] == body["queue_age"]["eval-work"]["status"] == "off"
    assert "evals are off" in body["queue_depth"]["eval-work"]["detail"]
    assert body["switches"]["evals_enabled"] is False
    queue[0].get_queue_attributes.assert_not_called()
    queue[1].get_metric_statistics.assert_not_called()


def test_work_fifo_is_off_while_its_url_is_empty_and_answers_once_it_is_set(aws, queue, monkeypatch):
    _, body = call("GET /evals/status")
    for part in ("queue_depth", "queue_age"):
        assert body[part]["work.fifo"] == {
            "status": "off",
            "detail": "the queue platform is not deployed: there is no work queue (WORK_QUEUE_URL is empty)",
        }, part
    assert [kw.kwargs["QueueUrl"] for kw in queue[0].get_queue_attributes.call_args_list] == [QUEUE_URL]

    monkeypatch.setenv("WORK_QUEUE_URL", WORK_QUEUE_URL)
    _, body = call("GET /evals/status")

    assert body["queue_depth"]["work.fifo"] == {
        "queue": "acme-dev-work.fifo", "waiting": 3, "in_flight": 1, "delayed": 2,
    }  # fmt: skip
    assert body["queue_age"]["work.fifo"]["queue"] == "acme-dev-work.fifo"
    named = [kw.kwargs["Dimensions"] for kw in queue[1].get_metric_statistics.call_args_list[-2:]]
    names = ("acme-dev-eval-work", "acme-dev-work.fifo")
    assert named == [[{"Name": "QueueName", "Value": name}] for name in names]


def test_each_queue_answers_on_its_own(aws, queue, monkeypatch):
    monkeypatch.setenv("WORK_QUEUE_URL", WORK_QUEUE_URL)
    answer = queue[0].get_queue_attributes.return_value

    def attributes(QueueUrl, AttributeNames):  # noqa: N803 - boto3's own argument names
        if QueueUrl == WORK_QUEUE_URL:
            raise RuntimeError("access denied")
        return answer

    queue[0].get_queue_attributes.side_effect = attributes

    _, body = call("GET /evals/status")

    assert body["queue_depth"]["work.fifo"] == {"status": "could not check", "error": "RuntimeError"}
    assert body["queue_depth"]["eval-work"]["waiting"] == 3
    assert body["queue_age"]["work.fifo"]["oldest_seconds"] == 900


def test_an_idle_queue_with_no_datapoint_is_said_to_be_one(aws, queue):
    queue[1].get_metric_statistics.return_value = {"Datapoints": []}

    _, body = call("GET /evals/status")

    age = body["queue_age"]["eval-work"]
    assert age["oldest_seconds"] is None and "idle" in age["detail"]


def test_status_shows_the_cull_proposal_in_place_and_could_not_check_when_it_cannot_be_read(aws, queue):
    proposal = {"ids": ["maker.poor-v1:0"], "scores": [-1200], "expires_at": "2026-11-02T00:00:00Z"}
    aws.Table("ModelConfig").put_item(Item={"config_id": "model-catalog-state", "cull_proposal": proposal})

    _, body = call("GET /evals/status")
    assert body["cull"] == {"proposal": proposal}

    with patch.object(evals_status.model_catalog, "get_state", side_effect=RuntimeError("no row")):
        _, body = call("GET /evals/status")
    assert body["cull"] == {"status": "could not check", "error": "RuntimeError"}
    assert body["judge"]["judge_model"] == ALPHA


def test_a_part_that_cannot_be_read_says_could_not_check_and_the_others_still_answer(aws, queue, capsys):
    queue[0].get_queue_attributes.side_effect = RuntimeError("sqs\ndown")
    with patch.object(evals_status.dynamo, "list_topics", side_effect=RuntimeError("scan refused")):
        _, body = call("GET /evals/status")

    assert body["queue_depth"]["eval-work"] == {"status": "could not check", "error": "RuntimeError"}
    assert body["topics_by_dispatch_mode"] == {"status": "could not check", "error": "RuntimeError"}
    assert body["queue_age"]["eval-work"]["oldest_seconds"] == 900
    assert body["switches"]["evals_enabled"] is True
    assert "gated" in body["gated_models"] and "agreement" in body["judge"]
    out = capsys.readouterr().out
    assert "evals_status: could not check queue_depth/eval-work: RuntimeError('sqs\\ndown')" in out


def test_without_its_settings_only_the_parts_that_need_them_cannot_be_checked(aws, queue):
    with patch.object(evals_status.evals_settings, "get_settings", side_effect=RuntimeError("no row")):
        _, body = call("GET /evals/status")

    for part in ("switches", "judge", "gated_models"):
        assert body[part]["status"] == "could not check", part
    for part in ("rollup", "late_dropped", "cull", "topics_by_dispatch_mode"):
        assert "status" not in body[part], part
    for part in ("queue_depth", "queue_age"):
        assert "status" not in body[part]["eval-work"], part


def test_without_the_evals_table_only_the_parts_that_read_it_cannot_be_checked(aws, queue, monkeypatch):
    monkeypatch.setenv("EVALS_TABLE", "NoSuchTable")

    _, body = call("GET /evals/status")

    for part in ("rollup", "late_dropped", "judge"):
        assert body[part] == {"status": "could not check", "error": "ResourceNotFoundException"}, part
    assert body["switches"]["judge_enabled"] is True and body["gated_models"]["gated"] == []


# --- the digest -----------------------------------------------------------------------------------


def day_row(aws, entity, day, perf, events, good=0, bad=0, reason="Article Completion"):
    aws.Table("Evals").put_item(
        Item={
            "entity": entity, "sk": f"day#{day}", "perf": perf, "events": events, "good": good, "bad": bad,
            "by_reason": {reason: {"perf": perf, "events": events, "good": good, "bad": bad}},
        }
    )  # fmt: skip
    listed = aws.Table("Evals").get_item(Key={"entity": "index", "sk": f"day#{day}"}).get("Item")
    entities = set(listed["entities"]) if listed else set()
    aws.Table("Evals").put_item(Item={"entity": "index", "sk": f"day#{day}", "entities": entities | {entity}})


def test_the_digest_adds_each_models_and_prompts_days_up_from_a_day(aws):
    # The rollup's windows on ALPHA's registry row: 12 events today, under the daily gate of -50.
    aws.Table("Models").update_item(
        Key={"model_id": ALPHA},
        UpdateExpression="SET daily_perf = :p, perf_events = :e",
        ExpressionAttributeValues={":p": -60, ":e": {"daily": 12, "weekly": 12, "monthly": 12, "total": 12}},
    )
    day_row(aws, f"model#{ALPHA}", "2026-10-25", 4, 2)
    day_row(aws, f"model#{ALPHA}", "2026-10-26", -6, 3, good=0, bad=3, reason="User Feedback")
    day_row(aws, f"model#{ALPHA}", "2026-10-10", 100, 50)  # before the digest's first day
    day_row(aws, "model#maker.gone-v1:0", "2026-10-26", 2, 1)
    day_row(aws, "prompt#daily_cycle/draft#v2", "2026-10-26", 2, 1)
    day_row(aws, "cell#maker.alpha-v1:0#daily_cycle/draft#v2", "2026-10-26", 2, 1)

    with patch.object(evals_status, "datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
        status, body = call("GET /evals/digest", query={"since": "2026-10-24"})

    assert status == 200
    assert (body["since"], body["until"]) == ("2026-10-24", "2026-10-27")
    alpha, gone = body["models"]
    assert alpha["model_id"] == ALPHA
    assert (alpha["perf"], alpha["events"], alpha["good"], alpha["bad"]) == (-2, 5, 0, 3)
    assert alpha["n"] == 3 and alpha["interval"][0] == 0.0 and alpha["in_registry"] is True
    assert alpha["by_reason"]["User Feedback"]["perf"] == -6
    # What the gates say of the registry row's windows now, not of the digest's own sum of -2.
    assert alpha["gated"] == ["daily"]
    assert (gone["model_id"], gone["in_registry"], gone["gated"]) == ("maker.gone-v1:0", False, [])
    assert body["prompts"] == [
        {
            "prompt_id": "daily_cycle/draft", "version": "v2", "perf": 2, "events": 1, "good": 0, "bad": 0,
            "n": 0, "interval": [None, None],
            "by_reason": {"Article Completion": {"perf": 2, "events": 1, "good": 0, "bad": 0}},
        }
    ]  # fmt: skip


def test_the_digest_covers_the_last_week_by_default_and_refuses_a_day_it_cannot(aws):
    with patch.object(evals_status, "datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
        assert call("GET /evals/digest")[1]["since"] == "2026-10-21"
        assert call("GET /evals/digest", query={"since": "2026-10-26T14:00:00Z"})[1]["since"] == "2026-10-27"
        for since in ("last week", "2026-10-28", "2026-01-01"):
            status, body = call("GET /evals/digest", query={"since": since})
            assert status == 400 and "'since'" in body["error"], since


def test_the_digests_days_are_those_of_the_settings_time_zone(aws):
    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "timezone": "UTC"})
    with patch.object(evals_status, "datetime") as clock:
        # 20:00 on 26 October in UTC: already 07:00 on the 27th in Sydney.
        clock.now.return_value = datetime(2026, 10, 26, 20, 0, tzinfo=UTC)
        status, body = call("GET /evals/digest")
        at_eight = call("GET /evals/digest", query={"since": "2026-10-26T20:00:00Z"})[1]

    assert status == 200
    assert (body["since"], body["until"], body["timezone"]) == ("2026-10-20", "2026-10-26", "UTC")
    assert at_eight["since"] == "2026-10-26"


# --- GET /models ----------------------------------------------------------------------------------


def test_get_models_rows_carry_the_scores_the_rollup_put_on_them(aws):
    logs = FakeLogs()
    completion(logs, "a1", "daily_cycle/draft", sydney("2026-10-27T09:00"))
    evals_rollup.run(now=sydney("2026-10-27T14:00"), logs_client=lambda region: logs, bedrock=FakeBedrock())

    status, body = call("GET /models")

    assert status == 200
    [alpha] = body["models"]
    assert [alpha[f"{window}_perf"] for window in ("daily", "weekly", "monthly", "total")] == [2, 2, 2, 2]
    assert alpha["perf_events"] == {"daily": 1, "weekly": 1, "monthly": 1, "total": 1}
    # BEDROCK_MODEL_ID, with no default row and no judge_model_id: the default, and the judge.
    assert alpha["model_roles"] == ["default", "judge"]
    assert alpha["perf_updated_at"].startswith("2026-10-27T03:00")


# --- Terraform ------------------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_evals_route_is_in_the_environments_terraform(env):
    main = (Path(__file__).resolve().parents[2] / "infra" / "environments" / env / "main.tf").read_text(
        encoding="utf-8"
    )
    admin_routes = main[: main.index('"GET /articles/{article_id}/feedback-status"')]
    routes = {route for route in admin_api_handler._ROUTES if "/evals" in route}
    assert routes == ROUTES
    for route in routes:
        assert f'"{route}",' in admin_routes, route
