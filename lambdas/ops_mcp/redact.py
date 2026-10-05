"""The sweep every piece of log-derived text goes through before it leaves the server: personal data
out, secrets out, and text that reads like instructions to the assistant withheld.

Logs are the most hostile thing the assistant reads. A Lambda's log line can carry what a visitor
typed (a feedback comment, a search), an exception message quoting a request, a header, an address;
an access log carries paths an attacker chose. So nothing read from a log reaches a tool's result
without `scrub` (or `scrub_tree` for a structure), and what it returns still goes under `untrusted`,
never into `spoken`.

**What is removed** (each replaced by a placeholder that says what was there, so the operator knows
something was taken out and why):

    e-mail addresses          [email]
    IPv4 addresses            masked: first and last octet kept, 123.XXX.XXX.34 (the owner's rule:
                              enough to say "the address ending .34", not enough to name anyone)
    IPv6 addresses            masked the same way: first and last group kept, 2001:XXXX:…:7334
    bearer tokens and JWTs    [token]
    AWS access key ids        [aws-key]
    key=value secrets         password=[redacted] (password, secret, api key, token, authorization,
                              cookie, session)
    AWS account ids           [account] (twelve digits standing alone, as in an ARN)
    card numbers              [card] (13 to 19 digits that pass the Luhn check)
    phone numbers             [phone] (international form, starting with +)
    long hex or base64 runs   [secret] (40 characters or more: keys, signatures, session blobs)

UUIDs and request ids stay: they identify a request or a row, not a person, and are what the
operator searches the console with.

**Text that reads like instructions** (`looks_like_instructions`): "ignore previous instructions",
"you are now", "system prompt", a fake tool call, and the like. Such a line is not shown at all:
it is replaced by WITHHELD, and the result says how many were withheld. The model reading the
result never sees the words, so they cannot steer it, and the operator still learns that a line
like that was in the log (which is itself worth knowing: someone is probing).

Pure functions and regular expressions: no AWS, no model.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping

from common.security_events import untrusted_text

TEXT_MAX_CHARS = 300
WITHHELD = "[withheld: this line reads like instructions to the assistant]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Not inside a longer dotted number (a version "1.2.3.4.5"), but a sentence's full stop after it is
# fine: "blocked from 198.51.100.7." is an address.
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)")
# Two or more colons between hex groups; checked with ipaddress before it is masked, so a time
# ("12:30:45") or a Python repr is left alone.
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}\b")
_KEY_VALUE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|authorization|cookie|session(?:[_-]?id)?)"
    r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\"',;&]+)"
)
_ACCOUNT = re.compile(r"(?<![\d-])\d{12}(?![\d-])")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE = re.compile(r"(?<![\w+])\+\d{1,3}[ -]?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){2,4}(?!\d)")
_LONG_SECRET = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/=_-])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Phrases that are addressed to a model, not written by a program. Matched case-insensitively,
# anywhere in a line. Deliberately broad: a false positive only hides one log line, and says so.
_INSTRUCTION_LIKE = re.compile(
    r"(?i)("
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|messages?)"
    r"|disregard (all |any |the )?(previous|prior|above|earlier)"
    r"|forget (all |everything|your) (previous |prior )?(instructions|rules)"
    r"|you are now\b|you must now\b|act as (an? |the )?(admin|assistant|system|developer)"
    r"|new instructions?:|system prompt|system message|<\s*/?\s*(system|assistant|tool|instructions?)\b"
    r"|\[\s*(system|inst)\s*\]|\bassistant\s*:|\bsystem\s*:"
    r"|tool_use|tool_call|function_call|call the tool|run the command|execute the following"
    r"|reveal (your|the) (prompt|instructions|system)"
    r")"
)


def mask_ip(value: str) -> str:
    """An address with its middle masked: 123.XXX.XXX.34, or 2001:XXXX:…:7334. Anything that does
    not parse as an address comes back as [ip]."""
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return "[ip]"
    if address.version == 4:
        parts = str(address).split(".")
        return f"{parts[0]}.XXX.XXX.{parts[3]}"
    groups = address.exploded.split(":")
    return f"{groups[0].lstrip('0') or '0'}:XXXX:…:{groups[-1].lstrip('0') or '0'}"


def _luhn(digits: str) -> bool:
    total, double = 0, False
    for char in reversed(digits):
        number = int(char)
        if double:
            number *= 2
            if number > 9:
                number -= 9
        total += number
        double = not double
    return total % 10 == 0


def _card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)


def _ipv4(match: re.Match) -> str:
    text = match.group(0)
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return text  # a version number or a date, not an address
    return mask_ip(text)


def _ipv6(match: re.Match) -> str:
    text = match.group(0)
    if text.count(":") < 2:
        return text
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return text
    return mask_ip(text)


def _key_value(match: re.Match) -> str:
    """password=... becomes password=[redacted]; an auth scheme word ("Bearer", whose token the
    rule before has already replaced) and a placeholder are left as they are."""
    value = match.group(3)
    if value.startswith("[") or value.lower() in ("bearer", "basic"):
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}[redacted]"


def _long_secret(match: re.Match) -> str:
    """A long run is a secret when it is all hex (a key, a signature) or mixes upper case, lower
    case and digits the way base64 does. A long name or path (bloggerbear-dev-research-tick) is
    neither, and stays."""
    text = match.group(0)
    if re.fullmatch(r"[0-9A-Fa-f]+", text) and re.search(r"\d", text):
        return "[secret]"
    digits = sum(char.isdigit() for char in text)
    if digits * 10 >= len(text) and re.search(r"[A-Z]", text) and re.search(r"[a-z]", text):
        return "[secret]"
    return text


def looks_like_instructions(text) -> bool:
    """Whether the text reads like something written to steer a model."""
    return bool(_INSTRUCTION_LIKE.search(str(text or "")))


def scrub(value, limit: int = TEXT_MAX_CHARS) -> str:
    """One piece of text from a log, safe to put in a result: instructions withheld, personal data
    and secrets replaced, control characters gone, whitespace collapsed, cut to `limit`.

    The order matters: tokens and keys before the generic long-secret rule (so a JWT says [token]),
    cards before account ids (a 16-digit card holds 12 digits in a row), addresses last of the
    numeric rules."""
    text = _CONTROL.sub(" ", str(value if value is not None else ""))
    if looks_like_instructions(text):
        return WITHHELD
    return untrusted_text(_personal(text), limit)


def sweep_answer(text) -> str:
    """The last sweep of what the agent is about to say or show (ops_agent): personal data and
    secrets replaced as `scrub` replaces them, and nothing else changed. Not the instruction rule:
    the answer is the assistant's own words, and "a suggested fix is on screen" is not an attack."""
    return _personal(_CONTROL.sub(" ", str(text if text is not None else "")))


def _personal(text: str) -> str:
    """Personal data and secrets out, in the order `scrub` explains."""
    text = _EMAIL.sub("[email]", text)
    text = _JWT.sub("[token]", text)
    text = _BEARER.sub(lambda match: f"{match.group(1)} [token]", text)
    text = _AWS_KEY.sub("[aws-key]", text)
    text = _KEY_VALUE.sub(_key_value, text)
    text = _CARD.sub(_card, text)
    text = _PHONE.sub("[phone]", text)
    text = _ACCOUNT.sub("[account]", text)
    text = _IPV4.sub(_ipv4, text)
    text = _IPV6.sub(_ipv6, text)
    return _LONG_SECRET.sub(_long_secret, text)


def scrub_tree(value, limit: int = TEXT_MAX_CHARS, *, depth: int = 0):
    """`scrub` over a structure: every string in it, at any depth (keys included), numbers and
    booleans left as they are, anything else turned into scrubbed text. Deeper than eight levels
    is cut off: nothing a tool builds is that deep."""
    if depth > 8:
        return "[cut]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {scrub(key, 80): scrub_tree(item, limit, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scrub_tree(item, limit, depth=depth + 1) for item in value]
    return scrub(value, limit)


def withheld(values) -> int:
    """How many of the scrubbed values were withheld as instruction-like."""
    return sum(1 for value in values if value == WITHHELD)


def personal_data_kinds(text) -> list[str]:
    """Which kinds of personal data or secrets the text still holds, by the same rules `scrub`
    applies. Empty for anything `scrub` returned. Used by tests, and by the agent's last sweep of
    its own answer (ops_agent)."""
    text = str(text or "")
    kinds = []
    if _EMAIL.search(text):
        kinds.append("email")
    if any(_ipv4(match) != match.group(0) for match in _IPV4.finditer(text)):
        kinds.append("ip")
    if any(_ipv6(match) != match.group(0) for match in _IPV6.finditer(text)):
        kinds.append("ip")
    if _JWT.search(text) or _BEARER.search(text):
        kinds.append("token")
    if _AWS_KEY.search(text):
        kinds.append("aws_key")
    if any(_key_value(match) != match.group(0) for match in _KEY_VALUE.finditer(text)):
        kinds.append("secret")
    if any(_card(match) != match.group(0) for match in _CARD.finditer(text)):
        kinds.append("card")
    if _PHONE.search(text):
        kinds.append("phone")
    return list(dict.fromkeys(kinds))
