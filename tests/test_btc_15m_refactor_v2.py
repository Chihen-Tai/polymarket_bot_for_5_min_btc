
import unittest
from unittest.mock import MagicMock, patch
from core.trade_manager import _decide_exit_15m, should_block_same_market_reentry
from core.latency_monitor import LatencyMonitor
from core.runner import should_allow_high_confidence_taker_fallback

class TestBTC15mRefactorV2(unittest.TestCase):
    def test_expiry_first_certainty_hold(self):
        """Verify that high-certainty winners hold to expiry instead of deadline-exit."""
        # Fair value 0.95, secs_left 5 -> Should hold
        res = _decide_exit_15m(pnl_pct=0.15, hold_sec=100, secs_left=5, fair_value=0.95, side="UP")
        self.assertFalse(res.should_close)
        self.assertEqual(res.reason, "expiry-first-certainty-hold")

        # Fair value 0.80, secs_left 5 -> Should deadline exit
        res = _decide_exit_15m(pnl_pct=0.15, hold_sec=100, secs_left=5, fair_value=0.80, side="UP")
        self.assertTrue(res.should_close)
        self.assertEqual(res.reason, "deadline-exit-win")

    def test_reentry_blocking_logic(self):
        """Verify that benign exits don't block reentry while losses do."""
        # Hard stop loss -> should block
        self.assertTrue(should_block_same_market_reentry("hard-stop-loss"))
        
        # Realized loss -> should block
        self.assertTrue(should_block_same_market_reentry("deadline-exit-win", realized_pnl_usd=-0.05))
        
        # Strategic take profit -> should NOT block
        self.assertFalse(should_block_same_market_reentry("strategic-take-profit", realized_pnl_usd=0.10))
        
        # Deadline exit win -> should NOT block
        self.assertFalse(should_block_same_market_reentry("deadline-exit-win", realized_pnl_usd=0.05))

    def test_high_confidence_taker_fallback(self):
        """Verify high-confidence taker fallback gating."""
        with patch('core.runner.SETTINGS') as mock_settings:
            mock_settings.high_confidence_taker_fallback_enabled = True
            mock_settings.high_confidence_edge_extra = 0.02
            
            # High edge (0.08 vs 0.04 required + 0.02 extra) -> True
            self.assertTrue(should_allow_high_confidence_taker_fallback(
                raw_edge=0.08, required_edge=0.04, market_secs_left=200, network_mode="normal"
            ))
            
            # Low edge (0.05 vs 0.04 required + 0.02 extra) -> False
            self.assertFalse(should_allow_high_confidence_taker_fallback(
                raw_edge=0.05, required_edge=0.04, market_secs_left=200, network_mode="normal"
            ))
            
            # High edge but close_only -> False
            self.assertFalse(should_allow_high_confidence_taker_fallback(
                raw_edge=0.08, required_edge=0.04, market_secs_left=200, network_mode="close_only"
            ))

    def test_graded_network_degradation(self):
        """Verify network modes based on latency/jitter."""
        monitor = LatencyMonitor()
        
        # No data -> normal
        self.assertEqual(monitor.get_network_mode(), "normal")
        
        # High latency (400ms) -> maker_only
        with patch('core.latency_monitor.SETTINGS') as mock_settings:
            mock_settings.vpn_safe_mode = True
            mock_settings.NETWORK_MAKER_ONLY_LATENCY_MS = 350.0
            mock_settings.NETWORK_CLOSE_ONLY_LATENCY_MS = 600.0
            mock_settings.NETWORK_MAKER_ONLY_JITTER_MS = 50.0
            mock_settings.NETWORK_CLOSE_ONLY_JITTER_MS = 120.0
            
            # Setup 400ms median
            monitor.add_rtt(400.0)
            monitor.add_rtt(400.0)
            monitor.add_rtt(400.0)
            self.assertEqual(monitor.get_network_mode(), "maker_only")
            
            # Severe latency (700ms) -> close_only
            monitor.add_rtt(700.0)
            monitor.add_rtt(700.0)
            self.assertEqual(monitor.get_network_mode(), "close_only")

if __name__ == '__main__':
    unittest.main()
