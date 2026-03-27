from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Signal:
    side: str  # "UP" or "DOWN"
    confidence: float


def simple_5min_momentum(price_now: float, price_prev: float) -> Signal | None:
    """
    極簡策略（示範）：
    - 價格下跌 -> DOWN
    - 價格上漲 -> UP
    - 幅度太小就不做
    """
    if price_prev <= 0:
        return None

    change = (price_now - price_prev) / price_prev

    if abs(change) < 0.0005:  # 0.05% 內不做
        return None

    if change > 0:
        return Signal(side="UP", confidence=min(abs(change) * 100, 1.0))

    return Signal(side="DOWN", confidence=min(abs(change) * 100, 1.0))
