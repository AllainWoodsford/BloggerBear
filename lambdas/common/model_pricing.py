"""Model-id normalisation and built-in fallback prices for the AI lineage/cost
tracking (docs/project-plan.md §11).

Two problems this solves, both of which left every new article's cost blank:

* **Ids arrive as ARNs.** `BEDROCK_MODEL_ID` is set by Terraform, and for a
  cross-region inference profile it is the full ARN
  (`arn:aws:bedrock:<region>:<acct>:inference-profile/au.anthropic...`).
  That ARN was recorded as the model in every lineage and used as the price
  lookup key, so it could never match a registry row keyed the way the admin
  CLI documents (`au.anthropic.claude-haiku-4-5-...`). `canonical_model_id`
  reduces an ARN to the profile/model id it names; the recorded id and the
  lookup key are both the canonical one.

* **Nothing ever seeded the registry.** The Models table is filled by hand via
  the admin CLI, so on a fresh deploy every lookup missed and cost was `None`.
  `DEFAULT_MODEL_PRICES` is a small built-in fallback for the models this
  project runs, used only when the registry has no priced row: the registry
  always wins, so a price can be corrected without a deploy.

The built-in prices are the provider's list price for the model, keyed by the
*base* model id, so one entry serves every geo profile of it (`au.`, `apac.`,
`us.`, ...). A geographic profile can carry a regional premium over the list
price; register the exact price in the Models table to override this.
"""

from __future__ import annotations

import re

_ARN_ID_RE = re.compile(r"^arn:[^:]*:bedrock:[^:]*:[^:]*:(?:inference-profile|foundation-model)/(.+)$")

# Geographic inference-profile prefixes that sit in front of a base model id.
_GEO_PREFIXES = ("au.", "apac.", "us-gov.", "us.", "eu.", "jp.", "global.")

# Keyed by base model id (no geo prefix). Provider list prices, USD per 1K tokens.
DEFAULT_MODEL_PRICES: dict[str, dict] = {
    "anthropic.claude-haiku-4-5-20251001-v1:0": {
        "display_name": "Claude Haiku 4.5",
        "provider": "anthropic",
        "input_price_usd_per_1k_tokens": 0.001,
        "output_price_usd_per_1k_tokens": 0.005,
    },
}


def canonical_model_id(model_id):
    """The profile/model id an id or Bedrock ARN names.

    `arn:aws:bedrock:...:inference-profile/au.anthropic.x` -> `au.anthropic.x`.
    Anything else (a plain id, an application-inference-profile ARN whose id
    carries no model name, a non-string) comes back unchanged.
    """
    if not isinstance(model_id, str):
        return model_id
    match = _ARN_ID_RE.match(model_id.strip())
    return match.group(1) if match else model_id


def base_model_id(model_id: str) -> str:
    """`canonical_model_id` with any geo-profile prefix removed."""
    canonical = canonical_model_id(model_id)
    for prefix in _GEO_PREFIXES:
        if canonical.startswith(prefix):
            return canonical[len(prefix):]
    return canonical


def default_model_entry(model_id: str) -> dict | None:
    """The built-in registry-shaped entry for a model, or None if not known."""
    entry = DEFAULT_MODEL_PRICES.get(base_model_id(model_id))
    if entry is None:
        return None
    return {"model_id": canonical_model_id(model_id), **entry}


def model_label(model_id: str, registry_entry: dict | None = None) -> str:
    """A readable name for a model: the registry's display name, else the
    built-in one, else the canonical id."""
    if registry_entry and registry_entry.get("display_name"):
        return registry_entry["display_name"]
    default = default_model_entry(model_id)
    if default is not None:
        return default["display_name"]
    return canonical_model_id(model_id)
