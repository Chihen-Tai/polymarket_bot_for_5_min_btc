# Changelog

## 2026-04-19 - Phase 1 audit handoff

- Added `AI_handoff/codemap.md`.
- Added `AI_handoff/config_snapshot.md`.
- Added `AI_handoff/fee_model_audit.md`.
- No trading code changed in this session.

Why:

- Capture the exact strategy path, runtime config reality, and fee-model divergences before any Phase 2 instrumentation work.

Rollback:

- Delete the three new handoff files if you want to remove this audit snapshot. No runtime behavior depends on them.

## 2026-04-19 - Phase 2 instrumentation and shadow-mode plumbing

- Added `SHADOW_MODE` config support in `core/config.py`.
- Added `shadow_journal.csv` support via `core/runtime_paths.py` and `core/journal.py`.
- Added cycle-metrics formatting and emission in `core/runner.py` with the fields `rtt_http_ms`, `rtt_ws_ms`, `ws_age_ms`, `clob_skew_ms`, `binance_ws_age_ms`, and `chainlink_oracle_age_s`.
- Added network-gate bypass support for shadow-mode candidate selection in `core/runner.py`.
- Added Binance websocket lag/age tracking in `core/ws_binance.py`.
- Added CLOB timestamp capture in `core/exchange.py` so cycle logs and shadow rows can be attributed to CLOB time.
- Added targeted tests in `tests/test_phase2_shadow_mode.py`, `tests/test_phase2_cycle_metrics.py`, and `tests/test_phase2_ws_metrics.py`.
- Fixed `scripts/journal_analysis.py` so orphan residual rows include `entry_secs_left`, and added `tests/test_phase2_journal_analysis.py`.
- Added `AI_handoff/journal_completeness_check.md`.

Why:

- Phase 2 needs instrumentation before strategy changes, plus a non-ordering shadow path that still shows what the strategy would have done when the VPN latency gate blocks live execution.

Verification:

- `python3 -m unittest tests.test_phase2_shadow_mode`
- `python3 -m unittest tests.test_phase2_cycle_metrics`
- `python3 -m unittest tests.test_phase2_ws_metrics`
- `python3 -m unittest tests.test_phase2_journal_analysis`
- `python3 -m compileall core tests`
- `python3 tests/test_ws_binance.py`
- Targeted smoke with stubbed optional modules: selected `model-fade_retail_fomo`, wrote `data/shadow_journal.csv`, and formatted a cycle-metrics line with all six required fields.
- `python3 -c "from scripts.journal_analysis import load_trade_events, build_trade_pairs, summarize_trade_pairs, summarize_shadow_signals; ..."` now runs on `data/trade_journal-dryrun.jsonl`

Dry-run note:

- A full end-to-end shadow-mode bot run was not possible in this shell because the local Python environment is missing runtime dependencies and the bundled `.venv` resolves to Python 3.9, while `py-clob-client>=0.28.0` is not installable there. The Phase 2 path was still smoke-verified with a targeted shadow-mode command that exercised candidate selection, CSV writing, and cycle-metrics formatting without placing orders.

Rollback:

- Remove `SHADOW_MODE` usage and the new helper functions in `core/runner.py`, `core/journal.py`, `core/ws_binance.py`, `core/exchange.py`, and `core/runtime_paths.py`, then delete the three Phase 2 test files if you want to revert to the previous instrumentation surface.
- Revert the `scripts/journal_analysis.py` residual-row fix and delete `tests/test_phase2_journal_analysis.py` if you want to restore the previous analysis behavior.

## 2026-04-19 - Phase 4 hedge sizing and low-cash entry policy

- Added structured hedge sizing helpers in `core/hedge_logic.py`.
- Added `HEDGE_RESERVE_USD` and `HEDGE_LOW_CASH_POLICY` support in `core/config.py`.
- Wired the runner to:
- block entry when low cash and `HEDGE_LOW_CASH_POLICY=skip_entry`
- allow primary-only entry when `HEDGE_LOW_CASH_POLICY=primary_only`
- cap the hedge to available post-fill cash minus reserve
- skip the hedge cleanly with `hedge_skipped_insufficient_capital`
- wait for primary fill before attempting any hedge
- Added `tests/test_hedge_sizing.py`.

Why:

- The previous hedge path used `order_usd * hedge_ratio` without checking remaining cash, reserve, or primary-fill state, which allowed exchange-side balance failures instead of an explicit policy decision.

Verification:

- `python3 -m unittest tests.test_hedge_sizing`
- `python3 -m unittest tests.test_hedge_sizing tests.test_phase2_cycle_metrics tests.test_phase2_shadow_mode`
- `python3 -m compileall core/hedge_logic.py core/config.py core/runner.py tests/test_hedge_sizing.py`

Default policy assumption:

- I set `HEDGE_LOW_CASH_POLICY` default to `skip_entry`, which is the safer profitability-preserving choice. If you want low-cash cycles to enter primary-only instead, set `HEDGE_LOW_CASH_POLICY=primary_only`.

Local config:

- `.env.local` now pins `HEDGE_LOW_CASH_POLICY=skip_entry` and `HEDGE_RESERVE_USD=0.50` explicitly.

Rollback:

- Remove the structured hedge helpers from `core/hedge_logic.py`, remove the two config keys from `core/config.py`, restore the old inline hedge branch in `core/runner.py`, and delete `tests/test_hedge_sizing.py`.

## 2026-04-19 - Phase 3 Option B local retune

- Added a simple `.env` fallback parser in `core/config.py` so repo-local env files load without `python-dotenv`.
- Wired `ENABLE_SHADOW_JOURNAL` to `SETTINGS.enable_shadow_journal`.
- Added calibrated VPN threshold support in `core/latency_monitor.py`.
- Updated `.env.local` for Option B:
- `MAX_VPN_LATENCY_MS=900`
- `VPN_AUTO_CALIBRATE_LATENCY=true`
- `VPN_LATENCY_MULTIPLIER=1.2`
- `VPN_LATENCY_FLOOR_MS=900`
- `SHADOW_MODE=true`
- Added `tests/test_phase3_option_b.py`.
- Added `AI_handoff/phase3_option_b.md`.

Why:

- Option B could not work in this workspace until `.env.local` overrides actually loaded, shadow-mode collection was enabled from env, and the VPN latency gate could be raised from measured RTT instead of the hardcoded 600ms default.

Verification:

- `python3 -m unittest tests.test_phase3_option_b`
- `python3 -m unittest tests.test_phase3_option_b tests.test_phase2_shadow_mode tests.test_phase2_cycle_metrics tests.test_phase2_ws_metrics`
- `python3 -c "import core.config as cfg; print(...)"` confirmed:
- `shadow_mode=True`
- `enable_shadow_journal=True`
- `max_vpn_latency_ms=900.0`
- `vpn_auto_calibrate_latency=True`
- `vpn_latency_multiplier=1.2`
- `vpn_latency_floor_ms=900.0`
- `python3 -c "from core.latency_monitor import LatencyMonitor; ..."` produced:
- `effective_max_vpn_latency_ms=1020.0`
- `blocked=(False, '')`

Rollback:

- Remove the fallback parser and calibrated-threshold helper, restore the old `enable_shadow_journal` default, revert the `.env.local` Option B keys, delete `tests/test_phase3_option_b.py`, and delete `AI_handoff/phase3_option_b.md`.

## 2026-04-19 - Phase 5 strategy upgrades

- Added 15-minute market-window spot features in `core/decision_engine.py`:
- `window_delta_pct`
- `last_10s_velocity_bps`
- `oracle_implied_prob`
- Added the fade guardrail that suppresses `model-fade_retail_fomo` when spot delta confirms the same direction as OFI.
- Added the `model-follow_momentum_t60` candidate for late-window trend continuation when spot delta and book imbalance agree.
- Added price-aware fee-buffer gating in `core/runner.py` so entries must clear breakeven plus a minimum `FEE_BUFFER` of `0.02`.
- Added config keys for the new thresholds in `core/config.py`.
- Added Binance 5m timestamp fields in `core/exchange.py` so market-window features can align to the market start.
- Corrected the fee model in `core/execution_engine.py` so taker fees are no longer reduced by maker rebates.
- Updated fee tests in `tests/test_vpn_calibrated_fees.py`.
- Added `tests/test_phase5_strategy_upgrades.py`.

Why:

- Phase 5 required adding the strongest missing public predictor, preventing bad contrarian fades when spot confirms the move, introducing a second trend-following strategy near expiry, and making the entry gate explicitly fee-aware.

Verification:

- `python3 -m unittest tests.test_phase5_strategy_upgrades`
- `python3 -c "import tests.test_vpn_calibrated_fees as t; ...; print('OK')"`
- `python3 -m unittest tests.test_phase5_strategy_upgrades tests.test_phase3_option_b tests.test_hedge_sizing tests.test_phase2_shadow_mode tests.test_phase2_cycle_metrics tests.test_phase2_ws_metrics tests.test_phase2_journal_analysis`
- `python3 -m compileall core/config.py core/exchange.py core/execution_engine.py core/decision_engine.py core/runner.py tests/test_phase5_strategy_upgrades.py tests/test_vpn_calibrated_fees.py`

Rollback:

- Remove the market-window feature helpers and momentum candidate generation from `core/decision_engine.py`, remove the new fee-aware gate fields from `core/runner.py`, restore the older taker-fee behavior in `core/execution_engine.py`, remove the new config keys from `core/config.py`, and delete `tests/test_phase5_strategy_upgrades.py`.

## 2026-04-19 - Phase 6 offline replay baseline

- Added `tests/test_phase6_replay_harness.py`.
- Updated `scripts/replay_harness.py` so it:
- falls back to the local Binance cache when the network is unavailable
- evaluates a 15-second seconds-left entry grid
- reports timing buckets and entry-price buckets
- computes a simple 5-day walk-forward split
- reports paper-trade gate status from the available dry-run journal
- Generated a fresh `data/backtest_results.csv` and `BACKTEST_REPORT.md` from cached data with `python3 scripts/replay_harness.py --days 30`.
- Added `AI_handoff/phase6_baseline.md`.

Why:

- The existing replay harness was the fastest path to a runnable Phase 6 baseline in this shell, but it previously failed offline even when the required Binance cache was already present.

Verification:

- `python3 -m unittest tests.test_phase6_replay_harness`
- `python3 scripts/replay_harness.py --days 30`

Observed baseline output:

- `95040` evaluated signals
- `3416` executed trades
- expectancy `2209.71 bps/trade`
- daily Sharpe `17.849`
- walk-forward aggregate test expectancy `2169.77 bps`
- paper-trade gate `insufficient_data` with a `2`-day continuous dry-run streak

Limitation:

- This remains a simplified replay baseline, not the full walk-forward/current-strategy evaluation harness requested in the brief.

Rollback:

- Remove the cache fallback logic from `scripts/replay_harness.py`, delete `tests/test_phase6_replay_harness.py`, and delete `AI_handoff/phase6_baseline.md`.
