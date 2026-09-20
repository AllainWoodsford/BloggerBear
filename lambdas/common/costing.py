"""Per-article cost math for the AI lineage/cost-tracking enhancement
(docs/project-plan.md §11, PR 2 of 5).

Single source of truth for turning a list of tracked Bedrock calls (see
common.bedrock.invoke_model_tracked) into the `lineage` dict stored on an
Articles item -- kept separate from daily_cycle_handler.py/
trending_digest_handler.py so both handlers assemble lineage identically,
and so a future public Stats page (PR 5 of 5) can reuse the same cost
math for aggregate reporting without duplicating it.
"""

from __future__ import annotations

from common.dynamo import get_model

# Fixed, periodically-updated approximation -- deliberately not a live FX
# rate, to avoid a third-party API dependency for what's already only an
# approximate cost estimate (per-article lineage, not a real invoice).
# Update by hand if it drifts far enough to matter.
USD_TO_AUD_RATE = 1.50


def calculate_lineage_cost_aud(calls: list[dict]) -> tuple[float | None, str | None]:
    """Sum a list of tracked-call dicts' cost in AUD, or (None, note) if
    any contributing model's pricing isn't registered.

    Each call dict needs `model_id`, `input_tokens`, `output_tokens`.
    Deliberately does NOT partially sum and skip an unpriced call --
    a lower number that silently excludes some calls is worse than an
    honest "unknown", since the incomplete total looks like a real one.
    """
    total_usd = 0.0
    for call in calls:
        model_id = call["model_id"]
        model = get_model(model_id)
        if model is None or model.get("input_price_usd_per_1k_tokens") is None:
            return None, f"pricing not available for {model_id}"
        input_price = model["input_price_usd_per_1k_tokens"]
        output_price = model.get("output_price_usd_per_1k_tokens") or 0.0
        total_usd += (call["input_tokens"] / 1000) * input_price
        total_usd += (call["output_tokens"] / 1000) * output_price
    return total_usd * USD_TO_AUD_RATE, None


def build_lineage(calls: list[dict]) -> dict:
    """Assemble the full `lineage` dict (docs/project-plan.md §11) from a
    list of tracked-call dicts, one per Bedrock call that contributed to
    an article (ideation/draft/title/compliance_review). `calls` should
    already have any None entries filtered out by the caller (e.g. a
    financial-topic compliance review makes no Bedrock call at all).
    """
    total_input_tokens = sum(call["input_tokens"] for call in calls)
    total_output_tokens = sum(call["output_tokens"] for call in calls)

    models_used: list[str] = []
    for call in calls:
        if call["model_id"] not in models_used:
            models_used.append(call["model_id"])

    cost_aud, cost_note = calculate_lineage_cost_aud(calls)

    return {
        "calls": calls,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "models_used": models_used,
        "cost_aud": cost_aud,
        "cost_note": cost_note,
    }
