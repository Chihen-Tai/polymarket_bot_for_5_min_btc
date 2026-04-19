import json
import os
import sys
import unittest
from types import ModuleType

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if "websocket" not in sys.modules:
    websocket_stub = ModuleType("websocket")
    websocket_stub.WebSocketApp = object
    sys.modules["websocket"] = websocket_stub

import core.ws_binance as ws_mod
from core.ws_binance import BinanceWebSocket


class TestPhase2WebsocketMetrics(unittest.TestCase):
    def test_event_latency_and_bba_age(self):
        ws = BinanceWebSocket("btcusdt")
        ws.running = False
        ws.bba = {"b": 0.0, "B": 0.0, "a": 0.0, "A": 0.0, "ts": 0.0, "u": 0}
        ws.bba_history.clear()
        ws.recent_prices.clear()

        original_time = ws_mod.time.time
        try:
            ws_mod.time.time = lambda: 100.250
            ws._on_message(
                None,
                json.dumps(
                    {
                        "stream": "btcusdt@bookTicker",
                        "data": {
                            "u": 42,
                            "b": "100000.0",
                            "B": "1.5",
                            "a": "100001.0",
                            "A": "2.5",
                            "E": 100000,
                        },
                    }
                ),
            )
            self.assertAlmostEqual(ws.get_last_event_latency_ms(), 250.0)
            self.assertAlmostEqual(ws.get_bba_age_ms(), 0.0)
        finally:
            ws_mod.time.time = original_time


if __name__ == "__main__":
    unittest.main()
