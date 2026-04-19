"""Phase 3 tests for fair value model improvements."""
import math
import pytest
from core.fair_value_model import calculate_realized_vol, calculate_binary_probability


class TestRealizedVol:
    def test_insufficient_data_returns_fallback(self):
        assert calculate_realized_vol([100.0], window=20) == 0.60

    def test_constant_prices_return_low_vol(self):
        prices = [100.0] * 100
        vol = calculate_realized_vol(prices, window=50)
        assert vol == 0.20  # hits floor — zero stdev

    def test_normal_btc_vol_below_60pct(self):
        """Simulate ~0.03% per-minute moves (normal BTC)."""
        import random
        random.seed(42)
        prices = [80000.0]
        for _ in range(43200):  # 30 days of 1m
            ret = random.gauss(0, 0.0003)  # ~0.03% per minute
            prices.append(prices[-1] * math.exp(ret))
        vol = calculate_realized_vol(prices, window=43200)
        # Should be ~40-55% annualized, well below the old 70% fallback
        assert 0.20 < vol < 0.60

    def test_uses_tail_of_history(self):
        """With window=10, only last 10 prices matter."""
        stable = [100.0] * 100
        volatile = [100.0, 110.0, 90.0, 115.0, 85.0, 120.0, 80.0, 105.0, 95.0, 100.0]
        combined = stable + volatile
        vol_full = calculate_realized_vol(combined, window=10)
        vol_tail = calculate_realized_vol(volatile, window=10)
        assert abs(vol_full - vol_tail) < 0.01


class TestBinaryProbability:
    def test_default_vol_is_50pct(self):
        """Default vol parameter should be 0.50 (was 0.60)."""
        import inspect
        sig = inspect.signature(calculate_binary_probability)
        default = sig.parameters["volatility_annual"].default
        assert default == 0.50

    def test_atm_near_half(self):
        prob = calculate_binary_probability(80000, 80000, 900, 0.50)
        assert 0.45 < prob < 0.55

    def test_deep_itm(self):
        prob = calculate_binary_probability(81000, 80000, 900, 0.50)
        assert prob > 0.6

    def test_expired_above(self):
        assert calculate_binary_probability(81000, 80000, 0) == 1.0

    def test_expired_below(self):
        assert calculate_binary_probability(79000, 80000, 0) == 0.0
