"""Adapter for crypto market data (CoinGecko) and crypto news (web search).

Bitcoin and Ethereum are the structural *market anchors*: every snapshot
carries their price and multi-timeframe change, and everything else is read
relative to them. What else a snapshot carries depends on the day's
editorial goal (common/editorial_goals.py -- a pure function of the UTC date,
so this adapter and the daily cycle always agree on it):

  ALTCOIN_DEEP_DIVE / TREND_INVENTOR
      a pool of 10 altcoins sampled from the top 200 by market cap (never
      the anchors, stablecoins, tokenized funds, or wrapped/staked/bridged
      derivatives), each enriched from one CoinGecko history call with its
      3-month and 1-year change and a 5-day anomaly read (price spike and/or
      volume surge).
  WEB_AGGREGATOR
      5-15 crypto news items from the last 24h, via common/web_search.py.

**The pool is random but stable within a UTC day** -- seeded by (topic, date).
The research tick runs hourly; an unseeded sample would pick 10 different
coins every hour, so every tick would look like a material change and burn a
Bedrock call and a Finding. Seeded, the day's pool only changes when the
date does, and ticks within a day only fire on a real price move.

**History is fetched once per UTC day, not once per tick.** It is daily data,
and CoinGecko's public API answers 429 well below what 10 history calls an
hour would need (observed live: 3 of 10 failed even with backoff). The adapter
opts into `uses_previous_state`, so on later ticks it reuses the day's
already-fetched metrics -- refreshing only each coin's current price and its
3-month/1-year change from the stored baselines -- and re-requests only
coins whose history failed earlier. A partly failed first tick therefore
heals over the day instead of repeating the whole fetch.

Requests that are made run concurrently (asyncio, a small semaphore) through
common/http_retry.py's exponential backoff; a coin whose history can't be
fetched is dropped, and the run fails only if fewer than MIN_POOL_SIZE coins
survive.

**An optional CoinGecko API key** (env `COINGECKO_API_KEY`, plan in
`COINGECKO_API_PLAN`) raises the rate limit. See CoinGeckoClient: if the keyed
request is throttled, errors, or the key is rejected, the same request is
retried against the public keyless API, so the key can only ever help.

This adapter contains no domain branches in the core pipeline
(docs/project-plan.md §6): the goal rotation lives in editorial_goals.py, and
the financial-topic safety rules (forced is_financial, mandatory moderation,
drafting guidance, disclaimer) remain in admin_api_handler.py and
common/compliance.py, unchanged.

adapter_config (all optional): `editorial_goal` pins one goal instead of
rotating; `web_search_queries` / `web_search_provider` override the
web-aggregator's search. The old `coin_ids` setting is no longer used --
the anchors are always Bitcoin and Ethereum.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import UTC, date, datetime
from decimal import Decimal

import requests

from common.editorial_goals import EditorialGoal, goal_for_adapter_config, parse_goal
from common.http_retry import get_json_with_backoff
from common.relevance import research_relevance_rule, topic_label
from common.web_search import search_web

from .base import Adapter

PUBLIC_BASE_URL = "https://api.coingecko.com/api/v3"
PRO_BASE_URL = "https://pro-api.coingecko.com/api/v3"
MARKETS_PATH = "/coins/markets"
HISTORY_PATH = "/coins/{coin_id}/market_chart"
USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# The API key comes from the environment (set by Terraform on the research-tick
# Lambda from a CI secret), never from adapter_config: that is stored in
# DynamoDB and readable through the admin API, so a secret there would leak.
API_KEY_ENV = "COINGECKO_API_KEY"
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

# A price moving by at least this many percentage points since the last
# snapshot counts as a material change (crypto is volatile enough that a
# much smaller threshold would fire on ordinary noise).
PRICE_MOVE_THRESHOLD_PERCENT = 5.0

# 5-day anomaly read: a daily move at least this large is a "spike" (higher
# than for the anchors' typical moves, since altcoins are far more volatile);
# a day's volume this many times the trailing 30-day average is a "surge".
ANOMALY_DAILY_MOVE_PERCENT = 8.0
VOLUME_SURGE_RATIO = 2.0

WEB_MAX_RESULTS = 15
WEB_MAX_AGE_HOURS = 24
# New web headlines needed within a day before another Finding is worth a
# Bedrock call (headlines turn over constantly; a couple aren't material).
WEB_NEW_RESULTS_THRESHOLD = 5
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

SUMMARY_STATE_MAX_CHARS = 9000

# Things that are not "altcoins" for an altcoin analysis. Stablecoins: known
# ids catch the big ones and the peg check catches the long tail without a
# second API call (it needs a *flat week* as well as a ~$1 price, so a
# volatile coin that merely trades around $1 stays eligible). Tokenized
# funds / treasuries and other NAV-style assets (seen live in the top 200:
# "Janus Henderson Anemoy Treasury Fund" at $1.12, an EUR swap fund at $1.16)
# don't sit at $1 but barely move over any window, which the flatness check
# catches whatever their price or currency.
KNOWN_STABLECOIN_IDS = frozenset({
    "tether", "usd-coin", "dai", "usds", "ethena-usde", "first-digital-usd", "usdd",
    "paypal-usd", "true-usd", "frax", "usd1-wlfi", "global-dollar", "ripple-usd",
    "gemini-dollar", "pax-dollar", "binance-usd", "usdb", "stasis-eurs", "tether-gold",
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


def select_altcoin_pool(markets: list[dict], topic_id: str, day: date) -> list[dict]:
    """Sample POOL_SIZE altcoins, reproducibly for (`topic_id`, `day`)."""
    eligible = sorted(
        (coin for coin in markets if is_altcoin_candidate(coin)),
        key=lambda coin: coin["market_cap_rank"],
    )
    rng = random.Random(f"{topic_id}:{day.isoformat()}")
    return rng.sample(eligible, min(POOL_SIZE, len(eligible)))


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


def _known_prices(state: dict) -> dict[str, float]:
    prices = {cid: a["price"] for cid, a in (state.get("market_anchors") or {}).items()}
    for coin in state.get("analyzed_pool") or []:
        price = (coin.get("metrics") or {}).get("price_now")
        if price is not None:
            prices[coin["id"]] = price
    return prices


def _status_code(exc: Exception) -> int | None:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code
    return None


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
        return cls(api_key=os.environ.get(API_KEY_ENV), plan=plan)

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

        client = CoinGeckoClient.from_env()
        markets = self._fetch_markets(client)
        state: dict = {
            "fetched_at": now.isoformat(),
            "editorial_goal": goal.value,
            "market_anchors": self._anchors(markets),
        }

        if goal is EditorialGoal.WEB_AGGREGATOR:
            state["web_results"] = self._fetch_web_results(adapter_config)
        else:
            state["analyzed_pool"] = self._build_pool(
                markets, topic_config.get("topic_id", ""), now, previous_state, client
            )
        return state

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
        topic_id: str,
        now: datetime,
        previous_state: dict | None,
        client: CoinGeckoClient,
    ) -> list[dict]:
        target = select_altcoin_pool(markets, topic_id, now.date())
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

    def _fetch_web_results(self, adapter_config: dict) -> list[dict]:
        queries = [
            q for q in (adapter_config.get("web_search_queries") or []) if isinstance(q, str) and q
        ] or [DEFAULT_WEB_QUERY]

        merged: list[dict] = []
        seen_urls: set[str] = set()
        for query in queries:
            for result in search_web(
                query,
                max_results=WEB_MAX_RESULTS,
                max_age_hours=WEB_MAX_AGE_HOURS,
                title_keywords=CRYPTO_TITLE_KEYWORDS,
                provider=adapter_config.get("web_search_provider"),
            ):
                if result["url"] not in seen_urls:
                    seen_urls.add(result["url"])
                    merged.append(result)

        if not merged:
            raise RuntimeError("web search returned no crypto news items from the last 24h")
        merged.sort(key=lambda r: r.get("published_at") or "", reverse=True)
        return merged[:WEB_MAX_RESULTS]

    # --- diffing ----------------------------------------------------------

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

        old_prices, new_prices = _known_prices(old_state), _known_prices(new_state)
        moves = []
        for coin_id in sorted(set(old_prices) & set(new_prices)):
            old_price, new_price = old_prices[coin_id], new_prices[coin_id]
            if not old_price:
                continue
            change = (new_price - old_price) / old_price * 100
            if abs(change) >= PRICE_MOVE_THRESHOLD_PERCENT:
                moves.append(
                    f"{coin_id} {_format_usd(old_price)}->{_format_usd(new_price)} ({change:+.1f}%)"
                )
        if moves:
            return True, "price moves: " + ", ".join(moves)

        old_urls = {r["url"] for r in old_state.get("web_results") or []}
        fresh = [r for r in new_state.get("web_results") or [] if r["url"] not in old_urls]
        if len(fresh) >= WEB_NEW_RESULTS_THRESHOLD:
            return True, f"{len(fresh)} new news items: " + "; ".join(r["title"] for r in fresh[:5])

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
        compact_state = json.dumps(new_state, separators=(",", ":"))[:SUMMARY_STATE_MAX_CHARS]
        header = (
            f'You are monitoring the topic "{topic_name}" for a research digest. '
            f"Current Market Anchors: {_anchor_summary(new_state)}.\n\n"
            f"What changed: {diff_summary}\n\n"
        )
        closing = (
            " Be concise, objective, and data-grounded. Do not speculate beyond what the "
            "data shows, and do not give financial or investment advice."
        )

        if goal is EditorialGoal.WEB_AGGREGATOR:
            count = len(new_state.get("web_results") or [])
            return (
                header
                + f"Today's Analysis Focus: A synthesis of {count} crypto news items published "
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
        return (
            header + focus + f"Raw Analysis Metrics (JSON): {compact_state}\n\n" + task + closing
        )
