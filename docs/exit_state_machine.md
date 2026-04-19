# Exit State Machine

## Overview

The bot's exit logic has two layers:
1. **Primary** (`core/trade_manager.py:decide_exit`) — EV-aware 15m exit rules
2. **Secondary** (`core/runner.py`) — supplementary triggers checked each poll cycle

## State Diagram

```
                         POSITION OPEN
                              |
                    +---------+---------+
                    |                   |
              [secs_left > 60]    [secs_left <= 60]
                    |                   |
                    v                   v
            +-- HOLDING --+      DEADLINE ZONE
            |             |           |
            |   check:    |     +-----+------+
            |   - catastrophic   |            |
            |   - profit_reversal |    [secs <= 15]
            |   - binance_adverse |        |
            |   - binance_profit  |   +----+----+
            |     _protect        |   |         |
            |             |       | [FV>=95%]  [FV<95%]
            +------+------+       |   |         |
                   |              | HOLD TO   DEADLINE
                   v              | SETTLE    FINAL EXIT
              still HOLDING       |
                   |              |
                   v              |
             [bid > EV+0.01      |
              & pnl > 5%]        |
                   |              |
              FOMO PREMIUM       |
                EXIT             |
                                 |
                    +------------+
                    v
               POSITION CLOSED
```

## Exit Conditions Catalog

### Layer 1: trade_manager.py (decide_exit)

| # | Condition | Reason String | Trigger |
|---|-----------|--------------|---------|
| 1 | PnL <= -30% | `catastrophic-reversal-stop` | Immediate |
| 2 | Bid > FV+0.01, PnL > 5%, secs <= 60 | `early-exit-fomo-premium` | Last minute |
| 3 | secs <= 15, FV >= 95% | `sniper-hold-to-settle-lock` | HOLD (no exit) |
| 4 | secs <= 15, FV < 95% | `deadline-final-exit` | Last 15 seconds |
| 5 | Default | `hold` | HOLD (no exit) |

### Layer 2: runner.py (supplementary)

| # | Function | Reason | When |
|---|----------|--------|------|
| 6 | `should_trigger_profit_reversal_exit` | `profit-reversal` | Drawdown >= 18% from peak, adverse velocity |
| 7 | `should_trigger_binance_adverse_exit` | `binance-adverse-exit` | Binance velocity adverse, held > 4s, profit < 8% |
| 8 | `should_trigger_binance_profit_protect_exit` | `binance-profit-protect-exit` | Large profit + adverse velocity |
| 9 | `should_force_full_loss_exit` | (wraps loss reasons) | Live mode, any loss exit reason |
| 10 | `should_arm_residual_force_close` | `residual-force-close` | After partial stop-loss scale-out |
| 11 | `should_force_taker_take_profit` | (taker mode) | Force taker for take-profit |
| 12 | `should_force_taker_profit_protection` | (taker mode) | Force taker for profit protection |
| 13 | `should_force_taker_exit` | (taker mode) | Force taker on loss exits |
| 14 | Soft stop confirmation delay | (delays stop-loss) | Waits `soft_stop_confirm_sec` before executing |

### LOSS_EXIT_REASONS (14 strings)

```
moonbag-drawdown-stop, post-scaleout-stop-loss, residual-force-close,
failed-follow-through, hard-stop-loss, smart-stop-loss, stop-loss,
stop-loss-full, stop-loss-scale-out, deadline-exit-loss,
deadline-exit-flat, deadline-exit-weak-win, stalled-trade,
break-even-giveback, max-hold-loss, max-hold-loss-extended
```

## Simplification Analysis

### Orthogonal exit dimensions (target: 6)

1. **Catastrophic stop** (Layer 1, #1) — KEEP. Non-negotiable risk control.
2. **Deadline management** (Layer 1, #3-4) — KEEP. Core to 15m binary markets.
3. **Fomo premium capture** (Layer 1, #2) — KEEP. Rational EV exit.
4. **Profit reversal protection** (Layer 2, #6) — KEEP. Protects realized gains.
5. **Binance adverse signal** (Layer 2, #7) — MERGE with #8 into single "CEX signal exit".
6. **Default hold** (Layer 1, #5) — KEEP. The base case.

### Candidates for removal

- **#9-13 (force taker variants)**: These are execution mode overrides, not exit decisions. They belong in the execution engine, not exit logic. With maker-only VPN mode (`vpn_maker_only=True`), taker forcing is contradictory.
- **#14 (soft stop delay)**: Adds complexity to delay a stop-loss by a few seconds. With maker-only, this is less relevant.
- **#10 (residual force close)**: Edge case cleanup after partial fills. Keep but simplify.

### Loss reason consolidation

The 14 loss exit reasons can be grouped into 4:
1. `stop-loss` (covers: hard, smart, full, scale-out, post-scaleout)
2. `deadline-exit` (covers: loss, flat, weak-win)
3. `stalled-trade` (covers: failed-follow-through, stalled, break-even-giveback)
4. `max-hold-loss` (covers: regular, extended, moonbag-drawdown, residual-force-close)

## Recommended Next Steps

1. Merge `binance_adverse_exit` and `binance_profit_protect_exit` into a single function
2. Move taker-forcing logic (#9, 11, 12, 13) to execution engine
3. Consolidate loss exit reasons from 14 to 4 categories
4. Add integration test: position enters, hits each exit condition, verify reason string
