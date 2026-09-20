"""Aggregate cost/token statistics for the public Stats page (PR 5 of 5 of
the AI lineage/cost-tracking enhancement, docs/project-plan.md §11).

Pure functions over data the caller has already fetched (all Articles,
Topics, and the Models registry) -- no AWS calls in here, so the
aggregation is trivially testable and the handler stays a thin wrapper.

Everything is derived from per-article `lineage` (which model, how many
tokens, on every Bedrock call that went into an article) priced against
the *current* Models registry, so it's an *estimate of AI spend*, not AWS
billing: it does not include Lambda/DynamoDB/S3/CloudFront/etc., and pulling
those in (Cost Explorer) is a deliberately deferred follow-up.

**Cost is computed per call, everywhere.** Totals, per-model, per-topic and
per-day figures are all sums of the same per-call costs, so they always
agree with each other. A call whose model has no registered price
contributes tokens but no cost, and is counted in `unpriced_calls` so a
reader can see the cost figure is a lower bound -- never silently
under-counted, never guessed at.

**Spend counts regardless of publish status.** An article that went to
moderation or was rejected still cost tokens, so "what has this cost"
includes every drafted article; only `published` is broken out separately.
Only aggregates leave this module -- no article ids, titles, or content.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from common.costing import USD_TO_AUD_RATE, call_cost_usd
from common.digest import DIGEST_TOPIC_ID, DIGEST_TOPIC_NAME

DEFAULT_DAILY_WINDOW_DAYS = 30

COST_BASIS_NOTE = (
    "Estimated from recorded token counts and the registered per-model prices "
    "(converted at a fixed USD to AUD rate). Not AWS billing: excludes Lambda, "
    "DynamoDB, S3, CloudFront and other non-AI services."
)


def _created_date(article: dict) -> date | None:
    created_at = article.get("created_at")
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(created_at).astimezone(UTC).date()
    except ValueError:
        return None


def _zero_bucket() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cost_aud": 0.0}


def build_stats(
    articles: list[dict],
    topics: list[dict],
    models: list[dict],
    *,
    today: date | None = None,
    window_days: int = DEFAULT_DAILY_WINDOW_DAYS,
) -> dict:
    today = today or datetime.now(UTC).date()
    pricing = {model["model_id"]: model for model in models}
    model_names = {model["model_id"]: model.get("display_name") or model["model_id"] for model in models}
    topic_names = {topic["topic_id"]: topic.get("name") or topic["topic_id"] for topic in topics}
    topic_names[DIGEST_TOPIC_ID] = DIGEST_TOPIC_NAME

    days = [today - timedelta(days=offset) for offset in range(window_days - 1, -1, -1)]
    daily: dict[date, dict] = {day: {"articles": 0, **_zero_bucket()} for day in days}

    by_model: dict[str, dict] = {}
    by_topic: dict[str, dict] = {}
    totals = {
        "articles": 0,
        "published": 0,
        "articles_with_lineage": 0,
        "calls": 0,
        "unpriced_calls": 0,
        **_zero_bucket(),
    }

    for article in articles:
        totals["articles"] += 1
        if article.get("status") == "published":
            totals["published"] += 1

        lineage = article.get("lineage")
        if not lineage:
            continue
        totals["articles_with_lineage"] += 1

        topic_id = article.get("topic_id") or "unknown"
        topic_bucket = by_topic.setdefault(topic_id, {"articles": 0, **_zero_bucket()})
        topic_bucket["articles"] += 1

        created = _created_date(article)
        day_bucket = daily.get(created) if created else None
        if day_bucket is not None:
            day_bucket["articles"] += 1

        for call in lineage.get("calls") or []:
            input_tokens = int(call.get("input_tokens", 0))
            output_tokens = int(call.get("output_tokens", 0))
            model_id = call.get("model_id") or "unknown"

            usd = call_cost_usd(
                {"input_tokens": input_tokens, "output_tokens": output_tokens}, pricing.get(model_id)
            )
            cost_aud = usd * USD_TO_AUD_RATE if usd is not None else None

            model_bucket = by_model.setdefault(
                model_id, {"calls": 0, "unpriced_calls": 0, **_zero_bucket()}
            )
            model_bucket["calls"] += 1
            totals["calls"] += 1
            if cost_aud is None:
                model_bucket["unpriced_calls"] += 1
                totals["unpriced_calls"] += 1

            buckets = [totals, model_bucket, topic_bucket]
            if day_bucket is not None:
                buckets.append(day_bucket)
            for bucket in buckets:
                bucket["input_tokens"] += input_tokens
                bucket["output_tokens"] += output_tokens
                if cost_aud is not None:
                    bucket["cost_aud"] += cost_aud

    with_lineage = totals["articles_with_lineage"]
    totals["avg_cost_aud"] = totals["cost_aud"] / with_lineage if with_lineage else None

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "currency": "AUD",
        "cost_basis": COST_BASIS_NOTE,
        "totals": totals,
        "by_model": sorted(
            (
                {
                    "model_id": model_id,
                    "display_name": model_names.get(model_id, model_id),
                    **bucket,
                    # None (not 0) when *every* call to this model was unpriced,
                    # so the page can say "unpriced" rather than "$0.00".
                    "cost_aud": None if bucket["calls"] == bucket["unpriced_calls"] else bucket["cost_aud"],
                }
                for model_id, bucket in by_model.items()
            ),
            key=lambda row: (row["cost_aud"] or 0.0, row["input_tokens"] + row["output_tokens"]),
            reverse=True,
        ),
        "by_topic": sorted(
            (
                {"topic_id": topic_id, "name": topic_names.get(topic_id, topic_id), **bucket}
                for topic_id, bucket in by_topic.items()
            ),
            key=lambda row: (row["cost_aud"], row["input_tokens"] + row["output_tokens"]),
            reverse=True,
        ),
        "daily": [{"date": day.isoformat(), **daily[day]} for day in days],
    }
