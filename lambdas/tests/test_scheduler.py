from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

import common.scheduler as scheduler

REGION = "ap-southeast-2"

RESEARCH_TICK_ARN = "arn:aws:lambda:ap-southeast-2:123456789012:function:research-tick"
STATE_MACHINE_ARN = "arn:aws:states:ap-southeast-2:123456789012:stateMachine:daily-cycle"
INVOKE_ROLE_ARN = "arn:aws:iam::123456789012:role/scheduler-invoke"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("RESEARCH_TICK_FUNCTION_ARN", RESEARCH_TICK_ARN)
    monkeypatch.setenv("STATE_MACHINE_ARN", STATE_MACHINE_ARN)
    monkeypatch.setenv("SCHEDULER_INVOKE_ROLE_ARN", INVOKE_ROLE_ARN)
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")

    # scheduler.py caches a boto3 client at module scope -- reset it so each
    # test gets one bound to moto's mock.
    scheduler._scheduler_client = None


@pytest.fixture
def scheduler_client(aws_env):
    with mock_aws():
        yield boto3.client("scheduler", region_name=REGION)


def _schedule(client, name):
    return client.get_schedule(Name=name, GroupName="default")


# --- _validate_schedule_expression -----------------------------------------


@pytest.mark.parametrize("expr", ["rate(1 hour)", "cron(0 6 * * ? *)", "at(2026-01-01T00:00:00)"])
def test_validate_schedule_expression_accepts_valid_forms(expr):
    scheduler._validate_schedule_expression(expr)  # must not raise


@pytest.mark.parametrize("expr", ["", "hourly", "1 hour", "RATE(1 hour)", None])
def test_validate_schedule_expression_rejects_invalid_forms(expr):
    with pytest.raises(ValueError):
        scheduler._validate_schedule_expression(expr)


def test_upsert_topic_schedules_invalid_expression_makes_no_aws_calls(monkeypatch):
    calls = []

    class _ExplodingClient:
        def __getattr__(self, name):
            def _boom(*args, **kwargs):
                calls.append(name)
                raise AssertionError("no AWS calls expected")

            return _boom

    monkeypatch.setattr(scheduler, "_get_scheduler_client", lambda: _ExplodingClient())

    with pytest.raises(ValueError):
        scheduler.upsert_topic_schedules("topic-a", "not-a-valid-expression", "cron(0 6 * * ? *)")

    assert calls == []


# --- upsert_topic_schedules: create path ------------------------------------


def test_upsert_topic_schedules_creates_both_schedules(scheduler_client):
    scheduler.upsert_topic_schedules("topic-a", "rate(1 hour)", "cron(0 6 * * ? *)")

    research = _schedule(scheduler_client, "bloggerbear-dev-topic-a-research-tick")
    assert research["ScheduleExpression"] == "rate(1 hour)"
    assert research["Target"]["Arn"] == RESEARCH_TICK_ARN
    assert research["Target"]["RoleArn"] == INVOKE_ROLE_ARN
    assert json.loads(research["Target"]["Input"]) == {"topic_id": "topic-a"}

    daily = _schedule(scheduler_client, "bloggerbear-dev-topic-a-daily-cycle")
    assert daily["ScheduleExpression"] == "cron(0 6 * * ? *)"
    assert daily["Target"]["Arn"] == STATE_MACHINE_ARN
    assert daily["Target"]["RoleArn"] == INVOKE_ROLE_ARN
    assert json.loads(daily["Target"]["Input"]) == {"topic_id": "topic-a"}


# --- upsert_topic_schedules: update (conflict) path -------------------------


def test_upsert_topic_schedules_falls_back_to_update_on_conflict(scheduler_client):
    scheduler.upsert_topic_schedules("topic-b", "rate(1 hour)", "cron(0 6 * * ? *)")

    # Topic already exists (e.g. cadence edited) -- calling again must hit
    # ConflictException internally and fall back to update_schedule rather
    # than raising.
    scheduler.upsert_topic_schedules("topic-b", "rate(2 hours)", "cron(0 12 * * ? *)")

    research = _schedule(scheduler_client, "bloggerbear-dev-topic-b-research-tick")
    assert research["ScheduleExpression"] == "rate(2 hours)"
    daily = _schedule(scheduler_client, "bloggerbear-dev-topic-b-daily-cycle")
    assert daily["ScheduleExpression"] == "cron(0 12 * * ? *)"


def test_upsert_topic_schedules_update_path_calls_update_not_create(monkeypatch, scheduler_client):
    # Pre-create so the real create_schedule call raises ConflictException.
    scheduler.upsert_topic_schedules("topic-c", "rate(1 hour)", "cron(0 6 * * ? *)")

    real_client = scheduler._get_scheduler_client()
    update_calls = []
    original_update = real_client.update_schedule

    def _tracking_update(**kwargs):
        update_calls.append(kwargs)
        return original_update(**kwargs)

    monkeypatch.setattr(real_client, "update_schedule", _tracking_update)

    scheduler.upsert_topic_schedules("topic-c", "rate(3 hours)", "cron(0 18 * * ? *)")

    assert len(update_calls) == 2
    names = {c["Name"] for c in update_calls}
    assert names == {
        "bloggerbear-dev-topic-c-research-tick",
        "bloggerbear-dev-topic-c-daily-cycle",
    }


# --- delete_topic_schedules --------------------------------------------------


def test_delete_topic_schedules_removes_existing(scheduler_client):
    scheduler.upsert_topic_schedules("topic-d", "rate(1 hour)", "cron(0 6 * * ? *)")

    scheduler.delete_topic_schedules("topic-d")

    with pytest.raises(scheduler_client.exceptions.ResourceNotFoundException):
        _schedule(scheduler_client, "bloggerbear-dev-topic-d-research-tick")
    with pytest.raises(scheduler_client.exceptions.ResourceNotFoundException):
        _schedule(scheduler_client, "bloggerbear-dev-topic-d-daily-cycle")


def test_delete_topic_schedules_ignores_missing_schedules(scheduler_client):
    # Never created -- must not raise.
    scheduler.delete_topic_schedules("never-existed")
