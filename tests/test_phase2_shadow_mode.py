import csv
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


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


class DummyScoreboard:
    def get_strategy_score(self, _strategy_name):
        return 0.5

    def get_strategy_trade_count(self, _strategy_name):
        return 0

    def get_strategy_decisive_trade_count(self, _strategy_name):
        return 0


class TestShadowMode(unittest.TestCase):
    def test_select_ranked_entry_candidate_can_ignore_network_gate(self):
        from core.runner import select_ranked_entry_candidate

        model_decision = {
            "ok": True,
            "slug": "btc-updown-15m-test",
            "regime": "mid",
            "ranked_candidates": [
                {
                    "side": "DOWN",
                    "strategy_name": "model-fade_retail_fomo",
                    "entry_price": 0.40,
                    "canonical_entry_price": 0.40,
                    "signal_probability": 0.72,
                    "probability_source": "fair_value_model",
                    "token_id": "token-down",
                }
            ],
        }

        with patch("core.runner.SETTINGS.vpn_safe_mode", True), patch(
            "core.runner.LATENCY_MONITOR.is_blocked",
            return_value=(True, "NetworkTier=BLOCKED (rtt=812ms > 600)"),
        ), patch("core.runner.BINANCE_WS.get_last_update_age", return_value=0.1):
            candidate, notes = select_ranked_entry_candidate(
                model_decision,
                ws_velocity=0.0,
                current_ws_velocity=0.0,
                secs_left=120.0,
                scoreboard=DummyScoreboard(),
            )
            self.assertIsNone(candidate)
            self.assertTrue(any("VPN_LATENCY_BLOCK" in note for note in notes))

            candidate, notes = select_ranked_entry_candidate(
                model_decision,
                ws_velocity=0.0,
                current_ws_velocity=0.0,
                secs_left=120.0,
                scoreboard=DummyScoreboard(),
                ignore_network_gate=True,
            )
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["strategy_name"], "model-fade_retail_fomo")
            self.assertEqual(notes, [])

    def test_append_shadow_csv_row_writes_header_and_row(self):
        from core.journal import append_shadow_csv_row

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "shadow_journal.csv"
            row = append_shadow_csv_row(
                {
                    "clob_ts": "2026-04-19T00:00:00Z",
                    "local_ts": "2026-04-19T00:00:01Z",
                    "market_slug": "btc-updown-15m-test",
                    "side": "DOWN",
                    "strategy_name": "model-fade_retail_fomo",
                    "entry_price": 0.385,
                    "model_probability": 0.432,
                    "effective_probability": 0.435,
                    "raw_edge": 0.045,
                    "required_edge": 0.037,
                    "network_block_reason": "VPN_LATENCY_BLOCK(NetworkTier=BLOCKED)",
                },
                path=csv_path,
            )

            self.assertEqual(row["strategy_name"], "model-fade_retail_fomo")
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["clob_ts"], "2026-04-19T00:00:00Z")
            self.assertEqual(rows[0]["network_block_reason"], "VPN_LATENCY_BLOCK(NetworkTier=BLOCKED)")


if __name__ == "__main__":
    unittest.main()
