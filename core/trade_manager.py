from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.config import SETTINGS


@dataclass
class ExitDecision:
    should_close: bool
    reason: str = ""
    pnl_pct: float = 0.0
    hold_sec: float = 0.0


@dataclass
class EntryDecision:
    side: Optional[str]
    reason: str = ""


def _check_expiry_hold_state(
    pnl_pct: float,
    hold_sec: float,
    secs_left: Optional[float],
    has_extracted_principal: bool,
    profit_pnl_pct: Optional[float] = None,
) -> Optional[ExitDecision]:
    if secs_left is None:
        return None

    # Skip for moonbags as per original logic (where moonbag block was before deadline)
    if has_extracted_principal:
        return None

    profit_deadline_sec = float(getattr(SETTINGS, "exit_deadline_profit_sec", 45) or 0.0)
    if profit_deadline_sec > 0.0 and secs_left <= profit_deadline_sec:
        if pnl_pct > 0:
            return ExitDecision(True, "deadline-take-profit-full", pnl_pct, hold_sec)

    exit_deadline_sec = getattr(SETTINGS, "exit_deadline_sec", 20)
    if secs_left <= exit_deadline_sec:
        if pnl_pct < 0:
            return ExitDecision(True, "deadline-exit-loss", pnl_pct, hold_sec)

        if pnl_pct <= getattr(SETTINGS, "exit_deadline_flat_pnl_pct", 0.0):
            if profit_pnl_pct is not None and profit_pnl_pct < 0:
                return None
            return ExitDecision(True, "deadline-exit-flat", pnl_pct, hold_sec)

        min_safe_profit = getattr(
            SETTINGS,
            "exit_deadline_min_safe_profit_pct",
            getattr(SETTINGS, "take_profit_soft_pct", 0.30),
        )
        if pnl_pct < min_safe_profit:
            if profit_pnl_pct is not None and profit_pnl_pct < 0:
                return None
            return ExitDecision(True, "deadline-exit-weak-win", pnl_pct, hold_sec)

    return None


def _check_emergency_loss_state(
    pnl_pct: float,
    hold_sec: float,
    has_scaled_out_loss: bool,
    recovery_chance_low: bool,
    has_extracted_principal: bool,
) -> Optional[ExitDecision]:
    # Skip for moonbags as per original logic
    if has_extracted_principal:
        return None

    # Post scale-out stop loss
    if has_scaled_out_loss:
        delay = getattr(SETTINGS, "post_scaleout_loss_exit_delay_sec", 20)
        pct = getattr(
            SETTINGS,
            "post_scaleout_loss_exit_pct",
            getattr(SETTINGS, "stop_loss_warn_pct", 0.08),
        )
        if hold_sec >= delay and pnl_pct <= -pct and recovery_chance_low:
            return ExitDecision(True, "post-scaleout-stop-loss", pnl_pct, hold_sec)

    # Stop Loss Handling
    sl_min_hold = float(getattr(SETTINGS, "stop_loss_min_hold_sec", 0.0) or 0.0)
    if hold_sec >= sl_min_hold:
        smart_enabled = getattr(SETTINGS, "smart_stop_loss_enabled", False)

        if pnl_pct <= -SETTINGS.stop_loss_pct:
            reason = (
                "hard-stop-loss" if smart_enabled else "stop-loss"
            )
            return ExitDecision(True, reason, pnl_pct, hold_sec)

        if (
            smart_enabled
            and recovery_chance_low
            and pnl_pct <= -getattr(SETTINGS, "stop_loss_warn_pct", 0.08)
        ):
            return ExitDecision(True, "smart-stop-loss", pnl_pct, hold_sec)

        if not has_scaled_out_loss and pnl_pct <= -getattr(
            SETTINGS, "stop_loss_partial_pct", 0.05
        ):
            if getattr(SETTINGS, "force_full_exit_on_stop_loss_scaleout", False):
                return ExitDecision(True, "stop-loss-full", pnl_pct, hold_sec)
            return ExitDecision(True, "stop-loss-scale-out", pnl_pct, hold_sec)

    # Max Hold
    max_hold = SETTINGS.max_hold_seconds
    if hold_sec >= max_hold and pnl_pct < 0:
        if (
            getattr(SETTINGS, "smart_stop_loss_enabled", False)
            and not recovery_chance_low
        ):
            if hold_sec >= max_hold * 2:
                return ExitDecision(True, "max-hold-loss-extended", pnl_pct, hold_sec)
        else:
            return ExitDecision(True, "max-hold-loss", pnl_pct, hold_sec)

    return None


def _check_soft_profit_state(
    pnl_pct: float,
    profit_pnl_pct: Optional[float],
    hold_sec: float,
    secs_left: Optional[float],
    has_taken_partial: bool,
    mfe_pnl_pct: float,
    has_extracted_principal: bool,
) -> Optional[ExitDecision]:
    if has_extracted_principal:
        return None

    take_profit_pnl_pct = None if profit_pnl_pct is None else float(profit_pnl_pct)
    _soft_tp = float(SETTINGS.take_profit_soft_pct)
    _hard_tp = getattr(SETTINGS, "take_profit_hard_pct", 0.50)
    _bid_discount_buffer = float(SETTINGS.take_profit_bid_discount_buffer)

    # Mark-price fallback
    _mark_tp_threshold = _soft_tp + _bid_discount_buffer
    _min_exec_fallback_pct = max(0.0, _soft_tp - _bid_discount_buffer)
    if (
        not has_taken_partial
        and pnl_pct >= _mark_tp_threshold
        and take_profit_pnl_pct is not None
    ):
        if (_min_exec_fallback_pct + 1e-9) <= take_profit_pnl_pct < _soft_tp:
            reason = (
                "take-profit-full"
                if getattr(SETTINGS, "force_full_exit_on_take_profit", False)
                else "take-profit-partial"
            )
            return ExitDecision(True, reason, pnl_pct, hold_sec)

    # Tiered Take Profit
    if take_profit_pnl_pct is not None and take_profit_pnl_pct >= _hard_tp:
        reason = (
            "take-profit-full"
            if getattr(SETTINGS, "force_full_exit_on_take_profit", False)
            else "take-profit-principal"
        )
        return ExitDecision(True, reason, take_profit_pnl_pct, hold_sec)

    # Principal extraction after partial
    if (
        has_taken_partial
        and take_profit_pnl_pct is not None
        and bool(getattr(SETTINGS, "take_profit_principal_after_partial_enabled", True))
    ):
        min_mfe = float(
            getattr(SETTINGS, "take_profit_principal_after_partial_min_mfe_pct", 0.24)
            or 0.24
        )
        drawdown = float(
            getattr(SETTINGS, "take_profit_principal_after_partial_drawdown_pct", 0.08)
            or 0.08
        )
        min_curr = float(
            getattr(SETTINGS, "take_profit_principal_after_partial_min_current_pct", 0.14)
            or 0.14
        )
        trigger = max(min_curr, mfe_pnl_pct - drawdown)
        if (
            mfe_pnl_pct >= min_mfe
            and pnl_pct <= trigger
            and take_profit_pnl_pct >= min_curr
        ):
            reason = (
                "take-profit-full"
                if getattr(SETTINGS, "force_full_exit_on_take_profit", False)
                else "take-profit-principal"
            )
            return ExitDecision(True, reason, take_profit_pnl_pct, hold_sec)

    # Soft Take Profit
    if (
        not has_taken_partial
        and take_profit_pnl_pct is not None
        and take_profit_pnl_pct >= _soft_tp
    ):
        reason = (
            "take-profit-full"
            if getattr(SETTINGS, "force_full_exit_on_take_profit", False)
            else "take-profit-partial"
        )
        return ExitDecision(True, reason, take_profit_pnl_pct, hold_sec)

    # Break-even giveback
    if bool(getattr(SETTINGS, "breakeven_giveback_enabled", True)) and not has_taken_partial:
        min_mfe = float(getattr(SETTINGS, "breakeven_giveback_min_mfe_pct", 0.10) or 0.10)
        floor = float(getattr(SETTINGS, "breakeven_giveback_floor_pct", 0.0) or 0.0)
        min_hold = float(
            getattr(SETTINGS, "breakeven_giveback_min_hold_sec", 12.0) or 12.0
        )
        min_secs = float(
            getattr(SETTINGS, "breakeven_giveback_min_secs_left", 45.0) or 45.0
        )
        if (
            mfe_pnl_pct >= min_mfe
            and pnl_pct <= floor
            and hold_sec >= min_hold
            and (secs_left is None or secs_left >= min_secs)
        ):
            return ExitDecision(True, "break-even-giveback", pnl_pct, hold_sec)

    return None


def _check_fresh_entry_state(
    pnl_pct: float,
    hold_sec: float,
    secs_left: Optional[float],
    mfe_pnl_pct: float,
    profit_pnl_pct: Optional[float] = None,
) -> Optional[ExitDecision]:
    # Failed Follow Through
    ff_window = getattr(SETTINGS, "failed_follow_through_window_sec", 45)
    ff_loss = getattr(SETTINGS, "failed_follow_through_loss_pct", 0.03)
    ff_min_secs = getattr(SETTINGS, "failed_follow_through_min_secs_left", 90)
    ff_max_mfe = getattr(SETTINGS, "failed_follow_through_max_mfe_pct", 0.02)
    
    # Check fast window FF
    if (
        profit_pnl_pct is not None
        and hold_sec >= getattr(SETTINGS, "failed_follow_through_fast_window_sec", 20)
        and profit_pnl_pct <= -ff_loss
        and (secs_left is None or secs_left >= ff_min_secs)
        and mfe_pnl_pct <= ff_max_mfe
    ):
        return ExitDecision(True, "failed-follow-through", pnl_pct, hold_sec)

    if (
        hold_sec >= ff_window
        and pnl_pct <= -ff_loss
        and (secs_left is None or secs_left >= ff_min_secs)
        and mfe_pnl_pct <= ff_max_mfe
    ):
        return ExitDecision(True, "failed-follow-through", pnl_pct, hold_sec)

    # Stalled Trade
    st_window = getattr(SETTINGS, "stalled_exit_window_sec", 35)
    st_min_secs = getattr(SETTINGS, "stalled_exit_min_secs_left", 45)
    st_min_loss = getattr(SETTINGS, "stalled_exit_min_loss_pct", 0.01)
    st_max_abs = getattr(SETTINGS, "stalled_exit_max_abs_pnl_pct", 0.02)
    st_max_mfe = getattr(SETTINGS, "stalled_exit_max_mfe_pct", 0.02)
    if hold_sec >= st_window and (secs_left is not None and secs_left >= st_min_secs):
        if (
            pnl_pct <= -st_min_loss
            and pnl_pct >= -st_max_abs
            and mfe_pnl_pct <= st_max_mfe
        ):
            return ExitDecision(True, "stalled-trade", pnl_pct, hold_sec)

    return None


def decide_exit(
    *,
    pnl_pct: float,
    profit_pnl_pct: Optional[float] = None,
    hold_sec: float,
    secs_left: Optional[float] = None,
    has_scaled_out: bool = False,
    recovery_chance_low: bool = False,
    has_scaled_out_loss: bool = False,
    has_taken_partial: bool = False,
    has_extracted_principal: bool = False,
    mfe_pnl_pct: float = 0.0,
    runner_drawdown_pct: float = 0.0,
    runner_peak_age_sec: Optional[float] = None,
    runner_peak_value_usd: float = 0.0,
) -> ExitDecision:
    # 0. VPN_EXPIRY_FIRST Short-circuit
    if SETTINGS.vpn_safe_mode and SETTINGS.vpn_expiry_first:
        # Only allow emergency or absolute deadline exits
        res_expiry = _check_expiry_hold_state(
            pnl_pct, hold_sec, secs_left, has_extracted_principal, profit_pnl_pct
        )
        if res_expiry:
            # In vpn mode, we only allow deadline exits if very close to end
            if "deadline" in res_expiry.reason:
                if secs_left is not None and secs_left <= 45.0:
                    return res_expiry
            else:
                return res_expiry

        # Allow hard stop loss only
        if pnl_pct <= -SETTINGS.stop_loss_pct:
            return ExitDecision(True, "hard-stop-loss", pnl_pct, hold_sec)
        
        # Allow max hold as a failsafe
        if hold_sec >= SETTINGS.max_hold_seconds and pnl_pct < 0:
            return ExitDecision(True, "max-hold-loss", pnl_pct, hold_sec)

        return ExitDecision(False, "", pnl_pct, hold_sec)

    # 1. EXPIRY_HOLD (Precedence)
    res_expiry = _check_expiry_hold_state(
        pnl_pct, hold_sec, secs_left, has_extracted_principal, profit_pnl_pct
    )
    if res_expiry:
        return res_expiry

    # 2. SOFT_PROFIT
    res_profit = _check_soft_profit_state(
        pnl_pct,
        profit_pnl_pct,
        hold_sec,
        secs_left,
        has_taken_partial,
        mfe_pnl_pct,
        has_extracted_principal,
    )
    if res_profit:
        return res_profit

    # 3. EMERGENCY_LOSS (Precedence)
    res_emergency = _check_emergency_loss_state(
        pnl_pct,
        hold_sec,
        has_scaled_out_loss,
        recovery_chance_low,
        has_extracted_principal,
    )
    if res_emergency:
        return res_emergency

    # 4. PRINCIPAL_EXTRACTED (Moonbags)
    if has_extracted_principal:
        moon_min_peak = getattr(SETTINGS, "moonbag_min_peak_value_usd", 0.10)
        moon_window = getattr(SETTINGS, "moonbag_drawdown_window_sec", 30)
        moon_drawdown = -getattr(SETTINGS, "moonbag_drawdown_pct", 0.30)
        if (
            runner_peak_value_usd >= moon_min_peak
            and runner_peak_age_sec is not None
            and runner_peak_age_sec <= moon_window
            and runner_drawdown_pct <= moon_drawdown
        ):
            return ExitDecision(True, "moonbag-drawdown-stop", pnl_pct, hold_sec)
        # Once principal has been recovered, the remainder is treated as a free runner.
        return ExitDecision(False, "", pnl_pct, hold_sec)

    # 5. FRESH_ENTRY
    if not has_taken_partial and not has_scaled_out_loss:
        res_fresh = _check_fresh_entry_state(pnl_pct, hold_sec, secs_left, mfe_pnl_pct, profit_pnl_pct)
        if res_fresh:
            return res_fresh

    return ExitDecision(False, "", pnl_pct, hold_sec)


def maybe_reverse_entry(
    *, signal_side: Optional[str], live_consec_losses: int, last_loss_side: str
) -> EntryDecision:
    if (
        signal_side in {"UP", "DOWN"}
        and live_consec_losses >= 2
        and last_loss_side == signal_side
    ):
        return EntryDecision("DOWN" if signal_side == "UP" else "UP", "loss-reversal")
    return EntryDecision(signal_side, "")


def should_block_same_market_reentry(
    exit_reason: str | None,
    *,
    remaining_shares: float = 0.0,
    realized_pnl_usd: Optional[float] = None,
) -> bool:
    if float(remaining_shares or 0.0) > 1e-6:
        return False

    normalized = str(exit_reason or "").strip().lower()
    if normalized in {
        "binance-adverse-exit",
        "binance-profit-protect-exit",
        "break-even-giveback",
        "deadline-exit-flat",
        "deadline-exit-loss",
        "deadline-exit-weak-win",
        "failed-follow-through",
        "hard-stop-loss",
        "max-hold-loss",
        "max-hold-loss-extended",
        "lottery-plateau-stop",
        "moonbag-drawdown-stop",
        "post-scaleout-stop-loss",
        "profit-reversal-stop",
        "residual-force-close",
        "smart-stop-loss",
        "stalled-trade",
        "stop-loss",
        "stop-loss-full",
        "stop-loss-scale-out",
        "take-profit-full",
        "take-profit-partial",
        "take-profit-principal",
        "take-profit-principal-partial",
    }:
        return True

    if realized_pnl_usd is not None and float(realized_pnl_usd) < 0.0:
        return True
    return False


def can_reenter_same_market(
    *,
    has_current_market_pos: bool,
    closed_any: bool,
    secs_left: Optional[float],
    current_market_slug: str = "",
    blocked_market_slug: str = "",
) -> bool:
    min_secs_left = float(getattr(SETTINGS, "same_market_reentry_min_secs_left", 60))
    if has_current_market_pos or secs_left is None or secs_left < min_secs_left:
        return False
    normalized_current_slug = str(current_market_slug or "").strip()
    normalized_blocked_slug = str(blocked_market_slug or "").strip()
    if (
        normalized_current_slug
        and normalized_blocked_slug
        and normalized_current_slug == normalized_blocked_slug
    ):
        return False
    return bool(closed_any or not normalized_blocked_slug)
