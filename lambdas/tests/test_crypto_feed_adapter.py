from __future__ import annotations

import json
import random
import threading
import time
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import requests

from common.adapters.crypto_feed import (
    HISTORY_CONCURRENCY,
    HISTORY_PATH,
    KEYED_MAX_ATTEMPTS,
    MARKETS_PATH,
    MIN_POOL_SIZE,
    POOL_SIZE,
    PRO_BASE_URL,
    PUBLIC_BASE_URL,
    CoinGeckoClient,
    CryptoFeedAdapter,
    _anomaly_5d,
    _format_usd,
    _round_price,
    compute_coin_metrics,
    is_altcoin_candidate,
    is_flat_asset,
    is_stablecoin,
    select_altcoin_pool,
)
from common.editorial_goals import EditorialGoal, goal_for_date

DAY_MS = 86_400_000
TODAY = datetime.now(UTC).date()
MARKETS_URL = PUBLIC_BASE_URL + MARKETS_PATH
HISTORY_URL = PUBLIC_BASE_URL + HISTORY_PATH


@pytest.fixture(autouse=True)
def _no_api_key_by_default(monkeypatch):
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_API_PLAN", raising=False)


@pytest.fixture(autouse=True)
def _no_real_news_search():
    """Analysis days now also search for headlines. No test may reach the network by
    accident: the default is a search that finds nothing (tests that care patch it)."""
    with patch("common.adapters.crypto_feed.search_web", return_value=[]):
        yield


def _market(coin_id, price, rank, c24=6.0, c7=10.0, c30=15.0, c1y=50.0, name=None):
    return {
        "id": coin_id,
        "symbol": coin_id[:3],
        "name": name or coin_id.title(),
        "current_price": price,
        "market_cap": 1_000_000_000 // (rank or 300),
        "market_cap_rank": rank,
        "price_change_percentage_24h_in_currency": c24,
        "price_change_percentage_7d_in_currency": c7,
        "price_change_percentage_30d_in_currency": c30,
        "price_change_percentage_1y_in_currency": c1y,
    }


def _markets(altcoins=25):
    coins = [
        _market("bitcoin", 80000.0, 1, name="Bitcoin"),
        _market("ethereum", 2600.0, 2, name="Ethereum"),
        _market("tether", 1.0, 3, c24=0.0, c7=0.0, c30=0.0, c1y=0.0, name="Tether"),
        _market("wrapped-bitcoin", 80000.0, 15, name="Wrapped Bitcoin"),
    ]
    coins += [_market(f"alt-{i}", 10.0 + i, 4 + i, name=f"Alt {i}") for i in range(altcoins)]
    return coins


def _history(days=366, first=50.0, last=100.0, volume=1000.0):
    start = 1_700_000_000_000
    step = (last - first) / (days - 1)
    prices = [[start + i * DAY_MS, first + step * i] for i in range(days)]
    volumes = [[start + i * DAY_MS, volume] for i in range(days)]
    return {"prices": prices, "total_volumes": volumes}


def _topic(**adapter_config):
    return {"topic_id": "crypto", "name": "Crypto", "adapter_config": adapter_config}


def _web_result(n, published_at="2026-09-20T12:00:00+00:00"):
    return {
        "title": f"Bitcoin story {n}",
        "url": f"https://news.example/{n}",
        "source": "news.example",
        "published_at": published_at,
        "snippet": None,
    }


def _market_result(n, title=None, published_at="2026-09-20T12:00:00+00:00"):
    return {
        "title": title or f"Stocks rally as inflation cools {n}",
        "url": f"https://markets.example/{n}",
        "source": "markets.example",
        "published_at": published_at,
        "snippet": None,
    }


def _fake_get(markets, failing=(), history=None):
    def fake(url, **kwargs):
        if url == MARKETS_URL:
            return markets
        coin_id = url.split("/coins/")[1].split("/")[0]
        if coin_id in failing:
            raise RuntimeError(f"history unavailable for {coin_id}")
        return history or _history()

    return fake


def _history_calls(mock_get):
    return [c for c in mock_get.call_args_list if c.args[0] != MARKETS_URL]


POOL_SEED = 7


def _pinned_draw(seed=POOL_SEED):
    """The adapter's pool draw is unseeded on purpose; tests pin it so a run is
    repeatable. `select_altcoin_pool(markets, rng=random.Random(seed))` gives the
    coins such a pinned run will draw."""
    return patch("common.adapters.crypto_feed.random.SystemRandom", lambda: random.Random(seed))


def _fetch(topic, markets=None, previous_state=None, failing=(), headlines=(), seed=POOL_SEED):
    """One analysis-day tick with CoinGecko and the headline search stubbed. By default
    the search finds nothing (which must not sink the tick)."""
    with (
        patch(
            "common.adapters.crypto_feed.get_json_with_backoff",
            side_effect=_fake_get(markets or _markets(), failing),
        ) as mock_get,
        patch("common.adapters.crypto_feed.search_web", return_value=list(headlines)),
        _pinned_draw(seed),
    ):
        state = CryptoFeedAdapter().fetch_state(topic, previous_state=previous_state)
    return state, mock_get


DEEP_DIVE = _topic(editorial_goal="ALTCOIN_DEEP_DIVE")


# --- formatting helpers -----------------------------------------------------


def test_format_usd_covers_whole_dollars_cents_and_sub_dollar_prices():
    assert _format_usd(81584.0) == "$81,584"
    assert _format_usd(2648.78) == "$2,648.78"
    assert _format_usd(0.0811858) == "$0.0811858"
    assert _format_usd(0.00000033139) == "$0.00000033139"  # never scientific notation


def test_round_price_keeps_precision_for_sub_dollar_coins():
    assert _round_price(1234.5678) == 1234.57
    assert _round_price(0.000012345678) == 0.0000123457


# --- eligibility ------------------------------------------------------------


def test_stablecoins_are_recognised_by_id_and_by_a_flat_peg():
    assert is_stablecoin(_market("tether", 1.0, 3))
    assert is_stablecoin(_market("mystery-usd", 1.001, 40, c24=0.1, c7=0.2))
    # trades around $1 but is volatile: an altcoin, not a stablecoin
    assert not is_stablecoin(_market("volatile", 1.01, 40, c24=9.0, c7=25.0))
    assert not is_stablecoin(_market("solana", 150.0, 5))


def test_flat_assets_need_every_window_flat_and_missing_data_is_not_flat():
    flat = _market("fund", 1.12, 90, c24=0.1, c7=0.3, c30=0.8)
    assert is_flat_asset(flat)
    assert not is_flat_asset(_market("mover", 1.12, 90, c30=12.0))
    missing = _market("new", 1.12, 90, c24=0.1, c7=0.3, c30=0.8)
    missing["price_change_percentage_30d_in_currency"] = None
    assert not is_flat_asset(missing)


def test_altcoin_candidates_exclude_anchors_pegs_funds_and_wrapped_or_staked_assets():
    assert is_altcoin_candidate(_market("solana", 150.0, 5))
    assert not is_altcoin_candidate(_market("bitcoin", 80000.0, 1))
    assert not is_altcoin_candidate(_market("ethereum", 2600.0, 2))
    assert not is_altcoin_candidate(_market("tether", 1.0, 3))
    assert not is_altcoin_candidate(_market("wrapped-steth", 3000.0, 20, name="Wrapped stETH"))
    assert not is_altcoin_candidate(_market("coinbase-staked", 2800.0, 21, name="Staked ETH"))
    assert not is_altcoin_candidate(
        _market("jh-fund", 1.12, 90, c24=0.1, c7=0.3, c30=0.8, name="Treasury Fund")
    )
    assert not is_altcoin_candidate(_market("unranked", 5.0, None))


# --- pool selection ---------------------------------------------------------


def test_pool_has_ten_altcoins_and_never_an_anchor_or_stablecoin():
    pool = select_altcoin_pool(_markets())

    ids = {coin["id"] for coin in pool}
    assert len(pool) == POOL_SIZE == len(ids)
    assert not ids & {"bitcoin", "ethereum", "tether", "wrapped-bitcoin"}


def test_the_same_generator_gives_the_same_draw_and_different_ones_differ():
    markets = _markets(altcoins=60)

    first = [c["id"] for c in select_altcoin_pool(markets, rng=random.Random(1))]

    assert first == [c["id"] for c in select_altcoin_pool(markets, rng=random.Random(1))]
    assert first != [c["id"] for c in select_altcoin_pool(markets, rng=random.Random(2))]


def test_the_default_draw_is_unseeded_so_repeated_calls_differ():
    """No date or topic seeds it: every call is a fresh draw."""
    markets = _markets(altcoins=60)

    draws = {tuple(sorted(c["id"] for c in select_altcoin_pool(markets))) for _ in range(12)}

    assert len(draws) > 1  # 12 identical draws of 10 from 60 is astronomically unlikely


def test_the_default_draw_uses_the_systems_generator_not_a_seeded_one():
    with patch("common.adapters.crypto_feed.random.SystemRandom") as system_random:
        system_random.return_value = random.Random(3)
        select_altcoin_pool(_markets())

    system_random.assert_called_once_with()


def test_coins_already_analysed_today_are_left_out_while_enough_others_remain():
    markets = _markets(altcoins=40)
    seen = {c["id"] for c in select_altcoin_pool(markets, rng=random.Random(1))}

    pool = select_altcoin_pool(markets, exclude_ids=seen, rng=random.Random(2))

    assert len(pool) == POOL_SIZE
    assert not {c["id"] for c in pool} & seen


def test_the_exclusion_lapses_rather_than_shrinking_the_pool_when_unseen_coins_run_out():
    markets = _markets(altcoins=14)
    eligible = {c["id"] for c in select_altcoin_pool(markets, rng=random.Random(1))} | {
        c["id"] for c in select_altcoin_pool(markets, rng=random.Random(2))
    }
    seen = set(list(eligible)[:8])  # only 6 of the 14 remain unseen: fewer than a pool

    pool = select_altcoin_pool(markets, exclude_ids=seen, rng=random.Random(3))

    assert len(pool) == POOL_SIZE  # full again, repeats allowed


def test_pool_shrinks_when_fewer_than_ten_altcoins_are_eligible():
    assert len(select_altcoin_pool(_markets(altcoins=6))) == 6


# --- per-coin metrics -------------------------------------------------------


def test_metrics_for_a_full_year_of_history():
    history = _history(first=50.0, last=100.0)

    metrics = compute_coin_metrics(history, 100.0)

    price_3m = history["prices"][275][1]  # the point 90 days before the last one
    assert metrics["price_now"] == 100.0
    assert metrics["price_1y_ago"] == 50.0
    assert metrics["price_3m_ago"] == round(price_3m, 2)
    assert metrics["change_1y_percent"] == 100.0
    assert metrics["change_3m_percent"] == round((100.0 - price_3m) / price_3m * 100, 2)
    assert len(metrics["sparkline_5d"]) == 5
    assert metrics["high_5d"] == max(metrics["sparkline_5d"])
    assert metrics["low_5d"] == min(metrics["sparkline_5d"])
    assert metrics["anomaly_5d"]["has_spike"] is False


def test_a_young_coin_gets_null_baselines_instead_of_guesses():
    metrics = compute_coin_metrics(_history(days=120), 100.0)  # ~4 months of history

    assert metrics["price_3m_ago"] is not None
    assert metrics["price_1y_ago"] is None
    assert metrics["change_1y_percent"] is None

    newborn = compute_coin_metrics(_history(days=30), 100.0)
    assert newborn["price_3m_ago"] is None and newborn["change_3m_percent"] is None


def test_too_little_history_yields_no_metrics():
    assert compute_coin_metrics(_history(days=5), 100.0) is None
    assert compute_coin_metrics({}, 100.0) is None
    assert compute_coin_metrics(_history(), 0) is None


def _flat_series(n=40, price=100.0, volume=1000.0):
    prices = [[i * DAY_MS, price] for i in range(n)]
    volumes = [[i * DAY_MS, volume] for i in range(n)]
    return prices, volumes


def test_anomaly_flags_a_price_spike_with_its_day_and_direction():
    prices, volumes = _flat_series()
    prices[-1][1] = 120.0

    anomaly = _anomaly_5d(prices, volumes)

    assert anomaly["has_spike"] is True
    assert anomaly["direction"] == "up"
    assert anomaly["max_deviation_percent"] == 20.0
    assert anomaly["context"] == "+20.0% move on day 5 of the last 5"


def test_anomaly_flags_a_crash_and_a_volume_surge_together():
    prices, volumes = _flat_series()
    prices[-3][1] = 80.0
    for point in prices[-2:]:
        point[1] = 80.0
    volumes[-1][1] = 3000.0

    anomaly = _anomaly_5d(prices, volumes)

    assert anomaly["has_spike"] is True and anomaly["direction"] == "down"
    assert "volume 3.0x the 30-day average on day 5" in anomaly["context"]


def test_anomaly_reports_a_volume_surge_without_a_price_spike():
    prices, volumes = _flat_series()
    volumes[-2][1] = 2500.0

    anomaly = _anomaly_5d(prices, volumes)

    assert anomaly["has_spike"] is False
    assert anomaly["context"].startswith("no price spike; volume 2.5x")


def test_a_quiet_week_is_reported_as_no_anomaly():
    prices, volumes = _flat_series()

    anomaly = _anomaly_5d(prices, volumes)

    assert anomaly["has_spike"] is False
    assert anomaly["direction"] is None
    assert anomaly["context"].startswith("no spike")


# --- fetch_state: altcoin goals ----------------------------------------------


def test_the_adapter_opts_into_previous_state():
    assert CryptoFeedAdapter().uses_previous_state is True


def test_fetch_state_builds_the_snapshot_structure_for_an_altcoin_goal():
    state, mock_get = _fetch(DEEP_DIVE)

    assert state["editorial_goal"] == "ALTCOIN_DEEP_DIVE"
    assert state["fetched_at"]
    assert set(state["market_anchors"]) == {"bitcoin", "ethereum"}
    assert state["market_anchors"]["bitcoin"]["price"] == 80000.0
    assert state["market_anchors"]["bitcoin"]["change_1y"] == 50.0
    assert state["web_results"] == []  # the search found nothing; the tick went on without it

    pool = state["analyzed_pool"]
    assert len(pool) == POOL_SIZE
    assert {"id", "symbol", "name", "market_cap_rank", "metrics"} <= set(pool[0])
    assert {"price_now", "price_3m_ago", "price_1y_ago", "sparkline_5d", "anomaly_5d"} <= set(
        pool[0]["metrics"]
    )
    assert len(_history_calls(mock_get)) == POOL_SIZE

    markets_call = mock_get.call_args_list[0]
    assert markets_call.args[0] == MARKETS_URL
    assert markets_call.kwargs["params"]["per_page"] == 200


def test_history_requests_ask_for_a_year_of_daily_data():
    _, mock_get = _fetch(DEEP_DIVE)

    call = _history_calls(mock_get)[0]
    assert call.args[0] == HISTORY_URL.format(coin_id=call.args[0].split("/coins/")[1].split("/")[0])
    assert call.kwargs["params"] == {"vs_currency": "usd", "days": 365, "interval": "daily"}


def test_without_a_pin_the_goal_follows_the_daily_draw():
    # search is stubbed with one crypto and one market headline, whichever web goal is drawn
    headlines = [_web_result(1), _market_result(2)]
    with patch("common.adapters.crypto_feed.search_web", return_value=headlines):
        state, _ = _fetch(_topic())

    assert state["editorial_goal"] == goal_for_date(datetime.now(UTC).date()).value


def test_a_coin_with_failed_history_is_dropped_but_the_pool_survives():
    target = select_altcoin_pool(_markets(), rng=random.Random(POOL_SEED))
    failing = {coin["id"] for coin in target[:3]}

    state, _ = _fetch(DEEP_DIVE, failing=failing)

    ids = {coin["id"] for coin in state["analyzed_pool"]}
    assert len(ids) == POOL_SIZE - 3
    assert not ids & failing


def test_the_run_fails_when_too_few_coins_have_history():
    target = select_altcoin_pool(_markets(), rng=random.Random(POOL_SEED))
    failing = {coin["id"] for coin in target[: POOL_SIZE - MIN_POOL_SIZE + 1]}

    with pytest.raises(RuntimeError, match=f"need at least {MIN_POOL_SIZE}"):
        _fetch(DEEP_DIVE, failing=failing)


def test_a_missing_anchor_is_an_error():
    markets = [c for c in _markets() if c["id"] != "ethereum"]

    with pytest.raises(RuntimeError, match="ethereum"):
        _fetch(DEEP_DIVE, markets=markets)


def test_history_requests_are_concurrent_but_never_exceed_the_limit():
    lock = threading.Lock()
    active = {"now": 0, "peak": 0}

    def fake(url, **kwargs):
        if url == MARKETS_URL:
            return _markets()
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.03)
        with lock:
            active["now"] -= 1
        return _history()

    with (
        patch("common.adapters.crypto_feed.get_json_with_backoff", side_effect=fake),
        patch("common.adapters.crypto_feed.search_web", return_value=[]),
    ):
        CryptoFeedAdapter().fetch_state(DEEP_DIVE)

    assert active["peak"] == HISTORY_CONCURRENCY


# --- fetch_state: history carried for a coin drawn again the same day ---------------
#
# Each tick draws a fresh pool, skipping coins already analysed today, so a coin is only
# drawn twice in a day once the unseen ones run out. These tests use exactly one pool's
# worth of altcoins so that happens on the second tick.

ONE_POOL = POOL_SIZE


def test_a_coin_drawn_again_the_same_day_reuses_history_and_refreshes_its_price():
    first, _ = _fetch(DEEP_DIVE, markets=_markets(altcoins=ONE_POOL))

    repriced = _markets(altcoins=ONE_POOL)
    for coin in repriced:
        coin["current_price"] = coin["current_price"] * 1.5
    second, mock_get = _fetch(DEEP_DIVE, markets=repriced, previous_state=first)

    assert _history_calls(mock_get) == []  # no per-coin history calls at all
    assert [c["id"] for c in second["analyzed_pool"]] == [c["id"] for c in first["analyzed_pool"]]
    for old, new in zip(first["analyzed_pool"], second["analyzed_pool"], strict=True):
        assert new["metrics"]["price_now"] == pytest.approx(old["metrics"]["price_now"] * 1.5, abs=0.02)
        assert new["metrics"]["sparkline_5d"] == old["metrics"]["sparkline_5d"]
        assert new["metrics"]["price_1y_ago"] == old["metrics"]["price_1y_ago"]
        assert new["metrics"]["change_1y_percent"] != old["metrics"]["change_1y_percent"]


def test_a_partly_failed_first_tick_only_refetches_the_missing_coins():
    first, _ = _fetch(DEEP_DIVE, markets=_markets(altcoins=ONE_POOL))
    partial = {**first, "analyzed_pool": first["analyzed_pool"][:7], "analyzed_today": []}

    second, mock_get = _fetch(DEEP_DIVE, markets=_markets(altcoins=ONE_POOL), previous_state=partial)

    assert len(_history_calls(mock_get)) == 3
    assert len(second["analyzed_pool"]) == POOL_SIZE


def test_nothing_carries_over_from_a_previous_day_or_a_legacy_snapshot():
    first, _ = _fetch(DEEP_DIVE, markets=_markets(altcoins=ONE_POOL))

    yesterday = {**first, "fetched_at": "2020-01-01T00:00:00+00:00"}
    _, mock_get = _fetch(DEEP_DIVE, previous_state=yesterday)
    assert len(_history_calls(mock_get)) == POOL_SIZE

    _, mock_get = _fetch(DEEP_DIVE, previous_state={"coins": [], "fetched_at": first["fetched_at"]})
    assert len(_history_calls(mock_get)) == POOL_SIZE


# --- fetch_state: web aggregator ---------------------------------------------


WEB_AGGREGATOR = _topic(editorial_goal="WEB_AGGREGATOR")


def _fetch_web(results_by_query, topic=WEB_AGGREGATOR):
    with (
        patch(
            "common.adapters.crypto_feed.get_json_with_backoff",
            side_effect=_fake_get(_markets()),
        ) as mock_get,
        patch(
            "common.adapters.crypto_feed.search_web",
            side_effect=lambda query, **kwargs: results_by_query[query],
        ) as mock_search,
    ):
        state = CryptoFeedAdapter().fetch_state(topic)
    return state, mock_get, mock_search


def test_web_aggregator_snapshot_has_news_and_anchors_but_no_pool_or_history_calls():
    default_query = "(bitcoin OR ethereum OR cryptocurrency OR crypto)"
    results = [_web_result(1, "2026-09-20T08:00:00+00:00"), _web_result(2, "2026-09-20T10:00:00+00:00")]

    state, mock_get, mock_search = _fetch_web({default_query: results})

    assert state["editorial_goal"] == "WEB_AGGREGATOR"
    assert "analyzed_pool" not in state
    assert set(state["market_anchors"]) == {"bitcoin", "ethereum"}
    assert [r["url"] for r in state["web_results"]] == [
        "https://news.example/2",
        "https://news.example/1",
    ]
    assert _history_calls(mock_get) == []
    kwargs = mock_search.call_args.kwargs
    assert kwargs["max_results"] == 15 and kwargs["max_age_hours"] == 24
    assert "bitcoin" in kwargs["title_keywords"]


def test_web_aggregator_merges_configured_queries_and_dedupes_urls():
    topic = _topic(editorial_goal="WEB_AGGREGATOR", web_search_queries=["a", "b"])
    per_query = {"a": [_web_result(1), _web_result(2)], "b": [_web_result(2), _web_result(3)]}

    state, _, _ = _fetch_web(per_query, topic=topic)

    assert sorted(r["url"] for r in state["web_results"]) == [
        "https://news.example/1",
        "https://news.example/2",
        "https://news.example/3",
    ]


def test_web_aggregator_fails_loudly_when_there_is_no_news():
    with pytest.raises(RuntimeError, match="no crypto news"):
        _fetch_web({"(bitcoin OR ethereum OR cryptocurrency OR crypto)": []})


# --- material_diff -----------------------------------------------------------


def _state(day="2026-09-20", goal="ALTCOIN_DEEP_DIVE", btc=80000.0, alt=10.0, urls=()):
    state = {
        "fetched_at": f"{day}T12:00:00+00:00",
        "editorial_goal": goal,
        "market_anchors": {"bitcoin": {"name": "Bitcoin", "price": btc, "change_24h": 1.0}},
        "analyzed_pool": [{"id": "alt-1", "name": "Alt 1", "metrics": {"price_now": alt}}],
    }
    if urls:
        state["web_results"] = [{"title": f"Story {u}", "url": f"https://x/{u}"} for u in urls]
    return state


def test_material_diff_true_on_first_observation():
    changed, summary = CryptoFeedAdapter().material_diff(None, _state())

    assert changed is True and "initial observation" in summary


def test_material_diff_true_when_the_prior_snapshot_is_the_legacy_format():
    changed, summary = CryptoFeedAdapter().material_diff({"coins": []}, _state())

    assert changed is True and "format upgraded" in summary


def test_material_diff_true_on_a_new_utc_day_and_on_a_goal_change():
    adapter = CryptoFeedAdapter()

    changed, summary = adapter.material_diff(_state(day="2026-09-19"), _state())
    assert changed is True and "new daily analysis" in summary

    changed, summary = adapter.material_diff(_state(), _state(goal="TREND_INVENTOR"))
    assert changed is True and "editorial goal changed to TREND_INVENTOR" in summary


@pytest.mark.parametrize(
    ("btc", "alt"),
    [(80800.0, 10.2), (88000.0, 10.0), (72000.0, 10.0), (80000.0, 8.0), (120000.0, 30.0)],
    ids=["wobble", "anchor-up-10pct", "anchor-down-10pct", "altcoin-down-20pct", "everything-jumps"],
)
def test_material_diff_ignores_price_moves_of_any_size_within_a_day(btc, alt):
    changed, summary = CryptoFeedAdapter().material_diff(_state(), _state(btc=btc, alt=alt))

    assert (changed, summary) == (False, "no material change")


def test_a_price_move_does_not_stop_the_day_rollover_from_being_material():
    changed, summary = CryptoFeedAdapter().material_diff(_state(day="2026-09-19"), _state(btc=80001.0))

    assert changed is True and "new daily analysis" in summary


def test_material_diff_any_new_headline_is_a_web_change():
    adapter = CryptoFeedAdapter()
    old = _state(goal="WEB_AGGREGATOR", urls=[1, 2, 3])

    assert adapter.material_diff(old, _state(goal="WEB_AGGREGATOR", urls=[1, 2, 3])) == (
        False,
        "no material change",
    )

    one_new = _state(goal="WEB_AGGREGATOR", urls=[1, 2, 3, 4])
    changed, summary = adapter.material_diff(old, one_new)
    assert changed is True and summary.startswith("1 new news items: Story 4")


def test_material_diff_a_headline_already_reported_is_not_new_when_it_returns():
    adapter = CryptoFeedAdapter()
    old = _state(goal="WEB_AGGREGATOR", urls=[2, 3])
    old["_seen"] = {"https://x/1": "2026-09-20", "https://x/2": "2026-09-20", "https://x/3": "2026-09-20"}

    # story 1 dropped out of the results earlier and is back: not news
    assert adapter.material_diff(old, _state(goal="WEB_AGGREGATOR", urls=[1, 2, 3])) == (
        False,
        "no material change",
    )


# --- source_refs -------------------------------------------------------------


def test_source_refs_trace_anchors_pool_coins_and_articles():
    state = _state(urls=[1, 2])
    state["fetched_at"] = "2026-09-20T12:00:00+00:00"

    refs = CryptoFeedAdapter().source_refs(state)

    assert [r["url"] for r in refs] == [
        "https://www.coingecko.com/en/coins/bitcoin",
        "https://www.coingecko.com/en/coins/alt-1",
        "https://x/1",
        "https://x/2",
    ]
    assert {r["accessed_at"] for r in refs} == {"2026-09-20T12:00:00+00:00"}
    assert refs[1]["title"] == "Alt 1" and refs[2]["title"] == "Story 1"


def test_source_refs_from_a_real_snapshot_cover_every_sampled_coin():
    state, _ = _fetch(DEEP_DIVE)

    urls = {r["url"] for r in CryptoFeedAdapter().source_refs(state)}

    assert len(urls) == 2 + POOL_SIZE
    assert {f"https://www.coingecko.com/en/coins/{c['id']}" for c in state["analyzed_pool"]} <= urls


# --- research-summary prompt (P1) ----------------------------------------------


def _prompt(goal, **kwargs):
    state = _state(goal=goal, **kwargs)
    return CryptoFeedAdapter().build_summary_prompt({"name": "Crypto"}, "the diff", state)


def test_altcoin_prompt_names_the_anchors_the_diff_and_the_raw_metrics():
    prompt = _prompt("ALTCOIN_DEEP_DIVE")

    assert 'topic "Crypto"' in prompt
    assert "Current Market Anchors: Bitcoin $80,000 (+1.00% 24h)" in prompt
    assert "What changed: the diff" in prompt
    assert "randomized deep dive into 1 altcoins" in prompt
    assert "5-day anomaly" in prompt
    assert '"price_now":10.0' in prompt
    assert "do not give financial or investment advice" in prompt


def test_trend_prompt_asks_for_framework_raw_material_not_a_verdict():
    prompt = _prompt("TREND_INVENTOR")

    assert "multi-metric trends" in prompt
    assert "5-day, 3-month and 1-year windows" in prompt


def test_web_prompt_lists_the_headlines_and_admits_it_has_no_article_bodies():
    prompt = _prompt("WEB_AGGREGATOR", urls=[1, 2])

    assert "synthesis of 2 crypto news items" in prompt
    assert "only headlines and source names are available" in prompt
    assert "https://x/1" in prompt
    # headlines are the noisiest input, so the web summary carries the rule
    assert "RELEVANCE RULE: this digest covers 'Crypto' and nothing else" in prompt


def test_the_research_prompt_carries_the_crypto_adapter_default_goal():
    topic = {"topic_id": "c", "name": "Crypto", "adapter": "crypto_feed"}

    prompt = CryptoFeedAdapter().build_summary_prompt(topic, "the diff", _state(goal="ALTCOIN_DEEP_DIVE"))

    assert "OPERATIONAL EDITORIAL GOAL:\nAdapter-Specific Standard Goal: Prioritize structural" in prompt
    assert prompt.index("OPERATIONAL EDITORIAL GOAL") < prompt.index("What changed:")


def test_a_topic_specific_goal_overrides_the_crypto_default_in_every_goal_mode():
    topic = {
        "topic_id": "c",
        "name": "Crypto",
        "adapter": "crypto_feed",
        "editorial_goals": {
            "primary_focus": "Track stablecoin supply.",
            "exclusion_criteria": "No memecoins.",
        },
    }

    for goal in EditorialGoal:
        prompt = CryptoFeedAdapter().build_summary_prompt(
            topic, "the diff", _state(goal=goal.value, urls=[1])
        )
        assert "Topic-Specific Focus: Track stablecoin supply.\nStrict Constraints: No memecoins." in prompt
        assert "Adapter-Specific Standard Goal" not in prompt


def test_no_prompt_for_a_legacy_or_unknown_goal_snapshot():
    adapter = CryptoFeedAdapter()

    assert adapter.build_summary_prompt({}, "diff", {"coins": []}) is None
    assert adapter.build_summary_prompt({}, "diff", _state(goal="NOPE")) is None


def test_every_goal_has_a_prompt():
    for goal in EditorialGoal:
        assert _prompt(goal.value, urls=[1])


# --- CoinGecko API key + keyless fallback --------------------------------------

SECRET = "CG-super-secret-key"
GET = "common.adapters.crypto_feed.get_json_with_backoff"


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} error", response=response)


def _keyed(kwargs):
    return any(name.startswith("x-cg-") for name in kwargs["headers"])


def test_without_a_key_requests_go_to_the_public_api_with_no_key_header():
    with patch(GET, return_value=[]) as mock_get:
        CoinGeckoClient().get_json("/coins/markets", params={"a": 1})

    url = mock_get.call_args.args[0]
    assert url == PUBLIC_BASE_URL + "/coins/markets"
    assert not _keyed(mock_get.call_args.kwargs)
    assert mock_get.call_count == 1


def test_a_demo_key_is_sent_as_a_header_on_the_public_host_never_in_the_url():
    with patch(GET, return_value=[]) as mock_get:
        CoinGeckoClient(api_key=SECRET).get_json("/coins/markets", params={"a": 1})

    call = mock_get.call_args
    assert call.args[0] == PUBLIC_BASE_URL + "/coins/markets"
    assert call.kwargs["headers"]["x-cg-demo-api-key"] == SECRET
    assert call.kwargs["max_attempts"] == KEYED_MAX_ATTEMPTS
    assert SECRET not in call.args[0] and SECRET not in str(call.kwargs["params"])


def test_a_pro_key_uses_the_pro_host_and_header():
    with patch(GET, return_value=[]) as mock_get:
        CoinGeckoClient(api_key=SECRET, plan="pro").get_json("/coins/markets")

    call = mock_get.call_args
    assert call.args[0] == PRO_BASE_URL + "/coins/markets"
    assert call.kwargs["headers"]["x-cg-pro-api-key"] == SECRET


def test_from_env_reads_key_and_plan_and_treats_blank_or_unknown_values_safely(monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", "  ")
    assert CoinGeckoClient.from_env().uses_key is False

    monkeypatch.setenv("COINGECKO_API_KEY", SECRET)
    monkeypatch.setenv("COINGECKO_API_PLAN", "PRO")
    with patch(GET, return_value=[]) as mock_get:
        CoinGeckoClient.from_env().get_json("/x")
    assert mock_get.call_args.args[0].startswith(PRO_BASE_URL)

    monkeypatch.setenv("COINGECKO_API_PLAN", "nonsense")
    with patch(GET, return_value=[]) as mock_get:
        CoinGeckoClient.from_env().get_json("/x")
    assert mock_get.call_args.args[0].startswith(PUBLIC_BASE_URL)
    assert "x-cg-demo-api-key" in mock_get.call_args.kwargs["headers"]


@pytest.mark.parametrize(
    "failure",
    [_http_error(429), _http_error(503), requests.ConnectionError("down"), requests.Timeout("slow")],
    ids=["rate-limited", "server-error", "connection", "timeout"],
)
def test_a_failed_keyed_request_falls_back_to_the_public_api(failure):
    client = CoinGeckoClient(api_key=SECRET)
    outcomes = [failure, {"ok": True}]

    def fake(url, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with patch(GET, side_effect=fake) as mock_get:
        result = client.get_json("/coins/markets")

    assert result == {"ok": True}
    assert _keyed(mock_get.call_args_list[0].kwargs)
    assert not _keyed(mock_get.call_args_list[1].kwargs)
    assert client.uses_key is True  # transient trouble doesn't disable the key


def test_a_rejected_key_is_dropped_for_the_rest_of_the_run():
    client = CoinGeckoClient(api_key=SECRET)

    def fake(url, **kwargs):
        if _keyed(kwargs):
            raise _http_error(401)
        return {"ok": True}

    with patch(GET, side_effect=fake) as mock_get:
        assert client.get_json("/a") == {"ok": True}
        assert client.get_json("/b") == {"ok": True}
        assert client.get_json("/c") == {"ok": True}

    keyed_calls = [c for c in mock_get.call_args_list if _keyed(c.kwargs)]
    assert len(keyed_calls) == 1  # one wasted request, not one per call
    assert client.uses_key is False


def test_if_the_public_fallback_also_fails_its_error_is_raised():
    client = CoinGeckoClient(api_key=SECRET)

    with patch(GET, side_effect=_http_error(429)):
        with pytest.raises(requests.HTTPError):
            client.get_json("/coins/markets")


def test_the_key_never_appears_in_logs(capsys):
    client = CoinGeckoClient(api_key=SECRET)

    def fake(url, **kwargs):
        if _keyed(kwargs):
            raise _http_error(401)
        return {}

    with patch(GET, side_effect=fake):
        client.get_json("/coins/markets")

    output = capsys.readouterr().out
    assert "API key rejected" in output
    assert SECRET not in output


def test_a_full_fetch_still_succeeds_when_the_key_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("COINGECKO_API_KEY", SECRET)
    markets = _markets()
    public = _fake_get(markets)

    def fake(url, **kwargs):
        if _keyed(kwargs):
            raise _http_error(401)
        return public(url, **kwargs)

    with patch(GET, side_effect=fake):
        state = CryptoFeedAdapter().fetch_state(DEEP_DIVE)

    assert len(state["analyzed_pool"]) == POOL_SIZE
    assert SECRET not in capsys.readouterr().out


def test_a_full_fetch_uses_the_key_for_every_request_when_it_works(monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", SECRET)
    public = _fake_get(_markets())

    with patch(GET, side_effect=lambda url, **kwargs: public(url, **kwargs)) as mock_get:
        state = CryptoFeedAdapter().fetch_state(DEEP_DIVE)

    assert len(state["analyzed_pool"]) == POOL_SIZE
    assert all(_keyed(call.kwargs) for call in mock_get.call_args_list)


# --- MARKET_NEWS day: general finance news, nothing to do with crypto ---------------------

MARKET_NEWS_TOPIC = _topic(editorial_goal="MARKET_NEWS")


def _fetch_market_news(results, topic=MARKET_NEWS_TOPIC):
    with (
        patch(GET) as mock_get,
        patch("common.adapters.crypto_feed.search_web", return_value=results) as mock_search,
    ):
        state = CryptoFeedAdapter().fetch_state(topic)
    return state, mock_get, mock_search


def test_a_market_news_day_makes_no_coingecko_call_and_carries_no_crypto_data():
    state, mock_get, _ = _fetch_market_news([_market_result(1), _market_result(2)])

    mock_get.assert_not_called()
    assert state["editorial_goal"] == "MARKET_NEWS"
    assert state["market_anchors"] == {}  # key kept so the snapshot format is unchanged
    assert "analyzed_pool" not in state
    assert len(state["web_results"]) == 2


def test_market_news_searches_finance_terms_and_filters_titles_to_finance():
    _, _, mock_search = _fetch_market_news([_market_result(1)])

    call = mock_search.call_args
    assert '"stock market"' in call.args[0] and "inflation" in call.args[0]
    assert "bitcoin" not in call.args[0].lower()
    assert "stock*" in call.kwargs["title_keywords"]
    assert call.kwargs["max_age_hours"] == 24
    assert call.kwargs["max_results"] == 30  # over-fetch: crypto is excluded afterwards


def test_market_news_drops_crypto_headlines_but_keeps_ordinary_etf_news():
    results = [
        _market_result(1, "Bitcoin rallies as stocks climb"),
        _market_result(2, "Ethereum ETF sees record market inflows"),
        _market_result(3, "Coinbase earnings beat estimates"),
        _market_result(4, "Bond ETF inflows hit a record as yields fall"),
        _market_result(5, "Stocks rally as inflation cools"),
    ]

    state, _, _ = _fetch_market_news(results)

    assert [r["url"] for r in state["web_results"]] == [
        "https://markets.example/4",
        "https://markets.example/5",
    ]


def test_market_news_queries_can_be_overridden_and_are_kept_separate_from_crypto_queries():
    topic = _topic(
        editorial_goal="MARKET_NEWS",
        market_news_queries=["oil prices"],
        web_search_queries=["ignored for market news"],
    )

    _, _, mock_search = _fetch_market_news([_market_result(1)], topic=topic)

    assert [c.args[0] for c in mock_search.call_args_list] == ["oil prices"]


def test_market_news_fails_loudly_when_only_crypto_headlines_come_back():
    with pytest.raises(RuntimeError, match="no general financial-market news"):
        _fetch_market_news([_market_result(1, "Bitcoin and stocks both rise")])


def test_the_crypto_web_aggregator_is_unchanged_and_still_uses_crypto_filters():
    with (
        patch(GET, side_effect=_fake_get(_markets())),
        patch("common.adapters.crypto_feed.search_web", return_value=[_web_result(1)]) as mock_search,
    ):
        state = CryptoFeedAdapter().fetch_state(_topic(editorial_goal="WEB_AGGREGATOR"))

    assert "bitcoin" in mock_search.call_args.kwargs["title_keywords"]
    assert mock_search.call_args.kwargs["max_results"] == 15
    assert set(state["market_anchors"]) == {"bitcoin", "ethereum"}


def _market_news_state(day="2026-09-20", urls=()):
    return {
        "fetched_at": f"{day}T12:00:00+00:00",
        "editorial_goal": "MARKET_NEWS",
        "market_anchors": {},
        "web_results": [{"title": f"Story {u}", "url": f"https://x/{u}"} for u in urls],
    }


def test_market_news_material_diff_follows_new_headlines_not_crypto_prices():
    adapter = CryptoFeedAdapter()
    old = _market_news_state(urls=[1, 2, 3])

    assert adapter.material_diff(old, _market_news_state(urls=[1, 2, 3])) == (
        False,
        "no material change",
    )
    changed, summary = adapter.material_diff(old, _market_news_state(urls=range(1, 9)))
    assert changed is True and summary.startswith("5 new news items")
    changed, summary = adapter.material_diff(_market_news_state(day="2026-09-19"), old)
    assert changed is True and "new daily analysis" in summary


def test_market_news_prompt_has_no_crypto_anchors_and_says_crypto_is_out_of_scope():
    topic = {"topic_id": "c", "name": "Finance", "adapter": "crypto_feed"}

    prompt = CryptoFeedAdapter().build_summary_prompt(
        topic, "the diff", _market_news_state(urls=[1, 2])
    )

    assert "Current Market Anchors" not in prompt
    assert "synthesis of 2 general financial-market (not crypto) news items" in prompt
    assert "RELEVANCE RULE: this digest covers 'Finance' and nothing else" in prompt
    assert "do not give financial or investment advice" in prompt
    # the crypto adapter's own standing goal would contradict a non-crypto day
    assert "asset cap distributions" not in prompt
    assert "Global Default Goal" in prompt


def test_market_news_source_refs_are_just_the_articles():
    refs = CryptoFeedAdapter().source_refs(_market_news_state(urls=[1, 2]))

    assert [r["url"] for r in refs] == ["https://x/1", "https://x/2"]


# --- a fresh random pool every tick, and headlines on analysis days ----------------------------


def _pool_ids(state):
    return {coin["id"] for coin in state["analyzed_pool"]}


def test_successive_ticks_the_same_day_analyse_different_coins():
    markets = _markets(altcoins=40)
    first, _ = _fetch(DEEP_DIVE, markets=markets, seed=1)

    second, _ = _fetch(DEEP_DIVE, markets=markets, previous_state=first, seed=2)

    assert len(_pool_ids(second)) == POOL_SIZE
    assert not _pool_ids(first) & _pool_ids(second)  # nothing analysed twice while others remain


def test_the_snapshot_records_every_coin_analysed_so_far_today():
    markets = _markets(altcoins=40)
    first, _ = _fetch(DEEP_DIVE, markets=markets, seed=1)
    second, _ = _fetch(DEEP_DIVE, markets=markets, previous_state=first, seed=2)

    assert set(first["analyzed_today"]) == _pool_ids(first)
    assert set(second["analyzed_today"]) == _pool_ids(first) | _pool_ids(second)
    assert second["analyzed_today"] == sorted(second["analyzed_today"])


def test_a_coin_that_lost_its_history_is_not_counted_as_analysed():
    markets = _markets(altcoins=40)
    target = select_altcoin_pool(markets, rng=random.Random(POOL_SEED))
    failing = {target[0]["id"]}

    state, _ = _fetch(DEEP_DIVE, markets=markets, failing=failing)

    assert target[0]["id"] not in state["analyzed_today"]


def test_a_new_utc_day_or_a_legacy_snapshot_starts_with_nothing_analysed():
    markets = _markets(altcoins=40)
    first, _ = _fetch(DEEP_DIVE, markets=markets, seed=1)

    yesterday, _ = _fetch(
        DEEP_DIVE,
        markets=markets,
        previous_state={**first, "fetched_at": "2020-01-01T00:00:00+00:00"},
        seed=1,
    )
    legacy, _ = _fetch(
        DEEP_DIVE, markets=markets, previous_state={"coins": [], "fetched_at": first["fetched_at"]}, seed=1
    )

    # the same seed draws the same coins again: nothing was excluded
    assert _pool_ids(yesterday) == _pool_ids(first) == _pool_ids(legacy)


def test_a_previous_snapshot_without_the_list_still_excludes_its_own_pool():
    markets = _markets(altcoins=40)
    first, _ = _fetch(DEEP_DIVE, markets=markets, seed=1)
    without_list = {k: v for k, v in first.items() if k != "analyzed_today"}

    second, _ = _fetch(DEEP_DIVE, markets=markets, previous_state=without_list, seed=1)

    assert not _pool_ids(first) & _pool_ids(second)


def test_analysis_days_carry_the_latest_headlines():
    headlines = [_web_result(1, "2026-09-20T08:00:00+00:00"), _web_result(2, "2026-09-20T10:00:00+00:00")]

    state, _ = _fetch(DEEP_DIVE, headlines=headlines)

    assert [r["url"] for r in state["web_results"]] == ["https://news.example/2", "https://news.example/1"]
    assert len(state["analyzed_pool"]) == POOL_SIZE  # market data is unaffected


def test_a_headline_search_that_errors_does_not_sink_an_analysis_tick(capsys):
    with (
        patch(GET, side_effect=_fake_get(_markets())),
        patch("common.adapters.crypto_feed.search_web", side_effect=RuntimeError("gdelt down")),
        _pinned_draw(),
    ):
        state = CryptoFeedAdapter().fetch_state(DEEP_DIVE)

    assert state["web_results"] == [] and len(state["analyzed_pool"]) == POOL_SIZE
    assert "no headlines this tick (gdelt down)" in capsys.readouterr().out


def test_analysis_day_headlines_use_the_crypto_query_not_the_market_news_one():
    default_query = "(bitcoin OR ethereum OR cryptocurrency OR crypto)"
    seen_queries = []

    def search(query, **kwargs):
        seen_queries.append(query)
        return [_web_result(1)]

    with (
        patch(GET, side_effect=_fake_get(_markets())),
        patch("common.adapters.crypto_feed.search_web", side_effect=search),
        _pinned_draw(),
    ):
        CryptoFeedAdapter().fetch_state(DEEP_DIVE)

    assert seen_queries == [default_query]


def test_the_market_news_day_still_makes_no_coingecko_call_or_pool():
    state, mock_get, _ = _fetch_market_news([_market_result(1)])

    assert "analyzed_pool" not in state and "analyzed_today" not in state
    mock_get.assert_not_called()


def test_a_freshly_sampled_pool_is_new_information_even_when_prices_barely_moved():
    adapter = CryptoFeedAdapter()
    old = _state()
    new = _state()
    new["analyzed_pool"] = [{"id": "alt-2", "name": "Alt 2", "metrics": {"price_now": 10.0}}]

    changed, summary = adapter.material_diff(old, new)

    assert changed is True and summary == "1 newly sampled coins: Alt 2"


def test_only_the_coins_not_in_the_last_pool_count_as_newly_sampled():
    adapter = CryptoFeedAdapter()
    old = _state()
    new = _state()
    new["analyzed_pool"].append({"id": "alt-9", "name": "Alt 9", "metrics": {"price_now": 3.0}})

    changed, summary = adapter.material_diff(old, new)

    assert changed is True and summary == "1 newly sampled coins: Alt 9"


def test_the_same_pool_with_no_new_headlines_is_still_not_material():
    assert CryptoFeedAdapter().material_diff(_state(), _state(btc=90000.0, alt=12.0)) == (
        False,
        "no material change",
    )


def test_new_coins_and_new_headlines_are_both_reported():
    adapter = CryptoFeedAdapter()
    old = _state(urls=[1])
    new = _state(urls=[1, 2])
    new["analyzed_pool"] = [{"id": "alt-2", "name": "Alt 2", "metrics": {"price_now": 10.0}}]

    changed, summary = adapter.material_diff(old, new)

    assert changed is True
    assert summary == "1 newly sampled coins: Alt 2; 1 new news items: Story 2"


def _analysis_prompt(state, goal="ALTCOIN_DEEP_DIVE"):
    topic = {"topic_id": "crypto", "name": "Finance, Crypto & Investing", "adapter": "crypto_feed"}
    return CryptoFeedAdapter().build_summary_prompt(topic, "the diff", {**state, "editorial_goal": goal})


def test_an_analysis_prompt_tells_the_model_how_to_use_headlines_only_when_there_are_some():
    with_news = _analysis_prompt(_state(urls=[1]))
    without = _analysis_prompt(_state())

    assert "latest crypto news headlines" in with_news
    assert "attribute it rather than adding any detail the headline does not contain" in with_news
    assert "latest crypto news headlines" not in without


def test_the_bookkeeping_list_is_kept_out_of_the_summary_prompt():
    state = {**_state(), "analyzed_today": ["alt-1", "alt-secret-marker"]}

    prompt = _analysis_prompt(state)

    assert "alt-secret-marker" not in prompt and "analyzed_today" not in prompt
    assert "Raw Analysis Metrics" in prompt


# --- the fresh-data review's evidence ---------------------------------------------------------


def _evidence(topic=DEEP_DIVE, latest_state=None, markets=None, headlines=(), search_error=None):
    search = patch("common.adapters.crypto_feed.search_web", return_value=list(headlines))
    if search_error:
        search = patch("common.adapters.crypto_feed.search_web", side_effect=search_error)
    with patch(GET, side_effect=_fake_get(markets or _markets())) as mock_get, search:
        text = CryptoFeedAdapter().review_evidence(topic, latest_state)
    return json.loads(text), mock_get


def test_evidence_covers_the_anchors_and_only_the_coins_the_latest_snapshot_analysed():
    latest = {"analyzed_today": ["alt-1", "alt-2"], "analyzed_pool": [{"id": "alt-3"}]}

    evidence, mock_get = _evidence(latest_state=latest)

    assert set(evidence["market_anchors"]) == {"bitcoin", "ethereum"}
    assert [c["name"] for c in evidence["coins"]] == ["Alt 1", "Alt 2", "Alt 3"]
    assert _history_calls(mock_get) == []  # one markets call, no per-coin history


def test_the_evidence_is_the_current_price_not_the_one_in_the_snapshot():
    markets = _markets()
    for coin in markets:
        if coin["id"] == "alt-1":
            coin["current_price"] = 123.45

    evidence, _ = _evidence(latest_state={"analyzed_today": ["alt-1"]}, markets=markets)

    assert evidence["coins"][0]["price"] == 123.45
    assert {"change_24h", "change_7d", "change_30d", "rank", "symbol"} <= set(evidence["coins"][0])


def test_a_coin_that_has_left_the_top_200_is_counted_not_silently_dropped():
    evidence, _ = _evidence(latest_state={"analyzed_today": ["alt-1", "long-gone-coin"]})

    assert [c["name"] for c in evidence["coins"]] == ["Alt 1"]
    assert evidence["coins_no_longer_in_the_top_200"] == 1


def test_with_no_snapshot_the_evidence_is_the_anchors_and_headlines_only():
    evidence, _ = _evidence(latest_state=None, headlines=[_web_result(1)])

    assert evidence["coins"] == [] and set(evidence["market_anchors"]) == {"bitcoin", "ethereum"}
    assert [h["title"] for h in evidence["headlines"]] == ["Bitcoin story 1"]


def test_headlines_carry_their_source_and_date_and_nothing_else():
    evidence, _ = _evidence(headlines=[_web_result(1, "2026-09-20T08:00:00+00:00")])

    assert evidence["headlines"] == [
        {"title": "Bitcoin story 1", "source": "news.example", "published_at": "2026-09-20T08:00:00+00:00"}
    ]


def test_a_failed_headline_search_does_not_stop_the_evidence(capsys):
    evidence, _ = _evidence(
        latest_state={"analyzed_today": ["alt-1"]}, search_error=RuntimeError("gdelt down")
    )

    assert evidence["headlines"] == [] and len(evidence["coins"]) == 1
    assert "no headlines for the review" in capsys.readouterr().out


def test_a_general_market_news_day_has_no_coins_and_makes_no_coingecko_call():
    with (
        patch(GET) as mock_get,
        patch("common.adapters.crypto_feed.search_web", return_value=[_market_result(1)]),
    ):
        text = CryptoFeedAdapter().review_evidence(MARKET_NEWS_TOPIC, {"analyzed_today": ["alt-1"]})

    evidence = json.loads(text)
    assert "coins" not in evidence and "market_anchors" not in evidence
    assert evidence["headlines"] and evidence["editorial_goal"] == "MARKET_NEWS"
    mock_get.assert_not_called()


def test_a_markets_failure_is_raised_so_the_review_reports_unavailable():
    with (
        patch(GET, side_effect=RuntimeError("coingecko down")),
        patch("common.adapters.crypto_feed.search_web", return_value=[]),
        pytest.raises(RuntimeError, match="coingecko down"),
    ):
        CryptoFeedAdapter().review_evidence(DEEP_DIVE, None)
