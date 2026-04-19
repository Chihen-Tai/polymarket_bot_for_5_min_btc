from dataclasses import dataclass
from typing import Optional


@dataclass
class HedgeState:
    active: bool = False
    market_slug: str = ""
    leg1_side: str = ""
    leg1_price: float = 0.0
    leg1_ts: float = 0.0


@dataclass
class StructuredHedgeDecision:
    entry_allowed: bool = True
    should_place_hedge: bool = False
    hedge_size_usd: float = 0.0
    reason: str = ""


def opposite(side: str) -> str:
    return "DOWN" if side == "UP" else "UP"


def plan_structured_hedge_entry(
    *,
    cash_balance_usd: float,
    primary_order_usd: float,
    hedge_ratio: float,
    reserve_usd: float,
    min_order_usd: float,
    low_cash_policy: str,
) -> StructuredHedgeDecision:
    planned_hedge_usd = max(0.0, float(primary_order_usd or 0.0) * float(hedge_ratio or 0.0))
    if planned_hedge_usd <= 0.0:
        return StructuredHedgeDecision(
            entry_allowed=True,
            should_place_hedge=False,
            hedge_size_usd=0.0,
            reason="hedge_disabled",
        )

    required_cash = (2.0 * float(min_order_usd or 0.0)) + (2.0 * float(reserve_usd or 0.0))
    if float(cash_balance_usd or 0.0) + 1e-9 < required_cash:
        policy = str(low_cash_policy or "skip_entry").strip().lower()
        if policy == "primary_only":
            return StructuredHedgeDecision(
                entry_allowed=True,
                should_place_hedge=False,
                hedge_size_usd=0.0,
                reason="hedge_disabled_low_cash_policy",
            )
        return StructuredHedgeDecision(
            entry_allowed=False,
            should_place_hedge=False,
            hedge_size_usd=0.0,
            reason="entry_blocked_low_cash_for_hedge",
        )

    if planned_hedge_usd + 1e-9 < float(min_order_usd or 0.0):
        return StructuredHedgeDecision(
            entry_allowed=True,
            should_place_hedge=False,
            hedge_size_usd=0.0,
            reason="hedge_skipped_below_min_order",
        )

    return StructuredHedgeDecision(
        entry_allowed=True,
        should_place_hedge=True,
        hedge_size_usd=planned_hedge_usd,
        reason="hedge_planned",
    )


def finalize_structured_hedge_after_fill(
    *,
    cash_balance_usd: float,
    primary_fill_cost_usd: float,
    planned_hedge_usd: float,
    reserve_usd: float,
    min_order_usd: float,
    primary_filled: bool,
) -> StructuredHedgeDecision:
    if not primary_filled:
        return StructuredHedgeDecision(
            entry_allowed=True,
            should_place_hedge=False,
            hedge_size_usd=0.0,
            reason="hedge_waiting_for_primary_fill",
        )

    max_affordable_hedge = max(
        0.0,
        float(cash_balance_usd or 0.0)
        - float(primary_fill_cost_usd or 0.0)
        - float(reserve_usd or 0.0),
    )
    capped_hedge_usd = min(max(0.0, float(planned_hedge_usd or 0.0)), max_affordable_hedge)

    if capped_hedge_usd + 1e-9 < float(min_order_usd or 0.0):
        return StructuredHedgeDecision(
            entry_allowed=True,
            should_place_hedge=False,
            hedge_size_usd=0.0,
            reason="hedge_skipped_insufficient_capital",
        )

    if capped_hedge_usd + 1e-9 < float(planned_hedge_usd or 0.0):
        return StructuredHedgeDecision(
            entry_allowed=True,
            should_place_hedge=True,
            hedge_size_usd=capped_hedge_usd,
            reason="hedge_capped_to_available_cash",
        )

    return StructuredHedgeDecision(
        entry_allowed=True,
        should_place_hedge=True,
        hedge_size_usd=capped_hedge_usd,
        reason="hedge_planned",
    )


def should_trigger_dump(prev_up: Optional[float], prev_down: Optional[float], up: Optional[float], down: Optional[float], move_threshold: float) -> Optional[str]:
    if prev_up is not None and up is not None and prev_up > 0:
        if (prev_up - up) / prev_up >= move_threshold:
            return "UP"
    if prev_down is not None and down is not None and prev_down > 0:
        if (prev_down - down) / prev_down >= move_threshold:
            return "DOWN"
    return None
