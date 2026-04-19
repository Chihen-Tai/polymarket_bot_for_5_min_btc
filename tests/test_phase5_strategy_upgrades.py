import os
import sys
import unittest
from collections import deque
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if "requests" not in sys.modules:
    requests_stub = ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    requests_stub.post = lambda *args, **kwargs: None
    requests_stub.exceptions = SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = requests_stub

if "websocket" not in sys.modules:
    websocket_stub = ModuleType("websocket")
    websocket_stub.WebSocketApp = object
    sys.modules["websocket"] = websocket_stub


class TestPhase5StrategyUpgrades(unittest.TestCase):
    def setUp(self):
        self.market = {
            "slug": "btc-updown-15m-1776504600",
            "question": "Bitcoin Up or Down - test",
            "strike_price": 100000.0,
            "endDate": "2026-12-31T23:59:59Z",
        }
        self.yes_window = deque([0.40, 0.41, 0.42], maxlen=20)
        self.up_window = deque([0.40, 0.41, 0.42], maxlen=20)
        self.down_window = deque([0.60, 0.59, 0.58], maxlen=20)
        self.poly_ob_up = {
            "best_bid": 0.41,
            "best_ask": 0.42,
            "bids": [{"price": 0.41, "size": 400.0}],
            "asks": [{"price": 0.42, "size": 100.0}],
            "ask_levels": [(0.42, 1000.0)],
        }
        self.poly_ob_down = {
            "best_bid": 0.58,
            "best_ask": 0.59,
            "bids": [{"price": 0.58, "size": 400.0}],
            "asks": [{"price": 0.59, "size": 100.0}],
            "ask_levels": [(0.59, 1000.0)],
        }

    def test_market_window_features_capture_spot_delta_and_velocity(self):
        from core.decision_engine import compute_market_window_features

        binance_5m = [
            {
                "open_time": 1776504600000,
                "open": 100000.0,
                "close": 100020.0,
            },
            {
                "open_time": 1776504900000,
                "open": 100020.0,
                "close": 100080.0,
            },
            {
                "open_time": 1776505200000,
                "open": 100080.0,
                "close": 100150.0,
            },
        ]
        ws_trades = [
            {"price": 100100.0, "timestamp": 100.0},
            {"price": 100150.0, "timestamp": 109.0},
        ]

        features = compute_market_window_features(
            market=self.market,
            btc_price=100150.0,
            fair_value_yes=0.61,
            binance_5m=binance_5m,
            ws_trades=ws_trades,
        )

        self.assertAlmostEqual(features["window_delta_pct"], 0.0015)
        self.assertAlmostEqual(features["last_10s_velocity_bps"], 4.995004995004995)
        self.assertAlmostEqual(features["oracle_implied_prob"], 0.61)

    def test_fade_strategy_blocks_when_spot_delta_confirms_ofi(self):
        from core.decision_engine import explain_choose_side

        binance_1m = {"close": 100150.0}
        binance_5m = [
            {
                "open_time": 1776504600000,
                "open": 100000.0,
                "close": 100150.0,
            }
        ]
        ws_trades = [
            {"p": 100120.0, "q": 5.0, "m": False, "ts": 100.0},
            {"p": 100150.0, "q": 5.0, "m": False, "ts": 109.0},
        ]

        with patch("core.decision_engine.seconds_to_market_end", return_value=300.0), patch(
            "core.decision_engine.get_fair_value", return_value=0.62
        ), patch("core.execution_engine.calculate_committed_edge", side_effect=[0.09, -0.02]):
            decision = explain_choose_side(
                self.market,
                self.yes_window,
                self.up_window,
                self.down_window,
                observed_up=0.42,
                observed_down=0.58,
                binance_1m=binance_1m,
                binance_5m=binance_5m,
                ws_trades=ws_trades,
                poly_ob_up=self.poly_ob_up,
                poly_ob_down=self.poly_ob_down,
            )

        self.assertFalse(decision["ok"])
        self.assertIn("spot_delta_confirms_ofi", decision["reason"])

    def test_momentum_t60_candidate_is_created(self):
        from core.decision_engine import explain_choose_side

        binance_1m = {"close": 100150.0}
        binance_5m = [
            {
                "open_time": 1776504600000,
                "open": 100000.0,
                "close": 100150.0,
            }
        ]
        ws_trades = [
            {"p": 100120.0, "q": 5.0, "m": False, "ts": 100.0},
            {"p": 100150.0, "q": 5.0, "m": False, "ts": 109.0},
        ]

        with patch("core.decision_engine.seconds_to_market_end", return_value=55.0), patch(
            "core.decision_engine.get_fair_value", return_value=0.63
        ), patch("core.execution_engine.calculate_committed_edge", side_effect=[0.00, -0.03]):
            decision = explain_choose_side(
                self.market,
                self.yes_window,
                self.up_window,
                self.down_window,
                observed_up=0.42,
                observed_down=0.58,
                binance_1m=binance_1m,
                binance_5m=binance_5m,
                ws_trades=ws_trades,
                poly_ob_up=self.poly_ob_up,
                poly_ob_down=self.poly_ob_down,
            )

        ranked = decision.get("ranked_candidates", [])
        self.assertTrue(
            any(c.get("strategy_name") == "model-follow_momentum_t60" for c in ranked)
        )

    def test_fee_aware_breakeven_blocks_weak_entry(self):
        from core.runner import summarize_entry_edge
        from core.config import SETTINGS

        original_fee_buffer = getattr(SETTINGS, "fee_buffer", 0.02)
        SETTINGS.fee_buffer = 0.02
        try:
            edge = summarize_entry_edge(
                win_rate=0.58,
                entry_price=0.56,
                secs_left=60.0,
                history_count=30,
                fee_rate=0.0156,
                assume_maker=False,
            )
        finally:
            SETTINGS.fee_buffer = original_fee_buffer

        self.assertFalse(edge["ok"])
        self.assertEqual(edge["blocked_reason"], "fee-aware-breakeven")


if __name__ == "__main__":
    unittest.main()
