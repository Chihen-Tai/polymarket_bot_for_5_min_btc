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


def _market_start_epoch(market: dict) -> int | None:
    slug = str(market.get("slug") or "").strip()
    if not slug:
        return None
    try:
        return int(slug.split("-")[-1])
    except Exception:
        return None


def _kline_open_price(kline: dict) -> float | None:
    return _sf(kline.get("open", kline.get("o")))


def _kline_open_time_ms(kline: dict) -> float | None:
    return _sf(kline.get("open_time", kline.get("t", kline.get("openTime"))))


def _kline_close_time_ms(kline: dict) -> float | None:
    return _sf(kline.get("close_time", kline.get("T", kline.get("closeTime"))))


def compute_market_window_features(
    *,
    market: dict,
    btc_price: float,
    fair_value_yes: float,
    binance_5m: Optional[list[dict]] = None,
    ws_trades: Optional[list[dict]] = None,
) -> dict:
    window_open_price: float | None = None
    market_start_epoch = _market_start_epoch(market)
    candles = list(binance_5m or [])
    if candles:
        if market_start_epoch is not None:
            market_start_ms = float(market_start_epoch) * 1000.0
            for candle in candles:
                open_time_ms = _kline_open_time_ms(candle)
                close_time_ms = _kline_close_time_ms(candle)
                if open_time_ms is None:
                    continue
                if close_time_ms is None:
                    close_time_ms = open_time_ms + 300000.0
                if open_time_ms <= market_start_ms < close_time_ms:
                    window_open_price = _kline_open_price(candle)
                    break
            if window_open_price is None:
                eligible = [
                    candle
                    for candle in candles
                    if _kline_open_time_ms(candle) is not None
                    and float(_kline_open_time_ms(candle)) <= market_start_ms
                ]
                if eligible:
                    window_open_price = _kline_open_price(eligible[-1])
        if window_open_price is None:
            recent_window = candles[-3:] if len(candles) >= 3 else candles
            if recent_window:
                window_open_price = _kline_open_price(recent_window[0])

    window_delta_pct = 0.0
    if window_open_price and window_open_price > 0 and btc_price > 0:
        window_delta_pct = (btc_price - window_open_price) / window_open_price

    last_10s_velocity_bps = 0.0
    if ws_trades:
        try:
            normalized = []
            for trade in ws_trades:
                ts = _sf(trade.get("ts", trade.get("timestamp")))
                px = _sf(trade.get("p", trade.get("price")))
                if ts is None or px is None or px <= 0:
                    continue
                normalized.append((ts, px))
            if len(normalized) >= 2:
                normalized.sort(key=lambda item: item[0])
                latest_ts = normalized[-1][0]
                window = [item for item in normalized if item[0] >= latest_ts - 10.0]
                if len(window) >= 2 and window[0][1] > 0:
                    last_10s_velocity_bps = ((window[-1][1] - window[0][1]) / window[0][1]) * 10000.0
        except Exception:
            last_10s_velocity_bps = 0.0

    return {
        "window_delta_pct": float(window_delta_pct),
        "last_10s_velocity_bps": float(last_10s_velocity_bps),
        "oracle_implied_prob": _clamp(float(fair_value_yes), 0.01, 0.99),
    }


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
            "model_probability": _clamp(float(signal_score), 0.01, 0.99),
            "probability_source": "fair_value_model",
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
    
    if binance_1m is None:
        base_result["reason"] = "missing_valuation_inputs"
        return base_result

    price_history = [float(k.get('close', k.get('c', 0))) for k in (binance_5m or [])]
    btc_price = float(binance_1m.get("close", binance_1m.get("c", 0)))
    fv_yes = get_fair_value(
        btc_price, 
        strike_price, 
        secs_left, 
        price_history=price_history,
        ws_bba=ws_bba
    )
    market_features = compute_market_window_features(
        market=market,
        btc_price=btc_price,
        fair_value_yes=fv_yes,
        binance_5m=binance_5m,
        ws_trades=ws_trades,
    )
    base_result.update(market_features)

    # 3. Sniper 核心過濾：只交易極端區域
    up_price = float(observed_up or 0.5)
    down_price = float(observed_down or 0.5)
    
    # 攔截空訂單簿或報價錯誤 (正常 up+down 應該在 0.9 ~ 1.1 之間)
    if abs(up_price + down_price - 1.0) > 0.15:
        base_result["reason"] = "empty_orderbook_anomaly"
        return base_result

    # 必須不是中性區 (0.45 - 0.55 block)
    neutral_width = float(getattr(SETTINGS, "vpn_neutral_zone_width", 0.05) or 0.05)
    if abs(up_price - 0.5) <= neutral_width:
        base_result["reason"] = "neutral_zone_no_trade"
        return base_result

    # 4. 波動率閘門 (Volatility Gate) 與 OFI Override
    bypass_vol_gate = False
    ofi_ratio = None
    if ws_trades:
        from core.indicators import compute_buy_sell_pressure
        bv, sv = compute_buy_sell_pressure(ws_trades)
        tv = bv + sv
        if tv > 0:
            ofi_ratio = bv / max(tv, 1e-9)
        if tv > 50000 and ofi_ratio is not None:
            if ofi_ratio > 0.70 or ofi_ratio < 0.30:
                bypass_vol_gate = True

    if binance_5m and not bypass_vol_gate:
        recent_prices = [float(k.get('close', k.get('c', btc_price))) for k in binance_5m[-5:]]
        if recent_prices:
            price_range_bps = (max(recent_prices) - min(recent_prices)) / max(min(recent_prices), 1e-9) * 10000.0
            min_vol_bps = float(getattr(SETTINGS, "min_volatility_gate_bps", 8.0) or 8.0)
            if price_range_bps < min_vol_bps:
                base_result["reason"] = f"low_volatility_gate (range={price_range_bps:.1f}bps < {min_vol_bps}bps)"
                return base_result

    # 5. OFI threshold gate — require minimum book imbalance (§6.2)
    # Skip if bypass_vol_gate already triggered (extreme OFI confirmed)
    min_ofi = float(getattr(SETTINGS, "min_poly_ofi_threshold", 0.15) or 0.15)
    poly_ofi_up = 0.0
    poly_ofi_down = 0.0
    if poly_ob_up:
        bids_up = poly_ob_up.get("bids", [])[:3]
        asks_up = poly_ob_up.get("asks", [])[:3]
        bid_vol_up = sum(float(b.size or 0) if hasattr(b, "size") else float(b.get("size", 0)) for b in bids_up)
        ask_vol_up = sum(float(a.size or 0) if hasattr(a, "size") else float(a.get("size", 0)) for a in asks_up)
        tv_up = bid_vol_up + ask_vol_up
        if tv_up > 1e-9:
            poly_ofi_up = (bid_vol_up - ask_vol_up) / tv_up
    if poly_ob_down:
        bids_dn = poly_ob_down.get("bids", [])[:3]
        asks_dn = poly_ob_down.get("asks", [])[:3]
        bid_vol_dn = sum(float(b.size or 0) if hasattr(b, "size") else float(b.get("size", 0)) for b in bids_dn)
        ask_vol_dn = sum(float(a.size or 0) if hasattr(a, "size") else float(a.get("size", 0)) for a in asks_dn)
        tv_dn = bid_vol_dn + ask_vol_dn
        if tv_dn > 1e-9:
            poly_ofi_down = (bid_vol_dn - ask_vol_dn) / tv_dn
    if not bypass_vol_gate:
        if abs(poly_ofi_up) < min_ofi and abs(poly_ofi_down) < min_ofi:
            base_result["reason"] = f"ofi_below_threshold (up={poly_ofi_up:.3f}, dn={poly_ofi_down:.3f} < {min_ofi})"
            return base_result

    # 6. 10-minute macro trend filter — suppress direction opposing recent candle momentum (§6.2)
    if binance_5m and len(binance_5m) >= 2 and getattr(SETTINGS, "macro_trend_filter_enabled", True):
        last_two = binance_5m[-2:]
        both_bearish = all(
            float(k.get("close", k.get("c", 0))) < float(k.get("open", k.get("o", 0)))
            for k in last_two
        )
        both_bullish = all(
            float(k.get("close", k.get("c", 0))) > float(k.get("open", k.get("o", 0)))
            for k in last_two
        )
        if both_bearish:
            base_result["_macro_trend_suppress_up"] = True
        if both_bullish:
            base_result["_macro_trend_suppress_down"] = True

    # 7. Golden entry window enforcement — only enter between T-8min and T-5min (§6.2)
    golden_window_enabled = bool(getattr(SETTINGS, "golden_entry_window_enabled", False))
    golden_min_sec = float(getattr(SETTINGS, "golden_entry_window_min_sec", 300.0) or 300.0)
    golden_max_sec = float(getattr(SETTINGS, "golden_entry_window_max_sec", 480.0) or 480.0)
    if golden_window_enabled and secs_left is not None:
        if not (golden_min_sec <= secs_left <= golden_max_sec):
            base_result["reason"] = f"outside_golden_entry_window (secs_left={secs_left:.0f}, window={golden_min_sec:.0f}-{golden_max_sec:.0f}s)"
            return base_result

    # 8. 承諾邊際 (Committed Edge) 與 延遲補償
    order_size = float(getattr(SETTINGS, "min_live_order_usd", 1.0))
    # We enforce assume_maker=True for the VPN profile
    edge_up = calculate_committed_edge(fv_yes, poly_ob_up, poly_ob_down, order_size, "UP", assume_maker=SETTINGS.vpn_maker_only, secs_left=secs_left)
    edge_down = calculate_committed_edge(fv_yes, poly_ob_up, poly_ob_down, order_size, "DOWN", assume_maker=SETTINGS.vpn_maker_only, secs_left=secs_left)
    
    candidates = {}
    threshold = float(SETTINGS.min_sniper_edge_bps) / 10000.0

    suppress_up = base_result.pop("_macro_trend_suppress_up", False)
    suppress_down = base_result.pop("_macro_trend_suppress_down", False)
    spot_delta_threshold = max(
        0.001,
        float(getattr(SETTINGS, "spot_delta_guard_threshold_pct", 0.001) or 0.001),
    )
    fade_blocked_by_spot_delta = False
    if ofi_ratio is not None:
        ofi_direction = 1 if ofi_ratio > 0.5 else (-1 if ofi_ratio < 0.5 else 0)
        if (
            ofi_direction != 0
            and abs(float(market_features["window_delta_pct"])) > spot_delta_threshold
            and (float(market_features["window_delta_pct"]) * ofi_direction) > 0
        ):
            fade_blocked_by_spot_delta = True
            base_result["_fade_block_reason"] = (
                f"spot_delta_confirms_ofi (delta={market_features['window_delta_pct']:.4%}, ofi={ofi_ratio:.3f})"
            )

    if edge_up >= threshold and not suppress_up and not fade_blocked_by_spot_delta:
        reason = "fade_retail_panic" if up_price < SETTINGS.sniper_extreme_lower else "fade_retail_fomo"
        up_ladder = poly_ob_up.get('ask_levels', poly_ob_up.get('asks', []))
        candidates["sniper_fade_up"] = _build_candidate(
            base_result, side="UP", strategy_key=reason,
            entry_price=get_vwap_from_ladder(up_ladder, order_size),
            signal_score=fv_yes, signal_confidence=1.0,
            extras={
                "sniper_edge": edge_up,
                "behavioral_alpha": reason,
                "latency_buffer": SETTINGS.latency_buffer_usd,
                "preferred_execution_style": "maker",
            }
        )

    if edge_down >= threshold and not suppress_down and not fade_blocked_by_spot_delta:
        reason = "fade_retail_panic" if (1.0 - up_price) < SETTINGS.sniper_extreme_lower else "fade_retail_fomo"
        down_ladder = poly_ob_down.get('ask_levels', poly_ob_down.get('asks', []))
        candidates["sniper_fade_down"] = _build_candidate(
            base_result, side="DOWN", strategy_key=reason,
            entry_price=get_vwap_from_ladder(down_ladder, order_size),
            signal_score=1.0 - fv_yes, signal_confidence=1.0,
            extras={
                "sniper_edge": edge_down,
                "behavioral_alpha": reason,
                "latency_buffer": SETTINGS.latency_buffer_usd,
                "preferred_execution_style": "maker",
            }
        )

    momentum_delta_threshold = max(
        0.001,
        float(
            getattr(SETTINGS, "momentum_window_delta_threshold_pct", 0.001) or 0.001
        ),
    )
    momentum_entry_offset = float(
        getattr(SETTINGS, "momentum_entry_offset", 0.01) or 0.01
    )
    momentum_secs_left = float(
        getattr(SETTINGS, "momentum_t60_entry_sec", 60.0) or 60.0
    )
    momentum_boost = min(
        0.05,
        max(0.02, abs(float(market_features["window_delta_pct"])) * 20.0),
    )
    if secs_left is not None and secs_left <= momentum_secs_left:
        if (
            float(market_features["window_delta_pct"]) >= momentum_delta_threshold
            and poly_ofi_up >= min_ofi
        ):
            oracle_prob = float(market_features["oracle_implied_prob"])
            candidates["follow_momentum_t60_up"] = _build_candidate(
                base_result,
                side="UP",
                strategy_key="follow_momentum_t60",
                entry_price=_clamp(oracle_prob - momentum_entry_offset, 0.01, 0.99),
                signal_score=_clamp(oracle_prob + momentum_boost, 0.01, 0.99),
                signal_confidence=1.0,
                extras={
                    "window_delta_pct": market_features["window_delta_pct"],
                    "last_10s_velocity_bps": market_features["last_10s_velocity_bps"],
                    "oracle_implied_prob": oracle_prob,
                    "book_skew_agrees": True,
                    "preferred_execution_style": "maker",
                },
            )
        elif (
            float(market_features["window_delta_pct"]) <= -momentum_delta_threshold
            and poly_ofi_down >= min_ofi
        ):
            oracle_prob = 1.0 - float(market_features["oracle_implied_prob"])
            candidates["follow_momentum_t60_down"] = _build_candidate(
                base_result,
                side="DOWN",
                strategy_key="follow_momentum_t60",
                entry_price=_clamp(oracle_prob - momentum_entry_offset, 0.01, 0.99),
                signal_score=_clamp(oracle_prob + momentum_boost, 0.01, 0.99),
                signal_confidence=1.0,
                extras={
                    "window_delta_pct": market_features["window_delta_pct"],
                    "last_10s_velocity_bps": market_features["last_10s_velocity_bps"],
                    "oracle_implied_prob": oracle_prob,
                    "book_skew_agrees": True,
                    "preferred_execution_style": "maker",
                },
            )

    # 10. Legacy strategies (OFI, flash snipe) — gated by ENABLE_LEGACY_STRATEGIES
    if getattr(SETTINGS, "enable_legacy_strategies", False):
        _vel = 0.0
        if ws_trades and len(ws_trades) >= 2:
            t0 = float(ws_trades[0].get("timestamp", 0) or 0)
            t1 = float(ws_trades[-1].get("timestamp", 0) or 0)
            dt = max(t1 - t0, 1e-9)
            p0 = float(ws_trades[0].get("price", 0) or 0)
            p1 = float(ws_trades[-1].get("price", 0) or 0)
            _vel = (p1 - p0) / dt

        ofi_results = get_ofi_signal(
            ws_trades or [], up_price, down_price,
            poly_ob_up, poly_ob_down, SETTINGS,
        )
        for sr in ofi_results:
            if (sr.side == "UP" and not suppress_up) or (sr.side == "DOWN" and not suppress_down):
                candidates[f"legacy_{sr.strategy_name}"] = _build_candidate(
                    base_result, side=sr.side, strategy_key=sr.strategy_name,
                    entry_price=sr.entry_price, signal_score=sr.signal_score,
                    signal_confidence=sr.confidence, extras=sr.metadata,
                )

        snipe_valid_up = up_price < SETTINGS.sniper_extreme_lower
        snipe_valid_down = (1.0 - up_price) < SETTINGS.sniper_extreme_lower
        flash_results = get_flash_snipe_signal(
            _vel, up_price, down_price,
            snipe_valid_up, snipe_valid_down, SETTINGS,
        )
        for sr in flash_results:
            if (sr.side == "UP" and not suppress_up) or (sr.side == "DOWN" and not suppress_down):
                candidates[f"legacy_{sr.strategy_name}"] = _build_candidate(
                    base_result, side=sr.side, strategy_key=sr.strategy_name,
                    entry_price=sr.entry_price, signal_score=sr.signal_score,
                    signal_confidence=sr.confidence, extras=sr.metadata,
                )

    if not candidates:
        if fade_blocked_by_spot_delta:
            base_result["reason"] = base_result.get("_fade_block_reason") or "spot_delta_confirms_ofi"
            return base_result
        base_result["reason"] = f"edge_below_sniper_threshold (up={edge_up:.4f}, down={edge_down:.4f} < req={threshold:.4f})"
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
