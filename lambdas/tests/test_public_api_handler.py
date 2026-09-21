from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from decimal import Decimal

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

import public_api_handler

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("TOPICS_TABLE", "Topics")
    monkeypatch.setenv("FINDINGS_TABLE", "Findings")
    monkeypatch.setenv("ARTICLES_TABLE", "Articles")
    monkeypatch.setenv("FEEDBACK_TABLE", "Feedback")
    monkeypatch.setenv("MUSINGS_TABLE", "Musings")
    monkeypatch.setenv("MODERATION_QUEUE_TABLE", "ModerationQueue")
    monkeypatch.setenv("MODELS_TABLE", "Models")
    monkeypatch.setenv("CONTENT_BUCKET", "bloggerbear-content-test")
    monkeypatch.setenv("SITE_URL", "https://example.cloudfront.net")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "model-id")

    # common.dynamo caches a boto3 resource at module scope, and
    # public_api_handler caches a boto3 s3 client -- reset both so each
    # test gets one bound to moto's mock.
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    public_api_handler._s3_client = None


@pytest.fixture
def aws_resources(aws_env):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="Topics",
            KeySchema=[{"AttributeName": "topic_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "topic_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
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
        dynamodb.create_table(
            TableName="Articles",
            KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "article_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Feedback",
            KeySchema=[
                {"AttributeName": "article_id", "KeyType": "HASH"},
                {"AttributeName": "feedback_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "article_id", "AttributeType": "S"},
                {"AttributeName": "feedback_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        dynamodb.create_table(
            TableName="Musings",
            KeySchema=[{"AttributeName": "musing_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "musing_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Models",
            KeySchema=[{"AttributeName": "model_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "model_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="ModerationQueue",
            KeySchema=[{"AttributeName": "queue_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "queue_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket="bloggerbear-content-test",
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield


TOPIC = {
    "topic_id": "github-trending",
    "name": "GitHub Trending",
    "adapter": "github_trending",
    "adapter_config": {"language": "python"},
    "is_financial": False,
    "research_cadence": "rate(1 hour)",
    "daily_cadence": "cron(0 6 * * ? *)",
}


def _event(route_key, *, path_params=None, query_params=None, body=None):
    event = {"routeKey": route_key}
    if path_params is not None:
        event["pathParameters"] = path_params
    if query_params is not None:
        event["queryStringParameters"] = query_params
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _put_topic(topic=None):
    topic = topic or TOPIC
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    table.put_item(Item=topic)


def _put_musing(
    musing_id="musing-1",
    *,
    kind="article",
    text="I pawed at a few sources today.",
    mood="proud",
    created_at="2026-09-12T00:00:00+00:00",
    article_id=None,
    topic_id=None,
):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Musings")
    table.put_item(
        Item={
            "musing_id": musing_id,
            "kind": kind,
            "article_id": article_id,
            "topic_id": topic_id,
            "text": text,
            "mood": mood,
            "created_at": created_at,
        }
    )


def _put_moderation_item(
    queue_id="queue-1",
    *,
    article_id="article-1",
    topic_id="github-trending",
    status="pending",
    reasons=None,
    created_at="2026-09-12T00:00:00+00:00",
):
    table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    table.put_item(
        Item={
            "queue_id": queue_id,
            "article_id": article_id,
            "topic_id": topic_id,
            "reasons": reasons if reasons is not None else ["needs review"],
            "status": status,
            "created_at": created_at,
        }
    )


def _put_finding(
    topic_id="github-trending",
    captured_at="2026-09-12T00:00:00+00:00",
    *,
    source_refs=None,
):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Findings")
    table.put_item(
        Item={
            "topic_id": topic_id,
            "captured_at": captured_at,
            "expires_at": 9999999999,
            "summary": "Something happened.",
            "raw_snapshot_s3_key": "snapshots/x.json",
            "source_refs": source_refs or [],
        }
    )


def _put_article(
    article_id="article-1",
    *,
    topic_id="github-trending",
    title="A Title",
    status="published",
    published_at="2026-09-12T00:00:00+00:00",
    body_s3_key=None,
    source_refs=None,
    body_text="Full article body text.",
    created_at="2026-09-12T00:00:00+00:00",
):
    body_s3_key = body_s3_key or f"articles/{article_id}.md"
    s3 = boto3.client("s3", region_name=REGION)
    s3.put_object(Bucket="bloggerbear-content-test", Key=body_s3_key, Body=body_text.encode("utf-8"))

    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    item = {
        "article_id": article_id,
        "topic_id": topic_id,
        "title": title,
        "body_s3_key": body_s3_key,
        "status": status,
        "created_at": created_at,
        "published_at": published_at,
        "source_refs": source_refs or [],
    }
    table.put_item(Item=item)
    return item


# --- Topics -----------------------------------------------------------


def test_list_topics_empty(aws_resources):
    result = public_api_handler.handler(_event("GET /topics"), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"topics": []}


def test_list_topics_hides_internal_fields(aws_resources):
    _put_topic()
    result = public_api_handler.handler(_event("GET /topics"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["topics"] == [
        {
            "topic_id": "github-trending",
            "name": "GitHub Trending",
            "article_count": 0,
            "latest_published_at": None,
            "researching": False,
        }
    ]

    topic = body["topics"][0]
    for leaked_field in (
        "adapter",
        "adapter_config",
        "is_financial",
        "research_cadence",
        "daily_cadence",
    ):
        assert leaked_field not in topic


def test_list_topics_article_count_and_latest_published_at(aws_resources):
    _put_topic()
    _put_article("article-1", published_at="2026-09-10T00:00:00+00:00")
    _put_article("article-2", published_at="2026-09-15T00:00:00+00:00")
    _put_article("article-3", published_at="2026-09-12T00:00:00+00:00")

    result = public_api_handler.handler(_event("GET /topics"), None)
    body = json.loads(result["body"])
    assert body["topics"] == [
        {
            "topic_id": "github-trending",
            "name": "GitHub Trending",
            "article_count": 3,
            "latest_published_at": "2026-09-15T00:00:00+00:00",
            "researching": False,
        }
    ]


def test_list_topics_researching_when_zero_articles_but_findings_exist(aws_resources):
    _put_topic()
    _put_finding()

    result = public_api_handler.handler(_event("GET /topics"), None)
    body = json.loads(result["body"])
    assert body["topics"][0]["article_count"] == 0
    assert body["topics"][0]["researching"] is True


def test_list_topics_not_researching_with_articles_even_if_findings_exist(aws_resources):
    # A topic that's already publishing shouldn't bother reporting
    # "researching" -- that flag exists only to cover the zero-articles gap.
    _put_topic()
    _put_finding()
    _put_article("article-1")

    result = public_api_handler.handler(_event("GET /topics"), None)
    body = json.loads(result["body"])
    assert body["topics"][0]["article_count"] == 1
    assert body["topics"][0]["researching"] is False


def test_list_topics_only_counts_published_articles(aws_resources):
    _put_topic()
    _put_article("article-1", status="published")
    _put_article("article-2", status="pending_moderation", published_at=None)

    result = public_api_handler.handler(_event("GET /topics"), None)
    body = json.loads(result["body"])
    assert body["topics"][0]["article_count"] == 1


# --- Topic activity (docs/project-plan.md §11) ---------------------------


def test_topic_activity_false_when_no_findings(aws_resources):
    event = _event("GET /topics/{topic_id}/activity", path_params={"topic_id": "github-trending"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "topic_id": "github-trending",
        "researching": False,
        "pending_review_count": 0,
        "pipeline_items": [],
    }


def test_topic_activity_true_when_finding_exists(aws_resources):
    _put_finding(source_refs=[{"url": "https://github.com/example/x", "title": "example/x"}])

    event = _event("GET /topics/{topic_id}/activity", path_params={"topic_id": "github-trending"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "topic_id": "github-trending",
        "researching": True,
        "pending_review_count": 0,
        "pipeline_items": [
            {"status": "researching", "label": "Researching", "title": "example/x"}
        ],
    }


def test_topic_activity_counts_only_pending_items_for_this_topic(aws_resources):
    _put_article(
        "article-1",
        status="pending_moderation",
        published_at=None,
        title="Pending One",
    )
    _put_article(
        "article-2",
        status="pending_moderation",
        published_at=None,
        title="Pending Two",
        created_at="2026-09-13T00:00:00+00:00",
    )
    _put_moderation_item("queue-1", topic_id="github-trending", status="pending")
    _put_moderation_item(
        "queue-2",
        article_id="article-2",
        topic_id="github-trending",
        status="pending",
        created_at="2026-09-13T00:00:00+00:00",
    )
    # Belongs to a different topic -- must not be counted.
    _put_moderation_item("queue-3", topic_id="other-topic", status="pending")
    # Already resolved -- must not be counted.
    _put_moderation_item("queue-4", topic_id="github-trending", status="approved")

    event = _event("GET /topics/{topic_id}/activity", path_params={"topic_id": "github-trending"})
    result = public_api_handler.handler(event, None)
    body = json.loads(result["body"])
    assert body["pending_review_count"] == 2
    assert body["pipeline_items"][:2] == [
        {"status": "pending_review", "label": "Pending review", "title": "Pending Two"},
        {"status": "pending_review", "label": "Pending review", "title": "Pending One"},
    ]

    other_event = _event("GET /topics/{topic_id}/activity", path_params={"topic_id": "other-topic"})
    other_result = public_api_handler.handler(other_event, None)
    assert json.loads(other_result["body"])["pending_review_count"] == 1


def test_topic_activity_never_leaks_raw_finding_or_moderation_content(aws_resources):
    _put_finding(source_refs=[{"url": "https://github.com/example/x", "title": "example/x"}])
    _put_moderation_item(
        "queue-1",
        article_id="secret-article",
        topic_id="github-trending",
        status="pending",
        reasons=["unsubstantiated claim about star counts"],
    )

    event = _event("GET /topics/{topic_id}/activity", path_params={"topic_id": "github-trending"})
    result = public_api_handler.handler(event, None)
    body = json.loads(result["body"])
    assert set(body.keys()) == {"topic_id", "researching", "pending_review_count", "pipeline_items"}
    # Nothing from the pending ModerationQueue item -- not its article_id,
    # not its reasons, not its queue_id -- appears anywhere in the response.
    body_text = result["body"]
    assert "secret-article" not in body_text
    assert "unsubstantiated" not in body_text
    assert "queue-1" not in body_text


# --- Musings --------------------------------------------------------------


def test_list_musings_empty(aws_resources):
    result = public_api_handler.handler(_event("GET /musings"), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"musings": []}


def test_list_musings_sorted_newest_first_with_full_shape(aws_resources):
    _put_musing(
        "musing-old",
        kind="feedback",
        text="Quiet week.",
        mood="curious",
        created_at="2026-09-10T00:00:00+00:00",
    )
    _put_musing(
        "musing-new",
        kind="article",
        text="I'm quite proud of this one.",
        mood="proud",
        created_at="2026-09-12T00:00:00+00:00",
        article_id="article-1",
        topic_id="github-trending",
    )

    result = public_api_handler.handler(_event("GET /musings"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    musings = body["musings"]
    assert [m["musing_id"] for m in musings] == ["musing-new", "musing-old"]

    newest = musings[0]
    assert newest == {
        "musing_id": "musing-new",
        "kind": "article",
        "article_id": "article-1",
        "topic_id": "github-trending",
        "text": "I'm quite proud of this one.",
        "mood": "proud",
        "created_at": "2026-09-12T00:00:00+00:00",
    }


# --- Stats ---------------------------------------------------------------


def test_stats_empty(aws_resources):
    result = public_api_handler.handler(_event("GET /stats"), None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["currency"] == "AUD"
    assert body["totals"]["articles"] == 0
    assert body["by_model"] == []
    assert len(body["daily"]) == 30


def test_stats_aggregates_across_all_statuses_and_is_cacheable(aws_resources):
    _put_topic()
    boto3.resource("dynamodb", region_name=REGION).Table("Models").put_item(
        Item={
            "model_id": "model-a",
            "display_name": "Model A",
            "input_price_usd_per_1k_tokens": Decimal("1.0"),
            "output_price_usd_per_1k_tokens": Decimal("2.0"),
        }
    )
    lineage = {
        "calls": [{"stage": "draft", "model_id": "model-a", "input_tokens": 1000, "output_tokens": 500}],
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "models_used": ["model-a"],
        "cost_aud": None,
        "cost_note": None,
    }
    articles = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    for article_id, status in (("a-pub", "published"), ("a-pending", "pending_moderation")):
        _put_article(article_id, status=status)
        articles.update_item(
            Key={"article_id": article_id},
            UpdateExpression="SET lineage = :l",
            ExpressionAttributeValues={":l": lineage},
        )

    result = public_api_handler.handler(_event("GET /stats"), None)

    assert result["statusCode"] == 200
    assert result["headers"]["Cache-Control"] == "public, max-age=300"
    body = json.loads(result["body"])
    assert body["totals"]["articles"] == 2
    assert body["totals"]["published"] == 1
    assert body["totals"]["input_tokens"] == 2000
    assert body["by_model"][0]["model_id"] == "model-a"
    assert body["by_model"][0]["display_name"] == "Model A"
    assert body["by_topic"][0]["name"] == "GitHub Trending"
    # Aggregates only -- never an article id or title.
    assert "a-pub" not in result["body"]
    assert "A Title" not in result["body"]


# --- Articles listing ---------------------------------------------------


def test_list_articles_requires_topic_id(aws_resources):
    result = public_api_handler.handler(_event("GET /articles"), None)
    assert result["statusCode"] == 400


def test_list_articles_missing_query_params_returns_400(aws_resources):
    event = {"routeKey": "GET /articles"}
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_list_articles_only_published_and_sorted_newest_first(aws_resources):
    _put_article("article-old", published_at="2026-09-10T00:00:00+00:00", title="Old")
    _put_article("article-new", published_at="2026-09-12T00:00:00+00:00", title="New")
    _put_article("article-pending", status="pending_moderation", title="Pending")
    _put_article(
        "article-other-topic",
        topic_id="other-topic",
        published_at="2026-09-13T00:00:00+00:00",
        title="Other",
    )

    event = _event("GET /articles", query_params={"topic_id": "github-trending"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["topic_id"] == "github-trending"
    assert [a["article_id"] for a in body["articles"]] == ["article-new", "article-old"]
    # Listing shape: no body field, but a slim lineage projection.
    for article in body["articles"]:
        assert set(article.keys()) == {
            "article_id",
            "title",
            "published_at",
            "models_used",
            "total_input_tokens",
            "total_output_tokens",
            "cost_aud",
            "cost_note",
            "model_labels",
            "published_by",
        }
        # None of the fixtures above set lineage/published_by -- explicit
        # None (not omitted), same "no data" contract as the detail route.
        assert article["models_used"] is None
        assert article["total_input_tokens"] is None
        assert article["total_output_tokens"] is None
        assert article["cost_aud"] is None
        assert article["cost_note"] is None
        assert article["model_labels"] is None
        assert article["published_by"] is None


def test_list_articles_projects_lineage_summary_when_present(aws_resources):
    lineage = {
        "calls": [
            {
                "stage": "draft",
                "model_id": "model-a",
                "input_tokens": Decimal(10),
                "output_tokens": Decimal(5),
            }
        ],
        "total_input_tokens": Decimal(10),
        "total_output_tokens": Decimal(5),
        "models_used": ["model-a"],
        "cost_aud": Decimal("0.05"),
        "cost_note": None,
    }
    _put_article("article-1", published_at="2026-09-12T00:00:00+00:00", title="Has lineage")
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    table.update_item(
        Key={"article_id": "article-1"},
        UpdateExpression="SET lineage = :lineage, published_by = :published_by",
        ExpressionAttributeValues={":lineage": lineage, ":published_by": "ai_only"},
    )

    event = _event("GET /articles", query_params={"topic_id": "github-trending"})
    result = public_api_handler.handler(event, None)
    body = json.loads(result["body"])
    article = body["articles"][0]
    assert article["models_used"] == ["model-a"]
    assert article["total_input_tokens"] == 10
    assert article["total_output_tokens"] == 5
    assert article["cost_aud"] == 0.05
    assert article["published_by"] == "ai_only"


# --- Article detail -------------------------------------------------------


def test_get_article_detail_success(aws_resources):
    refs = [{"url": "https://example.com", "title": "Example", "accessed_at": "2026-09-12T00:00:00+00:00"}]
    _put_article(source_refs=refs, body_text="The full body.")

    event = _event("GET /articles/{article_id}", path_params={"article_id": "article-1"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["article_id"] == "article-1"
    assert body["title"] == "A Title"
    assert body["body"] == "The full body."
    assert body["published_at"] == "2026-09-12T00:00:00+00:00"
    assert body["source_refs"] == refs
    assert body["view_count"] == 0
    # No lineage/published_by set on this fixture -- explicit None, not
    # omitted, so the frontend's "no data" detection has something to
    # check (docs/project-plan.md §11, PR 3 of 5).
    assert body["lineage"] is None
    assert body["published_by"] is None


def test_get_article_detail_includes_lineage_when_present(aws_resources):
    lineage = {
        "calls": [
            {
                "stage": "draft",
                "model_id": "model-a",
                "input_tokens": Decimal(100),
                "output_tokens": Decimal(50),
                "used_fallback": False,
            }
        ],
        "total_input_tokens": Decimal(100),
        "total_output_tokens": Decimal(50),
        "models_used": ["model-a"],
        "cost_aud": Decimal("0.12"),
        "cost_note": None,
    }
    _put_article()
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    table.update_item(
        Key={"article_id": "article-1"},
        UpdateExpression="SET lineage = :lineage, published_by = :published_by",
        ExpressionAttributeValues={":lineage": lineage, ":published_by": "humans"},
    )

    event = _event("GET /articles/{article_id}", path_params={"article_id": "article-1"})
    result = public_api_handler.handler(event, None)
    body = json.loads(result["body"])
    assert body["published_by"] == "humans"
    assert body["lineage"]["models_used"] == ["model-a"]
    assert body["lineage"]["cost_aud"] == 0.12
    assert body["lineage"]["calls"][0]["input_tokens"] == 100


def test_get_article_detail_dedupes_duplicate_source_refs(aws_resources):
    _put_article(
        source_refs=[
            {"url": "https://example.com", "title": "Example", "accessed_at": "2026-09-12T00:00:00+00:00"},
            {"url": "https://example.com", "title": "Example", "accessed_at": "2026-09-12T00:00:00+00:00"},
        ]
    )

    event = _event("GET /articles/{article_id}", path_params={"article_id": "article-1"})
    result = public_api_handler.handler(event, None)
    body = json.loads(result["body"])
    assert body["source_refs"] == [
        {"url": "https://example.com", "title": "Example", "accessed_at": "2026-09-12T00:00:00+00:00"}
    ]


def test_get_article_detail_defaults_view_count_when_absent(aws_resources):
    _put_article()
    event = _event("GET /articles/{article_id}", path_params={"article_id": "article-1"})
    result = public_api_handler.handler(event, None)
    body = json.loads(result["body"])
    assert body["view_count"] == 0


def test_get_article_detail_not_found(aws_resources):
    event = _event("GET /articles/{article_id}", path_params={"article_id": "nope"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_get_article_detail_non_published_returns_404(aws_resources):
    _put_article(status="pending_moderation")
    event = _event("GET /articles/{article_id}", path_params={"article_id": "article-1"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_get_article_detail_rejected_returns_404(aws_resources):
    _put_article(status="rejected")
    event = _event("GET /articles/{article_id}", path_params={"article_id": "article-1"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- View increment ---------------------------------------------------


def test_view_increment_starts_from_one(aws_resources):
    _put_article()
    event = _event("POST /articles/{article_id}/view", path_params={"article_id": "article-1"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"article_id": "article-1", "view_count": 1}


def test_view_increment_accumulates(aws_resources):
    _put_article()
    event = _event("POST /articles/{article_id}/view", path_params={"article_id": "article-1"})
    public_api_handler.handler(event, None)
    public_api_handler.handler(event, None)
    result = public_api_handler.handler(event, None)
    assert json.loads(result["body"]) == {"article_id": "article-1", "view_count": 3}


def test_view_increment_non_published_returns_404(aws_resources):
    _put_article(status="pending_moderation")
    event = _event("POST /articles/{article_id}/view", path_params={"article_id": "article-1"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_view_increment_unknown_article_returns_404(aws_resources):
    event = _event("POST /articles/{article_id}/view", path_params={"article_id": "nope"})
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- RSS feed -----------------------------------------------------------


def test_rss_feed_valid_xml_and_content_type(aws_resources):
    _put_article("article-1", published_at="2026-09-12T00:00:00+00:00", title="First")
    event = _event("GET /rss.xml")
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert result["headers"]["Content-Type"] == "application/rss+xml; charset=utf-8"

    root = ET.fromstring(result["body"])
    assert root.tag == "rss"


def test_rss_feed_only_published_ordered_newest_first(aws_resources):
    _put_article("article-old", published_at="2026-09-10T00:00:00+00:00", title="Old")
    _put_article("article-new", published_at="2026-09-12T00:00:00+00:00", title="New")
    _put_article("article-pending", status="pending_moderation", title="Pending")
    _put_article(
        "article-other-topic",
        topic_id="other-topic",
        published_at="2026-09-13T00:00:00+00:00",
        title="Other",
    )

    event = _event("GET /rss.xml")
    result = public_api_handler.handler(event, None)
    root = ET.fromstring(result["body"])
    titles = [item.find("title").text for item in root.find("channel").findall("item")]
    assert titles == ["Other", "New", "Old"]


def test_rss_feed_caps_at_50_items(aws_resources):
    for i in range(60):
        _put_article(
            f"article-{i:02d}",
            published_at=f"2026-09-{(i % 28) + 1:02d}T00:00:00+00:00",
            title=f"Article {i}",
        )
    event = _event("GET /rss.xml")
    result = public_api_handler.handler(event, None)
    root = ET.fromstring(result["body"])
    items = root.find("channel").findall("item")
    assert len(items) == 50


def test_rss_feed_escapes_special_characters(aws_resources):
    _put_article(title="Foo & <Bar>", body_text="Body with & and <tags> inside it.")
    event = _event("GET /rss.xml")
    result = public_api_handler.handler(event, None)

    # Must parse cleanly despite raw & and < in source data.
    root = ET.fromstring(result["body"])
    item = root.find("channel").find("item")
    assert item.find("title").text == "Foo & <Bar>"
    assert item.find("description").text == "Body with & and <tags> inside it."
    # The raw XML must carry escaped entities, not literal & / < in the item.
    assert "<title>Foo &amp; &lt;Bar&gt;</title>" in result["body"]

    link = item.find("link").text
    assert link == "https://example.cloudfront.net/#/article/article-1"


def test_rss_feed_description_truncated_to_300_chars(aws_resources):
    long_body = "x" * 500
    _put_article(body_text=long_body)
    event = _event("GET /rss.xml")
    result = public_api_handler.handler(event, None)
    root = ET.fromstring(result["body"])
    description = root.find("channel").find("item").find("description").text
    assert len(description) == 300


# --- Feedback -----------------------------------------------------------


def _feedback_items(article_id="article-1"):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Feedback")
    response = table.query(KeyConditionExpression=Key("article_id").eq(article_id))
    return response.get("Items", [])


def _get_article_item(article_id="article-1"):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    return table.get_item(Key={"article_id": article_id}).get("Item")


def _unexpected_call(*_args, **_kwargs):
    raise AssertionError("should not have been called")


def test_feedback_upvote_no_comment_succeeds(aws_resources, monkeypatch):
    _put_article()
    # No comment was submitted, so neither redaction pass should run.
    monkeypatch.setattr(public_api_handler, "regex_redact", _unexpected_call)
    monkeypatch.setattr(public_api_handler, "bedrock_redact_review", _unexpected_call)

    event = _event(
        "POST /articles/{article_id}/feedback",
        path_params={"article_id": "article-1"},
        body={"vote": "up"},
    )
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 201
    body = json.loads(result["body"])
    assert body["status"] == "recorded"
    assert body["article_id"] == "article-1"
    assert "feedback_id" in body
    assert "comment" not in body

    items = _feedback_items()
    assert len(items) == 1
    assert items[0]["vote"] == "up"
    assert items[0]["comment"] is None

    article = _get_article_item()
    assert int(article["net_votes"]) == 1


def test_feedback_downvote_updates_net_votes_negative(aws_resources):
    _put_article()
    event = _event(
        "POST /articles/{article_id}/feedback",
        path_params={"article_id": "article-1"},
        body={"vote": "down"},
    )
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 201

    article = _get_article_item()
    assert int(article["net_votes"]) == -1


def test_feedback_with_comment_runs_regex_then_bedrock_redaction(aws_resources, monkeypatch):
    _put_article()

    regex_calls = []
    bedrock_calls = []

    def fake_regex_redact(text):
        regex_calls.append(text)
        return "regex-redacted-text"

    def fake_bedrock_redact_review(redacted_text, model_id):
        bedrock_calls.append((redacted_text, model_id))
        return "final-safe-text"

    monkeypatch.setattr(public_api_handler, "regex_redact", fake_regex_redact)
    monkeypatch.setattr(public_api_handler, "bedrock_redact_review", fake_bedrock_redact_review)

    event = _event(
        "POST /articles/{article_id}/feedback",
        path_params={"article_id": "article-1"},
        body={"vote": "up", "comment": "My name is Jane, email jane@example.com"},
    )
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 201

    # Regex pass ran on the raw comment, Bedrock pass ran on its output.
    assert regex_calls == ["My name is Jane, email jane@example.com"]
    assert bedrock_calls == [("regex-redacted-text", "model-id")]

    # The STORED comment is whatever the (mocked) redaction pipeline
    # produced -- never the original raw text.
    items = _feedback_items()
    assert items[0]["comment"] == "final-safe-text"
    assert "Jane" not in items[0]["comment"]
    assert "jane@example.com" not in items[0]["comment"]


def test_feedback_comment_rejected_by_bedrock_stored_as_none(aws_resources, monkeypatch):
    _put_article()
    monkeypatch.setattr(public_api_handler, "bedrock_redact_review", lambda *a, **k: None)

    event = _event(
        "POST /articles/{article_id}/feedback",
        path_params={"article_id": "article-1"},
        body={"vote": "up", "comment": "some comment text"},
    )
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 201

    items = _feedback_items()
    assert len(items) == 1
    assert items[0]["comment"] is None


def test_feedback_empty_comment_skips_redaction_pipeline(aws_resources, monkeypatch):
    _put_article()
    monkeypatch.setattr(public_api_handler, "regex_redact", _unexpected_call)
    monkeypatch.setattr(public_api_handler, "bedrock_redact_review", _unexpected_call)

    event = _event(
        "POST /articles/{article_id}/feedback",
        path_params={"article_id": "article-1"},
        body={"vote": "up", "comment": ""},
    )
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 201
    assert _feedback_items()[0]["comment"] is None


def test_feedback_invalid_vote_returns_400(aws_resources):
    _put_article()
    event = _event(
        "POST /articles/{article_id}/feedback",
        path_params={"article_id": "article-1"},
        body={"vote": "sideways"},
    )
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 400
    assert _feedback_items() == []


def test_feedback_missing_article_returns_404(aws_resources):
    event = _event(
        "POST /articles/{article_id}/feedback",
        path_params={"article_id": "nope"},
        body={"vote": "up"},
    )
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_feedback_non_published_article_returns_404(aws_resources):
    _put_article(status="pending_moderation")
    event = _event(
        "POST /articles/{article_id}/feedback",
        path_params={"article_id": "article-1"},
        body={"vote": "up"},
    )
    result = public_api_handler.handler(event, None)
    assert result["statusCode"] == 404
    assert _feedback_items() == []


# --- Exception safety / routing -----------------------------------------


def test_unknown_route_returns_404(aws_resources):
    result = public_api_handler.handler(_event("GET /nope"), None)
    assert result["statusCode"] == 404


def test_unhandled_exception_returns_500(aws_resources, monkeypatch):
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(public_api_handler, "list_topics", lambda: _boom())
    result = public_api_handler.handler(_event("GET /topics"), None)
    assert result["statusCode"] == 500
    assert "error" in json.loads(result["body"])


def test_the_fresh_data_review_is_never_exposed_publicly(aws_resources):
    _put_article()
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    table.update_item(
        Key={"article_id": "article-1"},
        UpdateExpression="SET review = :r",
        ExpressionAttributeValues={
            ":r": {"status": "reviewed", "outcome": "major", "claims": [{"claim": "secret note"}]}
        },
    )

    detail = public_api_handler.handler(
        _event("GET /articles/{article_id}", path_params={"article_id": "article-1"}), None
    )
    listing = public_api_handler.handler(
        _event("GET /topics/{topic_id}/articles", path_params={"topic_id": "github-trending"}), None
    )

    assert "review" not in json.loads(detail["body"])
    assert "secret note" not in detail["body"] and "secret note" not in listing["body"]
