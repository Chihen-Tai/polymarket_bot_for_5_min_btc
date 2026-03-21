from __future__ import annotations

import argparse
from pathlib import Path

from journal_analysis import (
    build_exit_accounting_rows,
    dataclass_list_to_csv,
    dataclass_list_to_json,
    load_trade_events,
    summarize_exit_accounting,
)


def fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.4f}" if v < 0 else f"{v:.4f}"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.1%}"


def severity(row, warn_diff: float, critical_diff: float) -> str:
    if row.actual_status == "missing":
        return "NO_ACTUAL"
    if row.actual_status != "ok":
        return "CHECK_SRC"
    diff = abs(row.difference_usd or 0.0)
    if diff >= critical_diff:
        return "CRITICAL"
    if diff >= warn_diff:
        return "WARN"
    return "OK"


def maybe_export(rows, fmt: str, output: str | None) -> None:
    if fmt == "table":
        return
    path = Path(output) if output else Path(f"verify_close_accounting_export.{fmt}")
    if fmt == "json":
        dataclass_list_to_json(rows, path)
    elif fmt == "csv":
        dataclass_list_to_csv(rows, path)
    print(f"\nexported {len(rows)} rows -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify recent exit accounting: actual vs observed")
    ap.add_argument("--limit", type=int, default=30, help="Show most recent N exit events")
    ap.add_argument("--warn-diff", type=float, default=0.10, help="Warn when |actual-observed| >= this USD amount")
    ap.add_argument("--critical-diff", type=float, default=0.25, help="Critical when |actual-observed| >= this USD amount")
    ap.add_argument("--only-flags", action="store_true", help="Show only NO_ACTUAL/WARN/CRITICAL rows")
    ap.add_argument("--format", choices=["table", "json", "csv"], default="table", help="Optional export format")
    ap.add_argument("--output", help="Export file path for --format json/csv")
    ap.add_argument("--summary", action="store_true", help="Print aggregated summary after the table")
    args = ap.parse_args()

    events = load_trade_events(limit=0)
    rows = build_exit_accounting_rows(events)
    rows = rows[-args.limit:]
    if not rows:
        print("No exit events found.")
        return

    header = (
        f"{'flag':<10} {'ts':<19} {'market':<28} {'side':<4} {'reason':<18} "
        f"{'cost':>8} {'actual':>8} {'observed':>9} {'diff':>8} {'tier':<6} {'actual_src':<22} {'flags':<30}"
    )
    print(header)
    print("-" * len(header))

    shown_rows = []
    shown = 0
    for row in rows:
        flag = severity(row, args.warn_diff, args.critical_diff)
        if args.only_flags and flag == "OK":
            continue
        shown += 1
        shown_rows.append(row)
        print(
            f"{flag:<10} {row.ts:<19} {row.market[:28]:<28} {row.side:<4} {row.reason[:18]:<18} "
            f"{row.realized_cost_usd:>8.4f} {fmt_money(row.actual_exit_value_usd):>8} {fmt_money(row.observed_exit_value_usd):>9} "
            f"{fmt_money(row.difference_usd):>8} {row.actual_source_tier:<6} {row.actual_source[:22]:<22} {'|'.join(row.flags)[:30]:<30}"
        )

    print()
    total = len(rows)
    no_actual = sum(1 for r in rows if severity(r, args.warn_diff, args.critical_diff) == "NO_ACTUAL")
    check_src = sum(1 for r in rows if severity(r, args.warn_diff, args.critical_diff) == "CHECK_SRC")
    warn = sum(1 for r in rows if severity(r, args.warn_diff, args.critical_diff) == "WARN")
    critical = sum(1 for r in rows if severity(r, args.warn_diff, args.critical_diff) == "CRITICAL")
    ok = total - no_actual - check_src - warn - critical
    print(f"shown={shown} total={total} ok={ok} warn={warn} critical={critical} check_src={check_src} no_actual={no_actual}")

    maybe_export(shown_rows if args.only_flags else rows, args.format, args.output)

    if args.summary:
        summary = summarize_exit_accounting(shown_rows if args.only_flags else rows)
        print("summary:")
        for key, value in summary.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
