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


def _decide_exit_15m(
    *,
    pnl_pct: float,
    hold_sec: float,
    secs_left: Optional[float] = None,
) -> ExitDecision:
    """
    Refactored Phase-1 exit logic for 15m markets.
    Focuses strictly on preserving edge by holding to expiry.
    Exits only on Hard Stop Loss or Expiry Deadlines.
    """
    # 1. Hard Stop Loss (Absolute Safety)
    if pnl_pct <= -SETTINGS.stop_loss_pct:
        return ExitDecision(True, "hard-stop-loss", pnl_pct, hold_sec)

    # 2. Hold to Expiry / Deadline Exit
    if secs_left is not None:
        # Absolute deadline exit (Close just before expiry to ensure settlement)
        exit_deadline = float(getattr(SETTINGS, "exit_deadline_sec", 15.0))
        if secs_left <= exit_deadline:
            if pnl_pct <= 0:
                return ExitDecision(True, "deadline-exit-loss", pnl_pct, hold_sec)
            return ExitDecision(True, "deadline-exit-win", pnl_pct, hold_sec)

    # 3. Max Hold Failsafe
    if hold_sec >= SETTINGS.max_hold_seconds and pnl_pct < 0:
        return ExitDecision(True, "max-hold-loss", pnl_pct, hold_sec)

    return ExitDecision(False, "hold", pnl_pct, hold_sec)


def decide_exit(
    *,
    pnl_pct: float,
    hold_sec: float,
    secs_left: Optional[float] = None,
) -> ExitDecision:
    # 0. 15m Default Path (Execution-First: Hold to Expiry)
    if SETTINGS.market_profile == "btc_15m":
        return _decide_exit_15m(
            pnl_pct=pnl_pct,
            hold_sec=hold_sec,
            secs_left=secs_left,
        )

    # Legacy 5m / Non-15m Path (Maintained for safety, but simplified)
    # 1. Hard Stop Loss (Absolute Safety)
    if pnl_pct <= -SETTINGS.stop_loss_pct:
        return ExitDecision(True, "hard-stop-loss", pnl_pct, hold_sec)

    # 2. Hold to Expiry / Deadline Exit
    if secs_left is not None:
        exit_deadline = float(getattr(SETTINGS, "exit_deadline_sec", 15.0))
        if secs_left <= exit_deadline:
            if pnl_pct <= 0:
                return ExitDecision(True, "deadline-exit-loss", pnl_pct, hold_sec)
            return ExitDecision(True, "deadline-exit-win", pnl_pct, hold_sec)

    # 3. Max Hold Failsafe
    if hold_sec >= SETTINGS.max_hold_seconds and pnl_pct < 0:
        return ExitDecision(True, "max-hold-loss", pnl_pct, hold_sec)

    return ExitDecision(False, "hold", pnl_pct, hold_sec)


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
