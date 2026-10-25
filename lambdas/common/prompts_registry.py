"""The registry of prompts: every PromptSpec the code has, for common/prompts.py and the lint.

docs/enhancements/model-ranch-prompt-assets-evals.md (section 4.10 is the inventory). Each
migration PR moves a call site's inline template here as a `PromptSpec` (its vanilla, with the
version and pinned sha256, the placeholders it takes and the locked blocks that end it) and
renders through `prompts.get_prompt`; the publisher's lint (scripts/prompt_lint.py) imports
`all_specs` to hold every asset file in `prompts/` to its spec. Empty until the first migration.
"""

from __future__ import annotations

from common.prompts import PromptSpec


def all_specs() -> list[PromptSpec]:
    """Every prompt the code knows, one spec each. `prompts.spec_for` looks ids up here."""
    return []
