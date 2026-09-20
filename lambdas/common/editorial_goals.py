"""Daily editorial goals for the crypto topic.

The crypto feed varies its content strategy from day to day instead of
re-summarising one price snapshot every day:

  ALTCOIN_DEEP_DIVE  a randomized pool of altcoins analysed against the
                     BTC/ETH market anchors (data-driven, quantitative)
  WEB_AGGREGATOR     a synthesis of crypto news published in the last 24h
  TREND_INVENTOR     an original interpretive framework built from the
                     anchors and the altcoin pool's multi-metric trends
  MARKET_NEWS        a synthesis of general financial-market news from the
                     last 24h with nothing to do with crypto (equities, rates,
                     inflation, central banks, earnings, commodities)

**Each day's goal is drawn at random, but as a pure function of the UTC
calendar date** (a random generator seeded with the date). Every day is an
independent draw -- no fixed cycle, and the same goal can come up twice in a
row -- yet it is stable for the whole day and needs no stored state. That
matters because the research-tick adapter (which fetches the data a goal
needs, hourly) and the daily cycle (which writes the article) compute it
independently and must always agree; an unseeded draw would pick a different
goal on every tick. An operator can pin one goal for a topic with
`adapter_config.editorial_goal`, e.g. to run one mode on demand.

Only topics using the crypto adapter have goals (`resolve_goal_for_topic`
returns None for everything else, which leaves every other topic's prompts
exactly as they were). The mandate/style text is data here, not branches in
daily_cycle_handler.py, so the core pipeline stays topic-agnostic
(docs/project-plan.md §2 rule 5).
"""

from __future__ import annotations

import random
from datetime import date
from enum import Enum

from common.adapters import CRYPTO_FEED_ADAPTER_KEY


class EditorialGoal(str, Enum):
    ALTCOIN_DEEP_DIVE = "ALTCOIN_DEEP_DIVE"
    WEB_AGGREGATOR = "WEB_AGGREGATOR"
    TREND_INVENTOR = "TREND_INVENTOR"
    MARKET_NEWS = "MARKET_NEWS"


# The goals a day's draw chooses from, in a fixed order (the draw indexes it).
GOAL_POOL: tuple[EditorialGoal, ...] = (
    EditorialGoal.ALTCOIN_DEEP_DIVE,
    EditorialGoal.WEB_AGGREGATOR,
    EditorialGoal.TREND_INVENTOR,
    EditorialGoal.MARKET_NEWS,
)

# Goals whose subject lies outside the adapter's own domain. The adapter-specific
# standing goal (e.g. the crypto feed's "asset cap distributions...") describes
# that domain, so it would contradict these days and is skipped for them.
OFF_ADAPTER_DOMAIN_GOALS = frozenset({EditorialGoal.MARKET_NEWS})

# The "Editorial Mandate" folded into the ideation prompt (P2).
EDITORIAL_MANDATES: dict[EditorialGoal, str] = {
    EditorialGoal.ALTCOIN_DEEP_DIVE: (
        "Focus on executing a strict historical and statistical breakdown of the random "
        "altcoins in the data. Contrast their 1-year and 3-month performance traits with "
        "Bitcoin and Ethereum's stability. Highlight short-term 5-day anomalies as "
        "tactical focal points."
    ),
    EditorialGoal.WEB_AGGREGATOR: (
        "Synthesize external perspectives across the provided web search context (the news "
        "items listed in the data). Identify recurring themes, conflicting opinions, and "
        "macro narratives shaping market sentiment today."
    ),
    EditorialGoal.TREND_INVENTOR: (
        "Deconstruct the current structural state of the market. Invent an unconventional "
        "framework, analogy, or metric ratio to interpret data anomalies in a way a retail "
        "reader wouldn't naturally see."
    ),
    EditorialGoal.MARKET_NEWS: (
        "Synthesize today's general financial-market news across the provided web search "
        "context (the news items listed in the data): equities, interest rates, inflation, "
        "central banks, earnings, commodities and the broader economy. Identify the dominant "
        "stories, how they connect, and the macro narrative they form. Crypto assets are out "
        "of scope for today; do not bring them in."
    ),
}

# The article style folded into the drafting prompt (P3).
ARTICLE_STYLES: dict[EditorialGoal, str] = {
    EditorialGoal.ALTCOIN_DEEP_DIVE: "structural, quantitative, and comparative data analysis.",
    EditorialGoal.WEB_AGGREGATOR: (
        "a current-events digest: narrative trends and sentiment synthesis. Only headlines "
        "and source names are available, not article bodies, so attribute claims to their "
        "source and don't invent detail beyond the headlines."
    ),
    EditorialGoal.TREND_INVENTOR: (
        "thought leadership: conceptual framing and long-form analysis, grounded in the "
        "supplied data."
    ),
    EditorialGoal.MARKET_NEWS: (
        "a market-news digest: the day's dominant stories and the macro narrative linking "
        "them. Only headlines and source names are available, not article bodies, so "
        "attribute claims to their source and don't invent detail beyond the headlines."
    ),
}


def goal_for_date(day: date) -> EditorialGoal:
    """The day's goal: a uniform random draw seeded by the date, so it is
    unpredictable from day to day but identical for everyone who asks about
    the same day."""
    return random.Random(f"editorial-goal:{day.isoformat()}").choice(GOAL_POOL)


def parse_goal(value: object) -> EditorialGoal | None:
    """Parse an operator-supplied goal name; None for anything unrecognised."""
    if isinstance(value, EditorialGoal):
        return value
    if isinstance(value, str):
        try:
            return EditorialGoal(value.strip().upper())
        except ValueError:
            return None
    return None


def goal_for_adapter_config(adapter_config: dict | None, day: date) -> EditorialGoal:
    """The pinned goal if `adapter_config.editorial_goal` names a valid one,
    else the draw for `day`."""
    pinned = parse_goal((adapter_config or {}).get("editorial_goal"))
    return pinned or goal_for_date(day)


def resolve_goal_for_topic(topic: dict, day: date) -> EditorialGoal | None:
    """The topic's editorial goal for `day`, or None if it has none (any
    topic not using the crypto adapter)."""
    if topic.get("adapter") != CRYPTO_FEED_ADAPTER_KEY:
        return None
    return goal_for_adapter_config(topic.get("adapter_config"), day)
