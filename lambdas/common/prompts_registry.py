"""The registry of prompts: every PromptSpec the code has, for common/prompts.py and the lint.

docs/enhancements/model-ranch-prompt-assets-evals.md (section 4.10 is the inventory). Each
migration PR moves a call site's inline template here as a `PromptSpec` (its vanilla, with the
version and pinned sha256, the placeholders it takes and the locked blocks that end it) and
renders through `prompts.get_prompt`; the publisher's lint (scripts/prompt_lint.py) imports
`all_specs` to hold every asset file in `prompts/` to its spec. The migration PR that adds an asset
spec also adds its vanilla as `prompts/<prompt_id>/latest/v1.txt`, byte for byte: the first asset
the publisher serves is the text the code already sends.

**How a template is split.** A vanilla renders to exactly the text its call site built before it
moved here. Every value the call site works out at run time is a `{{name}}`, and so is every rule
that belongs to another module (the relevance guardrails, the editorial alignment rules) or that a
parser depends on (a reply format): the daily cycle's values are all `required`, so an asset must
place each of them and can reword none. The rules an asset may not move either are
`{{locked:name}}` blocks, and because locked blocks must end the template, only what already ends
the prompt can be locked (in the daily cycle: the financial-topic guidance, the topic adapter's
writing rules and the few-shot excerpt after them, and the title's reply contract). A block that is
sometimes absent (the gear's guidance, an article style) is a value whose empty form is zero bytes,
with the newlines that frame it inside the value, so an empty one leaves no blank line behind.
What is left is plain text an asset may rewrite.
"""

from __future__ import annotations

from common.prompts import PromptSpec

# --- daily_cycle (daily_cycle_handler) ---------------------------------------------------------------

# The two ideation prompts (`_ideate`): without a daily editorial goal, and with one (the crypto
# feed's rotating goal, or a rotating topic's day subject), which adds the day's mandate. The reply
# format is a value: the angle parser depends on it, and it is not at the end, so it cannot be locked.
_IDEATION = (
    "Based on the following recent research findings about '{{topic_name}}', propose exactly "
    "{{num_angles}} distinct, specific candidate article angles. {{reply_rule}}\n\n"
    "ACTIVE EDITORIAL MANDATE:\n{{editorial_goals}}\n\n"
    "{{relevance_rule}}\n{{alignment_rule}}{{revisit_note}}{{already_covered}}\n\n"
    "Findings:\n{{summaries}}{{guidance_block}}{{locked:financial_guidance}}"
)
_IDEATION_GOAL = (
    "Based on the following recent research findings and the assigned daily editorial vector for "
    "'{{topic_name}}', propose exactly {{num_angles}} distinct, specific candidate article angles. "
    "{{reply_rule}}\n\n"
    "ACTIVE EDITORIAL MANDATE:\n{{editorial_goals}}\n\n"
    "{{relevance_rule}}\n{{alignment_rule}}{{revisit_note}}{{already_covered}}\n\n"
    "Editorial Mandate: {{daily_mandate}}\n"
    "Apply this mandate strictly within the theme of '{{topic_name}}' and the Active Editorial "
    "Mandate above: it decides the shape of each angle, never the subject.\n\n"
    "Data Payload:\n{{summaries}}{{guidance_block}}{{locked:financial_guidance}}"
)
_IDEATION_VALUES = frozenset(
    {
        "topic_name",
        "num_angles",
        "reply_rule",
        "editorial_goals",
        "relevance_rule",
        "alignment_rule",
        "revisit_note",
        "already_covered",
        "summaries",
        "guidance_block",
    }
)

# The article draft (`_draft_article`). The financial guidance, the adapter's writing rules (what
# the fresh-data review holds the draft to) and the few-shot excerpt, whose framing says not to
# repeat the past article, end the prompt, in that order, so all three are locked.
_DRAFT = (
    "Write a full article draft in markdown (a few paragraphs) for a blog about '{{topic_name}}', "
    "on this angle: {{angle}}\n\n"
    "CORE EDITORIAL DIRECTION:\n{{editorial_goals}}{{revisit_note}}\n\n"
    "Base it on these recent findings:\n{{summaries}}\n\n"
    "{{relevance_boundary}}\n\n"
    "{{alignment_directive}}{{style_block}}{{guidance_block}}"
    "{{locked:financial_guidance}}{{locked:source_guidance}}{{locked:few_shot}}"
)

# The title (`_draft_title`). Its reply contract is what `_is_plausible_reply` checks the answer
# against, and `title_retry` is the firmer line the one retry adds (empty on the first call).
_TITLE = (
    "Write a short, engaging article title for an article with this angle: {{angle}}\n\n"
    "{{locked:reply_rule}}{{locked:title_retry}}"
)

# max_chars is measured on the rendered text. The findings block is up to 40,000 characters
# (daily_cycle_handler.SUMMARIES_MAX_CHARS) and the gear's guidance up to 4,000
# (equipment.MAX_GUIDANCE_CHARS); with every other value at its own limit a render stays under
# 55,000, so 64,000 leaves room without letting an asset grow without bound. A title is its angle
# (40 words at most) and two short rules.
_DRAFTING_MAX_CHARS = 64_000
_TITLE_MAX_CHARS = 4_000

DAILY_CYCLE_SPECS = (
    PromptSpec(
        prompt_id="daily_cycle/ideation",
        vanilla=_IDEATION,
        vanilla_version="v1",
        vanilla_sha256="28ca57593ed076a474d2e1666e4761c53448dbbd221389e88872cd443c628d16",
        required=_IDEATION_VALUES,
        optional=frozenset(),
        locked=("financial_guidance",),
        max_chars=_DRAFTING_MAX_CHARS,
    ),
    PromptSpec(
        prompt_id="daily_cycle/ideation_goal",
        vanilla=_IDEATION_GOAL,
        vanilla_version="v1",
        vanilla_sha256="3a259a87474585a814cdc830708ad49b043224e18c35466325f86e9a21eb308c",
        required=_IDEATION_VALUES | {"daily_mandate"},
        optional=frozenset(),
        locked=("financial_guidance",),
        max_chars=_DRAFTING_MAX_CHARS,
    ),
    PromptSpec(
        prompt_id="daily_cycle/draft",
        vanilla=_DRAFT,
        vanilla_version="v1",
        vanilla_sha256="bb1289e0db7f66e874f147289ca4fec18417a412d4c39827fc413846bd97b918",
        required=frozenset(
            {
                "topic_name",
                "angle",
                "editorial_goals",
                "revisit_note",
                "summaries",
                "relevance_boundary",
                "alignment_directive",
                "style_block",
                "guidance_block",
            }
        ),
        optional=frozenset(),
        locked=("financial_guidance", "source_guidance", "few_shot"),
        max_chars=_DRAFTING_MAX_CHARS,
    ),
    PromptSpec(
        prompt_id="daily_cycle/title",
        vanilla=_TITLE,
        vanilla_version="v1",
        vanilla_sha256="62c0a80cc539e27eeb778104593d142aa62ae70db30f9d099e7f4273a321f1f3",
        required=frozenset({"angle"}),
        optional=frozenset(),
        locked=("reply_rule", "title_retry"),
        max_chars=_TITLE_MAX_CHARS,
    ),
)


def all_specs() -> list[PromptSpec]:
    """Every prompt the code knows, one spec each. `prompts.spec_for` looks ids up here."""
    return [*DAILY_CYCLE_SPECS]
