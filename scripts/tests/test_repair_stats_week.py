"""Tests for scripts/repair_stats_week.py: mending StatsHistory after a week the rollover missed.

The table is moto's. The case is production's own: the rollover did not run on 2026-09-28, and
the one on 2026-10-05 filed two weeks of counters under 2026-09-21.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import boto3
import pytest
from moto import mock_aws

import repair_stats_week as repair

TABLE = "bloggerbear-production-stats-history"
REGION = "eu-west-1"  # any region: the script names none of its own
TODAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
MERGED = {
    "week_start": "2026-09-21",
    "rolled_over_at": "2026-10-05T09:15:07.183436+00:00",
    "articles_calls": 311,
    "aws_bill_week_complete": True,
}


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        made = resource.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "week_start", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "week_start", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        made.put_item(Item=dict(MERGED))
        made.put_item(Item={"week_start": "all-time", "articles_calls": 311})
        yield made


def row(table, week):
    return table.get_item(Key={"week_start": week}).get("Item")


def test_the_plan_names_both_weeks_and_reads_the_day_from_the_rollover(table):
    assert repair.plan(table, "2026-09-28", TODAY) == {
        "missing_week": "2026-09-28",
        "earlier_week": "2026-09-21",
        "covers_through": "2026-10-05",
    }
    assert row(table, "2026-09-28") is None  # planning writes nothing


def test_applying_makes_a_placeholder_and_notes_the_earlier_week(table):
    repair.apply(table, repair.plan(table, "2026-09-28", TODAY), NOW)

    placeholder = row(table, "2026-09-28")
    assert placeholder["counters_in_week"] == "2026-09-21"
    assert placeholder["backfilled_at"] == NOW.isoformat()
    # No counter, and nothing that says its bill is known: the cost poll fills that in.
    assert set(placeholder) == {"week_start", "backfilled_at", "counters_in_week", "note"}

    earlier = row(table, "2026-09-21")
    assert earlier["covers_through"] == "2026-10-05" and "Covers two weeks" in earlier["note"]
    # The counters are as they were, and the all-time row is not touched.
    assert earlier["articles_calls"] == 311 and earlier["rolled_over_at"] == MERGED["rolled_over_at"]
    assert row(table, "all-time") == {"week_start": "all-time", "articles_calls": 311}


def test_the_placeholder_is_a_row_the_cost_poll_may_fill(table):
    """The poll's write is conditional on the row existing (set_stats_history_week_fields)."""
    repair.apply(table, repair.plan(table, "2026-09-28", TODAY), NOW)

    table.update_item(
        Key={"week_start": "2026-09-28"},
        UpdateExpression="SET aws_bill_week_complete = :done",
        ConditionExpression="attribute_exists(week_start)",
        ExpressionAttributeValues={":done": True},
    )
    assert row(table, "2026-09-28")["aws_bill_week_complete"] is True


def test_a_second_run_finds_nothing_to_do_and_never_replaces_a_row(table):
    planned = repair.plan(table, "2026-09-28", TODAY)
    repair.apply(table, planned, NOW)
    table.update_item(
        Key={"week_start": "2026-09-28"},
        UpdateExpression="SET aws_bill_week_complete = :done",
        ExpressionAttributeValues={":done": True},
    )

    with pytest.raises(repair.RepairError, match="already has a row"):
        repair.plan(table, "2026-09-28", TODAY)
    # Even applied again with the old plan, what the poll wrote since is kept.
    repair.apply(table, planned, NOW)
    assert row(table, "2026-09-28")["aws_bill_week_complete"] is True


@pytest.mark.parametrize(
    ("week", "today", "reason"),
    [
        ("2026-09-29", TODAY, "not a Monday"),
        ("28 September", TODAY, "not a date"),
        ("2026-10-05", TODAY, "has not ended yet"),
        ("2026-09-14", TODAY, "no row for 2026-09-07"),
    ],
)
def test_it_refuses_what_it_cannot_mend(table, week, today, reason):
    with pytest.raises(repair.RepairError, match=reason):
        repair.plan(table, week, today)


def test_it_refuses_when_the_earlier_week_was_rolled_over_on_time(table):
    """Rolled over on the missing week's own Monday: that row holds one week, not two."""
    table.put_item(Item={**MERGED, "rolled_over_at": "2026-09-28T02:15:00+00:00"})
    with pytest.raises(repair.RepairError, match="cannot hold that week's counters"):
        repair.plan(table, "2026-09-28", TODAY)


def test_without_apply_the_command_writes_nothing(table, capsys):
    assert repair.main(["--env", "production", "--missing-week", "2026-09-28"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert row(table, "2026-09-28") is None and "covers_through" not in row(table, "2026-09-21")

    assert repair.main(["--env", "production", "--missing-week", "2026-09-28", "--apply"]) == 0
    assert row(table, "2026-09-28") is not None
    assert repair.main(["--env", "production", "--missing-week", "2026-09-28", "--apply"]) == 1
    assert "Nothing written" in capsys.readouterr().out
