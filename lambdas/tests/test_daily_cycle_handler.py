from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import daily_cycle_handler
from common.editorial_goals import (
    ARTICLE_STYLES,
    EDITORIAL_MANDATES,
    EditorialGoal,
    goal_for_date,
)


def _tracked_result(
    text, *, model_id="anthropic.claude-test-model", used_fallback=False, input_tokens=10, output_tokens=5
):
    return {
        "text": text,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "used_fallback": used_fallback,
    }


_DUMMY_LINEAGE = {
    "calls": [],
    "total_input_tokens": 30,
    "total_output_tokens": 15,
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

ENV = {
    "TOPICS_TABLE": "Topics",
    "FINDINGS_TABLE": "Findings",
    "CANDIDATE_IDEAS_TABLE": "CandidateIdeas",
    "ARTICLES_TABLE": "Articles",
    "MODERATION_QUEUE_TABLE": "ModerationQueue",
    "CONTENT_BUCKET": "bloggerbear-content-test",
    "SITE_BUCKET": "bloggerbear-site-test",
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


NON_FINANCIAL_TOPIC = {
    "topic_id": "github-trending",
    "name": "GitHub Trending",
    "adapter": "github_trending",
    "adapter_config": {},
    "is_financial": False,
}

FINANCIAL_TOPIC = {
    "topic_id": "crypto",
    "name": "Crypto Markets",
    "adapter": "crypto_feed",
    "adapter_config": {},
    "is_financial": True,
}

FINDINGS = [
    {
        "topic_id": "github-trending",
        "captured_at": "2026-09-12T00:00:00+00:00",
        "summary": "Repo X jumped to #1 trending after a viral launch post.",
        "source_refs": [{"url": "https://github.com/example/x", "title": "example/x"}],
    },
    {
        "topic_id": "github-trending",
        "captured_at": "2026-09-11T00:00:00+00:00",
        "summary": "Repo Y gained stars following a conference talk.",
        "source_refs": [{"url": "https://github.com/example/y", "title": "example/y"}],
    },
]


def test_handler_missing_topic_id_returns_error():
    result = daily_cycle_handler.handler({}, None)
    assert result["status"] == "error"


def test_handler_topic_not_found_returns_error(s3_bucket):
    with patch("daily_cycle_handler.get_topic", return_value=None):
        result = daily_cycle_handler.handler({"topic_id": "unknown"}, None)

    assert result == {
        "status": "error",
        "topic_id": "unknown",
        "error": "topic not found",
    }


def test_handler_no_findings_returns_no_findings_status(s3_bucket):
    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=[]),
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result == {"status": "no_findings", "topic_id": "github-trending"}


def test_handler_publishes_when_compliant(s3_bucket):
    ideation_response = "Angle one about repo X\nAngle two about repo Y\nAngle three misc"
    invoke_responses = [ideation_response, "# Draft body\n\nSome article content.", "A Great Title"]

    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch("daily_cycle_handler.get_latest_approved_prompt_refinement", return_value=None),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in invoke_responses],
        ) as mock_invoke,
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={"compliant": True, "reasons": [], "lineage_call": _DUMMY_LINEAGE_CALL},
        ) as mock_review,
        patch(
            "daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea
        ) as mock_put_candidate,
        patch("daily_cycle_handler.put_article") as mock_put_article,
        patch("daily_cycle_handler.put_moderation_item") as mock_put_moderation,
        patch("daily_cycle_handler.render_and_publish_article_page") as mock_render_page,
        patch("daily_cycle_handler.generate_and_store_article_musing") as mock_musing,
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result["status"] == "published"
    assert result["compliant"] is True
    assert result["topic_id"] == "github-trending"
    assert "article_id" in result

    assert mock_invoke.call_count == 3
    mock_review.assert_called_once()

    # 3 "considered" + 1 "selected" re-write of the first candidate.
    assert mock_put_candidate.call_count == 4
    statuses = [
        call.kwargs.get("status", call.args[-1] if call.args else None)
        for call in mock_put_candidate.call_args_list
    ]
    assert statuses.count("selected") == 1

    mock_put_article.assert_called_once()
    article_kwargs = mock_put_article.call_args.kwargs
    assert article_kwargs["status"] == "published"
    assert article_kwargs["published_at"] is not None
    assert article_kwargs["source_refs"] == [
        {"url": "https://github.com/example/x", "title": "example/x"},
        {"url": "https://github.com/example/y", "title": "example/y"},
    ]

    # AI lineage/cost tracking (docs/project-plan.md §11, PR 2 of 5):
    # published_by="ai_only" on a compliant, fully-automatic publish, and
    # the lineage dict from build_lineage (mocked above) passed through
    # untouched.
    assert article_kwargs["published_by"] == "ai_only"
    assert article_kwargs["lineage"]["models_used"] == ["anthropic.claude-test-model"]
    assert article_kwargs["lineage"]["total_input_tokens"] == 30

    mock_put_moderation.assert_not_called()

    # The draft body must have actually been written to S3.
    stored = s3_bucket.get_object(
        Bucket=ENV["CONTENT_BUCKET"], Key=article_kwargs["body_s3_key"]
    )
    assert stored["Body"].read().decode("utf-8") == "# Draft body\n\nSome article content."

    # Static article publishing (docs/project-plan.md §11): the compliant
    # branch must render the static page immediately, using the
    # already-in-memory draft text rather than re-reading it from S3.
    mock_render_page.assert_called_once()
    render_kwargs = mock_render_page.call_args.kwargs
    assert render_kwargs["article_id"] == article_kwargs["article_id"]
    assert render_kwargs["title"] == "A Great Title"
    assert render_kwargs["body_markdown"] == "# Draft body\n\nSome article content."
    assert render_kwargs["topic_name"] == "GitHub Trending"
    assert render_kwargs["published_at"] == article_kwargs["published_at"]
    assert render_kwargs["source_refs"] == article_kwargs["source_refs"]
    assert render_kwargs["view_count"] == 0
    # AI lineage/cost tracking (docs/project-plan.md §11, PR 3 of 5): the
    # static page gets the same lineage put_article was called with, and
    # published_by="ai_only" since this is the compliant, published-
    # cleanly branch.
    assert render_kwargs["lineage"] == article_kwargs["lineage"]
    assert render_kwargs["published_by"] == "ai_only"

    # A musing gets generated for the same compliant publish, with
    # compliant=True (published cleanly -- the "proud" mood, not the
    # "needed a second look" one).
    mock_musing.assert_called_once()
    musing_kwargs = mock_musing.call_args.kwargs
    assert musing_kwargs["article_id"] == article_kwargs["article_id"]
    assert musing_kwargs["topic_id"] == "github-trending"
    assert musing_kwargs["topic_name"] == "GitHub Trending"
    assert musing_kwargs["title"] == "A Great Title"
    assert musing_kwargs["compliant"] is True


def test_handler_dedupes_duplicate_source_refs_before_publishing(s3_bucket):
    findings = [
        {
            "topic_id": "github-trending",
            "captured_at": "2026-09-12T00:00:00+00:00",
            "summary": "Repo X jumped.",
            "source_refs": [{"url": "https://github.com/example/x", "title": "example/x"}],
        },
        {
            "topic_id": "github-trending",
            "captured_at": "2026-09-11T00:00:00+00:00",
            "summary": "Repo X stayed hot.",
            "source_refs": [{"url": "https://github.com/example/x", "title": "example/x"}],
        },
    ]
    invoke_responses = ["Angle one", "Draft body text.", "Some Title"]

    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=findings),
        patch("daily_cycle_handler.get_latest_approved_prompt_refinement", return_value=None),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in invoke_responses],
        ),
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={"compliant": True, "reasons": [], "lineage_call": _DUMMY_LINEAGE_CALL},
        ),
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article") as mock_put_article,
        patch("daily_cycle_handler.render_and_publish_article_page"),
        patch("daily_cycle_handler.generate_and_store_article_musing"),
    ):
        daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert mock_put_article.call_args.kwargs["source_refs"] == [
        {"url": "https://github.com/example/x", "title": "example/x"}
    ]


def test_handler_moderates_when_non_compliant(s3_bucket):
    ideation_response = "Angle one\nAngle two\nAngle three"
    invoke_responses = [ideation_response, "Draft body text.", "Some Title"]

    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch("daily_cycle_handler.get_latest_approved_prompt_refinement", return_value=None),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in invoke_responses],
        ),
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={
                "compliant": False,
                "reasons": ["unsubstantiated claim"],
                "lineage_call": _DUMMY_LINEAGE_CALL,
            },
        ),
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article") as mock_put_article,
        patch("daily_cycle_handler.put_moderation_item") as mock_put_moderation,
        patch("daily_cycle_handler.render_and_publish_article_page") as mock_render_page,
        patch("daily_cycle_handler.generate_and_store_article_musing") as mock_musing,
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result["status"] == "pending_moderation"
    assert result["compliant"] is False
    assert result["reasons"] == ["unsubstantiated claim"]

    article_kwargs = mock_put_article.call_args.kwargs
    assert article_kwargs["status"] == "pending_moderation"
    assert article_kwargs["published_at"] is None
    # Not yet decided who publishes it -- see admin_api_handler.py's
    # moderation-approve/force-publish routes, which set "humans" later.
    assert article_kwargs["published_by"] is None
    # Lineage (tokens/models/cost) is still recorded even though this
    # didn't publish -- it describes how the draft was written, not
    # whether it ended up live (docs/project-plan.md §11, PR 2 of 5).
    assert article_kwargs["lineage"]["models_used"] == ["anthropic.claude-test-model"]

    mock_put_moderation.assert_called_once()
    moderation_kwargs = mock_put_moderation.call_args.kwargs
    assert moderation_kwargs["article_id"] == result["article_id"]
    assert moderation_kwargs["topic_id"] == "github-trending"
    assert moderation_kwargs["reasons"] == ["unsubstantiated claim"]

    # A pending_moderation article isn't public yet -- no static page
    # should exist until it's actually approved (see admin_api_handler.py's
    # _resolve_moderation_item, tested separately).
    mock_render_page.assert_not_called()
    # Same reasoning for the musing -- nothing to reflect on publishing
    # until it's actually published.
    mock_musing.assert_not_called()


def test_handler_financial_topic_routes_to_moderation_without_calling_bedrock_for_review(
    s3_bucket,
):
    ideation_response = "Angle one\nAngle two\nAngle three"
    invoke_responses = [ideation_response, "Draft body text.", "Some Title"]

    with (
        patch("daily_cycle_handler.get_topic", return_value=FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch("daily_cycle_handler.get_latest_approved_prompt_refinement", return_value=None),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in invoke_responses],
        ) as mock_invoke,
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article") as mock_put_article,
        patch("daily_cycle_handler.put_moderation_item") as mock_put_moderation,
    ):
        result = daily_cycle_handler.handler({"topic_id": "crypto"}, None)

    # Ideation, draft, and title still call Bedrock (3 calls) but the
    # compliance review itself must never invoke Bedrock for a financial
    # topic -- so total invoke_claude calls stays at exactly 3.
    assert mock_invoke.call_count == 3

    assert result["status"] == "pending_moderation"
    assert result["compliant"] is False
    assert result["reasons"] == [
        "financial topic - routed to manual moderation regardless of content"
    ]

    mock_put_article.assert_called_once()
    assert mock_put_article.call_args.kwargs["status"] == "pending_moderation"
    mock_put_moderation.assert_called_once()


def test_handler_financial_topic_folds_guidance_into_prompts_and_appends_disclaimer(
    s3_bucket,
):
    ideation_response = "Angle one\nAngle two\nAngle three"
    invoke_responses = [ideation_response, "Draft body text.", "Some Title"]

    with (
        patch("daily_cycle_handler.get_topic", return_value=FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch("daily_cycle_handler.get_latest_approved_prompt_refinement", return_value=None),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in invoke_responses],
        ) as mock_invoke,
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article") as mock_put_article,
        patch("daily_cycle_handler.put_moderation_item"),
    ):
        daily_cycle_handler.handler({"topic_id": "crypto"}, None)

    # Ideation prompt (call 0) and draft prompt (call 1) must both carry
    # the mandatory financial-topic guidance; the title prompt (call 2)
    # doesn't need it.
    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    assert "Financial-topic guidance (mandatory):" in ideation_prompt
    assert "Financial-topic guidance (mandatory):" in draft_prompt
    assert 'Do not use recommendation language' in ideation_prompt
    assert 'Do not use recommendation language' in draft_prompt

    # The standing disclaimer must be appended to the stored draft body
    # regardless of what the model actually wrote.
    body_s3_key = mock_put_article.call_args.kwargs["body_s3_key"]
    stored = s3_bucket.get_object(Bucket=ENV["CONTENT_BUCKET"], Key=body_s3_key)
    stored_body = stored["Body"].read().decode("utf-8")
    assert stored_body.startswith("Draft body text.")
    assert "not constitute financial or investment advice" in stored_body


def test_handler_catches_unexpected_exception(s3_bucket):
    with patch("daily_cycle_handler.get_topic", side_effect=RuntimeError("boom")):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result["status"] == "error"
    assert result["topic_id"] == "github-trending"
    assert "boom" in result["error"]


def _fake_put_candidate_idea(topic_id, created_at, angle, status="considered"):
    return {
        "topic_id": topic_id,
        "created_at": created_at,
        "angle": angle,
        "status": status,
    }


# --- Phase 5: prompt-refinement guidance / few-shot splicing ---------------


def test_approved_prompt_refinement_guidance_appended_to_ideation_and_draft_prompts(s3_bucket):
    ideation_response = "Angle one\nAngle two\nAngle three"
    invoke_responses = [ideation_response, "Draft body text.", "Some Title"]
    refinement = {
        "topic_id": "github-trending",
        "version": "2026-09-01T00:00:00+00:00",
        "rationale": "reader feedback skewed negative",
        "prompt_changes": "Write in a more accessible, less jargon-heavy style.",
        "status": "approved",
    }

    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch(
            "daily_cycle_handler.get_latest_approved_prompt_refinement",
            return_value=refinement,
        ),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in invoke_responses],
        ) as mock_invoke,
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={"compliant": True, "reasons": [], "lineage_call": _DUMMY_LINEAGE_CALL},
        ),
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article"),
        patch("daily_cycle_handler.put_moderation_item"),
        patch("daily_cycle_handler.render_and_publish_article_page"),
        patch("daily_cycle_handler.generate_and_store_article_musing"),
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result["status"] == "published"

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    title_prompt = mock_invoke.call_args_list[2].args[0]

    guidance_text = "Write in a more accessible, less jargon-heavy style."
    assert "Additional guidance based on reader feedback:" in ideation_prompt
    assert guidance_text in ideation_prompt
    assert "Additional guidance based on reader feedback:" in draft_prompt
    assert guidance_text in draft_prompt
    # Title prompt is untouched by this feature.
    assert "reader feedback" not in title_prompt


def test_top_voted_article_excerpt_appended_to_draft_prompt_only(s3_bucket):
    ideation_response = "Angle one\nAngle two\nAngle three"
    invoke_responses = [ideation_response, "Draft body text.", "Some Title"]

    top_voted_body = "This is the body of the best-received past article. " * 20
    s3_bucket.put_object(
        Bucket=ENV["CONTENT_BUCKET"], Key="articles/top-voted.md", Body=top_voted_body.encode("utf-8")
    )
    top_articles = [{"article_id": "top-voted", "body_s3_key": "articles/top-voted.md"}]

    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch("daily_cycle_handler.get_latest_approved_prompt_refinement", return_value=None),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=top_articles) as mock_top,
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in invoke_responses],
        ) as mock_invoke,
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={"compliant": True, "reasons": [], "lineage_call": _DUMMY_LINEAGE_CALL},
        ),
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article"),
        patch("daily_cycle_handler.put_moderation_item"),
        patch("daily_cycle_handler.render_and_publish_article_page"),
        patch("daily_cycle_handler.generate_and_store_article_musing"),
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result["status"] == "published"
    mock_top.assert_called_once_with("github-trending", limit=1)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]

    excerpt = top_voted_body[:500]
    assert excerpt in draft_prompt
    assert "well-received past article" in draft_prompt
    # Few-shot examples are a drafting-stage concern only, not ideation.
    assert excerpt not in ideation_prompt
    assert "well-received past article" not in ideation_prompt


def test_no_refinement_and_no_top_voted_article_leaves_prompts_unchanged(s3_bucket):
    """Regression check: with neither Phase 5 addition present, the prompts are
    exactly the base prompt plus the topic-relevance guardrails."""
    ideation_response = "Angle one\nAngle two\nAngle three"
    invoke_responses = [ideation_response, "Draft body text.", "Some Title"]

    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch("daily_cycle_handler.get_latest_approved_prompt_refinement", return_value=None),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in invoke_responses],
        ) as mock_invoke,
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={"compliant": True, "reasons": [], "lineage_call": _DUMMY_LINEAGE_CALL},
        ),
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article"),
        patch("daily_cycle_handler.put_moderation_item"),
        patch("daily_cycle_handler.render_and_publish_article_page"),
        patch("daily_cycle_handler.generate_and_store_article_musing"),
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result["status"] == "published"

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]

    expected_ideation = (
        "Based on the following recent research findings about "
        "'GitHub Trending', propose exactly 3 distinct, specific candidate "
        "article angles. Reply with exactly one angle per line, no "
        "numbering, no extra commentary.\n\n"
        "ACTIVE EDITORIAL MANDATE:\n"
        "Adapter-Specific Standard Goal: Focus on rapid open-source star acceleration, "
        "architectural paradigm shifts (e.g., new framework primitives), and infrastructural "
        "utilities.\n\n"
        "CRITICAL RELEVANCE RULE:\n"
        "You are a strict domain-specific writer. Every proposed angle MUST remain deeply "
        "relevant to the core theme of 'GitHub Trending'. If the raw data findings contain "
        "fringe, accidental, or off-topic subjects (e.g., pop culture, unrelated hobbies, "
        "speculative fiction, or internet noise), you MUST either completely ignore those "
        "findings or aggressively reframe them strictly through the functional lens of "
        "'GitHub Trending'. Do not wander off-topic.\n"
        "Every proposed angle MUST also directly serve the Active Editorial Mandate above. If "
        "raw data findings contain noisy or off-topic subjects, you MUST aggressively reframe "
        "them through the lens of this mandate.\n\n"
        f"Findings:\n{daily_cycle_handler._format_findings_summaries(FINDINGS)}"
    )
    expected_draft = (
        "Write a full article draft in markdown (a few paragraphs) for a "
        "blog about 'GitHub Trending', on this angle: Angle one\n\n"
        "CORE EDITORIAL DIRECTION:\n"
        "Adapter-Specific Standard Goal: Focus on rapid open-source star acceleration, "
        "architectural paradigm shifts (e.g., new framework primitives), and infrastructural "
        "utilities.\n\n"
        f"Base it on these recent findings:\n{daily_cycle_handler._format_findings_summaries(FINDINGS)}"
        "\n\nCRITICAL RELEVANCE BOUNDARY:\n"
        "The primary mandate of this publication is to provide high-signal commentary on "
        "'GitHub Trending'. Maintain absolute thematic integrity. Under no circumstances should "
        "you dive into literal or surface-level interpretations of noisy data inputs (for "
        "example, interpreting a technical 'cookbook' repository as literal culinary recipes, "
        "or general interest forum posts as core domain facts). Every paragraph must deliver "
        "value directly aligned with the expectation of a reader subscribing to 'GitHub Trending'."
        "\n\nMaintain absolute structural alignment with the Core Editorial Direction. Every "
        "paragraph must deliver high-signal insight directly tailored to a reader tracking this "
        "exact objective."
    )

    assert ideation_prompt == expected_ideation
    assert draft_prompt == expected_draft


# --- editorial goals (crypto feed) --------------------------------------------


def _finding_at(captured_at, summary, url):
    return {
        "topic_id": "crypto",
        "captured_at": captured_at,
        "summary": summary,
        "source_refs": [{"url": url, "title": summary}],
    }


def _run_crypto(topic, findings):
    responses = ["Angle one\nAngle two\nAngle three", "Draft body text.", "Some Title"]
    with (
        patch("daily_cycle_handler.get_topic", return_value=topic),
        patch("daily_cycle_handler.list_recent_findings", return_value=findings),
        patch("daily_cycle_handler.get_latest_approved_prompt_refinement", return_value=None),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("anthropic.claude-test-model", None)),
        patch("daily_cycle_handler.build_lineage", return_value=_DUMMY_LINEAGE),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in responses],
        ) as mock_invoke,
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={"compliant": True, "reasons": [], "lineage_call": _DUMMY_LINEAGE_CALL},
        ),
        patch("daily_cycle_handler.put_article") as mock_put_article,
        patch("daily_cycle_handler.put_moderation_item"),
        patch("daily_cycle_handler.render_and_publish_article_page"),
        patch("daily_cycle_handler.generate_and_store_article_musing"),
    ):
        result = daily_cycle_handler.handler({"topic_id": topic["topic_id"]}, None)
    return result, mock_invoke, mock_put_article


def test_crypto_topic_prompts_carry_todays_goal_and_only_todays_findings(s3_bucket):
    now = datetime.now(UTC)
    findings = [
        _finding_at(now.isoformat(), "Today's finding", "https://example/today"),
        _finding_at((now - timedelta(days=1)).isoformat(), "Yesterday's finding", "https://example/old"),
    ]
    goal = goal_for_date(now.date())

    _, mock_invoke, mock_put_article = _run_crypto(FINANCIAL_TOPIC, findings)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    assert "assigned daily editorial vector for 'Crypto Markets'" in ideation_prompt
    assert f"Editorial Mandate: {EDITORIAL_MANDATES[goal]}" in ideation_prompt
    assert "Reply with exactly one angle per line" in ideation_prompt  # the parser depends on it
    assert "Data Payload:\n- Today's finding" in ideation_prompt
    assert f"Article style: {ARTICLE_STYLES[goal]}" in draft_prompt
    assert "Yesterday's finding" not in ideation_prompt + draft_prompt
    # source_refs must trace only the findings the draft was actually based on
    assert mock_put_article.call_args.kwargs["source_refs"] == [
        {"url": "https://example/today", "title": "Today's finding"}
    ]


def test_with_nothing_captured_today_the_goal_follows_the_newest_findings_own_day(s3_bucket):
    newest_day = date(2026, 9, 12)
    findings = [
        _finding_at("2026-09-12T08:00:00+00:00", "Newest", "https://example/newest"),
        _finding_at("2026-09-11T08:00:00+00:00", "Older", "https://example/older"),
    ]
    goal = goal_for_date(newest_day)

    _, mock_invoke, mock_put_article = _run_crypto(FINANCIAL_TOPIC, findings)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    assert f"Editorial Mandate: {EDITORIAL_MANDATES[goal]}" in ideation_prompt
    assert "Older" not in ideation_prompt
    assert [r["url"] for r in mock_put_article.call_args.kwargs["source_refs"]] == [
        "https://example/newest"
    ]


@pytest.mark.parametrize("goal", list(EditorialGoal))
def test_a_pinned_goal_drives_the_mandate_and_style(s3_bucket, goal):
    topic = {**FINANCIAL_TOPIC, "adapter_config": {"editorial_goal": goal.value}}
    findings = [_finding_at(datetime.now(UTC).isoformat(), "Finding", "https://example/f")]

    _, mock_invoke, _ = _run_crypto(topic, findings)

    assert EDITORIAL_MANDATES[goal] in mock_invoke.call_args_list[0].args[0]
    assert ARTICLE_STYLES[goal] in mock_invoke.call_args_list[1].args[0]


def test_topics_without_a_goal_are_untouched_by_the_goal_logic(s3_bucket):
    findings = [_finding_at(datetime.now(UTC).isoformat(), "Repo news", "https://example/r")]
    old = _finding_at("2026-01-01T00:00:00+00:00", "Old repo news", "https://example/o")

    _, mock_invoke, mock_put_article = _run_crypto(NON_FINANCIAL_TOPIC, [*findings, old])

    prompts = mock_invoke.call_args_list[0].args[0] + mock_invoke.call_args_list[1].args[0]
    # no rotating daily goal (its "Editorial Mandate:" line / article style) --
    # only the standing goal's uppercase ACTIVE EDITORIAL MANDATE block
    assert "Editorial Mandate:" not in prompts and "Article style" not in prompts
    assert "Old repo news" in prompts  # no day filtering for goal-less topics
    assert len(mock_put_article.call_args.kwargs["source_refs"]) == 2


def test_captured_date_treats_naive_timestamps_as_utc_and_tolerates_garbage():
    assert daily_cycle_handler._captured_date({"captured_at": "2026-09-12T23:30:00"}) == date(2026, 9, 12)
    assert daily_cycle_handler._captured_date(
        {"captured_at": "2026-09-12T23:30:00-05:00"}
    ) == date(2026, 9, 13)
    assert daily_cycle_handler._captured_date({"captured_at": "not a date"}) is None
    assert daily_cycle_handler._captured_date({}) is None


def test_unparseable_finding_dates_fall_back_to_todays_goal_and_all_findings():
    findings = [{"captured_at": "garbage", "summary": "s", "source_refs": []}]

    goal, kept = daily_cycle_handler._select_goal_and_findings(FINANCIAL_TOPIC, findings)

    assert goal is goal_for_date(datetime.now(UTC).date())
    assert kept == findings


# --- topic relevance guardrails ---------------------------------------------------

NOISE_FINDINGS = [
    _finding_at(
        "2026-09-20T01:00:00+00:00",
        "A trending post about a backyard pizza oven repository",
        "https://example/pizza",
    ),
    _finding_at(
        "2026-09-20T02:00:00+00:00", "An alien classification article climbs the front page", "https://example/ufo"
    ),
]


def _topic_named(name, topic_id="some-topic"):
    return {
        "topic_id": topic_id,
        "name": name,
        "adapter": "hacker_news",
        "adapter_config": {},
        "is_financial": False,
    }


@pytest.mark.parametrize("name", ["Security & Hacker News", "Urban Beekeeping", "Quantum Computing"])
def test_ideation_and_drafting_are_anchored_to_whichever_topic_is_active(s3_bucket, name):
    _, mock_invoke, _ = _run_crypto(_topic_named(name), NOISE_FINDINGS)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    assert "CRITICAL RELEVANCE RULE:" in ideation_prompt
    assert f"core theme of '{name}'" in ideation_prompt
    assert f"functional lens of '{name}'" in ideation_prompt
    assert "CRITICAL RELEVANCE BOUNDARY:" in draft_prompt
    assert f"high-signal commentary on '{name}'" in draft_prompt
    assert f"subscribing to '{name}'" in draft_prompt


def test_the_ideation_rule_precedes_the_noisy_findings_and_the_draft_boundary_follows_them(s3_bucket):
    _, mock_invoke, _ = _run_crypto(_topic_named("Security & Hacker News"), NOISE_FINDINGS)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    assert ideation_prompt.index("CRITICAL RELEVANCE RULE") < ideation_prompt.index("pizza oven")
    assert ideation_prompt.index("Reply with exactly one angle per line") < ideation_prompt.index(
        "CRITICAL RELEVANCE RULE"
    )
    assert draft_prompt.index("pizza oven") < draft_prompt.index("CRITICAL RELEVANCE BOUNDARY")


def test_a_topic_without_a_name_is_addressed_by_its_id(s3_bucket):
    topic = {**_topic_named(None, topic_id="raw-topic-id")}

    _, mock_invoke, _ = _run_crypto(topic, NOISE_FINDINGS)

    assert "core theme of 'raw-topic-id'" in mock_invoke.call_args_list[0].args[0]
    assert "commentary on 'raw-topic-id'" in mock_invoke.call_args_list[1].args[0]


def test_editorial_goals_stay_and_are_kept_inside_the_topic(s3_bucket):
    topic = {**FINANCIAL_TOPIC, "adapter_config": {"editorial_goal": "TREND_INVENTOR"}}
    findings = [_finding_at(datetime.now(UTC).isoformat(), "Anchors diverge", "https://example/f")]

    _, mock_invoke, _ = _run_crypto(topic, findings)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    mandate = EDITORIAL_MANDATES[EditorialGoal.TREND_INVENTOR]
    assert ideation_prompt.index("CRITICAL RELEVANCE RULE") < ideation_prompt.index(
        f"Editorial Mandate: {mandate}"
    )
    assert "Apply this mandate strictly within the theme of 'Crypto Markets'" in ideation_prompt
    assert ideation_prompt.index("Editorial Mandate") < ideation_prompt.index("Data Payload:")
    assert "Reply with exactly one angle per line" in ideation_prompt
    assert draft_prompt.index("CRITICAL RELEVANCE BOUNDARY") < draft_prompt.index("Article style:")
    assert ARTICLE_STYLES[EditorialGoal.TREND_INVENTOR] in draft_prompt


def test_the_guardrails_sit_alongside_financial_and_feedback_guidance(s3_bucket):
    _, mock_invoke, _ = _run_crypto(FINANCIAL_TOPIC, [_finding_at(datetime.now(UTC).isoformat(), "F", "https://e/f")])

    for call in mock_invoke.call_args_list[:2]:
        assert "Financial-topic guidance (mandatory):" in call.args[0]
    assert "CRITICAL RELEVANCE RULE" in mock_invoke.call_args_list[0].args[0]
    assert "CRITICAL RELEVANCE BOUNDARY" in mock_invoke.call_args_list[1].args[0]


# --- hierarchical editorial goals (standing objective) -----------------------------

FOCUS = "Identify unpatched zero-day exploits actively being observed in production environments."
EXCLUSIONS = "Ignore generalized marketing press releases or compliance frameworks."


def _topic_with(adapter, **goals):
    topic = {**_topic_named("Cybersecurity & Infrastructure Threats"), "adapter": adapter}
    if goals:
        topic["editorial_goals"] = goals
    return topic


def _positions(prompt, markers):
    return [prompt.index(marker) for marker in markers]


def test_a_topic_specific_goal_reaches_ideation_and_drafting(s3_bucket):
    topic = _topic_with("web_search", primary_focus=FOCUS, exclusion_criteria=EXCLUSIONS)

    _, mock_invoke, _ = _run_crypto(topic, NOISE_FINDINGS)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    block = f"Topic-Specific Focus: {FOCUS}\nStrict Constraints: {EXCLUSIONS}"
    assert f"ACTIVE EDITORIAL MANDATE:\n{block}" in ideation_prompt
    assert f"CORE EDITORIAL DIRECTION:\n{block}" in draft_prompt
    assert "Global Default Goal" not in ideation_prompt + draft_prompt


def test_a_bare_topic_inherits_independent_web_research(s3_bucket):
    _, mock_invoke, _ = _run_crypto(_topic_with("web_search"), NOISE_FINDINGS)

    for call in mock_invoke.call_args_list[:2]:
        assert "Global Default Goal: Execute independent web research" in call.args[0]


def test_an_adapter_default_applies_when_the_topic_sets_no_goal(s3_bucket):
    _, mock_invoke, _ = _run_crypto(_topic_with("github_trending"), NOISE_FINDINGS)

    assert "Adapter-Specific Standard Goal: Focus on rapid open-source star" in (
        mock_invoke.call_args_list[0].args[0]
    )


def test_the_mandate_rule_and_topic_relevance_rule_both_apply_in_the_right_order(s3_bucket):
    _, mock_invoke, _ = _run_crypto(_topic_with("web_search", primary_focus=FOCUS), NOISE_FINDINGS)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    ideation_order = [
        "Reply with exactly one angle per line",
        "ACTIVE EDITORIAL MANDATE:",
        "CRITICAL RELEVANCE RULE",
        "Every proposed angle MUST also directly serve the Active Editorial Mandate",
        "Findings:",
    ]
    positions = _positions(ideation_prompt, ideation_order)
    assert positions == sorted(positions)
    draft_order = [
        "on this angle:",
        "CORE EDITORIAL DIRECTION:",
        "Base it on these recent findings:",
        "CRITICAL RELEVANCE BOUNDARY",
        "Maintain absolute structural alignment with the Core Editorial Direction",
    ]
    positions = _positions(draft_prompt, draft_order)
    assert positions == sorted(positions)


def test_the_standing_goal_and_the_crypto_daily_vector_are_layered_not_replaced(s3_bucket):
    topic = {
        **FINANCIAL_TOPIC,
        "adapter_config": {"editorial_goal": "WEB_AGGREGATOR"},
        "editorial_goals": {
            "primary_focus": "Track institutional flows.",
            "exclusion_criteria": "No price calls.",
        },
    }
    findings = [_finding_at(datetime.now(UTC).isoformat(), "Headline digest", "https://example/h")]

    _, mock_invoke, _ = _run_crypto(topic, findings)

    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    draft_prompt = mock_invoke.call_args_list[1].args[0]
    standing = "Topic-Specific Focus: Track institutional flows.\nStrict Constraints: No price calls."
    assert standing in ideation_prompt
    assert f"Editorial Mandate: {EDITORIAL_MANDATES[EditorialGoal.WEB_AGGREGATOR]}" in ideation_prompt
    assert "and the Active Editorial Mandate above" in ideation_prompt
    assert ideation_prompt.index("ACTIVE EDITORIAL MANDATE") < ideation_prompt.index("Editorial Mandate:")
    assert "CORE EDITORIAL DIRECTION:\nTopic-Specific Focus" in draft_prompt
    assert f"Article style: {ARTICLE_STYLES[EditorialGoal.WEB_AGGREGATOR]}" in draft_prompt
    # financial safety rules are untouched by any editorial goal
    assert "Financial-topic guidance (mandatory):" in ideation_prompt
    assert "Financial-topic guidance (mandatory):" in draft_prompt


def test_a_crypto_topic_with_no_goal_of_its_own_uses_the_crypto_adapter_default(s3_bucket):
    findings = [_finding_at(datetime.now(UTC).isoformat(), "Anchors diverge", "https://example/a")]

    _, mock_invoke, _ = _run_crypto(FINANCIAL_TOPIC, findings)

    assert "Adapter-Specific Standard Goal: Prioritize structural changes in asset cap" in (
        mock_invoke.call_args_list[0].args[0]
    )
