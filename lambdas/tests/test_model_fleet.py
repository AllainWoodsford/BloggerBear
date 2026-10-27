"""The model fleet (common/model_fleet.py): pruning the registry to its cap, cycling the topics'
rotations, what disabling a model does, routing around an inactive model
(common/model_routing.py), and pricing what was pruned, against moto tables."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

import admin_api_handler
from common import costing, dynamo, evidence, model_catalog, model_fleet, model_routing, vision_triage

REGION = "ap-southeast-2"
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
SETTINGS = model_catalog.effective_settings(None)


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MODELS_TABLE": "Models",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "TOPICS_TABLE": "Topics",
        "BEDROCK_MODEL_ID": "env-default",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, key in {"Models": "model_id", "ModelConfig": "config_id", "Topics": "topic_id"}.items():
            client.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield boto3.resource("dynamodb", region_name=REGION)
    dynamo._dynamodb_resource = None


def put_model(aws, model_id, price=None, enabled=True, **fields):
    item = {"model_id": model_id, "display_name": model_id, "provider": "test", "enabled": enabled, **fields}
    if price is not None:
        item["input_price_usd_per_1k_tokens"] = Decimal(str(price))
        item["output_price_usd_per_1k_tokens"] = Decimal(str(price * 4))
    aws.Table("Models").put_item(Item=item)


def put_topic(aws, topic_id, **fields):
    aws.Table("Topics").put_item(Item={"topic_id": topic_id, "name": topic_id, **fields})


def put_config(aws, model_id=None, fallback_model_id=None):
    aws.Table("ModelConfig").put_item(
        Item={"config_id": "default", "model_id": model_id, "fallback_model_id": fallback_model_id}
    )


def topic(aws, topic_id):
    return aws.Table("Topics").get_item(Key={"topic_id": topic_id})["Item"]


def ids(aws):
    return sorted(m["model_id"] for m in dynamo.list_models())


# --- pruning --------------------------------------------------------------------------------------


def test_pruning_takes_inactive_then_unpriced_then_the_most_expensive_in_value_mode():
    models = [
        {"model_id": "active-cheap", "input_price_usd_per_1k_tokens": 0.001},
        {"model_id": "active-dear", "input_price_usd_per_1k_tokens": 0.01},
        {"model_id": "inactive-cheap", "enabled": False, "input_price_usd_per_1k_tokens": 0.001},
        {"model_id": "inactive-dear", "enabled": False, "input_price_usd_per_1k_tokens": 0.01},
        {"model_id": "inactive-unpriced", "enabled": False},
        {"model_id": "inactive-image", "enabled": False, "pricing_unit": "other"},
        {"model_id": "active-unpriced"},
    ]
    value = [m["model_id"] for m in model_fleet.prune_order(models, "value_for_money")]
    assert value == [
        "inactive-image",
        "inactive-unpriced",
        "inactive-dear",
        "inactive-cheap",
        "active-unpriced",
        "active-dear",
        "active-cheap",
    ]
    frontier = [m["model_id"] for m in model_fleet.prune_order(models, "frontier")]
    assert frontier[2:4] == ["inactive-cheap", "inactive-dear"]
    assert frontier[-2:] == ["active-cheap", "active-dear"]


def test_a_full_registry_is_pruned_to_make_room_never_touching_a_protected_model(aws):
    settings = {**SETTINGS, "max_models": 8, "discover_max_added": 2}
    put_config(aws, model_id="default-m", fallback_model_id="fallback-m")
    put_model(aws, "default-m", 0.05, enabled=False)
    put_model(aws, "fallback-m", 0.04, enabled=False)
    put_model(aws, "env-default", 0.03, enabled=False)
    put_model(aws, "rotated", 0.02, enabled=False)
    put_model(aws, "pooled", 0.02, enabled=False)
    put_model(aws, "locked", 0.09, enabled=False, price_locked=True)
    put_model(aws, "dear", 0.01, enabled=False)
    put_model(aws, "cheap", 0.001, enabled=False)
    put_model(aws, "unpriced", enabled=False)
    put_topic(aws, "t1", model_id_candidates=["rotated"])
    dynamo.put_model_rotation_candidates(["pooled"])

    result = model_fleet.prune(settings, NOW)

    # 9 held, room for 2 more under 8 means 6: three go, every unprotected one, worst first.
    assert (result["status"], result["models"], result["pruned"]) == ("pruned", 9, 3)
    assert result["pruned_ids"] == ["unpriced", "dear", "cheap"]
    assert result["short_by"] == 0
    assert "dear" not in ids(aws) and "default-m" in ids(aws) and "locked" in ids(aws)
    archive = dynamo.get_model_archive()
    assert archive["dear"]["input_price_usd_per_1k_tokens"] == 0.01
    assert archive["dear"]["pruned_at"] == NOW.isoformat()


def test_pruning_stops_at_its_per_run_limit_and_says_when_protection_left_it_short(aws):
    for n in range(10):
        put_model(aws, f"m{n}", 0.001 * (n + 1), enabled=False)
    settings = {**SETTINGS, "max_models": 5, "discover_max_added": 0}
    capped = model_fleet.prune({**settings, "prune_max_per_run": 2}, NOW)
    assert (capped["pruned"], len(ids(aws))) == (2, 8)

    put_topic(aws, "t", model_id_candidates=[f"m{n}" for n in range(8)])
    short = model_fleet.prune(settings, NOW)
    assert (short["pruned"], short["short_by"]) == (0, 3)


def test_discover_prunes_once_full_and_never_adds_a_pruned_model_back(aws, monkeypatch):
    settings = {**SETTINGS, "max_models": 2, "discover_max_added": 1}
    put_model(aws, "a", 0.001, enabled=False)
    put_model(aws, "b", 0.002, enabled=False)
    found = [
        {"model_id": m, "display_name": m, "provider": "p", "pricing_unit": "tokens"} for m in ("b", "c")
    ]
    monkeypatch.setattr(model_catalog, "account_models", lambda region, client=None: found)

    full = model_catalog.discover("schedule", settings, NOW)
    assert full["status"] == "full"
    assert full["prune"]["pruned_ids"] == ["b"]  # value for money: the dearer one

    again = model_catalog.discover("schedule", settings, NOW)
    assert (again["status"], again["added_ids"]) == ("done", ["c"])  # b is archived: never re-added
    assert ids(aws) == ["a", "c"]


# --- cycling --------------------------------------------------------------------------------------


def test_the_cycle_drops_inactive_models_and_adds_the_best_pick_up_to_the_cap(aws):
    put_config(aws, fallback_model_id="fb")
    put_model(aws, "cheap", 0.001)
    put_model(aws, "mid", 0.002)
    put_model(aws, "dear", 0.01)
    put_model(aws, "fb", 0.0001)  # the cheapest, but a fallback
    put_model(aws, "off", 0.0001, enabled=False)
    put_model(aws, "unpriced")
    put_model(aws, "gone", 0.003, enabled=False)
    put_topic(aws, "t1", model_id_candidates=["mid", "gone"])
    put_topic(aws, "t2", model_id_candidates=["cheap"])
    put_topic(aws, "full", model_id_candidates=["mid", "dear"])
    put_topic(aws, "none")

    result = model_fleet.cycle({**SETTINGS, "rotation_max": 2}, NOW)

    assert topic(aws, "t1")["model_id_candidates"] == ["mid", "cheap"]
    assert topic(aws, "t2")["model_id_candidates"] == ["cheap", "mid"]
    assert topic(aws, "full")["model_id_candidates"] == ["mid", "dear"]
    assert "model_id_candidates" not in topic(aws, "none")
    assert sorted(result["topics_changed"]) == ["t1", "t2"]
    assert (result["added"], result["dropped_inactive"]) == (2, 1)


def test_the_cycle_in_frontier_mode_adds_the_high_end_model(aws):
    put_model(aws, "cheap", 0.001)
    put_model(aws, "dear", 0.01)
    put_model(aws, "mid", 0.002)
    put_topic(aws, "t", model_id_candidates=["mid"])
    model_fleet.cycle({**SETTINGS, "prune_mode": "frontier"}, NOW)
    assert topic(aws, "t")["model_id_candidates"] == ["mid", "dear"]


def test_the_cycle_runs_as_a_job_unless_paused(aws, monkeypatch):
    ran = []
    monkeypatch.setattr(model_fleet, "cycle", lambda settings, now: ran.append(now) or {"status": "done"})
    assert model_catalog.handle({"job": "cycle", "trigger": "schedule"}, NOW)["status"] == "ran"
    aws.Table("ModelConfig").put_item(Item={"config_id": model_catalog.CONFIG_ID, "cycle_enabled": False})
    assert model_catalog.handle({"job": "cycle", "trigger": "schedule"}, NOW)["status"] == "paused"
    assert len(ran) == 1


# --- disabling a model ----------------------------------------------------------------------------


def test_a_disabled_model_leaves_every_rotation_model_fallback_and_the_pool(aws):
    put_model(aws, "old", 0.001, enabled=False)
    put_model(aws, "keep", 0.002)
    put_model(aws, "next", 0.003)
    put_topic(aws, "t1", model_id_candidates=["old", "keep"], model_id="old", fallback_model_id="old")
    put_topic(aws, "t2", model_id_candidates=["keep"])
    dynamo.put_model_rotation_candidates(["old", "keep"])

    result = model_fleet.deactivated("old", SETTINGS)

    t1 = topic(aws, "t1")
    assert t1["model_id_candidates"] == ["keep", "next"]  # the cycler's pick in its place
    assert "model_id" not in t1 and "fallback_model_id" not in t1
    assert topic(aws, "t2")["model_id_candidates"] == ["keep"]
    assert dynamo.get_model_rotation_candidates() == ["keep"]
    assert result == {"topics_updated": ["t1"], "rotation_pool_updated": True}


def model_body(model_id, enabled=False, **extra):
    return {
        "model_id": model_id,
        "display_name": model_id,
        "provider": "test",
        "input_price_usd_per_1k_tokens": 0.001,
        "output_price_usd_per_1k_tokens": 0.004,
        "enabled": enabled,
        **extra,
    }


def call(body):
    result = admin_api_handler.handler({"routeKey": "POST /models", "body": json.dumps(body)}, None)
    return result["statusCode"], json.loads(result["body"])


def test_disabling_the_default_or_fallback_needs_confirming_then_updates_the_topics(aws):
    put_config(aws, model_id="main", fallback_model_id="spare")
    put_model(aws, "main", 0.001)
    put_model(aws, "spare", 0.001)
    put_model(aws, "other", 0.002)
    put_topic(aws, "t", model_id_candidates=["main", "other"])

    status, refused = call(model_body("main"))
    assert status == 409
    assert (refused["confirm_required"], refused["roles"]) == (True, ["default"])
    assert dynamo.get_model("main")["enabled"] is True
    assert call(model_body("spare"))[1]["roles"] == ["fallback"]

    status, done = call(model_body("main", confirm=True))
    assert status == 200
    assert done["rotations"]["topics_updated"] == ["t"]
    assert dynamo.get_model("main")["enabled"] is False
    assert topic(aws, "t")["model_id_candidates"] == ["other"]  # spare is a fallback: no pick


def test_disabling_any_other_model_needs_no_confirming_and_a_disabled_one_is_left(aws):
    put_model(aws, "plain", 0.001)
    put_topic(aws, "t", model_id_candidates=["plain"])
    assert call(model_body("plain"))[0] == 200
    assert topic(aws, "t")["model_id_candidates"] == []
    status, body = call(model_body("plain"))  # already disabled: nothing to review
    assert status == 200 and "rotations" not in body


# --- routing around an inactive model -------------------------------------------------------------


def test_an_inactive_rotation_model_routes_to_the_fallback_then_the_default(aws):
    put_model(aws, "off", 0.001, enabled=False)
    put_model(aws, "fb", 0.001)
    rotating = {"topic_id": "t", "model_id_candidates": ["off"], "fallback_model_id": "fb"}
    put_topic(aws, "t", **{k: v for k, v in rotating.items() if k != "topic_id"})
    assert model_routing.resolve_model(rotating) == ("fb", "env-default")

    put_config(aws, model_id="cfg-default")
    no_fallback = {"topic_id": "t", "model_id_candidates": ["off"]}
    assert model_routing.resolve_model(no_fallback)[0] == "cfg-default"
    assert topic(aws, "t")["model_id_candidates"] == ["off"]  # a configured route changes nothing


def test_when_nothing_configured_works_a_random_active_model_takes_the_rotation_slot(aws):
    put_config(aws, model_id="cfg-default", fallback_model_id="cfg-fallback")
    for model_id in ("off", "cfg-default", "cfg-fallback"):
        put_model(aws, model_id, 0.001, enabled=False)
    put_model(aws, "alive", 0.002)
    put_model(aws, "image", 0.002, pricing_unit="other")
    put_topic(aws, "t", model_id_candidates=["off"])

    model_id, _ = model_routing.resolve_model(topic(aws, "t"))

    assert model_id == "alive"
    assert topic(aws, "t")["model_id_candidates"] == ["alive"]


def test_an_inactive_topic_model_is_replaced_on_the_topic(aws):
    put_config(aws, model_id="cfg-default")
    put_model(aws, "off", 0.001, enabled=False)
    put_model(aws, "cfg-default", 0.001, enabled=False)
    put_model(aws, "alive", 0.002)
    put_topic(aws, "t", model_id="off")
    assert model_routing.resolve_model({"topic_id": "t", "model_id": "off"})[0] == "alive"
    assert topic(aws, "t")["model_id"] == "alive"


def test_an_active_or_unregistered_model_routes_as_before(aws):
    put_model(aws, "on", 0.001)
    assert model_routing.resolve_model({"topic_id": "t", "model_id_candidates": ["on"]})[0] == "on"
    assert model_routing.resolve_model({"topic_id": "t", "model_id": "unregistered"})[0] == "unregistered"


# --- pricing what was pruned ----------------------------------------------------------------------


def test_a_pruned_models_archived_price_still_prices_its_calls_and_the_stats(aws):
    dynamo.archive_models(
        {
            "gone": {
                "display_name": "Gone",
                "input_price_usd_per_1k_tokens": 0.002,
                "output_price_usd_per_1k_tokens": 0.008,
            }
        }
    )
    put_model(aws, "here", 0.001)
    assert costing.pricing_for("gone")["input_price_usd_per_1k_tokens"] == 0.002
    priced = {m["model_id"]: m for m in dynamo.list_priced_models()}
    assert priced["gone"]["archived"] is True
    assert "archived" not in priced["here"]


def test_settings_take_a_prune_mode_and_refuse_any_other():
    assert model_catalog.validate_setting("prune_mode", "frontier") is None
    assert model_catalog.validate_setting("prune_mode", "cheapest")
    assert model_catalog.validate_setting("rotation_max", 0)
    assert SETTINGS["prune_mode"] == "value_for_money"


# --- access ---------------------------------------------------------------------------------------


class FakeRuntime:
    def __init__(self, error_code=None, exc=None):
        self.error_code = error_code
        self.exc = exc
        self.calls = []

    def converse(self, **kwargs):
        from botocore.exceptions import ClientError

        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "nope"}}, "Converse")
        return {"usage": {"inputTokens": 8, "outputTokens": 1}}


@pytest.mark.real_probe
@pytest.mark.parametrize(
    "runtime, access",
    [
        (FakeRuntime(), "granted"),
        (FakeRuntime("AccessDeniedException"), "denied"),
        (FakeRuntime("ResourceNotFoundException"), "denied"),  # use-case form not submitted
        (FakeRuntime("ValidationException"), "denied"),  # not callable on demand through Converse
        (FakeRuntime("ThrottlingException"), "unknown"),
        (FakeRuntime("ServiceUnavailableException"), "unknown"),
        (FakeRuntime(exc=TimeoutError("slow")), "unknown"),
    ],
)
def test_a_probe_is_one_token_and_only_access_errors_count_as_denied(runtime, access, monkeypatch):
    recorded = []
    monkeypatch.setattr("common.stats_tracking.record_model_usage", lambda *a: recorded.append(a))
    found, why = model_catalog.probe_access("m", client=runtime)
    assert found == access
    assert runtime.calls[0]["inferenceConfig"] == {"maxTokens": 1}
    assert (why is None) == (access == "granted")
    assert recorded == ([("model_pricing", "m", 8, 1, 1)] if access == "granted" else [])


def probes(answers):
    asked = []

    def probe(model_id):
        asked.append(model_id)
        return answers.get(model_id, ("granted", None))

    return probe, asked


def found_models(*model_ids):
    return [{"model_id": m, "display_name": m, "provider": "p", "pricing_unit": "tokens"} for m in model_ids]


def test_discover_never_adds_a_model_the_account_cannot_call_and_remembers_it(aws, monkeypatch):
    monkeypatch.setattr(
        model_catalog, "account_models", lambda region, client=None: found_models("a", "b", "c")
    )
    probe, asked = probes({"a": ("denied", "AccessDeniedException: nope"), "c": ("unknown", "Throttling")})

    result = model_catalog.discover("schedule", SETTINGS, NOW, probe=probe)

    assert (result["added_ids"], result["denied_ids"]) == (["b", "c"], ["a"])
    assert dynamo.get_model("b")["access"] == "granted"
    c = dynamo.get_model("c")
    assert (c["access"], c["access_error"]) == ("unknown", "Throttling")
    assert "a" in model_catalog.recently_denied(NOW)

    # The next run neither probes nor adds the denied one; its spare probes re-check c.
    probe, asked = probes({})
    again = model_catalog.discover("schedule", SETTINGS, NOW, probe=probe)
    assert asked == ["c"]
    assert again["added"] == 0 and again["access_checks"]["granted"] == ["c"]
    # After DENIED_RECHECK it is tried again.
    later = NOW + model_catalog.DENIED_RECHECK
    assert "a" not in model_catalog.recently_denied(later)


def test_discover_stops_adding_text_models_when_its_probes_run_out(aws, monkeypatch):
    monkeypatch.setattr(
        model_catalog, "account_models", lambda region, client=None: found_models("a", "b", "c")
    )
    probe, asked = probes({})
    result = model_catalog.discover("schedule", {**SETTINGS, "access_checks_per_run": 2}, NOW, probe=probe)
    assert result["added_ids"] == ["a", "b"] and asked == ["a", "b"]


def test_the_recheck_takes_never_checked_then_unknown_then_denied_oldest_first(aws):
    put_model(aws, "granted", 0.001, access="granted")
    put_model(aws, "denied-old", 0.001, access="denied", access_checked_at="2026-01-01T00:00:00+00:00")
    put_model(aws, "unknown-new", 0.001, access="unknown", access_checked_at="2026-10-01T00:00:00+00:00")
    put_model(aws, "unknown-old", 0.001, access="unknown", access_checked_at="2026-09-01T00:00:00+00:00")
    put_model(aws, "never", 0.001)
    put_model(aws, "image", 0.001, pricing_unit="other")
    probe, asked = probes({"never": ("denied", "AccessDeniedException: x")})

    result = model_catalog.recheck_access(4, NOW, probe)

    assert asked == ["never", "unknown-old", "unknown-new", "denied-old"]
    assert result["denied"] == ["never"]
    assert dynamo.get_model("never")["access_checked_at"] == NOW.isoformat()


def test_the_recheck_probes_only_text_models_by_their_purpose_not_their_pricing_unit(aws):
    """A classified row is judged by its purpose: one Bedrock no longer lists (`unknown`, still
    token-priced) or an embeddings model with no pricing unit (a `models add` row) is no text model,
    and is never probed, though the old pricing-unit rule would have probed both."""
    put_model(aws, "words", 0.001, model_purpose="text", model_inputs=["TEXT"])
    put_model(aws, "gone", 0.001, model_purpose="unknown", pricing_unit="tokens")
    put_model(aws, "embed", 0.001, model_purpose="embedding")
    probe, asked = probes({})

    result = model_catalog.recheck_access(10, NOW, probe)

    assert asked == ["words"] and result["checked"] == 1
    assert "access_checked_at" not in dynamo.get_model("gone")
    assert "access_checked_at" not in dynamo.get_model("embed")


def test_a_denied_model_is_pruned_first_and_never_joins_or_stays_in_a_rotation(aws):
    models = [
        {"model_id": "inactive", "enabled": False},
        {"model_id": "denied", "access": "denied", "input_price_usd_per_1k_tokens": 0.001},
    ]
    assert model_fleet.prune_order(models, "value_for_money")[0]["model_id"] == "denied"

    put_model(aws, "denied", 0.0001, access="denied")
    put_model(aws, "ok", 0.001, access="granted")
    put_model(aws, "unchecked", 0.002)
    put_topic(aws, "t", model_id_candidates=["denied", "unchecked"])
    model_fleet.cycle(SETTINGS, NOW)
    assert topic(aws, "t")["model_id_candidates"] == ["unchecked", "ok"]


def test_enabling_a_model_probes_it_and_warns_when_the_account_cannot_call_it(aws, monkeypatch):
    def denied(model_id, client=None):
        return "denied", "AccessDeniedException: x"

    monkeypatch.setattr(model_catalog, "probe_access", denied)
    status, body = call(model_body("new", enabled=True))
    assert status == 200
    assert body["access"] == "denied" and "cannot call" in body["warning"]
    assert dynamo.get_model("new")["access"] == "denied"

    probed = []
    monkeypatch.setattr(model_catalog, "probe_access", lambda model_id, client=None: probed.append(model_id))
    put_model(aws, "known", 0.001, access="granted")
    assert call(model_body("known", enabled=True))[0] == 200
    assert probed == []  # already known to be callable
    assert dynamo.get_model("known")["access"] == "granted"  # kept across the rewrite


# --- suitability: what a model is for ---------------------------------------------------------------


def test_suitability_is_the_recorded_purpose_or_for_an_older_row_its_pricing_unit():
    sees = {"model_id": "sees", "model_purpose": "text", "model_inputs": ["TEXT", "IMAGE"]}
    blind = {"model_id": "blind", "model_purpose": "text", "model_inputs": ["TEXT"]}
    embed = {"model_id": "embed", "model_purpose": "embedding", "model_inputs": ["TEXT"]}
    legacy = {"model_id": "legacy"}
    legacy_other = {"model_id": "legacy-other", "pricing_unit": "other"}
    unknown = {"model_id": "unknown", "model_purpose": "unknown"}
    rows = (sees, blind, embed, legacy, legacy_other, unknown)

    assert [model_fleet.suitable(m) for m in rows] == [True, True, False, True, False, False]
    # The triage needs an image input; a legacy row's inputs are unrecorded, so it is not held to it.
    with_image = [model_fleet.suitable(m, model_fleet.TEXT_WITH_IMAGE_INPUT) for m in rows]
    assert with_image == [True, False, False, True, False, False]
    assert [model_fleet.purpose(m) for m in rows] == ["text", "text", "embedding", "text", "other", "unknown"]
    assert not model_fleet.suitable(None) and not model_fleet.suitable(sees, "DRAWING")
    assert model_fleet.NEEDS == ("TEXT", "TEXT_WITH_IMAGE_INPUT")


def test_an_embeddings_model_is_never_picked_for_a_rotation_and_is_dropped_from_one(aws):
    put_model(aws, "embed", 0.0001, model_purpose="embedding", model_inputs=["TEXT"])
    put_model(aws, "words", 0.002, model_purpose="text", model_inputs=["TEXT"])
    put_topic(aws, "t", model_id_candidates=["embed", "words"])

    picks = model_fleet.rotation_picks(dynamo.list_models(), None, [], "value_for_money")
    assert [m["model_id"] for m in picks] == ["words"]  # the cheapest, but no text model

    result = model_fleet.cycle(SETTINGS, NOW)
    assert topic(aws, "t")["model_id_candidates"] == ["words"]
    assert (result["topics_changed"], result["dropped_inactive"]) == (["t"], 1)
    # Nor does the daily cycle roll a topic to it from the shared pool; a model with no row is kept.
    assert model_fleet.rotation_pool(["embed", "words", "unregistered"]) == ["words", "unregistered"]


def test_the_pool_stands_as_configured_when_the_registry_cannot_be_read(monkeypatch):
    def down():
        raise RuntimeError("down")

    monkeypatch.setattr(model_fleet.dynamo, "list_models", down)
    assert model_fleet.rotation_pool(["a", "b"]) == ["a", "b"]


def recorded_draws(monkeypatch):
    """Every list routing's random pick draws from (sorted: the registry's order is not fixed), and
    the real pick from it."""
    drawn = []
    real_choice = model_routing.random.choice

    def recording_choice(options):
        drawn.append(sorted(options))
        return real_choice(options)

    monkeypatch.setattr(model_routing.random, "choice", recording_choice)
    return drawn


def test_an_embeddings_model_is_routed_around_like_an_inactive_one_and_replaced(aws, monkeypatch):
    put_config(aws, model_id="cfg-default", fallback_model_id="cfg-fallback")
    for model_id in ("cfg-default", "cfg-fallback", "env-default"):
        put_model(aws, model_id, 0.001, enabled=False)
    put_model(aws, "embed", 0.0001, model_purpose="embedding", model_inputs=["TEXT"])
    put_model(aws, "embed-too", 0.0001, model_purpose="embedding", model_inputs=["TEXT"])
    put_model(aws, "words", 0.002, model_purpose="text", model_inputs=["TEXT"])
    put_topic(aws, "t", model_id_candidates=["embed"])
    put_topic(aws, "pinned", model_id="embed")
    drawn = recorded_draws(monkeypatch)

    assert model_routing.resolve_model(topic(aws, "t"))[0] == "words"
    assert topic(aws, "t")["model_id_candidates"] == ["words"]
    assert drawn == [["embed"], ["words"]]  # the rotation's draw, then the registry's: text models only
    drawn.clear()
    assert model_routing.resolve_model(topic(aws, "pinned"))[0] == "words"
    assert drawn == [["words"]]  # never the other embeddings model
    assert topic(aws, "pinned")["model_id"] == "words"


def test_the_triage_gets_a_model_that_takes_an_image_and_the_topic_keeps_its_text_model(aws, monkeypatch):
    put_config(aws, model_id="cfg-default", fallback_model_id="sees")
    put_model(aws, "cfg-default", 0.001, model_purpose="text", model_inputs=["TEXT"])
    put_model(aws, "sees", 0.002, model_purpose="text", model_inputs=["TEXT", "IMAGE"])
    put_model(aws, "blind", 0.001, model_purpose="text", model_inputs=["TEXT"])
    put_topic(aws, "t", model_id="blind")
    need = model_fleet.TEXT_WITH_IMAGE_INPUT

    # The default the router hands back as the fallback takes no image either: BEDROCK_MODEL_ID
    # (no row here, so taken as fit) stands in. The vision adapters discard the fallback anyway.
    assert model_routing.resolve_model(topic(aws, "t"), need=need) == ("sees", "env-default")
    assert model_routing.resolve_model(topic(aws, "t")) == ("blind", "sees")  # the text work keeps it
    assert topic(aws, "t")["model_id"] == "blind"  # only this call went elsewhere

    # Nothing configured takes an image: a random registry model that does serves the call, and
    # the topic still keeps its model, which is fine for everything else it does.
    put_config(aws, model_id="cfg-default")
    put_model(aws, "env-default", 0.001, model_purpose="text", model_inputs=["TEXT"])
    drawn = recorded_draws(monkeypatch)
    assert model_routing.resolve_model(topic(aws, "t"), need=need)[0] == "sees"
    assert drawn == [["sees"]]  # only the models that take an image
    assert topic(aws, "t")["model_id"] == "blind"

    # A row from before purposes were recorded is not held to the image rule.
    put_model(aws, "legacy", 0.001)
    put_topic(aws, "l", model_id="legacy")
    assert model_routing.resolve_model(topic(aws, "l"), need=need)[0] == "legacy"


def test_the_triage_holds_the_default_to_its_image_need_and_writes_nothing(aws, monkeypatch):
    """A vision topic that names no model gets the default; one that takes no image is routed
    around for the triage's call, as the same model on the topic would be, and neither the topic
    nor the config changes. The text work keeps the default as it is."""
    put_config(aws, model_id="blind")
    put_model(aws, "blind", 0.001, model_purpose="text", model_inputs=["TEXT"])
    put_model(aws, "sees", 0.002, model_purpose="text", model_inputs=["TEXT", "IMAGE"])
    put_model(aws, "env-default", 0.001, model_purpose="text", model_inputs=["TEXT"])
    put_topic(aws, "ships")
    writes = []
    monkeypatch.setattr(model_routing.dynamo, "set_topic_model_fields", lambda *a: writes.append(a))
    before = (topic(aws, "ships"), dynamo.get_model_config())
    need = model_fleet.TEXT_WITH_IMAGE_INPUT

    # No fallback: neither the config nor BEDROCK_MODEL_ID takes an image.
    assert vision_triage.choose_model(topic(aws, "ships")) == ("sees", None)
    assert model_routing.resolve_model(None, need=need) == ("sees", None)
    assert model_routing.resolve_model(topic(aws, "ships")) == ("blind", "env-default")
    assert writes == [] and (topic(aws, "ships"), dynamo.get_model_config()) == before

    # With no ModelConfig row, BEDROCK_MODEL_ID is the default, and is held to the same need.
    aws.Table("ModelConfig").delete_item(Key={"config_id": "default"})
    assert vision_triage.choose_model(topic(aws, "ships")) == ("sees", None)
    assert model_routing.resolve_model(topic(aws, "ships")) == ("env-default", None)
    assert writes == [] and topic(aws, "ships") == before[0]

    # A default the registry has no row for, or one that takes an image, serves as before.
    put_model(aws, "env-default", 0.001, model_purpose="text", model_inputs=["TEXT", "IMAGE"])
    assert vision_triage.choose_model(topic(aws, "ships")) == ("env-default", None)


def test_a_fallback_that_is_not_a_text_model_gives_way_to_the_terraform_default(aws):
    put_model(aws, "words", 0.002, model_purpose="text", model_inputs=["TEXT"])
    put_model(aws, "embed", 0.0001, model_purpose="embedding", model_inputs=["TEXT"])
    put_model(aws, "gone", 0.001, model_purpose="unknown", model_inputs=["TEXT"])
    put_model(aws, "off", 0.001, enabled=False, model_purpose="text", model_inputs=["TEXT"])

    for fallback in ("embed", "gone"):
        pinned = {"topic_id": "t", "model_id": "words", "fallback_model_id": fallback}
        assert model_routing.resolve_model(pinned) == ("words", "env-default")
    # Suitability only: a disabled text fallback is handed back as it always was.
    off_fallback = {"topic_id": "t", "model_id": "words", "fallback_model_id": "off"}
    assert model_routing.resolve_model(off_fallback) == ("words", "off")
    # The config's fallback, with or without a topic.
    put_config(aws, model_id="words", fallback_model_id="embed")
    assert model_routing.resolve_model(None) == ("words", "env-default")
    assert model_routing.resolve_model({"topic_id": "t"}) == ("words", "env-default")

    # Routed around: neither the fallback the router turned down nor the default it hands back is
    # returned as the fallback when it is no text model.
    put_config(aws, model_id="off")
    put_topic(aws, "random", model_id="embed", fallback_model_id="gone")
    assert model_routing.resolve_model(topic(aws, "random")) == ("words", "env-default")
    assert topic(aws, "random")["model_id"] == "words"
    put_config(aws, model_id="embed")
    put_model(aws, "off-too", 0.001, enabled=False)
    by_fallback = {"topic_id": "t", "model_id": "off-too", "fallback_model_id": "words"}
    assert model_routing.resolve_model(by_fallback) == ("words", "env-default")


# --- protection: roles, and the evals judge ---------------------------------------------------------


def test_protection_names_each_models_role_and_the_evals_judge_when_that_row_exists(aws):
    put_config(aws, model_id="main", fallback_model_id="spare")
    topics = [{"topic_id": "t", "model_id": "tm", "fallback_model_id": "tf", "model_id_candidates": ["rot"]}]

    protected = model_fleet.protected_models(topics, dynamo.get_model_config(), ["pooled"])

    assert protected == {
        "main": "default",
        "env-default": "default",
        "spare": "fallback",
        "tm": "topic_model",
        "tf": "topic_fallback",
        "rot": "rotation",
        "pooled": "pool",
    }
    assert model_fleet.judge_model() is None  # no evals row yet: nothing to protect
    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "judge_model_id": "judge-m"})
    assert model_fleet.protected_models(topics, dynamo.get_model_config(), ["pooled"])["judge-m"] == "judge"
    assert model_fleet.judge_model({"judge_model_id": ""}) is None


def test_pruning_never_removes_the_evals_judge(aws, capsys):
    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "judge_model_id": "judge-m"})
    put_model(aws, "judge-m", 0.09, enabled=False)
    put_model(aws, "dear", 0.002, enabled=False)
    put_model(aws, "cheap", 0.001, enabled=False)
    capsys.readouterr()

    result = model_fleet.prune({**SETTINGS, "max_models": 3, "discover_max_added": 2}, NOW)

    assert result["pruned_ids"] == ["dear", "cheap"] and result["protected"] == 2
    assert "judge-m" in ids(aws)
    # One evidence line of its counts, read raw: never the pruned ids, and no field the schema lacks.
    [text] = [
        line
        for line in capsys.readouterr().out.splitlines()
        if (parsed := evidence.parse(line)) and parsed["event"] == "model_catalog_job"
    ]
    assert evidence.parse(text).items() >= {
        "job": "prune", "status": "pruned", "mode": SETTINGS["prune_mode"], "models": 3, "max_models": 3,
        "pruned": 2, "protected": 2, "short_by": 0,
    }.items()  # fmt: skip
    raw = json.loads(text)
    assert "dropped" not in raw and not {"judge-m", "dear", "cheap"} & set(map(str, raw.values()))
