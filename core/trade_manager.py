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
    take_profit_pnl_pct = float(pnl_pct if profit_pnl_pct is None else max(profit_pnl_pct, pnl_pct))
    # 1. Tiered Take Profit (Risk-Free Moonbag Strategy)
    if not has_extracted_principal and take_profit_pnl_pct >= getattr(SETTINGS, "take_profit_hard_pct", 0.50):
        if getattr(SETTINGS, "force_full_exit_on_take_profit", False):
            return ExitDecision(True, "take-profit-full", take_profit_pnl_pct, hold_sec)
        # Sell enough to recover principal -> guaranteed risk-free
        return ExitDecision(True, "take-profit-principal", take_profit_pnl_pct, hold_sec)
        
    if not has_taken_partial and not has_extracted_principal and take_profit_pnl_pct >= getattr(SETTINGS, "take_profit_soft_pct", 0.30):
        if getattr(SETTINGS, "force_full_exit_on_take_profit", False):
            return ExitDecision(True, "take-profit-full", take_profit_pnl_pct, hold_sec)
        # Sell 30% to lock in early profit and reduce anxiety
        return ExitDecision(True, "take-profit-partial", take_profit_pnl_pct, hold_sec)

    if has_extracted_principal:
        if (
            runner_peak_value_usd >= getattr(SETTINGS, "moonbag_min_peak_value_usd", 0.10)
            and runner_peak_age_sec is not None
            and runner_peak_age_sec <= getattr(SETTINGS, "moonbag_drawdown_window_sec", 30)
            and runner_drawdown_pct <= -getattr(SETTINGS, "moonbag_drawdown_pct", 0.30)
        ):
            return ExitDecision(True, "moonbag-drawdown-stop", pnl_pct, hold_sec)
        # Once principal has been recovered, the remainder is treated as a free runner.
        # Let it ride until the market resolves; manual claim / redeem can happen later.
        return ExitDecision(False, "", pnl_pct, hold_sec)

    if (
        hold_sec >= getattr(SETTINGS, "failed_follow_through_window_sec", 45)
        and pnl_pct <= -getattr(SETTINGS, "failed_follow_through_loss_pct", 0.03)
        and secs_left is not None
        and secs_left >= getattr(SETTINGS, "failed_follow_through_min_secs_left", 90)
        and mfe_pnl_pct <= getattr(SETTINGS, "failed_follow_through_max_mfe_pct", 0.02)
    ):
        return ExitDecision(True, "failed-follow-through", pnl_pct, hold_sec)

    # Dead trades that never developed are better recycled while there is still
    # enough clock to earn a fresh signal in the same market.
    if (
        hold_sec >= getattr(SETTINGS, "stalled_exit_window_sec", 35)
        and secs_left is not None
        and secs_left >= getattr(SETTINGS, "stalled_exit_min_secs_left", 45)
        and not has_extracted_principal
        and pnl_pct <= -getattr(SETTINGS, "stalled_exit_min_loss_pct", 0.01)
        and pnl_pct >= -getattr(SETTINGS, "stalled_exit_max_abs_pnl_pct", 0.02)
        and mfe_pnl_pct <= getattr(SETTINGS, "stalled_exit_max_mfe_pct", 0.02)
    ):
        return ExitDecision(True, "stalled-trade", pnl_pct, hold_sec)

    if (
        has_scaled_out_loss
        and hold_sec >= getattr(SETTINGS, "post_scaleout_loss_exit_delay_sec", 20)
        and pnl_pct <= -getattr(
            SETTINGS,
            "post_scaleout_loss_exit_pct",
            getattr(SETTINGS, "stop_loss_warn_pct", 0.08),
        )
    ):
        return ExitDecision(True, "post-scaleout-stop-loss", pnl_pct, hold_sec)

    # 2. Stop Loss Handling
    if getattr(SETTINGS, "smart_stop_loss_enabled", False):
        if pnl_pct <= -SETTINGS.stop_loss_pct:
            return ExitDecision(True, "hard-stop-loss", pnl_pct, hold_sec)
        if pnl_pct <= -getattr(SETTINGS, "stop_loss_warn_pct", 0.08) and recovery_chance_low:
            return ExitDecision(True, "smart-stop-loss", pnl_pct, hold_sec)
        if not has_scaled_out_loss and pnl_pct <= -getattr(SETTINGS, "stop_loss_partial_pct", 0.05):
            if getattr(SETTINGS, "force_full_exit_on_stop_loss_scaleout", False):
                return ExitDecision(True, "stop-loss-full", pnl_pct, hold_sec)
            return ExitDecision(True, "stop-loss-scale-out", pnl_pct, hold_sec)
    else:
        if pnl_pct <= -SETTINGS.stop_loss_pct:
            return ExitDecision(True, "stop-loss", pnl_pct, hold_sec)
        if not has_scaled_out_loss and pnl_pct <= -getattr(SETTINGS, "stop_loss_partial_pct", 0.05):
            if getattr(SETTINGS, "force_full_exit_on_stop_loss_scaleout", False):
                return ExitDecision(True, "stop-loss-full", pnl_pct, hold_sec)
            return ExitDecision(True, "stop-loss-scale-out", pnl_pct, hold_sec)

    if secs_left is not None and secs_left <= getattr(SETTINGS, "exit_deadline_sec", 20):
        if pnl_pct < 0:
            return ExitDecision(True, "deadline-exit-loss", pnl_pct, hold_sec)
        if (
            not has_extracted_principal
            and pnl_pct <= getattr(SETTINGS, "exit_deadline_flat_pnl_pct", 0.0)
        ):
            # If the trade never became meaningfully profitable, don't let a near-expiry
            # binary position drift into a full settlement loss just because the mark stayed flat.
            return ExitDecision(True, "deadline-exit-flat", pnl_pct, hold_sec)
        if (
            not has_extracted_principal
            and pnl_pct < getattr(
                SETTINGS,
                "exit_deadline_min_safe_profit_pct",
                getattr(SETTINGS, "take_profit_soft_pct", 0.30),
            )
        ):
            # Near expiry, a small green mark is still fragile on a binary contract.
            # If we have not actually recovered principal yet, take the weak win and leave.
            return ExitDecision(True, "deadline-exit-weak-win", pnl_pct, hold_sec)

    if hold_sec >= SETTINGS.max_hold_seconds and pnl_pct < 0:
        if getattr(SETTINGS, "smart_stop_loss_enabled", False) and not recovery_chance_low:
            if hold_sec >= SETTINGS.max_hold_seconds * 2:
                return ExitDecision(True, "max-hold-loss-extended", pnl_pct, hold_sec)
        else:
            return ExitDecision(True, "max-hold-loss", pnl_pct, hold_sec)

    return ExitDecision(False, "", pnl_pct, hold_sec)


def maybe_reverse_entry(*, signal_side: Optional[str], live_consec_losses: int, last_loss_side: str) -> EntryDecision:
    if signal_side in {"UP", "DOWN"} and live_consec_losses >= 2 and last_loss_side == signal_side:
        return EntryDecision("DOWN" if signal_side == "UP" else "UP", "loss-reversal")
    return EntryDecision(signal_side, "")


def should_block_same_market_reentry(
    exit_reason: str | None,
    *,
    remaining_shares: float = 0.0,
    realized_pnl_usd: Optional[float] = None,
) -> bool:
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
    return bool(closed_any and (not has_current_market_pos) and secs_left is not None and secs_left >= min_secs_left)
