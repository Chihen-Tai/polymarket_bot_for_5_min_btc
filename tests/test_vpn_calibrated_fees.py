import pytest
from core.execution_engine import FEE_MODEL, calculate_committed_edge
from core.config import SETTINGS

def test_fee_model_maker():
    # Polymarket maker rebate is 0.0 or slightly negative.
    FEE_MODEL.maker_rate = 0.0
    fee = FEE_MODEL.calculate_maker_fee(0.8, 100.0)
    assert fee == 0.0

def test_fee_model_taker():
    FEE_MODEL.taker_rate = 0.02 # 2% taker
    fee = FEE_MODEL.calculate_taker_fee(0.8, 100.0)
    # p * (1-p) * 0.02 * amount
    # 0.8 * 0.2 * 0.02 * 100 = 0.16 * 0.02 * 100 = 0.32
    assert abs(fee - 0.32) < 1e-4

def test_committed_edge():
    # SETTINGS state
    SETTINGS.latency_buffer_usd = 0.02
    
    ob_up = {'asks': [{'price': 0.8, 'size': 1000}]}
    ob_down = {'asks': [{'price': 0.2, 'size': 1000}]}
    
    # Fair value is 0.9. Ask is 0.8. Maker fee is 0.
    # Edge = 0.9 - 0.8 - 0.0 - latency_buffer(0.02) = 0.08
    edge = calculate_committed_edge(0.9, ob_up, ob_down, 10.0, "UP", assume_maker=True)
    assert abs(edge - 0.08) < 1e-4
