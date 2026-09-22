"""Tests for common/stats_tracking.py: Bedrock usage and reader activity that isn't part of any
one article's lineage, rolled onto this week's StatsCurrent row.

Same relationship to common/bedrock.py's invoke_model_tracked as invoke_model_tracked itself has to
plain invoke_claude (test_bedrock.py owns that layer): callers of tracked_claude elsewhere in this
codebase just patch it directly and trust it, the way callers of invoke_model_tracked trust that.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from common import stats_tracking as st

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("STATS_CURRENT_TABLE", "StatsCurrent")
    monkeypatch.setenv("MODELS_TABLE", "Models")
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture
def table():
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="StatsCurrent",
            KeySchema=[{"AttributeName": "stats_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "stats_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Models",
            KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "model_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table("StatsCurrent")


def _put_model(model_id="model-a", input_price=1.0, output_price=2.0):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Models")
    table.put_item(
        Item={
            "model_id": model_id,
            "display_name": model_id,
            "input_price_usd_per_1k_tokens": Decimal(str(input_price)),
            "output_price_usd_per_1k_tokens": Decimal(str(output_price)),
        }
    )


def _tracked_result(text="hi", model_id="model-a", input_tokens=100, output_tokens=50):
    return {
        "text": text,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "used_fallback": False,
        "stop_reason": "end_turn",
        "attempts": 1,
    }


def _row(t):
    return {k: v for k, v in t.get_item(Key={"stats_id": "current"}).get("Item", {}).items()}


# --- the current week's Monday --------------------------------------------------------------


@pytest.mark.parametrize(
    "today, expected",
    [
        (date(2026, 9, 21), "2026-09-21"),  # a Monday: itself
        (date(2026, 9, 22), "2026-09-21"),  # Tuesday
        (date(2026, 9, 27), "2026-09-21"),  # Sunday: still last Monday
        (date(2026, 9, 28), "2026-09-28"),  # the next Monday
        (date(2026, 3, 4), "2026-03-02"),  # crosses a month boundary
    ],
)
def test_current_week_start_is_always_the_monday_on_or_before_today(today, expected):
    assert st._current_week_start(today) == expected


# --- tracked_claude: returns the text, tallies the call ---------------------------------------


def test_tracked_claude_returns_the_models_text(table):
    _put_model()
    with patch("common.stats_tracking.invoke_model_tracked", return_value=_tracked_result("Hello!")):
        assert st.tracked_claude("musings", "prompt", "model-a") == "Hello!"


def test_a_priced_call_tallies_calls_tokens_and_cost(table):
    _put_model("model-a", input_price=1.0, output_price=2.0)
    result = _tracked_result(model_id="model-a", input_tokens=1000, output_tokens=500)
    with patch("common.stats_tracking.invoke_model_tracked", return_value=result):
        st.tracked_claude("musings", "prompt", "model-a")

    row = _row(table)
    assert row["musings_calls"] == 1
    assert row["musings_input_tokens"] == 1000
    assert row["musings_output_tokens"] == 500
    # 1000/1000 * $1.00 + 500/1000 * $2.00 = $2.00 USD * 1.50 AUD/USD = $3.00 AUD
    assert row["musings_cost_aud"] == Decimal("3") or abs(float(row["musings_cost_aud"]) - 3.0) < 1e-9
    assert "musings_unpriced_calls" not in row


def test_an_unpriced_call_is_counted_separately_never_costed_as_zero(table):
    # No Models row for "mystery-model" at all.
    with patch(
        "common.stats_tracking.invoke_model_tracked",
        return_value=_tracked_result(model_id="mystery-model"),
    ):
        st.tracked_claude("musings", "prompt", "mystery-model")

    row = _row(table)
    assert row["musings_unpriced_calls"] == 1
    assert "musings_cost_aud" not in row  # never guessed at


def test_repeated_calls_accumulate_rather_than_overwrite(table):
    _put_model()
    with patch(
        "common.stats_tracking.invoke_model_tracked",
        return_value=_tracked_result(input_tokens=10, output_tokens=5),
    ):
        st.tracked_claude("musings", "p1", "model-a")
        st.tracked_claude("musings", "p2", "model-a")
        st.tracked_claude("gear_identity", "p3", "model-a")

    row = _row(table)
    assert row["musings_calls"] == 2 and row["musings_input_tokens"] == 20
    assert row["gear_identity_calls"] == 1  # a different category, its own counters


def test_each_category_gets_its_own_counters(table):
    _put_model()
    with patch("common.stats_tracking.invoke_model_tracked", return_value=_tracked_result()):
        for category in st.BEDROCK_CATEGORIES:
            st.tracked_claude(category, "p", "model-a")

    row = _row(table)
    for category in st.BEDROCK_CATEGORIES:
        assert row[f"{category}_calls"] == 1


def test_an_unknown_category_is_refused_before_any_model_call():
    with patch("common.stats_tracking.invoke_model_tracked") as mock_invoke:
        with pytest.raises(ValueError, match="musings"):
            st.tracked_claude("not_a_real_category", "p", "model-a")
    mock_invoke.assert_not_called()


def test_a_real_model_failure_is_not_swallowed(table):
    with patch("common.stats_tracking.invoke_model_tracked", side_effect=RuntimeError("throttled")):
        with pytest.raises(RuntimeError, match="throttled"):
            st.tracked_claude("musings", "p", "model-a")


def test_the_text_is_still_returned_even_if_recording_the_tally_fails(table):
    """The model call succeeded; the caller's answer must never be lost over bookkeeping."""
    with (
        patch("common.stats_tracking.invoke_model_tracked", return_value=_tracked_result("still here")),
        patch("common.stats_tracking.increment_current_stats", side_effect=RuntimeError("dynamo is down")),
    ):
        assert st.tracked_claude("musings", "p", "model-a") == "still here"


def test_recording_still_works_with_no_stats_table_at_all(monkeypatch):
    """No mock_aws / no table fixture here on purpose: this call must not raise even though
    STATS_CURRENT_TABLE points nowhere real."""
    with patch("common.stats_tracking.invoke_model_tracked", return_value=_tracked_result("ok")):
        assert st.tracked_claude("musings", "p", "model-a") == "ok"


# --- the simple counters ----------------------------------------------------------------------


def test_feedback_given_increments(table):
    st.record_feedback_given()
    st.record_feedback_given()

    assert _row(table)[st.FEEDBACK_GIVEN] == 2


def test_feedback_rejected_comment_increments_its_own_counter(table):
    st.record_feedback_rejected_comment()

    row = _row(table)
    assert row[st.FEEDBACK_REJECTED_COMMENT] == 1
    assert st.FEEDBACK_GIVEN not in row  # a rejection is not also counted as given


def test_loot_drops_increment(table):
    st.record_loot_drop()
    st.record_loot_drop()
    st.record_loot_drop()

    assert _row(table)[st.LOOT_DROPS] == 3


def test_the_simple_counters_never_raise_even_with_no_table(monkeypatch):
    st.record_feedback_given()
    st.record_feedback_rejected_comment()
    st.record_loot_drop()


def test_week_start_is_set_once_and_left_alone(table):
    expected = st._current_week_start()

    st.record_feedback_given()
    table.update_item(
        Key={"stats_id": "current"},
        UpdateExpression="SET week_start = :old",
        ExpressionAttributeValues={":old": "2000-01-03"},
    )
    st.record_feedback_given()  # a second write must not touch week_start again

    assert _row(table)["week_start"] == "2000-01-03"
    assert expected  # sanity: the helper itself did produce a real date string


def test_unrelated_categories_and_counters_do_not_interfere(table):
    _put_model()
    with patch("common.stats_tracking.invoke_model_tracked", return_value=_tracked_result()):
        st.tracked_claude("comment_screening", "p", "model-a")
    st.record_feedback_given()
    st.record_loot_drop()

    row = _row(table)
    assert row["comment_screening_calls"] == 1
    assert row[st.FEEDBACK_GIVEN] == 1
    assert row[st.LOOT_DROPS] == 1
    assert "musings_calls" not in row
    assert "weekly_reflection_calls" not in row


def test_a_week_start_computed_a_day_apart_can_still_land_on_the_same_monday():
    today = date(2026, 9, 24)
    tomorrow = today + timedelta(days=1)
    assert st._current_week_start(today) == st._current_week_start(tomorrow)


# --- api gateway cost: a SET snapshot, not an ADD counter ---------------------------------------


def test_api_gateway_cost_is_recorded_in_usd_and_aud(table):
    st.record_api_gateway_cost(Decimal("2.00"), "2026-09-22T00:00:00+00:00")

    row = _row(table)
    assert row[st.API_GATEWAY_COST_USD_30D] == Decimal("2.00")
    assert row[st.API_GATEWAY_COST_AUD_30D] == Decimal("3.00")  # 2.00 * 1.50 AUD/USD
    assert row[st.API_GATEWAY_COST_AS_OF] == "2026-09-22T00:00:00+00:00"


def test_a_repeat_poll_overwrites_rather_than_accumulates(table):
    st.record_api_gateway_cost(Decimal("2.00"), "2026-09-22T00:00:00+00:00")
    st.record_api_gateway_cost(Decimal("5.00"), "2026-09-23T00:00:00+00:00")

    row = _row(table)
    assert row[st.API_GATEWAY_COST_USD_30D] == Decimal("5.00")  # not 7.00
    assert row[st.API_GATEWAY_COST_AS_OF] == "2026-09-23T00:00:00+00:00"


def test_api_gateway_cost_does_not_disturb_unrelated_counters(table):
    st.record_loot_drop()
    st.record_api_gateway_cost(Decimal("1.00"), "2026-09-22T00:00:00+00:00")

    row = _row(table)
    assert row[st.LOOT_DROPS] == 1
    assert row[st.API_GATEWAY_COST_USD_30D] == Decimal("1.00")


def test_api_gateway_cost_never_raises_even_with_no_table(monkeypatch):
    st.record_api_gateway_cost(Decimal("1.00"), "2026-09-22T00:00:00+00:00")
