import unittest
try:
    from core.strategies.base import StrategyResult
except ImportError:
    StrategyResult = None

class TestStrategyBase(unittest.TestCase):
    def test_strategy_result_import(self):
        """Verify StrategyResult can be imported."""
        self.assertIsNotNone(StrategyResult, "StrategyResult not found in core.strategies.base")

    def test_strategy_result_instantiation(self):
        """Verify StrategyResult can be instantiated with required fields."""
        if StrategyResult is None:
            self.fail("StrategyResult not imported")
        result = StrategyResult(
            strategy_name="test_strat",
            side="UP",
            trigger_reason="test_reason",
            entry_price=0.5,
            signal_score=0.6,
            confidence=0.8,
            required_edge=0.05,
            raw_edge=0.1
        )
        self.assertEqual(result.strategy_name, "test_strat")
        self.assertEqual(result.side, "UP")
        self.assertEqual(result.trigger_reason, "test_reason")
        self.assertEqual(result.entry_price, 0.5)
        self.assertEqual(result.signal_score, 0.6)
        self.assertEqual(result.confidence, 0.8)
        self.assertEqual(result.required_edge, 0.05)
        self.assertEqual(result.raw_edge, 0.1)
        self.assertEqual(result.execution_preference, "hybrid")  # default
        self.assertEqual(result.metadata, {})  # default

    def test_strategy_result_with_optional_fields(self):
        """Verify StrategyResult can be instantiated with optional fields."""
        if StrategyResult is None:
            self.fail("StrategyResult not imported")
        result = StrategyResult(
            strategy_name="test_strat",
            side="DOWN",
            trigger_reason="test_reason",
            entry_price=0.4,
            signal_score=0.3,
            confidence=0.7,
            required_edge=0.02,
            raw_edge=-0.1,
            execution_preference="taker",
            metadata={"key": "value"}
        )
        self.assertEqual(result.execution_preference, "taker")
        self.assertEqual(result.metadata, {"key": "value"})

if __name__ == "__main__":
    unittest.main()
