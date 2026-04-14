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
    fair_value: float = 0.5,
    side: str = "UP",
    ob_bids: list = None,
    shares: float = 0.0,
) -> ExitDecision:
    """
    EV-Aware Exit Logic for 15m markets.
    Compares Theoretical EV (Expiry) vs. Real-world Executable Maker Exit.
    """
    # 1. 災難性止損
    catastrophic_stop = -abs(float(getattr(SETTINGS, "catastrophic_stop_loss_pct", 0.30)))
    if pnl_pct <= catastrophic_stop:
        return ExitDecision(True, "catastrophic-reversal-stop", pnl_pct, hold_sec)

    # 2. 確定性持倉 (Expiry-First Certainty Hold)
    pos_fv = fair_value if side == "UP" else (1.0 - fair_value)
    
    # 如果快到期，判斷是否值得提前平倉
    if secs_left is not None and secs_left <= 60.0:
        # 取得當前可執行的 Maker Exit 價格 (Best Bid)
        best_bid = float(ob_bids[0]['price']) if ob_bids else 0.0
        
        # 如果當前市價價格 > 理論 EV (Retail FOMO)，提前平倉鎖定利潤
        # 加入小幅 buffer (0.01) 避免頻繁抖動
        if best_bid > (pos_fv + 0.01) and pnl_pct > 0.05:
            return ExitDecision(True, f"early-exit-fomo-premium (bid={best_bid:.3f} > ev={pos_fv:.3f})", pnl_pct, hold_sec)

        # 在最後 15 秒，如果勝率不夠高 (>95%)，執行 Taker 清理
        if secs_left <= 15.0:
            if pos_fv >= 0.95:
                return ExitDecision(False, "sniper-hold-to-settle-lock", pnl_pct, hold_sec)
            return ExitDecision(True, "deadline-final-exit", pnl_pct, hold_sec)

    # 3. 預設：持有到期 (Max realization of edge)
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
    """
    Enforces the strict 15M Maker VPN exit rules.
    """
    return _decide_exit_15m(
        pnl_pct=pnl_pct,
        hold_sec=hold_sec,
        secs_left=secs_left,
        fair_value=fair_value,
        side=side,
        ob_bids=ob_bids,
        shares=shares,
    )


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
