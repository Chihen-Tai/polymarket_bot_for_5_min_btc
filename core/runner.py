from __future__ import annotations

import signal
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from random import uniform

from core.config import SETTINGS
from core.decision_engine import choose_side, explain_choose_side, get_outcome_prices, seconds_to_market_end
from core.exchange import (
    PolymarketExchange,
    Position,
    estimate_book_exit_value,
    order_below_minimum_shares,
    plan_live_order,
)
from core.hedge_logic import should_trigger_dump
from core.notifier import notify_discord
from core.risk import RiskState, can_place_order, current_5min_key, update_window
from core.market_resolver import resolve_latest_btc_5m_token_ids, MarketResolutionError
from core.run_journal import RunJournal
from core.state_store import load_state, save_state
from core.trade_manager import ExitDecision, decide_exit, maybe_reverse_entry, should_block_same_market_reentry
from core.ws_binance import BINANCE_WS
from core.indicators import compute_buy_sell_pressure
from core.journal import (
    LOT_EPS_COST_USD,
    LOT_EPS_SHARES,
    STALE_HOURS,
    append_event,
    set_journal_context,
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


def normal_poll_interval_seconds() -> float:
    return max(2.0, float(getattr(SETTINGS, "poll_seconds", 3) or 3.0))


def pending_order_poll_interval_seconds() -> float:
    requested = max(0.5, float(getattr(SETTINGS, "pending_order_poll_seconds", 1.0) or 1.0))
    return min(normal_poll_interval_seconds(), requested)


def open_position_poll_interval_seconds() -> float:
    requested = max(0.5, float(getattr(SETTINGS, "open_position_poll_seconds", 1.0) or 1.0))
    return min(normal_poll_interval_seconds(), requested)


def next_cycle_interval_seconds(*, has_pending_orders: bool, has_open_positions: bool = False) -> float:
    if has_pending_orders:
        return pending_order_poll_interval_seconds()
    if has_open_positions:
        return open_position_poll_interval_seconds()
    return normal_poll_interval_seconds()


def idle_sleep_seconds(*, has_open_positions: bool, has_pending_orders: bool, secs_left: float | None = None) -> float:
    if has_pending_orders:
        return pending_order_poll_interval_seconds()
    if has_open_positions:
        return open_position_poll_interval_seconds()
    if secs_left is not None and 200 <= secs_left <= 260:
        return 1.0
    return float(getattr(SETTINGS, "poll_seconds", 3) or 3.0)



STATE_VERSION = 2


class GracefulStop(SystemExit):
    pass


STOP_REQUEST = {"signal": None}


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def maybe_log_position_watch(
    pos,
    *,
    pnl_pct: float,
    hard_stop_pnl_pct: float,
    profit_pnl_pct: float | None,
    hold_sec: float,
    secs_left: float | None,
    mark: float | None,
    observed_value: float | None,
    profit_reference_value: float | None,
    exit_decision: ExitDecision,
):
    if not getattr(SETTINGS, "position_watch_debug_enabled", True):
        return

    decision = exit_decision.reason if getattr(exit_decision, "should_close", False) else "hold"
    interval = max(1.0, float(getattr(SETTINGS, "position_watch_log_interval_sec", 5.0) or 5.0))
    rounded_secs_left = -1 if secs_left is None else int(secs_left // 5)
    mark_bucket = -1 if mark is None else int(float(mark) * 1000)
    observed_bucket = -1 if observed_value is None else int(float(observed_value) * 1000)
    profit_bucket = -1 if profit_pnl_pct is None else int(float(profit_pnl_pct) * 1000)
    signature = (
        f"{decision}|{int(pnl_pct * 1000)}|{int(hard_stop_pnl_pct * 1000)}|"
        f"{profit_bucket}|{rounded_secs_left}|{mark_bucket}|{observed_bucket}|"
        f"{int(bool(getattr(pos, 'force_close_only', False)))}|{int(bool(getattr(pos, 'pending_confirmation', False)))}"
    )
    now_ts = time.time()
    if signature == getattr(pos, "last_watch_log_sig", "") and (now_ts - float(getattr(pos, "last_watch_log_ts", 0.0) or 0.0)) < interval:
        return

    pos.last_watch_log_sig = signature
    pos.last_watch_log_ts = now_ts
    flags = []
    if getattr(pos, "pending_confirmation", False):
        flags.append("pending-confirmation")
    if getattr(pos, "force_close_only", False):
        flags.append("force-close-only")
    if getattr(pos, "has_scaled_out_loss", False):
        flags.append("scaled-out-loss")
    if getattr(pos, "has_taken_partial", False):
        flags.append("partial-profit")
    if getattr(pos, "has_extracted_principal", False):
        flags.append("principal-extracted")
    flags_text = ",".join(flags) if flags else "none"
    mark_text = "n/a" if mark is None else f"{float(mark):.3f}"
    observed_text = "n/a" if observed_value is None else f"{float(observed_value):.4f}"
    profit_ref_text = "n/a" if profit_reference_value is None else f"{float(profit_reference_value):.4f}"
    profit_return_text = "n/a" if profit_pnl_pct is None else f"{float(profit_pnl_pct):.2%}"
    secs_left_text = "n/a" if secs_left is None else f"{secs_left:.0f}s"
    log(
        f"position watch | side={pos.side} slug={pos.slug} hold={hold_sec:.0f}s secs_left={secs_left_text} "
        f"mark={mark_text} decision_ref=${observed_text} profit_ref=${profit_ref_text} "
        f"observed_return={pnl_pct:.2%} hard_stop_return={hard_stop_pnl_pct:.2%} profit_return={profit_return_text} "
        f"decision={decision} flags={flags_text}"
    )


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
    max_favorable_ts: float = 0.0
    has_scaled_out: bool = False
    has_scaled_out_loss: bool = False
    has_taken_partial: bool = False
    has_extracted_principal: bool = False
    has_panic_dumped: bool = False
    profit_plateau_entry_ts: float = 0.0
    force_close_only: bool = False
    is_moonbag: bool = False
    entry_shares: float = 0.0
    runner_peak_value_usd: float = 0.0
    runner_peak_ts: float = 0.0
    dust_retry_count: int = 0  # Number of times this residual lot has been kept for retry
    last_watch_log_ts: float = 0.0
    last_watch_log_sig: str = ""
    soft_stop_breach_ts: float = 0.0
    binance_adverse_breach_ts: float = 0.0
    binance_profit_protect_breach_ts: float = 0.0

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
    entry_reason: str = "signal"
    fallback_attempted: bool = False
    disappeared_since_ts: float = 0.0
    cancel_requested: bool = False


@dataclass
class RuntimeFlags:
    live_consec_losses: int
    last_loss_side: str
    close_fail_streak: int
    panic_exit_mode: bool
    network_fail_safe_mode: bool = False
    api_fail_streak: int = 0
    slow_api_streak: int = 0
    ws_stale_streak: int = 0
    network_recovery_streak: int = 0
    last_api_latency_ms: float = 0.0


def normalize_execution_style(style: str | None, *, default: str = "unknown") -> str:
    raw = str(style or "").strip().lower()
    if not raw:
        return default
    if "mixed" in raw:
        return "mixed"
    if "expiry" in raw:
        return "expiry-settlement"
    if "taker" in raw:
        return "taker"
    if raw in {"maker-timeout-fallback", "simulated-cross", "dry-run-cross", "dry_run_cross"}:
        return "taker"
    if "timeout-fallback" in raw:
        return "taker"
    if "maker" in raw:
        return "maker"
    if raw in {"dry-run", "dry_run"}:
        return default
    return raw


def principal_extraction_complete(
    recovered_usd: float,
    target_principal_usd: float,
    *,
    recovery_ratio: float = 0.95,
) -> bool:
    recovered = max(0.0, float(recovered_usd or 0.0))
    target = max(0.0, float(target_principal_usd or 0.0))
    if target <= LOT_EPS_COST_USD:
        return True
    return recovered >= target * max(0.0, float(recovery_ratio))


def principal_extraction_sell_fraction(
    current_value_usd: float,
    target_principal_usd: float,
    *,
    current_shares: float | None = None,
    target_remaining_shares: float | None = None,
) -> float:
    current_total_value = max(1e-9, float(current_value_usd or 0.0))
    target = max(0.0, float(target_principal_usd or 0.0))
    sell_fraction = max(0.0, target / current_total_value)
    if current_shares is not None and target_remaining_shares is not None:
        shares_now = max(1e-9, float(current_shares or 0.0))
        desired_remaining = min(shares_now, max(0.0, float(target_remaining_shares or 0.0)))
        sell_fraction = max(sell_fraction, 1.0 - (desired_remaining / shares_now))
    return min(0.99, sell_fraction)


def realized_exit_pnl(
    actual_exit_value_usd: float | None,
    observed_exit_value_usd: float,
    realized_cost_usd: float,
) -> float:
    actual_value = None if actual_exit_value_usd is None else float(actual_exit_value_usd)
    observed_value = float(observed_exit_value_usd or 0.0)
    realized_cost = float(realized_cost_usd or 0.0)
    recovered_value = actual_value if actual_value is not None and actual_value > 0.0 else observed_value
    return recovered_value - realized_cost


def reference_entry_shares(pos: OpenPos) -> float:
    recorded = max(0.0, float(getattr(pos, "entry_shares", 0.0) or 0.0))
    current = max(0.0, float(getattr(pos, "shares", 0.0) or 0.0))
    if recorded > LOT_EPS_SHARES:
        return max(recorded, current)
    if getattr(pos, "has_taken_partial", False) and not getattr(pos, "has_extracted_principal", False):
        partial_fraction = min(
            0.95,
            max(0.05, float(getattr(SETTINGS, "take_profit_partial_fraction", 0.30) or 0.30)),
        )
        remaining_fraction = max(1e-9, 1.0 - partial_fraction)
        inferred = current / remaining_fraction
        if inferred > LOT_EPS_SHARES:
            return inferred
    return current


def target_runner_remaining_shares(pos: OpenPos) -> float:
    entry_shares = reference_entry_shares(pos)
    runner_fraction = min(0.95, max(0.0, float(getattr(SETTINGS, "take_profit_runner_fraction", 0.10) or 0.10)))
    return max(0.0, entry_shares * runner_fraction)


def entry_velocity_gate_rejects(
    signal_side: str | None,
    signal_origin: str | None,
    ws_velocity: float,
    *,
    current_ws_velocity: float | None = None,
    require_dual_confirmation: bool | None = None,
) -> bool:
    side = str(signal_side or "").strip().upper()
    origin = str(signal_origin or "").strip().lower()
    vel = float(ws_velocity or 0.0)
    current_vel = vel if current_ws_velocity is None else float(current_ws_velocity or 0.0)
    dual_confirm = (
        bool(getattr(SETTINGS, "entry_dual_velocity_confirm", True))
        if require_dual_confirmation is None
        else bool(require_dual_confirmation)
    )

    if side not in {"UP", "DOWN"}:
        return False

    def _adverse(v: float, threshold: float) -> bool:
        if side == "UP":
            return v < -threshold
        return v > threshold

    if "ws_order_flow_" in origin:
        if _adverse(vel, 0.0):
            return True
        if dual_confirm and _adverse(current_vel, 0.0):
            return True
        return False

    entry_vel_min = max(0.0, float(getattr(SETTINGS, "entry_velocity_min", 0.0) or 0.0))
    if entry_vel_min <= 0.0:
        return False

    if _adverse(vel, entry_vel_min):
        return True
    if dual_confirm and _adverse(current_vel, entry_vel_min):
        return True
    return False


def extract_entry_response_details(resp: dict | None) -> tuple[float, str]:
    payload = resp.get("response", {}) if isinstance(resp, dict) else {}
    if not isinstance(payload, dict):
        return 0.0, ""
    shares = float(payload.get("takingAmount", 0) or 0.0)
    order_id = str(payload.get("orderID") or "")
    return shares, order_id


def extract_entry_cost_usd(resp: dict | None, fallback_usd: float) -> float:
    if not isinstance(resp, dict):
        return float(fallback_usd)
    actual_cost = resp.get("actual_entry_cost_usd")
    if actual_cost is not None:
        try:
            actual_cost = float(actual_cost)
            if actual_cost > 0:
                return actual_cost
        except Exception:
            pass
    amount_usd = resp.get("amount_usd")
    if amount_usd is not None:
        try:
            amount_usd = float(amount_usd)
            if amount_usd > 0:
                return amount_usd
        except Exception:
            pass
    return float(fallback_usd)


def extract_entry_implied_avg_price(resp: dict | None, fallback_usd: float = 0.0) -> float | None:
    shares, _ = extract_entry_response_details(resp)
    if shares <= 0:
        return None
    actual_cost = extract_entry_cost_usd(resp, fallback_usd)
    if actual_cost <= 0:
        return None
    return actual_cost / max(shares, 1e-9)


def entry_slippage_breach(
    *,
    expected_entry_price: float | None,
    actual_avg_price: float | None,
    dry_run: bool,
) -> tuple[bool, float]:
    if dry_run or not bool(getattr(SETTINGS, "entry_slippage_guard_enabled", True)):
        return False, 0.0
    try:
        expected = float(expected_entry_price or 0.0)
        actual = float(actual_avg_price or 0.0)
    except Exception:
        return False, 0.0
    if expected <= 0.0 or actual <= 0.0:
        return False, 0.0
    premium_pct = (actual / max(expected, 1e-9)) - 1.0
    breach = premium_pct > max(0.0, float(getattr(SETTINGS, "entry_max_actual_slippage_pct", 0.18) or 0.18))
    return breach, premium_pct


def entry_response_has_actionable_state(resp: dict | None) -> bool:
    shares, order_id = extract_entry_response_details(resp)
    return shares > 0 or bool(order_id)


def should_count_entry_toward_market_limit(*, slippage_breach: bool, shares: float, order_id: str | None) -> bool:
    if slippage_breach:
        return False
    if float(shares or 0.0) > LOT_EPS_SHARES:
        return True
    return bool(str(order_id or "").strip())


LOSS_EXIT_REASONS = {
    "moonbag-drawdown-stop",
    "post-scaleout-stop-loss",
    "residual-force-close",
    "failed-follow-through",
    "hard-stop-loss",
    "smart-stop-loss",
    "stop-loss",
    "stop-loss-full",
    "stop-loss-scale-out",
    "deadline-exit-loss",
    "max-hold-loss",
    "max-hold-loss-extended",
}


def is_loss_exit_reason(reason: str | None) -> bool:
    return str(reason or "").strip().lower() in LOSS_EXIT_REASONS


def effective_stop_loss_partial_fraction(*, dry_run: bool) -> float:
    configured = (
        getattr(SETTINGS, "stop_loss_partial_fraction", 0.50)
        if dry_run
        else getattr(
            SETTINGS,
            "live_stop_loss_partial_fraction",
            getattr(SETTINGS, "stop_loss_partial_fraction", 0.50),
        )
    )
    return min(0.99, max(0.01, float(configured or 0.50)))


def should_delay_soft_stop_scaleout(
    *,
    reason: str | None,
    side: str | None,
    pnl_pct: float,
    breach_age_sec: float,
    secs_left: float | None,
    ws_velocity: float,
) -> bool:
    if str(reason or "").strip().lower() != "stop-loss-scale-out":
        return False
    confirm_sec = max(0.0, float(getattr(SETTINGS, "soft_stop_confirm_sec", 0.0) or 0.0))
    if confirm_sec <= 0.0 or breach_age_sec >= confirm_sec:
        return False
    exit_deadline_sec = float(getattr(SETTINGS, "exit_deadline_sec", 20) or 20.0)
    if secs_left is not None and secs_left <= exit_deadline_sec + 5.0:
        return False
    partial_pct = abs(float(getattr(SETTINGS, "stop_loss_partial_pct", 0.05) or 0.05))
    buffer_pct = abs(float(getattr(SETTINGS, "soft_stop_confirm_buffer_pct", 0.03) or 0.03))
    if pnl_pct <= -(partial_pct + buffer_pct):
        return False
    adverse_velocity = abs(float(getattr(SETTINGS, "soft_stop_adverse_velocity", 0.0003) or 0.0003))
    normalized_side = str(side or "").strip().upper()
    if normalized_side == "UP" and ws_velocity <= -adverse_velocity:
        return False
    if normalized_side == "DOWN" and ws_velocity >= adverse_velocity:
        return False
    return True


def should_trigger_profit_reversal_exit(
    *,
    has_extracted_principal: bool,
    side: str | None,
    profit_pnl_pct: float | None,
    mfe_pnl_pct: float,
    current_value_usd: float,
    peak_value_usd: float,
    ws_velocity: float,
    secs_left: float | None,
) -> bool:
    if has_extracted_principal or not bool(getattr(SETTINGS, "profit_reversal_enabled", True)):
        return False
    if profit_pnl_pct is None:
        return False
    if secs_left is not None and secs_left <= float(getattr(SETTINGS, "exit_deadline_sec", 20) or 20.0):
        return False
    min_mfe_pct = float(getattr(SETTINGS, "profit_reversal_min_mfe_pct", 0.50) or 0.50)
    min_current_profit_pct = float(getattr(SETTINGS, "profit_reversal_min_current_profit_pct", 0.12) or 0.12)
    if mfe_pnl_pct < min_mfe_pct or profit_pnl_pct < min_current_profit_pct:
        return False
    peak_value = max(float(peak_value_usd or 0.0), float(current_value_usd or 0.0))
    current_value = max(0.0, float(current_value_usd or 0.0))
    if peak_value <= LOT_EPS_COST_USD or current_value <= LOT_EPS_COST_USD:
        return False
    drawdown_pct = (current_value - peak_value) / max(peak_value, 1e-9)
    required_drawdown_pct = abs(float(getattr(SETTINGS, "profit_reversal_drawdown_pct", 0.18) or 0.18))
    if drawdown_pct > -required_drawdown_pct:
        return False
    adverse_velocity = abs(float(getattr(SETTINGS, "profit_reversal_adverse_velocity", 0.0003) or 0.0003))
    normalized_side = str(side or "").strip().upper()
    if normalized_side == "UP":
        return ws_velocity <= -adverse_velocity
    if normalized_side == "DOWN":
        return ws_velocity >= adverse_velocity
    return False


def should_trigger_binance_adverse_exit(
    *,
    has_extracted_principal: bool,
    side: str | None,
    pnl_pct: float,
    profit_pnl_pct: float | None,
    hold_sec: float,
    breach_age_sec: float,
    secs_left: float | None,
    ws_velocity: float,
    current_ws_velocity: float | None = None,
) -> bool:
    if has_extracted_principal or not bool(getattr(SETTINGS, "binance_adverse_exit_enabled", True)):
        return False
    if hold_sec < float(getattr(SETTINGS, "binance_adverse_exit_min_hold_sec", 4.0) or 4.0):
        return False
    if secs_left is not None and secs_left <= float(getattr(SETTINGS, "exit_deadline_sec", 20) or 20.0) + 5.0:
        return False
    confirm_sec = max(0.0, float(getattr(SETTINGS, "binance_adverse_exit_confirm_sec", 3.0) or 3.0))
    if breach_age_sec < confirm_sec:
        return False
    safe_profit_pnl_pct = (
        float(profit_pnl_pct)
        if profit_pnl_pct is not None
        else min(float(pnl_pct or 0.0), 0.0)
    )
    max_safe_profit_pct = float(getattr(SETTINGS, "binance_adverse_exit_max_profit_pct", 0.08) or 0.08)
    if safe_profit_pnl_pct > max_safe_profit_pct:
        return False
    adverse_velocity = abs(float(getattr(SETTINGS, "binance_adverse_exit_velocity", 0.00035) or 0.00035))
    normalized_side = str(side or "").strip().upper()
    lag_adverse = (
        (normalized_side == "UP" and ws_velocity <= -adverse_velocity)
        or (normalized_side == "DOWN" and ws_velocity >= adverse_velocity)
    )
    if not lag_adverse:
        return False
    if bool(getattr(SETTINGS, "binance_adverse_exit_require_current_confirm", True)):
        current_vel = float(current_ws_velocity if current_ws_velocity is not None else ws_velocity)
        current_adverse = (
            (normalized_side == "UP" and current_vel <= -adverse_velocity)
            or (normalized_side == "DOWN" and current_vel >= adverse_velocity)
        )
        if not current_adverse:
            return False
    return normalized_side in {"UP", "DOWN"}


def should_trigger_binance_profit_protect_exit(
    *,
    has_extracted_principal: bool,
    side: str | None,
    profit_pnl_pct: float | None,
    take_profit_soft_pct: float,
    hold_sec: float,
    peak_age_sec: float | None,
    breach_age_sec: float,
    secs_left: float | None,
    ws_velocity: float,
    current_ws_velocity: float | None = None,
) -> bool:
    if has_extracted_principal or not bool(getattr(SETTINGS, "binance_profit_protect_enabled", True)):
        return False
    if profit_pnl_pct is None:
        return False
    if hold_sec < float(getattr(SETTINGS, "binance_profit_protect_min_hold_sec", 8.0) or 8.0):
        return False
    if secs_left is not None and secs_left <= float(getattr(SETTINGS, "exit_deadline_sec", 20) or 20.0) + 10.0:
        return False
    if peak_age_sec is None:
        return False
    stall_sec = max(0.0, float(getattr(SETTINGS, "binance_profit_protect_stall_sec", 8.0) or 8.0))
    if peak_age_sec < stall_sec:
        return False
    confirm_sec = max(0.0, float(getattr(SETTINGS, "binance_profit_protect_confirm_sec", 2.0) or 2.0))
    if breach_age_sec < confirm_sec:
        return False
    current_profit_pct = float(profit_pnl_pct or 0.0)
    min_profit_pct = float(getattr(SETTINGS, "binance_profit_protect_min_profit_pct", 0.06) or 0.06)
    max_profit_pct = min(
        float(getattr(SETTINGS, "binance_profit_protect_max_profit_pct", 0.18) or 0.18),
        max(0.0, float(take_profit_soft_pct or 0.0) - 0.01),
    )
    if current_profit_pct < min_profit_pct or current_profit_pct > max_profit_pct:
        return False
    adverse_velocity = abs(float(getattr(SETTINGS, "binance_profit_protect_velocity", 0.00025) or 0.00025))
    normalized_side = str(side or "").strip().upper()
    lag_adverse = (
        (normalized_side == "UP" and ws_velocity <= -adverse_velocity)
        or (normalized_side == "DOWN" and ws_velocity >= adverse_velocity)
    )
    if not lag_adverse:
        return False
    if bool(getattr(SETTINGS, "binance_profit_protect_require_current_confirm", True)):
        current_vel = float(current_ws_velocity if current_ws_velocity is not None else ws_velocity)
        current_adverse = (
            (normalized_side == "UP" and current_vel <= -adverse_velocity)
            or (normalized_side == "DOWN" and current_vel >= adverse_velocity)
        )
        if not current_adverse:
            return False
    return normalized_side in {"UP", "DOWN"}


def should_force_full_loss_exit(*, reason: str | None, dry_run: bool) -> bool:
    normalized = str(reason or "").strip().lower()
    return (
        not dry_run
        and bool(getattr(SETTINGS, "live_force_full_loss_exit", True))
        and is_loss_exit_reason(normalized)
        and normalized != "stop-loss-scale-out"
    )


def should_arm_residual_force_close_after_stop_loss_scaleout(
    *,
    dry_run: bool,
    remaining_shares: float,
    remaining_cost_usd: float,
) -> bool:
    return (
        not dry_run
        and float(remaining_shares or 0.0) > LOT_EPS_SHARES
        and float(remaining_cost_usd or 0.0) > LOT_EPS_COST_USD
    )


def should_force_taker_take_profit(*, dry_run: bool) -> bool:
    if dry_run:
        return False
    return bool(getattr(SETTINGS, "live_take_profit_force_taker", True))


def should_force_taker_profit_protection(*, reason: str | None, dry_run: bool) -> bool:
    if dry_run:
        return False
    normalized = str(reason or "").strip().lower()
    return normalized in {
        "take-profit-full",
        "binance-adverse-exit",
        "binance-profit-protect-exit",
        "profit-reversal-stop",
        "deadline-exit-weak-win",
        "deadline-exit-flat",
    } and bool(
        getattr(SETTINGS, "live_take_profit_force_taker", True)
    )


def emergency_exit_retry_kwargs(*, reason: str | None, secs_left: float | None, dry_run: bool) -> dict[str, float | int]:
    if dry_run or secs_left is None:
        return {}
    normalized = str(reason or "").strip().lower()
    if normalized not in {
        "deadline-exit-weak-win",
        "deadline-exit-flat",
        "deadline-exit-loss",
        "residual-force-close",
    }:
        return {}
    if float(secs_left) > float(getattr(SETTINGS, "exit_deadline_sec", 20) or 20):
        return {}
    return {
        "retry_delay_sec": max(0.25, float(getattr(SETTINGS, "emergency_exit_retry_delay_sec", 1.0) or 1.0)),
        "max_attempts": max(1, int(getattr(SETTINGS, "emergency_exit_max_attempts", 8) or 8)),
    }


def should_force_taker_exit(*, reason: str | None, dry_run: bool, has_panic_dumped: bool = False) -> bool:
    if has_panic_dumped:
        return True
    return (
        not dry_run
        and bool(getattr(SETTINGS, "live_loss_exit_force_taker", True))
        and is_loss_exit_reason(reason)
    )


def update_runner_peak(pos: OpenPos, current_value_usd: float, *, now_ts: float | None = None) -> tuple[float, float | None]:
    value = max(0.0, float(current_value_usd or 0.0))
    if value <= LOT_EPS_COST_USD:
        return 0.0, None
    now_value = float(now_ts or time.time())
    peak = max(0.0, float(getattr(pos, "runner_peak_value_usd", 0.0) or 0.0))
    peak_ts = float(getattr(pos, "runner_peak_ts", 0.0) or 0.0)
    if peak <= LOT_EPS_COST_USD or value >= peak - 1e-9:
        pos.runner_peak_value_usd = value
        pos.runner_peak_ts = now_value
        return 0.0, 0.0
    drawdown_pct = (value - peak) / max(peak, 1e-9)
    peak_age_sec = max(0.0, now_value - peak_ts) if peak_ts > 0 else None
    return drawdown_pct, peak_age_sec


def favorable_peak_age_sec(pos: OpenPos, *, now_ts: float | None = None) -> float | None:
    peak_value = max(0.0, float(getattr(pos, "max_favorable_value_usd", 0.0) or 0.0))
    peak_ts = float(getattr(pos, "max_favorable_ts", 0.0) or 0.0)
    if peak_value <= LOT_EPS_COST_USD or peak_ts <= 0.0:
        return None
    now_value = float(now_ts or time.time())
    return max(0.0, now_value - peak_ts)


def decide_pending_order_action(
    *,
    order_still_open: bool,
    age_sec: float,
    side: str,
    ws_vel: float,
    cancel_velocity: float,
    timeout_sec: float,
    has_live_position: bool,
    fallback_enabled: bool,
    fallback_attempted: bool,
) -> str:
    if not order_still_open:
        return "filled" if has_live_position else "gone"
    if cancel_velocity > 0.0:
        reversal = (
            (side == "UP" and ws_vel < -cancel_velocity)
            or (side == "DOWN" and ws_vel > cancel_velocity)
        )
        if reversal:
            return "cancel-reversal"
    if age_sec > timeout_sec:
        if has_live_position:
            return "filled"
        if fallback_enabled and not fallback_attempted:
            return "fallback-taker"
        return "cancel-timeout"
    return "wait"


def track_pending_fill(
    open_positions: list["OpenPos"],
    po: PendingOrder,
    *,
    shares: float,
    cost_usd: float,
    entry_reason: str | None = None,
    source: str = "pending-order",
    execution_style: str | None = None,
) -> bool:
    if shares <= LOT_EPS_SHARES:
        return False
    if any(p.token_id == po.token_id for p in open_positions):
        return False
    opened_ts = time.time()
    position_id = f"pos_{int(opened_ts)}_{po.token_id[-6:]}"
    reason = entry_reason or po.entry_reason or "signal"
    open_positions.append(OpenPos(
        slug=po.slug,
        side=po.side,
        token_id=po.token_id,
        shares=shares,
        entry_shares=shares,
        cost_usd=cost_usd,
        opened_ts=opened_ts,
        position_id=position_id,
        entry_reason=reason,
        source=source,
        pending_confirmation=True,
        max_favorable_value_usd=cost_usd,
        max_adverse_value_usd=cost_usd,
        max_favorable_pnl_usd=0.0,
        max_adverse_pnl_usd=0.0,
        max_favorable_ts=opened_ts,
    ))
    append_event({
        "kind": "entry",
        "slug": po.slug,
        "side": po.side,
        "token_id": po.token_id,
        "position_id": position_id,
        "shares": shares,
        "cost_usd": cost_usd,
        "opened_ts": opened_ts,
        "entry_reason": reason,
        "classification": source,
        "execution_style": normalize_execution_style(execution_style or source, default="maker"),
        "mae_pnl_usd": 0.0,
        "mfe_pnl_usd": 0.0,
    })
    return True


def assess_entry_liquidity(
    *,
    book: dict | None,
    est_shares: float,
    max_spread: float,
    min_best_ask_multiple: float,
    min_total_ask_multiple: float,
) -> dict[str, float | bool | str | None]:
    if not isinstance(book, dict):
        return {"ok": True, "available": False, "reason": "book-unavailable"}

    best_bid = float(book.get("best_bid", 0.0) or 0.0)
    best_ask = float(book.get("best_ask", 0.0) or 0.0)
    best_ask_size = float(book.get("best_ask_size", 0.0) or 0.0)
    asks_volume = float(book.get("asks_volume", 0.0) or 0.0)

    if best_bid <= 0.0 or best_ask <= 0.0 or best_ask < best_bid:
        return {"ok": True, "available": False, "reason": "book-unavailable"}

    spread = max(0.0, best_ask - best_bid)
    min_best_ask = max(0.0, est_shares * max(0.0, min_best_ask_multiple))
    min_total_ask = max(0.0, est_shares * max(0.0, min_total_ask_multiple))

    if max_spread > 0.0 and spread > max_spread:
        return {
            "ok": False,
            "available": True,
            "reason": "spread-too-wide",
            "spread": spread,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "best_ask_size": best_ask_size,
            "asks_volume": asks_volume,
        }

    if min_best_ask > 0.0 and best_ask_size < min_best_ask:
        return {
            "ok": False,
            "available": True,
            "reason": "best-ask-too-thin",
            "spread": spread,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "best_ask_size": best_ask_size,
            "asks_volume": asks_volume,
            "required_best_ask": min_best_ask,
        }

    if min_total_ask > 0.0 and asks_volume < min_total_ask:
        return {
            "ok": False,
            "available": True,
            "reason": "ask-depth-too-thin",
            "spread": spread,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "best_ask_size": best_ask_size,
            "asks_volume": asks_volume,
            "required_asks_volume": min_total_ask,
        }

    return {
        "ok": True,
        "available": True,
        "reason": "ok",
        "spread": spread,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
        "asks_volume": asks_volume,
    }


def should_block_live_entry_for_unavailable_book(
    *,
    dry_run: bool,
    entry_book_quality: dict | None,
) -> tuple[bool, str]:
    if dry_run:
        return False, ""
    if not isinstance(entry_book_quality, dict):
        return True, "book-unavailable"
    if bool(entry_book_quality.get("available")):
        return False, ""
    return True, str(entry_book_quality.get("reason") or "book-unavailable")


def estimate_book_entry_fill(
    *,
    book: dict | None,
    amount_usd: float,
) -> tuple[float | None, float, float]:
    target_notional = max(0.0, float(amount_usd or 0.0))
    if target_notional <= 0.0:
        return 0.0, 0.0, 1.0
    if not isinstance(book, dict):
        return None, 0.0, 0.0

    ask_levels = book.get("ask_levels")
    if isinstance(ask_levels, list) and ask_levels:
        remaining_notional = target_notional
        spent_notional = 0.0
        acquired_shares = 0.0
        for lv in ask_levels:
            if not isinstance(lv, (list, tuple)) or len(lv) < 2:
                continue
            try:
                ask_price = float(lv[0] or 0.0)
                ask_size = float(lv[1] or 0.0)
            except Exception:
                continue
            if ask_price <= 0.0 or ask_size <= 0.0:
                continue
            level_notional = ask_price * ask_size
            take_notional = min(remaining_notional, level_notional)
            if take_notional <= 0.0:
                continue
            acquired_shares += take_notional / ask_price
            spent_notional += take_notional
            remaining_notional -= take_notional
            if remaining_notional <= 1e-9:
                break

        fill_ratio = min(1.0, spent_notional / target_notional) if target_notional > 0.0 else 1.0
        if acquired_shares > 0.0:
            return spent_notional / acquired_shares, acquired_shares, fill_ratio
        return None, 0.0, fill_ratio

    best_ask = float(book.get("best_ask", 0.0) or 0.0)
    if best_ask <= 0.0:
        return None, 0.0, 0.0
    best_ask_size = float(book.get("best_ask_size", 0.0) or 0.0)
    acquired_shares = target_notional / best_ask
    fill_ratio = 1.0
    if best_ask_size > 0.0:
        fill_ratio = min(1.0, best_ask_size / max(acquired_shares, 1e-9))
    return best_ask, acquired_shares, fill_ratio


def place_entry_order_with_retry(
    ex: PolymarketExchange,
    side: str,
    amount_usd: float,
    token_id: str,
    *,
    simulated_price: float | None,
    force_taker: bool,
    max_attempts: int,
    backoff_sec: float,
) -> tuple[dict, list[float], int]:
    attempts = max(1, int(max_attempts))
    backoff = max(0.0, float(backoff_sec))
    latencies_ms: list[float] = []
    last_resp: dict | None = None
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            resp = ex.place_order(
                side,
                amount_usd,
                token_id,
                simulated_price=simulated_price,
                force_taker=force_taker,
            )
            latencies_ms.append((time.perf_counter() - started) * 1000.0)
            last_resp = resp
            if entry_response_has_actionable_state(resp):
                return resp, latencies_ms, attempt
            last_error = RuntimeError("no-takingAmount-no-orderID")
        except Exception as exc:
            latencies_ms.append((time.perf_counter() - started) * 1000.0)
            last_error = exc
            err_text = str(exc).lower()
            if (
                "order size below minimum" in err_text
                or ("lower than the minimum" in err_text and "size" in err_text)
                or "order notional exceeds live cap" in err_text
                or "not enough balance / allowance" in err_text
            ):
                raise
            if attempt >= attempts:
                raise

        if attempt < attempts:
            log(
                f"entry retry scheduled | side={side} attempt={attempt + 1}/{attempts} "
                f"reason={last_error}"
            )
            time.sleep(backoff)

    if last_resp is not None:
        return last_resp, latencies_ms, attempts
    raise last_error or RuntimeError("entry retry exhausted without response")


def realistic_exit_value(pos: OpenPos, up: float | None, down: float | None, ob_up: dict | None, ob_down: dict | None) -> float | None:
    mark = up if pos.side == "UP" else down
    if mark is None:
        return None
    orderbook = None
    if pos.side == "UP" and ob_up:
        orderbook = ob_up
    elif pos.side == "DOWN" and ob_down:
        orderbook = ob_down

    executable_value, _fill_ratio = estimate_book_exit_value(orderbook, pos.shares)
    if executable_value is not None:
        return float(executable_value)

    best_bid = None
    if orderbook:
        best_bid = orderbook.get("best_bid")
    if best_bid is not None and float(best_bid) > 0:
        return pos.shares * float(best_bid)

    # In live trading, missing executable book depth should not be treated as
    # a real exit value. Otherwise optimistic marks create phantom profits.
    if not getattr(SETTINGS, "dry_run", False):
        return None

    # Without orderbook depth passed in Dry Run polling, we assume Maker/Limit orders track the mark exactly over time without massive taker penalties.
    return pos.shares * float(mark)


def executable_take_profit_value(pos: OpenPos, ob_up: dict | None, ob_down: dict | None) -> float | None:
    orderbook = None
    if pos.side == "UP" and ob_up:
        orderbook = ob_up
    elif pos.side == "DOWN" and ob_down:
        orderbook = ob_down
    if not isinstance(orderbook, dict):
        return None
    executable_value, _fill_ratio = estimate_book_exit_value(orderbook, pos.shares)
    if executable_value is None:
        return None
    return float(executable_value)


def conservative_exit_decision_value(
    pos: OpenPos,
    *,
    executable_exit_value: float | None,
    mark_value: float | None,
) -> float:
    if executable_exit_value is not None:
        return float(executable_exit_value)
    if mark_value is not None:
        # When we only have a mark, allow it to reveal downside but never to
        # manufacture unrealized profits that cannot actually be sold.
        return min(float(mark_value), float(pos.cost_usd))
    return float(pos.cost_usd)


def resolve_close_remaining_shares(
    *,
    requested_shares: float,
    sold_shares: float,
    remaining_hint: float | None,
) -> float:
    requested = max(0.0, float(requested_shares or 0.0))
    sold = min(requested, max(0.0, float(sold_shares or 0.0)))
    local_remaining = max(0.0, requested - sold)
    if remaining_hint is None:
        return 0.0 if local_remaining <= LOT_EPS_SHARES else local_remaining
    try:
        hinted_remaining = float(remaining_hint or 0.0)
    except Exception:
        hinted_remaining = local_remaining
    hinted_remaining = min(requested, max(0.0, hinted_remaining))
    if hinted_remaining <= LOT_EPS_SHARES:
        return 0.0
    # Trust the exchange-provided residual hint over simple subtraction. This
    # keeps local state aligned after partial fills or allowance-limited sweeps.
    return hinted_remaining


def resolve_effective_closed_shares(
    *,
    starting_shares: float,
    sold_shares: float,
    remaining_shares: float,
) -> float:
    starting = max(0.0, float(starting_shares or 0.0))
    explicit_sold = min(starting, max(0.0, float(sold_shares or 0.0)))
    remaining = min(starting, max(0.0, float(remaining_shares or 0.0)))
    hinted_sold = max(0.0, starting - remaining)
    return min(starting, max(explicit_sold, hinted_sold))


def paper_settlement_from_last_mark(last_mark: float | None) -> tuple[float, str]:
    if last_mark is None:
        return 0.0, "binary-unknown-conservative"
    if last_mark > 0.5:
        return 1.0, "binary-win"
    if last_mark < 0.5:
        return 0.0, "binary-lose"
    return 0.5, "binary-neutral"


def strategy_name_for_side(strategy_name: str, side: str | None) -> str:
    base = str(strategy_name or "").split("+")[0]
    target = str(side or "").upper()
    if target not in {"UP", "DOWN"}:
        return base
    lower = base.lower()
    if lower.endswith("_up"):
        return f"{base[:-3]}_{target.lower()}"
    if lower.endswith("_down"):
        return f"{base[:-5]}_{target.lower()}"
    return base


def timed_call(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return result, elapsed_ms


def current_ws_age() -> float:
    try:
        return float(BINANCE_WS.get_last_update_age())
    except Exception:
        return float("inf")


def observe_api_latency(flags: RuntimeFlags, label: str, elapsed_ms: float) -> bool:
    flags.last_api_latency_ms = max(float(getattr(flags, "last_api_latency_ms", 0.0) or 0.0), float(elapsed_ms))
    threshold_ms = float(getattr(SETTINGS, "api_slow_threshold_ms", 1500.0))
    if elapsed_ms >= threshold_ms:
        log(f"slow api | call={label} latency_ms={elapsed_ms:.0f} threshold_ms={threshold_ms:.0f}")
        return True
    return False


def update_network_guard(
    flags: RuntimeFlags,
    *,
    ws_age: float,
    cycle_had_slow_api: bool = False,
    cycle_api_error: bool = False,
) -> list[str]:
    notes: list[str] = []
    prev_mode = bool(getattr(flags, "network_fail_safe_mode", False))
    stale_limit = float(getattr(SETTINGS, "ws_stale_max_age_sec", 5.0))

    if ws_age > stale_limit:
        flags.ws_stale_streak += 1
        notes.append(
            f"ws stale detected | age={ws_age:.1f}s threshold={stale_limit:.1f}s streak={flags.ws_stale_streak}"
        )
    else:
        flags.ws_stale_streak = 0

    if cycle_had_slow_api:
        flags.slow_api_streak += 1
        notes.append(
            f"slow api streak | streak={flags.slow_api_streak} last_latency_ms={flags.last_api_latency_ms:.0f}"
        )
    else:
        flags.slow_api_streak = 0

    if cycle_api_error:
        flags.api_fail_streak += 1
        notes.append(f"api failure streak | streak={flags.api_fail_streak}")
    else:
        flags.api_fail_streak = 0

    fail_safe_threshold = int(getattr(SETTINGS, "api_fail_safe_streak", 3))
    ws_fail_safe_threshold = int(getattr(SETTINGS, "ws_stale_fail_safe_streak", 2))
    should_activate = (
        flags.api_fail_streak >= fail_safe_threshold
        or flags.slow_api_streak >= fail_safe_threshold
        or flags.ws_stale_streak >= ws_fail_safe_threshold
    )

    if should_activate:
        flags.network_fail_safe_mode = True
        flags.network_recovery_streak = 0
        if not prev_mode:
            notes.append(
                "network fail-safe mode ACTIVATED | new entries disabled until connectivity and latency recover"
            )
    elif flags.network_fail_safe_mode:
        flags.network_recovery_streak += 1
        recovery_target = int(getattr(SETTINGS, "network_recovery_streak", 2))
        if flags.network_recovery_streak >= recovery_target:
            flags.network_fail_safe_mode = False
            flags.network_recovery_streak = 0
            notes.append("network fail-safe mode CLEARED | ws/api health back to normal")
    else:
        flags.network_recovery_streak = 0

    return notes


def required_trade_edge(entry_price: float, secs_left: float | None, history_count: int = 0) -> float:
    required = max(0.0, float(getattr(SETTINGS, "edge_threshold", 0.0)))
    if history_count < 5:
        required *= 0.25
    elif history_count < 20:
        required *= 0.50

    late_threshold = max(
        float(getattr(SETTINGS, "entry_window_min_sec", 120.0)) + 45.0,
        min(float(getattr(SETTINGS, "entry_window_max_sec", 999999.0)) * 0.5, 120.0),
    )
    if secs_left is not None and secs_left < late_threshold:
        required += float(getattr(SETTINGS, "late_entry_edge_penalty", 0.015))

    rich_price_penalty = float(getattr(SETTINGS, "rich_price_edge_penalty", 0.015))
    if entry_price >= 0.60:
        required += rich_price_penalty
    if entry_price >= 0.68:
        required += rich_price_penalty

    center_distance = abs(float(entry_price) - 0.5)
    if center_distance <= float(getattr(SETTINGS, "entry_neutral_band_half_width", 0.0)):
        required += float(getattr(SETTINGS, "entry_neutral_edge_penalty", 0.0))
    if center_distance <= float(getattr(SETTINGS, "entry_micro_band_half_width", 0.0)):
        required += float(getattr(SETTINGS, "entry_micro_edge_penalty", 0.0))

    return required


def price_aware_kelly_fraction(win_rate: float, entry_price: float) -> float:
    if entry_price <= 0.0 or entry_price >= 1.0:
        return 0.0
    raw_fraction = max(0.0, (win_rate - entry_price) / max(1.0 - entry_price, 1e-9))
    return raw_fraction / max(1.0, float(getattr(SETTINGS, "binary_kelly_divisor", 4.0)))


def apply_scoreboard_aux_probability(model_probability: float, scoreboard_win_rate: float) -> float:
    aux_weight = max(0.0, float(getattr(SETTINGS, "scoreboard_aux_weight", 0.0)))
    adjusted = float(model_probability) + ((float(scoreboard_win_rate) - 0.5) * aux_weight)
    return min(0.99, max(0.01, adjusted))


def summarize_entry_edge(*, win_rate: float, entry_price: float, secs_left: float | None, history_count: int = 0) -> dict:
    required = required_trade_edge(entry_price, secs_left, history_count=history_count)
    raw_edge = win_rate - entry_price
    return {
        "win_rate": win_rate,
        "entry_price": entry_price,
        "raw_edge": raw_edge,
        "required_edge": required,
        "ok": raw_edge >= required,
        "history_count": history_count,
    }


def stabilize_entry_win_rate(win_rate: float, decisive_history_count: int) -> float:
    min_decisive = int(getattr(SETTINGS, "scoreboard_entry_gate_min_decisive_trades", 5))
    observed = min(0.99, max(0.01, float(win_rate)))
    if decisive_history_count >= min_decisive:
        return observed
    if min_decisive <= 0:
        return observed
    weight = max(0.0, min(1.0, float(decisive_history_count) / float(min_decisive)))
    return 0.5 + ((observed - 0.5) * weight)


def score_entry_candidate(
    candidate: dict,
    *,
    secs_left: float | None,
    scoreboard=None,
) -> dict:
    side = str(candidate.get("side") or "").strip().upper()
    strategy_name = str(candidate.get("strategy_name") or candidate.get("reason") or "").strip()
    if strategy_name and not strategy_name.startswith("model-"):
        strategy_name = f"model-{strategy_name}"
    entry_price = float(candidate.get("entry_price") or 0.0)
    model_probability = candidate.get("model_probability")
    signal_probability = float(model_probability) if model_probability is not None else None

    strategy_win_rate = 0.5
    strategy_trade_count = 0
    strategy_decisive_trade_count = 0
    if strategy_name:
        try:
            if scoreboard is None:
                from core.learning import SCOREBOARD as scoreboard  # type: ignore
            strategy_win_rate = scoreboard.get_strategy_score(strategy_name)
            strategy_trade_count = scoreboard.get_strategy_trade_count(strategy_name)
            strategy_decisive_trade_count = scoreboard.get_strategy_decisive_trade_count(strategy_name)
        except Exception:
            strategy_win_rate = 0.5
            strategy_trade_count = 0
            strategy_decisive_trade_count = 0

    strategy_win_rate = stabilize_entry_win_rate(strategy_win_rate, strategy_decisive_trade_count)
    effective_probability = strategy_win_rate if signal_probability is None else apply_scoreboard_aux_probability(signal_probability, strategy_win_rate)
    entry_edge = summarize_entry_edge(
        win_rate=effective_probability,
        entry_price=entry_price,
        secs_left=secs_left,
        history_count=strategy_decisive_trade_count,
    )
    return {
        "ok": bool(side in {"UP", "DOWN"} and strategy_name and entry_price > 0.0 and entry_edge["ok"]),
        "side": side,
        "strategy_name": strategy_name,
        "entry_price": entry_price,
        "signal_probability": signal_probability,
        "strategy_win_rate": strategy_win_rate,
        "strategy_trade_count": strategy_trade_count,
        "strategy_decisive_trade_count": strategy_decisive_trade_count,
        "effective_probability": effective_probability,
        "entry_edge": entry_edge,
    }


def select_ranked_entry_candidate(
    model_decision: dict,
    *,
    ws_velocity: float,
    current_ws_velocity: float | None = None,
    secs_left: float | None,
    scoreboard=None,
) -> tuple[dict | None, list[str]]:
    ranked_candidates = model_decision.get("ranked_candidates")
    if not isinstance(ranked_candidates, list) or not ranked_candidates:
        ranked_candidates = [model_decision] if model_decision.get("ok") else []

    rejection_notes: list[str] = []
    eligible_candidates: list[dict] = []
    for idx, candidate in enumerate(ranked_candidates, start=1):
        scored = score_entry_candidate(candidate, secs_left=secs_left, scoreboard=scoreboard)
        if entry_velocity_gate_rejects(
            scored.get("side"),
            scored.get("strategy_name"),
            ws_velocity,
            current_ws_velocity=current_ws_velocity,
        ):
            rejection_notes.append(
                f"rank={idx} strategy={scored.get('strategy_name') or 'unknown'} "
                f"rejected=velocity lag={float(ws_velocity or 0.0):.4%} "
                f"current={float(current_ws_velocity if current_ws_velocity is not None else ws_velocity):.4%}"
            )
            continue
        if not scored.get("ok"):
            rejection_notes.append(
                f"rank={idx} strategy={scored.get('strategy_name') or 'unknown'} "
                f"rejected=edge raw={float(scored['entry_edge']['raw_edge']):.3f} "
                f"required={float(scored['entry_edge']['required_edge']):.3f}"
            )
            continue
        scored["rank"] = idx
        scored["candidate_count"] = len(ranked_candidates)
        eligible_candidates.append(scored)
    if not eligible_candidates:
        return None, rejection_notes

    def _sort_key(scored: dict) -> tuple[float, float, float, float]:
        entry_edge = scored.get("entry_edge") or {}
        return (
            float(entry_edge.get("raw_edge") or 0.0),
            float(scored.get("effective_probability") or 0.0),
            float(scored.get("strategy_win_rate") or 0.0),
            float(scored.get("signal_probability") or 0.0),
        )

    best_candidate = max(eligible_candidates, key=_sort_key)

    if bool(getattr(SETTINGS, "entry_side_conflict_enabled", True)):
        best_by_side: dict[str, dict] = {}
        for scored in eligible_candidates:
            side = str(scored.get("side") or "").strip().upper()
            if side not in {"UP", "DOWN"}:
                continue
            prior = best_by_side.get(side)
            if prior is None or _sort_key(scored) > _sort_key(prior):
                best_by_side[side] = scored
        if len(best_by_side) >= 2:
            up_best = best_by_side.get("UP")
            down_best = best_by_side.get("DOWN")
            if up_best and down_best:
                winner, loser = sorted((up_best, down_best), key=_sort_key, reverse=True)
                raw_gap = float((winner.get("entry_edge") or {}).get("raw_edge") or 0.0) - float((loser.get("entry_edge") or {}).get("raw_edge") or 0.0)
                prob_gap = float(winner.get("effective_probability") or 0.0) - float(loser.get("effective_probability") or 0.0)
                min_edge_gap = max(0.0, float(getattr(SETTINGS, "entry_side_conflict_min_edge_gap", 0.025) or 0.025))
                min_prob_gap = max(0.0, float(getattr(SETTINGS, "entry_side_conflict_min_prob_gap", 0.03) or 0.03))
                if raw_gap < min_edge_gap or prob_gap < min_prob_gap:
                    rejection_notes.append(
                        f"rank={int(winner.get('rank') or 1)} strategy={winner.get('strategy_name') or 'unknown'} "
                        f"rejected=side-conflict other_side={loser.get('side') or 'unknown'} "
                        f"raw_gap={raw_gap:.3f} required_raw_gap={min_edge_gap:.3f} "
                        f"prob_gap={prob_gap:.3f} required_prob_gap={min_prob_gap:.3f}"
                    )
                    return None, rejection_notes
    return best_candidate, rejection_notes


def observed_mark_value(pos: OpenPos, up: float | None, down: float | None) -> float | None:
    mark = up if pos.side == "UP" else down
    if mark is None:
        return None
    return pos.shares * float(mark)


def observed_exit_value_from_mark(*, sold_shares: float, mark: float | None) -> float:
    if mark is None or sold_shares <= 0:
        return 0.0
    return float(sold_shares) * float(mark)


def sanitize_live_actual_exit_value(
    *,
    actual_exit_value_usd: float | None,
    actual_exit_value_source: str,
    sold_shares: float,
    mark: float | None,
    dry_run: bool,
) -> tuple[float | None, str]:
    value = float(actual_exit_value_usd or 0.0)
    source = str(actual_exit_value_source or "")
    if value <= 0.0:
        return None, source
    if dry_run or sold_shares <= LOT_EPS_SHARES or mark is None:
        return value, source
    implied_price = value / max(float(sold_shares), 1e-9)
    if implied_price < -1e-9 or implied_price > 1.0 + 1e-6:
        return None, f"sanity-rejected-{source or 'actual-exit'}"
    if abs(implied_price - float(mark)) > 0.25:
        return None, f"sanity-rejected-{source or 'actual-exit'}"
    return value, source


def update_position_excursions(pos: OpenPos, observed_value: float | None) -> None:
    if observed_value is None:
        return
    now_ts = time.time()
    pnl = observed_value - pos.cost_usd
    if pos.max_favorable_value_usd <= 0:
        pos.max_favorable_value_usd = observed_value
        pos.max_adverse_value_usd = observed_value
        pos.max_favorable_pnl_usd = pnl
        pos.max_adverse_pnl_usd = pnl
        pos.max_favorable_ts = now_ts
        return
    if observed_value > pos.max_favorable_value_usd + 1e-9:
        pos.max_favorable_value_usd = observed_value
        pos.max_favorable_ts = now_ts
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
        chosen_prev_peak = float(chosen.max_favorable_value_usd or 0.0)
        incoming_peak = float(incoming.max_favorable_value_usd or 0.0)
        chosen.max_favorable_value_usd = max(chosen_prev_peak, incoming_peak)
        chosen_peak_ts = float(getattr(chosen, "max_favorable_ts", 0.0) or 0.0)
        incoming_peak_ts = float(getattr(incoming, "max_favorable_ts", 0.0) or 0.0)
        if incoming_peak > chosen_prev_peak + 1e-9:
            chosen.max_favorable_ts = incoming_peak_ts
        elif incoming_peak >= chosen_prev_peak - 1e-9:
            chosen.max_favorable_ts = max(chosen_peak_ts, incoming_peak_ts)
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
    if not open_positions:
        return [], []

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
    base_grace_sec = max(5.0, float(getattr(SETTINGS, "live_position_grace_sec", 90) or 90.0))
    base_miss_limit = max(1, int(getattr(SETTINGS, "live_position_miss_limit", 3) or 3))
    for p in open_positions:
        ap = actual.get(p.token_id)
        if ap is None or ap.size <= 0:
            age_sec = max(0.0, time.time() - float(p.opened_ts or 0.0)) if p.opened_ts else 999999.0
            miss_count = int(getattr(p, "live_miss_count", 0) or 0) + 1  # Only increment when API responded!
            grace_sec = base_grace_sec
            miss_limit = base_miss_limit
            protect_missing_partial = bool(
                getattr(p, "has_scaled_out", False)
                or getattr(p, "has_scaled_out_loss", False)
                or getattr(p, "has_taken_partial", False)
                or getattr(p, "has_extracted_principal", False)
                or getattr(p, "force_close_only", False)
            )
            if getattr(p, "pending_confirmation", False):
                # Freshly filled live orders can take a little longer to show up in the
                # positions API. Give them extra breathing room before treating them as missing.
                grace_sec = max(grace_sec, 30.0)
                miss_limit = max(miss_limit, base_miss_limit + 2)
                in_grace = age_sec <= grace_sec
            elif protect_missing_partial:
                # After a partial exit, prefer a conservative local hold over forgetting
                # the residual lot because the live positions API briefly missed it.
                grace_sec = max(grace_sec, base_grace_sec)
                miss_limit = max(miss_limit, base_miss_limit + 3)
                in_grace = age_sec <= grace_sec
            else:
                in_grace = age_sec <= grace_sec and miss_count <= miss_limit
            if in_grace:
                held = OpenPos(**p.__dict__)
                held.live_miss_count = miss_count
                if getattr(p, "has_scaled_out_loss", False) or getattr(p, "force_close_only", False):
                    held.force_close_only = True
                synced.append(held)
                notes.append(
                    f"sync_hold token={p.token_id} slug={p.slug} reason=missing-live-position age_sec={age_sec:.1f} miss_count={miss_count}"
                )
                continue
            if getattr(p, "has_scaled_out_loss", False) or getattr(p, "force_close_only", False):
                held = OpenPos(**p.__dict__)
                held.live_miss_count = miss_count
                held.force_close_only = True
                synced.append(held)
                notes.append(
                    f"sync_protect token={p.token_id} slug={p.slug} reason=missing-live-position-force-close age_sec={age_sec:.1f} miss_count={miss_count}"
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
            max_favorable_ts=float(getattr(p, "max_favorable_ts", p.opened_ts) or p.opened_ts or 0.0),
            has_scaled_out=getattr(p, "has_scaled_out", False),
            has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
            has_taken_partial=getattr(p, "has_taken_partial", False),
            has_extracted_principal=getattr(p, "has_extracted_principal", False),
            force_close_only=getattr(p, "force_close_only", False),
            runner_peak_value_usd=float(getattr(p, "runner_peak_value_usd", 0.0) or 0.0),
            runner_peak_ts=float(getattr(p, "runner_peak_ts", 0.0) or 0.0),
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
            max_favorable_ts=float(lot.get("max_favorable_ts", opened_ts) or opened_ts or 0.0),
        ))
    return positions, notes_out


def load_runtime_flags(state: dict, open_positions: list[OpenPos]) -> RuntimeFlags:
    live_consec_losses = int(state.get("live_consec_losses", 0))
    last_loss_side = state.get("last_loss_side", "")
    close_fail_streak = int(state.get("close_fail_streak", 0))
    panic_exit_mode = bool(state.get("panic_exit_mode", False))
    network_fail_safe_mode = bool(state.get("network_fail_safe_mode", False))
    api_fail_streak = int(state.get("api_fail_streak", 0))
    slow_api_streak = int(state.get("slow_api_streak", 0))
    ws_stale_streak = int(state.get("ws_stale_streak", 0))
    network_recovery_streak = int(state.get("network_recovery_streak", 0))
    last_api_latency_ms = float(state.get("last_api_latency_ms", 0.0))

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
        network_fail_safe_mode=network_fail_safe_mode,
        api_fail_streak=api_fail_streak,
        slow_api_streak=slow_api_streak,
        ws_stale_streak=ws_stale_streak,
        network_recovery_streak=network_recovery_streak,
        last_api_latency_ms=last_api_latency_ms,
    )


def refresh_runtime_flags(flags: RuntimeFlags, open_positions: list[OpenPos], panic_market_slug: str) -> RuntimeFlags:
    return load_runtime_flags({
        "live_consec_losses": flags.live_consec_losses,
        "last_loss_side": flags.last_loss_side,
        "close_fail_streak": flags.close_fail_streak,
        "panic_exit_mode": flags.panic_exit_mode,
        "network_fail_safe_mode": flags.network_fail_safe_mode,
        "api_fail_streak": flags.api_fail_streak,
        "slow_api_streak": flags.slow_api_streak,
        "ws_stale_streak": flags.ws_stale_streak,
        "network_recovery_streak": flags.network_recovery_streak,
        "last_api_latency_ms": flags.last_api_latency_ms,
        "panic_market_slug": panic_market_slug,
    }, open_positions)


def clear_expired_market_state(
    current_market_slug: str,
    open_positions: list[OpenPos],
    pending_orders: list[PendingOrder],
    *,
    cancel_order=None,
) -> tuple[list[OpenPos], list[PendingOrder], list[str], list[dict[str, object]]]:
    kept_positions: list[OpenPos] = []
    kept_pending: list[PendingOrder] = []
    notes: list[str] = []
    unresolved_events: list[dict[str, object]] = []

    for pos in open_positions:
        if pos.slug and pos.slug != current_market_slug:
            unresolved = bool(
                float(getattr(pos, "shares", 0.0) or 0.0) > LOT_EPS_SHARES
                and (
                    getattr(pos, "force_close_only", False)
                    or getattr(pos, "has_scaled_out_loss", False)
                    or getattr(pos, "pending_confirmation", False)
                )
            )
            if unresolved:
                notes.append(
                    f"expired unresolved live runtime position | slug={pos.slug} side={pos.side} "
                    f"token={pos.token_id} remaining_shares={pos.shares:.6f} remaining_cost={pos.cost_usd:.6f} "
                    f"force_close_only={getattr(pos, 'force_close_only', False)}"
                )
                unresolved_events.append({
                    "kind": "runtime_cleanup",
                    "status": "expired-unresolved-position",
                    "slug": pos.slug,
                    "side": pos.side,
                    "token_id": pos.token_id,
                    "position_id": pos.position_id,
                    "remaining_shares": pos.shares,
                    "remaining_cost_usd": pos.cost_usd,
                    "force_close_only": bool(getattr(pos, "force_close_only", False)),
                    "has_scaled_out_loss": bool(getattr(pos, "has_scaled_out_loss", False)),
                    "pending_confirmation": bool(getattr(pos, "pending_confirmation", False)),
                })
            else:
                notes.append(
                    f"clear expired live runtime position | slug={pos.slug} side={pos.side} token={pos.token_id}"
                )
            continue
        kept_positions.append(pos)

    for po in pending_orders:
        if po.slug and po.slug != current_market_slug:
            if cancel_order and po.order_id:
                try:
                    cancel_order(po.order_id)
                    notes.append(
                        f"cancel expired live pending order | slug={po.slug} side={po.side} order_id={po.order_id}"
                    )
                except Exception as exc:
                    notes.append(
                        f"drop expired live pending order | slug={po.slug} side={po.side} order_id={po.order_id} cancel_error={exc}"
                    )
            else:
                notes.append(
                    f"drop expired live pending order | slug={po.slug} side={po.side} order_id={po.order_id or 'n/a'}"
                )
            continue
        kept_pending.append(po)

    return kept_positions, kept_pending, notes, unresolved_events


def save_runtime_state(
    risk: RiskState,
    *,
    last_market_slug: str,
    same_market_reentry_block_slug: str,
    yes_price_window: deque,
    up_price_window: deque,
    down_price_window: deque,
    last_trade_ts: float,
    prev_up,
    prev_down,
    error_cooldown_until: float,
    open_positions: list[OpenPos],
    pending_orders: list[PendingOrder],
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
        "same_market_reentry_block_slug": same_market_reentry_block_slug,
        "yes_price_window": list(yes_price_window),
        "up_price_window": list(up_price_window),
        "down_price_window": list(down_price_window),
        "last_trade_ts": last_trade_ts,
        "prev_up": prev_up,
        "prev_down": prev_down,
        "error_cooldown_until": error_cooldown_until,
        "open_positions": [p.__dict__ for p in sanitized_positions],
        "pending_orders": [po.__dict__ for po in pending_orders],
        "live_consec_losses": flags.live_consec_losses,
        "last_loss_side": flags.last_loss_side,
        "close_fail_streak": flags.close_fail_streak,
        "panic_exit_mode": flags.panic_exit_mode,
        "network_fail_safe_mode": flags.network_fail_safe_mode,
        "api_fail_streak": flags.api_fail_streak,
        "slow_api_streak": flags.slow_api_streak,
        "ws_stale_streak": flags.ws_stale_streak,
        "network_recovery_streak": flags.network_recovery_streak,
        "last_api_latency_ms": flags.last_api_latency_ms,
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

    if getattr(SETTINGS, "dry_run", False) and ex.reconcile_dry_run_positions(sanitized_positions):
        notes.append(
            f"reconciled dry-run paper balance to startup positions | kept_positions={len(sanitized_positions)}"
        )

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
    set_journal_context(run_id=run_journal.run_id)
    install_signal_handlers(run_journal)

    log(f"bot started | dry_run={SETTINGS.dry_run}")

    risk.daily_pnl = float(state.get("risk_daily_pnl", 0.0))
    risk.orders_this_window = int(state.get("risk_orders_this_window", 0))
    risk.window_key = state.get("risk_window_key", "")
    risk.consec_losses = int(state.get("risk_consec_losses", 0))

    last_market_slug = state.get("last_market_slug", "")
    same_market_reentry_block_slug = str(state.get("same_market_reentry_block_slug") or "")
    yes_price_window: deque = deque(state.get("yes_price_window", []), maxlen=max(5, SETTINGS.zscore_window))
    up_price_window: deque = deque(state.get("up_price_window", []), maxlen=max(5, SETTINGS.momentum_ticks + 2))
    down_price_window: deque = deque(state.get("down_price_window", []), maxlen=max(5, SETTINGS.momentum_ticks + 2))
    last_trade_ts = float(state.get("last_trade_ts", time.time()))
    prev_up = state.get("prev_up")
    prev_down = state.get("prev_down")
    error_cooldown_until = float(state.get("error_cooldown_until", 0.0))
    pending_orders = [PendingOrder(**dict(p)) for p in state.get("pending_orders", []) if isinstance(p, dict)]
    flags = load_runtime_flags(state, open_positions)
    panic_market_slug = str(state.get("panic_market_slug") or "")

    if runtime_state_changed:
        save_runtime_state(
            risk,
            last_market_slug=last_market_slug,
            same_market_reentry_block_slug=same_market_reentry_block_slug,
            yes_price_window=yes_price_window,
            up_price_window=up_price_window,
            down_price_window=down_price_window,
            last_trade_ts=last_trade_ts,
            prev_up=prev_up,
            prev_down=prev_down,
            error_cooldown_until=error_cooldown_until,
            open_positions=open_positions,
            pending_orders=pending_orders,
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
            cycle_interval = next_cycle_interval_seconds(
                has_pending_orders=bool(pending_orders),
                has_open_positions=bool(open_positions),
            )
            if time_since_last_query < cycle_interval:
                time.sleep(cycle_interval - time_since_last_query)
            last_rest_query_ts = time.time()
            flags.last_api_latency_ms = 0.0
            cycle_had_slow_api = False
            cycle_ws_age = current_ws_age()

            now = datetime.now()
            key = current_5min_key(now)
            update_window(risk, key)

            try:
                acct, acct_ms = timed_call(ex.get_account)
                cycle_had_slow_api = observe_api_latency(flags, "get_account", acct_ms) or cycle_had_slow_api
                synced_result, sync_ms = timed_call(sync_open_positions, ex, open_positions)
                cycle_had_slow_api = observe_api_latency(flags, "sync_open_positions", sync_ms) or cycle_had_slow_api
                open_positions, sync_notes = synced_result
                for note in sync_notes:
                    log(note)
                if getattr(SETTINGS, "dry_run", False) and ex.reconcile_dry_run_positions(open_positions):
                    log("dry-run reconcile: aligned internal exposure with runtime open positions")
                    acct = ex.get_account()
            except Exception as sync_err:
                network_notes = update_network_guard(
                    flags,
                    ws_age=current_ws_age(),
                    cycle_had_slow_api=cycle_had_slow_api,
                    cycle_api_error=True,
                )
                for note in network_notes:
                    log(note)
                log(f"API sync error (account/positions): {sync_err}")
                save_runtime_state(
                    risk,
                    last_market_slug=last_market_slug,
                    same_market_reentry_block_slug=same_market_reentry_block_slug,
                    yes_price_window=yes_price_window,
                    up_price_window=up_price_window,
                    down_price_window=down_price_window,
                    last_trade_ts=last_trade_ts,
                    prev_up=prev_up,
                    prev_down=prev_down,
                    error_cooldown_until=error_cooldown_until,
                    open_positions=open_positions,
                    pending_orders=pending_orders,
                    flags=flags,
                    last_cycle_label=state.get("last_cycle_label", ""),
                    panic_market_slug=panic_market_slug,
                )
                smart_sleep(SETTINGS.poll_seconds)
                continue
            flags = refresh_runtime_flags(flags, open_positions, panic_market_slug)

            # --- PENDING ORDERS / KILL-SWITCH ---
            if pending_orders:
                try:
                    open_clob_orders, open_orders_ms = timed_call(ex.get_open_orders)
                    cycle_had_slow_api = observe_api_latency(flags, "get_open_orders", open_orders_ms) or cycle_had_slow_api
                    open_order_ids = {o.get("orderID") for o in open_clob_orders} if isinstance(open_clob_orders, list) else set()
                    live_positions_snapshot, pending_pos_ms = timed_call(ex.get_positions)
                    cycle_had_slow_api = observe_api_latency(flags, "get_positions_pending_orders", pending_pos_ms) or cycle_had_slow_api
                    live_positions_by_token = {
                        p.token_id: p for p in (live_positions_snapshot or []) if float(getattr(p, "size", 0.0) or 0.0) > LOT_EPS_SHARES
                    }
                    
                    ws_vel = 0.0
                    try:
                        ws_vel = BINANCE_WS.get_price_velocity(
                            3.0,
                            lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0)),
                        )
                    except Exception:
                        pass
                        
                    for po in list(pending_orders):
                        live_pos = live_positions_by_token.get(po.token_id)
                        has_live_position = live_pos is not None
                        order_still_open = bool(po.order_id) and po.order_id in open_order_ids
                        if order_still_open:
                            po.disappeared_since_ts = 0.0
                            po.cancel_requested = False
                        action = decide_pending_order_action(
                            order_still_open=order_still_open,
                            age_sec=time.time() - po.placed_ts,
                            side=po.side,
                            ws_vel=ws_vel,
                            cancel_velocity=float(getattr(SETTINGS, "cancel_on_reversal_velocity", 0.0)),
                            timeout_sec=float(getattr(SETTINGS, "maker_order_timeout_sec", 15)),
                            has_live_position=has_live_position,
                            fallback_enabled=bool(getattr(SETTINGS, "maker_timeout_fallback_taker", True)),
                            fallback_attempted=bool(getattr(po, "fallback_attempted", False)),
                        )

                        if action == "filled":
                            shares = float(live_pos.size) if live_pos is not None else 0.0
                            cost_usd = float(live_pos.initial_value) if live_pos is not None and float(live_pos.initial_value) > 0 else po.order_usd
                            track_pending_fill(
                                open_positions,
                                po,
                                shares=shares,
                                cost_usd=cost_usd,
                                entry_reason=po.entry_reason,
                                source="maker-fill-confirmed",
                            )
                            log(f"Pending order {po.order_id or 'n/a'} confirmed filled on CLOB/runtime state.")
                            pending_orders.remove(po)
                            continue

                        if action == "gone":
                            if po.disappeared_since_ts <= 0.0:
                                po.disappeared_since_ts = time.time()
                                cancel_ok = False
                                if po.order_id and not po.cancel_requested:
                                    cancel_ok = ex.cancel_order(po.order_id)
                                    po.cancel_requested = True
                                log(
                                    f"Pending order {po.order_id or 'n/a'} disappeared from open-orders view with no live position; "
                                    f"keeping it blocked until fill/cancel/market switch (cancel_requested={po.cancel_requested} cancel_ok={cancel_ok})"
                                )
                            continue

                        if action == "cancel-reversal":
                            log(f"KILL-SWITCH TRIGGERED on {po.side} {po.order_id} (velocity: {ws_vel:.4f})")
                            ex.cancel_order(po.order_id)
                            if live_pos is not None:
                                shares = float(live_pos.size)
                                cost_usd = float(live_pos.initial_value) if float(live_pos.initial_value) > 0 else po.order_usd
                                track_pending_fill(
                                    open_positions,
                                    po,
                                    shares=shares,
                                    cost_usd=cost_usd,
                                    entry_reason=po.entry_reason,
                                    source="maker-cancel-reconciled",
                                )
                            pending_orders.remove(po)
                            continue

                        if action == "fallback-taker":
                            log(f"MAKER TIMEOUT on {po.side} {po.order_id} -> attempting taker fallback")
                            ex.cancel_order(po.order_id)
                            po.fallback_attempted = True
                            try:
                                fallback_resp, fallback_latencies, fallback_attempts = place_entry_order_with_retry(
                                    ex,
                                    po.side,
                                    po.order_usd,
                                    po.token_id,
                                    simulated_price=None,
                                    force_taker=True,
                                    max_attempts=int(getattr(SETTINGS, "entry_retry_attempts", 3)),
                                    backoff_sec=float(getattr(SETTINGS, "entry_retry_backoff_sec", 2.0)),
                                )
                                for idx, latency_ms in enumerate(fallback_latencies, start=1):
                                    cycle_had_slow_api = observe_api_latency(
                                        flags,
                                        f"place_order_timeout_fallback#{idx}",
                                        latency_ms,
                                    ) or cycle_had_slow_api
                                shares, _ = extract_entry_response_details(fallback_resp)
                                if shares > 0:
                                    track_pending_fill(
                                        open_positions,
                                        po,
                                        shares=shares,
                                        cost_usd=po.order_usd,
                                        entry_reason=f"{(po.entry_reason or 'maker-timeout')}+taker-fallback",
                                        source="maker-timeout-fallback",
                                        execution_style=(
                                            fallback_resp.get("execution_style")
                                            if isinstance(fallback_resp, dict)
                                            else "taker"
                                        ),
                                    )
                                    maybe_record_cycle_label(state, "maker-timeout-fallback-filled", slug=po.slug, side=po.side)
                                    log(
                                        f"maker timeout fallback filled | side={po.side} token={po.token_id} "
                                        f"attempts={fallback_attempts} shares={shares:.4f}"
                                    )
                                else:
                                    maybe_record_cycle_label(state, "signal-but-no-fill", slug=po.slug, side=po.side, reason="maker-timeout-fallback-no-fill")
                                    append_event({
                                        "kind": "entry_attempt",
                                        "slug": po.slug,
                                        "side": po.side,
                                        "token_id": po.token_id,
                                        "status": "signal-but-no-fill",
                                        "reason": "maker-timeout-fallback-no-fill",
                                        "response_mode": fallback_resp.get("mode") if isinstance(fallback_resp, dict) else "",
                                    })
                                    log(f"maker timeout fallback returned no fill | side={po.side} token={po.token_id}")
                            except Exception as fallback_err:
                                network_notes = update_network_guard(
                                    flags,
                                    ws_age=current_ws_age(),
                                    cycle_had_slow_api=cycle_had_slow_api,
                                    cycle_api_error=True,
                                )
                                for note in network_notes:
                                    log(note)
                                error_cooldown_until = time.time() + 20
                                log(f"maker timeout fallback failed: {fallback_err}")
                            pending_orders.remove(po)
                            continue

                        if action == "cancel-timeout":
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
            entry_price = None
            signal_probability = None
            strategy_win_rate = 0.5
            strategy_trade_count = 0
            strategy_decisive_trade_count = 0
            entry_edge = None
            effective_probability = None

            # The daily loss circuit breaker is handled properly in `can_place_order`
            # Removing the unconditional continue so open positions are still managed.

            if SETTINGS.auto_market_selection:
                try:
                    previous_market_slug = last_market_slug
                    previous_up = prev_up
                    previous_down = prev_down
                    market, resolve_ms = timed_call(resolve_latest_btc_5m_token_ids)
                    cycle_had_slow_api = observe_api_latency(flags, "resolve_latest_btc_5m_token_ids", resolve_ms) or cycle_had_slow_api
                    if market["slug"] != last_market_slug:
                        # Clear price history to prevent artificial momentum / mean-reversion signals
                        if last_market_slug != "None":
                            yes_price_window.clear()
                            up_price_window.clear()
                            down_price_window.clear()
                        same_market_reentry_block_slug = ""
                        last_market_slug = market["slug"]
                        log(f"market switched => {market['slug']}")
                        
                    if getattr(SETTINGS, "dry_run", False):
                        ghosts = [p for p in open_positions if p.slug != market["slug"]]
                        for gp in ghosts:
                            last_mark = None
                            if gp.slug == (previous_market_slug or ""):
                                last_mark = previous_up if gp.side == "UP" else previous_down
                            settlement_price, resolution_note = paper_settlement_from_last_mark(last_mark)
                            close_resp = ex.close_position(gp.token_id, gp.shares, simulated_price=settlement_price)
                            resolution_value = float(close_resp.get("actual_exit_value_usd", gp.shares * settlement_price) or 0.0)
                            realized_pnl = resolution_value - gp.cost_usd
                            risk.daily_pnl += realized_pnl
                            mark_text = f"{last_mark:.3f}" if last_mark is not None else "n/a"
                            log(f"Force-clearing stale dry-run position from expired market: {gp.slug} | {resolution_note} mark={mark_text} value=${resolution_value:.4f} pnl={realized_pnl:+.4f}")
                            append_event({
                                "kind": "exit",
                                "slug": gp.slug,
                                "side": gp.side,
                                "token_id": gp.token_id,
                                "position_id": gp.position_id,
                                "closed_shares": float(close_resp.get("closed_shares", gp.shares) or gp.shares),
                                "remaining_shares": 0.0,
                                "realized_cost_usd": gp.cost_usd,
                                "actual_exit_value_usd": resolution_value,
                                "actual_exit_value_source": close_resp.get("actual_exit_value_source") or "paper_trade_settlement",
                                "observed_exit_value_usd": resolution_value,
                                "observed_exit_value_source": "expiry-settlement",
                                "actual_realized_pnl_usd": realized_pnl,
                                "exit_execution_style": "expiry-settlement",
                                "status": "closed",
                                "reason": f"dry-run-market-expired-{resolution_note}",
                            })
                            open_positions.remove(gp)
                        if ghosts:
                            acct = ex.get_account()
                    else:
                        open_positions, pending_orders, live_cleanup_notes, expired_unresolved_events = clear_expired_market_state(
                            market["slug"],
                            open_positions,
                            pending_orders,
                            cancel_order=ex.cancel_order,
                        )
                        for note in live_cleanup_notes:
                            log(note)
                        for cleanup_event in expired_unresolved_events:
                            append_event(cleanup_event)

                            
                    token_up = market.get("token_up", "")
                    token_down = market.get("token_down", "")
                    poly_ob_up, ob_up_ms = timed_call(ex.get_full_orderbook, token_up)
                    cycle_had_slow_api = observe_api_latency(flags, "get_full_orderbook_up", ob_up_ms) or cycle_had_slow_api
                    poly_ob_down, ob_down_ms = timed_call(ex.get_full_orderbook, token_down)
                    cycle_had_slow_api = observe_api_latency(flags, "get_full_orderbook_down", ob_down_ms) or cycle_had_slow_api

                    secs_left = seconds_to_market_end(market)
                    cycle_ws_age = current_ws_age()

                    # Use LIVE CLOB mid-prices instead of stale Gamma API outcomePrices
                    up = None
                    if poly_ob_up and poly_ob_up.get("best_ask", 0) <= 1.0:
                        up_bid = poly_ob_up.get("best_bid", 0.0)
                        up_ask = poly_ob_up.get("best_ask", 1.0)
                        up = round((up_bid + up_ask) / 2.0, 3)

                    down = None
                    if poly_ob_down and poly_ob_down.get("best_ask", 0) <= 1.0:
                        down_bid = poly_ob_down.get("best_bid", 0.0)
                        down_ask = poly_ob_down.get("best_ask", 1.0)
                        down = round((down_bid + down_ask) / 2.0, 3)

                    # Fallback to Gamma API if live CLOB is totally broken
                    if up is None or down is None:
                        prices = get_outcome_prices(market)
                        up = up or prices.get("up") or prices.get("漲")
                        down = down or prices.get("down") or prices.get("跌")

                    if up is not None:
                        up_price_window.append(float(up))
                        yes_price_window.append(float(up))
                    if down is not None:
                        down_price_window.append(float(down))

                    binance_1m = ex.get_binance_1m_candle() if SETTINGS.use_cex_oracle else None
                    binance_5m = ex.get_binance_5m_klines(100)

                    try:
                        ws_bba = BINANCE_WS.get_bba(
                            lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0))
                        )
                        ws_trades = BINANCE_WS.get_recent_trades(
                            seconds=60.0,
                            lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0)),
                        )
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
                            SETTINGS.stop_loss_pct = _f("STOP_LOSS_PCT", 0.15)
                            SETTINGS.zscore_threshold = _f("ZSCORE_THRESHOLD", 2.0)

                    arbitrage_triggered = False
                    from core.decision_engine import check_arbitrage
                    if check_arbitrage(up, down):
                        log(f"ARBITRAGE DETECTED! up={up} down={down} sum={up+down}")
                        res_up = ex.place_order("UP", 1.0, token_up, simulated_price=float(up) if up is not None else None)
                        res_down = ex.place_order("DOWN", 1.0, token_down, simulated_price=float(down) if down is not None else None)
                        log(f"Arbitrage execution: UP={res_up} DOWN={res_down}")
                        maybe_record_cycle_label(state, "arbitrage-execution", slug=market["slug"], up=up, down=down)
                        arbitrage_triggered = True

                    if not arbitrage_triggered:
                        model_decision = explain_choose_side(
                            market, yes_price_window, up_price_window, down_price_window,
                            observed_up=up, observed_down=down,
                            binance_1m=binance_1m, binance_5m=binance_5m,
                            ws_bba=ws_bba, ws_trades=ws_trades,
                            poly_ob_up=poly_ob_up, poly_ob_down=poly_ob_down
                        )
                        no_entry_reason = model_decision.get("reason")
                        _entry_ws_vel = 0.0
                        _entry_ws_vel_now = 0.0
                        try:
                            _entry_ws_vel = BINANCE_WS.get_price_velocity(
                                3.0,
                                lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0)),
                            )
                            _entry_ws_vel_now = BINANCE_WS.get_price_velocity(3.0, lag_sec=0.0)
                        except Exception:
                            p.binance_adverse_breach_ts = 0.0
                            pass

                        chosen_candidate, candidate_rejections = select_ranked_entry_candidate(
                            model_decision,
                            ws_velocity=_entry_ws_vel,
                            current_ws_velocity=_entry_ws_vel_now,
                            secs_left=secs_left,
                        )
                        if chosen_candidate:
                            signal_side = chosen_candidate.get("side")
                            signal_origin = chosen_candidate.get("strategy_name") or ""
                            signal_probability = chosen_candidate.get("signal_probability")
                            entry_price = chosen_candidate.get("entry_price")
                            strategy_win_rate = float(chosen_candidate.get("strategy_win_rate") or 0.5)
                            strategy_trade_count = int(chosen_candidate.get("strategy_trade_count") or 0)
                            strategy_decisive_trade_count = int(chosen_candidate.get("strategy_decisive_trade_count") or 0)
                            effective_probability = float(chosen_candidate.get("effective_probability") or strategy_win_rate)
                            entry_edge = chosen_candidate.get("entry_edge")
                            rank = int(chosen_candidate.get("rank") or 1)
                            candidate_count = int(chosen_candidate.get("candidate_count") or 1)
                            if rank > 1:
                                log(
                                    f"candidate fallback | picked rank={rank}/{candidate_count} "
                                    f"strategy={signal_origin} side={signal_side}"
                                )
                        else:
                            signal_side = None
                            signal_origin = ""
                            signal_probability = None
                            if candidate_rejections:
                                no_entry_reason = candidate_rejections[0]

                        if (
                            signal_side is None
                            and bool(getattr(SETTINGS, "enable_dump_trigger", False))
                            and secs_left is not None
                            and 90 <= secs_left <= 240
                        ):
                            dumped_side = should_trigger_dump(prev_up, prev_down, up, down, SETTINGS.dump_move_threshold)
                            if dumped_side:
                                signal_side = dumped_side
                                signal_origin = "dump-trigger"
                                no_entry_reason = ""
                                signal_probability = None
                                strategy_win_rate = 0.5
                                strategy_trade_count = 0
                                strategy_decisive_trade_count = 0
                                effective_probability = None
                                entry_edge = None
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
                        executable_exit_value = realistic_exit_value(p, up, down, poly_ob_up, poly_ob_down)
                        mark_value = observed_mark_value(p, up, down)
                        if executable_exit_value is None and mark_value is None:
                            keep_positions.append(p)
                            continue
                        effective_exit_value = conservative_exit_decision_value(
                            p,
                            executable_exit_value=executable_exit_value,
                            mark_value=mark_value,
                        )
                        hard_stop_value = float(effective_exit_value or 0.0)
                        profit_reference_value = executable_take_profit_value(
                            p,
                            poly_ob_up,
                            poly_ob_down,
                        )
                        effective_exit_value = float(effective_exit_value or 0.0)
                        update_position_excursions(p, effective_exit_value)
                        pnl_pct = (effective_exit_value - p.cost_usd) / max(p.cost_usd, 1e-9)
                        hard_stop_pnl_pct = (hard_stop_value - p.cost_usd) / max(p.cost_usd, 1e-9)
                        profit_pnl_pct = (
                            (float(profit_reference_value) - p.cost_usd) / max(p.cost_usd, 1e-9)
                            if profit_reference_value is not None
                            else None
                        )
                        plateau_ref_pct = profit_pnl_pct if profit_pnl_pct is not None else pnl_pct
                        plateau_min = float(getattr(SETTINGS, "binance_profit_protect_min_profit_pct", 0.06) or 0.06)
                        plateau_max = min(
                            float(getattr(SETTINGS, "binance_profit_protect_max_profit_pct", 0.18) or 0.18),
                            max(0.0, float(getattr(SETTINGS, "take_profit_soft_pct", 0.18) or 0.18) - 0.01)
                        )
                        if plateau_ref_pct is not None and plateau_min <= plateau_ref_pct <= plateau_max:
                            if getattr(p, "profit_plateau_entry_ts", 0.0) <= 0.0:
                                p.profit_plateau_entry_ts = time.time()
                        elif plateau_ref_pct is None or not (plateau_min - 0.02 <= plateau_ref_pct <= plateau_max + 0.02):
                            p.profit_plateau_entry_ts = 0.0
                        
                        profit_peak_age_sec = max(0.0, time.time() - getattr(p, "profit_plateau_entry_ts", 0.0)) if getattr(p, "profit_plateau_entry_ts", 0.0) > 0.0 else 0.0
                        stop_loss_partial_pct = abs(float(getattr(SETTINGS, "stop_loss_partial_pct", 0.05) or 0.05))
                        stop_loss_pct = abs(float(getattr(SETTINGS, "stop_loss_pct", 0.15) or 0.15))
                        if hard_stop_pnl_pct <= -stop_loss_partial_pct and hard_stop_pnl_pct > -stop_loss_pct:
                            if getattr(p, "soft_stop_breach_ts", 0.0) <= 0.0:
                                p.soft_stop_breach_ts = time.time()
                        else:
                            p.soft_stop_breach_ts = 0.0
                        mfe_pnl_pct = p.max_favorable_pnl_usd / max(p.cost_usd, 1e-9)
                        runner_drawdown_pct = 0.0
                        runner_peak_age_sec = None
                        if getattr(p, "has_extracted_principal", False):
                            runner_drawdown_pct, runner_peak_age_sec = update_runner_peak(
                                p,
                                effective_exit_value,
                            )
                        hold_sec = time.time() - p.opened_ts
                        recovery_chance_low = False
                        if getattr(SETTINGS, "smart_stop_loss_enabled", False) and hard_stop_pnl_pct < -0.10:
                            if signal_side and signal_side != p.side:
                                recovery_chance_low = True
                            elif hold_sec >= 90.0 and (secs_left or 1000.0) <= 60.0:
                                recovery_chance_low = True

                        if getattr(p, "force_close_only", False):
                            exit_decision = ExitDecision(True, "residual-force-close", hard_stop_pnl_pct, hold_sec)
                        elif getattr(p, "is_moonbag", False):
                            keep_positions.append(p)
                            continue
                        else:
                            exit_decision = decide_exit(
                                pnl_pct=hard_stop_pnl_pct,
                                profit_pnl_pct=profit_pnl_pct,
                                hold_sec=hold_sec,
                                secs_left=secs_left,
                                has_scaled_out=getattr(p, "has_scaled_out", False),
                                recovery_chance_low=recovery_chance_low,
                                has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
                                has_taken_partial=getattr(p, "has_taken_partial", False),
                                has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                mfe_pnl_pct=mfe_pnl_pct,
                                runner_drawdown_pct=runner_drawdown_pct,
                                runner_peak_age_sec=runner_peak_age_sec,
                                runner_peak_value_usd=float(getattr(p, "runner_peak_value_usd", 0.0) or 0.0),
                            )

                        maybe_log_position_watch(
                            p,
                            pnl_pct=pnl_pct,
                            hard_stop_pnl_pct=hard_stop_pnl_pct,
                            profit_pnl_pct=profit_pnl_pct,
                            hold_sec=hold_sec,
                            secs_left=secs_left,
                            mark=mark,
                            observed_value=effective_exit_value,
                            profit_reference_value=profit_reference_value,
                            exit_decision=exit_decision,
                        )

                        # --- Phase 2: Advanced Loophole Exploitation ---
                        try:
                            ws_vel = BINANCE_WS.get_price_velocity(
                                3.0,
                                lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0)),
                            )
                            ws_vel_now = BINANCE_WS.get_price_velocity(3.0, lag_sec=0.0)
                            
                            # 1. Panic Dump Override
                            is_panic = (p.side == "UP" and ws_vel < -SETTINGS.panic_dump_velocity) or \
                                       (p.side == "DOWN" and ws_vel > SETTINGS.panic_dump_velocity)
                            if is_panic and hold_sec > 2.0:
                                log(f"🚨 PANIC DUMP OVERRIDE! {p.side} {p.token_id[-6:]} Binance vel={ws_vel:.4%}")
                                exit_decision.should_close = True
                                exit_decision.reason = "panic-dump"
                                p.has_panic_dumped = True

                            soft_stop_breach_age_sec = 0.0
                            if getattr(p, "soft_stop_breach_ts", 0.0) > 0.0:
                                soft_stop_breach_age_sec = max(0.0, time.time() - float(p.soft_stop_breach_ts))
                            if should_delay_soft_stop_scaleout(
                                reason=exit_decision.reason,
                                side=p.side,
                                pnl_pct=hard_stop_pnl_pct,
                                breach_age_sec=soft_stop_breach_age_sec,
                                secs_left=secs_left,
                                ws_velocity=ws_vel,
                            ):
                                log(
                                    f"SOFT STOP CONFIRMING: {p.side} {p.token_id[-6:]} "
                                    f"pnl={hard_stop_pnl_pct:.2%} breach_age={soft_stop_breach_age_sec:.1f}s "
                                    f"Binance vel={ws_vel:.4%}"
                                )
                                exit_decision.should_close = False
                                exit_decision.reason = ""

                            binance_adverse_breach_age_sec = 0.0
                            if should_trigger_binance_adverse_exit(
                                has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                side=p.side,
                                pnl_pct=hard_stop_pnl_pct,
                                profit_pnl_pct=profit_pnl_pct,
                                hold_sec=hold_sec,
                                breach_age_sec=float("inf"),
                                secs_left=secs_left,
                                ws_velocity=ws_vel,
                                current_ws_velocity=ws_vel_now,
                            ):
                                if getattr(p, "binance_adverse_breach_ts", 0.0) <= 0.0:
                                    p.binance_adverse_breach_ts = time.time()
                                binance_adverse_breach_age_sec = max(
                                    0.0,
                                    time.time() - float(getattr(p, "binance_adverse_breach_ts", 0.0) or 0.0),
                                )
                            else:
                                p.binance_adverse_breach_ts = 0.0

                            if should_trigger_binance_adverse_exit(
                                has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                side=p.side,
                                pnl_pct=hard_stop_pnl_pct,
                                profit_pnl_pct=profit_pnl_pct,
                                hold_sec=hold_sec,
                                breach_age_sec=binance_adverse_breach_age_sec,
                                secs_left=secs_left,
                                ws_velocity=ws_vel,
                                current_ws_velocity=ws_vel_now,
                            ):
                                safe_profit_text = "n/a" if profit_pnl_pct is None else f"{float(profit_pnl_pct):.2%}"
                                log(
                                    f"BINANCE ADVERSE EXIT: {p.side} {p.token_id[-6:]} "
                                    f"hold={hold_sec:.0f}s pnl={hard_stop_pnl_pct:.2%} profit={safe_profit_text} "
                                    f"breach_age={binance_adverse_breach_age_sec:.1f}s lag_vel={ws_vel:.4%} current_vel={ws_vel_now:.4%}"
                                )
                                exit_decision.should_close = True
                                exit_decision.reason = "binance-adverse-exit"

                            profit_protect_breach_age_sec = 0.0
                            if should_trigger_binance_profit_protect_exit(
                                has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                side=p.side,
                                profit_pnl_pct=profit_pnl_pct,
                                take_profit_soft_pct=float(getattr(SETTINGS, "take_profit_soft_pct", 0.35) or 0.35),
                                hold_sec=hold_sec,
                                peak_age_sec=profit_peak_age_sec,
                                breach_age_sec=float("inf"),
                                secs_left=secs_left,
                                ws_velocity=ws_vel,
                                current_ws_velocity=ws_vel_now,
                            ):
                                if getattr(p, "binance_profit_protect_breach_ts", 0.0) <= 0.0:
                                    p.binance_profit_protect_breach_ts = time.time()
                                profit_protect_breach_age_sec = max(
                                    0.0,
                                    time.time() - float(getattr(p, "binance_profit_protect_breach_ts", 0.0) or 0.0),
                                )
                            else:
                                p.binance_profit_protect_breach_ts = 0.0

                            if should_trigger_binance_profit_protect_exit(
                                has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                side=p.side,
                                profit_pnl_pct=profit_pnl_pct,
                                take_profit_soft_pct=float(getattr(SETTINGS, "take_profit_soft_pct", 0.35) or 0.35),
                                hold_sec=hold_sec,
                                peak_age_sec=profit_peak_age_sec,
                                breach_age_sec=profit_protect_breach_age_sec,
                                secs_left=secs_left,
                                ws_velocity=ws_vel,
                                current_ws_velocity=ws_vel_now,
                            ):
                                log(
                                    f"BINANCE PROFIT PROTECT EXIT: {p.side} {p.token_id[-6:]} "
                                    f"profit={float(profit_pnl_pct or 0.0):.2%} peak_age={float(profit_peak_age_sec or 0.0):.1f}s "
                                    f"breach_age={profit_protect_breach_age_sec:.1f}s lag_vel={ws_vel:.4%} current_vel={ws_vel_now:.4%}"
                                )
                                exit_decision.should_close = True
                                exit_decision.reason = "binance-profit-protect-exit"

                            if should_trigger_profit_reversal_exit(
                                has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                side=p.side,
                                profit_pnl_pct=profit_pnl_pct,
                                mfe_pnl_pct=mfe_pnl_pct,
                                current_value_usd=effective_exit_value,
                                peak_value_usd=float(getattr(p, "max_favorable_value_usd", effective_exit_value) or effective_exit_value),
                                ws_velocity=ws_vel,
                                secs_left=secs_left,
                            ):
                                peak_value_usd = max(
                                    float(getattr(p, "max_favorable_value_usd", effective_exit_value) or effective_exit_value),
                                    float(effective_exit_value or 0.0),
                                )
                                drawdown_pct = (float(effective_exit_value or 0.0) - peak_value_usd) / max(peak_value_usd, 1e-9)
                                log(
                                    f"PROFIT REVERSAL EXIT: {p.side} {p.token_id[-6:]} "
                                    f"profit={profit_pnl_pct:.2%} mfe={mfe_pnl_pct:.2%} "
                                    f"drawdown={drawdown_pct:.2%} Binance vel={ws_vel:.4%}"
                                )
                                exit_decision.should_close = True
                                exit_decision.reason = "profit-reversal-stop"
                            
                            # 2. Let Profits Run
                            if getattr(exit_decision, "should_close", False) and exit_decision.reason in ("take-profit-partial", "take-profit-principal", "take-profit-full"):
                                is_pump = (p.side == "UP" and ws_vel > SETTINGS.tp_hold_velocity) or \
                                          (p.side == "DOWN" and ws_vel < -SETTINGS.tp_hold_velocity)
                                if is_pump:
                                    log(f"📈 LET PROFITS RUN! Delaying TP for {p.side}. Binance vel={ws_vel:.4%}")
                                    exit_decision.should_close = False
                        except Exception:
                            pass
                        # --- Hard Stop Velocity Shield ---
                        # If Binance price is still moving with the position, delay hard-stop for one cycle.
                        # Panic-dump override (above) fires first and is unaffected.
                        if (
                            getattr(SETTINGS, "enable_hard_stop_shield", False)
                            and exit_decision.should_close
                            and exit_decision.reason == "hard-stop-loss"
                        ):
                            _shield_vel = getattr(SETTINGS, "hard_stop_shield_velocity", 0.0)
                            if _shield_vel > 0.0:
                                _hs_ws_vel = 0.0
                                try:
                                    _hs_ws_vel = BINANCE_WS.get_price_velocity(
                                        3.0,
                                        lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0)),
                                    )
                                except Exception:
                                    pass
                                _same_dir = (
                                    (p.side == "UP" and _hs_ws_vel > _shield_vel) or
                                    (p.side == "DOWN" and _hs_ws_vel < -_shield_vel)
                                )
                                if _same_dir:
                                    log(
                                        f"HARD STOP SHIELDED: {p.side} {p.token_id[-6:]} "
                                        f"Binance vel={_hs_ws_vel:.4%} — skipping this cycle"
                                    )
                                    exit_decision.should_close = False
                                    exit_decision.reason = ""
                        # ---------------------------------
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
                                
                                try:
                                    close_resp = ex.close_position(p.token_id, sell_shares, simulated_price=float(mark) if mark is not None else None)
                                    if close_resp.get("ok"):
                                        starting_shares = float(p.shares)
                                        starting_cost = float(p.cost_usd)
                                        sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), sell_shares)
                                        if sold_shares > 0:
                                            remaining_hint = close_resp.get("remaining_shares")
                                            resolved_remaining_shares = resolve_close_remaining_shares(
                                                requested_shares=starting_shares,
                                                sold_shares=sold_shares,
                                                remaining_hint=remaining_hint,
                                            )
                                            sold_shares = resolve_effective_closed_shares(
                                                starting_shares=starting_shares,
                                                sold_shares=sold_shares,
                                                remaining_shares=resolved_remaining_shares,
                                            )
                                            avg_cost = starting_cost / max(starting_shares, 1e-9)
                                            remaining_cost = avg_cost * resolved_remaining_shares
                                            realized_cost = max(0.0, starting_cost - remaining_cost)
                                            p.shares = resolved_remaining_shares
                                            p.cost_usd = remaining_cost
                                            p.has_scaled_out = True
                                            
                                            _raw_act_val = close_resp.get("actual_exit_value_usd", 0.0)
                                            _raw_act_src = str(close_resp.get("actual_exit_value_source") or "unavailable")
                                            _act_val, _act_src = sanitize_live_actual_exit_value(
                                                actual_exit_value_usd=_raw_act_val,
                                                actual_exit_value_source=_raw_act_src,
                                                sold_shares=sold_shares,
                                                mark=mark,
                                                dry_run=SETTINGS.dry_run,
                                            )
                                            _obs_val = observed_exit_value_from_mark(sold_shares=sold_shares, mark=mark)
                                            _realized_pnl = realized_exit_pnl(
                                                _act_val,
                                                _obs_val,
                                                realized_cost,
                                            )
                                            risk.daily_pnl += _realized_pnl
                                            append_event({
                                                "kind": "exit",
                                                "slug": p.slug,
                                                "side": p.side,
                                                "token_id": p.token_id,
                                                "position_id": p.position_id,
                                                "closed_shares": sold_shares,
                                                "remaining_shares": resolved_remaining_shares,
                                                "realized_cost_usd": realized_cost,
                                                "actual_exit_value_usd": _act_val,
                                                "actual_exit_value_source": _act_src or "unavailable",
                                                "actual_realized_pnl_usd": (_act_val - realized_cost) if _act_val is not None else None,
                                                "observed_exit_value_usd": _obs_val,
                                                "observed_exit_value_source": "observed_mark_price",
                                                "observed_realized_pnl_usd": _obs_val - realized_cost,
                                                "exit_execution_style": normalize_execution_style(close_resp.get("execution_style"), default="maker"),
                                                "status": "partial",
                                                "reason": "scale-out",
                                                "mfe_pnl_usd": p.max_favorable_pnl_usd,
                                                "mae_pnl_usd": p.max_adverse_pnl_usd
                                            })
                                            
                                            log(f"SCALED OUT! Sold {sold_shares:.2f} shares to lock in cost. Moonbag active.")
                                            maybe_record_cycle_label(state, "scale-out", slug=p.slug, side=p.side)
                                except Exception as e:
                                    log(f"Scale-out error: {e}")
                                if p.shares > LOT_EPS_SHARES and p.cost_usd > LOT_EPS_COST_USD:
                                    keep_positions.append(p)
                                else:
                                    log(
                                        f"drop residual after scale-out | token={p.token_id} "
                                        f"remaining_shares={p.shares:.6f} remaining_cost={p.cost_usd:.6f}"
                                    )
                                continue

                            if exit_decision.reason == "stop-loss-scale-out":
                                sell_fraction = effective_stop_loss_partial_fraction(
                                    dry_run=SETTINGS.dry_run,
                                )
                                sell_shares = p.shares * sell_fraction
                                
                                try:
                                    close_resp = ex.close_position(p.token_id, sell_shares, simulated_price=float(mark) if mark is not None else None)
                                    if close_resp.get("ok"):
                                        starting_shares = float(p.shares)
                                        starting_cost = float(p.cost_usd)
                                        sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), sell_shares)
                                        if sold_shares > 0:
                                            remaining_hint = close_resp.get("remaining_shares")
                                            resolved_remaining_shares = resolve_close_remaining_shares(
                                                requested_shares=starting_shares,
                                                sold_shares=sold_shares,
                                                remaining_hint=remaining_hint,
                                            )
                                            sold_shares = resolve_effective_closed_shares(
                                                starting_shares=starting_shares,
                                                sold_shares=sold_shares,
                                                remaining_shares=resolved_remaining_shares,
                                            )
                                            avg_cost = starting_cost / max(starting_shares, 1e-9)
                                            remaining_cost = avg_cost * resolved_remaining_shares
                                            realized_cost = max(0.0, starting_cost - remaining_cost)
                                            p.shares = resolved_remaining_shares
                                            p.cost_usd = remaining_cost
                                            p.has_scaled_out_loss = True
                                            if should_arm_residual_force_close_after_stop_loss_scaleout(
                                                dry_run=SETTINGS.dry_run,
                                                remaining_shares=resolved_remaining_shares,
                                                remaining_cost_usd=remaining_cost,
                                            ):
                                                p.force_close_only = True
                                                p.has_panic_dumped = should_force_taker_exit(
                                                    reason="residual-force-close",
                                                    dry_run=SETTINGS.dry_run,
                                                    has_panic_dumped=getattr(p, "has_panic_dumped", False),
                                                )
                                            
                                            _raw_act_val = close_resp.get("actual_exit_value_usd", 0.0)
                                            _raw_act_src = str(close_resp.get("actual_exit_value_source") or "unavailable")
                                            _act_val, _act_src = sanitize_live_actual_exit_value(
                                                actual_exit_value_usd=_raw_act_val,
                                                actual_exit_value_source=_raw_act_src,
                                                sold_shares=sold_shares,
                                                mark=mark,
                                                dry_run=SETTINGS.dry_run,
                                            )
                                            _obs_val = observed_exit_value_from_mark(sold_shares=sold_shares, mark=mark)
                                            _realized_pnl = realized_exit_pnl(
                                                _act_val,
                                                _obs_val,
                                                realized_cost,
                                            )
                                            risk.daily_pnl += _realized_pnl
                                            append_event({
                                                "kind": "exit",
                                                "slug": p.slug,
                                                "side": p.side,
                                                "token_id": p.token_id,
                                                "position_id": p.position_id,
                                                "closed_shares": sold_shares,
                                                "remaining_shares": resolved_remaining_shares,
                                                "realized_cost_usd": realized_cost,
                                                "actual_exit_value_usd": _act_val,
                                                "actual_exit_value_source": _act_src or "unavailable",
                                                "actual_realized_pnl_usd": (_act_val - realized_cost) if _act_val is not None else None,
                                                "observed_exit_value_usd": _obs_val,
                                                "observed_exit_value_source": "observed_mark_price",
                                                "observed_realized_pnl_usd": _obs_val - realized_cost,
                                                "exit_execution_style": normalize_execution_style(close_resp.get("execution_style"), default="maker"),
                                                "status": "partial",
                                                "reason": "stop-loss-scale-out",
                                                "mfe_pnl_usd": p.max_favorable_pnl_usd,
                                                "mae_pnl_usd": p.max_adverse_pnl_usd
                                            })
                                            
                                            log(f"STOP-LOSS SCALED OUT! Sold {sold_shares:.2f} shares to mitigate risk.")
                                            if getattr(p, "force_close_only", False):
                                                log(
                                                    f"stop-loss tail cleanup armed | token={p.token_id} "
                                                    f"remaining_shares={p.shares:.6f} remaining_cost={p.cost_usd:.6f}"
                                                )
                                            if should_block_same_market_reentry(
                                                "stop-loss-scale-out",
                                                remaining_shares=resolved_remaining_shares,
                                                realized_pnl_usd=(
                                                    (_act_val - realized_cost)
                                                    if _act_val is not None
                                                    else (_obs_val - realized_cost)
                                                ),
                                            ):
                                                same_market_reentry_block_slug = p.slug
                                                log(
                                                    f"same-market reentry blocked | slug={p.slug} side={p.side} "
                                                    "reason=stop-loss-scale-out"
                                                )
                                            maybe_record_cycle_label(state, "stop-loss-scale-out", slug=p.slug, side=p.side)
                                except Exception as e:
                                    log(f"Stop-loss scale-out error: {e}")
                                if p.shares > LOT_EPS_SHARES and p.cost_usd > LOT_EPS_COST_USD:
                                    keep_positions.append(p)
                                else:
                                    log(
                                        f"drop residual after stop-loss scale-out | token={p.token_id} "
                                        f"remaining_shares={p.shares:.6f} remaining_cost={p.cost_usd:.6f}"
                                    )
                                continue

                            if exit_decision.reason == "take-profit-principal":
                                current_value = max(float(profit_reference_value or 0.0), 1e-9)
                                p.entry_shares = max(float(getattr(p, "entry_shares", 0.0) or 0.0), float(p.shares or 0.0))
                                target_runner_shares = target_runner_remaining_shares(p)
                                sell_fraction = principal_extraction_sell_fraction(
                                    current_value,
                                    p.cost_usd,
                                    current_shares=p.shares,
                                    target_remaining_shares=target_runner_shares,
                                )
                                sell_shares = p.shares * sell_fraction
                                target_principal_usd = max(p.cost_usd, 0.0)
                                
                                try:
                                    close_resp = ex.close_position(
                                        p.token_id,
                                        sell_shares,
                                        simulated_price=float(mark) if mark is not None else None,
                                        force_taker=True,
                                    )
                                    if close_resp.get("ok"):
                                        starting_shares = float(p.shares)
                                        starting_cost = float(p.cost_usd)
                                        sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), sell_shares)
                                        if sold_shares > 0:
                                            remaining_hint = close_resp.get("remaining_shares")
                                            resolved_remaining_shares = resolve_close_remaining_shares(
                                                requested_shares=starting_shares,
                                                sold_shares=sold_shares,
                                                remaining_hint=remaining_hint,
                                            )
                                            sold_shares = resolve_effective_closed_shares(
                                                starting_shares=starting_shares,
                                                sold_shares=sold_shares,
                                                remaining_shares=resolved_remaining_shares,
                                            )
                                            avg_cost = starting_cost / max(starting_shares, 1e-9)
                                            remaining_cost = avg_cost * resolved_remaining_shares
                                            realized_cost = max(0.0, starting_cost - remaining_cost)
                                            actual_fraction = sold_shares / max(starting_shares, 1e-9)
                                            p.shares = resolved_remaining_shares
                                            p.cost_usd = remaining_cost

                                            _raw_act_val = close_resp.get("actual_exit_value_usd", 0.0)
                                            _raw_act_src = str(close_resp.get("actual_exit_value_source") or "unavailable")
                                            _act_val, _act_src = sanitize_live_actual_exit_value(
                                                actual_exit_value_usd=_raw_act_val,
                                                actual_exit_value_source=_raw_act_src,
                                                sold_shares=sold_shares,
                                                mark=mark,
                                                dry_run=SETTINGS.dry_run,
                                            )
                                            _obs_val = observed_exit_value_from_mark(sold_shares=sold_shares, mark=mark)
                                            _principal_recovered = _act_val if _act_val is not None else _obs_val
                                            principal_done = principal_extraction_complete(
                                                _principal_recovered,
                                                target_principal_usd,
                                            )
                                            p.has_extracted_principal = principal_done
                                            if principal_done:
                                                remaining_runner_value = current_value * max(0.0, 1.0 - actual_fraction)
                                                p.runner_peak_value_usd = max(0.0, float(remaining_runner_value or 0.0))
                                                p.runner_peak_ts = time.time()
                                            _realized_pnl = (_act_val - realized_cost) if _act_val is not None else (_obs_val - realized_cost)
                                            risk.daily_pnl += _realized_pnl
                                            tp_reason = "take-profit-principal" if principal_done else "take-profit-principal-partial"
                                            append_event({
                                                "kind": "exit",
                                                "slug": p.slug,
                                                "side": p.side,
                                                "token_id": p.token_id,
                                                "position_id": p.position_id,
                                                "closed_shares": sold_shares,
                                                "remaining_shares": resolved_remaining_shares,
                                                "realized_cost_usd": realized_cost,
                                                "actual_exit_value_usd": _act_val,
                                                "actual_exit_value_source": _act_src or "unavailable",
                                                "actual_realized_pnl_usd": (_act_val - realized_cost) if _act_val is not None else None,
                                                "observed_exit_value_usd": _obs_val,
                                                "observed_exit_value_source": "observed_mark_price",
                                                "observed_realized_pnl_usd": _obs_val - realized_cost,
                                                "exit_execution_style": normalize_execution_style(close_resp.get("execution_style"), default="maker"),
                                                "status": "partial",
                                                "reason": tp_reason,
                                                "mfe_pnl_usd": p.max_favorable_pnl_usd,
                                                "mae_pnl_usd": p.max_adverse_pnl_usd
                                            })

                                            if principal_done:
                                                log(f"PRINCIPAL EXTRACTED! Sold {sold_shares:.2f} shares. Risk-Free Moonbag active.")
                                                if should_block_same_market_reentry(
                                                    tp_reason,
                                                    remaining_shares=resolved_remaining_shares,
                                                    realized_pnl_usd=_realized_pnl,
                                                ):
                                                    same_market_reentry_block_slug = p.slug
                                                    log(
                                                        f"same-market reentry blocked | slug={p.slug} side={p.side} "
                                                        f"reason={tp_reason}"
                                                    )
                                                maybe_record_cycle_label(state, "take-profit-principal", slug=p.slug, side=p.side)
                                            else:
                                                log(
                                                    f"principal extraction incomplete | side={p.side} "
                                                    f"recovered=${_principal_recovered:.4f} target=${target_principal_usd:.4f} "
                                                    f"sold_shares={sold_shares:.4f} remaining_shares={p.shares:.4f} "
                                                    f"runner_target={target_runner_shares:.4f}"
                                                )
                                                maybe_record_cycle_label(state, "take-profit-principal-partial", slug=p.slug, side=p.side)
                                    else:
                                        log(f"take-profit principal close failed: {close_resp}")
                                except Exception as e:
                                    log(f"Take-profit principal error: {e}")
                                if p.shares > LOT_EPS_SHARES and p.cost_usd > LOT_EPS_COST_USD:
                                    keep_positions.append(p)
                                else:
                                    log(
                                        f"drop residual after principal extraction | token={p.token_id} "
                                        f"remaining_shares={p.shares:.6f} remaining_cost={p.cost_usd:.6f}"
                                    )
                                continue

                            if exit_decision.reason == "take-profit-partial":
                                sell_fraction = min(0.95, max(0.05, float(getattr(SETTINGS, "take_profit_partial_fraction", 0.30) or 0.30)))
                                sell_shares = p.shares * sell_fraction
                                try:
                                    close_resp = ex.close_position(
                                        p.token_id,
                                        sell_shares,
                                        simulated_price=float(mark) if mark is not None else None,
                                        force_taker=(
                                            getattr(p, "has_panic_dumped", False)
                                            or should_force_taker_take_profit(dry_run=SETTINGS.dry_run)
                                        ),
                                    )
                                    if close_resp.get("ok"):
                                        starting_shares = float(p.shares)
                                        starting_cost = float(p.cost_usd)
                                        sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), sell_shares)
                                        if sold_shares > 0:
                                            remaining_hint = close_resp.get("remaining_shares")
                                            resolved_remaining_shares = resolve_close_remaining_shares(
                                                requested_shares=starting_shares,
                                                sold_shares=sold_shares,
                                                remaining_hint=remaining_hint,
                                            )
                                            sold_shares = resolve_effective_closed_shares(
                                                starting_shares=starting_shares,
                                                sold_shares=sold_shares,
                                                remaining_shares=resolved_remaining_shares,
                                            )
                                            avg_cost = starting_cost / max(starting_shares, 1e-9)
                                            remaining_cost = avg_cost * resolved_remaining_shares
                                            realized_cost = max(0.0, starting_cost - remaining_cost)

                                            _raw_act_val = close_resp.get("actual_exit_value_usd", 0.0)
                                            _raw_act_src = str(close_resp.get("actual_exit_value_source") or "unavailable")
                                            _act_val, _act_src = sanitize_live_actual_exit_value(
                                                actual_exit_value_usd=_raw_act_val,
                                                actual_exit_value_source=_raw_act_src,
                                                sold_shares=sold_shares,
                                                mark=mark,
                                                dry_run=SETTINGS.dry_run,
                                            )
                                            _obs_val = observed_exit_value_from_mark(sold_shares=sold_shares, mark=mark)
                                            _realized_pnl = (_act_val - realized_cost) if _act_val is not None else (_obs_val - realized_cost)
                                            risk.daily_pnl += _realized_pnl

                                            p.shares = resolved_remaining_shares
                                            p.cost_usd = remaining_cost
                                            p.has_taken_partial = True
                                            append_event({
                                                "kind": "exit",
                                                "slug": p.slug,
                                                "side": p.side,
                                                "token_id": p.token_id,
                                                "position_id": p.position_id,
                                                "closed_shares": sold_shares,
                                                "remaining_shares": resolved_remaining_shares,
                                                "realized_cost_usd": realized_cost,
                                                "actual_exit_value_usd": _act_val,
                                                "actual_exit_value_source": _act_src or "unavailable",
                                                "actual_realized_pnl_usd": (_act_val - realized_cost) if _act_val is not None else None,
                                                "observed_exit_value_usd": _obs_val,
                                                "observed_exit_value_source": "observed_mark_price",
                                                "observed_realized_pnl_usd": _obs_val - realized_cost,
                                                "exit_execution_style": normalize_execution_style(close_resp.get("execution_style"), default="maker"),
                                                "status": "partial",
                                                "reason": "take-profit-partial",
                                                "mfe_pnl_usd": p.max_favorable_pnl_usd,
                                                "mae_pnl_usd": p.max_adverse_pnl_usd,
                                            })
                                            log(
                                                f"PARTIAL PROFIT TAKEN! Sold {sold_shares:.2f} shares "
                                                f"({sell_fraction:.0%} clip at +{getattr(SETTINGS, 'take_profit_soft_pct', 0.30):.0%} threshold)."
                                            )
                                            if should_block_same_market_reentry(
                                                "take-profit-partial",
                                                remaining_shares=resolved_remaining_shares,
                                                realized_pnl_usd=_realized_pnl,
                                            ):
                                                same_market_reentry_block_slug = p.slug
                                                log(
                                                    f"same-market reentry blocked | slug={p.slug} side={p.side} "
                                                    "reason=take-profit-partial"
                                                )
                                            maybe_record_cycle_label(state, "take-profit-partial", slug=p.slug, side=p.side)
                                    else:
                                        log(f"take-profit partial close failed: {close_resp}")
                                except Exception as e:
                                    log(f"Take-profit partial error: {e}")
                                if p.shares > LOT_EPS_SHARES and p.cost_usd > LOT_EPS_COST_USD:
                                    keep_positions.append(p)
                                else:
                                    log(
                                        f"drop residual after take-profit partial | token={p.token_id} "
                                        f"remaining_shares={p.shares:.6f} remaining_cost={p.cost_usd:.6f}"
                                    )
                                continue

                            try:
                                close_retry_kwargs = emergency_exit_retry_kwargs(
                                    reason=exit_decision.reason,
                                    secs_left=secs_left,
                                    dry_run=SETTINGS.dry_run,
                                )
                                moonbag_pct = max(0.0, min(0.99, float(getattr(SETTINGS, "leave_moonbag_pct", 0.0) or 0.0)))
                                sell_shares = p.shares
                                if moonbag_pct > 0.0 and exit_decision.reason not in {"manual-emergency-close", "residual-force-close"}:
                                    moonbag_target = p.shares * moonbag_pct
                                    sell_target = p.shares - moonbag_target
                                    if sell_target > 0.0001:
                                        sell_shares = sell_target

                                close_resp = ex.close_position(
                                    p.token_id,
                                    sell_shares,
                                    simulated_price=float(mark) if mark is not None else None,
                                    force_taker=(
                                        should_force_taker_exit(
                                            reason=exit_decision.reason,
                                            dry_run=SETTINGS.dry_run,
                                            has_panic_dumped=getattr(p, "has_panic_dumped", False),
                                        )
                                        or should_force_taker_profit_protection(
                                            reason=exit_decision.reason,
                                            dry_run=SETTINGS.dry_run,
                                        )
                                    ),
                                    **close_retry_kwargs,
                                )
                                if close_resp.get("ok"):
                                    starting_shares = float(p.shares)
                                    starting_cost = float(p.cost_usd)
                                    sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), starting_shares)
                                    remaining_hint = close_resp.get("remaining_shares")
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
                                            residual_force_close = should_force_full_loss_exit(
                                                reason=exit_decision.reason,
                                                dry_run=SETTINGS.dry_run,
                                            )
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
                                                max_favorable_ts=float(getattr(p, "max_favorable_ts", p.opened_ts) or p.opened_ts or 0.0),
                                                has_scaled_out=getattr(p, "has_scaled_out", False),
                                                has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
                                                has_taken_partial=getattr(p, "has_taken_partial", False),
                                                has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                                has_panic_dumped=should_force_taker_exit(
                                                    reason=exit_decision.reason,
                                                    dry_run=SETTINGS.dry_run,
                                                    has_panic_dumped=getattr(p, "has_panic_dumped", False),
                                                ),
                                                force_close_only=residual_force_close,
                                                runner_peak_value_usd=float(getattr(p, "runner_peak_value_usd", 0.0) or 0.0),
                                                runner_peak_ts=float(getattr(p, "runner_peak_ts", 0.0) or 0.0),
                                            ))
                                        else:
                                            log(f"{exit_decision.reason} close failed: zero shares closed | resp={close_resp}")
                                            keep_positions.append(p)
                                    else:
                                        flags.close_fail_streak = 0
                                        closed_any = True
                                        remaining_shares = resolve_close_remaining_shares(
                                            requested_shares=starting_shares,
                                            sold_shares=sold_shares,
                                            remaining_hint=remaining_hint,
                                        )
                                        sold_shares = resolve_effective_closed_shares(
                                            starting_shares=starting_shares,
                                            sold_shares=sold_shares,
                                            remaining_shares=remaining_shares,
                                        )
                                        remaining_cost = p.avg_cost_per_share * remaining_shares
                                        realized_cost = max(0.0, starting_cost - remaining_cost)
                                        observed_exit_value_usd = observed_exit_value_from_mark(
                                            sold_shares=sold_shares,
                                            mark=mark,
                                        )
                                        observed_realized_pnl_usd = observed_exit_value_usd - realized_cost
                                        observed_realized_return_pct = observed_realized_pnl_usd / max(realized_cost, 1e-9)

                                        actual_exit_value_usd = float(close_resp.get("actual_exit_value_usd", 0.0) or 0.0)
                                        actual_exit_value_source = str(close_resp.get("actual_exit_value_source") or "")
                                        close_response_value = close_resp.get("close_response_value")
                                        close_response_value_source = str(close_resp.get("close_response_value_source") or "")
                                        close_response_amount_fields = close_resp.get("close_response_amount_fields") or {}

                                        if close_response_value is not None and float(close_response_value) > 0 and "balance" in close_response_value_source:
                                            actual_exit_value_usd = float(close_response_value)
                                            actual_exit_value_source = close_response_value_source or actual_exit_value_source or "close_response_value"

                                        actual_exit_value_usd, actual_exit_value_source = sanitize_live_actual_exit_value(
                                            actual_exit_value_usd=actual_exit_value_usd,
                                            actual_exit_value_source=actual_exit_value_source,
                                            sold_shares=sold_shares,
                                            mark=mark,
                                            dry_run=SETTINGS.dry_run,
                                        )

                                        if actual_exit_value_usd is not None and actual_exit_value_source == "cash_balance_delta":
                                            actual_realized_pnl_usd = actual_exit_value_usd - realized_cost
                                            actual_realized_return_pct = actual_realized_pnl_usd / max(realized_cost, 1e-9)
                                            pnl_source = "actual_cash_recovered"
                                            risk.daily_pnl += actual_realized_pnl_usd
                                        elif actual_exit_value_usd is not None and "sanity-rejected" not in actual_exit_value_source:
                                            actual_realized_pnl_usd = actual_exit_value_usd - realized_cost
                                            actual_realized_return_pct = actual_realized_pnl_usd / max(realized_cost, 1e-9)
                                            pnl_source = "actual_close_response_value"
                                            risk.daily_pnl += actual_realized_pnl_usd
                                        else:
                                            actual_exit_value_usd = actual_exit_value_usd if actual_exit_value_usd and actual_exit_value_usd > 0 else None
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
                                            "exit_execution_style": normalize_execution_style(close_resp.get("execution_style"), default="maker"),
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
                                        if should_block_same_market_reentry(
                                            exit_decision.reason,
                                            remaining_shares=remaining_shares,
                                            realized_pnl_usd=(
                                                actual_realized_pnl_usd
                                                if actual_realized_pnl_usd is not None
                                                else observed_realized_pnl_usd
                                            ),
                                        ):
                                            same_market_reentry_block_slug = p.slug
                                            log(
                                                f"same-market reentry blocked | slug={p.slug} side={p.side} "
                                                f"reason={exit_decision.reason}"
                                            )

                                        if p.entry_reason:
                                            from core.learning import SCOREBOARD
                                            SCOREBOARD.record_outcome(
                                                strategy_name=p.entry_reason,
                                                pnl_pct=actual_realized_return_pct if actual_realized_return_pct is not None else observed_realized_return_pct,
                                                timestamp=time.time()
                                            )
                                        if remaining_shares > LOT_EPS_SHARES and remaining_cost > LOT_EPS_COST_USD:
                                            # If we successfully created a moonbag, skip force close and mark it
                                            is_moonbag_triggered = False
                                            if moonbag_pct > 0.0 and exit_decision.reason not in {"manual-emergency-close", "residual-force-close"}:
                                                is_moonbag_triggered = True

                                            residual_force_close = False if is_moonbag_triggered else should_force_full_loss_exit(
                                                reason=exit_decision.reason,
                                                dry_run=SETTINGS.dry_run,
                                            )
                                            dust_retry = int(getattr(p, "dust_retry_count", 0)) + 1
                                            if residual_force_close:
                                                log(
                                                    f"residual liquidation mode | token={p.token_id} remaining_shares={remaining_shares:.6f} "
                                                    f"remaining_cost={remaining_cost:.6f}"
                                                )
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
                                                    max_favorable_ts=float(getattr(p, "max_favorable_ts", p.opened_ts) or p.opened_ts or 0.0),
                                                    has_scaled_out=getattr(p, "has_scaled_out", False),
                                                    has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
                                                    has_taken_partial=getattr(p, "has_taken_partial", False),
                                                    has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                                    has_panic_dumped=should_force_taker_exit(

                                                        reason=exit_decision.reason,
                                                        dry_run=SETTINGS.dry_run,
                                                        has_panic_dumped=getattr(p, "has_panic_dumped", False),
                                                    ),
                                                    force_close_only=True,
                                                    runner_peak_value_usd=float(getattr(p, "runner_peak_value_usd", 0.0) or 0.0),
                                                    runner_peak_ts=float(getattr(p, "runner_peak_ts", 0.0) or 0.0),
                                                    dust_retry_count=dust_retry,
                                                ))
                                            elif dust_retry > 3:  # DUST_MAX_RETRIES = 3
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
                                                    max_favorable_ts=float(getattr(p, "max_favorable_ts", p.opened_ts) or p.opened_ts or 0.0),
                                                    has_scaled_out=getattr(p, "has_scaled_out", False),
                                                    has_scaled_out_loss=getattr(p, "has_scaled_out_loss", False),
                                                    has_taken_partial=getattr(p, "has_taken_partial", False),
                                                    has_extracted_principal=getattr(p, "has_extracted_principal", False),
                                                    has_panic_dumped=getattr(p, "has_panic_dumped", False),
                                                    force_close_only=getattr(p, "force_close_only", False),
                                                    is_moonbag=is_moonbag_triggered,
                                                    runner_peak_value_usd=float(getattr(p, "runner_peak_value_usd", 0.0) or 0.0),
                                                    runner_peak_ts=float(getattr(p, "runner_peak_ts", 0.0) or 0.0),
                                                    dust_retry_count=dust_retry,
                                                ))
                                            
                                            if is_moonbag_triggered:
                                                log(f"🎑 MOONBAG SECURED! {p.token_id[-6:]} remaining {remaining_shares:.2f} shares to run risk-free.")
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
                                    kept = OpenPos(**p.__dict__)
                                    if should_force_full_loss_exit(reason=exit_decision.reason, dry_run=SETTINGS.dry_run):
                                        kept.force_close_only = True
                                        kept.has_panic_dumped = should_force_taker_exit(
                                            reason=exit_decision.reason,
                                            dry_run=SETTINGS.dry_run,
                                            has_panic_dumped=getattr(p, "has_panic_dumped", False),
                                        )
                                    keep_positions.append(kept)
                            except Exception as e:
                                flags.close_fail_streak += 1
                                if urgent_exit:
                                    flags.panic_exit_mode = True
                                    panic_market_slug = p.slug
                                log(f"{exit_decision.reason} close failed: {e}")
                                kept = OpenPos(**p.__dict__)
                                if should_force_full_loss_exit(reason=exit_decision.reason, dry_run=SETTINGS.dry_run):
                                    kept.force_close_only = True
                                    kept.has_panic_dumped = should_force_taker_exit(
                                        reason=exit_decision.reason,
                                        dry_run=SETTINGS.dry_run,
                                        has_panic_dumped=getattr(p, "has_panic_dumped", False),
                                    )
                                keep_positions.append(kept)
                        else:
                            keep_positions.append(p)
                    open_positions, residual_notes = sanitize_open_positions(keep_positions, source="post-close")
                    for note in residual_notes:
                        log(note)
                    if (not open_positions) and flags.close_fail_streak == 0:
                        flags.panic_exit_mode = False
                        panic_market_slug = ""

                    idle_min = (time.time() - last_trade_ts) / 60.0
                    # Cadence fallback disabled per Openclaw report (prevents forced trades without edge)

                    if signal_side:
                        entry_decision = maybe_reverse_entry(
                            signal_side=signal_side,
                            live_consec_losses=flags.live_consec_losses,
                            last_loss_side=flags.last_loss_side,
                        )
                        if entry_decision.reason:
                            # Guard: only reverse if the target side has a proven scoreboard edge (WR > 55%)
                            try:
                                from core.learning import SCOREBOARD
                                reversed_strat = strategy_name_for_side(signal_origin, entry_decision.side)
                                _rev_wr = SCOREBOARD.get_strategy_score(reversed_strat)
                            except Exception:
                                _rev_wr = 0.5  # fallback: neutral
                            if _rev_wr > 0.55:
                                signal_side = entry_decision.side
                                signal_origin = f"{signal_origin}+{entry_decision.reason}" if signal_origin else entry_decision.reason
                                signal_probability = None
                                strategy_win_rate = 0.5
                                strategy_trade_count = 0
                                strategy_decisive_trade_count = 0
                                effective_probability = None
                                entry_edge = None
                                log(f"{entry_decision.reason} applied | consec_losses={flags.live_consec_losses} last_loss_side={flags.last_loss_side} -> side={signal_side} (WR={_rev_wr:.1%})")
                            else:
                                log(f"loss-reversal SKIPPED: reversed side WR={_rev_wr:.1%} <= 55% threshold, keeping original signal={signal_side}")


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
                    network_notes = update_network_guard(
                        flags,
                        ws_age=current_ws_age(),
                        cycle_had_slow_api=cycle_had_slow_api,
                        cycle_api_error=True,
                    )
                    for note in network_notes:
                        log(note)
                    log(f"unexpected network or API error in main loop: {e}. Retrying in 5s...")
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
                        pending_orders=pending_orders,
                        flags=flags,
                        last_cycle_label=state.get("last_cycle_label", ""),
                        panic_market_slug=panic_market_slug,
                    )
                    smart_sleep(5.0)
                    continue
            else:
                price_now = ex.get_btc_price()
                signal_side = "UP" if int(price_now) % 2 == 0 else "DOWN"
                signal_origin = "dry-run-fallback"
                signal_probability = None

            network_notes = update_network_guard(
                flags,
                ws_age=cycle_ws_age,
                cycle_had_slow_api=cycle_had_slow_api,
                cycle_api_error=False,
            )
            for note in network_notes:
                log(note)
                if "network fail-safe mode ACTIVATED" in note:
                    notify_discord(SETTINGS.discord_webhook_url, "🛡️ Network fail-safe activated: new entries paused")
                elif "network fail-safe mode CLEARED" in note:
                    notify_discord(SETTINGS.discord_webhook_url, "✅ Network fail-safe cleared: entry engine resumed")

            save_runtime_state(
                risk,
                last_market_slug=last_market_slug,
                same_market_reentry_block_slug=same_market_reentry_block_slug,
                yes_price_window=yes_price_window,
                up_price_window=up_price_window,
                down_price_window=down_price_window,
                last_trade_ts=last_trade_ts,
                prev_up=prev_up,
                prev_down=prev_down,
                error_cooldown_until=error_cooldown_until,
                open_positions=open_positions,
                pending_orders=pending_orders,
                flags=flags,
                last_cycle_label=state.get("last_cycle_label", ""),
                panic_market_slug=panic_market_slug,
            )

            if signal_side is None:
                if SETTINGS.dry_run:
                    acct = ex.get_account()
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

            if cycle_ws_age > float(getattr(SETTINGS, "ws_stale_max_age_sec", 5.0)):
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="ws-stale")
                log(
                    f"skip entry: Binance WS stale | age={cycle_ws_age:.1f}s "
                    f"threshold={float(getattr(SETTINGS, 'ws_stale_max_age_sec', 5.0)):.1f}s"
                )
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if cycle_had_slow_api:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="slow-api-latency")
                log(
                    f"skip entry: slow API latency this cycle | last_latency_ms={flags.last_api_latency_ms:.0f} "
                    f"threshold_ms={float(getattr(SETTINGS, 'api_slow_threshold_ms', 1500.0)):.0f}"
                )
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if flags.network_fail_safe_mode:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="network-fail-safe")
                log(
                    f"network_fail_safe_mode active: block new entries | api_fail_streak={flags.api_fail_streak} "
                    f"slow_api_streak={flags.slow_api_streak} ws_stale_streak={flags.ws_stale_streak}"
                )
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if signal_side and signal_origin and entry_price and float(entry_price) > 0 and entry_edge is None:
                effective_probability = signal_probability
                try:
                    from core.learning import SCOREBOARD
                    strategy_win_rate = SCOREBOARD.get_strategy_score(signal_origin)
                    strategy_trade_count = SCOREBOARD.get_strategy_trade_count(signal_origin)
                    strategy_decisive_trade_count = SCOREBOARD.get_strategy_decisive_trade_count(signal_origin)
                except Exception as e:
                    log(f"scoreboard lookup error: {e}")
                strategy_win_rate = stabilize_entry_win_rate(strategy_win_rate, strategy_decisive_trade_count)
                if effective_probability is None:
                    effective_probability = strategy_win_rate
                else:
                    effective_probability = apply_scoreboard_aux_probability(effective_probability, strategy_win_rate)
                entry_edge = summarize_entry_edge(
                    win_rate=effective_probability,
                    entry_price=float(entry_price),
                    secs_left=secs_left if "secs_left" in locals() else None,
                    history_count=strategy_decisive_trade_count,
                )
                if not entry_edge["ok"]:
                    maybe_record_cycle_label(
                        state,
                        "signal-blocked",
                        slug=last_market_slug,
                        side=signal_side,
                        reason="weak-model-edge",
                    )
                    log(
                        f"skip entry: weak model edge | strategy={signal_origin} "
                        f"modelP={(signal_probability if signal_probability is not None else strategy_win_rate):.1%} "
                        f"auxWR={strategy_win_rate:.1%} effectiveP={effective_probability:.1%} "
                        f"price={float(entry_price):.3f} raw_edge={entry_edge['raw_edge']:.3f} "
                        f"required={entry_edge['required_edge']:.3f} history={strategy_trade_count} decisive={strategy_decisive_trade_count}"
                    )
                    smart_sleep(SETTINGS.poll_seconds)
                    continue

            if signal_side and signal_origin and entry_price and float(entry_price) > 0 and entry_edge is not None:
                log(
                    f"entry approved | strategy={signal_origin} side={signal_side} "
                    f"modelP={(signal_probability if signal_probability is not None else strategy_win_rate):.1%} "
                    f"auxWR={strategy_win_rate:.1%} effectiveP={effective_probability:.1%} "
                    f"price={float(entry_price):.3f} raw_edge={entry_edge['raw_edge']:.3f} "
                    f"required={entry_edge['required_edge']:.3f}"
                )

            order_usd = SETTINGS.max_order_usd
            entry_book_quality: dict[str, float | bool | str | None] | None = None
            estimated_market_entry_avg_price: float | None = None
            estimated_market_entry_shares: float = 0.0
            if getattr(SETTINGS, "use_kelly_sizing", False) and signal_origin:
                try:
                    win_rate = effective_probability if effective_probability is not None else strategy_win_rate
                    entry_price_value = float(entry_price) if entry_price and float(entry_price) > 0 else 0.0
                    q_kelly = price_aware_kelly_fraction(win_rate, entry_price_value) if entry_price_value > 0 else 0.0
                    if q_kelly > 0:
                        bankroll = acct.equity
                        kelly_bet = bankroll * q_kelly
                        # Kelly can shrink OR grow the bet — respect min $1 floor and hard cap.
                        min_bet = SETTINGS.max_order_usd  # never bet less than baseline
                        order_usd = max(min_bet, min(kelly_bet, getattr(SETTINGS, "max_bet_cap_usd", 50.0)))
                        log(
                            f"Kelly Sizing | Strategy={signal_origin} estP={win_rate:.1%} price={entry_price_value:.3f} "
                            f"qK={q_kelly:.2%} Bankroll=${bankroll:.2f} -> Bet=${order_usd:.2f}"
                        )
                    else:
                        log(
                            f"Kelly Sizing | Strategy={signal_origin} estP={win_rate:.1%} "
                            f"price={entry_price_value:.3f} -> no size edge, baseline bet=${order_usd:.2f}"
                        )
                except Exception as e:
                    log(f"Kelly calc error: {e}")

            if not SETTINGS.dry_run:
                live_order_hard_cap_usd = float(getattr(SETTINGS, "live_order_hard_cap_usd", 0.0) or 0.0)
                if live_order_hard_cap_usd > 0.0 and order_usd > live_order_hard_cap_usd + 1e-9:
                    log(f"live order cap applied | requested=${order_usd:.2f} -> capped=${live_order_hard_cap_usd:.2f}")
                    order_usd = live_order_hard_cap_usd

            if signal_side and token_override and entry_price and float(entry_price) > 0:
                est_shares = order_usd / float(entry_price)
                entry_book = None
                estimated_market_entry_avg_price = None
                estimated_market_entry_shares = 0.0
                if not SETTINGS.dry_run:
                    min_live_order_shares = float(getattr(SETTINGS, "min_live_order_shares", 5.0) or 0.0)
                    min_live_order_usd = float(getattr(SETTINGS, "min_live_order_usd", 1.0) or 0.0)
                    live_order_hard_cap_usd = float(getattr(SETTINGS, "live_order_hard_cap_usd", 0.0) or 0.0)
                    requested_shares = est_shares
                    live_market_entry = bool(getattr(SETTINGS, "live_entry_use_market_orders", True))
                    if live_market_entry:
                        required_usd = round(max(order_usd, min_live_order_usd), 4)
                        if live_order_hard_cap_usd > 0.0 and required_usd > live_order_hard_cap_usd + 1e-9:
                            maybe_record_cycle_label(
                                state,
                                "signal-blocked",
                                slug=last_market_slug,
                                side=signal_side,
                                reason="order-size-below-minimum",
                            )
                            log(
                                f"skip entry: order size below minimum | requested=${order_usd:.2f} "
                                f"price={float(entry_price):.3f} requested_shares={requested_shares:.2f} "
                                f"required_usd=${required_usd:.2f} cap_usd=${live_order_hard_cap_usd:.2f}"
                            )
                            smart_sleep(SETTINGS.poll_seconds)
                            continue
                        if required_usd > order_usd + 1e-9:
                            log(
                                f"live market order auto-bump | requested=${order_usd:.2f} "
                                f"-> actual=${required_usd:.4f} min_notional=${min_live_order_usd:.2f}"
                            )
                            order_usd = required_usd
                        est_shares = order_usd / float(entry_price)
                    else:
                        required_shares, required_usd = plan_live_order(
                            order_usd,
                            float(entry_price),
                            min_live_order_shares,
                            min_live_order_usd,
                        )
                        if live_order_hard_cap_usd > 0.0 and required_usd > live_order_hard_cap_usd + 1e-9:
                            maybe_record_cycle_label(
                                state,
                                "signal-blocked",
                                slug=last_market_slug,
                                side=signal_side,
                                reason="order-size-below-minimum",
                            )
                            log(
                                f"skip entry: order size below minimum | requested=${order_usd:.2f} "
                                f"price={float(entry_price):.3f} requested_shares={requested_shares:.2f} "
                                f"required_shares={required_shares:.2f} "
                                f"min_shares={min_live_order_shares:.2f} min_notional=${min_live_order_usd:.2f} "
                                f"required_usd=${required_usd:.2f} cap_usd=${live_order_hard_cap_usd:.2f}"
                            )
                            smart_sleep(SETTINGS.poll_seconds)
                            continue
                        if required_usd > order_usd + 1e-9:
                            log(
                                f"live order auto-bump | requested=${order_usd:.2f} "
                                f"price={float(entry_price):.3f} requested_shares={requested_shares:.2f} "
                                f"-> actual=${required_usd:.4f} shares={required_shares:.2f} "
                                f"cap_usd={live_order_hard_cap_usd:.2f}"
                            )
                            order_usd = required_usd
                        est_shares = required_shares
                    entry_book = ex.get_full_orderbook(token_override)
                    if live_market_entry:
                        (
                            estimated_market_entry_avg_price,
                            estimated_market_entry_shares,
                            _estimated_market_entry_fill_ratio,
                        ) = estimate_book_entry_fill(book=entry_book, amount_usd=order_usd)
                        if (
                            estimated_market_entry_avg_price is not None
                            and float(estimated_market_entry_avg_price) > 0.0
                        ):
                            estimated_slippage_breach, estimated_slippage_premium_pct = entry_slippage_breach(
                                expected_entry_price=float(entry_price),
                                actual_avg_price=float(estimated_market_entry_avg_price),
                                dry_run=False,
                            )
                            if estimated_slippage_breach:
                                maybe_record_cycle_label(
                                    state,
                                    "signal-blocked",
                                    slug=last_market_slug,
                                    side=signal_side,
                                    reason="entry-slippage-precheck",
                                )
                                log(
                                    f"skip entry: entry-slippage-precheck | side={signal_side} "
                                    f"quoted={float(entry_price):.3f} est_avg={float(estimated_market_entry_avg_price):.3f} "
                                    f"premium={estimated_slippage_premium_pct:.2%} est_shares={estimated_market_entry_shares:.4f}"
                                )
                                smart_sleep(SETTINGS.poll_seconds)
                                continue
                try:
                    entry_book_quality = assess_entry_liquidity(
                        book=entry_book if entry_book is not None else ex.get_full_orderbook(token_override),
                        est_shares=est_shares,
                        max_spread=float(getattr(SETTINGS, "entry_max_spread", 0.0)),
                        min_best_ask_multiple=float(getattr(SETTINGS, "entry_min_best_ask_multiple", 0.0)),
                        min_total_ask_multiple=float(getattr(SETTINGS, "entry_min_total_ask_multiple", 0.0)),
                    )
                except Exception as e:
                    entry_book_quality = {"ok": True, "available": False, "reason": f"book-check-error:{e}"}

                block_for_book, book_block_reason = should_block_live_entry_for_unavailable_book(
                    dry_run=SETTINGS.dry_run,
                    entry_book_quality=entry_book_quality,
                )
                if block_for_book:
                    maybe_record_cycle_label(
                        state,
                        "signal-blocked",
                        slug=last_market_slug,
                        side=signal_side,
                        reason="live-entry-book-unavailable",
                    )
                    log(
                        f"skip entry: live entry requires usable orderbook | reason={book_block_reason} "
                        f"side={signal_side} quoted={float(entry_price):.3f}"
                    )
                    smart_sleep(SETTINGS.poll_seconds)
                    continue

                if entry_book_quality and entry_book_quality.get("available") and not entry_book_quality.get("ok"):
                    reason = str(entry_book_quality.get("reason") or "book-quality-fail")
                    maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason=reason)
                    log(
                        f"skip entry: {reason} | spread={float(entry_book_quality.get('spread') or 0.0):.3f} "
                        f"ask1={float(entry_book_quality.get('best_ask_size') or 0.0):.2f} "
                        f"askDepth={float(entry_book_quality.get('asks_volume') or 0.0):.2f} "
                        f"needShares={est_shares:.2f}"
                    )
                    smart_sleep(SETTINGS.poll_seconds)
                    continue

                if not ex.has_exit_liquidity(token_override, est_shares):
                    maybe_record_cycle_label(state, "signal-but-no-fill", slug=last_market_slug, side=signal_side, reason="weak-exit-liquidity-sized")
                    log(f"skip entry: weak exit liquidity for sized order (${order_usd:.2f}, est_shares={est_shares:.4f})")
                    smart_sleep(SETTINGS.poll_seconds)
                    continue

            if flags.panic_exit_mode:
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="panic-exit-mode")
                log("panic_exit_mode active: block new entries")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if any(po.slug == last_market_slug for po in pending_orders):
                maybe_record_cycle_label(state, "signal-blocked", slug=last_market_slug, side=signal_side, reason="existing-pending-order-still-open")
                log("skip entry: existing pending order still open")
                smart_sleep(SETTINGS.poll_seconds)
                continue

            if same_market_reentry_block_slug and same_market_reentry_block_slug == last_market_slug:
                maybe_record_cycle_label(
                    state,
                    "signal-blocked",
                    slug=last_market_slug,
                    side=signal_side,
                    reason="same-market-reentry-block",
                )
                log("skip entry: same market reentry blocked after recent terminal exit")
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
                    ws_vel = BINANCE_WS.get_price_velocity(
                        3.0,
                        lag_sec=float(getattr(SETTINGS, "binance_signal_lag_sec", 0.0)),
                    )
                    if (signal_side == "UP" and ws_vel > SETTINGS.taker_snipe_velocity) or \
                       (signal_side == "DOWN" and ws_vel < -SETTINGS.taker_snipe_velocity):
                        log(f"⚡ TAKER SNIPE TRIGGERED! {signal_side} Binance vel={ws_vel:.4%}")
                        force_taker_snipe = True
                except Exception:
                    pass
                use_market_entry = (not SETTINGS.dry_run) and bool(getattr(SETTINGS, "live_entry_use_market_orders", True))
                force_taker_entry = force_taker_snipe or use_market_entry

                resp, order_latencies, order_attempts = place_entry_order_with_retry(
                    ex,
                    signal_side,
                    order_usd,
                    token_override,
                    simulated_price=sim_price,
                    force_taker=force_taker_entry,
                    max_attempts=int(getattr(SETTINGS, "entry_retry_attempts", 3)),
                    backoff_sec=float(getattr(SETTINGS, "entry_retry_backoff_sec", 2.0)),
                )
                for idx, latency_ms in enumerate(order_latencies, start=1):
                    cycle_had_slow_api = observe_api_latency(flags, f"place_order#{idx}", latency_ms) or cycle_had_slow_api
                if order_attempts > 1:
                    log(f"entry order recovered after retry | attempts={order_attempts} side={signal_side}")
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
                        h_res, hedge_ms = timed_call(
                            ex.place_order,
                            hedge_side,
                            hedge_usd,
                            token_id_override=hedge_token_id,
                            simulated_price=h_sim_price,
                        )
                        observe_api_latency(flags, "place_order_hedge", hedge_ms)
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
                                    entry_shares=h_shares,
                                    cost_usd=hedge_usd,
                                    opened_ts=h_ts,
                                    position_id=f"pos_{int(h_ts)}_{hedge_token_id[-6:]}",
                                    entry_reason="structured-hedge",
                                    source="runtime",
                                    max_favorable_value_usd=hedge_usd,
                                    max_favorable_ts=h_ts,
                                ))
                        except Exception as e:
                            log(f"hedge parsing error: {e}")
                try:
                    r = resp.get("response", {}) if isinstance(resp, dict) else {}
                    actual_entry_cost_usd = extract_entry_cost_usd(resp, order_usd)
                    shares, order_id = extract_entry_response_details(resp)
                    actual_entry_avg_price = extract_entry_implied_avg_price(resp, order_usd)
                    token_id = token_override or (market["token_up"] if signal_side == "UP" else market["token_down"])
                    if shares > 0 and token_id:
                        slippage_breach = False
                        slippage_premium_pct = 0.0
                        slippage_expected_price = float(entry_price) if entry_price and float(entry_price) > 0 else 0.0
                        if estimated_market_entry_avg_price is not None and float(estimated_market_entry_avg_price) > 0.0:
                            slippage_expected_price = float(estimated_market_entry_avg_price)
                        if slippage_expected_price > 0:
                            slippage_breach, slippage_premium_pct = entry_slippage_breach(
                                expected_entry_price=slippage_expected_price,
                                actual_avg_price=actual_entry_avg_price,
                                dry_run=SETTINGS.dry_run,
                            )
                        if slippage_breach:
                            expected_price = float(slippage_expected_price)
                            actual_avg_price = float(actual_entry_avg_price or 0.0)
                            maybe_record_cycle_label(
                                state,
                                "entry-slippage-guard",
                                slug=market["slug"],
                                side=signal_side,
                                reason=f"premium={slippage_premium_pct:.2%}",
                            )
                            append_event({
                                "kind": "entry_attempt",
                                "slug": market["slug"],
                                "side": signal_side,
                                "token_id": token_id,
                                "status": "entry-slippage-breach",
                                "reason": "entry-slippage-guard",
                                "shares": shares,
                                "cost_usd": actual_entry_cost_usd,
                                "quoted_entry_price": expected_price,
                                "actual_entry_avg_price": actual_avg_price,
                                "slippage_premium_pct": slippage_premium_pct,
                                "response_mode": resp.get("mode") if isinstance(resp, dict) else "",
                            })
                            log(
                                f"ENTRY SLIPPAGE GUARD: side={signal_side} slug={market['slug']} "
                                f"quoted={expected_price:.3f} actual_avg={actual_avg_price:.3f} "
                                f"premium={slippage_premium_pct:.2%} shares={shares:.4f} -> forcing immediate close"
                            )
                            close_resp = ex.close_position(
                                token_id,
                                shares,
                                simulated_price=expected_price,
                                force_taker=True,
                            )
                            if close_resp.get("ok"):
                                sold_shares = min(float(close_resp.get("closed_shares", 0.0) or 0.0), shares)
                                close_fraction = sold_shares / max(shares, 1e-9)
                                realized_cost = actual_entry_cost_usd * close_fraction
                                remaining_shares = resolve_close_remaining_shares(
                                    requested_shares=shares,
                                    sold_shares=sold_shares,
                                    remaining_hint=close_resp.get("remaining_shares"),
                                )
                                remaining_cost = max(
                                    0.0,
                                    (actual_entry_cost_usd / max(shares, 1e-9)) * remaining_shares,
                                )
                                realized_cost = max(0.0, actual_entry_cost_usd - remaining_cost)
                                _raw_act_val = close_resp.get("actual_exit_value_usd", 0.0)
                                _raw_act_src = str(close_resp.get("actual_exit_value_source") or "unavailable")
                                _act_val, _act_src = sanitize_live_actual_exit_value(
                                    actual_exit_value_usd=_raw_act_val,
                                    actual_exit_value_source=_raw_act_src,
                                    sold_shares=sold_shares,
                                    mark=expected_price,
                                    dry_run=SETTINGS.dry_run,
                                )
                                _obs_val = observed_exit_value_from_mark(sold_shares=sold_shares, mark=expected_price)
                                realized_exit_value = _act_val if _act_val is not None else _obs_val
                                realized_pnl = realized_exit_value - realized_cost
                                risk.daily_pnl += realized_pnl
                                append_event({
                                    "kind": "exit",
                                    "slug": market["slug"],
                                    "side": signal_side,
                                    "token_id": token_id,
                                    "closed_shares": sold_shares,
                                    "remaining_shares": remaining_shares,
                                    "realized_cost_usd": realized_cost,
                                    "actual_exit_value_usd": _act_val,
                                    "actual_exit_value_source": _act_src or "unavailable",
                                    "actual_realized_pnl_usd": (_act_val - realized_cost) if _act_val is not None else None,
                                    "observed_exit_value_usd": _obs_val,
                                    "observed_exit_value_source": "observed_mark_price",
                                    "observed_realized_pnl_usd": _obs_val - realized_cost,
                                    "exit_execution_style": normalize_execution_style(close_resp.get("execution_style"), default="taker"),
                                    "status": "closed" if remaining_shares <= LOT_EPS_SHARES else "partial",
                                    "reason": "entry-slippage-guard",
                                    "mfe_pnl_usd": 0.0,
                                    "mae_pnl_usd": 0.0,
                                })
                                log(
                                    f"entry slippage guard close | side={signal_side} recovered=${realized_exit_value:.4f} "
                                    f"realized_pnl=${realized_pnl:.4f} remaining_shares={remaining_shares:.6f}"
                                )
                                if remaining_shares > LOT_EPS_SHARES:
                                    opened_ts = time.time()
                                    position_id = f"pos_{int(opened_ts)}_{token_id[-6:]}"
                                    open_positions.append(OpenPos(
                                        slug=market["slug"],
                                        side=signal_side,
                                        token_id=token_id,
                                        shares=remaining_shares,
                                        entry_shares=shares,
                                        cost_usd=remaining_cost,
                                        opened_ts=opened_ts,
                                        position_id=position_id,
                                        entry_reason=f"{signal_origin or 'signal'}-slippage-guard",
                                        source="live-order",
                                        force_close_only=True,
                                        max_favorable_value_usd=remaining_cost,
                                        max_adverse_value_usd=remaining_cost,
                                        max_favorable_pnl_usd=0.0,
                                        max_adverse_pnl_usd=0.0,
                                        max_favorable_ts=opened_ts,
                                    ))
                                    log(
                                        f"entry slippage guard residual | side={signal_side} "
                                        f"remaining_shares={remaining_shares:.6f} remaining_cost=${remaining_cost:.4f}"
                                    )
                            else:
                                opened_ts = time.time()
                                position_id = f"pos_{int(opened_ts)}_{token_id[-6:]}"
                                open_positions.append(OpenPos(
                                    slug=market["slug"],
                                    side=signal_side,
                                    token_id=token_id,
                                    shares=shares,
                                    entry_shares=shares,
                                    cost_usd=actual_entry_cost_usd,
                                    opened_ts=opened_ts,
                                    position_id=position_id,
                                    entry_reason=f"{signal_origin or 'signal'}-slippage-guard",
                                    source="live-order",
                                    pending_confirmation=True,
                                    force_close_only=True,
                                    max_favorable_value_usd=actual_entry_cost_usd,
                                    max_adverse_value_usd=actual_entry_cost_usd,
                                    max_favorable_pnl_usd=0.0,
                                    max_adverse_pnl_usd=0.0,
                                    max_favorable_ts=opened_ts,
                                ))
                                log(f"entry slippage guard close failed: {close_resp}")
                            log(f"order placed: {resp}")
                            notify_discord(
                                SETTINGS.discord_webhook_url,
                                f"⚠️ Entry slippage guard {signal_side} quoted {expected_price:.3f} actual {actual_avg_price:.3f}"
                            )
                            save_runtime_state(
                                risk,
                                last_market_slug=last_market_slug,
                                same_market_reentry_block_slug=same_market_reentry_block_slug,
                                yes_price_window=yes_price_window,
                                up_price_window=up_price_window,
                                down_price_window=down_price_window,
                                last_trade_ts=last_trade_ts,
                                prev_up=prev_up,
                                prev_down=prev_down,
                                error_cooldown_until=error_cooldown_until,
                                open_positions=open_positions,
                                pending_orders=pending_orders,
                                flags=flags,
                                last_cycle_label=state.get("last_cycle_label", ""),
                                panic_market_slug=panic_market_slug,
                            )
                            continue
                        opened_ts = time.time()
                        position_id = f"pos_{int(opened_ts)}_{token_id[-6:]}"
                        if should_count_entry_toward_market_limit(
                            slippage_breach=False,
                            shares=shares,
                            order_id=order_id,
                        ):
                            risk.orders_this_window += 1
                        open_positions.append(OpenPos(
                            slug=market["slug"],
                            side=signal_side,
                            token_id=token_id,
                            shares=shares,
                            entry_shares=shares,
                            cost_usd=actual_entry_cost_usd,
                            opened_ts=opened_ts,
                            position_id=position_id,
                            entry_reason=signal_origin or "signal",
                            source="live-order",
                            pending_confirmation=True,
                            max_favorable_value_usd=actual_entry_cost_usd,
                            max_adverse_value_usd=actual_entry_cost_usd,
                            max_favorable_pnl_usd=0.0,
                            max_adverse_pnl_usd=0.0,
                            max_favorable_ts=opened_ts,
                        ))
                        append_event({
                            "kind": "entry",
                            "slug": market["slug"],
                            "side": signal_side,
                            "token_id": token_id,
                            "position_id": position_id,
                            "shares": shares,
                            "cost_usd": actual_entry_cost_usd,
                            "opened_ts": opened_ts,
                            "entry_reason": signal_origin or "signal",
                            "classification": "good-entry-candidate",
                            "execution_style": normalize_execution_style(
                                resp.get("execution_style") if isinstance(resp, dict) else "",
                                default="taker" if force_taker_entry else "maker",
                            ),
                            "entry_price": float(entry_price),
                            "entry_book_spread": (float(entry_book_quality.get("spread")) if entry_book_quality and entry_book_quality.get("spread") is not None else None),
                            "entry_best_ask_size": (float(entry_book_quality.get("best_ask_size")) if entry_book_quality and entry_book_quality.get("best_ask_size") is not None else None),
                            "entry_ask_depth_shares": (float(entry_book_quality.get("asks_volume")) if entry_book_quality and entry_book_quality.get("asks_volume") is not None else None),
                            "mae_pnl_usd": 0.0,
                            "mfe_pnl_usd": 0.0,
                        })
                        maybe_record_cycle_label(state, "good-entry", slug=market["slug"], side=signal_side, reason=signal_origin or "signal")
                    else:
                        if order_id:
                            if should_count_entry_toward_market_limit(
                                slippage_breach=False,
                                shares=shares,
                                order_id=order_id,
                            ):
                                risk.orders_this_window += 1
                            pending_orders.append(PendingOrder(
                                order_id=order_id,
                                slug=market["slug"],
                                side=signal_side,
                                token_id=token_id,
                                placed_ts=time.time(),
                                order_usd=actual_entry_cost_usd,
                                entry_reason=signal_origin or "signal",
                                fallback_attempted=False,
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
                    same_market_reentry_block_slug=same_market_reentry_block_slug,
                    yes_price_window=yes_price_window,
                    up_price_window=up_price_window,
                    down_price_window=down_price_window,
                    last_trade_ts=last_trade_ts,
                    prev_up=prev_up,
                    prev_down=prev_down,
                    error_cooldown_until=error_cooldown_until,
                    open_positions=open_positions,
                    pending_orders=pending_orders,
                    flags=flags,
                    last_cycle_label=state.get("last_cycle_label", ""),
                    panic_market_slug=panic_market_slug,
                )
                log(f"order placed: {resp}")
                notify_discord(
                    SETTINGS.discord_webhook_url,
                    f"✅ Order {signal_side} ${float(resp.get('amount_usd', order_usd) or order_usd):.2f} ({resp.get('mode')})"
                )
            except Exception as e:
                network_notes = update_network_guard(
                    flags,
                    ws_age=current_ws_age(),
                    cycle_had_slow_api=cycle_had_slow_api,
                    cycle_api_error=True,
                )
                for note in network_notes:
                    log(note)
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
                    same_market_reentry_block_slug=same_market_reentry_block_slug,
                    yes_price_window=yes_price_window,
                    up_price_window=up_price_window,
                    down_price_window=down_price_window,
                    last_trade_ts=last_trade_ts,
                    prev_up=prev_up,
                    prev_down=prev_down,
                    error_cooldown_until=error_cooldown_until,
                    open_positions=open_positions,
                    pending_orders=pending_orders,
                    flags=flags,
                    last_cycle_label=state.get("last_cycle_label", ""),
                    panic_market_slug=panic_market_slug,
                )
                smart_sleep(
                    idle_sleep_seconds(
                        has_open_positions=bool(open_positions),
                        has_pending_orders=bool(pending_orders),
                    )
                )
                continue

            smart_sleep(
                idle_sleep_seconds(
                    has_open_positions=bool(open_positions),
                    has_pending_orders=bool(pending_orders),
                    secs_left=secs_left if "secs_left" in locals() else None,
                )
            )
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
                report_args = [sys.executable, str(script_path), "--limit", "30", "--summary", "--run-id", run_journal.run_id]
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
