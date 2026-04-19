# Phase 2.3 Journal Completeness Check

Generated: 2026-04-19

## Verification Result

The repo does **not** contain the specific operator artifact described in the brief:

- 10-hour live log from `2026-04-18 17:47` to `2026-04-19 03:34`
- the exact session that reportedly produced `VPN_LATENCY_BLOCK=6430`, `attempted entries=1`, `filled entries=1`

Because that artifact is missing from the workspace, I could not reproduce the exact `6430 / 1 / 1` counts from the brief.

## Script Status

`scripts/journal_analysis.py` was not runnable on the current dry-run journal at the start of this session. It crashed while constructing orphan residual rows because `TradePairRow` required `entry_secs_left`.

Fix applied:

- `scripts/journal_analysis.py:1203` now sets `entry_secs_left=None` for residual rows.

Verification:

- `python3 -m unittest tests.test_phase2_journal_analysis`
- `python3 -c "from scripts.journal_analysis import load_trade_events, build_trade_pairs, summarize_trade_pairs, summarize_shadow_signals; ..."`

## Current Dry-Run Journal Summary

Computed from the currently available `data/trade_journal-dryrun.jsonl`:

| Metric | Value |
| --- | --- |
| entry/exit events loaded | `41` |
| trade pairs built | `19` |
| total trades | `19` |
| closed | `14` |
| partial | `1` |
| residual | `1` |
| unmatched | `3` |
| fee-adjusted actual pnl count | `15` |
| fee-adjusted actual pnl sum | `0.9179023885` |
| fee-adjusted actual pnl average | `0.0611934926` |
| shadow signals in current dry-run journal | `0` |

## Closest Available Log Snapshot

The latest repo-local dry-run log near the brief date is `data/log-dryrun-2026-04-18T17-29-40.txt`, but it is only a short run, not the 10-hour session from the brief.

Manual counts from that file:

| Metric | Value |
| --- | --- |
| `entry approved` | `1` |
| `order placed` | `1` |
| `executing structured hedge` | `0` |
| `VPN_LATENCY_BLOCK` | `0` |
| `VPN_WS_STALE_BLOCK` | `0` |
| `ofi_below_threshold` | `0` |
| `edge_below_sniper_threshold` | `0` |
| `not enough balance` | `0` |

## Conclusion

Phase 2.3 is only partially complete in this workspace:

- The journal-analysis script now runs on the current dry-run journal.
- A current summary is available.
- Exact reproduction of the operator’s audited 10-hour metrics is blocked by a missing source artifact.

To finish the brief exactly, the missing 10-hour log or corresponding journal slice needs to be added to the repo or provided in-session.
