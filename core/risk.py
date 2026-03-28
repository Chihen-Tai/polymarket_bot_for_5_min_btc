from dataclasses import dataclass
from datetime import datetime


@dataclass
class RiskState:
    consec_losses: int = 0
    daily_pnl: float = 0.0
    orders_this_window: int = 0
    window_key: str = ""


def current_5min_key(now: datetime) -> str:
    bucket = now.minute // 5
    return f"{now.date()}-{now.hour:02d}-{bucket}"


def update_window(state: RiskState, key: str):
    if state.window_key != key:
        state.window_key = key
        state.orders_this_window = 0


def can_place_order(
    *,
    equity: float,
    open_exposure: float,
    order_usd: float,
    min_equity: float,
    max_exposure_usd: float,
    max_orders_per_5min: int,
    consec_losses: int,
    max_consec_loss: int,
    daily_pnl: float,
    daily_max_loss: float,
    orders_this_window: int,
    current_ofi: float = 0.0,
    ofi_bypass_threshold: float = 0.65,
) -> tuple[bool, str]:
    if equity < min_equity:
        return False, f"equity {equity:.2f} < min {min_equity:.2f}"

    # 最壞情境：這筆單全損
    if (equity - order_usd) < min_equity:
        return False, "hard floor breach after worst-case loss"

    if (open_exposure + order_usd) > max_exposure_usd:
        return False, "exposure limit exceeded"

    if orders_this_window >= max_orders_per_5min:
        return False, "order frequency limit exceeded"

    if consec_losses >= max_consec_loss:
        return False, "consecutive loss circuit breaker"

    # Daily max loss check restored
    if daily_pnl <= -daily_max_loss:
        return False, "daily max loss reached"

    return True, "ok"
