"""The stagger (common/stagger.py): every topic takes a slot, and its two schedules are written at
that slot's minute, so no two topics call their sources at the same moment. The pipeline setting
`stagger_minutes` (5 by default) is the gap; the daily cycles are delayed past the hour's research
heartbeats first. Pure logic first, then the scheduler's state handling, then the Admin API:
slots on create and update, the schedule listing, and the rewrite of every schedule."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
import test_admin_api_handler as admin_tests
from moto import mock_aws
from test_admin_api_handler import TOPIC, _event, _put_topic

import admin_api_handler
import common.scheduler as scheduler
from common import stagger

REGION = "ap-southeast-2"

# The Admin API's fixtures (the tables, the environment), under their own names.
aws_env = admin_tests.aws_env
aws_resources = admin_tests.aws_resources


# --- the pipeline setting and the slot ----------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 5, 30, Decimal("10")])
def test_whole_minutes_in_range_are_a_valid_stagger(value):
    assert stagger.stagger_error(value) is None


@pytest.mark.parametrize("value", [-1, 31, 2.5, "5", True, [5], Decimal("1.5")])
def test_anything_else_is_not(value):
    assert "whole number of minutes from 0 to 30" in stagger.stagger_error(value)


def test_unset_is_valid_and_means_the_default():
    assert stagger.stagger_error(None) is None
    assert stagger.resolve_stagger_minutes(None) == stagger.DEFAULT_STAGGER_MINUTES == 5
    assert stagger.resolve_stagger_minutes({}) == 5
    assert stagger.resolve_stagger_minutes({"stagger_minutes": 0}) == 0
    assert stagger.resolve_stagger_minutes({"stagger_minutes": Decimal("3")}) == 3
    # A stored value that is no longer valid is skipped, never trusted.
    assert stagger.resolve_stagger_minutes({"stagger_minutes": 99}) == 5


@pytest.mark.parametrize("value", [1, 2, 288, Decimal("7")])
def test_slots_are_whole_numbers_from_one(value):
    assert stagger.slot_error(value) is None
    assert stagger.topic_slot({"stagger_slot": value}) == int(value)


@pytest.mark.parametrize("value", [0, -1, 289, 1.5, "2", True])
def test_other_slots_are_refused_and_an_invalid_stored_one_reads_as_none(value):
    assert "whole number from 1 to 288" in stagger.slot_error(value)
    assert stagger.topic_slot({"stagger_slot": value}) is None


def test_the_next_free_slot_fills_gaps_before_opening_new_ones():
    topics = [
        {"topic_id": "a", "stagger_slot": 1},
        {"topic_id": "b", "stagger_slot": 3},
        {"topic_id": "c"},  # from before slots existed
    ]
    assert stagger.used_slots(topics) == {1: "a", 3: "b"}
    assert stagger.next_free_slot(topics) == 2
    assert stagger.next_free_slot(topics, exclude="a") == 1
    assert stagger.next_free_slot([]) == 1
    assert stagger.first_free_slot({1, 2, 3}) == 4


# --- offsets and expressions ---------------------------------------------------------------------


def test_slot_k_researches_k_staggers_past_the_hour_and_the_daily_waits_for_every_heartbeat():
    # Four topics, 5 minutes: research at :05, :10, :15, :20; daily cycles 20 minutes late, one
    # slot apart: 9:20, 9:25, 9:30, 9:35.
    assert [stagger.research_offset_minutes(k, 5) for k in (1, 2, 3, 4)] == [5, 10, 15, 20]
    assert [stagger.daily_offset_minutes(k, 5, 4) for k in (1, 2, 3, 4)] == [20, 25, 30, 35]
    # No slot, or staggering off: no offset at all.
    assert stagger.research_offset_minutes(None, 5) == 0
    assert stagger.daily_offset_minutes(2, 0, 4) == 0


@pytest.mark.parametrize(
    "expression, offset, expected",
    [
        ("rate(1 hour)", 5, "cron(5 * * * ? *)"),
        ("rate(4 hours)", 10, "cron(10 */4 * * ? *)"),
        ("rate(4 hours)", 65, "cron(5 1/4 * * ? *)"),  # an hour and five minutes late
        ("rate(1 day)", 5, "cron(5 0 * * ? *)"),
        ("rate(2 days)", 5, "cron(5 0 1/2 * ? *)"),
        ("cron(1 */4 * * ? *)", 15, "cron(15 */4 * * ? *)"),  # the slot's minute replaces the cadence's
        ("cron(0 9 * * ? *)", 25, "cron(25 9 * * ? *)"),
        ("cron(0 9 * * ? *)", 85, "cron(25 10 * * ? *)"),  # carried into the hour
        ("cron(0 23 * * ? *)", 70, "cron(10 0 * * ? *)"),  # and round the clock
        ("cron(0 9,21 * * ? *)", 65, "cron(5 10,22 * * ? *)"),
        ("cron(0 9-17 ? * MON-FRI *)", 65, "cron(5 10-18 ? * MON-FRI *)"),
        ("cron(30 8 * * ? *)", 5, "cron(5 8 * * ? *)"),
    ],
)
def test_the_offset_sets_the_minute_of_a_rate_or_a_cron(expression, offset, expected):
    assert stagger.staggered_expression(expression, offset) == (expected, True)


@pytest.mark.parametrize(
    "expression",
    ["rate(30 minutes)", "cron(*/15 * * * ? *)", "cron(0,30 * * * ? *)", "at(2026-01-01T00:00:00)", "junk"],
)
def test_what_has_no_single_minute_to_set_is_written_as_it_is(expression):
    assert stagger.staggered_expression(expression, 5) == (expression, False)


def test_no_offset_leaves_everything_alone():
    assert stagger.staggered_expression("rate(1 hour)", 0) == ("rate(1 hour)", False)


def test_effective_schedules_say_what_was_applied():
    result = stagger.effective_schedules(
        research_cadence="rate(1 hour)",
        daily_cadence="cron(0 9 * * ? *)",
        slot=2,
        stagger_minutes=5,
        topic_count=4,
    )
    assert result == {
        "research": "cron(10 * * * ? *)",
        "daily": "cron(25 9 * * ? *)",
        "research_offset_minutes": 10,
        "daily_offset_minutes": 25,
        "staggered": True,
    }
    unchanged = stagger.effective_schedules(
        research_cadence="rate(30 minutes)",
        daily_cadence="cron(*/30 * * * ? *)",
        slot=2,
        stagger_minutes=5,
        topic_count=4,
    )
    assert unchanged["staggered"] is False
    assert unchanged["research_offset_minutes"] == unchanged["daily_offset_minutes"] == 0


@pytest.mark.parametrize(
    "expression, words",
    [
        ("cron(5 * * * ? *)", "every hour at :05 past"),
        ("cron(10 */4 * * ? *)", "every 4 hours at :10 past"),
        ("cron(5 1/4 * * ? *)", "every 4 hours at :05 past (from 01:05)"),
        ("cron(20 9 * * ? *)", "daily at 09:20"),
        ("cron(5 0 1/2 * ? *)", "at 00:05 (cron(5 0 1/2 * ? *))"),
        ("rate(1 hour)", "every hour"),
        ("rate(2 days)", "every 2 days"),
        ("cron(*/15 * * * ? *)", "cron(*/15 * * * ? *)"),
    ],
)
def test_expressions_are_described_in_plain_words_or_shown_as_they_are(expression, words):
    assert stagger.describe_expression(expression) == words


# --- the scheduler: a paused topic's schedules are disabled, not deleted ---------------------------


@pytest.fixture
def scheduler_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("RESEARCH_TICK_FUNCTION_ARN", "arn:aws:lambda:ap-southeast-2:123456789012:function:rt")
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:ap-southeast-2:123456789012:stateMachine:dc")
    monkeypatch.setenv("SCHEDULER_INVOKE_ROLE_ARN", "arn:aws:iam::123456789012:role/scheduler-invoke")
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")
    scheduler._scheduler_client = None
    with mock_aws():
        yield boto3.client("scheduler", region_name=REGION)


def _state(client, name):
    return client.get_schedule(Name=name, GroupName="default")["State"]


def test_schedules_are_written_enabled_unless_told_otherwise(scheduler_env):
    scheduler.upsert_topic_schedules("topic-a", "cron(5 * * * ? *)", "cron(20 9 * * ? *)")
    scheduler.upsert_topic_schedules("topic-b", "cron(10 * * * ? *)", "cron(25 9 * * ? *)", enabled=False)

    assert _state(scheduler_env, "bloggerbear-dev-topic-a-research-tick") == "ENABLED"
    assert _state(scheduler_env, "bloggerbear-dev-topic-b-research-tick") == "DISABLED"
    assert _state(scheduler_env, "bloggerbear-dev-topic-b-daily-cycle") == "DISABLED"


def test_the_state_can_be_flipped_without_rewriting_what_runs(scheduler_env):
    scheduler.upsert_topic_schedules(
        "topic-a", "cron(5 * * * ? *)", "cron(20 9 * * ? *)", "Australia/Sydney"
    )

    changed = scheduler.set_topic_schedules_state("topic-a", enabled=False)

    assert changed == ["bloggerbear-dev-topic-a-research-tick", "bloggerbear-dev-topic-a-daily-cycle"]
    daily = scheduler_env.get_schedule(Name="bloggerbear-dev-topic-a-daily-cycle", GroupName="default")
    assert daily["State"] == "DISABLED"
    assert daily["ScheduleExpression"] == "cron(20 9 * * ? *)"
    assert daily["ScheduleExpressionTimezone"] == "Australia/Sydney"
    assert json.loads(daily["Target"]["Input"]) == {"topic_id": "topic-a"}

    scheduler.set_topic_schedules_state("topic-a", enabled=True)
    assert _state(scheduler_env, "bloggerbear-dev-topic-a-daily-cycle") == "ENABLED"


def test_a_topic_with_no_schedules_yet_is_skipped_not_an_error(scheduler_env):
    assert scheduler.set_topic_schedules_state("never-made", enabled=False) == []


# --- the Admin API: slots on create and update ----------------------------------------------------


def _create(topic_id, **extra):
    body = {"topic_id": topic_id, "name": topic_id.title(), "adapter": "github_trending", **extra}
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)
    return result, mock_upsert


def _row(topic_id):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    return table.get_item(Key={"topic_id": topic_id})["Item"]


def test_each_new_topic_takes_the_next_slot_and_its_schedules_are_written_at_that_minute(aws_resources):
    first, upsert_first = _create("first")
    second, upsert_second = _create("second")

    assert json.loads(first["body"])["stagger_slot"] == 1
    assert json.loads(second["body"])["stagger_slot"] == 2
    # One topic when the first was made: its daily cycle 5 minutes late. Two when the second was:
    # 10 minutes of delay, then slot 2 is one slot later still (15).
    upsert_first.assert_called_once_with(
        "first", "cron(5 * * * ? *)", "cron(5 9 * * ? *)", "Australia/Sydney", enabled=True
    )
    upsert_second.assert_called_once_with(
        "second", "cron(10 * * * ? *)", "cron(15 9 * * ? *)", "Australia/Sydney", enabled=True
    )
    # What was written is on the row, for DynamoDB and `topics schedule`.
    assert _row("second")["research_schedule"] == "cron(10 * * * ? *)"
    assert _row("second")["daily_schedule"] == "cron(15 9 * * ? *)"


def test_a_slot_can_be_chosen_but_not_one_another_topic_holds(aws_resources):
    result, _ = _create("first", stagger_slot=3)
    assert json.loads(result["body"])["stagger_slot"] == 3

    clash, mock_upsert = _create("second", stagger_slot=3)
    assert clash["statusCode"] == 409
    assert "taken by topic 'first'" in json.loads(clash["body"])["error"]
    mock_upsert.assert_not_called()

    bad, _ = _create("third", stagger_slot=0)
    assert bad["statusCode"] == 400
    assert "'stagger_slot' must be a whole number from 1 to 288" in json.loads(bad["body"])["error"]


def test_an_update_can_move_a_topic_or_hand_it_the_next_free_slot(aws_resources):
    _create("first")
    _create("second")

    def _update(slot):
        event = _event(
            "PUT /topics/{topic_id}", path_params={"topic_id": "first"}, body={"stagger_slot": slot}
        )
        return admin_api_handler.handler(event, None)

    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        moved = _update(5)
    assert json.loads(moved["body"])["stagger_slot"] == 5
    # Two topics: 10 minutes of delay, then slot 5 is four slots later (30).
    mock_upsert.assert_called_once_with(
        "first", "cron(25 * * * ? *)", "cron(30 9 * * ? *)", "Australia/Sydney", enabled=True
    )

    with patch("admin_api_handler.upsert_topic_schedules"):
        cleared = _update(None)
    assert json.loads(cleared["body"])["stagger_slot"] == 1  # the gap "second" (slot 2) left below it

    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        clash = _update(2)
    assert clash["statusCode"] == 409 and "taken by topic 'second'" in clash["body"]
    mock_upsert.assert_not_called()


def test_staggering_off_writes_the_cadences_as_they_are(aws_resources):
    admin_api_handler.handler(_event("PUT /pipeline-config", body={"stagger_minutes": 0}), None)

    _, mock_upsert = _create("plain")

    mock_upsert.assert_called_once_with(
        "plain", "rate(1 hour)", "cron(0 9 * * ? *)", "Australia/Sydney", enabled=True
    )


def test_the_pipeline_stagger_can_be_set_read_and_cleared(aws_resources):
    before = json.loads(admin_api_handler.handler(_event("GET /pipeline-config"), None)["body"])
    assert before["stagger_minutes"] is None and before["effective_stagger_minutes"] == 5

    result = admin_api_handler.handler(_event("PUT /pipeline-config", body={"stagger_minutes": 3}), None)
    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["stagger_minutes"] == 3 and body["effective_stagger_minutes"] == 3

    cleared = json.loads(
        admin_api_handler.handler(
            _event("PUT /pipeline-config", body={"stagger_minutes": None}), None
        )["body"]
    )
    assert cleared["stagger_minutes"] is None and cleared["effective_stagger_minutes"] == 5

    refused = admin_api_handler.handler(_event("PUT /pipeline-config", body={"stagger_minutes": 45}), None)
    assert refused["statusCode"] == 400 and "'stagger_minutes'" in refused["body"]


# --- the schedule listing and the rewrite of every schedule --------------------------------------


def test_the_schedule_listing_shows_each_topic_as_written_and_in_plain_words(aws_resources):
    _create("first")
    _put_topic({**TOPIC, "topic_id": "legacy", "research_cadence": "cron(1 */4 * * ? *)"})

    listing = json.loads(admin_api_handler.handler(_event("GET /topics/schedule"), None)["body"])

    assert listing["stagger_minutes"] == 5 and listing["topic_count"] == 2
    assert listing["daily_delay_minutes"] == 10
    first, legacy = listing["topics"]
    assert first["topic_id"] == "first" and first["stagger_slot"] == 1 and first["paused"] is False
    assert first["research_schedule"] == "cron(5 * * * ? *)"
    assert first["research_schedule_text"] == "every hour at :05 past"
    assert first["daily_schedule_text"] == "daily at 09:05" and first["daily_timezone"] == "Australia/Sydney"
    # A topic from before slots existed shows its cadence as it stands, and no slot.
    assert legacy["stagger_slot"] is None
    assert legacy["research_schedule"] == "cron(1 */4 * * ? *)"
    assert legacy["research_schedule_text"] == "every 4 hours at :01 past"


def test_reschedule_gives_older_topics_slots_and_rewrites_every_schedule(aws_resources):
    # Production's hand-kept minutes (:02 and :01) are replaced by the slots'.
    def _legacy(topic_id, research, daily):
        _put_topic({**TOPIC, "topic_id": topic_id, "research_cadence": research, "daily_cadence": daily})

    _legacy("crypto", "cron(2 */4 * * ? *)", "cron(1 9 * * ? *)")
    _legacy("github", "cron(1 */4 * * ? *)", "cron(0 9 * * ? *)")
    _put_topic({**TOPIC, "topic_id": "wow", "stagger_slot": 1, "research_cadence": "rate(1 hour)"})
    _put_topic({**_row("wow"), "paused": True})

    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics/reschedule"), None)

    body = json.loads(result["body"])
    assert body["dry_run"] is False and body["topic_count"] == 3 and body["daily_delay_minutes"] == 15
    # "wow" keeps slot 1; the others are filled in topic-id order.
    slots = [(t["topic_id"], t["stagger_slot"]) for t in body["topics"]]
    assert slots == [("crypto", 2), ("github", 3), ("wow", 1)]
    assert _row("github")["stagger_slot"] == 3 and _row("github")["daily_schedule"] == "cron(25 9 * * ? *)"
    # A paused topic's schedules are rewritten disabled, so a reschedule never restarts it.
    calls = {c.args[0]: c for c in mock_upsert.call_args_list}
    assert calls["crypto"].args[1:] == ("cron(10 */4 * * ? *)", "cron(20 9 * * ? *)", "UTC")
    assert calls["github"].args[1:] == ("cron(15 */4 * * ? *)", "cron(25 9 * * ? *)", "UTC")
    assert calls["wow"].args[1:] == ("cron(5 * * * ? *)", "cron(15 9 * * ? *)", "UTC")
    assert calls["wow"].kwargs == {"enabled": False}
    assert calls["crypto"].kwargs == {"enabled": True}


def test_a_dry_run_reports_the_plan_and_writes_nothing(aws_resources):
    _put_topic({**TOPIC, "topic_id": "legacy"})

    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics/reschedule", body={"dry_run": True}), None)

    body = json.loads(result["body"])
    assert body["dry_run"] is True
    assert body["topics"][0]["stagger_slot"] == 1
    assert body["topics"][0]["research_schedule"] == "cron(5 * * * ? *)"
    mock_upsert.assert_not_called()
    assert "stagger_slot" not in _row("legacy")


def test_one_topics_bad_cadence_does_not_stop_the_rest(aws_resources):
    _put_topic({**TOPIC, "topic_id": "broken", "daily_cadence": "whenever"})
    _put_topic({**TOPIC, "topic_id": "fine"})

    def _upsert(topic_id, *args, **kwargs):
        if topic_id == "broken":
            raise ValueError("bad")

    with patch("admin_api_handler.upsert_topic_schedules", side_effect=_upsert):
        result = admin_api_handler.handler(_event("POST /topics/reschedule"), None)

    assert result["statusCode"] == 200
    broken, fine = json.loads(result["body"])["topics"]
    assert broken["topic_id"] == "broken" and "bad" in broken["error"]
    assert fine["topic_id"] == "fine" and "error" not in fine
