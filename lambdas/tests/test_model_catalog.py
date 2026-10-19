"""The model catalog (common/model_catalog.py, model_catalog_handler.py): settings, when a job is
due, where prices come from, and a pricing run from applied to reviewed, against moto tables."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

import model_catalog_handler
from common import model_catalog as catalog

REGION = "ap-southeast-2"
BUCKET = "bloggerbear-content-test"
# A Wednesday, 05:00 in Sydney (AEDT, UTC+11).
NOW = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MODELS_TABLE": "Models",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "FAILED_EXECUTIONS_TABLE": "FailedExecutions",
        "CONTENT_BUCKET": BUCKET,
    }.items():
        monkeypatch.setenv(key, value)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, key in {
            "Models": "model_id",
            "ModelConfig": "config_id",
            "FailedExecutions": "failure_id",
        }.items():
            client.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield boto3.resource("dynamodb", region_name=REGION)
    dynamo_module._dynamodb_resource = None


def put_model(aws, model_id, inp=None, out=None, **fields):
    item = {"model_id": model_id, "display_name": f"Model {model_id}", "provider": "test", **fields}
    if inp is not None:
        item["input_price_usd_per_1k_tokens"] = Decimal(str(inp))
        item["output_price_usd_per_1k_tokens"] = Decimal(str(out))
    aws.Table("Models").put_item(Item=item)


def model(aws, model_id):
    return aws.Table("Models").get_item(Key={"model_id": model_id}).get("Item")


def config_row(aws, config_id):
    return aws.Table("ModelConfig").get_item(Key={"config_id": config_id}).get("Item")


class FakeLookup:
    """Answers from a dict of model id -> (input, output), or None/UNCHANGED."""

    def __init__(self, prices):
        self.prices = prices
        self.asked = []

    def lookup(self, row):
        self.asked.append(row["model_id"])
        found = self.prices.get(row["model_id"])
        if found is None or found == catalog.UNCHANGED:
            return found
        return catalog.Price(found[0], found[1], "price-list")


SETTINGS = catalog.effective_settings(None)


def start_run(aws, prices, now=NOW):
    return catalog.reprice("admin", SETTINGS, now, lookup=FakeLookup(prices))


def active(aws):
    return catalog.active_run()


# --- settings -----------------------------------------------------------------------------------


def test_every_setting_has_a_default_and_invalid_values_fall_back_to_it():
    assert catalog.effective_settings(None)["reprice_weekday"] == "WED"
    settings = catalog.effective_settings(
        {"discover_weekday": "FRI", "run_hour": 99, "review_after_hours": [1], "timezone": "Mars/Base"}
    )
    assert settings["discover_weekday"] == "FRI"
    assert settings["run_hour"] == 3
    assert settings["review_after_hours"] == [1]
    assert settings["timezone"] == "Australia/Sydney"


@pytest.mark.parametrize(
    "name, value",
    [
        ("discover_enabled", "yes"),
        ("reprice_weekday", "Wednesday"),
        ("run_hour", 24),
        ("run_hour", True),
        ("review_after_hours", []),
        ("review_after_hours", [1, 2, 3, 4, 5]),
        ("review_after_hours", [0]),
        ("max_change_factor", 1),
        ("timezone", "Nowhere/Special"),
        ("not_a_setting", 1),
    ],
)
def test_invalid_settings_are_refused(name, value):
    assert catalog.validate_setting(name, value)


def test_none_clears_any_setting():
    assert catalog.validate_setting("run_hour", None) is None


# --- when a job is due --------------------------------------------------------------------------


def test_the_last_scheduled_moment_is_read_in_the_settings_time_zone():
    # Wednesday 05:00 Sydney: the last Tuesday 03:00 Sydney was the day before, 16:00 UTC Monday.
    moment = catalog.last_scheduled_moment("TUE", 3, "Australia/Sydney", NOW)
    assert moment == datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
    # Before Wednesday's 03:00 the last Wednesday moment is a week back.
    early = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)  # Wednesday 02:00 Sydney
    assert catalog.last_scheduled_moment("WED", 3, "Australia/Sydney", early) == datetime(
        2026, 10, 13, 16, 0, tzinfo=UTC
    )


def test_a_job_is_due_once_after_its_moment_and_caught_up_when_missed():
    assert catalog.is_due("reprice", SETTINGS, {}, NOW)  # never ran
    ran_this_morning = {"last_reprice_at": (NOW - timedelta(hours=1)).isoformat()}
    assert not catalog.is_due("reprice", SETTINGS, ran_this_morning, NOW)
    ran_last_week = {"last_reprice_at": (NOW - timedelta(days=6)).isoformat()}
    assert catalog.is_due("reprice", SETTINGS, ran_last_week, NOW)
    # Missed on its Wednesday: still due on the Thursday.
    assert catalog.is_due("reprice", SETTINGS, ran_last_week, NOW + timedelta(days=1))


# --- where a price comes from -------------------------------------------------------------------


def product(model, inference, unit, usd, feature="On-demand Inference"):
    return {
        "product": {"attributes": {"model": model, "inferenceType": inference, "feature": feature}},
        "terms": {
            "OnDemand": {"t": {"priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": usd}}}}}
        },
    }


class FakePricing:
    def __init__(self, products, fail_codes=()):
        self.products = products
        self.fail_codes = fail_codes
        self.calls = []

    def get_paginator(self, name):
        assert name == "get_products"
        return self

    def paginate(self, ServiceCode, Filters, FormatVersion):
        self.calls.append((ServiceCode, Filters))
        if ServiceCode in self.fail_codes:
            raise RuntimeError("no such service")
        if ServiceCode == "AmazonBedrock":
            return [{"PriceList": [json.dumps(p) for p in self.products]}]
        return [{"PriceList": []}]


def test_the_price_list_gives_on_demand_token_prices_per_1k():
    pricing = FakePricing(
        [
            product("Claude Haiku 4.5", "Input tokens", "1M tokens", "1.00"),
            product("Claude Haiku 4.5", "Output tokens", "1M tokens", "5.00"),
            product("Claude Haiku 4.5", "Input tokens batch", "1M tokens", "0.50"),
            product("Claude Haiku 4.5", "Cache read input tokens", "1M tokens", "0.10"),
            product("Claude Haiku 4.5", "Input tokens", "1M tokens", "0.40", feature="Batch Inference"),
            product("Nova Lite", "Input tokens", "1K tokens", "0.00006"),
            product("Nova Lite", "Output tokens", "1K tokens", "0.00024"),
        ],
        fail_codes=("AmazonBedrockService",),
    )
    price_list = catalog.PriceList(REGION, client=pricing)

    haiku = price_list.price({"model_id": "x", "display_name": "claude-haiku-4.5"})
    assert (haiku.input_per_1k, haiku.output_per_1k, haiku.source) == (0.001, 0.005, "price-list")
    nova = price_list.price({"model_id": "y", "display_name": "Nova Lite"})
    assert (nova.input_per_1k, nova.output_per_1k) == (0.00006, 0.00024)
    assert price_list.price({"model_id": "z", "display_name": "Unknown"}) is None
    assert {code for code, _ in pricing.calls} == set(catalog.PRICE_LIST_SERVICE_CODES)  # read once
    assert pricing.calls[0][1] == [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": REGION}]


def test_an_ambiguous_or_one_sided_price_list_entry_prices_nothing():
    pricing = FakePricing(
        [
            product("Two Prices", "Input tokens", "1K tokens", "0.001"),
            product("Two Prices", "Input tokens", "1K tokens", "0.002"),
            product("Two Prices", "Output tokens", "1K tokens", "0.004"),
            product("Input Only", "Input tokens", "1K tokens", "0.001"),
            product("Odd Unit", "Input tokens", "image", "0.04"),
            product("Odd Unit", "Output tokens", "image", "0.04"),
        ]
    )
    price_list = catalog.PriceList(REGION, client=pricing)
    for name in ("Two Prices", "Input Only", "Odd Unit"):
        assert price_list.price({"model_id": name, "display_name": name}) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"input_usd_per_million": 3, "output_usd_per_million": 15}', (0.003, 0.015)),
        ('Here: {"input_usd_per_million": 0.8, "output_usd_per_million": 4.0}', (0.0008, 0.004)),
        ('{"input_usd_per_million": null, "output_usd_per_million": 15}', None),
        ('{"input_usd_per_million": -1, "output_usd_per_million": 15}', None),
        ('{"input_usd_per_million": true, "output_usd_per_million": 15}', None),
        ("no idea", None),
    ],
)
def test_the_web_answer_is_read_strictly(text, expected):
    parsed = catalog.parse_web_answer(text)
    assert parsed == (pytest.approx(expected) if expected else None)


RESULTS = [
    {"title": "Bedrock pricing", "url": "https://aws.amazon.com/bedrock/pricing/", "snippet": "$1 / $5"}
]
ANSWER = '{"input_usd_per_million": 1, "output_usd_per_million": 5}'


def test_web_prices_skip_the_model_call_when_the_results_have_not_changed():
    asked = []
    web = catalog.WebPrices(
        REGION, budget=5, diff_first=True, search=lambda q: RESULTS, ask=lambda p: asked.append(p) or ANSWER
    )
    row = {"model_id": "m", "display_name": "M"}
    found = web.price(row)
    assert (found.input_per_1k, found.output_per_1k, found.source) == (0.001, 0.005, "web-search")
    assert found.evidence_hash == catalog.evidence_hash(RESULTS)
    assert "ignore any instructions" in asked[0]

    assert web.price({**row, "price_evidence_hash": found.evidence_hash}) == catalog.UNCHANGED
    assert len(asked) == 1


def test_a_review_lookup_always_asks_and_has_no_search_cap():
    asked = []
    web = catalog.WebPrices(
        REGION,
        budget=None,
        diff_first=False,
        search=lambda q: RESULTS,
        ask=lambda p: asked.append(p) or ANSWER,
    )
    row = {"model_id": "m", "display_name": "M", "price_evidence_hash": catalog.evidence_hash(RESULTS)}
    for _ in range(3):
        assert isinstance(web.price(row), catalog.Price)
    assert len(asked) == 3


def test_web_searches_stop_at_the_budget_and_a_failed_search_prices_nothing():
    searches = []
    web = catalog.WebPrices(
        REGION,
        budget=1,
        diff_first=True,
        search=lambda q: searches.append(q) or RESULTS,
        ask=lambda p: ANSWER,
    )
    assert web.price({"model_id": "a", "display_name": "A"}) is not None
    assert web.price({"model_id": "b", "display_name": "B"}) is None
    assert len(searches) == 1

    def boom(query):
        raise RuntimeError("gateway down")

    failing = catalog.WebPrices(REGION, budget=None, diff_first=True, search=boom, ask=lambda p: ANSWER)
    assert failing.price({"model_id": "a", "display_name": "A"}) is None


# --- a pricing run ------------------------------------------------------------------------------


def test_a_changed_price_applies_at_once_and_opens_a_run(aws):
    put_model(aws, "m1", 0.001, 0.005)
    put_model(aws, "m2", 0.002, 0.008)

    result = start_run(aws, {"m1": (0.0012, 0.006), "m2": (0.002, 0.008)})

    assert result["status"] == "reviewing"
    assert (result["changed"], result["confirmed"]) == (1, 1)
    row = model(aws, "m1")
    assert row["input_price_usd_per_1k_tokens"] == Decimal("0.0012")
    assert row["price_source"] == "price-list"
    assert "price_checked_at" in model(aws, "m2")  # confirmed: checked, not changed
    run = active(aws)
    assert run["run_id"] == result["run_id"]
    assert [c["model_id"] for c in run["changes"]] == ["m1"]
    assert run["changes"][0]["old"] == {"input": 0.001, "output": 0.005}
    assert len(run["reviews_due"]) == 2
    assert run["reviews_due"][0] == (NOW + timedelta(hours=2)).isoformat()


def test_no_change_opens_no_run_and_frees_the_slot(aws):
    put_model(aws, "m1", 0.001, 0.005)
    result = start_run(aws, {"m1": (0.001, 0.005)})
    assert result["status"] == "no_change"
    assert active(aws) is None
    assert "active_run_id" not in (config_row(aws, catalog.STATE_ID) or {})


def test_locked_and_non_token_models_are_never_repriced(aws):
    put_model(aws, "locked", 0.001, 0.005, price_locked=True)
    put_model(aws, "image", pricing_unit="other")
    lookup = FakeLookup({"locked": (0.5, 0.5), "image": (0.1, 0.1)})
    catalog.reprice("admin", SETTINGS, NOW, lookup=lookup)
    assert lookup.asked == []


def test_unpriced_models_are_listed(aws):
    put_model(aws, "nothing")
    put_model(aws, "unchanged")
    result = start_run(aws, {"unchanged": catalog.UNCHANGED})
    assert result["unpriced"] == ["nothing", "unchanged"]


def test_a_reprice_waits_while_a_run_is_under_review(aws):
    put_model(aws, "m1", 0.001, 0.005)
    first = start_run(aws, {"m1": (0.0012, 0.006)})
    second = start_run(aws, {"m1": (0.0013, 0.007)})
    assert second == {"status": "review_pending", "run_id": first["run_id"]}
    assert model(aws, "m1")["input_price_usd_per_1k_tokens"] == Decimal("0.0012")


def test_two_passed_reviews_keep_the_price_and_snapshot_the_old_one(aws):
    put_model(aws, "m1", 0.001, 0.005)
    run_id = start_run(aws, {"m1": (0.0012, 0.006)})["run_id"]
    fresh = FakeLookup({"m1": (0.00121, 0.006)})  # within 5%

    first = catalog.review(active(aws), SETTINGS, NOW + timedelta(hours=2), lookup=fresh)
    assert (first["status"], first["review"], first["passed"]) == ("reviewing", 1, 1)
    second = catalog.review(active(aws), SETTINGS, NOW + timedelta(hours=24), lookup=fresh)
    assert (second["status"], second["review"]) == ("passed", 2)

    assert model(aws, "m1")["input_price_usd_per_1k_tokens"] == Decimal("0.0012")
    assert active(aws) is None
    run = config_row(aws, f"{catalog.RUN_PREFIX}{run_id}")
    assert run["status"] == "passed"
    assert run["expires_at"] > int(NOW.timestamp())
    key = run["snapshot_key"]
    assert key == f"pricing/2026-10-21-{run_id}.json"
    body = json.loads(
        boto3.client("s3", region_name=REGION).get_object(Bucket=BUCKET, Key=key)["Body"].read()
    )
    assert body["models"] == [
        {
            "model_id": "m1",
            "old": {"input": 0.001, "output": 0.005},
            "new": {"input": 0.0012, "output": 0.006},
        }
    ]
    assert body["written_at"].startswith("2026-10-21")
    assert aws.Table("FailedExecutions").scan()["Items"] == []


@pytest.mark.parametrize(
    "fresh, why",
    [
        ({"m1": (0.0015, 0.006)}, "a fresh lookup found 0.0015 for the input price"),
        ({}, "a fresh lookup could not confirm the price"),
    ],
)
def test_a_review_that_is_not_confident_puts_the_old_price_back(aws, fresh, why):
    put_model(
        aws, "m1", 0.001, 0.005, price_source="web-search", price_checked_at="2026-10-01T00:00:00+00:00"
    )
    put_model(aws, "m2", 0.002, 0.008)
    start_run(aws, {"m1": (0.0012, 0.006), "m2": (0.0021, 0.0084)})

    result = catalog.review(
        active(aws), SETTINGS, NOW + timedelta(hours=2), lookup=FakeLookup({**fresh, "m2": (0.0021, 0.0084)})
    )

    assert result["reverted"] == ["m1"]
    row = model(aws, "m1")
    assert row["input_price_usd_per_1k_tokens"] == Decimal("0.001")
    assert row["price_source"] == "web-search"
    assert row["price_checked_at"] == "2026-10-01T00:00:00+00:00"
    assert model(aws, "m2")["input_price_usd_per_1k_tokens"] == Decimal("0.0021")  # still on probation
    [failure] = aws.Table("FailedExecutions").scan()["Items"]
    assert failure["kind"] == "model_pricing"
    assert failure["topic_id"] is None
    assert failure["error"]["Error"] == "ModelPricingReverted"
    assert why in failure["error"]["Cause"]
    assert json.loads(failure["raw_message"])["reverted"][0]["model_id"] == "m1"
    assert active(aws)["reverted"][0]["model_id"] == "m1"


def test_a_new_models_price_is_removed_again_when_it_fails(aws):
    put_model(aws, "new")
    run_id = start_run(aws, {"new": (0.9, 0.9)})["run_id"]  # over the ceiling
    result = catalog.review(
        active(aws), SETTINGS, NOW + timedelta(hours=2), lookup=FakeLookup({"new": (0.9, 0.9)})
    )
    assert result["status"] == "reverted"  # nothing left on probation: finished at once
    row = model(aws, "new")
    assert "input_price_usd_per_1k_tokens" not in row
    assert "price_source" not in row
    assert config_row(aws, f"{catalog.RUN_PREFIX}{run_id}")["status"] == "reverted"
    assert active(aws) is None


def test_a_price_that_moved_too_far_fails_even_when_confirmed(aws):
    put_model(aws, "m1", 0.001, 0.005)
    start_run(aws, {"m1": (0.02, 0.005)})
    result = catalog.review(
        active(aws), SETTINGS, NOW + timedelta(hours=2), lookup=FakeLookup({"m1": (0.02, 0.005)})
    )
    assert result["reverted"] == ["m1"]
    cause = aws.Table("FailedExecutions").scan()["Items"][0]["error"]["Cause"]
    assert "moved more than 10x" in cause


def test_a_price_changed_by_hand_since_is_left_alone(aws):
    put_model(aws, "m1", 0.001, 0.005)
    start_run(aws, {"m1": (0.0012, 0.006)})
    put_model(aws, "m1", 0.003, 0.009)  # the operator set it meanwhile

    result = catalog.review(active(aws), SETTINGS, NOW + timedelta(hours=2), lookup=FakeLookup({}))

    assert result["dropped"] == ["m1"]
    assert result["status"] == "dropped"
    assert model(aws, "m1")["input_price_usd_per_1k_tokens"] == Decimal("0.003")
    assert aws.Table("FailedExecutions").scan()["Items"] == []


# --- discovery ----------------------------------------------------------------------------------


class FakeBedrock:
    def list_foundation_models(self):
        return {
            "modelSummaries": [
                {
                    "modelId": "amazon.nova-lite-v1:0",
                    "modelName": "Nova Lite",
                    "providerName": "Amazon",
                    "outputModalities": ["TEXT"],
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "modelLifecycle": {"status": "ACTIVE"},
                },
                {
                    "modelId": "anthropic.claude-haiku-4-5-20251001-v1:0",
                    "modelName": "Claude Haiku 4.5",
                    "providerName": "Anthropic",
                    "outputModalities": ["TEXT"],
                    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
                    "modelLifecycle": {"status": "ACTIVE"},
                },
                {
                    "modelId": "amazon.titan-embed-text-v2:0",
                    "modelName": "Titan Embeddings",
                    "providerName": "Amazon",
                    "outputModalities": ["EMBEDDING"],
                    "inferenceTypesSupported": ["ON_DEMAND"],
                },
                {
                    "modelId": "old.model-v1",
                    "modelName": "Old",
                    "providerName": "Old",
                    "outputModalities": ["TEXT"],
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "modelLifecycle": {"status": "LEGACY"},
                },
            ]
        }

    def list_inference_profiles(self, typeEquals, nextToken=None):
        arn = "arn:aws:bedrock:ap-southeast-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
        if nextToken is None:
            return {
                "inferenceProfileSummaries": [
                    {
                        "inferenceProfileId": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
                        "models": [{"modelArn": arn}],
                    }
                ],
                "nextToken": "page2",
            }
        return {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileId": "apac.amazon.titan-embed-text-v2:0",
                    "models": [
                        {"modelArn": "arn:aws:bedrock:::foundation-model/amazon.titan-embed-text-v2:0"}
                    ],
                }
            ]
        }


def test_account_models_are_the_text_models_callable_on_demand_or_by_profile():
    found = catalog.account_models(REGION, FakeBedrock())
    assert found == [
        {"model_id": "amazon.nova-lite-v1:0", "display_name": "Nova Lite", "provider": "amazon"},
        {
            "model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
            "display_name": "Claude Haiku 4.5",
            "provider": "anthropic",
        },
    ]


def test_discover_adds_new_models_disabled_and_prices_them(aws):
    put_model(aws, "amazon.nova-lite-v1:0", 0.00006, 0.00024, enabled=True)
    lookup = FakeLookup({"au.anthropic.claude-haiku-4-5-20251001-v1:0": (0.0011, 0.0055)})

    result = catalog.discover("schedule", SETTINGS, NOW, bedrock=FakeBedrock(), lookup=lookup)

    assert result["added_ids"] == ["au.anthropic.claude-haiku-4-5-20251001-v1:0"]
    assert lookup.asked == ["au.anthropic.claude-haiku-4-5-20251001-v1:0"]  # only the new one
    row = model(aws, "au.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert row["enabled"] is False
    assert row["region"] == REGION
    assert row["pricing_unit"] == "tokens"
    assert row["input_price_usd_per_1k_tokens"] == Decimal("0.0011")
    assert model(aws, "amazon.nova-lite-v1:0")["enabled"] is True  # untouched
    assert result["pricing"]["status"] == "reviewing"


# --- the heartbeat ------------------------------------------------------------------------------


def test_paused_jobs_are_skipped_but_a_running_review_still_finishes(aws, monkeypatch):
    put_model(aws, "m1", 0.001, 0.005)
    start_run(aws, {"m1": (0.0012, 0.006)})
    aws.Table("ModelConfig").put_item(
        Item={"config_id": catalog.CONFIG_ID, "discover_enabled": False, "reprice_enabled": False}
    )
    reviewed = []
    monkeypatch.setattr(catalog, "review", lambda run, settings, now: reviewed.append(run["run_id"]) or {})

    result = catalog.heartbeat({}, NOW + timedelta(hours=3))

    assert result["discover"] == {"status": "paused"}
    assert result["reprice"] == {"status": "paused"}
    assert len(reviewed) == 1


def test_a_review_runs_only_when_it_is_due(aws, monkeypatch):
    put_model(aws, "m1", 0.001, 0.005)
    start_run(aws, {"m1": (0.0012, 0.006)})
    aws.Table("ModelConfig").put_item(
        Item={"config_id": catalog.CONFIG_ID, "discover_enabled": False, "reprice_enabled": False}
    )
    monkeypatch.setattr(catalog, "review", lambda run, settings, now: pytest.fail("not due yet"))
    assert "review" not in catalog.heartbeat({}, NOW + timedelta(hours=1))


def test_due_jobs_run_and_are_recorded_and_an_admin_job_runs_whatever_the_day(aws, monkeypatch):
    ran = []
    monkeypatch.setattr(
        catalog, "discover", lambda trigger, s, now: ran.append(("discover", trigger)) or {"added": 0}
    )
    monkeypatch.setattr(
        catalog, "reprice", lambda trigger, s, now: ran.append(("reprice", trigger)) or {"changed": 0}
    )

    catalog.heartbeat({}, NOW)
    assert ran == [("discover", "schedule"), ("reprice", "schedule")]  # neither ever ran
    state = config_row(aws, catalog.STATE_ID)
    assert state["last_reprice_at"] == NOW.isoformat()
    assert state["last_reprice_result"]["trigger"] == "schedule"

    ran.clear()
    catalog.heartbeat({}, NOW + timedelta(hours=1))
    assert ran == []  # done for the week

    catalog.heartbeat({"job": "reprice"}, NOW + timedelta(hours=2))
    assert ran == [("reprice", "admin")]


def test_a_slot_left_by_a_run_that_died_is_freed_after_an_hour(aws):
    old_id = (NOW - timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ") + "-abcdef"
    aws.Table("ModelConfig").put_item(Item={"config_id": catalog.STATE_ID, "active_run_id": old_id})
    aws.Table("ModelConfig").put_item(
        Item={"config_id": catalog.CONFIG_ID, "discover_enabled": False, "reprice_enabled": False}
    )
    catalog.heartbeat({}, NOW)
    assert "active_run_id" not in config_row(aws, catalog.STATE_ID)

    fresh_id = NOW.strftime("%Y%m%dT%H%M%SZ") + "-abcdef"
    aws.Table("ModelConfig").put_item(Item={"config_id": catalog.STATE_ID, "active_run_id": fresh_id})
    catalog.heartbeat({}, NOW + timedelta(minutes=5))
    assert config_row(aws, catalog.STATE_ID)["active_run_id"] == fresh_id  # still pricing


def test_the_summary_shows_the_run_under_review(aws):
    put_model(aws, "m1", 0.001, 0.005)
    run_id = start_run(aws, {"m1": (0.0012, 0.006)})["run_id"]
    summary = catalog.summary()
    assert summary["effective"]["reprice_weekday"] == "WED"
    assert summary["settings"]["reprice_weekday"] is None
    assert summary["under_review"]["run_id"] == run_id
    assert summary["under_review"]["models"] == ["m1"]
    assert summary["under_review"]["next_review_at"] == (NOW + timedelta(hours=2)).isoformat()


def test_the_handler_never_raises(monkeypatch):
    def boom(event):
        raise RuntimeError("nope")

    monkeypatch.setattr(catalog, "heartbeat", boom)
    monkeypatch.setattr("common.lambda_timing.record_lambda_duration", lambda *a: None)
    assert model_catalog_handler.handler({}, None) == {"status": "error", "error": "nope"}
