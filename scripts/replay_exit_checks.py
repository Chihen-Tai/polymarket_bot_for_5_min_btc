from dataclasses import dataclass

from core.trade_manager import decide_exit


@dataclass
class Scenario:
    name: str
    entry_price: float
    side: str
    marks: list[tuple[int, float]]  # (hold_sec, market_price)


def pnl_pct(entry_price: float, mark: float) -> float:
    shares = 1.0 / entry_price
    value = shares * mark
    return value - 1.0


def run_scenario(s: Scenario):
    print(f"\n=== {s.name} ({s.side} @ {s.entry_price}) ===")
    for hold_sec, mark in s.marks:
        pnl = pnl_pct(s.entry_price, mark)
        decision = decide_exit(pnl_pct=pnl, hold_sec=hold_sec)
        print(
            f"t={hold_sec:>3}s mark={mark:.3f} pnl={pnl:+.2%} "
            f"-> {'EXIT:'+decision.reason if decision.should_close else 'HOLD'}"
        )
        if decision.should_close:
            break


def main():
    scenarios = [
        Scenario(
            name="fast loss should stop out",
            entry_price=0.63,
            side="DOWN",
            marks=[(5, 0.58), (12, 0.48), (20, 0.40)],
        ),
        Scenario(
            name="soft take profit after 20s",
            entry_price=0.34,
            side="UP",
            marks=[(5, 0.42), (15, 0.48), (22, 0.53)],
        ),
        Scenario(
            name="hard take profit immediately",
            entry_price=0.12,
            side="DOWN",
            marks=[(8, 0.27)],
        ),
        Scenario(
            name="max hold timeout",
            entry_price=0.44,
            side="UP",
            marks=[(30, 0.45), (60, 0.43), (95, 0.44)],
        ),
    ]

    for s in scenarios:
        run_scenario(s)


if __name__ == "__main__":
    main()
