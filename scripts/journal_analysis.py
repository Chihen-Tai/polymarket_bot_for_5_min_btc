from __future__ import annotations

import csv
from datetime import datetime
import json
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collections import defaultdict, deque, Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.config import SETTINGS
from core.journal import read_events


EPS = 1e-9


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


def _event_pair_key(ev: dict) -> str:
    position_id = str(ev.get("position_id") or "").strip()
    if position_id:
        return position_id
    return str(ev.get("token_id") or "").strip()


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
    entry_cost_usd: float
    entry_shares: float
    matched_exit_shares: float
    exit_recovered_actual_usd: float | None
    exit_recovered_observed_usd: float | None
    actual_pnl_usd: float | None
    observed_pnl_usd: float | None
    fee_adjusted_actual_pnl_usd: float | None
    fee_adjusted_observed_pnl_usd: float | None
    estimated_total_fees_actual_usd: float | None
    estimated_total_fees_observed_usd: float | None
    actual_source: str
    actual_source_tier: str
    entry_execution_style: str
    exit_execution_style: str
    close_bucket: str
    close_reason: str
    entry_quality: str
    remaining_shares: float
    unmatched_entry_cost_usd: float
    unmatched_entry_shares: float
    mae_pnl_usd: float | None
    mfe_pnl_usd: float | None
    flags: list[str]
    legs: list[TradeLeg]


def load_trade_events(limit: int = 0, run_id: str | None = None, since_ts: str | None = None) -> list[dict]:
    events = [ev for ev in read_events(limit=limit) if ev.get("kind") in {"entry", "exit"}]
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

    selected.sort(key=lambda ev: (str(ev.get("ts") or ""), str(ev.get("event_id") or "")))
    return selected


def classify_actual_source_tier(source: str | None, actual_value: float | None = None) -> str:
    src = str(source or "").strip().lower()
    if actual_value is None or actual_value <= 0:
        return "none"
    if src == "cash_balance_delta":
        return "high"
    if "balance-delta" in src or "balance_delta" in src:
        return "high"
    if src in {"close_response_amount", "close_response_value", "close_response_raw_amount", "actual_close_response_value", "response_amount", "response_value"}:
        return "medium"
    if src == "paper_trade_simulation":
        return "medium"
    if src in {"actual_exit_value", "observed_mark_estimate", "observed_only", "unavailable", "cash_balance_non_positive", "cash_balance_unavailable", ""}:
        return "low"
    if "cash_balance" in src:
        return "high"
    if "response" in src or "close_response" in src:
        return "medium"
    return "low"


def actual_status_for_exit(ev: dict) -> str:
    actual = _maybe_float(ev.get("actual_exit_value_usd"))
    tier = classify_actual_source_tier(ev.get("actual_exit_value_source") or ev.get("actual_close_response_value_source") or ev.get("pnl_source"), actual)
    if actual is None or actual <= 0:
        return "missing"
    if tier == "high":
        return "ok"
    if tier == "medium":
        return "estimated"
    return "low_confidence"


def exit_flags_for_event(ev: dict, actual: float | None, observed: float | None, diff: float | None) -> list[str]:
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
        actual_value = actual if actual is not None and actual > 0 else None
        if actual_value is not None and observed is not None:
            diff = actual_value - observed
            if realized_cost > EPS:
                diff_pct = diff / realized_cost
        source = str(ev.get("actual_exit_value_source") or ev.get("close_response_value_source") or ev.get("actual_close_response_value_source") or ev.get("pnl_source") or "unavailable")
        tier = classify_actual_source_tier(source, actual_value)
        rows.append(ExitAccountingRow(
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
            observed_source=str(ev.get("observed_exit_value_source") or ev.get("pnl_source") or "observed_mark_price"),
            difference_usd=diff,
            difference_pct_of_cost=diff_pct,
            actual_status=actual_status_for_exit(ev),
            mae_pnl_usd=_maybe_float(ev.get("mae_pnl_usd")),
            mfe_pnl_usd=_maybe_float(ev.get("mfe_pnl_usd")),
            flags=exit_flags_for_event(ev, actual_value, observed, diff),
        ))
    return rows


def classify_pair_status(*, remaining_shares: float, has_exit: bool, exit_count: int, matched_shares: float, entry_shares: float) -> str:
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
    if text in {"maker-timeout-fallback", "simulated-cross", "dry-run-cross", "dry_run_cross"}:
        return "taker"
    if "timeout-fallback" in text:
        return "taker"
    if "maker" in text:
        return "maker"
    if text in {"dry-run", "dry_run"}:
        return "unknown"
    return text


def execution_fee_rate(style: str | None, *, close_reason: str | None = None, for_exit: bool = False) -> float:
    if for_exit and classify_close_bucket(close_reason) != "active-close":
        return 0.0
    normalized = normalize_execution_style(style)
    if normalized in {"taker", "mixed"}:
        return max(0.0, float(getattr(SETTINGS, "report_assumed_taker_fee_rate", 0.0)))
    return 0.0


def estimate_pair_fees(
    *,
    matched_cost_usd: float,
    actual_exit_value_usd: float | None,
    observed_exit_value_usd: float | None,
    entry_execution_style: str | None,
    exit_execution_style: str | None,
    close_reason: str | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    entry_fee_rate = execution_fee_rate(entry_execution_style, close_reason=close_reason, for_exit=False)
    exit_fee_rate = execution_fee_rate(exit_execution_style, close_reason=close_reason, for_exit=True)
    entry_fee = matched_cost_usd * entry_fee_rate if matched_cost_usd > EPS else 0.0

    actual_total_fees = None
    if actual_exit_value_usd is not None:
        actual_total_fees = entry_fee + (actual_exit_value_usd * exit_fee_rate)

    observed_total_fees = None
    if observed_exit_value_usd is not None:
        observed_total_fees = entry_fee + (observed_exit_value_usd * exit_fee_rate)

    return entry_fee, actual_total_fees, observed_total_fees, exit_fee_rate


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
            open_entries.setdefault(key, deque()).append({
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
                "entry_execution_style": normalize_execution_style(ev.get("execution_style")),
                "exit_execution_style": "unknown",
                "close_reason": "",
                "entry_quality": "unknown",
                "closed_ts": "",
                "exit_count": 0,
                "flags": [],
                "legs": [],
                "mae_pnl_usd": _maybe_float(ev.get("mae_pnl_usd")),
                "mfe_pnl_usd": _maybe_float(ev.get("mfe_pnl_usd")),
            })
            continue

        if kind != "exit":
            continue

        exit_shares = _f(ev.get("closed_shares"), 0.0)
        if exit_shares <= 0:
            continue
        remaining = exit_shares
        actual_total = _maybe_float(ev.get("actual_exit_value_usd"))
        observed_total = _maybe_float(ev.get("observed_exit_value_usd"))
        actual_value = actual_total if actual_total is not None and actual_total > 0 else None
        actual_source = str(ev.get("actual_exit_value_source") or ev.get("close_response_value_source") or ev.get("actual_close_response_value_source") or ev.get("pnl_source") or "unavailable")
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

            observed_piece = None
            if observed_total is not None and exit_shares > EPS:
                observed_piece = observed_total * (matched / exit_shares)
                lot["exit_recovered_observed_usd"] += observed_piece
                lot["has_observed"] = True

            lot["remaining_shares"] = max(0.0, available_shares - matched)
            lot["remaining_cost_usd"] = max(0.0, cost_basis - cost_piece)
            lot["matched_shares"] += matched
            lot["matched_cost_usd"] += cost_piece
            lot["close_reason"] = str(ev.get("reason") or lot["close_reason"] or "")
            lot["entry_quality"] = str(ev.get("entry_quality") or lot.get("entry_quality") or "unknown")
            lot["closed_ts"] = str(ev.get("ts") or lot["closed_ts"] or "")
            lot["exit_count"] += 1
            lot["actual_source"] = actual_source
            lot["actual_source_tier"] = actual_tier
            lot["exit_execution_style"] = normalize_execution_style(ev.get("exit_execution_style"))
            lot["mae_pnl_usd"] = _coalesce_extreme(lot.get("mae_pnl_usd"), _maybe_float(ev.get("mae_pnl_usd")), min)
            lot["mfe_pnl_usd"] = _coalesce_extreme(lot.get("mfe_pnl_usd"), _maybe_float(ev.get("mfe_pnl_usd")), max)
            if actual_tier == "medium":
                lot["flags"].append("actual-medium-confidence")
            elif actual_tier == "low":
                lot["flags"].append("actual-low-confidence")
            if actual_value is None and observed_total is not None:
                lot["flags"].append("observed-only")
            lot["legs"].append(TradeLeg(
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
            ))
            remaining -= matched

            if lot["remaining_shares"] <= EPS:
                entry_ev = lot["event"]
                rows.append(_finalize_pair_row(entry_ev, token_id, key, lot))
                lots.popleft()

        if remaining > EPS:
            residual_counter += 1
            actual_piece = actual_value * (remaining / exit_shares) if actual_value is not None and exit_shares > EPS else None
            observed_piece = observed_total * (remaining / exit_shares) if observed_total is not None and exit_shares > EPS else None
            residual_flags = []
            if actual_tier == "medium":
                residual_flags.append("actual-medium-confidence")
            elif actual_tier == "low":
                residual_flags.append("actual-low-confidence")
            if actual_piece is None and observed_piece is not None:
                residual_flags.append("observed-only")
            rows.append(TradePairRow(
                position_id=f"{key}#residual{residual_counter}",
                token_id=token_id,
                market=str(ev.get("slug") or ""),
                side=str(ev.get("side") or ""),
                status="residual",
                opened_ts="",
                closed_ts=str(ev.get("ts") or ""),
                entry_cost_usd=0.0,
                entry_shares=0.0,
                matched_exit_shares=remaining,
                exit_recovered_actual_usd=actual_piece,
                exit_recovered_observed_usd=observed_piece,
                actual_pnl_usd=actual_piece,
                observed_pnl_usd=observed_piece,
                fee_adjusted_actual_pnl_usd=actual_piece,
                fee_adjusted_observed_pnl_usd=observed_piece,
                estimated_total_fees_actual_usd=0.0 if actual_piece is not None else None,
                estimated_total_fees_observed_usd=0.0 if observed_piece is not None else None,
                actual_source=actual_source,
                actual_source_tier=actual_tier,
                entry_execution_style="unknown",
                exit_execution_style=normalize_execution_style(ev.get("exit_execution_style")),
                close_bucket=classify_close_bucket(ev.get("reason")),
                close_reason=str(ev.get("reason") or ""),
                entry_quality=str(ev.get("entry_quality") or "unknown"),
                remaining_shares=0.0,
                unmatched_entry_cost_usd=0.0,
                unmatched_entry_shares=0.0,
                mae_pnl_usd=_maybe_float(ev.get("mae_pnl_usd")),
                mfe_pnl_usd=_maybe_float(ev.get("mfe_pnl_usd")),
                flags=residual_flags,
                legs=[TradeLeg(
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
                )],
            ))

        if key in open_entries and not open_entries[key]:
            open_entries.pop(key, None)

    for key, lots in open_entries.items():
        for lot in lots:
            rows.append(_finalize_pair_row(lot["event"], str(lot["event"].get("token_id") or ""), key, lot))

    rows.sort(key=lambda row: (row.opened_ts or row.closed_ts or "", row.market, row.side, row.position_id))
    return rows


def _finalize_pair_row(entry_ev: dict, token_id: str, key: str, lot: dict) -> TradePairRow:
    entry_cost = _f(entry_ev.get("cost_usd"), 0.0)
    entry_shares = _f(entry_ev.get("shares"), 0.0)
    exit_actual = lot["exit_recovered_actual_usd"] if lot.get("has_actual") else None
    exit_observed = lot["exit_recovered_observed_usd"] if lot.get("has_observed") else None
    matched_cost = float(lot.get("matched_cost_usd") or 0.0)
    actual_pnl = (exit_actual - matched_cost) if exit_actual is not None else None
    observed_pnl = (exit_observed - matched_cost) if exit_observed is not None else None
    entry_execution_style = normalize_execution_style(lot.get("entry_execution_style") or entry_ev.get("execution_style"))
    exit_execution_style = normalize_execution_style(lot.get("exit_execution_style"))
    close_reason = str(lot.get("close_reason") or "")
    _, actual_total_fees, observed_total_fees, _ = estimate_pair_fees(
        matched_cost_usd=matched_cost,
        actual_exit_value_usd=exit_actual,
        observed_exit_value_usd=exit_observed,
        entry_execution_style=entry_execution_style,
        exit_execution_style=exit_execution_style,
        close_reason=close_reason,
    )
    fee_adjusted_actual_pnl = (actual_pnl - actual_total_fees) if actual_pnl is not None and actual_total_fees is not None else None
    fee_adjusted_observed_pnl = (observed_pnl - observed_total_fees) if observed_pnl is not None and observed_total_fees is not None else None
    remaining_shares = float(lot.get("remaining_shares") or 0.0)
    flags = list(dict.fromkeys(lot.get("flags") or []))
    if remaining_shares > EPS:
        flags.append("open-remainder")
    if not lot.get("exit_count"):
        flags.append("no-exit")
    return TradePairRow(
        position_id=str(entry_ev.get("position_id") or key),
        token_id=token_id,
        market=str(entry_ev.get("slug") or ""),
        side=str(entry_ev.get("side") or ""),
        status=classify_pair_status(
            remaining_shares=remaining_shares,
            has_exit=bool(lot.get("exit_count")),
            exit_count=int(lot.get("exit_count") or 0),
            matched_shares=float(lot.get("matched_shares") or 0.0),
            entry_shares=entry_shares,
        ),
        opened_ts=str(entry_ev.get("ts") or ""),
        closed_ts=str(lot.get("closed_ts") or ""),
        entry_cost_usd=entry_cost,
        entry_shares=entry_shares,
        matched_exit_shares=float(lot.get("matched_shares") or 0.0),
        exit_recovered_actual_usd=exit_actual,
        exit_recovered_observed_usd=exit_observed,
        actual_pnl_usd=actual_pnl,
        observed_pnl_usd=observed_pnl,
        fee_adjusted_actual_pnl_usd=fee_adjusted_actual_pnl,
        fee_adjusted_observed_pnl_usd=fee_adjusted_observed_pnl,
        estimated_total_fees_actual_usd=actual_total_fees,
        estimated_total_fees_observed_usd=observed_total_fees,
        actual_source=str(lot.get("actual_source") or "unavailable"),
        actual_source_tier=str(lot.get("actual_source_tier") or "none"),
        entry_execution_style=entry_execution_style,
        exit_execution_style=exit_execution_style,
        close_bucket=classify_close_bucket(close_reason),
        close_reason=close_reason,
        entry_quality=str(lot.get("entry_quality") or "unknown"),
        remaining_shares=remaining_shares,
        unmatched_entry_cost_usd=float(lot.get("remaining_cost_usd") or 0.0),
        unmatched_entry_shares=remaining_shares,
        mae_pnl_usd=lot.get("mae_pnl_usd"),
        mfe_pnl_usd=lot.get("mfe_pnl_usd"),
        flags=flags,
        legs=list(lot.get("legs") or []),
    )


def _coalesce_extreme(current: float | None, candidate: float | None, chooser) -> float | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return chooser(current, candidate)


def dataclass_list_to_json(rows: list[Any], path: str | Path) -> None:
    payload = [asdict(row) for row in rows]
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def dataclass_list_to_csv(rows: list[Any], path: str | Path, *, flatten_legs: bool = False) -> None:
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
    fee_actual_rows = [row for row in rows if row.fee_adjusted_actual_pnl_usd is not None]
    fee_observed_rows = [row for row in rows if row.fee_adjusted_observed_pnl_usd is not None]
    scratch_threshold = max(0.0, float(getattr(SETTINGS, "report_scratch_pnl_pct", 0.03)))

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
        return abs(float(basis_pnl)) / max(float(row.entry_cost_usd), EPS) <= scratch_threshold

    scratch_rows = [row for row in rows if _scratch_like(row)]
    scratch_reasons = Counter(row.close_reason for row in scratch_rows if row.close_reason)

    bucket_summary: dict[str, Any] = {}
    for bucket in sorted(by_bucket):
        bucket_rows = [row for row in rows if row.close_bucket == bucket]
        bucket_summary[bucket] = {
            "count": len(bucket_rows),
            "actual_pnl": _summarize_values(bucket_rows, "actual_pnl_usd"),
            "fee_adjusted_actual_pnl": _summarize_values(bucket_rows, "fee_adjusted_actual_pnl_usd"),
        }

    return {
        "total_trades": total,
        "status_counts": dict(sorted(by_status.items())),
        "entry_quality_counts": dict(sorted(by_quality.items())),
        "close_reason_counts": dict(sorted(by_reason.items())),
        "close_bucket_counts": dict(sorted(by_bucket.items())),
        "close_bucket_pnl": bucket_summary,
        "actual_source_tier_counts": dict(sorted(by_tier.items())),
        "actual_available_ratio": (len(actual_rows) / total) if total else None,
        "actual_pnl": _summarize_values(actual_rows, "actual_pnl_usd"),
        "observed_pnl": _summarize_values(observed_rows, "observed_pnl_usd"),
        "fee_adjusted_actual_pnl": _summarize_values(fee_actual_rows, "fee_adjusted_actual_pnl_usd"),
        "fee_adjusted_observed_pnl": _summarize_values(fee_observed_rows, "fee_adjusted_observed_pnl_usd"),
        "scratch_trades": {
            "count": len(scratch_rows),
            "ratio": (len(scratch_rows) / total) if total else None,
            "close_reason_counts": dict(sorted(scratch_reasons.items())),
            "fee_adjusted_actual_pnl": _summarize_values(scratch_rows, "fee_adjusted_actual_pnl_usd"),
        },
        "mae": {
            "count": sum(1 for row in rows if row.mae_pnl_usd is not None),
            "average": (sum(row.mae_pnl_usd for row in rows if row.mae_pnl_usd is not None) / max(1, sum(1 for row in rows if row.mae_pnl_usd is not None))),
        },
        "mfe": {
            "count": sum(1 for row in rows if row.mfe_pnl_usd is not None),
            "average": (sum(row.mfe_pnl_usd for row in rows if row.mfe_pnl_usd is not None) / max(1, sum(1 for row in rows if row.mfe_pnl_usd is not None))),
        },
        "notes": {
            "actual_unavailable": "actual_pnl average uses only rows with actual data; missing actual rows are excluded, not imputed"
            if len(actual_rows) < total else "all rows have actual pnl",
            "fee_model": "fee_adjusted pnl applies assumed taker fees only to legs tagged taker/mixed; maker-like and unknown legs are treated as zero-fee, and expiry settlements pay no exit fee",
        },
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
        "average_actual_exit_value_usd": (sum(row.actual_exit_value_usd for row in actual_rows) / len(actual_rows)) if actual_rows else None,
        "average_observed_exit_value_usd": (sum(row.observed_exit_value_usd for row in observed_rows) / len(observed_rows)) if observed_rows else None,
        "average_actual_minus_observed_usd": (sum(row.difference_usd for row in diff_rows) / len(diff_rows)) if diff_rows else None,
        "notes": {
            "actual_unavailable": "actual averages use only rows with actual data; unavailable actual values are excluded"
            if len(actual_rows) < total else "all rows have actual exit values",
        },
    }
