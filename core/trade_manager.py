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
    take_profit_pnl_pct = None if profit_pnl_pct is None else float(profit_pnl_pct)
    ghost_town_sec = float(getattr(SETTINGS, "exit_ghost_town_sec", 30) or 0.0)
    profit_deadline_sec = float(getattr(SETTINGS, "exit_deadline_profit_sec", 45) or 0.0)
    inside_ghost_town_window = (
        secs_left is not None
        and ghost_town_sec > 0.0
        and secs_left <= ghost_town_sec
    )

    # LAST 30 SECONDS LOCK:
    # 一旦進到最後 30 秒內（<=30），不管賺錢、虧錢、平盤都 let ride。
    # 45~30 秒之間若有獲利，再交給後面的 deadline take-profit 規則處理。
    if inside_ghost_town_window:
        return ExitDecision(False, "ghost-town-let-ride", pnl_pct, hold_sec)

    # Mark-price fallback take-profit:
    # 當 bid 可成交報酬 (profit_pnl_pct) 因流動性折扣略低於停利門檻，
    # 但帳面 mark 報酬 (pnl_pct) 已明顯超過門檻（加上 bid-discount buffer），
    # 就以 mark 報酬為依據觸發停利，避免薄書把已大幅獲利的單拖到錯過出場。
    # 這個 fallback 仍要求存在可執行的正報酬，而且 bid 只能低於 soft tp 一小段緩衝，
    # 不能在完全沒有 executable profit signal 時就直接停利。
    _soft_tp = float(SETTINGS.take_profit_soft_pct)
    _bid_discount_buffer = float(SETTINGS.take_profit_bid_discount_buffer)
    _mark_tp_threshold = _soft_tp + _bid_discount_buffer  # 例如 30% + 8% = mark 要達到 38% 才觸發 fallback
    _min_exec_fallback_pct = max(0.0, _soft_tp - _bid_discount_buffer)
    _fallback_eps = 1e-9
    if (
        not has_taken_partial
        and not has_extracted_principal
        and pnl_pct >= _mark_tp_threshold  # mark 已明顯大幅超過停利門檻
        and take_profit_pnl_pct is not None
        and (_min_exec_fallback_pct + _fallback_eps) <= take_profit_pnl_pct < _soft_tp
    ):
        # 用 mark 報酬觸發停利，但只接受「接近 soft tp、只是被 bid 折價壓住」的情況。
        if getattr(SETTINGS, "force_full_exit_on_take_profit", False):
            return ExitDecision(True, "take-profit-full", pnl_pct, hold_sec)
        return ExitDecision(True, "take-profit-partial", pnl_pct, hold_sec)

    # 1. Tiered Take Profit (Risk-Free Moonbag Strategy)
    if (
        take_profit_pnl_pct is not None
        and not has_extracted_principal
        and take_profit_pnl_pct >= getattr(SETTINGS, "take_profit_hard_pct", 0.50)
    ):
        if getattr(SETTINGS, "force_full_exit_on_take_profit", False):
            return ExitDecision(True, "take-profit-full", take_profit_pnl_pct, hold_sec)
        # Sell enough to recover principal -> guaranteed risk-free
        return ExitDecision(True, "take-profit-principal", take_profit_pnl_pct, hold_sec)

    if (
        bool(getattr(SETTINGS, "take_profit_principal_after_partial_enabled", True))
        and has_taken_partial
        and not has_extracted_principal
        and take_profit_pnl_pct is not None
    ):
        partial_runner_min_mfe_pct = float(
            getattr(SETTINGS, "take_profit_principal_after_partial_min_mfe_pct", 0.24) or 0.24
        )
        partial_runner_drawdown_pct = float(
            getattr(SETTINGS, "take_profit_principal_after_partial_drawdown_pct", 0.08) or 0.08
        )
        partial_runner_min_current_pct = float(
            getattr(SETTINGS, "take_profit_principal_after_partial_min_current_pct", 0.14) or 0.14
        )
        partial_runner_giveback_trigger_pct = max(
            partial_runner_min_current_pct,
            mfe_pnl_pct - partial_runner_drawdown_pct,
        )
        if (
            mfe_pnl_pct >= partial_runner_min_mfe_pct
            and pnl_pct <= partial_runner_giveback_trigger_pct
            and take_profit_pnl_pct >= partial_runner_min_current_pct
        ):
            if getattr(SETTINGS, "force_full_exit_on_take_profit", False):
                return ExitDecision(True, "take-profit-full", take_profit_pnl_pct, hold_sec)
            # After a partial clip, a runner that has already shown real life should not
            # be allowed to give back most of its gains before principal is secured.
            return ExitDecision(True, "take-profit-principal", take_profit_pnl_pct, hold_sec)
        
    if (
        take_profit_pnl_pct is not None
        and not has_taken_partial
        and not has_extracted_principal
        and take_profit_pnl_pct >= getattr(SETTINGS, "take_profit_soft_pct", 0.30)
    ):
        if getattr(SETTINGS, "force_full_exit_on_take_profit", False):
            return ExitDecision(True, "take-profit-full", take_profit_pnl_pct, hold_sec)
        # Sell the configured first clip to lock in gains and cut exposure.
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
        bool(getattr(SETTINGS, "breakeven_giveback_enabled", True))
        and not has_taken_partial
        and not has_extracted_principal
        and mfe_pnl_pct >= float(getattr(SETTINGS, "breakeven_giveback_min_mfe_pct", 0.10) or 0.10)
        and pnl_pct <= float(getattr(SETTINGS, "breakeven_giveback_floor_pct", 0.0) or 0.0)
        and hold_sec >= float(getattr(SETTINGS, "breakeven_giveback_min_hold_sec", 12.0) or 12.0)
        and (
            secs_left is None
            or secs_left >= float(getattr(SETTINGS, "breakeven_giveback_min_secs_left", 45.0) or 45.0)
        )
    ):
        return ExitDecision(True, "break-even-giveback", pnl_pct, hold_sec)

    if (
        hold_sec >= getattr(SETTINGS, "failed_follow_through_window_sec", 45)
        and pnl_pct <= -getattr(SETTINGS, "failed_follow_through_loss_pct", 0.03)
        and secs_left is not None
        and secs_left >= getattr(SETTINGS, "failed_follow_through_min_secs_left", 90)
        and mfe_pnl_pct <= getattr(SETTINGS, "failed_follow_through_max_mfe_pct", 0.02)
        and not has_taken_partial
        and not has_scaled_out_loss
        and not has_extracted_principal
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
        and recovery_chance_low
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
    if secs_left is not None and profit_deadline_sec > 0.0 and secs_left <= profit_deadline_sec:
        if pnl_pct > 0:
            return ExitDecision(True, "deadline-take-profit-full", pnl_pct, hold_sec)

    if secs_left is not None and secs_left <= getattr(SETTINGS, "exit_deadline_sec", 20):
        if pnl_pct < 0:
            return ExitDecision(True, "deadline-exit-loss", pnl_pct, hold_sec)
        if (
            not has_extracted_principal
            and pnl_pct <= getattr(SETTINGS, "exit_deadline_flat_pnl_pct", 0.0)
        ):
            if profit_pnl_pct is not None and profit_pnl_pct < 0:
                return ExitDecision(False, "", pnl_pct, hold_sec)
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
            if profit_pnl_pct is not None and profit_pnl_pct < 0:
                return ExitDecision(False, "", pnl_pct, hold_sec)
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
    if normalized_current_slug and normalized_blocked_slug and normalized_current_slug == normalized_blocked_slug:
        return False
    return bool(closed_any or not normalized_blocked_slug)
