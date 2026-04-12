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
            "probability_source": "heuristic",
            "signal_confidence": _clamp(float(signal_confidence), 0.0, 1.0),
        }
    )
    result["model_edge"] = result["model_probability"] - result["market_probability"]
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
                c.get("model_probability", 0.5),
                c.get("signal_confidence", 0.0),
            )
        # StrategyResult
        return (
            getattr(c, "raw_edge", float("-inf")),
            getattr(c, "model_probability", 0.5),
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
            "market_probability": float(c.entry_price),
            "model_probability": _clamp(float(c.model_probability), 0.01, 0.99),
            "signal_confidence": _clamp(float(c.confidence), 0.0, 1.0),
            "model_edge": float(c.raw_edge),
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
    from core.exchange import estimate_entry_avg_price_from_asks

    prices = get_outcome_prices(market)
    gamma_up = prices.get("up") or prices.get("漲")
    gamma_down = prices.get("down") or prices.get("跌")
    up = observed_up if observed_up is not None else gamma_up
    down = observed_down if observed_down is not None else gamma_down

    # Calculate Executable Prices (Assume default $10 estimation size)
    exec_est_size = float(getattr(SETTINGS, "min_live_order_usd", 10.0) or 10.0)
    exec_up = up
    if poly_ob_up:
        est_price, _, fill_ratio = estimate_entry_avg_price_from_asks(poly_ob_up, exec_est_size)
        if est_price is not None and est_price > 0 and fill_ratio >= 0.5:
            exec_up = est_price

    exec_down = down
    if poly_ob_down:
        est_price, _, fill_ratio = estimate_entry_avg_price_from_asks(poly_ob_down, exec_est_size)
        if est_price is not None and est_price > 0 and fill_ratio >= 0.5:
            exec_down = est_price

    up = exec_up
    down = exec_down

    secs_left = seconds_to_market_end(market)
    
    # 15m Time Regime Split
    regime = _get_time_regime(secs_left) if secs_left is not None else "unknown"

    base_result = {
        "ok": False,
        "side": None,
        "reason": "no_valid_signals",
        "up": exec_up, # use executable price
        "down": exec_down, # use executable price
        "secs_left": secs_left,
        "spread": None,
        "entry_price": None,
        "mr_side": None,
        "regime": regime,
    }


    if up is None or down is None:
        base_result["reason"] = "missing_prices"
        return base_result

    if secs_left is None:
        base_result["reason"] = "missing_end_time"
        return base_result
    
    # Get AI Advisory for 15m
    ai_advice = {"no_trade_bias": False, "allow_strategies": [], "confidence_modifier": 0.0}
    if SETTINGS.market_profile == "btc_15m" and SETTINGS.ai_advisor_enabled:
        vel_3s = 0.0
        try:
            from core.ws_binance import BINANCE_WS
            vel_3s = BINANCE_WS.get_price_velocity(3.0)
        except Exception: pass
        
        ai_advice = AI_ADVISOR.get_advisory(
            market.get("slug", ""),
            secs_left,
            float(up),
            float(down),
            vel_3s
        )

    time_valid = False
    if secs_left is not None:
        if secs_left > SETTINGS.entry_window_max_sec:
            base_result["reason"] = "too_early_in_market"
        elif secs_left < SETTINGS.entry_window_min_sec:
            base_result["reason"] = "too_late_in_market"
        else:
            time_valid = True

    regular_valid_up = (
        up is not None
        and SETTINGS.min_entry_price <= float(up) <= SETTINGS.max_entry_price
    )
    regular_valid_down = (
        down is not None
        and SETTINGS.min_entry_price <= float(down) <= SETTINGS.max_entry_price
    )

    _snipe_min = float(getattr(SETTINGS, "snipe_min_entry_price", 0.05))
    _snipe_max = float(getattr(SETTINGS, "snipe_max_entry_price", 0.96))
    snipe_valid_up = up is not None and _snipe_min <= float(up) <= _snipe_max
    snipe_valid_down = down is not None and _snipe_min <= float(down) <= _snipe_max

    if (
        base_result.get("reason") == "no_valid_signals"
        and not regular_valid_up
        and not regular_valid_down
        and not snipe_valid_up
        and not snipe_valid_down
    ):
        base_result["reason"] = f"prices_out_of_bounds_up{up}_down{down}"

    candidates = {}

    # Extract Strike Price for Advanced Strategies
    strike_price = market.get("strike_price")
    if strike_price is None:
        strike_price = _extract_strike_price(market.get("question", ""))

    # Advanced Strategy 1: Theta Bleed Arbitrage
    if getattr(SETTINGS, "theta_bleed_enabled", True) and strike_price is not None:
        # VPN Safe Mode: Disable Theta Bleed (high latency dependency)
        if SETTINGS.vpn_safe_mode and SETTINGS.vpn_disable_theta_bleed:
            pass
        else:
            try:
                from core.ws_binance import BINANCE_WS

                if secs_left is not None and secs_left <= float(
                    getattr(SETTINGS, "theta_bleed_min_sec", 60.0)
                ):
                    if BINANCE_WS.get_last_update_age() < 5.0:
                        binance_bba = BINANCE_WS.get_bba()
                        binance_mid = (
                            binance_bba.get("b", 0.0) + binance_bba.get("a", 0.0)
                        ) / 2.0
                        if binance_mid > 0:
                            dist = binance_mid - strike_price
                            theta_dist = float(
                                getattr(SETTINGS, "theta_bleed_distance", 120.0)
                            )

                            # If Binance is > 120 dist ABOVE strike, UP is highly certain
                            if dist > theta_dist and snipe_valid_up:
                                r = _build_candidate(
                                    base_result,
                                    side="UP",
                                    strategy_key="theta_bleed_up",
                                    entry_price=float(up),
                                    model_probability=0.99,  # Extremely high probability
                                    signal_confidence=1.0,
                                    extras={
                                        "binance_mid": binance_mid,
                                        "strike_price": strike_price,
                                        "dist": dist,
                                    },
                                )
                                candidates["theta_bleed_up"] = r

                            # If Binance is < 120 dist BELOW strike, DOWN is highly certain
                            elif dist < -theta_dist and snipe_valid_down:
                                r = _build_candidate(
                                    base_result,
                                    side="DOWN",
                                    strategy_key="theta_bleed_down",
                                    entry_price=float(down),
                                    model_probability=0.99,
                                    signal_confidence=1.0,
                                    extras={
                                        "binance_mid": binance_mid,
                                        "strike_price": strike_price,
                                        "dist": dist,
                                    },
                                )
                                candidates["theta_bleed_down"] = r
            except Exception:
                pass

    # Advanced Strategy 3: Strike Cross Front-run Snipe
    if (
        getattr(SETTINGS, "strike_cross_snipe_enabled", True)
        and strike_price is not None
    ):
        # VPN Safe Mode: Disable Strike Cross Snipe
        if SETTINGS.vpn_safe_mode and SETTINGS.vpn_disable_strike_cross:
            pass
        else:
            try:
                from core.ws_binance import BINANCE_WS

                if BINANCE_WS.get_last_update_age() < 5.0:
                    oldest, newest = BINANCE_WS.get_recent_prices_window(seconds=5.0)
                    if oldest is not None and newest is not None:
                        gap = float(getattr(SETTINGS, "strike_cross_gap", 20.0))

                        # Crossed UP securely
                        if (
                            oldest < strike_price
                            and newest > (strike_price + gap)
                            and snipe_valid_up
                        ):
                            r = _build_candidate(
                                base_result,
                                side="UP",
                                strategy_key="strike_cross_snipe_up",
                                entry_price=float(up),
                                model_probability=0.99,  # High, exempts from stabilization
                                signal_confidence=0.95,
                                extras={
                                    "oldest": oldest,
                                    "newest": newest,
                                    "strike_price": strike_price,
                                },
                            )
                            candidates["strike_cross_snipe_up"] = r

                        # Crossed DOWN securely
                        elif (
                            oldest > strike_price
                            and newest < (strike_price - gap)
                            and snipe_valid_down
                        ):
                            r = _build_candidate(
                                base_result,
                                side="DOWN",
                                strategy_key="strike_cross_snipe_down",
                                entry_price=float(down),
                                model_probability=0.99,  # High, exempts from stabilization
                                signal_confidence=0.95,
                                extras={
                                    "oldest": oldest,
                                    "newest": newest,
                                    "strike_price": strike_price,
                                },
                            )
                            candidates["strike_cross_snipe_down"] = r
            except Exception:
                pass

    # Strategy 1: Binance Oracle Front-running (Disabled)
    # ... (omitted) ...

    # Strategy 5: Zhihu ZLSMA + ATR Scalper (Disabled)
    # ... (omitted) ...

    # Strategy 6: WebSocket Order Flow Imbalance (OFI)
    for res in get_ofi_signal(ws_trades, up, down, poly_ob_up, poly_ob_down, SETTINGS):
        candidates[res.strategy_name.replace("model-", "")] = res

    # Strategy 7: WS Flash Snipe (WebSocket 閃電狙擊 0.3%)
    try:
        from core.ws_binance import BINANCE_WS
        # VPN Safe Mode: Disable Flash Snipe
        if SETTINGS.vpn_safe_mode and SETTINGS.vpn_disable_flash_snipe:
            pass
        elif BINANCE_WS.get_last_update_age() < 5.0:
            vel = BINANCE_WS.get_price_velocity(
                seconds=3.0,
                lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0)),
            )
            for res in get_flash_snipe_signal(vel, up, down, snipe_valid_up, snipe_valid_down, SETTINGS):
                candidates[res.strategy_name.replace("model-", "")] = res
    except Exception:
        pass

    # Strategy 8: Polymarket Orderbook Imbalance
    if poly_ob_up and poly_ob_down:
        imbalance_up = _check_imbalance(poly_ob_up)
        imbalance_down = _check_imbalance(poly_ob_down)

        if imbalance_up > 0.78 and regular_valid_up:
            imbalance_confidence = _confidence_from_signal(
                imbalance_up - 0.5, 0.28, 0.5
            )
            imbalance_probability = _probability_from_confidence(
                imbalance_confidence, floor=0.53, ceiling=0.72
            )
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
        if imbalance_down > 0.78 and regular_valid_down:
            imbalance_confidence = _confidence_from_signal(
                imbalance_down - 0.5, 0.28, 0.5
            )
            imbalance_probability = _probability_from_confidence(
                imbalance_confidence, floor=0.53, ceiling=0.72
            )
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

    # Strategy 9-10 (Disabled)
    # ... (omitted) ...

    # Strategy 11: Binance Liquidation Fader
    try:
        from core.ws_binance import BINANCE_WS

        if (
            getattr(SETTINGS, "liquidation_fade_min_usd", 0.0) > 0
            and BINANCE_WS.get_last_update_age() < 5.0
        ):
            # VPN Safe Mode: Disable Liquidation Fade
            if SETTINGS.vpn_safe_mode and SETTINGS.vpn_disable_liquidation_fade:
                pass
            else:
                window = float(getattr(SETTINGS, "liquidation_fade_window_sec", 20.0))
                lqs = BINANCE_WS.get_recent_liquidations(seconds=window)
                if lqs:
                    long_liq_usd = sum(lq["usd_size"] for lq in lqs if lq["side"] == "SELL")
                    short_liq_usd = sum(lq["usd_size"] for lq in lqs if lq["side"] == "BUY")
                    min_thresh = float(SETTINGS.liquidation_fade_min_usd)

                    if long_liq_usd >= min_thresh and regular_valid_up:
                        fade_confidence = _clamp(
                            long_liq_usd / (min_thresh * 3.0), 0.6, 1.0
                        )
                        r = _build_candidate(
                            base_result,
                            side="UP",
                            strategy_key="liquidation_fade_up",
                            entry_price=float(up),
                            model_probability=0.75,
                            signal_confidence=fade_confidence,
                            extras={"long_liq_usd": long_liq_usd},
                        )
                        candidates["liquidation_fade_up"] = r
                    elif short_liq_usd >= min_thresh and regular_valid_down:
                        fade_confidence = _clamp(
                            short_liq_usd / (min_thresh * 3.0), 0.6, 1.0
                        )
                        r = _build_candidate(
                            base_result,
                            side="DOWN",
                            strategy_key="liquidation_fade_down",
                            entry_price=float(down),
                            model_probability=0.75,
                            signal_confidence=fade_confidence,
                            extras={"short_liq_usd": short_liq_usd},
                        )
                        candidates["liquidation_fade_down"] = r
    except Exception:
        pass

    # Strategy 12: Early Underdog Sniper
    try:
        if secs_left is not None:
            min_time = float(getattr(SETTINGS, "early_underdog_min_time", 220.0))
            if secs_left >= min_time:
                # VPN Safe Mode: Disable Early Underdog in Live
                if not SETTINGS.dry_run and SETTINGS.vpn_safe_mode and SETTINGS.vpn_disable_early_underdog_live:
                    pass
                else:
                    max_price = float(getattr(SETTINGS, "early_underdog_max_price", 0.35))
                    from core.ws_binance import BINANCE_WS

                    if BINANCE_WS.get_last_update_age() < 5.0:
                        vel = BINANCE_WS.get_price_velocity(seconds=3.0)
                        if up is not None and 0.0 < float(up) <= max_price and vel > 0.0003:
                            r = _build_candidate(
                                base_result,
                                side="UP",
                                strategy_key="early_underdog_up",
                                entry_price=float(up),
                                model_probability=0.76,
                                signal_confidence=0.8,
                                extras={"secs_left": secs_left, "vel": vel},
                            )
                            candidates["early_underdog_up"] = r
                        elif (
                            down is not None
                            and 0.0 < float(down) <= max_price
                            and vel < -0.0003
                        ):
                            r = _build_candidate(
                                base_result,
                                side="DOWN",
                                strategy_key="early_underdog_down",
                                entry_price=float(down),
                                model_probability=0.76,
                                signal_confidence=0.8,
                                extras={"secs_left": secs_left, "vel": vel},
                            )
                            candidates["early_underdog_down"] = r
    except Exception:
        pass

    # Strategy 13: 15m Extreme-Price Fade (Counter-trend Value Entry)
    if SETTINGS.market_profile == "btc_15m":
        # Fade UP (Price of UP is too high, entry DOWN is cheap)
        if float(up) >= SETTINGS.soft_no_chase_above and float(down) <= SETTINGS.cheap_ticket_max_price:
            if regime in ["mid", "late"]:
                 r = _build_candidate(
                    base_result,
                    side="DOWN",
                    strategy_key="extreme_price_fade_down",
                    entry_price=float(down),
                    model_probability=0.68,
                    signal_confidence=0.8,
                    extras={"fade_target": "UP", "price": up, "regime": regime}
                )
                 candidates["extreme_price_fade_down"] = r
        # Fade DOWN (Price of DOWN is too high, entry UP is cheap)
        elif float(down) >= SETTINGS.soft_no_chase_above and float(up) <= SETTINGS.cheap_ticket_max_price:
            if regime in ["mid", "late"]:
                 r = _build_candidate(
                    base_result,
                    side="UP",
                    strategy_key="extreme_price_fade_up",
                    entry_price=float(up),
                    model_probability=0.68,
                    signal_confidence=0.8,
                    extras={"fade_target": "DOWN", "price": down, "regime": regime}
                )
                 candidates["extreme_price_fade_up"] = r

    # Mean Reversion
    mr_res = mean_reversion.run(up, yes_window, SETTINGS)
    if mr_res:
        candidates["mean_reversion"] = mr_res

    # Apply Momentum Confirmation and Edge Filters
    latency_penalty = LATENCY_MONITOR.get_edge_penalty()
    filtered_candidates = {}
    
    for name, s_result in candidates.items():
        if not time_valid:
            continue
        
        # 15m Strategy Blacklist (Disable latency-sensitive 5m strategies)
        if SETTINGS.market_profile == "btc_15m":
            if any(k in name for k in ["ws_flash_snipe", "strike_cross_snipe", "theta_bleed"]):
                continue
            
            # AI Advisor Strategy Filter & No-Trade Bias
            if SETTINGS.ai_advisor_enabled:
                if ai_advice.get("no_trade_bias"):
                    continue
                # If AI allows specific strategies, only allow those + extreme_fade
                allowed = ai_advice.get("allow_strategies", [])
                if allowed and not any(a in name for a in allowed) and "extreme_price_fade" not in name:
                    continue

        # VPN Safe Mode: Block if secs_left < 150
        if SETTINGS.vpn_safe_mode and secs_left is not None and secs_left < SETTINGS.vpn_entry_min_secs_left:
            continue

        side = s_result.side if hasattr(s_result, "side") else s_result.get("side")
        price = float(up if side == "UP" else down)

        # 15m Value Entry Bands & No-Chase Rules
        if SETTINGS.market_profile == "btc_15m":
            if price > SETTINGS.hard_no_chase_above:
                continue
            # If price is in soft-no-chase zone, require very high probability
            if price > SETTINGS.soft_no_chase_above:
                prob = s_result.get("model_probability", 0)
                if prob < 0.85:
                    continue

        # Apply AI Confidence Modifier
        if SETTINGS.ai_advisor_enabled:
            s_result["model_probability"] += ai_advice.get("confidence_modifier", 0.0)

        raw_edge = getattr(s_result, "raw_edge", None)
        if raw_edge is None:
            raw_edge = s_result.get("model_edge", 0.0)
            
        required_edge = getattr(s_result, "required_edge", 0.05)
        
        # VPN Safe Mode: Hard floor for required edge
        if SETTINGS.vpn_safe_mode:
            required_edge = max(required_edge, SETTINGS.vpn_min_executable_edge)
            
        effective_required_edge = required_edge + latency_penalty

        # 1. Price Bounds Filter
        is_snipe = name.startswith("ws_flash_snipe") or name.startswith("strike_cross_snipe") or name.startswith("theta_bleed")
        if side == "UP":
            if is_snipe:
                if not snipe_valid_up: continue
            elif not regular_valid_up:
                continue
        elif side == "DOWN":
            if is_snipe:
                if not snipe_valid_down: continue
            elif not regular_valid_down:
                continue

        # 2. Momentum Filter
        if up_window is not None and down_window is not None:
            if not _has_momentum(side, up_window, down_window):
                continue

        # 3. Edge Filter (Latency Aware)
        if raw_edge < effective_required_edge:
            continue

        filtered_candidates[name] = s_result

    if not filtered_candidates:
        r = base_result.copy()
        if candidates:
            r["reason"] = (
                f"flow_too_weak_{len(candidates)}_lat{latency_penalty:.3f}"
                if time_valid
                else r.get("reason", "too_late_in_market")
            )
        else:
            if not r.get("reason"):
                 r["reason"] = "no_valid_signals"
        return r

    # Handle Aggressive Volume Mode (Return all candidates)
    if getattr(SETTINGS, "aggressive_volume_mode", False):
        ranked = _rank_candidates(filtered_candidates)
        # Convert all to dicts for output
        all_dicts = []
        for c in ranked:
            if isinstance(c, dict):
                all_dicts.append(c)
            else:
                d = base_result.copy()
                d.update({
                    "ok": True,
                    "side": c.side,
                    "reason": c.trigger_reason,
                    "strategy_name": c.strategy_name,
                    "entry_price": float(c.entry_price),
                    "market_probability": float(c.entry_price),
                    "model_probability": _clamp(float(c.model_probability), 0.01, 0.99),
                    "signal_confidence": _clamp(float(c.confidence), 0.0, 1.0),
                    "model_edge": float(c.raw_edge),
                })
                if hasattr(c, "metadata") and c.metadata:
                    d.update(c.metadata)
                all_dicts.append(d)
        
        best = all_dicts[0].copy()
        best["candidates"] = all_dicts
        best["candidate_count"] = len(all_dicts)
        return best

    best_decision = _select_best_candidate(filtered_candidates, base_result)
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
