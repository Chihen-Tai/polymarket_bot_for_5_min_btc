import pytest
from core.ensemble_models.ensemble import ENSEMBLE
from core.fair_value_model import get_fair_value
from core.ensemble_models.microstructure import M2_MICROSTRUCTURE

def test_ensemble_aggregator_no_bba():
    # Base prob 0.5 without BBA should remain 0.5
    prob = ENSEMBLE.get_calibrated_fair_value(0.5, None)
    assert prob == 0.5

def test_ensemble_aggregator_bid_wall():
    # A massive bid wall should skew the imbalance positive
    ws_bba = {
        'b': [{'price': 60000, 'size': 10}],
        'a': [{'price': 60100, 'size': 0.1}]
    }
    
    # Imbalance = (10 - 0.1) / 10.1 = ~0.98. 
    # Max skew penalty is 0.05. Modification = 0.98 * 0.05 = ~0.049
    base_prob = 0.5
    prob = ENSEMBLE.get_calibrated_fair_value(base_prob, ws_bba)
    assert prob > base_prob
    assert abs(prob - 0.549) < 0.01

def test_ensemble_aggregator_ask_wall():
    # A massive ask wall should skew the imbalance negative
    ws_bba = {
        'b': [{'price': 60000, 'size': 1}],
        'a': [{'price': 60100, 'size': 50}]
    }
    
    # Decreases prob
    base_prob = 0.8
    prob = ENSEMBLE.get_calibrated_fair_value(base_prob, ws_bba)
    assert prob < base_prob
    assert prob > 0.7  # It shouldn't wreck the probability entirely

def test_get_fair_value_integration():
    # Test that the outer layer orchestrates correctly
    btc_price = 60000
    strike_price = 60000
    secs_left = 900
    
    ws_bba_neutral = {
        'b': [{'price': 60000, 'size': 1}],
        'a': [{'price': 60001, 'size': 1}]
    }
    
    prob1 = get_fair_value(btc_price, strike_price, secs_left, ws_bba=ws_bba_neutral)
    
    # Should be around 0.5 for ATM with neutral book
    assert abs(prob1 - 0.5) < 0.05
