"""The rotation guard (common/model_ranch.py; docs/enhancements/model-ranch-prompt-assets-evals.md,
section 4.5 and PR15): which models rows are gated, which models never are, and that it reads
nothing and gates nothing while evals are off or its settings cannot be read."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from common import evals_settings, evidence, model_ranch

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
SETTINGS = evals_settings.effective_settings(None)  # gates -50 / -100 / -1000, min_events 10


def row(model_id="maker.alpha-v1:0", **fields):
    return {"model_id": model_id, "display_name": model_id, **fields}


def scored(model_id="maker.alpha-v1:0", *, daily=0, weekly=0, total=0, events=10, **fields):
    """A models row as the rollup leaves it: every window with `events` events behind it."""
    counts = dict.fromkeys(("daily", "weekly", "monthly", "total"), events)
    return row(
        model_id, daily_perf=daily, weekly_perf=weekly, monthly_perf=0, total_perf=total, perf_events=counts,
        **fields,
    )  # fmt: skip


@pytest.fixture
def evals_on(monkeypatch):
    monkeypatch.setenv("EVALS_ENABLED", "true")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "maker.env-default-v1:0")
    reads = []

    def get_settings():
        reads.append("evals")
        return SETTINGS

    def get_model_config():
        reads.append("default")
        return {"model_id": "maker.default-v1:0", "fallback_model_id": "maker.fallback-v1:0"}

    monkeypatch.setattr(evals_settings, "get_settings", get_settings)
    monkeypatch.setattr(model_ranch.dynamo, "get_model_config", get_model_config)
    return reads


def lines(capsys, event="model_gated_protected"):
    found = [evidence.parse(text) for text in capsys.readouterr().out.splitlines()]
    return [line for line in found if line and line["event"] == event]


# --- gate -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "gated"),
    [
        ({"daily": -51}, True),
        ({"daily": -50}, False),  # at the gate is not below it
        ({"weekly": -101}, True),
        ({"total": -1001}, True),
        ({"daily": 40, "weekly": -20, "total": -900}, False),
    ],
)
def test_a_window_below_its_gate_with_enough_events_gates_the_model(scores, gated):
    assert model_ranch.gate(scored(**scores), SETTINGS, NOW) is gated


def test_a_window_gates_only_when_its_events_reach_min_events():
    assert model_ranch.gate(scored(daily=-80, events=9), SETTINGS, NOW) is False
    assert model_ranch.gate(scored(daily=-80, events=10), SETTINGS, NOW) is True
    assert model_ranch.gate(scored(daily=-80, events=3), {**SETTINGS, "min_events": 3}, NOW) is True


def test_the_monthly_window_has_no_gate():
    found = scored()
    found["monthly_perf"] = -10_000
    assert model_ranch.gate(found, SETTINGS, NOW) is False


def test_scores_as_dynamodb_stores_them_gate_like_plain_numbers():
    found = row(daily_perf=Decimal("-60"), perf_events={"daily": Decimal("12")})
    assert model_ranch.gate(found, SETTINGS, NOW) is True


def test_an_ungated_until_still_ahead_holds_the_gate_open_and_one_past_does_not():
    ahead = scored(daily=-80, ungated_until="2026-10-28T00:00:00Z")
    past = scored(daily=-80, ungated_until="2026-10-26T00:00:00+00:00")
    assert model_ranch.gate(ahead, SETTINGS, NOW) is False
    assert model_ranch.gate(past, SETTINGS, NOW) is True


@pytest.mark.parametrize(
    "found",
    [
        None,
        "maker.alpha-v1:0",
        ["daily_perf", -80],
        row(),  # no scores yet
        row(daily_perf=-80, perf_events="lots"),
        row(daily_perf=-80, perf_events=[10, 10]),
        row(daily_perf="-80", perf_events={"daily": 10}),
        row(daily_perf=-80, perf_events={"daily": "ten"}),
        row(daily_perf=True, perf_events={"daily": 10}),
        scored(daily=-80, ungated_until="next week"),
        scored(daily=-80, ungated_until=20261028),
    ],
)
def test_a_row_that_cannot_be_read_fails_open(found):
    assert model_ranch.gate(found, SETTINGS, NOW) is False


@pytest.mark.parametrize(
    ("until", "held"),
    [
        ("2026-10-28T00:00:00Z", model_ranch.HELD_OPEN_UNTIL),
        ("2026-10-27T03:00:01", model_ranch.HELD_OPEN_UNTIL),  # no zone: UTC
        ("soon", model_ranch.HELD_OPEN_UNREADABLE),
        (20261028, model_ranch.HELD_OPEN_UNREADABLE),
        ("2026-10-27T03:00:00Z", None),  # passed (it is now)
        (None, None),
    ],
)
def test_held_open_says_whether_and_why_an_override_holds_a_gate_open(until, held):
    """The one rule the guard and `evals status` both apply to `ungated_until`."""
    found = scored(daily=-80, **({} if until is None else {"ungated_until": until}))
    assert model_ranch.held_open(found, NOW) == held
    assert model_ranch.gate(found, SETTINGS, NOW) is (held is None)


def test_settings_or_a_clock_that_cannot_be_read_fail_open():
    found = scored(daily=-80)
    assert model_ranch.gate(found, None, NOW) is False
    assert model_ranch.gate(found, {"daily_gate": -50}, NOW) is False  # no min_events
    assert model_ranch.gate(found, SETTINGS, "now") is False
    held = scored(daily=-80, ungated_until="2026-10-28T00:00:00Z")
    assert model_ranch.gate(held, SETTINGS, datetime(2026, 10, 27, 3, 0)) is False  # no zone


# --- what is never gated --------------------------------------------------------------------------


def test_the_default_the_fallback_and_the_judge_are_never_gated(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "maker.env-default-v1:0")
    config = {"model_id": "maker.default-v1:0", "fallback_model_id": "maker.fallback-v1:0"}

    assert model_ranch.never_gated(SETTINGS, config) == {
        "maker.default-v1:0": "default",
        "maker.env-default-v1:0": "default",
        "maker.fallback-v1:0": "fallback",  # and the judge, by the chain: named once, by its first role
    }
    judged = {**SETTINGS, "judge_model_id": "maker.judge-v1:0"}
    assert model_ranch.never_gated(judged, config)["maker.judge-v1:0"] == "judge"
    # With no default row, the judge's chain ends at BEDROCK_MODEL_ID.
    assert model_ranch.never_gated(SETTINGS, {}) == {"maker.env-default-v1:0": "default"}


def test_a_protected_model_is_kept_however_low_its_scores(evals_on):
    guard = model_ranch.load_guard(now=NOW)
    models = [
        scored("maker.default-v1:0", daily=-500),
        scored("maker.fallback-v1:0", total=-5000),
        scored("maker.env-default-v1:0", weekly=-500),
        scored("maker.other-v1:0", daily=-500),
        scored("maker.fine-v1:0", daily=10),
    ]

    kept = [model["model_id"] for model in model_ranch.filter_healthy(models, guard)]

    assert kept == ["maker.default-v1:0", "maker.fallback-v1:0", "maker.env-default-v1:0", "maker.fine-v1:0"]
    assert guard.role("maker.fallback-v1:0") == "fallback" and guard.role("maker.other-v1:0") is None


def test_an_arn_naming_a_protected_profile_is_protected_too(evals_on):
    guard = model_ranch.load_guard(now=NOW)
    arn = "arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/maker.default-v1:0"
    assert guard.gated(scored(arn, daily=-500)) is False


# --- the switch, the reads and failing open -------------------------------------------------------


def test_with_evals_off_nothing_is_filtered_and_nothing_is_read(monkeypatch):
    monkeypatch.setenv("EVALS_ENABLED", "false")
    reads = []

    def refuse(*args, **kwargs):
        # Recorded first: load_guard's fail-open `except Exception` would swallow the raise alone.
        reads.append(args)
        raise AssertionError("read while evals are off")

    monkeypatch.setattr(evals_settings, "get_settings", refuse)
    monkeypatch.setattr(model_ranch.dynamo, "get_model_config", refuse)
    models = [scored("maker.bad-v1:0", daily=-500), scored("maker.good-v1:0")]

    assert model_ranch.filter_healthy(models) == models
    guard = model_ranch.load_guard()
    assert guard.on is False and guard.gated(models[0]) is False
    assert reads == []


def test_the_guard_reads_the_evals_row_and_the_default_row_once_and_takes_a_row_it_is_given(evals_on):
    guard = model_ranch.load_guard(now=NOW)
    models = [scored(f"maker.m{n}-v1:0", daily=-500 if n % 2 else 0) for n in range(6)]

    kept = [m["model_id"] for m in guard.healthy(models)]
    assert kept == ["maker.m0-v1:0", "maker.m2-v1:0", "maker.m4-v1:0"]
    assert evals_on == ["evals", "default"]
    model_ranch.load_guard({"model_id": "maker.m1-v1:0"}, now=NOW)
    assert evals_on == ["evals", "default", "evals"]


def test_settings_that_cannot_be_read_gate_nothing(evals_on, monkeypatch, capsys):
    def broken():
        raise RuntimeError("throttled")

    monkeypatch.setattr(evals_settings, "get_settings", broken)
    models = [scored("maker.bad-v1:0", daily=-500)]

    assert model_ranch.filter_healthy(models) == models
    assert "nothing is gated" in capsys.readouterr().out


def test_a_default_row_that_cannot_be_read_gates_nothing(evals_on, monkeypatch):
    def broken():
        raise RuntimeError("throttled")

    monkeypatch.setattr(model_ranch.dynamo, "get_model_config", broken)
    assert model_ranch.load_guard().on is False


def test_a_model_with_no_row_is_never_gated(evals_on):
    assert model_ranch.load_guard(now=NOW).gated(None) is False


# --- the line a protected model's serving leaves --------------------------------------------------


def test_serving_a_gated_protected_model_prints_one_line_of_ids_and_codes(evals_on, capsys):
    guard = model_ranch.load_guard(now=NOW)
    default = scored("maker.default-v1:0", daily=-500, weekly=-500)

    guard.served("maker.default-v1:0", default, caller="routing", topic_id="topic-a")

    [line] = lines(capsys)
    assert {key: line[key] for key in ("model_id", "role", "slot", "windows", "caller", "topic_id")} == {
        "model_id": "maker.default-v1:0", "role": "default", "slot": "model", "windows": ["daily", "weekly"],
        "caller": "routing", "topic_id": "topic-a",
    }  # fmt: skip
    assert "dropped" not in line
    assert "maker.default-v1:0" not in json.dumps({k: v for k, v in line.items() if k != "model_id"})


def test_a_topics_own_model_is_named_by_the_role_given_and_a_healthy_one_leaves_no_line(evals_on, capsys):
    guard = model_ranch.load_guard(now=NOW)

    own = scored("maker.own-v1:0", total=-2000)
    guard.served("maker.own-v1:0", own, caller="routing", role="topic_model")
    guard.served("maker.fine-v1:0", scored("maker.fine-v1:0"), caller="routing", role="topic_model")
    guard.served("maker.none-v1:0", None, caller="routing", role="topic_model")

    [line] = lines(capsys)
    assert (line["model_id"], line["role"], line["windows"]) == ("maker.own-v1:0", "topic_model", ["total"])
