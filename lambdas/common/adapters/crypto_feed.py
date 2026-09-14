"""Adapter for crypto market data (via the public CoinGecko API).

Phase 7's third adapter -- the one explicitly called for by
docs/PROGRESS.md's Phase 7 scope: "crypto, with the stricter compliance
rubric ... no recommendation language, standing 'not financial advice'
disclaimer, always routed to manual moderation regardless of confidence".

This adapter itself only fetches and diffs market data -- it is exactly
as topic-agnostic as github_trending.py/hacker_news.py, per
docs/project-plan.md §6 ("new domains are implemented as adapters, not
core pipeline branches"). The financial-specific behavior lives entirely
outside the adapter, in two places that already existed before this
adapter did:
  - admin_api_handler.py forces `is_financial = True` for any topic using
    this adapter (see CRYPTO_FEED_ADAPTER_KEY below), so that property
    can't be bypassed by an operator forgetting the flag.
  - common/compliance.py's `is_financial_topic` gate (unconditional
    manual-moderation routing, since Phase 1) plus its Phase 7 drafting
    guidance/disclaimer additions (FINANCIAL_DRAFTING_GUIDANCE,
    append_financial_disclaimer), used by daily_cycle_handler.py.
This split is the point: it's what lets this adapter add zero branches
to the core research-tick/daily-cycle flow.
"""

from __future__ import annotations

from datetime import UTC, datetime

import requests

from .base import Adapter

MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
REQUEST_TIMEOUT_SECONDS = 10
USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# Used when a topic's adapter_config doesn't specify coin_ids -- keeps
# topic creation usable with zero config, same as github_trending.py
# defaulting to the unscoped trending page when `language` is omitted.
DEFAULT_COIN_IDS = ["bitcoin", "ethereum"]

# A coin's 24h price moving by at least this many percentage points since
# the last snapshot counts as a material change -- crypto prices are
# volatile enough that a much smaller/absolute threshold (like
# github_trending.py's star-jump one) would fire on ordinary noise.
PRICE_MOVE_THRESHOLD_PERCENT = 5.0


class CryptoFeedAdapter(Adapter):
    """Fetches current market data for a configured set of coins via CoinGecko."""

    def fetch_state(self, topic_config: dict) -> dict:
        adapter_config = topic_config.get("adapter_config") or {}
        coin_ids = adapter_config.get("coin_ids") or DEFAULT_COIN_IDS

        response = requests.get(
            MARKETS_URL,
            params={
                "vs_currency": "usd",
                "ids": ",".join(coin_ids),
                "order": "market_cap_desc",
                "price_change_percentage": "24h",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()

        coins = [
            {
                "id": coin["id"],
                "symbol": coin.get("symbol"),
                "name": coin.get("name"),
                "current_price": coin.get("current_price"),
                "market_cap": coin.get("market_cap"),
                "price_change_percentage_24h": coin.get("price_change_percentage_24h"),
            }
            for coin in response.json()
        ]
        return {"coins": coins, "fetched_at": datetime.now(UTC).isoformat()}

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        if old_state is None:
            return True, "initial observation: no prior snapshot to compare against"

        old_coins = {c["id"]: c for c in old_state.get("coins", [])}
        new_coins = {c["id"]: c for c in new_state.get("coins", [])}

        entered = sorted(set(new_coins) - set(old_coins))
        left = sorted(set(old_coins) - set(new_coins))

        price_moves = []
        for coin_id in sorted(set(old_coins) & set(new_coins)):
            old_price = old_coins[coin_id].get("current_price")
            new_price = new_coins[coin_id].get("current_price")
            if not old_price or new_price is None:
                continue
            pct_change = (new_price - old_price) / old_price * 100
            if abs(pct_change) >= PRICE_MOVE_THRESHOLD_PERCENT:
                price_moves.append((coin_id, old_price, new_price, pct_change))

        if not entered and not left and not price_moves:
            return False, "no material change"

        parts = []
        if entered:
            parts.append(f"entered: {', '.join(entered)}")
        if left:
            parts.append(f"left: {', '.join(left)}")
        if price_moves:
            move_desc = ", ".join(
                f"{cid} ${o:,.2f}->${n:,.2f} ({p:+.1f}%)" for cid, o, n, p in price_moves
            )
            parts.append(f"price moves: {move_desc}")

        return True, "; ".join(parts)

    def source_refs(self, new_state: dict) -> list[dict]:
        accessed_at = new_state.get("fetched_at")
        return [
            {
                "url": f"https://www.coingecko.com/en/coins/{coin['id']}",
                "title": coin.get("name") or coin["id"],
                "accessed_at": accessed_at,
            }
            for coin in new_state.get("coins", [])
        ]
