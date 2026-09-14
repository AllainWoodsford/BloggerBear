from __future__ import annotations

from unittest.mock import Mock, patch

from common.adapters.crypto_feed import CryptoFeedAdapter


def _mock_response(coins: list[dict]) -> Mock:
    response = Mock()
    response.json = Mock(return_value=coins)
    response.raise_for_status = Mock()
    return response


def _coin(coin_id, symbol, name, price, market_cap=1_000_000, change_24h=0.0):
    return {
        "id": coin_id,
        "symbol": symbol,
        "name": name,
        "current_price": price,
        "market_cap": market_cap,
        "price_change_percentage_24h": change_24h,
    }


def test_fetch_state_parses_coins_and_uses_default_ids_when_unconfigured():
    coins = [_coin("bitcoin", "btc", "Bitcoin", 50000.0), _coin("ethereum", "eth", "Ethereum", 3000.0)]

    adapter = CryptoFeedAdapter()
    with patch(
        "common.adapters.crypto_feed.requests.get", return_value=_mock_response(coins)
    ) as mock_get:
        state = adapter.fetch_state({"adapter_config": {}})

    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs["params"]["ids"] == "bitcoin,ethereum"
    assert "fetched_at" in state
    assert len(state["coins"]) == 2
    assert state["coins"][0]["id"] == "bitcoin"
    assert state["coins"][0]["current_price"] == 50000.0


def test_fetch_state_uses_configured_coin_ids():
    coins = [_coin("solana", "sol", "Solana", 100.0)]

    adapter = CryptoFeedAdapter()
    with patch(
        "common.adapters.crypto_feed.requests.get", return_value=_mock_response(coins)
    ) as mock_get:
        adapter.fetch_state({"adapter_config": {"coin_ids": ["solana"]}})

    assert mock_get.call_args.kwargs["params"]["ids"] == "solana"


def test_material_diff_true_on_first_observation():
    adapter = CryptoFeedAdapter()
    new_state = {"coins": [_coin("bitcoin", "btc", "Bitcoin", 50000.0)]}

    changed, summary = adapter.material_diff(None, new_state)

    assert changed is True
    assert "initial observation" in summary


def test_material_diff_false_on_small_price_wobble():
    old_state = {"coins": [_coin("bitcoin", "btc", "Bitcoin", 50000.0)]}
    new_state = {"coins": [_coin("bitcoin", "btc", "Bitcoin", 50500.0)]}  # +1%

    adapter = CryptoFeedAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is False
    assert summary == "no material change"


def test_material_diff_true_on_large_price_move():
    old_state = {"coins": [_coin("bitcoin", "btc", "Bitcoin", 50000.0)]}
    new_state = {"coins": [_coin("bitcoin", "btc", "Bitcoin", 55000.0)]}  # +10%

    adapter = CryptoFeedAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is True
    assert "price moves" in summary


def test_material_diff_true_on_large_price_drop():
    old_state = {"coins": [_coin("bitcoin", "btc", "Bitcoin", 50000.0)]}
    new_state = {"coins": [_coin("bitcoin", "btc", "Bitcoin", 45000.0)]}  # -10%

    adapter = CryptoFeedAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is True
    assert "price moves" in summary


def test_material_diff_true_when_coin_set_changes():
    old_state = {"coins": [_coin("bitcoin", "btc", "Bitcoin", 50000.0)]}
    new_state = {"coins": [_coin("dogecoin", "doge", "Dogecoin", 0.1)]}

    adapter = CryptoFeedAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is True
    assert "entered: dogecoin" in summary
    assert "left: bitcoin" in summary


def test_source_refs_one_per_coin():
    new_state = {
        "fetched_at": "2026-09-13T00:00:00+00:00",
        "coins": [_coin("bitcoin", "btc", "Bitcoin", 50000.0)],
    }

    adapter = CryptoFeedAdapter()
    refs = adapter.source_refs(new_state)

    assert refs == [
        {
            "url": "https://www.coingecko.com/en/coins/bitcoin",
            "title": "Bitcoin",
            "accessed_at": "2026-09-13T00:00:00+00:00",
        }
    ]
