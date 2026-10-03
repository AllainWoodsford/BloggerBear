"""Sharded hot counters (Scaling PR B): the week's Stats row and article view counts.

Writes spread over several items; every reader still sees one total, and whatever was counted on
the single item before sharding is still part of it.
"""
from __future__ import annotations

import itertools
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

import common.dynamo as dynamo
import stats_rollover_handler
from common.stats_tracking import public_view

REGION = "ap-southeast-2"


@pytest.fixture
def tables(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "STATS_CURRENT_TABLE": "StatsCurrent",
        "STATS_HISTORY_TABLE": "StatsHistory",
        "ARTICLES_TABLE": "Articles",
        "VIEW_COUNTS_TABLE": "ViewCounts",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, key in (
            ("StatsCurrent", "stats_id"),
            ("StatsHistory", "week_start"),
            ("Articles", "article_id"),
            ("ViewCounts", "counter_id"),
        ):
            client.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        resource = boto3.resource("dynamodb", region_name=REGION)
        names = ("StatsCurrent", "StatsHistory", "Articles", "ViewCounts")
        yield {name: resource.Table(name) for name in names}


@pytest.fixture
def round_robin(monkeypatch):
    """Shards picked in turn instead of at random, so a test knows writes really spread out."""
    counter = itertools.count()
    monkeypatch.setattr(dynamo.secrets, "randbelow", lambda n: next(counter) % n)


def _ids(table, key):
    return sorted(item[key] for item in table.scan()["Items"])


# --- the week's Stats row ----------------------------------------------------------------------


def test_increments_land_on_shards_never_on_the_base_row(tables, round_robin):
    for _ in range(3):
        dynamo.increment_current_stats({"musings_calls": 1}, "2026-09-15")

    assert _ids(tables["StatsCurrent"], "stats_id") == ["current#0", "current#1", "current#2"]


def test_every_increment_is_counted_once_however_the_shards_fall(tables):
    for _ in range(40):
        dynamo.increment_current_stats({"musings_calls": 1, "musings_cost_aud": Decimal("0.5")}, "2026-09-15")

    row = dynamo.get_current_stats()

    assert row["musings_calls"] == 40
    assert row["musings_cost_aud"] == Decimal("20.0")
    assert row["week_start"] == "2026-09-15"
    assert row["stats_id"] == "current"


def test_what_the_single_row_counted_before_sharding_is_still_in_the_total(tables, round_robin):
    tables["StatsCurrent"].put_item(
        Item={"stats_id": "current", "week_start": "2026-09-15", "feedback_given": 7, "loot_drops": 1}
    )
    dynamo.increment_current_stats({"feedback_given": 1}, "2026-09-15")
    dynamo.increment_current_stats({"feedback_given": 2}, "2026-09-15")

    row = dynamo.get_current_stats()

    assert row["feedback_given"] == 10
    assert row["loot_drops"] == 1


def test_snapshots_stay_on_the_base_row_and_are_never_summed(tables, round_robin):
    dynamo.set_current_stats_fields(
        {"api_gateway_cost_usd_30d": Decimal("1.25"), "waf_cost_month": "2026-10"}, "2026-09-15"
    )
    dynamo.set_current_stats_fields({"api_gateway_cost_usd_30d": Decimal("1.50")}, "2026-09-15")
    dynamo.increment_current_stats({"musings_calls": 1}, "2026-09-15")

    row = dynamo.get_current_stats()

    assert row["api_gateway_cost_usd_30d"] == Decimal("1.50")
    assert row["waf_cost_month"] == "2026-10"
    assert tables["StatsCurrent"].get_item(Key={"stats_id": "current"})["Item"].get("musings_calls") is None


def test_the_week_is_the_earliest_any_row_started(tables, round_robin):
    dynamo.increment_current_stats({"musings_calls": 1}, "2026-09-15")
    dynamo.increment_current_stats({"musings_calls": 1}, "2026-09-22")  # a write after Monday's turn

    assert dynamo.get_current_stats()["week_start"] == "2026-09-15"


def test_an_empty_week_reads_as_the_same_empty_shell_as_before(tables):
    assert dynamo.get_current_stats() == {"stats_id": "current"}


def test_clearing_the_week_clears_the_base_row_and_every_shard(tables):
    tables["StatsCurrent"].put_item(Item={"stats_id": "current", "week_start": "2026-09-15"})
    for _ in range(20):
        dynamo.increment_current_stats({"musings_calls": 1}, "2026-09-15")

    dynamo.delete_current_stats()

    assert tables["StatsCurrent"].scan()["Items"] == []


def test_the_stats_page_shows_the_same_figures_whether_counted_on_one_row_or_many(tables):
    figures = {"musings_calls": 6, "musings_input_tokens": 600, "feedback_given": 4, "loot_drops": 2}
    tables["StatsCurrent"].put_item(Item={"stats_id": "current", "week_start": "2026-09-15", **figures})
    single = public_view(dynamo.get_current_stats())

    dynamo.delete_current_stats()
    for key, value in figures.items():
        for _ in range(value):
            dynamo.increment_current_stats({key: 1}, "2026-09-15")
    sharded = public_view(dynamo.get_current_stats())

    assert sharded == single


def test_the_rollover_copies_and_totals_the_summed_week_once(tables, round_robin):
    tables["StatsCurrent"].put_item(
        Item={"stats_id": "current", "week_start": "2026-09-15", "musings_calls": 2}
    )
    for _ in range(5):
        dynamo.increment_current_stats({"musings_calls": 1}, "2026-09-15")

    assert stats_rollover_handler.handler({}, None)["status"] == "rolled_over"

    assert tables["StatsHistory"].get_item(Key={"week_start": "2026-09-15"})["Item"]["musings_calls"] == 7
    assert dynamo.get_stats_totals()["musings_calls"] == 7
    # Nothing of that week is left behind (the rollover's own run time starts the new week).
    assert all(i.get("week_start") != "2026-09-15" for i in tables["StatsCurrent"].scan()["Items"])


# --- article view counts -----------------------------------------------------------------------


def _article(view_count=None):
    item = {"article_id": "a1", "topic_id": "t", "title": "T", "status": "published"}
    if view_count is not None:
        item["view_count"] = view_count
    return item


def test_views_spread_over_counter_items_and_never_rewrite_the_article(tables, round_robin):
    tables["Articles"].put_item(Item=_article(view_count=5))

    totals = [dynamo.increment_view_count("a1") for _ in range(6)]

    assert totals == [6, 7, 8, 9, 10, 11]
    assert _ids(tables["ViewCounts"], "counter_id") == [f"a1#{n}" for n in range(dynamo.VIEW_COUNT_SHARDS)]
    assert tables["Articles"].get_item(Key={"article_id": "a1"})["Item"]["view_count"] == 5


def test_an_article_with_no_earlier_count_starts_from_one(tables):
    tables["Articles"].put_item(Item=_article())

    assert dynamo.increment_view_count("a1") == 1
    assert dynamo.increment_view_count("a1", stored_view_count=0) == 2


def test_the_total_is_the_earlier_count_plus_every_shard(tables):
    tables["Articles"].put_item(Item=_article(view_count=100))
    for _ in range(25):
        dynamo.increment_view_count("a1", stored_view_count=100)

    assert dynamo.get_view_count("a1") == 125
    assert dynamo.get_view_count("a1", stored_view_count=100) == 125


def test_one_articles_views_never_count_towards_another(tables):
    tables["Articles"].put_item(Item=_article())
    dynamo.increment_view_count("a1", stored_view_count=0)

    assert dynamo.get_view_count("a2", stored_view_count=0) == 0
