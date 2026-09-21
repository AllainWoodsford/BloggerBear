import json
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
    text,
    *,
    model_id="anthropic.claude-test-model",
    used_fallback=False,
    input_tokens=10,
    output_tokens=5,
    stop_reason="end_turn",
    attempts=1,
):
    return {
        "text": text,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "used_fallback": used_fallback,
        "stop_reason": stop_reason,
        "attempts": attempts,
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


@pytest.fixture(autouse=True)
def _review_off_by_default():
    """The fresh-data review is on (shadow) by default and would fetch live data; these
    tests are about everything else in the cycle. Tests of the review turn it on."""
    with patch("daily_cycle_handler.get_pipeline_config", return_value={"review_mode": "off"}) as mock_config:
        yield mock_config


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


def test_handler_reads_every_finding_from_the_last_day_not_just_the_newest_few(s3_bucket):
    before = datetime.now(UTC)
    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=[]) as mock_list,
    ):
        daily_cycle_handler.handler({"topic_id": "github-trending"}, None)
    after = datetime.now(UTC)

    kwargs = mock_list.call_args.kwargs
    assert kwargs["limit"] == daily_cycle_handler.MAX_WINDOW_FINDINGS > 5
    window = timedelta(hours=daily_cycle_handler.FINDINGS_WINDOW_HOURS)
    assert before - window <= datetime.fromisoformat(kwargs["since"]) <= after - window


def test_a_topic_with_nothing_new_in_the_window_writes_no_article(s3_bucket):
    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=[]),
        patch("daily_cycle_handler.invoke_model_tracked") as mock_invoke,
        patch("daily_cycle_handler.put_article") as mock_put_article,
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result["status"] == "no_findings"
    mock_invoke.assert_not_called()
    mock_put_article.assert_not_called()


# --- the window starts at the topic's last article ------------------------------------------------

_RUN = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
_DAY_BEFORE = (_RUN - timedelta(hours=24)).isoformat()


def _recent_topic(hours_ago=2, **extra):
    stamp = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {**NON_FINANCIAL_TOPIC, "last_article_at": stamp, **extra}


def test_window_is_the_last_day_when_the_topic_has_no_previous_article():
    assert daily_cycle_handler._window_start({}, _RUN, force=False) == _DAY_BEFORE


def test_window_starts_at_the_last_article_when_that_is_within_the_day():
    last = _RUN - timedelta(hours=3)

    since = daily_cycle_handler._window_start({"last_article_at": last.isoformat()}, _RUN, force=False)

    assert since == last.isoformat()


def test_window_never_reaches_back_past_a_day_even_if_the_last_article_is_older():
    last = _RUN - timedelta(days=5)

    since = daily_cycle_handler._window_start({"last_article_at": last.isoformat()}, _RUN, force=False)

    assert since == _DAY_BEFORE


def test_force_ignores_the_last_article():
    last = _RUN - timedelta(hours=3)

    since = daily_cycle_handler._window_start({"last_article_at": last.isoformat()}, _RUN, force=True)

    assert since == _DAY_BEFORE


@pytest.mark.parametrize("bad", ["not a date", 12345, ""])
def test_an_unreadable_last_article_falls_back_to_the_last_day(bad):
    since = daily_cycle_handler._window_start({"last_article_at": bad}, _RUN, force=False)

    assert since == _DAY_BEFORE


def test_a_naive_last_article_timestamp_is_read_as_utc():
    last = (_RUN - timedelta(hours=2)).replace(tzinfo=None)

    since = daily_cycle_handler._window_start({"last_article_at": last.isoformat()}, _RUN, force=False)

    assert since == last.replace(tzinfo=UTC).isoformat()


def test_handler_passes_the_window_start_to_the_findings_query(s3_bucket):
    topic = _recent_topic()
    with (
        patch("daily_cycle_handler.get_topic", return_value=topic),
        patch("daily_cycle_handler.list_recent_findings", return_value=[]) as mock_list,
    ):
        daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert mock_list.call_args.kwargs["since"] == topic["last_article_at"]


def test_a_forced_run_reads_the_whole_window(s3_bucket):
    before = datetime.now(UTC)
    with (
        patch("daily_cycle_handler.get_topic", return_value=_recent_topic()),
        patch("daily_cycle_handler.list_recent_findings", return_value=[]) as mock_list,
    ):
        daily_cycle_handler.handler({"topic_id": "github-trending", "force": True}, None)

    since = datetime.fromisoformat(mock_list.call_args.kwargs["since"])
    assert since <= before - timedelta(hours=24) + timedelta(seconds=5)


def test_only_a_literal_true_forces_a_run(s3_bucket):
    topic = _recent_topic()
    with (
        patch("daily_cycle_handler.get_topic", return_value=topic),
        patch("daily_cycle_handler.list_recent_findings", return_value=[]) as mock_list,
    ):
        daily_cycle_handler.handler({"topic_id": "github-trending", "force": "true"}, None)

    assert mock_list.call_args.kwargs["since"] == topic["last_article_at"]


def _run_a_full_cycle(
    compliant=True,
    record_side_effect=None,
    findings=FINDINGS,
    lineage_calls=None,
    topic=NON_FINANCIAL_TOPIC,
):
    """Run a whole cycle with everything external mocked. `lineage_calls`, if given,
    collects the (args, kwargs) build_lineage was called with."""
    responses = ["Angle one\nAngle two\nAngle three", "# Draft body", "A Title"]

    def _build_lineage(*args, **kwargs):
        if lineage_calls is not None:
            lineage_calls.append((args, kwargs))
        return _DUMMY_LINEAGE

    with (
        patch("daily_cycle_handler.get_topic", return_value=topic),
        patch("daily_cycle_handler.list_recent_findings", return_value=findings),
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("m", None)),
        patch("daily_cycle_handler.build_lineage", side_effect=_build_lineage),
        patch(
            "daily_cycle_handler.invoke_model_tracked",
            side_effect=[_tracked_result(r) for r in responses],
        ),
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={"compliant": compliant, "reasons": [], "lineage_call": _DUMMY_LINEAGE_CALL},
        ),
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article"),
        patch("daily_cycle_handler.put_moderation_item"),
        patch("daily_cycle_handler.render_and_publish_article_page"),
        patch("daily_cycle_handler.generate_and_store_article_musing"),
        patch(
            "daily_cycle_handler.set_topic_last_article_at", side_effect=record_side_effect
        ) as mock_set,
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)
    return result, mock_set


@pytest.mark.parametrize("compliant,status", [(True, "published"), (False, "pending_moderation")])
def test_an_article_records_when_the_run_began_whether_published_or_moderated(
    s3_bucket, compliant, status
):
    before = datetime.now(UTC)
    result, mock_set = _run_a_full_cycle(compliant=compliant)
    after = datetime.now(UTC)

    assert result["status"] == status
    topic_id, stamp = mock_set.call_args.args
    assert topic_id == "github-trending"
    assert before <= datetime.fromisoformat(stamp) <= after


def _research_finding(captured_at, input_tokens=400, output_tokens=100):
    return {
        "captured_at": captured_at,
        "summary": "A research summary.",
        "source_refs": [],
        "research_call": {
            "model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "used_fallback": False,
        },
    }


def test_the_articles_lineage_carries_the_research_tally_of_its_whole_window(s3_bucket):
    window = [
        _research_finding("2026-09-21T03:00:00+00:00"),
        _research_finding("2026-09-21T02:00:00+00:00"),
    ]
    calls = []

    with patch("common.costing.get_model", return_value=None):
        result, _ = _run_a_full_cycle(findings=window, lineage_calls=calls)

    assert result["status"] == "published"
    ((_, kwargs),) = calls
    research = kwargs["research"]
    assert research["tracked_findings"] == 2 and research["untracked_findings"] == 0
    assert (research["input_tokens"], research["output_tokens"]) == (800, 200)
    assert research["cost_aud"] is not None  # priced from the built-in table: registry empty


def test_findings_written_before_research_tracking_are_counted_as_untracked(s3_bucket):
    calls = []

    _run_a_full_cycle(findings=FINDINGS, lineage_calls=calls)  # FINDINGS carry no research_call

    ((_, kwargs),) = calls
    assert kwargs["research"]["untracked_findings"] == len(FINDINGS)
    assert kwargs["research"]["tracked_findings"] == 0


def test_the_research_tally_covers_findings_the_crypto_goal_filter_sets_aside(s3_bucket):
    """A finding from an earlier UTC day is dropped from the article's content by the
    daily-goal filter, but its Bedrock call still cost money and is still counted."""
    today = datetime.now(UTC)
    window = [
        _research_finding(today.isoformat()),
        _research_finding((today - timedelta(days=1)).isoformat()),
    ]
    crypto_topic = {**NON_FINANCIAL_TOPIC, "adapter": "crypto_feed", "is_financial": True}
    calls = []

    with patch("common.costing.get_model", return_value=None):
        _run_a_full_cycle(findings=window, lineage_calls=calls, topic=crypto_topic)

    ((_, kwargs),) = calls
    assert kwargs["research"]["findings"] == 2 and kwargs["research"]["tracked_findings"] == 2


def test_failing_to_record_the_last_article_does_not_fail_the_run(s3_bucket):
    # An error here would make Step Functions retry and write a duplicate article.
    result, mock_set = _run_a_full_cycle(record_side_effect=RuntimeError("throttled"))

    assert result["status"] == "published"
    mock_set.assert_called_once()


def test_no_article_means_nothing_is_recorded(s3_bucket):
    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=[]),
        patch("daily_cycle_handler.set_topic_last_article_at") as mock_set,
    ):
        daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    mock_set.assert_not_called()


def test_format_findings_summaries_keeps_everything_when_it_fits():
    findings = [{"summary": "newest"}, {"summary": "older"}]

    assert daily_cycle_handler._format_findings_summaries(findings) == "- newest\n- older"


def test_format_findings_summaries_drops_the_oldest_when_over_budget(monkeypatch):
    monkeypatch.setattr(daily_cycle_handler, "SUMMARIES_MAX_CHARS", 30)
    findings = [{"summary": "n" * 10}, {"summary": "m" * 10}, {"summary": "o" * 10}]  # newest first

    block = daily_cycle_handler._format_findings_summaries(findings)

    assert block == f"- {'n' * 10}\n- {'m' * 10}"


def test_format_findings_summaries_always_keeps_the_newest_even_if_it_alone_is_over_budget(monkeypatch):
    monkeypatch.setattr(daily_cycle_handler, "SUMMARIES_MAX_CHARS", 5)

    block = daily_cycle_handler._format_findings_summaries([{"summary": "x" * 50}, {"summary": "y"}])

    assert block == f"- {'x' * 50}"


def test_handler_publishes_when_compliant(s3_bucket):
    ideation_response = "Angle one about repo X\nAngle two about repo Y\nAngle three misc"
    invoke_responses = [ideation_response, "# Draft body\n\nSome article content.", "A Great Title"]

    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
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
    # The reviewer is shown the research the draft was written from, so a figure taken
    # straight from a finding is not mistaken for an invented one.
    source = mock_review.call_args.kwargs["source_material"]
    assert "Repo X jumped to #1 trending after a viral launch post." in source
    assert "Repo Y gained stars following a conference talk." in source

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
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
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
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
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
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
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
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
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


# --- Phase 5: prompt-refinement guidance / few-shot splicing --------------------------------------


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
            "daily_cycle_handler.list_prompt_refinements",
            return_value=[refinement],
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


def _run_with_gear(s3_bucket, gear):
    """Run one daily cycle for github-trending with `gear` as the approved refinements; returns
    (the ideation prompt, the draft prompt, put_article's keyword arguments)."""
    invoke_responses = ["Angle one\nAngle two\nAngle three", "Draft body text.", "Some Title"]
    with (
        patch("daily_cycle_handler.get_topic", return_value=NON_FINANCIAL_TOPIC),
        patch("daily_cycle_handler.list_recent_findings", return_value=FINDINGS),
        patch("daily_cycle_handler.list_prompt_refinements", return_value=gear),
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
        patch("daily_cycle_handler.put_article") as mock_put_article,
        patch("daily_cycle_handler.put_moderation_item"),
        patch("daily_cycle_handler.render_and_publish_article_page"),
        patch("daily_cycle_handler.generate_and_store_article_musing"),
    ):
        daily_cycle_handler.handler({"topic_id": "github-trending"}, None)
    calls = mock_invoke.call_args_list
    return calls[0].args[0], calls[1].args[0], mock_put_article.call_args.kwargs


def _gear(topic, version, slot, scope, text):
    return {
        "topic_id": topic,
        "version": version,
        "prompt_changes": text,
        "status": "approved",
        "equipped": True,
        "slot": slot,
        "scope": scope,
        "equipped_at": version,
    }


def test_worn_armor_and_the_topics_rings_are_injected_and_recorded_on_the_article(s3_bucket):
    gear = [
        _gear("security-hacker-news", "2026-09-01", "helmet", "global", "Keep it short."),
        _gear("github-trending", "2026-09-02", "ring", "topic", "Name the repository."),
        _gear("crypto", "2026-09-03", "ring", "topic", "Quote the price."),  # another topic's ring
        {**_gear("github-trending", "2026-09-04", "ring", "topic", "Benched."), "equipped": False},
    ]

    ideation_prompt, draft_prompt, article = _run_with_gear(s3_bucket, gear)

    for prompt in (ideation_prompt, draft_prompt):
        header = "Additional guidance based on reader feedback:"
        assert header + "\n- Keep it short.\n- Name the repository." in prompt
        assert "Quote the price" not in prompt and "Benched" not in prompt
    assert article["equipment_used"] == [
        {"topic_id": "security-hacker-news", "version": "2026-09-01", "slot": "helmet"},
        {"topic_id": "github-trending", "version": "2026-09-02", "slot": "ring"},
    ]


def test_an_article_written_with_no_gear_records_an_empty_list(s3_bucket):
    ideation_prompt, draft_prompt, article = _run_with_gear(s3_bucket, [])

    assert "reader feedback" not in ideation_prompt and "reader feedback" not in draft_prompt
    assert article["equipment_used"] == []  # "wore nothing", not "written before gear existed"


def test_a_legacy_approval_is_still_injected_and_recorded_as_legacy(s3_bucket):
    legacy = {
        "topic_id": "github-trending",
        "version": "2026-08-01",
        "prompt_changes": "Old guidance.",
        "status": "approved",
    }

    ideation_prompt, _, article = _run_with_gear(s3_bucket, [legacy])

    assert "Additional guidance based on reader feedback:\nOld guidance." in ideation_prompt
    assert article["equipment_used"] == [
        {"topic_id": "github-trending", "version": "2026-08-01", "slot": "legacy"}
    ]


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
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
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
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
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


# --- editorial goals (crypto feed) ----------------------------------------------------------------


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
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
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


# --- topic relevance guardrails -------------------------------------------------------------------

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


# --- hierarchical editorial goals (standing objective) --------------------------------------------

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


@pytest.mark.parametrize(
    "daily_goal", ["ALTCOIN_DEEP_DIVE", "WEB_AGGREGATOR", "TREND_INVENTOR"]
)
def test_a_crypto_topic_with_no_goal_of_its_own_uses_the_crypto_adapter_default(
    s3_bucket, daily_goal
):
    topic = {**FINANCIAL_TOPIC, "adapter_config": {"editorial_goal": daily_goal}}
    findings = [_finding_at(datetime.now(UTC).isoformat(), "Anchors diverge", "https://example/a")]

    _, mock_invoke, _ = _run_crypto(topic, findings)

    for call in mock_invoke.call_args_list[:2]:
        assert "Adapter-Specific Standard Goal: Prioritize structural changes in asset cap" in (
            call.args[0]
        )


def test_on_a_market_news_day_the_crypto_standing_goal_is_skipped(s3_bucket):
    topic = {**FINANCIAL_TOPIC, "adapter_config": {"editorial_goal": "MARKET_NEWS"}}
    findings = [_finding_at(datetime.now(UTC).isoformat(), "Stocks rally", "https://example/s")]

    _, mock_invoke, _ = _run_crypto(topic, findings)

    for call in mock_invoke.call_args_list[:2]:
        prompt = call.args[0]
        assert "Asset cap" not in prompt and "asset cap distributions" not in prompt
        assert "Global Default Goal: Execute independent web research" in prompt
    ideation_prompt = mock_invoke.call_args_list[0].args[0]
    assert f"Editorial Mandate: {EDITORIAL_MANDATES[EditorialGoal.MARKET_NEWS]}" in ideation_prompt
    assert ARTICLE_STYLES[EditorialGoal.MARKET_NEWS] in mock_invoke.call_args_list[1].args[0]
    # still a financial topic: the mandatory guidance is unaffected
    assert "Financial-topic guidance (mandatory):" in ideation_prompt


def test_a_topic_specific_focus_still_wins_on_a_market_news_day(s3_bucket):
    topic = {
        **FINANCIAL_TOPIC,
        "adapter_config": {"editorial_goal": "MARKET_NEWS"},
        "editorial_goals": {"primary_focus": "Track institutional flows."},
    }
    findings = [_finding_at(datetime.now(UTC).isoformat(), "Stocks rally", "https://example/s")]

    _, mock_invoke, _ = _run_crypto(topic, findings)

    assert "Topic-Specific Focus: Track institutional flows." in mock_invoke.call_args_list[0].args[0]


# --- the fresh-data review (shadow mode) ----------------------------------------------------------


def _review_record(outcome="clean", claims=(), status="reviewed", **extra):
    """What common.fresh_review.run_review returns (its lineage call still attached)."""
    return {
        "status": status,
        "outcome": outcome,
        "claims": list(claims),
        "evidence_as_of": "2026-09-21T09:00:00+00:00",
        "mode": "shadow",
        "lineage_call": {
            "stage": "adversarial_review",
            "model_id": "anthropic.claude-test-model",
            "input_tokens": 900,
            "output_tokens": 60,
            "used_fallback": False,
        },
        **extra,
    }


_MAJOR = {"claim": "Repo X is #1", "problem": "stale", "evidence": "now #4", "severity": "major"}
_MINOR = {"claim": "Repo X has 5,000 stars", "problem": "stale", "evidence": "4,000", "severity": "minor"}

_REVISION_CALL = {
    "stage": "revision",
    "model_id": "anthropic.claude-test-model",
    "input_tokens": 1500,
    "output_tokens": 900,
    "used_fallback": False,
    "stop_reason": "end_turn",
}


def _revised(title="Corrected Title", body="# Corrected body"):
    """What run_revision returns when the correction passes every guard."""
    return {"status": "revised", "title": title, "body": body, "lineage_call": dict(_REVISION_CALL)}


def _rejected(reason="introduces figure(s) found in none of the sources: 9"):
    return {
        "status": "rejected",
        "reason": reason,
        "violations": [reason],
        "lineage_call": dict(_REVISION_CALL),
    }


def _run_with_review(
    *,
    config=None,
    config_error=None,
    record=None,
    review_error=None,
    compliant=True,
    topic=NON_FINANCIAL_TOPIC,
    findings=FINDINGS,
    draft_stop_reason="end_turn",
    draft_attempts=1,
    model_calls=None,
    revision=None,
    revision_error=None,
    mocks=None,
):
    """A whole cycle with the review turned on (unless `config` says otherwise) and the
    reviewer itself mocked. Returns the result and everything the review touched.

    `draft_stop_reason` is why the draft call stopped ("max_tokens" = cut off);
    `model_calls`, if given, collects the keyword arguments of each drafting call.
    `revision` is what the (mocked) revision pass returns; `mocks`, if given, is filled
    with the revision and render mocks so a test can inspect them."""
    responses = [
        _tracked_result("Angle one\nAngle two\nAngle three"),
        _tracked_result("# Draft body", stop_reason=draft_stop_reason, attempts=draft_attempts),
        _tracked_result("A Title"),
    ]
    lineage_calls = []

    def model(prompt, model_id, **kwargs):
        if model_calls is not None:
            model_calls.append(kwargs)
        return responses.pop(0)

    def build_lineage(calls, **kwargs):
        lineage_calls.append(calls)
        return _DUMMY_LINEAGE

    config_patch = (
        patch("daily_cycle_handler.get_pipeline_config", side_effect=config_error)
        if config_error
        else patch("daily_cycle_handler.get_pipeline_config", return_value=config or {})
    )
    with (
        patch("daily_cycle_handler.get_topic", return_value=topic),
        patch("daily_cycle_handler.list_recent_findings", return_value=findings),
        patch("daily_cycle_handler.list_prompt_refinements", return_value=[]),
        patch("daily_cycle_handler.get_top_voted_articles", return_value=[]),
        patch("daily_cycle_handler.resolve_model", return_value=("m", "fallback-m")),
        patch("daily_cycle_handler.build_lineage", side_effect=build_lineage),
        patch("daily_cycle_handler.invoke_model_tracked", side_effect=model),
        patch(
            "daily_cycle_handler.compliance.review_draft",
            return_value={
                "compliant": compliant,
                "reasons": [] if compliant else ["needs a look"],
                "lineage_call": _DUMMY_LINEAGE_CALL,
            },
        ),
        patch("daily_cycle_handler.put_candidate_idea", wraps=_fake_put_candidate_idea),
        patch("daily_cycle_handler.put_article") as mock_put_article,
        patch("daily_cycle_handler.put_moderation_item") as mock_put_moderation,
        patch("daily_cycle_handler.render_and_publish_article_page") as mock_render,
        patch("daily_cycle_handler.generate_and_store_article_musing"),
        patch("daily_cycle_handler.set_topic_last_article_at"),
        config_patch,
        patch(
            "daily_cycle_handler.fresh_review.run_review",
            side_effect=review_error,
            return_value=record if record is not None else _review_record(),
        ) as mock_run,
        patch(
            "daily_cycle_handler.fresh_review.run_revision",
            side_effect=revision_error,
            return_value=revision if revision is not None else _revised(),
        ) as mock_revision,
    ):
        result = daily_cycle_handler.handler({"topic_id": topic["topic_id"]}, None)
    if mocks is not None:
        mocks.update(revision=mock_revision, render=mock_render)
    return result, mock_run, mock_put_article, mock_put_moderation, lineage_calls


def test_the_review_runs_by_default_in_shadow_mode(s3_bucket):
    _, mock_run, _, _, _ = _run_with_review(config={})

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs["mode"] == "shadow"
    assert kwargs["topic"] == NON_FINANCIAL_TOPIC
    assert kwargs["draft"] == "# Draft body"
    assert "Repo X jumped to #1" in kwargs["findings_text"]  # the window's summaries
    assert (kwargs["model_id"], kwargs["fallback_model_id"]) == ("m", "fallback-m")


def test_the_review_can_be_switched_off_from_the_pipeline_config(s3_bucket):
    _, mock_run, mock_put_article, _, lineage_calls = _run_with_review(config={"review_mode": "off"})

    mock_run.assert_not_called()
    assert "review" not in mock_put_article.call_args.kwargs
    assert all(call["stage"] != "adversarial_review" for call in lineage_calls[0])


def test_the_record_is_stored_on_the_article_without_its_lineage_call(s3_bucket):
    record = _review_record("major", [_MAJOR])

    _, _, mock_put_article, _, _ = _run_with_review(record=record)

    stored = mock_put_article.call_args.kwargs["review"]
    assert stored["outcome"] == "major" and stored["claims"] == [_MAJOR] and stored["status"] == "reviewed"
    assert "lineage_call" not in stored  # that goes into the article's lineage instead


def test_the_reviewers_call_becomes_a_lineage_stage(s3_bucket):
    _, _, _, _, lineage_calls = _run_with_review()

    stages = [call["stage"] for call in lineage_calls[0]]
    assert "adversarial_review" in stages and "compliance_review" in stages
    assert stages.index("adversarial_review") < stages.index("compliance_review")


@pytest.mark.parametrize(
    "record",
    [
        _review_record("major", [_MAJOR]),
        _review_record("minor", [{**_MAJOR, "severity": "minor"}]),
        _review_record(status="unavailable", outcome=None, reason="source is down"),
    ],
)
def test_shadow_mode_never_changes_what_happens_to_an_article(s3_bucket, record):
    """The whole point of shadow mode: however bad the review, the outcome is exactly what
    it would have been without one."""
    published, _, _, _, _ = _run_with_review(record=record, compliant=True)
    held, _, _, _, _ = _run_with_review(record=record, compliant=False)

    assert published["status"] == "published" and published["compliant"] is True
    assert held["status"] == "pending_moderation"


def test_a_moderated_article_carries_the_review_notes_for_the_operator(s3_bucket):
    record = _review_record("major", [_MAJOR])

    _, _, _, mock_put_moderation, _ = _run_with_review(record=record, compliant=False)

    assert mock_put_moderation.call_args.kwargs["review_notes"] == [
        "fresh-data review: Repo X is #1 -- stale (major): now #4"
    ]
    assert mock_put_moderation.call_args.kwargs["reasons"] == ["needs a look"]  # unchanged


def test_an_unavailable_review_leaves_a_note_on_a_moderated_article(s3_bucket):
    record = _review_record(status="unavailable", outcome=None, reason="source is down")

    _, _, _, mock_put_moderation, _ = _run_with_review(record=record, compliant=False)

    assert mock_put_moderation.call_args.kwargs["review_notes"] == [
        "fresh-data review unavailable: source is down"
    ]


def test_a_clean_review_leaves_no_notes(s3_bucket):
    _, _, _, mock_put_moderation, _ = _run_with_review(record=_review_record(), compliant=False)

    assert "review_notes" not in mock_put_moderation.call_args.kwargs


def test_a_published_article_does_not_touch_the_moderation_queue(s3_bucket):
    _, _, _, mock_put_moderation, _ = _run_with_review(record=_review_record("major", [_MAJOR]))

    mock_put_moderation.assert_not_called()


def test_a_financial_topic_gets_its_notes_and_the_review_sees_the_draft_before_the_disclaimer(s3_bucket):
    record = _review_record("major", [_MAJOR])

    _, mock_run, _, mock_put_moderation, _ = _run_with_review(
        record=record, topic=FINANCIAL_TOPIC, compliant=False
    )

    assert "financial or investment advice" not in mock_run.call_args.kwargs["draft"]
    assert mock_put_moderation.call_args.kwargs["review_notes"]


def test_a_review_that_blows_up_never_fails_the_article(s3_bucket):
    result, _, mock_put_article, _, _ = _run_with_review(review_error=RuntimeError("boom"))

    assert result["status"] == "published"
    stored = mock_put_article.call_args.kwargs["review"]
    assert stored["status"] == "unavailable" and "boom" in stored["reason"]


def test_an_unreadable_pipeline_config_falls_back_to_the_default_mode(s3_bucket):
    result, mock_run, _, _, _ = _run_with_review(config_error=RuntimeError("throttled"))

    assert result["status"] == "published"
    assert mock_run.call_args.kwargs["mode"] == "shadow"


def test_an_invalid_stored_mode_is_ignored(s3_bucket):
    _, mock_run, _, _, _ = _run_with_review(config={"review_mode": "bogus"})

    assert mock_run.call_args.kwargs["mode"] == "shadow"


def test_a_skipped_review_is_recorded_but_says_nothing_to_the_operator(s3_bucket):
    record = _review_record(status="skipped", outcome=None, reason="nothing to review against")

    _, _, mock_put_article, mock_put_moderation, _ = _run_with_review(record=record, compliant=False)

    assert mock_put_article.call_args.kwargs["review"]["status"] == "skipped"
    assert "review_notes" not in mock_put_moderation.call_args.kwargs


# --- what the adapter is asked to re-check --------------------------------------------------------


def _findings_with_snapshots(*keys):
    return [{**FINDINGS[0], "captured_at": f"2026-09-21T0{i}:00:00+00:00", "raw_snapshot_s3_key": key}
            for i, key in enumerate(keys)]


def test_the_review_is_given_the_newest_findings_stored_snapshot(s3_bucket):
    snapshot = {"analyzed_today": ["alt-1"], "editorial_goal": "ALTCOIN_DEEP_DIVE"}
    s3_bucket.put_object(
        Bucket=ENV["CONTENT_BUCKET"], Key="snapshots/newest.json", Body=json.dumps(snapshot).encode()
    )
    s3_bucket.put_object(Bucket=ENV["CONTENT_BUCKET"], Key="snapshots/older.json", Body=b'{"old": true}')

    _, mock_run, _, _, _ = _run_with_review(
        findings=_findings_with_snapshots("snapshots/newest.json", "snapshots/older.json")
    )

    assert mock_run.call_args.kwargs["latest_state"] == snapshot


def test_a_snapshot_that_cannot_be_read_gives_the_review_no_state_rather_than_failing(s3_bucket, capsys):
    _, mock_run, _, _, _ = _run_with_review(findings=_findings_with_snapshots("snapshots/missing.json"))

    assert mock_run.call_args.kwargs["latest_state"] is None
    assert "could not load the latest snapshot" in capsys.readouterr().out


def test_findings_with_no_snapshot_key_give_no_state(s3_bucket):
    _, mock_run, _, _, _ = _run_with_review(findings=FINDINGS)  # these carry no raw_snapshot_s3_key

    assert mock_run.call_args.kwargs["latest_state"] is None


def test_the_snapshot_comes_from_the_whole_window_not_just_the_goals_findings(s3_bucket):
    """The crypto goal filter keeps only today's findings, but the newest finding in the
    window is what tells the adapter which coins to re-check."""
    today = datetime.now(UTC)
    newest = {**FINDINGS[0], "captured_at": today.isoformat(), "raw_snapshot_s3_key": "snapshots/today.json"}
    s3_bucket.put_object(Bucket=ENV["CONTENT_BUCKET"], Key="snapshots/today.json", Body=b'{"today": true}')
    crypto_topic = {**NON_FINANCIAL_TOPIC, "adapter": "crypto_feed", "is_financial": True}

    _, mock_run, _, _, _ = _run_with_review(findings=[newest], topic=crypto_topic)

    assert mock_run.call_args.kwargs["latest_state"] == {"today": True}


# --- a draft that ran out of tokens ---------------------------------------------------------------


def _draft_lineage_call(lineage_calls):
    return next(call for call in lineage_calls[0] if call["stage"] == "draft")


def test_the_draft_is_given_room_for_an_article_and_one_automatic_retry(s3_bucket):
    calls = []

    _run_with_review(model_calls=calls)

    ideate, draft, title = calls
    assert draft["max_tokens"] == daily_cycle_handler.DRAFT_MAX_TOKENS == 4096
    assert draft["retry_max_tokens"] == daily_cycle_handler.DRAFT_RETRY_MAX_TOKENS == 8192
    assert "max_tokens" not in ideate and "max_tokens" not in title  # short outputs keep the default


def test_a_complete_draft_is_published_as_before(s3_bucket):
    result, _, mock_put_article, mock_put_moderation, _ = _run_with_review(draft_stop_reason="end_turn")

    assert result["status"] == "published"
    assert mock_put_article.call_args.kwargs["status"] == "published"
    mock_put_moderation.assert_not_called()


def test_a_draft_cut_off_even_after_the_retry_is_never_published(s3_bucket):
    """Even when the compliance review is happy with what text there is."""
    result, _, mock_put_article, mock_put_moderation, _ = _run_with_review(
        draft_stop_reason="max_tokens", compliant=True
    )

    assert result["status"] == "pending_moderation" and result["compliant"] is False
    assert result["reasons"] == [daily_cycle_handler.TRUNCATED_DRAFT_REASON]
    assert mock_put_article.call_args.kwargs["status"] == "pending_moderation"
    assert mock_put_article.call_args.kwargs["published_at"] is None
    assert mock_put_article.call_args.kwargs["published_by"] is None
    assert mock_put_moderation.call_args.kwargs["reasons"] == [daily_cycle_handler.TRUNCATED_DRAFT_REASON]


def test_the_truncated_reason_comes_first_ahead_of_the_compliance_reasons(s3_bucket):
    result, _, _, mock_put_moderation, _ = _run_with_review(draft_stop_reason="max_tokens", compliant=False)

    expected = [daily_cycle_handler.TRUNCATED_DRAFT_REASON, "needs a look"]
    assert result["reasons"] == expected
    assert mock_put_moderation.call_args.kwargs["reasons"] == expected


def test_a_truncated_financial_draft_says_so_too(s3_bucket):
    result, _, _, mock_put_moderation, _ = _run_with_review(
        draft_stop_reason="max_tokens", topic=FINANCIAL_TOPIC, compliant=False
    )

    assert result["reasons"][0] == daily_cycle_handler.TRUNCATED_DRAFT_REASON
    assert mock_put_moderation.call_args.kwargs["reasons"][0] == daily_cycle_handler.TRUNCATED_DRAFT_REASON


def test_the_truncated_body_is_still_stored_so_a_person_can_see_what_there_is(s3_bucket):
    _, _, mock_put_article, _, _ = _run_with_review(draft_stop_reason="max_tokens")

    key = mock_put_article.call_args.kwargs["body_s3_key"]
    body = s3_bucket.get_object(Bucket=ENV["CONTENT_BUCKET"], Key=key)["Body"].read().decode("utf-8")
    assert body.startswith("# Draft body")


def test_the_drafts_stop_reason_is_recorded_in_its_lineage(s3_bucket):
    _, _, _, _, lineage_calls = _run_with_review(draft_stop_reason="end_turn")

    call = _draft_lineage_call(lineage_calls)
    assert call["stop_reason"] == "end_turn" and "attempts" not in call


def test_a_draft_that_needed_the_retry_but_finished_is_published_and_the_retry_is_recorded(s3_bucket):
    result, _, _, _, lineage_calls = _run_with_review(draft_stop_reason="end_turn", draft_attempts=2)

    assert result["status"] == "published"
    assert _draft_lineage_call(lineage_calls)["attempts"] == 2


def test_a_truncated_drafts_lineage_says_so(s3_bucket):
    _, _, _, _, lineage_calls = _run_with_review(draft_stop_reason="max_tokens", draft_attempts=2)

    call = _draft_lineage_call(lineage_calls)
    assert call["stop_reason"] == "max_tokens" and call["attempts"] == 2


# --- enforce mode ---------------------------------------------------------------------------------


def _enforced(outcome="clean", claims=(), status="reviewed", **extra):
    """A review record from a run in enforce mode (evidence attached, as run_review returns it)."""
    return _review_record(outcome, claims, status=status, mode="enforce", evidence="FRESH EVIDENCE", **extra)


ENFORCE = {"review_mode": "enforce"}


def _stored_review(mock_put_article):
    return mock_put_article.call_args.kwargs["review"]


def test_a_clean_enforced_review_publishes_untouched_and_says_it_was_checked(s3_bucket):
    mocks = {}

    result, _, mock_put_article, mock_put_moderation, _ = _run_with_review(
        config=ENFORCE, record=_enforced("clean"), mocks=mocks
    )

    assert result["status"] == "published"
    mocks["revision"].assert_not_called()
    assert mocks["render"].call_args.kwargs["fact_check"] == "Checked against current data: no problems found"
    assert mocks["render"].call_args.kwargs["title"] == "A Title"
    assert "body_original_s3_key" not in mock_put_article.call_args.kwargs
    mock_put_moderation.assert_not_called()


def test_the_review_is_given_the_articles_title(s3_bucket):
    _, mock_run, _, _, _ = _run_with_review(config=ENFORCE, record=_enforced("clean"))

    assert mock_run.call_args.kwargs["title"] == "A Title"


def test_minor_problems_are_corrected_and_the_corrected_article_is_what_publishes(s3_bucket):
    mocks = {}
    record = _enforced("minor", [_MINOR])

    result, _, mock_put_article, _, _ = _run_with_review(
        config=ENFORCE, record=record, revision=_revised("Corrected Title", "# Corrected body"), mocks=mocks
    )

    assert result["status"] == "published"
    kwargs = mock_put_article.call_args.kwargs
    assert kwargs["title"] == "Corrected Title" and kwargs["status"] == "published"
    stored = s3_bucket.get_object(Bucket=ENV["CONTENT_BUCKET"], Key=kwargs["body_s3_key"])["Body"].read()
    assert stored.decode("utf-8") == "# Corrected body"
    assert mocks["render"].call_args.kwargs["body_markdown"] == "# Corrected body"
    assert mocks["render"].call_args.kwargs["title"] == "Corrected Title"
    assert (
        mocks["render"].call_args.kwargs["fact_check"]
        == "Checked against current data: corrected before publishing"
    )


def test_the_revision_is_given_everything_it_needs(s3_bucket):
    mocks = {}

    _run_with_review(config=ENFORCE, record=_enforced("minor", [_MINOR]), mocks=mocks)

    kwargs = mocks["revision"].call_args.kwargs
    assert kwargs["title"] == "A Title" and kwargs["body"] == "# Draft body"
    assert kwargs["claims"] == [_MINOR] and kwargs["evidence"] == "FRESH EVIDENCE"
    assert "Repo X jumped to #1" in kwargs["findings_text"]
    assert (kwargs["model_id"], kwargs["fallback_model_id"]) == ("m", "fallback-m")


def test_the_original_draft_is_kept_beside_a_corrected_one(s3_bucket):
    _, _, mock_put_article, _, _ = _run_with_review(
        config=ENFORCE, record=_enforced("minor", [_MINOR]), revision=_revised(body="# Corrected body")
    )

    key = mock_put_article.call_args.kwargs["body_original_s3_key"]
    assert key.endswith(".original.md")
    original = s3_bucket.get_object(Bucket=ENV["CONTENT_BUCKET"], Key=key)["Body"].read().decode("utf-8")
    assert original == "# Draft body"


def test_a_corrected_articles_review_says_so_and_keeps_the_old_title(s3_bucket):
    _, _, mock_put_article, _, _ = _run_with_review(
        config=ENFORCE, record=_enforced("minor", [_MINOR]), revision=_revised("Corrected Title")
    )

    review = _stored_review(mock_put_article)
    assert review["revised"] is True and review["original_title"] == "A Title"
    assert "held" not in review and "evidence" not in review and "lineage_call" not in review


def test_a_correction_that_leaves_the_title_alone_records_no_original_title(s3_bucket):
    _, _, mock_put_article, _, _ = _run_with_review(
        config=ENFORCE, record=_enforced("minor", [_MINOR]), revision=_revised("A Title")
    )

    assert "original_title" not in _stored_review(mock_put_article)


def test_the_revision_is_a_lineage_stage_between_the_review_and_compliance(s3_bucket):
    _, _, _, _, lineage_calls = _run_with_review(config=ENFORCE, record=_enforced("minor", [_MINOR]))

    stages = [call["stage"] for call in lineage_calls[0]]
    assert stages.index("adversarial_review") < stages.index("revision") < stages.index("compliance_review")


def test_a_correction_that_cannot_be_trusted_holds_the_article_with_the_original_text(s3_bucket):
    result, _, mock_put_article, mock_put_moderation, lineage_calls = _run_with_review(
        config=ENFORCE,
        record=_enforced("minor", [_MINOR]),
        revision=_rejected("introduces figure(s) found in none of the sources: 9"),
    )

    assert result["status"] == "pending_moderation"
    reason = result["reasons"][0]
    assert "could not be trusted" in reason and "introduces figure(s)" in reason
    kwargs = mock_put_article.call_args.kwargs
    assert kwargs["status"] == "pending_moderation" and kwargs["title"] == "A Title"
    body = s3_bucket.get_object(Bucket=ENV["CONTENT_BUCKET"], Key=kwargs["body_s3_key"])["Body"].read()
    assert body.decode("utf-8") == "# Draft body"  # the draft as written, not the rejected rewrite
    assert "body_original_s3_key" not in kwargs
    review = _stored_review(mock_put_article)
    assert review["held"] is True and "introduces figure(s)" in review["revision_rejected"]
    assert "revised" not in review
    assert any(call["stage"] == "revision" for call in lineage_calls[0])  # the spend is still recorded
    notes = mock_put_moderation.call_args.kwargs["review_notes"]
    assert any("correction was rejected" in note for note in notes)


def test_a_revision_call_that_failed_outright_holds_the_article_and_records_no_cost(s3_bucket):
    failed = {"status": "failed", "reason": "the revision model call failed: throttled", "lineage_call": None}

    result, _, _, _, lineage_calls = _run_with_review(
        config=ENFORCE, record=_enforced("minor", [_MINOR]), revision=failed
    )

    assert result["status"] == "pending_moderation"
    assert all(call["stage"] != "revision" for call in lineage_calls[0])


def test_a_major_problem_holds_the_article_and_does_not_try_to_fix_it(s3_bucket):
    mocks = {}
    record = _enforced("major", [_MAJOR])

    result, _, mock_put_article, mock_put_moderation, _ = _run_with_review(
        config=ENFORCE, record=record, compliant=True, mocks=mocks
    )

    assert result["status"] == "pending_moderation" and result["compliant"] is False
    assert "1 major claim(s)" in result["reasons"][0]
    mocks["revision"].assert_not_called()
    mocks["render"].assert_not_called()  # never published
    assert mock_put_article.call_args.kwargs["published_at"] is None
    review = _stored_review(mock_put_article)
    assert review["held"] is True and review["hold_reasons"] == result["reasons"]
    assert mock_put_moderation.call_args.kwargs["review_notes"]  # the operator is told why


def test_the_hold_reason_is_listed_ahead_of_the_compliance_reasons(s3_bucket):
    result, _, _, _, _ = _run_with_review(
        config=ENFORCE, record=_enforced("major", [_MAJOR]), compliant=False
    )

    assert "major claim(s)" in result["reasons"][0] and result["reasons"][-1] == "needs a look"


def test_a_review_that_could_not_run_holds_the_article_by_default(s3_bucket):
    record = _enforced(status="unavailable", outcome=None, reason="could not fetch fresh data: down")

    result, _, mock_put_article, _, _ = _run_with_review(config=ENFORCE, record=record)

    assert result["status"] == "pending_moderation"
    assert "unavailable" in result["reasons"][0]
    assert _stored_review(mock_put_article)["held"] is True


def test_the_operator_can_choose_to_publish_and_note_an_unavailable_review_instead(s3_bucket):
    record = _enforced(status="unavailable", outcome=None, reason="down")
    mocks = {}

    result, _, mock_put_article, _, _ = _run_with_review(
        config={**ENFORCE, "review_on_unavailable": "note"}, record=record, mocks=mocks
    )

    assert result["status"] == "published"
    assert "held" not in _stored_review(mock_put_article)
    assert mocks["render"].call_args.kwargs["fact_check"] == (
        "Not checked against current data (the check was unavailable)"
    )


def test_an_invalid_stored_unavailable_action_holds_never_publishes(s3_bucket):
    record = _enforced(status="unavailable", outcome=None, reason="down")

    result, _, _, _, _ = _run_with_review(
        config={**ENFORCE, "review_on_unavailable": "publish"}, record=record
    )

    assert result["status"] == "pending_moderation"


def test_a_skipped_review_changes_nothing_and_claims_nothing(s3_bucket):
    record = _enforced(status="skipped", outcome=None, reason="nothing to review against")
    mocks = {}

    result, _, _, _, _ = _run_with_review(config=ENFORCE, record=record, mocks=mocks)

    assert result["status"] == "published"
    assert mocks["render"].call_args.kwargs["fact_check"] is None


def test_a_review_step_that_itself_fails_holds_the_article_in_enforce_mode(s3_bucket):
    result, _, mock_put_article, _, _ = _run_with_review(
        config=ENFORCE, review_error=RuntimeError("boom"), compliant=True
    )

    assert result["status"] == "pending_moderation"
    review = _stored_review(mock_put_article)
    assert review["status"] == "unavailable" and review["mode"] == "enforce" and review["held"] is True


def test_an_unexpected_error_while_enforcing_holds_the_article_rather_than_failing_or_publishing(s3_bucket):
    result, _, mock_put_article, _, _ = _run_with_review(
        config=ENFORCE, record=_enforced("minor", [_MINOR]), revision_error=RuntimeError("bug")
    )

    assert result["status"] == "pending_moderation"
    assert "could not be applied" in result["reasons"][0]
    assert _stored_review(mock_put_article)["held"] is True


def test_shadow_mode_never_revises_or_holds_however_bad_the_review(s3_bucket):
    mocks = {}

    result, _, mock_put_article, _, _ = _run_with_review(
        config={"review_mode": "shadow"}, record=_review_record("major", [_MAJOR]), mocks=mocks
    )

    assert result["status"] == "published"
    mocks["revision"].assert_not_called()
    assert "held" not in _stored_review(mock_put_article)


def test_a_topics_own_mode_overrides_the_pipeline_wide_one(s3_bucket):
    enforced_topic = {**NON_FINANCIAL_TOPIC, "review_mode": "enforce"}
    shadowed_topic = {**NON_FINANCIAL_TOPIC, "review_mode": "shadow"}

    held, _, _, _, _ = _run_with_review(
        config={"review_mode": "shadow"}, topic=enforced_topic, record=_enforced("major", [_MAJOR])
    )
    passed, mock_run, _, _, _ = _run_with_review(
        config=ENFORCE, topic=shadowed_topic, record=_review_record("major", [_MAJOR])
    )

    assert held["status"] == "pending_moderation"
    assert passed["status"] == "published" and mock_run.call_args.kwargs["mode"] == "shadow"


def test_a_topic_can_turn_the_review_off_while_the_pipeline_enforces(s3_bucket):
    topic = {**NON_FINANCIAL_TOPIC, "review_mode": "off"}

    result, mock_run, _, _, _ = _run_with_review(config=ENFORCE, topic=topic)

    assert result["status"] == "published"
    mock_run.assert_not_called()


def test_a_financial_topic_is_still_corrected_and_still_moderated(s3_bucket):
    mocks = {}

    result, _, mock_put_article, mock_put_moderation, _ = _run_with_review(
        config=ENFORCE,
        record=_enforced("minor", [_MINOR]),
        revision=_revised("Corrected Title", "# Corrected body"),
        topic=FINANCIAL_TOPIC,
        compliant=False,
        mocks=mocks,
    )

    assert result["status"] == "pending_moderation"  # financial articles always go to a person
    assert mock_put_article.call_args.kwargs["title"] == "Corrected Title"
    assert _stored_review(mock_put_article)["revised"] is True
    mocks["render"].assert_not_called()
    assert any("corrected automatically" in n for n in mock_put_moderation.call_args.kwargs["review_notes"])


def test_a_truncated_draft_is_reported_before_the_reviews_reasons(s3_bucket):
    result, _, _, _, _ = _run_with_review(
        config=ENFORCE, record=_enforced("major", [_MAJOR]), draft_stop_reason="max_tokens"
    )

    assert result["reasons"][0] == daily_cycle_handler.TRUNCATED_DRAFT_REASON
    assert "major claim(s)" in result["reasons"][1]


def test_an_article_a_person_later_approves_can_still_say_it_was_held_for_review(s3_bucket):
    _, _, mock_put_article, _, _ = _run_with_review(config=ENFORCE, record=_enforced("major", [_MAJOR]))

    from common.fact_check import fact_check_label

    review = _stored_review(mock_put_article)
    assert fact_check_label(review, "humans") == (
        "Checked against current data: reviewed by a person before publishing"
    )
