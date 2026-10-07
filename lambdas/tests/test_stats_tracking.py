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
    """The week's figures as every reader sees them: the base row and its shards, summed."""
    import common.dynamo as dynamo

    return dynamo.get_current_stats()


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


def test_week_start_is_set_once_and_left_alone(table, monkeypatch):
    import common.dynamo as dynamo

    expected = st._current_week_start()
    monkeypatch.setattr(dynamo.secrets, "randbelow", lambda n: 0)  # both writes on one shard

    st.record_feedback_given()
    table.update_item(
        Key={"stats_id": "current#0"},
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


# --- record_article_lineage: an article's own lineage, folded onto the same weekly row ---------


def _lineage(calls=None, cost_aud=None, research=None):
    return {"calls": calls or [], "cost_aud": cost_aud, "research": research}


def _call(model_id="model-a", input_tokens=100, output_tokens=50):
    return {"model_id": model_id, "input_tokens": input_tokens, "output_tokens": output_tokens}


def test_record_article_lineage_tallies_calls_tokens_and_cost(table):
    lineage = _lineage(calls=[_call(input_tokens=1000, output_tokens=500)], cost_aud=3.0)

    st.record_article_lineage(lineage)

    row = _row(table)
    assert row["articles_calls"] == 1
    assert row["articles_input_tokens"] == 1000
    assert row["articles_output_tokens"] == 500
    assert row["articles_cost_aud"] == Decimal("3.0")
    assert "articles_unpriced_articles" not in row


def test_record_article_lineage_includes_research_calls_and_uses_total_cost(table):
    lineage = _lineage(
        calls=[_call(input_tokens=100, output_tokens=50)],
        cost_aud=1.0,  # authoring alone -- must NOT be used once there's a research component
        research={"calls": [_call(input_tokens=10, output_tokens=5)]},
    )
    lineage["total_cost_aud"] = 4.0

    st.record_article_lineage(lineage)

    row = _row(table)
    assert row["articles_calls"] == 2  # 1 authoring + 1 research
    assert row["articles_input_tokens"] == 110
    assert row["articles_output_tokens"] == 55
    assert row["articles_cost_aud"] == Decimal("4.0")  # total, not the authoring-only figure


def test_record_article_lineage_counts_unpriced_articles_when_cost_is_none(table):
    lineage = _lineage(calls=[_call()], cost_aud=None)  # an unpriced call left cost_aud blank

    st.record_article_lineage(lineage)

    row = _row(table)
    assert row["articles_calls"] == 1
    assert row["articles_unpriced_articles"] == 1
    assert "articles_cost_aud" not in row  # never guessed at


def test_record_article_lineage_does_nothing_for_a_lineage_with_no_calls(table):
    # e.g. a non-financial topic's compliance review makes no Bedrock call at all.
    st.record_article_lineage(_lineage(calls=[]))

    assert table.scan()["Items"] == []


def test_record_article_lineage_never_raises_even_on_a_malformed_lineage(monkeypatch):
    st.record_article_lineage({"calls": "not a list"})  # would raise inside, if not caught


def test_record_article_lineage_never_raises_with_no_table_at_all(monkeypatch):
    st.record_article_lineage(_lineage(calls=[_call()], cost_aud=1.0))


def test_record_article_lineage_does_not_disturb_other_categories(table):
    _put_model()
    with patch("common.stats_tracking.invoke_model_tracked", return_value=_tracked_result()):
        st.tracked_claude("musings", "p", "model-a")
    st.record_article_lineage(_lineage(calls=[_call(input_tokens=1, output_tokens=1)], cost_aud=0.01))

    row = _row(table)
    assert row["musings_calls"] == 1
    assert row["articles_calls"] == 1


# --- split_for_rollover: additive counters vs. the API Gateway snapshot ------------------------


def test_split_for_rollover_separates_the_api_gateway_snapshot_from_everything_else():
    row = {
        "stats_id": "current",
        "week_start": "2026-09-15",
        "musings_calls": 3,
        "articles_cost_aud": Decimal("4.0"),
        "feedback_given": 2,
        st.API_GATEWAY_COST_USD_30D: Decimal("1.5"),
        st.API_GATEWAY_COST_AUD_30D: Decimal("2.25"),
        st.API_GATEWAY_COST_AS_OF: "2026-09-14T00:00:00+00:00",
    }

    additive, snapshot = st.split_for_rollover(row)

    assert additive == {"musings_calls": 3, "articles_cost_aud": Decimal("4.0"), "feedback_given": 2}
    assert snapshot == {
        st.API_GATEWAY_COST_USD_30D: Decimal("1.5"),
        st.API_GATEWAY_COST_AUD_30D: Decimal("2.25"),
        st.API_GATEWAY_COST_AS_OF: "2026-09-14T00:00:00+00:00",
    }


def test_split_for_rollover_drops_metadata_fields_from_both_halves():
    additive, snapshot = st.split_for_rollover(
        {"stats_id": "current", "week_start": "2026-09-15", "rolled_over_at": "x", "loot_drops": 1}
    )

    assert additive == {"loot_drops": 1}
    assert snapshot == {}


# --- public_view: shaping a row for the public Stats page --------------------------------------


def test_public_view_shapes_one_entry_per_category():
    row = {
        "musings_calls": 2,
        "musings_input_tokens": 20,
        "musings_output_tokens": 10,
        "musings_cost_aud": Decimal("0.5"),
        "articles_calls": 5,
        "articles_cost_aud": Decimal("12.34"),
    }

    view = st.public_view(row)

    by_category = {c["category"]: c for c in view["categories"]}
    assert by_category["musings"] == {
        "category": "musings",
        "calls": 2,
        "input_tokens": 20,
        "output_tokens": 10,
        "cost_aud": 0.5,
        "unpriced": 0,
    }
    assert by_category["articles"]["calls"] == 5
    assert by_category["articles"]["cost_aud"] == 12.34
    # every category from BEDROCK_CATEGORIES plus "articles" appears even with nothing recorded
    assert set(by_category) == {*st.BEDROCK_CATEGORIES, "articles"}


def test_public_view_adds_the_categories_up_so_the_page_never_does():
    """One section's whole token estimate: the table's Total row. The sum is made here."""
    row = {
        "musings_calls": 2,
        "musings_input_tokens": 20,
        "musings_output_tokens": 10,
        "musings_cost_aud": Decimal("0.10"),
        "assistant_calls": 3,
        "assistant_input_tokens": 300,
        "assistant_output_tokens": 30,
        "assistant_cost_aud": Decimal("0.20"),
        "articles_calls": 5,
        "articles_input_tokens": 5000,
        "articles_output_tokens": 500,
        "articles_cost_aud": Decimal("1.00"),
        "articles_unpriced_articles": 1,
        "comment_screening_calls": 4,
        "comment_screening_unpriced_calls": 4,
    }

    view = st.public_view(row)

    assert view["ai_estimate"] == {
        "calls": 14,
        "input_tokens": 5320,
        "output_tokens": 540,
        "cost_aud": 1.30,  # exact: summed as Decimal, not as floats
        "unpriced": 5,
    }
    assert view["ai_estimate"]["cost_aud"] == sum(
        c["cost_aud"] for c in view["categories"] if c["cost_aud"] is not None
    )


def test_the_ai_estimate_is_a_real_zero_when_nothing_ran_and_unknown_when_nothing_was_priced():
    assert st.public_view({})["ai_estimate"] == {
        "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_aud": 0.0, "unpriced": 0,
    }  # fmt: skip
    unpriced = st.public_view({"musings_calls": 2, "musings_unpriced_calls": 2})["ai_estimate"]
    assert unpriced["cost_aud"] is None and unpriced["unpriced"] == 2


def test_public_view_says_which_week_a_current_row_is_and_nothing_for_the_all_time_row():
    assert st.public_view({"week_start": "2026-10-05"})["week_start"] == "2026-10-05"
    assert st.public_view({"week_start": "all-time"})["week_start"] is None
    assert st.public_view({})["week_start"] is None


def test_one_row_in_one_section_out():
    """Weekly Stats is the current table's row and Total Stats the history table's all-time row.
    public_view takes one row, so neither section can borrow a figure from the other."""
    import inspect

    assert list(inspect.signature(st.public_view).parameters) == ["row"]


def test_public_view_uses_the_articles_specific_unpriced_key():
    row = {"articles_unpriced_articles": 2, "musings_unpriced_calls": 3}

    view = st.public_view(row)

    by_category = {c["category"]: c for c in view["categories"]}
    assert by_category["articles"]["unpriced"] == 2
    assert by_category["musings"]["unpriced"] == 3


def test_public_view_totals_pipeline_hours_across_every_lambda_function():
    row = {
        "lambda_ms_research_tick": 3_600_000,  # 1 hour
        "lambda_ms_daily_cycle": 1_800_000,  # 30 minutes
    }

    assert st.public_view(row)["pipeline_hours"] == 1.5


def test_public_view_converts_api_gateway_cost_but_never_exposes_as_of():
    row = {
        st.API_GATEWAY_COST_USD_30D: Decimal("2.5"),
        st.API_GATEWAY_COST_AUD_30D: Decimal("3.75"),
        st.API_GATEWAY_COST_AS_OF: "2026-09-22T00:00:00+00:00",
    }

    view = st.public_view(row)

    assert view["api_gateway_cost_usd_30d"] == 2.5
    assert view["api_gateway_cost_aud_30d"] == 3.75
    assert "api_gateway_cost_as_of" not in view
    assert "api_gateway_cost_as_of" not in str(view)  # not tucked away under another key either


# --- plan_articles_backfill / to_stats_updates: the one-time catch-up (PR 5) --------------------


def test_plan_articles_backfill_sums_several_articles_into_one_totals_dict():
    lineage_1 = _lineage(calls=[_call(input_tokens=1000, output_tokens=500)], cost_aud=3.0)
    lineage_2 = _lineage(calls=[_call(input_tokens=200, output_tokens=100)], cost_aud=1.0)
    articles = [
        {"article_id": "a1", "lineage": lineage_1},
        {"article_id": "a2", "lineage": lineage_2},
    ]

    plan = st.plan_articles_backfill(articles)

    assert (plan["examined"], plan["included"]) == (2, 2)
    assert plan["totals"]["articles_calls"] == 2
    assert plan["totals"]["articles_input_tokens"] == 1200
    assert plan["totals"]["articles_cost_aud"] == pytest.approx(4.0)


def test_plan_articles_backfill_skips_articles_with_no_lineage_or_no_calls():
    articles = [
        {"article_id": "a1", "lineage": None},
        {"article_id": "a2"},  # no "lineage" key at all
        {"article_id": "a3", "lineage": _lineage(calls=[])},
    ]

    plan = st.plan_articles_backfill(articles)

    assert (plan["examined"], plan["included"]) == (3, 0)
    assert plan["totals"] == {}


def test_plan_articles_backfill_counts_unpriced_articles_separately_from_priced_ones():
    articles = [
        {"article_id": "a1", "lineage": _lineage(calls=[_call()], cost_aud=1.0)},
        {"article_id": "a2", "lineage": _lineage(calls=[_call()], cost_aud=None)},
    ]

    plan = st.plan_articles_backfill(articles)

    assert plan["totals"]["articles_cost_aud"] == pytest.approx(1.0)  # only the priced one
    assert plan["totals"]["articles_unpriced_articles"] == 1


def test_to_stats_updates_wraps_only_the_float_in_decimal():
    updates = st.to_stats_updates({"articles_calls": 3, "articles_cost_aud": 4.5})

    assert updates == {"articles_calls": 3, "articles_cost_aud": Decimal("4.5")}
    assert isinstance(updates["articles_calls"], int)
    assert isinstance(updates["articles_cost_aud"], Decimal)


def test_public_view_on_an_entirely_empty_row_is_all_zeros_and_nones():
    view = st.public_view({"week_start": "all-time"})

    assert view["feedback_given"] == 0
    assert view["feedback_rejected_comment"] == 0
    assert view["loot_drops"] == 0
    assert view["pipeline_hours"] == 0
    assert view["api_gateway_cost_usd_30d"] is None
    assert view["api_gateway_cost_aud_30d"] is None
    assert all(c["cost_aud"] is None and c["calls"] == 0 for c in view["categories"])


# --- web search (AgentCore queries and GDELT fallbacks) ---------------------------------------


def test_each_agentcore_query_adds_one_query_and_its_per_query_price(table):
    st.record_web_search_query()
    st.record_web_search_query()

    row = _row(table)
    assert row[st.WEB_SEARCH_AGENTCORE_QUERIES] == 2
    per_query_aud = Decimal(str(st.AGENTCORE_WEB_SEARCH_USD_PER_QUERY * st.USD_TO_AUD_RATE))
    assert row[st.WEB_SEARCH_AGENTCORE_COST_AUD] == per_query_aud * 2


def test_a_gdelt_fallback_is_its_own_counter(table):
    st.record_web_search_fallback()

    row = _row(table)
    assert row[st.WEB_SEARCH_GDELT_FALLBACKS] == 1
    assert st.WEB_SEARCH_AGENTCORE_QUERIES not in row  # the query itself is counted separately


def test_the_web_search_recorders_never_raise_even_with_no_table(monkeypatch):
    monkeypatch.delenv("STATS_CURRENT_TABLE")
    st.record_web_search_query()
    st.record_web_search_fallback()


def test_web_search_counters_roll_over_as_additive_counters():
    row = {
        "stats_id": "current",
        "week_start": "2026-09-21",
        st.WEB_SEARCH_AGENTCORE_QUERIES: Decimal("3"),
        st.WEB_SEARCH_AGENTCORE_COST_AUD: Decimal("0.0315"),
        st.WEB_SEARCH_GDELT_FALLBACKS: Decimal("2"),
    }

    additive, snapshot = st.split_for_rollover(row)

    assert additive == {
        st.WEB_SEARCH_AGENTCORE_QUERIES: Decimal("3"),
        st.WEB_SEARCH_AGENTCORE_COST_AUD: Decimal("0.0315"),
        st.WEB_SEARCH_GDELT_FALLBACKS: Decimal("2"),
    }
    assert snapshot == {}


def test_public_view_shows_web_search_queries_cost_and_fallbacks():
    row = {
        st.WEB_SEARCH_AGENTCORE_QUERIES: Decimal("4"),
        st.WEB_SEARCH_AGENTCORE_COST_AUD: Decimal("0.042"),
        st.WEB_SEARCH_GDELT_FALLBACKS: Decimal("3"),
    }

    assert st.public_view(row)["web_search"] == {
        "agentcore_queries": 4,
        "agentcore_cost_aud": 0.042,
        "gdelt_fallbacks": 3,
        "agentcore_actual_cost_aud_30d": None,  # no Cost Explorer poll yet
    }


def test_public_view_with_no_web_searches_is_a_real_zero_cost():
    assert st.public_view({})["web_search"] == {
        "agentcore_queries": 0,
        "agentcore_cost_aud": 0.0,
        "gdelt_fallbacks": 0,
        "agentcore_actual_cost_aud_30d": None,
    }


def test_public_view_never_invents_a_web_search_cost_it_does_not_have():
    view = st.public_view({st.WEB_SEARCH_AGENTCORE_QUERIES: Decimal("2")})

    assert view["web_search"]["agentcore_cost_aud"] is None


# --- the actual AgentCore charge (Cost Explorer): a SET snapshot like API Gateway's -------------


def test_agentcore_cost_is_recorded_in_usd_and_aud(table):
    st.record_agentcore_cost(Decimal("0.40"), "2026-09-22T00:00:00+00:00")

    row = _row(table)
    assert row[st.AGENTCORE_COST_USD_30D] == Decimal("0.40")
    assert row[st.AGENTCORE_COST_AUD_30D] == Decimal("0.60")  # 0.40 * 1.50 AUD/USD
    assert row[st.AGENTCORE_COST_AS_OF] == "2026-09-22T00:00:00+00:00"


def test_a_repeat_agentcore_poll_overwrites_rather_than_accumulates(table):
    st.record_agentcore_cost(Decimal("0.40"), "2026-09-22T00:00:00+00:00")
    st.record_agentcore_cost(Decimal("0.70"), "2026-09-23T00:00:00+00:00")

    assert _row(table)[st.AGENTCORE_COST_USD_30D] == Decimal("0.70")  # not 1.10


def test_the_agentcore_actual_leaves_the_per_query_estimate_alone(table):
    st.record_web_search_query()
    st.record_agentcore_cost(Decimal("0.40"), "2026-09-22T00:00:00+00:00")

    row = _row(table)
    assert row[st.WEB_SEARCH_AGENTCORE_QUERIES] == 1
    assert row[st.AGENTCORE_COST_USD_30D] == Decimal("0.40")


def test_agentcore_cost_never_raises_even_with_no_table(monkeypatch):
    monkeypatch.delenv("STATS_CURRENT_TABLE")
    st.record_agentcore_cost(Decimal("1.00"), "2026-09-22T00:00:00+00:00")


def test_the_agentcore_actual_rolls_over_as_a_snapshot_never_summed():
    row = {
        "stats_id": "current",
        "week_start": "2026-09-15",
        st.WEB_SEARCH_AGENTCORE_QUERIES: 5,
        st.AGENTCORE_COST_USD_30D: Decimal("0.40"),
        st.AGENTCORE_COST_AUD_30D: Decimal("0.60"),
        st.AGENTCORE_COST_AS_OF: "2026-09-14T00:00:00+00:00",
    }

    additive, snapshot = st.split_for_rollover(row)

    assert additive == {st.WEB_SEARCH_AGENTCORE_QUERIES: 5}
    assert snapshot == {
        st.AGENTCORE_COST_USD_30D: Decimal("0.40"),
        st.AGENTCORE_COST_AUD_30D: Decimal("0.60"),
        st.AGENTCORE_COST_AS_OF: "2026-09-14T00:00:00+00:00",
    }


def test_public_view_shows_the_agentcore_actual_beside_the_estimate_but_never_its_as_of():
    view = st.public_view(
        {
            st.WEB_SEARCH_AGENTCORE_QUERIES: Decimal("4"),
            st.WEB_SEARCH_AGENTCORE_COST_AUD: Decimal("0.042"),
            st.AGENTCORE_COST_USD_30D: Decimal("0.03"),
            st.AGENTCORE_COST_AUD_30D: Decimal("0.045"),
            st.AGENTCORE_COST_AS_OF: "2026-09-22T00:00:00+00:00",
        }
    )

    assert view["web_search"]["agentcore_cost_aud"] == 0.042  # the estimate
    assert view["web_search"]["agentcore_actual_cost_aud_30d"] == 0.045  # the bill
    assert "2026-09-22T00:00:00" not in str(view)  # the as_of stays owner-only


def test_public_view_shows_a_zero_actual_as_zero_not_missing():
    view = st.public_view({st.AGENTCORE_COST_AUD_30D: Decimal("0")})

    assert view["web_search"]["agentcore_actual_cost_aud_30d"] == 0.0


# --- AWS WAF (Cost Explorer): 30 days, this week, this month and last month, all snapshots -----


def _record_waf(**overrides):
    values = {
        "usd_30d": Decimal("10.00"),
        "usd_week_to_date": Decimal("2.00"),
        "usd_month_to_date": Decimal("1.00"),
        "usd_previous_month": Decimal("11.00"),
        "month": "2026-10",
        "previous_month": "2026-09",
        "as_of": "2026-10-03T10:00:00+00:00",
    }
    st.record_waf_cost(**{**values, **overrides})


def test_waf_cost_is_recorded_for_every_window_in_usd_and_aud(table):
    _record_waf()

    row = _row(table)
    assert row[st.WAF_COST_USD_30D] == Decimal("10.00")
    assert row[st.WAF_COST_AUD_30D] == Decimal("15.00")  # 1.50 AUD/USD
    assert row[st.WAF_COST_AUD_WEEK_TO_DATE] == Decimal("3.00")
    assert row[st.WAF_COST_AUD_MONTH_TO_DATE] == Decimal("1.50")
    assert row[st.WAF_COST_AUD_PREVIOUS_MONTH] == Decimal("16.50")
    assert (row[st.WAF_COST_MONTH], row[st.WAF_COST_PREVIOUS_MONTH]) == ("2026-10", "2026-09")
    assert row[st.WAF_COST_AS_OF] == "2026-10-03T10:00:00+00:00"


def test_a_repeat_waf_poll_overwrites_rather_than_accumulates(table):
    _record_waf()
    _record_waf(usd_30d=Decimal("12.00"), usd_month_to_date=Decimal("1.40"))

    row = _row(table)
    assert row[st.WAF_COST_USD_30D] == Decimal("12.00")  # not 22.00
    assert row[st.WAF_COST_USD_MONTH_TO_DATE] == Decimal("1.40")


def test_waf_cost_never_raises_even_with_no_table(monkeypatch):
    monkeypatch.delenv("STATS_CURRENT_TABLE")
    _record_waf()


def test_the_waf_readings_roll_over_as_snapshots_never_summed():
    row = {
        "stats_id": "current",
        "week_start": "2026-09-28",
        st.FEEDBACK_GIVEN: 2,
        st.WAF_COST_USD_30D: Decimal("10.00"),
        st.WAF_COST_USD_WEEK_TO_DATE: Decimal("2.00"),
        st.WAF_COST_MONTH: "2026-10",
    }

    additive, snapshot = st.split_for_rollover(row)

    assert additive == {st.FEEDBACK_GIVEN: 2}
    assert set(snapshot) == {st.WAF_COST_USD_30D, st.WAF_COST_USD_WEEK_TO_DATE, st.WAF_COST_MONTH}


def test_public_view_shows_the_waf_readings_but_never_their_as_of(table):
    _record_waf()

    view = st.public_view(_row(table))

    assert view["waf"] == {
        "cost_aud_30d": 15.0,
        "cost_aud_week_to_date": 3.0,
        "month": "2026-10",
        "cost_aud_month_to_date": 1.5,
        "previous_month": "2026-09",
        "cost_aud_previous_month": 16.5,
    }
    assert "2026-10-03T10:00" not in str(view)


def test_public_view_has_no_waf_readings_until_the_first_poll():
    assert st.public_view({"week_start": "2026-09-28"})["waf"] is None


def test_public_view_shows_a_zero_waf_month_as_zero_not_missing(table):
    _record_waf(usd_month_to_date=Decimal("0"))

    assert st.public_view(_row(table))["waf"]["cost_aud_month_to_date"] == 0.0


# --- The whole AWS bill: this week, each complete week, and all time ---------------------------


@pytest.fixture
def bill_tables(monkeypatch):
    monkeypatch.setenv("STATS_HISTORY_TABLE", "StatsHistory")
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        for name, key in (("StatsCurrent", "stats_id"), ("StatsHistory", "week_start")):
            dynamodb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield boto3.resource("dynamodb", region_name=REGION).Table("StatsHistory")


AS_OF = "2026-10-03T10:00:00+00:00"


def _history_week(history, week_start, **fields):
    history.put_item(Item={"week_start": week_start, st.FEEDBACK_GIVEN: 1, **fields})


def test_this_weeks_bill_is_kept_per_service_on_the_current_row(bill_tables):
    week = {"AWS WAF": Decimal("2.10"), "AmazonCloudWatch": Decimal("0.70")}

    st.record_aws_bill(week_to_date=week, complete_weeks={}, as_of=AS_OF)

    assert _row(None)[st.AWS_BILL_WEEK_USD] == week


def test_a_completed_week_gets_its_whole_bill_once_the_rollover_has_made_its_row(bill_tables):
    # The rollover copied a partial week (it never sees the Sunday); the poll overwrites it.
    _history_week(bill_tables, "2026-09-21", **{st.AWS_BILL_WEEK_USD: {"AWS WAF": Decimal("1.00")}})

    result = st.record_aws_bill(
        week_to_date={},
        complete_weeks={
            "2026-09-14": {"AWS WAF": Decimal("9.99")},  # no row: before Stats existed, left alone
            "2026-09-21": {"AWS WAF": Decimal("2.50"), "Amazon DynamoDB": Decimal("0.40")},
        },
        as_of=AS_OF,
    )

    assert result["weeks_filled"] == ["2026-09-21"]
    row = bill_tables.get_item(Key={"week_start": "2026-09-21"})["Item"]
    assert row[st.AWS_BILL_WEEK_USD] == {"AWS WAF": Decimal("2.50"), "Amazon DynamoDB": Decimal("0.40")}
    assert row[st.AWS_BILL_WEEK_COMPLETE] is True
    assert row[st.FEEDBACK_GIVEN] == 1  # the rest of the week's row is untouched
    assert "Item" not in bill_tables.get_item(Key={"week_start": "2026-09-14"})


def test_the_all_time_bill_is_the_sum_of_every_complete_week_recomputed_each_time(bill_tables):
    complete = {st.AWS_BILL_WEEK_COMPLETE: True}
    _history_week(bill_tables, "2026-09-14", **{st.AWS_BILL_WEEK_USD: {"AWS WAF": Decimal("2")}}, **complete)
    _history_week(bill_tables, "2026-09-21")  # rolled over, bill not known yet: not counted
    _history_week(bill_tables, "2026-09-28", **{st.AWS_BILL_WEEK_USD: {"AWS WAF": Decimal("9")}})  # partial

    for _ in range(2):  # a second poll must not double it
        st.record_aws_bill(
            week_to_date={},
            complete_weeks={"2026-09-21": {"AWS WAF": Decimal("3"), "AWS Lambda": Decimal("0.5")}},
            as_of=AS_OF,
        )

    totals = bill_tables.get_item(Key={"week_start": "all-time"})["Item"]
    assert totals[st.AWS_BILL_TOTAL_USD] == {"AWS WAF": Decimal("5"), "AWS Lambda": Decimal("0.5")}
    assert totals[st.AWS_BILL_TOTAL_SINCE] == "2026-09-14"
    assert totals[st.AWS_BILL_TOTAL_WEEKS] == 2


def test_the_bill_never_rolls_onto_the_all_time_row():
    row = {
        "stats_id": "current",
        "week_start": "2026-09-28",
        st.FEEDBACK_GIVEN: 2,
        st.AWS_BILL_WEEK_USD: {"AWS WAF": Decimal("1")},
        st.AWS_BILL_AS_OF: AS_OF,
    }

    additive, snapshot = st.split_for_rollover(row)

    assert additive == {st.FEEDBACK_GIVEN: 2}
    assert snapshot == {}


def test_a_bill_failure_never_raises(monkeypatch):
    monkeypatch.delenv("STATS_CURRENT_TABLE")
    result = st.record_aws_bill(week_to_date={}, complete_weeks={"2026-09-21": {}}, as_of=AS_OF)
    assert result["weeks_filled"] == []


def test_public_view_shows_the_bill_as_three_groups_and_a_total_never_per_service():
    week = {
        "Amazon Bedrock": Decimal("1.00"),
        "Claude Haiku 4.5 (Amazon Bedrock Edition)": Decimal("1.00"),
        "AWS WAF": Decimal("2.00"),
        "AmazonCloudWatch": Decimal("0.50"),
        "Amazon CloudFront": Decimal("0.50"),
    }

    bill = st.public_view({st.AWS_BILL_WEEK_USD: week})["aws_bill"]

    assert bill == {
        "categories": [
            {"category": "ai", "cost_aud": 3.0},
            {"category": "security", "cost_aud": 3.0},
            {"category": "infrastructure", "cost_aud": 1.5},
        ],
        "total_aud": 7.5,
        "since": None,
        "weeks": None,
        "scope": "account",
    }
    assert "CloudWatch" not in str(bill)


def test_public_view_of_the_all_time_row_shows_the_total_since_the_first_week():
    row = {st.AWS_BILL_TOTAL_USD: {"AWS Lambda": Decimal("2")}, st.AWS_BILL_TOTAL_SINCE: "2026-09-14"}

    bill = st.public_view(row)["aws_bill"]

    assert bill["total_aud"] == 3.0 and bill["since"] == "2026-09-14"
    # How many weeks that is, and whose bill: the page says both beside the figure.
    assert (bill["weeks"], bill["scope"]) == (None, "account")
    counted = st.public_view({**row, st.AWS_BILL_TOTAL_WEEKS: Decimal(3)})["aws_bill"]
    assert counted["weeks"] == 3 and isinstance(counted["weeks"], int)


def test_public_view_has_no_bill_until_the_first_poll():
    assert st.public_view({"week_start": "2026-09-28"})["aws_bill"] is None


# --- Total Stats' cost summary: the assistant, infrastructure and the overall total -------------

_RATE = Decimal(str(st.USD_TO_AUD_RATE))
_ALL_TIME_BILL = {
    "Amazon Bedrock": Decimal("2.00"),
    "Claude Haiku 4.5 (Amazon Bedrock Edition)": Decimal("1.00"),
    "Amazon Bedrock AgentCore": Decimal("0.50"),
    "AWS WAF": Decimal("10.00"),
    "AWS Lambda": Decimal("4.00"),
    "Amazon CloudFront": Decimal("0.25"),
}


def _aud(usd: str) -> float:
    return float(Decimal(usd) * _RATE)


def _bill_row(**fields) -> dict:
    return {
        "week_start": "all-time",
        st.AWS_BILL_TOTAL_USD: _ALL_TIME_BILL,
        st.AWS_BILL_TOTAL_SINCE: "2026-08-31",
        st.AWS_BILL_TOTAL_WEEKS: 5,
        **fields,
    }


def test_the_overall_total_is_the_bills_ai_plus_everything_else_on_the_bill():
    bill = st.overall_view(_bill_row(), {})["aws_bill"]

    assert bill["ai_aud"] == pytest.approx(_aud("3.50"))
    # "Infrastructure" here is all that is not AI: hosting and data, and the firewall.
    assert bill["infrastructure_aud"] == pytest.approx(_aud("14.25"))
    assert bill["total_aud"] == pytest.approx(_aud("17.75"))
    assert bill["total_aud"] == pytest.approx(bill["ai_aud"] + bill["infrastructure_aud"])
    assert bill["total_aud"] == pytest.approx(st.public_view(_bill_row())["aws_bill"]["total_aud"])


def test_no_token_estimate_is_ever_added_to_the_overall_total():
    """Bedrock is counted by tokens here and charged on the bill: the same dollars. However much
    the rows say was spent by tokens (the assistant, articles, musings, web search), the overall
    total is the bill and only the bill."""
    estimates = {
        "assistant_calls": 40,
        "assistant_cost_aud": Decimal("5.00"),
        "articles_calls": 900,
        "articles_cost_aud": Decimal("50.00"),
        "musings_cost_aud": Decimal("7.00"),
        st.WEB_SEARCH_AGENTCORE_QUERIES: 100,
        st.WEB_SEARCH_AGENTCORE_COST_AUD: Decimal("1.00"),
    }

    without = st.overall_view(_bill_row(), {})["aws_bill"]
    with_estimates = st.overall_view(_bill_row(**estimates), {"week_start": "2026-10-05", **estimates})

    assert with_estimates["aws_bill"] == without
    assert with_estimates["assistant"]["cost_aud"] == 10.0  # shown beside the total, never in it


def test_this_weeks_bill_and_the_rolling_readings_are_not_in_the_overall_total():
    """Only complete weeks: this week so far would be added again when its week completes, and
    the 30-day readings overlap the weeks already counted."""
    current = {
        "week_start": "2026-10-05",
        st.AWS_BILL_WEEK_USD: {"AWS WAF": Decimal("99")},
        st.WAF_COST_AUD_30D: Decimal("99"),
        st.API_GATEWAY_COST_AUD_30D: Decimal("99"),
        st.AGENTCORE_COST_AUD_30D: Decimal("99"),
    }
    totals = _bill_row(**{st.WAF_COST_AUD_30D: Decimal("99"), st.API_GATEWAY_COST_AUD_30D: Decimal("99")})

    assert st.overall_view(totals, current)["aws_bill"]["total_aud"] == pytest.approx(_aud("17.75"))


def test_the_overall_bill_is_labelled_with_its_real_period_and_as_the_whole_accounts():
    """Not "all time" (it starts at the first complete week that has a bill) and not this
    environment's: dev and production share the account, so neither may call it its own."""
    overall = st.overall_view(_bill_row(), {})

    bill = overall["aws_bill"]
    assert (bill["since"], bill["weeks"], bill["scope"]) == ("2026-08-31", 5, "account")
    note = overall["note"]
    assert "every complete week since 2026-08-31 (5 weeks)" in note
    assert "whole AWS account (dev and production together)" in note
    assert "before tax" in note and "fixed USD to AUD rate" in note
    assert "not added again" in note and "counted once" in note
    assert "all time" not in note.lower()


def test_an_environment_with_no_bill_reports_none_never_the_ai_estimate_as_a_total():
    """Where the daily poll has not totalled a complete week (a fresh deployment, or one where
    the poll does not run), there is no infrastructure figure and no overall total: nothing is
    invented, and what was spent by tokens is not passed off as the whole cost."""
    totals = {"week_start": "all-time", "assistant_calls": 3, "assistant_cost_aud": Decimal("0.50")}
    # This week's bill alone is not an all-time figure either.
    current = {"week_start": "2026-10-05", st.AWS_BILL_WEEK_USD: {"AWS WAF": Decimal("3")}}

    overall = st.overall_view(totals, current)

    assert overall["aws_bill"] is None
    assert overall["assistant"]["cost_aud"] == 0.5
    assert "has not been totalled" in overall["note"]


def test_the_assistants_spend_to_date_is_every_rolled_over_week_plus_this_one():
    totals = {
        "assistant_calls": 10,
        "assistant_input_tokens": 9000,
        "assistant_output_tokens": 1000,
        "assistant_cost_aud": Decimal("0.30"),
        "assistant_unpriced_calls": 1,
    }
    current = {
        "assistant_calls": 2,
        "assistant_input_tokens": 1500,
        "assistant_output_tokens": 500,
        "assistant_cost_aud": Decimal("0.06"),
    }

    assert st.overall_view(totals, current)["assistant"] == {
        "calls": 12,
        "input_tokens": 10500,
        "output_tokens": 1500,
        "cost_aud": 0.36,
        "unpriced": 1,
    }


def test_rows_from_before_the_assistant_existed_are_zeros_not_errors():
    """Old all-time rows have no assistant_* fields and no bill; an empty week has nothing."""
    overall = st.overall_view({"week_start": "all-time", "musings_calls": 4}, {})

    assert overall["assistant"] == {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_aud": 0.0,
        "unpriced": 0,
    }
    assert overall["aws_bill"] is None


def test_an_assistant_that_ran_with_no_price_has_no_cost_rather_than_a_free_one():
    current = {"assistant_calls": 4, "assistant_unpriced_calls": 4}

    assistant = st.overall_view({}, current)["assistant"]

    assert assistant["cost_aud"] is None and assistant["unpriced"] == 4


def test_a_bill_total_with_no_week_count_or_start_still_adds_up():
    """A row written by an older poll: the missing label is left out, not guessed."""
    overall = st.overall_view({st.AWS_BILL_TOTAL_USD: {"AWS Lambda": Decimal("2")}}, {})

    assert overall["aws_bill"] == {
        "ai_aud": 0.0,
        "infrastructure_aud": _aud("2"),
        "total_aud": _aud("2"),
        "since": None,
        "weeks": None,
        "scope": "account",
    }
    assert "covers every complete week, for the whole AWS account" in overall["note"]


def test_the_overall_summary_is_money_and_counts_and_nothing_else():
    """The Stats page is public: no service names, no timestamps of the owner's polls, and the
    only strings are the period's first Monday, the scope word and the note."""
    row = _bill_row(**{st.AWS_BILL_AS_OF: "2026-10-05T03:00:00+00:00", "assistant_calls": 1})

    overall = st.overall_view(row, {})

    assert set(overall) == {"assistant", "aws_bill", "note"}
    assert "2026-10-05T03" not in str(overall) and "Lambda" not in str(overall)
    fields = {**overall["assistant"], **overall["aws_bill"]}
    assert {key for key, value in fields.items() if isinstance(value, str)} == {"since", "scope"}
