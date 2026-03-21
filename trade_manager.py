from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import SETTINGS


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


def decide_exit(*, pnl_pct: float, hold_sec: float, secs_left: Optional[float] = None, has_scaled_out: bool = False) -> ExitDecision:
    if pnl_pct <= -SETTINGS.stop_loss_pct:
        return ExitDecision(True, "stop-loss", pnl_pct, hold_sec)

    if secs_left is not None and secs_left <= getattr(SETTINGS, "exit_deadline_sec", 20) and pnl_pct < 0:
        return ExitDecision(True, "deadline-exit-loss", pnl_pct, hold_sec)

    if hold_sec >= SETTINGS.max_hold_seconds and pnl_pct < 0:
        return ExitDecision(True, "max-hold-loss", pnl_pct, hold_sec)

    return ExitDecision(False, "", pnl_pct, hold_sec)


def maybe_reverse_entry(*, signal_side: Optional[str], live_consec_losses: int, last_loss_side: str) -> EntryDecision:
    if signal_side == "DOWN" and live_consec_losses >= 2 and last_loss_side == "DOWN":
        return EntryDecision("UP", "loss-reversal")
    return EntryDecision(signal_side, "")


def can_reenter_same_market(*, has_current_market_pos: bool, closed_any: bool, secs_left: Optional[float]) -> bool:
    return bool(closed_any and (not has_current_market_pos) and secs_left is not None and secs_left >= 60)
