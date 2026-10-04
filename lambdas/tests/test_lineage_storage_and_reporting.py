"""Where the new lineage data is stored and where it is shown: DynamoDB round
trips, the daily cycle's research bundle, the Stats aggregation, the static
article page, and the admin audit/backfill routes."""

from __future__ import annotations

import json
from datetime import date

import boto3
import pytest
from moto import mock_aws
from table_schemas import create_table

import admin_api_handler
import common.dynamo as dynamo
from common import costing, static_pages
from common.stats import build_stats

REGION = "ap-southeast-2"
PROFILE = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
ARN = f"arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/{PROFILE}"


def _call(model_id=PROFILE, input_tokens=1000, output_tokens=1000, stage="draft"):
    return {
        "stage": stage,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "used_fallback": False,
    }


def _research_finding(captured_at="2026-09-21T01:00:00+00:00"):
    return {
        "topic_id": "t",
        "captured_at": captured_at,
        "summary": "s",
        "research_call": {
            "model_id": PROFILE,
            "input_tokens": 400,
            "output_tokens": 100,
            "used_fallback": False,
        },
    }


@pytest.fixture
def tables(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "ARTICLES_TABLE": "Articles",
        "FINDINGS_TABLE": "Findings",
        "MODELS_TABLE": "Models",
        "STATS_HISTORY_TABLE": "StatsHistory",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        create_table(
            client,
            TableName="Articles",
            KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "article_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        create_table(
            client,
            TableName="Models",
            KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "model_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        create_table(
            client,
            TableName="Findings",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "captured_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "captured_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        create_table(
            client,
            TableName="StatsHistory",
            KeySchema=[{"AttributeName": "week_start", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "week_start", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


@pytest.fixture(autouse=True)
def _registry(request, monkeypatch):
    """Tests that use `tables` get a real (empty) Models table. The rest have no AWS
    at all, so price lookups see an empty registry instead of reaching for one."""
    if "tables" not in request.fixturenames:
        monkeypatch.setattr("common.costing.get_model", lambda model_id: None)


def _put_article(article_id, lineage, status="published", topic_id="t"):
    dynamo.put_article(
        article_id=article_id,
        topic_id=topic_id,
        title=article_id,
        body_s3_key=f"articles/{article_id}.md",
        status=status,
        created_at="2026-09-21T00:00:00+00:00",
        lineage=lineage,
    )


# --- storage -----------------------------------------------------------------------


def test_a_lineage_with_a_research_block_round_trips_through_dynamodb(tables):
    research = costing.build_research_lineage([_research_finding()])
    lineage = costing.build_lineage([_call(ARN)], research=research)
    _put_article("a1", lineage)

    stored = dynamo.get_article("a1")["lineage"]

    assert stored == lineage  # floats stay floats (costs), counts stay ints, nothing lost
    assert isinstance(stored["cost_aud"], float)
    assert isinstance(stored["research"]["cost_aud"], float)
    assert isinstance(stored["research"]["calls"][0]["input_tokens"], int)
    assert isinstance(stored["total_cost_aud"], float)


def test_a_lineage_with_an_unknown_cost_round_trips_as_none(tables):
    lineage = costing.build_lineage([_call("acme.mystery-model-v1")])
    _put_article("a1", lineage)

    stored = dynamo.get_article("a1")["lineage"]

    assert stored["cost_aud"] is None
    assert stored["cost_note"] == "pricing not available for acme.mystery-model-v1"


def test_list_all_articles_returns_json_serialisable_lineage(tables):
    research = costing.build_research_lineage([_research_finding()])
    _put_article("a1", costing.build_lineage([_call()], research=research))

    articles = dynamo.list_all_articles()

    json.dumps(articles)  # would raise on a stray Decimal


def test_a_finding_keeps_the_research_call_behind_its_summary(tables):
    call = {"model_id": PROFILE, "input_tokens": 400, "output_tokens": 100, "used_fallback": False}

    dynamo.put_finding("t", "2026-09-21T01:00:00+00:00", 1, "summary", "snap.json", [], research_call=call)
    dynamo.put_finding("t", "2026-09-21T02:00:00+00:00", 1, "summary", "snap.json", [])

    with_call, without = sorted(dynamo.list_recent_findings("t", limit=10), key=lambda f: f["captured_at"])
    assert with_call["research_call"] == {**call, "input_tokens": 400, "output_tokens": 100}
    assert "research_call" not in without  # written before tracking: no attribute, not a null


def test_update_article_lineage_replaces_only_the_lineage(tables):
    _put_article("a1", costing.build_lineage([_call("acme.mystery-model-v1")]))

    dynamo.update_article_lineage("a1", costing.build_lineage([_call(ARN)]))

    article = dynamo.get_article("a1")
    assert article["title"] == "a1" and article["status"] == "published"
    assert article["lineage"]["cost_aud"] is not None


def test_update_article_lineage_does_not_create_a_missing_article(tables):
    failed = boto3.client("dynamodb", region_name=REGION).exceptions.ConditionalCheckFailedException
    with pytest.raises(failed):
        dynamo.update_article_lineage("nope", costing.build_lineage([_call()]))


# --- Stats ----------------------------------------------------------------------------------


def _stats(articles, models=()):
    return build_stats(articles, [], list(models), today=date(2026, 9, 21))


def _article(lineage, article_id="a", topic_id="t"):
    return {
        "article_id": article_id,
        "topic_id": topic_id,
        "status": "published",
        "created_at": "2026-09-21T00:00:00+00:00",
        "lineage": lineage,
    }


def test_stats_prices_with_the_builtin_fallback_when_the_registry_is_empty():
    stats = _stats([_article(costing.build_lineage([_call()]))])

    assert stats["totals"]["unpriced_calls"] == 0
    assert stats["totals"]["cost_aud"] == pytest.approx((0.001 + 0.005) * costing.USD_TO_AUD_RATE)


def test_stats_groups_an_arn_recorded_by_an_older_article_with_its_canonical_model():
    older = {"calls": [_call(ARN)], "total_input_tokens": 1000, "total_output_tokens": 1000}
    newer = costing.build_lineage([_call(PROFILE)])

    stats = _stats([_article(older, "old"), _article(newer, "new")])

    assert [row["model_id"] for row in stats["by_model"]] == [PROFILE]
    assert stats["by_model"][0]["calls"] == 2
    assert stats["by_model"][0]["display_name"] == "Claude Haiku 4.5"


def test_stats_counts_research_inside_the_totals_and_breaks_it_out():
    later = _research_finding("2026-09-21T02:00:00+00:00")
    research = costing.build_research_lineage([_research_finding(), later])
    lineage = costing.build_lineage([_call(PROFILE, 1000, 1000)], research=research)

    stats = _stats([_article(lineage)])

    totals = stats["totals"]
    assert totals["research"]["calls"] == 2
    assert (totals["research"]["input_tokens"], totals["research"]["output_tokens"]) == (800, 200)
    assert totals["research"]["cost_aud"] == pytest.approx(research["cost_aud"])
    # the combined figures are authoring + research
    assert totals["input_tokens"] == 1000 + 800 and totals["output_tokens"] == 1000 + 200
    assert totals["cost_aud"] == pytest.approx(lineage["total_cost_aud"])
    assert totals["calls"] == 3
    topic = stats["by_topic"][0]
    assert topic["research"]["calls"] == 2
    assert topic["research"]["cost_aud"] == pytest.approx(research["cost_aud"])


def test_stats_without_research_reports_an_empty_research_bucket():
    stats = _stats([_article(costing.build_lineage([_call()]))])

    assert stats["totals"]["research"] == {
        "calls": 0, "unpriced_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_aud": 0.0,
    }


def test_the_registry_overrides_the_builtin_price_in_stats():
    registry = [{
        "model_id": PROFILE,
        "display_name": "Haiku (contract rate)",
        "input_price_usd_per_1k_tokens": 0.01,
        "output_price_usd_per_1k_tokens": 0.05,
    }]

    stats = _stats([_article(costing.build_lineage([_call()]))], registry)

    assert stats["totals"]["cost_aud"] == pytest.approx((0.01 + 0.05) * costing.USD_TO_AUD_RATE)
    assert stats["by_model"][0]["display_name"] == "Haiku (contract rate)"


def test_unpriced_research_calls_are_counted_so_the_figure_reads_as_a_lower_bound():
    research = costing.build_research_lineage([{**_research_finding(), "research_call": {
        "model_id": "acme.mystery-model-v1", "input_tokens": 1, "output_tokens": 1, "used_fallback": False,
    }}])

    stats = _stats([_article({**costing.build_lineage([_call()]), "research": research})])

    assert stats["totals"]["unpriced_calls"] == 1
    assert stats["totals"]["research"]["unpriced_calls"] == 1


# --- the static article page ----------------------------------------------------------------


def test_the_lineage_footer_shows_readable_model_names_and_the_research_rows():
    later = _research_finding("2026-09-21T02:00:00+00:00")
    research = costing.build_research_lineage([_research_finding(), later])
    lineage = costing.build_lineage([_call(ARN)], research=research)

    html = static_pages._render_lineage_footer_html(lineage, "ai_only")

    assert "Claude Haiku 4.5" in html and "arn:aws" not in html
    assert "<dt>Research tokens</dt><dd>2 finding(s): 800 in / 200 out</dd>" in html
    assert "<dt>Research cost</dt>" in html and "<dt>Total cost</dt><dd>~$" in html


def test_the_lineage_footer_of_an_article_made_before_research_tracking_has_no_research_rows():
    html = static_pages._render_lineage_footer_html(costing.build_lineage([_call()]), "ai_only")

    assert "Research" not in html and "Total cost" not in html


def test_the_footer_says_when_findings_predate_research_tracking():
    older = {"topic_id": "t", "captured_at": "2026-09-20T01:00:00+00:00", "summary": "s"}
    research = costing.build_research_lineage([older])

    lineage = costing.build_lineage([_call()], research=research)
    html = static_pages._render_lineage_footer_html(lineage, "ai_only")

    assert "Not tracked (1 finding(s) predate research tracking)" in html


def test_the_summary_line_uses_model_names():
    line = static_pages._render_lineage_summary_line_html(costing.build_lineage([_call(ARN)]), "ai_only")

    assert "models [Claude Haiku 4.5]" in line and "Claude Haiku 4.5: 1,000 in / 1,000 out" in line


def test_the_summary_line_shows_the_articles_total_cost_not_just_the_authoring_cost():
    """A real production report: the summary line showed ~$0.09 (the authoring-only cost_aud)
    for an article whose research made its true total noticeably higher -- readers have no way
    to know that figure excludes research, so it read as the whole cost when it wasn't."""
    research = costing.summarise_research(
        [{"stage": "research", "model_id": PROFILE, "input_tokens": 20000, "output_tokens": 4000}],
        findings=1,
        untracked=0,
    )
    lineage = costing.build_lineage([_call(ARN, input_tokens=10000, output_tokens=10000)], research=research)
    # Well apart, not a rounding fluke.
    assert lineage["cost_aud"] == pytest.approx(0.09)
    assert lineage["total_cost_aud"] == pytest.approx(0.15)

    line = static_pages._render_lineage_summary_line_html(lineage, "ai_only")

    assert "~$0.15 AUD" in line
    assert "~$0.09 AUD" not in line


def test_the_summary_line_says_no_data_rather_than_understate_an_unpriced_total():
    research = costing.build_research_lineage([_research_finding()])
    research["cost_aud"] = None  # simulate a research model that couldn't be priced
    lineage = costing.build_lineage([_call(ARN)], research=research)
    assert lineage["total_cost_aud"] is None and lineage["cost_aud"] is not None

    line = static_pages._render_lineage_summary_line_html(lineage, "ai_only")

    assert "No data" in line
    assert f"~${lineage['cost_aud']:.2f} AUD" not in line  # never falls back to the partial figure


def test_the_summary_line_shows_the_only_cost_figure_when_there_is_no_research_tally_at_all():
    """trending_digest_handler.py's cross-topic synthesis never tallies research separately --
    cost_aud there already *is* the whole cost, so the summary line should still show it."""
    lineage = costing.build_lineage([_call(ARN)])
    assert "research" not in lineage

    line = static_pages._render_lineage_summary_line_html(lineage, "ai_only")

    assert f"~${lineage['cost_aud']:.2f} AUD" in line


# --- admin: audit and backfill ----------------------------------------------------------------


def _admin(route_key, body=None):
    event = {"routeKey": route_key}
    if body is not None:
        event["body"] = json.dumps(body)
    return admin_api_handler.handler(event, None)


def _legacy_lineage():
    return {
        "calls": [_call(ARN, 1485, 194, "ideation"), _call(ARN, 1424, 1024, "draft")],
        "total_input_tokens": 2909,
        "total_output_tokens": 1218,
        "models_used": [ARN],
        "cost_aud": None,
        "cost_note": f"pricing not available for {ARN}",
    }


def test_the_audit_route_reports_gaps(tables):
    _put_article("no-lineage", None)
    _put_article("no-cost", _legacy_lineage())

    result = _admin("GET /lineage/audit")

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["without_lineage"] == ["no-lineage"]
    assert body["cost_missing"] == ["no-cost"]


def test_the_backfill_route_is_a_dry_run_by_default(tables):
    _put_article("no-cost", _legacy_lineage())

    result = _admin("POST /lineage/backfill")

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert (body["applied"], body["examined"], body["changed"]) == (False, 1, 1)
    reported = body["articles"][0]
    assert reported["cost_aud_before"] is None and reported["cost_aud_after"] is not None
    assert "lineage" not in body["articles"][0]  # a report, not a dump of the data
    assert dynamo.get_article("no-cost")["lineage"]["cost_aud"] is None  # nothing was written


def test_the_backfill_route_writes_when_asked_and_a_second_run_changes_nothing(tables):
    _put_article("no-cost", _legacy_lineage())
    _put_article("no-lineage", None)

    first = json.loads(_admin("POST /lineage/backfill", {"apply": True})["body"])
    second = json.loads(_admin("POST /lineage/backfill", {"apply": True})["body"])

    assert (first["applied"], first["examined"], first["changed"]) == (True, 1, 1)
    assert second["changed"] == 0
    stored = dynamo.get_article("no-cost")["lineage"]
    assert stored["cost_aud"] is not None and stored["models_used"] == [PROFILE]
    assert dynamo.get_article("no-lineage")["lineage"] is None  # cannot be recovered; left alone


def test_the_backfill_route_rejects_a_non_boolean_apply(tables):
    result = _admin("POST /lineage/backfill", {"apply": "yes"})

    assert result["statusCode"] == 400


def test_a_backfill_uses_the_registry_price_when_one_exists(tables):
    dynamo.put_model({
        "model_id": PROFILE,
        "display_name": "Haiku",
        "provider": "anthropic",
        "input_price_usd_per_1k_tokens": 0.01,
        "output_price_usd_per_1k_tokens": 0.05,
    })
    _put_article("no-cost", _legacy_lineage())

    _admin("POST /lineage/backfill", {"apply": True})

    expected_usd = (2909 / 1000) * 0.01 + (1218 / 1000) * 0.05
    assert dynamo.get_article("no-cost")["lineage"]["cost_aud"] == pytest.approx(
        expected_usd * costing.USD_TO_AUD_RATE
    )


# --- admin: the one-time articles-into-StatsHistory backfill (Observability enhancement, PR 5) --


def _priced_lineage(cost_aud=3.0):
    return {
        "calls": [_call(input_tokens=1000, output_tokens=500)],
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "models_used": [PROFILE],
        "cost_aud": cost_aud,
        "cost_note": None,
    }


def test_the_articles_backfill_route_is_a_dry_run_by_default(tables):
    _put_article("a1", _priced_lineage(cost_aud=3.0))
    _put_article("a2", _priced_lineage(cost_aud=2.0))

    result = _admin("POST /stats/backfill-articles")

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert (body["already_run"], body["applied"]) == (False, False)
    assert (body["examined"], body["included"]) == (2, 2)
    assert body["totals"]["articles_calls"] == 2
    assert body["totals"]["articles_cost_aud"] == pytest.approx(5.0)
    # A dry run previews the numbers -- it never writes anything.
    assert dynamo.get_stats_history_row("articles-backfill") is None


def test_the_articles_backfill_route_writes_when_asked_and_a_second_run_refuses(tables):
    _put_article("a1", _priced_lineage(cost_aud=3.0))

    first = json.loads(_admin("POST /stats/backfill-articles", {"apply": True})["body"])
    second = json.loads(_admin("POST /stats/backfill-articles", {"apply": True})["body"])

    assert (first["already_run"], first["applied"]) == (False, True)
    assert (second["already_run"], second["applied"]) == (True, False)  # refused, not double-applied
    marker = dynamo.get_stats_history_row("articles-backfill")
    assert marker is not None and marker["examined"] == 1 and "applied_at" in marker


def test_the_articles_backfill_route_only_folds_the_totals_in_once(tables):
    _put_article("a1", _priced_lineage(cost_aud=3.0))
    _put_article("a2", _priced_lineage(cost_aud=2.0))

    _admin("POST /stats/backfill-articles", {"apply": True})
    _admin("POST /stats/backfill-articles", {"apply": True})  # a second attempt, refused

    all_time = dynamo.get_stats_totals()
    assert all_time["articles_calls"] == 2  # not 4
    assert float(all_time["articles_cost_aud"]) == pytest.approx(5.0)  # not 10.0


def test_the_articles_backfill_route_counts_an_unpriced_article_without_a_cost(tables):
    _put_article("a1", _legacy_lineage())  # cost_aud is None -- pricing not available

    result = _admin("POST /stats/backfill-articles", {"apply": True})

    body = json.loads(result["body"])
    assert body["totals"]["articles_unpriced_articles"] == 1
    assert "articles_cost_aud" not in body["totals"]
    all_time = dynamo.get_stats_totals()
    assert all_time["articles_unpriced_articles"] == 1
    assert "articles_cost_aud" not in all_time


def test_the_articles_backfill_route_skips_articles_with_no_lineage_at_all(tables):
    _put_article("no-lineage", None)

    result = _admin("POST /stats/backfill-articles", {"apply": True})

    body = json.loads(result["body"])
    assert (body["examined"], body["included"]) == (1, 0)  # scanned, but nothing to tally
    assert body["totals"] == {}
    assert dynamo.get_stats_history_row("articles-backfill") is None  # nothing to apply


def test_the_articles_backfill_route_rejects_a_non_boolean_apply(tables):
    result = _admin("POST /stats/backfill-articles", {"apply": "yes"})

    assert result["statusCode"] == 400

