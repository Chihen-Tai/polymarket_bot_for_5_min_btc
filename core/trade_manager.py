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
    fair_value: float = 0.5, # 傳入當前的公平價值 (YES)
    side: str = "UP",
    ob_bids: list = None,
    shares: float = 0.0,
) -> ExitDecision:
    """
    15m 執行優先出場邏輯。
    原則：除非有緊急危險或提前平倉的 EV 顯著優於持有到期，否則持有。
    """
    # 1. 強制止損 (保護生存)
    if pnl_pct <= -SETTINGS.stop_loss_pct:
        return ExitDecision(True, "hard-stop-loss", pnl_pct, hold_sec)

    # 2. Expiry-First Certainty Hold
    # 如果剩餘時間極短且勝率極高，即使達到 deadline 也優先持有到期。
    pos_fv = fair_value if side == "UP" else (1.0 - fair_value)
    
    if bool(getattr(SETTINGS, "expiry_first_certainty_hold_enabled", True)):
        max_secs = float(getattr(SETTINGS, "expiry_first_hold_max_secs_left", 10.0))
        min_fv = float(getattr(SETTINGS, "expiry_first_hold_min_fair_value", 0.92))
        if secs_left is not None and secs_left <= max_secs:
            if pos_fv >= min_fv:
                return ExitDecision(False, "expiry-first-certainty-hold", pnl_pct, hold_sec)

    # 3. 到期前強制清理 (避開結算不確定性)
    if secs_left is not None and secs_left <= 15.0:
        reason = "deadline-exit-loss" if pnl_pct <= 0 else "deadline-exit-win"
        return ExitDecision(True, reason, pnl_pct, hold_sec)

    # 4. EV 比較：持有到期 vs. 提前平倉
    if ob_bids and shares > 0:
        from core.execution_engine import get_vwap_from_ladder
        # 真實可執行出場價格 (VWAP based on actual shares)
        executable_bid = get_vwap_from_ladder(ob_bids, shares * 0.5) 
        taker_fee = 0.0156
        
        # 提前平倉的淨價值 (扣除 Taker 費)
        ev_sell = executable_bid * (1.0 - taker_fee)
        # 持有到期的價值 (假設 0 費用)
        ev_hold = pos_fv
        
        # 只有當提前賣出的價值比持有到期高出指定邊際時，才平倉
        min_advantage = float(getattr(SETTINGS, "strategic_exit_min_ev_advantage", 0.03))
        if ev_sell > (ev_hold + min_advantage):
            return ExitDecision(True, "strategic-take-profit", pnl_pct, hold_sec)

    return ExitDecision(False, "hold", pnl_pct, hold_sec)


def decide_exit(
    *,
    pnl_pct: float,
    hold_sec: float,
    secs_left: Optional[float] = None,
    fair_value: float = 0.5,
    side: str = "UP",
    ob_bids: list = None,
    shares: float = 0.0,
) -> ExitDecision:
    # 0. 15m Default Path (Execution-First: Expiry Priority)
    if SETTINGS.market_profile == "btc_15m":
        return _decide_exit_15m(
            pnl_pct=pnl_pct,
            hold_sec=hold_sec,
            secs_left=secs_left,
            fair_value=fair_value,
            side=side,
            ob_bids=ob_bids,
            shares=shares,
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
    """
    Classifies exit reasons to determine if same-market reentry should be blocked.
    """
    if float(remaining_shares or 0.0) > 1e-6:
        return False

    normalized = str(exit_reason or "").strip().lower()
    
    # Category A: Hard Block (Losses, Defensive exits, Failures)
    hard_block_reasons = {
        "hard-stop-loss",
        "stop-loss",
        "stop-loss-full",
        "stop-loss-scale-out",
        "failed-follow-through",
        "stalled-trade",
        "deadline-exit-loss",
        "residual-force-close",
        "post-scaleout-stop-loss",
        "max-hold-loss",
        "max-hold-loss-extended",
        "binance-adverse-exit",
        "moonbag-drawdown-stop",
    }
    
    if normalized in hard_block_reasons:
        return True

    # If we realized a net loss, block reentry to prevent revenge trading
    if realized_pnl_usd is not None and float(realized_pnl_usd) < -0.01:
        return True

    # Category B & C: Profits and Benign outcomes do NOT block reentry.
    # Examples: deadline-exit-win, strategic-take-profit, signal-but-no-fill, etc.
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
