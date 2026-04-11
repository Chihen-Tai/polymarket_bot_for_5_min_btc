# BTC 5-Minute Profitability Hardening Design

## Goal

Shift the bot away from fee-heavy, slippage-heavy reactive trading and toward selective, higher-quality trades that have a materially better chance of finishing near break-even or slightly profitable after execution costs.

## Problem Statement

Current evidence shows the bot is not losing because the core idea is always wrong; it is losing because too many trades are being forced through execution paths that do not leave enough edge after taker fees, spread crossing, and late exits. The highest-risk pattern is not “missing a winner.” It is trading too often in low-quality windows, especially near fair-value pricing, then paying taker costs to realize weak or negative expectancy.

The design goal is therefore not “predict BTC better at any cost.” It is to be much more selective about when the bot participates, to prefer maker-style execution whenever practical, and to exit weak trades earlier so they do not become expensive deadline cleanups.

## Design Principles

1. **Selectivity over activity** — fewer trades is acceptable if net expectancy improves.
2. **Execution-aware expectancy** — a trade is invalid if expected edge does not survive fees and slippage.
3. **Maker first, taker only when justified** — taker flow is a fallback, not the default alpha engine.
4. **Middle-price skepticism** — markets near 0.50 are structurally dangerous because fee burden is highest while directional certainty is weakest.
5. **Earlier recycling of bad trades** — weak positions should be recycled before the final deadline zone.

## Proposed Strategy Changes

### 1. Entry Participation Filter

Add a stricter trade participation layer before the bot is allowed to enter.

#### Rules

- Introduce a **no-trade zone** around 0.50 where the bot either refuses to trade or requires a much larger edge.
- Increase required edge when all of the following are true:
  - entry price is close to 0.50,
  - order book depth is thin,
  - recent strategy history is weak or sparse,
  - the trade would likely need taker execution to get filled.
- Require stronger agreement between model probability, websocket momentum, and book structure before entering.
- Prefer not to enter when the expected trade only works if BTC keeps moving immediately in the bot’s favor.

#### Expected effect

This reduces low-conviction trades that currently end up as scratchy or fee-negative outcomes.

### 2. Execution Mode Changes

Execution should stop assuming that a mediocre edge can be rescued by late aggressive fills.

#### Rules

- **Maker-first remains the default path**.
- Normal maker-to-taker fallback stays available, but only when the trade still has enough post-cost edge.
- Emergency forced taker exits remain more aggressive than normal fallback, but routine exits should not degrade into “pay anything to get done.”
- The bot should explicitly block trade entry when projected net edge after fee/slippage buffer is too small to justify any later taker fallback.

#### Expected effect

This should reduce the number of trades that look fine on observed/mark pricing but become negative after real execution.

### 3. Exit Policy Adjustments

The bot should lose less by being willing to leave earlier.

#### Rules

- Keep profitable partial / principal extraction behavior, but bias toward **earlier cleanup of weak trades**.
- Reduce reliance on last-window deadline exits for trades that never developed.
- Preserve the existing “free runner” logic after principal extraction, but make weak pre-principal positions easier to recycle.
- Treat active-close PnL quality as a first-class signal: if active closes are consistently fee-negative, the bot should become more conservative in future entries.

#### Expected effect

This shifts losses away from late, expensive taker exits and toward earlier, smaller, more controlled outcomes.

### 4. Adaptive Safety Mode

Add a runtime regime switch that responds to recent execution quality.

#### Rules

- Enter **conservative mode** when recent active-close trades are net negative beyond a configured threshold.
- In conservative mode:
  - widen no-trade zone,
  - increase required edge,
  - reduce willingness to taker fallback,
  - possibly reduce position size or skip the next N windows.
- Recover from conservative mode only after a small streak of healthier executions or enough inactive time.

#### Expected effect

This prevents the bot from repeatedly trading the same hostile microstructure regime.

## External Research That Informed This Design

The external material consistently pointed in the same direction:

- Short-horizon BTC microstructure edges exist, but **fees and execution quality dominate realized profitability**.
- Around fair-value pricing, taker strategies are often mathematically underwater after dynamic fees.
- The more durable 2026-style edge in Polymarket BTC 5m appears closer to **selective maker behavior and inventory/quote discipline** than to indiscriminate last-second taker sniping.
- Simple “copy the fast wallets” logic is unreliable because the true edge is mostly speed and execution quality, not readable conviction.

This design does **not** fully convert the bot into a market-making system. Instead, it adapts the current architecture toward the same underlying lesson: be more selective, be more execution-aware, and avoid paying taker costs in mediocre setups.

## Scope of Implementation

### In scope

- Stricter entry gating around neutral pricing and weak execution conditions.
- Stronger execution-aware edge thresholds.
- Adjustments to maker-first / taker fallback policy.
- Conservative mode based on recent active-close quality.
- Tests that verify the new gating and execution decision behavior.
- Verification using bot-facing outputs, not only unit tests.

### Out of scope

- Full market-making engine redesign.
- Rewriting the strategy around external wallet copying.
- Any claim of guaranteed profitability.
- Exchange-infrastructure latency optimization beyond current architecture.

## Acceptance Criteria

The design should only be considered successful if the implementation produces both code-level and trading-quality evidence.

### Code-level

- New logic is covered by targeted tests.
- Existing scoped regression tests continue to pass.
- No new syntax/type issues are introduced.

### Trading-quality

Compared with current behavior, recent reports should show some combination of:

- lower taker usage rate,
- lower frequency of `deadline-*` active-close exits,
- improved `active-close` fee-adjusted pnl,
- fewer low-quality entries around neutral prices,
- lower ratio of trades whose observed edge disappears after execution.

## Risks

1. **Over-filtering risk** — the bot may become too selective and barely trade.
2. **Under-fitting to current regime** — market behavior may change, making static penalties too blunt.
3. **Maker miss risk** — better execution quality may come at the cost of missing some otherwise profitable trades.
4. **False confidence risk** — small sample improvements may look better than they are.

## Recommendation

Proceed with a selective-profitability hardening pass rather than a prediction-model rewrite.

The first implementation should focus on:

1. stronger neutral-zone / fee-aware entry gating,
2. stricter maker-first execution discipline,
3. adaptive conservative mode based on recent active-close performance,
4. verification through both tests and ledger/report metrics.
