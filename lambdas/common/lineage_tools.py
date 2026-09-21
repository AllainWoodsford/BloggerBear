"""Audit and backfill for stored article lineage (docs/project-plan.md §11).

An article's lineage is written once, when it is drafted, and records *tokens*
per call. Cost is derived from those tokens and a model's price, so it can be
recomputed later: an article whose cost was left blank because no price was
known can be fixed retroactively once one is. What can never be recovered is an
article drafted before lineage tracking existed -- it has no token counts, and
these tools report it rather than invent them.

Pure functions over articles the caller already fetched (plus the price lookup
in common/costing.py), so the admin routes are thin wrappers.
"""

from __future__ import annotations

from common.costing import build_lineage, pricing_for, summarise_research
from common.model_pricing import canonical_model_id


def recompute_lineage(lineage: dict) -> dict:
    """The same lineage with model ids canonicalised (older articles recorded the
    full ARN) and cost, cost note, labels and the research/total figures
    recomputed from the stored token counts at today's prices. Tokens, call
    order and everything the article recorded about *what happened* are kept."""
    research = lineage.get("research")
    new_research = None
    if research is not None:
        research_calls = [
            {**call, "model_id": canonical_model_id(call["model_id"])} for call in research.get("calls") or []
        ]
        new_research = summarise_research(
            research_calls,
            findings=research.get("findings", len(research_calls)),
            untracked=research.get("untracked_findings", 0),
        )
    return build_lineage(lineage.get("calls") or [], research=new_research)


def plan_backfill(articles: list[dict]) -> list[dict]:
    """One entry per article whose lineage has calls to recompute, saying whether
    recomputing changes it. Articles with no lineage (or no calls) are not
    listed: there is nothing to derive a cost from."""
    plan = []
    for article in articles:
        lineage = article.get("lineage")
        if not lineage or not lineage.get("calls"):
            continue
        updated = recompute_lineage(lineage)
        plan.append(
            {
                "article_id": article["article_id"],
                "topic_id": article.get("topic_id"),
                "status": article.get("status"),
                "changed": updated != lineage,
                "cost_aud_before": lineage.get("cost_aud"),
                "cost_aud_after": updated.get("cost_aud"),
                "cost_note_after": updated.get("cost_note"),
                "lineage": updated,
            }
        )
    return plan


def audit_lineage(articles: list[dict]) -> dict:
    """Where lineage is missing or incomplete, so a gap is seen rather than
    discovered on the site. Reports article ids only, never content."""
    without_lineage: list[str] = []
    cost_missing: list[str] = []
    research_missing: list[str] = []
    model_usage: dict[str, int] = {}

    for article in articles:
        lineage = article.get("lineage")
        if not lineage or not lineage.get("calls"):
            without_lineage.append(article["article_id"])
            continue
        if lineage.get("cost_aud") is None:
            cost_missing.append(article["article_id"])
        if lineage.get("research") is None:
            research_missing.append(article["article_id"])
        for call in list(lineage["calls"]) + list((lineage.get("research") or {}).get("calls") or []):
            model_id = canonical_model_id(call.get("model_id") or "unknown")
            model_usage[model_id] = model_usage.get(model_id, 0) + 1

    unpriced_models = sorted(
        model_id
        for model_id in model_usage
        if model_id != "unknown" and pricing_for(model_id) is None
    )
    return {
        "articles": len(articles),
        "with_lineage": len(articles) - len(without_lineage),
        "without_lineage": without_lineage,
        "cost_missing": cost_missing,
        "without_research_tally": research_missing,
        "models_used": model_usage,
        "unpriced_models": unpriced_models,
    }
