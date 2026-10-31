"""The admin API's continuous-evals events (common/evals_emission.py; docs/enhancements/
model-ranch-prompt-assets-evals.md, section 4.3, PR14): a person's approve or reject is one Admin
Review per prompt of the article, and a person's rewrite is one Article Rewrite for the model that
wrote it.

What is held: exactly one event per prompt (one for a rewrite) with exact fields; Professor
Grizzlewick's approvals, rejections and rewrites, which take the same routes, are none; a rewrite
that cannot start is none; after a rewrite that failed, a decision and the next rewrite credit the
text the article still has, never the failed attempt's; nothing is read or written while evals are
off; and an emit that fails leaves every answer exactly as it was.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
import test_evals_emission as emission_tests
from test_admin_api_handler import (  # noqa: F401 - the fixtures, used by name
    REGION,
    _event,
    _put_moderation_item,
    _put_topic,
    aws_env,
    aws_resources,
)
from test_evals_emission import ALPHA, BETA, LINEAGE, PROMPTS, stamped

import admin_api_handler
from common import prompts, rewrite

pytestmark = pytest.mark.usefixtures("aws_resources")

# The emission tests' fixtures, under their own names: evals on with every event captured, evals
# off, and Logs refusing every write.
events = emission_tests.events
evals_off = emission_tests.evals_off
logs_down = emission_tests.logs_down

ARTICLE_ID = "article-1"
TOPIC_ID = "github-trending"


def _put_written_article(status="pending_moderation", **fields):
    boto3.resource("dynamodb", region_name=REGION).Table("Articles").put_item(
        Item={
            "article_id": ARTICLE_ID,
            "topic_id": TOPIC_ID,
            "title": "A Title",
            "body_s3_key": f"articles/{ARTICLE_ID}.md",
            "status": status,
            "created_at": "2026-10-30T00:00:00+00:00",
            "source_refs": [],
            "lineage": LINEAGE,
            **fields,
        }
    )


@pytest.fixture
def held():
    """A held article with LINEAGE, its pending queue item and its topic; a model to rewrite with."""
    _put_written_article()
    _put_moderation_item(queue_id="queue-1", article_id=ARTICLE_ID, reasons=["Fabricated claim: x"])
    _put_topic()
    boto3.resource("dynamodb", region_name=REGION).Table("Models").put_item(
        Item={"model_id": BETA, "display_name": "Beta", "provider": "maker", "enabled": True}
    )


def _decide(decision: str):
    with (
        patch("admin_api_handler.read_article_body", return_value="# Body"),
        patch("admin_api_handler.render_and_publish_article_page"),
        patch("admin_api_handler.generate_and_store_article_musing"),
        patch("admin_api_handler.generate_and_store_rejection_musing"),
    ):
        event = _event(f"POST /moderation-queue/{{queue_id}}/{decision}", path_params={"queue_id": "queue-1"})
        result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


def _by_reviewer(decision: str):
    with (
        patch("admin_api_handler.read_article_body", return_value="# Body"),
        patch("admin_api_handler.render_and_publish_article_page"),
        patch("admin_api_handler.generate_and_store_article_musing"),
        patch("admin_api_handler.generate_and_store_rejection_musing"),
    ):
        if decision == "approve":
            return admin_api_handler.reviewer_approve("queue-1")
        return admin_api_handler.reviewer_reject("queue-1")


def _rewrite(route: str, client=None):
    """A person's rewrite through `route`: the queue item's, or the article's."""
    client = client or MagicMock()
    if route == "queue":
        path = {"queue_id": "queue-1"}
        event = _event("POST /moderation-queue/{queue_id}/rewrite", path_params=path, body={"model_id": BETA})
    else:
        event = _event(
            "POST /articles/{article_id}/rewrite",
            path_params={"article_id": ARTICLE_ID},
            body={"instructions": "The second section is out of date.", "model_id": BETA},
        )
    with patch("admin_api_handler._get_lambda_client", return_value=client):
        result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


def _rewrite_id(queue_id: str) -> str:
    table = boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue")
    return table.get_item(Key={"queue_id": queue_id})["Item"]["rewrite_id"]


# --- approve and reject ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route,decision,delta,performance",
    [("approve", "approved", 2, "good"), ("reject", "rejected", -2, "bad")],
)
def test_a_persons_decision_is_one_admin_review_per_prompt_with_exact_fields(
    held, events, route, decision, delta, performance
):
    status, body = _decide(route)

    assert status == 200 and body == {decision: "queue-1", "article_id": ARTICLE_ID}
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"admin:queue-1:{decision}:{prompt_id}",
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


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_professor_grizzlewicks_decisions_are_no_admin_review(held, events, decision):
    status, _ = _by_reviewer(decision)

    assert status == 200
    assert events == []


def test_a_decision_that_is_refused_is_no_admin_review(held, events):
    assert _decide("approve")[0] == 200
    events.clear()

    assert _decide("reject")[0] == 409  # no longer pending
    assert events == []


@pytest.mark.parametrize("route", ["approve", "reject"])
def test_a_decision_writes_nothing_while_evals_are_off(held, evals_off, route):
    assert _decide(route)[0] == 200


@pytest.mark.parametrize("route", ["approve", "reject"])
def test_a_failing_emit_leaves_the_decisions_answer_unchanged(held, logs_down, route):
    status, body = _decide(route)

    assert len(logs_down) == 1
    decision = "approved" if route == "approve" else "rejected"
    assert (status, body) == (200, {decision: "queue-1", "article_id": ARTICLE_ID})


# --- rewrites -------------------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["queue", "article"])
def test_a_persons_rewrite_is_one_article_rewrite_for_the_model_that_wrote_it(held, events, route):
    status, body = _rewrite(route)

    assert status == 202
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"rewrite:{_rewrite_id(body['rewriting'])}",
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


def test_a_rewrite_of_a_rewritten_article_scores_the_model_that_rewrote_it(held, events):
    rewrite_call = {
        "stage": "rewrite", "model_id": BETA, "input_tokens": 9, "output_tokens": 9,
        "prompt_id": "rewrite/rewrite", "prompt_version": "v1",
    }  # fmt: skip
    lineage = {**LINEAGE, "calls": [*LINEAGE["calls"], rewrite_call]}
    _put_written_article(lineage=lineage, rewrites=[{"number": 1, "model_id": BETA}])

    assert _rewrite("queue")[0] == 202

    (event,) = stamped(events)
    assert (event["model_id"], event["prompt_id"], event["prompt_version"]) == (BETA, "rewrite/rewrite", "v1")


def test_professor_grizzlewicks_rewrite_is_no_article_rewrite(held, events):
    with patch("admin_api_handler._get_lambda_client", return_value=MagicMock()):
        status, _ = admin_api_handler.reviewer_rewrite(ARTICLE_ID, "Readers say the figures are wrong.")

    assert status == 202
    assert events == []


def test_a_rewrite_that_cannot_start_is_no_article_rewrite(held, events):
    client = MagicMock()
    client.invoke.side_effect = RuntimeError("throttled")

    assert _rewrite("queue", client)[0] == 502
    assert events == []


def test_a_rewrite_reads_nothing_and_writes_nothing_while_evals_are_off(held, evals_off):
    with patch("admin_api_handler.get_article", side_effect=AssertionError("not read")) as read:
        assert _rewrite("queue")[0] == 202
    read.assert_not_called()


@pytest.mark.parametrize("route", ["queue", "article"])
def test_a_failing_emit_leaves_the_rewrites_answer_unchanged(held, logs_down, route):
    status, body = _rewrite(route)

    assert len(logs_down) == 1
    assert status == 202
    assert body == {"rewriting": body["rewriting"], "article_id": ARTICLE_ID, "model_id": BETA}
    assert _rewrite_id(body["rewriting"])


# --- after a failed rewrite -----------------------------------------------------------------------

FINISHED_REWRITE = {
    "stage": "rewrite", "model_id": BETA, "input_tokens": 9, "output_tokens": 9,
    "prompt_id": "rewrite/rewrite", "prompt_version": "v2",
}  # fmt: skip
# What the article holds before a person sends it back to BETA and that rewrite fails: (the calls
# after LINEAGE's, its finished rewrites, the model its text is credited to, its prompts as Admin
# Review names them, and the prompt and version an Article Rewrite names).
BEFORE_THE_FAILED_REWRITE = {
    "only_the_draft": ([], [], ALPHA, PROMPTS, ("daily_cycle/draft", "v2")),
    "a_finished_rewrite_by_the_same_model": (
        [FINISHED_REWRITE],
        [{"number": 1, "model_id": BETA}],
        BETA,
        [*PROMPTS[:5], ("rewrite/rewrite", "v2"), *PROMPTS[5:]],
        ("rewrite/rewrite", "v2"),
    ),
}


def _fail_a_rewrite(events: list, before: str) -> None:
    """The article as `before` says, sent back to BETA by a person, and that rewrite drawn at
    rewrite/rewrite v3 and cut off (common/rewrite.py): its call is stored, marked, and the item is
    back in the inbox. The events of sending it back are cleared."""
    calls, rewrites, *_ = BEFORE_THE_FAILED_REWRITE[before]
    # A stored research block carries its cost, which recording the failed rewrite's call reads.
    lineage = {"calls": [*LINEAGE["calls"], *calls], "research": {**LINEAGE["research"], "cost_aud": None}}
    _put_written_article(lineage=lineage, rewrites=rewrites)
    assert _rewrite("queue")[0] == 202
    prompt = prompts.Prompt(text="Rewrite it.", prompt_id="rewrite/rewrite", version="v3", source="asset")
    cut_off = {
        "text": '{"title": "A Ti', "model_id": BETA, "input_tokens": 900, "output_tokens": 4000,
        "used_fallback": False, "stop_reason": "max_tokens",
    }  # fmt: skip
    with (
        patch("common.rewrite.read_article_body", return_value="# Body"),
        patch("common.rewrite._evidence", return_value=""),
        patch("common.rewrite.rewrite_prompt", return_value=prompt),
        patch("common.rewrite.invoke_model_tracked", return_value=cut_off),
        patch("common.rewrite.record_article_lineage"),
    ):
        result = rewrite.run_rewrite("queue-1", _rewrite_id("queue-1"))
    assert result["status"] == "failed" and result["released"]
    stored = boto3.resource("dynamodb", region_name=REGION).Table("Articles").get_item(
        Key={"article_id": ARTICLE_ID}
    )["Item"]
    assert stored["lineage"]["calls"][-1]["prompt_version"] == "v3"
    events.clear()


@pytest.mark.parametrize("before", BEFORE_THE_FAILED_REWRITE)
@pytest.mark.parametrize(
    "route,decision,delta,performance",
    [("approve", "approved", 2, "good"), ("reject", "rejected", -2, "bad")],
)
def test_a_decision_after_a_failed_rewrite_credits_the_text_the_article_still_has(
    held, events, before, route, decision, delta, performance
):
    _fail_a_rewrite(events, before)
    _, _, model, prompts_named, _ = BEFORE_THE_FAILED_REWRITE[before]

    status, body = _decide(route)

    assert status == 200 and body == {decision: "queue-1", "article_id": ARTICLE_ID}
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"admin:queue-1:{decision}:{prompt_id}",
            "model_id": model,
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
        for prompt_id, version in prompts_named
    ]


@pytest.mark.parametrize("before", BEFORE_THE_FAILED_REWRITE)
def test_sending_it_back_again_after_a_failed_rewrite_scores_the_text_it_still_has(held, events, before):
    _fail_a_rewrite(events, before)
    _, _, model, _, (prompt_id, version) = BEFORE_THE_FAILED_REWRITE[before]

    status, body = _rewrite("queue")

    assert status == 202
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"rewrite:{_rewrite_id(body['rewriting'])}",
            "model_id": model,
            "prompt_id": prompt_id,
            "prompt_version": version,
            "performance": "bad",
            "rating_delta": -2,
            "reason": "Article Rewrite",
            "subject": ARTICLE_ID,
            "topic_id": TOPIC_ID,
            "source": "admin_api",
            "by": "human",
        }
    ]
