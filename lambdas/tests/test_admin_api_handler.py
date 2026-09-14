from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

import admin_api_handler

REGION = "ap-southeast-2"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("TOPICS_TABLE", "Topics")
    monkeypatch.setenv("FINDINGS_TABLE", "Findings")
    monkeypatch.setenv("CANDIDATE_IDEAS_TABLE", "CandidateIdeas")
    monkeypatch.setenv("ARTICLES_TABLE", "Articles")
    monkeypatch.setenv("MODERATION_QUEUE_TABLE", "ModerationQueue")
    monkeypatch.setenv("PROMPT_REFINEMENTS_TABLE", "PromptRefinements")
    monkeypatch.setenv("CONTENT_BUCKET", "bloggerbear-content-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
    monkeypatch.setenv("RESEARCH_TICK_FUNCTION_NAME", "research-tick-fn")
    monkeypatch.setenv("DAILY_CYCLE_FUNCTION_NAME", "daily-cycle-fn")
    monkeypatch.setenv(
        "RESEARCH_TICK_FUNCTION_ARN",
        "arn:aws:lambda:ap-southeast-2:123456789012:function:research-tick-fn",
    )
    monkeypatch.setenv(
        "STATE_MACHINE_ARN", "arn:aws:states:ap-southeast-2:123456789012:stateMachine:daily-cycle"
    )
    monkeypatch.setenv(
        "SCHEDULER_INVOKE_ROLE_ARN", "arn:aws:iam::123456789012:role/scheduler-invoke"
    )
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")

    # common.dynamo caches a boto3 resource at module scope, and
    # admin_api_handler caches a boto3 lambda client -- reset both so each
    # test gets one bound to moto's mock (or a fresh mock to patch over).
    import common.dynamo as dynamo_module
    import common.scheduler as scheduler_module

    dynamo_module._dynamodb_resource = None
    scheduler_module._scheduler_client = None
    admin_api_handler._lambda_client = None


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
            TableName="CandidateIdeas",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "created_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
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
            TableName="ModerationQueue",
            KeySchema=[{"AttributeName": "queue_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "queue_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="PromptRefinements",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


TOPIC = {
    "topic_id": "github-trending",
    "name": "GitHub Trending",
    "adapter": "github_trending",
    "adapter_config": {"language": "python"},
    "is_financial": False,
}


def _event(route_key, *, path_params=None, body=None, method=None):
    event = {"routeKey": route_key}
    if path_params is not None:
        event["pathParameters"] = path_params
    if body is not None:
        event["body"] = json.dumps(body)
    if method is not None:
        event["requestContext"] = {"http": {"method": method}}
    return event


def _put_topic(topic=TOPIC):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    table.put_item(Item=topic)


# --- Topics CRUD -----------------------------------------------------------


def test_list_topics_empty(aws_resources):
    result = admin_api_handler.handler(_event("GET /topics"), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"topics": []}


def test_list_topics_returns_items(aws_resources):
    _put_topic()
    result = admin_api_handler.handler(_event("GET /topics"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["topics"] == [TOPIC]


def test_create_topic_success(aws_resources):
    body = {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
    }
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 201
    created = json.loads(result["body"])
    assert created == {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
        "adapter_config": {},
        "is_financial": False,
        "research_cadence": "rate(1 hour)",
        "daily_cadence": "cron(0 6 * * ? *)",
    }

    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    assert table.get_item(Key={"topic_id": "new-topic"})["Item"] == created

    mock_upsert.assert_called_once_with("new-topic", "rate(1 hour)", "cron(0 6 * * ? *)")


def test_create_topic_crypto_feed_forces_is_financial_true(aws_resources):
    # Phase 7: is_financial must be forced True for crypto_feed even when
    # the caller explicitly passes False -- that safety property can't be
    # bypassed by an operator mistake.
    body = {
        "topic_id": "crypto",
        "name": "Crypto Markets",
        "adapter": "crypto_feed",
        "is_financial": False,
    }
    with patch("admin_api_handler.upsert_topic_schedules"):
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 201
    created = json.loads(result["body"])
    assert created["is_financial"] is True

    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    assert table.get_item(Key={"topic_id": "crypto"})["Item"]["is_financial"] is True


def test_create_topic_custom_cadence(aws_resources):
    body = {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
        "research_cadence": "rate(30 minutes)",
        "daily_cadence": "cron(0 12 * * ? *)",
    }
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 201
    created = json.loads(result["body"])
    assert created["research_cadence"] == "rate(30 minutes)"
    assert created["daily_cadence"] == "cron(0 12 * * ? *)"
    mock_upsert.assert_called_once_with("new-topic", "rate(30 minutes)", "cron(0 12 * * ? *)")


def test_create_topic_invalid_cadence_expression_returns_400(aws_resources):
    body = {
        "topic_id": "new-topic",
        "name": "New Topic",
        "adapter": "github_trending",
        "research_cadence": "every hour please",
    }
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(_event("POST /topics", body=body), None)

    assert result["statusCode"] == 400
    mock_upsert.assert_not_called()

    # The bad cadence must be rejected before the topic item is ever written.
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    assert "Item" not in table.get_item(Key={"topic_id": "new-topic"})


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Missing topic_id", "adapter": "x"},
        {"topic_id": "", "name": "Empty topic_id", "adapter": "x"},
        {"topic_id": "t", "adapter": "x"},
        {"topic_id": "t", "name": "n"},
        {"topic_id": "t", "name": "n", "adapter": "x", "adapter_config": "not-a-dict"},
        {"topic_id": "t", "name": "n", "adapter": "x", "is_financial": "not-a-bool"},
        {"topic_id": "t", "name": "n", "adapter": "x", "research_cadence": 123},
        {"topic_id": "t", "name": "n", "adapter": "x", "research_cadence": ""},
        {"topic_id": "t", "name": "n", "adapter": "x", "daily_cadence": 123},
        {"topic_id": "t", "name": "n", "adapter": "x", "daily_cadence": ""},
    ],
)
def test_create_topic_invalid_input_returns_400(aws_resources, body):
    result = admin_api_handler.handler(_event("POST /topics", body=body), None)
    assert result["statusCode"] == 400


def test_create_topic_malformed_json_body_returns_400(aws_resources):
    event = {"routeKey": "POST /topics", "body": "{not valid json"}
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_create_topic_conflict_returns_409(aws_resources):
    _put_topic()
    body = {"topic_id": "github-trending", "name": "Dup", "adapter": "github_trending"}
    result = admin_api_handler.handler(_event("POST /topics", body=body), None)
    assert result["statusCode"] == 409


def test_get_topic_found(aws_resources):
    _put_topic()
    event = _event("GET /topics/{topic_id}", path_params={"topic_id": "github-trending"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == TOPIC


def test_get_topic_not_found(aws_resources):
    event = _event("GET /topics/{topic_id}", path_params={"topic_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_update_topic_partial(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"name": "Renamed", "is_financial": True},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    updated = json.loads(result["body"])
    assert updated["name"] == "Renamed"
    assert updated["is_financial"] is True
    assert updated["adapter"] == TOPIC["adapter"]  # untouched field preserved
    # Not present on TOPIC -- update fills in the defaults.
    assert updated["research_cadence"] == "rate(1 hour)"
    assert updated["daily_cadence"] == "cron(0 6 * * ? *)"
    mock_upsert.assert_called_once_with(
        "github-trending", "rate(1 hour)", "cron(0 6 * * ? *)"
    )


def test_update_topic_switching_to_crypto_feed_forces_is_financial_true(aws_resources):
    _put_topic()  # adapter=github_trending, is_financial=False
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"adapter": "crypto_feed", "is_financial": False},
    )
    with patch("admin_api_handler.upsert_topic_schedules"):
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    updated = json.loads(result["body"])
    assert updated["adapter"] == "crypto_feed"
    assert updated["is_financial"] is True


def test_update_topic_already_crypto_feed_cannot_unset_is_financial(aws_resources):
    _put_topic({**TOPIC, "topic_id": "crypto", "adapter": "crypto_feed", "is_financial": True})
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "crypto"},
        body={"is_financial": False},
    )
    with patch("admin_api_handler.upsert_topic_schedules"):
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    updated = json.loads(result["body"])
    assert updated["is_financial"] is True


def test_update_topic_custom_cadence(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"research_cadence": "rate(2 hours)", "daily_cadence": "cron(0 18 * * ? *)"},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    updated = json.loads(result["body"])
    assert updated["research_cadence"] == "rate(2 hours)"
    assert updated["daily_cadence"] == "cron(0 18 * * ? *)"
    mock_upsert.assert_called_once_with(
        "github-trending", "rate(2 hours)", "cron(0 18 * * ? *)"
    )


def test_update_topic_invalid_cadence_expression_returns_400(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"daily_cadence": "whenever"},
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 400
    mock_upsert.assert_not_called()


def test_update_topic_not_found(aws_resources):
    event = _event(
        "PUT /topics/{topic_id}", path_params={"topic_id": "nope"}, body={"name": "x"}
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_update_topic_invalid_field_returns_400(aws_resources):
    _put_topic()
    event = _event(
        "PUT /topics/{topic_id}",
        path_params={"topic_id": "github-trending"},
        body={"adapter_config": "nope"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_delete_topic_success(aws_resources):
    _put_topic()
    event = _event("DELETE /topics/{topic_id}", path_params={"topic_id": "github-trending"})
    with patch("admin_api_handler.delete_topic_schedules") as mock_delete_schedules:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"deleted": "github-trending"}
    mock_delete_schedules.assert_called_once_with("github-trending")

    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    assert "Item" not in table.get_item(Key={"topic_id": "github-trending"})


def test_delete_topic_not_found(aws_resources):
    event = _event("DELETE /topics/{topic_id}", path_params={"topic_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- Trigger -----------------------------------------------------------


def test_trigger_research_tick_invokes_lambda(aws_resources):
    _put_topic()
    mock_client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=mock_client):
        event = _event(
            "POST /topics/{topic_id}/trigger",
            path_params={"topic_id": "github-trending"},
            body={"pipeline": "research_tick"},
        )
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 202
    assert json.loads(result["body"]) == {
        "triggered": "research_tick",
        "topic_id": "github-trending",
    }
    mock_client.invoke.assert_called_once_with(
        FunctionName="research-tick-fn",
        InvocationType="Event",
        Payload=json.dumps({"topic_id": "github-trending"}).encode("utf-8"),
    )


def test_trigger_daily_cycle_invokes_lambda(aws_resources):
    _put_topic()
    mock_client = MagicMock()
    with patch("admin_api_handler._get_lambda_client", return_value=mock_client):
        event = _event(
            "POST /topics/{topic_id}/trigger",
            path_params={"topic_id": "github-trending"},
            body={"pipeline": "daily_cycle"},
        )
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 202
    mock_client.invoke.assert_called_once_with(
        FunctionName="daily-cycle-fn",
        InvocationType="Event",
        Payload=json.dumps({"topic_id": "github-trending"}).encode("utf-8"),
    )


def test_trigger_invalid_pipeline_returns_400(aws_resources):
    _put_topic()
    event = _event(
        "POST /topics/{topic_id}/trigger",
        path_params={"topic_id": "github-trending"},
        body={"pipeline": "not_a_pipeline"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_trigger_malformed_json_body_returns_400(aws_resources):
    _put_topic()
    event = {
        "routeKey": "POST /topics/{topic_id}/trigger",
        "pathParameters": {"topic_id": "github-trending"},
        "body": "{not valid json",
    }
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 400


def test_trigger_unknown_topic_returns_404(aws_resources):
    event = _event(
        "POST /topics/{topic_id}/trigger",
        path_params={"topic_id": "nope"},
        body={"pipeline": "research_tick"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- Candidates -----------------------------------------------------------


def test_list_candidates_returns_all_statuses(aws_resources):
    _put_topic()
    table = boto3.resource("dynamodb", region_name=REGION).Table("CandidateIdeas")
    table.put_item(
        Item={
            "topic_id": "github-trending",
            "created_at": "2026-09-12T00:00:00+00:00",
            "angle": "considered angle",
            "status": "considered",
        }
    )
    table.put_item(
        Item={
            "topic_id": "github-trending",
            "created_at": "2026-09-12T00:00:01+00:00",
            "angle": "selected angle",
            "status": "selected",
        }
    )

    event = _event(
        "GET /topics/{topic_id}/candidates", path_params={"topic_id": "github-trending"}
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["topic_id"] == "github-trending"
    assert len(body["candidates"]) == 2
    statuses = {c["status"] for c in body["candidates"]}
    assert statuses == {"considered", "selected"}


def test_list_candidates_unknown_topic_returns_404(aws_resources):
    event = _event("GET /topics/{topic_id}/candidates", path_params={"topic_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


# --- Moderation queue -----------------------------------------------------


def _put_article(article_id="article-1", status="pending_moderation"):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    table.put_item(
        Item={
            "article_id": article_id,
            "topic_id": "github-trending",
            "title": "A Title",
            "body_s3_key": f"articles/{article_id}.md",
            "status": status,
            "created_at": "2026-09-12T00:00:00+00:00",
            "published_at": None,
            "source_refs": [],
        }
    )


def _put_moderation_item(
    queue_id="queue-1",
    article_id="article-1",
    status="pending",
    topic_id="github-trending",
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


def test_list_moderation_queue_only_pending(aws_resources):
    _put_moderation_item(queue_id="q-pending", status="pending")
    _put_moderation_item(queue_id="q-approved", status="approved")

    result = admin_api_handler.handler(_event("GET /moderation-queue"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert [item["queue_id"] for item in body["items"]] == ["q-pending"]


def test_moderation_queue_stats_empty(aws_resources):
    result = admin_api_handler.handler(_event("GET /moderation-queue/stats"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body == {
        "total_flagged": 0,
        "by_status": {},
        "by_topic": {},
        "reason_counts": {},
        "recent": [],
    }


def test_moderation_queue_stats_aggregates_across_all_statuses(aws_resources):
    # Unlike GET /moderation-queue (pending only), stats must include
    # approved/rejected history too -- that's the whole point.
    _put_moderation_item(
        queue_id="q-1",
        topic_id="github-trending",
        status="pending",
        reasons=["financial topic - routed to manual moderation regardless of content"],
        created_at="2026-09-10T00:00:00+00:00",
    )
    _put_moderation_item(
        queue_id="q-2",
        topic_id="github-trending",
        status="approved",
        reasons=["financial topic - routed to manual moderation regardless of content"],
        created_at="2026-09-11T00:00:00+00:00",
    )
    _put_moderation_item(
        queue_id="q-3",
        topic_id="crypto",
        status="rejected",
        reasons=["unsubstantiated factual claim"],
        created_at="2026-09-12T00:00:00+00:00",
    )

    result = admin_api_handler.handler(_event("GET /moderation-queue/stats"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])

    assert body["total_flagged"] == 3
    assert body["by_status"] == {"pending": 1, "approved": 1, "rejected": 1}
    assert body["by_topic"] == {"github-trending": 2, "crypto": 1}
    assert body["reason_counts"] == {
        "financial topic - routed to manual moderation regardless of content": 2,
        "unsubstantiated factual claim": 1,
    }
    # Most recent first.
    assert [item["queue_id"] for item in body["recent"]] == ["q-3", "q-2", "q-1"]


def test_approve_moderation_item(aws_resources):
    _put_article()
    _put_moderation_item()

    event = _event("POST /moderation-queue/{queue_id}/approve", path_params={"queue_id": "queue-1"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"approved": "queue-1", "article_id": "article-1"}

    articles_table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    article = articles_table.get_item(Key={"article_id": "article-1"})["Item"]
    assert article["status"] == "published"
    assert article["published_at"] is not None

    queue_table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    queue_item = queue_table.get_item(Key={"queue_id": "queue-1"})["Item"]
    assert queue_item["status"] == "approved"


def test_reject_moderation_item(aws_resources):
    _put_article()
    _put_moderation_item()

    event = _event("POST /moderation-queue/{queue_id}/reject", path_params={"queue_id": "queue-1"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"rejected": "queue-1", "article_id": "article-1"}

    articles_table = boto3.resource("dynamodb", region_name=REGION).Table("Articles")
    article = articles_table.get_item(Key={"article_id": "article-1"})["Item"]
    assert article["status"] == "rejected"
    assert article["published_at"] is None

    queue_table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    queue_item = queue_table.get_item(Key={"queue_id": "queue-1"})["Item"]
    assert queue_item["status"] == "rejected"


def test_approve_unknown_queue_item_returns_404(aws_resources):
    event = _event("POST /moderation-queue/{queue_id}/approve", path_params={"queue_id": "nope"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_approve_already_actioned_returns_409(aws_resources):
    _put_article()
    _put_moderation_item(status="approved")

    event = _event("POST /moderation-queue/{queue_id}/approve", path_params={"queue_id": "queue-1"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 409


def test_reject_already_actioned_returns_409(aws_resources):
    _put_article()
    _put_moderation_item(status="rejected")

    event = _event("POST /moderation-queue/{queue_id}/reject", path_params={"queue_id": "queue-1"})
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 409


# --- Prompt refinements -----------------------------------------------


def _put_refinement(
    topic_id="github-trending",
    version="2026-09-12T00:00:00+00:00",
    status="pending",
    rationale="recent feedback skewed negative",
    prompt_changes="Write in a more accessible style.",
):
    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    item = {
        "topic_id": topic_id,
        "version": version,
        "proposed_at": version,
        "rationale": rationale,
        "prompt_changes": prompt_changes,
        "status": status,
    }
    table.put_item(Item=item)
    return item


def test_list_prompt_refinements_no_filters(aws_resources):
    _put_refinement(topic_id="topic-a", version="v1")
    _put_refinement(topic_id="topic-b", version="v1")

    result = admin_api_handler.handler(_event("GET /prompt-refinements"), None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert {r["topic_id"] for r in body["refinements"]} == {"topic-a", "topic-b"}


def test_list_prompt_refinements_filters_by_topic_and_status(aws_resources):
    _put_refinement(topic_id="topic-a", version="v1", status="pending")
    _put_refinement(topic_id="topic-a", version="v2", status="approved")
    _put_refinement(topic_id="topic-b", version="v1", status="pending")

    event = {
        "routeKey": "GET /prompt-refinements",
        "queryStringParameters": {"topic_id": "topic-a", "status": "pending"},
    }
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert len(body["refinements"]) == 1
    assert body["refinements"][0]["version"] == "v1"
    assert body["refinements"][0]["topic_id"] == "topic-a"


def test_approve_prompt_refinement_success(aws_resources):
    _put_refinement()
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/approve",
        path_params={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "approved": {"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"}
    }

    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    item = table.get_item(
        Key={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"}
    )["Item"]
    assert item["status"] == "approved"


def test_reject_prompt_refinement_success(aws_resources):
    _put_refinement()
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/reject",
        path_params={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "rejected": {"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"}
    }

    table = boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")
    item = table.get_item(
        Key={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"}
    )["Item"]
    assert item["status"] == "rejected"


def test_approve_prompt_refinement_not_found_returns_404(aws_resources):
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/approve",
        path_params={"topic_id": "nope", "version": "nope"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 404


def test_approve_prompt_refinement_already_decided_returns_409(aws_resources):
    _put_refinement(status="approved")
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/approve",
        path_params={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 409


def test_reject_prompt_refinement_already_decided_returns_409(aws_resources):
    _put_refinement(status="rejected")
    event = _event(
        "POST /prompt-refinements/{topic_id}/{version}/reject",
        path_params={"topic_id": "github-trending", "version": "2026-09-12T00:00:00+00:00"},
    )
    result = admin_api_handler.handler(event, None)
    assert result["statusCode"] == 409


# --- Exception safety / routing -----------------------------------------


def test_unknown_route_returns_404(aws_resources):
    result = admin_api_handler.handler(_event("GET /nope"), None)
    assert result["statusCode"] == 404


def test_unhandled_exception_returns_500(aws_resources):
    with patch("admin_api_handler.list_topics", side_effect=RuntimeError("boom")):
        result = admin_api_handler.handler(_event("GET /topics"), None)

    assert result["statusCode"] == 500
    assert "error" in json.loads(result["body"])
