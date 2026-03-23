import signal
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from random import uniform

from core.config import SETTINGS
from core.decision_engine import choose_side, explain_choose_side, get_outcome_prices, seconds_to_market_end
from core.exchange import PolymarketExchange, Position
from core.hedge_logic import should_trigger_dump
from core.notifier import notify_discord
from core.risk import RiskState, can_place_order, current_5min_key, update_window
from core.market_resolver import resolve_latest_btc_5m_token_ids, MarketResolutionError
from core.run_journal import RunJournal
from core.state_store import load_state, save_state
from core.trade_manager import decide_exit, maybe_reverse_entry, can_reenter_same_market
from core.ws_binance import BINANCE_WS
from core.indicators import compute_buy_sell_pressure
from core.journal import (
    LOT_EPS_COST_USD,
    LOT_EPS_SHARES,
    STALE_HOURS,
    append_event,
    replay_open_positions,
    read_events,
    format_exit_summary,
)


def smart_sleep(seconds: float):
    sleep_start = time.time()
    while time.time() - sleep_start < seconds:
        try:
            wt = BINANCE_WS.get_recent_trades(seconds=60.0)
            bv, sv = compute_buy_sell_pressure(wt)
            tv = bv + sv
            if tv > 50000:
                ofi = bv / max(tv, 1e-9)
                if ofi > 0.70 or ofi < 0.30:
                    log(f"EVENT INTERRUPT: OFI={ofi:.2f} Vol=${tv:.0f} -> forcing fast poll")
                    break
        except Exception:
            pass
        time.sleep(1.0)



STATE_VERSION = 2


class GracefulStop(SystemExit):
    pass


STOP_REQUEST = {"signal": None}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


@dataclass
class OpenPos:
    slug: str
    side: str
    token_id: str
    shares: float
    cost_usd: float
    opened_ts: float
    position_id: str = ""
    entry_reason: str = "signal"
    source: str = "runtime"
    last_synced_size: float = 0.0
    last_synced_initial_value: float = 0.0
    last_synced_current_value: float = 0.0
    last_synced_cash_pnl: float = 0.0
    last_synced_at: float = 0.0
    live_miss_count: int = 0
    pending_confirmation: bool = False
    max_favorable_value_usd: float = 0.0
    max_adverse_value_usd: float = 0.0
    max_favorable_pnl_usd: float = 0.0
    max_adverse_pnl_usd: float = 0.0
    has_scaled_out: bool = False
    has_scaled_out_loss: bool = False
    has_taken_partial: bool = False
    has_extracted_principal: bool = False
    has_panic_dumped: bool = False
    dust_retry_count: int = 0  # Number of times this residual lot has been kept for retry

    @property
    def avg_cost_per_share(self) -> float:
        return self.cost_usd / max(self.shares, 1e-9)


@dataclass
class PendingOrder:
    order_id: str
    slug: str
    side: str
    token_id: str
    placed_ts: float
    order_usd: float


@dataclass
class RuntimeFlags:
    live_consec_losses: int
    last_loss_side: str
    close_fail_streak: int
    panic_exit_mode: bool


def realistic_exit_value(pos: OpenPos, up: float | None, down: float | None, ob_up: dict | None, ob_down: dict | None) -> float | None:
    mark = up if pos.side == "UP" else down
    if mark is None:
        return None
    best_bid = None
    if pos.side == "UP" and ob_up:
        best_bid = ob_up.get("best_bid")
    elif pos.side == "DOWN" and ob_down:
        best_bid = ob_down.get("best_bid")
    
    if best_bid is not None and float(best_bid) > 0:
        return pos.shares * float(best_bid)
    
    # Without orderbook depth passed in Dry Run polling, we assume Maker/Limit orders track the mark exactly over time without massive taker penalties.
    return pos.shares * float(mark)


def observed_mark_value(pos: OpenPos, up: float | None, down: float | None) -> float | None:
    mark = up if pos.side == "UP" else down
    if mark is None:
        return None
    return pos.shares * float(mark)


def update_position_excursions(pos: OpenPos, observed_value: float | None) -> None:
    if observed_value is None:
        return
    pnl = observed_value - pos.cost_usd
    if pos.max_favorable_value_usd <= 0:
        pos.max_favorable_value_usd = observed_value
        pos.max_adverse_value_usd = observed_value
        pos.max_favorable_pnl_usd = pnl
        pos.max_adverse_pnl_usd = pnl
        return
    pos.max_favorable_value_usd = max(pos.max_favorable_value_usd, observed_value)
    pos.max_adverse_value_usd = min(pos.max_adverse_value_usd, observed_value)
    pos.max_favorable_pnl_usd = max(pos.max_favorable_pnl_usd, pnl)
    pos.max_adverse_pnl_usd = min(pos.max_adverse_pnl_usd, pnl)


def position_age_hours(opened_ts: float | None) -> float | None:
    if not opened_ts:
        return None
    return max(0.0, (time.time() - float(opened_ts)) / 3600.0)


def inspect_open_position(pos: OpenPos, live_pos: Position | None = None) -> tuple[list[str], dict]:
    notes: list[str] = []
    age_hours = position_age_hours(pos.opened_ts)
    worthless = False
    stale = False

    if pos.shares <= LOT_EPS_SHARES:
        worthless = True
        notes.append(f"shares<=eps({pos.shares:.6f})")
    if pos.cost_usd <= LOT_EPS_COST_USD:
        worthless = True
        notes.append(f"cost<=eps({pos.cost_usd:.6f})")
    if age_hours is not None and age_hours >= STALE_HOURS:
        stale = True
        notes.append(f"age>={STALE_HOURS}h({age_hours:.2f}h)")

    if live_pos is not None:
        if float(live_pos.size) <= LOT_EPS_SHARES:
            worthless = True
            notes.append(f"live_size<=eps({float(live_pos.size):.6f})")
        if float(live_pos.current_value) <= 0 and float(live_pos.percent_pnl) <= -99.9:
            worthless = True
            notes.append(
                f"live_current_value={float(live_pos.current_value):.6f},live_percent_pnl={float(live_pos.percent_pnl):.2f}"
            )

    return notes, {
        "worthless": worthless,
        "stale": stale,
        "age_hours": age_hours,
    }


def sanitize_open_positions(open_positions: list[OpenPos], *, live_positions: list[Position] | None = None, source: str = "runtime") -> tuple[list[OpenPos], list[str]]:
    actual = {p.token_id: p for p in (live_positions or [])}
    kept: list[OpenPos] = []
    notes: list[str] = []
    for pos in open_positions:
        row_notes, flags = inspect_open_position(pos, actual.get(pos.token_id))
        if flags["worthless"] or flags["stale"]:
            reason_bits = ", ".join(row_notes) or "unknown"
            notes.append(
                f"sanitize_drop[{source}] token={pos.token_id} slug={pos.slug} side={pos.side} reason={reason_bits}"
            )
            continue
        kept.append(pos)
    return kept, notes


def merge_recovery_positions(runtime_positions: list[OpenPos], rebuilt_positions: list[OpenPos]) -> tuple[list[OpenPos], list[str]]:
    merged: dict[str, OpenPos] = {}
    notes: list[str] = []

    def _pick(base: OpenPos, incoming: OpenPos) -> OpenPos:
        chosen = OpenPos(**base.__dict__)
        if not chosen.slug and incoming.slug:
            chosen.slug = incoming.slug
        if not chosen.side and incoming.side:
            chosen.side = incoming.side
        if not chosen.position_id and incoming.position_id:
            chosen.position_id = incoming.position_id
        if (not chosen.entry_reason or chosen.entry_reason == "signal") and incoming.entry_reason:
            chosen.entry_reason = incoming.entry_reason
        if chosen.opened_ts <= 0 and incoming.opened_ts > 0:
            chosen.opened_ts = incoming.opened_ts
        if chosen.last_synced_at <= 0 < incoming.last_synced_at:
            chosen.last_synced_at = incoming.last_synced_at
            chosen.last_synced_size = incoming.last_synced_size
            chosen.last_synced_initial_value = incoming.last_synced_initial_value
            chosen.last_synced_current_value = incoming.last_synced_current_value
            chosen.last_synced_cash_pnl = incoming.last_synced_cash_pnl
        chosen.max_favorable_value_usd = max(chosen.max_favorable_value_usd, incoming.max_favorable_value_usd)
        if chosen.max_adverse_value_usd <= 0:
            chosen.max_adverse_value_usd = incoming.max_adverse_value_usd
        elif incoming.max_adverse_value_usd > 0:
            chosen.max_adverse_value_usd = min(chosen.max_adverse_value_usd, incoming.max_adverse_value_usd)
        chosen.max_favorable_pnl_usd = max(chosen.max_favorable_pnl_usd, incoming.max_favorable_pnl_usd)
        chosen.max_adverse_pnl_usd = min(chosen.max_adverse_pnl_usd, incoming.max_adverse_pnl_usd)
        return chosen

    for pos in runtime_positions:
        merged[pos.token_id] = pos

    for pos in rebuilt_positions:
        existing = merged.get(pos.token_id)
        if existing is None:
            merged[pos.token_id] = pos
            continue
        merged[pos.token_id] = _pick(existing, pos)
        notes.append(
            f"recovery_merge token={pos.token_id} slug={merged[pos.token_id].slug} kept_source={existing.source} merged_source={pos.source}"
        )

    return list(merged.values()), notes


def sync_open_positions(ex, open_positions: list[OpenPos]) -> tuple[list[OpenPos], list[str]]:
    # Only reconcile positions already tracked by this bot.
    # This avoids importing unrelated legacy holdings from the wallet.
    live_list = ex.get_positions()
    actual = {p.token_id: p for p in live_list}
    # Track whether data-api actually returned data (non-empty response)
    # If empty, it could be an API hiccup — do NOT penalize positions with miss counts.
    api_returned_data = bool(actual)

    if not api_returned_data:
        # Data API returned nothing — could be down or delayed.
        # Hold all positions as-is but don't increment miss_count.
        sanitized, notes = sanitize_open_positions(open_positions, source="runtime-no-live")
        notes.insert(0, "sync_hold_all: data-api returned empty (no positions), holding all without miss penalty")
        return sanitized, notes

    synced: list[OpenPos] = []
    notes: list[str] = []
    for p in open_positions:
        ap = actual.get(p.token_id)
        if ap is None or ap.size <= 0:
            age_sec = max(0.0, time.time() - float(p.opened_ts or 0.0)) if p.opened_ts else 999999.0
            miss_count = int(getattr(p, "live_miss_count", 0) or 0) + 1  # Only increment when API responded!
            # Give ALL missing positions a longer grace period to survive API delays/hiccups
            in_grace = age_sec <= 300 and miss_count <= 5
            if in_grace:
                held = OpenPos(**p.__dict__)
                held.live_miss_count = miss_count
                synced.append(held)
                notes.append(
                    f"sync_hold token={p.token_id} slug={p.slug} reason=missing-live-position age_sec={age_sec:.1f} miss_count={miss_count}"
                )
                continue
            notes.append(f"sync_drop token={p.token_id} slug={p.slug} reason=missing-live-position age_sec={age_sec:.1f} miss_count={miss_count}")
            continue
        assert ap is not None
        row_notes, flags = inspect_open_position(p, ap)
        if flags["worthless"] or flags["stale"]:
            notes.append(
                f"sync_drop token={p.token_id} slug={p.slug} reason={', '.join(row_notes) or 'stale-or-worthless'}"
            )
            continue
        synced.append(OpenPos(
            slug=p.slug,
            side=p.side,
            token_id=p.token_id,
            shares=float(ap.size),
            cost_usd=float(ap.initial_value) if ap.initial_value > 0 else p.cost_usd,
            opened_ts=p.opened_ts,
            position_id=p.position_id,
            entry_reason=p.entry_reason,
            source=p.source,
            last_synced_size=float(ap.size),
            last_synced_initial_value=float(ap.initial_value),
            last_synced_current_value=float(ap.current_value),
            last_synced_cash_pnl=float(ap.cash_pnl),
            last_synced_at=time.time(),
            live_miss_count=0,
            pending_confirmation=False,
            max_favorable_value_usd=p.max_favorable_value_usd,
            max_adverse_value_usd=p.max_adverse_value_usd,
            max_favorable_pnl_usd=p.max_favorable_pnl_usd,
            max_adverse_pnl_usd=p.max_adverse_pnl_usd,
            has_scaled_out=getattr(p, "has_scaled_out", False),
            has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
            has_taken_partial=getattr(p, "has_taken_partial", False),
            has_extracted_principal=getattr(p, "has_extracted_principal", False),
        ))
    return synced, notes



def rebuild_positions_from_journal() -> tuple[list[OpenPos], list[str]]:
    events = read_events(limit=1000)
    positions: list[OpenPos] = []
    notes_out: list[str] = []
    lots, notes = replay_open_positions(events)
    now_ts = time.time()
    for note in notes:
        msg = f"journal reconcile note | {note.get('note')} | token={note.get('token_id')}"
        log(msg)
        notes_out.append(msg)
    for token_id, lot in lots.items():
        opened_ts = float(lot.get("opened_ts", 0.0) or 0.0)
        age_hours = ((now_ts - opened_ts) / 3600.0) if opened_ts > 0 else 999.0
        shares = float(lot.get("shares", 0.0) or 0.0)
        cost_usd = float(lot.get("cost_usd", 0.0) or 0.0)

        # Do not resurrect stale/legacy residual lots into active runtime state.
        if age_hours >= STALE_HOURS or shares <= LOT_EPS_SHARES or cost_usd <= LOT_EPS_COST_USD:
            msg = (
                f"ignore stale journal lot | token={token_id} age_h={age_hours:.1f} "
                f"shares={shares:.6f} cost={cost_usd:.4f}"
            )
            log(msg)
            notes_out.append(msg)
            continue

        positions.append(OpenPos(
            slug=str(lot.get("slug") or ""),
            side=str(lot.get("side") or ""),
            token_id=token_id,
            shares=shares,
            cost_usd=cost_usd,
            opened_ts=opened_ts,
            position_id=str(lot.get("position_id") or ""),
            entry_reason=str(lot.get("entry_reason") or "signal"),
            source="journal-rebuild",
            max_favorable_value_usd=float(lot.get("max_favorable_value_usd", cost_usd) or cost_usd),
            max_adverse_value_usd=float(lot.get("max_adverse_value_usd", cost_usd) or cost_usd),
            max_favorable_pnl_usd=float(lot.get("max_favorable_pnl_usd", 0.0) or 0.0),
            max_adverse_pnl_usd=float(lot.get("max_adverse_pnl_usd", 0.0) or 0.0),
        ))
    return positions, notes_out


def load_runtime_flags(state: dict, open_positions: list[OpenPos]) -> RuntimeFlags:
    live_consec_losses = int(state.get("live_consec_losses", 0))
    last_loss_side = state.get("last_loss_side", "")
    close_fail_streak = int(state.get("close_fail_streak", 0))
    panic_exit_mode = bool(state.get("panic_exit_mode", False))

    if not open_positions:
        close_fail_streak = 0
        panic_exit_mode = False

    active_market_slugs = {p.slug for p in open_positions if p.slug}
    panic_market_slug = str(state.get("panic_market_slug") or "")
    if panic_market_slug and panic_market_slug not in active_market_slugs:
        close_fail_streak = 0
        panic_exit_mode = False

    return RuntimeFlags(
        live_consec_losses=live_consec_losses,
        last_loss_side=last_loss_side,
        close_fail_streak=close_fail_streak,
        panic_exit_mode=panic_exit_mode,
    )


def save_runtime_state(
    risk: RiskState,
    *,
    last_market_slug: str,
    yes_price_window: deque,
    up_price_window: deque,
    down_price_window: deque,
    last_trade_ts: float,
    prev_up,
    prev_down,
    error_cooldown_until: float,
    open_positions: list[OpenPos],
    flags: RuntimeFlags,
    last_cycle_label: str,
    panic_market_slug: str,
):
    sanitized_positions, _ = sanitize_open_positions(open_positions, source="save-runtime")
    save_state({
        "state_version": STATE_VERSION,
        "risk_daily_pnl": risk.daily_pnl,
        "risk_orders_this_window": risk.orders_this_window,
        "risk_window_key": risk.window_key,
        "risk_consec_losses": risk.consec_losses,
        "last_market_slug": last_market_slug,
        "yes_price_window": list(yes_price_window),
        "up_price_window": list(up_price_window),
        "down_price_window": list(down_price_window),
        "last_trade_ts": last_trade_ts,
        "prev_up": prev_up,
        "prev_down": prev_down,
        "error_cooldown_until": error_cooldown_until,
        "open_positions": [p.__dict__ for p in sanitized_positions],
        "live_consec_losses": flags.live_consec_losses,
        "last_loss_side": flags.last_loss_side,
        "close_fail_streak": flags.close_fail_streak,
        "panic_exit_mode": flags.panic_exit_mode,
        "panic_market_slug": panic_market_slug,
        "last_cycle_label": last_cycle_label,
        "last_cycle_payload": {},
    })


def maybe_record_cycle_label(state: dict, label: str, **payload):
    prev = str(state.get("last_cycle_label") or "")
    signature = {k: payload[k] for k in sorted(payload)}
    prev_sig = state.get("last_cycle_payload") or {}
    if prev == label and prev_sig == signature:
        return
    append_event({
        "kind": "cycle_label",
        "label": label,
        **payload,
    })
    state["last_cycle_label"] = label
    state["last_cycle_payload"] = signature


def perform_startup_sanity_check(ex: PolymarketExchange, state: dict) -> tuple[list[OpenPos], list[str], bool, bool]:
    notes: list[str] = []
    recovery_restart = False

    runtime_positions = [OpenPos(**dict(p)) for p in state.get("open_positions", []) if isinstance(p, dict)]
    runtime_positions, runtime_notes = sanitize_open_positions(runtime_positions, source="runtime-state")
    notes.extend(runtime_notes)

    rebuilt_positions, rebuild_notes = rebuild_positions_from_journal()
    notes.extend(rebuild_notes)

    live_positions = ex.get_positions()
    tracked_tokens = {p.token_id for p in runtime_positions} | {p.token_id for p in rebuilt_positions}
    live_notes: list[str] = []
    for live_pos in live_positions:
        if live_pos.token_id not in tracked_tokens:
            continue
        stub = OpenPos(
            slug="",
            side="",
            token_id=live_pos.token_id,
            shares=float(live_pos.size),
            cost_usd=float(live_pos.initial_value),
            opened_ts=0.0,
            source="live-position",
        )
        row_notes, flags = inspect_open_position(stub, live_pos)
        if flags["worthless"] or flags["stale"]:
            live_notes.append(
                f"sanitize_drop[live] token={live_pos.token_id} reason={', '.join(row_notes) or 'stale-or-worthless'}"
            )
    notes.extend(live_notes)

    merged_positions, merge_notes = merge_recovery_positions(runtime_positions, rebuilt_positions)
    notes.extend(merge_notes)

    sanitized_positions, final_notes = sanitize_open_positions(merged_positions, live_positions=live_positions, source="startup-final")
    notes.extend(final_notes)

    runtime_state_changed = sanitized_positions != runtime_positions

    if notes:
        recovery_restart = True
        append_event({
            "kind": "startup_sanity",
            "status": "sanitized",
            "notes": notes,
            "runtime_candidates": len(runtime_positions),
            "journal_candidates": len(rebuilt_positions),
            "live_positions": len(live_positions),
            "kept_positions": len(sanitized_positions),
            "merged_candidates": len(merged_positions),
        })
    else:
        append_event({
            "kind": "startup_sanity",
            "status": "clean",
            "runtime_candidates": len(runtime_positions),
            "journal_candidates": len(rebuilt_positions),
            "live_positions": len(live_positions),
            "kept_positions": len(sanitized_positions),
            "merged_candidates": len(merged_positions),
        })

    for note in notes:
        log(f"startup sanity | {note}")
    return sanitized_positions, notes, recovery_restart, runtime_state_changed


def install_signal_handlers(run_journal: RunJournal):
    def _handle(sig, _frame):
        STOP_REQUEST["signal"] = sig
        run_journal.mark_signal(sig)
        raise GracefulStop(f"received signal {sig}")

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def main():
    ex = PolymarketExchange(dry_run=SETTINGS.dry_run)
    risk = RiskState()
    state = load_state()
    open_positions, startup_notes, recovery_restart, runtime_state_changed = perform_startup_sanity_check(ex, state)
    run_journal = RunJournal(notes=startup_notes, recovery_restart=recovery_restart)
    install_signal_handlers(run_journal)

    log(f"bot started | dry_run={SETTINGS.dry_run}")

    risk.daily_pnl = float(state.get("risk_daily_pnl", 0.0))
    risk.orders_this_window = int(state.get("risk_orders_this_window", 0))
    risk.window_key = state.get("risk_window_key", "")
    risk.consec_losses = int(state.get("risk_consec_losses", 0))

    last_market_slug = state.get("last_market_slug", "")
    yes_price_window: deque = deque(state.get("yes_price_window", []), maxlen=max(5, SETTINGS.zscore_window))
    up_price_window: deque = deque(state.get("up_price_window", []), maxlen=max(5, SETTINGS.momentum_ticks + 2))
    down_price_window: deque = deque(state.get("down_price_window", []), maxlen=max(5, SETTINGS.momentum_ticks + 2))
    last_trade_ts = float(state.get("last_trade_ts", time.time()))
    prev_up = state.get("prev_up")
    prev_down = state.get("prev_down")
    error_cooldown_until = float(state.get("error_cooldown_until", 0.0))
    flags = load_runtime_flags(state, open_positions)
    panic_market_slug = str(state.get("panic_market_slug") or "")

    if runtime_state_changed:
        save_runtime_state(
            risk,
            last_market_slug=last_market_slug,
            yes_price_window=yes_price_window,
            up_price_window=up_price_window,
            down_price_window=down_price_window,
            last_trade_ts=last_trade_ts,
            prev_up=prev_up,
            prev_down=prev_down,
            error_cooldown_until=error_cooldown_until,
            open_positions=open_positions,
            flags=flags,
            last_cycle_label=state.get("last_cycle_label", ""),
            panic_market_slug=panic_market_slug,
        )
        log(f"startup sanity persisted runtime state | open_positions={len(open_positions)}")

    try:
        from core.ws_binance import BINANCE_WS
        BINANCE_WS.start()
    except Exception as e:
        log(f"Failed to start WS: {e}")

    last_rest_query_ts = 0.0

    try:
        while True:
            time_since_last_query = time.time() - last_rest_query_ts
            if time_since_last_query < 3.0:
                time.sleep(3.0 - time_since_last_query)
            last_rest_query_ts = time.time()

            now = datetime.now()
            key = current_5min_key(now)
            update_window(risk, key)

            try:
                acct = ex.get_account()
                open_positions, sync_notes = sync_open_positions(ex, open_positions)
                for note in sync_notes:
                    log(note)
            except Exception as sync_err:
                log(f"API sync error (account/positions): {sync_err}")
                smart_sleep(SETTINGS.poll_seconds)
                continue
            flags = load_runtime_flags({
                "live_consec_losses": flags.live_consec_losses,
                "last_loss_side": flags.last_loss_side,
                "close_fail_streak": flags.close_fail_streak,
                "panic_exit_mode": flags.panic_exit_mode,
                "panic_market_slug": panic_market_slug,
            }, open_positions)

            # --- PENDING ORDERS / KILL-SWITCH ---
            if 'pending_orders' not in locals():
                pending_orders = []
            
            if pending_orders:
                try:
                    open_clob_orders = ex.get_open_orders()
                    open_order_ids = {o.get("orderID") for o in open_clob_orders} if isinstance(open_clob_orders, list) else set()
                    
                    ws_vel = 0.0
                    try:
                        ws_vel = BINANCE_WS.get_price_velocity(3.0)
                    except Exception:
                        pass
                        
                    for po in list(pending_orders):
                        if po.order_id and po.order_id not in open_order_ids:
                            log(f"Pending order {po.order_id} filled or cancelled on CLOB.")
                            open_positions.append(OpenPos(
                                slug=po.slug,
                                side=po.side,
                                token_id=po.token_id,
                                shares=0.0001, # placeholder till next sync
                                cost_usd=po.order_usd,
                                opened_ts=time.time(),
                                position_id=f"pos_{int(time.time())}_{po.token_id[-6:]}",
                                entry_reason="maker-fill",
                                source="live-order",
                                pending_confirmation=True,
                                max_favorable_value_usd=po.order_usd,
                            ))
                            pending_orders.remove(po)
                            continue
                            
                        # Kill-Switch: Cancel if adverse velocity
                        if (po.side == "UP" and ws_vel < -SETTINGS.cancel_on_reversal_velocity) or \
                           (po.side == "DOWN" and ws_vel > SETTINGS.cancel_on_reversal_velocity):
                            log(f"KILL-SWITCH TRIGGERED on {po.side} {po.order_id} (velocity: {ws_vel:.4f})")
                            ex.cancel_order(po.order_id)
                            pending_orders.remove(po)
                            continue
                            
                        # Timeout
                        if time.time() - po.placed_ts > getattr(SETTINGS, "maker_order_timeout_sec", 15):
                            log(f"MAKER TIMEOUT on {po.side} {po.order_id}")
                            ex.cancel_order(po.order_id)
                            pending_orders.remove(po)
                            continue
                except Exception as e:
                    log(f"Pending orders check error: {e}")

            market = None
            token_override = None
            signal_side = None
            signal_origin = ""
            no_entry_reason = ""

            # Daily loss circuit breaker restored
            if risk.daily_pnl <= -SETTINGS.daily_max_loss:
                log(f"CIRCUIT BREAKER: Daily loss (-${abs(risk.daily_pnl):.2f}) reached limit (-${SETTINGS.daily_max_loss:.2f}). Pausing new entries.")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if SETTINGS.auto_market_selection:
                try:
                    market = resolve_latest_btc_5m_token_ids()
                    if market["slug"] != last_market_slug:
                        last_market_slug = market["slug"]
                        log(f"market switched => {market['slug']}")
                        
                    if getattr(SETTINGS, "dry_run", False):
                        ghosts = [p for p in open_positions if p.slug != market["slug"]]
                        for gp in ghosts:
                            log(f"Force-clearing stale dry-run position from expired market: {gp.slug}")
                            append_event({
                                "kind": "exit",
                                "slug": gp.slug,
                                "side": gp.side,
                                "token_id": gp.token_id,
                                "position_id": gp.position_id,
                                "closed_shares": gp.shares,
                                "remaining_shares": 0.0,
                                "realized_cost_usd": gp.cost_usd,
                                "actual_exit_value_usd": gp.cost_usd,
                                "observed_exit_value_usd": gp.cost_usd,
                                "status": "closed",
                                "exit_reason": "dry-run-market-expired"
                            })
                            open_positions.remove(gp)
                            
                    prices = get_outcome_prices(market)
                    up = prices.get("up") or prices.get("漲")
                    down = prices.get("down") or prices.get("跌")
                    secs_left = seconds_to_market_end(market)

                    if up is not None:
                        up_price_window.append(float(up))
                        yes_price_window.append(float(up))
                    if down is not None:
                        down_price_window.append(float(down))

                    binance_1m = ex.get_binance_1m_candle() if SETTINGS.use_cex_oracle else None
                    binance_5m = ex.get_binance_5m_klines(100)

                    try:
                        ws_bba = BINANCE_WS.get_bba()
                        ws_trades = BINANCE_WS.get_recent_trades(seconds=60.0)
                    except Exception:
                        ws_bba = None
                        ws_trades = None

                    if SETTINGS.use_dynamic_thresholds and binance_1m:
                        change_abs = abs(binance_1m.get("change", 0.0))
                        if change_abs > 30.0:
                            # widen stop loss slightly in high vol, but not 40%
                            SETTINGS.stop_loss_pct = max(SETTINGS.stop_loss_pct, 0.15)
                            SETTINGS.zscore_threshold = max(SETTINGS.zscore_threshold, 2.5)
                        else:
                            from core.config import _f
                            SETTINGS.stop_loss_pct = _f("STOP_LOSS_PCT", 0.10)
                            SETTINGS.zscore_threshold = _f("ZSCORE_THRESHOLD", 2.0)

                    arbitrage_triggered = False
                    from core.decision_engine import check_arbitrage
                    if check_arbitrage(up, down):
                        log(f"ARBITRAGE DETECTED! up={up} down={down} sum={up+down}")
                        res_up = ex.place_order("UP", 1.0, market.get("token_up"), simulated_price=float(up) if up is not None else None)
                        res_down = ex.place_order("DOWN", 1.0, market.get("token_down"), simulated_price=float(down) if down is not None else None)
                        log(f"Arbitrage execution: UP={res_up} DOWN={res_down}")
                        maybe_record_cycle_label(state, "arbitrage-execution", slug=market["slug"], up=up, down=down)
                        arbitrage_triggered = True

                    poly_ob_up = ex.get_full_orderbook(market.get("token_up", ""))
                    poly_ob_down = ex.get_full_orderbook(market.get("token_down", ""))

                    if not arbitrage_triggered:
                        model_decision = explain_choose_side(
                            market, yes_price_window, up_price_window, down_price_window,
                            binance_1m=binance_1m, binance_5m=binance_5m,
                            ws_bba=ws_bba, ws_trades=ws_trades,
                            poly_ob_up=poly_ob_up, poly_ob_down=poly_ob_down
                        )
                        signal_side = model_decision.get("side") if model_decision.get("ok") else None
                        no_entry_reason = model_decision.get("reason")
                        if signal_side:
                            signal_origin = f"model-{model_decision.get('reason')}"

                        if signal_side is None and secs_left is not None and 90 <= secs_left <= 240:
                            dumped_side = should_trigger_dump(prev_up, prev_down, up, down, SETTINGS.dump_move_threshold)
                            if dumped_side:
                                signal_side = dumped_side
                                signal_origin = "dump-trigger"
                                no_entry_reason = ""
                                log(f"dump trigger | side={dumped_side} prev_up={prev_up} up={up} prev_down={prev_down} down={down}")

                    prev_up, prev_down = up, down

                    keep_positions: list[OpenPos] = []
                    closed_any = False
                    for p in open_positions:
                        if p.slug != market["slug"]:
                            keep_positions.append(p)
                            continue
                        mark = up if p.side == "UP" else down
                        if mark is None:
                            keep_positions.append(p)
                            continue
                        observed_value = realistic_exit_value(p, up, down, None, None)
                        mark_value = observed_mark_value(p, up, down)
                        if observed_value is None and mark_value is None:
                            keep_positions.append(p)
                            continue
                        effective_exit_value = observed_value if observed_value is not None else mark_value
                        hard_stop_value = float(min(
                            [v for v in (observed_value, mark_value) if v is not None] or [0.0]
                        ))
                        effective_exit_value = float(effective_exit_value or 0.0)
                        update_position_excursions(p, effective_exit_value)
                        pnl_pct = (effective_exit_value - p.cost_usd) / max(p.cost_usd, 1e-9)
                        hard_stop_pnl_pct = (hard_stop_value - p.cost_usd) / max(p.cost_usd, 1e-9)
                        hold_sec = time.time() - p.opened_ts
                        recovery_chance_low = False
                        if getattr(SETTINGS, "smart_stop_loss_enabled", False) and hard_stop_pnl_pct < -0.10:
                            if signal_side and signal_side != p.side:
                                recovery_chance_low = True
                            elif hold_sec >= 90.0 and (secs_left or 1000.0) <= 60.0:
                                recovery_chance_low = True

                        exit_decision = decide_exit(
                            pnl_pct=hard_stop_pnl_pct, 
                            hold_sec=hold_sec, 
                            secs_left=secs_left, 
                            has_scaled_out=getattr(p, "has_scaled_out", False), 
                            recovery_chance_low=recovery_chance_low,
                            has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
                            has_taken_partial=getattr(p, "has_taken_partial", False),
                            has_extracted_principal=getattr(p, "has_extracted_principal", False)
                        )

                        # --- Phase 2: Advanced Loophole Exploitation ---
                        try:
                            ws_vel = BINANCE_WS.get_price_velocity(3.0)
                            
                            # 1. Panic Dump Override
                            is_panic = (p.side == "UP" and ws_vel < -SETTINGS.panic_dump_velocity) or \
                                       (p.side == "DOWN" and ws_vel > SETTINGS.panic_dump_velocity)
                            if is_panic and hold_sec > 2.0:
                                log(f"🚨 PANIC DUMP OVERRIDE! {p.side} {p.token_id[-6:]} Binance vel={ws_vel:.4%}")
                                exit_decision.should_close = True
                                exit_decision.reason = "panic-dump"
                                p.has_panic_dumped = True
                            
                            # 2. Let Profits Run
                            if getattr(exit_decision, "should_close", False) and exit_decision.reason in ("take-profit-partial", "take-profit-principal"):
                                is_pump = (p.side == "UP" and ws_vel > SETTINGS.tp_hold_velocity) or \
                                          (p.side == "DOWN" and ws_vel < -SETTINGS.tp_hold_velocity)
                                if is_pump:
                                    log(f"📈 LET PROFITS RUN! Delaying TP for {p.side}. Binance vel={ws_vel:.4%}")
                                    exit_decision.should_close = False
                        except Exception:
                            pass
                        # ----------------------------------------------
                        stop_warn = hard_stop_pnl_pct <= -SETTINGS.stop_loss_warn_pct
                        urgent_exit = hard_stop_pnl_pct <= -SETTINGS.stop_loss_pct

                        if stop_warn and not urgent_exit:
                            append_event({
                                "kind": "risk_warning",
                                "slug": p.slug,
                                "side": p.side,
                                "token_id": p.token_id,
                                "position_id": p.position_id,
                                "warning": "stop-loss-warning",
                                "observed_return_pct": pnl_pct,
                                "hold_sec": hold_sec,
                            })
                            log(f"stop-loss warning | side={p.side} observed_return={pnl_pct:.2%} hold={hold_sec:.0f}s")

                        if exit_decision.should_close:
                            if exit_decision.reason == "scale-out":
                                sell_fraction = min(0.99, p.cost_usd / max(observed_value, 1e-9))
                                sell_shares = p.shares * sell_fraction
                                
                                sell_value = sell_shares * effective_exit_value
                                remain_value = (p.shares - sell_shares) * effective_exit_value
                                if sell_value < 1.0 or remain_value < 1.0:
                                    log(f"Scale-out sub-$1.00 API limit reached (sell=${sell_value:.2f}, remain=${remain_value:.2f}). Escalating to FULL exit.")
                                    exit_decision.reason = "scale-out-escalated"
                                else:
                                    try:
                                        close_resp = ex.close_position(p.token_id, sell_shares, simulated_price=float(mark) if mark is not None else None)
                                        if close_resp.get("ok"):
                                            sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), sell_shares)
                                            if sold_shares > 0:
                                                actual_fraction = sold_shares / p.shares
                                                realized_cost = p.cost_usd * actual_fraction
                                                p.shares -= sold_shares
                                                p.cost_usd *= max(0.0, 1.0 - actual_fraction)
                                                p.has_scaled_out = True
                                                
                                                _act_val = close_resp.get("actual_exit_value_usd", 0.0)
                                                _obs_val = sold_shares * effective_exit_value
                                                append_event({
                                                    "kind": "exit",
                                                    "slug": p.slug,
                                                    "side": p.side,
                                                    "token_id": p.token_id,
                                                    "position_id": p.position_id,
                                                    "closed_shares": sold_shares,
                                                    "remaining_shares": p.shares,
                                                    "realized_cost_usd": realized_cost,
                                                    "actual_exit_value_usd": _act_val,
                                                    "actual_realized_pnl_usd": _act_val - realized_cost,
                                                    "observed_exit_value_usd": _obs_val,
                                                    "observed_realized_pnl_usd": _obs_val - realized_cost,
                                                    "status": "partial",
                                                    "reason": "scale-out",
                                                    "mfe_pnl_usd": p.max_favorable_pnl_usd,
                                                    "mae_pnl_usd": p.max_adverse_pnl_usd
                                                })
                                                
                                                log(f"SCALED OUT! Sold {sold_shares:.2f} shares to lock in cost. Moonbag active.")
                                                maybe_record_cycle_label(state, "scale-out", slug=p.slug, side=p.side)
                                    except Exception as e:
                                        log(f"Scale-out error: {e}")
                                    keep_positions.append(p)
                                    continue

                            if exit_decision.reason == "stop-loss-scale-out":
                                sell_fraction = getattr(SETTINGS, "stop_loss_partial_fraction", 0.50)
                                sell_shares = p.shares * sell_fraction
                                
                                sell_value = sell_shares * effective_exit_value
                                remain_value = (p.shares - sell_shares) * effective_exit_value
                                if sell_value < 1.0 or remain_value < 1.0:
                                    log(f"Stop-loss scale-out sub-$1.00 API limit reached (sell=${sell_value:.2f}). Escalating to FULL exit.")
                                    exit_decision.reason = "stop-loss-escalated"
                                else:
                                    try:
                                        close_resp = ex.close_position(p.token_id, sell_shares, simulated_price=float(mark) if mark is not None else None)
                                        if close_resp.get("ok"):
                                            sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), sell_shares)
                                            if sold_shares > 0:
                                                actual_fraction = sold_shares / p.shares
                                                realized_cost = p.cost_usd * actual_fraction
                                                p.shares -= sold_shares
                                                p.cost_usd *= max(0.0, 1.0 - actual_fraction)
                                                p.has_scaled_out_loss = True
                                                
                                                _act_val = close_resp.get("actual_exit_value_usd", 0.0)
                                                _obs_val = sold_shares * effective_exit_value
                                                append_event({
                                                    "kind": "exit",
                                                    "slug": p.slug,
                                                    "side": p.side,
                                                    "token_id": p.token_id,
                                                    "position_id": p.position_id,
                                                    "closed_shares": sold_shares,
                                                    "remaining_shares": p.shares,
                                                    "realized_cost_usd": realized_cost,
                                                    "actual_exit_value_usd": _act_val,
                                                    "actual_realized_pnl_usd": _act_val - realized_cost,
                                                    "observed_exit_value_usd": _obs_val,
                                                    "observed_realized_pnl_usd": _obs_val - realized_cost,
                                                    "status": "partial",
                                                    "reason": "stop-loss-scale-out",
                                                    "mfe_pnl_usd": p.max_favorable_pnl_usd,
                                                    "mae_pnl_usd": p.max_adverse_pnl_usd
                                                })
                                                
                                                log(f"STOP-LOSS SCALED OUT! Sold {sold_shares:.2f} shares to mitigate risk.")
                                                maybe_record_cycle_label(state, "stop-loss-scale-out", slug=p.slug, side=p.side)
                                    except Exception as e:
                                        log(f"Stop-loss scale-out error: {e}")
                                    keep_positions.append(p)
                                    continue

                            if exit_decision.reason == "take-profit-principal":
                                current_value = max(p.shares * effective_exit_value, 1e-9)
                                sell_fraction = min(0.99, p.cost_usd / current_value)
                                sell_shares = p.shares * sell_fraction
                                
                                sell_value = sell_shares * effective_exit_value
                                remain_value = (p.shares - sell_shares) * effective_exit_value
                                if sell_value < 1.0 or remain_value < 1.0:
                                    log(f"Principal-extract sub-$1.00 API limit reached (sell=${sell_value:.2f}). Escalating to FULL exit.")
                                    exit_decision.reason = "take-profit-escalated"
                                else:
                                    try:
                                        close_resp = ex.close_position(p.token_id, sell_shares, simulated_price=float(mark) if mark is not None else None)
                                        if close_resp.get("ok"):
                                            sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), sell_shares)
                                            if sold_shares > 0:
                                                actual_fraction = sold_shares / p.shares
                                                realized_cost = p.cost_usd * actual_fraction
                                                p.shares -= sold_shares
                                                p.cost_usd *= max(0.0, 1.0 - actual_fraction)
                                                p.has_extracted_principal = True
                                                
                                                _act_val = close_resp.get("actual_exit_value_usd", 0.0)
                                                _obs_val = sold_shares * effective_exit_value
                                                append_event({
                                                    "kind": "exit",
                                                    "slug": p.slug,
                                                    "side": p.side,
                                                    "token_id": p.token_id,
                                                    "position_id": p.position_id,
                                                    "closed_shares": sold_shares,
                                                    "remaining_shares": p.shares,
                                                    "realized_cost_usd": realized_cost,
                                                    "actual_exit_value_usd": _act_val,
                                                    "actual_realized_pnl_usd": _act_val - realized_cost,
                                                    "observed_exit_value_usd": _obs_val,
                                                    "observed_realized_pnl_usd": _obs_val - realized_cost,
                                                    "status": "partial",
                                                    "reason": "take-profit-principal",
                                                    "mfe_pnl_usd": p.max_favorable_pnl_usd,
                                                    "mae_pnl_usd": p.max_adverse_pnl_usd
                                                })
                                                
                                                log(f"PRINCIPAL EXTRACTED! Sold {sold_shares:.2f} shares. Risk-Free Moonbag active.")
                                                maybe_record_cycle_label(state, "take-profit-principal", slug=p.slug, side=p.side)
                                    except Exception as e:
                                        log(f"Take-profit principal error: {e}")
                                    keep_positions.append(p)
                                    continue

                            if exit_decision.reason == "take-profit-partial":
                                sell_fraction = 0.30
                                sell_shares = p.shares * sell_fraction
                                try:
                                    close_resp = ex.close_position(p.token_id, sell_shares, simulated_price=float(mark) if mark is not None else None, force_taker=getattr(p, "has_panic_dumped", False))
                                    if close_resp.get("ok"):
                                        sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), sell_shares)
                                        if sold_shares > 0:
                                            actual_fraction = sold_shares / p.shares
                                            p.shares -= sold_shares
                                            p.cost_usd *= max(0.0, 1.0 - actual_fraction)
                                            p.has_taken_partial = True
                                            log(f"PARTIAL PROFIT TAKEN! Sold {sold_shares:.2f} shares (+30% threshold).")
                                            maybe_record_cycle_label(state, "take-profit-partial", slug=p.slug, side=p.side)
                                except Exception as e:
                                    log(f"Take-profit partial error: {e}")
                                keep_positions.append(p)
                                continue

                            try:
                                close_resp = ex.close_position(p.token_id, p.shares, simulated_price=float(mark) if mark is not None else None, force_taker=getattr(p, "has_panic_dumped", False))
                                if close_resp.get("ok"):
                                    sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), p.shares)
                                    remaining_hint = max(0.0, float(close_resp.get("remaining_shares", p.shares - sold_shares) or 0.0))
                                    if sold_shares <= 0:
                                        flags.close_fail_streak += 1
                                        if urgent_exit:
                                            flags.panic_exit_mode = True
                                            panic_market_slug = p.slug

                                        live_after_fail = None
                                        try:
                                            live_after_fail = ex.get_position(p.token_id)
                                        except Exception as reconcile_err:
                                            log(f"reconcile after close fail errored | token={p.token_id} err={reconcile_err}")

                                        err_text = str(close_resp.get("error") or "")
                                        if live_after_fail is None or float(live_after_fail.size) <= LOT_EPS_SHARES:
                                            log(
                                                f"{exit_decision.reason} close failed but live position missing -> drop local lot | "
                                                f"token={p.token_id} err={err_text or 'n/a'}"
                                            )
                                        elif "not enough balance" in err_text.lower() or "allowance" in err_text.lower() or (live_after_fail is not None and float(live_after_fail.size) < max(LOT_EPS_SHARES, p.shares - LOT_EPS_SHARES)):
                                            resized_shares = float(live_after_fail.size)
                                            resized_cost = p.avg_cost_per_share * resized_shares
                                            log(
                                                f"{exit_decision.reason} close reconciled live position | token={p.token_id} "
                                                f"local_shares={p.shares:.6f} live_shares={resized_shares:.6f} err={err_text or 'n/a'}"
                                            )
                                            keep_positions.append(OpenPos(
                                                slug=p.slug,
                                                side=p.side,
                                                token_id=p.token_id,
                                                shares=resized_shares,
                                                cost_usd=resized_cost,
                                                opened_ts=p.opened_ts,
                                                position_id=p.position_id,
                                                entry_reason=p.entry_reason,
                                                source=p.source,
                                                last_synced_size=float(live_after_fail.size),
                                                last_synced_initial_value=float(live_after_fail.initial_value),
                                                last_synced_current_value=float(live_after_fail.current_value),
                                                last_synced_cash_pnl=float(live_after_fail.cash_pnl),
                                                last_synced_at=time.time(),
                                                max_favorable_value_usd=p.max_favorable_value_usd,
                                                max_adverse_value_usd=p.max_adverse_value_usd,
                                                max_favorable_pnl_usd=p.max_favorable_pnl_usd,
                                                max_adverse_pnl_usd=p.max_adverse_pnl_usd,
                                                has_scaled_out=getattr(p, "has_scaled_out", False),
                                                has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
                                                has_taken_partial=getattr(p, "has_taken_partial", False),
                                                has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                            ))
                                        else:
                                            log(f"{exit_decision.reason} close failed: zero shares closed | resp={close_resp}")
                                            keep_positions.append(p)
                                    else:
                                        flags.close_fail_streak = 0
                                        closed_any = True
                                        close_fraction = sold_shares / max(p.shares, 1e-9)
                                        realized_cost = p.cost_usd * close_fraction
                                        observed_exit_value_usd = sold_shares * float(mark)
                                        observed_realized_pnl_usd = observed_exit_value_usd - realized_cost
                                        observed_realized_return_pct = observed_realized_pnl_usd / max(realized_cost, 1e-9)

                                        actual_exit_value_usd = float(close_resp.get("actual_exit_value_usd", 0.0) or 0.0)
                                        actual_exit_value_source = str(close_resp.get("actual_exit_value_source") or "")
                                        close_response_value = close_resp.get("close_response_value")
                                        close_response_value_source = str(close_resp.get("close_response_value_source") or "")
                                        close_response_amount_fields = close_resp.get("close_response_amount_fields") or {}

                                        if close_response_value is not None and float(close_response_value) > 0:
                                            actual_exit_value_usd = float(close_response_value)
                                            actual_exit_value_source = close_response_value_source or "close_response_value"
                                            actual_realized_pnl_usd = actual_exit_value_usd - realized_cost
                                            actual_realized_return_pct = actual_realized_pnl_usd / max(realized_cost, 1e-9)
                                            pnl_source = "actual_close_response_value"
                                            risk.daily_pnl += actual_realized_pnl_usd
                                        elif actual_exit_value_usd > 0 and actual_exit_value_source == "cash_balance_delta":
                                            actual_realized_pnl_usd = actual_exit_value_usd - realized_cost
                                            actual_realized_return_pct = actual_realized_pnl_usd / max(realized_cost, 1e-9)
                                            pnl_source = "actual_cash_recovered"
                                            risk.daily_pnl += actual_realized_pnl_usd
                                        else:
                                            actual_exit_value_usd = actual_exit_value_usd if actual_exit_value_usd > 0 else None
                                            actual_realized_pnl_usd = None
                                            actual_realized_return_pct = None
                                            pnl_source = "observed_mark_estimate"
                                            risk.daily_pnl += observed_realized_pnl_usd

                                        if (actual_realized_pnl_usd if actual_realized_pnl_usd is not None else observed_realized_pnl_usd) < 0:
                                            flags.live_consec_losses += 1
                                            flags.last_loss_side = p.side
                                        else:
                                            flags.live_consec_losses = 0
                                            flags.last_loss_side = ""
                                        risk.consec_losses = flags.live_consec_losses

                                        remaining_shares = max(max(0.0, p.shares - sold_shares), remaining_hint)
                                        remaining_cost = max(0.0, p.cost_usd - realized_cost)
                                        quality_pnl = actual_realized_pnl_usd if actual_realized_pnl_usd is not None else observed_realized_pnl_usd
                                        entry_quality = "good-entry" if quality_pnl > 0 else "bad-entry" if quality_pnl < 0 else "flat-entry"
                                        exit_event = append_event({
                                            "kind": "exit",
                                            "slug": p.slug,
                                            "side": p.side,
                                            "token_id": p.token_id,
                                            "position_id": p.position_id,
                                            "closed_shares": sold_shares,
                                            "remaining_shares": remaining_shares,
                                            "realized_cost_usd": realized_cost,
                                            "actual_exit_value_usd": actual_exit_value_usd,
                                            "actual_exit_value_source": actual_exit_value_source or "unavailable",
                                            "actual_realized_pnl_usd": actual_realized_pnl_usd,
                                            "actual_realized_return_pct": actual_realized_return_pct,
                                            "actual_close_response_value": close_response_value,
                                            "actual_close_response_value_source": close_response_value_source or "close_response_unavailable",
                                            "actual_close_response_amount_fields": close_response_amount_fields,
                                            "observed_mark_price": float(mark),
                                            "observed_exit_value_usd": observed_exit_value_usd,
                                            "observed_exit_value_source": "observed_mark_price",
                                            "observed_realized_pnl_usd": observed_realized_pnl_usd,
                                            "observed_realized_return_pct": observed_realized_return_pct,
                                            "pnl_source": pnl_source,
                                            "reason": exit_decision.reason,
                                            "entry_quality": entry_quality,
                                            "mae_pnl_usd": p.max_adverse_pnl_usd,
                                            "mfe_pnl_usd": p.max_favorable_pnl_usd,
                                            "mae_value_usd": p.max_adverse_value_usd,
                                            "mfe_value_usd": p.max_favorable_value_usd,
                                        })
                                        log(format_exit_summary(exit_event))
                                        actual_bits = ""
                                        if actual_realized_pnl_usd is not None:
                                            actual_bits = (
                                                f" actual_realized_pnl_usd={actual_realized_pnl_usd:+.4f}"
                                                f" actual_return={actual_realized_return_pct:.2%}"
                                            )
                                        log(
                                            f"{exit_decision.reason} close | side={p.side} pnl_pct={pnl_pct:.2%} hard_stop_pnl_pct={hard_stop_pnl_pct:.2%}"
                                            f"{actual_bits} observed_pnl_usd={observed_realized_pnl_usd:+.4f} hold={hold_sec:.0f}s "
                                            f"consec_losses={flags.live_consec_losses} resp={close_resp}"
                                        )

                                        if p.entry_reason:
                                            from core.learning import SCOREBOARD
                                            SCOREBOARD.record_outcome(
                                                strategy_name=p.entry_reason,
                                                pnl_pct=actual_realized_return_pct if actual_realized_return_pct is not None else observed_realized_return_pct,
                                                timestamp=time.time()
                                            )
                                        if remaining_shares > LOT_EPS_SHARES and remaining_cost > LOT_EPS_COST_USD:
                                            dust_retry = int(getattr(p, "dust_retry_count", 0)) + 1
                                            if dust_retry > 3:  # DUST_MAX_RETRIES = 3
                                                log(
                                                    f"dust_abandoned | token={p.token_id} remaining_shares={remaining_shares:.6f} "
                                                    f"after {dust_retry} retries — forcing drop to avoid zombie position"
                                                )
                                            else:
                                                keep_positions.append(OpenPos(
                                                    slug=p.slug,
                                                    side=p.side,
                                                    token_id=p.token_id,
                                                    shares=remaining_shares,
                                                    cost_usd=remaining_cost,
                                                    opened_ts=p.opened_ts,
                                                    position_id=p.position_id,
                                                    entry_reason=p.entry_reason,
                                                    source=p.source,
                                                    max_favorable_value_usd=p.max_favorable_value_usd,
                                                    max_adverse_value_usd=p.max_adverse_value_usd,
                                                    max_favorable_pnl_usd=p.max_favorable_pnl_usd,
                                                    max_adverse_pnl_usd=p.max_adverse_pnl_usd,
                                                    has_scaled_out=getattr(p, "has_scaled_out", False),
                                                    has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
                                                    has_taken_partial=getattr(p, "has_taken_partial", False),
                                                    has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                                    dust_retry_count=dust_retry,
                                                ))
                                        else:
                                            log(
                                                f"drop residual after close | token={p.token_id} remaining_shares={remaining_shares:.6f} "
                                                f"remaining_cost={remaining_cost:.6f}"
                                            )
                                else:
                                    flags.close_fail_streak += 1
                                    if urgent_exit:
                                        flags.panic_exit_mode = True
                                        panic_market_slug = p.slug
                                    log(f"{exit_decision.reason} close failed: {close_resp}")
                                    keep_positions.append(p)
                            except Exception as e:
                                flags.close_fail_streak += 1
                                if urgent_exit:
                                    flags.panic_exit_mode = True
                                    panic_market_slug = p.slug
                                log(f"{exit_decision.reason} close failed: {e}")
                                keep_positions.append(p)
                        else:
                            keep_positions.append(p)
                    open_positions, residual_notes = sanitize_open_positions(keep_positions, source="post-close")
                    for note in residual_notes:
                        log(note)
                    if (not open_positions) and flags.close_fail_streak == 0:
                        flags.panic_exit_mode = False
                        panic_market_slug = ""

                    has_current_market_pos = any(p.slug == market["slug"] for p in open_positions)
                    if can_reenter_same_market(has_current_market_pos=has_current_market_pos, closed_any=closed_any, secs_left=secs_left):
                        risk.orders_this_window = 0
                        log(f"re-entry unlocked in same market | secs_left={secs_left:.0f}")

                    idle_min = (time.time() - last_trade_ts) / 60.0
                    # Cadence fallback disabled per Openclaw report (prevents forced trades without edge)

                    if signal_side:
                        entry_decision = maybe_reverse_entry(
                            signal_side=signal_side,
                            live_consec_losses=flags.live_consec_losses,
                            last_loss_side=flags.last_loss_side,
                        )
                        if entry_decision.reason:
                            signal_side = entry_decision.side
                            signal_origin = f"{signal_origin}+{entry_decision.reason}" if signal_origin else entry_decision.reason
                            log(f"{entry_decision.reason} applied (DOWN losing streak) | consec_losses={flags.live_consec_losses} -> side={signal_side}")

                        token_override = market["token_up"] if signal_side == "UP" else market["token_down"]
                        entry_price = up if signal_side == "UP" else down
                        if entry_price and entry_price > 0:
                            try:
                                book = ex.get_full_orderbook(token_override)
                                clob_best_ask = book.get("best_ask", 0.0)
                                if clob_best_ask > 0:
                                    if clob_best_ask < SETTINGS.min_entry_price:
                                        maybe_record_cycle_label(state, "signal-blocked", slug=market["slug"], side=signal_side, reason="clob-ask-too-low")
                                        log(f"skip entry: CLOB best_ask ({clob_best_ask}) < min_entry ({SETTINGS.min_entry_price}), avoiding deep downward slippage!")
                                        signal_side = None
                                        continue
                                    elif clob_best_ask > getattr(SETTINGS, "max_entry_price", 0.8):
                                        maybe_record_cycle_label(state, "signal-blocked", slug=market["slug"], side=signal_side, reason="clob-ask-too-high")
                                        log(f"skip entry: CLOB best_ask ({clob_best_ask}) > max_entry ({getattr(SETTINGS, 'max_entry_price', 0.8)}), avoiding terrible risk/reward!")
                                        signal_side = None
                                        continue
                            except Exception as e:
                                log(f"clob slippage check failed: {e}")

                            if signal_side and float(entry_price) < SETTINGS.min_entry_price:
                                maybe_record_cycle_label(state, "signal-blocked", slug=market["slug"], side=signal_side, reason="price-too-low")
                                log(f"skip entry: {signal_side} price {entry_price} < {SETTINGS.min_entry_price}")
                                signal_side = None
                            else:
                                est_shares = 1.0 / float(entry_price)
                                if not ex.has_exit_liquidity(token_override, est_shares):
                                    maybe_record_cycle_label(state, "signal-but-no-fill", slug=market["slug"], side=signal_side, reason="weak-exit-liquidity")
                                    log("skip entry: weak exit liquidity")
                                    signal_side = None
                    else:
                        maybe_record_cycle_label(state, "no-entry", slug=market["slug"], secs_left=secs_left, up=up, down=down, reason=no_entry_reason or "no_signal")
                        log(f"no entry | slug={market['slug']} reason={no_entry_reason or 'no_signal'} secs_left={secs_left} up={up} down={down}")
                except MarketResolutionError as e:
                    if SETTINGS.token_id_up and SETTINGS.token_id_down:
                        log(f"market resolve failed: {e} | fallback to static token ids")
                    else:
                        log(f"market resolve failed: {e}")
                        smart_sleep(SETTINGS.poll_seconds)
                        continue
                except Exception as e:
                    log(f"unexpected network or API error in main loop: {e}. Retrying in 5s...")
                    smart_sleep(5.0)
                    continue
            else:
                price_now = ex.get_btc_price()
                signal_side = "UP" if int(price_now) % 2 == 0 else "DOWN"
                signal_origin = "dry-run-fallback"

            save_runtime_state(
                risk,
                last_market_slug=last_market_slug,
                yes_price_window=yes_price_window,
                up_price_window=up_price_window,
                down_price_window=down_price_window,
                last_trade_ts=last_trade_ts,
                prev_up=prev_up,
                prev_down=prev_down,
                error_cooldown_until=error_cooldown_until,
                open_positions=open_positions,
                flags=flags,
                last_cycle_label=state.get("last_cycle_label", ""),
                panic_market_slug=panic_market_slug,
            )

            if signal_side is None:
                if SETTINGS.dry_run and open_positions:
                    mock_value = 0.0
                    for p in open_positions:
                        if p.shares <= 0: continue
                        if market.get("slug") == p.slug:
                            mark = (up if p.side == "UP" else down)
                            mock_value += p.shares * float(mark if mark is not None else 0.5)
                        else:
                            mock_value += p.shares * 0.5
                    acct.equity = acct.cash + mock_value
                log(f"no signal | equity={acct.equity:.2f} cash={acct.cash:.2f}")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            order_usd = SETTINGS.max_order_usd
            if getattr(SETTINGS, "use_kelly_sizing", False) and signal_origin:
                try:
                    from core.learning import SCOREBOARD
                    win_rate = SCOREBOARD.get_strategy_score(signal_origin)
                    f_star = max(0.0, 2.0 * win_rate - 1.0)
                    q_kelly = f_star / 4.0
                    if q_kelly > 0:
                        bankroll = acct.equity
                        kelly_bet = bankroll * q_kelly
                        order_usd = max(SETTINGS.max_order_usd, min(kelly_bet, getattr(SETTINGS, "max_bet_cap_usd", 50.0)))
                        log(f"Kelly Sizing | Strategy={signal_origin} WR={win_rate:.1%} qK={q_kelly:.2%} Bankroll=${bankroll:.2f} -> Bet=${order_usd:.2f}")
                except Exception as e:
                    log(f"Kelly calc error: {e}")

            if flags.panic_exit_mode:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="panic-exit-mode")
                log("panic_exit_mode active: block new entries")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if open_positions:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="existing-position-still-open")
                log("skip entry: existing position still open")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if flags.close_fail_streak >= 2:
                flags.panic_exit_mode = True
                panic_market_slug = last_market_slug
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="close-fail-streak")
                log(f"protection mode: close_fail_streak={flags.close_fail_streak}, block new entries")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if time.time() < error_cooldown_until:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="error-cooldown")
                log("in error cooldown, skip this cycle")
                smart_sleep(SETTINGS.poll_seconds)
                continue
            if acct.cash < 1.0:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="cash-below-1")
                log(f"blocked by cash: cash={acct.cash:.2f} < 1.00")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            # 計算當前 OFI 供風控判斷（與 decision_engine 共用 ws_trades 資料）
            current_ofi = 0.0
            if ws_trades:
                from core.indicators import compute_buy_sell_pressure
                _bv, _sv = compute_buy_sell_pressure(ws_trades)
                _total = _bv + _sv
                if _total > 0:
                    current_ofi = _bv / _total

            ok, reason = can_place_order(
                equity=acct.equity,
                open_exposure=acct.open_exposure,
                order_usd=order_usd,
                min_equity=SETTINGS.min_equity,
                max_exposure_usd=SETTINGS.max_exposure_usd,
                max_orders_per_5min=SETTINGS.max_orders_per_5min,
                consec_losses=risk.consec_losses,
                max_consec_loss=SETTINGS.max_consec_loss,
                daily_pnl=risk.daily_pnl,
                daily_max_loss=SETTINGS.daily_max_loss,
                orders_this_window=risk.orders_this_window,
                current_ofi=current_ofi,
                ofi_bypass_threshold=SETTINGS.ofi_bypass_threshold,
            )

            if not ok:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason=reason)
                log(f"blocked by risk: {reason}")
                notify_discord(SETTINGS.discord_webhook_url, f"🚫 Bot blocked: {reason}")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            try:
                sim_price = (float(up) if up is not None else None) if signal_side == "UP" else (float(down) if down is not None else None)
                
                force_taker_snipe = False
                try:
                    ws_vel = BINANCE_WS.get_price_velocity(3.0)
                    if (signal_side == "UP" and ws_vel > SETTINGS.taker_snipe_velocity) or \
                       (signal_side == "DOWN" and ws_vel < -SETTINGS.taker_snipe_velocity):
                        log(f"⚡ TAKER SNIPE TRIGGERED! {signal_side} Binance vel={ws_vel:.4%}")
                        force_taker_snipe = True
                except Exception:
                    pass

                resp = ex.place_order(signal_side, order_usd, token_override, simulated_price=sim_price, force_taker=force_taker_snipe)
                risk.orders_this_window += 1
                last_trade_ts = time.time()
                risk.consec_losses = flags.live_consec_losses
                
                # Hedge Logic
                hedge_ratio = getattr(SETTINGS, "hedge_ratio", 0.0)
                if hedge_ratio > 0.0 and market:
                    hedge_side = "DOWN" if signal_side == "UP" else "UP"
                    hedge_usd = order_usd * hedge_ratio
                    hedge_token_id = market.get("token_down") if signal_side == "UP" else market.get("token_up")
                    if hedge_token_id and hedge_usd >= 0.5:
                        log(f"executing structured hedge | side={hedge_side} cost=${hedge_usd:.4f}")
                        h_sim_price = (float(down) if down is not None else None) if signal_side == "UP" else (float(up) if up is not None else None)
                        h_res = ex.place_order(hedge_side, hedge_usd, token_id_override=hedge_token_id, simulated_price=h_sim_price)
                        try:
                            hr = h_res.get("response", {}) if isinstance(h_res, dict) else {}
                            h_shares = float(hr.get("takingAmount", 0) or 0)
                            if h_shares > 0:
                                h_ts = time.time()
                                open_positions.append(OpenPos(
                                    slug=market["slug"],
                                    side=hedge_side,
                                    token_id=hedge_token_id,
                                    shares=h_shares,
                                    cost_usd=hedge_usd,
                                    opened_ts=h_ts,
                                    position_id=f"pos_{int(h_ts)}_{hedge_token_id[-6:]}",
                                    entry_reason="structured-hedge",
                                    source="runtime",
                                    max_favorable_value_usd=hedge_usd,
                                ))
                        except Exception as e:
                            log(f"hedge parsing error: {e}")
                try:
                    r = resp.get("response", {}) if isinstance(resp, dict) else {}
                    shares = float(r.get("takingAmount", 0) or 0)
                    token_id = token_override or (market["token_up"] if signal_side == "UP" else market["token_down"])
                    if shares > 0 and token_id:
                        opened_ts = time.time()
                        position_id = f"pos_{int(opened_ts)}_{token_id[-6:]}"
                        open_positions.append(OpenPos(
                            slug=market["slug"],
                            side=signal_side,
                            token_id=token_id,
                            shares=shares,
                            cost_usd=order_usd,
                            opened_ts=opened_ts,
                            position_id=position_id,
                            entry_reason=signal_origin or "signal",
                            source="live-order",
                            pending_confirmation=True,
                            max_favorable_value_usd=order_usd,
                            max_adverse_value_usd=order_usd,
                            max_favorable_pnl_usd=0.0,
                            max_adverse_pnl_usd=0.0,
                        ))
                        append_event({
                            "kind": "entry",
                            "slug": market["slug"],
                            "side": signal_side,
                            "token_id": token_id,
                            "position_id": position_id,
                            "shares": shares,
                            "cost_usd": order_usd,
                            "opened_ts": opened_ts,
                            "entry_reason": signal_origin or "signal",
                            "classification": "good-entry-candidate",
                            "mae_pnl_usd": 0.0,
                            "mfe_pnl_usd": 0.0,
                        })
                        maybe_record_cycle_label(state, "good-entry", slug=market["slug"], side=signal_side, reason=signal_origin or "signal")
                    else:
                        order_id = r.get("orderID")
                        if order_id:
                            pending_orders.append(PendingOrder(
                                order_id=order_id,
                                slug=market["slug"],
                                side=signal_side,
                                token_id=token_id,
                                placed_ts=time.time(),
                                order_usd=order_usd
                            ))
                            maybe_record_cycle_label(state, "maker-order-placed", slug=market["slug"], side=signal_side, reason="waiting-for-fill")
                            log(f"Maker order placed on {signal_side}, awaiting fill: {order_id}")
                        else:
                            maybe_record_cycle_label(state, "signal-but-no-fill", slug=market["slug"], side=signal_side, reason="no-takingAmount-no-orderID")
                            append_event({
                                "kind": "entry_attempt",
                                "slug": market["slug"],
                                "side": signal_side,
                                "token_id": token_id,
                                "status": "signal-but-no-fill",
                                "reason": "no-takingAmount-no-orderID",
                                "response_mode": resp.get("mode") if isinstance(resp, dict) else "",
                            })
                except Exception:
                    pass
                save_runtime_state(
                    risk,
                    last_market_slug=last_market_slug,
                    yes_price_window=yes_price_window,
                    up_price_window=up_price_window,
                    down_price_window=down_price_window,
                    last_trade_ts=last_trade_ts,
                    prev_up=prev_up,
                    prev_down=prev_down,
                    error_cooldown_until=error_cooldown_until,
                    open_positions=open_positions,
                    flags=flags,
                    last_cycle_label=state.get("last_cycle_label", ""),
                    panic_market_slug=panic_market_slug,
                )
                log(f"order placed: {resp}")
                notify_discord(SETTINGS.discord_webhook_url, f"✅ Order {signal_side} ${order_usd:.2f} ({resp.get('mode')})")
            except Exception as e:
                log(f"order skipped: {e}")
                maybe_record_cycle_label(state, "signal-but-no-fill", slug=last_market_slug, side=signal_side, reason=str(e))
                append_event({
                    "kind": "entry_attempt",
                    "slug": market["slug"] if market else last_market_slug,
                    "side": signal_side,
                    "token_id": token_override,
                    "status": "signal-but-no-fill",
                    "reason": str(e),
                })
                error_cooldown_until = time.time() + 20
                save_runtime_state(
                    risk,
                    last_market_slug=last_market_slug,
                    yes_price_window=yes_price_window,
                    up_price_window=up_price_window,
                    down_price_window=down_price_window,
                    last_trade_ts=last_trade_ts,
                    prev_up=prev_up,
                    prev_down=prev_down,
                    error_cooldown_until=error_cooldown_until,
                    open_positions=open_positions,
                    flags=flags,
                    last_cycle_label=state.get("last_cycle_label", ""),
                    panic_market_slug=panic_market_slug,
                )
                has_active = bool(open_positions) or (len(pending_orders) > 0 if 'pending_orders' in locals() else False)
                if has_active:
                    smart_sleep(1.5)
                else:
                    smart_sleep(SETTINGS.poll_seconds)
                continue

            if SETTINGS.dry_run:
                pnl = round(uniform(-0.3, 0.3), 2)
                ex.settle_mock(pnl)
                risk.daily_pnl += pnl
                risk.consec_losses = risk.consec_losses + 1 if pnl < 0 else 0
                log(f"mock settle pnl={pnl:+.2f} daily_pnl={risk.daily_pnl:+.2f} consec_losses={risk.consec_losses}")

            has_active = bool(open_positions) or (len(pending_orders) > 0 if 'pending_orders' in locals() else False)
            if has_active:
                smart_sleep(1.5)
            elif "secs_left" in locals() and secs_left is not None and 200 <= secs_left <= 260:
                smart_sleep(1.0)
            else:
                smart_sleep(SETTINGS.poll_seconds)
    except GracefulStop:
        reason = "manual-stop" if STOP_REQUEST["signal"] == signal.SIGINT else "timeout-or-sigterm"
        run_journal.finalize(status="terminated", reason=reason, notes=["graceful signal stop"])
        raise
    except KeyboardInterrupt:
        run_journal.mark_signal(signal.SIGINT)
        run_journal.finalize(status="terminated", reason="manual-stop", notes=["keyboard interrupt"])
        raise
    except Exception as e:
        run_journal.finalize(status="crashed", reason="exception", notes=[repr(e)])
        raise
    else:
        run_journal.finalize(status="stopped", reason="clean-exit")
    finally:
        try:
            import subprocess
            import sys
            from pathlib import Path
            script_path = Path(__file__).parent.parent / "scripts" / "trade_pair_ledger.py"
            data_dir = Path(__file__).parent.parent / "data"
            # 以啟動時間作為報告檔名
            _ts = run_journal.started_at.replace(":", "-")
            mode_tag = "dryrun" if SETTINGS.dry_run else "live"
            timestamped_path = data_dir / f"report-{mode_tag}-{_ts}.txt"
            latest_path = data_dir / "latest_run_report.txt"
            if script_path.exists():
                print("\n================= RUN REPORT =================")
                print("Generating post-run summary report...")
                report_args = [sys.executable, str(script_path), "--limit", "30", "--summary"]
                # 儲存帶時間戳的報告（實戰和模擬都執行）
                with open(timestamped_path, "w", encoding="utf-8") as f:
                    subprocess.run(report_args, stdout=f, check=False)
                # 同時更新 latest_run_report.txt 方便快速查看
                import shutil
                shutil.copy2(timestamped_path, latest_path)
                # 印到 console
                subprocess.run(report_args, check=False)
                print(f"Report saved to: {timestamped_path}")
                print(f"Also copied to:  {latest_path}")
                print("==============================================\n")
        except Exception as report_err:
            log(f"Failed to generate run report: {report_err}")


if __name__ == "__main__":
    main()
