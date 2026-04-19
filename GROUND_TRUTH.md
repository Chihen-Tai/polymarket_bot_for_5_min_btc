# GROUND TRUTH — Phase 0

**Date:** 2026-04-18
**Operator:** Taipei (UTC+8), Linux box + Japan VPN
**Measured from:** macOS dev machine (not the VPN endpoint)

---

## 1. Network Latency

> **NOTE:** These measurements are from the dev Mac, NOT from the Japan VPN
> Linux box. The operator reports ~842ms RTT from the VPN endpoint. `mtr` was
> not installed on this machine; ICMP ping was used instead.

### clob.polymarket.com (via ping -c 20)

```
20 packets transmitted, 20 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 11.997/20.336/39.778/8.287 ms
```

### data-api.polymarket.com (via ping -c 20)

```
20 packets transmitted, 20 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 11.791/17.229/31.844/5.569 ms
```

### ACTION REQUIRED

The operator must run from the **actual VPN endpoint** (Linux box, Japan):

```bash
mtr -rw -c 50 clob.polymarket.com
mtr -rw -c 50 data-api.polymarket.com
```

If p90 RTT exceeds 500ms, the VPN endpoint is unsuitable for any taker
execution and marginal even for maker-only.

---

## 2. Fee Model — CONFIRMED WRONG

### Actual fee schedule (from gamma-api, slug `btc-updown-15m-1776487500`)

```
feeType          = crypto_fees_v2
feesEnabled      = True
feeSchedule      = {
  "exponent":   1,
  "rate":       0.072,      # 7.2% BASE rate
  "takerOnly":  true,
  "rebateRate":  0.2         # 20% rebate (expires 2026-04-30)
}
makerBaseFee     = 1000
takerBaseFee     = 1000
```

### Correct formula

```
taker_fee = rate x p^exponent x (1-p)^exponent x shares
          = 0.072 x p x (1-p) x shares          (exponent=1)

With 20% rebate (until 2026-04-30):
effective_fee = 0.0576 x p x (1-p) x shares
```

### Bot's hardcoded model (execution_engine.py:56-71)

```python
BASE_TAKER_RATE = 0.0156          # WRONG - flat rate, ignores p x (1-p)
# Also applies fictional time-based multipliers:
# >10min: 1.0x, 5-10min: 1.4x, 2-5min: 2.0x, 1-2min: 3.0x, <1min: 4.5x
# These multipliers DO NOT EXIST in the Polymarket API.
```

### Comparison at common entry prices

| Entry Price (p) | Actual Fee (w/ rebate) | Bot Assumes (flat) | Actual Fee (NO rebate, after Apr 30) |
|-----------------|----------------------|---------------------|--------------------------------------|
| 0.10            | 0.518%               | 1.56%               | 0.648%                               |
| 0.20            | 0.922%               | 1.56%               | 1.152%                               |
| 0.30            | 1.210%               | 1.56%               | 1.512%                               |
| 0.40            | 1.382%               | 1.56%               | 1.728%                               |
| 0.50            | 1.440%               | 1.56%               | 1.800%                               |
| 0.60            | 1.382%               | 1.56%               | 1.728%                               |
| 0.70            | 1.210%               | 1.56%               | 1.512%                               |
| 0.80            | 0.922%               | 1.56%               | 1.152%                               |
| 0.90            | 0.518%               | 1.56%               | 0.648%                               |

### Impact

- **Currently (with rebate):** The bot OVERESTIMATES fees at extreme prices
  (p<0.30 or p>0.70) and slightly underestimates near p=0.50. This means it
  rejects edge at extremes (where the sniper strategy actually operates!) while
  being approximately correct at mid-prices it rarely trades.
- **After April 30 (no rebate):** Fees at p=0.50 rise to 1.80%, making the
  bot's 1.56% assumption an underestimate. The bot will think it has edge when
  it doesn't.
- **The time multipliers are fabricated.** The bot applies 2x-4.5x fee
  escalation near expiry that doesn't exist on Polymarket. This makes the bot
  extremely conservative in the final minutes -- exactly when binary outcomes
  become more predictable.
- **Maker fee is 0%.** The bot's `calculate_maker_fee` applies `p x (1-p)` to
  the maker rate, but maker rate should be 0 (post-only orders). The
  `takerOnly: true` flag in the API confirms this.

---

## 3. Recent Market Outcomes

> **NOT YET COLLECTED.** Scraping 672 resolved markets from the last 7 days
> requires iterating epoch-based slugs via gamma-api. This should be done from
> the operator's Linux box with a script.
>
> **Required script:** `scripts/scrape_outcomes.py` -- iterate slugs from
> `btc-updown-15m-{epoch}` for the last 7 days, fetch resolution and final
> prices, output CSV.

### What we know from training data

The training data CSVs contain **5m** market trades, not 15m:
- Log files reference `btc-updown-5m-*` slugs
- The bot was recently reconfigured from 5m to 15m markets
  (`market_slug_prefix=btc-updown-15m-` in config, but historical data is 5m)

---

## 4. ENTRY_BLOCKED_UTC_HOURS Derivation

### CONFIRMED: Derived from 775 trades

From `.env:364`:
```
# Data source: analysis of 4 CSV historical trade files (775 total trades)
```

Original Chinese: `資料來源：分析 4 份 CSV 歷史交易資料（775 筆總交易）`

### Blocked hours (from `.env:375`)

```
ENTRY_BLOCKED_UTC_HOURS=1,8,13,20,21,23
```

| UTC Hour | ET Equiv | Win Rate | PnL     | Rationale                       |
|----------|----------|----------|---------|---------------------------------|
| 01       | 9 PM     | 24.2%    | -$14.60 | Asian late-night thin liquidity |
| 08       | 4 AM     | 52%      | -$6.39  | Large single losses             |
| 13       | 9 AM     | 30.0%    | -$15.27 | Pre-US-open noise               |
| 20       | 4 PM     | 44.4%    | -$7.73  | US afternoon chop               |
| 21       | 5 PM     | 43.3%    | -$9.19  | US afternoon                    |
| 23       | 7 PM     | 33.3%    | -$13.01 | Asian close                     |

### Problems

1. **Sample size is far too small.** 775 trades across 24 hours = ~32 trades
   per hour on average. The blocked hours likely have even fewer. This is
   nowhere near statistically significant.
2. **Data is from 5m markets, not 15m.** The bot was recently switched to 15m
   markets. The hourly PnL patterns may differ significantly.
3. **The blocked hours are set in `.env` (git-tracked).** `config.py:182`
   defaults to `""` (empty), but `.env` sets them to `1,8,13,20,21,23`.
4. **6 of 24 hours blocked = 25% of trading time eliminated** based on
   statistically insignificant data from a different market type.

### Actual CSV trade counts

```
Polymarket-History-2026-03-21.csv:  489 rows
Polymarket-History-2026-03-22.csv:  650 rows
Polymarket-History-2026-03-29.csv: 1000 rows
Polymarket-History-2026-04-07.csv: 1000 rows
TOTAL:                             3139 rows (includes buy+sell, ~1570 round trips)
```

The 775 figure likely comes from filtering to only "decisive" or one-sided
trades from these 3139 raw rows.

---

## Summary of Critical Findings

| # | Finding | Severity | Impact |
|---|---------|----------|--------|
| 1 | Fee model uses wrong formula (flat rate vs p x (1-p) x rate) | **CRITICAL** | Edge calculations wrong at all prices |
| 2 | Fee model applies fictional time-based multipliers | **HIGH** | Rejects valid late-game entries |
| 3 | Rebate expiry (Apr 30) not tracked | **HIGH** | Post-April fees will be underestimated |
| 4 | Fee rate never fetched from API -- hardcoded 1.56% | **HIGH** | Will break if Polymarket changes rates |
| 5 | Hour blocking based on 775 trades from 5m markets | **MEDIUM** | Blocks 25% of time with no statistical basis |
| 6 | Historical data is from 5m markets, bot now targets 15m | **MEDIUM** | All historical analysis may be invalid |
| 7 | VPN latency not measured from actual endpoint | **MEDIUM** | MAX_VPN_LATENCY_MS may be wrong |
| 8 | Dead imports in decision_engine.py (lines 9-10) | **LOW** | get_ofi_signal, get_flash_snipe_signal unused |
