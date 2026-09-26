"""Cleanup PR: the DynamoDB-layer half of every item that now self-clears via TTL --
common/dynamo.py's put_candidate_idea (always) and _expires_in/CLEANUP_TTL_DAYS (the shared
clock every one of these uses). update_moderation_status/update_prompt_refinement_status's own
conditional expires_at (only on "rejected") is covered where they're actually exercised,
lambdas/tests/test_admin_api_handler.py; put_failed_execution's is covered in
lambdas/tests/test_dlq_handler.py. This file is what's left with no other natural home: the raw
dynamo.py behaviour for CandidateIdeas, and the shared _expires_in helper itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

import common.dynamo as dynamo

REGION = "ap-southeast-2"


@pytest.fixture
def candidate_ideas_table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("CANDIDATE_IDEAS_TABLE", "CandidateIdeas")
    dynamo._dynamodb_resource = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
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
        yield boto3.resource("dynamodb", region_name=REGION).Table("CandidateIdeas")


def test_put_candidate_idea_sets_an_expiry_about_a_week_out(candidate_ideas_table):
    dynamo.put_candidate_idea("topic-a", "2026-09-21T00:00:00+00:00", "an angle")

    item = candidate_ideas_table.get_item(
        Key={"topic_id": "topic-a", "created_at": "2026-09-21T00:00:00+00:00"}
    )["Item"]
    expected = datetime.now(UTC) + timedelta(days=dynamo.CLEANUP_TTL_DAYS)
    assert abs(int(item["expires_at"]) - expected.timestamp()) < 5  # within a few seconds of "now"


def test_put_candidate_idea_still_returns_the_item_it_wrote(candidate_ideas_table):
    written = dynamo.put_candidate_idea("topic-a", "2026-09-21T00:00:00+00:00", "an angle")

    assert written["angle"] == "an angle" and written["status"] == "considered"
    assert "expires_at" in written  # the caller sees exactly what was stored, not a partial view


def test_expires_in_is_days_from_now_in_epoch_seconds():
    before = datetime.now(UTC) + timedelta(days=7)

    result = dynamo._expires_in(7)

    assert abs(result - before.timestamp()) < 5
