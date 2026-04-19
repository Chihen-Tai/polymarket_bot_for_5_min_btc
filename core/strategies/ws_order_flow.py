from __future__ import annotations
from typing import Any, Optional
from core.strategies.base import StrategyResult
from core.indicators import compute_buy_sell_pressure

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))

def _confidence_from_signal(strength: float, trigger: float, ceiling: float) -> float:
    if ceiling <= trigger:
        return 1.0 if strength >= trigger else 0.0
    return _clamp((strength - trigger) / max(ceiling - trigger, 1e-9), 0.0, 1.0)

def _probability_from_confidence(confidence: float, *, floor: float, ceiling: float) -> float:
    confidence = _clamp(confidence, 0.0, 1.0)
    return floor + (ceiling - floor) * confidence

def _check_imbalance(ob: dict) -> float:
    if not ob:
        return 0.5
    bids = ob.get("bids_volume", 0.0)
    asks = ob.get("asks_volume", 0.0)
    if bids + asks == 0:
        return 0.5
    return bids / (bids + asks)

def get_ofi_signal(
    ws_trades: list[dict],
    up_price: float,
    down_price: float,
    poly_ob_up: Optional[dict],
    poly_ob_down: Optional[dict],
    settings: Any
) -> list[StrategyResult]:
    results = []
    if not ws_trades:
        return results

    buy_vol, sell_vol = compute_buy_sell_pressure(ws_trades)
    total_vol = buy_vol + sell_vol
    if total_vol <= 0:
        return results

    ofi_ratio = buy_vol / total_vol
    ofi_threshold = getattr(settings, "ofi_bypass_threshold", 0.73)
    ofi_confidence = _confidence_from_signal(
        abs(ofi_ratio - 0.5),
        max(0.0, ofi_threshold - 0.5),
        0.5,
    )
    
    # Polymarket OB cross-confirmation
    poly_up_imbalance = _check_imbalance(poly_ob_up) if poly_ob_up else 0.5
    poly_down_imbalance = _check_imbalance(poly_ob_down) if poly_ob_down else 0.5
    
    # Required edge baseline before dynamic fee evaluation
    required_edge = 0.05
    
    # True edge of order flow is usually small (0.5% - 2%)
    expected_edge_hint = 0.005 + (0.015 * ofi_confidence)

    # Check UP signal
    if ofi_ratio > ofi_threshold:
        if poly_up_imbalance >= 0.55:
            adj_prob = min(0.99, float(up_price) + expected_edge_hint)
            results.append(StrategyResult(
                strategy_name="model-ws_order_flow_up",
                side="UP",
                trigger_reason="ofi_up",
                entry_price=float(up_price),
                signal_score=adj_prob,
                confidence=ofi_confidence,
                required_edge=required_edge,
                raw_edge=expected_edge_hint,
                metadata={
                    "ofi_ratio": ofi_ratio,
                    "poly_up_imbalance": poly_up_imbalance,
                    "buy_vol": buy_vol,
                    "sell_vol": sell_vol,
                    "expected_edge_hint": expected_edge_hint
                }
            ))

    # Check DOWN signal
    if ofi_ratio < (1.0 - ofi_threshold):
        if poly_down_imbalance >= 0.55:
            adj_prob = min(0.99, float(down_price) + expected_edge_hint)
            results.append(StrategyResult(
                strategy_name="model-ws_order_flow_down",
                side="DOWN",
                trigger_reason="ofi_down",
                entry_price=float(down_price),
                signal_score=adj_prob,
                confidence=ofi_confidence,
                required_edge=required_edge,
                raw_edge=expected_edge_hint,
                metadata={
                    "ofi_ratio": ofi_ratio,
                    "poly_down_imbalance": poly_down_imbalance,
                    "buy_vol": buy_vol,
                    "sell_vol": sell_vol,
                    "expected_edge_hint": expected_edge_hint
                }
            ))

    return results
