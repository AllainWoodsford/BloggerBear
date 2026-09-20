from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import musing_feedback_handler

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("FEEDBACK_TABLE", "Feedback")
    monkeypatch.setenv("MUSINGS_TABLE", "Musings")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-test-model")

    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture
def aws_resources(aws_env):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="Feedback",
            KeySchema=[
                {"AttributeName": "article_id", "KeyType": "HASH"},
                {"AttributeName": "feedback_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "article_id", "AttributeType": "S"},
                {"AttributeName": "feedback_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Musings",
            KeySchema=[{"AttributeName": "musing_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "musing_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _recent_iso(days_ago: float = 1) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _put_feedback(article_id: str, feedback_id: str, vote: str, days_ago: float = 1):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Feedback")
    table.put_item(
        Item={
            "article_id": article_id,
            "feedback_id": feedback_id,
            "vote": vote,
            "comment": None,
            "created_at": _recent_iso(days_ago=days_ago),
        }
    )


def _list_musings():
    table = boto3.resource("dynamodb", region_name=REGION).Table("Musings")
    return table.scan().get("Items", [])


def test_zero_feedback_still_creates_a_curious_musing(aws_resources):
    with patch(
        "musing_feedback_handler.generate_and_store_feedback_musing",
        wraps=musing_feedback_handler.generate_and_store_feedback_musing,
    ) as mock_generate, patch(
        "common.musings.invoke_claude", return_value="Quiet out there today."
    ):
        result = musing_feedback_handler.handler({}, None)

    mock_generate.assert_called_once_with(
        up_votes=0, down_votes=0, lookback_days=4, model_id="anthropic.claude-test-model"
    )
    assert result["status"] == "musing_created"
    assert result["mood"] == "curious"
    musings = _list_musings()
    assert len(musings) == 1
    assert musings[0]["kind"] == "feedback"
    assert musings[0]["article_id"] is None


def test_net_positive_feedback_is_pleased(aws_resources):
    _put_feedback("a1", "f1", "up")
    _put_feedback("a1", "f2", "up")
    _put_feedback("a2", "f3", "down")

    with patch("common.musings.invoke_claude", return_value="Feeling good about this one."):
        result = musing_feedback_handler.handler({}, None)

    assert result["mood"] == "pleased"
    assert result["up_votes"] == 2
    assert result["down_votes"] == 1


def test_net_negative_feedback_is_reflective(aws_resources):
    _put_feedback("a1", "f1", "down")
    _put_feedback("a1", "f2", "down")
    _put_feedback("a2", "f3", "up")

    with patch("common.musings.invoke_claude", return_value="Something to think about."):
        result = musing_feedback_handler.handler({}, None)

    assert result["mood"] == "reflective"


def test_tied_feedback_is_reflective(aws_resources):
    _put_feedback("a1", "f1", "up")
    _put_feedback("a1", "f2", "down")

    with patch("common.musings.invoke_claude", return_value="A mixed bag."):
        result = musing_feedback_handler.handler({}, None)

    assert result["mood"] == "reflective"


def test_feedback_outside_lookback_window_is_ignored(aws_resources):
    _put_feedback("a1", "f1", "up", days_ago=10)

    with patch("common.musings.invoke_claude", return_value="Quiet out there today."):
        result = musing_feedback_handler.handler({}, None)

    assert result["up_votes"] == 0
    assert result["down_votes"] == 0
    assert result["mood"] == "curious"


def test_generated_text_is_truncated_and_stored(aws_resources):
    overlong = "x" * 400
    with patch("common.musings.invoke_claude", return_value=overlong):
        musing_feedback_handler.handler({}, None)

    musings = _list_musings()
    assert len(musings[0]["text"]) == 280
    assert musings[0]["text"].endswith("…")


def test_unhandled_exception_returns_error_dict(aws_resources):
    with patch(
        "musing_feedback_handler.list_feedback_since", side_effect=RuntimeError("boom")
    ):
        result = musing_feedback_handler.handler({}, None)

    assert result["status"] == "error"
    assert "boom" in result["error"]
