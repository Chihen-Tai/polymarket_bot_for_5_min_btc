from __future__ import annotations

import argparse
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from journal_analysis import (
    build_trade_pairs,
    dataclass_list_to_csv,
    dataclass_list_to_json,
    load_trade_events,
    summarize_trade_pairs,
)


def fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.4f}" if v < 0 else f"{v:.4f}"


def maybe_export(rows, fmt: str, output: str | None, *, show_legs: bool) -> None:
    if fmt == "table":
        return
    path = Path(output) if output else Path(f"trade_pair_ledger_export.{fmt}")
    if fmt == "json":
        dataclass_list_to_json(rows, path)
    elif fmt == "csv":
        dataclass_list_to_csv(rows, path, flatten_legs=show_legs)
    print(f"\nexported {len(rows)} rows -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Pair entry/exit journal events into trade ledger rows")
    ap.add_argument("--limit", type=int, default=40, help="Show most recent N trade rows")
    ap.add_argument("--status", choices=["all", "closed", "partial", "unmatched", "residual"], default="all")
    ap.add_argument("--show-legs", action="store_true", help="Print matched exit legs under each trade")
    ap.add_argument("--format", choices=["table", "json", "csv"], default="table", help="Optional export format")
    ap.add_argument("--output", help="Export file path for --format json/csv")
    ap.add_argument("--summary", action="store_true", help="Print aggregated summary after the table")
    ap.add_argument("--run-id", help="Only include events from a specific run_id and matching predecessor entries")
    ap.add_argument("--since-ts", help="Only include events at/after this ISO timestamp and matching predecessor entries")
    args = ap.parse_args()

    events = load_trade_events(limit=0, run_id=args.run_id, since_ts=args.since_ts)
    rows = build_trade_pairs(events)
    if args.status != "all":
        rows = [row for row in rows if row.status == args.status]
    rows = rows[-args.limit:]

    if not rows:
        print("No trade rows found.")
        return

    header = (
        f"{'status':<10} {'opened/closed':<19} {'market':<26} {'side':<4} {'entry':>8} {'actual':>8} {'observed':>9} "
        f"{'act.pnl':>8} {'obs.pnl':>8} {'tier':<6} {'reason':<18} {'remain':>8} {'mae':>8} {'mfe':>8}"
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        stamp = row.closed_ts or row.opened_ts
        print(
            f"{row.status:<10} {stamp:<19} {row.market[:26]:<26} {row.side:<4} {row.matched_cost_usd:>8.4f} "
            f"{fmt_money(row.exit_recovered_actual_usd):>8} {fmt_money(row.exit_recovered_observed_usd):>9} "
            f"{fmt_money(row.actual_pnl_usd):>8} {fmt_money(row.observed_pnl_usd):>8} {row.actual_source_tier:<6} {row.close_reason[:18]:<18} "
            f"{row.remaining_shares:>8.4f} {fmt_money(row.mae_pnl_usd):>8} {fmt_money(row.mfe_pnl_usd):>8}"
        )
        if row.status in {"partial", "unmatched", "residual"}:
            print(
                f"  -> position_id={row.position_id} unmatched_cost={row.unmatched_entry_cost_usd:.4f} "
                f"unmatched_shares={row.unmatched_entry_shares:.6f} entry_quality={row.entry_quality} flags={'|'.join(row.flags) or 'n/a'}"
            )
        if args.show_legs and row.legs:
            for leg in row.legs:
                print(
                    f"     leg {leg.kind:<13} ts={leg.ts} shares={leg.shares:.6f} cost={leg.cost_usd:.4f} "
                    f"actual={fmt_money(leg.recovered_actual_usd)} observed={fmt_money(leg.recovered_observed_usd)} "
                    f"tier={leg.source_tier:<6} reason={leg.reason}"
                )

    print()
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    print("summary:", " ".join(f"{k}={v}" for k, v in sorted(by_status.items())))

    maybe_export(rows, args.format, args.output, show_legs=args.show_legs)

    if args.summary:
        summary = summarize_trade_pairs(rows)
        print("summary details:")
        for key, value in summary.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
