"""list_recent_findings' optional `since` window (the daily cycle's article input)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

import common.dynamo as dynamo
from common.adapters.base import SEEN_KEY, Adapter

REGION = "ap-southeast-2"


@pytest.fixture
def findings_table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("FINDINGS_TABLE", "Findings")
    dynamo._dynamodb_resource = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
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
        table = boto3.resource("dynamodb", region_name=REGION).Table("Findings")
        for hour in range(10):
            table.put_item(
                Item={
                    "topic_id": "t",
                    "captured_at": f"2026-09-20T{hour:02d}:00:00+00:00",
                    "summary": f"finding {hour}",
                }
            )
        table.put_item(
            Item={"topic_id": "other", "captured_at": "2026-09-20T09:00:00+00:00", "summary": "x"}
        )
        yield table


def test_without_since_it_returns_the_newest_few(findings_table):
    found = dynamo.list_recent_findings("t")

    assert [f["summary"] for f in found] == [f"finding {h}" for h in (9, 8, 7, 6, 5)]


def test_since_returns_only_findings_captured_at_or_after_it_newest_first(findings_table):
    found = dynamo.list_recent_findings("t", limit=48, since="2026-09-20T06:00:00+00:00")

    assert [f["summary"] for f in found] == [f"finding {h}" for h in (9, 8, 7, 6)]


def test_since_still_honours_the_limit_and_the_topic(findings_table):
    found = dynamo.list_recent_findings("t", limit=2, since="2026-09-20T00:00:00+00:00")

    assert [f["summary"] for f in found] == ["finding 9", "finding 8"]
    assert dynamo.list_recent_findings("other", limit=48, since="2026-09-20T00:00:00+00:00")[0][
        "topic_id"
    ] == "other"


# --- the adapter contract behind "what counts as new" -----------------------


class _Keyed(Adapter):
    def fetch_state(self, topic_config):
        return {}

    def material_diff(self, old_state, new_state):
        return False, ""

    def source_refs(self, new_state):
        return []

    def item_keys(self, state):
        return set(state.get("keys", []))


def test_known_keys_is_empty_on_the_first_tick():
    assert _Keyed().known_keys(None) == set()


def test_known_keys_includes_items_reported_earlier_that_the_last_snapshot_no_longer_lists():
    old_state = {"keys": ["a"], SEEN_KEY: {"a": "2026-09-20", "gone": "2026-09-19"}}

    assert _Keyed().known_keys(old_state) == {"a", "gone"}


def test_known_keys_falls_back_to_the_items_of_a_snapshot_stored_before_the_seen_set():
    assert _Keyed().known_keys({"keys": ["a", "b"]}) == {"a", "b"}


def test_an_adapter_that_exposes_no_items_knows_nothing_and_keeps_its_own_diff():
    class _Plain(_Keyed):
        def item_keys(self, state):
            return Adapter.item_keys(self, state)

    assert _Plain().known_keys({"keys": ["a"]}) == set()


# --- delete_musings_for_article -----------------------------------------------


@pytest.fixture
def musings_table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MUSINGS_TABLE", "Musings")
    dynamo._dynamodb_resource = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="Musings",
            KeySchema=[{"AttributeName": "musing_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "musing_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = boto3.resource("dynamodb", region_name=REGION).Table("Musings")
        for musing_id, article_id in (("m1", "a1"), ("m2", "a1"), ("m3", "a2"), ("m4", None)):
            table.put_item(Item={"musing_id": musing_id, "article_id": article_id, "text": "t"})
        yield table


def test_delete_musings_for_article_removes_only_that_articles(musings_table):
    removed = dynamo.delete_musings_for_article("a1")

    assert removed == 2
    left = sorted(item["musing_id"] for item in musings_table.scan()["Items"])
    assert left == ["m3", "m4"]


def test_delete_musings_for_an_article_with_none_is_a_no_op(musings_table):
    assert dynamo.delete_musings_for_article("nope") == 0
    assert len(musings_table.scan()["Items"]) == 4
