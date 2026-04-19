import sys
import unittest
from types import ModuleType, SimpleNamespace


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


class TestCycleMetrics(unittest.TestCase):
    def test_format_cycle_metrics_line_contains_required_fields(self):
        from core.runner import format_cycle_metrics_line

        line = format_cycle_metrics_line(
            market_slug="btc-updown-15m-test",
            rtt_http_ms=812.3,
            rtt_ws_ms=94.7,
            ws_age_ms=120.0,
            clob_skew_ms=-37.0,
            binance_ws_age_ms=118.0,
            chainlink_oracle_age_s=None,
        )

        self.assertIn("rtt_http_ms=812.3", line)
        self.assertIn("rtt_ws_ms=94.7", line)
        self.assertIn("ws_age_ms=120.0", line)
        self.assertIn("clob_skew_ms=-37.0", line)
        self.assertIn("binance_ws_age_ms=118.0", line)
        self.assertIn("chainlink_oracle_age_s=na", line)


if __name__ == "__main__":
    unittest.main()
