import json
from pathlib import Path

from core.journal import read_events, summarize_reconciliation

BASE = Path(__file__).resolve().parent
OUT = BASE / 'trade_journal_reconciled.jsonl'


def main():
    events = read_events(limit=0)
    if not events:
        print('no journal')
        return

    summary = summarize_reconciliation(events)
    rows = [*events, *summary['notes']]
    OUT.write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in rows) + '\n', encoding='utf-8')
    print(f'wrote {OUT.name}')
    print(f"open_lots={len(summary['open_lots'])}")
    print(f"notes={len(summary['notes'])}")


if __name__ == '__main__':
    main()
