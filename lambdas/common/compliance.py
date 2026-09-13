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
