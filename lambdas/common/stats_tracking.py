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
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from common.bedrock import invoke_model_tracked
from common.costing import USD_TO_AUD_RATE, call_cost_usd, pricing_for
from common.dynamo import increment_current_stats, set_current_stats_fields

BEDROCK_CATEGORIES = ("musings", "weekly_reflection", "gear_identity", "comment_screening")

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
