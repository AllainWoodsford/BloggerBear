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
    # The state machine's NameTheRun state puts the execution's ARN in the state, so the Catch's
    # message carries it, and the raw message keeps it.
    body = json.dumps(
        {
            "topic_id": "github-trending",
            "execution": {"id": "arn:aws:states:ap-southeast-2:123456789012:execution:daily-cycle:run-1"},
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
    # Cleanup PR: common/dynamo.py's put_failed_execution sets expires_at so this self-clears via
    # TTL after CLEANUP_TTL_DAYS -- a bounded operational glance-back, not a permanent record.
    assert "expires_at" in items[0]


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


def test_a_queue_job_is_worded_and_recorded_as_its_job_not_as_a_daily_cycle(failed_executions_table, capsys):
    """A research job that reached work-dlq.fifo: the dispatcher's message itself, with no error (it
    timed out or crashed three times). The line names the job, not a daily cycle, and so does the
    record's kind: pipeline_health reads a record with no kind as a daily run that ran out of
    retries."""
    body = json.dumps(
        {
            "topic_id": "github-trending",
            "job": "research",
            "force": False,
            "due_checked_at": "2026-10-24T22:05:30+00:00",
            "dispatched_at": "2026-10-24T22:05:30+00:00",
        }
    )

    result = dlq_handler.handler(_sqs_event(body), None)

    assert result == {"status": "ok", "processed": 1}
    out = capsys.readouterr().out
    assert "dlq_handler: research job for topic_id=github-trending failed" in out
    assert "daily_cycle" not in out
    (item,) = boto3.resource("dynamodb", region_name=REGION).Table("FailedExecutions").scan()["Items"]
    assert item["topic_id"] == "github-trending" and item["error"] is None and item["raw_message"] == body
    assert item["kind"] == "research_job"


def test_a_daily_cycle_failure_is_still_worded_and_recorded_as_one(failed_executions_table, capsys):
    body = json.dumps({"topic_id": "topic-a", "error": {"Error": "States.Timeout"}})

    dlq_handler.handler(_sqs_event(body), None)

    assert "dlq_handler: daily_cycle failed for topic_id=topic-a: {'Error': 'States.Timeout'}" in (
        capsys.readouterr().out
    )
    (item,) = boto3.resource("dynamodb", region_name=REGION).Table("FailedExecutions").scan()["Items"]
    assert "kind" not in item  # a daily cycle's record has none


@pytest.mark.parametrize("job", ["Research\nevidence", 7, "", "x" * 41])
def test_a_job_that_is_not_a_plain_code_is_named_unknown(failed_executions_table, capsys, job):
    dlq_handler.handler(_sqs_event(json.dumps({"topic_id": "t", "job": job})), None)

    assert "dlq_handler: unknown job for topic_id=t failed" in capsys.readouterr().out
    (item,) = boto3.resource("dynamodb", region_name=REGION).Table("FailedExecutions").scan()["Items"]
    assert item["kind"] == "unknown_job"  # still not a daily cycle, and never the body's own text
