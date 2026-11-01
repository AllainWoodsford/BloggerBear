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


def test_an_operator_s_research_message_is_forced_and_has_a_dedup_id_of_its_own():
    now = at("2026-10-25T03:04:05")

    assert dispatch.research_message("github-trending", now=now, force=True)["force"] is True
    assert dispatch.manual_research_dedup_id("github-trending", "ab12") == (
        "github-trending:research:manual:ab12"
    )
    # Never the dispatcher's id for the same minute, whatever the token.
    assert dispatch.manual_research_dedup_id("github-trending", "2026-10-25T03:04") != (
        dispatch.research_dedup_id("github-trending", now)
    )


@pytest.mark.parametrize(
    "topic_id",
    ["abc\n", "abc def", "", "a" * 80],
    ids=["trailing_newline", "space", "empty", "too_long"],
)
def test_a_topic_id_sqs_would_refuse_as_a_message_group_id_is_named(topic_id):
    assert "its topic id cannot be the work queue's message group id" in dispatch.queue_id_problem(topic_id)


def test_a_topic_id_with_punctuation_the_queue_takes_is_fine():
    assert dispatch.queue_id_problem("github-trending") is None
    assert dispatch.queue_id_problem("rail:sea/2.0") is None


def test_a_topic_id_longer_than_the_queue_s_ids_allow_is_refused_and_one_at_the_limit_is_not():
    # At 79 characters the manual deduplication id, the longest id built from it, is SQS's 128.
    assert len(dispatch.manual_research_dedup_id("a" * 79, "0" * dispatch.MANUAL_TOKEN_CHARS)) == 128
    assert dispatch.queue_id_problem("a" * 79) is None
    assert "at most 79 letters, digits and punctuation" in dispatch.queue_id_problem("a" * 80)


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


def test_releasing_a_daily_claim_puts_back_what_was_there_and_gives_the_number_back(aws_resources):
    """The retry after a failed start sends the same execution name, so Step Functions drops it if
    the failed start had reached it after all (spec 4.7)."""
    put_topics(topic())
    table = topics_table()
    dispatch.claim_daily(table, "github-trending", "2026-10-24")
    claim = dispatch.claim_daily(table, "github-trending", "2026-10-25")

    dispatch.release_daily(table, "github-trending", "2026-10-25", claim.previous)
    assert stored()["last_daily_dispatched_on"] == "2026-10-24" and stored()["daily_claim_n"] == 1
    assert dispatch.claim_daily(table, "github-trending", "2026-10-25") == Claim(previous="2026-10-24", n=2)

    put_topics(topic())
    first = dispatch.claim_daily(table, "github-trending", "2026-10-25")
    dispatch.release_daily(table, "github-trending", "2026-10-25", first.previous)
    assert "last_daily_dispatched_on" not in stored() and stored()["daily_claim_n"] == 0


def test_a_daily_release_is_one_conditional_update_that_gives_the_number_back():
    table = MagicMock()

    dispatch.release_daily(table, "github-trending", "2026-10-25", "2026-10-24")
    dispatch.release_daily(table, "github-trending", "2026-10-25", None)

    first, second = (call.kwargs for call in table.update_item.call_args_list)
    assert first["UpdateExpression"] == (
        "SET last_daily_dispatched_on = :previous ADD daily_claim_n :minus_one"
    )
    assert second["UpdateExpression"] == "REMOVE last_daily_dispatched_on ADD daily_claim_n :minus_one"
    for kwargs in (first, second):
        assert kwargs["ConditionExpression"] == "last_daily_dispatched_on = :today"
        assert kwargs["ExpressionAttributeValues"][":minus_one"] == -1


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
    assert row["daily_claim_n"] == 0  # given back: the retry sends the same name


# Written out rather than read from dispatch._TOPICS_OWN, so a code dropped from the set fails here.
@pytest.mark.parametrize("code", ["InvalidName", "InvalidExecutionInput", "ValidationException"])
def test_a_start_refused_for_the_topics_own_name_or_input_is_that_topics_problem_only(
    aws_resources, capsys, code
):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic("alpha", stagger_slot=1), topic("beta", stagger_slot=2))
    sfn = MagicMock()
    sfn.start_execution.side_effect = [
        ClientError({"Error": {"Code": code, "Message": "bad"}}, "StartExecution"),
        {"executionArn": "arn"},
    ]

    counts = dispatcher(sfn=sfn).tick(at("2026-10-24T23:30:00"))  # returns: no DispatchError

    assert counts["started"] == 1 and counts["considered"] == 2
    assert "last_daily_dispatched_on" not in stored("alpha")
    assert stored("alpha")["daily_claim_n"] == 0  # given back: the next try sends the same name
    assert stored("beta")["last_daily_dispatched_on"] == "2026-10-25"
    out = capsys.readouterr().out
    assert "dispatcher: topic_id=alpha skipped: ClientError(" in out and "start refused" not in out


def test_a_start_refused_for_an_infrastructure_reason_lets_the_other_topics_run_then_raises(
    aws_resources, capsys
):
    """AccessDenied on alpha's start is no fault of alpha's, and lasts until someone mends the role.
    Raised there and then, it would end the tick at alpha every minute, so beta's research and its
    daily start would never go out and last_tick would never move. The tick finishes every topic,
    writes last_tick, and raises after the loop, so the Errors alarm still fires."""
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic("alpha", stagger_slot=1), topic("beta", stagger_slot=2))
    sqs, sfn = MagicMock(), MagicMock()
    sfn.start_execution.side_effect = [
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "StartExecution"),
        {"executionArn": "arn"},
    ]
    now = at("2026-10-24T23:00:00")  # 10:00 Sydney: both research turns and both daily turns

    with pytest.raises(DispatchError, match="alpha"):
        dispatcher(sqs=sqs, sfn=sfn).tick(now)

    assert "last_daily_dispatched_on" not in stored("alpha")  # released: the next tick tries again
    assert stored("beta")["last_daily_dispatched_on"] == "2026-10-25"
    assert [call.kwargs["MessageGroupId"] for call in sqs.send_message.call_args_list] == ["alpha", "beta"]
    assert pipeline_row()["last_tick"] == "2026-10-24T23:00:00+00:00"
    out = capsys.readouterr().out
    assert "dispatcher: topic_id=alpha start refused: StartRefused(" in out and "AccessDeniedException" in out


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


def test_a_topic_flipped_to_the_queue_after_its_shadowed_turn_starts_nothing_that_day(aws_resources):
    """Cutover day: shadow mode recorded today's 09:10 turn while the Scheduler still owned it, then
    the topic was flipped. A tick at 15:10 starts no second cycle; the next day's turn starts one."""
    put_topics(topic(shadow_last_daily_dispatch_at=DAILY_TURN.isoformat()))  # dispatch_mode queue
    sfn = MagicMock()
    run = dispatcher(sfn=sfn)

    assert run.tick(DAILY_TURN + timedelta(hours=6))["started"] == 0  # 15:10 Sydney
    sfn.start_execution.assert_not_called()
    assert "last_daily_dispatched_on" not in stored()

    assert run.tick(DAILY_TURN + timedelta(days=1))["started"] == 1
    assert sfn.start_execution.call_args.kwargs["name"] == "github-trending-2026-10-26-1"


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


def _every_minute(first: datetime, last: datetime) -> list[datetime]:
    ticks = [first]
    while ticks[-1] + timedelta(minutes=1) <= last:
        ticks.append(ticks[-1] + timedelta(minutes=1))
    return ticks


def _replay(run: Dispatcher, *, ticks, stamps=(), topic_id="github-trending") -> list[str]:
    """Tick the dispatcher at each of `ticks` and, in time order among them, let the Scheduler
    path's research tick stamp last_research_at (its run_started) at each of `stamps`. Returns
    the research turns shadow mode recorded, in order."""
    recorded: list[str] = []
    for when, is_stamp in sorted([(t, False) for t in ticks] + [(s, True) for s in stamps]):
        if is_stamp:
            topics_table().update_item(
                Key={"topic_id": topic_id},
                UpdateExpression="SET last_research_at = :t",
                ExpressionAttributeValues={":t": when.isoformat()},
            )
            continue
        run.tick(when)
        value = stored(topic_id).get("shadow_last_research_dispatch_at")
        if value and (not recorded or recorded[-1] != value):
            recorded.append(value)
    return recorded


def test_neither_order_of_the_two_ticks_in_a_turn_minute_counts_a_mismatch(aws_resources):
    """The Scheduler path's research tick (cron(5 * * * ? *) for slot 1) and the dispatcher fire in
    the same minute, in either order: at 10:05 the dispatcher reads first, at 11:05 the stamp lands
    first, at 12:05 the dispatcher again. Every turn is recorded and none is a mismatch (comparing
    at the next recorded turn counted 12:05 against 10:05's record)."""
    put_topics(topic(dispatch_mode="schedule", last_research_at=at("2026-10-25T09:05:02").isoformat()))

    recorded = _replay(
        dispatcher(),
        ticks=_every_minute(at("2026-10-25T10:04:03"), at("2026-10-25T12:12:03")),
        stamps=[at("2026-10-25T10:05:05"), at("2026-10-25T11:05:01"), at("2026-10-25T12:05:05")],
    )

    assert recorded == [
        "2026-10-25T10:05:03+00:00",
        "2026-10-25T11:05:03+00:00",
        "2026-10-25T12:05:03+00:00",
    ]
    assert stored()["shadow_mismatches"] == 0


def test_a_turn_the_scheduler_path_already_stamped_is_recorded_without_a_mismatch(aws_resources):
    hour_before = RESEARCH_TURN - timedelta(hours=1)
    put_topics(
        topic(
            dispatch_mode="schedule",
            shadow_last_research_dispatch_at=dispatch._iso(hour_before),
            last_research_at=(RESEARCH_TURN - timedelta(seconds=20)).isoformat(),
        )
    )
    run = dispatcher()

    run.tick(RESEARCH_TURN)  # 22:05:30; the stamp landed at 22:05:10

    assert stored()["shadow_last_research_dispatch_at"] == "2026-10-24T22:05:30+00:00"
    assert stored()["shadow_mismatches"] == 0

    # The next turn, the dispatcher first and the stamp left as it was: still none.
    run.tick(RESEARCH_TURN + timedelta(hours=1))
    assert stored()["shadow_last_research_dispatch_at"] == "2026-10-24T23:05:30+00:00"
    assert stored()["shadow_mismatches"] == 0


def test_a_turn_the_scheduler_path_never_took_is_one_mismatch_even_when_the_next_stamp_lands_first(
    aws_resources, capsys
):
    """10:05 is recorded and nothing stamps it; at 11:05 the stamp lands before the dispatcher
    reads. The 10:05 turn is judged at 10:10, before 11:05's stamp could hide it."""
    put_topics(topic(dispatch_mode="schedule", last_research_at=at("2026-10-25T09:05:01").isoformat()))

    recorded = _replay(
        dispatcher(),
        ticks=_every_minute(at("2026-10-25T10:04:30"), at("2026-10-25T11:12:30")),
        stamps=[at("2026-10-25T11:05:01")],
    )

    assert recorded == ["2026-10-25T10:05:30+00:00", "2026-10-25T11:05:30+00:00"]
    assert stored()["shadow_mismatches"] == 1
    assert (
        "dispatcher: shadow research mismatch topic_id=github-trending turn=2026-10-25T10:05:30+00:00 "
        "last_research_at=2026-10-25T09:05:01+00:00"
    ) in capsys.readouterr().out


def test_a_recorded_turn_is_judged_once_five_minutes_on_against_when_last_research_at_moved(aws_resources):
    turn = at("2026-10-25T04:05:30")

    def row(name, **fields):
        recorded = {"shadow_last_research_dispatch_at": turn.isoformat()}
        return topic(name, dispatch_mode="schedule", **recorded, **fields)

    put_topics(
        row("agreed", last_research_at=(turn + timedelta(seconds=40)).isoformat()),
        row("stamped-first", last_research_at=(turn - timedelta(seconds=20)).isoformat()),
        row("not-moved", last_research_at=(turn - timedelta(hours=1)).isoformat()),
        row("early", last_research_at=(turn - timedelta(minutes=7)).isoformat()),
        row("never"),
        # The Scheduler path's tick took the turn, but its fetch failed, so only its attempt moved.
        row(
            "fetch-failed",
            last_research_at=(turn - timedelta(hours=1)).isoformat(),
            last_research_attempt_at=(turn + timedelta(seconds=40)).isoformat(),
        ),
        # Its attempt is last hour's: this turn was never taken.
        row(
            "attempted-last-hour",
            last_research_at=(turn - timedelta(hours=2)).isoformat(),
            last_research_attempt_at=(turn - timedelta(hours=1)).isoformat(),
        ),
    )
    dynamo.update_config_row("pipeline", {"last_tick": (turn + timedelta(minutes=4)).isoformat()})
    run = dispatcher()

    run.tick(turn + timedelta(minutes=5))
    run.tick(turn + timedelta(minutes=6))  # judged once: the next window no longer holds it

    expected = {"agreed": 0, "stamped-first": 0, "not-moved": 1, "early": 1, "never": 1,
                "fetch-failed": 0, "attempted-last-hour": 1}  # fmt: skip
    assert {name: stored(name).get("shadow_mismatches", 0) for name in expected} == expected
    # Each judged turn is dated on its row: what the cutover's pre-flight compares.
    fields = ("shadow_research_matched_at", "shadow_research_mismatch_at")
    names = ("agreed", "stamped-first", "never")
    dated = {name: tuple(stored(name).get(field) for field in fields) for name in names}
    assert dated == {
        "agreed": ("2026-10-25T04:05:30+00:00", None),
        "stamped-first": ("2026-10-25T04:05:30+00:00", None),
        "never": (None, "2026-10-25T04:05:30+00:00"),
    }


def test_a_run_now_that_lands_after_the_turns_tolerance_is_never_a_mismatch(aws_resources, capsys):
    """An operator's run-now stamped 5 min 5 s after the 04:05:30 turn, just before the tick that
    judges it: the turn's own evidence is gone, and a run-now is never counted."""
    turn = at("2026-10-25T04:05:30")
    put_topics(
        topic(
            dispatch_mode="schedule",
            shadow_last_research_dispatch_at=turn.isoformat(),
            last_research_at=(turn + timedelta(minutes=5, seconds=5)).isoformat(),
        )
    )
    dynamo.update_config_row("pipeline", {"last_tick": (turn + timedelta(minutes=4, seconds=40)).isoformat()})

    dispatcher().tick(turn + timedelta(minutes=5, seconds=10))

    assert stored().get("shadow_mismatches", 0) == 0
    assert (
        "dispatcher: shadow research turn not judged topic_id=github-trending turn=2026-10-25T04:05:30+00:00 "
        "last_research_at=2026-10-25T04:10:35+00:00: stamped since by a run-now"
    ) in capsys.readouterr().out


def test_a_run_now_whose_fetch_failed_after_the_turns_tolerance_is_never_a_mismatch(aws_resources, capsys):
    """A forced run-now at 04:11:30 whose source failed stamps only its attempt, after the 04:05:30
    turn's tolerance and before the tick that judges it: the turn's own evidence is gone, and a
    run-now is never counted, whichever of the two stamps it moved."""
    turn = at("2026-10-25T04:05:30")
    put_topics(
        topic(
            dispatch_mode="schedule",
            shadow_last_research_dispatch_at=turn.isoformat(),
            last_research_at=(turn - timedelta(hours=1)).isoformat(),
            last_research_attempt_at=(turn + timedelta(minutes=6)).isoformat(),
        )
    )
    dynamo.update_config_row("pipeline", {"last_tick": (turn + timedelta(minutes=4)).isoformat()})

    dispatcher().tick(turn + timedelta(minutes=8))

    row = stored()
    assert row.get("shadow_mismatches", 0) == 0
    assert "shadow_research_matched_at" not in row and "shadow_research_mismatch_at" not in row
    out = capsys.readouterr().out
    assert (
        "dispatcher: shadow research turn not judged topic_id=github-trending turn=2026-10-25T04:05:30+00:00 "
        "last_research_at=2026-10-25T03:05:30+00:00 last_research_attempt_at=2026-10-25T04:11:30+00:00: "
        "stamped since by a run-now"
    ) in out
    assert "shadow research mismatch" not in out


def test_a_run_now_the_hour_before_or_inside_the_tolerance_is_never_a_mismatch(aws_resources):
    """A run-now at 09:50 leaves the 10:05 turn not due (and unrecorded, so unjudged); one at 10:07,
    after the Scheduler path stamped 10:05, is within the turn's tolerance."""
    put_topics(topic(dispatch_mode="schedule", last_research_at=at("2026-10-25T09:05:02").isoformat()))

    _replay(
        dispatcher(),
        ticks=_every_minute(at("2026-10-25T09:49:30"), at("2026-10-25T11:12:30")),
        stamps=[at("2026-10-25T09:50:10"), at("2026-10-25T11:05:04"), at("2026-10-25T11:07:40")],
    )

    assert stored()["shadow_mismatches"] == 0
    assert stored()["shadow_last_research_dispatch_at"] == "2026-10-25T11:05:30+00:00"


def test_a_shadow_research_turn_is_not_recorded_while_the_interval_runs(aws_resources):
    # Ten minutes since the last check: not due, and too long ago to be this turn's own stamp.
    put_topics(
        topic(dispatch_mode="schedule", last_research_at=(RESEARCH_TURN - timedelta(minutes=10)).isoformat())
    )

    dispatcher().tick(RESEARCH_TURN)

    assert "shadow_last_research_dispatch_at" not in stored()


def test_a_rate_cadence_is_recorded_once_an_interval_and_never_judged(aws_resources):
    """With the stagger off, rate(1 hour) makes every minute a turn and is_due true from 55 minutes
    on, exactly DUE_TOLERANCE before the Scheduler's next heartbeat (a minute the expression does
    not say). Shadow mode records the minute the live claim would take, once an interval, and
    counts nothing: the comparison would sit on the tolerance edge (here 10:55:03 against
    11:00:05)."""
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", last_research_at=at("2026-10-25T10:00:02").isoformat()))

    recorded = _replay(
        dispatcher(),
        ticks=_every_minute(at("2026-10-25T10:50:03"), at("2026-10-25T12:10:03")),
        stamps=[at("2026-10-25T11:00:05"), at("2026-10-25T12:00:04")],
    )

    assert recorded == ["2026-10-25T10:55:03+00:00", "2026-10-25T11:56:03+00:00"]
    assert stored()["shadow_mismatches"] == 0


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
    assert row["shadow_mismatches"] == 0 and row["shadow_unchecked"] == 0
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


def test_the_check_stops_at_the_first_execution_older_than_the_day(aws_resources, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    # The same page every call: a walk that did not stop would read on until the pages ran out.
    sfn.list_executions.return_value = {
        "executions": [
            _execution("arn:x:today", at("2026-10-24T22:00:10")),
            _execution("arn:x:older", at("2026-10-22T22:00:10")),
        ],
        "nextToken": "more",
    }

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_mismatches"] == 1
    sfn.list_executions.assert_called_once()
    sfn.describe_execution.assert_not_called()  # neither execution started on the day asked about
    assert "shadow daily mismatch topic_id=github-trending local_date=2026-10-24" in capsys.readouterr().out


def test_a_check_that_runs_out_of_pages_counts_an_unchecked_day_not_a_mismatch(aws_resources, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    sfn.list_executions.return_value = {
        "executions": [_execution("arn:x:today", at("2026-10-24T22:00:10"))],
        "nextToken": "more",
    }

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_mismatches"] == 0 and stored()["shadow_unchecked"] == 1
    assert stored()["shadow_last_daily_dispatch_at"] == "2026-10-24T22:00:00+00:00"
    assert sfn.list_executions.call_count == dispatch._MAX_EXECUTION_PAGES == 10
    out = capsys.readouterr().out
    assert "execution check ran out of pages topic_id=github-trending" in out
    assert "dispatcher: shadow daily unchecked topic_id=github-trending local_date=2026-10-24" in out


def test_a_check_that_could_not_run_counts_an_unchecked_day_not_a_mismatch(aws_resources, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    sfn.list_executions.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "ListExecutions"
    )

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_mismatches"] == 0 and stored()["shadow_unchecked"] == 1
    assert stored()["shadow_last_daily_dispatch_at"] == "2026-10-24T22:00:00+00:00"
    assert "could not check the executions of topic_id=github-trending" in capsys.readouterr().out


def test_a_missing_grant_never_passes_as_zero_mismatches_each_day_counts_as_unchecked(aws_resources):
    """Without states:ListExecutions every check is refused. Three shadow days later the topic shows
    zero mismatches but two unchecked days (the first day has nothing to compare), which the
    cutover's pre-flight refuses; a check that answers adds nothing. (Researched for the week, so
    only the daily turn is in play.)"""
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(
        topic(
            dispatch_mode="schedule",
            research_interval_hours=168,
            last_research_at=at("2026-10-23T21:00:00").isoformat(),
        )
    )
    sfn = MagicMock()
    sfn.list_executions.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "ListExecutions"
    )
    run = dispatcher(sfn=sfn)

    for day in range(3):
        run.tick(at("2026-10-23T22:00:00") + timedelta(days=day))

    assert stored()["shadow_mismatches"] == 0 and stored()["shadow_unchecked"] == 2

    sfn.list_executions.side_effect = None
    sfn.list_executions.return_value = {"executions": [_execution("arn:x:ours", at("2026-10-25T22:00:10"))]}
    sfn.describe_execution.return_value = {"input": json.dumps({"topic_id": "github-trending"})}
    run.tick(at("2026-10-26T22:00:00"))
    assert stored()["shadow_mismatches"] == 0 and stored()["shadow_unchecked"] == 2


def test_the_first_shadow_day_already_shows_no_unchecked_days(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", research_cadence="cron(30 * * * ? *)"))

    dispatcher().tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_unchecked"] == 0 and stored()["shadow_mismatches"] == 0


def test_the_first_shadow_day_is_no_checked_day_since_it_had_nothing_to_compare(aws_resources):
    """Zero mismatches and zero unchecked days after one shadow turn would pass for a clean day:
    shadow_checked_days says no day was checked yet, which the cutover's pre-flight refuses."""
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", research_cadence="cron(30 * * * ? *)"))

    dispatcher().tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_checked_days"] == 0


@pytest.mark.parametrize(
    ("ran_for", "checked", "mismatches"),
    [("github-trending", 1, 0), ("someone-else", 1, 1)],
    ids=["the_day_had_its_run", "the_day_had_no_run"],
)
def test_a_daily_check_that_answers_counts_a_checked_day_whatever_it_found(
    aws_resources, ran_for, checked, mismatches
):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [_execution("arn:x:run", at("2026-10-23T22:00:10"))]}
    sfn.describe_execution.return_value = {"input": json.dumps({"topic_id": ran_for})}

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    row = stored()
    assert row["shadow_checked_days"] == checked
    assert row["shadow_mismatches"] == mismatches and row["shadow_unchecked"] == 0


def test_a_daily_check_that_could_not_run_is_no_checked_day(aws_resources):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(topic(dispatch_mode="schedule", shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat()))
    sfn = MagicMock()
    sfn.list_executions.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "ListExecutions"
    )

    dispatcher(sfn=sfn).tick(at("2026-10-24T22:00:00"))

    assert stored()["shadow_checked_days"] == 0 and stored()["shadow_unchecked"] == 1


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


# --- the recent stretch the cutover judges ---------------------------------------------------------


def _shadowed_days(sfn, *, days: int, **fields) -> list[dict]:
    """Tick the 09:00 Sydney daily turn of `days` days from 2026-10-24 for a topic on its schedules,
    researched for the week with a research turn already judged on time (so only the daily turn is
    in play), and return the row after each day."""
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(
        topic(
            dispatch_mode="schedule",
            research_cadence="cron(30 * * * ? *)",
            research_interval_hours=168,
            last_research_at=at("2026-10-23T20:30:10").isoformat(),
            shadow_last_research_dispatch_at=at("2026-10-23T20:30:00").isoformat(),
            shadow_research_matched_at=at("2026-10-23T20:30:00").isoformat(),
            **fields,
        )
    )
    run, rows = dispatcher(sfn=sfn), []
    for day in range(days):
        run.tick(YESTERDAY_TURN + timedelta(days=day))
        rows.append(stored())
    return rows


def test_a_mismatch_day_starts_the_clean_count_again_and_the_next_matching_day_counts_one(aws_resources):
    """Day one has nothing to compare; day two finds no run on the Scheduler path (a mismatch); day
    three finds one. The history keeps the mismatch, and the pre-flight passes on day three."""
    sfn = MagicMock()
    # One run on the Scheduler path, on 2026-10-25 (Sydney): none on the 24th.
    sfn.list_executions.return_value = {"executions": [_execution("arn:x:ours", at("2026-10-24T22:00:10"))]}
    sfn.describe_execution.return_value = {"input": json.dumps({"topic_id": "github-trending"})}

    first, second, third = _shadowed_days(sfn, days=3)

    assert "shadow_clean_days" not in first and first["shadow_checked_days"] == 0
    assert (second["shadow_mismatches"], second["shadow_clean_days"]) == (1, 0)
    assert dispatch.shadow_problems(second, research_is_rate=False) == [
        "shadow mode has not watched a full day of it since its last mismatch or unchecked day "
        "(1 mismatch and 0 unchecked days in all; one clean day checked is enough, and "
        "`topics schedule` shows the record)"
    ]
    assert (third["shadow_mismatches"], third["shadow_checked_days"], third["shadow_clean_days"]) == (1, 2, 1)
    assert dispatch.shadow_problems(third, research_is_rate=False) == []


def test_an_unchecked_day_starts_the_clean_count_again(aws_resources):
    sfn = MagicMock()
    sfn.list_executions.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "ListExecutions"
    )

    rows = _shadowed_days(sfn, days=2, shadow_checked_days=4, shadow_clean_days=4)

    assert (rows[-1]["shadow_unchecked"], rows[-1]["shadow_clean_days"]) == (1, 0)
    assert dispatch.shadow_problems(rows[-1], research_is_rate=False)


def test_matching_days_add_up_and_a_row_counted_before_clean_days_reads_as_clean_without_a_problem(
    aws_resources,
):
    sfn = MagicMock()
    sfn.list_executions.return_value = {  # a run on the 24th and on the 25th (Sydney), newest first
        "executions": [
            _execution("arn:x:second", at("2026-10-24T22:00:10")),
            _execution("arn:x:first", at("2026-10-23T22:00:10")),
        ]
    }
    sfn.describe_execution.return_value = {"input": json.dumps({"topic_id": "github-trending"})}

    rows = _shadowed_days(sfn, days=3)

    assert [row.get("shadow_clean_days") for row in rows] == [None, 1, 2]
    legacy = {"shadow_mismatches": 0, "shadow_unchecked": 0, "shadow_checked_days": 3,
              "shadow_research_matched_at": "2026-10-24T20:30:00+00:00"}  # fmt: skip
    assert dispatch.shadow_problems(legacy, research_is_rate=False) == []
    assert dispatch.shadow_problems({**legacy, "shadow_mismatches": 1}, research_is_rate=False)


def test_a_research_mismatch_starts_the_clean_count_again_and_holds_the_cutover_until_a_turn_is_on_time(
    aws_resources, capsys
):
    """A drifted heartbeat is judged once, and when its interval matches its cadence its later
    turns are neither recorded nor judged: the mismatch is dated, and the cutover waits for a
    research turn judged on time since, which only a heartbeat back on its minute gives."""
    turn = at("2026-10-25T04:05:30")
    put_topics(
        topic(
            dispatch_mode="schedule",
            shadow_last_research_dispatch_at=turn.isoformat(),
            last_research_at=(turn - timedelta(minutes=12)).isoformat(),  # the drifted heartbeat
            shadow_research_matched_at=(turn - timedelta(hours=1)).isoformat(),
            shadow_mismatches=0,
            shadow_unchecked=0,
            shadow_checked_days=3,
            shadow_clean_days=3,
        )
    )
    dynamo.update_config_row("pipeline", {"last_tick": (turn + timedelta(minutes=4)).isoformat()})

    dispatcher().tick(turn + timedelta(minutes=5))

    row = stored()
    assert (row["shadow_mismatches"], row["shadow_clean_days"]) == (1, 0)
    assert row["shadow_research_mismatch_at"] == "2026-10-25T04:05:30+00:00"
    assert row["shadow_last_research_dispatch_at"] == "2026-10-25T04:05:30+00:00"  # the record stays
    assert "shadow research mismatch topic_id=github-trending" in capsys.readouterr().out
    reasons = dispatch.shadow_problems(row, research_is_rate=False)
    assert len(reasons) == 2 and reasons[1].startswith(
        "shadow mode has seen none of its research turns taken on time since its last research "
        "mismatch, the turn at 2026-10-25T04:05:30+00:00"
    )
    # A turn judged on time since answers the research half; a clean day still has to be checked.
    later = {**row, "shadow_research_matched_at": "2026-10-25T05:05:30+00:00"}
    assert len(dispatch.shadow_problems(later, research_is_rate=False)) == 1
    assert dispatch.shadow_problems({**later, "shadow_clean_days": 1}, research_is_rate=False) == []


def _executions_daily(*first_days: datetime) -> MagicMock:
    """A Step Functions client that lists one run of this topic at each of `first_days` (newest
    first, as Step Functions lists them)."""
    sfn = MagicMock()
    newest_first = sorted(first_days, reverse=True)
    sfn.list_executions.return_value = {
        "executions": [_execution(f"arn:x:{n}", moment) for n, moment in enumerate(newest_first)]
    }
    sfn.describe_execution.return_value = {"input": json.dumps({"topic_id": "github-trending"})}
    return sfn


def _refusals() -> list[str]:
    row = stored()
    return dispatch.cutover_refusals(row, topics=[row], config=pipeline_row())


def test_a_heartbeat_drifted_late_on_a_cadence_longer_than_its_interval_never_passes_the_pre_flight(
    aws_resources,
):
    """Every four hours with a two-hour interval, the Scheduler's heartbeat ten minutes late: each
    turn finds the topic due, is recorded, and is judged a mismatch five minutes later. No day,
    record to record, is free of one, so no clean day is ever counted, and the pre-flight refuses
    at every moment, the minute after a turn is recorded included (counting the daily check alone
    once let it through there)."""
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(
        topic(
            dispatch_mode="schedule",
            research_cadence="cron(0 */4 * * ? *)",
            research_interval_hours=2,
            research_schedule="cron(0 */4 * * ? *)",
            daily_schedule="cron(0 9 * * ? *)",
            last_research_at=at("2026-10-23T20:10:00").isoformat(),
            shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat(),
            shadow_last_research_dispatch_at=at("2026-10-23T20:00:30").isoformat(),
            shadow_research_matched_at=at("2026-10-23T16:00:30").isoformat(),  # before the drift
            shadow_research_mismatch_at=at("2026-10-23T20:00:30").isoformat(),  # the drift's first turn
            shadow_mismatches=1,
            shadow_unchecked=0,
            shadow_checked_days=3,
            shadow_clean_days=0,
        )
    )
    dynamo.update_config_row("pipeline", {"last_tick": at("2026-10-23T23:59:30").isoformat()})
    run = dispatcher(sfn=_executions_daily(*(at(f"2026-10-{day}T22:00:40") for day in (23, 24, 25, 26))))
    seen = []

    recorded, minute_after, judged = (timedelta(seconds=s) for s in (30, 90, 330))
    for hour in range(0, 3 * 24 + 1):
        turn = at("2026-10-24T00:00:00") + timedelta(hours=hour)
        if turn.hour % 4 == 0:
            for offset in (recorded, minute_after, judged):
                run.tick(turn + offset)
                seen.append((turn + offset, stored().get("shadow_clean_days"), _refusals()))
            topics_table().update_item(  # the Scheduler path's heartbeat, ten minutes late
                Key={"topic_id": "github-trending"},
                UpdateExpression="SET last_research_at = :t",
                ExpressionAttributeValues={":t": (turn + timedelta(minutes=10)).isoformat()},
            )
        if turn.hour == 22:  # 09:00 Sydney: the daily turn, and the minute after it
            for offset in (recorded, minute_after):
                run.tick(turn + offset)
                seen.append((turn + offset, stored().get("shadow_clean_days"), _refusals()))

    assert stored()["shadow_checked_days"] == 6 and stored()["shadow_mismatches"] == 20
    assert [moment for moment, clean, _ in seen if clean] == []
    assert [moment for moment, _, refusals in seen if not refusals] == []


def test_one_missed_heartbeat_holds_the_cutover_until_a_full_day_after_it_is_checked_clean(aws_resources):
    """Hourly research, the Scheduler's 20:00 UTC heartbeat missed (a mismatch at 20:05), every
    other turn on time, and the daily turn at 22:00 UTC (09:00 Sydney). That day's 22:00 check
    finds yesterday's run, but its day, record to record, held the mismatch: no clean day, and the
    pre-flight refuses even with the 22:00 research turn recorded and judged on time. The next
    day's check stands for a day with none, and counts the first clean day."""
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(
        topic(
            dispatch_mode="schedule",
            research_cadence="cron(0 * * * ? *)",
            research_interval_hours=1,
            research_schedule="cron(0 * * * ? *)",
            daily_schedule="cron(0 9 * * ? *)",
            last_research_at=at("2026-10-24T19:00:05").isoformat(),
            shadow_last_research_dispatch_at=at("2026-10-24T19:00:30").isoformat(),
            shadow_research_matched_at=at("2026-10-24T19:00:30").isoformat(),
            shadow_last_daily_dispatch_at=YESTERDAY_TURN.isoformat(),
            shadow_mismatches=0,
            shadow_unchecked=0,
            shadow_checked_days=3,
            shadow_clean_days=3,
        )
    )
    dynamo.update_config_row("pipeline", {"last_tick": at("2026-10-24T19:05:30").isoformat()})
    run = dispatcher(sfn=_executions_daily(at("2026-10-23T22:00:40"), at("2026-10-24T22:00:40")))
    after = {}

    for hour in range(1, 28):  # 20:00 on the 24th to 22:00 on the 25th
        turn = at("2026-10-24T19:00:00") + timedelta(hours=hour)
        if turn != at("2026-10-24T20:00:00"):  # the heartbeat the Scheduler missed
            topics_table().update_item(
                Key={"topic_id": "github-trending"},
                UpdateExpression="SET last_research_at = :t",
                ExpressionAttributeValues={":t": (turn + timedelta(seconds=5)).isoformat()},
            )
        for offset in (timedelta(seconds=30), timedelta(minutes=5, seconds=30)):
            run.tick(turn + offset)
            after[turn + offset] = (stored(), _refusals())

    missed, _ = after[at("2026-10-24T20:05:30")]
    assert (missed["shadow_mismatches"], missed["shadow_clean_days"]) == (1, 0)
    for moment in (at("2026-10-24T22:00:30"), at("2026-10-24T22:05:30")):
        row, refusals = after[moment]
        assert (row["shadow_checked_days"], row["shadow_clean_days"]) == (4, 0)
        assert [reason[:46] for reason in refusals] == ["shadow mode has not watched a full day of it s"]
    held = [refusals for moment, (_, refusals) in after.items() if moment < at("2026-10-25T22:00:30")]
    assert all(held[1:])  # from the tick that judged the miss on (20:00:30 could not know of it yet)
    row, refusals = after[at("2026-10-25T22:00:30")]
    assert (row["shadow_mismatches"], row["shadow_checked_days"], row["shadow_clean_days"]) == (1, 5, 1)
    assert refusals == []


def test_a_research_mismatch_judged_in_the_daily_check_s_own_tick_counts_no_clean_day(aws_resources):
    """With one topic and the default stagger, the 09:05 Sydney heartbeat is judged at 09:10, the
    minute the daily check runs. The check reads the row as the tick found it, so it is handed the
    mismatch just counted: setting the count to 0 and then adding the day would read 1 straight
    after a mismatch."""
    turn = at("2026-10-24T22:05:00")
    put_topics(
        topic(
            dispatch_mode="schedule",
            shadow_last_research_dispatch_at=turn.isoformat(),
            last_research_at=(turn - timedelta(hours=1)).isoformat(),  # the 09:05 heartbeat missed
            shadow_research_matched_at=(turn - timedelta(hours=1)).isoformat(),
            shadow_last_daily_dispatch_at=at("2026-10-23T22:10:00").isoformat(),
            shadow_mismatches=0,
            shadow_unchecked=0,
            shadow_checked_days=3,
            shadow_clean_days=3,
        )
    )
    dynamo.update_config_row("pipeline", {"last_tick": (DAILY_TURN - timedelta(minutes=1)).isoformat()})
    run = dispatcher(sfn=_executions_daily(at("2026-10-23T22:10:10")))

    run.tick(DAILY_TURN)

    row = stored()
    assert (row["shadow_mismatches"], row["shadow_checked_days"], row["shadow_clean_days"]) == (1, 4, 0)
    # The next heartbeat lands on time and its turn is recorded: still no clean day to cut over on.
    topics_table().update_item(
        Key={"topic_id": "github-trending"},
        UpdateExpression="SET last_research_at = :t",
        ExpressionAttributeValues={":t": (turn + timedelta(hours=1, seconds=5)).isoformat()},
    )
    run.tick(turn + timedelta(hours=1, seconds=30))
    row = stored()
    assert row["shadow_last_research_dispatch_at"] == "2026-10-24T23:05:30+00:00"
    assert dispatch.shadow_problems(row, research_is_rate=False)[0].startswith(
        "shadow mode has not watched a full day of it since its last mismatch"
    )


# --- a daily start whose answer was lost ------------------------------------------------------------


class _StepFunctions:
    """Step Functions as StartExecution answers a name: a new name starts a run; the same name and
    input while that run is RUNNING returns success and starts nothing; any other repeat of a name
    is ExecutionAlreadyExists. The first `lose_responses` calls create the run and then fail, as a
    request that reached Step Functions but whose answer never came back (a 5xx after botocore's
    retries, a dropped connection)."""

    def __init__(self, lose_responses: int, error: Exception):
        self.runs: dict[str, str] = {}
        self.lose_responses = lose_responses
        self.error = error

    def start_execution(self, *, stateMachineArn, name, input):
        if name in self.runs:
            if self.runs[name] == input:
                return {"executionArn": f"{stateMachineArn}:{name}"}  # RUNNING, same input
            exists = {"Error": {"Code": "ExecutionAlreadyExists", "Message": name}}
            raise ClientError(exists, "StartExecution")
        self.runs[name] = input
        if self.lose_responses:
            self.lose_responses -= 1
            raise self.error
        return {"executionArn": f"{stateMachineArn}:{name}"}


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "try later"}}, "StartExecution"),
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "StartExecution"),
        ConnectionError("connection reset after the request was sent"),
    ],
    ids=["5xx", "throttled_on_retry", "connection"],
)
def test_a_start_that_reached_step_functions_but_failed_is_not_run_twice_on_the_retry(aws_resources, error):
    """Spec 4.7: the execution name is the daily start's idempotency (the same name while RUNNING
    is success; an existing name is consumed). The release gives the claim's number back, so the
    retry sends the same name, and a start whose answer was lost does not run the day twice."""
    put_topics(topic())
    sfn = _StepFunctions(lose_responses=1, error=error)

    try:
        dispatcher(sfn=sfn).tick(DAILY_TURN)  # the run is created, the answer is lost, the day released
    except DispatchError:
        pass
    assert "last_daily_dispatched_on" not in stored()  # released: the next tick tries again

    dispatcher(sfn=sfn).tick(DAILY_TURN + timedelta(minutes=1))

    assert list(sfn.runs) == ["github-trending-2026-10-25-1"]  # one daily run for the day
    assert stored()["last_daily_dispatched_on"] == "2026-10-25"
    assert stored()["daily_claim_n"] == 1
