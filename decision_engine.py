from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from config import SETTINGS


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
    ob_up: Optional[dict] = None,
    ob_down: Optional[dict] = None
) -> dict:
    prices = get_outcome_prices(market)
    up = prices.get("up") or prices.get("漲")
    down = prices.get("down") or prices.get("跌")
    secs_left = seconds_to_market_end(market)
    result = {
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
        result["reason"] = "missing_prices"
        return result

    if secs_left is None:
        result["reason"] = "missing_end_time"
        return result
    if secs_left < SETTINGS.entry_window_min_sec:
        result["reason"] = "too_late_in_market"
        return result
    if secs_left > SETTINGS.entry_window_max_sec:
        result["reason"] = "too_early_in_market"
        return result

    # Strategy 1: Binance Oracle Front-running
    if SETTINGS.use_cex_oracle and binance_1m:
        change = binance_1m.get("change", 0.0)
        if change >= SETTINGS.cex_frontrun_threshold:
            result["ok"] = True
            result["side"] = "UP"
            result["reason"] = "cex_oracle_pump"
            result["entry_price"] = up
            return result
        elif change <= -SETTINGS.cex_frontrun_threshold:
            result["ok"] = True
            result["side"] = "DOWN"
            result["reason"] = "cex_oracle_dump"
            result["entry_price"] = down
            return result

    # Strategy 4: Orderbook Imbalance
    if SETTINGS.use_ob_imbalance and ob_up and ob_down:
        up_bid_ratio = _check_imbalance(ob_up)
        down_bid_ratio = _check_imbalance(ob_down)
        if up_bid_ratio >= SETTINGS.imbalance_threshold:
            result["ok"] = True
            result["side"] = "UP"
            result["reason"] = "orderbook_imbalance_up"
            result["entry_price"] = up
            return result
        elif down_bid_ratio >= SETTINGS.imbalance_threshold:
            result["ok"] = True
            result["side"] = "DOWN"
            result["reason"] = "orderbook_imbalance_down"
            result["entry_price"] = down
            return result

    mr = mean_reversion_side(up, yes_window)
    result["mr_side"] = mr
    if mr:
        side = mr
        result["reason"] = "mean_reversion_signal"
    else:
        spread = abs(up - down)
        result["spread"] = spread
        min_spread = max(SETTINGS.edge_threshold, SETTINGS.fee_buffer)
        if spread < min_spread:
            result["reason"] = "edge_below_threshold"
            return result
        side = "DOWN" if up > down else "UP"
        result["reason"] = "spread_signal"

    entry_price = up if side == "UP" else down
    result["entry_price"] = entry_price
    if entry_price is None:
        result["reason"] = "missing_entry_price"
        return result
    if entry_price < SETTINGS.min_entry_price:
        result["reason"] = "entry_price_below_min"
        return result
    if entry_price > SETTINGS.max_entry_price:
        result["reason"] = "entry_price_above_max"
        return result

    if up_window is not None and down_window is not None:
        if not _has_momentum(side, up_window, down_window):
            result["reason"] = "momentum_not_confirmed"
            result["side"] = side
            return result

    result["ok"] = True
    result["side"] = side
    result["reason"] = "ok"
    return result


def choose_side(
    market: dict, 
    yes_window: deque, 
    up_window: Optional[deque] = None, 
    down_window: Optional[deque] = None,
    binance_1m: Optional[dict] = None,
    ob_up: Optional[dict] = None,
    ob_down: Optional[dict] = None
) -> Optional[str]:
    decision = explain_choose_side(market, yes_window, up_window, down_window, binance_1m, ob_up, ob_down)
    if not decision.get("ok"):
        return None
    return decision.get("side")
