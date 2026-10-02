"""Adapter for crypto market data (CoinGecko) and crypto news (web search).

Bitcoin and Ethereum are the structural *market anchors*: every snapshot
carries their price and multi-timeframe change, and everything else is read
relative to them. What else a snapshot carries depends on the day's
editorial goal (common/editorial_goals.py -- drawn at random each day, but
seeded by the UTC date, so this adapter and the daily cycle always agree on it):

  ALTCOIN_DEEP_DIVE / TREND_INVENTOR
      a pool of 10 altcoins sampled from the top 200 by market cap (never
      the anchors, stablecoins, tokenized funds, or wrapped/staked/bridged
      derivatives), each enriched from one CoinGecko history call with its
      3-month and 1-year change and a 5-day anomaly read (price spike and/or
      volume surge), plus the latest crypto headlines as context.
  WEB_AGGREGATOR
      5-15 crypto news items from the last 24h, via common/web_search.py.
  MARKET_NEWS
      5-15 *general financial-market* news items from the last 24h with no
      crypto in them (crypto headlines are filtered out). No CoinGecko call
      and no anchors that day: `market_anchors` is empty.

**The pool is a fresh random draw on every tick** -- unseeded, so no two ticks
watch the same coins by design, and each tick has genuinely new information to
report. (Only the *goal* is seeded by the date, because this adapter and the
daily cycle must agree on it.) Coins already analysed earlier the same UTC day
(`analyzed_today`, carried in the snapshot) are left out of the draw while enough
others remain, so the day's ticks cover fresh ground; once fewer than 10 unseen
coins are left the exclusion lapses rather than shrinking the pool. A snapshot is
material on the first observation, a new UTC day, a goal change, any coin newly
sampled into the pool, or any headline not already reported. Price moves are not
a trigger. With a new pool each tick, that means a Finding per tick on analysis
days -- the research interval (common/research_schedule.py) is the dial for cost.

**History is fetched for each tick's new pool.** That is up to 10 CoinGecko
history calls per tick, and the public keyless API answers 429 well below that
(observed live: 3 of 10 failed even with backoff), so set a CoinGecko key (below)
for this to be dependable. A coin whose history can't be fetched is dropped and
the tick fails only if fewer than MIN_POOL_SIZE survive (the next heartbeat
simply draws again). The adapter still opts into `uses_previous_state`: a coin
that is drawn again the same day (only once the unseen ones run out) reuses its
already-fetched metrics, refreshing just its current price.

Requests that are made run concurrently (asyncio, a small semaphore) through
common/http_retry.py's exponential backoff; a coin whose history can't be
fetched is dropped, and the run fails only if fewer than MIN_POOL_SIZE coins
survive.

**An optional CoinGecko API key** raises the rate limit. In AWS it is a SecureString in SSM
Parameter Store, named by `COINGECKO_API_KEY_PARAMETER` (set by Terraform on the two crypto
Lambdas) and read once per cold start; a plain `COINGECKO_API_KEY` is still honoured, for local
runs. The plan is in `COINGECKO_API_PLAN`. See CoinGeckoClient: if the keyed request is throttled,
errors, or the key is rejected -- or there is no key at all -- the same request goes to the public
keyless API, so the key can only ever help.

This adapter contains no domain branches in the core pipeline
(docs/project-plan.md §6): the daily goal draw lives in editorial_goals.py, and
the financial-topic safety rules (forced is_financial, mandatory moderation,
drafting guidance, disclaimer) remain in admin_api_handler.py and
common/compliance.py, unchanged.

adapter_config (all optional): `editorial_goal` pins one goal instead of
the daily draw; `web_search_queries` / `market_news_queries` /
`web_search_provider` override the news searches. The old `coin_ids` setting is no longer used --
the anchors are always Bitcoin and Ethereum.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import requests
from botocore.exceptions import ClientError

from common.editorial_goals import EditorialGoal, goal_for_adapter_config, parse_goal
from common.editorial_resolver import resolve_editorial_goals
from common.http_retry import get_json_with_backoff
from common.relevance import matches_keywords, research_relevance_rule, topic_label
from common.web_search import search_web

from .base import Adapter, render_review_evidence

PUBLIC_BASE_URL = "https://api.coingecko.com/api/v3"
PRO_BASE_URL = "https://pro-api.coingecko.com/api/v3"
MARKETS_PATH = "/coins/markets"
HISTORY_PATH = "/coins/{coin_id}/market_chart"
USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# The API key never comes from adapter_config: that is stored in DynamoDB and readable through
# the admin API, so a secret there would leak. In AWS it is read from SSM Parameter Store (the
# parameter's name in API_KEY_PARAMETER_ENV, set by Terraform on the two crypto Lambdas); a plain
# API_KEY_ENV value, if set, wins -- that is for running locally.
API_KEY_ENV = "COINGECKO_API_KEY"
API_KEY_PARAMETER_ENV = "COINGECKO_API_KEY_PARAMETER"
API_PLAN_ENV = "COINGECKO_API_PLAN"
# plan -> (base URL, header the key travels in). "demo" is the free key and
# uses the same host as the public API; "pro" is a paid key on its own host.
API_PLANS = {
    "demo": (PUBLIC_BASE_URL, "x-cg-demo-api-key"),
    "pro": (PRO_BASE_URL, "x-cg-pro-api-key"),
}
DEFAULT_API_PLAN = "demo"
# A keyed request still failing after this many attempts falls back to the
# public API rather than burning the run's time budget on a throttled key.
KEYED_MAX_ATTEMPTS = 3
PUBLIC_MAX_ATTEMPTS = 4
_KEY_REJECTED_STATUS_CODES = frozenset({401, 403})

ANCHOR_IDS = ("bitcoin", "ethereum")
MARKETS_PER_PAGE = 200
POOL_SIZE = 10
MIN_POOL_SIZE = 5

# Deliberately gentle: CoinGecko's unauthenticated API tolerates only a
# handful of requests a minute, and more parallelism just earns more 429s.
HISTORY_CONCURRENCY = 2
# Leaves room in the 120s Lambda timeout for the markets call and the
# Bedrock summary that follows.
HISTORY_TIME_BUDGET_SECONDS = 85.0
_HISTORY_TIMEOUT_SECONDS = 15.0
_HISTORY_DAYS = 365

# 5-day anomaly read: a daily move at least this large is a "spike" (higher
# than for the anchors' typical moves, since altcoins are far more volatile);
# a day's volume this many times the trailing 30-day average is a "surge".
ANOMALY_DAILY_MOVE_PERCENT = 8.0
VOLUME_SURGE_RATIO = 2.0

WEB_MAX_RESULTS = 15
WEB_MAX_AGE_HOURS = 24
# The fresh-data review's headlines get their own budget inside the review's overall 45s
# fetch limit (common/fresh_review.py's FETCH_TIMEOUT_SECONDS). GDELT can take 80s+, and
# without this a slow search used up the whole limit, so the review came back
# "unavailable" even though the prices (the part that actually checks claims) had
# arrived in under a second. Past this the review simply goes without headlines.
REVIEW_HEADLINES_BUDGET_SECONDS = 15.0
# Headlines not yet reported that make a tick worth a Finding. One: novelty is
# already judged against everything reported (Adapter.known_keys), so a headline
# that is genuinely new is information worth recording.
WEB_NEW_RESULTS_THRESHOLD = 1
DEFAULT_WEB_QUERY = "(bitcoin OR ethereum OR cryptocurrency OR crypto)"
# Search backends match full page text, so a title filter drops the
# tangential pages (an "Interpol tool" story that mentions crypto in passing).
# Matched as whole words (common/relevance.py), so "eth" can't match "together";
# a trailing "*" is a prefix match ("crypto*" covers "cryptocurrency").
CRYPTO_TITLE_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "ether", "eth", "crypto*", "coin*", "token*",
    "blockchain", "defi", "altcoin", "stablecoin", "solana", "xrp", "binance",
    "coinbase", "etf", "web3", "nft",
]  # fmt: skip

# MARKET_NEWS day: general finance headlines with no crypto in them. GDELT
# needs multi-word phrases quoted, and the title filter keeps only headlines
# that are actually about markets/economy; anything crypto-flavoured is then
# excluded (bare "etf" isn't -- most ETF news is equities/bonds).
MARKET_NEWS_QUERY = (
    '("stock market" OR equities OR "interest rates" OR inflation OR "central bank" '
    'OR earnings OR "bond yields" OR "oil prices" OR economy)'
)
MARKET_NEWS_TITLE_KEYWORDS = [
    "stock*", "market*", "equit*", "shares", "wall street", "s&p", "nasdaq", "dow",
    "rate", "rates", "inflation", "fed", "federal reserve", "central bank", "ecb",
    "earnings", "economy", "economic", "gdp", "jobs", "unemployment", "recession",
    "bond*", "yield*", "treasur*", "oil", "commodit*", "tariff*", "ipo", "dollar",
]  # fmt: skip
CRYPTO_EXCLUSION_KEYWORDS = [k for k in CRYPTO_TITLE_KEYWORDS if k != "etf"]

SUMMARY_STATE_MAX_CHARS = 9000

# Things that are not "altcoins" for an altcoin analysis. Stablecoins: known
# ids catch the big ones and the peg check catches the long tail without a
# second API call (it needs a *flat week* as well as a ~$1 price, so a
# volatile coin that merely trades around $1 stays eligible). Tokenized
# funds / treasuries and other NAV-style assets (seen live in the top 200:
# "Janus Henderson Anemoy Treasury Fund" at $1.12, an EUR swap fund at $1.16)
# don't sit at $1 but barely move over any window, which the flatness check
# catches whatever their price or currency. Gold-backed tokens (tether-gold, pax-gold) track
# the metal, not a peg, so at thousands of dollars and gold-volatile they pass neither check
# and are listed by id.
KNOWN_STABLECOIN_IDS = frozenset({
    "tether", "usd-coin", "dai", "usds", "ethena-usde", "first-digital-usd", "usdd",
    "paypal-usd", "true-usd", "frax", "usd1-wlfi", "global-dollar", "ripple-usd",
    "gemini-dollar", "pax-dollar", "binance-usd", "usdb", "stasis-eurs", "tether-gold",
    "pax-gold",
})  # fmt: skip
_PEG_BAND = 0.03
_PEG_MAX_24H_MOVE = 1.0
_PEG_MAX_7D_MOVE = 2.0
_FLAT_MAX_24H_MOVE = 0.5
_FLAT_MAX_7D_MOVE = 1.0
_FLAT_MAX_30D_MOVE = 2.0
_NOT_AN_ALTCOIN_WORDS = re.compile(r"\b(wrapped|staked|bridged|treasury|fund)\b")

_DAY_MS = 86_400_000


# --- pure helpers ---------------------------------------------------------


def _round_price(value: float) -> float:
    """2 decimals for prices >= $1, 6 significant figures below (a sub-cent
    coin rounded to 2 decimals would read as $0.00)."""
    return round(value, 2) if abs(value) >= 1 else float(f"{value:.6g}")


def _format_usd(value: float) -> str:
    """"$81,584", "$2,648.78", "$0.0811858", "$0.00000033139" -- whole
    dollars without a trailing ".00", cents when there are any, and
    significant figures (never scientific notation) below $1."""
    if abs(value) < 1:
        return "$" + format(Decimal(f"{value:.6g}"), "f")
    if value == round(value):
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def _pct_change(now: float, past: float | None) -> float | None:
    if not past:
        return None
    return round((now - past) / past * 100, 2)


def _market_pct(coin: dict, window: str) -> float | None:
    """A coin's % change over `window` ("24h", "7d", "30d", "1y") from a
    /coins/markets item, rounded; None if CoinGecko didn't report it."""
    value = coin.get(f"price_change_percentage_{window}_in_currency")
    if value is None and window == "24h":
        value = coin.get("price_change_percentage_24h")
    return None if value is None else round(float(value), 2)


def is_stablecoin(coin: dict) -> bool:
    if coin.get("id") in KNOWN_STABLECOIN_IDS:
        return True
    price = coin.get("current_price")
    if price is None or abs(price - 1.0) > _PEG_BAND:
        return False
    return (
        abs(_market_pct(coin, "24h") or 0.0) < _PEG_MAX_24H_MOVE
        and abs(_market_pct(coin, "7d") or 0.0) < _PEG_MAX_7D_MOVE
    )


def is_flat_asset(coin: dict) -> bool:
    """Barely moving across the 24h, 7d and 30d windows -- a pegged or
    NAV-style asset rather than a freely-trading altcoin. Unknown (missing)
    windows never count as flat: no data is not evidence of stability."""
    moves = (_market_pct(coin, "24h"), _market_pct(coin, "7d"), _market_pct(coin, "30d"))
    if any(move is None for move in moves):
        return False
    return (
        abs(moves[0]) < _FLAT_MAX_24H_MOVE
        and abs(moves[1]) < _FLAT_MAX_7D_MOVE
        and abs(moves[2]) < _FLAT_MAX_30D_MOVE
    )


def is_altcoin_candidate(coin: dict) -> bool:
    if coin.get("id") in ANCHOR_IDS or is_stablecoin(coin) or is_flat_asset(coin):
        return False
    if coin.get("current_price") is None or coin.get("market_cap_rank") is None:
        return False
    label = f"{coin.get('id', '')} {coin.get('name', '')}".lower()
    return _NOT_AN_ALTCOIN_WORDS.search(label) is None


def select_altcoin_pool(
    markets: list[dict],
    *,
    exclude_ids: frozenset[str] | set[str] = frozenset(),
    rng: random.Random | None = None,
) -> list[dict]:
    """Sample POOL_SIZE altcoins entirely at random: a fresh, unseeded draw on every
    call, so no two ticks watch the same coins by design.

    Coins in `exclude_ids` (those already analysed earlier the same UTC day) are
    left out while enough others remain, so a tick's pool is about coins not yet
    looked at today. Once fewer than POOL_SIZE unseen ones are left the exclusion is
    dropped rather than shrinking the pool.
    """
    eligible = sorted(
        (coin for coin in markets if is_altcoin_candidate(coin)),
        key=lambda coin: coin["market_cap_rank"],
    )
    fresh = [coin for coin in eligible if coin["id"] not in exclude_ids]
    candidates = fresh if len(fresh) >= min(POOL_SIZE, len(eligible)) else eligible
    return (rng or random.SystemRandom()).sample(candidates, min(POOL_SIZE, len(candidates)))


def _nearest_price(prices: list[list[float]], target_ts: float) -> float:
    return min(prices, key=lambda point: abs(point[0] - target_ts))[1]


def _anomaly_5d(prices: list[list[float]], volumes: list[list[float]]) -> dict:
    """Read the last 5 daily moves for a spike (and a volume surge).

    Uses daily points, so "high/low" are the highest/lowest daily *closes*
    (CoinGecko's free daily series has no intraday range); the final point is
    the live price rather than a completed day's close.
    """
    recent = prices[-6:]
    moves = [
        (recent[i][1] - recent[i - 1][1]) / recent[i - 1][1] * 100
        for i in range(1, len(recent))
        if recent[i - 1][1]
    ]
    if not moves:
        return {"has_spike": False, "direction": None, "max_deviation_percent": 0.0,
                "context": "not enough recent data"}  # fmt: skip

    biggest = max(range(len(moves)), key=lambda i: abs(moves[i]))
    move = moves[biggest]
    day_of_five = biggest + 1
    has_spike = abs(move) >= ANOMALY_DAILY_MOVE_PERCENT

    surge = None
    if len(volumes) >= 36:
        baseline = sum(point[1] for point in volumes[-36:-6]) / 30
        if baseline > 0:
            ratios = [(volumes[-6 + i][1] / baseline, i) for i in range(1, 6)]
            best_ratio, best_index = max(ratios)
            if best_ratio >= VOLUME_SURGE_RATIO:
                surge = (best_ratio, best_index)

    if has_spike and surge:
        context = (
            f"{move:+.1f}% move on day {day_of_five} of the last 5; volume "
            f"{surge[0]:.1f}x the 30-day average on day {surge[1]}"
        )
    elif has_spike:
        context = f"{move:+.1f}% move on day {day_of_five} of the last 5"
    elif surge:
        context = f"no price spike; volume {surge[0]:.1f}x the 30-day average on day {surge[1]}"
    else:
        context = f"no spike: largest daily move {move:+.1f}%"

    return {
        "has_spike": has_spike,
        "direction": "up" if move > 0 else "down" if move < 0 else None,
        "max_deviation_percent": round(abs(move), 1),
        "context": context,
    }


def compute_coin_metrics(history: dict, price_now: float) -> dict | None:
    """Turn one /market_chart response (daily prices + volumes over ~1 year)
    into the snapshot's per-coin metrics; None if there isn't enough history
    to say anything (a brand-new listing).

    A coin younger than ~3 months has no 3-month baseline and younger than
    ~1 year has no 1-year one -- those fields are None rather than guessed.
    """
    prices = history.get("prices") or []
    volumes = history.get("total_volumes") or []
    if len(prices) < 7 or not price_now:
        return None

    last_ts = prices[-1][0]
    span_days = (last_ts - prices[0][0]) / _DAY_MS
    price_3m = _nearest_price(prices, last_ts - 90 * _DAY_MS) if span_days >= 88 else None
    price_1y = prices[0][1] if span_days >= 355 else None
    closes_5d = [_round_price(point[1]) for point in prices[-5:]]

    return {
        "price_now": _round_price(price_now),
        "price_3m_ago": _round_price(price_3m) if price_3m else None,
        "price_1y_ago": _round_price(price_1y) if price_1y else None,
        "change_3m_percent": _pct_change(price_now, price_3m),
        "change_1y_percent": _pct_change(price_now, price_1y),
        "sparkline_5d": closes_5d,
        "high_5d": max(closes_5d),
        "low_5d": min(closes_5d),
        "anomaly_5d": _anomaly_5d(prices, volumes),
    }


def _refresh_entry(previous: dict, market_coin: dict) -> dict:
    """A pool entry carried forward from earlier today: only what a price tick
    changes is recomputed (current price and the changes measured from the
    stored 3-month/1-year baselines); the day's history-derived fields
    (baselines, 5-day sparkline and anomaly) are kept as fetched."""
    price_now = market_coin["current_price"]
    metrics = dict(previous["metrics"])
    metrics["price_now"] = _round_price(price_now)
    metrics["change_3m_percent"] = _pct_change(price_now, metrics.get("price_3m_ago"))
    metrics["change_1y_percent"] = _pct_change(price_now, metrics.get("price_1y_ago"))
    return {**previous, "market_cap_rank": market_coin.get("market_cap_rank"), "metrics": metrics}


def _anchor_summary(state: dict) -> str:
    parts = []
    for coin_id in ANCHOR_IDS:
        anchor = (state.get("market_anchors") or {}).get(coin_id)
        if not anchor:
            continue
        change = anchor.get("change_24h")
        change_text = f" ({change:+.2f}% 24h)" if change is not None else ""
        parts.append(f"{anchor.get('name', coin_id)} {_format_usd(anchor['price'])}{change_text}")
    return ", ".join(parts)


def _status_code(exc: Exception) -> int | None:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code
    return None


# The key read from SSM, once per Lambda container (cold start) -- not once per request, and not
# once per invocation: a warm container keeps it. _UNREAD until the first read; None afterwards means
# "no key". Only a definite answer (the key, or no such parameter) is kept: a failed read is retried
# on the next run rather than leaving the container keyless for its whole life.
_UNREAD = object()
_ssm_api_key: object = _UNREAD


def _api_key_from_ssm() -> str | None:
    """The CoinGecko key from the SecureString named by COINGECKO_API_KEY_PARAMETER, or None (no
    parameter configured, none created yet, or SSM unreachable) -- the adapter then goes keyless.
    The key is never logged: only the parameter's name and the error's type are."""
    global _ssm_api_key
    if _ssm_api_key is not _UNREAD:
        return _ssm_api_key
    name = (os.environ.get(API_KEY_PARAMETER_ENV) or "").strip()
    if not name:
        return None
    try:
        response = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            print(f"crypto_feed: no CoinGecko key at {name}; using the public API")
            _ssm_api_key = None
            return None
        print(f"crypto_feed: could not read the CoinGecko key at {name} ({type(exc).__name__}); keyless")
        return None
    except Exception as exc:  # noqa: BLE001 - an unreadable key only costs the rate limit
        print(f"crypto_feed: could not read the CoinGecko key at {name} ({type(exc).__name__}); keyless")
        return None
    _ssm_api_key = (response.get("Parameter", {}).get("Value") or "").strip() or None
    return _ssm_api_key


class CoinGeckoClient:
    """GET JSON from CoinGecko with an optional API key and a keyless fallback.

    The key is an optimisation, never a dependency: if the keyed request
    still fails once its retries are spent (rate limited, a CoinGecko 5xx, a
    network error, or the key being rejected/expired/mistyped), the same
    request is retried against the public keyless API, so a bad or throttled
    key degrades the adapter to its pre-key behaviour instead of failing the
    run. A key CoinGecko *rejects* (401/403) is switched off for the rest of
    this client's life, so a bad key costs one wasted request, not one per
    coin. The key only ever travels in a request header -- never the URL --
    and is never logged, so it can't leak through an exception message.
    """

    def __init__(self, api_key: str | None = None, plan: str = DEFAULT_API_PLAN):
        self._api_key = (api_key or "").strip() or None
        if plan not in API_PLANS:
            print(f"crypto_feed: unknown CoinGecko plan {plan!r}, treating it as demo")
            plan = DEFAULT_API_PLAN
        self._base_url, self._key_header = API_PLANS[plan]
        self._key_rejected = False

    @classmethod
    def from_env(cls) -> CoinGeckoClient:
        plan = (os.environ.get(API_PLAN_ENV) or DEFAULT_API_PLAN).strip().lower()
        return cls(api_key=os.environ.get(API_KEY_ENV) or _api_key_from_ssm(), plan=plan)

    @property
    def uses_key(self) -> bool:
        return self._api_key is not None and not self._key_rejected

    def get_json(
        self,
        path: str,
        *,
        params: dict | None = None,
        timeout: float = 15.0,
        deadline: float | None = None,
    ):
        """Keyed first when a key is set, then keyless; raises the keyless
        attempt's error if both fail."""
        if self.uses_key:
            try:
                return get_json_with_backoff(
                    self._base_url + path,
                    params=params,
                    headers={"User-Agent": USER_AGENT, self._key_header: self._api_key},
                    timeout=timeout,
                    max_attempts=KEYED_MAX_ATTEMPTS,
                    base_delay=2.0,
                    deadline=deadline,
                )
            except Exception as exc:  # noqa: BLE001 - any keyed failure falls back to keyless
                status = _status_code(exc)
                reason = f"HTTP {status}" if status else type(exc).__name__
                if status in _KEY_REJECTED_STATUS_CODES:
                    self._key_rejected = True
                    print(
                        f"crypto_feed: CoinGecko API key rejected ({reason}); "
                        "using the public API for the rest of this run"
                    )
                else:
                    print(
                        f"crypto_feed: keyed CoinGecko request to {path} failed ({reason}); "
                        "falling back to the public API"
                    )

        return get_json_with_backoff(
            PUBLIC_BASE_URL + path,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            max_attempts=PUBLIC_MAX_ATTEMPTS,
            base_delay=2.0,
            deadline=deadline,
        )


class CryptoFeedAdapter(Adapter):
    """CoinGecko market data + crypto news, shaped by the day's editorial goal."""

    uses_previous_state = True

    # --- fetching ---------------------------------------------------------

    def fetch_state(self, topic_config: dict, previous_state: dict | None = None) -> dict:
        adapter_config = topic_config.get("adapter_config") or {}
        now = datetime.now(UTC)
        goal = goal_for_adapter_config(adapter_config, now.date())

        state: dict = {"fetched_at": now.isoformat(), "editorial_goal": goal.value}

        if goal is EditorialGoal.MARKET_NEWS:
            # A general finance-news day has nothing to do with crypto: no
            # CoinGecko call and no BTC/ETH anchors (the key stays, empty, so
            # the snapshot keeps the current format), so a crypto price move
            # can't make this day's snapshot look "material" either.
            state["market_anchors"] = {}
            state["web_results"] = self._fetch_web_results(adapter_config, goal)
            return state

        client = CoinGeckoClient.from_env()
        markets = self._fetch_markets(client)
        state["market_anchors"] = self._anchors(markets)

        if goal is EditorialGoal.WEB_AGGREGATOR:
            state["web_results"] = self._fetch_web_results(adapter_config, goal)
        else:
            already_analysed = self._analyzed_today(previous_state, now)
            pool = self._build_pool(markets, already_analysed, previous_state, now, client)
            state["analyzed_pool"] = pool
            # Every coin analysed so far today (this pool included), so the next tick
            # draws from coins not yet looked at. Kept out of the summary prompt.
            state["analyzed_today"] = sorted(already_analysed | {coin["id"] for coin in pool})
            # The analysis is the substance; the headlines are context. A search
            # that fails or finds nothing must not sink a tick whose market data
            # is fine (unlike a news day, where the headlines are the whole point).
            try:
                state["web_results"] = self._fetch_web_results(adapter_config, goal)
            except Exception as exc:  # noqa: BLE001
                print(f"crypto_feed: no headlines this tick ({exc}); continuing with the market data")
                state["web_results"] = []
        return state

    def review_evidence(self, topic_config: dict, latest_state: dict | None) -> str | None:
        """Current prices for what the article can mention, plus the latest headlines.

        The default (a plain re-fetch) would look at a *different* random pool of coins
        than the one the article was written about, so this re-reads the coins the
        latest snapshot analysed (`analyzed_today`) from the one markets call -- current
        price and 24h/7d/30d change for the top 200, no per-coin history -- plus the
        anchors. A general-market news day has no coins at all: headlines only.
        """
        adapter_config = topic_config.get("adapter_config") or {}
        now = datetime.now(UTC)
        goal = goal_for_adapter_config(adapter_config, now.date())
        evidence: dict = {"as_of": now.isoformat(), "editorial_goal": goal.value}

        if goal is not EditorialGoal.MARKET_NEWS:
            markets = self._fetch_markets(CoinGeckoClient.from_env())
            evidence["market_anchors"] = self._anchors(markets)
            by_id = {coin["id"]: coin for coin in markets}
            wanted = self._analyzed_ids(latest_state)
            evidence["coins"] = [
                {
                    "name": by_id[coin_id].get("name") or coin_id,
                    "symbol": by_id[coin_id].get("symbol"),
                    "rank": by_id[coin_id].get("market_cap_rank"),
                    "price": _round_price(by_id[coin_id]["current_price"]),
                    "change_24h": _market_pct(by_id[coin_id], "24h"),
                    "change_7d": _market_pct(by_id[coin_id], "7d"),
                    "change_30d": _market_pct(by_id[coin_id], "30d"),
                }
                for coin_id in sorted(wanted)
                if coin_id in by_id and by_id[coin_id].get("current_price") is not None
            ]
            missing = len(wanted) - len(evidence["coins"])
            if missing:
                evidence["coins_no_longer_in_the_top_200"] = missing

        try:
            headlines = self._fetch_web_results(
                adapter_config, goal, deadline=time.monotonic() + REVIEW_HEADLINES_BUDGET_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - headlines are context, not the point
            print(f"crypto_feed: no headlines for the review ({exc})")
            headlines = []
        evidence["headlines"] = [
            {"title": r["title"], "source": r.get("source"), "published_at": r.get("published_at")}
            for r in headlines
        ]
        return render_review_evidence(evidence)

    @staticmethod
    def _analyzed_ids(latest_state: dict | None) -> set[str]:
        """Every coin id a snapshot analysed (its cumulative day list and its pool)."""
        if not latest_state:
            return set()
        ids = set(latest_state.get("analyzed_today") or [])
        ids.update(coin["id"] for coin in latest_state.get("analyzed_pool") or [] if coin.get("id"))
        return ids

    def _fetch_markets(self, client: CoinGeckoClient) -> list[dict]:
        return client.get_json(
            MARKETS_PATH,
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": MARKETS_PER_PAGE,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h,7d,30d,1y",
            },
        )

    def _anchors(self, markets: list[dict]) -> dict:
        by_id = {coin["id"]: coin for coin in markets}
        anchors = {}
        for coin_id in ANCHOR_IDS:
            coin = by_id.get(coin_id)
            if coin is None:
                raise RuntimeError(f"CoinGecko markets response is missing anchor {coin_id!r}")
            anchors[coin_id] = {
                "name": coin.get("name") or coin_id,
                "price": _round_price(coin["current_price"]),
                "change_24h": _market_pct(coin, "24h"),
                "change_7d": _market_pct(coin, "7d"),
                "change_30d": _market_pct(coin, "30d"),
                "change_1y": _market_pct(coin, "1y"),
                "market_cap": coin.get("market_cap"),
            }
        return anchors

    def _build_pool(
        self,
        markets: list[dict],
        already_analysed: set[str],
        previous_state: dict | None,
        now: datetime,
        client: CoinGeckoClient,
    ) -> list[dict]:
        target = select_altcoin_pool(markets, exclude_ids=already_analysed)
        carried = self._carried_entries(previous_state, now)

        by_id: dict[str, dict] = {}
        missing: list[dict] = []
        for coin in target:
            previous = carried.get(coin["id"])
            if previous is not None:
                by_id[coin["id"]] = _refresh_entry(previous, coin)
            else:
                missing.append(coin)

        if missing:
            for entry in self._enrich(missing, client):
                by_id[entry["id"]] = entry

        pool = [by_id[coin["id"]] for coin in target if coin["id"] in by_id]
        if len(pool) < MIN_POOL_SIZE:
            raise RuntimeError(
                f"only {len(pool)} of {len(target)} pool coins had usable history "
                f"(need at least {MIN_POOL_SIZE})"
            )
        return pool

    @staticmethod
    def _analyzed_today(previous_state: dict | None, now: datetime) -> set[str]:
        """Coin ids already analysed earlier *today* (same UTC date), from the last
        snapshot. Nothing carries across a date change or from a legacy snapshot."""
        if not previous_state or "market_anchors" not in previous_state:
            return set()
        if (previous_state.get("fetched_at") or "")[:10] != now.date().isoformat():
            return set()
        ids = set(previous_state.get("analyzed_today") or [])
        ids.update(coin["id"] for coin in previous_state.get("analyzed_pool") or [] if coin.get("id"))
        return ids

    @staticmethod
    def _carried_entries(previous_state: dict | None, now: datetime) -> dict[str, dict]:
        """Pool entries recorded earlier *today* (same UTC date), by coin id --
        their history is the same for the rest of the day. Nothing carries
        across a date change or from a legacy/other-goal snapshot."""
        if not previous_state or "market_anchors" not in previous_state:
            return {}
        if (previous_state.get("fetched_at") or "")[:10] != now.date().isoformat():
            return {}
        return {
            coin["id"]: coin
            for coin in previous_state.get("analyzed_pool") or []
            if coin.get("metrics")
        }

    def _enrich(self, coins: list[dict], client: CoinGeckoClient) -> list[dict]:
        histories = asyncio.run(self._fetch_histories(client, [coin["id"] for coin in coins]))

        entries = []
        for coin in coins:
            history = histories.get(coin["id"])
            metrics = compute_coin_metrics(history, coin["current_price"]) if history else None
            if metrics is None:
                print(f"crypto_feed: dropping {coin['id']} from the pool (no usable history)")
                continue
            entries.append(
                {
                    "id": coin["id"],
                    "symbol": coin.get("symbol"),
                    "name": coin.get("name") or coin["id"],
                    "market_cap_rank": coin.get("market_cap_rank"),
                    "metrics": metrics,
                }
            )
        return entries

    async def _fetch_histories(
        self, client: CoinGeckoClient, coin_ids: list[str]
    ) -> dict[str, dict | None]:
        """Fetch every coin's history concurrently, at most HISTORY_CONCURRENCY
        at a time. One coin failing (after its retries) yields None for that
        coin instead of failing the batch."""
        semaphore = asyncio.Semaphore(HISTORY_CONCURRENCY)
        deadline = time.monotonic() + HISTORY_TIME_BUDGET_SECONDS

        async def fetch_one(coin_id: str) -> tuple[str, dict | None]:
            async with semaphore:
                try:
                    history = await asyncio.to_thread(
                        client.get_json,
                        HISTORY_PATH.format(coin_id=coin_id),
                        params={"vs_currency": "usd", "days": _HISTORY_DAYS, "interval": "daily"},
                        timeout=_HISTORY_TIMEOUT_SECONDS,
                        deadline=deadline,
                    )
                    return coin_id, history
                except Exception as exc:  # noqa: BLE001 - one bad coin must not sink the batch
                    print(f"crypto_feed: history fetch failed for {coin_id}: {exc!r}")
                    return coin_id, None

        return dict(await asyncio.gather(*(fetch_one(coin_id) for coin_id in coin_ids)))

    def _fetch_web_results(
        self, adapter_config: dict, goal: EditorialGoal, deadline: float | None = None
    ) -> list[dict]:
        """News for the web-based goals: crypto headlines for WEB_AGGREGATOR,
        general finance headlines (crypto excluded) for MARKET_NEWS.

        `deadline` (time.monotonic()) caps the whole fetch: once it passes, the
        remaining queries are skipped and whatever was already found is kept."""
        if goal is EditorialGoal.MARKET_NEWS:
            config_key, default_query = "market_news_queries", MARKET_NEWS_QUERY
            title_keywords, exclude_keywords = MARKET_NEWS_TITLE_KEYWORDS, CRYPTO_EXCLUSION_KEYWORDS
            label = "general financial-market"
        else:
            config_key, default_query = "web_search_queries", DEFAULT_WEB_QUERY
            title_keywords, exclude_keywords = CRYPTO_TITLE_KEYWORDS, []
            label = "crypto"

        queries = [
            q for q in (adapter_config.get(config_key) or []) if isinstance(q, str) and q
        ] or [default_query]
        # Excluding crypto happens after the search caps its results, so ask for
        # extra to still end up with a full set.
        fetch_count = WEB_MAX_RESULTS * 2 if exclude_keywords else WEB_MAX_RESULTS

        merged: list[dict] = []
        seen_urls: set[str] = set()
        budget = {"deadline": deadline} if deadline is not None else {}
        for query in queries:
            if merged and deadline is not None and time.monotonic() >= deadline:
                break
            for result in search_web(
                query,
                max_results=fetch_count,
                max_age_hours=WEB_MAX_AGE_HOURS,
                title_keywords=title_keywords,
                provider=adapter_config.get("web_search_provider"),
                **budget,
            ):
                if exclude_keywords and matches_keywords(result["title"], exclude_keywords):
                    continue
                if result["url"] not in seen_urls:
                    seen_urls.add(result["url"])
                    merged.append(result)

        if not merged:
            raise RuntimeError(f"web search returned no {label} news items from the last 24h")
        merged.sort(key=lambda r: r.get("published_at") or "", reverse=True)
        return merged[:WEB_MAX_RESULTS]

    # --- diffing ----------------------------------------------------------

    def item_keys(self, state: dict) -> set[str]:
        return {r["url"] for r in state.get("web_results") or []}

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        if old_state is None:
            return True, "initial observation: no prior snapshot to compare against"
        if "market_anchors" not in old_state:
            return True, "snapshot format upgraded: prior snapshot predates editorial goals"

        goal = new_state.get("editorial_goal")
        old_day = (old_state.get("fetched_at") or "")[:10]
        new_day = (new_state.get("fetched_at") or "")[:10]
        if old_day != new_day:
            return True, f"new daily analysis: editorial goal {goal}"
        if old_state.get("editorial_goal") != goal:
            return True, f"editorial goal changed to {goal}"

        # Price moves are deliberately not a trigger. What is new is (a) coins
        # sampled into this tick's pool that the last snapshot's pool did not have
        # -- the pool is a fresh random draw each tick, so on the analysis days that
        # is nearly every tick -- and (b) headlines not already reported. Novelty of
        # headlines is judged against every one already reported (the carried
        # seen-set), so a story that drops out of the results and returns later is
        # not reported twice.
        parts = []
        old_pool_ids = {coin["id"] for coin in old_state.get("analyzed_pool") or []}
        fresh_coins = [c for c in new_state.get("analyzed_pool") or [] if c["id"] not in old_pool_ids]
        if fresh_coins:
            names = ", ".join(c.get("name") or c["id"] for c in fresh_coins[:POOL_SIZE])
            parts.append(f"{len(fresh_coins)} newly sampled coins: {names}")

        known = self.known_keys(old_state)
        fresh = [r for r in new_state.get("web_results") or [] if r["url"] not in known]
        if len(fresh) >= WEB_NEW_RESULTS_THRESHOLD:
            parts.append(f"{len(fresh)} new news items: " + "; ".join(r["title"] for r in fresh[:5]))

        if parts:
            return True, "; ".join(parts)
        return False, "no material change"

    # --- citation ---------------------------------------------------------

    def source_refs(self, new_state: dict) -> list[dict]:
        """One reference per anchor, per analysed altcoin, and per news item,
        so a moderator can trace exactly which coins/articles fed a draft."""
        accessed_at = new_state.get("fetched_at")
        refs = [
            {
                "url": f"https://www.coingecko.com/en/coins/{coin_id}",
                "title": anchor.get("name") or coin_id,
                "accessed_at": accessed_at,
            }
            for coin_id, anchor in (new_state.get("market_anchors") or {}).items()
        ]
        refs.extend(
            {
                "url": f"https://www.coingecko.com/en/coins/{coin['id']}",
                "title": coin.get("name") or coin["id"],
                "accessed_at": accessed_at,
            }
            for coin in new_state.get("analyzed_pool") or []
        )
        refs.extend(
            {"url": r["url"], "title": r.get("title") or r["url"], "accessed_at": accessed_at}
            for r in new_state.get("web_results") or []
        )
        return refs

    # --- research-summary prompt (P1) -------------------------------------

    def build_summary_prompt(self, topic: dict, diff_summary: str, new_state: dict) -> str | None:
        goal = parse_goal(new_state.get("editorial_goal"))
        if goal is None or "market_anchors" not in new_state:
            return None  # legacy snapshot: the generic prompt is all it supports

        topic_name = topic_label(topic)
        # `analyzed_today` is bookkeeping for the next tick's draw, not data to summarise.
        prompt_state = {k: v for k, v in new_state.items() if k != "analyzed_today"}
        compact_state = json.dumps(prompt_state, separators=(",", ":"))[:SUMMARY_STATE_MAX_CHARS]
        anchors = _anchor_summary(new_state)  # empty on a MARKET_NEWS day: no crypto data
        header = (
            f'You are monitoring the topic "{topic_name}" for a research digest.'
            + (f" Current Market Anchors: {anchors}." if anchors else "")
            + "\n\n"
            f"OPERATIONAL EDITORIAL GOAL:\n{resolve_editorial_goals(topic, goal)}\n\n"
            f"What changed: {diff_summary}\n\n"
        )
        closing = (
            " Be concise, objective, and data-grounded. Do not speculate beyond what the "
            "data shows, and do not give financial or investment advice."
        )

        if goal in (EditorialGoal.WEB_AGGREGATOR, EditorialGoal.MARKET_NEWS):
            count = len(new_state.get("web_results") or [])
            kind = (
                "general financial-market (not crypto)"
                if goal is EditorialGoal.MARKET_NEWS
                else "crypto"
            )
            return (
                header
                + f"Today's Analysis Focus: A synthesis of {count} {kind} news items published "
                "in the last 24 hours.\n\n"
                f"News items (JSON; only headlines and source names are available, not "
                f"article bodies): {compact_state}\n\n"
                f"{research_relevance_rule(topic_name)}\n\n"
                "List the distinct stories and themes with their source, note where sources "
                "agree or conflict, and describe the overall sentiment the headlines convey."
                + closing
            )

        count = len(new_state.get("analyzed_pool") or [])
        if goal is EditorialGoal.TREND_INVENTOR:
            focus = (
                f"Today's Analysis Focus: The anchors' multi-timeframe performance alongside "
                f"{count} randomly selected altcoins' multi-metric trends -- raw material for "
                "an original interpretive framework.\n\n"
            )
            task = (
                "Summarize how the anchors and the altcoins behave across the 5-day, 3-month "
                "and 1-year windows, and point out the relationships, divergences and "
                "anomalies a thoughtful analyst would want to frame."
            )
        else:
            focus = f"Today's Analysis Focus: A randomized deep dive into {count} altcoins.\n\n"
            task = (
                f"Synthesize the state of the broader market by comparing the {count} random "
                "altcoins against the baseline performance of the market anchors. Name each "
                "analysed coin with its key figures, note major 1-year macro trends and "
                "3-month velocity shifts, and flag any asset experiencing an active 5-day "
                "anomaly (price spikes or sharp crashes)."
            )
        if new_state.get("web_results"):
            task += (
                " The state also lists the latest crypto news headlines (headline and source "
                "only, no article text): where one relates to an analysed coin or to the market "
                "moves described, mention it and name its source, and attribute it rather than "
                "adding any detail the headline does not contain."
            )
        return (
            header + focus + f"Raw Analysis Metrics (JSON): {compact_state}\n\n" + task + closing
        )
