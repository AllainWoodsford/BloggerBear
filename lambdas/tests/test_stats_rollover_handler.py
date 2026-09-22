"""Tests for stats_rollover_handler.py: copying StatsCurrent into a new StatsHistory row, then
clearing StatsCurrent for the next week.

One wrinkle worth knowing before reading the assertions below: stats_rollover_handler.handler is
itself decorated with common.lambda_timing.track_lambda_duration("stats_rollover") (like every
other pipeline handler), which records its own run time onto StatsCurrent *after* handler() returns
-- which is *after* _roll_over() has already cleared the row. So "current is cleared" in practice
means "last week's data is gone", not "the row is empty": a fresh row for the new week reappears
immediately, seeded with nothing but this run's own duration. That is correct, not a bug -- the
rollover job's own cost belongs to the week it actually ran in, not the one it just archived."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import stats_rollover_handler

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("STATS_CURRENT_TABLE", "StatsCurrent")
    monkeypatch.setenv("STATS_HISTORY_TABLE", "StatsHistory")
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture
def tables():
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="StatsCurrent",
            KeySchema=[{"AttributeName": "stats_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "stats_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="StatsHistory",
            KeySchema=[{"AttributeName": "week_start", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "week_start", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {
            "current": boto3.resource("dynamodb", region_name=REGION).Table("StatsCurrent"),
            "history": boto3.resource("dynamodb", region_name=REGION).Table("StatsHistory"),
        }


def _put_current(table, week_start="2026-09-15", **fields):
    table.put_item(Item={"stats_id": "current", "week_start": week_start, **fields})


# --- the normal case --------------------------------------------------------------------------


def test_a_populated_week_is_copied_into_history_and_current_is_cleared(tables):
    _put_current(tables["current"], musings_calls=5, feedback_given=3)

    result = stats_rollover_handler.handler({}, None)

    assert result == {"status": "rolled_over", "week_start": "2026-09-15"}
    history_row = tables["history"].get_item(Key={"week_start": "2026-09-15"})["Item"]
    assert history_row["musings_calls"] == 5 and history_row["feedback_given"] == 3
    assert "rolled_over_at" in history_row
    assert "stats_id" not in history_row  # StatsCurrent's own key, meaningless in StatsHistory
    # Last week's data is gone; only the rollover job's own self-timed duration remains, seeding
    # the new week's row (see the module docstring above).
    fresh = tables["current"].get_item(Key={"stats_id": "current"}).get("Item", {})
    assert "musings_calls" not in fresh and "feedback_given" not in fresh
    assert set(fresh) <= {"stats_id", "week_start", "lambda_ms_stats_rollover"}


def test_decimal_fields_like_cost_survive_the_copy_intact(tables):
    _put_current(tables["current"], musings_cost_aud=Decimal("3.14"))

    stats_rollover_handler.handler({}, None)

    row = tables["history"].get_item(Key={"week_start": "2026-09-15"})["Item"]
    assert row["musings_cost_aud"] == Decimal("3.14")


# --- nothing to roll over ---------------------------------------------------------------------


def test_an_empty_week_rolls_over_nothing(tables):
    result = stats_rollover_handler.handler({}, None)

    assert result == {"status": "nothing_to_roll_over"}
    assert tables["history"].scan()["Count"] == 0


def test_a_current_row_with_no_week_start_is_treated_as_nothing_to_roll_over(tables):
    tables["current"].put_item(Item={"stats_id": "current"})  # no week_start at all

    result = stats_rollover_handler.handler({}, None)

    assert result == {"status": "nothing_to_roll_over"}
    # An odd row with nothing useful on it is left alone rather than guessed about.
    assert "Item" in tables["current"].get_item(Key={"stats_id": "current"})


# --- a duplicate rollover never overwrites history ---------------------------------------------


def test_a_week_already_rolled_over_is_not_overwritten_but_current_is_still_cleared(tables):
    tables["history"].put_item(
        Item={"week_start": "2026-09-15", "musings_calls": 999, "rolled_over_at": "first-time"}
    )
    _put_current(tables["current"], musings_calls=5)  # a second, stale copy of the same week

    result = stats_rollover_handler.handler({}, None)

    assert result == {"status": "already_rolled_over", "week_start": "2026-09-15"}
    kept = tables["history"].get_item(Key={"week_start": "2026-09-15"})["Item"]
    assert kept["musings_calls"] == 999 and kept["rolled_over_at"] == "first-time"  # untouched
    fresh = tables["current"].get_item(Key={"stats_id": "current"}).get("Item", {})
    assert "musings_calls" not in fresh  # last week's stale data is gone either way


def test_a_different_weeks_history_is_unaffected_by_this_weeks_rollover(tables):
    tables["history"].put_item(Item={"week_start": "2026-09-08", "musings_calls": 1})
    _put_current(tables["current"], week_start="2026-09-15", musings_calls=2)

    stats_rollover_handler.handler({}, None)

    assert tables["history"].get_item(Key={"week_start": "2026-09-08"})["Item"]["musings_calls"] == 1
    assert tables["history"].get_item(Key={"week_start": "2026-09-15"})["Item"]["musings_calls"] == 2


# --- it never raises unhandled ------------------------------------------------------------------


def test_a_failure_reading_current_stats_is_reported_not_raised(tables):
    with patch("stats_rollover_handler.get_current_stats", side_effect=RuntimeError("dynamo down")):
        result = stats_rollover_handler.handler({}, None)

    assert result["status"] == "error" and "dynamo down" in result["error"]


def test_a_failure_writing_history_is_reported_not_raised(tables):
    _put_current(tables["current"], musings_calls=1)

    with patch("stats_rollover_handler.put_stats_history_row", side_effect=RuntimeError("boom")):
        result = stats_rollover_handler.handler({}, None)

    assert result["status"] == "error"
    # Current is left exactly as it was -- nothing was cleared on a failed rollover.
    assert tables["current"].get_item(Key={"stats_id": "current"})["Item"]["musings_calls"] == 1
