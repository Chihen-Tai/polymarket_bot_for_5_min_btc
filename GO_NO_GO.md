# GO / NO-GO Recommendation

**Date:** 2026-04-18
**Author:** Claude Opus 4.6 (automated audit)
**Scope:** polymarket_bot_for_15_min_btc — deploy, dry-run-only, or abandon?

---

## Verdict: DRY-RUN ONLY (30 days minimum)

**Do NOT deploy live with real capital yet.**

---

## Rationale

### What was fixed (Phase 1)

| Fix | Impact |
|-----|--------|
| Fee model replaced (flat 1.56% -> correct `p*(1-p)*0.072`) | Edge calculations now accurate |
| Fictional time multipliers removed | Bot no longer rejects valid late-game entries |
| Maker fee confirmed 0% | Maker-only strategy validated |
| Dead zone removed (0.25-0.75 blocked) | Bot can now trade full price range |
| Min edge lowered 150 -> 80 bps | More opportunities with maker-only |
| OFI/flash snipe strategies wired in (behind flag) | Additional signal sources available |
| Timezone-aware hour blocking | Operator can see local time context |

### What the backtest showed (Phase 2)

- **Theoretical edge exists** — CEX-to-PM lag arbitrage is real
- **89.5% win rate** on 716 simulated trades (90-day, optimistic assumptions)
- **BUT:** Results use minute-5 BTC look-ahead, which overstates the bot's actual signal quality

### What remains unproven

1. **Real signal quality**: The bot's actual signals (OFI, flash snipe, sniper fade) have never been validated on 15m markets. All historical data is from 5m markets.
2. **Fill rate**: Maker order fill rate on Polymarket 15m markets is unknown. The 40% assumption in backtest is a guess.
3. **Adverse selection**: When maker orders fill, it may be because the market moved against you. This is not modeled.
4. **Post-rebate economics**: The 20% fee rebate expires 2026-04-30. After that, taker fees at p=0.50 rise from 1.44% to 1.80%. The bot fetches this dynamically now, but the edge narrows.
5. **Competition**: Other bots exploiting the same CEX-PM lag will compress edge over time.

### Why not "abandon"?

- The fee model fixes are real improvements that make the bot's calculations correct
- The dead zone removal opens significant opportunity space
- Maker-only execution at 0% fee is genuinely advantaged
- The architecture is sound (just misconfigured)
- Capital at risk is tiny ($0.50/trade in dry-run)

### Why not "deploy live"?

- Zero validated live trades on 15m markets
- Backtest has known look-ahead bias
- No walk-forward validation
- Insufficient training data (775 trades from wrong market type)
- Rebate expiry in 12 days changes economics

---

## Recommended Path

### Immediate (now)

1. Deploy dry-run on the Japan VPN Linux box
2. Collect `data/decisions.csv` via `scripts/dry_run_dashboard.py`
3. Monitor for 30 days minimum

### After 30 days dry-run

4. Analyze 2000+ decisions from `decisions.csv`
5. Compute real signal accuracy, fill rate, edge distribution
6. Re-run `scripts/replay_harness.py` with real fill/accuracy data
7. If expectancy > 30 bps on 2000+ decisions: proceed to micro-live ($1-$5/trade)

### After 14 days micro-live

8. If net positive PnL on 500+ trades: scale to $10+
9. If net negative: evaluate Phase 4 options (copy-trade basket, spread capture, or stat arb)

---

## Phase 4 Readiness (if needed)

| Option | Complexity | Capital | Recommended? |
|--------|-----------|---------|-------------|
| A: Copy-Trade Basket | Low (500 LOC) | $50 | Yes — highest P(success) |
| B: Spread Capture | Medium | $50 | Maybe — adverse selection risk |
| C: Cross-Venue Stat Arb | High | $1000+ | No — out of scope for current capital |

If dry-run fails, **Option A (copy-trade)** is the recommended next step.

---

## Success Criteria Checklist

- [ ] 30 consecutive days dry-run with positive expectancy on >= 2000 decisions
- [ ] Max drawdown < 20%
- [ ] Sharpe > 0.8
- [ ] 14 days micro-live ($1-$5) with net positive PnL
- [ ] 500 live trades with Sharpe > 1.2

**Current status: 0 of 5 criteria met. Dry-run phase not yet started.**
