"""What the operator's assistant costs, recorded and bounded (common/stats_tracking.py's
record_assistant_run, ops_agent/quota.py, and where the agent and its handler use them).

Held here: every agent run's model calls, tokens and cost reach the week's Stats row under the
"assistant" category, a failed or cut-off run included, and nothing about it can fail an answer;
the category is on the Stats page and in the `spend` tool's AI spend; each user's questions are
capped per UTC day, counted before the model is called, for questions on the page and briefings
Alexa+ starts alike; and anything that stops the count from being kept refuses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from ops_agent_fakes import ScriptedModel
from table_schemas import create_table

import ops_agent_handler
from common import stats_tracking
from ops_agent import agent, policy, quota
from ops_mcp import account, briefings

REGION = "ap-southeast-2"
USER = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
OTHER = "11111111-2222-4333-8444-555555555555"
# Not shaped like a real token on purpose: the secret scan reads test files too.
TOKEN = "Bearer not-a-real-token.xyzzy-token-marker.for-tests-only"
HAIKU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
NOW = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(quota.CAP_ENV, raising=False)
    monkeypatch.delenv("MODELS_TABLE", raising=False)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    yield
    dynamo_module._dynamodb_resource = None


@pytest.fixture
def tables(monkeypatch):
    monkeypatch.setenv("STATS_CURRENT_TABLE", "StatsCurrent")
    monkeypatch.setenv(briefings.TABLE_ENV, "Briefings")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        create_table(
            client,
            TableName="StatsCurrent",
            KeySchema=[{"AttributeName": "stats_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "stats_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        create_table(
            client,
            TableName="Briefings",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        resource = boto3.resource("dynamodb", region_name=REGION)
        yield resource.Table("StatsCurrent"), resource.Table("Briefings")


def _week_totals(stats) -> dict:
    """This week's row, summed over its shards (increment_current_stats spreads writes)."""
    totals: dict = {}
    for item in stats.scan()["Items"]:
        for key, value in item.items():
            if key.startswith("assistant_"):
                totals[key] = totals.get(key, 0) + value
    return totals


# --- the tally ------------------------------------------------------------------------------------


def test_a_run_is_tallied_with_its_calls_tokens_and_cost(tables):
    stats, _ = tables

    stats_tracking.record_assistant_run(HAIKU, 12_000, 400, 3)

    totals = _week_totals(stats)
    assert totals["assistant_calls"] == 3
    assert totals["assistant_input_tokens"] == 12_000
    assert totals["assistant_output_tokens"] == 400
    # Haiku 4.5's built-in price: US$0.001 in and US$0.005 out per 1k tokens, in AUD. The agent's
    # role reads no Models table, so the built-in table is what prices it.
    expected_usd = Decimal("0.012") + Decimal("0.002")
    assert totals["assistant_cost_aud"] == pytest.approx(
        expected_usd * Decimal(str(stats_tracking.USD_TO_AUD_RATE)), rel=Decimal("1e-6")
    )


def test_an_unpriced_model_is_counted_as_unpriced_never_free(tables):
    stats, _ = tables

    stats_tracking.record_assistant_run("nobody.made-up-model-v9", 100, 10, 2)

    totals = _week_totals(stats)
    assert totals["assistant_unpriced_calls"] == 2
    assert "assistant_cost_aud" not in totals


def test_nothing_is_recorded_without_a_stats_table_or_without_usage(tables, monkeypatch):
    stats, _ = tables
    stats_tracking.record_assistant_run(HAIKU, 0, 0, 0)
    monkeypatch.delenv("STATS_CURRENT_TABLE")
    stats_tracking.record_assistant_run(HAIKU, 100, 10, 1)

    assert _week_totals(stats) == {}


def test_recording_never_raises(monkeypatch, capsys):
    monkeypatch.setenv("STATS_CURRENT_TABLE", "StatsCurrent")
    with patch.object(stats_tracking, "increment_current_stats", side_effect=RuntimeError("down")):
        stats_tracking.record_assistant_run(HAIKU, 100, 10, 1)
    assert "could not record an assistant run: RuntimeError" in capsys.readouterr().out


def test_the_category_is_on_the_stats_page_and_in_the_assistants_own_ai_spend():
    assert "assistant" in stats_tracking.BEDROCK_CATEGORIES
    view = stats_tracking.public_view({"assistant_calls": Decimal(4), "assistant_cost_aud": Decimal("0.5")})
    assistant = [c for c in view["categories"] if c["category"] == "assistant"][0]
    assert assistant["calls"] == 4 and assistant["cost_aud"] == 0.5
    assert "assistant" in account._AI_CATEGORIES


# --- the agent records every run ------------------------------------------------------------------


def _tool_run(script):
    model = ScriptedModel(script)
    with patch.object(stats_tracking, "record_assistant_run") as record:
        try:
            agent.run("What needs my attention?", [], [], model=model)
        except agent.AgentError:
            pass
    return model, record


def test_an_answered_run_records_what_strands_counted(monkeypatch):
    monkeypatch.setenv("OPS_AGENT_MODEL_ID", HAIKU)
    model, record = _tool_run(["All quiet."])

    record.assert_called_once()
    model_id, tokens_in, tokens_out, calls = record.call_args.args
    assert model_id == HAIKU
    assert calls == model.calls == 1
    assert (tokens_in, tokens_out) == (1, 1)  # ScriptedModel reports one each per call


def test_a_failed_run_is_recorded_too(monkeypatch):
    """A model that fails after being called was still billed for what it read."""
    monkeypatch.setenv("OPS_AGENT_MODEL_ID", HAIKU)
    _, record = _tool_run([RuntimeError("throttled")])

    record.assert_called_once()


def test_a_recording_that_fails_never_costs_the_answer(monkeypatch):
    model = ScriptedModel(["All quiet."])
    with patch.object(stats_tracking, "record_assistant_run", side_effect=RuntimeError("down")):
        result = agent.run("What needs my attention?", [], [], model=model)
    assert result["answer"] == "All quiet."


# --- the daily cap --------------------------------------------------------------------------------


def test_with_no_cap_nothing_is_counted(tables):
    _, counts = tables
    assert quota.take(USER, now=NOW) == (True, quota.NO_CAP)
    assert counts.scan()["Items"] == []


def test_each_user_gets_the_cap_per_utc_day(tables, monkeypatch):
    _, counts = tables
    monkeypatch.setenv(quota.CAP_ENV, "3")

    assert [quota.take(USER, now=NOW)[0] for _ in range(4)] == [True, True, True, False]
    assert quota.take(USER, now=NOW) == (False, quota.OVER_CAP)
    # Another user, and the next UTC day, start afresh.
    assert quota.take(OTHER, now=NOW)[0] is True
    assert quota.take(USER, now=NOW + timedelta(hours=1))[0] is True
    item = counts.get_item(Key={"user_id": f"usage#{USER}#2026-10-05"})["Item"]
    assert item["questions"] == 3 and item["expires_at"] > NOW.timestamp()
    # Never a key a briefing could be read back by.
    assert not briefings.valid_user(item["user_id"])


@pytest.mark.parametrize(
    "setting, cap",
    [("5", 5), ("1", 1), ("0", 1), ("-4", 1), ("lots", 1), ("2.5", 1)],
)
def test_a_mistyped_cap_is_the_tightest_one_not_none(monkeypatch, setting, cap):
    monkeypatch.setenv(quota.CAP_ENV, setting)
    assert quota.cap() == cap


def test_whatever_stops_the_count_refuses(tables, monkeypatch):
    monkeypatch.setenv(quota.CAP_ENV, "10")
    assert quota.take(None, now=NOW) == (False, quota.NO_USER)
    assert quota.take("not-a-subject", now=NOW) == (False, quota.NO_USER)
    with patch.object(briefings, "_table", side_effect=RuntimeError("down")):
        assert quota.take(USER, now=NOW) == (False, quota.UNCOUNTABLE)
    monkeypatch.delenv(briefings.TABLE_ENV)
    assert quota.take(USER, now=NOW) == (False, quota.NOT_CONFIGURED)


# --- the handler, on the page and for Alexa+ ------------------------------------------------------


@pytest.fixture
def open_switch(monkeypatch):
    monkeypatch.setattr(ops_agent_handler.dynamo, "get_pipeline_config", lambda: None)
    monkeypatch.delenv("OPS_ASSISTANT_ALLOWED_CIDRS", raising=False)


def _page(question="What needs my attention?"):
    return {
        "httpMethod": "POST",
        "resource": "/ask",
        "headers": {"Authorization": TOKEN},
        "requestContext": {"identity": {"sourceIp": "203.0.113.9"}, "authorizer": {"claims": {"sub": USER}}},
        "body": json.dumps({"question": question}),
    }


ANSWER = {"answer": "All quiet.", "tool_calls": [], "findings": [], "turn": policy.FOLLOW_UP}


def test_past_the_cap_the_page_gets_a_429_with_plain_words_and_the_model_is_not_called(
    tables, open_switch, monkeypatch, capsys
):
    monkeypatch.setenv(quota.CAP_ENV, "2")
    with patch.object(agent, "answer", return_value=dict(ANSWER)) as answer:
        codes = [ops_agent_handler.handler(_page(), None)["statusCode"] for _ in range(3)]
        refused = ops_agent_handler.handler(_page("xyzzy-question-marker"), None)

    assert codes == [200, 200, 429]
    assert answer.call_count == 2
    assert json.loads(refused["body"])["error"] == quota.message(2)
    out = capsys.readouterr().out
    assert "question refused (over the daily cap)" in out and "xyzzy" not in out


def test_a_count_that_cannot_be_kept_is_a_503_not_a_free_question(tables, open_switch, monkeypatch):
    monkeypatch.setenv(quota.CAP_ENV, "2")
    with (
        patch.object(briefings, "_table", side_effect=RuntimeError("down")),
        patch.object(agent, "answer") as answer,
    ):
        response = ops_agent_handler.handler(_page(), None)
    assert response["statusCode"] == 503
    answer.assert_not_called()


def test_a_briefing_alexa_started_counts_against_the_same_cap(tables, open_switch, monkeypatch, capsys):
    monkeypatch.setenv(quota.CAP_ENV, "1")
    monkeypatch.setenv(briefings.AGENT_FUNCTION_ENV, "agent")
    started = []
    with patch.object(agent, "answer", return_value=dict(ANSWER)):
        assert ops_agent_handler.handler(_page(), None)["statusCode"] == 200
    briefings.start(USER, TOKEN, invoke=started.append)
    with patch.object(agent, "answer") as answer:
        assert ops_agent_handler.handler(started[0], None) == {"ok": False}

    answer.assert_not_called()
    assert briefings.latest(USER)["status"] == "failed"
    assert "briefing run refused (over the daily cap)" in capsys.readouterr().out
