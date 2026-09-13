from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import boto3
import pytest
from moto import mock_aws

import public_api_handler

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("TOPICS_TABLE", "Topics")
    monkeypatch.setenv("ARTICLES_TABLE", "Articles")
    monkeypatch.setenv("CONTENT_BUCKET", "bloggerbear-content-test")
    monkeypatch.setenv("SITE_URL", "https://example.cloudfront.net")

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
            TableName="Articles",
            KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "article_id", "AttributeType": "S"}],
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
        "created_at": "2026-09-12T00:00:00+00:00",
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
    assert body["topics"] == [{"topic_id": "github-trending", "name": "GitHub Trending"}]

    topic = body["topics"][0]
    for leaked_field in (
        "adapter",
        "adapter_config",
        "is_financial",
        "research_cadence",
        "daily_cadence",
    ):
        assert leaked_field not in topic


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
    # Listing shape: no body field.
    for article in body["articles"]:
        assert set(article.keys()) == {"article_id", "title", "published_at"}


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
