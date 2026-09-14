from __future__ import annotations

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import trending_digest_handler

ENV = {
    "TOPICS_TABLE": "Topics",
    "FINDINGS_TABLE": "Findings",
    "ARTICLES_TABLE": "Articles",
    "MODERATION_QUEUE_TABLE": "ModerationQueue",
    "CONTENT_BUCKET": "bloggerbear-content-test",
    "BEDROCK_MODEL_ID": "anthropic.claude-test-model",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def s3_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="ap-southeast-2")
        client.create_bucket(
            Bucket=ENV["CONTENT_BUCKET"],
            CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
        )
        yield client


GITHUB_TOPIC = {"topic_id": "github-trending", "name": "GitHub Trending", "is_financial": False}
HN_TOPIC = {"topic_id": "hacker-news", "name": "Hacker News", "is_financial": False}
CRYPTO_TOPIC = {"topic_id": "crypto", "name": "Crypto Markets", "is_financial": True}

RECENT = "2026-09-14T00:00:00+00:00"
STALE = "2026-01-01T00:00:00+00:00"


def _finding(summary="Something happened.", source_refs=None, captured_at=RECENT):
    return {
        "summary": summary,
        "source_refs": source_refs or [],
        "captured_at": captured_at,
    }


def test_no_topics_returns_no_recent_findings(s3_bucket):
    with patch("trending_digest_handler.list_topics", return_value=[]):
        result = trending_digest_handler.handler({}, None)

    assert result == {"status": "no_recent_findings"}


def test_topics_with_stale_or_missing_findings_are_skipped(s3_bucket):
    findings_by_topic = {"github-trending": None, "hacker-news": _finding(captured_at=STALE)}

    with (
        patch("trending_digest_handler.list_topics", return_value=[GITHUB_TOPIC, HN_TOPIC]),
        patch(
            "trending_digest_handler.get_latest_finding",
            side_effect=lambda topic_id: findings_by_topic[topic_id],
        ),
    ):
        result = trending_digest_handler.handler({}, None)

    assert result == {"status": "no_recent_findings"}


def test_publishes_digest_when_compliant(s3_bucket):
    findings_by_topic = {
        "github-trending": _finding(
            "Repo X is trending.", source_refs=[{"url": "https://github.com/x", "title": "x"}]
        ),
        "hacker-news": _finding(
            "Story Y hit the front page.", source_refs=[{"url": "https://hn.example/y", "title": "y"}]
        ),
    }

    with (
        patch("trending_digest_handler.list_topics", return_value=[GITHUB_TOPIC, HN_TOPIC]),
        patch(
            "trending_digest_handler.get_latest_finding",
            side_effect=lambda topic_id: findings_by_topic[topic_id],
        ),
        patch("trending_digest_handler.invoke_claude", return_value="A synthesized digest.") as mock_invoke,
        patch(
            "trending_digest_handler.compliance.review_draft",
            return_value={"compliant": True, "reasons": []},
        ) as mock_review,
        patch("trending_digest_handler.put_article") as mock_put_article,
        patch("trending_digest_handler.put_moderation_item") as mock_put_moderation,
    ):
        result = trending_digest_handler.handler({}, None)

    assert result["status"] == "published"
    assert result["compliant"] is True

    # Both topics' summaries must have been folded into the synthesis prompt.
    prompt = mock_invoke.call_args.args[0]
    assert "Repo X is trending." in prompt
    assert "Story Y hit the front page." in prompt
    # Neither topic is financial -> no financial guidance in the prompt, and
    # no disclaimer on the stored body (checked below).
    assert "Financial-topic guidance (mandatory):" not in prompt

    # Neither contributing topic is financial -> reviewed as non-financial.
    mock_review.assert_called_once()
    assert mock_review.call_args.args[1] == {"is_financial": False}

    mock_put_article.assert_called_once()
    article_kwargs = mock_put_article.call_args.kwargs
    assert article_kwargs["topic_id"] == "digest"
    assert article_kwargs["status"] == "published"
    assert article_kwargs["published_at"] is not None
    assert article_kwargs["source_refs"] == [
        {"url": "https://github.com/x", "title": "x"},
        {"url": "https://hn.example/y", "title": "y"},
    ]
    mock_put_moderation.assert_not_called()

    stored = s3_bucket.get_object(Bucket=ENV["CONTENT_BUCKET"], Key=article_kwargs["body_s3_key"])
    assert stored["Body"].read().decode("utf-8") == "A synthesized digest."


def test_any_financial_contributor_routes_digest_to_moderation(s3_bucket):
    findings_by_topic = {
        "github-trending": _finding("Repo X is trending."),
        "crypto": _finding("Bitcoin moved 10%."),
    }

    with (
        patch("trending_digest_handler.list_topics", return_value=[GITHUB_TOPIC, CRYPTO_TOPIC]),
        patch(
            "trending_digest_handler.get_latest_finding",
            side_effect=lambda topic_id: findings_by_topic[topic_id],
        ),
        patch(
            "trending_digest_handler.invoke_claude", return_value="A synthesized digest."
        ) as mock_invoke,
        patch(
            "trending_digest_handler.compliance.review_draft",
            return_value={
                "compliant": False,
                "reasons": ["financial topic - routed to manual moderation regardless of content"],
            },
        ) as mock_review,
        patch("trending_digest_handler.put_article") as mock_put_article,
        patch("trending_digest_handler.put_moderation_item") as mock_put_moderation,
    ):
        result = trending_digest_handler.handler({}, None)

    # One contributing topic (crypto) is financial -> the synthesis prompt
    # must carry the mandatory financial guidance, and review_draft must be
    # called with is_financial=True, regardless of the other (non-financial)
    # contributor.
    synthesis_prompt = mock_invoke.call_args.args[0]
    assert "Financial-topic guidance (mandatory):" in synthesis_prompt
    assert "Do not use recommendation language" in synthesis_prompt
    assert mock_review.call_args.args[1] == {"is_financial": True}

    assert result["status"] == "pending_moderation"
    assert result["compliant"] is False
    mock_put_article.assert_called_once()
    assert mock_put_article.call_args.kwargs["status"] == "pending_moderation"
    assert mock_put_article.call_args.kwargs["published_at"] is None
    mock_put_moderation.assert_called_once()
    moderation_kwargs = mock_put_moderation.call_args.kwargs
    assert moderation_kwargs["topic_id"] == "digest"
    assert moderation_kwargs["reasons"] == [
        "financial topic - routed to manual moderation regardless of content"
    ]

    # The standing disclaimer must be appended to the stored draft body
    # (even though it's pending moderation, not yet published) --
    # deterministic, not left to the model to remember.
    body_s3_key = mock_put_article.call_args.kwargs["body_s3_key"]
    stored = s3_bucket.get_object(Bucket=ENV["CONTENT_BUCKET"], Key=body_s3_key)
    stored_body = stored["Body"].read().decode("utf-8")
    assert stored_body.startswith("A synthesized digest.")
    assert "not constitute financial or investment advice" in stored_body


def test_unhandled_exception_returns_error_dict(s3_bucket):
    with patch("trending_digest_handler.list_topics", side_effect=RuntimeError("boom")):
        result = trending_digest_handler.handler({}, None)

    assert result == {"status": "error", "error": "boom"}
