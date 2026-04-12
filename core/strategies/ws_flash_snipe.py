from __future__ import annotations
from typing import Any, Optional
from core.strategies.base import StrategyResult

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))

def _confidence_from_signal(strength: float, trigger: float, ceiling: float) -> float:
    if ceiling <= trigger:
        return 1.0 if strength >= trigger else 0.0
    return _clamp((strength - trigger) / max(ceiling - trigger, 1e-9), 0.0, 1.0)

def _probability_from_confidence(confidence: float, *, floor: float, ceiling: float) -> float:
    confidence = _clamp(confidence, 0.0, 1.0)
    return floor + (ceiling - floor) * confidence

def get_flash_snipe_signal(
    vel: float,
    up_price: float,
    down_price: float,
    snipe_valid_up: bool,
    snipe_valid_down: bool,
    settings: Any
) -> list[StrategyResult]:
    results = []
    
    flash_threshold = float(getattr(settings, "ws_flash_snipe_threshold", 0.001))
    if flash_threshold <= 0:
        return results

    # signal_strength reflects how many multiples of threshold we've seen
    signal_strength = min(3.0, abs(vel) / flash_threshold)
    flash_confidence = _confidence_from_signal(abs(vel), flash_threshold, flash_threshold * 2.0)
    
    # We no longer manufacture an arbitrary 0.88 probability. 
    # The true edge of a flash snipe comes from catching a lag before PM updates.
    # A typical PM lag might grant 1-3% edge if the book doesn't evaporate.
    expected_lag_edge_hint = 0.01 + (0.02 * flash_confidence) # 1% to 3% edge
    
    required_edge = 0.05 # Baseline required edge before dynamic fee evaluation

    if vel > flash_threshold and snipe_valid_up:
        # model_probability = entry_price + expected_lag_edge_hint
        # This keeps the strategy bounded to realistic execution realities, not fake high probabilities.
        adj_prob = min(0.99, float(up_price) + expected_lag_edge_hint)
        results.append(StrategyResult(
            strategy_name="model-ws_flash_snipe_up",
            side="UP",
            trigger_reason="flash_snipe_up",
            entry_price=float(up_price),
            model_probability=adj_prob,
            confidence=flash_confidence,
            required_edge=required_edge,
            raw_edge=expected_lag_edge_hint,
            metadata={
                "velocity_3s": vel,
                "signal_strength": signal_strength,
                "expected_lag_edge_hint": expected_lag_edge_hint,
                "urgency_class": "high" if signal_strength > 2.0 else "medium"
            }
        ))
    elif vel < -flash_threshold and snipe_valid_down:
        adj_prob = min(0.99, float(down_price) + expected_lag_edge_hint)
        results.append(StrategyResult(
            strategy_name="model-ws_flash_snipe_down",
            side="DOWN",
            trigger_reason="flash_snipe_down",
            entry_price=float(down_price),
            model_probability=adj_prob,
            confidence=flash_confidence,
            required_edge=required_edge,
            raw_edge=expected_lag_edge_hint,
            metadata={
                "velocity_3s": vel,
                "signal_strength": signal_strength,
                "expected_lag_edge_hint": expected_lag_edge_hint,
                "urgency_class": "high" if signal_strength > 2.0 else "medium"
            }
        ))
        
    return results
