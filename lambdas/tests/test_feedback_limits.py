"""Tests for common/feedback_limits.py: when BloggerBear is, and isn't, taking feedback."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from common import dynamo
from common import feedback_limits as fl

REGION = "ap-southeast-2"

# 12:00 Sydney (UTC+10 in September, before daylight saving starts in October).
NOON = datetime(2026, 9, 21, 2, 0, 0, tzinfo=UTC)
WINDOW_KEY = f"feedback-window#300#{int(NOON.timestamp()) // 300}"


@pytest.fixture(autouse=True)
def tables(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("ARTICLES_TABLE", "Articles")
    monkeypatch.setenv("MODEL_CONFIG_TABLE", "ModelConfig")
    dynamo._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName="Articles",
            KeySchema=[{"AttributeName": "article_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "article_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _article(article_id="a1", **extra):
    item = {"article_id": article_id, "status": "published", **extra}
    boto3.resource("dynamodb", region_name=REGION).Table("Articles").put_item(Item=item)
    return dynamo.get_article(article_id)


def _stored_article(article_id="a1"):
    return dynamo.get_article(article_id)


def _configure(**settings):
    dynamo.put_feedback_config(settings)


def _counter_rows():
    table = boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig")
    return {
        item["config_id"]: int(item["count"])
        for item in table.scan()["Items"]
        if item["config_id"].startswith("feedback-")
    }


# --- settings -----------------------------------------------------------------------------


def test_defaults_when_nothing_is_configured():
    assert fl.effective_settings(None) == {
        "locked_down": False,
        "lockdown_reason": None,
        "rate_limit_count": 20,
        "rate_limit_window_minutes": 5,
        "daily_limit": 100,
        "article_limit": 50,
        "screening_limit": 300,
        "verification_required": True,
        "token_delay_min_ms": 500,
        "token_delay_max_ms": 2000,
        "pow_threshold_percent": 70,
        "pow_difficulty_bits": 16,
        "daily_timezone": "Australia/Sydney",
    }


def test_configured_values_are_used():
    _configure(
        rate_limit_count=3,
        rate_limit_window_minutes=10,
        daily_limit=7,
        article_limit=2,
        screening_limit=11,
        token_delay_min_ms=100,
        token_delay_max_ms=300,
        pow_threshold_percent=40,
        pow_difficulty_bits=12,
        verification_required=False,
        locked_down=True,
        lockdown_reason="  Back soon  ",
        daily_timezone="UTC",
    )

    assert fl.effective_settings(dynamo.get_feedback_config()) == {
        "locked_down": True,
        "lockdown_reason": "Back soon",
        "rate_limit_count": 3,
        "rate_limit_window_minutes": 10,
        "daily_limit": 7,
        "article_limit": 2,
        "screening_limit": 11,
        "verification_required": False,
        "token_delay_min_ms": 100,
        "token_delay_max_ms": 300,
        "pow_threshold_percent": 40,
        "pow_difficulty_bits": 12,
        "daily_timezone": "UTC",
    }


@pytest.mark.parametrize(
    "row",
    [
        {"rate_limit_count": 0},
        {"rate_limit_count": -5},
        {"rate_limit_count": "20"},
        {"rate_limit_count": 2.5},
        {"rate_limit_count": True},
        {"rate_limit_count": 10**9},
        {"rate_limit_window_minutes": 0},
        {"rate_limit_window_minutes": 5000},
        {"daily_limit": "many"},
        {"article_limit": None},
        {"screening_limit": 0},
        {"screening_limit": "300"},
        {"daily_timezone": "Mars/Olympus"},
        {"daily_timezone": 5},
        {"lockdown_reason": "   "},
        {"lockdown_reason": 5},
    ],
)
def test_an_invalid_stored_value_takes_the_default_never_trusted(row):
    assert fl.effective_settings(row) == fl.effective_settings(None)


@pytest.mark.parametrize("value", [True, "true", "TRUE", " yes ", "on", "1", 1, Decimal(1)])
def test_a_hand_edited_lock_flag_that_means_yes_still_locks(value):
    assert fl.effective_settings({"locked_down": value})["locked_down"] is True
    article = {"article_id": "a", "feedback_locked": value}
    assert fl.status_for(article, NOON)["reason"] == fl.ARTICLE_LOCKED


@pytest.mark.parametrize("value", [False, "false", "no", "", 0, None, 2, "maybe"])
def test_other_values_do_not_lock(value):
    assert fl.effective_settings({"locked_down": value})["locked_down"] is False


def test_settings_error_validation():
    assert fl.settings_error("rate_limit_count", None) is None  # None clears a setting
    assert fl.settings_error("rate_limit_count", 20) is None
    assert fl.settings_error("rate_limit_window_minutes", 1440) is None
    assert fl.settings_error("rate_limit_window_minutes", 1441)
    for bad in (0, -1, 2.5, "5", True):
        assert fl.settings_error("daily_limit", bad)
    assert fl.settings_error("locked_down", True) is None
    assert fl.settings_error("locked_down", "true")
    assert fl.settings_error("lockdown_reason", "Maintenance") is None
    assert fl.settings_error("lockdown_reason", "x" * 101)
    assert fl.settings_error("lockdown_reason", "  ")
    assert fl.settings_error("daily_timezone", "Australia/Sydney") is None
    assert fl.settings_error("daily_timezone", "Nowhere/Land")


# --- status, and what takes precedence ---------------------------------------------------------


def test_open_by_default():
    assert fl.status_for(_article(), NOON) == {
        "open": True,
        "reason": None,
        "label": None,
        "retry_at": None,
    }


def test_a_locked_article_is_closed_whatever_else_is_true():
    _configure(locked_down=True)
    article = _article(feedback_locked=True)

    status = fl.status_for(article, NOON)

    # The article lock wins over the site-wide lockdown.
    assert status["open"] is False
    assert status["reason"] == fl.ARTICLE_LOCKED
    assert status["label"] == "This article is locked"


def test_an_article_at_its_limit_says_so():
    _configure(article_limit=3)

    assert fl.status_for(_article(feedback_count=3), NOON)["reason"] == fl.ARTICLE_LIMIT
    assert fl.status_for(_article(feedback_count=2), NOON)["open"] is True
    # Locked by reaching the limit reads as the limit; locked by hand, as a lock.
    assert (
        fl.status_for(_article(feedback_locked=True, feedback_count=3), NOON)["reason"]
        == fl.ARTICLE_LIMIT
    )
    assert (
        fl.status_for(_article(feedback_locked=True, feedback_count=1), NOON)["reason"]
        == fl.ARTICLE_LOCKED
    )


def test_lockdown_beats_the_limits_and_can_carry_a_reason():
    _configure(locked_down=True, lockdown_reason="Sharpening pencils", daily_limit=1)

    status = fl.status_for(_article(), NOON)

    assert status["reason"] == fl.LOCKDOWN
    assert status["label"] == "Sharpening pencils"
    assert status["retry_at"] is None  # a lockdown reopens when you say so


def test_lockdown_without_a_reason_uses_the_default_label():
    _configure(locked_down=True)

    assert fl.status_for(_article(), NOON)["label"] == "Feedback is paused"


def test_the_daily_limit_beats_the_rate_limit_and_reopens_at_the_next_day_in_sydney():
    _configure(daily_limit=2, rate_limit_count=2)
    article = _article()
    assert fl.acquire(article, NOON)["open"]
    assert fl.acquire(article, NOON)["open"]

    status = fl.status_for(_article(), NOON)

    assert status["reason"] == fl.DAILY_LIMIT
    assert status["label"] == "Daily limit reached"
    # Midnight at the end of 21 September, Sydney time (UTC+10) = 14:00 UTC.
    assert status["retry_at"] == "2026-09-21T14:00:00+00:00"


def test_the_rate_limit_reopens_when_its_window_ends():
    _configure(rate_limit_count=2, rate_limit_window_minutes=5)
    article = _article()
    assert fl.acquire(article, NOON)["open"]
    assert fl.acquire(article, NOON)["open"]

    status = fl.status_for(article, NOON)

    assert status["reason"] == fl.RATE_LIMIT
    assert status["label"] == "Rate limit"
    assert status["retry_at"] == "2026-09-21T02:05:00+00:00"  # the window is 02:00-02:05
    # Once that time has come the same article is open again.
    assert fl.status_for(article, NOON + timedelta(minutes=5))["open"] is True


# --- taking feedback ---------------------------------------------------------------------------


def test_acquire_counts_against_the_article_the_day_and_the_window():
    article = _article()

    assert fl.acquire(article, NOON)["open"] is True
    assert fl.acquire(article, NOON)["open"] is True

    assert int(_stored_article()["feedback_count"]) == 2
    assert _counter_rows() == {"feedback-day#2026-09-21": 2, WINDOW_KEY: 2}


def test_the_default_rate_limit_is_twenty_per_five_minutes():
    article = _article(article_id="a1")
    results = [fl.acquire(_stored_article("a1"), NOON)["open"] for _ in range(21)]

    assert results == [True] * 20 + [False]
    # The refused one used up nothing.
    assert int(_stored_article()["feedback_count"]) == 20
    assert fl.status_for(article, NOON)["reason"] == fl.RATE_LIMIT
    # The next window: open again.
    assert fl.acquire(_stored_article(), NOON + timedelta(minutes=5))["open"] is True


def test_the_default_daily_limit_is_one_hundred_and_resets_at_midnight_sydney():
    _configure(rate_limit_count=1000, article_limit=1000)
    for n in range(100):
        assert fl.acquire(_article(f"art{n}"), NOON)["open"], n

    refused = fl.acquire(_article("late"), NOON)

    assert refused["reason"] == fl.DAILY_LIMIT
    # 23:59 Sydney is still the same day...
    just_before = datetime(2026, 9, 21, 13, 59, tzinfo=UTC)
    assert fl.status_for(_article("late"), just_before)["reason"] == fl.DAILY_LIMIT
    # ...and 00:00 Sydney is a new one.
    midnight = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    assert fl.acquire(_article("late"), midnight)["open"] is True


def test_the_day_follows_sydney_daylight_saving():
    _configure(daily_limit=1)
    # 2026-10-04 is when Sydney moves to UTC+11: a day that starts at 13:00 UTC the day before.
    after_change = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # 23:00 Sydney on the 4th
    assert fl.acquire(_article(), after_change)["open"] is True

    status = fl.status_for(_article(), after_change)

    assert status["reason"] == fl.DAILY_LIMIT
    assert status["retry_at"] == "2026-10-04T13:00:00+00:00"  # midnight at UTC+11


def test_the_day_can_be_read_in_another_zone():
    _configure(daily_limit=1, daily_timezone="UTC")
    assert fl.acquire(_article(), NOON)["open"] is True

    assert fl.status_for(_article(), NOON)["retry_at"] == "2026-09-22T00:00:00+00:00"


def test_an_article_locks_when_it_reaches_its_limit():
    _configure(article_limit=3)
    article = _article()

    assert [fl.acquire(_stored_article(), NOON)["open"] for _ in range(3)] == [True] * 3

    stored = _stored_article()
    assert stored["feedback_locked"] is True  # the flag now says so
    assert int(stored["feedback_count"]) == 3
    refused = fl.acquire(article, NOON)
    assert refused["open"] is False and refused["reason"] == fl.ARTICLE_LIMIT
    assert int(_stored_article()["feedback_count"]) == 3  # the refused one counted nothing


def test_the_article_limit_is_per_article():
    _configure(article_limit=1)

    assert fl.acquire(_article("a"), NOON)["open"] is True
    assert fl.acquire(_article("b"), NOON)["open"] is True
    assert fl.acquire(_stored_article("a"), NOON)["open"] is False


def test_a_manual_lock_stops_feedback_without_counting():
    article = _article(feedback_locked=True)

    result = fl.acquire(article, NOON)

    assert result["reason"] == fl.ARTICLE_LOCKED
    assert _counter_rows() == {}
    assert "feedback_count" not in _stored_article()


def test_unlocking_by_hand_reopens_an_article_locked_by_hand():
    _article(feedback_locked=True)
    assert fl.acquire(_stored_article(), NOON)["open"] is False

    dynamo.set_article_feedback_lock("a1", False)

    assert fl.acquire(_stored_article(), NOON)["open"] is True


def test_an_article_locked_by_its_limit_needs_the_count_reset_too():
    _configure(article_limit=1)
    _article()
    assert fl.acquire(_stored_article(), NOON)["open"] is True

    dynamo.set_article_feedback_lock("a1", False)  # the flag alone: still at its limit
    assert fl.acquire(_stored_article(), NOON)["reason"] == fl.ARTICLE_LIMIT

    dynamo.set_article_feedback_lock("a1", False, reset_count=True)
    assert int(_stored_article()["feedback_count"]) == 0
    assert fl.acquire(_stored_article(), NOON)["open"] is True


def test_raising_the_article_limit_reopens_a_full_article():
    _configure(article_limit=1)
    _article()
    assert fl.acquire(_stored_article(), NOON)["open"] is True
    assert fl.acquire(_stored_article(), NOON)["open"] is False

    _configure(article_limit=5)
    dynamo.set_article_feedback_lock("a1", False)

    assert fl.acquire(_stored_article(), NOON)["open"] is True


# --- a refusal gives back what an earlier check took ---------------------------------------------


def test_a_refusal_by_the_daily_limit_gives_back_the_article_count():
    _configure(daily_limit=1, article_limit=10)
    assert fl.acquire(_article("a"), NOON)["open"] is True
    other = _article("b")

    result = fl.acquire(other, NOON)

    assert result["reason"] == fl.DAILY_LIMIT
    assert "feedback_count" not in _stored_article("b") or int(_stored_article("b")["feedback_count"]) == 0
    assert _counter_rows() == {"feedback-day#2026-09-21": 1, WINDOW_KEY: 1}


def test_a_refusal_by_the_rate_limit_gives_back_the_article_and_day_counts():
    _configure(rate_limit_count=1, article_limit=10)
    assert fl.acquire(_article("a"), NOON)["open"] is True
    # Racing past the read-only check: the atomic step is what refuses.
    other = _article("b")
    with patch.object(fl, "status_for", return_value=dict(fl._OPEN)):
        result = fl.acquire(other, NOON)

    assert result["reason"] == fl.RATE_LIMIT
    assert int(_stored_article("b").get("feedback_count", 0)) == 0
    assert _counter_rows() == {"feedback-day#2026-09-21": 1, WINDOW_KEY: 1}


def test_the_atomic_step_refuses_the_one_past_the_limit_even_after_a_clean_read():
    _configure(article_limit=1)
    stale = _article()
    with patch.object(fl, "status_for", return_value=dict(fl._OPEN)):
        assert fl.acquire(stale, NOON)["open"] is True
        # Both callers read "open" before either wrote; only one gets in.
        second = fl.acquire(stale, NOON)

    assert second["open"] is False
    assert int(_stored_article()["feedback_count"]) == 1


def test_counters_are_conditional_and_atomic():
    key = "feedback-day#test"
    assert [dynamo.consume_feedback_counter(key, 3, 9999999999) for _ in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert dynamo.get_feedback_counter(key) == 3
    dynamo.refund_feedback_counter(key)
    assert dynamo.get_feedback_counter(key) == 2
    for _ in range(5):
        dynamo.refund_feedback_counter(key)
    assert dynamo.get_feedback_counter(key) == 0  # never below zero


def test_counter_rows_carry_a_ttl_the_table_can_expire():
    fl.acquire(_article(), NOON)

    table = boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig")
    rows = [i for i in table.scan()["Items"] if i["config_id"].startswith("feedback-")]
    assert len(rows) == 2
    assert all(int(row["expires_at"]) > NOON.timestamp() for row in rows)
    # The settings row never expires.
    dynamo.put_feedback_config({"daily_limit": 5})
    assert "expires_at" not in table.get_item(Key={"config_id": "feedback"})["Item"]


# --- failing safe --------------------------------------------------------------------------------


def test_status_is_unavailable_not_open_if_the_limiter_cannot_be_read():
    with patch.object(fl, "get_feedback_config", side_effect=RuntimeError("dynamodb down")):
        status = fl.status_for(_article(), NOON)

    assert status["open"] is False
    assert status["reason"] == fl.UNAVAILABLE


def test_acquire_refuses_and_counts_nothing_if_a_write_fails():
    article = _article()
    with patch.object(fl, "consume_feedback_counter", side_effect=RuntimeError("boom")):
        result = fl.acquire(article, NOON)

    assert result["open"] is False and result["reason"] == fl.UNAVAILABLE
    # The article count taken before the failure was given back.
    assert int(_stored_article().get("feedback_count", 0)) == 0


def test_a_failed_lock_write_does_not_undo_an_accepted_submission():
    _configure(article_limit=1)
    article = _article()
    with patch.object(fl, "set_article_feedback_lock", side_effect=RuntimeError("boom")):
        result = fl.acquire(article, NOON)

    assert result["open"] is True  # it was counted and stays counted
    assert int(_stored_article()["feedback_count"]) == 1
    # And the next one is refused anyway: the article is at its limit.
    assert fl.acquire(_stored_article(), NOON)["open"] is False


def test_a_missing_article_row_is_refused():
    ghost = {"article_id": "ghost", "status": "published"}

    assert fl.acquire(ghost, NOON)["open"] is False
    assert _counter_rows() == {}


def test_stored_counts_as_decimals_or_junk_do_not_break_status():
    assert fl.status_for({"article_id": "x", "feedback_count": Decimal(50)}, NOON)["reason"] == (
        fl.ARTICLE_LIMIT
    )
    assert fl.status_for({"article_id": "x", "feedback_count": "lots"}, NOON)["open"] is True
    assert fl.status_for({"article_id": "x", "feedback_count": True}, NOON)["open"] is True


# --- usage (the admin view) ------------------------------------------------------------------


def test_usage_reports_todays_and_this_windows_counts():
    _configure(daily_limit=10, rate_limit_count=4)
    article = _article()
    fl.acquire(article, NOON)
    fl.acquire(article, NOON)

    assert fl.usage(NOON) == {
        "today": 2,
        "daily_limit": 10,
        "screened_today": 0,
        "screening_limit": 300,
        "day_ends_at": "2026-09-21T14:00:00+00:00",
        "this_window": 2,
        "rate_limit_count": 4,
        "window_ends_at": "2026-09-21T02:05:00+00:00",
    }


# --- the model-check budget: what bounds the cost of rejected comments -----------------------


def test_a_whole_decimal_from_dynamodb_is_a_valid_setting():
    assert fl.effective_settings({"screening_limit": Decimal(7)})["screening_limit"] == 7
    assert fl.effective_settings({"screening_limit": Decimal("7.5")})["screening_limit"] == 300


def test_screening_slots_run_out_at_the_screening_limit():
    _configure(screening_limit=3)

    assert [fl.take_screening_slot(NOON) for _ in range(5)] == [True, True, True, False, False]
    assert fl.usage(NOON)["screened_today"] == 3


def test_the_default_screening_budget_is_three_hundred_a_day():
    assert [fl.take_screening_slot(NOON) for _ in range(301)].count(True) == 300


def test_screening_slots_reset_with_the_day_in_sydney():
    _configure(screening_limit=1)
    assert fl.take_screening_slot(NOON) is True
    assert fl.take_screening_slot(NOON) is False

    assert fl.take_screening_slot(datetime(2026, 9, 21, 14, 0, tzinfo=UTC)) is True  # 00:00 Sydney


def test_screening_slots_do_not_touch_the_feedback_counters():
    fl.take_screening_slot(NOON)

    assert list(_counter_rows()) == ["feedback-screen#2026-09-21"]
    assert fl.status_for(_article(), NOON)["open"] is True


def test_model_checks_used_count_towards_how_busy_the_site_is():
    """Rejected feedback counts against neither the daily nor the rate limit, so a stream of
    comments that all get dropped would never trigger proof-of-work without this."""
    _configure(screening_limit=10)
    settings = fl.effective_settings(dynamo.get_feedback_config())
    assert fl.load_percent(settings, NOON) == 0

    for _ in range(7):
        fl.take_screening_slot(NOON)

    assert fl.load_percent(settings, NOON) == 70
    assert fl.status_for(_article(), NOON)["open"] is True  # busy, not closed


def test_the_busiest_counter_decides_the_load():
    _configure(screening_limit=10, daily_limit=4)
    settings = fl.effective_settings(dynamo.get_feedback_config())
    fl.take_screening_slot(NOON)  # 10% of the model checks
    fl.acquire(_article(), NOON)  # 25% of the day

    assert fl.load_percent(settings, NOON) == 25


def test_no_screening_slot_if_the_counter_cannot_be_used():
    with patch.object(fl, "consume_feedback_counter", side_effect=RuntimeError("boom")):
        assert fl.take_screening_slot(NOON) is False
