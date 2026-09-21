"""Per-article cost math for the AI lineage/cost-tracking enhancement
(docs/project-plan.md §11, PR 2 of 5).

Single source of truth for turning a list of tracked Bedrock calls (see
common.bedrock.invoke_model_tracked) into the `lineage` dict stored on an
Articles item -- kept separate from daily_cycle_handler.py/
trending_digest_handler.py so both handlers assemble lineage identically,
and so the public Stats page can reuse the same cost math for aggregate
reporting without duplicating it.

**Pricing** is looked up by the canonical model id (an ARN is reduced to the
profile id it names -- see common/model_pricing.py): the Models registry first,
then the built-in fallback prices. A model with neither is logged and left
unpriced (never guessed at).

**Research is tallied separately from authoring.** The hourly research tick
makes its own Bedrock call for every Finding it writes, long before any article
exists. Each Finding records that call (`research_call`), and when an article is
written the daily cycle sums the calls of the Findings in its window into
`lineage["research"]` -- so an article carries its authoring cost (`calls`,
`cost_aud`), its research cost (`research`), and their sum (`total_cost_aud`).
Since an article's window starts where the topic's previous article's ended,
each research call is counted in exactly one article.
"""

from __future__ import annotations

from common.dynamo import get_model
from common.model_pricing import canonical_model_id, default_model_entry, model_label

# Fixed, periodically-updated approximation -- deliberately not a live FX
# rate, to avoid a third-party API dependency for what's already only an
# approximate cost estimate (per-article lineage, not a real invoice).
# Update by hand if it drifts far enough to matter.
USD_TO_AUD_RATE = 1.50


def call_cost_usd(call: dict, model: dict | None) -> float | None:
    """USD cost of one tracked call given its model's registry entry, or
    None if that model isn't registered / has no input price. The single
    formula behind both per-article lineage cost (below) and the public
    Stats page's aggregation (common/stats.py), which passes in
    entries from one list_models() scan instead of a get_model per call.
    """
    if model is None or model.get("input_price_usd_per_1k_tokens") is None:
        return None
    input_price = model["input_price_usd_per_1k_tokens"]
    output_price = model.get("output_price_usd_per_1k_tokens") or 0.0
    return (call["input_tokens"] / 1000) * input_price + (call["output_tokens"] / 1000) * output_price


def _priced(entry: dict | None) -> bool:
    return bool(entry) and entry.get("input_price_usd_per_1k_tokens") is not None


def pricing_for(model_id: str) -> dict | None:
    """The price entry to use for a model id, or None if nothing prices it.

    The Models registry wins (under the id as given, then the canonical id, so
    a row registered by ARN or by profile id both match); the built-in table is
    the fallback that makes cost work on a deploy nobody has seeded.
    """
    for candidate in dict.fromkeys((model_id, canonical_model_id(model_id))):
        entry = get_model(candidate)
        if _priced(entry):
            return entry
    return default_model_entry(model_id)


def calculate_lineage_cost_aud(calls: list[dict]) -> tuple[float | None, str | None]:
    """Sum a list of tracked-call dicts' cost in AUD, or (None, note) if
    any contributing model's pricing isn't known.

    Each call dict needs `model_id`, `input_tokens`, `output_tokens`.
    Deliberately does NOT partially sum and skip an unpriced call --
    a lower number that silently excludes some calls is worse than an
    honest "unknown", since the incomplete total looks like a real one.
    """
    total_usd = 0.0
    pricing: dict[str, dict | None] = {}
    for call in calls:
        model_id = call["model_id"]
        if model_id not in pricing:
            pricing[model_id] = pricing_for(model_id)
        call_usd = call_cost_usd(call, pricing[model_id])
        if call_usd is None:
            print(
                f"costing: no price for model {model_id!r}; register one with "
                "`admin_cli models add` or cost stays blank"
            )
            return None, f"pricing not available for {canonical_model_id(model_id)}"
        total_usd += call_usd
    return total_usd * USD_TO_AUD_RATE, None


def build_research_lineage(findings: list[dict]) -> dict:
    """Bundle the research spend behind a window of Findings.

    Each Finding written since research tracking began carries `research_call`
    (model, tokens); one written before it does not, and is counted as
    `untracked_findings` and said so in the note rather than passing as free.
    Every research call is included, whether or not the article ended up using
    that Finding's content.
    """
    calls: list[dict] = []
    untracked = 0
    for finding in findings:
        research_call = finding.get("research_call")
        if not research_call:
            untracked += 1
            continue
        calls.append(
            {
                "stage": "research",
                "captured_at": finding.get("captured_at"),
                "model_id": canonical_model_id(research_call["model_id"]),
                "input_tokens": int(research_call["input_tokens"]),
                "output_tokens": int(research_call["output_tokens"]),
                "used_fallback": bool(research_call.get("used_fallback", False)),
            }
        )

    return summarise_research(calls, findings=len(findings), untracked=untracked)


def summarise_research(calls: list[dict], *, findings: int, untracked: int) -> dict:
    """The `lineage["research"]` block for a set of research calls. Split out of
    build_research_lineage so the lineage backfill can re-price a stored block
    with exactly the same arithmetic."""
    cost_aud, cost_note = calculate_lineage_cost_aud(calls)
    if untracked:
        excluded = f"excludes {untracked} finding(s) recorded before research tracking"
        cost_note = f"{cost_note}; {excluded}" if cost_note else excluded

    models_used: list[str] = []
    for call in calls:
        if call["model_id"] not in models_used:
            models_used.append(call["model_id"])

    return {
        "findings": findings,
        "tracked_findings": len(calls),
        "untracked_findings": untracked,
        "calls": calls,
        "input_tokens": sum(call["input_tokens"] for call in calls),
        "output_tokens": sum(call["output_tokens"] for call in calls),
        "models_used": models_used,
        "cost_aud": cost_aud,
        "cost_note": cost_note,
    }


def build_lineage(calls: list[dict], research: dict | None = None) -> dict:
    """Assemble the full `lineage` dict (docs/project-plan.md §11) from a
    list of tracked-call dicts, one per Bedrock call that contributed to
    an article (ideation/draft/title/compliance_review). `calls` should
    already have any None entries filtered out by the caller (e.g. a
    financial-topic compliance review makes no Bedrock call at all).

    `research` is the optional research tally from `build_research_lineage`.
    When given, `total_cost_aud` is authoring + research (None if either is
    unknown); `cost_aud` stays the authoring cost alone so it keeps its meaning.
    """
    calls = [{**call, "model_id": canonical_model_id(call["model_id"])} for call in calls]

    total_input_tokens = sum(call["input_tokens"] for call in calls)
    total_output_tokens = sum(call["output_tokens"] for call in calls)

    models_used: list[str] = []
    for call in calls:
        if call["model_id"] not in models_used:
            models_used.append(call["model_id"])

    cost_aud, cost_note = calculate_lineage_cost_aud(calls)

    lineage = {
        "calls": calls,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "models_used": models_used,
        "cost_aud": cost_aud,
        "cost_note": cost_note,
    }
    if research is not None:
        lineage["research"] = research
        research_cost = research["cost_aud"]
        lineage["total_cost_aud"] = (
            cost_aud + research_cost if cost_aud is not None and research_cost is not None else None
        )

    all_models = models_used + [m for m in (research or {}).get("models_used", []) if m not in models_used]
    lineage["model_labels"] = {
        model_id: model_label(model_id, pricing_for(model_id)) for model_id in all_models
    }
    return lineage
