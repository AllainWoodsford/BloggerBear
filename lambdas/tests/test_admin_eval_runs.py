"""The admin API's offline scoring run routes (admin_api_handler.py over common/eval_score.py;
docs/enhancements/archive/model-ranch-prompt-assets-evals.md, PR18b): POST /evals/runs, GET /evals/runs,
GET and DELETE /evals/runs/{run_id}, POST /evals/runs/{run_id}/resume, and their place in both
environments' Terraform.

What is held: each route hands eval_score what the request carries and answers with its status and
payload; a refusal comes back with its status, message and figures; a body that is not JSON and a
limit out of range are refused before eval_score is asked; an unknown run is a 404; and the five routes
are in _ROUTES and in both roots' routes lists. eval_score itself is test_eval_score.py's.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import admin_api_handler
from common import eval_score

RUN_ROUTES = {
    "POST /evals/runs",
    "GET /evals/runs",
    "GET /evals/runs/{run_id}",
    "DELETE /evals/runs/{run_id}",
    "POST /evals/runs/{run_id}/resume",
}
RUN_ID = "score-20261027T030005-a1b2c3"


def call(route, body=None, *, run_id=None, query=None, raw=None):
    event = {"routeKey": route}
    if body is not None:
        event["body"] = json.dumps(body)
    if raw is not None:
        event["body"] = raw
    if run_id is not None:
        event["pathParameters"] = {"run_id": run_id}
    if query is not None:
        event["queryStringParameters"] = query
    response = admin_api_handler.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def test_post_hands_the_request_to_eval_score_and_answers_its_plan_or_run():
    request = {"judge_model_id": "j", "model_ids": ["m"], "prompt_id": "daily_cycle/draft", "sample": 2}
    with patch.object(eval_score, "start", return_value=(202, {"run_id": RUN_ID, "sent": 4})) as start:
        assert call("POST /evals/runs", request) == (202, {"run_id": RUN_ID, "sent": 4})
    start.assert_called_once_with(request)


def test_a_refused_run_comes_back_with_its_status_message_and_figures():
    refusal = eval_score.Refused(409, "the estimate is more than score_max_cost_aud", confirm_required=True,
                                 estimate_aud=6.1)  # fmt: skip
    with patch.object(eval_score, "start", side_effect=refusal):
        status, answer = call("POST /evals/runs", {})
    assert status == 409
    assert answer == {
        "error": "the estimate is more than score_max_cost_aud",
        "confirm_required": True,
        "estimate_aud": 6.1,
    }


def test_a_body_that_is_not_json_is_refused_before_eval_score_is_asked():
    with patch.object(eval_score, "start", side_effect=AssertionError("asked")):
        assert call("POST /evals/runs", raw="{not json")[0] == 400


@pytest.mark.parametrize("query,limit", [(None, 20), ({"limit": "5"}, 5), ({"limit": "100"}, 100)])
def test_the_runs_list_takes_a_limit_of_one_to_a_hundred(query, limit):
    with patch.object(eval_score, "list_runs", return_value={"runs": [], "count": 0}) as listed:
        assert call("GET /evals/runs", query=query) == (200, {"runs": [], "count": 0})
    listed.assert_called_once_with(limit)


@pytest.mark.parametrize("limit", ["0", "101", "ten", "-1"])
def test_a_limit_out_of_range_is_refused(limit):
    with patch.object(eval_score, "list_runs", side_effect=AssertionError("asked")):
        assert call("GET /evals/runs", query={"limit": limit})[0] == 400


def test_one_run_is_its_view_and_an_unknown_one_is_a_404():
    view = {"run_id": RUN_ID, "status": "running", "progress": {"messages": 4, "results": 1}}
    with patch.object(eval_score, "run_view", return_value=view) as viewed:
        assert call("GET /evals/runs/{run_id}", run_id=RUN_ID) == (200, view)
    viewed.assert_called_once_with(RUN_ID)
    with patch.object(eval_score, "run_view", return_value=None):
        assert call("GET /evals/runs/{run_id}", run_id="score-gone") == (404, {"error": "no run score-gone"})
    with patch.object(eval_score, "run_view", side_effect=eval_score.Refused(400, "a run id is ...")):
        assert call("GET /evals/runs/{run_id}", run_id="bad id")[0] == 400


def test_delete_cancels_the_run_and_resume_sends_its_items_again():
    with patch.object(eval_score, "cancel", return_value={"run_id": RUN_ID, "status": "cancelled"}) as cancel:
        cancelled = call("DELETE /evals/runs/{run_id}", run_id=RUN_ID)
    assert cancelled == (200, {"run_id": RUN_ID, "status": "cancelled"})
    cancel.assert_called_once_with(RUN_ID)
    resumed = {"run_id": RUN_ID, "status": "queued", "resent": 3, "estimate_aud": 0.38}
    with patch.object(eval_score, "resume", return_value=resumed) as resume:
        assert call("POST /evals/runs/{run_id}/resume", run_id=RUN_ID) == (202, resumed)
    resume.assert_called_once_with(RUN_ID)
    with patch.object(eval_score, "cancel", side_effect=eval_score.Refused(409, "run is done")):
        assert call("DELETE /evals/runs/{run_id}", run_id=RUN_ID) == (409, {"error": "run is done"})


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_five_run_routes_are_in_the_environments_terraform(env):
    main = (Path(__file__).resolve().parents[2] / "infra" / "environments" / env / "main.tf").read_text(
        encoding="utf-8"
    )
    admin_routes = main[: main.index('"GET /articles/{article_id}/feedback-status"')]
    assert {route for route in admin_api_handler._ROUTES if "/evals/runs" in route} == RUN_ROUTES
    for route in RUN_ROUTES:
        assert f'"{route}",' in admin_routes, route
