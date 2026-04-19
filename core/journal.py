from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from core.runtime_paths import shadow_journal_csv_path, trade_journal_path
from core.config import SETTINGS

def _journal_path():
    return trade_journal_path()


LOT_EPS_SHARES = 0.20
LOT_EPS_COST_USD = 0.10
STALE_HOURS = 6
_JOURNAL_CONTEXT: dict[str, Any] = {}
_SHADOW_CSV_FIELDS = [
    "clob_ts",
    "local_ts",
    "market_slug",
    "side",
    "strategy_name",
    "entry_price",
    "model_probability",
    "effective_probability",
    "raw_edge",
    "required_edge",
    "network_block_reason",
    "reason",
    "regime",
]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_event_id(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def set_journal_context(**context: Any) -> None:
    _JOURNAL_CONTEXT.clear()
    _JOURNAL_CONTEXT.update({k: v for k, v in context.items() if v not in (None, "")})


def clear_journal_context() -> None:
    _JOURNAL_CONTEXT.clear()


def append_event(event: dict) -> dict:
    journal_path = _journal_path()
    row = {
        **_JOURNAL_CONTEXT,
        "market_profile": SETTINGS.market_profile,
        "ts": _now_iso(),
        "event_id": event.get("event_id")
        or new_event_id(str(event.get("kind") or "evt")),
        **event,
    }
    with journal_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def append_shadow_event(event: dict) -> dict:
    """Record a signal that was blocked by execution filters or risk rules."""
    if not SETTINGS.enable_shadow_journal:
        return event
    
    event["kind"] = "shadow_signal"
    return append_event(event)


def append_shadow_csv_row(event: dict, *, path: Path | None = None) -> dict:
    csv_path = path or shadow_journal_csv_path()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    row = {field: event.get(field, "") for field in _SHADOW_CSV_FIELDS}
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SHADOW_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    return row


def read_events(limit: int = 500) -> list[dict]:
    journal_path = _journal_path()
    if not journal_path.exists():
        return []
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    if limit > 0:
        lines = lines[-limit:]
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


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


def format_entry_summary(ev: dict) -> str:
    side = str(ev.get("side") or "")
    slug = str(ev.get("slug") or "")
    shares = _f(ev.get("shares"), 0.0)
    cost_usd = _f(ev.get("cost_usd"), 0.0)
    reason = str(ev.get("entry_reason") or ev.get("reason") or "signal")
    avg_cost = cost_usd / shares if shares > 0 else None

    bits = [
        f"ENTRY {side}".strip(),
        slug,
        f"cost=${cost_usd:.4f}",
        f"shares={shares:.6f}",
    ]
    if avg_cost is not None:
        bits.append(f"avg_cost=${avg_cost:.4f}/share")
    bits.append(f"reason={reason}")
    return " | ".join([b for b in bits if b])


def format_exit_summary(ev: dict) -> str:
    side = str(ev.get("side") or "")
    slug = str(ev.get("slug") or "")
    closed_shares = _f(ev.get("closed_shares"), 0.0)
    remaining_shares = _f(ev.get("remaining_shares"), 0.0)
    realized_cost_usd = _f(ev.get("realized_cost_usd"), 0.0)
    reason = str(ev.get("reason") or "")

    actual_exit_value_usd = _maybe_float(ev.get("actual_exit_value_usd"))
    actual_realized_pnl_usd = _maybe_float(ev.get("actual_realized_pnl_usd"))
    observed_exit_value_usd = _maybe_float(ev.get("observed_exit_value_usd"))
    observed_realized_pnl_usd = _maybe_float(ev.get("observed_realized_pnl_usd"))
    actual_source = str(
        ev.get("actual_exit_value_source") or ev.get("pnl_source") or ""
    )
    observed_source = str(ev.get("observed_exit_value_source") or "observed_mark_price")

    bits = [
        f"EXIT {side}".strip(),
        slug,
        f"cost=${realized_cost_usd:.4f}",
        f"closed_shares={closed_shares:.6f}",
    ]
    if actual_exit_value_usd is not None:
        actual_bits = f"actual_recovered=${actual_exit_value_usd:.4f}"
        if actual_realized_pnl_usd is not None:
            actual_bits += f" pnl={actual_realized_pnl_usd:+.4f}"
        if actual_source:
            actual_bits += f" src={actual_source}"
        bits.append(actual_bits)
    else:
        bits.append("actual_recovered=n/a")

    if observed_exit_value_usd is not None:
        observed_bits = f"observed_est=${observed_exit_value_usd:.4f}"
        if observed_realized_pnl_usd is not None:
            observed_bits += f" pnl={observed_realized_pnl_usd:+.4f}"
        if observed_source:
            observed_bits += f" src={observed_source}"
        bits.append(observed_bits)

    bits.append(f"remaining_shares={remaining_shares:.6f}")
    bits.append(f"reason={reason}")
    return " | ".join([b for b in bits if b])


def replay_open_positions(events: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    lots: dict[str, dict] = {}
    notes: list[dict] = []

    for ev in events:
        kind = str(ev.get("kind") or "")
        token_id = str(ev.get("token_id") or "")
        if not token_id:
            continue

        if kind == "entry":
            shares = _f(ev.get("shares"), 0.0)
            cost_usd = _f(ev.get("cost_usd"), 0.0)
            if shares <= 0 or cost_usd <= 0:
                notes.append(
                    {
                        "ts": ev.get("ts"),
                        "kind": "reconcile_note",
                        "token_id": token_id,
                        "slug": ev.get("slug"),
                        "side": ev.get("side"),
                        "level": "warning",
                        "note": "entry ignored because shares/cost_usd were invalid",
                    }
                )
                continue
            lots[token_id] = {
                "slug": str(ev.get("slug") or ""),
                "side": str(ev.get("side") or ""),
                "token_id": token_id,
                "shares": shares,
                "cost_usd": cost_usd,
                "opened_ts": _f(ev.get("opened_ts"), 0.0),
                "position_id": ev.get("position_id")
                or ev.get("event_id")
                or new_event_id("pos"),
                "entry_event_id": ev.get("event_id"),
                "source": ev.get("source") or "journal",
                "entry_reason": ev.get("entry_reason") or ev.get("reason") or "signal",
                "max_favorable_value_usd": _f(ev.get("mfe_value_usd"), cost_usd),
                "max_adverse_value_usd": _f(ev.get("mae_value_usd"), cost_usd),
                "max_favorable_pnl_usd": _f(ev.get("mfe_pnl_usd"), 0.0),
                "max_adverse_pnl_usd": _f(ev.get("mae_pnl_usd"), 0.0),
            }
            continue

        if kind != "exit":
            continue

        lot = lots.get(token_id)
        closed_shares = _f(ev.get("closed_shares"), 0.0)
        remaining_hint = ev.get("remaining_shares")
        remaining_hint_f = (
            _f(remaining_hint, -1.0) if remaining_hint is not None else -1.0
        )
        if lot is None:
            notes.append(
                {
                    "ts": ev.get("ts"),
                    "kind": "reconcile_note",
                    "token_id": token_id,
                    "slug": ev.get("slug"),
                    "side": ev.get("side"),
                    "level": "warning",
                    "note": "exit without matching open entry in local journal",
                    "exit_event_id": ev.get("event_id"),
                }
            )
            continue
        if closed_shares <= 0:
            notes.append(
                {
                    "ts": ev.get("ts"),
                    "kind": "reconcile_note",
                    "token_id": token_id,
                    "slug": ev.get("slug"),
                    "side": ev.get("side"),
                    "level": "warning",
                    "note": "exit ignored because closed_shares <= 0",
                    "exit_event_id": ev.get("event_id"),
                }
            )
            continue

        lot["max_favorable_pnl_usd"] = max(
            lot.get("max_favorable_pnl_usd", 0.0),
            _f(ev.get("mfe_pnl_usd"), lot.get("max_favorable_pnl_usd", 0.0)),
        )
        lot["max_adverse_pnl_usd"] = min(
            lot.get("max_adverse_pnl_usd", 0.0),
            _f(ev.get("mae_pnl_usd"), lot.get("max_adverse_pnl_usd", 0.0)),
        )
        lot["max_favorable_value_usd"] = max(
            lot.get("max_favorable_value_usd", lot["cost_usd"]),
            _f(
                ev.get("mfe_value_usd"),
                lot.get("max_favorable_value_usd", lot["cost_usd"]),
            ),
        )
        lot["max_adverse_value_usd"] = min(
            lot.get("max_adverse_value_usd", lot["cost_usd"]),
            _f(
                ev.get("mae_value_usd"),
                lot.get("max_adverse_value_usd", lot["cost_usd"]),
            ),
        )

        effective_closed = min(lot["shares"], closed_shares)
        close_fraction = effective_closed / max(lot["shares"], 1e-9)
        realized_cost = lot["cost_usd"] * close_fraction
        lot["shares"] = max(0.0, lot["shares"] - effective_closed)
        lot["cost_usd"] = max(0.0, lot["cost_usd"] - realized_cost)

        if remaining_hint_f >= 0:
            # trust explicit remaining_shares from execution more than accumulated subtraction
            if abs(remaining_hint_f - lot["shares"]) > LOT_EPS_SHARES:
                notes.append(
                    {
                        "ts": ev.get("ts"),
                        "kind": "reconcile_note",
                        "token_id": token_id,
                        "slug": ev.get("slug"),
                        "side": ev.get("side"),
                        "level": "info",
                        "note": "remaining_shares hint differed from reconstructed lot; journal used execution hint",
                        "reconstructed_remaining_shares": lot["shares"],
                        "remaining_shares_hint": remaining_hint_f,
                        "exit_event_id": ev.get("event_id"),
                    }
                )
            if remaining_hint_f <= LOT_EPS_SHARES:
                lot["shares"] = 0.0
                lot["cost_usd"] = 0.0
            else:
                lot["shares"] = remaining_hint_f
                if lot["shares"] <= LOT_EPS_SHARES:
                    lot["cost_usd"] = 0.0

        if lot["shares"] <= LOT_EPS_SHARES or lot["cost_usd"] <= LOT_EPS_COST_USD:
            lots.pop(token_id, None)

    return lots, notes


def summarize_reconciliation(events: list[dict]) -> dict:
    lots, notes = replay_open_positions(events)
    open_lots = []
    now = time.time()
    for token_id, lot in list(lots.items()):
        age_hours = (
            (now - float(lot.get("opened_ts") or 0.0)) / 3600
            if lot.get("opened_ts")
            else None
        )
        is_stale = bool(age_hours is not None and age_hours >= STALE_HOURS)
        note = "entry remains open/unreconciled in local journal; verify against live wallet/history"
        level = "warning"
        if is_stale:
            note = "stale open lot in local journal; exclude from active bot recovery unless manually verified"
            level = "info"
        notes.append(
            {
                "ts": None,
                "kind": "reconcile_note",
                "token_id": token_id,
                "slug": lot.get("slug"),
                "side": lot.get("side"),
                "level": level,
                "note": note,
                "remaining_shares": lot.get("shares"),
                "remaining_cost_usd": lot.get("cost_usd"),
                "position_id": lot.get("position_id"),
                "age_hours": age_hours,
                "stale": is_stale,
            }
        )
        tagged = dict(lot)
        tagged["stale"] = is_stale
        tagged["age_hours"] = age_hours
        open_lots.append(tagged)
    return {
        "open_lots": open_lots,
        "notes": notes,
    }
