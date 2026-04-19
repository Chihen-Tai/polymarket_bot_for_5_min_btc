# Phase 6 Baseline

Generated: 2026-04-19

## What Was Completed

The existing replay harness in `scripts/replay_harness.py` now:

- runs offline from the cached Binance data in `data/binance_1m.csv`
- evaluates multiple late-window entry points on a 15-second seconds-left grid
- reports entry-price buckets
- reports timing buckets
- runs a simple 5-day walk-forward split with 3 train days and 2 test days
- reports paper-trade gate status from the available dry-run journal

Verification:

- `python3 -m unittest tests.test_phase6_replay_harness`
- `python3 scripts/replay_harness.py --days 30`

Observed output from the current cached replay:

- windows: `8640`
- signals evaluated: `95040`
- executed trades: `3416`
- expectancy: `2209.71 bps/trade`
- reported trade-level Sharpe: `128.626`
- reported daily Sharpe: `17.849`
- reported max drawdown: `20000.0 bps`
- walk-forward aggregate test expectancy: `2169.77 bps`
- walk-forward aggregate test win rate: `99.9%`
- paper-trade gate: `insufficient_data`
- longest continuous dry-run streak: `2 days`
- fee-adjusted dry-run trades available: `15`

## What Changed

- `scripts/replay_harness.py` now falls back to the local cache when Binance cannot be reached from the current shell.
- The replay harness fee section was aligned with the repo’s corrected taker-fee semantics in source, although the generated report should still be treated as a simplified baseline report, not a production-grade fee audit.

## Important Limitation

This is **still not** the full Phase 6 harness from the brief.

What it is:

- a simplified replay baseline over cached Binance 1m data
- a rough maker-first expectancy sanity check
- a report that can run in this shell without live network access

What it is not:

- a replay of the upgraded live decision stack from `core/decision_engine.py`
- a historical CLOB reconstruction from shadow journal or archived orderbook snapshots
- a statistically realistic maker-fill model
- a paper/live agreement gate that has actually cleared; the current status is `insufficient_data`

## Recommended Next Step

If continuing Phase 6, the next realistic increment is:

1. Reuse `scripts/replay_harness.py` as the baseline driver
2. Replace its simplified signal logic with the current decision-engine candidate path
3. Emit bucketed results by entry price and timing
4. Add a walk-forward wrapper over 5-day windows
