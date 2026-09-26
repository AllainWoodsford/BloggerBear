"""common/dynamo.py's list_recent_article_titles: a topic's own recently-published titles, fed
into daily_cycle_handler.py's ideation prompt so a story that stays trending for days doesn't get
written up again each day just because that day's numbers are technically new."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

import common.dynamo as dynamo

REGION = "ap-southeast-2"


@pytest.fixture
def articles_table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("ARTICLES_TABLE", "Articles")
    dynamo._dynamodb_resource = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="Articles",
            KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "article_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table("Articles")


def _put(table, article_id, topic_id, title, created_at, status="published"):
    table.put_item(
        Item={
            "article_id": article_id,
            "topic_id": topic_id,
            "title": title,
            "status": status,
            "created_at": created_at,
        }
    )


def test_returns_titles_most_recent_first(articles_table):
    _put(articles_table, "a1", "github-trending", "Older title", "2026-09-22T00:00:00+00:00")
    _put(articles_table, "a2", "github-trending", "Newer title", "2026-09-24T00:00:00+00:00")

    titles = dynamo.list_recent_article_titles("github-trending")

    assert titles == ["Newer title", "Older title"]


def test_respects_the_limit(articles_table):
    for day in range(1, 6):
        _put(articles_table, f"a{day}", "t", f"Title {day}", f"2026-09-{20 + day:02d}T00:00:00+00:00")

    titles = dynamo.list_recent_article_titles("t", limit=2)

    assert titles == ["Title 5", "Title 4"]


def test_ignores_other_topics_and_unpublished_articles(articles_table):
    _put(articles_table, "a1", "github-trending", "Right topic", "2026-09-24T00:00:00+00:00")
    _put(articles_table, "a2", "other-topic", "Wrong topic", "2026-09-24T00:00:00+00:00")
    _put(
        articles_table,
        "a3",
        "github-trending",
        "Still pending",
        "2026-09-25T00:00:00+00:00",
        status="pending_moderation",
    )

    titles = dynamo.list_recent_article_titles("github-trending")

    assert titles == ["Right topic"]


def test_returns_an_empty_list_for_a_topic_with_nothing_published(articles_table):
    assert dynamo.list_recent_article_titles("brand-new-topic") == []
