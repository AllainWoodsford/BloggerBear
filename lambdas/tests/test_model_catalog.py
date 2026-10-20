"""The model catalog (common/model_catalog.py, model_catalog_handler.py): settings, where prices
come from, discovery, a pricing run from applied to reviewed, and what each kind of invocation
does, against moto tables."""

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
        "ENVIRONMENT_NAME": "dev",
        "MODEL_CATALOG_FUNCTION_ARN": "arn:aws:lambda:ap-southeast-2:123456789012:function:mc",
        "SCHEDULER_INVOKE_ROLE_ARN": "arn:aws:iam::123456789012:role/scheduler-invoke",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(catalog, "_get_scheduler", lambda: SCHEDULER)
    SCHEDULER.created.clear()
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


class FakeScheduler:
    def __init__(self):
        self.created = []

    def create_schedule(self, **kwargs):
        self.created.append(kwargs)


SCHEDULER = FakeScheduler()


def start_run(aws, prices, now=NOW):
    return catalog.reprice("admin", SETTINGS, now, lookup=FakeLookup(prices))


def active(aws):
    return catalog.active_run()


# --- settings -----------------------------------------------------------------------------------


def test_every_setting_has_a_default_and_invalid_values_fall_back_to_it():
    assert catalog.effective_settings(None)["review_after_hours"] == [1, 2]
    settings = catalog.effective_settings({"reprice_enabled": False, "max_change_factor": 1})
    assert settings["reprice_enabled"] is False
    assert settings["max_change_factor"] == 10


@pytest.mark.parametrize(
    "name, value",
    [
        ("discover_enabled", "yes"),
        ("review_after_hours", []),
        ("review_after_hours", [1, 2, 3, 4, 5]),
        ("review_after_hours", [0]),
        ("review_tolerance_pct", True),
        ("max_change_factor", 1),
        ("reprice_weekday", "WED"),  # the days are the schedules' (Terraform), not a setting
        ("not_a_setting", 1),
    ],
)
def test_invalid_settings_are_refused(name, value):
    assert catalog.validate_setting(name, value)


def test_none_clears_any_setting():
    assert catalog.validate_setting("max_web_lookups", None) is None


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
    assert run["reviews_due"] == [(NOW + timedelta(hours=h)).isoformat() for h in (1, 2)]


def test_a_run_schedules_its_own_reviews_which_delete_themselves(aws):
    put_model(aws, "m1", 0.001, 0.005)
    run_id = start_run(aws, {"m1": (0.0012, 0.006)})["run_id"]

    assert [c["Name"] for c in SCHEDULER.created] == [
        f"bloggerbear-dev-price-review-{run_id}-1",
        f"bloggerbear-dev-price-review-{run_id}-2",
    ]
    first = SCHEDULER.created[0]
    assert first["ScheduleExpression"] == "at(2026-10-20T19:00:00)"
    assert first["ScheduleExpressionTimezone"] == "UTC"
    assert first["ActionAfterCompletion"] == "DELETE"
    assert first["Target"]["Arn"].endswith(":function:mc")
    assert json.loads(first["Target"]["Input"]) == {"job": "review", "run_id": run_id}
    # The longest prefix Terraform accepts (14) and the longer environment still fit in 64.
    longest = f"{'x' * 14}-production-price-review-{run_id}-4"
    assert len(longest) <= 64


def test_a_run_whose_reviews_could_not_be_scheduled_still_opens(aws, monkeypatch):
    class Refusing:
        def create_schedule(self, **kwargs):
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr(catalog, "_get_scheduler", lambda: Refusing())
    put_model(aws, "m1", 0.001, 0.005)
    assert start_run(aws, {"m1": (0.0012, 0.006)})["status"] == "reviewing"


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
    second = catalog.review(active(aws), SETTINGS, NOW + timedelta(hours=26), lookup=fresh)
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
                    "modelId": "stability.sd3-large-v1:0",
                    "modelName": "SD3 Large",
                    "providerName": "Stability AI",
                    "outputModalities": ["IMAGE"],
                    "inferenceTypesSupported": ["ON_DEMAND"],
                    "modelLifecycle": {"status": "ACTIVE"},
                },
                {
                    "modelId": "meta.llama3-3-70b-instruct-v1:0",
                    "modelName": "Llama 3.3 70B Instruct",
                    "providerName": "Meta",
                    "outputModalities": ["TEXT"],
                    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
                    "modelLifecycle": {"status": "ACTIVE"},
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
                    },
                    {
                        "inferenceProfileId": "apac.meta.llama3-3-70b-instruct-v1:0",
                        "models": [
                            {"modelArn": "arn:aws:bedrock:::foundation-model/meta.llama3-3-70b-instruct-v1:0"}
                        ],
                    },
                    {
                        "inferenceProfileId": "apac.old.model-v1",
                        "models": [{"modelArn": "arn:aws:bedrock:::foundation-model/old.model-v1"}],
                    },
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


def test_account_models_are_every_providers_models_callable_on_demand_or_by_profile():
    found = {
        m["model_id"]: (m["provider"], m["pricing_unit"])
        for m in catalog.account_models(REGION, FakeBedrock())
    }
    assert found == {
        "amazon.nova-lite-v1:0": ("amazon", "tokens"),
        "amazon.titan-embed-text-v2:0": ("amazon", "other"),
        "apac.amazon.titan-embed-text-v2:0": ("amazon", "other"),
        "apac.meta.llama3-3-70b-instruct-v1:0": ("meta", "tokens"),
        "au.anthropic.claude-haiku-4-5-20251001-v1:0": ("anthropic", "tokens"),
        "stability.sd3-large-v1:0": ("stability ai", "other"),
    }
    # Not callable on demand (only through a profile), or no longer active: not listed.
    assert "anthropic.claude-haiku-4-5-20251001-v1:0" not in found
    assert "apac.old.model-v1" not in found


def test_discover_adds_new_models_disabled_and_prices_them(aws):
    put_model(aws, "amazon.nova-lite-v1:0", 0.00006, 0.00024, enabled=True)
    lookup = FakeLookup({"au.anthropic.claude-haiku-4-5-20251001-v1:0": (0.0011, 0.0055)})

    result = catalog.discover("schedule", SETTINGS, NOW, bedrock=FakeBedrock(), lookup=lookup)

    assert result["added"] == 5
    # Only the new token-priced ones are priced; the image and embedding models are listed only.
    assert sorted(lookup.asked) == [
        "apac.meta.llama3-3-70b-instruct-v1:0",
        "au.anthropic.claude-haiku-4-5-20251001-v1:0",
    ]
    image = model(aws, "stability.sd3-large-v1:0")
    assert (image["enabled"], image["pricing_unit"], image["provider"]) == (False, "other", "stability ai")
    assert "input_price_usd_per_1k_tokens" not in image
    row = model(aws, "au.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert row["enabled"] is False
    assert row["region"] == REGION
    assert row["pricing_unit"] == "tokens"
    assert row["input_price_usd_per_1k_tokens"] == Decimal("0.0011")
    assert model(aws, "amazon.nova-lite-v1:0")["enabled"] is True  # untouched
    assert result["pricing"]["status"] == "reviewing"


# --- invocations --------------------------------------------------------------------------------


def pause_both(aws):
    aws.Table("ModelConfig").put_item(
        Item={"config_id": catalog.CONFIG_ID, "discover_enabled": False, "reprice_enabled": False}
    )


def record_jobs(monkeypatch):
    ran = []
    monkeypatch.setattr(
        catalog, "discover", lambda trigger, s, now: ran.append(("discover", trigger)) or {"added": 0}
    )
    monkeypatch.setattr(
        catalog, "reprice", lambda trigger, s, now: ran.append(("reprice", trigger)) or {"changed": 0}
    )
    return ran


def test_a_scheduled_job_runs_and_is_recorded_unless_paused_and_the_cli_runs_it_anyway(aws, monkeypatch):
    ran = record_jobs(monkeypatch)

    catalog.handle({"job": "reprice", "trigger": "schedule"}, NOW)
    assert ran == [("reprice", "schedule")]
    state = config_row(aws, catalog.STATE_ID)
    assert state["last_reprice_at"] == NOW.isoformat()
    assert state["last_reprice_result"]["trigger"] == "schedule"

    pause_both(aws)
    ran.clear()
    assert catalog.handle({"job": "discover", "trigger": "schedule"}, NOW) == {
        "status": "paused",
        "job": "discover",
    }
    assert ran == []
    catalog.handle({"job": "discover"}, NOW)
    assert ran == [("discover", "admin")]


def test_an_event_with_no_job_does_nothing(aws, monkeypatch):
    ran = record_jobs(monkeypatch)
    assert catalog.handle({}, NOW)["status"] == "ignored"
    assert ran == []


def test_a_review_schedule_runs_its_runs_next_review_when_due(aws, monkeypatch):
    put_model(aws, "m1", 0.001, 0.005)
    run_id = start_run(aws, {"m1": (0.0012, 0.006)})["run_id"]
    pause_both(aws)  # pausing stops the schedules, never a run's reviews

    early = catalog.handle({"job": "review", "run_id": run_id}, NOW + timedelta(minutes=30))
    assert early["review"]["status"] == "not_due"
    assert catalog.handle({"job": "review", "run_id": "other"}, NOW)["review"]["status"] == "stale"

    # A fresh lookup agrees with the applied price. The schedule may fire a moment early.
    agreeing = FakeLookup({"m1": (0.0012, 0.006)})
    monkeypatch.setattr(catalog, "PriceLookup", lambda *a, **k: agreeing)
    first = catalog.handle({"job": "review", "run_id": run_id}, NOW + timedelta(hours=1, seconds=-2))
    second = catalog.handle({"job": "review", "run_id": run_id}, NOW + timedelta(hours=2))
    assert (first["review"]["review"], first["review"]["status"]) == (1, "reviewing")
    assert (second["review"]["review"], second["review"]["status"]) == (2, "passed")


def test_a_job_first_runs_the_reviews_whose_schedules_were_lost(aws, monkeypatch):
    put_model(aws, "m1", 0.001, 0.005)
    start_run(aws, {"m1": (0.0012, 0.006)})
    reviewed = []
    real_review = catalog.review

    def fake_review(run, settings, now):
        reviewed.append(len(run["reviews_done"]) + 1)
        return real_review(run, settings, now, lookup=FakeLookup({"m1": (0.0012, 0.006)}))

    monkeypatch.setattr(catalog, "review", fake_review)
    monkeypatch.setattr(catalog, "reprice", lambda trigger, s, now: {"changed": 0})

    result = catalog.handle({"job": "reprice"}, NOW + timedelta(days=1))

    assert reviewed == [1, 2]
    assert [r["status"] for r in result["overdue_reviews"]] == ["reviewing", "passed"]
    assert active(aws) is None
    assert not catalog.is_overdue(None, NOW)


def test_a_slot_left_by_a_run_that_died_is_freed_after_an_hour(aws, monkeypatch):
    record_jobs(monkeypatch)
    old_id = (NOW - timedelta(hours=2)).strftime(catalog.RUN_ID_TIME) + "-abcdef"
    aws.Table("ModelConfig").put_item(Item={"config_id": catalog.STATE_ID, "active_run_id": old_id})
    catalog.handle({"job": "discover"}, NOW)
    assert "active_run_id" not in config_row(aws, catalog.STATE_ID)

    fresh_id = NOW.strftime(catalog.RUN_ID_TIME) + "-abcdef"
    aws.Table("ModelConfig").put_item(Item={"config_id": catalog.STATE_ID, "active_run_id": fresh_id})
    catalog.handle({"job": "discover"}, NOW + timedelta(minutes=5))
    assert config_row(aws, catalog.STATE_ID)["active_run_id"] == fresh_id  # still pricing


def test_the_summary_shows_the_run_under_review(aws):
    put_model(aws, "m1", 0.001, 0.005)
    run_id = start_run(aws, {"m1": (0.0012, 0.006)})["run_id"]
    summary = catalog.summary()
    assert summary["effective"]["reprice_enabled"] is True
    assert summary["settings"]["reprice_enabled"] is None
    assert summary["under_review"]["run_id"] == run_id
    assert summary["under_review"]["models"] == ["m1"]
    assert summary["under_review"]["next_review_at"] == (NOW + timedelta(hours=1)).isoformat()


def test_the_handler_never_raises(monkeypatch):
    def boom(event):
        raise RuntimeError("nope")

    monkeypatch.setattr(catalog, "handle", boom)
    monkeypatch.setattr("common.lambda_timing.record_lambda_duration", lambda *a: None)
    assert model_catalog_handler.handler({}, None) == {"status": "error", "error": "nope"}
