"""The hot reads in common/dynamo.py Query an index instead of Scanning the whole table (Scaling PR A).

Each test runs with Scan switched off on the tables involved, so a function that quietly fell back
to a Scan fails here, and checks it still returns what its callers expect.
"""
from __future__ import annotations

import boto3
import pytest
from moto import mock_aws
from table_schemas import create_table

import common.dynamo as dynamo

REGION = "ap-southeast-2"


class _NoScan:
    """A Table that refuses to Scan and passes everything else through."""

    def __init__(self, table):
        self._table = table

    def scan(self, **kwargs):
        raise AssertionError(f"{self._table.name} was scanned")

    def __getattr__(self, name):
        return getattr(self._table, name)


@pytest.fixture
def tables(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "ARTICLES_TABLE": "Articles",
        "MODERATION_QUEUE_TABLE": "ModerationQueue",
        "PROMPT_REFINEMENTS_TABLE": "PromptRefinements",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, keys in {
            "Articles": [("article_id", "HASH")],
            "ModerationQueue": [("queue_id", "HASH")],
            "PromptRefinements": [("topic_id", "HASH"), ("version", "RANGE")],
        }.items():
            create_table(
                client,
                TableName=name,
                KeySchema=[{"AttributeName": a, "KeyType": t} for a, t in keys],
                AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a, _ in keys],
                BillingMode="PAY_PER_REQUEST",
            )
        real_get_table = dynamo.get_table
        monkeypatch.setattr(dynamo, "get_table", lambda name: _NoScan(real_get_table(name)))
        yield


def _article(article_id, *, topic="gh", status="published", day=1, net_votes=None):
    dynamo.put_article(
        article_id=article_id,
        topic_id=topic,
        title=f"Title {article_id}",
        body_s3_key=f"articles/{article_id}.md",
        status=status,
        created_at=f"2026-09-{day:02d}T00:00:00+00:00",
        published_at=f"2026-09-{day:02d}T01:00:00+00:00" if status == "published" else None,
    )
    if net_votes is not None:
        dynamo.update_article_net_votes(article_id, net_votes)


def _queue(queue_id, article_id, *, topic="gh", status="pending", day=1):
    dynamo.put_moderation_item(
        queue_id=queue_id,
        article_id=article_id,
        topic_id=topic,
        reasons=["r"],
        status=status,
        created_at=f"2026-09-{day:02d}T00:00:00+00:00",
    )


def test_published_articles_are_listed_without_scanning_and_drafts_stay_out(tables):
    _article("a1")
    _article("a2", topic="crypto")
    _article("d1", status="pending_moderation")
    _article("r1", status="rejected")

    assert sorted(a["article_id"] for a in dynamo.list_published_articles()) == ["a1", "a2"]


def test_a_topics_published_articles_leave_out_other_topics_and_unpublished_ones(tables):
    _article("a1")
    _article("a2", day=2)
    _article("x1", topic="crypto")
    _article("d1", status="pending_moderation")

    assert sorted(a["article_id"] for a in dynamo.list_published_articles("gh")) == ["a1", "a2"]


def test_listed_articles_come_back_with_plain_numbers_in_their_lineage(tables):
    dynamo.put_article(
        article_id="a1",
        topic_id="gh",
        title="T",
        body_s3_key="k",
        status="published",
        created_at="2026-09-01T00:00:00+00:00",
        lineage={"cost_aud": 0.25, "total_input_tokens": 10},
    )

    (article,) = dynamo.list_published_articles()

    assert article["lineage"] == {"cost_aud": 0.25, "total_input_tokens": 10}


def test_recent_titles_are_the_newest_published_ones_up_to_the_limit(tables):
    for day in range(1, 8):
        _article(f"a{day}", day=day)
    _article("d9", status="pending_moderation", day=9)

    assert dynamo.list_recent_article_titles("gh", limit=3) == ["Title a7", "Title a6", "Title a5"]


def test_the_top_voted_article_is_found_without_scanning(tables):
    _article("a1", net_votes=2)
    _article("a2", net_votes=5)
    _article("a3", net_votes=-1)
    _article("d1", status="pending_moderation", net_votes=9)

    assert [a["article_id"] for a in dynamo.get_top_voted_articles("gh", limit=2)] == ["a2", "a1"]


def test_pending_moderation_is_listed_without_reading_the_queues_history(tables):
    _queue("q1", "a1")
    _queue("q2", "a2", status="approved")
    _queue("q3", "a3", status="rejected")
    _queue("q4", "a4", status="rewriting")

    assert [i["queue_id"] for i in dynamo.list_pending_moderation()] == ["q1"]
    assert [i["queue_id"] for i in dynamo.list_moderation_by_status("rewriting")] == ["q4"]


def test_a_topics_pending_count_counts_only_that_topics_pending_items(tables):
    _queue("q1", "a1")
    _queue("q2", "a2")
    _queue("q3", "a3", topic="crypto")
    _queue("q4", "a4", status="approved")

    assert dynamo.count_pending_moderation_for_topic("gh") == 2
    assert [i["queue_id"] for i in dynamo.list_pending_moderation_for_topic("crypto")] == ["q3"]


def test_an_articles_queue_item_is_its_newest_one_with_its_current_status(tables):
    _queue("old", "a1", status="rewritten", day=1)
    _queue("new", "a1", status="pending", day=3)
    _queue("other", "a2", day=5)
    dynamo.update_moderation_status("new", "approved")

    item = dynamo.get_moderation_item_by_article_id("a1")

    assert item["queue_id"] == "new"
    assert item["status"] == "approved"
    assert item["reasons"] == ["r"]  # the whole item, not just the index's keys


def test_an_article_with_no_queue_item_has_none(tables):
    _queue("q1", "a1")

    assert dynamo.get_moderation_item_by_article_id("a2") is None


def test_a_topics_prompt_refinements_are_read_by_its_key_not_by_scanning(tables):
    dynamo.put_prompt_refinement("gh", "2026-09-01", "why", "change", status="approved")
    dynamo.put_prompt_refinement("gh", "2026-09-02", "why", "change", status="pending")
    dynamo.put_prompt_refinement("crypto", "2026-09-03", "why", "change", status="approved")

    assert [i["version"] for i in dynamo.list_prompt_refinements(topic_id="gh")] == [
        "2026-09-01",
        "2026-09-02",
    ]
    assert dynamo.get_latest_approved_prompt_refinement("gh")["version"] == "2026-09-01"
