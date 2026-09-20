from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

import common.model_routing as model_routing

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MODEL_CONFIG_TABLE", "ModelConfig")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "env-default-model")

    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture
def aws_resources(aws_env):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _put_config(model_id=None, fallback_model_id=None):
    table = boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig")
    table.put_item(
        Item={"config_id": "default", "model_id": model_id, "fallback_model_id": fallback_model_id}
    )


def test_resolve_model_falls_back_to_env_var_with_no_config_and_no_topic(aws_resources):
    model_id, fallback_model_id = model_routing.resolve_model()
    assert model_id == "env-default-model"
    assert fallback_model_id is None


def test_resolve_model_uses_model_config_default(aws_resources):
    _put_config(model_id="config-default-model", fallback_model_id="config-fallback-model")
    model_id, fallback_model_id = model_routing.resolve_model()
    assert model_id == "config-default-model"
    assert fallback_model_id == "config-fallback-model"


def test_resolve_model_topic_override_wins_over_config(aws_resources):
    _put_config(model_id="config-default-model", fallback_model_id="config-fallback-model")
    topic = {"topic_id": "github-trending", "model_id": "topic-override-model"}
    model_id, fallback_model_id = model_routing.resolve_model(topic)
    # Topic has no fallback_model_id of its own -> falls through to
    # ModelConfig's fallback, independently of the model_id precedence.
    assert model_id == "topic-override-model"
    assert fallback_model_id == "config-fallback-model"


def test_resolve_model_topic_overrides_both_fields(aws_resources):
    _put_config(model_id="config-default-model", fallback_model_id="config-fallback-model")
    topic = {
        "topic_id": "github-trending",
        "model_id": "topic-override-model",
        "fallback_model_id": "topic-fallback-model",
    }
    model_id, fallback_model_id = model_routing.resolve_model(topic)
    assert model_id == "topic-override-model"
    assert fallback_model_id == "topic-fallback-model"


def test_resolve_model_topic_with_no_override_fields_falls_through(aws_resources):
    _put_config(model_id="config-default-model")
    topic = {"topic_id": "github-trending"}
    model_id, fallback_model_id = model_routing.resolve_model(topic)
    assert model_id == "config-default-model"
    assert fallback_model_id is None


def test_resolve_model_none_topic_same_as_no_topic(aws_resources):
    model_id, fallback_model_id = model_routing.resolve_model(None)
    assert model_id == "env-default-model"
    assert fallback_model_id is None


# --- Rotation (PR 4 of 5): model_id_candidates -----------------------------


def test_resolve_model_picks_from_topic_candidates(aws_resources):
    topic = {"topic_id": "t", "model_id_candidates": ["model-a", "model-b", "model-c"]}

    seen = {model_routing.resolve_model(topic)[0] for _ in range(200)}

    # Every pick is a candidate, and over 200 draws all three show up
    # (chance of missing one is ~3 * (2/3)^200, effectively zero).
    assert seen == {"model-a", "model-b", "model-c"}


def test_resolve_model_candidates_take_precedence_over_topic_model_id(aws_resources):
    _put_config(model_id="config-default-model")
    topic = {
        "topic_id": "t",
        "model_id": "topic-override-model",
        "model_id_candidates": ["only-candidate"],
    }

    model_id, _ = model_routing.resolve_model(topic)

    assert model_id == "only-candidate"


def test_resolve_model_empty_or_blank_candidates_fall_through(aws_resources):
    _put_config(model_id="config-default-model")

    assert model_routing.resolve_model({"topic_id": "t", "model_id_candidates": []})[0] == (
        "config-default-model"
    )
    assert model_routing.resolve_model({"topic_id": "t", "model_id_candidates": [""]})[0] == (
        "config-default-model"
    )
    assert model_routing.resolve_model({"topic_id": "t", "model_id_candidates": None})[0] == (
        "config-default-model"
    )
    # An empty list also doesn't shadow a real per-topic model_id.
    topic = {"topic_id": "t", "model_id": "topic-override-model", "model_id_candidates": []}
    assert model_routing.resolve_model(topic)[0] == "topic-override-model"


def test_resolve_model_candidates_leave_fallback_resolution_alone(aws_resources):
    _put_config(model_id="config-default-model", fallback_model_id="config-fallback-model")
    topic = {"topic_id": "t", "model_id_candidates": ["model-a"]}

    model_id, fallback_model_id = model_routing.resolve_model(topic)

    assert model_id == "model-a"
    assert fallback_model_id == "config-fallback-model"
