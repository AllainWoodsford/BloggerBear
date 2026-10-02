"""Is this submission from a browser that asked for the form first, and waited a moment?

Nothing on the client can be trusted, and a script can call the API directly, so what this adds
is not a wall but a cost: a submission must carry a token that the server issued for that article
a moment earlier, that is good for one use, and (when the site is busy) that comes with a small
piece of proof-of-work. A bot can still get through, but it must make a request per submission,
wait, and (when busy) spend CPU on each one; a blind POST or an instant script cannot.

Stateless and anonymous. The token is an HMAC-signed blob with the article, the issue time, a
"not valid before" time, a random value and the proof-of-work difficulty. It holds nothing about
the visitor, is not a cookie, and nothing about the visitor is stored: the only thing written is
the random value, once, when the token is used (so it cannot be used twice), and it expires with
the token.

* Not before. The token is issued at once but is valid only from a random moment 0.5 to 2 seconds
  later (`token_delay_min_ms`/`token_delay_max_ms`), which the server enforces. Nothing sleeps
  and a human never notices. A fast client is told how long to wait and simply retries.
* Proof-of-work. When the site is at or past `pow_threshold_percent` of its daily limit, rate limit
  or daily model-check budget (feedback_limits.load_percent), tokens are issued needing a nonce so
  that SHA-256(token + ":" + nonce) has `pow_difficulty_bits` leading zero bits (about a second or
  two of browser CPU at 16). It asks nothing of the person: no clicking, nothing to read, nothing
  for a screen reader or a switch user to do. The trigger is the site's own counters, not anything
  observed about the visitor.
* A decoy field on the form (the honeypot, see public_api_handler.py) catches scripts that fill
  every input.

`verification_required` (default true) switches all of this off in an emergency.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime

from common import feedback_limits
from common.dynamo import consume_verification_nonce, get_verification_secret

TOKEN_TTL_SECONDS = 2 * 60 * 60  # long enough to read an article; the page fetches a fresh one
_VERSION = "v1"

# Why a submission failed verification. The client acts on these; nothing else is said.
MISSING = "missing"
INVALID = "invalid"
WRONG_ARTICLE = "wrong_article"
EXPIRED = "expired"
TOO_EARLY = "too_early"
WORK = "work"
USED = "used"
UNAVAILABLE = "unavailable"

_secret_cache: str | None = None


def _secret() -> bytes:
    global _secret_cache
    if _secret_cache is None:
        _secret_cache = get_verification_secret()
    return _secret_cache.encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def issue(article_id: str, settings: dict, now: datetime | None = None) -> dict | None:
    """A token for `article_id`, and what the browser must do with it:
    {token, wait_ms, pow_bits}. None if verification is switched off. Raises if the signing key
    cannot be read (the caller treats that as "unavailable")."""
    if not settings["verification_required"]:
        return None
    now = now or datetime.now(UTC)
    delay = secrets.SystemRandom().randint(
        settings["token_delay_min_ms"], settings["token_delay_max_ms"]
    )
    bits = 0
    if (
        settings["pow_difficulty_bits"] > 0
        and feedback_limits.load_percent(settings, now) >= settings["pow_threshold_percent"]
    ):
        bits = settings["pow_difficulty_bits"]
    issued = _ms(now)
    payload = {
        "a": article_id,
        "i": issued,
        "nb": issued + delay,
        "e": issued + TOKEN_TTL_SECONDS * 1000,
        "n": secrets.token_urlsafe(12),
        "d": bits,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return {"token": f"{_VERSION}.{body}.{_sign(body)}", "wait_ms": delay, "pow_bits": bits}


def _fail(reason: str, retry_after_ms: int | None = None) -> dict:
    return {"ok": False, "reason": reason, "retry_after_ms": retry_after_ms}


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        bits += 8 - byte.bit_length()
        break
    return bits


def work_is_valid(token: str, nonce, bits: int) -> bool:
    """Does SHA-256(token + ":" + nonce) start with `bits` zero bits? (Base-10 digits only.)"""
    text = str(nonce) if isinstance(nonce, int | str) and not isinstance(nonce, bool) else ""
    if not text.isascii() or not text.isdigit() or len(text) > 20:
        return False
    digest = hashlib.sha256(f"{token}:{text}".encode()).digest()
    return leading_zero_bits(digest) >= bits


def verify(article_id: str, token, work, settings: dict, now: datetime | None = None) -> dict:
    """Check a submission's token. Returns {ok, reason, retry_after_ms}. On success the token is
    spent. A submission that is too early is NOT spent: the client waits and sends it again.
    Never raises: if the key or the used-token record cannot be reached, verification fails
    closed ("unavailable")."""
    if not settings["verification_required"]:
        return {"ok": True, "reason": None, "retry_after_ms": None}
    now = now or datetime.now(UTC)
    if not isinstance(token, str) or not token:
        return _fail(MISSING)
    try:
        version, body, signature = token.split(".")
        if version != _VERSION:
            return _fail(INVALID)
        if not hmac.compare_digest(signature, _sign(body)):
            return _fail(INVALID)
        payload = json.loads(_unb64(body))
        issued_for, not_before, expires = payload["a"], int(payload["nb"]), int(payload["e"])
        nonce, bits = str(payload["n"]), int(payload["d"])
    except (ValueError, KeyError, TypeError):
        return _fail(INVALID)
    except Exception as exc:  # noqa: BLE001 - the signing key could not be read
        print(f"feedback_verification: could not check a token: {exc!r}")
        return _fail(UNAVAILABLE)

    now_ms = _ms(now)
    if issued_for != article_id:
        return _fail(WRONG_ARTICLE)
    if now_ms > expires:
        return _fail(EXPIRED)
    if now_ms < not_before:
        return _fail(TOO_EARLY, not_before - now_ms)
    if bits > 0 and not work_is_valid(token, work, bits):
        return _fail(WORK)
    try:
        first_use = consume_verification_nonce(nonce, expires // 1000 + 3600)
    except Exception as exc:  # noqa: BLE001
        print(f"feedback_verification: could not record a token as used: {exc!r}")
        return _fail(UNAVAILABLE)
    if not first_use:
        return _fail(USED)
    return {"ok": True, "reason": None, "retry_after_ms": None}
