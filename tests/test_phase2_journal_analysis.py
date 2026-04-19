import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestPhase2JournalAnalysis(unittest.TestCase):
    def test_build_trade_pairs_handles_orphan_residual_rows(self):
        from scripts.journal_analysis import build_trade_pairs

        events = [
            {
                "kind": "exit",
                "event_id": "exit-1",
                "position_id": "pos-1",
                "token_id": "token-1",
                "slug": "btc-updown-15m-1776504600",
                "side": "UP",
                "ts": "2026-04-18T17:30:03",
                "closed_shares": 1.0,
                "actual_exit_value_usd": 0.45,
                "actual_exit_value_source": "cash_balance_delta",
                "observed_exit_value_usd": 0.45,
                "exit_execution_style": "maker",
                "reason": "maker-timeout-no-fallback",
            }
        ]

        rows = build_trade_pairs(events)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "residual")
        self.assertIsNone(rows[0].entry_secs_left)


if __name__ == "__main__":
    unittest.main()
