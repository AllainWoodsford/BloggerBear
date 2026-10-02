"""A shocked musing when a draft is turned away at moderation (common/musings.py): the topic's name
only -- never the article's title or id, since a rejected article isn't public."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from common import musings

REGION = "ap-southeast-2"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MUSINGS_TABLE", "Musings")
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture
def table():
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="Musings",
            KeySchema=[{"AttributeName": "musing_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "musing_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table("Musings")


def react(reply, topic_name="Finance, Crypto & Investing"):
    """Post a rejection musing, with the model answering `reply` (or raising it, if an exception)."""
    fake = patch(
        "common.musings.tracked_claude",
        side_effect=reply if isinstance(reply, Exception) else None,
        return_value=None if isinstance(reply, Exception) else reply,
    )
    with fake as mock:
        musing = musings.generate_and_store_rejection_musing(
            topic_id="finance-crypto-investing", topic_name=topic_name, model_id="model"
        )
        return musing, mock


def test_a_rejection_is_a_shocked_musing_with_the_topic_and_no_article(table):
    text = "Whoa! A Finance, Crypto & Investing draft of mine got turned away. Back to the den!"

    musing, _ = react(text)

    assert musing["kind"] == "rejection" and musing["mood"] == "shocked" and musing["text"] == text
    assert musing["topic_id"] == "finance-crypto-investing"
    assert musing["article_id"] is None  # no link to an article that isn't public
    stored = table.get_item(Key={"musing_id": musing["musing_id"]})["Item"]
    assert stored["kind"] == "rejection" and stored["mood"] == "shocked"


def test_the_model_is_given_the_topic_and_nothing_about_the_draft(table):
    _, mock = react("Whoa, Finance, Crypto & Investing!")

    prompt = mock.call_args.args[1]
    assert '"Finance, Crypto & Investing"' in prompt
    assert "Say nothing about what the draft said" in prompt
    assert "title" not in prompt.lower()


@pytest.mark.parametrize(
    "reply",
    [
        RuntimeError("bedrock down"),
        "Whoa, a draft got turned away!",  # doesn't name the topic
        "Whoa, Finance, Crypto & Investing! See https://example.com",  # fails the comment screen
        "",
    ],
)
def test_an_unusable_reply_gets_the_plain_accurate_post(table, reply):
    musing, _ = react(reply)

    assert musing["text"] == (
        "Whoa! One of my Finance, Crypto & Investing drafts didn't make it past review. "
        "Back to the den to sniff out something better!"
    )
    assert musing["mood"] == "shocked"


def test_shocked_is_one_of_the_moods_and_has_a_bear():
    assert "shocked" in musings.MOODS
    svg = (FRONTEND / "bears" / "shocked.svg").read_text(encoding="utf-8")
    assert svg.startswith("<svg") and 'viewBox="0 0 64 64"' in svg
