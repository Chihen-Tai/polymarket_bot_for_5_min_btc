from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from core.config import SETTINGS
from core.indicators import calc_zlsma, calc_chandelier_exit, compute_buy_sell_pressure


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
        result[str(outcome).strip().lower()] = _sf(prices[i]) if i < len(prices) else None
    return result


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


def mean_reversion_signal(yes_price: Optional[float], yes_window: deque) -> tuple[Optional[str], Optional[float]]:
    if yes_price is None or len(yes_window) < 10:  # Require at least 10 ticks (~150s of data) for stability
        return None, None
    vals = list(yes_window)
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / len(vals)
    std = var ** 0.5
    if std <= 1e-9:
        return None, None
    z = (yes_price - mean) / std
    if z > SETTINGS.zscore_threshold:
        return "DOWN", z
    if z < -SETTINGS.zscore_threshold:
        return "UP", z
    return None, z


# Strategies that already encode velocity / order-flow — skip extra momentum filter
_MOMENTUM_EXEMPT = frozenset([
    "ws_order_flow_up", "ws_order_flow_down",
    "ws_flash_snipe_up", "ws_flash_snipe_down",
    "poly_ob_imbalance_up", "poly_ob_imbalance_down",
    "time_snipe_up", "time_snipe_down",
    "binance_macd_rsi_up", "binance_macd_rsi_down",
    "cex_oracle_pump", "cex_oracle_dump", # Exempt front-running to ensure early entry!
])


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


def _probability_from_confidence(confidence: float, *, floor: float, ceiling: float) -> float:
    confidence = _clamp(confidence, 0.0, 1.0)
    return floor + (ceiling - floor) * confidence


def _build_candidate(
    base_result: dict,
    *,
    side: str,
    strategy_key: str,
    entry_price: float,
    model_probability: float,
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
            "market_probability": float(entry_price),
            "model_probability": _clamp(float(model_probability), 0.01, 0.99),
            "signal_confidence": _clamp(float(signal_confidence), 0.0, 1.0),
        }
    )
    result["model_edge"] = result["model_probability"] - result["market_probability"]
    if extras:
        result.update(extras)
    return result


def _rank_candidates(candidates: dict[str, dict]) -> list[dict]:
    if not candidates:
        return []
    ranked = sorted(
        candidates.values(),
        key=lambda candidate: (
            candidate.get("model_edge", float("-inf")),
            candidate.get("model_probability", 0.5),
            candidate.get("signal_confidence", 0.0),
        ),
        reverse=True,
    )
    return [candidate.copy() for candidate in ranked]


def _select_best_candidate(candidates: dict[str, dict]) -> Optional[dict]:
    if not candidates:
        return None
    ranked = _rank_candidates(candidates)
    best = ranked[0].copy()
    best["candidate_count"] = len(ranked)
    best["ranked_candidates"] = ranked
    return best


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
    poly_ob_down: Optional[dict] = None
) -> dict:
    prices = get_outcome_prices(market)
    gamma_up = prices.get("up") or prices.get("漲")
    gamma_down = prices.get("down") or prices.get("跌")
    up = observed_up if observed_up is not None else gamma_up
    down = observed_down if observed_down is not None else gamma_down
    secs_left = seconds_to_market_end(market)
    base_result = {
        "ok": False,
        "side": None,
        "reason": "unknown",
        "up": up,
        "down": down,
        "secs_left": secs_left,
        "spread": None,
        "entry_price": None,
        "mr_side": None,
    }
    
    if up is None or down is None:
        base_result["reason"] = "missing_prices"
        return base_result

    if secs_left is None:
        base_result["reason"] = "missing_end_time"
        return base_result
    if secs_left > SETTINGS.entry_window_max_sec:
        base_result["reason"] = "too_early_in_market"
        return base_result
    if secs_left < SETTINGS.entry_window_min_sec:
        base_result["reason"] = "too_late_in_market"
        return base_result

    valid_up = up is not None and SETTINGS.min_entry_price <= float(up) <= SETTINGS.max_entry_price
    valid_down = down is not None and SETTINGS.min_entry_price <= float(down) <= SETTINGS.max_entry_price

    if not valid_up and not valid_down:
        base_result["reason"] = f"prices_out_of_bounds_up{up}_down{down}"
        return base_result

    candidates = {}

    # Strategy 1: Binance Oracle Front-running (Disabled: 1-minute candle causes 60-second continuous false re-entries. Using Strategy 7 WS 3s pulse instead.)
    # if SETTINGS.use_cex_oracle and binance_1m:
    #     change = binance_1m.get("change", 0.0)
    #     if change >= SETTINGS.cex_frontrun_threshold and valid_up:
    #         r = base_result.copy()
    #         r.update({"ok": True, "side": "UP", "reason": "model-cex_oracle_pump", "entry_price": up})
    #         candidates["cex_oracle_pump"] = r
    #     elif change <= -SETTINGS.cex_frontrun_threshold and valid_down:
    #         r = base_result.copy()
    #         r.update({"ok": True, "side": "DOWN", "reason": "model-cex_oracle_dump", "entry_price": down})
    #         candidates["cex_oracle_dump"] = r

    # Strategy 5: Zhihu ZLSMA + ATR Scalper (disabled: continuous state trigger causes naive entries)
    # if binance_5m and len(binance_5m) >= 99:
    #     try:
    #         closes = [c['close'] for c in binance_5m]
    #         zlsma = calc_zlsma(closes, length=50)
    #         chandelier_dir = calc_chandelier_exit(binance_5m, atr_period=1, mult=2.0)
    #         
    #         if zlsma is not None:
    #             current_close = closes[-1]
    #             if current_close > zlsma and chandelier_dir == 1 and valid_up:
    #                 r = base_result.copy()
    #                 r.update({"ok": True, "side": "UP", "reason": "model-zlsma_scalper_up", "entry_price": up})
    #                 candidates["zlsma_scalper_up"] = r
    #             elif current_close < zlsma and chandelier_dir == -1 and valid_down:
    #                 r = base_result.copy()
    #                 r.update({"ok": True, "side": "DOWN", "reason": "model-zlsma_scalper_down", "entry_price": down})
    #                 candidates["zlsma_scalper_down"] = r
    #     except Exception:
    #         pass

    # Strategy 6: WebSocket Order Flow Imbalance (OFI) — requires Polymarket OB cross-confirmation
    if ws_trades:
        buy_vol, sell_vol = compute_buy_sell_pressure(ws_trades)
        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            ofi_ratio = buy_vol / total_vol
            ofi_threshold = getattr(SETTINGS, "ofi_bypass_threshold", 0.73)
            ofi_confidence = _confidence_from_signal(
                abs(ofi_ratio - 0.5),
                max(0.0, ofi_threshold - 0.5),
                0.5,
            )
            ofi_probability = _probability_from_confidence(ofi_confidence, floor=0.54, ceiling=0.74)

            # Polymarket OB cross-confirmation: also check that Poly bid pressure agrees
            poly_up_imbalance = _check_imbalance(poly_ob_up) if poly_ob_up else 0.5
            poly_down_imbalance = _check_imbalance(poly_ob_down) if poly_ob_down else 0.5

            if ofi_ratio > ofi_threshold and valid_up:
                # Binance says UP: Polymarket UP token must also have bid pressure > 0.55
                if poly_up_imbalance >= 0.55:
                    r = _build_candidate(
                        base_result,
                        side="UP",
                        strategy_key="ws_order_flow_up",
                        entry_price=float(up),
                        model_probability=ofi_probability,
                        signal_confidence=ofi_confidence,
                        extras={"ofi_ratio": ofi_ratio, "poly_up_imbalance": poly_up_imbalance},
                    )
                    candidates["ws_order_flow_up"] = r
            elif ofi_ratio < (1.0 - ofi_threshold) and valid_down:
                # Binance says DOWN: Polymarket DOWN token must also have bid pressure > 0.55
                if poly_down_imbalance >= 0.55:
                    r = _build_candidate(
                        base_result,
                        side="DOWN",
                        strategy_key="ws_order_flow_down",
                        entry_price=float(down),
                        model_probability=ofi_probability,
                        signal_confidence=ofi_confidence,
                        extras={"ofi_ratio": ofi_ratio, "poly_down_imbalance": poly_down_imbalance},
                    )
                    candidates["ws_order_flow_down"] = r


    # Strategy 7: WS Flash Snipe (WebSocket 閃電狙擊 0.3%)
    if getattr(SETTINGS, "ws_flash_snipe_threshold", 0.0) > 0 and ws_bba and ws_bba.get("b", 0.0) > 0:
        try:
            from core.ws_binance import BINANCE_WS
            # Guard: skip if WS has been silent for > 5 seconds (disconnected)
            if BINANCE_WS.get_last_update_age() < 5.0:
                vel = BINANCE_WS.get_price_velocity(
                    seconds=3.0,
                    lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0)),
                )
                flash_threshold = float(SETTINGS.ws_flash_snipe_threshold)
                flash_confidence = _confidence_from_signal(abs(vel), flash_threshold, flash_threshold * 2.0)
                flash_probability = _probability_from_confidence(flash_confidence, floor=0.54, ceiling=0.72)
                if vel > SETTINGS.ws_flash_snipe_threshold and valid_up:
                    r = _build_candidate(
                        base_result,
                        side="UP",
                        strategy_key="ws_flash_snipe_up",
                        entry_price=float(up),
                        model_probability=flash_probability,
                        signal_confidence=flash_confidence,
                        extras={"velocity_3s": vel},
                    )
                    candidates["ws_flash_snipe_up"] = r
                elif vel < -SETTINGS.ws_flash_snipe_threshold and valid_down:
                    r = _build_candidate(
                        base_result,
                        side="DOWN",
                        strategy_key="ws_flash_snipe_down",
                        entry_price=float(down),
                        model_probability=flash_probability,
                        signal_confidence=flash_confidence,
                        extras={"velocity_3s": vel},
                    )
                    candidates["ws_flash_snipe_down"] = r
        except Exception:
            pass

    # Strategy 8: Polymarket Orderbook Imbalance
    if poly_ob_up and poly_ob_down:
        imbalance_up = _check_imbalance(poly_ob_up)
        imbalance_down = _check_imbalance(poly_ob_down)
        
        # If bids dominate asks heavily (lowered threshold 0.85→0.78 to increase trade frequency)
        if imbalance_up > 0.78 and valid_up:
            imbalance_confidence = _confidence_from_signal(imbalance_up - 0.5, 0.28, 0.5)
            imbalance_probability = _probability_from_confidence(imbalance_confidence, floor=0.53, ceiling=0.72)
            r = _build_candidate(
                base_result,
                side="UP",
                strategy_key="poly_ob_imbalance_up",
                entry_price=float(up),
                model_probability=imbalance_probability,
                signal_confidence=imbalance_confidence,
                extras={"orderbook_imbalance": imbalance_up},
            )
            candidates["poly_ob_imbalance_up"] = r
        elif imbalance_down > 0.78 and valid_down:
            imbalance_confidence = _confidence_from_signal(imbalance_down - 0.5, 0.28, 0.5)
            imbalance_probability = _probability_from_confidence(imbalance_confidence, floor=0.53, ceiling=0.72)
            r = _build_candidate(
                base_result,
                side="DOWN",
                strategy_key="poly_ob_imbalance_down",
                entry_price=float(down),
                model_probability=imbalance_probability,
                signal_confidence=imbalance_confidence,
                extras={"orderbook_imbalance": imbalance_down},
            )
            candidates["poly_ob_imbalance_down"] = r

    # Strategy 9: Time-Based Snipe (disabled: leads to predictable naive entries and adverse selection)
    # if secs_left is not None and 235 <= secs_left <= 245:
    #     if up is not None and up > 0.60 and valid_up:
    #         r = base_result.copy()
    #         r.update({"ok": True, "side": "UP", "reason": "model-time_snipe_up", "entry_price": up})
    #         candidates["time_snipe_up"] = r
    #     elif down is not None and down > 0.60 and valid_down:
    #         r = base_result.copy()
    #         r.update({"ok": True, "side": "DOWN", "reason": "model-time_snipe_down", "entry_price": down})
    #         candidates["time_snipe_down"] = r

    # Strategy 10: Binance MACD & RSI Momentum (disabled: continuous state trigger causes naive entries)
    # if binance_5m and len(binance_5m) >= 30:
    #     try:
    #         from core.indicators import calc_rsi, calc_macd
    #         closes = [c['close'] for c in binance_5m]
    #         rsi = calc_rsi(closes, period=14)
    #         macd_res = calc_macd(closes)
    #         
    #         if rsi is not None and macd_res is not None:
    #             macd_line, signal_line, hist = macd_res
    #             
    #             # Bullish momentum on Binance
    #             if rsi < 70 and hist > 0 and valid_up:
    #                 r = base_result.copy()
    #                 r.update({"ok": True, "side": "UP", "reason": "model-binance_macd_rsi_up", "entry_price": up})
    #                 candidates["binance_macd_rsi_up"] = r
    #             
    #             # Bearish momentum on Binance
    #             elif rsi > 30 and hist < 0 and valid_down:
    #                 r = base_result.copy()
    #                 r.update({"ok": True, "side": "DOWN", "reason": "model-binance_macd_rsi_down", "entry_price": down})
    #                 candidates["binance_macd_rsi_down"] = r
    #     except Exception:
    #         pass

    # Mean Reversion
    mr, mr_zscore = mean_reversion_signal(up, yes_window)
    base_result["mr_side"] = mr
    base_result["mr_zscore"] = mr_zscore
    if mr:
        side = mr
        entry_price = up if side == "UP" else down
        if (side == "UP" and valid_up) or (side == "DOWN" and valid_down):
            mr_confidence = _confidence_from_signal(
                abs(float(mr_zscore or 0.0)),
                float(SETTINGS.zscore_threshold),
                float(SETTINGS.zscore_threshold) * 2.0,
            )
            mr_probability = _probability_from_confidence(mr_confidence, floor=0.52, ceiling=0.68)
            r = _build_candidate(
                base_result,
                side=side,
                strategy_key="mean_reversion_signal",
                entry_price=float(entry_price),
                model_probability=mr_probability,
                signal_confidence=mr_confidence,
                extras={"mr_zscore": mr_zscore},
            )
            candidates["mean_reversion_signal"] = r


    # Apply Momentum Confirmation (OFI/flash-snipe strategies are exempt — they carry own velocity)
    filtered_candidates = {}
    for name, s_result in candidates.items():
        if name in _MOMENTUM_EXEMPT:
            filtered_candidates[name] = s_result
            continue
        if up_window is not None and down_window is not None:
            if not _has_momentum(s_result.get("side"), up_window, down_window):
                continue
        filtered_candidates[name] = s_result

    if not filtered_candidates:
        r = base_result.copy()
        # Report why — if momentum filter ate everything vs no candidates at all
        if candidates:
            r["reason"] = f"flow_too_weak_{len(candidates)}{int(secs_left or 0)}"
        else:
            r["reason"] = "no_valid_signals"
        return r

    best_decision = _select_best_candidate(filtered_candidates)
    if best_decision:
        return best_decision

    r = base_result.copy()
    r["reason"] = "no_best_strategy_found"
    return r


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
    poly_ob_down: Optional[dict] = None
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
