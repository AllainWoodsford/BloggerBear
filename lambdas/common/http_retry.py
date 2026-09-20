"""GET-JSON with exponential backoff, for flaky/rate-limited public APIs.

Shared by the CoinGecko-backed crypto adapter and the web-search providers
(both hit free public APIs that answer 429 when hit too fast). Retries on
429/5xx and on connection errors/timeouts, sleeping with exponential
backoff plus jitter and honoring a server-sent `Retry-After`. Any other
4xx is a caller error and raises immediately -- retrying it can't help.

`http_get` and `sleep` are injectable so tests never touch the network or
actually wait.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import requests

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form -- not worth parsing, fall back to backoff


def get_json_with_backoff(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10.0,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    deadline: float | None = None,
    http_get: Callable[..., requests.Response] | None = None,
    sleep: Callable[[float], None] = time.sleep,
):
    """GET `url` and return its parsed JSON, retrying transient failures.

    `deadline` is an absolute `time.monotonic()` value: once the next
    backoff sleep would pass it, the last error is raised instead of
    waiting -- so a caller with a total time budget (a Lambda timeout)
    fails fast rather than sleeping past it.
    """
    do_get = http_get or requests.get
    attempt = 0
    while True:
        attempt += 1
        retry_after: float | None = None
        try:
            response = do_get(url, params=params, headers=headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            failure: Exception = exc
        else:
            if response.status_code not in RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                return response.json()
            retry_after = _retry_after_seconds(response)
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                failure = exc

        if attempt >= max_attempts:
            raise failure

        backoff = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay = random.uniform(backoff / 2, backoff)
        if retry_after is not None:
            delay = max(delay, min(retry_after, max_delay))

        if deadline is not None and time.monotonic() + delay > deadline:
            raise failure

        sleep(delay)
