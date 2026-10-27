"""The admin API's model catalog routes (admin_api_handler.py), and the `models add` route keeping
what the catalog recorded about a model."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import admin_api_handler
from common import model_catalog

REGION = "ap-southeast-2"


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MODELS_TABLE": "Models",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "MODEL_CATALOG_FUNCTION_NAME": "model-catalog-fn",
    }.items():
        monkeypatch.setenv(key, value)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    admin_api_handler._lambda_client = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, key in {"Models": "model_id", "ModelConfig": "config_id"}.items():
            client.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield boto3.resource("dynamodb", region_name=REGION)
    dynamo_module._dynamodb_resource = None


def call(route, body=None):
    event = {"routeKey": route}
    if body is not None:
        event["body"] = json.dumps(body)
    result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


def test_get_shows_the_defaults_when_nothing_is_set(aws):
    status, body = call("GET /model-catalog-config")
    assert status == 200
    assert body["effective"] == model_catalog.SETTINGS
    assert body["under_review"] is None


def test_put_sets_and_clears_settings_and_leaves_the_rest(aws):
    status, body = call("PUT /model-catalog-config", {"reprice_enabled": False, "review_after_hours": [1, 6]})
    assert status == 200
    assert body["effective"]["reprice_enabled"] is False
    assert body["effective"]["review_after_hours"] == [1, 6]

    status, body = call("PUT /model-catalog-config", {"reprice_enabled": None})
    assert body["effective"]["reprice_enabled"] is True
    assert body["settings"]["review_after_hours"] == [1, 6]


@pytest.mark.parametrize(
    "body", [{}, {"run_hour": 30}, {"discover_weekday": "Funday"}, {"nonsense": 1}, ["not", "a", "dict"]]
)
def test_put_refuses_what_is_not_a_valid_setting(aws, body):
    status, _ = call("PUT /model-catalog-config", body)
    assert status == 400


@pytest.mark.parametrize(
    "route, job",
    [
        ("POST /models/reprice", "reprice"),
        ("POST /models/discover", "discover"),
        ("POST /models/classify", "classify"),
    ],
)
def test_a_job_is_started_in_the_background(aws, route, job):
    client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=client):
        status, body = call(route)
    assert (status, body) == (202, {"triggered": job})
    kwargs = client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == "model-catalog-fn"
    assert kwargs["InvocationType"] == "Event"
    assert json.loads(kwargs["Payload"]) == {"job": job}


def test_a_reprice_is_refused_while_a_run_is_under_review(aws):
    aws.Table("ModelConfig").put_item(Item={"config_id": model_catalog.STATE_ID, "active_run_id": "r1"})
    aws.Table("ModelConfig").put_item(Item={"config_id": f"{model_catalog.RUN_PREFIX}r1", "run_id": "r1"})
    client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=client):
        status, body = call("POST /models/reprice")
        assert status == 409
        assert "r1" in body["error"]
        assert call("POST /models/discover")[0] == 202  # discovery is not held back
    client.invoke.assert_called_once()


def test_models_add_keeps_what_the_catalog_recorded_and_can_lock_the_price(aws):
    aws.Table("Models").put_item(
        Item={
            "model_id": "m1",
            "display_name": "M1",
            "provider": "test",
            "enabled": False,
            "region": REGION,
            "discovered_at": "2026-10-20T00:00:00+00:00",
            "pricing_unit": "tokens",
            "price_source": "web-search",
            "input_price_usd_per_1k_tokens": Decimal("0.001"),
            "output_price_usd_per_1k_tokens": Decimal("0.005"),
        }
    )
    status, _ = call(
        "POST /models",
        {
            "model_id": "m1",
            "display_name": "M1",
            "provider": "test",
            "input_price_usd_per_1k_tokens": 0.0011,
            "output_price_usd_per_1k_tokens": 0.0055,
            "price_locked": True,
        },
    )
    assert status == 200
    row = aws.Table("Models").get_item(Key={"model_id": "m1"})["Item"]
    assert row["enabled"] is True
    assert row["region"] == REGION
    assert row["discovered_at"] == "2026-10-20T00:00:00+00:00"
    assert row["price_locked"] is True
    assert "price_source" not in row  # set by hand now

    bad = {
        "model_id": "m1",
        "display_name": "M1",
        "provider": "test",
        "input_price_usd_per_1k_tokens": 0.001,
        "output_price_usd_per_1k_tokens": 0.005,
        "price_locked": "yes",
    }
    assert call("POST /models", bad)[0] == 400


RANCH = {
    "model_purpose": "text",
    "model_inputs": ["TEXT", "IMAGE"],
    "daily_perf": 4,
    "weekly_perf": -12,
    "monthly_perf": 30,
    "total_perf": 0,
    "perf_events": {"daily": 2, "weekly": 6, "monthly": 15, "total": 60},
    "perf_good": {"daily": 1, "weekly": 4, "monthly": 9, "total": 41},
    "perf_bad": {"daily": 1, "weekly": 2, "monthly": 3, "total": 7},
    "perf_updated_at": "2026-10-20T17:00:00+00:00",
    "model_roles": ["default", "rotation"],
    "ungated_until": "2026-10-21T17:00:00+00:00",
}


def test_models_add_on_an_existing_row_keeps_what_the_ranch_recorded(aws):
    assert admin_api_handler._CATALOG_MODEL_FIELDS == model_catalog.CATALOG_MODEL_FIELDS
    assert set(RANCH) == set(model_catalog.RANCH_FIELDS) <= set(model_catalog.CATALOG_MODEL_FIELDS)
    aws.Table("Models").put_item(
        Item={
            "model_id": "m1",
            "display_name": "M1",
            "provider": "test",
            "enabled": False,
            "pricing_unit": "tokens",
            "input_price_usd_per_1k_tokens": Decimal("0.001"),
            "output_price_usd_per_1k_tokens": Decimal("0.005"),
            **RANCH,
        }
    )

    status, body = call(
        "POST /models",
        {
            "model_id": "m1",
            "display_name": "M1 renamed",
            "provider": "test",
            "input_price_usd_per_1k_tokens": 0.0011,
            "output_price_usd_per_1k_tokens": 0.0055,
        },
    )

    assert status == 200
    row = aws.Table("Models").get_item(Key={"model_id": "m1"})["Item"]
    assert (row["enabled"], row["display_name"]) == (True, "M1 renamed")
    assert {field: row[field] for field in RANCH} == RANCH  # a zero total and a negative week included
    assert {field: body[field] for field in RANCH} == RANCH
