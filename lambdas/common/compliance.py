"""Compliance and safety helpers for the daily authoring cycle.

Implements project-plan.md §2 hard constraint #4 (financial topics always
route to manual moderation) and the §7 "regex redaction then Bedrock
redaction review" pattern, reused here for draft compliance review rather
than feedback (Feedback's own PII pipeline is Phase 5, out of scope here).
"""

import re

from common.bedrock import invoke_claude

# A handful of conservative, obvious-PII patterns -- not a PII-detection
# library. Applied in order, most specific first, with a bare long-digit-run
# catch-all last.
_REDACTION_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"),  # email addresses
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-shaped: 123-45-6789
    re.compile(r"\b\d(?:[ -]?\d){11,18}\b"),  # credit-card-shaped digit runs
    re.compile(
        r"\b(?:\+?\d{1,2}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    ),  # phone-number-shaped
    re.compile(r"\b\d{9,}\b"),  # catch-all: any other long digit sequence
]

_REDACTED = "[REDACTED]"

_COMPLIANT_TOKEN = "COMPLIANT"
_NON_COMPLIANT_TOKEN = "NON-COMPLIANT"

_FINANCIAL_REASON = "financial topic - routed to manual moderation regardless of content"

_REVIEW_PROMPT_TEMPLATE = """You are a compliance reviewer for a blog publishing platform.
Review the following draft article against this rubric:
- No unsubstantiated factual claims presented as certain
- No personally identifiable information (PII)
- No financial or investment recommendation language
- Neutral, appropriate tone

Respond in EXACTLY this format and nothing else:
- If the draft passes every rubric item, reply with the single word
  COMPLIANT on the first line, followed by any minor concerns, one per line.
- If it fails any rubric item, reply with NON-COMPLIANT on the first line,
  followed by the specific reasons, one per line.

Draft:
{draft}
"""


def is_financial_topic(topic: dict) -> bool:
    """Return whether a topic is financial/investment-adjacent."""
    return topic.get("is_financial", False)


# --- Financial-topic drafting guidance (Phase 7) -----------------------
#
# review_draft above already guarantees every financial draft goes to
# manual moderation, deterministically -- not something an LLM call could
# override. These two are defense-in-depth on the *drafting* side (per
# the project's financial-topic rubric: no recommendation language, and a
# standing "not financial advice" disclaimer), for topics like Phase 7's
# crypto_feed adapter and any other is_financial topic. The disclaimer is
# appended deterministically by the caller (daily_cycle_handler.py), not
# left to the model to remember -- same "deterministic, not
# LLM-dependent" posture as review_draft's unconditional routing above.

FINANCIAL_DRAFTING_GUIDANCE = (
    'Do not use recommendation language (e.g. "buy", "sell", "a good investment", '
    '"you should"), and do not predict future prices -- describe only what the data '
    "shows. This draft will always go to manual moderation regardless of content, but "
    "write it as informational commentary, never as advice."
)

FINANCIAL_DISCLAIMER = (
    "\n\n---\n\n*This article is for informational purposes only and does not "
    "constitute financial or investment advice. Nothing in it should be construed as "
    "a recommendation to buy, sell, or hold any asset.*"
)


def append_financial_disclaimer(draft_text: str) -> str:
    """Deterministically append the standing "not financial advice" disclaimer.

    Guaranteed regardless of whether the model actually followed
    FINANCIAL_DRAFTING_GUIDANCE above.
    """
    return f"{draft_text}{FINANCIAL_DISCLAIMER}"


def regex_redact(text: str) -> str:
    """Strip obvious PII-shaped substrings from `text`, replacing with a marker."""
    redacted = text
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def review_draft(draft_text: str, topic: dict, model_id: str) -> dict:
    """Run the compliance review for a draft and return {compliant, reasons}.

    Financial topics are routed to manual moderation unconditionally -- this
    is a deterministic routing rule, not something an LLM call could
    override, per the hard constraint in project-plan.md §2.
    """
    if is_financial_topic(topic):
        return {"compliant": False, "reasons": [_FINANCIAL_REASON]}

    redacted_draft = regex_redact(draft_text)
    prompt = _REVIEW_PROMPT_TEMPLATE.format(draft=redacted_draft)
    response = invoke_claude(prompt, model_id)
    return _parse_compliance_response(response)


def _parse_compliance_response(response: str) -> dict:
    """Parse the model's COMPLIANT/NON-COMPLIANT response, failing closed."""
    if not response or not response.strip():
        return {"compliant": False, "reasons": ["empty compliance review response"]}

    lines = [line.strip() for line in response.strip().splitlines() if line.strip()]
    first_line = lines[0].upper()

    if first_line.startswith(_NON_COMPLIANT_TOKEN):
        reasons = lines[1:] or ["non-compliant per compliance review"]
        return {"compliant": False, "reasons": reasons}

    if first_line.startswith(_COMPLIANT_TOKEN):
        return {"compliant": True, "reasons": lines[1:]}

    # Ambiguous response: fail closed rather than fail open, matching the
    # "no publish without compliance review" hard constraint.
    return {"compliant": False, "reasons": [response.strip()]}


# --- Feedback redaction review (Phase 5) ------------------------------------
#
# project-plan.md §7: "Run regex-based redaction then Bedrock redaction
# review before writing feedback. Never persist raw, unredacted comment
# text." `regex_redact` above is the first pass; this is the second. The
# caller (public_api_handler's feedback route) is responsible for calling
# `regex_redact` first and passing the *already-redacted* text in here --
# this function does not re-run the regex pass itself, to keep the two
# passes visibly separate.

_SAFE_TOKEN = "SAFE:"
_REJECT_TOKEN = "REJECT"

_REDACT_REVIEW_PROMPT_TEMPLATE = """You are reviewing a public comment submitted on a blog article. An
automated regex pass has already redacted obvious PII-shaped substrings
(replaced with [REDACTED]). Review what remains for any PII a regex pass
could miss: full names, email addresses, phone numbers, physical addresses,
usernames/handles, or any other detail that could identify a specific
person.

Respond in EXACTLY this format and nothing else:
- If the text is safe to publish as-is, or can be made safe with further
  redaction, reply with SAFE: on the first line, followed by the final safe
  text (with any further redaction applied) on the following line(s).
- If the text cannot be made safe without destroying its content, reply
  with the single word REJECT and nothing else.

Text:
{text}
"""


def bedrock_redact_review(redacted_text: str, model_id: str) -> str | None:
    """Run the Bedrock redaction-review pass on already regex-redacted text.

    Returns the final safe text on a clear SAFE response, or None on an
    explicit REJECT *or* any response that doesn't clearly match the
    expected format -- fail closed, since dropping an optional comment is
    harmless but silently passing through unreviewed text is not.
    """
    prompt = _REDACT_REVIEW_PROMPT_TEMPLATE.format(text=redacted_text)
    response = invoke_claude(prompt, model_id)
    return _parse_redact_review_response(response)


def _parse_redact_review_response(response: str) -> str | None:
    if not response or not response.strip():
        return None

    lines = response.strip().splitlines()
    first_line = lines[0].strip()

    if first_line.upper() == _REJECT_TOKEN:
        return None

    if first_line.upper().startswith(_SAFE_TOKEN):
        same_line_remainder = first_line[len(_SAFE_TOKEN):].strip()
        rest = lines[1:]
        final_text = "\n".join(([same_line_remainder] if same_line_remainder else []) + rest).strip()
        return final_text or None

    # Ambiguous/malformed response: fail closed rather than pass raw or
    # partially-reviewed text through.
    return None
