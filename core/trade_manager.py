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


def decide_exit(*, pnl_pct: float, hold_sec: float, secs_left: Optional[float] = None, has_scaled_out: bool = False, recovery_chance_low: bool = False, has_scaled_out_loss: bool = False, has_taken_partial: bool = False, has_extracted_principal: bool = False) -> ExitDecision:
    # 1. Tiered Take Profit (Risk-Free Moonbag Strategy)
    if not has_extracted_principal and pnl_pct >= getattr(SETTINGS, "take_profit_hard_pct", 0.50):
        # Sell enough to recover principal -> guaranteed risk-free
        return ExitDecision(True, "take-profit-principal", pnl_pct, hold_sec)
        
    if not has_taken_partial and not has_extracted_principal and pnl_pct >= getattr(SETTINGS, "take_profit_soft_pct", 0.30):
        # Sell 30% to lock in early profit and reduce anxiety
        return ExitDecision(True, "take-profit-partial", pnl_pct, hold_sec)

    # 2. Stop Loss Handling
    if getattr(SETTINGS, "smart_stop_loss_enabled", False):
        if pnl_pct <= -(SETTINGS.stop_loss_pct + 0.15):
            return ExitDecision(True, "hard-stop-loss", pnl_pct, hold_sec)
        if pnl_pct <= -SETTINGS.stop_loss_pct and recovery_chance_low:
            return ExitDecision(True, "smart-stop-loss", pnl_pct, hold_sec)
        if not has_scaled_out_loss and pnl_pct <= -getattr(SETTINGS, "stop_loss_partial_pct", 0.20):
            return ExitDecision(True, "stop-loss-scale-out", pnl_pct, hold_sec)
    else:
        if pnl_pct <= -SETTINGS.stop_loss_pct:
            return ExitDecision(True, "stop-loss", pnl_pct, hold_sec)
        if not has_scaled_out_loss and pnl_pct <= -getattr(SETTINGS, "stop_loss_partial_pct", 0.20):
            return ExitDecision(True, "stop-loss-scale-out", pnl_pct, hold_sec)

    if secs_left is not None and secs_left <= getattr(SETTINGS, "exit_deadline_sec", 20) and pnl_pct < 0:
        return ExitDecision(True, "deadline-exit-loss", pnl_pct, hold_sec)

    if hold_sec >= SETTINGS.max_hold_seconds and pnl_pct < 0:
        if getattr(SETTINGS, "smart_stop_loss_enabled", False) and not recovery_chance_low:
            if hold_sec >= SETTINGS.max_hold_seconds * 2:
                return ExitDecision(True, "max-hold-loss-extended", pnl_pct, hold_sec)
        else:
            return ExitDecision(True, "max-hold-loss", pnl_pct, hold_sec)

    return ExitDecision(False, "", pnl_pct, hold_sec)


def maybe_reverse_entry(*, signal_side: Optional[str], live_consec_losses: int, last_loss_side: str) -> EntryDecision:
    if signal_side == "DOWN" and live_consec_losses >= 2 and last_loss_side == "DOWN":
        return EntryDecision("UP", "loss-reversal")
    return EntryDecision(signal_side, "")


def can_reenter_same_market(*, has_current_market_pos: bool, closed_any: bool, secs_left: Optional[float]) -> bool:
    return bool(closed_any and (not has_current_market_pos) and secs_left is not None and secs_left >= 60)
