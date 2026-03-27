from __future__ import annotations

from dataclasses import dataclass

from core.trade_manager import decide_exit, maybe_reverse_entry


@dataclass
class PaperPos:
    side: str
    entry_price: float
    opened_sec: int


def pnl_pct(entry_price: float, mark: float) -> float:
    shares = 1.0 / entry_price
    return shares * mark - 1.0


def mark_for_side(side: str, up: float, down: float) -> float:
    return up if side == "UP" else down


def run_sequence(name: str, ticks: list[dict], initial_signal: str = "DOWN"):
    print(f"\n=== sequence: {name} ===")
    pos: PaperPos | None = None
    consec_losses = 0
    last_loss_side = ""

    for tick in ticks:
        t = tick["t"]
        up = tick["up"]
        down = tick["down"]
        raw_signal = tick.get("signal")
        no_entry_reason = tick.get("no_entry_reason")

        if pos is None and raw_signal:
            entry = maybe_reverse_entry(
                signal_side=raw_signal,
                live_consec_losses=consec_losses,
                last_loss_side=last_loss_side,
            )
            side = entry.side
            price = mark_for_side(side, up, down)
            pos = PaperPos(side=side, entry_price=price, opened_sec=t)
            extra = f" ({entry.reason})" if entry.reason else ""
            print(f"t={t:>3}s ENTER {side} @ {price:.3f}{extra}")
            continue

        if pos is None and no_entry_reason:
            print(f"t={t:>3}s NO-ENTRY reason={no_entry_reason} up={up:.3f} down={down:.3f}")
            continue

        if pos is not None:
            mark = mark_for_side(pos.side, up, down)
            hold = t - pos.opened_sec
            pnl = pnl_pct(pos.entry_price, mark)
            decision = decide_exit(pnl_pct=pnl, hold_sec=hold)
            print(f"t={t:>3}s HOLD {pos.side} mark={mark:.3f} pnl={pnl:+.2%}")
            if decision.should_close:
                print(f"t={t:>3}s EXIT {pos.side} reason={decision.reason} pnl={pnl:+.2%}")
                if pnl < 0:
                    consec_losses += 1
                    last_loss_side = pos.side
                else:
                    consec_losses = 0
                    last_loss_side = ""
                pos = None

    if pos is not None:
        print(f"END still open: {pos}")
    print(f"final consec_losses={consec_losses} last_loss_side={last_loss_side}")


def main():
    run_sequence(
        "stop loss then reverse next DOWN to UP",
        [
            {"t": 0, "up": 0.42, "down": 0.58, "signal": "DOWN"},
            {"t": 12, "up": 0.57, "down": 0.43},
            {"t": 30, "up": 0.48, "down": 0.52, "signal": "DOWN"},
            {"t": 42, "up": 0.64, "down": 0.36},
            {"t": 60, "up": 0.70, "down": 0.30, "signal": "DOWN"},
            {"t": 72, "up": 0.28, "down": 0.72},
        ],
    )

    run_sequence(
        "soft take profit and re-enter later",
        [
            {"t": 0, "up": 0.34, "down": 0.66, "signal": "UP"},
            {"t": 10, "up": 0.43, "down": 0.57},
            {"t": 22, "up": 0.53, "down": 0.47},
            {"t": 40, "up": 0.49, "down": 0.51, "signal": "UP"},
            {"t": 55, "up": 0.71, "down": 0.29},
        ],
    )


if __name__ == "__main__":
    main()
