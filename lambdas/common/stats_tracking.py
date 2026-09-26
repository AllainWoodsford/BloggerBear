"""Observability enhancement, PR 1: Bedrock usage that isn't part of any one article's lineage,
plus a few reader-activity counters, all rolled into one weekly running total.

Per-article cost (ideation, drafting, title, review, revision) has always been tracked --
common/costing.py builds it into each Articles item's own `lineage`, and the public Stats page
aggregates those. Four other Bedrock-calling code paths were never tracked at all, because they
don't belong to any one article:

    musings            common/musings.py    (an article, loot-drop or feedback musing)
    weekly_reflection  weekly_reflection_handler.py  (a topic's rationale + suggested change)
    gear_identity      common/gear.py       (naming a new piece of gear, and the name-safety check)
    comment_screening  common/comment_screening.py   (KEEP/DROP on a reader's comment)

`tracked_claude` below is what each of those now calls instead of `common.bedrock.invoke_claude`
directly -- same signature, same return (just the text), but the call's tokens and cost are also
tallied onto this week's StatsCurrent row (common/dynamo.py's increment_current_stats) under
`category`. `record_feedback_given`, `record_feedback_rejected` and `record_loot_drop` tally the
non-Bedrock reader-activity counters the same way.

Every function here fails open: if recording fails, it is logged and swallowed, never raised --
the caller's actual answer (the generated text, the feedback that was stored) must never be lost
over a bookkeeping write. This mirrors every other "never let this break the real work" pattern in
this codebase (e.g. daily_cycle_handler's musing generation, admin_api_handler's loot-drop
announcement).

PR 2 added the weekly rollover job (stats_rollover_handler.py, StatsCurrent -> a new StatsHistory
row, then reset) and Lambda billed-duration tracking (common/lambda_timing.py). PR 3 added API
Gateway cost via a daily Cost Explorer poll (common/cost_explorer.py, cost_explorer_poll_handler.py)
-- see record_api_gateway_cost below, the one field on this row that is SET rather than ADD'd: a
refreshed snapshot of a rolling 30-day total, not something to accumulate across polls.

PR 4 (the Stats-page UI) adds one more category to the same weekly row: `record_article_lineage`
folds each drafted article's own cost (already tracked per-article as `lineage` on the Articles
table, unaffected by any of this) into this week's `articles_*` counters too -- so "Weekly Stats"
and "Total Stats" show one holistic AI-spend figure instead of leaving out the largest share of it.
Per-model/per-topic/per-day detail stays common/stats.py's job, aggregated live from Articles,
unchanged; this is a coarse weekly total alongside it, not a replacement for it.

Also PR 4: StatsHistory holds one more row beyond the normal one-per-completed-week ones --
`week_start = "all-time"` (a sentinel that can never collide with a real Monday date), a running
total kept in sync by stats_rollover_handler.py at every rollover (common/dynamo.py's
increment_stats_totals/set_stats_totals_fields). `split_for_rollover` below is what tells the
rollover job which of this row's fields are weekly counters (safe to ADD onto that running total)
versus a refreshed snapshot (API Gateway's reading -- SET, never summed). This makes "Total Stats"
a single get_item, never a scan-and-sum over every week that has ever existed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from common.bedrock import invoke_model_tracked
from common.costing import USD_TO_AUD_RATE, call_cost_usd, pricing_for
from common.dynamo import increment_current_stats, set_current_stats_fields

BEDROCK_CATEGORIES = ("musings", "weekly_reflection", "gear_identity", "comment_screening")

# Not one of BEDROCK_CATEGORIES above -- articles never call tracked_claude (they build their own
# lineage via common/costing.py, long before this module existed), so record_article_lineage below
# tallies them onto the same weekly row under this category by hand, from a finished lineage dict
# rather than from a single tracked call.
ARTICLES_CATEGORY = "articles"

FEEDBACK_GIVEN = "feedback_given"
# Reasons a whole feedback submission was thrown away -- see public_api_handler.py's
# _submit_feedback. Deliberately narrower than "every way a submission didn't succeed": a closed
# site (a limit, a lockdown) never let the reader try in the first place, and a bad/missing
# verification token or a filled-in honeypot is a bot being caught, not a person's feedback being
# rejected -- neither belongs under the same word a person would read as "my feedback was
# rejected." Only a comment that reached and failed the content screen counts here.
FEEDBACK_REJECTED_COMMENT = "feedback_rejected_comment"
LOOT_DROPS = "loot_drops"

# Prefix for common/lambda_timing.py's per-function running total: "{PREFIX}{function_name}",
# e.g. "lambda_ms_research_tick" -- milliseconds, summed across every invocation this week.
LAMBDA_MS_PREFIX = "lambda_ms_"

# cost_explorer_poll_handler.py's latest reading -- a refreshed snapshot (SET), not a counter
# (ADD) like everything else on this row. Not all of these are meant for the public Stats page;
# api_gateway_cost_as_of in particular is for the owner's own troubleshooting.
API_GATEWAY_COST_USD_30D = "api_gateway_cost_usd_30d"
API_GATEWAY_COST_AUD_30D = "api_gateway_cost_aud_30d"
API_GATEWAY_COST_AS_OF = "api_gateway_cost_as_of"


def _current_week_start(today: date | None = None) -> str:
    """The Monday of the current ISO week, as StatsCurrent's `week_start` (e.g. "2026-09-15")."""
    today = today or datetime.now(UTC).date()
    return (today - timedelta(days=today.weekday())).isoformat()


def _record_bedrock_call(category: str, result: dict) -> None:
    """Tally one tracked call's tokens and cost onto this week's row under `category`. Pricing
    uses the same lookup as per-article cost (common/costing.py), so a call with no registered
    price is counted in `{category}_unpriced_calls` rather than silently costing nothing -- the
    same "never guess, never drop it" rule the rest of this project's cost math follows."""
    pricing = pricing_for(result["model_id"])
    cost_aud = None
    call = {"input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"]}
    cost_usd = call_cost_usd(call, pricing)
    if cost_usd is not None:
        cost_aud = cost_usd * USD_TO_AUD_RATE

    updates: dict[str, int | Decimal] = {
        f"{category}_calls": 1,
        f"{category}_input_tokens": result["input_tokens"],
        f"{category}_output_tokens": result["output_tokens"],
    }
    if cost_aud is not None:
        updates[f"{category}_cost_aud"] = Decimal(str(cost_aud))
    else:
        updates[f"{category}_unpriced_calls"] = 1
    increment_current_stats(updates, _current_week_start())


def tracked_claude(category: str, prompt: str, model_id: str, *, max_tokens: int = 1024) -> str:
    """Like common.bedrock.invoke_claude (same prompt, same return: just the generated text), but
    the call's tokens and cost are tallied onto this week's Stats row under `category` first.
    `category` must be one of BEDROCK_CATEGORIES, so a typo here fails loudly at review time
    rather than silently opening a new, unaccounted-for bucket.

    The model call itself is never affected by tracking: it always happens and its text is always
    returned, even if recording the tally fails.
    """
    if category not in BEDROCK_CATEGORIES:
        raise ValueError(f"category must be one of {BEDROCK_CATEGORIES}, got {category!r}")
    result = invoke_model_tracked(prompt, model_id, max_tokens=max_tokens)
    try:
        _record_bedrock_call(category, result)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never lose the caller's answer
        print(f"stats_tracking: could not record a {category!r} call: {exc!r}")
    return result["text"]


def _record(updates: dict[str, int]) -> None:
    try:
        increment_current_stats(updates, _current_week_start())
    except Exception as exc:  # noqa: BLE001 - never let a stats write break the real action
        print(f"stats_tracking: could not record {list(updates)}: {exc!r}")


def record_feedback_given() -> None:
    """One more piece of feedback was stored (public_api_handler.py's _submit_feedback)."""
    _record({FEEDBACK_GIVEN: 1})


def record_feedback_rejected_comment() -> None:
    """A whole feedback submission was thrown away because its comment failed the content
    screen (public_api_handler.py's _submit_feedback -- see FEEDBACK_REJECTED_COMMENT above)."""
    _record({FEEDBACK_REJECTED_COMMENT: 1})


def record_loot_drop() -> None:
    """A piece of gear was just announced as a loot drop (common/musings.py's
    generate_and_store_loot_musing) -- an activity count, not a cost figure."""
    _record({LOOT_DROPS: 1})


def record_lambda_duration(function_name: str, duration_ms: int) -> None:
    """One invocation's self-timed wall-clock duration, added to `function_name`'s running total
    for the week (common/lambda_timing.py's track_lambda_duration -- this is never called directly
    outside that decorator)."""
    _record({f"{LAMBDA_MS_PREFIX}{function_name}": duration_ms})


def record_api_gateway_cost(cost_usd: Decimal, as_of: str) -> None:
    """The latest Cost Explorer reading for API Gateway spend (common/cost_explorer.py's
    fetch_api_gateway_cost_usd_30d, via cost_explorer_poll_handler.py) -- a refreshed snapshot of
    a rolling 30-day total, not a counter, so this SETs rather than ADDs: a repeat poll overwrites
    the previous reading instead of compounding it onto every prior one."""
    try:
        set_current_stats_fields(
            {
                API_GATEWAY_COST_USD_30D: cost_usd,
                API_GATEWAY_COST_AUD_30D: cost_usd * Decimal(str(USD_TO_AUD_RATE)),
                API_GATEWAY_COST_AS_OF: as_of,
            },
            _current_week_start(),
        )
    except Exception as exc:  # noqa: BLE001 - never let a stats write break the real poll
        print(f"stats_tracking: could not record api gateway cost: {exc!r}")


def record_article_lineage(lineage: dict) -> None:
    """Tally one just-drafted article's total spend (authoring + research, if any) onto this
    week's Stats row under `articles` -- called once per article, right after
    common.costing.build_lineage (and build_research_lineage, when there is one) finish
    (daily_cycle_handler.py, trending_digest_handler.py). Deliberately per-article, not per
    Bedrock call within it: that finer detail (which model, ideation vs draft vs title, which
    topic) stays common/stats.py's job, read live off the Articles table; this is one coarse
    weekly total sitting alongside it, so Weekly/Total Stats aren't missing the largest share of
    AI spend just because it isn't tracked the same way musings/weekly_reflection/etc. are.

    Reuses the lineage's own already-computed cost (`total_cost_aud` when there was a research
    component, else `cost_aud`) rather than re-pricing every call a second time -- that figure is
    already None under the same "never partially sum an unpriced call" rule
    common.costing.calculate_lineage_cost_aud follows, so an article with any unpriced call is
    counted here the same honest way: its tokens still count, `articles_unpriced_articles` (an
    article count, deliberately not named `_unpriced_calls` like every other category -- this is
    coarser, per-article granularity) goes up, and no cost is guessed at.

    Fails open like every other recorder here: a bookkeeping failure is logged and swallowed,
    never raised back into the caller mid-publish.
    """
    try:
        _record_article_lineage(lineage)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a drafted article
        print(f"stats_tracking: could not record article lineage: {exc!r}")


def _record_article_lineage(lineage: dict) -> None:
    research = lineage.get("research") or {}
    calls = list(lineage.get("calls") or []) + list(research.get("calls") or [])
    if not calls:
        # e.g. a non-financial topic's compliance review makes no Bedrock call at all -- nothing
        # actually happened here, so there is nothing to tally.
        return

    cost_aud = lineage.get("total_cost_aud") if research else lineage.get("cost_aud")
    updates: dict[str, int | Decimal] = {
        f"{ARTICLES_CATEGORY}_calls": len(calls),
        f"{ARTICLES_CATEGORY}_input_tokens": sum(int(call.get("input_tokens", 0)) for call in calls),
        f"{ARTICLES_CATEGORY}_output_tokens": sum(int(call.get("output_tokens", 0)) for call in calls),
    }
    if cost_aud is not None:
        updates[f"{ARTICLES_CATEGORY}_cost_aud"] = Decimal(str(cost_aud))
    else:
        updates[f"{ARTICLES_CATEGORY}_unpriced_articles"] = 1
    _record(updates)


# Keys on a StatsCurrent/StatsHistory row that are a refreshed snapshot (SET at write time), never
# additive across weeks -- everything else this module ever writes onto the row is a plain weekly
# counter, safe to ADD. Metadata fields describe the row itself, not something to fold into a
# total at all.
_SNAPSHOT_FIELDS = frozenset({API_GATEWAY_COST_USD_30D, API_GATEWAY_COST_AUD_30D, API_GATEWAY_COST_AS_OF})
_METADATA_FIELDS = frozenset({"stats_id", "week_start", "rolled_over_at"})


def split_for_rollover(row: dict) -> tuple[dict, dict]:
    """Split a completed week's StatsCurrent row into (additive, snapshot) for
    stats_rollover_handler.py to fold onto StatsHistory's permanent all-time row
    (common/dynamo.py's increment_stats_totals/set_stats_totals_fields): every counter this
    module records is safe to ADD across every week there has ever been; API Gateway's reading is
    a rolling 30-day snapshot, never additive, so the all-time row keeps only the latest one
    (SET), whichever week's rollover happens to carry it."""
    additive = {k: v for k, v in row.items() if k not in _SNAPSHOT_FIELDS and k not in _METADATA_FIELDS}
    snapshot = {k: v for k, v in row.items() if k in _SNAPSHOT_FIELDS}
    return additive, snapshot


# --- Shaping a row for the public Stats page (Observability enhancement, PR 4) -----------------
#
# StatsCurrent's row (-> "Weekly Stats") and StatsHistory's all-time row (-> "Total Stats") are
# the same shape, so one function shapes either. Not everything this module records reaches the
# page: the per-function Lambda breakdown stays internal for now (only the combined pipeline run
# time is public), and api_gateway_cost_as_of never leaves this module at all (owner-only
# troubleshooting, per the module docstring) -- both left out below, not merely unused.

HISTORIC_EXCLUDES_CURRENT_WEEK_NOTE = (
    "Excludes the current week, which is still in progress and counted separately under Weekly Stats."
)

_PUBLIC_CATEGORIES = (*BEDROCK_CATEGORIES, ARTICLES_CATEGORY)


def _category_view(row: dict, category: str) -> dict:
    cost = row.get(f"{category}_cost_aud")
    # "articles" is tallied per-article (record_article_lineage above), never per-call, so its
    # unpriced count is named _unpriced_articles at the storage layer -- see that function's own
    # docstring. Normalised to the same "unpriced" key here so the frontend can loop one way over
    # every category without caring which kind of unit it's coarser at.
    unpriced_key = (
        f"{category}_unpriced_articles" if category == ARTICLES_CATEGORY else f"{category}_unpriced_calls"
    )
    return {
        "category": category,
        "calls": int(row.get(f"{category}_calls", 0)),
        "input_tokens": int(row.get(f"{category}_input_tokens", 0)),
        "output_tokens": int(row.get(f"{category}_output_tokens", 0)),
        "cost_aud": float(cost) if cost is not None else None,
        "unpriced": int(row.get(unpriced_key, 0)),
    }


def public_view(row: dict) -> dict:
    """Shape a StatsCurrent or StatsHistory-all-time row (Decimal-valued, straight from
    common/dynamo.py) into what the public Stats page actually reads: Decimal turned into a plain
    float (cost) or int (every count), one entry per category, the reader-activity counters, one
    combined pipeline run-time figure in hours, and the API Gateway reading with its `_as_of`
    timestamp left out."""
    total_lambda_ms = sum(
        int(value)
        for key, value in row.items()
        if key.startswith(LAMBDA_MS_PREFIX) and isinstance(value, int | Decimal)
    )
    api_gateway_cost_usd = row.get(API_GATEWAY_COST_USD_30D)
    api_gateway_cost_aud = row.get(API_GATEWAY_COST_AUD_30D)

    return {
        "categories": [_category_view(row, category) for category in _PUBLIC_CATEGORIES],
        "feedback_given": int(row.get(FEEDBACK_GIVEN, 0)),
        "feedback_rejected_comment": int(row.get(FEEDBACK_REJECTED_COMMENT, 0)),
        "loot_drops": int(row.get(LOOT_DROPS, 0)),
        "pipeline_hours": round(total_lambda_ms / 3_600_000, 2),
        "api_gateway_cost_usd_30d": float(api_gateway_cost_usd) if api_gateway_cost_usd is not None else None,
        "api_gateway_cost_aud_30d": float(api_gateway_cost_aud) if api_gateway_cost_aud is not None else None,
    }
