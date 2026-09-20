"""Hierarchical editorial goals: what a topic's research and writing are *for*.

A topic's standing editorial goal is resolved through a fallback tree, so a new
topic needs no code change and no configuration to behave sensibly:

    Topic-specific goal   (`editorial_goals.primary_focus` on the Topic)
        -> Adapter-specific goal   (ADAPTER_DEFAULTS, keyed by the topic's adapter)
            -> Global default goal (independent web research and synthesis)

A topic's `exclusion_criteria` apply on top of whichever layer wins -- ignoring
"marketing press releases" is just as valid for a topic on an adapter default
as for one with its own focus.

This is the topic's *standing* objective. It is separate from, and layered
with, the crypto feed's daily rotating editorial goal (common/editorial_goals.py:
today's analysis format), which works inside this objective and never replaces it.
The resolved text is mirrored into the research summary (P1), ideation (P2) and
drafting (P3) prompts. It shapes what to look for; it never relaxes the
relevance guardrails (common/relevance.py) or the financial-topic safety rules
(common/compliance.py), which are applied independently.
"""

from __future__ import annotations

from common.adapters import WEB_SEARCH_ADAPTER_KEY

# A topic created without an adapter gets independent web research.
DEFAULT_ADAPTER = WEB_SEARCH_ADAPTER_KEY

GLOBAL_DEFAULT_GOAL = (
    "Execute independent web research and data synthesis. Focus on randomly sampling "
    "5 to 10 highly relevant articles or primary data points from the raw feed payload. "
    "Identify underlying structural patterns, emerging shifts, or non-obvious anomalies "
    "that matter to an expert practitioner in this domain."
)

# Adding an adapter-specific goal is one line here; an adapter with no entry
# simply falls through to the global default.
ADAPTER_DEFAULTS = {
    "crypto_feed": (
        "Prioritize structural changes in asset cap distributions, 24-hour network momentum "
        "variations, and long-term 1-year historical pricing baselines. Avoid speculative hype."
    ),
    "github_trending": (
        "Focus on rapid open-source star acceleration, architectural paradigm shifts (e.g., "
        "new framework primitives), and infrastructural utilities."
    ),
}

# Mirrored into the writing prompts (daily_cycle_handler) next to the goal itself.
MANDATE_ALIGNMENT_RULE = (
    "Every proposed angle MUST also directly serve the Active Editorial Mandate above. If raw "
    "data findings contain noisy or off-topic subjects, you MUST aggressively reframe them "
    "through the lens of this mandate."
)
DRAFT_ALIGNMENT_DIRECTIVE = (
    "Maintain absolute structural alignment with the Core Editorial Direction. Every "
    "paragraph must deliver high-signal insight directly tailored to a reader tracking this "
    "exact objective."
)

GOAL_FIELDS = ("primary_focus", "exclusion_criteria")
MAX_GOAL_TEXT_CHARS = 1000


def _clean(value) -> str | None:
    """A goal field as stripped text, or None if it's absent/blank/not text
    (the block can be hand-edited in DynamoDB, so never trust its shape)."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def resolve_editorial_goals(topic: dict) -> str:
    """The topic's standing editorial goal, as prompt text.

    Topic-specific -> adapter-specific -> global default, with the topic's
    exclusion criteria (if any) appended whichever layer applies.
    """
    adapter = topic.get("adapter") or DEFAULT_ADAPTER
    raw_goals = topic.get("editorial_goals")
    goals = raw_goals if isinstance(raw_goals, dict) else {}
    focus = _clean(goals.get("primary_focus"))
    exclusions = _clean(goals.get("exclusion_criteria"))

    if focus:
        resolved = f"Topic-Specific Focus: {focus}"
    elif adapter in ADAPTER_DEFAULTS:
        resolved = f"Adapter-Specific Standard Goal: {ADAPTER_DEFAULTS[adapter]}"
    else:
        resolved = f"Global Default Goal: {GLOBAL_DEFAULT_GOAL}"

    if exclusions:
        resolved += f"\nStrict Constraints: {exclusions}"
    return resolved


def validate_editorial_goals(value) -> str | None:
    """Validate a Topic's optional `editorial_goals` block from the admin API.

    Returns an error message, or None if valid. None (unset) and {} both mean
    "no topic-specific goal" -- the topic falls back to its adapter/global one.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return "'editorial_goals' must be an object if provided"
    unknown = sorted(set(value) - set(GOAL_FIELDS))
    if unknown:
        return f"'editorial_goals' has unknown field(s) {unknown}; allowed: {list(GOAL_FIELDS)}"
    for field in GOAL_FIELDS:
        if field not in value:
            continue
        text = value[field]
        if not isinstance(text, str) or not text.strip():
            return f"'editorial_goals.{field}' must be a non-empty string if provided"
        if len(text.strip()) > MAX_GOAL_TEXT_CHARS:
            return f"'editorial_goals.{field}' must be at most {MAX_GOAL_TEXT_CHARS} characters"
    return None


def normalize_editorial_goals(value) -> dict:
    """A validated block with its text stripped (None becomes {})."""
    return {field: value[field].strip() for field in GOAL_FIELDS if field in (value or {})}
