# AI Handoff - 2026-03-29

## Repo

- Path: `/Applications/codes/polymarket-bot-by_openclaw`
- Purpose: trade Polymarket 5-minute BTC `UP/DOWN` markets with Binance WS used as a lead/confirmation signal.
- Main entrypoint: `/Applications/codes/polymarket-bot-by_openclaw/main.py`

## Current Git State

- Latest committed HEAD: `910ce5c Add side-conflict guard for ambiguous entry candidates`
- Recent commits:
  - `910ce5c` Add side-conflict guard for ambiguous entry candidates
  - `496201a` protect stalled profits with binance signal
  - `d3e72a2` improve payoff structure and aggressive take profit ladder
  - `01965c3` refine executable take-profit ladder
  - `6a8fddd` document collector and stack startup scripts
  - `656cf41` fix collector startup paths and add stack launcher
  - `1fd62fe` refine take-profit ladder and add market data collector
- Current working tree is dirty and not yet pushed.
- Modified local files right now:
  - `/Applications/codes/polymarket-bot-by_openclaw/.env`
  - `/Applications/codes/polymarket-bot-by_openclaw/.env.example`
  - `/Applications/codes/polymarket-bot-by_openclaw/.env.live.example`
  - `/Applications/codes/polymarket-bot-by_openclaw/core/config.py`
  - `/Applications/codes/polymarket-bot-by_openclaw/tests/test_exit_fix.py`
  - `/Applications/codes/polymarket-bot-by_openclaw/tests/test_trade_manager.py`

## Environment And Runtime Notes

- Conda env: `polymarket-bot`
- README says config load order is:
  - tracked `.env`
  - local `.env.local`
  - local `.env.secrets`
- The repo keeps `.env` tracked as a shared baseline, but live secrets should be stored only in local override files.

## Important Files

### Core execution

- `/Applications/codes/polymarket-bot-by_openclaw/core/runner.py`
  - Main loop
  - Market selection
  - Open-position watch
  - Runtime exit coordination
  - Binance WS overlays
  - Journaling and run labels
- `/Applications/codes/polymarket-bot-by_openclaw/core/trade_manager.py`
  - Exit decision policy
  - Same-market reentry block logic
- `/Applications/codes/polymarket-bot-by_openclaw/core/decision_engine.py`
  - Entry-side scoring and directional logic
- `/Applications/codes/polymarket-bot-by_openclaw/core/exchange.py`
  - Dry-run and live exchange adapter
  - Orderbook parsing
  - Close accounting extraction
- `/Applications/codes/polymarket-bot-by_openclaw/core/config.py`
  - All settings from env
- `/Applications/codes/polymarket-bot-by_openclaw/core/journal.py`
  - Open-position replay and journal reconstruction

### Reporting / analysis

- `/Applications/codes/polymarket-bot-by_openclaw/scripts/journal_analysis.py`
  - Report reconstruction
  - Exit accounting rows
  - Residual/orphan reconciliation
- `/Applications/codes/polymarket-bot-by_openclaw/scripts/trade_pair_ledger.py`
- `/Applications/codes/polymarket-bot-by_openclaw/scripts/verify_close_accounting.py`
- `/Applications/codes/polymarket-bot-by_openclaw/scripts/inspect_trades.py`

### Data collection

- `/Applications/codes/polymarket-bot-by_openclaw/scripts/market_data_collector.py`
  - Sidecar collector only
  - Reads journal
  - Captures market snapshots around trades
  - Writes to `market_data/`
- `/Applications/codes/polymarket-bot-by_openclaw/scripts/start_market_data_collector.sh`
- `/Applications/codes/polymarket-bot-by_openclaw/scripts/start_bot_with_market_data.sh`

### Tests

- `/Applications/codes/polymarket-bot-by_openclaw/tests/test_exit_fix.py`
- `/Applications/codes/polymarket-bot-by_openclaw/tests/test_trade_manager.py`
- `/Applications/codes/polymarket-bot-by_openclaw/tests/test_runtime_paths.py`
- `/Applications/codes/polymarket-bot-by_openclaw/tests/test_market_data_collector.py`

## Runtime Output Paths

- Logs:
  - `/Applications/codes/polymarket-bot-by_openclaw/data/log-dryrun-*.txt`
  - `/Applications/codes/polymarket-bot-by_openclaw/data/log-live-*.txt`
- Reports:
  - `/Applications/codes/polymarket-bot-by_openclaw/data/report-dryrun-*.txt`
  - `/Applications/codes/polymarket-bot-by_openclaw/data/report-live-*.txt`
  - `/Applications/codes/polymarket-bot-by_openclaw/data/latest_run_report.txt`
- Collector:
  - `/Applications/codes/polymarket-bot-by_openclaw/market_data/YYYY-MM-DD/<event-folder>/event.json`
  - `/Applications/codes/polymarket-bot-by_openclaw/market_data/YYYY-MM-DD/<event-folder>/window.jsonl`
  - `/Applications/codes/polymarket-bot-by_openclaw/market_data/logs/`

## Strategy State Before Current Local Tuning

These are the important strategy features already in code before the latest dirty changes:

- Executable-profit gating for take-profit.
  - TP does not trigger just from `mark`; it must have executable bid-depth profit.
- Side-conflict entry guard.
  - If best `UP` and `DOWN` candidates are too close, skip entry.
- Binance adverse exit.
  - If Binance lag/current velocity both go against the position before safe profit is reached, exit early.
- Binance stalled-profit protect exit.
  - Small profit that stalls and turns weak on Binance can exit before rolling over.
- Same-market reentry block.
  - Protection exits and full closes are intended to block reentry in the same 5-minute market.
- Live entry requires usable orderbook.
  - Bot should not hard-enter off Gamma fallback price if live CLOB book is unavailable.
- Collector is fully sidecar.
  - It does not modify bot trading logic.

## Current Local Dirty Tuning

These edits are local and not yet pushed.

### Goal

- The user observed that:
  - trades sitting around `+10%` for a while were not sold and later became losses
  - stop-loss exits often ended at `-40%` to `-60%`
  - overall system behavior looked "correct" structurally but still lost money because small wins did not offset large losses

### Current local parameter changes

From `/Applications/codes/polymarket-bot-by_openclaw/.env` and `/Applications/codes/polymarket-bot-by_openclaw/core/config.py`:

- `TAKE_PROFIT_SOFT_PCT=0.18`
- `TAKE_PROFIT_PARTIAL_FRACTION=0.40`
- `TAKE_PROFIT_HARD_PCT=0.30`
- `TAKE_PROFIT_RUNNER_FRACTION=0.10`
- `TP_HOLD_VELOCITY=0.00045`
- `BINANCE_PROFIT_PROTECT_MIN_PROFIT_PCT=0.08`
- `BINANCE_PROFIT_PROTECT_MAX_PROFIT_PCT=0.17`
- `BINANCE_PROFIT_PROTECT_STALL_SEC=6`
- `BINANCE_PROFIT_PROTECT_CONFIRM_SEC=1`
- `BINANCE_PROFIT_PROTECT_VELOCITY=0.00012`
- `BINANCE_PROFIT_PROTECT_MIN_HOLD_SEC=10`
- `BINANCE_PROFIT_PROTECT_REQUIRE_CURRENT_CONFIRM=false`
- `STOP_LOSS_PARTIAL_PCT=0.08`
- `STOP_LOSS_PCT=0.18`
- `STOP_LOSS_WARN_PCT=0.08`
- `SOFT_STOP_CONFIRM_SEC=2.5`
- `SOFT_STOP_CONFIRM_BUFFER_PCT=0.015`
- `SOFT_STOP_ADVERSE_VELOCITY=0.00018`
- `EXIT_DEADLINE_SEC=35`
- `FORCE_FULL_EXIT_ON_STOP_LOSS_SCALEOUT=true`

### Why these were changed

- Smaller winners were not monetized quickly enough.
- Current TP thresholds were too far away for the observed live market regime.
- First stop-loss stage was too slow and too forgiving.
- Deadline exit was too late, which left fragile near-expiry greens exposed to slippage and reversal.

## Recent External Files Analyzed

These files live outside the repo but were used to tune logic:

- `/Users/microdaery/Downloads/log-live-2026-03-29T06-47-28.txt`
- `/Users/microdaery/Downloads/latest_run_report-12.txt`

### Key findings from those files

- Trade `btc-updown-5m-1774738500`:
  - sat around `+11.54%` to `+15.39%` for a while
  - no profit-protect exit happened
  - later rolled to `-38.46%` observed and actual close was roughly `-34.95%`
- Trade `btc-updown-5m-1774742100`:
  - had a first big win and principal extraction on one leg
  - later same market had a second trade that sat around `+4.97%` to `+8.36%`
  - ended with `deadline-exit-weak-win`
  - observed looked slightly positive but actual realized was still a real loss
- Report summary in `latest_run_report-12.txt`:
  - `actual_pnl sum` about `-7.16`
  - many rows had positive `observed` but bad `actual`
  - `take-profit-partial+market-expired-binary-loss` and `take-profit-principal-partial+market-expired-binary-loss` existed
  - this strongly suggests real exits are too weak and losses are still too large

## Known Architecture Notes

### Profit references

- `profit_pnl_pct` is intentionally based on executable value, not raw mark.
- In `/Applications/codes/polymarket-bot-by_openclaw/core/runner.py`:
  - `profit_reference_value = executable_take_profit_value(...)`
  - `effective_exit_value = conservative_exit_decision_value(...)`
- This means:
  - upside should be based on executable orderbook depth
  - downside can still be revealed by mark if executable value is missing

### Same-market reentry

- Full exits with zero remaining shares should block reentry for the same market slug.
- This logic sits in `/Applications/codes/polymarket-bot-by_openclaw/core/trade_manager.py` via `should_block_same_market_reentry(...)`.

### Live full-book requirement

- Live entry should skip if the book is unavailable.
- A prior bug existed where object-style orderbooks were misread as unavailable; that was previously fixed.

### Residual accounting

- There has been recurring work to prevent `residual` rows from pretending to be profitable independent trades.
- Reconciliation logic lives in `/Applications/codes/polymarket-bot-by_openclaw/scripts/journal_analysis.py`.

## Testing State

Latest local verification for the current dirty changes:

- `python3 -m py_compile core/config.py core/runner.py core/trade_manager.py tests/test_exit_fix.py tests/test_trade_manager.py`
- `conda run -n polymarket-bot python -m pytest -q tests/test_exit_fix.py tests/test_trade_manager.py tests/test_runtime_paths.py`
- `git diff --check`

Result:

- `3 passed`
- compile OK
- diff check OK

Note:

- I only reran the focused smoke subset after the latest local tuning.
- Full-repo test suite was not rerun in this handoff step.

## How To Run

### Bot only

```bash
cd /Applications/codes/polymarket-bot-by_openclaw
conda activate polymarket-bot
python main.py
```

### Collector only

```bash
cd /Applications/codes/polymarket-bot-by_openclaw
bash scripts/start_market_data_collector.sh
```

### Bot + collector

```bash
cd /Applications/codes/polymarket-bot-by_openclaw
bash scripts/start_bot_with_market_data.sh
```

## What Another AI Should Check Next

1. Compare actual live fills against executable-profit assumptions.
   - Some report rows still show positive observed but negative actual.
2. Inspect whether `binance-profit-protect-exit` is truly firing in the intended `+8%~+15%` plateau regime after the local tuning.
3. Validate that `FORCE_FULL_EXIT_ON_STOP_LOSS_SCALEOUT=true` does not create a new bad interaction with partial-close bookkeeping.
4. Recheck the deadline-exit logic.
   - Near expiry, weak positive observed value can still realize as a loss.
5. Consider whether principal extraction should use even more actual-cash-aware logic rather than only executable notional.

## Suggested Prompt For Another AI

Use this repo as the primary source of truth:

- `/Applications/codes/polymarket-bot-by_openclaw/AI_HANDOFF_2026-03-29.md`
- `/Applications/codes/polymarket-bot-by_openclaw/README.md`
- `/Applications/codes/polymarket-bot-by_openclaw/core/runner.py`
- `/Applications/codes/polymarket-bot-by_openclaw/core/trade_manager.py`
- `/Applications/codes/polymarket-bot-by_openclaw/core/config.py`
- `/Applications/codes/polymarket-bot-by_openclaw/tests/test_exit_fix.py`
- `/Applications/codes/polymarket-bot-by_openclaw/tests/test_trade_manager.py`

Also inspect these external analysis artifacts:

- `/Users/microdaery/Downloads/log-live-2026-03-29T06-47-28.txt`
- `/Users/microdaery/Downloads/latest_run_report-12.txt`

Important caveat:

- The repo HEAD is committed at `910ce5c`, but the current working tree has uncommitted tuning changes that make profit-taking earlier and stop-loss more aggressive.
