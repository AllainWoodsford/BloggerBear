from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
import requests

from common.http_retry import get_json_with_backoff


def _response(status: int = 200, payload=None, headers: dict | None = None) -> Mock:
    response = Mock()
    response.status_code = status
    response.headers = headers or {}
    response.json = Mock(return_value=payload)
    if status >= 400:
        response.raise_for_status = Mock(side_effect=requests.HTTPError(f"{status}", response=response))
    else:
        response.raise_for_status = Mock()
    return response


@pytest.fixture(autouse=True)
def _deterministic_jitter(monkeypatch):
    # Jitter picks a delay in [backoff/2, backoff]; pin it to the top so the
    # exponential schedule is exactly base * 2^(attempt-1).
    monkeypatch.setattr("common.http_retry.random.uniform", lambda low, high: high)


def _call(responses, **kwargs):
    http_get = Mock(side_effect=responses)
    sleeps: list[float] = []
    result = get_json_with_backoff(
        "https://example.test/x", http_get=http_get, sleep=sleeps.append, **kwargs
    )
    return result, http_get, sleeps


def test_returns_parsed_json_without_sleeping():
    result, http_get, sleeps = _call([_response(200, {"ok": True})])

    assert result == {"ok": True}
    assert http_get.call_count == 1
    assert sleeps == []


def test_retries_429_with_exponential_backoff_then_succeeds():
    result, http_get, sleeps = _call(
        [_response(429), _response(429), _response(200, {"ok": 1})], base_delay=2.0
    )

    assert result == {"ok": 1}
    assert http_get.call_count == 3
    assert sleeps == [2.0, 4.0]


def test_honors_retry_after_when_longer_than_backoff():
    _, _, sleeps = _call(
        [_response(429, headers={"Retry-After": "9"}), _response(200, {})], base_delay=1.0
    )

    assert sleeps == [9.0]


def test_retry_after_is_capped_at_max_delay():
    _, _, sleeps = _call(
        [_response(429, headers={"Retry-After": "600"}), _response(200, {})],
        base_delay=1.0,
        max_delay=20.0,
    )

    assert sleeps == [20.0]


def test_retries_5xx_and_connection_errors_and_timeouts():
    _, http_get, sleeps = _call(
        [
            _response(503),
            requests.ConnectionError("reset"),
            requests.Timeout("slow"),
            _response(200, {"ok": 1}),
        ],
        max_attempts=4,
        base_delay=1.0,
    )

    assert http_get.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_raises_the_last_error_once_attempts_are_exhausted():
    http_get = Mock(side_effect=[_response(429)] * 3)
    sleeps: list[float] = []

    with pytest.raises(requests.HTTPError):
        get_json_with_backoff(
            "https://example.test/x", http_get=http_get, sleep=sleeps.append, max_attempts=3
        )

    assert http_get.call_count == 3
    assert len(sleeps) == 2  # no sleep after the final failed attempt


def test_non_retryable_client_error_raises_immediately():
    http_get = Mock(side_effect=[_response(404)])
    sleeps: list[float] = []

    with pytest.raises(requests.HTTPError):
        get_json_with_backoff("https://example.test/x", http_get=http_get, sleep=sleeps.append)

    assert http_get.call_count == 1
    assert sleeps == []


def test_deadline_stops_retrying_instead_of_sleeping_past_it():
    http_get = Mock(side_effect=[_response(429), _response(200, {})])
    sleeps: list[float] = []

    with pytest.raises(requests.HTTPError):
        get_json_with_backoff(
            "https://example.test/x",
            http_get=http_get,
            sleep=sleeps.append,
            base_delay=30.0,
            deadline=time.monotonic() + 1.0,
        )

    assert http_get.call_count == 1
    assert sleeps == []


def test_passes_params_headers_and_timeout_through():
    http_get = Mock(return_value=_response(200, {}))

    get_json_with_backoff(
        "https://example.test/x",
        params={"a": 1},
        headers={"User-Agent": "t"},
        timeout=7.0,
        http_get=http_get,
    )

    http_get.assert_called_once_with(
        "https://example.test/x", params={"a": 1}, headers={"User-Agent": "t"}, timeout=7.0
    )
