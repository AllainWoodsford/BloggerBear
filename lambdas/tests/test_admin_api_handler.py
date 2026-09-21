from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import admin_api_handler

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("TOPICS_TABLE", "Topics")
    monkeypatch.setenv("FINDINGS_TABLE", "Findings")
    monkeypatch.setenv("CANDIDATE_IDEAS_TABLE", "CandidateIdeas")
    monkeypatch.setenv("ARTICLES_TABLE", "Articles")
    monkeypatch.setenv("MODERATION_QUEUE_TABLE", "ModerationQueue")
    monkeypatch.setenv("PROMPT_REFINEMENTS_TABLE", "PromptRefinements")
    monkeypatch.setenv("FAILED_EXECUTIONS_TABLE", "FailedExecutions")
    monkeypatch.setenv("MODELS_TABLE", "Models")
    monkeypatch.setenv("MODEL_CONFIG_TABLE", "ModelConfig")
    monkeypatch.setenv("CONTENT_BUCKET", "bloggerbear-content-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
    monkeypatch.setenv("RESEARCH_TICK_FUNCTION_NAME", "research-tick-fn")
    monkeypatch.setenv("DAILY_CYCLE_FUNCTION_NAME", "daily-cycle-fn")
    monkeypatch.setenv(
        "RESEARCH_TICK_FUNCTION_ARN",
        "arn:aws:lambda:ap-southeast-2:123456789012:function:research-tick-fn",
    )
    monkeypatch.setenv(
        "STATE_MACHINE_ARN", "arn:aws:states:ap-southeast-2:123456789012:stateMachine:daily-cycle"
    )
    monkeypatch.setenv(
        "SCHEDULER_INVOKE_ROLE_ARN", "arn:aws:iam::123456789012:role/scheduler-invoke"
    )
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")

    # common.dynamo caches a boto3 resource at module scope, and
    # admin_api_handler caches a boto3 lambda client -- reset both so each
    # test gets one bound to moto's mock (or a fresh mock to patch over).
    import common.dynamo as dynamo_module
    import common.scheduler as scheduler_module

    dynamo_module._dynamodb_resource = None
    scheduler_module._scheduler_client = None
    admin_api_handler._lambda_client = None


@pytest.fixture
def aws_resources(aws_env):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="Topics",
            KeySchema=[{"AttributeName": "topic_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "topic_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Findings",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "captured_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "captured_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="CandidateIdeas",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Articles",
            KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "article_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="ModerationQueue",
            KeySchema=[{"AttributeName": "queue_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "queue_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="PromptRefinements",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="FailedExecutions",
            KeySchema=[{"AttributeName": "failure_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "failure_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Models",
            KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "model_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


TOPIC = {
    "topic_id": "github-trending",
    "name": "GitHub Trending",
    "adapter": "github_trending",
    "adapter_config": {"language": "python"},
    "is_financial": False,
}


def _event(route_key, *, path_params=None, body=None, method=None):
    event = {"routeKey": route_key}
    if path_params is not None:
        event["pathParameters"] = path_params
    if body is not None:
        event["body"] = json.dumps(body)
    if method is not None:
        event["requestContext"] = {"http": {"method": method}}
    return event


def _put_topic(topic=TOPIC):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    table.put_item(Item=topic)


# --- Topics CRUD -----------------------------------------------------------


def test_list_topics_empty(aws_resources):
    result = admin_api_handler.handler(_event("GET /topics"), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"topics": []}


def test_list_topics_returns_items(aws_resources):
    _put_topic()
    result = admin_api_handler.handler(_event("GET /topics"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["topics"] == [TOPIC]


def test_create_topic_success(aws_resources):
    body = {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
    }
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 201
    created = json.loads(result["body"])
    assert created == {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
        "adapter_config": {},
        "is_financial": False,
        "research_cadence": "rate(1 hour)",
        "daily_cadence": "cron(0 9 * * ? *)",
        "daily_timezone": "Australia/Sydney",
        "model_id": None,
        "fallback_model_id": None,
        "model_id_candidates": None,
    }

    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    assert table.get_item(Key={"topic_id": "new-topic"})["Item"] == created

    mock_upsert.assert_called_once_with(
        "new-topic", "rate(1 hour)", "cron(0 9 * * ? *)", "Australia/Sydney"
    )


def test_create_topic_accepts_model_id_candidates(aws_resources):
    body = {
        "topic_id": "rotating",
        "name": "Rotating Topic",
        "adapter": "github_trending",
        "model_id_candidates": ["model-a", "model-b"],
    }
    with patch("admin_api_handler.upsert_topic_schedules"):
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 201
    assert json.loads(result["body"])["model_id_candidates"] == ["model-a", "model-b"]


@pytest.mark.parametrize("bad_value", ["model-a", ["model-a", ""], ["model-a", 3], {"a": 1}])
def test_create_topic_rejects_invalid_model_id_candidates(aws_resources, bad_value):
    body = {
        "topic_id": "bad",
        "name": "Bad",
        "adapter": "github_trending",
        "model_id_candidates": bad_value,
    }
    with patch("admin_api_handler.upsert_topic_schedules"):
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 400
    assert "model_id_candidates" in json.loads(result["body"])["error"]


def test_update_topic_sets_and_clears_model_id_candidates(aws_resources):
    _put_topic()

    with patch("admin_api_handler.upsert_topic_schedules"):
        set_result = admin_api_handler.handler(
            _event(
                "PUT /topics/{topic_id}",
                path_params={"topic_id": TOPIC["topic_id"]},
                body={"model_id_candidates": ["model-a", "model-b"]},
            ),
            None,
        )
        clear_result = admin_api_handler.handler(
            _event(
                "PUT /topics/{topic_id}",
                path_params={"topic_id": TOPIC["topic_id"]},
                body={"model_id_candidates": []},
            ),
            None,
        )
        bad_result = admin_api_handler.handler(
            _event(
                "PUT /topics/{topic_id}",
                path_params={"topic_id": TOPIC["topic_id"]},
                body={"model_id_candidates": "model-a"},
            ),
            None,
        )

    assert set_result["statusCode"] == 200
    assert json.loads(set_result["body"])["model_id_candidates"] == ["model-a", "model-b"]
    assert clear_result["statusCode"] == 200
    assert json.loads(clear_result["body"])["model_id_candidates"] == []
    assert bad_result["statusCode"] == 400


def test_create_topic_crypto_feed_forces_is_financial_true(aws_resources):
    # Phase 7: is_financial must be forced True for crypto_feed even when
    # the caller explicitly passes False -- that safety property can't be
    # bypassed by an operator mistake.
    body = {
        "topic_id": "crypto",
        "name": "Crypto Markets",
        "adapter": "crypto_feed",
        "is_financial": False,
    }
    with patch("admin_api_handler.upsert_topic_schedules"):
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 201
    created = json.loads(result["body"])
    assert created["is_financial"] is True

    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    assert table.get_item(Key={"topic_id": "crypto"})["Item"]["is_financial"] is True


def test_create_topic_custom_cadence(aws_resources):
    body = {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
        "research_cadence": "rate(30 minutes)",
        "daily_cadence": "cron(0 12 * * ? *)",
    }
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 201
    created = json.loads(result["body"])
    assert created["research_cadence"] == "rate(30 minutes)"
    assert created["daily_cadence"] == "cron(0 12 * * ? *)"
    assert created["daily_timezone"] == "Australia/Sydney"
    mock_upsert.assert_called_once_with(
        "new-topic", "rate(30 minutes)", "cron(0 12 * * ? *)", "Australia/Sydney"
    )


def test_create_topic_custom_daily_timezone(aws_resources):
    body = {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
        "daily_timezone": "America/New_York",
    }
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 201
    assert json.loads(result["body"])["daily_timezone"] == "America/New_York"
    mock_upsert.assert_called_once_with(
        "new-topic", "rate(1 hour)", "cron(0 9 * * ? *)", "America/New_York"
    )


@pytest.mark.parametrize("bad_timezone", ["", "Sydney time", 5, "Australia/"])
def test_create_topic_invalid_daily_timezone_returns_400(aws_resources, bad_timezone):
    body = {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
        "daily_timezone": bad_timezone,
    }
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 400
    mock_upsert.assert_not_called()


def test_create_topic_invalid_cadence_expression_returns_400(aws_resources):
    body = {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
        "research_cadence": "every hour please",
    }
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 400
    mock_upsert.assert_not_called()

    # The bad cadence must be rejected before the topic item is ever written.
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    assert "Item" not in table.get_item(Key={"topic_id": "new-topic"})


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Missing topic_id", "adapter": "x"},
        {"topic_id": "", "name": "Empty topic_id", "adapter": "x"},
        {"topic_id": "t", "adapter": "x"},
        {"topic_id": "t", "name": "n", "adapter": ""},
        {"topic_id": "t", "name": "n", "adapter": 123},
        {"topic_id": "t", "name": "n", "editorial_goals": "not-an-object"},
        {"topic_id": "t", "name": "n", "editorial_goals": {"primary_focus": ""}},
        {"topic_id": "t", "name": "n", "editorial_goals": {"primary_focus": 5}},
        {"topic_id": "t", "name": "n", "editorial_goals": {"tone": "witty"}},
        {"topic_id": "t", "name": "n", "editorial_goals": {"primary_focus": "x" * 1001}},
        {"topic_id": "t", "name": "n", "adapter": "x", "adapter_config": "not-a-dict"},
        {"topic_id": "t", "name": "n", "adapter": "x", "is_financial": "not-a-bool"},
        {"topic_id": "t", "name": "n", "adapter": "x", "research_cadence": 123},
        {"topic_id": "t", "name": "n", "adapter": "x", "research_cadence": ""},
        {"topic_id": "t", "name": "n", "adapter": "x", "daily_cadence": 123},
        {"topic_id": "t", "name": "n", "adapter": "x", "daily_cadence": ""},
    ],
)
def test_create_topic_invalid_input_returns_400(aws_resources, body):
    result = admin_api_handler.handler(_event("POST /topics", body=body), None)
    assert result["statusCode"] == 400


def test_create_topic_malformed_json_body_returns_400(aws_resources):
    event = {"routeKey": "POST /topics", "body": "{not valid json"}
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_create_topic_conflict_returns_409(aws_resources):
    _put_topic()
    body = {"topic_id": "github-trending", "name": "Dup", "adapter": "github_trending"}
    result = admin_api_handler.handler(_event("POST /topics", body=body), None)
    assert result["statusCode"] == 409


def test_get_topic_found(aws_resources):
    _put_topic()
    event = _event("GET /topics/{topic_id}", path_params={"topic_id": "github-trending"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == TOPIC


def test_get_topic_not_found(aws_resources):
    event = _event("GET /topics/{topic_id}", path_params={"topic_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_update_topic_partial(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"name": "Renamed", "is_financial": True},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    updated = json.loads(result["body"])
    assert updated["name"] == "Renamed"
    assert updated["is_financial"] is True
    assert updated["adapter"] == TOPIC["adapter"]  # untouched field preserved
    # Not present on TOPIC -- update fills in the defaults. The zone is not the
    # new-topic default: this topic's existing schedule is UTC, and an unrelated
    # edit must not move it.
    assert updated["research_cadence"] == "rate(1 hour)"
    assert updated["daily_cadence"] == "cron(0 9 * * ? *)"
    assert updated["daily_timezone"] == "UTC"
    mock_upsert.assert_called_once_with("github-trending", "rate(1 hour)", "cron(0 9 * * ? *)", "UTC")


def test_update_topic_switching_to_crypto_feed_forces_is_financial_true(aws_resources):
    _put_topic()  # adapter=github_trending, is_financial=False
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"adapter": "crypto_feed", "is_financial": False},
    )
    with patch("admin_api_handler.upsert_topic_schedules"):
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    updated = json.loads(result["body"])
    assert updated["adapter"] == "crypto_feed"
    assert updated["is_financial"] is True


def test_update_topic_already_crypto_feed_cannot_unset_is_financial(aws_resources):
    _put_topic({**TOPIC, "topic_id": "crypto", "adapter": "crypto_feed", "is_financial": True})
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "crypto"},
        body={"is_financial": False},
    )
    with patch("admin_api_handler.upsert_topic_schedules"):
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    updated = json.loads(result["body"])
    assert updated["is_financial"] is True


def test_update_topic_custom_cadence(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"research_cadence": "rate(2 hours)", "daily_cadence": "cron(0 18 * * ? *)"},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    updated = json.loads(result["body"])
    assert updated["research_cadence"] == "rate(2 hours)"
    assert updated["daily_cadence"] == "cron(0 18 * * ? *)"
    mock_upsert.assert_called_once_with(
        "github-trending", "rate(2 hours)", "cron(0 18 * * ? *)", "UTC"
    )


def test_update_topic_moves_a_legacy_topic_to_sydney_time(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"daily_cadence": "cron(0 9 * * ? *)", "daily_timezone": "Australia/Sydney"},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    assert json.loads(result["body"])["daily_timezone"] == "Australia/Sydney"
    mock_upsert.assert_called_once_with(
        "github-trending", "rate(1 hour)", "cron(0 9 * * ? *)", "Australia/Sydney"
    )


def test_update_topic_keeps_an_already_set_timezone_on_unrelated_edits(aws_resources):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    table.put_item(Item={**TOPIC, "daily_cadence": "cron(0 9 * * ? *)", "daily_timezone": "Australia/Sydney"})
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"name": "Renamed"},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    assert mock_upsert.call_args.args[3] == "Australia/Sydney"


def test_update_topic_invalid_daily_timezone_returns_400(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"daily_timezone": "not a zone"},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 400
    mock_upsert.assert_not_called()


def test_update_topic_invalid_cadence_expression_returns_400(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"daily_cadence": "whenever"},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 400
    mock_upsert.assert_not_called()


def test_update_topic_not_found(aws_resources):
    event = _event(
        "PUT /topics/{topic_id}", path_params={"topic_id": "nope"}, body={"name": "x"}
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_update_topic_invalid_field_returns_400(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"adapter_config": "nope"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_delete_topic_success(aws_resources):
    _put_topic()
    event = _event("DELETE /topics/{topic_id}", path_params={"topic_id": "github-trending"})
    with patch("admin_api_handler.delete_topic_schedules") as mock_delete_schedules:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"deleted": "github-trending"}
    mock_delete_schedules.assert_called_once_with("github-trending")

    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    assert "Item" not in table.get_item(Key={"topic_id": "github-trending"})


def test_delete_topic_not_found(aws_resources):
    event = _event("DELETE /topics/{topic_id}", path_params={"topic_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- Trigger -----------------------------------------------------------


def test_trigger_research_tick_invokes_lambda(aws_resources):
    _put_topic()
    mock_client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=mock_client):
        event = _event(
            "POST /topics/{topic_id}/trigger",
            path_params={"topic_id": "github-trending"},
            body={"pipeline": "research_tick"},
        )
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 202
    assert json.loads(result["body"]) == {
        "triggered": "research_tick",
        "topic_id": "github-trending",
    }
    mock_client.invoke.assert_called_once_with(
        FunctionName="research-tick-fn",
        InvocationType="Event",
        Payload=json.dumps({"topic_id": "github-trending", "force": True}).encode("utf-8"),
    )


def test_trigger_daily_cycle_invokes_lambda(aws_resources):
    _put_topic()
    mock_client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=mock_client):
        event = _event(
            "POST /topics/{topic_id}/trigger",
            path_params={"topic_id": "github-trending"},
            body={"pipeline": "daily_cycle"},
        )
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 202
    mock_client.invoke.assert_called_once_with(
        FunctionName="daily-cycle-fn",
        InvocationType="Event",
        Payload=json.dumps({"topic_id": "github-trending"}).encode("utf-8"),
    )


def test_trigger_daily_cycle_can_be_forced(aws_resources):
    _put_topic()
    mock_client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=mock_client):
        event = _event(
            "POST /topics/{topic_id}/trigger",
            path_params={"topic_id": "github-trending"},
            body={"pipeline": "daily_cycle", "force": True},
        )
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 202
    mock_client.invoke.assert_called_once_with(
        FunctionName="daily-cycle-fn",
        InvocationType="Event",
        Payload=json.dumps({"topic_id": "github-trending", "force": True}).encode("utf-8"),
    )


@pytest.mark.parametrize(
    "body",
    [
        {"pipeline": "research_tick", "force": True},
        {"pipeline": "daily_cycle", "force": "yes"},
    ],
)
def test_trigger_rejects_force_on_the_wrong_pipeline_or_a_non_boolean(aws_resources, body):
    _put_topic()
    mock_client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=mock_client):
        event = _event(
            "POST /topics/{topic_id}/trigger",
            path_params={"topic_id": "github-trending"},
            body=body,
        )
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 400
    mock_client.invoke.assert_not_called()


def test_trigger_force_false_sends_the_default_payload(aws_resources):
    _put_topic()
    mock_client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=mock_client):
        event = _event(
            "POST /topics/{topic_id}/trigger",
            path_params={"topic_id": "github-trending"},
            body={"pipeline": "daily_cycle", "force": False},
        )
        admin_api_handler.handler(event, None)

    payload = json.loads(mock_client.invoke.call_args.kwargs["Payload"])
    assert payload == {"topic_id": "github-trending"}


def test_trigger_invalid_pipeline_returns_400(aws_resources):
    _put_topic()
    event = _event(
        "POST /topics/{topic_id}/trigger",
        path_params={"topic_id": "github-trending"},
        body={"pipeline": "not_a_pipeline"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_trigger_malformed_json_body_returns_400(aws_resources):
    _put_topic()
    event = {
        "routeKey": "POST /topics/{topic_id}/trigger",
        "pathParameters": {"topic_id": "github-trending"},
        "body": "{not valid json",
    }
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_trigger_unknown_topic_returns_404(aws_resources):
    event = _event(
        "POST /topics/{topic_id}/trigger",
        path_params={"topic_id": "nope"},
        body={"pipeline": "research_tick"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- Candidates -----------------------------------------------------------


def test_list_candidates_returns_all_statuses(aws_resources):
    _put_topic()
    table = boto3.resource("dynamodb", region_name=REGION).Table("CandidateIdeas")
    table.put_item(
        Item={
            "topic_id": "github-trending",
            "created_at": "2026-09-12T00:00:00+00:00",
            "angle": "considered angle",
            "status": "considered",
        }
    )
    table.put_item(
        Item={
            "topic_id": "github-trending",
            "created_at": "2026-09-12T00:00:01+00:00",
            "angle": "selected angle",
            "status": "selected",
        }
    )

    event = _event(
        "GET /topics/{topic_id}/candidates", path_params={"topic_id": "github-trending"}
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["topic_id"] == "github-trending"
    assert len(body["candidates"]) == 2
    statuses = {c["status"] for c in body["candidates"]}
    assert statuses == {"considered", "selected"}


# --- Findings ---------------------------------------------------------------


def test_get_latest_finding_returns_most_recent(aws_resources):
    _put_topic()
    table = boto3.resource("dynamodb", region_name=REGION).Table("Findings")
    table.put_item(
        Item={
            "topic_id": "github-trending",
            "captured_at": "2026-09-12T00:00:00+00:00",
            "summary": "older finding",
        }
    )
    table.put_item(
        Item={
            "topic_id": "github-trending",
            "captured_at": "2026-09-12T01:00:00+00:00",
            "summary": "newer finding",
        }
    )

    event = _event(
        "GET /topics/{topic_id}/findings/latest", path_params={"topic_id": "github-trending"}
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["summary"] == "newer finding"


def test_get_latest_finding_no_findings_yet_returns_404(aws_resources):
    _put_topic()
    event = _event(
        "GET /topics/{topic_id}/findings/latest", path_params={"topic_id": "github-trending"}
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_get_latest_finding_unknown_topic_returns_404(aws_resources):
    event = _event("GET /topics/{topic_id}/findings/latest", path_params={"topic_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_list_candidates_unknown_topic_returns_404(aws_resources):
    event = _event("GET /topics/{topic_id}/candidates", path_params={"topic_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- Moderation queue -----------------------------------------------------


def _put_article(article_id="article-1", status="pending_moderation"):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    table.put_item(
        Item={
            "article_id": article_id,
            "topic_id": "github-trending",
            "title": "A Title",
            "body_s3_key": f"articles/{article_id}.md",
            "status": status,
            "created_at": "2026-09-12T00:00:00+00:00",
            "published_at": None,
            "source_refs": [],
        }
    )


def _put_moderation_item(
    queue_id="queue-1",
    article_id="article-1",
    status="pending",
    topic_id="github-trending",
    reasons=None,
    created_at="2026-09-12T00:00:00+00:00",
):
    table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    table.put_item(
        Item={
            "queue_id": queue_id,
            "article_id": article_id,
            "topic_id": topic_id,
            "reasons": reasons if reasons is not None else ["needs review"],
            "status": status,
            "created_at": created_at,
        }
    )


def test_list_moderation_queue_only_pending(aws_resources):
    _put_moderation_item(queue_id="q-pending", status="pending")
    _put_moderation_item(queue_id="q-approved", status="approved")

    result = admin_api_handler.handler(_event("GET /moderation-queue"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert [item["queue_id"] for item in body["items"]] == ["q-pending"]


def test_moderation_queue_stats_empty(aws_resources):
    result = admin_api_handler.handler(_event("GET /moderation-queue/stats"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body == {
        "total_flagged": 0,
        "by_status": {},
        "by_topic": {},
        "reason_counts": {},
        "recent": [],
    }


def test_moderation_queue_stats_aggregates_across_all_statuses(aws_resources):
    # Unlike GET /moderation-queue (pending only), stats must include
    # approved/rejected history too -- that's the whole point.
    _put_moderation_item(
        queue_id="q-1",
        topic_id="github-trending",
        status="pending",
        reasons=["financial topic - routed to manual moderation regardless of content"],
        created_at="2026-09-10T00:00:00+00:00",
    )
    _put_moderation_item(
        queue_id="q-2",
        topic_id="github-trending",
        status="approved",
        reasons=["financial topic - routed to manual moderation regardless of content"],
        created_at="2026-09-11T00:00:00+00:00",
    )
    _put_moderation_item(
        queue_id="q-3",
        topic_id="crypto",
        status="rejected",
        reasons=["unsubstantiated factual claim"],
        created_at="2026-09-12T00:00:00+00:00",
    )

    result = admin_api_handler.handler(_event("GET /moderation-queue/stats"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])

    assert body["total_flagged"] == 3
    assert body["by_status"] == {"pending": 1, "approved": 1, "rejected": 1}
    assert body["by_topic"] == {"github-trending": 2, "crypto": 1}
    assert body["reason_counts"] == {
        "financial topic - routed to manual moderation regardless of content": 2,
        "unsubstantiated factual claim": 1,
    }
    # Most recent first.
    assert [item["queue_id"] for item in body["recent"]] == ["q-3", "q-2", "q-1"]


def test_approve_moderation_item(aws_resources):
    _put_article()
    _put_moderation_item()
    _put_topic()

    with (
        patch("admin_api_handler.read_article_body", return_value="# Body") as mock_read_body,
        patch("admin_api_handler.render_and_publish_article_page") as mock_render_page,
        patch("admin_api_handler.generate_and_store_article_musing") as mock_musing,
    ):
        event = _event(
            "POST /moderation-queue/{queue_id}/approve", path_params={"queue_id": "queue-1"}
        )
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"approved": "queue-1", "article_id": "article-1"}

    articles_table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    article = articles_table.get_item(Key={"article_id": "article-1"})["Item"]
    assert article["status"] == "published"
    assert article["published_at"] is not None

    queue_table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    queue_item = queue_table.get_item(Key={"queue_id": "queue-1"})["Item"]
    assert queue_item["status"] == "approved"

    # Static article publishing (docs/project-plan.md §11): approving a
    # moderation-queue item must regenerate the static page too, or it
    # would drift out of sync with the Articles table.
    mock_read_body.assert_called_once_with("articles/article-1.md")
    mock_render_page.assert_called_once()
    render_kwargs = mock_render_page.call_args.kwargs
    assert render_kwargs["article_id"] == "article-1"
    assert render_kwargs["title"] == "A Title"
    assert render_kwargs["body_markdown"] == "# Body"
    assert render_kwargs["topic_name"] == "GitHub Trending"
    assert render_kwargs["published_at"] == article["published_at"]
    # AI lineage/cost tracking (docs/project-plan.md §11, PR 3 of 5):
    # lineage was never set on this fixture (this article predates the
    # feature, or was drafted before lineage support existed) -- passed
    # through as-is, not fabricated. published_by is "humans" since this
    # path always needed a moderation-approve.
    assert render_kwargs["lineage"] is None
    assert render_kwargs["published_by"] == "humans"

    # Reaching this path always needed a moderation-approve, so the musing
    # is generated with compliant=False (the more measured/thoughtful
    # mood, not the published-cleanly "proud" one).
    mock_musing.assert_called_once()
    musing_kwargs = mock_musing.call_args.kwargs
    assert musing_kwargs["article_id"] == "article-1"
    assert musing_kwargs["topic_id"] == "github-trending"
    assert musing_kwargs["topic_name"] == "GitHub Trending"
    assert musing_kwargs["title"] == "A Title"
    assert musing_kwargs["compliant"] is False


def test_reject_moderation_item(aws_resources):
    _put_article()
    _put_moderation_item()

    event = _event("POST /moderation-queue/{queue_id}/reject", path_params={"queue_id": "queue-1"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"rejected": "queue-1", "article_id": "article-1"}

    articles_table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    article = articles_table.get_item(Key={"article_id": "article-1"})["Item"]
    assert article["status"] == "rejected"
    assert article["published_at"] is None

    queue_table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    queue_item = queue_table.get_item(Key={"queue_id": "queue-1"})["Item"]
    assert queue_item["status"] == "rejected"


def test_approve_unknown_queue_item_returns_404(aws_resources):
    event = _event("POST /moderation-queue/{queue_id}/approve", path_params={"queue_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_approve_already_actioned_returns_409(aws_resources):
    _put_article()
    _put_moderation_item(status="approved")

    event = _event("POST /moderation-queue/{queue_id}/approve", path_params={"queue_id": "queue-1"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 409


def test_reject_already_actioned_returns_409(aws_resources):
    _put_article()
    _put_moderation_item(status="rejected")

    event = _event("POST /moderation-queue/{queue_id}/reject", path_params={"queue_id": "queue-1"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 409


# --- Articles (force-publish) ------------------------------------------


def test_publish_article_sets_published_status(aws_resources):
    _put_article(status="pending_moderation")
    _put_topic()

    with (
        patch("admin_api_handler.read_article_body", return_value="# Body"),
        patch("admin_api_handler.render_and_publish_article_page") as mock_render_page,
        patch("admin_api_handler.generate_and_store_article_musing"),
    ):
        event = _event(
            "POST /articles/{article_id}/publish", path_params={"article_id": "article-1"}
        )
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"published": "article-1"}

    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    article = table.get_item(Key={"article_id": "article-1"})["Item"]
    assert article["status"] == "published"
    assert article["published_at"] is not None

    # Static article publishing (docs/project-plan.md §11): force-publish
    # must regenerate the static page, same as the moderation-approve path.
    mock_render_page.assert_called_once()
    assert mock_render_page.call_args.kwargs["article_id"] == "article-1"
    # AI lineage/cost tracking (docs/project-plan.md §11, PR 3 of 5):
    # force-publish is always "humans" too -- an operator invoked it.
    assert mock_render_page.call_args.kwargs["published_by"] == "humans"


def test_publish_article_passes_through_existing_lineage(aws_resources):
    """lineage was fixed at draft time -- force-publish must pass through
    whatever's already stored on the article, never fabricate or drop it."""
    lineage = {
        "calls": [
            {
                "stage": "draft",
                "model_id": "model-a",
                "input_tokens": Decimal(10),
                "output_tokens": Decimal(5),
                "used_fallback": False,
            }
        ],
        "total_input_tokens": Decimal(10),
        "total_output_tokens": Decimal(5),
        "models_used": ["model-a"],
        "cost_aud": Decimal("0.02"),
        "cost_note": None,
    }
    _put_article(status="pending_moderation")
    _put_topic()
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    table.update_item(
        Key={"article_id": "article-1"},
        UpdateExpression="SET lineage = :lineage",
        ExpressionAttributeValues={":lineage": lineage},
    )

    with (
        patch("admin_api_handler.read_article_body", return_value="# Body"),
        patch("admin_api_handler.render_and_publish_article_page") as mock_render_page,
        patch("admin_api_handler.generate_and_store_article_musing"),
    ):
        event = _event(
            "POST /articles/{article_id}/publish", path_params={"article_id": "article-1"}
        )
        admin_api_handler.handler(event, None)

    render_kwargs = mock_render_page.call_args.kwargs
    assert render_kwargs["lineage"]["models_used"] == ["model-a"]
    assert render_kwargs["lineage"]["cost_aud"] == 0.02


def test_publish_article_also_approves_pending_moderation_item(aws_resources):
    _put_article(status="pending_moderation")
    _put_moderation_item(status="pending")
    _put_topic()

    with (
        patch("admin_api_handler.read_article_body", return_value="# Body"),
        patch("admin_api_handler.render_and_publish_article_page"),
        patch("admin_api_handler.generate_and_store_article_musing"),
    ):
        event = _event(
            "POST /articles/{article_id}/publish", path_params={"article_id": "article-1"}
        )
        result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200

    queue_table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    queue_item = queue_table.get_item(Key={"queue_id": "queue-1"})["Item"]
    assert queue_item["status"] == "approved"


def test_publish_article_leaves_already_resolved_moderation_item_alone(aws_resources):
    _put_article(status="rejected")
    _put_moderation_item(status="rejected")
    _put_topic()

    with (
        patch("admin_api_handler.read_article_body", return_value="# Body"),
        patch("admin_api_handler.render_and_publish_article_page"),
        patch("admin_api_handler.generate_and_store_article_musing"),
    ):
        event = _event(
            "POST /articles/{article_id}/publish", path_params={"article_id": "article-1"}
        )
        result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200

    queue_table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    queue_item = queue_table.get_item(Key={"queue_id": "queue-1"})["Item"]
    assert queue_item["status"] == "rejected"


def test_publish_unknown_article_returns_404(aws_resources):
    event = _event("POST /articles/{article_id}/publish", path_params={"article_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- Articles (unpublish) ------------------------------------------------


def _unpublish(article_id="article-1", *, musings_removed=1, cache_invalidated=True):
    with (
        patch("admin_api_handler.remove_article_page") as mock_remove,
        patch("admin_api_handler.delete_musings_for_article", return_value=musings_removed) as mock_musings,
        patch("admin_api_handler.invalidate_article_page", return_value=cache_invalidated) as mock_invalidate,
    ):
        event = _event(
            "POST /articles/{article_id}/unpublish", path_params={"article_id": article_id}
        )
        result = admin_api_handler.handler(event, None)
    return result, mock_remove, mock_musings, mock_invalidate


def test_unpublish_takes_a_published_article_down(aws_resources):
    _put_article(status="published")
    _put_moderation_item(status="approved")

    result, mock_remove, mock_musings, mock_invalidate = _unpublish()

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "unpublished": "article-1",
        "musings_removed": 1,
        "cache_invalidated": True,
    }
    mock_remove.assert_called_once_with("article-1")
    mock_musings.assert_called_once_with("article-1")
    mock_invalidate.assert_called_once_with("article-1")

    article = boto3.resource("dynamodb", region_name=REGION).Table("Articles").get_item(
        Key={"article_id": "article-1"}
    )["Item"]
    assert article["status"] == "rejected"
    queue_item = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue").get_item(
        Key={"queue_id": "queue-1"}
    )["Item"]
    assert queue_item["status"] == "rejected"


def test_unpublish_reports_when_the_cache_could_not_be_invalidated(aws_resources):
    _put_article(status="published")

    result, *_ = _unpublish(cache_invalidated=False)

    assert result["statusCode"] == 200
    assert json.loads(result["body"])["cache_invalidated"] is False


def test_unpublish_works_for_an_article_that_never_had_a_queue_item(aws_resources):
    _put_article(status="published")

    result, *_ = _unpublish()

    assert result["statusCode"] == 200


def test_unpublish_deletes_the_page_before_changing_any_status(aws_resources):
    """If removing the page fails, the article must still read `published`, so
    a retry finds it and a half-done unpublish never hides a live page."""
    _put_article(status="published")
    _put_moderation_item(status="approved")

    with (
        patch("admin_api_handler.remove_article_page", side_effect=RuntimeError("s3 down")),
        patch("admin_api_handler.delete_musings_for_article"),
        patch("admin_api_handler.invalidate_article_page"),
    ):
        event = _event("POST /articles/{article_id}/unpublish", path_params={"article_id": "article-1"})
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 500
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    assert table.get_item(Key={"article_id": "article-1"})["Item"]["status"] == "published"
    queue = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    assert queue.get_item(Key={"queue_id": "queue-1"})["Item"]["status"] == "approved"


def test_unpublish_can_be_repeated_on_an_already_rejected_article(aws_resources):
    """The repair path: a failure after the status change is fixed by running it again."""
    _put_article(status="rejected")
    _put_moderation_item(status="rejected")

    result, mock_remove, mock_musings, mock_invalidate = _unpublish()

    assert result["statusCode"] == 200
    mock_remove.assert_called_once_with("article-1")
    mock_musings.assert_called_once_with("article-1")
    mock_invalidate.assert_called_once_with("article-1")


def test_unpublish_refuses_an_article_still_awaiting_moderation(aws_resources):
    _put_article(status="pending_moderation")
    _put_moderation_item(status="pending")

    result, mock_remove, mock_musings, mock_invalidate = _unpublish()

    assert result["statusCode"] == 409
    assert "moderation reject" in json.loads(result["body"])["error"]
    mock_remove.assert_not_called()
    mock_musings.assert_not_called()
    mock_invalidate.assert_not_called()
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    assert table.get_item(Key={"article_id": "article-1"})["Item"]["status"] == "pending_moderation"


def test_unpublish_unknown_article_returns_404(aws_resources):
    result, mock_remove, *_ = _unpublish("nope")

    assert result["statusCode"] == 404
    mock_remove.assert_not_called()


# --- Failed executions (DLQ consumer) -----------------------------------


def test_list_failed_executions_empty(aws_resources):
    result = admin_api_handler.handler(_event("GET /failed-executions"), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"items": []}


def test_list_failed_executions_returns_items(aws_resources):
    table = boto3.resource("dynamodb", region_name=REGION).Table("FailedExecutions")
    table.put_item(
        Item={
            "failure_id": "f-1",
            "topic_id": "github-trending",
            "error": {"Error": "States.TaskFailed", "Cause": "boom"},
            "raw_message": '{"topic_id": "github-trending"}',
            "created_at": "2026-09-19T21:00:00+00:00",
        }
    )
    result = admin_api_handler.handler(_event("GET /failed-executions"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert [item["failure_id"] for item in body["items"]] == ["f-1"]


# --- Models / ModelConfig (AI lineage/cost-tracking enhancement, PR 1) -----


def test_list_models_empty(aws_resources):
    result = admin_api_handler.handler(_event("GET /models"), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"models": []}


def test_put_model_creates_entry(aws_resources):
    body = {
        "model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
        "display_name": "Claude Haiku 4.5",
        "provider": "anthropic",
        "input_price_usd_per_1k_tokens": 0.0008,
        "output_price_usd_per_1k_tokens": 0.004,
        "enabled": True,
    }
    result = admin_api_handler.handler(_event("POST /models", body=body), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == body

    list_result = admin_api_handler.handler(_event("GET /models"), None)
    listed = json.loads(list_result["body"])["models"]
    assert [m["model_id"] for m in listed] == [body["model_id"]]


def test_put_model_defaults_enabled_true(aws_resources):
    body = {
        "model_id": "amazon.nova-2",
        "display_name": "Amazon Nova 2",
        "provider": "amazon",
        "input_price_usd_per_1k_tokens": 0.0003,
        "output_price_usd_per_1k_tokens": 0.0012,
    }
    result = admin_api_handler.handler(_event("POST /models", body=body), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["enabled"] is True


@pytest.mark.parametrize(
    "missing_field",
    [
        "model_id",
        "display_name",
        "provider",
        "input_price_usd_per_1k_tokens",
        "output_price_usd_per_1k_tokens",
    ],
)
def test_put_model_missing_required_field_returns_400(aws_resources, missing_field):
    body = {
        "model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
        "display_name": "Claude Haiku 4.5",
        "provider": "anthropic",
        "input_price_usd_per_1k_tokens": 0.0008,
        "output_price_usd_per_1k_tokens": 0.004,
    }
    del body[missing_field]
    result = admin_api_handler.handler(_event("POST /models", body=body), None)
    assert result["statusCode"] == 400


def test_put_model_negative_price_returns_400(aws_resources):
    body = {
        "model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
        "display_name": "Claude Haiku 4.5",
        "provider": "anthropic",
        "input_price_usd_per_1k_tokens": -0.1,
        "output_price_usd_per_1k_tokens": 0.004,
    }
    result = admin_api_handler.handler(_event("POST /models", body=body), None)
    assert result["statusCode"] == 400


def test_get_model_config_defaults_when_unset(aws_resources):
    result = admin_api_handler.handler(_event("GET /model-config"), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "config_id": "default",
        "model_id": None,
        "fallback_model_id": None,
    }


def test_put_model_config_roundtrip(aws_resources):
    body = {"model_id": "amazon.nova-2", "fallback_model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0"}
    result = admin_api_handler.handler(_event("PUT /model-config", body=body), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"config_id": "default", **body}

    get_result = admin_api_handler.handler(_event("GET /model-config"), None)
    assert json.loads(get_result["body"]) == {"config_id": "default", **body}


def test_put_model_config_invalid_type_returns_400(aws_resources):
    result = admin_api_handler.handler(_event("PUT /model-config", body={"model_id": 123}), None)
    assert result["statusCode"] == 400


# --- Prompt refinements -----------------------------------------------


def _put_refinement(
    topic_id="github-trending",
    version="2026-09-12T00:00:00+00:00",
    status="pending",
    rationale="recent feedback skewed negative",
    prompt_changes="Write in a more accessible style.",
):
    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    item = {
        "topic_id": topic_id,
        "version": version,
        "proposed_at": version,
        "rationale": rationale,
        "prompt_changes": prompt_changes,
        "status": status,
    }
    table.put_item(Item=item)
    return item


def test_list_prompt_refinements_no_filters(aws_resources):
    _put_refinement(topic_id="topic-a", version="v1")
    _put_refinement(topic_id="topic-b", version="v1")

    result = admin_api_handler.handler(_event("GET /prompt-refinements"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert {r["topic_id"] for r in body["refinements"]} == {"topic-a", "topic-b"}


def test_list_prompt_refinements_filters_by_topic_and_status(aws_resources):
    _put_refinement(topic_id="topic-a", version="v1", status="pending")
    _put_refinement(topic_id="topic-a", version="v2", status="approved")
    _put_refinement(topic_id="topic-b", version="v1", status="pending")

    event = {
        "routeKey": "GET /prompt-refinements",
        "queryStringParameters": {"topic_id": "topic-a", "status": "pending"},
    }
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert len(body["refinements"]) == 1
    assert body["refinements"][0]["version"] == "v1"
    assert body["refinements"][0]["topic_id"] == "topic-a"


def test_approve_prompt_refinement_success(aws_resources):
    _put_refinement()
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/approve",
        path_params={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    # Approving with no choice made wears it as a ring for its own topic.
    approved = json.loads(result["body"])["approved"]
    assert approved["topic_id"] == "github-trending"
    assert approved["version"] == "2026-09-12T00:00:00+00:00"
    assert approved["placement"] == {"equipped": True, "slot": "ring", "scope": "topic", "displaced": None}
    assert approved["item"]["rarity"] in ("common", "uncommon", "rare", "epic", "legendary")

    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    item = table.get_item(
        Key={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"}
    )["Item"]
    assert item["status"] == "approved"
    assert item["equipped"] is True
    assert item["slot"] == "ring"


def test_reject_prompt_refinement_success(aws_resources):
    _put_refinement()
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/reject",
        path_params={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "rejected": {"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"}
    }

    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    item = table.get_item(
        Key={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"}
    )["Item"]
    assert item["status"] == "rejected"


def test_approve_prompt_refinement_not_found_returns_404(aws_resources):
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/approve",
        path_params={"topic_id": "nope", "version": "nope"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_approve_prompt_refinement_already_decided_returns_409(aws_resources):
    _put_refinement(status="approved")
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/approve",
        path_params={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 409


def test_reject_prompt_refinement_already_decided_returns_409(aws_resources):
    _put_refinement(status="rejected")
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/reject",
        path_params={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 409


# --- Exception safety / routing -----------------------------------------


def test_unknown_route_returns_404(aws_resources):
    result = admin_api_handler.handler(_event("GET /nope"), None)
    assert result["statusCode"] == 404


def test_unhandled_exception_returns_500(aws_resources):
    with patch("admin_api_handler.list_topics", side_effect=RuntimeError("boom")):
        result = admin_api_handler.handler(_event("GET /topics"), None)

    assert result["statusCode"] == 500
    assert "error" in json.loads(result["body"])


# --- editorial goals + default adapter ------------------------------------------------


def _create(body):
    with patch("admin_api_handler.upsert_topic_schedules"):
        return admin_api_handler.handler(_event("POST /topics", body=body), None)


def _update(topic_id, body):
    event = _event("PUT /topics/{topic_id}", path_params={"topic_id": topic_id}, body=body)
    with patch("admin_api_handler.upsert_topic_schedules"):
        return admin_api_handler.handler(event, None)


def test_a_topic_created_without_an_adapter_defaults_to_web_search(aws_resources):
    result = _create({"topic_id": "bare-topic", "name": "Bare Topic"})

    assert result["statusCode"] == 201
    created = json.loads(result["body"])
    assert created["adapter"] == "web_search"
    assert "editorial_goals" not in created  # inherits the adapter/global default goal


def test_create_topic_stores_a_stripped_editorial_goals_block(aws_resources):
    goals = {
        "primary_focus": "  Identify unpatched zero-day exploits seen in production.  ",
        "exclusion_criteria": "Ignore marketing press releases.",
    }
    result = _create({"topic_id": "sec", "name": "Security", "editorial_goals": goals})

    assert result["statusCode"] == 201
    stored = boto3.resource("dynamodb", region_name=REGION).Table("Topics").get_item(
        Key={"topic_id": "sec"}
    )["Item"]
    assert stored["editorial_goals"] == {
        "primary_focus": "Identify unpatched zero-day exploits seen in production.",
        "exclusion_criteria": "Ignore marketing press releases.",
    }


def test_a_partial_editorial_goals_block_is_allowed(aws_resources):
    result = _create(
        {"topic_id": "sec", "name": "Security", "editorial_goals": {"exclusion_criteria": "No PR."}}
    )

    assert json.loads(result["body"])["editorial_goals"] == {"exclusion_criteria": "No PR."}


def test_update_topic_replaces_and_clears_editorial_goals(aws_resources):
    _put_topic()

    result = _update("github-trending", {"editorial_goals": {"primary_focus": "  Watch AI infra.  "}})
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["editorial_goals"] == {"primary_focus": "Watch AI infra."}

    result = _update("github-trending", {"editorial_goals": {}})
    assert json.loads(result["body"])["editorial_goals"] == {}

    _update("github-trending", {"editorial_goals": {"primary_focus": "Again"}})
    result = _update("github-trending", {"editorial_goals": None})
    assert json.loads(result["body"])["editorial_goals"] == {}


@pytest.mark.parametrize(
    "bad",
    ["nope", ["a"], {"primary_focus": ""}, {"primary_focus": "  "}, {"tone": "x"}, {"exclusion_criteria": 1}],
)
def test_update_topic_rejects_invalid_editorial_goals_without_writing(aws_resources, bad):
    _put_topic()

    result = _update("github-trending", {"editorial_goals": bad})

    assert result["statusCode"] == 400
    stored = boto3.resource("dynamodb", region_name=REGION).Table("Topics").get_item(
        Key={"topic_id": "github-trending"}
    )["Item"]
    assert "editorial_goals" not in stored


def test_updating_other_fields_leaves_editorial_goals_alone(aws_resources):
    _put_topic({**TOPIC, "editorial_goals": {"primary_focus": "Keep me"}})

    result = _update("github-trending", {"name": "Renamed"})

    assert json.loads(result["body"])["editorial_goals"] == {"primary_focus": "Keep me"}


# --- Feedback limits -----------------------------------------------------------------------


def _feedback_config(method_route, body=None):
    return admin_api_handler.handler(_event(method_route, body=body), None)


def test_feedback_config_get_shows_defaults_and_usage(aws_resources):
    result = _feedback_config("GET /feedback-config")

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["config_id"] == "feedback"
    assert body["rate_limit_count"] is None  # nothing stored...
    assert body["effective"] == {  # ...so the defaults are in force
        "locked_down": False,
        "lockdown_reason": None,
        "rate_limit_count": 20,
        "rate_limit_window_minutes": 5,
        "daily_limit": 100,
        "article_limit": 50,
        "screening_limit": 300,
        "verification_required": True,
        "token_delay_min_ms": 500,
        "token_delay_max_ms": 2000,
        "pow_threshold_percent": 70,
        "pow_difficulty_bits": 16,
        "daily_timezone": "Australia/Sydney",
    }
    assert body["usage"]["today"] == 0 and body["usage"]["this_window"] == 0


def test_feedback_config_put_sets_and_clears_settings(aws_resources):
    result = _feedback_config(
        "PUT /feedback-config",
        {
            "locked_down": True,
            "lockdown_reason": "  Back soon  ",
            "rate_limit_count": 5,
            "rate_limit_window_minutes": 10,
            "daily_limit": 30,
            "article_limit": 8,
            "screening_limit": 40,
            "verification_required": False,
            "token_delay_min_ms": 100,
            "token_delay_max_ms": 900,
            "pow_threshold_percent": 50,
            "pow_difficulty_bits": 12,
            "daily_timezone": "UTC",
        },
    )

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["effective"] == {
        "locked_down": True,
        "lockdown_reason": "Back soon",
        "rate_limit_count": 5,
        "rate_limit_window_minutes": 10,
        "daily_limit": 30,
        "article_limit": 8,
        "screening_limit": 40,
        "verification_required": False,
        "token_delay_min_ms": 100,
        "token_delay_max_ms": 900,
        "pow_threshold_percent": 50,
        "pow_difficulty_bits": 12,
        "daily_timezone": "UTC",
    }

    # null clears one setting back to its default; the others are untouched.
    cleared = json.loads(_feedback_config("PUT /feedback-config", {"rate_limit_count": None})["body"])
    assert cleared["rate_limit_count"] is None
    assert cleared["effective"]["rate_limit_count"] == 20
    assert cleared["effective"]["daily_limit"] == 30


def test_feedback_config_put_a_setting_not_sent_is_left_alone(aws_resources):
    _feedback_config("PUT /feedback-config", {"daily_limit": 30})

    body = json.loads(_feedback_config("PUT /feedback-config", {"article_limit": 9})["body"])

    assert body["daily_limit"] == 30 and body["article_limit"] == 9


@pytest.mark.parametrize(
    "body",
    [
        {"rate_limit_count": 0},
        {"rate_limit_count": "20"},
        {"rate_limit_count": 2.5},
        {"rate_limit_count": True},
        {"rate_limit_window_minutes": 5000},
        {"daily_limit": -1},
        {"article_limit": 10**9},
        {"screening_limit": 0},
        {"verification_required": "no"},
        {"token_delay_min_ms": -1},
        {"token_delay_max_ms": 30_001},
        {"pow_threshold_percent": 0},
        {"pow_threshold_percent": 101},
        {"pow_difficulty_bits": 25},
        {"locked_down": "true"},
        {"lockdown_reason": ""},
        {"lockdown_reason": "x" * 101},
        {"daily_timezone": "Mars/Olympus"},
    ],
)
def test_feedback_config_put_rejects_invalid_values(aws_resources, body):
    result = _feedback_config("PUT /feedback-config", body)

    assert result["statusCode"] == 400
    # Nothing was stored.
    assert json.loads(_feedback_config("GET /feedback-config")["body"])["effective"][
        "daily_limit"
    ] == 100


def test_feedback_config_put_needs_at_least_one_setting(aws_resources):
    assert _feedback_config("PUT /feedback-config", {})["statusCode"] == 400
    assert _feedback_config("PUT /feedback-config", {"unknown": 1})["statusCode"] == 400


def test_feedback_config_put_a_non_object_body_is_a_400(aws_resources):
    event = {"routeKey": "PUT /feedback-config", "body": json.dumps([1])}

    assert admin_api_handler.handler(event, None)["statusCode"] == 400


def _feedback_lock(article_id, body):
    return admin_api_handler.handler(
        _event(
            "PUT /articles/{article_id}/feedback-lock",
            path_params={"article_id": article_id},
            body=body,
        ),
        None,
    )


def _article_row(article_id="article-1"):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    return table.get_item(Key={"article_id": article_id})["Item"]


def test_feedback_lock_and_unlock_an_article(aws_resources):
    _put_article()

    locked = _feedback_lock("article-1", {"locked": True})
    assert locked["statusCode"] == 200
    assert json.loads(locked["body"]) == {
        "article_id": "article-1",
        "feedback_locked": True,
        "feedback_count": 0,
    }
    assert _article_row()["feedback_locked"] is True

    unlocked = _feedback_lock("article-1", {"locked": False})
    assert json.loads(unlocked["body"])["feedback_locked"] is False
    assert _article_row()["feedback_locked"] is False


def test_feedback_unlock_can_reset_the_count(aws_resources):
    _put_article()
    boto3.resource("dynamodb", region_name=REGION).Table("Articles").update_item(
        Key={"article_id": "article-1"},
        UpdateExpression="SET feedback_locked = :t, feedback_count = :n",
        ExpressionAttributeValues={":t": True, ":n": 50},
    )

    result = _feedback_lock("article-1", {"locked": False, "reset_count": True})

    assert json.loads(result["body"]) == {
        "article_id": "article-1",
        "feedback_locked": False,
        "feedback_count": 0,
    }


def test_feedback_lock_validates_its_input(aws_resources):
    _put_article()

    assert _feedback_lock("nope", {"locked": True})["statusCode"] == 404
    assert _feedback_lock("article-1", {})["statusCode"] == 400
    assert _feedback_lock("article-1", {"locked": "yes"})["statusCode"] == 400
    assert _feedback_lock("article-1", {"locked": False, "reset_count": "yes"})["statusCode"] == 400
    assert "feedback_locked" not in _article_row()


# --- GET /articles/{article_id}: one article, in any status, for the review inbox -----------


def _get_article_route(article_id="article-1"):
    event = _event("GET /articles/{article_id}", path_params={"article_id": article_id})
    result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


def test_get_article_shows_a_draft_that_is_still_waiting_for_a_decision(aws_resources):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    table.put_item(
        Item={
            "article_id": "article-1",
            "topic_id": "github-trending",
            "title": "A Title",
            "body_s3_key": "articles/article-1.md",
            "status": "pending_moderation",
            "created_at": "2026-09-12T00:00:00+00:00",
            "published_at": None,
            "source_refs": [
                {"url": "https://example.com/a", "title": "A"},
                {"url": "https://example.com/a", "title": "A"},
            ],
            "net_votes": 3,  # a Decimal in DynamoDB: must not break the response
            "lineage": {"models_used": ["m1"], "total_cost_aud": Decimal("0.0123"), "calls": []},
        }
    )

    with patch("admin_api_handler.read_article_body", return_value="# Body\n\nThe text."):
        code, body = _get_article_route()

    assert code == 200
    assert body["title"] == "A Title" and body["status"] == "pending_moderation"
    assert body["body"] == "# Body\n\nThe text."
    assert body["source_refs"] == [{"title": "A", "url": "https://example.com/a"}]  # de-duplicated
    assert body["models_used"] == ["m1"] and body["cost_aud"] == 0.0123
    assert body["topic_id"] == "github-trending" and "body_error" not in body


def test_get_article_still_answers_if_the_text_cannot_be_read(aws_resources):
    _put_article()

    with patch("admin_api_handler.read_article_body", side_effect=RuntimeError("s3 down")):
        code, body = _get_article_route()

    assert code == 200
    assert body["title"] == "A Title" and body["body"] == ""
    assert "could not read the article text" in body["body_error"]
    assert "s3 down" not in body["body_error"]  # the error type only


def test_get_article_without_lineage_has_no_cost(aws_resources):
    _put_article()

    with patch("admin_api_handler.read_article_body", return_value="x"):
        _, body = _get_article_route()

    assert body["cost_aud"] is None and body["models_used"] == []


def test_get_article_unknown_is_404(aws_resources):
    code, body = _get_article_route("nope")

    assert code == 404 and "not found" in body["error"]


# --- Equipment: wearing approved prompt refinements ------------------------------------


def _refinement_item(version):
    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    return table.get_item(Key={"topic_id": "github-trending", "version": version})["Item"]


def _post(action, version, body=None, topic_id="github-trending"):
    return admin_api_handler.handler(
        _event(
            f"POST /prompt-refinements/{{topic_id}}/{{version}}/{action}",
            path_params={"topic_id": topic_id, "version": version},
            body=body,
        ),
        None,
    )


def _approved(version, text="Be brief."):
    return _put_refinement(version=version, status="approved", prompt_changes=text)


def test_approve_into_an_armor_slot_makes_it_global_guidance(aws_resources):
    _put_refinement(version="v1")

    result = _post("approve", "v1", {"scope": "global", "slot": "helmet"})

    assert result["statusCode"] == 200
    placement = json.loads(result["body"])["approved"]["placement"]
    assert placement == {"equipped": True, "slot": "helmet", "scope": "global", "displaced": None}
    item = _refinement_item("v1")
    assert (item["equipped"], item["slot"], item["scope"]) == (True, "helmet", "global")


def test_approve_to_the_backpack_approves_without_wearing(aws_resources):
    _put_refinement(version="v1")

    result = _post("approve", "v1", {"scope": "backpack"})

    assert json.loads(result["body"])["approved"]["placement"]["equipped"] is False
    item = _refinement_item("v1")
    assert item["status"] == "approved" and item["equipped"] is False
    assert "slot" not in item


def test_approve_with_every_ring_worn_waits_in_the_backpack_by_default(aws_resources):
    for n in range(5):
        _put_refinement(version=f"r{n}")
        assert _post("approve", f"r{n}")["statusCode"] == 200
    _put_refinement(version="extra")

    result = _post("approve", "extra")

    assert result["statusCode"] == 200
    assert json.loads(result["body"])["approved"]["placement"]["equipped"] is False
    assert _refinement_item("extra")["status"] == "approved"


def test_approve_with_every_ring_worn_can_name_the_ring_to_replace(aws_resources):
    for n in range(5):
        _put_refinement(version=f"r{n}")
        _post("approve", f"r{n}")
    _put_refinement(version="extra")

    replace = {"topic_id": "github-trending", "version": "r2"}
    result = _post("approve", "extra", {"scope": "topic", "replace": replace})

    placement = json.loads(result["body"])["approved"]["placement"]
    assert placement["equipped"] is True
    assert placement["displaced"] == {"topic_id": "github-trending", "version": "r2"}
    assert _refinement_item("r2")["equipped"] is False
    assert _refinement_item("r2")["status"] == "approved"  # benched, not rejected


def test_an_impossible_placement_leaves_the_item_pending(aws_resources):
    _put_refinement(version="v1")

    result = _post("approve", "v1", {"scope": "global", "slot": "hat"})

    assert result["statusCode"] == 400
    assert _refinement_item("v1")["status"] == "pending"


def test_approve_rejects_a_body_that_is_not_an_object(aws_resources):
    _put_refinement(version="v1")

    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/approve",
        path_params={"topic_id": "github-trending", "version": "v1"},
    )
    event["body"] = "[1]"

    assert admin_api_handler.handler(event, None)["statusCode"] == 400
    event["body"] = "not json"
    assert admin_api_handler.handler(event, None)["statusCode"] == 400
    assert _refinement_item("v1")["status"] == "pending"


def test_rejecting_never_wears_anything(aws_resources):
    _put_refinement(version="v1")

    _post("reject", "v1")

    assert "equipped" not in _refinement_item("v1")


def test_equipping_into_an_occupied_slot_benches_the_old_item(aws_resources):
    _approved("old")
    _approved("new")
    assert _post("equip", "old", {"scope": "global", "slot": "helmet"})["statusCode"] == 200

    result = _post("equip", "new", {"scope": "global", "slot": "helmet"})

    assert result["statusCode"] == 200
    body = json.loads(result["body"])["equipped"]
    assert body["displaced"] == {"topic_id": "github-trending", "version": "old"}
    old, new = _refinement_item("old"), _refinement_item("new")
    assert old["equipped"] is False and "slot" not in old
    assert new["equipped"] is True and new["slot"] == "helmet"


def test_equip_with_no_slot_takes_the_first_empty_armor_slot(aws_resources):
    _approved("a")
    _approved("b")
    _post("equip", "a", {"scope": "global"})

    _post("equip", "b", {"scope": "global"})

    assert _refinement_item("a")["slot"] == "helmet"
    assert _refinement_item("b")["slot"] == "chest"


def test_equip_with_no_body_wears_it_as_a_ring(aws_resources):
    _approved("a")

    result = _post("equip", "a")

    assert result["statusCode"] == 200
    assert _refinement_item("a")["slot"] == "ring"


def test_the_sixth_ring_is_refused_with_a_conflict(aws_resources):
    for n in range(6):
        _approved(f"r{n}")
    for n in range(5):
        _post("equip", f"r{n}")

    result = _post("equip", "r5")

    assert result["statusCode"] == 409
    assert "equipped" not in _refinement_item("r5")


def test_only_an_approved_refinement_can_be_worn(aws_resources):
    _put_refinement(version="pending-one")
    _put_refinement(version="rejected-one", status="rejected")

    assert _post("equip", "pending-one")["statusCode"] == 409
    assert _post("equip", "rejected-one")["statusCode"] == 409
    assert _post("equip", "nowhere")["statusCode"] == 404


def test_unequip_sends_it_to_the_backpack(aws_resources):
    _approved("a")
    _post("equip", "a", {"scope": "global", "slot": "sword"})

    result = _post("unequip", "a")

    assert result["statusCode"] == 200
    item = _refinement_item("a")
    assert item["equipped"] is False and "slot" not in item and item["status"] == "approved"
    assert "unequipped_at" in item


def test_unequipping_something_not_worn_is_a_conflict(aws_resources):
    _approved("a")

    assert _post("unequip", "a")["statusCode"] == 409
    assert _post("unequip", "missing")["statusCode"] == 404


def test_the_equipment_view_shows_slots_rings_and_the_backpack(aws_resources):
    _approved("armor")
    _approved("ring")
    _approved("bench")
    _put_refinement(version="pending")
    _post("equip", "armor", {"scope": "global", "slot": "shield"})
    _post("equip", "ring")
    _post("unequip", "ring")
    _post("equip", "ring")

    result = admin_api_handler.handler(_event("GET /equipment"), None)

    assert result["statusCode"] == 200
    view = json.loads(result["body"])
    assert view["armor"]["shield"]["version"] == "armor"
    assert view["armor"]["helmet"] is None
    assert [r["version"] for r in view["rings"]] == ["ring"]
    assert view["backpack_count"] == 0
    assert [i["version"] for i in view["legacy"]] == ["bench"]  # approved before equipment: still in use


# --- Gear: names, rarity and durability -------------------------------------------------


def _identity(version, rarity="rare", durability=17, max_durability=17, theme="Plain Speaking", hint="chest"):
    """Give a stored refinement a gear identity, as the weekly reflection does."""
    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    table.update_item(
        Key={"topic_id": "github-trending", "version": version},
        UpdateExpression=(
            "SET rarity = :r, durability = :d, max_durability = :m, theme = :t, slot_hint = :h"
        ),
        ExpressionAttributeValues={
            ":r": rarity,
            ":d": durability,
            ":m": max_durability,
            ":t": theme,
            ":h": hint,
        },
    )


def test_listing_shows_plain_numbers_and_the_gears_name(aws_resources):
    _put_refinement(version="v1")
    _identity("v1")

    result = admin_api_handler.handler(_event("GET /prompt-refinements"), None)

    (item,) = json.loads(result["body"])["refinements"]
    assert item["name"] == "Breastplate of Plain Speaking"  # the bear's suggested slot
    assert (item["rarity"], item["durability"], item["max_durability"]) == ("rare", 17, 17)


def test_approving_something_that_predates_gear_gives_it_a_rarity_and_full_durability(aws_resources):
    _put_refinement(version="v1")

    result = _post("approve", "v1")

    item = json.loads(result["body"])["approved"]["item"]
    assert item["name"] == "Ring of Github Trending Lore"  # worn as a ring, themed from its topic
    stored = _refinement_item("v1")
    low, high = {
        "common": (6, 10),
        "uncommon": (10, 15),
        "rare": (15, 20),
        "epic": (21, 30),
        "legendary": (40, 50),
    }[stored["rarity"]]
    assert low <= stored["max_durability"] <= high
    assert stored["durability"] == stored["max_durability"]


def test_an_identity_is_rolled_once_and_never_again(aws_resources):
    _put_refinement(version="v1", status="approved")
    _post("equip", "v1")
    first = _refinement_item("v1")
    _post("unequip", "v1")
    _post("equip", "v1")

    again = _refinement_item("v1")

    assert (again["rarity"], again["max_durability"]) == (first["rarity"], first["max_durability"])


def test_a_proposals_own_identity_is_kept_when_it_is_approved(aws_resources):
    _put_refinement(version="v1")
    _identity("v1", rarity="epic", durability=25, max_durability=25, theme="Sharper Sources", hint="shield")

    result = _post("approve", "v1", {"scope": "global", "slot": "shield"})

    item = json.loads(result["body"])["approved"]["item"]
    assert item == {
        "name": "Shield of Sharper Sources",
        "rarity": "epic",
        "durability": 25,
        "max_durability": 25,
    }


def test_the_bears_suggested_slot_is_taken_when_it_is_empty(aws_resources):
    _put_refinement(version="v1", status="approved")
    _put_refinement(version="v2", status="approved")
    _identity("v1", hint="shield")
    _identity("v2", hint="shield")

    _post("equip", "v1", {"scope": "global"})
    _post("equip", "v2", {"scope": "global"})

    assert _refinement_item("v1")["slot"] == "shield"  # the suggestion
    assert _refinement_item("v2")["slot"] == "helmet"  # shield is taken: the first empty one


def test_the_equipment_view_names_every_item_and_uses_plain_numbers(aws_resources):
    _put_refinement(version="v1", status="approved")
    _identity("v1", hint="boots")
    _post("equip", "v1", {"scope": "global", "slot": "boots"})

    view = json.loads(admin_api_handler.handler(_event("GET /equipment"), None)["body"])

    assert view["armor"]["boots"]["name"] == "Boots of Plain Speaking"
    assert view["armor"]["boots"]["durability"] == 17


def test_an_admin_can_bump_the_rarity_up(aws_resources):
    _put_refinement(version="v1")
    _identity("v1", rarity="common", durability=6, max_durability=8)

    result = _post("rarity", "v1", {"rarity": "epic"})

    assert result["statusCode"] == 200
    bumped = json.loads(result["body"])["bumped"]
    assert bumped["was"] == "common" and bumped["rarity"] == "epic"
    stored = _refinement_item("v1")
    assert 21 <= stored["max_durability"] <= 30
    assert stored["durability"] == 6 + (stored["max_durability"] - 8)  # the new room, not a repair
    assert stored["status"] == "pending"  # bumping does not approve it


def test_a_bump_with_no_rarity_goes_up_one_step(aws_resources):
    _put_refinement(version="v1", status="approved")
    _identity("v1", rarity="rare")

    assert json.loads(_post("rarity", "v1")["body"])["bumped"]["rarity"] == "epic"


def test_a_bump_can_not_go_down_stay_or_pass_legendary(aws_resources):
    _put_refinement(version="v1", status="approved")
    _identity("v1", rarity="rare")

    assert _post("rarity", "v1", {"rarity": "common"})["statusCode"] == 409
    assert _post("rarity", "v1", {"rarity": "rare"})["statusCode"] == 409
    assert _post("rarity", "v1", {"rarity": "mythic"})["statusCode"] == 400
    assert _refinement_item("v1")["rarity"] == "rare"
    _identity("v1", rarity="legendary", durability=44, max_durability=44)
    assert _post("rarity", "v1")["statusCode"] == 409


def test_a_rejected_or_missing_proposal_cannot_be_bumped(aws_resources):
    _put_refinement(version="v1", status="rejected")

    assert _post("rarity", "v1")["statusCode"] == 409
    assert _post("rarity", "nowhere")["statusCode"] == 404


def test_bumping_something_that_predates_gear_rolls_it_an_identity_first(aws_resources):
    _put_refinement(version="v1", status="approved")

    with patch("common.gear.roll_rarity", return_value="common"):
        result = _post("rarity", "v1", {"rarity": "legendary"})

    assert result["statusCode"] == 200
    assert json.loads(result["body"])["bumped"]["was"] == "common"
    assert _refinement_item("v1")["rarity"] == "legendary"


# --- Gear: wear, repair, and why something is in the backpack ------------------------------


def _set(version, **fields):
    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    names = {f"#{k}": k for k in fields}
    table.update_item(
        Key={"topic_id": "github-trending", "version": version},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


def test_a_repair_restores_durability_to_full(aws_resources):
    _put_refinement(version="v1", status="approved")
    _identity("v1", durability=3, max_durability=17)

    result = _post("repair", "v1")

    assert result["statusCode"] == 200
    repaired = json.loads(result["body"])["repaired"]
    assert (repaired["was"], repaired["durability"], repaired["max_durability"]) == (3, 17, 17)
    assert _refinement_item("v1")["durability"] == 17


def test_a_repair_can_restore_some_of_it_but_never_past_the_maximum(aws_resources):
    _put_refinement(version="v1", status="approved")
    _identity("v1", durability=3, max_durability=17)

    assert json.loads(_post("repair", "v1", {"amount": 4})["body"])["repaired"]["durability"] == 7
    assert json.loads(_post("repair", "v1", {"amount": 500})["body"])["repaired"]["durability"] == 17


def test_repairing_gear_that_is_already_full_is_a_conflict(aws_resources):
    _put_refinement(version="v1", status="approved")
    _identity("v1")

    assert _post("repair", "v1")["statusCode"] == 409


@pytest.mark.parametrize("amount", [0, -3, 1.5, "2", True])
def test_a_repair_amount_must_be_a_positive_whole_number(aws_resources, amount):
    _put_refinement(version="v1", status="approved")
    _identity("v1", durability=3)

    assert _post("repair", "v1", {"amount": amount})["statusCode"] == 400
    assert _refinement_item("v1")["durability"] == 3


def test_only_approved_gear_can_be_repaired(aws_resources):
    _put_refinement(version="v1")

    assert _post("repair", "v1")["statusCode"] == 409
    assert _post("repair", "nowhere")["statusCode"] == 404


def test_repairing_worn_out_gear_leaves_it_in_the_backpack_to_be_worn_again_by_choice(aws_resources):
    _put_refinement(version="v1", status="approved")
    _identity("v1", durability=0, max_durability=17)
    _set("v1", equipped=False, unequipped_reason="worn_out")

    _post("repair", "v1")

    stored = _refinement_item("v1")
    assert stored["durability"] == 17 and stored["equipped"] is False  # repaired, not re-equipped
    assert _post("equip", "v1")["statusCode"] == 200  # the admin puts it back on


def test_gear_that_is_worn_out_cannot_be_worn_until_it_is_repaired(aws_resources):
    _put_refinement(version="v1", status="approved")
    _identity("v1", durability=0, max_durability=17)

    result = _post("equip", "v1")

    assert result["statusCode"] == 409 and "repair" in json.loads(result["body"])["error"]
    assert "equipped" not in _refinement_item("v1")


def test_the_reason_something_is_in_the_backpack_is_recorded(aws_resources):
    for version in ("bench", "old", "new"):
        _put_refinement(version=version, status="approved")
    _post("equip", "bench")
    _post("unequip", "bench")
    _post("equip", "old", {"scope": "global", "slot": "helmet"})
    _post("equip", "new", {"scope": "global", "slot": "helmet"})  # pushes "old" out

    assert _refinement_item("bench")["unequipped_reason"] == "benched"
    assert _refinement_item("old")["unequipped_reason"] == "displaced"


def test_an_item_approved_with_no_room_is_parked_and_one_shelved_by_choice_is_not(aws_resources):
    for n in range(5):
        _put_refinement(version=f"r{n}")
        _post("approve", f"r{n}")
    _put_refinement(version="no-room")
    _put_refinement(version="chosen")

    _post("approve", "no-room")
    _post("approve", "chosen", {"scope": "backpack"})

    parked, shelved = _refinement_item("no-room"), _refinement_item("chosen")
    assert (parked["unequipped_reason"], parked["scope"]) == ("parked", "topic")
    assert shelved["unequipped_reason"] == "shelved"


# --- Numbers stored in DynamoDB come back as Decimal: no route may 500 on them ---------------


def test_a_topic_with_its_own_research_interval_can_be_listed_and_fetched(aws_resources):
    """Regression: GET /topics answered 500 ("Decimal is not JSON serializable") as soon as any topic held
    a research_interval_hours, which blocked `admin_cli topics list` and every `topics get`."""
    _put_topic({**TOPIC, "topic_id": "slow-topic", "research_interval_hours": 6})
    _put_topic()

    listed = admin_api_handler.handler(_event("GET /topics"), None)
    fetched = admin_api_handler.handler(
        _event("GET /topics/{topic_id}", path_params={"topic_id": "slow-topic"}), None
    )

    assert listed["statusCode"] == 200 and fetched["statusCode"] == 200
    by_id = {t["topic_id"]: t for t in json.loads(listed["body"])["topics"]}
    assert by_id["slow-topic"]["research_interval_hours"] == 6
    assert isinstance(by_id["slow-topic"]["research_interval_hours"], int)
    assert json.loads(fetched["body"])["research_interval_hours"] == 6


def test_decimals_are_written_as_whole_numbers_or_floats():
    payload = {
        "whole": Decimal("6"),
        "fraction": Decimal("0.25"),
        "big": Decimal("1000000"),
        "n": [Decimal("2")],
    }

    body = admin_api_handler._response(200, payload)["body"]

    assert json.loads(body) == {"whole": 6, "fraction": 0.25, "big": 1000000, "n": [2]}
    assert '"whole": 6,' in body and "6.0" not in body


def test_something_that_is_not_a_number_still_fails_loudly():
    with pytest.raises(TypeError):
        admin_api_handler._response(200, {"when": object()})
