from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collections import defaultdict, deque, Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.config import SETTINGS
from core.journal import read_events


EPS = 1e-9
_SETTLEMENT_CACHE: dict[tuple[str, str], tuple[float | None, str | None]] = {}
_ACTIVITY_CACHE: dict[tuple[str, str], list[dict]] = {}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _maybe_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _event_ts(ev: dict) -> datetime | None:
    raw = str(ev.get("ts") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _parse_iso_dt(text: str | None) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone().replace(tzinfo=dt.astimezone().tzinfo)
    return dt.astimezone(timezone.utc)


def _event_pair_key(ev: dict) -> str:
    position_id = str(ev.get("position_id") or "").strip()
    if position_id:
        return position_id
    return str(ev.get("token_id") or "").strip()


def _coerce_list(v: Any) -> list[Any]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    return v if isinstance(v, list) else []


def _market_end_dt_from_slug(slug: str | None) -> datetime | None:
    text = str(slug or "").strip()
    if not text:
        return None
    try:
        start_epoch = int(text.split("-")[-1])
        # Abstracted duration from SETTINGS
        duration = float(SETTINGS.market_duration_sec)
    except Exception:
        return None
    return datetime.fromtimestamp(start_epoch + duration, tz=timezone.utc)


def _fetch_market_settlement(
    slug: str | None, side: str | None
) -> tuple[float | None, str | None]:
    slug_text = str(slug or "").strip()
    side_text = str(side or "").strip().lower()
    cache_key = (slug_text, side_text)
    if cache_key in _SETTLEMENT_CACHE:
        return _SETTLEMENT_CACHE[cache_key]

    end_dt = _market_end_dt_from_slug(slug_text)
    if end_dt is None or datetime.now(timezone.utc) < end_dt:
        _SETTLEMENT_CACHE[cache_key] = (None, None)
        return _SETTLEMENT_CACHE[cache_key]

    try:
        import requests

        response = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug_text},
            timeout=8,
        )
        response.raise_for_status()
        markets = response.json() or []
        if not markets:
            raise ValueError("market not found")
        market = markets[0]
        outcomes = _coerce_list(market.get("outcomes"))
        prices = _coerce_list(market.get("outcomePrices"))
        price_by_outcome: dict[str, float] = {}
        for idx, outcome in enumerate(outcomes):
            if idx >= len(prices):
                break
            try:
                price_by_outcome[str(outcome or "").strip().lower()] = float(
                    prices[idx]
                )
            except Exception:
                continue
        settlement_price = price_by_outcome.get(side_text)
        if settlement_price is None:
            raise ValueError("settlement price unavailable")
        if settlement_price >= 0.99:
            settlement_reason = "market-expired-binary-win"
        elif settlement_price <= 0.01:
            settlement_reason = "market-expired-binary-loss"
        else:
            settlement_reason = "market-expired-settlement"
        _SETTLEMENT_CACHE[cache_key] = (settlement_price, settlement_reason)
    except Exception:
        _SETTLEMENT_CACHE[cache_key] = (None, None)

    return _SETTLEMENT_CACHE[cache_key]


def _fetch_account_trade_activity(
    *, user: str | None, market: str | None = None, limit: int = 200
) -> list[dict]:
    user_text = str(user or "").strip()
    market_text = str(market or "").strip()
    if not user_text:
        return []

    cache_key = (user_text, market_text)
    if cache_key in _ACTIVITY_CACHE:
        return _ACTIVITY_CACHE[cache_key]

    page_size = max(1, min(int(limit or 200), 500))
    offset = 0
    out: list[dict] = []
    try:
        import requests

        while len(out) < limit:
            params: dict[str, Any] = {
                "user": user_text,
                "type": "TRADE",
                "limit": min(page_size, max(1, limit - len(out))),
                "offset": offset,
            }
            if market_text:
                params["market"] = market_text
            resp = requests.get(
                f"{SETTINGS.data_api_host}/activity",
                params=params,
                timeout=8,
            )
            resp.raise_for_status()
            batch = resp.json() or []
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < params["limit"]:
                break
            offset += len(batch)
    except Exception:
        out = []

    out.sort(key=lambda row: _f(row.get("timestamp"), 0.0))
    _ACTIVITY_CACHE[cache_key] = out
    return out


def _build_activity_sell_ledger(activity_rows: list[dict]) -> list[dict]:
    inventories: dict[str, deque[dict[str, float | str]]] = defaultdict(deque)
    sells: list[dict] = []
    for row in sorted(
        activity_rows or [], key=lambda item: _f(item.get("timestamp"), 0.0)
    ):
        if str(row.get("type") or "").upper() != "TRADE":
            continue
        asset = str(row.get("asset") or "").strip()
        trade_side = str(row.get("side") or "").upper().strip()
        slug = str(row.get("slug") or "").strip()
        size = _f(row.get("size"), 0.0)
        usdc_size = _f(row.get("usdcSize"), 0.0)
        if not asset or size <= EPS:
            continue
        if trade_side == "BUY":
            inventories[asset].append(
                {
                    "remaining_shares": size,
                    "remaining_cost_usd": usdc_size,
                    "slug": slug,
                }
            )
            continue
        if trade_side != "SELL":
            continue

        remaining = size
        matched_cost_usd = 0.0
        lots = inventories.get(asset) or deque()
        while lots and remaining > EPS:
            lot = lots[0]
            lot_shares = _f(lot.get("remaining_shares"), 0.0)
            lot_cost = _f(lot.get("remaining_cost_usd"), 0.0)
            if lot_shares <= EPS:
                lots.popleft()
                continue
            matched = min(lot_shares, remaining)
            cost_piece = lot_cost * (matched / lot_shares) if lot_cost > EPS else 0.0
            lot["remaining_shares"] = max(0.0, lot_shares - matched)
            lot["remaining_cost_usd"] = max(0.0, lot_cost - cost_piece)
            matched_cost_usd += cost_piece
            remaining -= matched
            if _f(lot.get("remaining_shares"), 0.0) <= EPS:
                lots.popleft()

        sells.append(
            {
                "asset": asset,
                "slug": slug,
                "timestamp": _f(row.get("timestamp"), 0.0),
                "ts": datetime.fromtimestamp(
                    _f(row.get("timestamp"), 0.0), tz=timezone.utc
                ).isoformat()
                if _f(row.get("timestamp"), 0.0) > 0
                else "",
                "shares": size,
                "usdc_size": usdc_size,
                "matched_cost_usd": matched_cost_usd
                if matched_cost_usd > EPS
                else None,
                "transaction_hash": str(row.get("transactionHash") or ""),
            }
        )
    return sells


def reconcile_rows_with_account_activity(
    rows: list[TradePairRow],
    *,
    user: str | None = None,
    activity_fetcher=None,
) -> list[TradePairRow]:
    user_text = str(user or getattr(SETTINGS, "funder_address", "") or "").strip()
    if not rows or not user_text:
        return rows

    fetcher = activity_fetcher or _fetch_account_trade_activity
    sells_cache: dict[str, list[dict]] = {}

    for row in rows:
        is_orphan_residual = row.status == "residual" and "orphan-residual" in row.flags
        is_settlement_imputed = (
            row.actual_source == "market-settlement-lookup"
            or "market-settlement-imputed" in row.flags
        )
        if not (is_orphan_residual or is_settlement_imputed):
            continue
        market = str(row.market or "").strip()
        if not market or not row.token_id:
            continue
        if market not in sells_cache:
            activity_rows = fetcher(user=user_text, market=market)
            sells_cache[market] = _build_activity_sell_ledger(activity_rows)

        candidates = []
        row_dt = _parse_iso_dt(row.closed_ts)
        target_shares = (
            row.matched_exit_shares
            if row.matched_exit_shares > EPS
            else row.entry_shares
        )
        ref_exit_value = (
            row.exit_recovered_actual_usd
            if row.exit_recovered_actual_usd is not None
            else row.exit_recovered_observed_usd
        )
        for sell in sells_cache.get(market, []):
            if str(sell.get("asset") or "") != row.token_id:
                continue
            matched_cost_usd = _maybe_float(sell.get("matched_cost_usd"))
            if matched_cost_usd is None:
                continue
            shares_diff = abs(_f(sell.get("shares"), 0.0) - target_shares)
            if target_shares > EPS and shares_diff > max(0.05, target_shares * 0.05):
                continue
            value_diff = abs(
                (_maybe_float(sell.get("usdc_size")) or 0.0) - (ref_exit_value or 0.0)
            )
            sell_dt = _parse_iso_dt(str(sell.get("ts") or ""))
            time_diff = (
                abs((sell_dt - row_dt).total_seconds())
                if sell_dt is not None and row_dt is not None
                else 0.0
            )
            if row_dt is not None and sell_dt is not None and time_diff > 3600:
                continue
            if is_settlement_imputed:
                candidates.append((shares_diff, time_diff, value_diff, sell))
            else:
                candidates.append((shares_diff, value_diff, time_diff, sell))

        if not candidates:
            continue

        _, _, _, best = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        matched_cost = _maybe_float(best.get("matched_cost_usd"))
        if matched_cost is None or matched_cost <= EPS:
            continue

        if row.entry_cost_usd <= EPS:
            row.entry_cost_usd = matched_cost
        if row.entry_shares <= EPS:
            row.entry_shares = _f(best.get("shares"), target_shares)
        if is_settlement_imputed:
            row.matched_exit_shares = _f(best.get("shares"), target_shares)
            row.exit_recovered_actual_usd = _maybe_float(best.get("usdc_size"))
            row.exit_recovered_observed_usd = row.exit_recovered_actual_usd
        row.actual_pnl_usd = (
            (row.exit_recovered_actual_usd - row.entry_cost_usd)
            if row.exit_recovered_actual_usd is not None
            else None
        )
        row.observed_pnl_usd = (
            (row.exit_recovered_observed_usd - row.entry_cost_usd)
            if row.exit_recovered_observed_usd is not None
            else None
        )
        _, actual_total_fees, observed_total_fees, _ = estimate_pair_fees(
            matched_cost_usd=row.entry_cost_usd,
            matched_shares=row.entry_shares,
            actual_exit_value_usd=row.exit_recovered_actual_usd,
            observed_exit_value_usd=row.exit_recovered_observed_usd,
            entry_execution_style=row.entry_execution_style,
            exit_execution_style=row.exit_execution_style,
            close_reason=row.close_reason,
        )
        row.estimated_total_fees_actual_usd = actual_total_fees
        row.estimated_total_fees_observed_usd = observed_total_fees
        row.fee_adjusted_actual_pnl_usd = (
            (row.actual_pnl_usd - actual_total_fees)
            if row.actual_pnl_usd is not None and actual_total_fees is not None
            else None
        )
        row.fee_adjusted_observed_pnl_usd = (
            (row.observed_pnl_usd - observed_total_fees)
            if row.observed_pnl_usd is not None and observed_total_fees is not None
            else None
        )
        row.actual_source = "account-activity-reconcile"
        row.actual_source_tier = (
            "high"
            if row.exit_recovered_actual_usd is not None
            else row.actual_source_tier
        )
        row.flags = list(
            dict.fromkeys(
                [
                    flag
                    for flag in row.flags
                    if flag
                    not in {"ui-reconciliation-needed", "market-settlement-imputed"}
                ]
                + ["account-activity-reconciled-leg"]
            )
        )

    return rows


@dataclass
class ExitAccountingRow:
    ts: str
    event_id: str
    position_id: str
    market: str
    side: str
    reason: str
    entry_quality: str
    closed_shares: float
    remaining_shares: float | None
    realized_cost_usd: float
    actual_exit_value_usd: float | None
    observed_exit_value_usd: float | None
    actual_source: str
    actual_source_tier: str
    observed_source: str
    difference_usd: float | None
    difference_pct_of_cost: float | None
    actual_status: str
    mae_pnl_usd: float | None
    mfe_pnl_usd: float | None
    flags: list[str]


@dataclass
class TradeLeg:
    ts: str
    event_id: str
    kind: str
    shares: float
    cost_usd: float
    recovered_actual_usd: float | None
    recovered_observed_usd: float | None
    reason: str
    source: str
    source_tier: str
    remaining_shares: float | None
    mae_pnl_usd: float | None
    mfe_pnl_usd: float | None


@dataclass
class TradePairRow:
    position_id: str
    token_id: str
    market: str
    side: str
    status: str
    opened_ts: str
    closed_ts: str
    entry_secs_left: float | None
    matched_exit_shares: float = 0.0
    market_profile: str = "btc_5m"
    regime: str = "unknown"
    entry_cost_usd: float = 0.0
    entry_shares: float = 0.0
    matched_cost_usd: float = 0.0
    exit_recovered_actual_usd: float | None = None
    exit_recovered_observed_usd: float | None = None
    actual_pnl_usd: float | None = None
    observed_pnl_usd: float | None = None
    fee_adjusted_actual_pnl_usd: float | None = None
    fee_adjusted_observed_pnl_usd: float | None = None
    estimated_total_fees_actual_usd: float | None = None
    estimated_total_fees_observed_usd: float | None = None
    actual_source: str = ""
    actual_source_tier: str = ""
    entry_execution_style: str = ""
    exit_execution_style: str = ""
    close_bucket: str = ""
    close_reason: str = ""
    entry_quality: str = ""
    remaining_shares: float = 0.0
    unmatched_entry_cost_usd: float = 0.0
    unmatched_entry_shares: float = 0.0
    mae_pnl_usd: float | None = None
    mfe_pnl_usd: float | None = None
    flags: list[str] = field(default_factory=list)
    legs: list[TradeLeg] = field(default_factory=list)


def load_trade_events(
    limit: int = 0, run_id: str | None = None, since_ts: str | None = None
) -> list[dict]:
    events = [
        ev for ev in read_events(limit=limit) if ev.get("kind") in {"entry", "exit"}
    ]
    if not run_id and not since_ts:
        return events

    since_dt = None
    if since_ts:
        try:
            since_dt = datetime.fromisoformat(str(since_ts))
        except Exception:
            since_dt = None

    selected: list[dict] = []
    selected_ids: set[str] = set()
    touched_keys: set[str] = set()
    for ev in events:
        include = False
        if run_id and str(ev.get("run_id") or "") == str(run_id):
            include = True
        if not include and since_dt is not None:
            ev_dt = _event_ts(ev)
            if ev_dt is not None and ev_dt >= since_dt:
                include = True
        if not include:
            continue
        selected.append(ev)
        if ev.get("event_id"):
            selected_ids.add(str(ev["event_id"]))
        key = _event_pair_key(ev)
        if key:
            touched_keys.add(key)

    if touched_keys:
        for ev in events:
            if ev.get("kind") != "entry":
                continue
            key = _event_pair_key(ev)
            event_id = str(ev.get("event_id") or "")
            if key in touched_keys and event_id not in selected_ids:
                selected.append(ev)
                if event_id:
                    selected_ids.add(event_id)

    selected.sort(
        key=lambda ev: (str(ev.get("ts") or ""), str(ev.get("event_id") or ""))
    )
    return selected


def classify_actual_source_tier(
    source: str | None, actual_value: float | None = None
) -> str:
    src = str(source or "").strip().lower()
    if actual_value is None:
        return "none"
    if src == "account-activity-reconcile":
        return "high"
    if src == "cash_balance_delta":
        return "high"
    if src == "market-settlement-lookup":
        return "medium"
    if "balance-delta" in src or "balance_delta" in src:
        return "high"
    if src in {
        "close_response_amount",
        "close_response_value",
        "close_response_raw_amount",
        "actual_close_response_value",
        "response_amount",
        "response_value",
    }:
        return "medium"
    if src == "paper_trade_simulation":
        return "medium"
    if src in {
        "actual_exit_value",
        "observed_mark_estimate",
        "observed_only",
        "unavailable",
        "cash_balance_non_positive",
        "cash_balance_unavailable",
        "",
    }:
        return "low"
    if "cash_balance" in src:
        return "high"
    if "response" in src or "close_response" in src:
        return "medium"
    return "low"


def actual_status_for_exit(ev: dict) -> str:
    actual = _maybe_float(ev.get("actual_exit_value_usd"))
    tier = classify_actual_source_tier(
        ev.get("actual_exit_value_source")
        or ev.get("actual_close_response_value_source")
        or ev.get("pnl_source"),
        actual,
    )
    if actual is None:
        return "missing"
    if tier == "high":
        return "ok"
    if tier == "medium":
        return "estimated"
    return "low_confidence"


def exit_flags_for_event(
    ev: dict, actual: float | None, observed: float | None, diff: float | None
) -> list[str]:
    flags: list[str] = []
    status = actual_status_for_exit(ev)
    if status == "missing":
        flags.append("no-actual")
    elif status == "estimated":
        flags.append("actual-medium-confidence")
    elif status == "low_confidence":
        flags.append("actual-low-confidence")
    if diff is not None:
        if abs(diff) >= 0.25:
            flags.append("actual-observed-critical-gap")
        elif abs(diff) >= 0.10:
            flags.append("actual-observed-warn-gap")
    if _f(ev.get("remaining_shares"), 0.0) > EPS:
        flags.append("position-still-open")
    if actual is None and observed is not None:
        flags.append("observed-only")
    return flags


def build_exit_accounting_rows(events: list[dict]) -> list[ExitAccountingRow]:
    rows: list[ExitAccountingRow] = []
    for ev in events:
        if ev.get("kind") != "exit":
            continue
        actual = _maybe_float(ev.get("actual_exit_value_usd"))
        observed = _maybe_float(ev.get("observed_exit_value_usd"))
        diff = None
        diff_pct = None
        realized_cost = _f(ev.get("realized_cost_usd"), 0.0)
        actual_value = actual if actual is not None else None
        if actual_value is not None and observed is not None:
            diff = actual_value - observed
            if realized_cost > EPS:
                diff_pct = diff / realized_cost
        source = str(
            ev.get("actual_exit_value_source")
            or ev.get("close_response_value_source")
            or ev.get("actual_close_response_value_source")
            or ev.get("pnl_source")
            or "unavailable"
        )
        tier = classify_actual_source_tier(source, actual_value)
        rows.append(
            ExitAccountingRow(
                ts=str(ev.get("ts") or ""),
                event_id=str(ev.get("event_id") or ""),
                position_id=str(ev.get("position_id") or ""),
                market=str(ev.get("slug") or ""),
                side=str(ev.get("side") or ""),
                reason=str(ev.get("reason") or ""),
                entry_quality=str(ev.get("entry_quality") or "unknown"),
                closed_shares=_f(ev.get("closed_shares"), 0.0),
                remaining_shares=_maybe_float(ev.get("remaining_shares")),
                realized_cost_usd=realized_cost,
                actual_exit_value_usd=actual_value,
                observed_exit_value_usd=observed,
                actual_source=source,
                actual_source_tier=tier,
                observed_source=str(
                    ev.get("observed_exit_value_source")
                    or ev.get("pnl_source")
                    or "observed_mark_price"
                ),
                difference_usd=diff,
                difference_pct_of_cost=diff_pct,
                actual_status=actual_status_for_exit(ev),
                mae_pnl_usd=_maybe_float(ev.get("mae_pnl_usd")),
                mfe_pnl_usd=_maybe_float(ev.get("mfe_pnl_usd")),
                flags=exit_flags_for_event(ev, actual_value, observed, diff),
            )
        )
    return rows


def classify_pair_status(
    *,
    remaining_shares: float,
    has_exit: bool,
    exit_count: int,
    matched_shares: float,
    entry_shares: float,
) -> str:
    if not has_exit:
        return "unmatched"
    if remaining_shares > 1e-6:
        return "partial"
    if exit_count > 1 and matched_shares + 1e-6 < entry_shares:
        return "partial"
    return "closed"


def classify_close_bucket(reason: str | None) -> str:
    text = str(reason or "").strip().lower()
    if "market-expired" in text:
        if "binary-win" in text:
            return "expiry-binary-win"
        if "binary-loss" in text:
            return "expiry-binary-loss"
        if "binary-neutral" in text:
            return "expiry-binary-neutral"
        return "expiry-settlement"
    return "active-close"


def normalize_execution_style(style: str | None) -> str:
    text = str(style or "").strip().lower()
    if not text:
        return "unknown"
    if "mixed" in text:
        return "mixed"
    if "expiry" in text:
        return "expiry-settlement"
    if "taker" in text:
        return "taker"
    if text in {
        "maker-timeout-fallback",
        "simulated-cross",
        "dry-run-cross",
        "dry_run_cross",
    }:
        return "taker"
    if "timeout-fallback" in text:
        return "taker"
    if "maker" in text:
        return "maker"
    if text in {"dry-run", "dry_run"}:
        return "unknown"
    return text


def calculate_dynamic_fee(usd_amount: float, price: float, rate: float = 0.02) -> float:
    """Calculates the Polymarket p*(1-p)*rate fee."""
    if price <= 0 or price >= 1.0 or usd_amount <= EPS:
        return 0.0
    return usd_amount * rate * price * (1.0 - price)

def estimate_pair_fees(
    *,
    matched_cost_usd: float,
    matched_shares: float,
    actual_exit_value_usd: float | None,
    observed_exit_value_usd: float | None,
    entry_execution_style: str | None,
    exit_execution_style: str | None,
    close_reason: str | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """
    Estimates total fees for a trade pair using dynamic pricing.
    """
    entry_style = normalize_execution_style(entry_execution_style)
    exit_style = normalize_execution_style(exit_execution_style)
    
    # 1. Entry Fee
    entry_fee = 0.0
    if entry_style in {"taker", "mixed"} and matched_shares > EPS:
        entry_price = matched_cost_usd / matched_shares
        entry_fee = calculate_dynamic_fee(matched_cost_usd, entry_price)
        
    # 2. Exit Fee (only if active-close)
    actual_total_fees = None
    observed_total_fees = None
    
    is_active_close = classify_close_bucket(close_reason) == "active-close"
    
    if actual_exit_value_usd is not None:
        exit_fee = 0.0
        if is_active_close and exit_style in {"taker", "mixed"} and matched_shares > EPS:
            exit_price = actual_exit_value_usd / matched_shares
            exit_fee = calculate_dynamic_fee(actual_exit_value_usd, exit_price)
        actual_total_fees = entry_fee + exit_fee

    if observed_exit_value_usd is not None:
        exit_fee = 0.0
        if is_active_close and exit_style in {"taker", "mixed"} and matched_shares > EPS:
            exit_price = observed_exit_value_usd / matched_shares
            exit_fee = calculate_dynamic_fee(observed_exit_value_usd, exit_price)
        observed_total_fees = entry_fee + exit_fee

    return entry_fee, actual_total_fees, observed_total_fees, 0.0 # fourth ret ignored mostly


def _actual_source_rank(tier: str | None) -> int:
    return {
        "none": 0,
        "low": 1,
        "medium": 2,
        "high": 3,
    }.get(str(tier or "").strip().lower(), 0)


def _recompute_pair_row_accounting(row: TradePairRow) -> None:
    row.actual_pnl_usd = (
        (row.exit_recovered_actual_usd - row.entry_cost_usd)
        if row.exit_recovered_actual_usd is not None
        else None
    )
    row.observed_pnl_usd = (
        (row.exit_recovered_observed_usd - row.entry_cost_usd)
        if row.exit_recovered_observed_usd is not None
        else None
    )
    _, actual_total_fees, observed_total_fees, _ = estimate_pair_fees(
        matched_cost_usd=row.entry_cost_usd,
        matched_shares=row.entry_shares,
        actual_exit_value_usd=row.exit_recovered_actual_usd,
        observed_exit_value_usd=row.exit_recovered_observed_usd,
        entry_execution_style=row.entry_execution_style,
        exit_execution_style=row.exit_execution_style,
        close_reason=row.close_reason,
    )
    row.estimated_total_fees_actual_usd = actual_total_fees
    row.estimated_total_fees_observed_usd = observed_total_fees
    row.fee_adjusted_actual_pnl_usd = (
        (row.actual_pnl_usd - actual_total_fees)
        if row.actual_pnl_usd is not None and actual_total_fees is not None
        else None
    )
    row.fee_adjusted_observed_pnl_usd = (
        (row.observed_pnl_usd - observed_total_fees)
        if row.observed_pnl_usd is not None and observed_total_fees is not None
        else None
    )


def _collapse_overflow_residual_rows(rows: list[TradePairRow]) -> list[TradePairRow]:
    if not rows:
        return rows

    base_indices: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        if row.status == "residual":
            continue
        key = str(row.position_id or "").strip()
        if key:
            base_indices.setdefault(key, []).append(idx)

    dropped_indices: set[int] = set()
    for idx, row in enumerate(rows):
        if row.status != "residual" or "orphan-residual" not in row.flags:
            continue
        pos_id = str(row.position_id or "").strip()
        if "#residual" not in pos_id:
            continue
        base_key = pos_id.split("#residual", 1)[0].strip()
        candidates = []
        for base_idx in base_indices.get(base_key, []):
            base_row = rows[base_idx]
            if (
                base_row.market != row.market
                or base_row.side != row.side
                or base_row.token_id != row.token_id
            ):
                continue
            if not base_row.opened_ts:
                continue
            base_dt = _parse_iso_dt(base_row.closed_ts)
            row_dt = _parse_iso_dt(row.closed_ts)
            time_diff = (
                abs((base_dt - row_dt).total_seconds())
                if base_dt is not None and row_dt is not None
                else 0.0
            )
            if base_dt is not None and row_dt is not None and time_diff > 120:
                continue
            reason_match = (
                str(base_row.close_reason or "").strip().lower()
                == str(row.close_reason or "").strip().lower()
            )
            candidates.append((0 if reason_match else 1, time_diff, base_idx))
        if not candidates:
            continue

        _, _, base_idx = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        base_row = rows[base_idx]
        if row.exit_recovered_actual_usd is not None:
            base_row.exit_recovered_actual_usd = (
                base_row.exit_recovered_actual_usd or 0.0
            ) + row.exit_recovered_actual_usd
        elif (
            row.exit_recovered_observed_usd is not None
            and base_row.exit_recovered_actual_usd is not None
        ):
            # When a later exit leg is missing live actuals, keep the already-realized
            # accounting whole by falling back only that leg to its observed value.
            base_row.exit_recovered_actual_usd = (
                base_row.exit_recovered_actual_usd or 0.0
            ) + row.exit_recovered_observed_usd
            base_row.actual_source = "mixed-actual-observed-fallback"
            base_row.actual_source_tier = "medium"
            base_row.flags.append("actual-partial-observed-fallback")
        if row.exit_recovered_observed_usd is not None:
            base_row.exit_recovered_observed_usd = (
                base_row.exit_recovered_observed_usd or 0.0
            ) + row.exit_recovered_observed_usd
        if _actual_source_rank(row.actual_source_tier) > _actual_source_rank(
            base_row.actual_source_tier
        ):
            base_row.actual_source = row.actual_source
            base_row.actual_source_tier = row.actual_source_tier
        if normalize_execution_style(base_row.exit_execution_style) == "unknown":
            base_row.exit_execution_style = row.exit_execution_style
        base_row.legs.extend(row.legs)
        base_row.flags = list(
            dict.fromkeys(
                [
                    flag
                    for flag in base_row.flags
                    if flag not in {"ui-reconciliation-needed"}
                ]
                + ["collapsed-overflow-residual"]
            )
        )
        _recompute_pair_row_accounting(base_row)
        dropped_indices.add(idx)

    if not dropped_indices:
        return rows
    return [row for idx, row in enumerate(rows) if idx not in dropped_indices]


def _collapse_entry_slippage_guard_rows(rows: list[TradePairRow]) -> list[TradePairRow]:
    if not rows:
        return rows

    base_indices: dict[tuple[str, str, str], list[int]] = {}
    for idx, row in enumerate(rows):
        if row.status == "residual":
            continue
        if "market-settlement-imputed" not in row.flags:
            continue
        key = (
            str(row.market or "").strip(),
            str(row.side or "").strip(),
            str(row.token_id or "").strip(),
        )
        if all(key):
            base_indices.setdefault(key, []).append(idx)

    dropped_indices: set[int] = set()
    for idx, row in enumerate(rows):
        if row.status != "residual" or "orphan-residual" not in row.flags:
            continue
        if str(row.close_reason or "").strip().lower() != "entry-slippage-guard":
            continue
        key = (
            str(row.market or "").strip(),
            str(row.side or "").strip(),
            str(row.token_id or "").strip(),
        )
        if not all(key):
            continue

        row_closed_dt = _parse_iso_dt(row.closed_ts)
        share_target = (
            row.matched_exit_shares
            if row.matched_exit_shares > EPS
            else row.entry_shares
        )
        candidates = []
        for base_idx in base_indices.get(key, []):
            base_row = rows[base_idx]
            base_open_dt = _parse_iso_dt(base_row.opened_ts)
            age_sec = (
                abs((row_closed_dt - base_open_dt).total_seconds())
                if row_closed_dt is not None and base_open_dt is not None
                else 0.0
            )
            if row_closed_dt is not None and base_open_dt is not None and age_sec > 300:
                continue
            base_shares = (
                base_row.entry_shares
                if base_row.entry_shares > EPS
                else base_row.matched_exit_shares
            )
            share_diff = abs(base_shares - share_target)
            candidates.append((share_diff, age_sec, base_idx))

        if not candidates:
            continue

        _, _, base_idx = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        base_row = rows[base_idx]
        base_row.matched_cost_usd = base_row.entry_cost_usd
        if share_target > EPS:
            base_row.matched_exit_shares = share_target
        base_row.exit_recovered_actual_usd = row.exit_recovered_actual_usd
        base_row.exit_recovered_observed_usd = row.exit_recovered_observed_usd
        base_row.close_reason = "entry-slippage-guard"
        base_row.close_bucket = classify_close_bucket(base_row.close_reason)
        base_row.closed_ts = row.closed_ts or base_row.closed_ts
        base_row.status = "closed"
        base_row.remaining_shares = 0.0
        base_row.unmatched_entry_cost_usd = 0.0
        base_row.unmatched_entry_shares = 0.0
        if _actual_source_rank(row.actual_source_tier) >= _actual_source_rank(
            base_row.actual_source_tier
        ):
            base_row.actual_source = row.actual_source
            base_row.actual_source_tier = row.actual_source_tier
        if normalize_execution_style(row.exit_execution_style) != "unknown":
            base_row.exit_execution_style = row.exit_execution_style
        base_row.mae_pnl_usd = _coalesce_extreme(
            base_row.mae_pnl_usd, row.mae_pnl_usd, min
        )
        base_row.mfe_pnl_usd = _coalesce_extreme(
            base_row.mfe_pnl_usd, row.mfe_pnl_usd, max
        )
        base_row.legs = [
            leg for leg in base_row.legs if leg.kind != "expiry_settlement"
        ] + row.legs
        base_row.flags = list(
            dict.fromkeys(
                [
                    flag
                    for flag in base_row.flags
                    if flag
                    not in {
                        "market-settlement-imputed",
                        "no-exit",
                        "open-remainder",
                        "ui-reconciliation-needed",
                    }
                ]
                + ["collapsed-entry-slippage-guard"]
            )
        )
        _recompute_pair_row_accounting(base_row)
        dropped_indices.add(idx)

    if not dropped_indices:
        return rows
    return [row for idx, row in enumerate(rows) if idx not in dropped_indices]


def build_trade_pairs(events: list[dict]) -> list[TradePairRow]:
    open_entries: dict[str, deque[dict]] = {}
    rows: list[TradePairRow] = []
    residual_counter = 0

    for ev in events:
        kind = str(ev.get("kind") or "")
        token_id = str(ev.get("token_id") or "")
        position_id = str(ev.get("position_id") or "")
        key = position_id or token_id
        if not key:
            continue

        if kind == "entry":
            open_entries.setdefault(key, deque()).append(
                {
                    "event": ev,
                    "remaining_shares": _f(ev.get("shares"), 0.0),
                    "remaining_cost_usd": _f(ev.get("cost_usd"), 0.0),
                    "matched_shares": 0.0,
                    "matched_cost_usd": 0.0,
                    "exit_recovered_actual_usd": 0.0,
                    "exit_recovered_observed_usd": 0.0,
                    "has_actual": False,
                    "has_observed": False,
                    "actual_source": "unavailable",
                    "actual_source_tier": "none",
                    "observed_fallback_actual_usd": 0.0,
                    "entry_execution_style": normalize_execution_style(
                        ev.get("execution_style")
                    ),
                    "exit_execution_style": "unknown",
                    "close_reason": "",
                    "entry_quality": "unknown",
                    "closed_ts": "",
                    "exit_count": 0,
                    "flags": [],
                    "legs": [],
                    "mae_pnl_usd": _maybe_float(ev.get("mae_pnl_usd")),
                    "mfe_pnl_usd": _maybe_float(ev.get("mfe_pnl_usd")),
                }
            )
            continue

        if kind != "exit":
            continue

        exit_shares = _f(ev.get("closed_shares"), 0.0)
        if exit_shares <= 0:
            continue
        remaining = exit_shares
        actual_total = _maybe_float(ev.get("actual_exit_value_usd"))
        observed_total = _maybe_float(ev.get("observed_exit_value_usd"))
        actual_value = actual_total if actual_total is not None else None
        actual_source = str(
            ev.get("actual_exit_value_source")
            or ev.get("close_response_value_source")
            or ev.get("actual_close_response_value_source")
            or ev.get("pnl_source")
            or "unavailable"
        )
        actual_tier = classify_actual_source_tier(actual_source, actual_value)
        lots = open_entries.get(key) or deque()

        while lots and remaining > EPS:
            lot = lots[0]
            available_shares = float(lot["remaining_shares"])
            if available_shares <= EPS:
                lots.popleft()
                continue

            matched = min(available_shares, remaining)
            cost_basis = float(lot["remaining_cost_usd"])
            cost_piece = 0.0
            if available_shares > EPS and cost_basis > 0:
                cost_piece = cost_basis * (matched / available_shares)

            actual_piece = None
            if actual_value is not None and exit_shares > EPS:
                actual_piece = actual_value * (matched / exit_shares)
                lot["exit_recovered_actual_usd"] += actual_piece
                lot["has_actual"] = True
                if _actual_source_rank(actual_tier) >= _actual_source_rank(
                    str(lot.get("actual_source_tier") or "none")
                ):
                    lot["actual_source"] = actual_source
                    lot["actual_source_tier"] = actual_tier

            observed_piece = None
            if observed_total is not None and exit_shares > EPS:
                observed_piece = observed_total * (matched / exit_shares)
                lot["exit_recovered_observed_usd"] += observed_piece
                lot["has_observed"] = True
                if actual_piece is None:
                    lot["observed_fallback_actual_usd"] += observed_piece

            lot["remaining_shares"] = max(0.0, available_shares - matched)
            lot["remaining_cost_usd"] = max(0.0, cost_basis - cost_piece)
            lot["matched_shares"] += matched
            lot["matched_cost_usd"] += cost_piece
            lot["close_reason"] = str(ev.get("reason") or lot["close_reason"] or "")
            lot["entry_quality"] = str(
                ev.get("entry_quality") or lot.get("entry_quality") or "unknown"
            )
            lot["closed_ts"] = str(ev.get("ts") or lot["closed_ts"] or "")
            lot["exit_count"] += 1
            lot["exit_execution_style"] = normalize_execution_style(
                ev.get("exit_execution_style")
            )
            lot["mae_pnl_usd"] = _coalesce_extreme(
                lot.get("mae_pnl_usd"), _maybe_float(ev.get("mae_pnl_usd")), min
            )
            lot["mfe_pnl_usd"] = _coalesce_extreme(
                lot.get("mfe_pnl_usd"), _maybe_float(ev.get("mfe_pnl_usd")), max
            )
            if actual_tier == "medium":
                lot["flags"].append("actual-medium-confidence")
            elif actual_tier == "low":
                lot["flags"].append("actual-low-confidence")
            if actual_value is None and observed_total is not None:
                lot["flags"].append("observed-only")
            lot["legs"].append(
                TradeLeg(
                    ts=str(ev.get("ts") or ""),
                    event_id=str(ev.get("event_id") or ""),
                    kind="exit",
                    shares=matched,
                    cost_usd=cost_piece,
                    recovered_actual_usd=actual_piece,
                    recovered_observed_usd=observed_piece,
                    reason=str(ev.get("reason") or ""),
                    source=actual_source,
                    source_tier=actual_tier,
                    remaining_shares=_maybe_float(ev.get("remaining_shares")),
                    mae_pnl_usd=_maybe_float(ev.get("mae_pnl_usd")),
                    mfe_pnl_usd=_maybe_float(ev.get("mfe_pnl_usd")),
                )
            )
            remaining -= matched

            if lot["remaining_shares"] <= EPS:
                entry_ev = lot["event"]
                rows.append(_finalize_pair_row(entry_ev, token_id, key, lot))
                lots.popleft()

        if remaining > EPS:
            residual_counter += 1
            actual_piece = (
                actual_value * (remaining / exit_shares)
                if actual_value is not None and exit_shares > EPS
                else None
            )
            observed_piece = (
                observed_total * (remaining / exit_shares)
                if observed_total is not None and exit_shares > EPS
                else None
            )
            residual_flags = [
                "orphan-residual",
                "no-entry-match",
                "ui-reconciliation-needed",
            ]
            if actual_tier == "medium":
                residual_flags.append("actual-medium-confidence")
            elif actual_tier == "low":
                residual_flags.append("actual-low-confidence")
            if actual_piece is None and observed_piece is not None:
                residual_flags.append("observed-only")
            rows.append(
                TradePairRow(
                    position_id=f"{key}#residual{residual_counter}",
                    token_id=token_id,
                    market=str(ev.get("slug") or ""),
                    side=str(ev.get("side") or ""),
                    status="residual",
                    opened_ts="",
                    closed_ts=str(ev.get("ts") or ""),
                    entry_secs_left=None,
                    entry_cost_usd=0.0,
                    entry_shares=0.0,
                    matched_cost_usd=0.0,
                    matched_exit_shares=remaining,
                    exit_recovered_actual_usd=actual_piece,
                    exit_recovered_observed_usd=observed_piece,
                    actual_pnl_usd=None,
                    observed_pnl_usd=None,
                    fee_adjusted_actual_pnl_usd=None,
                    fee_adjusted_observed_pnl_usd=None,
                    estimated_total_fees_actual_usd=None,
                    estimated_total_fees_observed_usd=None,
                    actual_source=actual_source,
                    actual_source_tier=actual_tier,
                    entry_execution_style="unknown",
                    exit_execution_style=normalize_execution_style(
                        ev.get("exit_execution_style")
                    ),
                    close_bucket=classify_close_bucket(ev.get("reason")),
                    close_reason=str(ev.get("reason") or ""),
                    entry_quality=str(ev.get("entry_quality") or "unknown"),
                    remaining_shares=0.0,
                    unmatched_entry_cost_usd=0.0,
                    unmatched_entry_shares=0.0,
                    mae_pnl_usd=_maybe_float(ev.get("mae_pnl_usd")),
                    mfe_pnl_usd=_maybe_float(ev.get("mfe_pnl_usd")),
                    flags=residual_flags,
                    legs=[
                        TradeLeg(
                            ts=str(ev.get("ts") or ""),
                            event_id=str(ev.get("event_id") or ""),
                            kind="residual_exit",
                            shares=remaining,
                            cost_usd=0.0,
                            recovered_actual_usd=actual_piece,
                            recovered_observed_usd=observed_piece,
                            reason=str(ev.get("reason") or ""),
                            source=actual_source,
                            source_tier=actual_tier,
                            remaining_shares=_maybe_float(ev.get("remaining_shares")),
                            mae_pnl_usd=_maybe_float(ev.get("mae_pnl_usd")),
                            mfe_pnl_usd=_maybe_float(ev.get("mfe_pnl_usd")),
                        )
                    ],
                )
            )

        if key in open_entries and not open_entries[key]:
            open_entries.pop(key, None)

    for key, lots in open_entries.items():
        for lot in lots:
            rows.append(
                _finalize_pair_row(
                    lot["event"], str(lot["event"].get("token_id") or ""), key, lot
                )
            )

    rows = _collapse_overflow_residual_rows(rows)
    rows = _collapse_entry_slippage_guard_rows(rows)
    rows = reconcile_rows_with_account_activity(rows)
    rows.sort(
        key=lambda row: (
            row.opened_ts or row.closed_ts or "",
            row.market,
            row.side,
            row.position_id,
        )
    )
    return rows


def _finalize_pair_row(
    entry_ev: dict, token_id: str, key: str, lot: dict
) -> TradePairRow:
    entry_cost = _f(entry_ev.get("cost_usd"), 0.0)
    entry_shares = _f(entry_ev.get("shares"), 0.0)
    exit_actual = lot["exit_recovered_actual_usd"] if lot.get("has_actual") else None
    exit_observed = (
        lot["exit_recovered_observed_usd"] if lot.get("has_observed") else None
    )
    observed_fallback_actual = float(lot.get("observed_fallback_actual_usd") or 0.0)
    matched_cost = float(lot.get("matched_cost_usd") or 0.0)
    matched_exit_shares = float(lot.get("matched_shares") or 0.0)
    entry_execution_style = normalize_execution_style(
        lot.get("entry_execution_style") or entry_ev.get("execution_style")
    )
    exit_execution_style = normalize_execution_style(lot.get("exit_execution_style"))
    close_reason = str(lot.get("close_reason") or "")
    remaining_shares = float(lot.get("remaining_shares") or 0.0)
    flags = list(dict.fromkeys(lot.get("flags") or []))
    actual_source = str(lot.get("actual_source") or "unavailable")
    actual_source_tier = str(lot.get("actual_source_tier") or "none")
    legs = list(lot.get("legs") or [])
    settlement_applied = False
    settlement_closed_ts = ""

    if exit_actual is not None and observed_fallback_actual > EPS:
        exit_actual += observed_fallback_actual
        actual_source = "mixed-actual-observed-fallback"
        actual_source_tier = "medium"
        flags.append("actual-partial-observed-fallback")

    if remaining_shares > EPS:
        settlement_price, settlement_reason = _fetch_market_settlement(
            str(entry_ev.get("slug") or ""),
            str(entry_ev.get("side") or ""),
        )
        if settlement_price is not None:
            settlement_applied = True
            remaining_cost = float(lot.get("remaining_cost_usd") or 0.0)
            settlement_value = remaining_shares * settlement_price
            matched_cost += remaining_cost
            matched_exit_shares += remaining_shares
            exit_actual = (exit_actual or 0.0) + settlement_value
            exit_observed = (exit_observed or 0.0) + settlement_value
            actual_source = "market-settlement-lookup"
            actual_source_tier = classify_actual_source_tier(actual_source, exit_actual)
            exit_execution_style = "expiry-settlement"
            close_reason = (
                settlement_reason
                if not close_reason
                else f"{close_reason}+{settlement_reason}"
            )
            settlement_end_dt = _market_end_dt_from_slug(
                str(entry_ev.get("slug") or "")
            )
            settlement_closed_ts = (
                settlement_end_dt.isoformat() if settlement_end_dt is not None else ""
            )
            legs.append(
                TradeLeg(
                    ts=settlement_closed_ts,
                    event_id=f"{key}#settlement",
                    kind="expiry_settlement",
                    shares=remaining_shares,
                    cost_usd=remaining_cost,
                    recovered_actual_usd=settlement_value,
                    recovered_observed_usd=settlement_value,
                    reason=settlement_reason,
                    source=actual_source,
                    source_tier=actual_source_tier,
                    remaining_shares=0.0,
                    mae_pnl_usd=None,
                    mfe_pnl_usd=None,
                )
            )
            remaining_shares = 0.0
            flags.append("market-settlement-imputed")

    actual_pnl = (exit_actual - matched_cost) if exit_actual is not None else None
    observed_pnl = (exit_observed - matched_cost) if exit_observed is not None else None
    _, actual_total_fees, observed_total_fees, _ = estimate_pair_fees(
        matched_cost_usd=matched_cost,
        actual_exit_value_usd=exit_actual,
        observed_exit_value_usd=exit_observed,
        entry_execution_style=entry_execution_style,
        exit_execution_style=exit_execution_style,
        close_reason=close_reason,
        matched_shares=matched_exit_shares,
    )
    fee_adjusted_actual_pnl = (
        (actual_pnl - actual_total_fees)
        if actual_pnl is not None and actual_total_fees is not None
        else None
    )
    fee_adjusted_observed_pnl = (
        (observed_pnl - observed_total_fees)
        if observed_pnl is not None and observed_total_fees is not None
        else None
    )
    if remaining_shares > EPS:
        flags.append("open-remainder")
    if not lot.get("exit_count") and not settlement_applied:
        flags.append("no-exit")
    entry_secs_left = _maybe_float(entry_ev.get("secs_left"))
    market_profile = str(entry_ev.get("market_profile") or "btc_5m")
    regime = str(entry_ev.get("regime") or "unknown")
    
    return TradePairRow(
        position_id=str(entry_ev.get("position_id") or key),
        token_id=token_id,
        market=str(entry_ev.get("slug") or ""),
        side=str(entry_ev.get("side") or ""),
        status=classify_pair_status(
            remaining_shares=remaining_shares,
            has_exit=bool(lot.get("exit_count")) or settlement_applied,
            exit_count=int(lot.get("exit_count") or 0)
            + (1 if settlement_applied else 0),
            matched_shares=matched_exit_shares,
            entry_shares=entry_shares,
        ),
        opened_ts=str(entry_ev.get("ts") or ""),
        closed_ts=settlement_closed_ts or str(lot.get("closed_ts") or ""),
        entry_secs_left=entry_secs_left,
        market_profile=market_profile,
        regime=regime,
        entry_cost_usd=entry_cost,
        entry_shares=entry_shares,
        matched_cost_usd=matched_cost,
        matched_exit_shares=matched_exit_shares,
        exit_recovered_actual_usd=exit_actual,
        exit_recovered_observed_usd=exit_observed,
        actual_pnl_usd=actual_pnl,
        observed_pnl_usd=observed_pnl,
        fee_adjusted_actual_pnl_usd=fee_adjusted_actual_pnl,
        fee_adjusted_observed_pnl_usd=fee_adjusted_observed_pnl,
        estimated_total_fees_actual_usd=actual_total_fees,
        estimated_total_fees_observed_usd=observed_total_fees,
        actual_source=actual_source,
        actual_source_tier=actual_source_tier,
        entry_execution_style=entry_execution_style,
        exit_execution_style=exit_execution_style,
        close_bucket=classify_close_bucket(close_reason),
        close_reason=close_reason,
        entry_quality=str(lot.get("entry_quality") or "unknown"),
        remaining_shares=remaining_shares,
        unmatched_entry_cost_usd=float(lot.get("remaining_cost_usd") or 0.0)
        if remaining_shares > EPS
        else 0.0,
        unmatched_entry_shares=remaining_shares,
        mae_pnl_usd=lot.get("mae_pnl_usd"),
        mfe_pnl_usd=lot.get("mfe_pnl_usd"),
        flags=flags,
        legs=legs,
    )


def _coalesce_extreme(
    current: float | None, candidate: float | None, chooser
) -> float | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return chooser(current, candidate)


def dataclass_list_to_json(rows: list[Any], path: str | Path) -> None:
    payload = [asdict(row) for row in rows]
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def dataclass_list_to_csv(
    rows: list[Any], path: str | Path, *, flatten_legs: bool = False
) -> None:
    data = []
    for row in rows:
        item = asdict(row)
        if flatten_legs:
            item["legs"] = json.dumps(item.get("legs", []), ensure_ascii=False)
            item["flags"] = "|".join(item.get("flags", []))
        else:
            item.pop("legs", None)
            item["flags"] = "|".join(item.get("flags", []))
        data.append(item)
    fieldnames: list[str] = []
    for item in data:
        for key in item.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)


def summarize_trade_pairs(rows: list[TradePairRow]) -> dict[str, Any]:
    total = len(rows)
    by_status = Counter(row.status for row in rows)
    by_quality = Counter(row.entry_quality for row in rows if row.entry_quality)
    by_reason = Counter(row.close_reason for row in rows if row.close_reason)
    by_bucket = Counter(row.close_bucket for row in rows if row.close_bucket)
    by_tier = Counter(row.actual_source_tier for row in rows)
    actual_rows = [row for row in rows if row.actual_pnl_usd is not None]
    observed_rows = [row for row in rows if row.observed_pnl_usd is not None]
    fee_actual_rows = [
        row for row in rows if row.fee_adjusted_actual_pnl_usd is not None
    ]
    fee_observed_rows = [
        row for row in rows if row.fee_adjusted_observed_pnl_usd is not None
    ]
    scratch_threshold = max(
        0.0, float(getattr(SETTINGS, "report_scratch_pnl_pct", 0.03))
    )

    def _summarize_values(items: list[TradePairRow], attr: str) -> dict[str, Any]:
        values = [getattr(row, attr) for row in items if getattr(row, attr) is not None]
        return {
            "count": len(values),
            "sum": sum(values) if values else None,
            "average": (sum(values) / len(values)) if values else None,
        }

    def _scratch_like(row: TradePairRow) -> bool:
        if row.close_reason in {"stalled-trade", "deadline-exit-flat"}:
            return True
        basis_pnl = (
            row.fee_adjusted_actual_pnl_usd
            if row.fee_adjusted_actual_pnl_usd is not None
            else row.actual_pnl_usd
            if row.actual_pnl_usd is not None
            else row.observed_pnl_usd
        )
        if basis_pnl is None or row.entry_cost_usd <= EPS:
            return False
        return (
            abs(float(basis_pnl)) / max(float(row.entry_cost_usd), EPS)
            <= scratch_threshold
        )

    def _summarize_actual_minus_observed(items: list[TradePairRow]) -> dict[str, Any]:
        values = [
            float(row.actual_pnl_usd) - float(row.observed_pnl_usd)
            for row in items
            if row.actual_pnl_usd is not None and row.observed_pnl_usd is not None
        ]
        total = round(sum(values), 10) if values else None
        average = round(total / len(values), 10) if values else None
        return {
            "count": len(values),
            "sum": total,
            "average": average,
        }

    # New timing and execution style buckets
    timing_summary: dict[str, Any] = {}
    timing_buckets = [
        ("240-300s", lambda sl: sl is not None and 240 <= sl <= 300),
        ("180-240s", lambda sl: sl is not None and 180 <= sl < 240),
        ("150-180s", lambda sl: sl is not None and 150 <= sl < 180),
        ("<150s", lambda sl: sl is not None and sl < 150),
        ("unknown", lambda sl: sl is None),
    ]
    for label, checker in timing_buckets:
        bucket_rows = [row for row in rows if checker(row.entry_secs_left)]
        if bucket_rows:
            timing_summary[label] = {
                "count": len(bucket_rows),
                "actual_pnl": _summarize_values(bucket_rows, "actual_pnl_usd"),
                "fee_adj_actual_pnl": _summarize_values(bucket_rows, "fee_adjusted_actual_pnl_usd"),
            }

    execution_summary: dict[str, Any] = {}
    execution_styles = ["maker", "taker", "expiry-settlement", "mixed", "unknown"]
    for style in execution_styles:
        # Group by entry style or exit style for special buckets
        if style == "expiry-settlement":
            style_rows = [row for row in rows if row.close_bucket == "expiry-settlement"]
        else:
            style_rows = [row for row in rows if normalize_execution_style(row.entry_execution_style) == style]
            
        if style_rows:
            execution_summary[style] = {
                "count": len(style_rows),
                "actual_pnl": _summarize_values(style_rows, "actual_pnl_usd"),
                "fee_adj_actual_pnl": _summarize_values(style_rows, "fee_adjusted_actual_pnl_usd"),
            }

    scratch_rows = [row for row in rows if _scratch_like(row)]
    scratch_reasons = Counter(
        row.close_reason for row in scratch_rows if row.close_reason
    )
    deadline_loss_rows = [
        row
        for row in rows
        if str(row.close_reason or "").strip() == "deadline-exit-loss"
    ]
    deadline_loss_reasons = Counter(
        row.close_reason for row in deadline_loss_rows if row.close_reason
    )
    weak_trade_recycle_rows = [
        row
        for row in rows
        if str(row.close_reason or "").strip()
        in {"stalled-trade", "failed-follow-through"}
    ]
    weak_trade_recycle_reasons = Counter(
        row.close_reason for row in weak_trade_recycle_rows if row.close_reason
    )

    bucket_summary: dict[str, Any] = {}
    bucket_gap_summary: dict[str, Any] = {}
    for bucket in sorted(by_bucket):
        bucket_rows = [row for row in rows if row.close_bucket == bucket]
        bucket_summary[bucket] = {
            "count": len(bucket_rows),
            "actual_pnl": _summarize_values(bucket_rows, "actual_pnl_usd"),
            "fee_adjusted_actual_pnl": _summarize_values(
                bucket_rows, "fee_adjusted_actual_pnl_usd"
            ),
        }
        bucket_gap_summary[bucket] = _summarize_actual_minus_observed(bucket_rows)

    # Profile and Regime Summary
    profile_summary: dict[str, Any] = {}
    for prof in ["btc_5m", "btc_15m"]:
        p_rows = [row for row in rows if row.market_profile == prof]
        if p_rows:
            profile_summary[prof] = {
                "count": len(p_rows),
                "actual_pnl": _summarize_values(p_rows, "actual_pnl_usd"),
                "fee_adj_actual_pnl": _summarize_values(p_rows, "fee_adjusted_actual_pnl_usd"),
            }

    regime_summary: dict[str, Any] = {}
    for reg in ["opening", "mid", "late", "unknown"]:
        r_rows = [row for row in rows if row.regime == reg]
        if r_rows:
            regime_summary[reg] = {
                "count": len(r_rows),
                "actual_pnl": _summarize_values(r_rows, "actual_pnl_usd"),
                "fee_adj_actual_pnl": _summarize_values(r_rows, "fee_adjusted_actual_pnl_usd"),
            }

    return {
        "total_trades": total,
        "status_counts": dict(sorted(by_status.items())),
        "entry_quality_counts": dict(sorted(by_quality.items())),
        "close_reason_counts": dict(sorted(by_reason.items())),
        "close_bucket_counts": dict(sorted(by_bucket.items())),
        "profile_summary": profile_summary,
        "regime_summary": regime_summary,
        "entry_timing_summary": timing_summary,
        "execution_style_summary": execution_summary,
        "close_bucket_pnl": bucket_summary,
        "close_bucket_actual_vs_observed": bucket_gap_summary,
        "actual_source_tier_counts": dict(sorted(by_tier.items())),
        "actual_available_ratio": (len(actual_rows) / total) if total else None,
        "actual_pnl": _summarize_values(actual_rows, "actual_pnl_usd"),
        "observed_pnl": _summarize_values(observed_rows, "observed_pnl_usd"),
        "actual_minus_observed_gap": _summarize_actual_minus_observed(rows),
        "fee_adjusted_actual_pnl": _summarize_values(
            fee_actual_rows, "fee_adjusted_actual_pnl_usd"
        ),
        "fee_adjusted_observed_pnl": _summarize_values(
            fee_observed_rows, "fee_adjusted_observed_pnl_usd"
        ),
        "scratch_trades": {
            "count": len(scratch_rows),
            "ratio": (len(scratch_rows) / total) if total else None,
            "close_reason_counts": dict(sorted(scratch_reasons.items())),
            "fee_adjusted_actual_pnl": _summarize_values(
                scratch_rows, "fee_adjusted_actual_pnl_usd"
            ),
        },
        "deadline_loss_trades": {
            "count": len(deadline_loss_rows),
            "ratio": (len(deadline_loss_rows) / total) if total else None,
            "close_reason_counts": dict(sorted(deadline_loss_reasons.items())),
        },
        "weak_trade_recycles": {
            "count": len(weak_trade_recycle_rows),
            "ratio": (len(weak_trade_recycle_rows) / total) if total else None,
            "close_reason_counts": dict(sorted(weak_trade_recycle_reasons.items())),
        },
        "mae": {
            "count": sum(1 for row in rows if row.mae_pnl_usd is not None),
            "average": (
                sum(row.mae_pnl_usd for row in rows if row.mae_pnl_usd is not None)
                / max(1, sum(1 for row in rows if row.mae_pnl_usd is not None))
            ),
        },
        "mfe": {
            "count": sum(1 for row in rows if row.mfe_pnl_usd is not None),
            "average": (
                sum(row.mfe_pnl_usd for row in rows if row.mfe_pnl_usd is not None)
                / max(1, sum(1 for row in rows if row.mfe_pnl_usd is not None))
            ),
        },
        "notes": {
            "actual_unavailable": "actual_pnl average uses only rows with actual data; missing actual rows are excluded, not imputed"
            if len(actual_rows) < total
            else "all rows have actual pnl",
            "fee_model": "fee_adjusted pnl applies assumed taker fees only to legs tagged taker/mixed; maker-like and unknown legs are treated as zero-fee, and expiry settlements pay no exit fee",
        },
    }


def summarize_shadow_signals(events: list[dict]) -> dict:
    shadows = [ev for ev in events if ev.get("kind") == "shadow_signal"]
    if not shadows:
        return {"total_blocked": 0}
    
    by_reason = Counter(ev.get("reason") for ev in shadows)
    by_strategy = Counter(ev.get("strategy") for ev in shadows)
    by_profile = Counter(ev.get("market_profile") for ev in shadows)
    
    return {
        "total_blocked": len(shadows),
        "reasons": dict(by_reason.most_common()),
        "strategies": dict(by_strategy.most_common()),
        "profiles": dict(by_profile.most_common()),
    }


def summarize_exit_accounting(rows: list[ExitAccountingRow]) -> dict[str, Any]:
    total = len(rows)
    by_status = Counter(row.actual_status for row in rows)
    by_tier = Counter(row.actual_source_tier for row in rows)
    flagged = Counter(flag for row in rows for flag in row.flags)
    actual_rows = [row for row in rows if row.actual_exit_value_usd is not None]
    observed_rows = [row for row in rows if row.observed_exit_value_usd is not None]
    diff_rows = [row for row in rows if row.difference_usd is not None]
    return {
        "total_exits": total,
        "actual_status_counts": dict(sorted(by_status.items())),
        "actual_source_tier_counts": dict(sorted(by_tier.items())),
        "flag_counts": dict(sorted(flagged.items())),
        "actual_available_ratio": (len(actual_rows) / total) if total else None,
        "average_actual_exit_value_usd": (
            sum(row.actual_exit_value_usd for row in actual_rows) / len(actual_rows)
        )
        if actual_rows
        else None,
        "average_observed_exit_value_usd": (
            sum(row.observed_exit_value_usd for row in observed_rows)
            / len(observed_rows)
        )
        if observed_rows
        else None,
        "average_actual_minus_observed_usd": (
            sum(row.difference_usd for row in diff_rows) / len(diff_rows)
        )
        if diff_rows
        else None,
        "notes": {
            "actual_unavailable": "actual averages use only rows with actual data; unavailable actual values are excluded"
            if len(actual_rows) < total
            else "all rows have actual exit values",
        },
    }
