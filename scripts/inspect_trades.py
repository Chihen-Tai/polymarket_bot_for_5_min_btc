from __future__ import annotations

import argparse

from core.journal import format_entry_summary, format_exit_summary, read_events


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect recent trade journal entries/exits")
    ap.add_argument("--limit", type=int, default=20, help="How many recent trade events to show")
    args = ap.parse_args()

    events = [ev for ev in read_events(limit=max(args.limit * 4, 50)) if ev.get("kind") in {"entry", "exit"}]
    events = events[-args.limit:]

    if not events:
        print("No entry/exit events found.")
        return

    for ev in events:
        ts = ev.get("ts") or ""
        if ev.get("kind") == "entry":
            summary = format_entry_summary(ev)
        else:
            summary = format_exit_summary(ev)
        print(f"{ts} | {summary}")


if __name__ == "__main__":
    main()
