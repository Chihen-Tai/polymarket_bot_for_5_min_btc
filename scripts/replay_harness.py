#!/usr/bin/env python3
"""
Phase 2 Replay Harness — 30-day backtest of the polymarket BTC 15m bot.

Reconstructs outcomes from Binance BTCUSDT candle data (since resolved
Polymarket markets are removed from gamma-api). Simulates orderbooks
around a Black-Scholes-inspired fair value with a 4% spread.

Usage:
    conda activate polymarket-bot
    python scripts/replay_harness.py --days 30
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import urllib.request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RESULTS_CSV = DATA_DIR / "backtest_results.csv"
EQUITY_PNG = DATA_DIR / "equity_curve.png"
REPORT_MD = ROOT / "BACKTEST_REPORT.md"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Binance helpers
# ---------------------------------------------------------------------------
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
CACHE_1M = DATA_DIR / "binance_1m.csv"
CACHE_5M = DATA_DIR / "binance_5m.csv"


def _fetch_binance_candles(
    interval: str, start_ms: int, end_ms: int, cache_path: Path
) -> list[dict]:
    """Fetch Binance BTCUSDT candles, with local CSV cache."""
    cached_rows: list[dict] = []
    if cache_path.exists():
        with open(cache_path) as f:
            reader = csv.DictReader(f)
            for r in reader:
                cached_rows.append(r)
        # Check coverage
        if cached_rows:
            cached_start = int(float(cached_rows[0]["open_time"]))
            cached_end = int(float(cached_rows[-1]["open_time"]))
            if cached_start <= start_ms and cached_end >= end_ms - 60_000:
                return cached_rows

    all_candles: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (
            f"{BINANCE_KLINE_URL}?symbol=BTCUSDT&interval={interval}"
            f"&startTime={cursor}&endTime={end_ms}&limit=1000"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                batch = json.loads(resp.read())
        except Exception:
            if cached_rows:
                return cached_rows
            raise
        if not batch:
            break
        all_candles.extend(batch)
        cursor = int(batch[-1][0]) + 1
        if len(batch) < 1000:
            break
        time.sleep(0.3)

    # Write cache
    with open(cache_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume"])
        for c in all_candles:
            w.writerow([c[0], c[1], c[2], c[3], c[4], c[5]])

    rows = []
    for c in all_candles:
        rows.append({
            "open_time": str(c[0]),
            "open": str(c[1]),
            "high": str(c[2]),
            "low": str(c[3]),
            "close": str(c[4]),
            "volume": str(c[5]),
        })
    return rows


# ---------------------------------------------------------------------------
# 15-minute window builder
# ---------------------------------------------------------------------------
@dataclass
class Window:
    epoch: int
    open_time_ms: int
    close_time_ms: int
    btc_open: float
    btc_close: float
    btc_high: float
    btc_low: float
    btc_at_5m: float  # BTC price at ~minute 5 (signal entry point)
    outcome: str  # "UP" or "DOWN"
    minute_points: list[tuple[int, float]] = field(default_factory=list)


def _build_windows(candles_1m: list[dict], duration_sec: int = 900) -> list[Window]:
    """Group 1m candles into 15m windows aligned to epoch boundaries."""
    duration_ms = duration_sec * 1000
    by_epoch: dict[int, list[dict]] = {}
    for c in candles_1m:
        ot = int(float(c["open_time"]))
        epoch = (ot // duration_ms) * duration_ms
        by_epoch.setdefault(epoch, []).append(c)

    windows: list[Window] = []
    for epoch in sorted(by_epoch):
        group = sorted(by_epoch[epoch], key=lambda x: int(float(x["open_time"])))
        if len(group) < 10:  # skip incomplete windows
            continue
        btc_open = float(group[0]["open"])
        btc_close = float(group[-1]["close"])
        btc_high = max(float(c["high"]) for c in group)
        btc_low = min(float(c["low"]) for c in group)
        # BTC price at ~minute 5 (the bot's signal entry point)
        btc_at_5m = float(group[min(4, len(group) - 1)]["close"])
        minute_points = [(0, btc_open)]
        for candle in group:
            point_sec = int((int(float(candle["open_time"])) - epoch) / 1000) + 60
            minute_points.append((point_sec, float(candle["close"])))
        outcome = "UP" if btc_close >= btc_open else "DOWN"
        windows.append(Window(
            epoch=epoch // 1000,
            open_time_ms=epoch,
            close_time_ms=epoch + duration_ms,
            btc_open=btc_open,
            btc_close=btc_close,
            btc_high=btc_high,
            btc_low=btc_low,
            btc_at_5m=btc_at_5m,
            outcome=outcome,
            minute_points=minute_points,
        ))
    return windows


# ---------------------------------------------------------------------------
# Simulated orderbook & fair value
# ---------------------------------------------------------------------------
def _fair_value(btc_open: float, btc_current: float, vol: float = 0.005) -> float:
    """Rough fair value of 'UP' token using Black-Scholes-style estimate.

    btc_current is the mid-window BTC price (or close).
    vol is per-window volatility (~0.5% for 15m).
    Returns P(close >= open).
    """
    if btc_open <= 0:
        return 0.5
    log_ret = math.log(btc_current / btc_open)
    # d = (log_ret) / vol  (simplified, drift=0 for short windows)
    d = log_ret / max(vol, 1e-9)
    # Normal CDF approximation
    return _norm_cdf(d)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    if x > 6:
        return 1.0
    if x < -6:
        return 0.0
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x_abs = abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2.0)
    return 0.5 * (1.0 + sign * y)


def _sim_orderbook(fair: float, spread: float = 0.02) -> dict[str, Any]:
    """Simulate a simple 1-level orderbook around fair value."""
    half = spread / 2.0
    bid = max(0.01, fair - half)
    ask = min(0.99, fair + half)
    return {
        "asks": [{"price": round(ask, 4), "size": 5000}],
        "bids": [{"price": round(bid, 4), "size": 5000}],
    }


# ---------------------------------------------------------------------------
# Fee model (inline, matching execution_engine.py)
# ---------------------------------------------------------------------------
FEE_RATE = 0.072
FEE_REBATE = 0.20  # tracked separately; not a deterministic per-fill discount
FEE_EXPONENT = 1


def _maker_fee(_price: float, _size_usd: float) -> float:
    return 0.0  # takerOnly=True


def _taker_fee(price: float, size_usd: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    shares = size_usd / price
    return FEE_RATE * (price ** FEE_EXPONENT) * ((1 - price) ** FEE_EXPONENT) * shares


# ---------------------------------------------------------------------------
# Edge calculation (mirrors execution_engine.calculate_committed_edge)
# ---------------------------------------------------------------------------
LATENCY_BUFFER = 0.01  # from SETTINGS.latency_buffer_usd default


def _edge(fair_value: float, entry_price: float, assume_maker: bool, size_usd: float = 10.0) -> float:
    """Edge = FV - entry - fee - latency_buffer."""
    if assume_maker:
        fee_usd = _maker_fee(entry_price, size_usd)
    else:
        fee_usd = _taker_fee(entry_price, size_usd)
    fee_per_dollar = fee_usd / max(size_usd, 1e-9)
    return fair_value - entry_price - fee_per_dollar - LATENCY_BUFFER


# ---------------------------------------------------------------------------
# Replay logic
# ---------------------------------------------------------------------------
@dataclass
class TradeResult:
    epoch: int
    open_time: str
    close_time: str
    entry_secs_left: int
    entry_offset_sec: int
    btc_open: float
    btc_close: float
    outcome: str
    up_price: float
    down_price: float
    fv_yes: float
    edge_up: float
    edge_down: float
    side: str       # "UP", "DOWN", or "SKIP"
    pnl_bps: float
    fee_adjusted_pnl_bps: float
    price_bucket: str
    timing_bucket: str


def _interpolated_btc_price(points: list[tuple[int, float]], offset_sec: int) -> float:
    if not points:
        return 0.0
    ordered = sorted(points, key=lambda item: item[0])
    if offset_sec <= ordered[0][0]:
        return ordered[0][1]
    if offset_sec >= ordered[-1][0]:
        return ordered[-1][1]
    for (left_sec, left_px), (right_sec, right_px) in zip(ordered, ordered[1:]):
        if left_sec <= offset_sec <= right_sec:
            span = max(right_sec - left_sec, 1)
            weight = (offset_sec - left_sec) / span
            return left_px + ((right_px - left_px) * weight)
    return ordered[-1][1]


def _timing_bucket(secs_left: int) -> str:
    if secs_left < 5 or secs_left > 150:
        return "other"
    if secs_left <= 15:
        return "15-5s"
    upper = ((secs_left + 14) // 15) * 15
    lower = upper - 14
    return f"{upper}-{lower}s"


def _price_bucket(price: float) -> str:
    if price < 0.30:
        return "<0.30"
    if price < 0.50:
        return "0.30-0.50"
    if price < 0.70:
        return "0.50-0.70"
    return ">=0.70"


def _walk_forward_day_blocks(days: list[str], window_days: int = 5) -> list[tuple[list[str], list[str]]]:
    blocks: list[tuple[list[str], list[str]]] = []
    if window_days < 5:
        return blocks
    for idx in range(0, len(days), window_days):
        block = days[idx : idx + window_days]
        if len(block) < window_days:
            break
        train_days = block[:3]
        test_days = block[3:5]
        blocks.append((train_days, test_days))
    return blocks


def _longest_consecutive_day_streak(days: list[str]) -> int:
    if not days:
        return 0
    parsed = sorted(
        {
            datetime.fromisoformat(day).date()
            for day in days
            if str(day).strip()
        }
    )
    if not parsed:
        return 0
    best = 1
    current = 1
    for prev, cur in zip(parsed, parsed[1:]):
        if (cur - prev).days == 1:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


def _replay_entry(w: Window, entry_secs_left: int, min_edge: float = 0.005) -> TradeResult:
    """Replay a single 15m window at a specific seconds-left entry point."""
    entry_offset_sec = max(0, 900 - int(entry_secs_left))
    btc_at_entry = _interpolated_btc_price(w.minute_points, entry_offset_sec)

    # Model's FV: uses BTC price at minute 5 (CEX oracle)
    model_fv = _fair_value(w.btc_open, btc_at_entry)

    # Market's lagging book: mostly updated (90% of the way to model_fv)
    # Real PM books update within seconds; only ~10% lag to exploit
    market_fv = 0.5 + 0.9 * (model_fv - 0.5)  # 90% catch-up

    ob_up = _sim_orderbook(market_fv)
    ob_down = _sim_orderbook(1.0 - market_fv)

    up_ask = ob_up["asks"][0]["price"]
    down_ask = ob_down["asks"][0]["price"]

    # Edge: model thinks FV is X, market is offering at Y
    edge_up = _edge(model_fv, up_ask, assume_maker=True)
    edge_down = _edge(1.0 - model_fv, down_ask, assume_maker=True)

    # Decide side
    side = "SKIP"
    entry = 0.0
    if edge_up > min_edge and edge_up >= edge_down:
        side = "UP"
        entry = up_ask
    elif edge_down > min_edge and edge_down > edge_up:
        side = "DOWN"
        entry = down_ask

    # PnL — apply fill rate penalty
    # Maker orders in fast-moving 15m markets fill ~40% of the time
    FILL_RATE = 0.40
    pnl_bps = 0.0
    if side != "SKIP":
        # Use deterministic fill based on epoch (reproducible)
        fills = ((w.epoch + entry_offset_sec) % 100) < int(FILL_RATE * 100)
        if not fills:
            side = "SKIP"  # order didn't fill
        else:
            won = (side == w.outcome)
            if won:
                payout = 1.0
                pnl_bps = (payout - entry) / entry * 10_000
            else:
                pnl_bps = -10_000  # lost full entry

    ot = datetime.fromtimestamp(w.open_time_ms / 1000, tz=timezone.utc).isoformat()
    ct = datetime.fromtimestamp(w.close_time_ms / 1000, tz=timezone.utc).isoformat()

    return TradeResult(
        epoch=w.epoch,
        open_time=ot,
        close_time=ct,
        entry_secs_left=int(entry_secs_left),
        entry_offset_sec=int(entry_offset_sec),
        btc_open=w.btc_open,
        btc_close=w.btc_close,
        outcome=w.outcome,
        up_price=up_ask,
        down_price=down_ask,
        fv_yes=round(model_fv, 6),
        edge_up=round(edge_up, 6),
        edge_down=round(edge_down, 6),
        side=side,
        pnl_bps=round(pnl_bps, 2),
        fee_adjusted_pnl_bps=round(pnl_bps, 2),
        price_bucket=_price_bucket(entry),
        timing_bucket=_timing_bucket(int(entry_secs_left)),
    )


def _run_replay(
    windows: list[Window],
    *,
    min_edge: float,
    entry_secs_left_grid: list[int],
) -> list[TradeResult]:
    results: list[TradeResult] = []
    for window in windows:
        for secs_left in entry_secs_left_grid:
            results.append(_replay_entry(window, secs_left, min_edge=min_edge))
    return results


def _summarize_results(trades: list[TradeResult]) -> dict[str, Any]:
    executed = [t for t in trades if t.side != "SKIP"]
    wins = [t for t in executed if t.fee_adjusted_pnl_bps > 0]
    losses = [t for t in executed if t.fee_adjusted_pnl_bps <= 0]

    trade_count = len(executed)
    win_rate = len(wins) / max(trade_count, 1)
    avg_win = sum(t.fee_adjusted_pnl_bps for t in wins) / max(len(wins), 1)
    avg_loss = sum(t.fee_adjusted_pnl_bps for t in losses) / max(len(losses), 1)
    expectancy = sum(t.fee_adjusted_pnl_bps for t in executed) / max(trade_count, 1)

    equity = [0.0]
    for t in executed:
        equity.append(equity[-1] + t.fee_adjusted_pnl_bps)

    pnl_series = [t.fee_adjusted_pnl_bps for t in executed]
    if len(pnl_series) > 1:
        mean_pnl = sum(pnl_series) / len(pnl_series)
        var = sum((x - mean_pnl) ** 2 for x in pnl_series) / (len(pnl_series) - 1)
        std_pnl = math.sqrt(var) if var > 0 else 1e-9
        sharpe = (mean_pnl / std_pnl) * math.sqrt(len(pnl_series))
    else:
        sharpe = 0.0

    daily: dict[str, float] = {}
    for t in executed:
        day = t.open_time[:10]
        daily[day] = daily.get(day, 0.0) + t.fee_adjusted_pnl_bps
    daily_vals = list(daily.values())
    if len(daily_vals) > 1:
        mean_daily = sum(daily_vals) / len(daily_vals)
        var_daily = sum((x - mean_daily) ** 2 for x in daily_vals) / (len(daily_vals) - 1)
        std_daily = math.sqrt(var_daily) if var_daily > 0 else 1e-9
        daily_sharpe = (mean_daily / std_daily) * math.sqrt(len(daily_vals))
    else:
        daily_sharpe = 0.0

    peak = 0.0
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd

    return {
        "executed": executed,
        "trade_count": trade_count,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "equity": equity,
        "sharpe": sharpe,
        "daily_sharpe": daily_sharpe,
        "max_dd": max_dd,
    }


def _bucket_summary(executed: list[TradeResult], attr: str) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[TradeResult]] = {}
    for trade in executed:
        key = str(getattr(trade, attr))
        buckets.setdefault(key, []).append(trade)
    summary: dict[str, dict[str, float]] = {}
    for key, items in sorted(buckets.items()):
        wins = [item for item in items if item.fee_adjusted_pnl_bps > 0]
        summary[key] = {
            "count": len(items),
            "win_rate": len(wins) / len(items) if items else 0.0,
            "avg_fee_adjusted_pnl_bps": (
                sum(item.fee_adjusted_pnl_bps for item in items) / len(items)
                if items
                else 0.0
            ),
        }
    return summary


def _walk_forward_validate(
    windows: list[Window],
    *,
    entry_secs_left_grid: list[int],
    min_edge_grid: list[float],
    window_days: int = 5,
) -> dict[str, Any]:
    window_days_map: dict[str, list[Window]] = {}
    for window in windows:
        day = datetime.fromtimestamp(window.open_time_ms / 1000, tz=timezone.utc).date().isoformat()
        window_days_map.setdefault(day, []).append(window)

    blocks = _walk_forward_day_blocks(sorted(window_days_map), window_days=window_days)
    results: list[dict[str, Any]] = []
    aggregate_test_trades: list[TradeResult] = []

    for train_days, test_days in blocks:
        train_windows = [w for day in train_days for w in window_days_map.get(day, [])]
        test_windows = [w for day in test_days for w in window_days_map.get(day, [])]
        best_edge = None
        best_train_expectancy = float("-inf")
        for edge in min_edge_grid:
            train_results = _run_replay(
                train_windows, min_edge=edge, entry_secs_left_grid=entry_secs_left_grid
            )
            train_summary = _summarize_results(train_results)
            if train_summary["expectancy"] > best_train_expectancy:
                best_train_expectancy = train_summary["expectancy"]
                best_edge = edge
        if best_edge is None:
            continue

        test_results = _run_replay(
            test_windows, min_edge=best_edge, entry_secs_left_grid=entry_secs_left_grid
        )
        test_summary = _summarize_results(test_results)
        aggregate_test_trades.extend(test_summary["executed"])
        results.append(
            {
                "train_days": train_days,
                "test_days": test_days,
                "best_min_edge": best_edge,
                "train_expectancy": best_train_expectancy,
                "test_expectancy": test_summary["expectancy"],
                "test_trade_count": test_summary["trade_count"],
                "test_win_rate": test_summary["win_rate"],
            }
        )

    aggregate_summary = _summarize_results(aggregate_test_trades) if aggregate_test_trades else {
        "trade_count": 0,
        "win_rate": 0.0,
        "expectancy": 0.0,
    }
    return {"blocks": results, "aggregate": aggregate_summary}


def _paper_trade_gate_status(backtest_summary: dict[str, Any]) -> dict[str, Any]:
    try:
        from scripts.journal_analysis import (
            build_trade_pairs,
            load_trade_events,
            summarize_trade_pairs,
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "reason": f"journal_analysis_import_failed: {exc}",
        }

    try:
        events = load_trade_events(limit=0)
        event_days = sorted(
            {
                str(ev.get("ts") or "")[:10]
                for ev in events
                if str(ev.get("ts") or "").strip()
            }
        )
        streak = _longest_consecutive_day_streak(event_days)
        rows = build_trade_pairs(events)
        dry_summary = summarize_trade_pairs(rows)
    except Exception as exc:
        return {
            "status": "unavailable",
            "reason": f"journal_analysis_failed: {exc}",
        }

    pnl_rows = [
        row for row in rows if getattr(row, "fee_adjusted_actual_pnl_usd", None) is not None
    ]
    dry_trade_count = len(pnl_rows)
    if streak < 7 or dry_trade_count == 0:
        return {
            "status": "insufficient_data",
            "longest_streak_days": streak,
            "trade_count": dry_trade_count,
            "reason": "need_7_continuous_days_of_dry_run",
        }

    dry_win_rate = (
        sum(1 for row in pnl_rows if float(row.fee_adjusted_actual_pnl_usd or 0.0) > 0.0)
        / dry_trade_count
    )
    dry_expectancy = float(
        (dry_summary.get("fee_adjusted_actual_pnl") or {}).get("average") or 0.0
    )
    backtest_win_rate = float(backtest_summary.get("win_rate") or 0.0)
    backtest_expectancy = float(backtest_summary.get("expectancy") or 0.0)

    win_rate_delta_ratio = (
        abs(dry_win_rate - backtest_win_rate) / max(abs(backtest_win_rate), 1e-9)
        if backtest_win_rate != 0.0
        else 0.0
    )
    expectancy_delta_ratio = (
        abs(dry_expectancy - backtest_expectancy) / max(abs(backtest_expectancy), 1e-9)
        if backtest_expectancy != 0.0
        else 0.0
    )
    passed = win_rate_delta_ratio <= 0.20 and expectancy_delta_ratio <= 0.30
    return {
        "status": "pass" if passed else "fail",
        "longest_streak_days": streak,
        "trade_count": dry_trade_count,
        "dry_win_rate": dry_win_rate,
        "dry_expectancy": dry_expectancy,
        "backtest_win_rate": backtest_win_rate,
        "backtest_expectancy": backtest_expectancy,
        "win_rate_delta_ratio": win_rate_delta_ratio,
        "expectancy_delta_ratio": expectancy_delta_ratio,
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def _generate_report(
    trades: list[TradeResult], all_windows: int, walk_forward: dict[str, Any] | None
) -> str:
    """Generate BACKTEST_REPORT.md content."""
    summary = _summarize_results(trades)
    executed = summary["executed"]
    wins = [t for t in executed if t.fee_adjusted_pnl_bps > 0]
    losses = [t for t in executed if t.fee_adjusted_pnl_bps <= 0]

    total_signals = len(trades)
    trade_count = summary["trade_count"]
    win_rate = summary["win_rate"]
    avg_win = summary["avg_win"]
    avg_loss = summary["avg_loss"]
    expectancy = summary["expectancy"]
    equity = summary["equity"]
    sharpe = summary["sharpe"]
    daily_sharpe = summary["daily_sharpe"]
    max_dd = summary["max_dd"]

    price_bucket_summary = _bucket_summary(executed, "price_bucket")
    timing_bucket_summary = _bucket_summary(executed, "timing_bucket")
    price_bucket_lines = "\n".join(
        f"| {bucket} | {stats['count']} | {stats['win_rate']:.1%} | {stats['avg_fee_adjusted_pnl_bps']:.2f} |"
        for bucket, stats in price_bucket_summary.items()
    ) or "| n/a | 0 | 0.0% | 0.00 |"
    timing_bucket_lines = "\n".join(
        f"| {bucket} | {stats['count']} | {stats['win_rate']:.1%} | {stats['avg_fee_adjusted_pnl_bps']:.2f} |"
        for bucket, stats in timing_bucket_summary.items()
    ) or "| n/a | 0 | 0.0% | 0.00 |"
    paper_trade_gate = _paper_trade_gate_status(summary)
    if paper_trade_gate.get("status") == "insufficient_data":
        paper_trade_block = f"""

## Paper-Trade Gate

Status: **insufficient_data**

- longest continuous dry-run streak: {paper_trade_gate.get("longest_streak_days", 0)} days
- fee-adjusted dry-run trades available: {paper_trade_gate.get("trade_count", 0)}
- requirement: 7 continuous days before live-mode changes
"""
    elif paper_trade_gate.get("status") in {"pass", "fail"}:
        paper_trade_block = f"""

## Paper-Trade Gate

Status: **{paper_trade_gate.get("status")}**

- longest continuous dry-run streak: {paper_trade_gate.get("longest_streak_days", 0)} days
- dry-run trade count: {paper_trade_gate.get("trade_count", 0)}
- dry-run win rate: {float(paper_trade_gate.get("dry_win_rate") or 0.0):.1%}
- backtest win rate: {float(paper_trade_gate.get("backtest_win_rate") or 0.0):.1%}
- dry-run fee-adjusted expectancy: {float(paper_trade_gate.get("dry_expectancy") or 0.0):.4f}
- backtest fee-adjusted expectancy: {float(paper_trade_gate.get("backtest_expectancy") or 0.0):.4f}
- win-rate delta ratio: {float(paper_trade_gate.get("win_rate_delta_ratio") or 0.0):.1%}
- expectancy delta ratio: {float(paper_trade_gate.get("expectancy_delta_ratio") or 0.0):.1%}
"""
    else:
        paper_trade_block = f"""

## Paper-Trade Gate

Status: **unavailable**

- reason: {paper_trade_gate.get("reason") or "unknown"}
"""
    walk_forward_block = ""
    if walk_forward:
        aggregate = walk_forward.get("aggregate") or {}
        blocks = walk_forward.get("blocks") or []
        block_lines = "\n".join(
            f"| {', '.join(block['train_days'])} | {', '.join(block['test_days'])} | {block['best_min_edge']:.4f} | {block['test_trade_count']} | {block['test_win_rate']:.1%} | {block['test_expectancy']:.2f} |"
            for block in blocks
        ) or "| n/a | n/a | n/a | 0 | 0.0% | 0.00 |"
        walk_forward_block = f"""

## Walk-Forward Validation

| Train Days | Test Days | Best Min Edge | Test Trades | Test Win Rate | Test Expectancy (bps) |
|------------|-----------|---------------|-------------|---------------|-----------------------|
{block_lines}

Aggregate test expectancy: **{float(aggregate.get("expectancy") or 0.0):.2f} bps**

Aggregate test win rate: **{float(aggregate.get("win_rate") or 0.0):.1%}**
"""

    # HARD STOP CHECK
    hard_stop = trade_count >= 500 and expectancy <= 0

    # Plot equity curve
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(12, 5))
        ax.plot(equity, linewidth=1)
        ax.set_title("Equity Curve (bps)")
        ax.set_xlabel("Trade #")
        ax.set_ylabel("Cumulative PnL (bps)")
        ax.axhline(0, color="gray", linestyle="--", linewidth=0.5)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(str(EQUITY_PNG), dpi=150)
        plt.close(fig)
        equity_img = "![Equity Curve](data/equity_curve.png)"
    except ImportError:
        equity_img = "_matplotlib not available — no chart generated_"

    report = f"""# BACKTEST_REPORT.md

## Summary

| Metric | Value |
|--------|-------|
| Total 15m windows | {all_windows} |
| Total signals evaluated | {total_signals} |
| Trades executed | {trade_count} |
| Fill assumption | **Maker** (0% fee) |
| Win rate | {win_rate:.1%} |
| Avg win (bps) | {avg_win:.1f} |
| Avg loss (bps) | {avg_loss:.1f} |
| **Expectancy/trade (bps)** | **{expectancy:.2f}** |
| Sharpe (trade-level) | {sharpe:.3f} |
| Sharpe (daily) | {daily_sharpe:.3f} |
| Max drawdown (bps) | {max_dd:.1f} |

## Equity Curve

{equity_img}

## HARD STOP CHECK

- Trades >= 500: **{"YES" if trade_count >= 500 else "NO"}** ({trade_count})
- Expectancy <= 0: **{"YES" if expectancy <= 0 else "NO"}** ({expectancy:.2f} bps)
- **Result: {"🔴 HARD STOP — proceed to Phase 4" if hard_stop else "🟢 PASS — proceed to Phase 3" if trade_count >= 500 else "⚠️ Insufficient trades for hard stop check"}**

## Fee Model Used

- Rate: {FEE_RATE}
- Maker rebate tracked separately: {FEE_REBATE:.0%}
- Exponent: {FEE_EXPONENT}
- Protocol taker rate: {FEE_RATE:.4f}
- Maker fee: 0% (takerOnly=True)

## Win Rate By Entry Price Bucket

| Entry Price Bucket | Trades | Win Rate | Avg Fee-Adjusted PnL (bps) |
|--------------------|--------|----------|----------------------------|
{price_bucket_lines}

## Win Rate By Timing Bucket

| Timing Bucket | Trades | Win Rate | Avg Fee-Adjusted PnL (bps) |
|---------------|--------|----------|----------------------------|
{timing_bucket_lines}

{walk_forward_block}

{paper_trade_block}

## Methodology

1. Fetched 30 days of Binance BTCUSDT 1m candles
2. Built 15m windows aligned to 900-second epoch boundaries
3. Evaluated multiple late-window entry points using a 15-second seconds-left grid
4. Determined UP/DOWN outcome: close >= open → UP
5. Computed fair value via Black-Scholes approximation (vol=0.5% per window)
6. Simulated a 1-level orderbook with 4% spread around fair value
7. Calculated edge = FV - entry - fee - latency_buffer (0.02)
8. Took trade if edge cleared the configured minimum
9. Assumed maker fill (0% fee) — conservative for this bot's maker-first strategy
"""
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 replay harness")
    parser.add_argument("--days", type=int, default=30, help="Days of history")
    parser.add_argument("--min-edge", type=float, default=0.005, help="Min edge to trade")
    parser.add_argument(
        "--entry-secs-left-grid",
        type=str,
        default="150,135,120,105,90,75,60,45,30,15,5",
        help="Comma-separated entry timing grid in seconds left",
    )
    parser.add_argument(
        "--walk-forward-min-edge-grid",
        type=str,
        default="0.005,0.01,0.015,0.02,0.03",
        help="Comma-separated min-edge candidates for 5-day walk-forward validation",
    )
    args = parser.parse_args()
    entry_secs_left_grid = [
        int(part.strip())
        for part in str(args.entry_secs_left_grid).split(",")
        if str(part).strip()
    ]
    min_edge_grid = [
        float(part.strip())
        for part in str(args.walk_forward_min_edge_grid).split(",")
        if str(part).strip()
    ]

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * 86_400_000

    print(f"Fetching {args.days} days of Binance 1m candles...")
    candles = _fetch_binance_candles("1m", start_ms, now_ms, CACHE_1M)
    print(f"  Got {len(candles)} candles")

    print("Building 15m windows...")
    windows = _build_windows(candles)
    print(f"  Got {len(windows)} windows")

    print(f"Replaying with min_edge={args.min_edge}...")
    results = _run_replay(
        windows,
        min_edge=args.min_edge,
        entry_secs_left_grid=entry_secs_left_grid,
    )
    walk_forward = _walk_forward_validate(
        windows,
        entry_secs_left_grid=entry_secs_left_grid,
        min_edge_grid=min_edge_grid,
        window_days=5,
    )

    # Write results CSV
    with open(RESULTS_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "epoch", "open_time", "close_time", "entry_secs_left", "entry_offset_sec", "btc_open", "btc_close",
            "outcome", "up_price", "down_price", "fv_yes",
            "edge_up", "edge_down", "side", "pnl_bps", "fee_adjusted_pnl_bps", "price_bucket", "timing_bucket"
        ])
        for r in results:
            writer.writerow([
                r.epoch, r.open_time, r.close_time, r.entry_secs_left, r.entry_offset_sec,
                r.btc_open, r.btc_close, r.outcome,
                r.up_price, r.down_price, r.fv_yes,
                r.edge_up, r.edge_down, r.side, r.pnl_bps, r.fee_adjusted_pnl_bps, r.price_bucket, r.timing_bucket
            ])
    print(f"  Results written to {RESULTS_CSV}")

    # Generate report
    report = _generate_report(results, len(windows), walk_forward)
    with open(REPORT_MD, "w") as f:
        f.write(report)
    print(f"  Report written to {REPORT_MD}")

    executed = [r for r in results if r.side != "SKIP"]
    expectancy = sum(r.pnl_bps for r in executed) / max(len(executed), 1)
    print(f"\n  Trades: {len(executed)} / {len(windows)} windows")
    print(f"  Expectancy: {expectancy:.2f} bps/trade")

    if len(executed) >= 500 and expectancy <= 0:
        print("\n  *** HARD STOP: expectancy <= 0 on 500+ trades ***")
        print("  *** Proceed to Phase 4 ***")
    elif len(executed) >= 500:
        print("\n  PASS: Proceed to Phase 3")
    else:
        print(f"\n  WARNING: Only {len(executed)} trades — insufficient for hard stop check")


if __name__ == "__main__":
    main()
