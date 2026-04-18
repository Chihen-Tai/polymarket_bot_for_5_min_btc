import unittest
from core.strategies.ws_order_flow import get_ofi_signal
from core.strategies.ws_flash_snipe import get_flash_snipe_signal
from core.strategies.base import StrategyResult

class MockSettings:
    def __init__(self):
        self.ofi_bypass_threshold = 0.73
        self.ws_flash_snipe_threshold = 0.0003

class TestStrategiesMigration(unittest.TestCase):
    def setUp(self):
        self.settings = MockSettings()

    def test_ws_order_flow_up(self):
        # buy_vol = 100 * 0.8 = 80, sell_vol = 100 * 0.2 = 20
        # ofi_ratio = 80 / 100 = 0.8 > 0.73
        ws_trades = [
            {"p": 1.0, "q": 80.0, "m": False}, # Market buy
            {"p": 1.0, "q": 20.0, "m": True},  # Market sell
        ]
        poly_ob_up = {"bids_volume": 60.0, "asks_volume": 40.0} # imbalance = 0.6 >= 0.55
        poly_ob_down = {"bids_volume": 40.0, "asks_volume": 60.0}

        results = get_ofi_signal(
            ws_trades, 
            up_price=0.5, 
            down_price=0.5, 
            poly_ob_up=poly_ob_up, 
            poly_ob_down=poly_ob_down, 
            settings=self.settings
        )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertIsInstance(res, StrategyResult)
        self.assertEqual(res.strategy_name, "model-ws_order_flow_up")
        self.assertEqual(res.side, "UP")
        self.assertTrue(res.signal_score > 0.5)
        self.assertTrue(res.confidence > 0)
        self.assertEqual(res.metadata["ofi_ratio"], 0.8)

    def test_ws_order_flow_down(self):
        # buy_vol = 20, sell_vol = 80
        # ofi_ratio = 20 / 100 = 0.2 < (1 - 0.73) = 0.27
        ws_trades = [
            {"p": 1.0, "q": 20.0, "m": False},
            {"p": 1.0, "q": 80.0, "m": True},
        ]
        poly_ob_up = {"bids_volume": 40.0, "asks_volume": 60.0}
        poly_ob_down = {"bids_volume": 60.0, "asks_volume": 40.0} # imbalance = 0.6 >= 0.55

        results = get_ofi_signal(
            ws_trades, 
            up_price=0.5, 
            down_price=0.5, 
            poly_ob_up=poly_ob_up, 
            poly_ob_down=poly_ob_down, 
            settings=self.settings
        )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertIsInstance(res, StrategyResult)
        self.assertEqual(res.strategy_name, "model-ws_order_flow_down")
        self.assertEqual(res.side, "DOWN")
        self.assertEqual(res.metadata["ofi_ratio"], 0.2)

    def test_ws_flash_snipe_up(self):
        vel = 0.0005 # > 0.0003
        results = get_flash_snipe_signal(
            vel, 
            up_price=0.5, 
            down_price=0.5, 
            snipe_valid_up=True, 
            snipe_valid_down=True, 
            settings=self.settings
        )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertIsInstance(res, StrategyResult)
        self.assertEqual(res.strategy_name, "model-ws_flash_snipe_up")
        self.assertEqual(res.side, "UP")
        self.assertEqual(res.metadata["velocity_3s"], 0.0005)

    def test_ws_flash_snipe_down(self):
        vel = -0.0005 # < -0.0003
        results = get_flash_snipe_signal(
            vel, 
            up_price=0.5, 
            down_price=0.5, 
            snipe_valid_up=True, 
            snipe_valid_down=True, 
            settings=self.settings
        )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertIsInstance(res, StrategyResult)
        self.assertEqual(res.strategy_name, "model-ws_flash_snipe_down")
        self.assertEqual(res.side, "DOWN")
        self.assertEqual(res.metadata["velocity_3s"], -0.0005)

if __name__ == "__main__":
    unittest.main()
