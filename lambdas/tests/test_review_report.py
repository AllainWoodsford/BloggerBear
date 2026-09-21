"""The fresh-data review report: aggregation of the records shadow mode leaves on articles,
including what enforcement would have done, so turning it on is decided from numbers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

import admin_api_handler
import common.dynamo as dynamo
from common import review_report as rr

REGION = "ap-southeast-2"
BASE = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)

TOPICS = [
    {"topic_id": "gh", "name": "GitHub Trending", "is_financial": False},
    {"topic_id": "hn", "name": "Hacker News", "is_financial": False},
    {"topic_id": "fin", "name": "Finance", "is_financial": True},
]


def _claim(problem="stale", severity="minor", claim="Repo X is #1", evidence="now #4"):
    return {"claim": claim, "problem": problem, "severity": severity, "evidence": evidence}


def _reviewed(outcome="clean", claims=()):
    return {"status": "reviewed", "outcome": outcome, "claims": list(claims), "mode": "shadow"}


def _unavailable(reason="could not fetch fresh data: source down"):
    return {"status": "unavailable", "reason": reason, "mode": "shadow"}


def _skipped():
    return {"status": "skipped", "reason": "no fresh data", "mode": "shadow"}


def _article(article_id, review, topic_id="gh", days=0):
    article = {
        "article_id": article_id,
        "topic_id": topic_id,
        "created_at": (BASE + timedelta(days=days)).isoformat(),
        "status": "published",
    }
    if review is not None:
        article["review"] = review
    return article


def _report(articles, **kwargs):
    return rr.build_review_report(articles, TOPICS, **kwargs)


# --- the basics -----------------------------------------------------------------------------------


def test_nothing_reviewed_yet_is_a_valid_report():
    report = _report([])

    assert report["articles"] == report["with_review"] == report["without_review"] == 0
    assert report["by_status"] == {} and report["by_outcome"] == {} and report["sample"] == []
    preview = report["enforcement_preview"]
    assert preview["would_hold"] == preview["would_revise"] == preview["would_pass"] == 0
    assert preview["would_hold_rate"] is None and preview["unavailable_rate"] is None
    assert report["days_covered"] == 0 and report["readiness"]["all_measurable_criteria_met"] is False


def test_articles_without_a_review_are_counted_but_not_analysed():
    report = _report([_article("a", None), _article("b", _reviewed()), _article("c", "not a dict")])

    assert (report["articles"], report["with_review"], report["without_review"]) == (3, 1, 2)


def test_counts_by_status_and_outcome():
    articles = [
        _article("a", _reviewed("clean")),
        _article("b", _reviewed("clean")),
        _article("c", _reviewed("minor", [_claim()])),
        _article("d", _reviewed("major", [_claim(severity="major")])),
        _article("e", _unavailable()),
        _article("f", _skipped()),
    ]

    report = _report(articles)

    assert report["by_status"] == {"reviewed": 4, "unavailable": 1, "skipped": 1}
    assert report["by_outcome"] == {"clean": 2, "minor": 1, "major": 1}


def test_claims_are_counted_by_problem_and_severity():
    articles = [
        _article("a", _reviewed("minor", [_claim("stale", "minor"), _claim("unsupported", "minor")])),
        _article("b", _reviewed("major", [_claim("contradicted", "major")])),
    ]

    claims = _report(articles)["claims"]

    assert claims["total"] == 3
    assert claims["by_problem"] == {"stale": 1, "unsupported": 1, "contradicted": 1}
    assert claims["by_severity"] == {"minor": 2, "major": 1}


def test_unavailable_reasons_are_grouped_by_their_stable_head():
    articles = [
        _article("a", _unavailable("could not fetch fresh data: 429 from coingecko")),
        _article("b", _unavailable("could not fetch fresh data: connection reset")),
        _article("c", _unavailable("the reviewer's reply was not the expected JSON")),
        _article("d", {"status": "unavailable"}),
    ]

    reasons = _report(articles)["unavailable_reasons"]

    assert reasons == {
        "could not fetch fresh data": 2,
        "the reviewer's reply was not the expected JSON": 1,
        "unknown": 1,
    }
    assert list(reasons)[0] == "could not fetch fresh data"  # most common first


# --- per topic ------------------------------------------------------------------------------------


def test_each_topic_gets_its_own_row_with_a_flag_rate():
    articles = [
        _article("a", _reviewed("clean"), "gh"),
        _article("b", _reviewed("minor", [_claim()]), "gh"),
        _article("c", _reviewed("major", [_claim(severity="major")]), "gh"),
        _article("d", _unavailable(), "gh"),
        _article("e", None, "gh"),
        _article("f", _reviewed("clean"), "hn"),
    ]

    rows = {row["topic_id"]: row for row in _report(articles)["by_topic"]}

    gh = rows["gh"]
    assert gh["name"] == "GitHub Trending" and gh["is_financial"] is False
    assert (gh["articles"], gh["reviewed"]) == (5, 4)
    assert (gh["clean"], gh["minor"], gh["major"], gh["unavailable"]) == (1, 1, 1, 1)
    assert gh["flag_rate"] == 0.5  # 2 flagged of 4 reviewed
    assert rows["hn"]["flag_rate"] == 0.0


def test_a_topic_with_no_reviews_has_no_flag_rate_and_topics_are_ordered_by_reviews():
    rows = _report([_article("a", None, "gh"), _article("b", _reviewed(), "hn")])["by_topic"]

    assert [r["topic_id"] for r in rows] == ["hn", "gh"]
    assert rows[1]["flag_rate"] is None


def test_a_topic_that_no_longer_exists_is_shown_by_its_id():
    rows = _report([_article("a", _reviewed(), "deleted-topic")])["by_topic"]

    assert rows[0]["name"] == "deleted-topic" and rows[0]["is_financial"] is False


# --- what enforcement would have done -------------------------------------------------------------


def test_the_enforcement_preview_holds_majors_and_unavailable_revises_minors_and_passes_the_rest():
    articles = [
        _article("major", _reviewed("major", [_claim(severity="major")])),
        _article("unavailable", _unavailable()),
        _article("minor", _reviewed("minor", [_claim()])),
        _article("clean", _reviewed("clean")),
        _article("skipped", _skipped()),
    ]

    preview = _report(articles)["enforcement_preview"]

    assert (preview["would_hold"], preview["would_revise"], preview["would_pass"]) == (2, 1, 2)
    assert preview["non_financial_reviewed"] == 5
    assert preview["would_hold_rate"] == 0.4 and preview["would_revise_rate"] == 0.2
    assert preview["unavailable_rate"] == 0.2


def test_financial_topics_are_always_moderated_so_they_are_left_out_of_the_rates():
    articles = [
        _article("f1", _reviewed("major", [_claim(severity="major")]), "fin"),
        _article("f2", _unavailable(), "fin"),
        _article("n1", _reviewed("clean"), "gh"),
    ]

    preview = _report(articles)["enforcement_preview"]

    assert preview["non_financial_reviewed"] == 1 and preview["would_hold"] == 0
    assert preview["would_hold_rate"] == 0.0 and preview["unavailable_rate"] == 0.0
    assert preview["financial_reviewed_always_moderated"] == 2


def test_an_unreadable_outcome_is_treated_as_a_pass_not_a_hold():
    preview = _report([_article("a", {"status": "reviewed", "outcome": "???"})])["enforcement_preview"]

    assert preview["would_hold"] == 0 and preview["would_pass"] == 1


# --- readiness ------------------------------------------------------------------------------------


def _enough(unavailable=0, majors=0, articles=24, topics=("gh", "hn"), days=8):
    made = []
    for i in range(articles):
        if i < unavailable:
            review = _unavailable()
        elif i < unavailable + majors:
            review = _reviewed("major", [_claim(severity="major")])
        else:
            review = _reviewed("clean")
        made.append(_article(f"a{i}", review, topics[i % len(topics)], days=i % days))
    return made


def test_a_healthy_dataset_meets_every_measurable_criterion():
    readiness = _report(_enough())["readiness"]

    assert readiness["all_measurable_criteria_met"] is True
    assert readiness["precision_needs_a_manual_check"] is True  # never claimed by the numbers


def test_too_few_articles_is_not_ready():
    readiness = _report(_enough(articles=10))["readiness"]

    assert readiness["enough_articles"] is False and readiness["all_measurable_criteria_met"] is False


def test_too_few_days_is_not_ready():
    assert _report(_enough(days=3))["readiness"]["enough_days"] is False


def test_a_single_topic_is_not_ready():
    assert _report(_enough(topics=("gh",)))["readiness"]["enough_topics"] is False


def test_too_many_unavailable_reviews_is_not_ready():
    readiness = _report(_enough(unavailable=6))["readiness"]  # 25% of 24

    assert readiness["unavailable_rate_ok"] is False and readiness["all_measurable_criteria_met"] is False


def test_holding_too_many_articles_is_not_ready():
    readiness = _report(_enough(majors=8))["readiness"]  # 33% of 24 would be held

    assert readiness["would_hold_rate_ok"] is False


def test_the_boundary_rates_are_inclusive():
    just_ok = _report(_enough(articles=20, majors=5, unavailable=0))  # exactly 25%
    assert just_ok["readiness"]["would_hold_rate_ok"] is True

    just_unavailable_ok = _report(_enough(articles=20, unavailable=2))  # exactly 10%
    assert just_unavailable_ok["readiness"]["unavailable_rate_ok"] is True


def test_the_thresholds_used_are_published_with_the_report():
    thresholds = _report([])["thresholds"]

    assert thresholds == {
        "min_reviewed_non_financial": 20,
        "min_days_covered": 7,
        "min_topics_covered": 2,
        "max_unavailable_rate": 0.10,
        "max_would_hold_rate": 0.25,
    }


def test_days_covered_spans_the_first_to_the_last_reviewed_article():
    articles = [
        _article("a", _reviewed(), days=0),
        _article("b", _reviewed(), days=6),
        _article("c", None, days=40),
    ]

    assert _report(articles)["days_covered"] == 7  # articles without a review do not stretch it


def test_unreadable_or_naive_dates_do_not_break_the_report():
    articles = [
        {**_article("a", _reviewed()), "created_at": "not a date"},
        {
            **_article("b", _reviewed()),
            "created_at": (BASE + timedelta(days=2)).replace(tzinfo=None).isoformat(),
        },
        {**_article("c", _reviewed()), "created_at": None},
    ]

    assert _report(articles)["days_covered"] == 1  # only the one readable date


# --- the sample of flagged claims -----------------------------------------------------------------


def _flagged(n):
    return [_article(f"a{i}", _reviewed("minor", [_claim(claim=f"claim {i}")]), days=i) for i in range(n)]


def test_the_sample_is_the_most_recent_flagged_claims_with_their_article_ids():
    sample = _report(_flagged(5), sample_size=3)["sample"]

    assert [s["claim"] for s in sample] == ["claim 4", "claim 3", "claim 2"]
    assert sample[0] == {
        "article_id": "a4",
        "topic_id": "gh",
        "created_at": (BASE + timedelta(days=4)).isoformat(),
        "claim": "claim 4",
        "problem": "stale",
        "severity": "minor",
        "evidence": "now #4",
    }


def test_the_default_sample_size_and_zero_and_the_cap():
    assert len(_report(_flagged(30))["sample"]) == rr.DEFAULT_SAMPLE_SIZE == 10
    assert _report(_flagged(30), sample_size=0)["sample"] == []
    assert len(_report(_flagged(80), sample_size=500)["sample"]) == rr.MAX_SAMPLE_SIZE == 50
    assert _report(_flagged(3), sample_size=-4)["sample"] == []


def test_the_sample_contains_only_flagged_claims_never_clean_reviews():
    sample = _report([_article("a", _reviewed("clean")), _article("b", _unavailable())])["sample"]

    assert sample == []


def test_the_generation_time_can_be_fixed():
    assert _report([], now=BASE)["generated_at"] == BASE.isoformat()


# --- the admin route ------------------------------------------------------------------------------


@pytest.fixture
def tables(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "ARTICLES_TABLE": "Articles",
        "TOPICS_TABLE": "Topics",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, key in (("Articles", "article_id"), ("Topics", "topic_id")):
            client.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        topics = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
        for topic in TOPICS:
            topics.put_item(Item={**topic, "adapter": "web_search"})
        yield


def _store(article_id, review, topic_id="gh", days=0):
    dynamo.put_article(
        article_id=article_id,
        topic_id=topic_id,
        title=article_id,
        body_s3_key="k",
        status="published",
        created_at=(BASE + timedelta(days=days)).isoformat(),
        review=review,
    )


def _get(query=None):
    event = {"routeKey": "GET /review/report"}
    if query is not None:
        event["queryStringParameters"] = query
    return admin_api_handler.handler(event, None)


def test_the_route_reports_from_the_stored_records(tables):
    _store("a", _reviewed("major", [_claim(severity="major")]))
    _store("b", _reviewed("clean"))
    _store("c", None)

    result = _get()

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert (body["articles"], body["with_review"]) == (3, 2)
    assert body["enforcement_preview"]["would_hold"] == 1
    assert body["by_topic"][0]["name"] == "GitHub Trending"  # names come from the Topics table


def test_the_route_is_json_serialisable_from_dynamodb_decimals(tables):
    _store("a", _reviewed("minor", [_claim()]))

    json.loads(_get()["body"])  # a stray Decimal would raise here


def test_the_route_honours_the_sample_parameter(tables):
    for i in range(6):
        _store(f"a{i}", _reviewed("minor", [_claim(claim=f"c{i}")]), days=i)

    assert len(json.loads(_get({"sample": "2"})["body"])["sample"]) == 2
    assert len(json.loads(_get()["body"])["sample"]) == 6  # fewer than the default of 10


@pytest.mark.parametrize("bad", ["abc", "-1", "51", "2.5", ""])
def test_an_invalid_sample_is_refused(tables, bad):
    result = _get({"sample": bad})

    assert result["statusCode"] == 400 and "sample" in json.loads(result["body"])["error"]


def test_an_empty_store_gives_an_empty_report_not_an_error(tables):
    result = _get()

    assert result["statusCode"] == 200
    assert json.loads(result["body"])["articles"] == 0
