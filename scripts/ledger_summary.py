from __future__ import annotations

import argparse
import json
from pathlib import Path

from journal_analysis import (
    build_exit_accounting_rows,
    build_trade_pairs,
    dataclass_list_to_csv,
    dataclass_list_to_json,
    load_trade_events,
    summarize_exit_accounting,
    summarize_trade_pairs,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Aggregate recent trade ledger/accounting into report-friendly summary")
    ap.add_argument("--limit", type=int, default=50, help="Use the most recent N paired trades/exits")
    ap.add_argument("--format", choices=["table", "json", "csv"], default="table", help="Optional export format")
    ap.add_argument("--output", help="Export file path for --format json/csv")
    args = ap.parse_args()

    events = load_trade_events(limit=0)
    pair_rows = build_trade_pairs(events)[-args.limit:]
    exit_rows = build_exit_accounting_rows(events)[-args.limit:]

    payload = {
        "scope": {"limit": args.limit},
        "trade_pairs": summarize_trade_pairs(pair_rows),
        "exit_accounting": summarize_exit_accounting(exit_rows),
    }

    if args.format == "table":
        print("ledger summary")
        print("=============")
        print(f"scope.limit: {args.limit}")
        print("trade_pairs:")
        for key, value in payload["trade_pairs"].items():
            print(f"  {key}: {value}")
        print("exit_accounting:")
        for key, value in payload["exit_accounting"].items():
            print(f"  {key}: {value}")
        return

    path = Path(args.output) if args.output else Path(f"ledger_summary.{args.format}")
    if args.format == "json":
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        rows = []
        for section, values in payload.items():
            if isinstance(values, dict):
                for key, value in values.items():
                    rows.append({"section": section, "metric": key, "value": json.dumps(value, ensure_ascii=False)})
            else:
                rows.append({"section": "root", "metric": section, "value": json.dumps(values, ensure_ascii=False)})
        dataclass_list_to_csv([], path) if False else None
        import csv
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["section", "metric", "value"])
            writer.writeheader()
            writer.writerows(rows)
    print(f"exported summary -> {path}")


if __name__ == "__main__":
    main()
