"""list_recent_findings' optional `since` window (the daily cycle's article input), and the one
rule for where that window starts (common/findings_window.py's `window_start`, which the daily
cycle, the assistant's topic_findings and the Admin API's findings route share)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

import common.dynamo as dynamo
from common import findings_window
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


@pytest.fixture
def topics_table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("TOPICS_TABLE", "Topics")
    dynamo._dynamodb_resource = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="Topics",
            KeySchema=[{"AttributeName": "topic_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "topic_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
        table.put_item(Item={"topic_id": "t", "name": "T", "adapter": "web_search"})
        yield table


def test_set_topic_last_article_at_changes_only_that_field(topics_table):
    dynamo.set_topic_last_article_at("t", "2026-09-21T09:00:00+00:00")

    item = topics_table.get_item(Key={"topic_id": "t"})["Item"]
    assert item == {
        "topic_id": "t",
        "name": "T",
        "adapter": "web_search",
        "last_article_at": "2026-09-21T09:00:00+00:00",
    }


def test_set_topic_last_article_at_does_not_create_a_missing_topic(topics_table):
    with pytest.raises(topics_table.meta.client.exceptions.ConditionalCheckFailedException):
        dynamo.set_topic_last_article_at("deleted", "2026-09-21T09:00:00+00:00")

    assert "Item" not in topics_table.get_item(Key={"topic_id": "deleted"})


def test_a_subjects_stamp_is_set_with_the_topics_and_the_other_subjects_are_left_alone(topics_table):
    dynamo.set_topic_last_article_at("t", "2026-09-21T09:00:00+00:00", subject_key="north")  # makes the map
    dynamo.set_topic_last_article_at("t", "2026-09-22T09:00:00+00:00", subject_key="south")
    dynamo.set_topic_last_article_at("t", "2026-09-23T09:00:00+00:00", subject_key="north")

    item = topics_table.get_item(Key={"topic_id": "t"})["Item"]
    assert item == {
        "topic_id": "t",
        "name": "T",
        "adapter": "web_search",
        "last_article_at": "2026-09-23T09:00:00+00:00",
        "last_article_by_subject": {
            "north": "2026-09-23T09:00:00+00:00",
            "south": "2026-09-22T09:00:00+00:00",
        },
    }
    # A plain stamp (a topic that does not rotate) leaves the map alone.
    dynamo.set_topic_last_article_at("t", "2026-09-24T09:00:00+00:00")
    item = topics_table.get_item(Key={"topic_id": "t"})["Item"]
    assert item["last_article_at"] == "2026-09-24T09:00:00+00:00"
    assert item["last_article_by_subject"]["north"] == "2026-09-23T09:00:00+00:00"


def test_a_subjects_stamp_does_not_create_a_missing_topic(topics_table):
    with pytest.raises(topics_table.meta.client.exceptions.ConditionalCheckFailedException):
        dynamo.set_topic_last_article_at("deleted", "2026-09-21T09:00:00+00:00", subject_key="north")

    assert "Item" not in topics_table.get_item(Key={"topic_id": "deleted"})


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


# --- window_start: the one rule for where a topic's findings window starts ---------------------

RUN = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def _hours_before(hours: int) -> str:
    return (RUN - timedelta(hours=hours)).isoformat()


def test_the_window_is_the_topics_own_or_the_default_day_when_there_is_no_previous_article():
    assert findings_window.window_start({}, RUN) == _hours_before(24)
    assert findings_window.window_start({"findings_window_hours": 192}, RUN) == _hours_before(192)
    assert findings_window.window_start({"findings_window_hours": Decimal("48")}, RUN) == _hours_before(48)
    # `hours` given wins over the stored window: the daily cycle passes the one it logged about.
    assert findings_window.window_start({"findings_window_hours": 192}, RUN, hours=6) == _hours_before(6)


def test_the_window_is_bounded_by_the_last_article_when_that_is_within_it():
    last = RUN - timedelta(hours=3)

    assert findings_window.window_start({"last_article_at": last.isoformat()}, RUN) == last.isoformat()
    older = RUN - timedelta(days=10)  # past a 192-hour (8-day) window
    topic = {"last_article_at": older.isoformat(), "findings_window_hours": 192}
    assert findings_window.window_start(topic, RUN) == _hours_before(192)


def test_force_ignores_the_last_article_but_keeps_the_window():
    last = RUN - timedelta(hours=3)
    topic = {"last_article_at": last.isoformat(), "findings_window_hours": 192}

    assert findings_window.window_start(topic, RUN, force=True) == _hours_before(192)
    only_last = {"last_article_at": last.isoformat()}
    assert findings_window.window_start(only_last, RUN, force=True) == _hours_before(24)


def test_a_naive_last_article_is_read_as_utc_and_an_unreadable_one_leaves_the_window_alone():
    naive = (RUN - timedelta(hours=2)).replace(tzinfo=None)

    assert findings_window.window_start({"last_article_at": naive.isoformat()}, RUN) == (
        naive.replace(tzinfo=UTC).isoformat()
    )
    for bad in ("not a date", 12345, "", None):
        assert findings_window.window_start({"last_article_at": bad}, RUN) == _hours_before(24)
    # A last article in the future bounds the window there: nothing before it is read.
    ahead = RUN + timedelta(hours=1)
    assert findings_window.window_start({"last_article_at": ahead.isoformat()}, RUN) == ahead.isoformat()


@pytest.mark.parametrize("bad", [0, -24, 337, "x", "", "192", 1.5, 192.0, True, Decimal("NaN"), [192]])
def test_an_invalid_stored_window_falls_back_to_the_default_day(bad):
    assert findings_window.window_start({"findings_window_hours": bad}, RUN) == _hours_before(24)


def test_it_is_pure_and_takes_no_topic_at_all():
    assert findings_window.window_start(None, RUN) == _hours_before(24)
    assert findings_window.window_start(None, RUN, subject_key="north") == _hours_before(24)


# --- window_start with a subject: a topic that rotates (common/day_subject.py) --------------------


def _rotating(**subject_stamps) -> dict:
    """A topic with a 96-hour window whose last article (on any subject) was 2 hours ago."""
    return {
        "findings_window_hours": Decimal("96"),  # as DynamoDB hands it back
        "last_article_at": _hours_before(2),
        "last_article_by_subject": {key: _hours_before(hours) for key, hours in subject_stamps.items()},
    }


def test_with_a_subject_its_own_last_article_bounds_the_window_not_the_topics():
    topic = _rotating(north=50, south=26)

    assert findings_window.window_start(topic, RUN, subject_key="north") == _hours_before(50)
    assert findings_window.window_start(topic, RUN, subject_key="south") == _hours_before(26)
    # Without a subject, the rule every other topic has: the topic-wide stamp.
    assert findings_window.window_start(topic, RUN) == _hours_before(2)


def test_a_subject_never_written_about_is_bounded_by_the_window_alone():
    topic = _rotating(north=50)

    assert findings_window.window_start(topic, RUN, subject_key="south") == _hours_before(96)
    no_map = {**topic, "last_article_by_subject": None}
    assert findings_window.window_start(no_map, RUN, subject_key="north") == _hours_before(96)
    # A map edited by hand into something else is no stamp at all.
    for odd in ("north", ["north"], 7):
        topic = {**_rotating(), "last_article_by_subject": odd}
        assert findings_window.window_start(topic, RUN, subject_key="north") == _hours_before(96)


def test_a_subject_stamp_older_than_the_window_leaves_the_window_and_force_ignores_both_stamps():
    topic = _rotating(north=200)
    assert findings_window.window_start(topic, RUN, subject_key="north") == _hours_before(96)

    topic = _rotating(north=50)
    assert findings_window.window_start(topic, RUN, force=True, subject_key="north") == _hours_before(96)
    assert findings_window.window_start(topic, RUN, force=True) == _hours_before(96)


def test_a_subject_stamp_is_read_as_the_topic_stamp_is_naive_as_utc_unreadable_as_none():
    naive = (RUN - timedelta(hours=5)).replace(tzinfo=None)
    topic = {"last_article_by_subject": {"north": naive.isoformat()}}

    as_utc = naive.replace(tzinfo=UTC).isoformat()
    assert findings_window.window_start(topic, RUN, subject_key="north") == as_utc
    for bad in ("not a date", 12345, "", None):
        topic = {"last_article_by_subject": {"north": bad}, "last_article_at": _hours_before(1)}
        assert findings_window.window_start(topic, RUN, subject_key="north") == _hours_before(24)


def test_last_article_at_reads_the_stamp_that_bounds_the_window():
    topic = _rotating(north=50)

    assert findings_window.last_article_at(topic) == _hours_before(2)
    assert findings_window.last_article_at(topic, "north") == _hours_before(50)
    assert findings_window.last_article_at(topic, "south") is None
    assert findings_window.last_article_at(None, "north") is None


# --- the paged readers behind topic_findings and the Admin API's findings route -------------------


def _epoch(**delta) -> int:
    return int((datetime.now(UTC) + timedelta(**delta)).timestamp())


def _set_expiry(table, hour: int, expires_at) -> None:
    table.update_item(
        Key={"topic_id": "t", "captured_at": f"2026-09-20T{hour:02d}:00:00+00:00"},
        UpdateExpression="SET expires_at = :e",
        ExpressionAttributeValues={":e": expires_at},
    )


def test_list_findings_pages_until_it_has_kept_enough_and_says_whether_more_remain(findings_table):
    for hour in (3, 4):  # two of the ten are expired, not yet deleted
        _set_expiry(findings_table, hour, _epoch(days=-1))
    for hour in (5, 6):
        _set_expiry(findings_table, hour, Decimal(_epoch(days=1)))
    now = _epoch()

    kept, expired, more = dynamo.list_findings("t", limit=4, unexpired_at=now)
    assert [f["summary"] for f in kept] == [f"finding {h}" for h in (9, 8, 7, 6)] and more is True
    assert expired == 0  # the newest four are alive: the expired ones were never reached

    kept, expired, more = dynamo.list_findings("t", limit=7, unexpired_at=now)
    assert [f["summary"] for f in kept] == [f"finding {h}" for h in (9, 8, 7, 6, 5, 2, 1)]
    assert expired == 2 and more is True

    kept, expired, more = dynamo.list_findings("t", limit=20, unexpired_at=now)
    assert len(kept) == 8 and expired == 2 and more is False
    # The daily run's reading: no expiry filter at all.
    kept, expired, more = dynamo.list_findings("t", limit=20, unexpired_at=None)
    assert len(kept) == 10 and expired == 0 and more is False
    # The page budget: one page of one item at a time reaches only so far.
    kept, expired, more = dynamo.list_findings("t", limit=1, unexpired_at=now, max_pages=1)
    assert [f["summary"] for f in kept] == ["finding 9"] and more is True
    kept, _, more = dynamo.list_findings("t", since="2026-09-20T08:00:00+00:00", limit=5, unexpired_at=now)
    assert [f["summary"] for f in kept] == ["finding 9", "finding 8"] and more is False


def test_count_findings_counts_the_unexpired_in_the_window_and_knows_when_it_stopped_short(findings_table):
    for hour in (0, 1):
        _set_expiry(findings_table, hour, _epoch(days=-2))
    now = _epoch()

    assert dynamo.count_findings("t", unexpired_at=now) == (8, True)
    assert dynamo.count_findings("t", unexpired_at=None) == (10, True)
    assert dynamo.count_findings("t", since="2026-09-20T07:00:00+00:00", unexpired_at=now) == (3, True)
    assert dynamo.count_findings("other", unexpired_at=now) == (1, True)
    assert dynamo.count_findings("nobody", unexpired_at=now) == (0, True)
    # Capped: at most `most` are counted, and the count is then a floor.
    assert dynamo.count_findings("t", unexpired_at=None, most=4) == (4, False)
    assert dynamo.count_findings("t", unexpired_at=None, most=10) == (10, True)
    assert dynamo.count_findings("t", unexpired_at=None, most=50) == (10, True)


def test_finding_is_expired_reads_only_a_number_at_or_before_now():
    assert dynamo.finding_is_expired({"expires_at": 10}, 10) is True
    assert dynamo.finding_is_expired({"expires_at": Decimal("9")}, 10) is True
    assert dynamo.finding_is_expired({"expires_at": 11}, 10) is False
    assert dynamo.finding_is_expired({"expires_at": 5}, None) is False
    for odd in ({}, {"expires_at": None}, {"expires_at": "5"}, {"expires_at": True}):
        assert dynamo.finding_is_expired(odd, 10) is False
