"""dynamo.list_articles_for_topic: the operator's assistant reads a topic's recent articles, in any
status, through the Articles table's topic index and nothing else.

Held here, against moto with the index as infra/modules/app-data/main.tf defines it
(table_schemas.create_table mirrors it; test_terraform_wiring.py holds the two together): the read
is a Query on `by_topic_created_at` with `created_at` in the key condition and no filter, never a
Scan; it pages until it has what it needs; it stops at ARTICLES_FOR_TOPIC_MAX whatever the caller
asks; it reads only ARTICLES_FOR_TOPIC_FIELDS; and the items come back newest first with plain
numbers in their lineage.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws
from table_schemas import INDEXES, create_table

import common.dynamo as dynamo

REGION = "ap-southeast-2"
PAGE = 7  # a small page, so a read of a few dozen articles pages several times


class _Watched:
    """A Table that refuses to Scan, records every Query's arguments, and asks DynamoDB for small
    pages so pagination is exercised for real."""

    def __init__(self, table):
        self._table = table
        self.queries: list[dict] = []

    def scan(self, **kwargs):
        raise AssertionError(f"{self._table.name} was scanned")

    def query(self, **kwargs):
        self.queries.append(dict(kwargs))
        return self._table.query(**kwargs, Limit=PAGE)

    def __getattr__(self, name):
        return getattr(self._table, name)


@pytest.fixture
def articles(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "ARTICLES_TABLE": "Articles",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    with mock_aws():
        create_table(
            boto3.client("dynamodb", region_name=REGION),
            TableName="Articles",
            KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "article_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        real_get_table = dynamo.get_table
        watched = _Watched(real_get_table("Articles"))
        monkeypatch.setattr(dynamo, "get_table", lambda name: watched)
        yield watched
    dynamo._dynamodb_resource = None


def _article(article_id, *, topic="rail-sydney", status="pending_moderation", day=1, hour=0, captured=None):
    created = f"2026-10-{day:02d}T{hour:02d}:00:00+00:00"
    lineage = None
    if captured is not None:
        lineage = {
            "research": {
                "calls": [
                    {"stage": "research", "captured_at": captured, "input_tokens": 10, "output_tokens": 2}
                ],
                "cost_aud": 0.001,
            },
            "total_cost_aud": 0.001,
        }
    dynamo.put_article(
        article_id=article_id,
        topic_id=topic,
        title=f"Title {article_id}",
        body_s3_key=f"articles/{article_id}.md",
        status=status,
        created_at=created,
        published_at=created if status == "published" else None,
        published_by="humans" if status == "published" else None,
        lineage=lineage,
    )


def test_the_index_the_read_uses_is_the_one_terraform_defines():
    assert dynamo.ARTICLES_BY_TOPIC_INDEX == "by_topic_created_at"
    assert ("by_topic_created_at", "topic_id", "created_at", "ALL") in INDEXES["Articles"]


def test_it_queries_the_topic_index_by_key_with_no_filter_and_never_scans(articles):
    _article("a1", day=3, captured="2026-10-03T00:00:00+00:00")
    _article("a2", day=5, status="published", captured="2026-10-05T00:00:00+00:00")
    _article("old", day=1)  # before `since`
    _article("other", topic="rail-sydney-demo", day=4)  # another topic, never read

    found = dynamo.list_articles_for_topic("rail-sydney", "2026-10-02T00:00:00+00:00")

    assert [item["article_id"] for item in found] == ["a2", "a1"]  # newest first
    assert found[0]["status"] == "published" and found[0]["published_by"] == "humans"
    # Plain numbers in the lineage, as every other article read gives them.
    assert found[0]["lineage"]["research"]["calls"][0]["captured_at"] == "2026-10-05T00:00:00+00:00"
    cost = found[0]["lineage"]["research"]["cost_aud"]
    assert cost == 0.001 and isinstance(cost, float)
    assert found[1]["lineage"]["research"]["calls"][0]["input_tokens"] == 10
    for kwargs in articles.queries:
        assert kwargs["IndexName"] == "by_topic_created_at"
        assert "FilterExpression" not in kwargs
        assert kwargs["ScanIndexForward"] is False
        # The key condition holds the topic and the time; nothing else is read.
        condition = kwargs["KeyConditionExpression"]
        assert condition.expression_operator == "AND"
        assert {c.expression_operator for c in condition._values} == {"=", ">="}


def test_it_pages_until_it_has_the_topics_articles(articles):
    for n in range(20):
        _article(f"a{n:02d}", day=1 + n % 9, hour=n)
    found = dynamo.list_articles_for_topic("rail-sydney", "2026-10-01T00:00:00+00:00")

    assert len(found) == 20
    assert len(articles.queries) >= 3  # paged, with PAGE items a page
    assert sum(1 for kwargs in articles.queries if "ExclusiveStartKey" in kwargs) == len(articles.queries) - 1
    created = [item["created_at"] for item in found]
    assert created == sorted(created, reverse=True)


def test_it_stops_at_the_cap(articles):
    for n in range(dynamo.ARTICLES_FOR_TOPIC_MAX + 15):
        _article(f"a{n:03d}", day=1 + n % 28, hour=n % 24)
    found = dynamo.list_articles_for_topic("rail-sydney", "2026-10-01T00:00:00+00:00")

    assert len(found) == dynamo.ARTICLES_FOR_TOPIC_MAX == 200
    # It stopped reading once it had them: the pages that hold 200, not every page of the 215.
    assert len(articles.queries) == -(-dynamo.ARTICLES_FOR_TOPIC_MAX // PAGE)
    # The newest 200: nothing older than the oldest kept was read in preference to a newer one.
    newest = sorted((f"2026-10-{1 + n % 28:02d}T{n % 24:02d}:00:00+00:00" for n in range(215)), reverse=True)
    assert [item["created_at"] for item in found] == newest[:200]
    # A smaller cap is honoured too, and never less than one.
    assert len(dynamo.list_articles_for_topic("rail-sydney", "2026-10-01T00:00:00+00:00", limit=3)) == 3
    assert len(dynamo.list_articles_for_topic("rail-sydney", "2026-10-01T00:00:00+00:00", limit=0)) == 1
    # A larger one is not: the ceiling holds whatever the caller asks for.
    before = len(articles.queries)
    found = dynamo.list_articles_for_topic("rail-sydney", "2026-10-01T00:00:00+00:00", limit=5000)
    assert len(found) == dynamo.ARTICLES_FOR_TOPIC_MAX
    assert len(articles.queries) - before == -(-dynamo.ARTICLES_FOR_TOPIC_MAX // PAGE)


def test_it_reads_only_the_fields_the_assistants_join_uses(articles):
    """One ProjectionExpression on the Query: the article's id, status, who published it and when,
    and its lineage (the Findings it was drafted from). Never the title, the body's key or anything
    else of an article, so a topic's 200 articles stay a short read."""
    _article("a1", day=3, status="published", captured="2026-10-03T00:00:00+00:00")
    (found,) = dynamo.list_articles_for_topic("rail-sydney", "2026-10-02T00:00:00+00:00")

    assert set(found) == set(dynamo.ARTICLES_FOR_TOPIC_FIELDS) == {
        "article_id", "status", "published_by", "created_at", "published_at", "lineage",
    }  # fmt: skip
    assert found["lineage"]["research"]["calls"][0]["captured_at"] == "2026-10-03T00:00:00+00:00"
    for kwargs in articles.queries:
        names = kwargs["ExpressionAttributeNames"]
        said = [names[each.strip()] for each in kwargs["ProjectionExpression"].split(",")]
        assert said == list(dynamo.ARTICLES_FOR_TOPIC_FIELDS)


def test_a_topic_with_nothing_since_gives_an_empty_list_from_one_query(articles):
    _article("a1", day=1)
    assert dynamo.list_articles_for_topic("rail-sydney", "2026-10-09T00:00:00+00:00") == []
    assert dynamo.list_articles_for_topic("nobody", "2026-10-01T00:00:00+00:00") == []
    assert len(articles.queries) == 2
