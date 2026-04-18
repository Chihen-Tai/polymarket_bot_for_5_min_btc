# DIAGNOSIS.md — Full Code Walkthrough

**Date:** 2026-04-18
**Auditor:** Claude Opus 4.6

---

## 1. Architecture Overview

```
main.py
  -> core/runner.py          (6688 lines — main trading loop)
     -> core/config.py       (269 lines — Settings dataclass, env loading)
     -> core/decision_engine.py  (426 lines — signal evaluation, candidate selection)
     -> core/execution_engine.py (230 lines — fee model, edge calculation, order placement)
     -> core/fair_value_model.py (98 lines — Black-Scholes binary probability)
     -> core/ensemble_models/ensemble.py (43 lines — M1+M2 probability blender)
     -> core/ensemble_models/microstructure.py — OFI skew modifier
     -> core/trade_manager.py (160 lines — exit decisions, reentry blocking)
     -> core/risk_manager.py  — consecutive loss tracking, exposure limits
     -> core/learning.py      (180 lines — strategy scoreboard, Bayesian win rate)
     -> core/journal.py       — trade journaling
     -> core/exchange.py      — Polymarket CLOB API, order placement
     -> core/latency_monitor.py — VPN/network health
     -> core/http.py          — HTTP request wrapper
```

## 2. Data Flow

```
Binance WS (BTC/USDT) ──> btc_price, ws_trades, ws_velocity
                              |
                              v
                    fair_value_model.py
                    (Black-Scholes + realized vol)
                              |
                              v
                    ensemble.py (M1=BS, M2=OFI)
                              |
                              v
                    fv_yes (fair value for YES token)
                              |
Polymarket CLOB ──> orderbooks (up/down) ──> decision_engine.py
                              |                    |
                              |              calculate_committed_edge()
                              |                    |
                              v                    v
                    candidates dict ──> _select_best_candidate()
                              |
                              v
                    runner.py (entry/exit loop)
                              |
                              v
                    exchange.py (place order)
```

## 3. Module-by-Module Analysis

### 3.1 core/config.py

**Purpose:** Central configuration via `Settings` dataclass. Loads from env vars with defaults.

**Key fields (post-fix):**
- `sniper_extreme_upper/lower`: 0.95/0.05 (was 0.75/0.25 — dead zone eliminated)
- `min_sniper_edge_bps`: 80 (was 150)
- `vpn_maker_only`: True (maker-first philosophy enforced)
- `max_consec_loss`: 10 (was 2 — too tight)
- `scoreboard_aux_weight`: 0.3 (was 0.2)
- `enable_legacy_strategies`: False (OFI/flash snipe behind feature flag)

**Remaining concerns:**
- 269 lines in a single dataclass — consider splitting into sub-configs
- Many fields have redundant `getattr(SETTINGS, ...)` patterns in runner.py that bypass type safety

### 3.2 core/execution_engine.py

**Purpose:** Fee model, edge calculation, VWAP computation.

**Post-fix state:**
- `PolymarketFeeModel` fetches from gamma-api, caches 5min
- Correct formula: `fee = rate * p^exp * (1-p)^exp * shares`
- Maker fee always 0 (takerOnly=True confirmed)
- `effective_taker_rate_after_rebate` property accounts for 20% rebate
- Falls back to conservative 1.80% if API unreachable

**`calculate_committed_edge(fair_value, ob_up, ob_down, size, side, assume_maker, secs_left)`:**
- Determines entry price from orderbook (maker=best ask, taker=VWAP)
- Edge = FV - entry - fee - latency_buffer
- Late certainty override: zeroes latency cost when secs_left < 180 and price > 0.85

### 3.3 core/fair_value_model.py

**Purpose:** Black-Scholes binary option probability.

**Post-fix state:**
- Default vol 50% (was 60%)
- `calculate_realized_vol()` uses rolling window of 1m prices, annualized
- Floor 20%, cap 150%
- Fallback 60% when insufficient data (was 70%)

**`get_fair_value(btc_price, strike, secs_left, implied_vol, price_history, ws_bba)`:**
- Computes BS probability, then passes through ensemble aggregator
- Ensemble: 35% BS weight + 65% OFI microstructure weight

### 3.4 core/decision_engine.py

**Purpose:** Evaluates all signals, builds candidate dict, selects best.

**`explain_choose_side()`:**
- Computes `fv_yes` via `get_fair_value()`
- Calculates `edge_up` and `edge_down` via `calculate_committed_edge()`
- Builds candidates: `sniper_fade_up/down` if edge >= threshold
- Optionally adds OFI/flash snipe candidates (if `ENABLE_LEGACY_STRATEGIES=true`)
- Returns best candidate via `_select_best_candidate()`

**Key issue found and fixed:**
- Lines 9-10 imported `get_ofi_signal` and `get_flash_snipe_signal` but never used them
- Now wired behind feature flag with proper StrategyResult -> candidate conversion

### 3.5 core/runner.py (6688 lines)

**Purpose:** Main trading loop, position management, exit logic, scoreboard integration.

**Critical functions:**
- `required_trade_edge()` (line 2530): Computes minimum edge needed. Now defaults to 0% fee (maker-only).
- `summarize_entry_edge()` (line 2628): Wraps required_trade_edge with neutral zone blocking.
- `apply_scoreboard_aux_probability()` (line 2605): Blends model probability with scoreboard win rate.
- `stabilize_entry_win_rate()` (line 2659): Penalizes win rate for low-sample strategies.
- `session_hour_entry_block_reason()` (line 1037): Now timezone-aware with `OPERATOR_TZ` env.

**Exit logic (14 conditions — see docs/exit_state_machine.md):**
- Layer 1: trade_manager.py (5 conditions)
- Layer 2: runner.py (9 supplementary triggers)
- Target: consolidate to 6 orthogonal exit dimensions

**Remaining concerns:**
- 6688 lines is far too large — should be split into 5-6 focused modules
- Heavy use of `getattr(SETTINGS, ...)` with string keys bypasses IDE support
- Many exit functions are taker-specific but bot is maker-only

### 3.6 core/learning.py

**Purpose:** Per-strategy performance tracking with Bayesian updating.

**Post-fix state:**
- `StrategyScoreboard` tracks fee-adjusted PnL per strategy
- `get_strategy_expectancy()`: Bayesian-smoothed with decay factor
- `get_bayesian_win_rate()` (NEW): Beta(alpha,beta) posterior, returns (mean, 5th percentile lower bound)
- Credible interval lower bound used to down-weight win rate when >= 20 decisive trades

### 3.7 core/trade_manager.py

**Purpose:** Exit decisions and reentry blocking.

**`decide_exit()`:** 5 conditions (see exit_state_machine.md)
- Catastrophic stop at -30%
- Fomo premium capture in last 60s
- Deadline management in last 15s
- Default: hold to expiry

**`should_block_same_market_reentry()`:** Prevents chasing after losses

### 3.8 core/ensemble_models/

**`ensemble.py`:** Simple weighted blend — 35% BS + 65% OFI
**`microstructure.py`:** Order flow imbalance skew modifier from Binance WS BBA

## 4. Bugs Found and Fixed

| # | Bug | Location | Severity | Fix |
|---|-----|----------|----------|-----|
| 1 | Fee model uses wrong formula | execution_engine.py | CRITICAL | Replaced with correct `p*(1-p)*rate` |
| 2 | Fictional time-based fee multipliers | execution_engine.py | HIGH | Removed entirely |
| 3 | Dead zone blocks 50% of price range | config.py:54-55 | HIGH | Widened to 0.05-0.95 |
| 4 | Min edge too high for maker-only | config.py:56 | HIGH | 150 -> 80 bps |
| 5 | StrategyResult uses wrong field name | ws_order_flow.py, ws_flash_snipe.py | MEDIUM | `model_probability` -> `signal_score` |
| 6 | OFI/flash snipe imported but never used | decision_engine.py:9-10 | MEDIUM | Wired behind feature flag |
| 7 | Default vol too high (60-70%) | fair_value_model.py | MEDIUM | 50% default, 60% fallback |
| 8 | Fee rate default 1.56% in edge calc | runner.py:2534 | MEDIUM | 0% for maker-only |
| 9 | Max consecutive loss = 2 | config.py:78 | LOW | Raised to 10 |
| 10 | No timezone context in hour blocking | runner.py:1037 | LOW | Added OPERATOR_TZ support |

## 5. Structural Debt

| Issue | Impact | Effort to Fix |
|-------|--------|--------------|
| runner.py is 6688 lines | Unmaintainable | High — needs modular extraction |
| 14 exit conditions, many overlapping | Complexity | Medium — consolidate to 6 |
| Training data from 5m markets | Invalid baselines | N/A — need new 15m data |
| Scoreboard has only 0-2 trades | No statistical power | Time — need 30 days dry-run |
| No automated CI/CD | Regressions possible | Low — add pytest to pre-commit |

## 6. Files Modified in This Audit

| File | Phase | Changes |
|------|-------|---------|
| core/execution_engine.py | 1 | New fee model class |
| core/config.py | 1, 3 | Settings fixes, dead zone removal |
| core/runner.py | 1, 3 | Timezone, fee defaults, Bayesian integration |
| core/decision_engine.py | 1 | Wired legacy strategies |
| core/fair_value_model.py | 3 | Vol defaults, realized vol improvement |
| core/learning.py | 3 | Bayesian win rate method |
| core/strategies/ws_order_flow.py | 1 | Fixed StrategyResult field |
| core/strategies/ws_flash_snipe.py | 1 | Fixed StrategyResult field |
| .env.local | 1 | Diagnostic overrides |
| scripts/dry_run_dashboard.py | 1 | Decision CSV logger |
| scripts/replay_harness.py | 2 | 90-day backtest harness |
| docs/exit_state_machine.md | 3 | Exit logic documentation |

## 7. Files Created in This Audit

| File | Purpose |
|------|---------|
| GROUND_TRUTH.md | Phase 0 measurements |
| BACKTEST_REPORT.md | Phase 2 results |
| GO_NO_GO.md | Deployment recommendation |
| DIAGNOSIS.md | This document |
| tests/test_vpn_calibrated_fees.py | Fee model tests (7) |
| tests/test_strategy_base.py | StrategyResult tests (3) |
| tests/test_strategies_migration.py | Strategy signal tests (4) |
| tests/test_legacy_strategies.py | Legacy strategy integration tests (2) |
| tests/test_fair_value_phase3.py | Fair value model tests (9) |
