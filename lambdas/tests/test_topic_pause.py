"""Pausing a topic (`topics pause`): nothing of it runs until it is resumed. The Topics row's
`paused` is the source of truth: the research tick and the daily cycle refuse a paused topic
whatever invoked them, the Admin API refuses to trigger one, its two schedules are disabled, the
topic page's "Articles in the Pipeline" says Paused, and the bear muses about the change."""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
import test_admin_api_handler as admin_tests
from moto import mock_aws
from test_admin_api_handler import TOPIC, _event, _put_topic

import admin_api_handler
import daily_cycle_handler
import research_tick_handler
from common import musings

REGION = "ap-southeast-2"
PAUSED_TOPIC = {**TOPIC, "paused": True, "paused_at": "2026-10-13T09:00:00+00:00"}

# The Admin API's fixtures (the tables, the environment), under their own names. The public
# topic page's side of a pause is tested beside the other activity tests, in
# test_public_api_handler.py.
aws_env = admin_tests.aws_env
aws_resources = admin_tests.aws_resources


# --- the handlers ---------------------------------------------------------------------------------


def test_the_research_tick_fetches_nothing_for_a_paused_topic(monkeypatch):
    monkeypatch.setenv("CONTENT_BUCKET", "bucket")
    with (
        patch("research_tick_handler.get_topic", return_value=PAUSED_TOPIC),
        patch("research_tick_handler.get_latest_finding") as mock_finding,
        patch("research_tick_handler.set_topic_last_research_at") as mock_record,
    ):
        result = research_tick_handler.handler({"topic_id": "github-trending", "force": True}, None)

    assert result == {"status": "paused", "topic_id": "github-trending"}
    mock_finding.assert_not_called()
    mock_record.assert_not_called()


def test_the_daily_cycle_writes_nothing_for_a_paused_topic():
    with (
        patch("daily_cycle_handler.get_topic", return_value=PAUSED_TOPIC),
        patch("daily_cycle_handler.list_recent_findings") as mock_findings,
        patch("daily_cycle_handler.refresh_articles_snapshot") as mock_refresh,
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending", "force": True}, None)

    assert result == {"status": "paused", "topic_id": "github-trending"}
    mock_findings.assert_not_called()
    mock_refresh.assert_not_called()


@pytest.mark.parametrize("stored", [False, None, "yes", 1])
def test_only_a_literal_true_pauses(stored):
    topic = {**TOPIC, "paused": stored}
    with (
        patch("daily_cycle_handler.get_topic", return_value=topic),
        patch("daily_cycle_handler.list_recent_findings", return_value=[]) as mock_findings,
    ):
        result = daily_cycle_handler.handler({"topic_id": "github-trending"}, None)

    assert result["status"] == "no_findings"
    mock_findings.assert_called_once()


# --- the Admin API ----------------------------------------------------------------------------------


def _row():
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    return table.get_item(Key={"topic_id": "github-trending"})["Item"]


def _pause(action="pause"):
    with (
        patch("admin_api_handler.set_topic_schedules_state", return_value=[f"x-{action}"]) as mock_state,
        patch("admin_api_handler.generate_and_store_pause_musing") as mock_musing,
    ):
        event = _event(f"POST /topics/{{topic_id}}/{action}", path_params={"topic_id": "github-trending"})
        result = admin_api_handler.handler(event, None)
    return result, mock_state, mock_musing


def test_pausing_writes_the_row_first_then_disables_the_schedules_and_the_bear_muses(aws_resources):
    _put_topic()

    result, mock_state, mock_musing = _pause()

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["paused"] is True and body["changed"] is True and body["schedules"] == ["x-pause"]
    row = _row()
    assert row["paused"] is True and row["paused_at"] == body["at"]
    mock_state.assert_called_once_with("github-trending", enabled=False)
    mock_musing.assert_called_once()
    assert mock_musing.call_args.kwargs["paused"] is True
    assert mock_musing.call_args.kwargs["topic_name"] == "GitHub Trending"


def test_pausing_a_paused_topic_changes_nothing_and_muses_nothing(aws_resources):
    _put_topic(PAUSED_TOPIC)

    result, mock_state, mock_musing = _pause()

    assert json.loads(result["body"]) == {"topic_id": "github-trending", "paused": True, "changed": False}
    mock_state.assert_not_called()
    mock_musing.assert_not_called()


def test_resuming_enables_the_schedules_again(aws_resources):
    _put_topic(PAUSED_TOPIC)

    result, mock_state, mock_musing = _pause("resume")

    body = json.loads(result["body"])
    assert body["paused"] is False and body["changed"] is True
    row = _row()
    assert row["paused"] is False and row["resumed_at"] == body["at"]
    assert row["paused_at"] == PAUSED_TOPIC["paused_at"]  # kept: when it was paused is still history
    mock_state.assert_called_once_with("github-trending", enabled=True)
    assert mock_musing.call_args.kwargs["paused"] is False


def test_a_schedule_that_could_not_be_changed_is_said_so_and_the_row_still_wins(aws_resources):
    _put_topic()

    with (
        patch("admin_api_handler.set_topic_schedules_state", side_effect=RuntimeError("scheduler down")),
        patch("admin_api_handler.generate_and_store_pause_musing"),
    ):
        result = admin_api_handler.handler(
            _event("POST /topics/{topic_id}/pause", path_params={"topic_id": "github-trending"}), None
        )

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["schedules"] == [] and "scheduler down" in body["schedules_error"]
    assert _row()["paused"] is True


def test_an_unknown_topic_is_a_404(aws_resources):
    result, _, _ = _pause()
    assert result["statusCode"] == 404


def test_a_paused_topic_cannot_be_triggered(aws_resources):
    _put_topic(PAUSED_TOPIC)
    admin_api_handler._lambda_client = None

    event = _event(
        "POST /topics/{topic_id}/trigger",
        path_params={"topic_id": "github-trending"},
        body={"pipeline": "research_tick"},
    )
    with patch("admin_api_handler._get_lambda_client") as mock_client:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 409
    assert "topics resume github-trending" in json.loads(result["body"])["error"]
    mock_client.assert_not_called()


def test_an_edit_to_a_paused_topic_keeps_its_schedules_disabled(aws_resources):
    _put_topic(PAUSED_TOPIC)

    event = _event(
        "PUT /topics/{topic_id}", path_params={"topic_id": "github-trending"}, body={"name": "Renamed"}
    )
    with patch("admin_api_handler.upsert_topic_schedules") as mock_upsert:
        result = admin_api_handler.handler(event, None)

    assert result["statusCode"] == 200
    assert mock_upsert.call_args.kwargs == {"enabled": False}
    assert _row()["paused"] is True


# --- the musing -------------------------------------------------------------------------------------


@pytest.fixture
def musings_table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MUSINGS_TABLE", "Musings")
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="Musings",
            KeySchema=[{"AttributeName": "musing_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "musing_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table("Musings")


def _muse(reply, *, paused):
    fake = patch(
        "common.musings.tracked_claude",
        side_effect=reply if isinstance(reply, Exception) else None,
        return_value=None if isinstance(reply, Exception) else reply,
    )
    with fake as mock:
        musing = musings.generate_and_store_pause_musing(
            topic_id="github-trending", topic_name="GitHub Trending", paused=paused, model_id="model"
        )
    return musing, mock


def test_a_pause_is_a_reflective_musing_and_a_resume_an_excited_one(musings_table):
    paused, mock = _muse("Setting my GitHub Trending research down for a while. Quiet den.", paused=True)
    assert paused["kind"] == "pause" and paused["mood"] == "reflective"
    assert paused["topic_id"] == "github-trending"
    assert "GitHub Trending" in paused["text"]
    assert "pause your research" in mock.call_args.args[1]

    resumed, _ = _muse("GitHub Trending is back on the trail!", paused=False)
    assert resumed["kind"] == "resume" and resumed["mood"] == "excited"
    assert musings_table.get_item(Key={"musing_id": resumed["musing_id"]})["Item"]["kind"] == "resume"
    # Both moods already have their bear: nothing new to draw.
    assert {"reflective", "excited"} <= set(musings.MOODS)


@pytest.mark.parametrize(
    "reply", [RuntimeError("model down"), "", "I'm setting something down for a while."]
)
def test_a_missing_or_off_topic_reply_gets_the_plain_post(musings_table, reply):
    musing, _ = _muse(reply, paused=True)
    assert musing["text"] == (
        "I'm setting my GitHub Trending research down for a while. "
        "The den is quiet on that front until I pick it up again."
    )
    musing, _ = _muse(reply, paused=False)
    assert musing["text"] == "I'm back on GitHub Trending! Nose to the ground from the next heartbeat."
