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

Web search (AgentCore fallback, lambdas/common/web_search.py) is tallied here too, but not as a
Bedrock category -- it is billed per query, not per token: `record_web_search_query` adds one
query and its fixed per-query price, and `record_web_search_fallback` counts a GDELT search that
failed over to AgentCore (how often GDELT is letting the site down). Plain weekly counters, so
they roll over and into the all-time row like every other counter.

Also PR 4: StatsHistory holds one more row beyond the normal one-per-completed-week ones --
`week_start = "all-time"` (a sentinel that can never collide with a real Monday date), a running
total kept in sync by stats_rollover_handler.py at every rollover (common/dynamo.py's
increment_stats_totals/set_stats_totals_fields). `split_for_rollover` below is what tells the
rollover job which of this row's fields are weekly counters (safe to ADD onto that running total)
versus a refreshed snapshot (API Gateway's reading -- SET, never summed). This makes "Total Stats"
a single get_item, never a scan-and-sum over every week that has ever existed.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from common.bedrock import invoke_model_tracked
from common.cost_explorer import BILL_CATEGORIES, bill_category
from common.costing import USD_TO_AUD_RATE, call_cost_usd, pricing_for
from common.dynamo import (
    increment_current_stats,
    list_stats_history_weeks,
    set_current_stats_fields,
    set_stats_history_week_fields,
    set_stats_totals_fields,
)
from common.model_pricing import default_model_entry

BEDROCK_CATEGORIES = ("musings", "weekly_reflection", "gear_identity", "comment_screening", "assistant")

# The operator's assistant (lambdas/ops_agent): every question it answers, on the page or started
# by Alexa+, is one agent run of several model calls. Its tokens and cost are tallied here per run
# (record_assistant_run below), so they are on the Stats page, in the `spend` tool's AI spend, and
# in each environment's own figure, the same week they are spent.
ASSISTANT_CATEGORY = "assistant"

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
# Web search: AgentCore queries (and their cost) and GDELT searches that failed over to it --
# counters (ADD), like everything else on the row except API Gateway's snapshot below.
WEB_SEARCH_AGENTCORE_QUERIES = "web_search_agentcore_queries"
WEB_SEARCH_AGENTCORE_COST_AUD = "web_search_agentcore_cost_aud"
WEB_SEARCH_GDELT_FALLBACKS = "web_search_gdelt_fallbacks"
# USD per AgentCore Web Search query: $7 per 1,000, from AWS's launch announcement for Web Search
# on Amazon Bedrock AgentCore. Confirm against https://aws.amazon.com/bedrock/agentcore/pricing/
# and update by hand if it changes -- a fixed approximation like USD_TO_AUD_RATE, not a live price.
AGENTCORE_WEB_SEARCH_USD_PER_QUERY = 0.007

API_GATEWAY_COST_USD_30D = "api_gateway_cost_usd_30d"
API_GATEWAY_COST_AUD_30D = "api_gateway_cost_aud_30d"
API_GATEWAY_COST_AS_OF = "api_gateway_cost_as_of"

# The same poll's reading of the *actual* AgentCore charge (common/cost_explorer.py) -- a SET
# snapshot like API Gateway's, shown beside the per-query estimate above so the two can be
# compared. agentcore_cost_as_of is owner-only, like api_gateway_cost_as_of.
AGENTCORE_COST_USD_30D = "agentcore_cost_usd_30d"
AGENTCORE_COST_AUD_30D = "agentcore_cost_aud_30d"
AGENTCORE_COST_AS_OF = "agentcore_cost_as_of"

# AWS WAF, from the same poll (common/cost_explorer.py): the rolling 30 days, this week so far
# (from the row's own Monday, so each week's history row keeps that week's WAF spend up to the
# last poll before its rollover), this calendar month so far, and last month in full -- every one
# a SET snapshot. The `*_month` / `*_previous_month` labels ("2026-10") say which month a figure
# is. waf_cost_as_of is owner-only, like api_gateway_cost_as_of.
WAF_COST_USD_30D = "waf_cost_usd_30d"
WAF_COST_AUD_30D = "waf_cost_aud_30d"
WAF_COST_USD_WEEK_TO_DATE = "waf_cost_usd_week_to_date"
WAF_COST_AUD_WEEK_TO_DATE = "waf_cost_aud_week_to_date"
WAF_COST_MONTH = "waf_cost_month"
WAF_COST_USD_MONTH_TO_DATE = "waf_cost_usd_month_to_date"
WAF_COST_AUD_MONTH_TO_DATE = "waf_cost_aud_month_to_date"
WAF_COST_PREVIOUS_MONTH = "waf_cost_previous_month"
WAF_COST_USD_PREVIOUS_MONTH = "waf_cost_usd_previous_month"
WAF_COST_AUD_PREVIOUS_MONTH = "waf_cost_aud_previous_month"
WAF_COST_AS_OF = "waf_cost_as_of"

# The whole AWS bill, every service by name (common/cost_explorer.py), in USD before tax; the
# Stats page shows only bill_category's three groups and a total. Three places, each a SET:
# - StatsCurrent: AWS_BILL_WEEK_USD, this week so far (to yesterday), refreshed by every poll.
# - Each StatsHistory week row: AWS_BILL_WEEK_USD again, overwritten with the *whole* week once
#   Cost Explorer has it (the rollover's copy misses the Sunday), and AWS_BILL_WEEK_COMPLETE.
# - StatsHistory's all-time row: AWS_BILL_TOTAL_USD, the sum of every complete week's bill,
#   recomputed by every poll, from AWS_BILL_TOTAL_SINCE (the first week counted).
# None of these is ever ADDed or carried onto the all-time row by the rollover (_WEEK_ONLY_FIELDS).
AWS_BILL_WEEK_USD = "aws_bill_week_usd"
AWS_BILL_WEEK_COMPLETE = "aws_bill_week_complete"
AWS_BILL_AS_OF = "aws_bill_as_of"
AWS_BILL_TOTAL_USD = "aws_bill_total_usd"
AWS_BILL_TOTAL_SINCE = "aws_bill_total_since"
AWS_BILL_TOTAL_WEEKS = "aws_bill_total_weeks"


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


def _assistant_pricing(model_id: str) -> dict | None:
    """The price for the agent's model. The registry first, as for every other call; but the
    agent's role reads no table it does not need, and the Models table is one of those, so a
    registry that cannot be read falls back to the built-in prices (common/model_pricing.py),
    which cover the model the agent is deployed with."""
    try:
        return pricing_for(model_id)
    except Exception:  # noqa: BLE001 - no registry here: the built-in table is the answer
        return default_model_entry(model_id)


def record_assistant_run(model_id: str, input_tokens: int, output_tokens: int, model_calls: int) -> None:
    """Tally one assistant run (a question answered, or a briefing Alexa+ started) onto this
    week's row under ASSISTANT_CATEGORY: `model_calls` calls, their tokens, and their cost. A run
    with no price is counted as unpriced calls, never as free. Never raises, and does nothing
    where the function has no Stats table (a local run, a test)."""
    if not os.environ.get("STATS_CURRENT_TABLE"):
        return
    try:
        calls = max(0, int(model_calls))
        tokens_in = max(0, int(input_tokens))
        tokens_out = max(0, int(output_tokens))
        if not calls and not tokens_in and not tokens_out:
            return
        updates: dict[str, int | Decimal] = {
            f"{ASSISTANT_CATEGORY}_calls": calls,
            f"{ASSISTANT_CATEGORY}_input_tokens": tokens_in,
            f"{ASSISTANT_CATEGORY}_output_tokens": tokens_out,
        }
        cost_usd = call_cost_usd(
            {"input_tokens": tokens_in, "output_tokens": tokens_out}, _assistant_pricing(model_id)
        )
        if cost_usd is not None:
            updates[f"{ASSISTANT_CATEGORY}_cost_aud"] = Decimal(str(cost_usd * USD_TO_AUD_RATE))
        else:
            updates[f"{ASSISTANT_CATEGORY}_unpriced_calls"] = calls
        increment_current_stats(updates, _current_week_start())
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the answer
        print(f"stats_tracking: could not record an assistant run: {type(exc).__name__}")


def _record(updates: dict[str, int | Decimal]) -> None:
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


def record_web_search_query() -> None:
    """One billable AgentCore Web Search query (common/web_search.py's AgentCoreProvider, called
    once per request the gateway answered with HTTP 2xx) and its fixed per-query price in AUD."""
    cost_aud = AGENTCORE_WEB_SEARCH_USD_PER_QUERY * USD_TO_AUD_RATE
    _record({WEB_SEARCH_AGENTCORE_QUERIES: 1, WEB_SEARCH_AGENTCORE_COST_AUD: Decimal(str(cost_aud))})


def record_web_search_fallback() -> None:
    """A GDELT search failed and was retried through AgentCore (common/web_search.py's
    search_web) -- an activity count; the query itself is counted by record_web_search_query."""
    _record({WEB_SEARCH_GDELT_FALLBACKS: 1})


def record_lambda_duration(function_name: str, duration_ms: int) -> None:
    """One invocation's self-timed wall-clock duration, added to `function_name`'s running total
    for the week (common/lambda_timing.py's track_lambda_duration -- this is never called directly
    outside that decorator)."""
    _record({f"{LAMBDA_MS_PREFIX}{function_name}": duration_ms})


def record_api_gateway_cost(cost_usd: Decimal, as_of: str) -> None:
    """The latest Cost Explorer reading for API Gateway spend (common/cost_explorer.py's
    fetch_costs, via cost_explorer_poll_handler.py) -- a refreshed snapshot of
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


def record_agentcore_cost(cost_usd: Decimal, as_of: str) -> None:
    """The latest Cost Explorer reading of the actual AgentCore charge over 30 days -- SET, not
    ADD, exactly like record_api_gateway_cost, for the same reason."""
    try:
        set_current_stats_fields(
            {
                AGENTCORE_COST_USD_30D: cost_usd,
                AGENTCORE_COST_AUD_30D: cost_usd * Decimal(str(USD_TO_AUD_RATE)),
                AGENTCORE_COST_AS_OF: as_of,
            },
            _current_week_start(),
        )
    except Exception as exc:  # noqa: BLE001 - never let a stats write break the real poll
        print(f"stats_tracking: could not record agentcore cost: {exc!r}")


def record_waf_cost(
    *,
    usd_30d: Decimal,
    usd_week_to_date: Decimal,
    usd_month_to_date: Decimal,
    usd_previous_month: Decimal,
    month: str,
    previous_month: str,
    as_of: str,
) -> None:
    """The latest Cost Explorer readings of the AWS WAF charge -- SET, not ADD, like
    record_api_gateway_cost: each is a fresh total of its window, so a repeat poll overwrites."""
    rate = Decimal(str(USD_TO_AUD_RATE))
    try:
        set_current_stats_fields(
            {
                WAF_COST_USD_30D: usd_30d,
                WAF_COST_AUD_30D: usd_30d * rate,
                WAF_COST_USD_WEEK_TO_DATE: usd_week_to_date,
                WAF_COST_AUD_WEEK_TO_DATE: usd_week_to_date * rate,
                WAF_COST_MONTH: month,
                WAF_COST_USD_MONTH_TO_DATE: usd_month_to_date,
                WAF_COST_AUD_MONTH_TO_DATE: usd_month_to_date * rate,
                WAF_COST_PREVIOUS_MONTH: previous_month,
                WAF_COST_USD_PREVIOUS_MONTH: usd_previous_month,
                WAF_COST_AUD_PREVIOUS_MONTH: usd_previous_month * rate,
                WAF_COST_AS_OF: as_of,
            },
            _current_week_start(),
        )
    except Exception as exc:  # noqa: BLE001 - never let a stats write break the real poll
        print(f"stats_tracking: could not record waf cost: {exc!r}")


def record_aws_bill(
    *, week_to_date: dict[str, Decimal], complete_weeks: dict[str, dict[str, Decimal]], as_of: str
) -> dict:
    """Store one poll's whole-bill reading (see AWS_BILL_WEEK_USD above for where each part goes)
    and return what was written: {"weeks_filled": [...], "total_weeks": n}. Each step fails open
    on its own, like every recorder here: the poll's other readings must still land."""
    result: dict = {"weeks_filled": [], "total_weeks": None}
    try:
        set_current_stats_fields(
            {AWS_BILL_WEEK_USD: week_to_date, AWS_BILL_AS_OF: as_of}, _current_week_start()
        )
    except Exception as exc:  # noqa: BLE001 - never let a stats write break the real poll
        print(f"stats_tracking: could not record this week's aws bill: {exc!r}")

    for week_start, services in sorted(complete_weeks.items()):
        try:
            # False for a week the rollover never wrote (before Stats existed): nothing to fill.
            if set_stats_history_week_fields(
                week_start,
                {AWS_BILL_WEEK_USD: services, AWS_BILL_WEEK_COMPLETE: True, AWS_BILL_AS_OF: as_of},
            ):
                result["weeks_filled"].append(week_start)
        except Exception as exc:  # noqa: BLE001
            print(f"stats_tracking: could not fill week {week_start}'s aws bill: {exc!r}")

    # The all-time figure is the sum of every complete week, recomputed rather than ADDed: a week
    # re-read with AWS's late corrections then can't be counted twice.
    try:
        total: dict[str, Decimal] = {}
        weeks = []
        for row in list_stats_history_weeks():
            bill = row.get(AWS_BILL_WEEK_USD)
            if not row.get(AWS_BILL_WEEK_COMPLETE) or not isinstance(bill, dict):
                continue
            weeks.append(row["week_start"])
            for service, usd in bill.items():
                total[service] = total.get(service, Decimal("0")) + Decimal(usd)
        if weeks:
            set_stats_totals_fields(
                {
                    AWS_BILL_TOTAL_USD: total,
                    AWS_BILL_TOTAL_SINCE: min(weeks),
                    AWS_BILL_TOTAL_WEEKS: len(weeks),
                    AWS_BILL_AS_OF: as_of,
                }
            )
        result["total_weeks"] = len(weeks)
    except Exception as exc:  # noqa: BLE001
        print(f"stats_tracking: could not total the aws bill: {exc!r}")
    return result


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
        tally = _lineage_tally(lineage)
        if tally is not None:
            _record(to_stats_updates(tally))
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a drafted article
        print(f"stats_tracking: could not record article lineage: {exc!r}")


def _lineage_tally(lineage: dict) -> dict[str, int | float] | None:
    """One article's raw calls/tokens/cost tally under `articles`, in native numbers -- Decimal
    is a storage concern, added only at the write boundary (to_stats_updates below), not here.
    None when there is nothing to tally (e.g. a non-financial topic's compliance review makes no
    Bedrock call at all). Shared by record_article_lineage (the live, per-article path, right
    after an article is drafted) and plan_articles_backfill (the one-time catch-up over every
    article that already exists, PR 5) so both tally a lineage exactly the same way.

    Reuses the lineage's own already-computed cost (`total_cost_aud` when there was a research
    component, else `cost_aud`) rather than re-pricing every call a second time -- that figure is
    already None under the same "never partially sum an unpriced call" rule
    common.costing.calculate_lineage_cost_aud follows, so an article with any unpriced call is
    counted here the same honest way: its tokens still count, `articles_unpriced_articles` (an
    article count, deliberately not named `_unpriced_calls` like every other category -- this is
    coarser, per-article granularity) goes up, and no cost is guessed at.
    """
    research = lineage.get("research") or {}
    calls = list(lineage.get("calls") or []) + list(research.get("calls") or [])
    if not calls:
        return None

    cost_aud = lineage.get("total_cost_aud") if research else lineage.get("cost_aud")
    tally: dict[str, int | float] = {
        f"{ARTICLES_CATEGORY}_calls": len(calls),
        f"{ARTICLES_CATEGORY}_input_tokens": sum(int(call.get("input_tokens", 0)) for call in calls),
        f"{ARTICLES_CATEGORY}_output_tokens": sum(int(call.get("output_tokens", 0)) for call in calls),
    }
    if cost_aud is not None:
        tally[f"{ARTICLES_CATEGORY}_cost_aud"] = cost_aud
    else:
        tally[f"{ARTICLES_CATEGORY}_unpriced_articles"] = 1
    return tally


def to_stats_updates(tally: dict[str, int | float]) -> dict[str, int | Decimal]:
    """`tally`'s native numbers, Decimal-wrapped wherever DynamoDB needs it (its boto3 resource
    rejects a native float) -- the one conversion point every writer of a StatsCurrent/
    StatsHistory row shares (record_article_lineage above; the one-time backfill admin route,
    which sums several articles' tallies together before writing, so this has to be a separate
    step from _lineage_tally rather than folded into it)."""
    return {key: Decimal(str(value)) if isinstance(value, float) else value for key, value in tally.items()}


def plan_articles_backfill(articles: list[dict]) -> dict:
    """Sum every existing article's lineage into one totals dict, shaped exactly like what
    record_article_lineage would have tallied onto StatsCurrent (and, via a rollover, onto
    StatsHistory's all-time row) had it existed when each article was drafted -- the one-time
    catch-up for articles drafted before this category existed (Observability enhancement,
    PR 5). Pure and read-only, the same "caller already fetched the data" shape as
    lineage_tools.py's plan_backfill -- the admin route decides whether to actually write this.
    """
    totals: dict[str, int | float] = {}
    included = 0
    for article in articles:
        lineage = article.get("lineage")
        if not lineage:
            continue
        tally = _lineage_tally(lineage)
        if tally is None:
            continue
        included += 1
        for key, value in tally.items():
            totals[key] = totals.get(key, 0) + value
    return {"examined": len(articles), "included": included, "totals": totals}


# StatsHistory's reserved marker for "the one-time articles backfill has already run" -- a
# sentinel week_start, like the all-time row's own, that can never collide with a real Monday
# date. Written via the same conditional put_stats_history_row every real week's row already
# uses, so a retried or duplicated backfill call can never fold these articles into the running
# total twice.
ARTICLES_BACKFILL_MARKER = "articles-backfill"


# Keys on a StatsCurrent/StatsHistory row that are a refreshed snapshot (SET at write time), never
# additive across weeks -- everything else this module ever writes onto the row is a plain weekly
# counter, safe to ADD. Metadata fields describe the row itself, not something to fold into a
# total at all.
_SNAPSHOT_FIELDS = frozenset(
    {
        API_GATEWAY_COST_USD_30D,
        API_GATEWAY_COST_AUD_30D,
        API_GATEWAY_COST_AS_OF,
        AGENTCORE_COST_USD_30D,
        AGENTCORE_COST_AUD_30D,
        AGENTCORE_COST_AS_OF,
        WAF_COST_USD_30D,
        WAF_COST_AUD_30D,
        WAF_COST_USD_WEEK_TO_DATE,
        WAF_COST_AUD_WEEK_TO_DATE,
        WAF_COST_MONTH,
        WAF_COST_USD_MONTH_TO_DATE,
        WAF_COST_AUD_MONTH_TO_DATE,
        WAF_COST_PREVIOUS_MONTH,
        WAF_COST_USD_PREVIOUS_MONTH,
        WAF_COST_AUD_PREVIOUS_MONTH,
        WAF_COST_AS_OF,
    }
)
_METADATA_FIELDS = frozenset({"stats_id", "week_start", "rolled_over_at"})
# Kept on the week's own row only, never folded onto the all-time row: the whole-bill reading is
# a map (not a number to ADD), and its all-time figure is recomputed by the poll instead.
_WEEK_ONLY_FIELDS = frozenset({AWS_BILL_WEEK_USD, AWS_BILL_WEEK_COMPLETE, AWS_BILL_AS_OF})


def split_for_rollover(row: dict) -> tuple[dict, dict]:
    """Split a completed week's StatsCurrent row into (additive, snapshot) for
    stats_rollover_handler.py to fold onto StatsHistory's permanent all-time row
    (common/dynamo.py's increment_stats_totals/set_stats_totals_fields): every counter this
    module records is safe to ADD across every week there has ever been; API Gateway's reading is
    a rolling 30-day snapshot, never additive, so the all-time row keeps only the latest one
    (SET), whichever week's rollover happens to carry it."""
    additive = {
        k: v
        for k, v in row.items()
        if k not in _SNAPSHOT_FIELDS and k not in _METADATA_FIELDS and k not in _WEEK_ONLY_FIELDS
    }
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
    web_search_queries = int(row.get(WEB_SEARCH_AGENTCORE_QUERIES, 0))
    web_search_cost = row.get(WEB_SEARCH_AGENTCORE_COST_AUD)
    agentcore_actual_aud = row.get(AGENTCORE_COST_AUD_30D)

    return {
        "categories": [_category_view(row, category) for category in _PUBLIC_CATEGORIES],
        "feedback_given": int(row.get(FEEDBACK_GIVEN, 0)),
        "feedback_rejected_comment": int(row.get(FEEDBACK_REJECTED_COMMENT, 0)),
        "loot_drops": int(row.get(LOOT_DROPS, 0)),
        "pipeline_hours": round(total_lambda_ms / 3_600_000, 2),
        "api_gateway_cost_usd_30d": float(api_gateway_cost_usd) if api_gateway_cost_usd is not None else None,
        "api_gateway_cost_aud_30d": float(api_gateway_cost_aud) if api_gateway_cost_aud is not None else None,
        # Every query is priced (a fixed per-query rate), so no queries is a real $0, and a
        # missing cost alongside queries (which record_web_search_query never writes) is None.
        "web_search": {
            "agentcore_queries": web_search_queries,
            "agentcore_cost_aud": (
                float(web_search_cost)
                if web_search_cost is not None
                else (0.0 if web_search_queries == 0 else None)
            ),
            "gdelt_fallbacks": int(row.get(WEB_SEARCH_GDELT_FALLBACKS, 0)),
            # The actual charge from the AWS bill (Cost Explorer, rolling 30 days, ~24h lag) --
            # None until the first poll has run; its as_of stays owner-only.
            "agentcore_actual_cost_aud_30d": (
                float(agentcore_actual_aud) if agentcore_actual_aud is not None else None
            ),
        },
        "waf": _waf_view(row),
        "aws_bill": _aws_bill_view(row),
    }


def _aws_bill_view(row: dict) -> dict | None:
    """The whole AWS bill in AUD, as bill_category's three groups and a total -- never per service
    (that detail stays in the table). StatsHistory's all-time row carries the sum of every
    complete week (`since` is the first one); StatsCurrent carries this week so far (`since` is
    None). None until the first poll has run."""
    services, since = row.get(AWS_BILL_TOTAL_USD), row.get(AWS_BILL_TOTAL_SINCE)
    if services is None:
        services, since = row.get(AWS_BILL_WEEK_USD), None
    if not isinstance(services, dict):
        return None
    rate = Decimal(str(USD_TO_AUD_RATE))
    totals = {category: Decimal("0") for category in BILL_CATEGORIES}
    for service, usd in services.items():
        totals[bill_category(service)] += Decimal(usd)
    return {
        "categories": [
            {"category": category, "cost_aud": float(totals[category] * rate)} for category in BILL_CATEGORIES
        ],
        "total_aud": float(sum(totals.values()) * rate),
        "since": since,
    }


def _waf_view(row: dict) -> dict | None:
    """The AWS WAF readings, from the AWS bill (~24h lag); None until the first poll has run. Its
    as_of stays owner-only, like the other cost readings'."""
    if row.get(WAF_COST_AUD_30D) is None:
        return None

    def aud(key: str) -> float | None:
        value = row.get(key)
        return float(value) if value is not None else None

    return {
        "cost_aud_30d": aud(WAF_COST_AUD_30D),
        "cost_aud_week_to_date": aud(WAF_COST_AUD_WEEK_TO_DATE),
        "month": row.get(WAF_COST_MONTH),
        "cost_aud_month_to_date": aud(WAF_COST_AUD_MONTH_TO_DATE),
        "previous_month": row.get(WAF_COST_PREVIOUS_MONTH),
        "cost_aud_previous_month": aud(WAF_COST_AUD_PREVIOUS_MONTH),
    }
