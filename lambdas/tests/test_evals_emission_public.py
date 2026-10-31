"""The public API's continuous-evals event (common/evals_emission.py; docs/enhancements/
model-ranch-prompt-assets-evals.md, section 4.3, PR14): a reader's vote is one User Feedback for
the model that wrote the article, written only once the vote is stored and the response built.

What is held: exactly one event per kept vote with exact fields (+-1, +-3 with a kept comment, no
prompt); none for a submission that is turned away; the order (stored, answered, then emitted) and
none when the vote could not be stored; nothing while evals are off; and an emit that fails leaves
the reader's answer exactly as it was.
"""

from __future__ import annotations

import json

import boto3
import pytest
import test_evals_emission as emission_tests
from test_evals_emission import ALPHA, LINEAGE, stamped
from test_public_api_handler import (  # noqa: F401 - the fixtures, used by name
    REGION,
    _event,
    _issue_token,
    _model_says,
    _submit,
    aws_env,
    aws_resources,
)

import public_api_handler
from common import evals

# The emission tests' fixtures, under their own names: evals on with every event captured, evals
# off, and Logs refusing every write.
events = emission_tests.events
evals_off = emission_tests.evals_off
logs_down = emission_tests.logs_down

ARTICLE_ID = "article-1"
TOPIC_ID = "github-trending"


@pytest.fixture(autouse=True)
def published(aws_resources):  # noqa: F811 - the fixture, by name
    """A published article written with LINEAGE (its draft by ALPHA), in the public API tests' tables."""
    boto3.resource("dynamodb", region_name=REGION).Table("Articles").put_item(
        Item={
            "article_id": ARTICLE_ID,
            "topic_id": TOPIC_ID,
            "title": "A Title",
            "body_s3_key": f"articles/{ARTICLE_ID}.md",
            "status": "published",
            "created_at": "2026-10-30T00:00:00+00:00",
            "published_at": "2026-10-30T00:00:00+00:00",
            "source_refs": [],
            "lineage": LINEAGE,
        }
    )


@pytest.mark.parametrize(
    "vote,comment,delta,performance",
    [
        ("up", None, 1, "good"),
        ("down", None, -1, "bad"),
        ("up", "Please add a chart of the star growth.", 3, "good"),
        ("down", "The second table has the wrong year.", -3, "bad"),
    ],
)
def test_a_kept_vote_is_one_user_feedback_for_the_articles_model_with_exact_fields(
    events, monkeypatch, vote, comment, delta, performance
):
    _model_says(monkeypatch, "KEEP")

    result, body = _submit(vote, **({"comment": comment} if comment else {}))

    assert result["statusCode"] == 201 and body["comment_saved"] is (comment is not None)
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"vote:{body['feedback_id']}",
            "model_id": ALPHA,
            "performance": performance,
            "rating_delta": delta,
            "reason": "User Feedback",
            "subject": ARTICLE_ID,
            "topic_id": TOPIC_ID,
            "source": "public_api",
        }
    ]


def test_a_comment_the_screening_drops_turns_the_vote_away_and_emits_nothing(events, monkeypatch):
    _model_says(monkeypatch, "DROP")

    result, _ = _submit("down", comment="you are all idiots")

    assert result["statusCode"] == 422
    assert events == []


def test_a_bot_caught_by_the_honeypot_is_told_it_worked_and_emits_nothing(events):
    result, _ = _submit("up", **{public_api_handler.HONEYPOT_FIELD: "filled in"})

    assert result["statusCode"] == 201
    assert events == []


def test_the_vote_is_stored_then_the_response_built_then_the_event_written(events, monkeypatch):
    """Section 4.3: in the public API the event is written after the response is built, and only a
    vote that was stored has one."""
    token = _issue_token()
    order = []
    put_feedback, response, put = public_api_handler.put_feedback, public_api_handler._response, evals._put

    def stored(*args, **kwargs):
        order.append("stored")
        return put_feedback(*args, **kwargs)

    def answered(status_code, payload, **kwargs):
        order.append(f"answered {status_code}")
        return response(status_code, payload, **kwargs)

    def written(messages):
        order.append("emitted")
        return put(messages)

    monkeypatch.setattr(public_api_handler, "put_feedback", stored)
    monkeypatch.setattr(public_api_handler, "_response", answered)
    monkeypatch.setattr(evals, "_put", written)
    path, body = {"article_id": ARTICLE_ID}, {"vote": "up", "token": token}
    event = _event("POST /articles/{article_id}/feedback", path_params=path, body=body)

    result = public_api_handler.handler(event, None)

    assert result["statusCode"] == 201
    assert order == ["stored", "answered 201", "emitted"]
    assert len(events) == 1


def test_a_vote_that_could_not_be_stored_emits_nothing(events, monkeypatch):
    def refused(*_args, **_kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(public_api_handler, "put_feedback", refused)

    result, _ = _submit("up")

    assert result["statusCode"] == 500
    assert events == []


def test_a_vote_writes_nothing_while_evals_are_off(evals_off):
    result, body = _submit("up")

    assert result["statusCode"] == 201 and body["status"] == "recorded"


def test_a_failing_emit_leaves_the_readers_answer_unchanged(logs_down):
    result, body = _submit("down")

    assert len(logs_down) == 1
    assert result["statusCode"] == 201
    recorded = {"status": "recorded", "article_id": ARTICLE_ID, "comment_saved": False}
    assert body == {**recorded, "feedback_id": body["feedback_id"]}
    assert result["headers"] == public_api_handler._response(201, {})["headers"]
    assert json.loads(result["body"]) == body
