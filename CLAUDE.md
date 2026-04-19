# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Python trading bot for Polymarket BTC binary "up/down" markets (15-minute windows, slug prefix `btc-updown-15m-`). It is maker-first, expiry-first, with real-time Binance WebSocket signals driving entry and exit timing. Runs on Polygon (chain ID 137) via the py-clob-client.

**The tracked `.env` sets `DRY_RUN=false` (live mode).** Real secrets (`PRIVATE_KEY`, `FUNDER_ADDRESS`, `CLOB_API_*`) live only in gitignored `.env.local` or `.env.secrets`.

---

## Commands

```bash
conda activate polymarket-bot   # Python 3.11 env
python main.py                  # run bot (mode from .env)

pytest tests/                              # all tests
pytest tests/test_trade_manager.py::test_name  # single test

# Config preset for high-frequency dry-run testing
set -a && source config_presets/dryrun_aggressive.env && set +a
python main.py
```

**Analysis scripts** (standalone, read from `data/`):
```bash
python scripts/journal_analysis.py    # fee-adjusted PnL report
python scripts/simulate.py            # backtester using Binance klines
python scripts/replay_harness.py      # walk-forward replay
python scripts/reconcile_journal.py   # audit open/closed lots
python scripts/plot_balance_curve.py  # balance chart
```

No linter is configured. `numpy` is used in `core/learning.py` but is missing from `environment.yml` and `requirements.txt` — install manually if needed.

---

## Architecture

### Execution Flow

```
main.py  →  core/runner.py::main()
  Startup: validate live requirements, load runtime state, sanity check
  Poll loop (~2s):
    market_resolver  →  find active BTC market + token IDs
    exchange         →  fetch orderbook, account balance, open positions
    ws_binance       →  real-time BTC price, trades, liquidations
    decision_engine  →  9-gate signal pipeline → side or None
    risk             →  can_place_order() gate
    runner           →  place_entry_order_with_retry()
    trade_manager    →  decide_exit() for open positions
    runner           →  supplementary exit checks (reversal, adverse, soft stop)
    state_store      →  atomic JSON state persist
  Journal all events → data/trade_journal-{mode}.jsonl
```

### Key Modules

| Module | Role |
|---|---|
| `core/runner.py` (~7k lines) | Main loop, `OpenPos`/`PendingOrder` dataclasses, all entry/exit execution, state management — the monolith |
| `core/exchange.py` | `PolymarketExchange` — CLOB REST calls, order placement, dry-run simulation |
| `core/decision_engine.py` | `explain_choose_side()` — 9-gate signal pipeline |
| `core/trade_manager.py` | `decide_exit()` — EV-aware exit state machine |
| `core/config.py` | `SETTINGS` singleton — loads `.env` → `.env.local` → `.env.secrets` (last overrides) |
| `core/ws_binance.py` | Singleton `BinanceWebSocket` — `bookTicker`, `aggTrade`, `forceOrder` streams |
| `core/fair_value_model.py` | Black-Scholes binary probability from BTC price, strike, realized vol |
| `core/risk.py` | `can_place_order()` — per-window frequency, exposure, equity, daily loss limits |
| `core/risk_manager.py` | `RISK_MANAGER` — consecutive loss tracking, cooldowns, Chainlink check |
| `core/latency_monitor.py` | `LATENCY_MONITOR` — RTT/E2E percentiles, grades NORMAL/DEGRADED/CLOSE_ONLY/BLOCKED |
| `core/execution_engine.py` | Fee model, `calculate_committed_edge()`, VWAP from order ladder |
| `core/market_resolver.py` | Fetches active market from Gamma API, extracts UP/DOWN token IDs and strike |
| `core/journal.py` | Append-only JSONL trade journal + shadow CSV for blocked signals |
| `core/learning.py` | `StrategyScoreboard` — per-strategy expectancy-based win rate, JSON persistence |
| `core/hedge_logic.py` | Structured hedge entry planning and dump trigger |
| `core/ai_advisor.py` | Optional Gemini advisor — disabled by default (`AI_ADVISOR_ENABLED=false`) |

### Signal Pipeline (`explain_choose_side`, 9 gates)

1. Missing data guard
2. Fair value via Black-Scholes
3. Sniper filter — only trade extreme zones (outside ±5% neutral band)
4. Volatility gate — BTC 5m kline range must exceed threshold
5. OFI override — strong order flow can bypass volatility gate
6. Macro trend — BTC window delta direction alignment
7. Momentum — dual velocity confirm (lagged + current Binance)
8. Time regime — edge requirements vary by `secs_left` bucket
9. Edge scoring — rank UP vs DOWN, select best

### Exit State Machine (two layers)

**Layer 1 — `trade_manager.decide_exit()`:** catastrophic stop (−30%), fomo premium (bid > FV+1% with PnL > 5% and secs ≤ 60), sniper hold-to-settle (secs ≤ 15, FV ≥ 95%), deadline final exit (secs ≤ 15, FV < 95%), default hold.

**Layer 2 — runner supplementary:** profit reversal, Binance adverse exit, Binance profit protect, soft stop with confirmation delay, residual force close.

---

## Persistent State

| Path | Contents |
|---|---|
| `core/.runtime_state-{dryrun\|live}.json` | Atomic JSON — open positions, pending orders, risk counters, daily PnL |
| `data/trade_journal-{mode}.jsonl` | Append-only trade events (`entry`/`exit`/`signal-blocked`) |
| `data/run_journal-{mode}.jsonl` | Per-run startup/shutdown metadata |
| `data/shadow_journal.csv` | Blocked signals for research |
| `data/strategy_scores.json` | `StrategyScoreboard` persistence |
| `data/log-{mode}-{timestamp}.txt` | stdout tee from `main.py` |

---

## Known Constraints

- `core/runner.py` is a ~7k-line monolith — all major logic lives here by design.
- `core/resolution_source.py` Chainlink oracle is unimplemented — divergence check always skipped.
- `vpn_disable_taker_fallback` setting is never enforced (dead gate).
- `exchange.py` silently returns `0.0` on position/balance API failures.
- All operational logging uses `print()`, not the `logging` module.
