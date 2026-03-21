from dataclasses import dataclass
from typing import Optional


@dataclass
class HedgeState:
    active: bool = False
    market_slug: str = ""
    leg1_side: str = ""
    leg1_price: float = 0.0
    leg1_ts: float = 0.0


def opposite(side: str) -> str:
    return "DOWN" if side == "UP" else "UP"


def should_trigger_dump(prev_up: Optional[float], prev_down: Optional[float], up: Optional[float], down: Optional[float], move_threshold: float) -> Optional[str]:
    if prev_up is not None and up is not None and prev_up > 0:
        if (prev_up - up) / prev_up >= move_threshold:
            return "UP"
    if prev_down is not None and down is not None and prev_down > 0:
        if (prev_down - down) / prev_down >= move_threshold:
            return "DOWN"
    return None
