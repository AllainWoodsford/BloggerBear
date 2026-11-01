from __future__ import annotations

import random

import boto3
import pytest
from moto import mock_aws

import common.model_routing as model_routing
from common import dynamo, evals_settings, evidence, model_fleet, model_ranch

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
    # No fallback configured anywhere: the env default stands in.
    assert fallback_model_id == "env-default-model"


def test_resolve_model_env_default_is_the_fallback_when_none_is_configured(aws_resources):
    """A topic or ModelConfig naming a model Bedrock has since retired still gets a fallback."""
    topic = {"topic_id": "t", "model_id": "retired-model"}
    assert model_routing.resolve_model(topic) == ("retired-model", "env-default-model")

    topic = {"topic_id": "t", "model_id_candidates": ["retired-model"]}
    assert model_routing.resolve_model(topic) == ("retired-model", "env-default-model")


def test_resolve_model_configured_fallback_wins_over_the_env_default(aws_resources):
    topic = {"topic_id": "t", "model_id": "topic-model", "fallback_model_id": "topic-fallback"}
    assert model_routing.resolve_model(topic) == ("topic-model", "topic-fallback")


def test_resolve_model_no_fallback_when_the_model_is_the_env_default(aws_resources):
    topic = {"topic_id": "t", "model_id": "env-default-model"}
    assert model_routing.resolve_model(topic) == ("env-default-model", None)


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


# --- The rotation guard (common/model_ranch.py, PR15) ---------------------------------------------
#
# While evals are on, a model the Model Ranch has gated is no candidate: not drawn from a rotation,
# not picked to route around an unfit model, and so never written to a topic. The default, the
# fallback, the judge and a topic's own model are served all the same, with a line saying so.

GATED_SCORES = {
    "daily_perf": -80,
    "weekly_perf": -80,
    "monthly_perf": -80,
    "total_perf": -80,
    "perf_events": {"daily": 20, "weekly": 20, "monthly": 20, "total": 20},
}


@pytest.fixture
def ranch(aws_env, monkeypatch):
    monkeypatch.setenv("MODELS_TABLE", "Models")
    monkeypatch.setenv("TOPICS_TABLE", "Topics")
    monkeypatch.setenv("EVALS_ENABLED", "true")
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        for name, key in {"ModelConfig": "config_id", "Models": "model_id", "Topics": "topic_id"}.items():
            dynamodb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield boto3.resource("dynamodb", region_name=REGION)


def _model(aws, model_id, *, gated=False, enabled=True, **fields):
    item = {"model_id": model_id, "display_name": model_id, "provider": "test", "enabled": enabled}
    item.update(model_purpose="text", **(GATED_SCORES if gated else {}), **fields)
    aws.Table("Models").put_item(Item=item)


def _topic(aws, topic_id="t", **fields):
    item = {"topic_id": topic_id, "name": topic_id, **fields}
    aws.Table("Topics").put_item(Item=item)
    return item


def _stored_topic(aws, topic_id="t"):
    return aws.Table("Topics").get_item(Key={"topic_id": topic_id})["Item"]


def _protected_lines(capsys):
    found = [evidence.parse(text) for text in capsys.readouterr().out.splitlines()]
    return [line for line in found if line and line["event"] == "model_gated_protected"]


def test_a_seeded_draw_never_picks_a_gated_model_from_the_rotation(ranch):
    for model_id in ("model-a", "model-c"):
        _model(ranch, model_id)
    _model(ranch, "model-b", gated=True)
    topic = _topic(ranch, model_id_candidates=["model-a", "model-b", "model-c"])

    seen = set()
    for seed in range(200):
        random.seed(seed)
        seen.add(model_routing.resolve_model(topic)[0])

    assert seen == {"model-a", "model-c"}
    assert _stored_topic(ranch)["model_id_candidates"] == ["model-a", "model-b", "model-c"]  # kept


def test_routing_around_an_unfit_model_never_writes_a_gated_pick_to_the_topic(ranch):
    _put_config(model_id="cfg-default", fallback_model_id="cfg-fallback")
    for model_id in ("off", "cfg-default", "cfg-fallback"):
        _model(ranch, model_id, enabled=False)
    _model(ranch, "alive")
    _model(ranch, "gated-1", gated=True)
    _model(ranch, "gated-2", gated=True)

    for seed in range(30):
        random.seed(seed)
        topic = _topic(ranch, model_id_candidates=["off"])
        assert model_routing.resolve_model(topic)[0] == "alive"
        assert _stored_topic(ranch)["model_id_candidates"] == ["alive"]
        own = _topic(ranch, "own", model_id="off")
        assert model_routing.resolve_model(own)[0] == "alive"
        assert _stored_topic(ranch, "own")["model_id"] == "alive"


def test_the_default_the_fallback_and_the_judge_are_never_gated_and_their_serving_says_so(ranch, capsys):
    _put_config(model_id="cfg-default", fallback_model_id="cfg-fallback")
    ranch.Table("ModelConfig").put_item(Item={"config_id": "evals", "judge_model_id": "the-judge"})
    for model_id in ("cfg-default", "cfg-fallback", "the-judge", "plain"):
        _model(ranch, model_id, gated=True)
    topic = _topic(ranch, model_id_candidates=["cfg-default", "cfg-fallback", "the-judge", "plain"])

    seen = set()
    for seed in range(200):
        random.seed(seed)
        seen.add(model_routing.resolve_model(topic)[0])

    assert seen == {"cfg-default", "cfg-fallback", "the-judge"}
    lines = _protected_lines(capsys)
    served = {(line["model_id"], line["role"], line["slot"]) for line in lines}
    assert {("cfg-default", "default", "model"), ("the-judge", "judge", "model")} <= served
    assert ("cfg-fallback", "fallback", "fallback") in served  # handed back as the fallback each time
    assert all(line["caller"] == "routing" and line["topic_id"] == "t" for line in lines)
    assert "plain" not in {line["model_id"] for line in lines}

    # Topic-agnostic calls get the default as they always have, gated or not.
    assert model_routing.resolve_model(None) == ("cfg-default", "cfg-fallback")


def test_a_topics_own_model_is_never_gated_and_is_served_with_a_line(ranch, capsys):
    _model(ranch, "own", gated=True)
    topic = _topic(ranch, model_id="own")

    assert model_routing.resolve_model(topic) == ("own", "env-default-model")

    [line] = _protected_lines(capsys)
    # -80 is below the daily gate (-50) only.
    assert (line["model_id"], line["role"], line["windows"]) == ("own", "topic_model", ["daily"])


def test_a_rotation_all_gated_falls_to_the_topics_model_then_the_default_never_the_list(ranch, capsys):
    _put_config(model_id="cfg-default")
    for model_id in ("g1", "g2"):
        _model(ranch, model_id, gated=True)
    _model(ranch, "own")

    with_own = _topic(ranch, "with-own", model_id_candidates=["g1", "g2"], model_id="own")
    without = _topic(ranch, "without", model_id_candidates=["g1", "g2"])
    for seed in range(20):
        random.seed(seed)
        assert model_routing.resolve_model(with_own)[0] == "own"
        assert model_routing.resolve_model(without)[0] == "cfg-default"

    assert _stored_topic(ranch, "without")["model_id_candidates"] == ["g1", "g2"]  # nothing written
    assert "every model in topic with-own's rotation is gated" in capsys.readouterr().out


def test_an_operators_ungated_until_puts_a_gated_model_back_in_the_draw(ranch):
    _model(ranch, "held-open", gated=True, ungated_until="2999-01-01T00:00:00Z")
    _model(ranch, "gated", gated=True)
    topic = _topic(ranch, model_id_candidates=["held-open", "gated"])

    assert {model_routing.resolve_model(topic)[0] for _ in range(50)} == {"held-open"}


def test_the_guard_reads_the_registry_and_the_evals_row_once_per_call(ranch, monkeypatch):
    _put_config(model_id="cfg-default", fallback_model_id="cfg-fallback")
    for model_id in ("off", "cfg-default", "cfg-fallback"):
        _model(ranch, model_id, enabled=False)
    _model(ranch, "alive")
    reads = {"models": 0, "evals": 0}
    real_list, real_settings = dynamo.list_models, evals_settings.get_settings

    def counted_list():
        reads["models"] += 1
        return real_list()

    def counted_settings():
        reads["evals"] += 1
        return real_settings()

    monkeypatch.setattr(dynamo, "list_models", counted_list)
    monkeypatch.setattr(evals_settings, "get_settings", counted_settings)

    # A rotation draw that then routes around an unfit model: both lists come from one listing.
    assert model_routing.resolve_model(_topic(ranch, model_id_candidates=["off"]))[0] == "alive"
    assert reads == {"models": 1, "evals": 1}


@pytest.mark.parametrize("broken", ["list_models", "get_settings", "get_model_config"])
def test_a_guard_that_cannot_read_its_rows_fails_open_and_routes_as_before(ranch, monkeypatch, broken):
    _model(ranch, "gated", gated=True)
    topic = _topic(ranch, model_id_candidates=["gated"])

    def fails(*args, **kwargs):
        raise RuntimeError("throttled")

    if broken == "get_settings":
        monkeypatch.setattr(evals_settings, "get_settings", fails)
    elif broken == "get_model_config":
        # The guard's own read fails; routing's own read of the row (imported by name) still works.
        monkeypatch.setattr(model_ranch.dynamo, "get_model_config", fails)
    else:
        monkeypatch.setattr(dynamo, "list_models", fails)

    assert model_routing.resolve_model(topic)[0] == "gated"


def test_a_malformed_models_row_fails_open(ranch):
    _model(ranch, "odd", daily_perf=-500, perf_events="many", ungated_until="soon")
    topic = _topic(ranch, model_id_candidates=["odd"])

    assert model_routing.resolve_model(topic)[0] == "odd"


def test_with_evals_off_nothing_is_filtered_and_no_settings_row_is_read(ranch, monkeypatch, capsys):
    monkeypatch.setenv("EVALS_ENABLED", "false")
    for model_id in ("model-a", "model-b"):
        _model(ranch, model_id, gated=True)
    topic = _topic(ranch, model_id_candidates=["model-a", "model-b"])
    reads = []

    def refuse(*args, **kwargs):
        # Recorded first: the guard's fail-open `except Exception` would swallow the raise alone.
        reads.append(args)
        raise AssertionError("read while evals are off")

    monkeypatch.setattr(evals_settings, "get_settings", refuse)
    monkeypatch.setattr(dynamo, "get_config_row", refuse)
    # The rotation guard's registry listing too: fit models need none while evals are off.
    monkeypatch.setattr(dynamo, "list_models", refuse)

    assert {model_routing.resolve_model(topic)[0] for _ in range(100)} == {"model-a", "model-b"}
    assert _protected_lines(capsys) == []
    assert reads == []


def test_routing_the_triage_around_a_text_only_default_never_picks_a_gated_model(ranch):
    """The stricter need's route-around (the default cannot take an image) draws from the registry
    models the guard has not gated, as the topic's does."""
    _put_config(model_id="cfg-default", fallback_model_id="cfg-fallback")
    for model_id in ("cfg-default", "cfg-fallback", "env-default-model"):
        _model(ranch, model_id, model_inputs=["TEXT"])
    _model(ranch, "seer", model_inputs=["TEXT", "IMAGE"])
    # Neither the default, the fallback nor the judge (the fallback, by its chain): not protected.
    _model(ranch, "gated-seer", gated=True, model_inputs=["TEXT", "IMAGE"])

    seen = set()
    for seed in range(60):
        random.seed(seed)
        seen.add(model_routing.resolve_model(None, model_fleet.TEXT_WITH_IMAGE_INPUT)[0])

    assert seen == {"seer"}
