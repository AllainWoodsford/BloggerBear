from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

import common.costing as costing
from common.dynamo import put_model

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MODELS_TABLE", "Models")

    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture
def aws_resources(aws_env):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="Models",
            KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "model_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _register(model_id, *, input_price=1.0, output_price=2.0):
    put_model(
        {
            "model_id": model_id,
            "display_name": model_id,
            "provider": "anthropic",
            "input_price_usd_per_1k_tokens": input_price,
            "output_price_usd_per_1k_tokens": output_price,
            "enabled": True,
        }
    )


# --- calculate_lineage_cost_aud ---------------------------------------------


def test_calculate_cost_single_call(aws_resources):
    _register("model-a", input_price=1.0, output_price=2.0)
    calls = [{"model_id": "model-a", "input_tokens": 1000, "output_tokens": 1000}]

    cost_aud, note = costing.calculate_lineage_cost_aud(calls)

    # (1000/1000 * 1.0) + (1000/1000 * 2.0) = 3.0 USD * 1.50 = 4.50 AUD
    assert cost_aud == pytest.approx(4.50)
    assert note is None


def test_calculate_cost_sums_across_multiple_calls_and_models(aws_resources):
    _register("model-a", input_price=1.0, output_price=2.0)
    _register("model-b", input_price=0.5, output_price=0.5)
    calls = [
        {"model_id": "model-a", "input_tokens": 1000, "output_tokens": 0},
        {"model_id": "model-b", "input_tokens": 2000, "output_tokens": 2000},
    ]

    cost_aud, note = costing.calculate_lineage_cost_aud(calls)

    # model-a: 1.0 USD. model-b: (2*0.5)+(2*0.5) = 2.0 USD. Total 3.0 USD * 1.50.
    assert cost_aud == pytest.approx(4.50)
    assert note is None


def test_calculate_cost_unregistered_model_returns_none_with_note(aws_resources):
    calls = [{"model_id": "unregistered-model", "input_tokens": 100, "output_tokens": 100}]

    cost_aud, note = costing.calculate_lineage_cost_aud(calls)

    assert cost_aud is None
    assert "unregistered-model" in note


def test_calculate_cost_one_unpriced_model_among_priced_ones_returns_none(aws_resources):
    # Never silently under-count by skipping the unpriced call -- an
    # honest "unknown" beats a lower number that looks complete.
    _register("model-a", input_price=1.0, output_price=2.0)
    calls = [
        {"model_id": "model-a", "input_tokens": 1000, "output_tokens": 1000},
        {"model_id": "model-b-not-registered", "input_tokens": 500, "output_tokens": 500},
    ]

    cost_aud, note = costing.calculate_lineage_cost_aud(calls)

    assert cost_aud is None
    assert "model-b-not-registered" in note


def test_calculate_cost_empty_calls_list_is_zero(aws_resources):
    cost_aud, note = costing.calculate_lineage_cost_aud([])
    assert cost_aud == 0.0
    assert note is None


# --- build_lineage -----------------------------------------------------------


def test_build_lineage_aggregates_tokens_and_dedupes_models(aws_resources):
    _register("model-a", input_price=1.0, output_price=1.0)
    calls = [
        {
            "stage": "ideation",
            "model_id": "model-a",
            "input_tokens": 100,
            "output_tokens": 50,
            "used_fallback": False,
        },
        {
            "stage": "draft",
            "model_id": "model-a",
            "input_tokens": 200,
            "output_tokens": 100,
            "used_fallback": False,
        },
        {
            "stage": "title",
            "model_id": "model-a",
            "input_tokens": 20,
            "output_tokens": 10,
            "used_fallback": False,
        },
    ]

    lineage = costing.build_lineage(calls)

    assert lineage["calls"] == calls
    assert lineage["total_input_tokens"] == 320
    assert lineage["total_output_tokens"] == 160
    assert lineage["models_used"] == ["model-a"]
    assert lineage["cost_aud"] is not None
    assert lineage["cost_note"] is None


def test_build_lineage_preserves_first_used_order_across_models(aws_resources):
    _register("model-a", input_price=1.0, output_price=1.0)
    _register("model-b", input_price=1.0, output_price=1.0)
    calls = [
        {
            "stage": "ideation",
            "model_id": "model-b",
            "input_tokens": 10,
            "output_tokens": 5,
            "used_fallback": False,
        },
        {
            "stage": "draft",
            "model_id": "model-a",
            "input_tokens": 10,
            "output_tokens": 5,
            "used_fallback": False,
        },
        {
            "stage": "title",
            "model_id": "model-b",
            "input_tokens": 10,
            "output_tokens": 5,
            "used_fallback": False,
        },
    ]

    lineage = costing.build_lineage(calls)

    assert lineage["models_used"] == ["model-b", "model-a"]


def test_build_lineage_with_no_calls(aws_resources):
    lineage = costing.build_lineage([])

    assert lineage["calls"] == []
    assert lineage["total_input_tokens"] == 0
    assert lineage["total_output_tokens"] == 0
    assert lineage["models_used"] == []
    assert lineage["cost_aud"] == 0.0
    assert lineage["cost_note"] is None


def test_build_lineage_unpriced_model_propagates_cost_note(aws_resources):
    calls = [
        {
            "stage": "draft",
            "model_id": "mystery-model",
            "input_tokens": 10,
            "output_tokens": 5,
            "used_fallback": False,
        }
    ]

    lineage = costing.build_lineage(calls)

    assert lineage["cost_aud"] is None
    assert "mystery-model" in lineage["cost_note"]
