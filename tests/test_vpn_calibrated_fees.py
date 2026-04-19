from core.execution_engine import FEE_MODEL, PolymarketFeeModel, calculate_committed_edge
from core.config import SETTINGS


def test_fee_model_maker_always_zero():
    """Maker fee is 0 for crypto_fees_v2 (takerOnly=True)."""
    model = PolymarketFeeModel()
    assert model.calculate_maker_fee(0.8, 100.0) == 0.0
    assert model.calculate_maker_fee(0.5, 100.0) == 0.0
    assert model.calculate_maker_fee(0.2, 100.0) == 0.0


def test_fee_model_taker_correct_formula():
    """Fee = effective_rate * p * (1-p) * shares."""
    model = PolymarketFeeModel()
    model.rate = 0.072
    model.exponent = 1
    model.rebate_rate = 0.0  # no rebate

    # At p=0.5, size_usd=100: shares = 100/0.5 = 200
    # fee = 0.072 * 0.5 * 0.5 * 200 = 3.60
    fee = model.calculate_taker_fee(0.5, 100.0)
    assert abs(fee - 3.60) < 1e-4

    # At p=0.8, size_usd=100: shares = 100/0.8 = 125
    # fee = 0.072 * 0.8 * 0.2 * 125 = 1.44
    fee = model.calculate_taker_fee(0.8, 100.0)
    assert abs(fee - 1.44) < 1e-4

    # At p=0.2, size_usd=100: shares = 100/0.2 = 500
    # fee = 0.072 * 0.2 * 0.8 * 500 = 5.76
    fee = model.calculate_taker_fee(0.2, 100.0)
    assert abs(fee - 5.76) < 1e-4


def test_fee_model_with_rebate_does_not_reduce_taker_fee():
    """Maker rebates do not reduce the protocol taker fee."""
    model = PolymarketFeeModel()
    model.rate = 0.072
    model.rebate_rate = 0.2  # 20% rebate

    assert abs(model.effective_taker_rate_after_rebate - 0.072) < 1e-6

    # At p=0.5, size_usd=100: shares = 200
    # fee remains 0.072 * 0.5 * 0.5 * 200 = 3.60
    fee = model.calculate_taker_fee(0.5, 100.0)
    assert abs(fee - 3.60) < 1e-4


def test_fee_model_edge_prices():
    """Fee is 0 at boundary prices."""
    model = PolymarketFeeModel()
    assert model.calculate_taker_fee(0.0, 100.0) == 0.0
    assert model.calculate_taker_fee(1.0, 100.0) == 0.0
    assert model.calculate_taker_fee(-0.1, 100.0) == 0.0


def test_fee_model_fallback_rate():
    """Default fallback matches BTC 15m crypto taker fees."""
    model = PolymarketFeeModel()
    # Default rate = 0.072, no rebate
    fee = model.calculate_taker_fee(0.5, 100.0)
    fee_pct = fee / 100.0
    assert abs(fee_pct - 0.036) < 1e-9


def test_committed_edge_with_new_fee_model():
    """Edge calculation uses the new fee model correctly."""
    SETTINGS.latency_buffer_usd = 0.02

    # Reset FEE_MODEL to known state
    FEE_MODEL.rate = 0.072
    FEE_MODEL.exponent = 1
    FEE_MODEL.rebate_rate = 0.0

    ob_up = {'asks': [{'price': 0.8, 'size': 1000}]}
    ob_down = {'asks': [{'price': 0.2, 'size': 1000}]}

    # Fair value = 0.9, ask = 0.8, maker fee = 0
    # Edge = 0.9 - 0.8 - 0.0 - 0.02 = 0.08
    edge = calculate_committed_edge(0.9, ob_up, ob_down, 10.0, "UP", assume_maker=True)
    assert abs(edge - 0.08) < 1e-4


def test_no_time_multiplier():
    """The new fee model does NOT apply time-based multipliers."""
    model = PolymarketFeeModel()
    model.rate = 0.072
    model.rebate_rate = 0.0

    fee_10min = model.calculate_taker_fee(0.5, 100.0, secs_left=600.0)
    fee_1min = model.calculate_taker_fee(0.5, 100.0, secs_left=60.0)
    fee_10sec = model.calculate_taker_fee(0.5, 100.0, secs_left=10.0)

    # All should be identical -- no time escalation
    assert fee_10min == fee_1min == fee_10sec
