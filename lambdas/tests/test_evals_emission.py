"""Continuous evals' emission points (common/evals_emission.py; docs/enhancements/
model-ranch-prompt-assets-evals.md, sections 4.3 and 5.4, PR14), and the daily cycle's: an article
stored earns an Article Completion per prompt of its lineage.

What is held: an article's prompts are every prompt id its lineage names, once each, at the most
recent call's version; its model is the one that wrote its text as it stands, the same on every
event; each point writes exactly section 4.3's fields and the event ids the rollup deduplicates and
joins by; nothing is read or written while evals are off; an event the schema refuses is skipped
and the rest written; a Logs that fails stops the rest and never raises; and a stored article, held
or published, emits once per prompt while the run's result stays what it was.

The admin API's points are in test_evals_emission_admin.py, the public API's in
test_evals_emission_public.py. `events` here is their fixture too.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
import test_evals_rollup as rollup_tests
from botocore.exceptions import ClientError
from moto import mock_aws
from test_evals_rollup import FakeBedrock, FakeLogs, plain, row

import daily_cycle_handler
from common import evals, evals_emission, evals_rollup

REGION = "ap-southeast-2"
GROUP = "acme-dev-evals"
# Not real models' names: the schema only cares about the shape.
ALPHA = "maker.alpha-v1:0"
BETA = "maker.beta-v1:0"
GAMMA = "maker.gamma-v1:0"
ALPHA_ARN = f"arn:aws:bedrock:{REGION}:123456789012:inference-profile/{ALPHA}"
TOPIC_ID = "github-trending"
ARTICLE_ID = "a1b2c3"

# The rollup tests' moto tables (evals, models with ALPHA, model config, topics), under their own name.
aws = rollup_tests.aws


@pytest.fixture
def events(monkeypatch):
    """Evals on, and every event Logs is handed, as the object its line holds, in the order written."""
    monkeypatch.setenv("EVALS_ENABLED", "true")
    monkeypatch.setenv("EVALS_LOG_GROUP", GROUP)
    written: list[dict] = []
    monkeypatch.setattr(evals, "_put", lambda messages: written.extend(json.loads(m) for m in messages))
    return written


@pytest.fixture
def evals_off(monkeypatch):
    """Evals off, and any attempt to build a Logs client or write fails the test."""
    monkeypatch.setenv("EVALS_ENABLED", "false")
    monkeypatch.setenv("EVALS_LOG_GROUP", GROUP)

    def never(*_args, **_kwargs):
        raise AssertionError("nothing is written while evals are off")

    monkeypatch.setattr(evals, "_put", never)
    monkeypatch.setattr(evals, "_logs", never)


@pytest.fixture
def logs_down(monkeypatch):
    """Evals on, and Logs refusing every write; how many writes were tried."""
    monkeypatch.setenv("EVALS_ENABLED", "true")
    monkeypatch.setenv("EVALS_LOG_GROUP", GROUP)
    tried: list[list[str]] = []

    def refuse(messages):
        tried.append(messages)
        error = {"Error": {"Code": "ServiceUnavailableException", "Message": "down"}}
        raise ClientError(error, "PutLogEvents")

    monkeypatch.setattr(evals, "_put", refuse)
    return tried


def stamped(found: list[dict]) -> list[dict]:
    """The events without their timestamps, each checked to be a UTC time of a moment ago."""
    plain = []
    for event in found:
        event = dict(event)
        at = datetime.strptime(event.pop("timestamp"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert abs((datetime.now(UTC) - at).total_seconds()) < 60
        assert evals.parse(json.dumps({**event, "timestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ")})) is not None
        plain.append(event)
    return plain


def call(stage, model=ALPHA, prompt_id=None, version="v1", **extra):
    found = {"stage": stage, "model_id": model, "input_tokens": 10, "output_tokens": 5, **extra}
    if prompt_id:
        found.update(prompt_id=prompt_id, prompt_version=version)
    return found


def research(prompt_id, version, captured_at, model=GAMMA):
    return {**call("research", model, prompt_id, version), "captured_at": captured_at}


# A stored article's lineage: ideation, draft and title by ALPHA (the title fell back to BETA), the
# reviews, a call recorded before prompts were registered, and research summarised by GAMMA with
# two versions of one prompt across the window.
LINEAGE = {
    "calls": [
        call("ideation", prompt_id="daily_cycle/ideation", version="v3"),
        call("draft", prompt_id="daily_cycle/draft", version="v2"),
        call("title", BETA, "daily_cycle/title", "vanilla:v1", used_fallback=True),
        call("fresh_review", prompt_id="reviews/adversarial_review"),
        call("compliance_review", prompt_id="reviews/compliance", version="vanilla:v1"),
        call("legacy"),
    ],
    "research": {
        "calls": [
            research("research_tick/summary", "v1", "2026-10-30T01:00:00+00:00"),
            research("research_tick/summary", "v2", "2026-10-30T03:00:00+00:00"),
            research("crypto_feed/news", "v1", "2026-10-30T02:00:00+00:00"),
            {**call("research"), "captured_at": "2026-10-30T04:00:00+00:00"},
        ]
    },
}
PROMPTS = [
    ("daily_cycle/ideation", "v3"),
    ("daily_cycle/draft", "v2"),
    ("daily_cycle/title", "vanilla:v1"),
    ("reviews/adversarial_review", "v1"),
    ("reviews/compliance", "vanilla:v1"),
    ("research_tick/summary", "v2"),
    ("crypto_feed/news", "v1"),
]


# --- an article's prompts and its model -----------------------------------------------------------


def test_an_articles_prompts_are_every_prompt_its_lineage_names_once_at_the_most_recent_version():
    assert evals_emission.article_prompts(LINEAGE) == PROMPTS


def test_a_rewritten_articles_prompts_carry_the_versions_of_its_latest_reviews():
    lineage = {
        "calls": [
            *LINEAGE["calls"],
            call("rewrite", BETA, "rewrite/rewrite", "v1"),
            call("fresh_review", prompt_id="reviews/adversarial_review", version="v4"),
            call("compliance_review", prompt_id="reviews/compliance", version="vanilla:v2"),
        ]
    }

    found = dict(evals_emission.article_prompts(lineage))

    assert found["reviews/adversarial_review"] == "v4" and found["reviews/compliance"] == "vanilla:v2"
    assert found["rewrite/rewrite"] == "v1" and found["daily_cycle/draft"] == "v2"


@pytest.mark.parametrize("lineage", [None, {}, {"calls": None}, {"calls": "x", "research": []}, "lineage"])
def test_a_lineage_with_nothing_readable_names_no_prompt(lineage):
    assert evals_emission.article_prompts(lineage) == []


def test_the_writer_is_the_draft_calls_model_even_when_another_call_fell_back():
    model, found = evals_emission.writer({"lineage": LINEAGE})

    assert model == ALPHA and found is LINEAGE["calls"][1]


def test_the_writer_is_the_latest_finished_rewrites_model_with_its_own_call_not_a_failed_ones():
    finished = call("rewrite", BETA, "rewrite/rewrite", "v1")
    failed_later = call("rewrite", GAMMA, "rewrite/rewrite", "v2")
    article = {
        "lineage": {"calls": [*LINEAGE["calls"], finished, failed_later]},
        "rewrites": [{"number": 1, "model_id": BETA}],
    }

    model, found = evals_emission.writer(article)

    assert model == BETA and found is finished


def test_a_rewrite_whose_call_is_not_in_the_lineage_still_names_its_model_and_no_call():
    article = {"lineage": LINEAGE, "rewrites": [{"number": 1, "model_id": GAMMA}]}

    assert evals_emission.writer(article) == (GAMMA, None)


def test_the_writer_is_named_by_its_canonical_id():
    article = {"lineage": {"calls": [call("draft", ALPHA_ARN, "daily_cycle/draft")]}}

    assert evals_emission.writer(article)[0] == ALPHA


@pytest.mark.parametrize(
    "article",
    [None, {}, {"lineage": {"calls": [call("ideation", prompt_id="daily_cycle/ideation")]}}, {"rewrites": 1}],
)
def test_an_article_with_no_draft_and_no_finished_rewrite_has_no_writer(article):
    assert evals_emission.writer(article) == (None, None)


# --- each point's events --------------------------------------------------------------------------


def test_a_completion_is_one_event_per_prompt_with_exactly_section_4_3s_fields(events):
    written = evals_emission.completion(article_id=ARTICLE_ID, topic_id=TOPIC_ID, lineage=LINEAGE)

    assert written == len(PROMPTS) == len(events)
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"completion:{ARTICLE_ID}:{prompt_id}",
            "model_id": ALPHA,
            "prompt_id": prompt_id,
            "prompt_version": version,
            "performance": "good",
            "rating_delta": 2,
            "reason": "Article Completion",
            "subject": ARTICLE_ID,
            "topic_id": TOPIC_ID,
            "source": "daily_cycle",
        }
        for prompt_id, version in PROMPTS
    ]


@pytest.mark.parametrize("decision,delta,performance", [("approved", 2, "good"), ("rejected", -2, "bad")])
def test_an_admin_review_is_one_event_per_prompt_for_a_persons_decision(events, decision, delta, performance):
    article = {"article_id": ARTICLE_ID, "topic_id": TOPIC_ID, "lineage": LINEAGE}

    written = evals_emission.admin_review(
        article, article_id=ARTICLE_ID, queue_id="q-1", decision=decision, topic_id=TOPIC_ID
    )

    assert written == len(PROMPTS)
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"admin:q-1:{decision}:{prompt_id}",
            "model_id": ALPHA,
            "prompt_id": prompt_id,
            "prompt_version": version,
            "performance": performance,
            "rating_delta": delta,
            "reason": "Admin Review",
            "subject": ARTICLE_ID,
            "topic_id": TOPIC_ID,
            "source": "admin_api",
            "by": "human",
        }
        for prompt_id, version in PROMPTS
    ]


@pytest.mark.parametrize("decision", ["force_published", "unpublished", "sent_back", None])
def test_only_an_approve_or_a_reject_is_an_admin_review(events, decision):
    article = {"article_id": ARTICLE_ID, "lineage": LINEAGE}

    written = evals_emission.admin_review(
        article, article_id=ARTICLE_ID, queue_id="q", decision=decision, topic_id=TOPIC_ID
    )

    assert written == 0 and events == []


def test_a_rewrite_is_one_event_for_the_writer_naming_the_prompt_that_wrote_the_text(events):
    article = {"article_id": ARTICLE_ID, "topic_id": TOPIC_ID, "lineage": LINEAGE}

    written = evals_emission.article_rewrite(
        lambda: article, article_id=ARTICLE_ID, rewrite_id="rw-1", topic_id=TOPIC_ID
    )

    assert written == 1
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": "rewrite:rw-1",
            "model_id": ALPHA,
            "prompt_id": "daily_cycle/draft",
            "prompt_version": "v2",
            "performance": "bad",
            "rating_delta": -2,
            "reason": "Article Rewrite",
            "subject": ARTICLE_ID,
            "topic_id": TOPIC_ID,
            "source": "admin_api",
            "by": "human",
        }
    ]


def test_a_rewrite_of_text_whose_call_names_no_prompt_is_the_models_alone(events):
    article = {"lineage": {"calls": [call("draft")]}, "rewrites": []}

    evals_emission.article_rewrite(
        lambda: article, article_id=ARTICLE_ID, rewrite_id="rw-2", topic_id=TOPIC_ID
    )

    (event,) = stamped(events)
    assert "prompt_id" not in event and "prompt_version" not in event
    assert (event["model_id"], event["subject"], event["rating_delta"]) == (ALPHA, ARTICLE_ID, -2)


@pytest.mark.parametrize(
    "vote,kept,delta,performance",
    [
        ("up", False, 1, "good"),
        ("down", False, -1, "bad"),
        ("up", True, 3, "good"),
        ("down", True, -3, "bad"),
    ],
)
def test_a_vote_is_one_event_for_the_articles_model_and_no_prompt(events, vote, kept, delta, performance):
    article = {"article_id": ARTICLE_ID, "topic_id": TOPIC_ID, "lineage": LINEAGE}

    written = evals_emission.user_feedback(
        article, article_id=ARTICLE_ID, feedback_id="fb-1", vote=vote, comment_kept=kept
    )

    assert written == 1
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": "vote:fb-1",
            "model_id": ALPHA,
            "performance": performance,
            "rating_delta": delta,
            "reason": "User Feedback",
            "subject": ARTICLE_ID,
            "topic_id": TOPIC_ID,
            "source": "public_api",
        }
    ]


def test_a_vote_on_a_rewritten_article_is_the_rewriters():
    article = {"lineage": LINEAGE, "rewrites": [{"model_id": BETA}]}

    assert evals_emission.writer(article)[0] == BETA


def test_nothing_is_read_or_written_while_evals_are_off(evals_off):
    article = {"article_id": ARTICLE_ID, "topic_id": TOPIC_ID, "lineage": LINEAGE}

    def unread():
        raise AssertionError("the article is not read while evals are off")

    ids = {"article_id": ARTICLE_ID}
    assert evals_emission.completion(**ids, topic_id=TOPIC_ID, lineage=LINEAGE) == 0
    assert evals_emission.admin_review(article, **ids, queue_id="q", decision="approved", topic_id="t") == 0
    assert evals_emission.article_rewrite(unread, **ids, rewrite_id="rw", topic_id=TOPIC_ID) == 0
    assert evals_emission.user_feedback(article, **ids, feedback_id="f", vote="up", comment_kept=False) == 0


def test_an_event_the_schema_refuses_is_skipped_and_the_others_written(events, capsys):
    calls = [call("draft", prompt_id="daily_cycle/draft"), call("title", prompt_id="Not a prompt")]
    lineage = {"calls": calls}

    written = evals_emission.completion(article_id=ARTICLE_ID, topic_id=TOPIC_ID, lineage=lineage)

    assert written == 1 and [event["prompt_id"] for event in events] == ["daily_cycle/draft"]
    assert "evals_emission: Article Completion event refused (bad_prompt_id)" in capsys.readouterr().out


def test_the_first_event_logs_cannot_take_stops_the_rest_and_nothing_raises(logs_down, capsys):
    written = evals_emission.completion(article_id=ARTICLE_ID, topic_id=TOPIC_ID, lineage=LINEAGE)

    assert written == 0 and len(logs_down) == 1
    assert "evals: could not write an event: ClientError(" in capsys.readouterr().out


def test_an_article_that_cannot_be_read_costs_the_rewrite_route_nothing(events, capsys):
    def unreadable():
        raise RuntimeError("throttled\nsecond line")

    written = evals_emission.article_rewrite(unreadable, article_id=ARTICLE_ID, rewrite_id="rw", topic_id="t")

    assert written == 0 and events == []
    said = "could not emit rewrite_id=rw's rewrite event: RuntimeError('throttled\\nsecond line')"
    assert said in capsys.readouterr().out


def test_an_article_with_no_writer_emits_nothing_and_says_so(events, capsys):
    lineage = {"calls": [call("ideation", prompt_id="daily_cycle/ideation")]}

    assert evals_emission.completion(article_id=ARTICLE_ID, topic_id=TOPIC_ID, lineage=lineage) == 0
    assert events == []
    said = "no Article Completion event: the article names no writing model or no prompt"
    assert said in capsys.readouterr().out


# --- what the rollup reads -----------------------------------------------------------------------


def test_the_rollup_counts_the_events_as_written_and_joins_the_vote_to_the_articles_prompts(aws, events):
    """The emitted lines, read back by the rollup (common/evals_rollup.py): the article's completions
    count once for its model and once for each prompt version and cell, and the reader's vote, which
    names no prompt, reaches the cells of the prompts its subject's completions name and never a
    prompt's own row."""
    article = {"article_id": ARTICLE_ID, "topic_id": TOPIC_ID, "lineage": LINEAGE}
    evals_emission.completion(article_id=ARTICLE_ID, topic_id=TOPIC_ID, lineage=LINEAGE)
    evals_emission.user_feedback(
        article, article_id=ARTICLE_ID, feedback_id="fb-1", vote="up", comment_kept=True
    )
    logs = FakeLogs()
    for event in events:
        logs.put(evals.line(event), datetime.now(UTC))

    summary = evals_rollup.run(
        now=datetime.now(UTC) + timedelta(minutes=1), logs_client=lambda region: logs, bedrock=FakeBedrock()
    )

    assert (summary["events"], summary["unreadable"], summary["duplicates"]) == (len(PROMPTS) + 1, 0, 0)
    day = evals_rollup.local_day(datetime.now(UTC), rollup_tests.SYDNEY).isoformat()
    completion = {"perf": 2, "events": 1, "good": 1, "bad": 0}
    vote = {"perf": 3, "events": 1, "good": 1, "bad": 0}
    model = plain(row(aws, f"model#{ALPHA}", day))
    assert model["by_reason"] == {"Article Completion": completion, "User Feedback": vote}
    for prompt_id, version in PROMPTS:
        prompt = plain(row(aws, f"prompt#{prompt_id}#{version}", day))
        assert prompt["by_reason"] == {"Article Completion": completion}, prompt_id
        cell = plain(row(aws, f"cell#{ALPHA}#{prompt_id}#{version}", day))
        assert cell["by_reason"] == {"Article Completion": completion, "User Feedback": vote}, prompt_id


# --- the daily cycle ------------------------------------------------------------------------------

CONTENT_BUCKET = "acme-content-test"
TOPIC = {"topic_id": TOPIC_ID, "name": "GitHub Trending", "adapter": "github_trending", "adapter_config": {}}
FINDINGS = [
    {
        "topic_id": TOPIC_ID,
        "captured_at": "2026-10-30T03:00:00+00:00",
        "summary": "Repo X jumped to the top after a launch post.",
        "source_refs": [{"url": "https://github.com/example/x", "title": "example/x"}],
        "research_call": {
            "model_id": GAMMA, "input_tokens": 400, "output_tokens": 100, "used_fallback": False,
            "prompt_id": "research_tick/summary", "prompt_version": "v2",
        },
    },
    {
        "topic_id": TOPIC_ID,
        "captured_at": "2026-10-30T01:00:00+00:00",
        "summary": "Repo Y gained stars after a talk.",
        "source_refs": [{"url": "https://github.com/example/y", "title": "example/y"}],
        "research_call": {
            "model_id": GAMMA, "input_tokens": 400, "output_tokens": 100, "used_fallback": False,
            "prompt_id": "research_tick/summary", "prompt_version": "v1",
        },
    },
]  # fmt: skip
COMPLIANCE_CALL = call("compliance_review", prompt_id="reviews/compliance", version="vanilla:v1")
CYCLE_PROMPTS = [
    ("daily_cycle/ideation", "vanilla:v1"),
    ("daily_cycle/draft", "vanilla:v1"),
    ("daily_cycle/title", "vanilla:v1"),
    ("reviews/compliance", "vanilla:v1"),
    ("research_tick/summary", "v2"),
]


def _tracked(text, model=ALPHA, used_fallback=False):
    return {
        "text": text, "model_id": model, "input_tokens": 10, "output_tokens": 5,
        "used_fallback": used_fallback, "stop_reason": "end_turn", "attempts": 1,
    }  # fmt: skip


def _candidate(topic_id, created_at, angle, status="considered"):
    return {"topic_id": topic_id, "created_at": created_at, "angle": angle, "status": status}


@pytest.fixture
def cycle(monkeypatch):
    """The daily cycle with everything outside it faked but its lineage and its prompts, so the
    article's lineage is the one the run builds. Calls the handler; returns (result, put_article's
    keywords)."""
    for key, value in {
        "AWS_DEFAULT_REGION": REGION, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "CONTENT_BUCKET": CONTENT_BUCKET, "BEDROCK_MODEL_ID": ALPHA, "PROMPT_AS_ASSET_ENABLED": "false",
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)

    def run(*, compliant=True, title_model=ALPHA):
        replies = [
            _tracked("Angle one\nAngle two\nAngle three"),
            _tracked("# Draft body"),
            _tracked("A Title", title_model, used_fallback=title_model != ALPHA),
        ]
        stored = {}
        fakes = {
            "get_pipeline_config": {"return_value": {"review_mode": "off"}},
            "list_recent_article_titles": {"return_value": []},
            "get_last_armor_versions": {"return_value": []},
            "set_last_armor_versions": {},
            "get_model_rotation_candidates": {"return_value": []},
            "set_topic_model_id": {},
            "get_topic": {"return_value": TOPIC},
            "list_recent_findings": {"return_value": FINDINGS},
            "list_prompt_refinements": {"return_value": []},
            "get_top_voted_articles": {"return_value": []},
            "resolve_model": {"return_value": (ALPHA, BETA)},
            "invoke_model_tracked": {"side_effect": replies},
            "compliance.review_draft": {
                "return_value": {"compliant": compliant, "reasons": [], "lineage_call": dict(COMPLIANCE_CALL)}
            },
            "put_candidate_idea": {"side_effect": _candidate},
            "put_article": {"side_effect": lambda **kw: stored.update(kw)},
            "put_moderation_item": {},
            "render_and_publish_article_page": {},
            "generate_and_store_article_musing": {},
            "set_topic_last_article_at": {},
            "record_article_lineage": {},
            "refresh_articles_snapshot": {},
        }
        with ExitStack() as stack:
            stack.enter_context(mock_aws())
            for name, how in fakes.items():
                stack.enter_context(patch(f"daily_cycle_handler.{name}", **how))
            stack.enter_context(patch("common.costing.get_model", return_value=None))
            stack.enter_context(patch("common.costing.get_model_archive", return_value={}))
            boto3.client("s3", region_name=REGION).create_bucket(
                Bucket=CONTENT_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
            )
            result = daily_cycle_handler.handler({"topic_id": TOPIC_ID}, None)
        return result, stored

    return run


@pytest.mark.parametrize("compliant,status", [(True, "published"), (False, "pending_moderation")])
def test_a_stored_article_emits_one_completion_per_prompt_in_its_lineage(events, cycle, compliant, status):
    result, stored = cycle(compliant=compliant)

    assert result["status"] == status
    article_id = stored["article_id"]
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"completion:{article_id}:{prompt_id}",
            "model_id": ALPHA,
            "prompt_id": prompt_id,
            "prompt_version": version,
            "performance": "good",
            "rating_delta": 2,
            "reason": "Article Completion",
            "subject": article_id,
            "topic_id": TOPIC_ID,
            "source": "daily_cycle",
        }
        for prompt_id, version in CYCLE_PROMPTS
    ]
    # The prompts are the stored lineage's, the very lineage the article carries.
    assert evals_emission.article_prompts(stored["lineage"]) == CYCLE_PROMPTS


def test_when_the_title_fell_back_every_event_still_names_the_model_that_drafted_the_article(events, cycle):
    _, stored = cycle(title_model=BETA)

    titles = [call for call in stored["lineage"]["calls"] if call["stage"] == "title"]
    assert titles[0]["model_id"] == BETA and titles[0]["used_fallback"] is True
    assert {event["model_id"] for event in events} == {ALPHA}


def test_a_stored_article_emits_nothing_while_evals_are_off(evals_off, cycle):
    result, _ = cycle()

    assert result["status"] == "published"


def test_a_failing_emit_leaves_the_runs_result_unchanged(logs_down, cycle):
    failing, stored = cycle()
    failing_article = stored["article_id"]

    assert len(logs_down) == 1  # one try, then the rest are not attempted
    with patch.dict("os.environ", {"EVALS_ENABLED": "false"}):
        working, stored = cycle()
    assert {**failing, "article_id": None} == {**working, "article_id": None}
    assert failing["status"] == "published" and failing["article_id"] == failing_article
