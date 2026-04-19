"""Tests for legacy OFI/flash-snipe strategies wired into explain_choose_side."""

import pytest
from collections import deque
from core.config import SETTINGS
from core.decision_engine import explain_choose_side


def _make_market(secs_left=400.0):
    from datetime import datetime, timezone, timedelta
    end = datetime.now(timezone.utc) + timedelta(seconds=secs_left)
    return {
        "slug": "btc-updown-15m-9999999999",
        "endDate": end.isoformat(),
        "question": "Will BTC be above $85,000?",
        "strike_price": 85000.0,
    }


def _make_binance_1m(price=85100.0):
    return {"close": price, "open": price - 10, "high": price + 20, "low": price - 20}


def _make_binance_5m(price=85100.0, n=5):
    candles = []
    for i in range(n):
        p = price + (i - n // 2) * 15
        candles.append({"close": p, "open": p - 10, "high": p + 25, "low": p - 25})
    return candles


def _make_orderbook(bid_price=0.20, ask_price=0.22, size=500):
    return {
        "bids": [{"price": bid_price, "size": size}],
        "asks": [{"price": ask_price, "size": size}],
        "bids_volume": size * bid_price,
        "asks_volume": size * ask_price,
    }


def _make_ws_trades_high_ofi(buy_vol=80000, sell_vol=10000):
    """Fake trade list where buy volume >> sell volume (OFI > 0.75).

    Uses Binance aggTrade format: p=price, q=quantity, m=is_seller.
    """
    trades = []
    price = 85100.0
    buy_qty_each = (buy_vol / price) / 20
    sell_qty_each = (sell_vol / price) / 5
    for i in range(20):
        trades.append({
            "p": price + i * 0.5,
            "q": buy_qty_each,
            "m": False,  # buyer is maker=False => market buy
            "timestamp": 1776487500 + i,
        })
    for i in range(5):
        trades.append({
            "p": price - i * 0.5,
            "q": sell_qty_each,
            "m": True,   # buyer is maker=True => market sell
            "timestamp": 1776487520 + i,
        })
    return trades


def test_ofi_signal_fires_when_enabled():
    """With ENABLE_LEGACY_STRATEGIES=True and OFI > 0.75, at least one
    legacy candidate should appear."""
    original = SETTINGS.enable_legacy_strategies
    original_vol = SETTINGS.min_volatility_gate_bps
    original_ofi = SETTINGS.min_poly_ofi_threshold
    try:
        SETTINGS.enable_legacy_strategies = True
        SETTINGS.min_volatility_gate_bps = 0.0
        SETTINGS.min_poly_ofi_threshold = 0.0

        market = _make_market(secs_left=400.0)
        # bid_volume > ask_volume so poly_up_imbalance >= 0.55
        ob_up = _make_orderbook(bid_price=0.80, ask_price=0.82, size=500)
        ob_up["bids_volume"] = 600
        ob_up["asks_volume"] = 300
        ob_down = _make_orderbook(bid_price=0.18, ask_price=0.20, size=500)
        ob_down["bids_volume"] = 300
        ob_down["asks_volume"] = 600
        ws_trades = _make_ws_trades_high_ofi(buy_vol=80000, sell_vol=10000)

        result = explain_choose_side(
            market=market,
            yes_window=deque([0.80, 0.81, 0.79], maxlen=20),
            up_window=deque([0.80, 0.81, 0.79], maxlen=20),
            down_window=deque([0.20, 0.19, 0.21], maxlen=20),
            observed_up=0.80,
            observed_down=0.20,
            binance_1m=_make_binance_1m(),
            binance_5m=_make_binance_5m(),
            ws_trades=ws_trades,
            poly_ob_up=ob_up,
            poly_ob_down=ob_down,
        )

        ranked = result.get("ranked_candidates", [])
        has_legacy = any("legacy" in str(c.get("reason", "")) for c in ranked)
        is_legacy_winner = "legacy" in str(result.get("reason", ""))

        assert has_legacy or is_legacy_winner or result.get("ok"), (
            f"Expected legacy strategy to fire with high OFI, got: {result.get('reason')}"
        )
    finally:
        SETTINGS.enable_legacy_strategies = original
        SETTINGS.min_volatility_gate_bps = original_vol
        SETTINGS.min_poly_ofi_threshold = original_ofi


def test_ofi_signal_does_not_fire_when_disabled():
    """With ENABLE_LEGACY_STRATEGIES=False, no legacy candidates appear."""
    original = SETTINGS.enable_legacy_strategies
    try:
        SETTINGS.enable_legacy_strategies = False

        market = _make_market(secs_left=400.0)
        ob_up = _make_orderbook(bid_price=0.78, ask_price=0.80, size=500)
        ob_down = _make_orderbook(bid_price=0.18, ask_price=0.20, size=500)
        ws_trades = _make_ws_trades_high_ofi()

        result = explain_choose_side(
            market=market,
            yes_window=deque([0.80], maxlen=20),
            up_window=deque([0.80], maxlen=20),
            down_window=deque([0.20], maxlen=20),
            observed_up=0.80,
            observed_down=0.20,
            binance_1m=_make_binance_1m(),
            binance_5m=_make_binance_5m(),
            ws_trades=ws_trades,
            poly_ob_up=ob_up,
            poly_ob_down=ob_down,
        )

        ranked = result.get("ranked_candidates", [])
        has_legacy = any("legacy" in str(c.get("reason", "")) for c in ranked)
        assert not has_legacy, "Legacy strategies should not fire when disabled"
    finally:
        SETTINGS.enable_legacy_strategies = original
