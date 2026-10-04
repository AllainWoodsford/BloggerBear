"""A model that answers with nothing must not publish a musing with a mood and a link but no text
(common/musings.py): the article and feedback musings fall back to a plain, accurate one."""

from __future__ import annotations

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from common import musings

REGION = "ap-southeast-2"


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


def article_musing(reply, *, compliant=True, title="Staking Yields This Week"):
    with patch("common.musings.tracked_claude", return_value=reply):
        return musings.generate_and_store_article_musing(
            article_id="a1",
            topic_id="crypto",
            topic_name="Crypto",
            title=title,
            compliant=compliant,
            model_id="model",
        )


def feedback_musing(reply, *, up_votes, down_votes):
    with patch("common.musings.tracked_claude", return_value=reply):
        return musings.generate_and_store_feedback_musing(
            up_votes=up_votes, down_votes=down_votes, lookback_days=7, model_id="model"
        )


@pytest.mark.parametrize("reply", ["", "   \n", None])
@pytest.mark.parametrize("compliant, mood", [(True, "proud"), (False, "thoughtful")])
def test_an_empty_reply_gets_a_plain_article_musing_that_names_the_article(table, reply, compliant, mood):
    musing = article_musing(reply, compliant=compliant)

    assert musing["mood"] == mood and musing["article_id"] == "a1"
    assert "Staking Yields This Week" in musing["text"]
    assert table.get_item(Key={"musing_id": musing["musing_id"]})["Item"]["text"] == musing["text"]


def test_the_models_own_article_musing_is_kept(table):
    assert article_musing("Paws up, a new one is out!")["text"] == "Paws up, a new one is out!"


def test_a_plain_article_musing_with_a_very_long_title_still_fits(table):
    musing = article_musing("", title="A" * 400)

    assert 0 < len(musing["text"]) <= 280


def test_an_empty_reply_gets_a_plain_feedback_musing_with_the_real_numbers(table):
    musing = feedback_musing("", up_votes=5, down_votes=2)

    assert musing["mood"] == "pleased"
    assert "7 piece(s)" in musing["text"] and "5 up" in musing["text"] and "2 down" in musing["text"]


def test_an_empty_reply_in_a_quiet_week_gets_the_plain_curious_musing(table):
    musing = feedback_musing(None, up_votes=0, down_votes=0)

    assert musing["mood"] == "curious" and "no feedback yet" in musing["text"]


def test_the_models_own_feedback_musing_is_kept(table):
    musing = feedback_musing("Seven notes this week!", up_votes=5, down_votes=2)

    assert musing["text"] == "Seven notes this week!"
