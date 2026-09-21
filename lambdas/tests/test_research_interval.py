"""The research interval: the schedule is only a heartbeat, and each tick checks
whether it is due against an interval read from DynamoDB (per topic, or one
pipeline-wide default)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import admin_api_handler
import common.dynamo as dynamo
import research_tick_handler
from common import research_schedule as rs
from common.adapters.github_trending import GitHubTrendingAdapter

REGION = "ap-southeast-2"
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


# --- the pure logic ------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 2, 24, 168, Decimal("3")])
def test_whole_hours_in_range_are_valid(value):
    assert rs.interval_error(value) is None


@pytest.mark.parametrize("value", [0, -1, 169, 2.5, "2", True, False, [2], Decimal("2.5")])
def test_anything_else_is_rejected(value):
    assert "whole number of hours from 1 to 168" in rs.interval_error(value)


def test_unset_is_valid_and_means_inherit():
    assert rs.interval_error(None) is None


def test_a_topics_own_interval_beats_the_global_default_which_beats_the_builtin():
    assert rs.resolve_interval_hours({"research_interval_hours": 3}, {"research_interval_hours": 2}) == 3
    assert rs.resolve_interval_hours({}, {"research_interval_hours": 2}) == 2
    assert rs.resolve_interval_hours({}, {}) == rs.DEFAULT_RESEARCH_INTERVAL_HOURS == 1
    assert rs.resolve_interval_hours({}, None) == 1


def test_a_stored_value_that_is_no_longer_valid_falls_through_instead_of_being_trusted():
    assert rs.resolve_interval_hours({"research_interval_hours": 0}, {"research_interval_hours": 2}) == 2
    junk = {"research_interval_hours": "abc"}
    assert rs.resolve_interval_hours(junk, {"research_interval_hours": 999}) == 1


def test_dynamodb_decimals_are_understood():
    assert rs.resolve_interval_hours({"research_interval_hours": Decimal("4")}, None) == 4


def test_a_topic_never_checked_is_due():
    assert rs.is_due(NOW, None, 2) is True
    assert rs.next_due_at(None, 2) is None


@pytest.mark.parametrize("bad", ["", "yesterday", 12345])
def test_an_unreadable_timestamp_counts_as_never_checked(bad):
    assert rs.is_due(NOW, bad, 2) is True


def test_a_naive_timestamp_is_read_as_utc():
    last = (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()

    assert rs.is_due(NOW, last, 2) is False


def test_due_needs_the_interval_to_have_elapsed_less_a_small_tolerance():
    interval = 2
    last = NOW - timedelta(hours=interval)

    # a hair under the interval (scheduler jitter) still counts as due...
    assert rs.is_due(NOW - timedelta(seconds=30), last.isoformat(), interval) is True
    # ...but a whole missed heartbeat's worth early does not
    assert rs.is_due(NOW - timedelta(hours=1), last.isoformat(), interval) is False


def test_the_next_due_time_is_the_last_check_plus_the_interval_less_the_tolerance():
    last = NOW - timedelta(hours=1)

    assert rs.next_due_at(last.isoformat(), 2) == last + timedelta(hours=2) - rs.DUE_TOLERANCE


def test_an_interval_of_one_hour_is_due_at_every_hourly_heartbeat():
    last = NOW - timedelta(hours=1) + timedelta(seconds=45)  # ticks drift a little

    assert rs.is_due(NOW, last.isoformat(), 1) is True


# --- fixtures for everything that touches DynamoDB ------------------------------------------


@pytest.fixture
def world(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "TOPICS_TABLE": "Topics",
        "FINDINGS_TABLE": "Findings",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "CONTENT_BUCKET": "bloggerbear-content-test",
        "BEDROCK_MODEL_ID": "anthropic.claude-3-haiku-20240307-v1:0",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    research_tick_handler._s3_client = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName="Topics",
            KeySchema=[{"AttributeName": "topic_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "topic_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Findings",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "captured_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "captured_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket="bloggerbear-content-test",
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield


def _put_topic(**extra):
    item = {
        "topic_id": "t",
        "name": "T",
        "adapter": "github_trending",
        "adapter_config": {},
        "is_financial": False,
        **extra,
    }
    boto3.resource("dynamodb", region_name=REGION).Table("Topics").put_item(Item=item)


def _topic():
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    return table.get_item(Key={"topic_id": "t"})["Item"]


def _checked(hours_ago):
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


class _Source:
    """A fake source whose fetches are counted (and can be made to fail)."""

    def __init__(self, monkeypatch, *, fails=False):
        self.fetches = 0
        self.fails = fails
        state = {
            "repos": [{"name": "a/b", "url": "u", "description": "", "stars": 1, "language": None}],
            "fetched_at": "2026-09-21T00:00:00+00:00",
        }

        def fetch(adapter, topic_config):
            self.fetches += 1
            if self.fails:
                raise RuntimeError("source down")
            return state

        monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", fetch)


def _tick(event=None):
    with patch("research_tick_handler.invoke_model_tracked") as mock_invoke:
        mock_invoke.return_value = {
            "text": "summary", "model_id": "m", "input_tokens": 1, "output_tokens": 1, "used_fallback": False,
        }
        result = research_tick_handler.handler(event or {"topic_id": "t"}, None)
    return result, mock_invoke


# --- the handler ------------------------------------------------------------------------------


def test_a_topic_never_checked_runs_and_records_the_check(world, monkeypatch):
    source = _Source(monkeypatch)
    _put_topic()
    before = datetime.now(UTC)

    result, _ = _tick()

    assert result["status"] == "material_change" and source.fetches == 1
    recorded = datetime.fromisoformat(_topic()["last_research_at"])
    assert before <= recorded <= datetime.now(UTC)


def test_a_topic_checked_within_its_interval_is_not_due_and_does_no_work(world, monkeypatch):
    source = _Source(monkeypatch)
    _put_topic(last_research_at=_checked(0.2))

    result, mock_invoke = _tick()

    assert result["status"] == "not_due"
    assert result["topic_id"] == "t" and result["interval_hours"] == 1
    assert datetime.fromisoformat(result["next_due_at"]) > datetime.now(UTC)
    assert source.fetches == 0  # no fetch, no model call
    mock_invoke.assert_not_called()


def test_a_topic_past_its_interval_is_due(world, monkeypatch):
    source = _Source(monkeypatch)
    _put_topic(last_research_at=_checked(1.1))

    result, _ = _tick()

    assert result["status"] == "material_change" and source.fetches == 1


def test_a_topics_own_interval_holds_it_back_between_heartbeats(world, monkeypatch):
    source = _Source(monkeypatch)
    _put_topic(research_interval_hours=2, last_research_at=_checked(1.0))

    result, _ = _tick()

    assert result["status"] == "not_due" and result["interval_hours"] == 2
    assert source.fetches == 0


def test_a_topic_runs_again_once_its_two_hours_are_up(world, monkeypatch):
    source = _Source(monkeypatch)
    _put_topic(research_interval_hours=2, last_research_at=_checked(2.0))

    result, _ = _tick()

    assert result["status"] == "material_change" and source.fetches == 1


def test_the_pipeline_wide_default_applies_to_a_topic_with_no_interval_of_its_own(world, monkeypatch):
    """The cost-saving case: one edit slows every topic."""
    source = _Source(monkeypatch)
    dynamo.put_pipeline_config(research_interval_hours=2)
    _put_topic(last_research_at=_checked(1.0))

    result, _ = _tick()

    assert result["status"] == "not_due" and result["interval_hours"] == 2
    assert source.fetches == 0


def test_a_topics_own_interval_overrides_the_pipeline_wide_default(world, monkeypatch):
    source = _Source(monkeypatch)
    dynamo.put_pipeline_config(research_interval_hours=6)
    _put_topic(research_interval_hours=1, last_research_at=_checked(1.0))

    result, _ = _tick()

    assert result["status"] == "material_change" and source.fetches == 1


def test_a_manual_trigger_bypasses_the_interval(world, monkeypatch):
    source = _Source(monkeypatch)
    _put_topic(research_interval_hours=24, last_research_at=_checked(0.1))

    result, _ = _tick({"topic_id": "t", "force": True})

    assert result["status"] == "material_change" and source.fetches == 1


def test_only_a_literal_true_forces(world, monkeypatch):
    source = _Source(monkeypatch)
    _put_topic(research_interval_hours=24, last_research_at=_checked(0.1))

    result, _ = _tick({"topic_id": "t", "force": "true"})

    assert result["status"] == "not_due" and source.fetches == 0


def test_a_check_that_finds_nothing_new_is_still_recorded(world, monkeypatch):
    """A tick that changes nothing writes no Finding, so this field is the only
    record that the source was looked at -- without it the topic would be re-checked
    at every heartbeat."""
    _Source(monkeypatch)
    _put_topic()
    _tick()  # first tick: material
    dynamo.set_topic_last_research_at("t", _checked(2.0))  # pretend it was long ago

    result, _ = _tick()

    assert result == {"status": "no_change"}
    assert datetime.now(UTC) - datetime.fromisoformat(_topic()["last_research_at"]) < timedelta(minutes=1)


def test_a_failed_fetch_records_nothing_so_the_next_heartbeat_retries(world, monkeypatch):
    _Source(monkeypatch, fails=True)
    _put_topic(research_interval_hours=6)

    result, _ = _tick()

    assert result["status"] == "error"
    assert "last_research_at" not in _topic()


def test_an_unreadable_pipeline_config_does_not_stop_research(world, monkeypatch):
    source = _Source(monkeypatch)
    _put_topic()

    with patch("research_tick_handler.get_pipeline_config", side_effect=RuntimeError("throttled")):
        result, _ = _tick()

    assert result["status"] == "material_change" and source.fetches == 1


def test_failing_to_record_the_check_does_not_fail_the_tick(world, monkeypatch):
    _Source(monkeypatch)
    _put_topic()

    with patch("research_tick_handler.set_topic_last_research_at", side_effect=RuntimeError("boom")):
        result, _ = _tick()

    assert result["status"] == "material_change"


# --- the DynamoDB helpers ---------------------------------------------------------------------


def test_no_pipeline_config_row_is_a_valid_state(world):
    assert dynamo.get_pipeline_config() is None


def test_the_pipeline_interval_round_trips_as_an_int_and_can_be_cleared(world):
    config = dynamo.put_pipeline_config(research_interval_hours=3)

    assert config == {"config_id": "pipeline", "research_interval_hours": 3}
    assert isinstance(dynamo.get_pipeline_config()["research_interval_hours"], int)

    cleared = dynamo.put_pipeline_config(research_interval_hours=None)
    assert "research_interval_hours" not in cleared


def test_the_pipeline_row_and_the_model_default_row_do_not_overwrite_each_other(world):
    dynamo.put_model_config(model_id="m1", fallback_model_id="m2")

    dynamo.put_pipeline_config(research_interval_hours=2)
    dynamo.put_model_config(model_id="m3", fallback_model_id=None)

    assert dynamo.get_pipeline_config()["research_interval_hours"] == 2
    assert dynamo.get_model_config()["model_id"] == "m3"


def test_setting_the_pipeline_interval_leaves_other_settings_on_that_row_alone(world):
    table = boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig")
    table.put_item(Item={"config_id": "pipeline", "some_future_setting": "keep me"})

    dynamo.put_pipeline_config(research_interval_hours=4)

    item = table.get_item(Key={"config_id": "pipeline"})["Item"]
    assert item["some_future_setting"] == "keep me" and item["research_interval_hours"] == 4


def test_set_topic_last_research_at_changes_only_that_field(world):
    _put_topic()

    dynamo.set_topic_last_research_at("t", "2026-09-21T09:00:00+00:00")

    item = _topic()
    assert item["last_research_at"] == "2026-09-21T09:00:00+00:00" and item["name"] == "T"


def test_set_topic_last_research_at_does_not_create_a_missing_topic(world):
    failed = boto3.client("dynamodb", region_name=REGION).exceptions.ConditionalCheckFailedException

    with pytest.raises(failed):
        dynamo.set_topic_last_research_at("deleted", "2026-09-21T09:00:00+00:00")


# --- the admin API -------------------------------------------------------------------------------


def _admin(route_key, body=None, path_params=None):
    event = {"routeKey": route_key}
    if body is not None:
        event["body"] = json.dumps(body)
    if path_params is not None:
        event["pathParameters"] = path_params
    return admin_api_handler.handler(event, None)


@pytest.fixture
def admin(world, monkeypatch):
    """Topic routes also call EventBridge Scheduler; that is not what is tested here."""
    monkeypatch.setattr(admin_api_handler, "upsert_topic_schedules", lambda *args, **kwargs: None)


def test_a_topic_can_be_created_with_an_interval(admin):
    result = _admin(
        "POST /topics",
        {"topic_id": "t", "name": "T", "adapter": "github_trending", "research_interval_hours": 2},
    )

    assert result["statusCode"] == 201
    assert _topic()["research_interval_hours"] == 2


def test_a_topic_created_without_one_stores_none_and_inherits(admin):
    _admin("POST /topics", {"topic_id": "t", "name": "T", "adapter": "github_trending"})

    assert "research_interval_hours" not in _topic()


@pytest.mark.parametrize("bad", [0, 169, "2", 2.5, True])
def test_an_invalid_interval_is_refused_on_create(admin, bad):
    result = _admin(
        "POST /topics",
        {"topic_id": "t", "name": "T", "adapter": "github_trending", "research_interval_hours": bad},
    )

    assert result["statusCode"] == 400
    assert "research_interval_hours" in json.loads(result["body"])["error"]


def test_an_interval_can_be_set_and_then_cleared_on_update(admin):
    _put_topic()

    set_result = _admin("PUT /topics/{topic_id}", {"research_interval_hours": 3}, {"topic_id": "t"})
    assert set_result["statusCode"] == 200 and _topic()["research_interval_hours"] == 3

    clear_result = _admin("PUT /topics/{topic_id}", {"research_interval_hours": None}, {"topic_id": "t"})
    assert clear_result["statusCode"] == 200
    assert "research_interval_hours" not in _topic()  # gone, so the topic inherits again


@pytest.mark.parametrize("bad", [0, 169, "2", 2.5])
def test_an_invalid_interval_is_refused_on_update_and_changes_nothing(admin, bad):
    _put_topic(research_interval_hours=2)

    result = _admin("PUT /topics/{topic_id}", {"research_interval_hours": bad}, {"topic_id": "t"})

    assert result["statusCode"] == 400
    assert _topic()["research_interval_hours"] == 2


def test_an_unrelated_update_keeps_the_interval_and_the_last_check(admin):
    _put_topic(research_interval_hours=2, last_research_at="2026-09-21T09:00:00+00:00")

    _admin("PUT /topics/{topic_id}", {"name": "Renamed"}, {"topic_id": "t"})

    item = _topic()
    assert item["research_interval_hours"] == 2 and item["last_research_at"] == "2026-09-21T09:00:00+00:00"


def test_the_pipeline_config_defaults_to_the_builtin_interval(world):
    body = json.loads(_admin("GET /pipeline-config")["body"])

    assert body["research_interval_hours"] is None
    assert body["effective_default_research_interval_hours"] == 1


def test_the_pipeline_wide_interval_can_be_set_read_and_cleared(world):
    put = _admin("PUT /pipeline-config", {"research_interval_hours": 2})
    assert put["statusCode"] == 200
    assert json.loads(put["body"])["effective_default_research_interval_hours"] == 2
    assert json.loads(_admin("GET /pipeline-config")["body"])["research_interval_hours"] == 2

    cleared = json.loads(_admin("PUT /pipeline-config", {"research_interval_hours": None})["body"])
    assert cleared["research_interval_hours"] is None
    assert cleared["effective_default_research_interval_hours"] == 1


@pytest.mark.parametrize("body", [{"research_interval_hours": 0}, {"research_interval_hours": "x"}, {}])
def test_an_invalid_or_missing_pipeline_interval_is_refused(world, body):
    assert _admin("PUT /pipeline-config", body)["statusCode"] == 400
    assert dynamo.get_pipeline_config() is None
