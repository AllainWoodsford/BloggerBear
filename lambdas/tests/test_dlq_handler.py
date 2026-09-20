from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

import dlq_handler

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("FAILED_EXECUTIONS_TABLE", "FailedExecutions")

    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture
def failed_executions_table(aws_env):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="FailedExecutions",
            KeySchema=[{"AttributeName": "failure_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "failure_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _sqs_event(*bodies: str) -> dict:
    return {"Records": [{"body": body} for body in bodies]}


def test_records_a_failed_execution(failed_executions_table):
    body = json.dumps(
        {
            "topic_id": "github-trending",
            "error": {"Error": "States.TaskFailed", "Cause": "synthetic test message"},
        }
    )
    result = dlq_handler.handler(_sqs_event(body), None)
    assert result == {"status": "ok", "processed": 1}

    table = boto3.resource("dynamodb", region_name=REGION).Table("FailedExecutions")
    items = table.scan()["Items"]
    assert len(items) == 1
    assert items[0]["topic_id"] == "github-trending"
    assert items[0]["error"] == {"Error": "States.TaskFailed", "Cause": "synthetic test message"}
    assert items[0]["raw_message"] == body


def test_processes_multiple_records_in_one_batch(failed_executions_table):
    body_1 = json.dumps({"topic_id": "topic-a", "error": {"Error": "States.Timeout"}})
    body_2 = json.dumps({"topic_id": "topic-b", "error": {"Error": "States.TaskFailed"}})

    result = dlq_handler.handler(_sqs_event(body_1, body_2), None)
    assert result == {"status": "ok", "processed": 2}

    table = boto3.resource("dynamodb", region_name=REGION).Table("FailedExecutions")
    topic_ids = {item["topic_id"] for item in table.scan()["Items"]}
    assert topic_ids == {"topic-a", "topic-b"}


def test_malformed_message_body_still_records_raw_message(failed_executions_table):
    result = dlq_handler.handler(_sqs_event("not valid json"), None)
    assert result == {"status": "ok", "processed": 1}

    table = boto3.resource("dynamodb", region_name=REGION).Table("FailedExecutions")
    items = table.scan()["Items"]
    assert len(items) == 1
    assert items[0]["topic_id"] is None
    assert items[0]["error"] is None
    assert items[0]["raw_message"] == "not valid json"


def test_no_records_processes_nothing(failed_executions_table):
    result = dlq_handler.handler({"Records": []}, None)
    assert result == {"status": "ok", "processed": 0}
