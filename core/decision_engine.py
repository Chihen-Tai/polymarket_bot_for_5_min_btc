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


def mean_reversion_side(yes_price: Optional[float], yes_window: deque) -> Optional[str]:
    if yes_price is None or len(yes_window) < 5:
        return None
    vals = list(yes_window)
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / len(vals)
    std = var ** 0.5
    if std <= 1e-9:
        return None
    z = (yes_price - mean) / std
    if z > SETTINGS.zscore_threshold:
        return "DOWN"
    if z < -SETTINGS.zscore_threshold:
        return "UP"
    return None


def _has_momentum(side: str, up_window: deque, down_window: deque) -> bool:
    ticks = max(2, SETTINGS.momentum_ticks)
    target = list(up_window if side == "UP" else down_window)
    if len(target) < ticks:
        return True
    recent = target[-ticks:]
    move = recent[-1] - recent[0]
    return move >= SETTINGS.momentum_min_move


def explain_choose_side(
    market: dict, 
    yes_window: deque, 
    up_window: Optional[deque] = None, 
    down_window: Optional[deque] = None,
    binance_1m: Optional[dict] = None,
    binance_5m: Optional[list[dict]] = None,
    ob_up: Optional[dict] = None,
    ob_down: Optional[dict] = None,
    ws_bba: Optional[dict] = None,
    ws_trades: Optional[list[dict]] = None
) -> dict:
    prices = get_outcome_prices(market)
    up = prices.get("up") or prices.get("漲")
    down = prices.get("down") or prices.get("跌")
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
    if secs_left < SETTINGS.entry_window_min_sec:
        base_result["reason"] = "too_late_in_market"
        return base_result

    valid_up = up is not None and SETTINGS.min_entry_price <= float(up) <= SETTINGS.max_entry_price
    valid_down = down is not None and SETTINGS.min_entry_price <= float(down) <= SETTINGS.max_entry_price

    if not valid_up and not valid_down:
        base_result["reason"] = f"prices_out_of_bounds_up{up}_down{down}"
        return base_result

    candidates = {}

    # Strategy 1: Binance Oracle Front-running
    if SETTINGS.use_cex_oracle and binance_1m:
        change = binance_1m.get("change", 0.0)
        if change >= SETTINGS.cex_frontrun_threshold and valid_up:
            r = base_result.copy()
            r.update({"ok": True, "side": "UP", "reason": "model-cex_oracle_pump", "entry_price": up})
            candidates["cex_oracle_pump"] = r
        elif change <= -SETTINGS.cex_frontrun_threshold and valid_down:
            r = base_result.copy()
            r.update({"ok": True, "side": "DOWN", "reason": "model-cex_oracle_dump", "entry_price": down})
            candidates["cex_oracle_dump"] = r


    # Strategy 5: Zhihu ZLSMA + ATR Scalper
    if binance_5m and len(binance_5m) >= 99:
        try:
            closes = [c['close'] for c in binance_5m]
            zlsma = calc_zlsma(closes, length=50)
            chandelier_dir = calc_chandelier_exit(binance_5m, atr_period=1, mult=2.0)
            
            if zlsma is not None:
                current_close = closes[-1]
                if current_close > zlsma and chandelier_dir == 1 and valid_up:
                    r = base_result.copy()
                    r.update({"ok": True, "side": "UP", "reason": "model-zlsma_scalper_up", "entry_price": up})
                    candidates["zlsma_scalper_up"] = r
                elif current_close < zlsma and chandelier_dir == -1 and valid_down:
                    r = base_result.copy()
                    r.update({"ok": True, "side": "DOWN", "reason": "model-zlsma_scalper_down", "entry_price": down})
                    candidates["zlsma_scalper_down"] = r
        except Exception:
            pass

    # Strategy 6: WebSocket Order Flow Imbalance (OFI)
    if ws_trades:
        buy_vol, sell_vol = compute_buy_sell_pressure(ws_trades)
        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            ofi_ratio = buy_vol / total_vol
            # If > 0.70, strong buy pressure
            if ofi_ratio > 0.70 and valid_up:
                r = base_result.copy()
                r.update({"ok": True, "side": "UP", "reason": "model-ws_order_flow_up", "entry_price": up})
                candidates["ws_order_flow_up"] = r
            elif ofi_ratio < 0.30 and valid_down:
                r = base_result.copy()
                r.update({"ok": True, "side": "DOWN", "reason": "model-ws_order_flow_down", "entry_price": down})
                candidates["ws_order_flow_down"] = r

    # Strategy 7: WS Flash Snipe (WebSocket 閃電狙擊 0.3%)
    if getattr(SETTINGS, "ws_flash_snipe_threshold", 0.0) > 0 and ws_bba and "b" in ws_bba:
        try:
            from core.ws_binance import BINANCE_WS
            vel = BINANCE_WS.get_price_velocity(seconds=3.0)
            if vel > SETTINGS.ws_flash_snipe_threshold and valid_up:
                r = base_result.copy()
                r.update({"ok": True, "side": "UP", "reason": "model-ws_flash_snipe_up", "entry_price": up})
                candidates["ws_flash_snipe_up"] = r
            elif vel < -SETTINGS.ws_flash_snipe_threshold and valid_down:
                r = base_result.copy()
                r.update({"ok": True, "side": "DOWN", "reason": "model-ws_flash_snipe_down", "entry_price": down})
                candidates["ws_flash_snipe_down"] = r
        except Exception:
            pass

    # Mean Reversion
    mr = mean_reversion_side(up, yes_window)
    base_result["mr_side"] = mr
    if mr:
        side = mr
        entry_price = up if side == "UP" else down
        if (side == "UP" and valid_up) or (side == "DOWN" and valid_down):
            r = base_result.copy()
            r.update({"ok": True, "side": side, "reason": "model-mean_reversion_signal", "entry_price": entry_price})
            candidates["mean_reversion_signal"] = r


    # Apply Momentum Confirmation
    filtered_candidates = {}
    for name, s_result in candidates.items():
        if up_window is not None and down_window is not None:
            if not _has_momentum(s_result.get("side"), up_window, down_window):
                continue
        filtered_candidates[name] = s_result

    if not filtered_candidates:
        r = base_result.copy()
        r["reason"] = "no_valid_signals"
        return r

    # Learning Engine Scoreboard Integration
    from core.learning import SCOREBOARD
    best_decision = SCOREBOARD.get_best_strategy(filtered_candidates)
    if best_decision:
        return best_decision

    r = base_result.copy()
    r["reason"] = "no_best_strategy_found"
    return result


def choose_side(
    market: dict, 
    yes_window: deque, 
    up_window: Optional[deque] = None, 
    down_window: Optional[deque] = None,
    binance_1m: Optional[dict] = None,
    binance_5m: Optional[list[dict]] = None,
    ob_up: Optional[dict] = None,
    ob_down: Optional[dict] = None,
    ws_bba: Optional[dict] = None,
    ws_trades: Optional[list[dict]] = None
) -> Optional[str]:
    decision = explain_choose_side(market, yes_window, up_window, down_window, binance_1m, binance_5m, ob_up, ob_down, ws_bba, ws_trades)
    if not decision.get("ok"):
        return None
    return decision.get("side")
