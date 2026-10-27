"""The evals settings (common/evals_settings.py; docs/enhancements/model-ranch-prompt-assets-evals.md,
section 4.5): the `evals` row of the model-config table, in the model catalog's pattern.

What is held: every setting and default is section 4.5's; the gates and the judge's thresholds are
DynamoDB-only; each setting refuses what it cannot hold; a stored value that is invalid falls back
to its default with one line, so one bad edit never stops a run; `excluded` reads deciders, offline
runs and sources, from a time when given; the gates apply only with min_events behind them; the
judge's model falls back as the spec says."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3
import pytest
from moto import mock_aws

from common import dynamo, evals_settings

REGION = "ap-southeast-2"
NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)


@pytest.fixture
def config(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "BEDROCK_MODEL_ID": "maker.default-v1:0",
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig")
    dynamo._dynamodb_resource = None


def test_every_setting_and_its_default_is_section_4_5s():
    assert evals_settings.CONFIG_ID == "evals"
    assert evals_settings.SETTINGS == {
        "daily_gate": -50, "weekly_gate": -100, "total_gate": -1000, "min_events": 10,
        "judge_min_agreement": 0.7, "judge_min_pairs": 20, "cull_enabled": False, "cull_max_per_run": 3,
        "judge_enabled": True, "judge_interval_hours": 4, "judge_max_items": 10, "judge_model_id": None,
        "score_max_cost_aud": 5, "evals_daily_spend_cap_aud": 2, "trace_calls": False,
        "timezone": "Australia/Sydney", "excluded_sources": [],
    }  # fmt: skip
    assert evals_settings.DYNAMODB_ONLY == {
        "daily_gate", "weekly_gate", "total_gate", "min_events", "judge_min_agreement", "judge_min_pairs",
    }  # fmt: skip
    for name, default in evals_settings.SETTINGS.items():
        assert evals_settings.validate_setting(name, default) is None, name


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("daily_gate", [-50, 0, -1_000_000], [1, -1_000_001, -5.5, True, "-50"]),
        ("min_events", [1, 1000], [0, 1001, 2.0]),
        ("judge_min_agreement", [0, 0.7, 1], [-0.1, 1.5, True, "0.7"]),
        ("judge_interval_hours", [1, 24], [0, 25]),
        ("judge_max_items", [1, 50], [0, 51]),
        ("cull_max_per_run", [1, 50], [0, 51]),
        ("cull_enabled", [True, False], ["on", 1]),
        ("trace_calls", [True, False], [0]),
        ("judge_model_id", ["maker.judge-v1:0"], ["", "two words", "maker.judge-v1:0\n", 7]),
        ("score_max_cost_aud", [0, 5, 99.5, 100], [-1, 101, False]),
        ("evals_daily_spend_cap_aud", [2], [1000]),
        ("timezone", ["Australia/Sydney", "UTC", "America/Los_Angeles"], ["Mars/Olympus", "", "../etc", 10]),
        (
            "excluded_sources",
            [[], ["judge"], ["run:r-123", "daily_cycle"],
             [{"source": "judge", "since": "2026-10-27T00:00:00Z"}]],
            ["judge", ["Judge"], ["run:"], ["two words"], [{"source": "judge", "until": "x"}],
             [{"since": "2026-10-27"}], [{"source": "judge", "since": "yesterday"}], ["judge"] * 21],
        ),
    ],
)  # fmt: skip
def test_each_setting_refuses_what_it_cannot_hold(name, good, bad):
    for value in good:
        assert evals_settings.validate_setting(name, value) is None, value
    for value in bad:
        assert evals_settings.validate_setting(name, value), value


def test_none_clears_any_setting_and_an_unknown_one_is_refused():
    assert evals_settings.validate_setting("daily_gate", None) is None
    assert evals_settings.validate_setting("made_up", 1) == "unknown setting 'made_up'"


def test_a_stored_value_that_is_invalid_falls_back_to_its_default_with_one_line(capsys):
    stored = {"min_events": 0, "judge_enabled": False, "timezone": "Nowhere"}
    effective = evals_settings.effective_settings(stored)

    assert effective["min_events"] == 10 and effective["timezone"] == "Australia/Sydney"
    assert effective["judge_enabled"] is False
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "evals_settings: ignoring invalid min_events=0; using 10",
        "evals_settings: ignoring invalid timezone='Nowhere'; using 'Australia/Sydney'",
    ]


def test_the_default_list_is_a_fresh_copy_each_time():
    first = evals_settings.effective_settings(None)
    first["excluded_sources"].append("judge")
    assert evals_settings.effective_settings(None)["excluded_sources"] == []


def test_the_zone_is_the_settings_and_sydney_otherwise():
    assert evals_settings.zone({"timezone": "UTC"}) == ZoneInfo("UTC")
    assert evals_settings.zone({"timezone": "Nowhere"}) == ZoneInfo("Australia/Sydney")


# --- excluded sources -----------------------------------------------------------------------------


EVENT = {
    "event_id": "offline:r-123:item-1:combo",
    "timestamp": "2026-10-27T05:00:00Z",
    "source": "eval_jobs",
    "by": "judge",
}


@pytest.mark.parametrize(
    ("entry", "left_out"),
    [
        ("judge", True),
        ("human", False),
        ("run:r-123", True),
        ("run:r-12", False),
        ("eval_jobs", True),
        ("daily_cycle", False),
        ({"source": "judge", "since": "2026-10-27T04:59:59Z"}, True),
        ({"source": "judge", "since": "2026-10-27T05:00:00Z"}, True),
        ({"source": "judge", "since": "2026-10-27T05:00:01Z"}, False),
        ({"source": "judge"}, True),
    ],
)
def test_an_excluded_source_names_a_decider_an_offline_run_or_a_lambda_from_a_time(entry, left_out):
    assert evals_settings.excluded(EVENT, {"excluded_sources": [entry]}) is left_out


def test_nothing_is_excluded_by_default():
    assert evals_settings.excluded(EVENT, evals_settings.effective_settings(None)) is False


# --- the gates ------------------------------------------------------------------------------------


SETTINGS = evals_settings.effective_settings(None)


def test_a_window_is_gated_below_its_gate_only_with_min_events_behind_it():
    row = {
        "daily_perf": Decimal("-51"),
        "weekly_perf": -100,
        "total_perf": -2000,
        "monthly_perf": -9999,
        "perf_events": {"daily": Decimal("10"), "weekly": 50, "monthly": 50, "total": 9},
    }
    # Daily is under -50 with 10 events; weekly is at its gate, not under; total has too few events;
    # the month has no gate.
    assert evals_settings.gated_windows(row, SETTINGS) == ["daily"]
    assert evals_settings.gated_windows({**row, "weekly_perf": -101}, SETTINGS) == ["daily", "weekly"]


@pytest.mark.parametrize(
    "row", [{}, {"daily_perf": "-99", "perf_events": {"daily": 99}}, {"perf_events": "x"}]
)
def test_a_row_with_no_scores_or_unreadable_ones_is_never_gated(row):
    assert evals_settings.gated_windows(row, SETTINGS) == []


def test_ungated_until_holds_a_gate_open_until_its_time():
    assert evals_settings.ungated({"ungated_until": "2026-10-28T00:00:00Z"}, NOW) is True
    assert evals_settings.ungated({"ungated_until": "2026-10-26T00:00:00+00:00"}, NOW) is False
    assert evals_settings.ungated({"ungated_until": "soon"}, NOW) is False
    assert evals_settings.ungated({}, NOW) is False


# --- the row --------------------------------------------------------------------------------------


def test_the_judges_model_is_the_setting_else_model_configs_fallback_else_the_deployments(config):
    assert evals_settings.judge_model({"judge_model_id": "maker.judge-v1:0"}) == "maker.judge-v1:0"
    assert evals_settings.judge_model(SETTINGS) == "maker.default-v1:0"
    config.put_item(
        Item={"config_id": "default", "model_id": "maker.main-v1:0", "fallback_model_id": "maker.spare-v1:0"}
    )
    assert evals_settings.judge_model(SETTINGS) == "maker.spare-v1:0"


def test_the_summary_shows_the_row_as_stored_and_every_value_in_effect(config):
    config.put_item(Item={"config_id": "evals", "judge_enabled": False, "min_events": 0})

    summary = evals_settings.summary()

    assert summary["settings"]["judge_enabled"] is False and summary["settings"]["min_events"] == 0
    assert summary["settings"]["cull_enabled"] is None
    assert summary["effective"] == {**evals_settings.SETTINGS, "judge_enabled": False}
    assert summary["judge_model"] == "maker.default-v1:0"
    assert summary["dynamodb_only"] == sorted(evals_settings.DYNAMODB_ONLY)
