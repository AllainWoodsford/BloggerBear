"""Tests for common/feedback_verification.py: the signed, one-use, not-before token."""

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from common import dynamo
from common import feedback_limits as fl
from common import feedback_verification as fv

REGION = "ap-southeast-2"
NOW = datetime(2026, 9, 21, 2, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MODEL_CONFIG_TABLE", "ModelConfig")
    dynamo._dynamodb_resource = None
    fv._secret_cache = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _settings(**overrides):
    row = {"token_delay_min_ms": 0, "token_delay_max_ms": 0, **overrides}
    return fl.effective_settings(row)


def _payload(token):
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _solve(token, bits):
    nonce = 0
    while not fv.work_is_valid(token, nonce, bits):
        nonce += 1
    return nonce


def _rows(prefix):
    scan = boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig").scan()["Items"]
    return [row for row in scan if row["config_id"].startswith(prefix)]


# --- issuing ------------------------------------------------------------------------------------


def test_a_token_carries_only_the_article_times_a_random_value_and_the_work_needed():
    issued = fv.issue("art-1", _settings(), NOW)

    assert set(issued) == {"token", "wait_ms", "pow_bits"}
    payload = _payload(issued["token"])
    assert set(payload) == {"a", "i", "nb", "e", "n", "d"}  # nothing about a visitor
    assert payload["a"] == "art-1" and payload["d"] == 0
    assert payload["e"] - payload["i"] == fv.TOKEN_TTL_SECONDS * 1000


def test_the_not_before_time_is_random_within_the_configured_window():
    settings = _settings(token_delay_min_ms=500, token_delay_max_ms=2000)

    delays = set()
    for _ in range(60):
        issued = fv.issue("art-1", settings, NOW)
        payload = _payload(issued["token"])
        delay = payload["nb"] - payload["i"]
        assert 500 <= delay <= 2000
        assert issued["wait_ms"] == delay
        delays.add(delay)

    assert len(delays) > 10  # random, not a constant


def test_the_default_window_is_half_a_second_to_two_seconds():
    settings = fl.effective_settings(None)

    assert (settings["token_delay_min_ms"], settings["token_delay_max_ms"]) == (500, 2000)


def test_a_minimum_above_the_maximum_falls_back_to_the_default_window():
    settings = fl.effective_settings({"token_delay_min_ms": 5000, "token_delay_max_ms": 100})

    assert (settings["token_delay_min_ms"], settings["token_delay_max_ms"]) == (500, 2000)


def test_no_token_is_issued_when_verification_is_off():
    assert fv.issue("art-1", _settings(verification_required=False), NOW) is None


def test_only_an_explicit_false_switches_verification_off():
    for value in (False, "false", "NO", "off", "0"):
        assert fl.effective_settings({"verification_required": value})["verification_required"] is False
    for value in (True, "true", None, "", "maybe", 1, 5):
        assert fl.effective_settings({"verification_required": value})["verification_required"] is True


def test_work_is_asked_for_only_when_the_site_is_busy():
    fl_settings = _settings(rate_limit_count=10, pow_threshold_percent=50, pow_difficulty_bits=9)
    assert fv.issue("a", fl_settings, NOW)["pow_bits"] == 0  # quiet

    table = boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig")
    window_key, _ = fl._window(fl_settings, NOW)
    table.put_item(Item={"config_id": window_key, "count": 5})  # 5 of 10: 50%
    assert fv.issue("a", fl_settings, NOW)["pow_bits"] == 9

    table.put_item(Item={"config_id": window_key, "count": 4})  # 40%
    assert fv.issue("a", fl_settings, NOW)["pow_bits"] == 0


def test_the_daily_counter_also_triggers_work():
    settings = _settings(daily_limit=10, pow_threshold_percent=70, pow_difficulty_bits=8)
    day_key, _ = fl._day(settings, NOW)
    boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig").put_item(
        Item={"config_id": day_key, "count": 7}
    )

    assert fv.issue("a", settings, NOW)["pow_bits"] == 8


def test_zero_bits_never_asks_for_work_however_busy_the_site_is():
    settings = _settings(rate_limit_count=1, pow_threshold_percent=1, pow_difficulty_bits=0)
    window_key, _ = fl._window(settings, NOW)
    boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig").put_item(
        Item={"config_id": window_key, "count": 1}
    )

    assert fv.issue("a", settings, NOW)["pow_bits"] == 0


# --- verifying ----------------------------------------------------------------------------------


def _fresh(article="art-1", **overrides):
    settings = _settings(**overrides)
    return settings, fv.issue(article, settings, NOW)["token"]


def test_a_good_token_is_accepted_once():
    settings, token = _fresh()

    assert fv.verify("art-1", token, None, settings, NOW)["ok"] is True
    second = fv.verify("art-1", token, None, settings, NOW)
    assert second == {"ok": False, "reason": fv.USED, "retry_after_ms": None}


def test_a_used_token_is_recorded_without_anything_about_the_visitor_and_expires():
    settings, token = _fresh()
    fv.verify("art-1", token, None, settings, NOW)

    (row,) = _rows("nonce#")
    assert set(row) == {"config_id", "expires_at"}
    assert int(row["expires_at"]) > int(_payload(token)["e"]) // 1000  # outlives the token a little


@pytest.mark.parametrize("token", [None, "", 5, ["x"], {"a": 1}])
def test_a_missing_or_wrong_typed_token_fails(token):
    settings = _settings()

    result = fv.verify("art-1", token, None, settings, NOW)

    assert result["ok"] is False and result["reason"] == fv.MISSING


@pytest.mark.parametrize(
    "token",
    ["garbage", "a.b", "a.b.c.d", "v1..", "v1.!!!.???", "v2.abc.def", "v1.e30.AAAA"],
)
def test_a_malformed_or_unsigned_token_is_invalid(token):
    result = fv.verify("art-1", token, None, _settings(), NOW)

    assert result["ok"] is False and result["reason"] == fv.INVALID


def test_a_forged_signature_is_invalid():
    settings, token = _fresh()
    version, body, signature = token.split(".")
    forged = f"{version}.{body}.{'A' * len(signature)}"

    assert fv.verify("art-1", forged, None, settings, NOW)["reason"] == fv.INVALID


def test_editing_the_payload_breaks_the_signature():
    settings, token = _fresh()
    version, body, signature = token.split(".")
    payload = _payload(token)
    payload["nb"] = 0  # try to make it valid earlier
    payload["d"] = 0
    edited = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    assert fv.verify("art-1", f"{version}.{edited}.{signature}", None, settings, NOW)["reason"] == (
        fv.INVALID
    )


def test_a_token_for_one_article_is_no_good_for_another():
    settings, token = _fresh("art-1")

    result = fv.verify("art-2", token, None, settings, NOW)

    assert result["reason"] == fv.WRONG_ARTICLE
    # ...and the wrong-article attempt did not spend it.
    assert fv.verify("art-1", token, None, settings, NOW)["ok"] is True


def test_a_token_expires():
    settings, token = _fresh()

    late = NOW + timedelta(seconds=fv.TOKEN_TTL_SECONDS, milliseconds=1)
    assert fv.verify("art-1", token, None, settings, late)["reason"] == fv.EXPIRED
    just_in_time = NOW + timedelta(seconds=fv.TOKEN_TTL_SECONDS - 1)
    assert fv.verify("art-1", token, None, settings, just_in_time)["ok"] is True


def test_a_token_is_not_valid_before_its_moment_and_says_how_long_to_wait():
    settings = _settings(token_delay_min_ms=1500, token_delay_max_ms=1500)
    token = fv.issue("art-1", settings, NOW)["token"]

    early = fv.verify("art-1", token, None, settings, NOW + timedelta(milliseconds=200))
    assert early == {"ok": False, "reason": fv.TOO_EARLY, "retry_after_ms": 1300}
    # Too early does not spend the token: wait, and the same one works.
    assert fv.verify("art-1", token, None, settings, NOW + timedelta(milliseconds=1500))["ok"]


def test_an_instant_script_is_refused_and_a_person_never_notices():
    settings = _settings(token_delay_min_ms=500, token_delay_max_ms=2000)
    token = fv.issue("art-1", settings, NOW)["token"]

    assert fv.verify("art-1", token, None, settings, NOW + timedelta(milliseconds=50))["reason"] == (
        fv.TOO_EARLY
    )
    # Someone who read an article for even ten seconds is far past any delay.
    assert fv.verify("art-1", token, None, settings, NOW + timedelta(seconds=10))["ok"] is True


def test_verification_off_lets_anything_through_without_a_token():
    settings = _settings(verification_required=False)

    assert fv.verify("art-1", None, None, settings, NOW)["ok"] is True
    assert _rows("nonce#") == []


# --- proof of work -----------------------------------------------------------------------------


def _busy_settings(bits=8):
    return _settings(rate_limit_count=1, pow_threshold_percent=1, pow_difficulty_bits=bits)


def _busy_token(bits=8):
    settings = _busy_settings(bits)
    window_key, _ = fl._window(settings, NOW)
    boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig").put_item(
        Item={"config_id": window_key, "count": 1}
    )
    return settings, fv.issue("art-1", settings, NOW)["token"]


def test_a_token_that_needs_work_is_refused_without_it_and_with_the_wrong_answer():
    settings, token = _busy_token(10)
    assert _payload(token)["d"] == 10

    assert fv.verify("art-1", token, None, settings, NOW)["reason"] == fv.WORK
    assert fv.verify("art-1", token, "abc", settings, NOW)["reason"] == fv.WORK
    wrong = next(n for n in range(10**6) if not fv.work_is_valid(token, n, 10))
    assert fv.verify("art-1", token, wrong, settings, NOW)["reason"] == fv.WORK
    assert _rows("nonce#") == []  # a failed attempt does not spend the token


def test_the_right_work_is_accepted():
    settings, token = _busy_token(10)
    nonce = _solve(token, 10)

    assert fv.verify("art-1", token, nonce, settings, NOW)["ok"] is True
    # The work is for that token: the same answer does not carry over to a spent one.
    assert fv.verify("art-1", token, nonce, settings, NOW)["reason"] == fv.USED


def test_the_difficulty_is_inside_the_signed_token_so_it_cannot_be_lowered():
    settings, token = _busy_token(12)
    version, body, signature = token.split(".")
    payload = _payload(token)
    payload["d"] = 0
    edited = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()

    assert fv.verify("art-1", f"{version}.{edited}.{signature}", None, settings, NOW)["reason"] == (
        fv.INVALID
    )


def test_leading_zero_bits():
    assert fv.leading_zero_bits(b"\x00\x00\x10") == 19
    assert fv.leading_zero_bits(b"\xff") == 0
    assert fv.leading_zero_bits(b"\x0f") == 4
    assert fv.leading_zero_bits(b"\x00\x00\x00") == 24


@pytest.mark.parametrize(
    "nonce",
    [None, True, False, -5, "-5", "1.5", "abc", "", " 7", "٣", "9" * 25, [1], 1.5],
)
def test_work_only_accepts_plain_whole_numbers(nonce):
    assert fv.work_is_valid("token", nonce, 0) is False


def test_work_accepts_a_number_or_its_digits():
    nonce = _solve("token", 8)

    assert fv.work_is_valid("token", nonce, 8) is True
    assert fv.work_is_valid("token", str(nonce), 8) is True


# --- the signing key ---------------------------------------------------------------------------


def test_the_key_is_created_once_and_shared():
    first = dynamo.get_verification_secret()
    second = dynamo.get_verification_secret()

    assert first == second and len(first) == 64


def test_the_key_row_is_not_a_counter_and_is_only_created_when_absent():
    dynamo.get_verification_secret()
    table = boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig")
    table.put_item(Item={"config_id": "verification-secret", "secret": "kept"})

    assert dynamo.get_verification_secret() == "kept"


def test_deleting_the_key_row_rotates_it_and_old_tokens_stop_working():
    settings, token = _fresh()
    boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig").delete_item(
        Key={"config_id": "verification-secret"}
    )
    fv._secret_cache = None

    assert fv.verify("art-1", token, None, settings, NOW)["reason"] == fv.INVALID


# --- failing safe ------------------------------------------------------------------------------


def test_verification_fails_closed_if_the_key_cannot_be_read():
    settings = _settings()
    with patch.object(fv, "get_verification_secret", side_effect=RuntimeError("down")):
        fv._secret_cache = None
        result = fv.verify("art-1", "v1.abc.def", None, settings, NOW)

    assert result["ok"] is False and result["reason"] == fv.UNAVAILABLE


def test_verification_fails_closed_if_the_used_token_record_cannot_be_written():
    settings, token = _fresh()
    with patch.object(fv, "consume_verification_nonce", side_effect=RuntimeError("down")):
        result = fv.verify("art-1", token, None, settings, NOW)

    assert result["ok"] is False and result["reason"] == fv.UNAVAILABLE


def test_issuing_raises_if_the_key_cannot_be_read_so_the_caller_can_close_the_form():
    with patch.object(fv, "get_verification_secret", side_effect=RuntimeError("down")):
        fv._secret_cache = None
        with pytest.raises(RuntimeError):
            fv.issue("art-1", _settings(), NOW)
