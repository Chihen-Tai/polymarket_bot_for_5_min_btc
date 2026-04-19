# Phase 1.3 Fee Model Audit

Generated: 2026-04-19
Sources verified against current Polymarket docs:

- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/market-makers/maker-rebates

## Current Official Rules

The current docs say:

- Crypto taker fee rate is `0.072`, maker fee is `0`, maker rebate is `20%`.
- Taker fees use `fee = C x feeRate x p x (1 - p)` where `C` is shares.
- Fees are applied by the protocol at match time.
- SDK users do not manually inject fee data into the order payload; the docs point to `getClobMarketInfo(conditionID)` for per-market fee parameters.
- Maker rebates are paid daily in USDC and are funded from taker fees. They do not reduce the taker fee formula itself.

## Repo Fee Paths

| Path | What it does | Status vs current docs |
| --- | --- | --- |
| `core/execution_engine.py:59-145` | Dynamic fee curve via `PolymarketFeeModel` | Partially correct |
| `core/runner.py:2751-2762` plus `core/exchange.py:315-318` | Uses a flat assumed `fee_rate` from `get_fee_rate_bps()` | Divergent |
| `core/runner.py:2569-2591` | Converts that flat fee rate into a coarse required-edge floor | Divergent |
| `scripts/journal_analysis.py:722-770` | Uses a flat `rate=0.02` for journal fee estimates | Divergent |

## Price Checks

Comparison basis: 100 shares, which matches the Polymarket fee tables.

| Price | Docs fee for 100 shares | `execution_engine` with `rate=0.072`, `rebate=0` | `execution_engine` with current rebate handling (`rebate=0.2`) | Verdict |
| --- | --- | --- | --- | --- |
| `0.25` | `1.3500` | `1.3500` | `1.0800` | Match only when rebate is not subtracted from taker fees |
| `0.50` | `1.8000` | `1.8000` | `1.4400` | Same issue |
| `0.75` | `1.3500` | `1.3500` | `1.0800` | Same issue |

Verification math:

- Docs table for crypto shows `1.35`, `1.80`, `1.35` for 100 shares at `0.25`, `0.50`, `0.75`.
- `core/execution_engine.PolymarketFeeModel.calculate_taker_fee()` matches those values exactly if `size_usd` is set to the 100-share notional (`25`, `50`, `75`) and `rebate_rate=0`.
- The current code multiplies taker fees by `(1 - rebate_rate)` at `core/execution_engine.py:127-128`, which undercharges taker fees by `20%`.

## Material Divergences

1. `core/execution_engine.py:127-128` incorrectly reduces taker fees by maker rebate.
2. `core/runner.py` does not use `PolymarketFeeModel` at all for entry gating. It uses a flat fallback from `core/exchange.py:315-318`.
3. `core/exchange.py:315-318` pulls `report_assumed_taker_fee_rate`, but that setting is not defined on `Settings`; it silently falls back to `0.0156`.
4. `core/runner.py:2572-2591` applies a price-insensitive fee floor. Current docs are explicitly price-sensitive through `p x (1 - p)`.
5. `scripts/journal_analysis.py:722-770` hardcodes a `0.02` rate, which is not the current crypto fee rate.
6. `core/execution_engine.py:71-115` fetches `feeSchedule` from Gamma market data, while current docs point to `getClobMarketInfo(conditionID)` as the official per-market fee source.

## Interpretation

There is no single repo-wide fee model today.

- The dynamic curve in `execution_engine.py` can reproduce the docs if used carefully and if rebate is not netted against taker fees.
- The live entry gate in `runner.py` uses a disconnected flat approximation.
- The journal analysis script uses a third, separate approximation.

That means fee-adjusted strategy ranking, live entry gating, and post-trade reporting are not operating on the same fee assumptions.

## Verification Gate Result

The operator brief asked for three spot checks at `0.25`, `0.50`, and `0.75`.

- Direct docs match: yes, for `core/execution_engine.py` after disabling the rebate subtraction.
- Current repo behavior matches within 1 bp: no, because the runner and journal paths still use different fee models.
- Root cause is explainable: yes. The mismatch is architectural, not just numeric.
