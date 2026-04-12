import unittest
import sys
import os
from unittest.mock import MagicMock, patch
from collections import deque

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import SETTINGS
from core.decision_engine import explain_choose_side
from core.trade_manager import decide_exit
from core.latency_monitor import LatencyMonitor

class TestVPNSafeMode(unittest.TestCase):
    def setUp(self):
        # Reset SETTINGS to VPN defaults
        SETTINGS.vpn_safe_mode = True
        SETTINGS.vpn_entry_min_secs_left = 150.0
        SETTINGS.vpn_min_executable_edge = 0.06
        SETTINGS.vpn_expiry_first = True
        SETTINGS.stop_loss_pct = 0.15
        SETTINGS.max_hold_seconds = 180
        
        self.market = {
            "slug": "btc-updown-5m-1775983800",
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["0.50", "0.50"],
            "endDate": "2026-12-31T23:59:59Z",
            "question": "Will BTC be above $100,000?"
        }
        self.window = deque([0.5] * 20, maxlen=20)

    @patch("core.decision_engine.seconds_to_market_end")
    def test_vpn_window_block(self, mock_secs):
        # Case: secs_left < 150 should be blocked
        mock_secs.return_value = 140.0
        res = explain_choose_side(
            self.market, self.window, self.window, self.window,
            observed_up=0.50, observed_down=0.50
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no_valid_signals")

    @patch("core.decision_engine.seconds_to_market_end")
    @patch("core.decision_engine.LATENCY_MONITOR")
    def test_vpn_edge_floor(self, mock_lat, mock_secs):
        mock_secs.return_value = 200.0
        mock_lat.get_edge_penalty.return_value = 0.0
        # Case: edge 0.04 < floor 0.06 should be blocked
        with patch("core.decision_engine.mean_reversion") as mock_mr:
            from core.strategies.base import StrategyResult
            mock_mr.run.return_value = StrategyResult(
                strategy_name="mean_reversion", side="UP", trigger_reason="test",
                entry_price=0.50, model_probability=0.54, # edge 0.04
                confidence=1.0, required_edge=0.02, raw_edge=0.04
            )
            res = explain_choose_side(
                self.market, self.window, self.window, self.window,
                observed_up=0.50, observed_down=0.50
            )
            self.assertFalse(res["ok"])
            self.assertIn("flow_too_weak", res["reason"])

    def test_vpn_expiry_first_exit(self):
        # Case: Normal profit +10% should NOT exit in VPN expiry-first mode
        res = decide_exit(
            pnl_pct=0.10, profit_pnl_pct=0.10, hold_sec=60.0, secs_left=100.0
        )
        self.assertFalse(res.should_close)

        # Case: Hard stop loss -20% SHOULD still exit
        res = decide_exit(
            pnl_pct=-0.20, profit_pnl_pct=-0.20, hold_sec=60.0, secs_left=100.0
        )
        self.assertTrue(res.should_close)
        self.assertEqual(res.reason, "hard-stop-loss")

        # Case: Deadline exit at 30s SHOULD exit
        res = decide_exit(
            pnl_pct=0.05, profit_pnl_pct=0.05, hold_sec=120.0, secs_left=30.0
        )
        self.assertTrue(res.should_close)
        self.assertIn("deadline", res.reason)

    def test_latency_monitor_block(self):
        monitor = LatencyMonitor(history_size=10)
        # Record some slow E2E times
        for _ in range(5):
            monitor.record_decision_to_order(300.0) # > 250ms p50 limit
        
        is_blocked, reason = monitor.is_blocked()
        self.assertTrue(is_blocked)
        self.assertIn("E2E_P50_TOO_HIGH", reason)

if __name__ == "__main__":
    unittest.main()
