from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from core.config import SETTINGS
from core.indicators import calc_zlsma, calc_chandelier_exit, compute_buy_sell_pressure
from core.strategies.ws_order_flow import get_ofi_signal
from core.strategies.ws_flash_snipe import get_flash_snipe_signal
from core.strategies import mean_reversion
from core.latency_monitor import LATENCY_MONITOR


from core.strategies.base import StrategyResult
from core.fair_value_model import get_fair_value
from core.microstructure import calculate_ofi


def _sf(x: Any) -> Optional[float]:
    try:
        if x in (None, ""):
            return None
        return float(x)
    except Exception:
        return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _parse_listish(x: Any):
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        import json

        try:
            y = json.loads(x)
            return y if isinstance(y, list) else [x]
        except Exception:
            return [x]
    return []


def get_outcome_prices(market: dict) -> dict:
    outcomes = _parse_listish(market.get("outcomes"))
    prices = _parse_listish(market.get("outcomePrices"))
    result = {}
    for i, outcome in enumerate(outcomes):
        result[str(outcome).strip().lower()] = (
            _sf(prices[i]) if i < len(prices) else None
        )
    return result


def _extract_strike_price(question: str) -> float | None:
    if not question:
        return None
    import re

    match = re.search(r"\$([\d,]+(\.\d+)?)", question)
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except Exception:
            return None
    return None


def seconds_to_market_end(market: dict) -> Optional[float]:
    end = str(market.get("endDate") or market.get("end_date_iso") or "")
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


def check_arbitrage(up_price: Optional[float], down_price: Optional[float]) -> bool:
    if not SETTINGS.enable_arbitrage:
        return False
    if up_price and down_price:
        if (up_price + down_price) <= SETTINGS.arbitrage_max_cost:
            return True
    return False


def _check_imbalance(ob: dict) -> float:
    bids = ob.get("bids_volume", 0.0)
    asks = ob.get("asks_volume", 0.0)
    if bids + asks == 0:
        return 0.5
    return bids / (bids + asks)


def _has_momentum(side: str, up_window: deque, down_window: deque, min_move: float | None = None) -> bool:
    if min_move is None:
        min_move = SETTINGS.momentum_min_move
    ticks = max(2, SETTINGS.momentum_ticks)
    target = list(up_window if side == "UP" else down_window)
    if len(target) < ticks:
        return True
    recent = target[-ticks:]
    move = recent[-1] - recent[0]
    return move >= min_move


def _confidence_from_signal(strength: float, trigger: float, ceiling: float) -> float:
    if ceiling <= trigger:
        return 1.0 if strength >= trigger else 0.0
    return _clamp((strength - trigger) / max(ceiling - trigger, 1e-9), 0.0, 1.0)


def _probability_from_confidence(
    confidence: float, *, floor: float, ceiling: float
) -> float:
    confidence = _clamp(confidence, 0.0, 1.0)
    return floor + (ceiling - floor) * confidence


def _build_candidate(
    base_result: dict,
    *,
    side: str,
    strategy_key: str,
    entry_price: float,
    signal_score: float,
    signal_confidence: float,
    extras: Optional[dict] = None,
) -> dict:
    result = base_result.copy()
    result.update(
        {
            "ok": True,
            "side": side,
            "reason": f"model-{strategy_key}",
            "strategy_name": f"model-{strategy_key}",
            "entry_price": float(entry_price),
            "canonical_entry_price": float(entry_price),
            "signal_score": _clamp(float(signal_score), 0.01, 1.0),
            "signal_confidence": _clamp(float(signal_confidence), 0.0, 1.0),
            "metadata": extras or {},
        }
    )
    # Edge is now purely heuristic: signal_score - entry_price
    # For production 15m, strategies should ideally provide a better edge estimate.
    result["model_edge"] = result["signal_score"] - float(entry_price)
    if extras:
        result.update(extras)
    return result


def _rank_candidates(candidates: dict[str, Any]) -> list[Any]:
    if not candidates:
        return []
    
    def get_sort_key(c):
        if isinstance(c, dict):
            return (
                c.get("model_edge", float("-inf")),
                c.get("signal_score", 0.0),
                c.get("signal_confidence", 0.0),
            )
        # StrategyResult
        return (
            getattr(c, "raw_edge", float("-inf")),
            getattr(c, "signal_score", 0.0),
            getattr(c, "confidence", 0.0),
        )

    return sorted(candidates.values(), key=get_sort_key, reverse=True)


def _select_best_candidate(candidates: dict[str, Any], base_result: dict) -> Optional[dict]:
    if not candidates:
        return None
    ranked_raw = _rank_candidates(candidates)
    
    def to_dict(c: Any) -> dict:
        if isinstance(c, dict):
            return c.copy()
        # StrategyResult conversion
        d = base_result.copy()
        d.update({
            "ok": True,
            "side": c.side,
            "reason": c.trigger_reason,
            "strategy_name": c.strategy_name,
            "entry_price": float(c.entry_price),
            "canonical_entry_price": float(c.entry_price),
            "signal_score": _clamp(float(c.signal_score), 0.01, 1.0),
            "signal_confidence": _clamp(float(c.confidence), 0.0, 1.0),
            "model_edge": float(c.raw_edge),
            "metadata": c.metadata if hasattr(c, "metadata") else {},
        })
        if hasattr(c, "metadata") and c.metadata:
            d.update(c.metadata)
        return d

    best = to_dict(ranked_raw[0])
    best["candidate_count"] = len(ranked_raw)
    best["ranked_candidates"] = [to_dict(c) for c in ranked_raw]
    return best


from core.ai_advisor import AI_ADVISOR

def _get_time_regime(secs_left: float) -> str:
    elapsed = SETTINGS.market_duration_sec - secs_left
    if elapsed <= SETTINGS.regime_opening_end_sec:
        return "opening"
    if elapsed <= SETTINGS.regime_mid_end_sec:
        return "mid"
    return "late"

def explain_choose_side(
    market: dict,
    yes_window: deque,
    up_window: Optional[deque] = None,
    down_window: Optional[deque] = None,
    observed_up: Optional[float] = None,
    observed_down: Optional[float] = None,
    binance_1m: Optional[dict] = None,
    binance_5m: Optional[list[dict]] = None,
    ws_bba: Optional[dict] = None,
    ws_trades: Optional[list[dict]] = None,
    poly_ob_up: Optional[dict] = None,
    poly_ob_down: Optional[dict] = None,
) -> dict:
    from core.execution_engine import calculate_committed_edge, get_vwap_from_ladder

    # 1. 數據準備與基礎過濾
    secs_left = seconds_to_market_end(market)
    base_result = {
        "ok": False, "side": None, "reason": "no_valid_signals",
        "market_slug": market.get("slug"), "secs_left": secs_left,
    }

    if binance_1m is None or secs_left is None or poly_ob_up is None or poly_ob_down is None:
        base_result["reason"] = "missing_market_data"
        return base_result

    # 2. 公平價值估算 (BS Model)
    strike_price = market.get("strike_price") or _extract_strike_price(market.get("question", ""))
    if strike_price is None or binance_1m is None:
        base_result["reason"] = "missing_valuation_inputs"
        return base_result

    price_history = [float(k.get('c', 0)) for k in (binance_5m or [])]
    btc_price = float(binance_1m.get("c", 0))
    fv_yes = get_fair_value(
        btc_price, 
        strike_price, 
        secs_left, 
        price_history=price_history,
        ws_bba=ws_bba
    )

    # 3. Sniper 核心過濾：只交易極端區域
    up_price = float(observed_up or 0.5)
    
    # 必須不是中性區 (0.45 - 0.55 block)
    neutral_width = float(getattr(SETTINGS, "vpn_neutral_zone_width", 0.05) or 0.05)
    if abs(up_price - 0.5) <= neutral_width:
        base_result["reason"] = "neutral_zone_no_trade"
        return base_result

    # 4. 波動率閘門 (Volatility Gate)
    if binance_5m:
        recent_prices = [float(k.get('c', btc_price)) for k in binance_5m[-5:]]
        if recent_prices:
            price_range_bps = (max(recent_prices) - min(recent_prices)) / max(min(recent_prices), 1e-9) * 10000.0
            min_vol_bps = float(getattr(SETTINGS, "min_volatility_gate_bps", 15.0) or 15.0)
            if price_range_bps < min_vol_bps:
                base_result["reason"] = f"low_volatility_gate (range={price_range_bps:.1f}bps < {min_vol_bps}bps)"
                return base_result

    # 5. 承諾邊際 (Committed Edge) 與 延遲補償
    order_size = float(getattr(SETTINGS, "min_live_order_usd", 1.0))
    # We enforce assume_maker=True for the VPN profile
    edge_up = calculate_committed_edge(fv_yes, poly_ob_up, poly_ob_down, order_size, "UP", assume_maker=SETTINGS.vpn_maker_only)
    edge_down = calculate_committed_edge(fv_yes, poly_ob_up, poly_ob_down, order_size, "DOWN", assume_maker=SETTINGS.vpn_maker_only)
    
    candidates = {}
    threshold = float(SETTINGS.min_sniper_edge_bps) / 10000.0

    if edge_up >= threshold:
        reason = "fade_retail_panic" if up_price < SETTINGS.sniper_extreme_lower else "fade_retail_fomo"
        candidates["sniper_fade_up"] = _build_candidate(
            base_result, side="UP", strategy_key=reason,
            entry_price=get_vwap_from_ladder(poly_ob_up.get('asks', []), order_size),
            signal_score=fv_yes, signal_confidence=1.0,
            extras={"sniper_edge": edge_up, "behavioral_alpha": reason, "latency_buffer": SETTINGS.latency_buffer_usd}
        )

    if edge_down >= threshold:
        reason = "fade_retail_panic" if (1.0 - up_price) < SETTINGS.sniper_extreme_lower else "fade_retail_fomo"
        candidates["sniper_fade_down"] = _build_candidate(
            base_result, side="DOWN", strategy_key=reason,
            entry_price=get_vwap_from_ladder(poly_ob_down.get('asks', []), order_size),
            signal_score=1.0 - fv_yes, signal_confidence=1.0,
            extras={"sniper_edge": edge_down, "behavioral_alpha": reason, "latency_buffer": SETTINGS.latency_buffer_usd}
        )

    if not candidates:
        base_result["reason"] = "edge_below_sniper_threshold"
        return base_result

    return _select_best_candidate(candidates, base_result)


def choose_side(
    market: dict,
    yes_window: deque,
    up_window: Optional[deque] = None,
    down_window: Optional[deque] = None,
    observed_up: Optional[float] = None,
    observed_down: Optional[float] = None,
    binance_1m: Optional[dict] = None,
    binance_5m: Optional[list[dict]] = None,
    ws_bba: Optional[dict] = None,
    ws_trades: Optional[list[dict]] = None,
    poly_ob_up: Optional[dict] = None,
    poly_ob_down: Optional[dict] = None,
) -> Optional[str]:
    decision = explain_choose_side(
        market,
        yes_window,
        up_window,
        down_window,
        observed_up,
        observed_down,
        binance_1m,
        binance_5m,
        ws_bba,
        ws_trades,
        poly_ob_up,
        poly_ob_down,
    )
    if not decision.get("ok"):
        return None
    return decision.get("side")
