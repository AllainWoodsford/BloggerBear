"""The dispatcher's decisions (common/dispatch.py): the two pipeline settings and their route, the
message and execution-name shapes, the claims as the exact conditional expressions the spec gives,
the due rules through a tick, shadow mode's fields and mismatches, and the start cap. Nothing calls
the dispatcher yet (its Lambda is Q1); the tick is driven here with moto's DynamoDB and fake queue
and Step Functions clients."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import boto3
import pytest
import test_admin_api_handler as admin_tests
from botocore.exceptions import ClientError
from test_admin_api_handler import _event

import admin_api_handler
from common import dispatch, dynamo
from common.dispatch import (
    DAILY_CLAIM_CONDITION,
    DAILY_CLAIM_UPDATE,
    RESEARCH_CLAIM_CONDITION,
    RESEARCH_CLAIM_UPDATE,
    Claim,
    Dispatcher,
    DispatchError,
)

REGION = "ap-southeast-2"
SYDNEY = ZoneInfo("Australia/Sydney")
QUEUE_URL = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/work.fifo"
STATE_MACHINE_ARN = "arn:aws:states:ap-southeast-2:123456789012:stateMachine:daily-cycle"

# The Admin API's fixtures (the tables, the environment), under their own names.
aws_env = admin_tests.aws_env
aws_resources = admin_tests.aws_resources

# 09:10 Sydney on 2026-10-25 (AEDT, UTC+11): with one topic in slot 1 and the default stagger, the
# research heartbeat is at :05 and the daily cycle at 09:10.
DAILY_TURN = datetime(2026, 10, 24, 22, 10, 0, tzinfo=UTC)
RESEARCH_TURN = datetime(2026, 10, 24, 22, 5, 30, tzinfo=UTC)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def topic(topic_id="github-trending", **fields) -> dict:
    row = {
        "topic_id": topic_id,
        "name": topic_id,
        "adapter": "github_trending",
        "research_cadence": "rate(1 hour)",
        "daily_cadence": "cron(0 9 * * ? *)",
        "daily_timezone": "Australia/Sydney",
        "stagger_slot": 1,
        "dispatch_mode": "queue",
    }
    row.update(fields)
    return row


def put_topics(*rows) -> None:
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    for row in rows:
        table.put_item(Item=row)


def topics_table():
    return boto3.resource("dynamodb", region_name=REGION).Table("Topics")


def stored(topic_id="github-trending") -> dict:
    return topics_table().get_item(Key={"topic_id": topic_id}).get("Item") or {}


def pipeline_row() -> dict:
    return dynamo.get_config_row("pipeline") or {}


def dispatcher(sqs=None, sfn=None) -> Dispatcher:
    return Dispatcher(
        sqs=sqs or MagicMock(),
        sfn=sfn or MagicMock(),
        queue_url=QUEUE_URL,
        state_machine_arn=STATE_MACHINE_ARN,
    )


# --- the two settings -------------------------------------------------------------------------------------


def test_every_setting_has_a_default_and_invalid_values_fall_back_to_it():
    assert dispatch.SETTINGS == {"dispatch_shadow": True, "daily_starts_per_tick": 3}
    assert dispatch.effective_settings(None) == {"dispatch_shadow": True, "daily_starts_per_tick": 3}
    settings = dispatch.effective_settings({"dispatch_shadow": False, "daily_starts_per_tick": 11})
    assert settings == {"dispatch_shadow": False, "daily_starts_per_tick": 3}
    # DynamoDB hands numbers back as Decimals; the effective value is a plain int.
    assert dispatch.effective_settings({"daily_starts_per_tick": Decimal("7")})["daily_starts_per_tick"] == 7


@pytest.mark.parametrize(
    "name, value",
    [
        ("dispatch_shadow", "on"),
        ("dispatch_shadow", 1),
        ("dispatch_shadow", "true"),
        ("daily_starts_per_tick", 0),
        ("daily_starts_per_tick", 11),
        ("daily_starts_per_tick", True),
        ("daily_starts_per_tick", "3"),
        ("daily_starts_per_tick", 2.5),
        ("daily_starts_per_tick", Decimal("2.5")),
        ("not_a_setting", 1),
    ],
)
def test_invalid_settings_are_refused(name, value):
    assert dispatch.validate_setting(name, value)


@pytest.mark.parametrize(
    "name, value",
    [("dispatch_shadow", False), ("daily_starts_per_tick", 10), ("daily_starts_per_tick", Decimal("1"))],
)
def test_valid_settings_pass(name, value):
    assert dispatch.validate_setting(name, value) is None


def test_none_clears_any_setting():
    assert dispatch.validate_setting("dispatch_shadow", None) is None
    assert dispatch.validate_setting("daily_starts_per_tick", None) is None


def test_the_settings_live_on_the_pipeline_row_dynamo_keeps():
    assert dispatch.PIPELINE_CONFIG_ID == dynamo._PIPELINE_CONFIG_ID == "pipeline"


def test_the_settings_round_trip_through_put_pipeline_config(aws_resources):
    config = dynamo.put_pipeline_config(dispatch_shadow=False, daily_starts_per_tick=4)

    assert config["dispatch_shadow"] is False and config["daily_starts_per_tick"] == 4
    assert isinstance(dynamo.get_pipeline_config()["daily_starts_per_tick"], int)

    cleared = dynamo.put_pipeline_config(dispatch_shadow=None, daily_starts_per_tick=None)
    assert "dispatch_shadow" not in cleared and "daily_starts_per_tick" not in cleared


# --- GET/PUT /pipeline-config -------------------------------------------------------------------------------


def _pipeline_config(method="GET", body=None):
    result = admin_api_handler.handler(_event(f"{method} /pipeline-config", body=body), None)
    return result["statusCode"], json.loads(result["body"])


def test_the_route_shows_the_two_settings_unset_with_their_defaults_in_effect(aws_resources):
    status, body = _pipeline_config()

    assert status == 200
    assert body["dispatch_shadow"] is None and body["effective_dispatch_shadow"] is True
    assert body["daily_starts_per_tick"] is None and body["effective_daily_starts_per_tick"] == 3


def test_the_route_sets_reads_and_clears_the_two_settings(aws_resources):
    status, body = _pipeline_config("PUT", {"dispatch_shadow": False, "daily_starts_per_tick": 5})

    assert status == 200
    assert body["dispatch_shadow"] is False and body["effective_dispatch_shadow"] is False
    assert body["daily_starts_per_tick"] == 5 and body["effective_daily_starts_per_tick"] == 5
    row = pipeline_row()
    assert row["dispatch_shadow"] is False and row["daily_starts_per_tick"] == 5

    _, body = _pipeline_config("PUT", {"dispatch_shadow": None, "daily_starts_per_tick": None})
    assert body["dispatch_shadow"] is None and body["effective_dispatch_shadow"] is True
    assert body["daily_starts_per_tick"] is None and body["effective_daily_starts_per_tick"] == 3
    assert "dispatch_shadow" not in pipeline_row() and "daily_starts_per_tick" not in pipeline_row()


@pytest.mark.parametrize(
    "body, message",
    [
        ({"dispatch_shadow": "on"}, "'dispatch_shadow' must be true or false"),
        ({"dispatch_shadow": 1}, "'dispatch_shadow' must be true or false"),
        ({"daily_starts_per_tick": 0}, "'daily_starts_per_tick' must be a whole number from 1 to 10"),
        ({"daily_starts_per_tick": 11}, "'daily_starts_per_tick' must be a whole number from 1 to 10"),
        ({"daily_starts_per_tick": "3"}, "'daily_starts_per_tick' must be a whole number from 1 to 10"),
        ({"daily_starts_per_tick": True}, "'daily_starts_per_tick' must be a whole number from 1 to 10"),
    ],
)
def test_a_bad_setting_is_refused_and_stops_the_whole_update(aws_resources, body, message):
    status, answer = _pipeline_config("PUT", {"stagger_minutes": 3, **body})

    assert status == 400 and answer["error"] == message
    assert pipeline_row() == {}  # nothing was written, not even the valid setting


def test_setting_the_dispatcher_leaves_the_other_settings_alone_and_the_reverse(aws_resources):
    _pipeline_config("PUT", {"research_interval_hours": 3, "stagger_minutes": 2})

    _, body = _pipeline_config("PUT", {"dispatch_shadow": False})
    assert body["research_interval_hours"] == 3 and body["stagger_minutes"] == 2

    _, body = _pipeline_config("PUT", {"stagger_minutes": None})
    assert body["dispatch_shadow"] is False and body["research_interval_hours"] == 3


def test_an_empty_update_names_the_new_settings_too(aws_resources):
    status, body = _pipeline_config("PUT", {"nothing": 1})

    assert status == 400
    assert "'dispatch_shadow' and/or 'daily_starts_per_tick'" in body["error"]


# --- names and messages (spec 4.7) -------------------------------------------------------------------------


def test_the_slug_is_the_topic_id_made_safe_for_an_execution_name():
    assert dispatch.slug("github-trending") == "github-trending"
    assert dispatch.slug("Finance Crypto_Investing!") == "finance-crypto-investing-"
    assert dispatch.slug("a" * 50) == "a" * 40
    assert dispatch.SLUG_MAX_CHARS == 40


def test_execution_names_carry_the_slug_the_local_date_and_the_claim_number():
    assert dispatch.execution_name("GitHub Trending", "2026-10-25", 1) == "github-trending-2026-10-25-1"
    assert dispatch.manual_execution_name("github-trending", "2026-10-25", at("2026-10-25T03:04:05")) == (
        "github-trending-2026-10-25-manual-030405"
    )
    assert len(dispatch.execution_name("x" * 60, "2026-10-25", 1000)) <= 80


def test_the_research_message_and_its_dedup_id_are_the_spec_s():
    now = at("2026-10-25T03:04:05.678901")

    assert dispatch.research_message("github-trending", now=now) == {
        "topic_id": "github-trending",
        "job": "research",
        "force": False,
        "due_checked_at": "2026-10-25T03:04:05+00:00",
        "dispatched_at": "2026-10-25T03:04:05+00:00",
    }
    assert dispatch.research_dedup_id("github-trending", now) == "github-trending:research:2026-10-25T03:04"


def test_the_research_claim_expires_one_interval_back_less_the_due_tolerance():
    now = at("2026-10-25T04:05:30")

    assert dispatch.research_claim_expiry(now, 1) == "2026-10-25T03:10:30+00:00"
    assert dispatch.research_claim_expiry(now, 4) == "2026-10-25T00:10:30+00:00"


# --- claims: the conditional expressions, word for word ---------------------------------------------------


def test_the_claim_expressions_are_the_spec_s_word_for_word():
    assert DAILY_CLAIM_UPDATE == "SET last_daily_dispatched_on = :today ADD daily_claim_n :one"
    assert DAILY_CLAIM_CONDITION == (
        "attribute_not_exists(last_daily_dispatched_on) OR last_daily_dispatched_on <> :today"
    )
    assert RESEARCH_CLAIM_UPDATE == "SET research_claimed_at = :now"
    assert RESEARCH_CLAIM_CONDITION == (
        "attribute_not_exists(research_claimed_at) OR research_claimed_at < :expiry"
    )


def test_the_daily_claim_is_one_conditional_update_with_those_expressions():
    table = MagicMock()
    table.update_item.return_value = {
        "Attributes": {"last_daily_dispatched_on": "2026-10-24", "daily_claim_n": 3}
    }

    claim = dispatch.claim_daily(table, "github-trending", "2026-10-25")

    assert claim == Claim(previous="2026-10-24", n=4)
    table.update_item.assert_called_once_with(
        Key={"topic_id": "github-trending"},
        UpdateExpression=DAILY_CLAIM_UPDATE,
        ConditionExpression=f"attribute_exists(topic_id) AND ({DAILY_CLAIM_CONDITION})",
        ExpressionAttributeValues={":today": "2026-10-25", ":one": 1},
        ReturnValues="ALL_OLD",
    )


def test_the_research_claim_is_one_conditional_update_with_those_expressions():
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {}}
    now = at("2026-10-25T04:05:30")

    claim = dispatch.claim_research(table, "github-trending", now, 4)

    assert claim == Claim(previous=None, n=None)
    table.update_item.assert_called_once_with(
        Key={"topic_id": "github-trending"},
        UpdateExpression=RESEARCH_CLAIM_UPDATE,
        ConditionExpression=f"attribute_exists(topic_id) AND ({RESEARCH_CLAIM_CONDITION})",
        ExpressionAttributeValues={
            ":now": "2026-10-25T04:05:30+00:00",
            ":expiry": "2026-10-25T00:10:30+00:00",
        },
        ReturnValues="ALL_OLD",
    )


def test_a_day_is_claimed_once_and_the_claim_number_counts_the_days(aws_resources):
    put_topics(topic())

    first = dispatch.claim_daily(topics_table(), "github-trending", "2026-10-25")
    assert first == Claim(previous=None, n=1)
    assert stored()["last_daily_dispatched_on"] == "2026-10-25" and stored()["daily_claim_n"] == 1

    assert dispatch.claim_daily(topics_table(), "github-trending", "2026-10-25") is None
    assert stored()["daily_claim_n"] == 1

    tomorrow = dispatch.claim_daily(topics_table(), "github-trending", "2026-10-26")
    assert tomorrow == Claim(previous="2026-10-25", n=2)
    assert stored()["last_daily_dispatched_on"] == "2026-10-26"


def test_a_claim_never_recreates_a_deleted_topic(aws_resources):
    assert dispatch.claim_daily(topics_table(), "gone", "2026-10-25") is None
    assert dispatch.claim_research(topics_table(), "gone", at("2026-10-25T04:05:30"), 1) is None
    assert stored("gone") == {}


def test_releasing_a_daily_claim_puts_back_what_was_there_and_keeps_the_number(aws_resources):
    put_topics(topic())
    table = topics_table()
    dispatch.claim_daily(table, "github-trending", "2026-10-24")
    claim = dispatch.claim_daily(table, "github-trending", "2026-10-25")

    dispatch.release_daily(table, "github-trending", "2026-10-25", claim.previous)
    assert stored()["last_daily_dispatched_on"] == "2026-10-24" and stored()["daily_claim_n"] == 2

    put_topics(topic())
    first = dispatch.claim_daily(table, "github-trending", "2026-10-25")
    dispatch.release_daily(table, "github-trending", "2026-10-25", first.previous)
    assert "last_daily_dispatched_on" not in stored() and stored()["daily_claim_n"] == 1


def test_a_release_leaves_a_claim_someone_else_now_holds(aws_resources):
    put_topics(topic())
    table = topics_table()
    dispatch.claim_daily(table, "github-trending", "2026-10-25")
    dispatch.claim_daily(table, "github-trending", "2026-10-26")  # the next day's tick, say

    dispatch.release_daily(table, "github-trending", "2026-10-25", None)  # no error, no effect
    assert stored()["last_daily_dispatched_on"] == "2026-10-26"


def test_research_is_claimed_once_per_interval_and_released_on_failure(aws_resources):
    put_topics(topic())
    table = topics_table()
    now = at("2026-10-25T04:05:30")

    assert dispatch.claim_research(table, "github-trending", now, 1) == Claim(previous=None)
    assert stored()["research_claimed_at"] == "2026-10-25T04:05:30+00:00"
    assert dispatch.claim_research(table, "github-trending", now + timedelta(minutes=30), 1) is None

    later = now + timedelta(hours=1)
    again = dispatch.claim_research(table, "github-trending", later, 1)
    assert again == Claim(previous="2026-10-25T04:05:30+00:00")

    dispatch.release_research(table, "github-trending", later, again.previous)
    assert stored()["research_claimed_at"] == "2026-10-25T04:05:30+00:00"
    dispatch.release_research(table, "github-trending", now, None)
    assert "research_claimed_at" not in stored()


def test_the_claim_expiry_agrees_with_the_due_rule_at_exactly_one_interval(aws_resources):
    """The tick says a topic last checked at :05 is due at the next :05 (less its tolerance). A
    claim that only expired strictly after a whole interval would refuse that very minute and skip
    a heartbeat; the expiry includes the same tolerance, so the two agree."""
    put_topics(topic())
    table = topics_table()
    claimed = at("2026-10-25T04:05:00")
    dispatch.claim_research(table, "github-trending", claimed, 1)

    assert dispatch.claim_research(table, "github-trending", claimed + timedelta(hours=1), 1) is not None


# --- a tick: research --------------------------------------------------------------------------------------


def test_a_queue_topic_due_for_research_is_claimed_and_sent_once(aws_resources):
    put_topics(topic())
    sqs = MagicMock()

    counts = dispatcher(sqs=sqs).tick(RESEARCH_TURN)

    assert counts == {"considered": 1, "dispatched": 1, "started": 0, "skipped_unparsable": 0}
    sqs.send_message.assert_called_once_with(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(
            {
                "topic_id": "github-trending",
                "job": "research",
                "force": False,
                "due_checked_at": "2026-10-24T22:05:30+00:00",
                "dispatched_at": "2026-10-24T22:05:30+00:00",
            }
        ),
        MessageGroupId="github-trending",
        MessageDeduplicationId="github-trending:research:2026-10-24T22:05",
    )
    assert stored()["research_claimed_at"] == "2026-10-24T22:05:30+00:00"
    assert pipeline_row()["last_tick"] == "2026-10-24T22:05:30+00:00"

    # The next minute is not a turn (the heartbeat is at :05), so nothing more goes out.
    counts = dispatcher(sqs=sqs).tick(RESEARCH_TURN + timedelta(minutes=1))
    assert counts["dispatched"] == 0 and sqs.send_message.call_count == 1


def test_research_waits_for_the_topic_to_be_due(aws_resources):
    put_topics(topic(last_research_at=(RESEARCH_TURN - timedelta(minutes=30)).isoformat()))
    sqs = MagicMock()

    counts = dispatcher(sqs=sqs).tick(RESEARCH_TURN)

    assert counts["dispatched"] == 0 and counts["considered"] == 1
    sqs.send_message.assert_not_called()
    assert "research_claimed_at" not in stored()


def test_a_topics_own_interval_holds_research_back(aws_resources):
    two_hours_ago = (RESEARCH_TURN - timedelta(hours=2)).isoformat()
    put_topics(topic(research_interval_hours=4, last_research_at=two_hours_ago))
    sqs = MagicMock()

    dispatcher(sqs=sqs).tick(RESEARCH_TURN)

    sqs.send_message.assert_not_called()


def test_with_the_stagger_off_a_rate_cadence_makes_every_minute_a_turn_and_is_due_decides(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic())
    sqs = MagicMock()

    dispatcher(sqs=sqs).tick(at("2026-10-24T22:17:00"))

    sqs.send_message.assert_called_once()
    sent = sqs.send_message.call_args.kwargs
    assert sent["MessageDeduplicationId"] == "github-trending:research:2026-10-24T22:17"


def test_the_research_window_runs_from_the_last_tick_so_a_missed_minute_costs_nothing(aws_resources):
    put_topics(topic())
    sqs = MagicMock()
    dynamo.update_config_row("pipeline", {"last_tick": (RESEARCH_TURN - timedelta(minutes=3)).isoformat()})

    dispatcher(sqs=sqs).tick(RESEARCH_TURN + timedelta(minutes=2))  # :07, the :05 turn in the gap

    sqs.send_message.assert_called_once()


def test_without_a_last_tick_only_this_minute_counts(aws_resources):
    put_topics(topic())
    sqs = MagicMock()

    dispatcher(sqs=sqs).tick(RESEARCH_TURN + timedelta(minutes=2))

    sqs.send_message.assert_not_called()


def test_a_send_that_fails_releases_the_claim_and_raises_so_the_alarm_fires(aws_resources):
    put_topics(topic())
    sqs = MagicMock()
    sqs.send_message.side_effect = RuntimeError("queue unavailable")

    with pytest.raises(DispatchError):
        dispatcher(sqs=sqs).tick(RESEARCH_TURN)

    assert "research_claimed_at" not in stored()
    assert "last_tick" not in pipeline_row()  # the next tick looks at this minute again


# --- a tick: the daily cycle -------------------------------------------------------------------------------


def test_the_daily_cycle_is_started_once_with_a_named_execution(aws_resources):
    put_topics(topic())
    sfn = MagicMock()

    counts = dispatcher(sfn=sfn).tick(DAILY_TURN)

    assert counts == {"considered": 1, "dispatched": 0, "started": 1, "skipped_unparsable": 0}
    sfn.start_execution.assert_called_once_with(
        stateMachineArn=STATE_MACHINE_ARN,
        name="github-trending-2026-10-25-1",
        input='{"topic_id": "github-trending"}',
    )
    assert stored()["last_daily_dispatched_on"] == "2026-10-25" and stored()["daily_claim_n"] == 1

    counts = dispatcher(sfn=sfn).tick(DAILY_TURN + timedelta(minutes=1))
    assert counts["started"] == 0 and sfn.start_execution.call_count == 1

    # The next day, the next number.
    dispatcher(sfn=sfn).tick(DAILY_TURN + timedelta(days=1))
    assert sfn.start_execution.call_args.kwargs["name"] == "github-trending-2026-10-26-2"


def test_the_daily_turn_has_not_passed_before_its_minute(aws_resources):
    put_topics(topic())
    sfn = MagicMock()

    dispatcher(sfn=sfn).tick(DAILY_TURN - timedelta(minutes=1))

    sfn.start_execution.assert_not_called()


def test_the_daily_turn_is_read_in_the_topics_zone(aws_resources):
    put_topics(topic(daily_timezone="America/Los_Angeles"))  # 09:10 in Los Angeles is 16:10 UTC (PDT)
    sfn = MagicMock()

    dispatcher(sfn=sfn).tick(at("2026-10-25T16:09:00"))
    sfn.start_execution.assert_not_called()
    dispatcher(sfn=sfn).tick(at("2026-10-25T16:10:00"))
    assert sfn.start_execution.call_args.kwargs["name"] == "github-trending-2026-10-25-1"


def test_a_topic_from_before_zones_existed_keeps_utc(aws_resources):
    row = topic()
    del row["daily_timezone"]
    put_topics(row)
    sfn = MagicMock()

    dispatcher(sfn=sfn).tick(at("2026-10-25T09:10:00"))

    assert sfn.start_execution.call_args.kwargs["name"] == "github-trending-2026-10-25-1"


def test_the_claim_stops_the_fall_back_repeat_of_the_daily_turn(aws_resources):
    """Sydney 2026-04-05: 02:30 AEDT, then 02:30 AEST an hour later. The clock pair passes both
    times; last_daily_dispatched_on says the day is done."""
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(daily_cadence="cron(30 2 * * ? *)"))
    sfn = MagicMock()

    dispatcher(sfn=sfn).tick(at("2026-04-04T15:30:00"))
    assert sfn.start_execution.call_count == 1
    dispatcher(sfn=sfn).tick(at("2026-04-04T16:30:00"))
    assert sfn.start_execution.call_count == 1
    # Even a tick that had not seen the row's date would be stopped by the claim itself.
    assert dispatch.claim_daily(topics_table(), "github-trending", "2026-04-05") is None


def test_a_spring_forward_turn_in_the_missing_hour_fires_at_three(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(daily_cadence="cron(30 2 * * ? *)"))
    sfn = MagicMock()

    dispatcher(sfn=sfn).tick(at("2026-10-03T15:59:00"))  # 01:59 AEST
    sfn.start_execution.assert_not_called()
    dispatcher(sfn=sfn).tick(at("2026-10-03T16:00:00"))  # 03:00 AEDT
    assert sfn.start_execution.call_args.kwargs["name"] == "github-trending-2026-10-04-1"


def test_at_most_daily_starts_per_tick_cycles_start_in_one_minute(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0, daily_starts_per_tick=2)
    put_topics(*(topic(f"topic-{n}", stagger_slot=n) for n in range(1, 6)))
    sfn = MagicMock()
    now = at("2026-10-24T23:30:00")  # 10:30 AEDT: every topic's 09:00 turn has passed

    assert dispatcher(sfn=sfn).tick(now)["started"] == 2
    assert [call.kwargs["name"] for call in sfn.start_execution.call_args_list] == [
        "topic-1-2026-10-25-1",
        "topic-2-2026-10-25-1",
    ]
    assert dispatcher(sfn=sfn).tick(now + timedelta(minutes=1))["started"] == 2
    assert dispatcher(sfn=sfn).tick(now + timedelta(minutes=2))["started"] == 1
    assert dispatcher(sfn=sfn).tick(now + timedelta(minutes=3))["started"] == 0
    assert sfn.start_execution.call_count == 5


def test_the_default_cap_is_three(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(*(topic(f"topic-{n}", stagger_slot=n) for n in range(1, 6)))
    sfn = MagicMock()

    assert dispatcher(sfn=sfn).tick(at("2026-10-24T23:30:00"))["started"] == 3


def test_an_execution_that_already_exists_is_consumed_not_retried(aws_resources, capsys):
    put_topics(topic())
    sfn = MagicMock()
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}}, "StartExecution"
    )

    counts = dispatcher(sfn=sfn).tick(DAILY_TURN)

    assert counts["started"] == 0
    assert stored()["last_daily_dispatched_on"] == "2026-10-25"  # the day stays claimed
    out = capsys.readouterr().out
    assert "execution exists topic_id=github-trending name=github-trending-2026-10-25-1" in out


def test_a_throttled_start_releases_the_claim_and_raises_so_the_alarm_fires(aws_resources):
    put_topics(topic())
    sfn = MagicMock()
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "StartExecution"
    )

    with pytest.raises(DispatchError):
        dispatcher(sfn=sfn).tick(DAILY_TURN)

    row = stored()
    assert "last_daily_dispatched_on" not in row
    assert row["daily_claim_n"] == 1  # the number moves on: the name is never reused


def test_any_other_start_failure_is_that_topics_problem_only(aws_resources, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic("alpha", stagger_slot=1), topic("beta", stagger_slot=2))
    sfn = MagicMock()
    sfn.start_execution.side_effect = [
        ClientError({"Error": {"Code": "InvalidName", "Message": "bad"}}, "StartExecution"),
        {"executionArn": "arn"},
    ]

    counts = dispatcher(sfn=sfn).tick(at("2026-10-24T23:30:00"))

    assert counts["started"] == 1 and counts["considered"] == 2
    assert "last_daily_dispatched_on" not in stored("alpha")
    assert stored("beta")["last_daily_dispatched_on"] == "2026-10-25"
    assert "dispatcher: topic_id=alpha skipped: ClientError(" in capsys.readouterr().out


# --- a tick: who is considered -----------------------------------------------------------------------------


def test_a_paused_topic_is_never_dispatched(aws_resources):
    put_topics(topic(paused=True))
    sqs, sfn = MagicMock(), MagicMock()

    counts = dispatcher(sqs=sqs, sfn=sfn).tick(DAILY_TURN)

    assert counts["considered"] == 0
    sqs.send_message.assert_not_called()
    sfn.start_execution.assert_not_called()


def test_a_topic_on_its_schedules_is_left_alone_when_shadow_mode_is_off(aws_resources):
    dynamo.put_pipeline_config(dispatch_shadow=False)
    put_topics(topic(dispatch_mode="schedule"))
    sqs, sfn = MagicMock(), MagicMock()

    counts = dispatcher(sqs=sqs, sfn=sfn).tick(DAILY_TURN)

    assert counts["considered"] == 0
    sqs.send_message.assert_not_called()
    sfn.start_execution.assert_not_called()
    assert "shadow_last_daily_dispatch_at" not in stored()


def test_a_queue_topic_is_dispatched_for_real_whatever_shadow_mode_says(aws_resources):
    put_topics(topic())  # dispatch_shadow is on by default
    sfn = MagicMock()

    dispatcher(sfn=sfn).tick(DAILY_TURN)

    sfn.start_execution.assert_called_once()
    assert "shadow_last_daily_dispatch_at" not in stored()


def test_an_unparsable_cadence_is_logged_and_skipped_never_guessed(aws_resources, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(
        topic("alpha", daily_cadence="at(2026-10-25T09:00:00)"),
        topic("beta", daily_cadence="rate(1 day)"),
        topic("gamma", research_cadence="cron(0 9 * JAN ? *)"),
        topic("delta"),
    )
    sfn = MagicMock()

    counts = dispatcher(sfn=sfn).tick(at("2026-10-24T23:30:00"))

    # delta alone goes through: its daily cycle, and (the stagger being off, its rate heartbeat
    # a turn every minute) one research message.
    assert counts == {"considered": 4, "dispatched": 1, "started": 1, "skipped_unparsable": 3}
    assert sfn.start_execution.call_args.kwargs["name"] == "delta-2026-10-25-1"
    out = capsys.readouterr().out
    assert "dispatcher: cadence unparsable topic_id=alpha expression='at(2026-10-25T09:00:00)'" in out
    assert "dispatcher: cadence unparsable topic_id=beta expression='rate(1 day)'" in out
    assert "dispatcher: cadence unparsable topic_id=gamma expression='cron(0 9 * JAN ? *)'" in out


def test_a_zone_nobody_knows_is_that_topics_problem_only(aws_resources, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic("alpha", daily_timezone="Mars/Olympus_Mons"), topic("beta"))
    sfn = MagicMock()

    counts = dispatcher(sfn=sfn).tick(at("2026-10-24T23:30:00"))

    assert counts["started"] == 1 and counts["considered"] == 2
    assert "dispatcher: topic_id=alpha skipped: ValueError(" in capsys.readouterr().out


def test_a_failed_topics_scan_raises_so_the_alarm_fires(aws_resources, monkeypatch):
    monkeypatch.setenv("TOPICS_TABLE", "NoSuchTable")

    with pytest.raises(DispatchError):
        dispatcher().tick(DAILY_TURN)


def test_the_stagger_decides_the_minutes_the_dispatcher_matches(aws_resources):
    """Two topics: slot 1 researches at :05 and slot 2 at :10; the daily cycles wait for both
    heartbeats, then slot 1 at 09:15 and slot 2 at 09:20 (common/stagger.py's arithmetic)."""
    put_topics(topic("alpha", stagger_slot=1), topic("beta", stagger_slot=2))
    sqs, sfn = MagicMock(), MagicMock()
    run = dispatcher(sqs=sqs, sfn=sfn)

    run.tick(at("2026-10-24T22:05:00"))
    assert [c.kwargs["MessageGroupId"] for c in sqs.send_message.call_args_list] == ["alpha"]
    run.tick(at("2026-10-24T22:10:00"))
    assert [c.kwargs["MessageGroupId"] for c in sqs.send_message.call_args_list] == ["alpha", "beta"]
    sfn.start_execution.assert_not_called()
    run.tick(at("2026-10-24T22:15:00"))
    assert [c.kwargs["name"] for c in sfn.start_execution.call_args_list] == ["alpha-2026-10-25-1"]
    run.tick(at("2026-10-24T22:20:00"))
    assert [c.kwargs["name"] for c in sfn.start_execution.call_args_list] == [
        "alpha-2026-10-25-1",
        "beta-2026-10-25-1",
    ]


# --- shadow mode -------------------------------------------------------------------------------------------


def test_shadow_mode_sends_nothing_and_records_what_it_would_have_done(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule"))
    sqs, sfn = MagicMock(), MagicMock()
    now = at("2026-10-24T22:00:00")  # 09:00 AEDT: the daily turn, and a rate heartbeat minute

    counts = dispatcher(sqs=sqs, sfn=sfn).tick(now)

    assert counts == {"considered": 1, "dispatched": 0, "started": 0, "skipped_unparsable": 0}
    sqs.send_message.assert_not_called()
    sfn.start_execution.assert_not_called()
    row = stored()
    assert row["shadow_last_research_dispatch_at"] == "2026-10-24T22:00:00+00:00"
    assert row["shadow_last_daily_dispatch_at"] == "2026-10-24T22:00:00+00:00"
    assert row["shadow_mismatches"] == 0
    assert "research_claimed_at" not in row and "last_daily_dispatched_on" not in row


def test_a_shadow_research_turn_compares_the_previous_turn_with_when_last_research_at_moved(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    now = at("2026-10-25T05:00:00")
    previous = now - timedelta(hours=1)
    put_topics(
        topic("agreed", dispatch_mode="schedule", shadow_last_research_dispatch_at=previous.isoformat(),
              last_research_at=(previous + timedelta(minutes=2)).isoformat()),
        topic("late", dispatch_mode="schedule", shadow_last_research_dispatch_at=previous.isoformat(),
              last_research_at=(previous - timedelta(hours=2)).isoformat()),
        topic("never", dispatch_mode="schedule", shadow_last_research_dispatch_at=previous.isoformat()),
        topic("first", dispatch_mode="schedule"),
    )

    dispatcher().tick(now)

    assert stored("agreed")["shadow_mismatches"] == 0
    assert stored("late")["shadow_mismatches"] == 1
    assert stored("never")["shadow_mismatches"] == 1
    assert stored("first")["shadow_mismatches"] == 0
    for name in ("agreed", "late", "never", "first"):
        assert stored(name)["shadow_last_research_dispatch_at"] == "2026-10-25T05:00:00+00:00"


def test_a_shadow_research_turn_is_only_recorded_when_the_topic_is_due(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    now = at("2026-10-25T05:00:00")
    put_topics(topic(dispatch_mode="schedule", last_research_at=(now - timedelta(minutes=10)).isoformat()))

    dispatcher().tick(now)

    assert "shadow_last_research_dispatch_at" not in stored()


# 09:00 AEDT on 2026-10-24: the shadow daily turn the day before the one most shadow tests tick at.
YESTERDAY_TURN = at("2026-10-23T22:00:00")


def _execution(arn: str, started: datetime) -> dict:
    return {"executionArn": arn, "name": arn.rsplit(":", 1)[1], "status": "SUCCEEDED", "startDate": started}


def test_a_shadow_daily_turn_checks_the_previous_day_for_an_execution_of_this_topic(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    sfn.list_executions.return_value = {  # newest first, as Step Functions lists them
        "executions": [
            _execution("arn:x:other", at("2026-10-24T22:00:10")),  # today's: not the day asked about
            _execution("arn:x:theirs", at("2026-10-23T22:05:10")),
            _execution("arn:x:ours", at("2026-10-23T22:00:10")),
            _execution("arn:x:older", at("2026-10-22T22:00:10")),
        ]
    }
    sfn.describe_execution.side_effect = lambda executionArn: {
        "input": json.dumps(
            {"topic_id": "github-trending" if executionArn == "arn:x:ours" else "someone-else"}
        )
    }

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    row = stored()
    assert row["shadow_mismatches"] == 0
    assert row["shadow_last_daily_dispatch_at"] == "2026-10-24T22:00:00+00:00"
    sfn.list_executions.assert_called_once_with(stateMachineArn=STATE_MACHINE_ARN, maxResults=100)
    described = [call.kwargs["executionArn"] for call in sfn.describe_execution.call_args_list]
    assert described == ["arn:x:theirs", "arn:x:ours"]  # yesterday's only, newest first, stopping when found
    sfn.start_execution.assert_not_called()


def test_a_shadow_daily_turn_counts_a_mismatch_when_the_previous_day_had_no_run(aws_resources, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [_execution("arn:x:theirs", at("2026-10-23T22:05:10"))]}
    sfn.describe_execution.return_value = {"input": json.dumps({"topic_id": "someone-else"})}

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_mismatches"] == 1
    assert "shadow daily mismatch topic_id=github-trending local_date=2026-10-24" in capsys.readouterr().out


def test_the_previous_day_s_executions_are_followed_across_pages(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    sfn.list_executions.side_effect = [
        {"executions": [_execution("arn:x:today", at("2026-10-24T22:00:10"))], "nextToken": "more"},
        {"executions": [_execution("arn:x:ours", at("2026-10-23T22:00:10"))]},
    ]
    sfn.describe_execution.return_value = {"input": json.dumps({"topic_id": "github-trending"})}

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_mismatches"] == 0
    assert sfn.list_executions.call_args_list[1].kwargs["nextToken"] == "more"


def test_a_check_that_could_not_run_counts_no_mismatch(aws_resources, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    sfn.list_executions.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "ListExecutions"
    )

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_mismatches"] == 0
    assert stored()["shadow_last_daily_dispatch_at"] == "2026-10-24T22:00:00+00:00"
    assert "could not check the executions of topic_id=github-trending" in capsys.readouterr().out


def test_the_first_shadow_day_has_nothing_to_compare_and_a_day_is_recorded_once(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    # A research heartbeat at :30, so neither tick below is a research turn and only the daily
    # turn is in play.
    put_topics(topic(dispatch_mode="schedule", research_cadence="cron(30 * * * ? *)"))
    sfn = MagicMock()

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))
    sfn.list_executions.assert_not_called()
    assert stored()["shadow_last_daily_dispatch_at"] == "2026-10-24T22:00:00+00:00"

    dispatcher(sfn=sfn).tick(at("2026-10-24T23:00:00"))  # later the same local day
    sfn.list_executions.assert_not_called()
    assert stored()["shadow_last_daily_dispatch_at"] == "2026-10-24T22:00:00+00:00"
    assert stored()["shadow_mismatches"] == 0
