import signal
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from random import uniform

from config import SETTINGS
from decision_engine import choose_side, explain_choose_side, get_outcome_prices, seconds_to_market_end
from exchange import PolymarketExchange, Position
from hedge_logic import should_trigger_dump
from notifier import notify_discord
from risk import RiskState, can_place_order, current_5min_key, update_window
from market_resolver import resolve_latest_btc_5m_token_ids, MarketResolutionError
from run_journal import RunJournal
from state_store import load_state, save_state
from trade_manager import decide_exit, maybe_reverse_entry, can_reenter_same_market
from journal import (
    LOT_EPS_COST_USD,
    LOT_EPS_SHARES,
    STALE_HOURS,
    append_event,
    replay_open_positions,
    read_events,
    format_exit_summary,
)


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
    max_favorable_value_usd: float = 0.0
    max_adverse_value_usd: float = 0.0
    max_favorable_pnl_usd: float = 0.0
    max_adverse_pnl_usd: float = 0.0

    @property
    def avg_cost_per_share(self) -> float:
        return self.cost_usd / max(self.shares, 1e-9)


@dataclass
class RuntimeFlags:
    live_consec_losses: int
    last_loss_side: str
    close_fail_streak: int
    panic_exit_mode: bool


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
    actual = {p.token_id: p for p in ex.get_positions()}
    if not actual:
        sanitized, notes = sanitize_open_positions(open_positions, source="runtime-no-live")
        return sanitized, notes

    synced: list[OpenPos] = []
    notes: list[str] = []
    for p in open_positions:
        ap = actual.get(p.token_id)
        if ap is None or ap.size <= 0:
            notes.append(f"sync_drop token={p.token_id} slug={p.slug} reason=missing-live-position")
            continue
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
            max_favorable_value_usd=p.max_favorable_value_usd,
            max_adverse_value_usd=p.max_adverse_value_usd,
            max_favorable_pnl_usd=p.max_favorable_pnl_usd,
            max_adverse_pnl_usd=p.max_adverse_pnl_usd,
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

    runtime_positions = [OpenPos(**p) for p in state.get("open_positions", [])]
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
        while True:
            now = datetime.now()
            key = current_5min_key(now)
            update_window(risk, key)

            acct = ex.get_account()
            open_positions, sync_notes = sync_open_positions(ex, open_positions)
            for note in sync_notes:
                log(note)
            flags = load_runtime_flags({
                "live_consec_losses": flags.live_consec_losses,
                "last_loss_side": flags.last_loss_side,
                "close_fail_streak": flags.close_fail_streak,
                "panic_exit_mode": flags.panic_exit_mode,
                "panic_market_slug": panic_market_slug,
            }, open_positions)

            market = None
            token_override = None
            signal_side = None
            signal_origin = ""
            no_entry_reason = ""

            if SETTINGS.auto_market_selection and not SETTINGS.dry_run:
                try:
                    market = resolve_latest_btc_5m_token_ids()
                    if market["slug"] != last_market_slug:
                        last_market_slug = market["slug"]
                        log(f"market switched => {market['slug']}")
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
                    ob_up = ex.get_full_orderbook(market.get("token_up", "")) if SETTINGS.use_ob_imbalance else None
                    ob_down = ex.get_full_orderbook(market.get("token_down", "")) if SETTINGS.use_ob_imbalance else None

                    if SETTINGS.use_dynamic_thresholds and binance_1m:
                        change_abs = abs(binance_1m.get("change", 0.0))
                        if change_abs > 30.0:
                            SETTINGS.stop_loss_pct = max(SETTINGS.stop_loss_pct, 0.40)
                            SETTINGS.zscore_threshold = max(SETTINGS.zscore_threshold, 2.5)
                        else:
                            from config import _f
                            SETTINGS.stop_loss_pct = _f("STOP_LOSS_PCT", 0.30)
                            SETTINGS.zscore_threshold = _f("ZSCORE_THRESHOLD", 2.0)

                    arbitrage_triggered = False
                    from decision_engine import check_arbitrage
                    if check_arbitrage(up, down):
                        log(f"ARBITRAGE DETECTED! up={up} down={down} sum={up+down}")
                        res_up = ex.place_order("UP", 1.0, market.get("token_up"))
                        res_down = ex.place_order("DOWN", 1.0, market.get("token_down"))
                        log(f"Arbitrage execution: UP={res_up} DOWN={res_down}")
                        maybe_record_cycle_label(state, "arbitrage-execution", slug=market["slug"], up=up, down=down)
                        arbitrage_triggered = True

                    if not arbitrage_triggered:
                        model_decision = explain_choose_side(
                            market, yes_price_window, up_price_window, down_price_window,
                            binance_1m=binance_1m, ob_up=ob_up, ob_down=ob_down
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
                        observed_value = observed_mark_value(p, up, down)
                        if observed_value is None:
                            keep_positions.append(p)
                            continue
                        update_position_excursions(p, observed_value)
                        pnl_pct = (observed_value - p.cost_usd) / max(p.cost_usd, 1e-9)
                        hold_sec = time.time() - p.opened_ts
                        exit_decision = decide_exit(pnl_pct=pnl_pct, hold_sec=hold_sec)
                        stop_warn = pnl_pct <= -SETTINGS.stop_loss_warn_pct
                        urgent_exit = pnl_pct <= -SETTINGS.stop_loss_pct

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
                            try:
                                close_resp = ex.close_position(p.token_id, p.shares)
                                if close_resp.get("ok"):
                                    sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), p.shares)
                                    remaining_hint = max(0.0, float(close_resp.get("remaining_shares", p.shares - sold_shares) or 0.0))
                                    if sold_shares <= 0:
                                        flags.close_fail_streak += 1
                                        if urgent_exit:
                                            flags.panic_exit_mode = True
                                            panic_market_slug = p.slug
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

                                        if actual_exit_value_usd > 0 and actual_exit_value_source == "cash_balance_delta":
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

                                        remaining_shares = min(max(0.0, p.shares - sold_shares), remaining_hint)
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
                                            f"{exit_decision.reason} close | side={p.side} pnl_pct={pnl_pct:.2%}"
                                            f"{actual_bits} observed_pnl_usd={observed_realized_pnl_usd:+.4f} hold={hold_sec:.0f}s "
                                            f"consec_losses={flags.live_consec_losses} resp={close_resp}"
                                        )
                                        if remaining_shares > LOT_EPS_SHARES and remaining_cost > LOT_EPS_COST_USD:
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
                    if signal_side is None and idle_min >= SETTINGS.max_idle_minutes:
                        if up is not None and down is not None and secs_left is not None and 60 <= secs_left <= 220:
                            signal_side = "DOWN" if up > down else "UP"
                            signal_origin = "cadence-fallback"
                            no_entry_reason = ""
                            log(f"cadence fallback triggered | idle={idle_min:.1f}m | side={signal_side}")

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
                            est_shares = 1.0 / float(entry_price)
                            if not ex.has_exit_liquidity(token_override, est_shares):
                                maybe_record_cycle_label(state, "signal-but-no-fill", slug=market["slug"], side=signal_side, reason="weak-exit-liquidity")
                                log("skip entry: weak exit liquidity")
                                signal_side = None
                    else:
                        maybe_record_cycle_label(state, "no-entry", slug=market["slug"], secs_left=secs_left, up=up, down=down, reason=no_entry_reason or "no_signal")
                        log(f"no entry | slug={market["slug"]} reason={no_entry_reason or "no_signal"} secs_left={secs_left} up={up} down={down}")
                except MarketResolutionError as e:
                    if SETTINGS.token_id_up and SETTINGS.token_id_down:
                        log(f"market resolve failed: {e} | fallback to static token ids")
                    else:
                        log(f"market resolve failed: {e}")
                        time.sleep(SETTINGS.poll_seconds)
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
                log(f"no signal | equity={acct.equity:.2f} cash={acct.cash:.2f}")
                time.sleep(SETTINGS.poll_seconds)
                continue

            order_usd = 1.0

            if flags.panic_exit_mode:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="panic-exit-mode")
                log("panic_exit_mode active: block new entries")
                time.sleep(SETTINGS.poll_seconds)
                continue

            if open_positions:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="existing-position-still-open")
                log("skip entry: existing position still open")
                time.sleep(SETTINGS.poll_seconds)
                continue

            if flags.close_fail_streak >= 2:
                flags.panic_exit_mode = True
                panic_market_slug = last_market_slug
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="close-fail-streak")
                log(f"protection mode: close_fail_streak={flags.close_fail_streak}, block new entries")
                time.sleep(SETTINGS.poll_seconds)
                continue

            if time.time() < error_cooldown_until:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="error-cooldown")
                log("in error cooldown, skip this cycle")
                time.sleep(SETTINGS.poll_seconds)
                continue
            if acct.cash < 1.0:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="cash-below-1")
                log(f"blocked by cash: cash={acct.cash:.2f} < 1.00")
                time.sleep(SETTINGS.poll_seconds)
                continue

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
            )

            if not ok:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason=reason)
                log(f"blocked by risk: {reason}")
                notify_discord(SETTINGS.discord_webhook_url, f"🚫 Bot blocked: {reason}")
                time.sleep(SETTINGS.poll_seconds)
                continue

            try:
                resp = ex.place_order(signal_side, order_usd, token_id_override=token_override)
                risk.orders_this_window += 1
                last_trade_ts = time.time()
                risk.consec_losses = flags.live_consec_losses
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
                        maybe_record_cycle_label(state, "signal-but-no-fill", slug=market["slug"], side=signal_side, reason="no-takingAmount")
                        append_event({
                            "kind": "entry_attempt",
                            "slug": market["slug"],
                            "side": signal_side,
                            "token_id": token_id,
                            "status": "signal-but-no-fill",
                            "reason": "no-takingAmount",
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
                time.sleep(SETTINGS.poll_seconds)
                continue

            if SETTINGS.dry_run:
                pnl = round(uniform(-0.3, 0.3), 2)
                ex.settle_mock(pnl)
                risk.daily_pnl += pnl
                risk.consec_losses = risk.consec_losses + 1 if pnl < 0 else 0
                log(f"mock settle pnl={pnl:+.2f} daily_pnl={risk.daily_pnl:+.2f} consec_losses={risk.consec_losses}")

            time.sleep(SETTINGS.poll_seconds)
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


if __name__ == "__main__":
    main()
