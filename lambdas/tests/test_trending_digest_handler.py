from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

# Computed relative to the real clock (not hardcoded) so this test doesn't
# silently go stale itself once enough real time has passed -- RECENT must
# stay within trending_digest_handler.DIGEST_LOOKBACK_HOURS, STALE well
# outside it, regardless of what "now" is when the suite runs.
_NOW = datetime.now(UTC)
RECENT = (_NOW - timedelta(hours=1)).isoformat()
STALE = (_NOW - timedelta(days=30)).isoformat()


def _finding(summary="Something happened.", source_refs=None, captured_at=RECENT):
    return {
        "summary": summary,
        "source_refs": source_refs or [],
        "captured_at": captured_at,
    }


def _tracked_result(text, *, model_id="anthropic.claude-test-model", used_fallback=False):
    return {
        "text": text,
        "model_id": model_id,
        "input_tokens": 10,
        "output_tokens": 5,
        "used_fallback": used_fallback,
    }


_DUMMY_LINEAGE = {
    "calls": [],
    "total_input_tokens": 20,
    "total_output_tokens": 10,
    "models_used": ["anthropic.claude-test-model"],
    "cost_aud": 0.01,
    "cost_note": None,
}

_DUMMY_LINEAGE_CALL = {
    "stage": "compliance_review",
    "model_id": "anthropic.claude-test-model",
    "input_tokens": 10,
    "output_tokens": 5,
    "used_fallback": False,
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
        patch("trending_digest_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("trending_digest_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "trending_digest_handler.invoke_model_tracked",
            return_value=_tracked_result("A synthesized digest."),
        ) as mock_invoke,
        patch(
            "trending_digest_handler.compliance.review_draft",
            return_value={"compliant": True, "reasons": [], "lineage_call": _DUMMY_LINEAGE_CALL},
        ) as mock_review,
        patch("trending_digest_handler.put_article") as mock_put_article,
        patch("trending_digest_handler.put_moderation_item") as mock_put_moderation,
        patch("trending_digest_handler.render_and_publish_article_page") as mock_render_page,
        patch("trending_digest_handler.generate_and_store_article_musing") as mock_musing,
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

    # AI lineage/cost tracking (docs/project-plan.md §11, PR 2 of 5) --
    # same pattern as daily_cycle_handler's own compliant branch.
    assert article_kwargs["published_by"] == "ai_only"
    assert article_kwargs["lineage"] == _DUMMY_LINEAGE

    stored = s3_bucket.get_object(Bucket=ENV["CONTENT_BUCKET"], Key=article_kwargs["body_s3_key"])
    assert stored["Body"].read().decode("utf-8") == "A synthesized digest."

    # Bugfix regression check: a digest article that publishes cleanly on
    # the first pass must also get a static page and a musing, same as
    # daily_cycle_handler.py's own compliant branch -- previously missed
    # entirely.
    mock_render_page.assert_called_once()
    render_kwargs = mock_render_page.call_args.kwargs
    assert render_kwargs["article_id"] == article_kwargs["article_id"]
    assert render_kwargs["topic_name"] == "Trending Everywhere"
    assert render_kwargs["body_markdown"] == "A synthesized digest."
    # AI lineage/cost tracking (docs/project-plan.md §11, PR 3 of 5).
    assert render_kwargs["lineage"] == article_kwargs["lineage"]
    assert render_kwargs["published_by"] == "ai_only"

    mock_musing.assert_called_once()
    musing_kwargs = mock_musing.call_args.kwargs
    assert musing_kwargs["article_id"] == article_kwargs["article_id"]
    assert musing_kwargs["topic_id"] == "digest"
    assert musing_kwargs["topic_name"] == "Trending Everywhere"
    assert musing_kwargs["compliant"] is True


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
        patch("trending_digest_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("trending_digest_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "trending_digest_handler.invoke_model_tracked",
            return_value=_tracked_result("A synthesized digest."),
        ) as mock_invoke,
        patch(
            "trending_digest_handler.compliance.review_draft",
            return_value={
                "compliant": False,
                "reasons": ["financial topic - routed to manual moderation regardless of content"],
                "lineage_call": _DUMMY_LINEAGE_CALL,
            },
        ) as mock_review,
        patch("trending_digest_handler.put_article") as mock_put_article,
        patch("trending_digest_handler.put_moderation_item") as mock_put_moderation,
        patch("trending_digest_handler.render_and_publish_article_page") as mock_render_page,
        patch("trending_digest_handler.generate_and_store_article_musing") as mock_musing,
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
    assert mock_put_article.call_args.kwargs["published_by"] is None
    mock_put_moderation.assert_called_once()
    moderation_kwargs = mock_put_moderation.call_args.kwargs
    assert moderation_kwargs["topic_id"] == "digest"
    assert moderation_kwargs["reasons"] == [
        "financial topic - routed to manual moderation regardless of content"
    ]
    # Not published on this pass -- nothing to render a static page for or
    # muse about yet (see the compliant-path test for when these do fire).
    mock_render_page.assert_not_called()
    mock_musing.assert_not_called()

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
