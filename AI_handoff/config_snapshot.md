# Phase 1.2 Config Snapshot

Generated: 2026-04-19
Scope: current runtime values in this workspace, plus intended overrides found in tracked env files

## Loader Status

`core/config.py` expects `python-dotenv`, but that package is not installed in this workspace. The import falls back to a no-op loader at `core/config.py:8-12`, so `.env`, `.env.local`, and `.env.secrets` are currently ignored.

Verification:

- `importlib.util.find_spec("dotenv")` returned `None`.
- Re-running `load_repo_env()` left `MAX_ORDER_USD`, `MAX_VPN_LATENCY_MS`, `MIN_ENTRY_PRICE`, and `SCOREBOARD_AUX_WEIGHT` unset in `os.environ`.

Result: the effective runtime values below are the dataclass defaults from `core/config.py`, not the intended values in `.env` or `.env.local`.

## Requested Thresholds

| Requested name | Runtime value | Internal setting | Source | Inactive file override(s) |
| --- | --- | --- | --- | --- |
| `MIN_ENTRY_PRICE` | `0.05` | `SETTINGS.min_entry_price` | `core/config.py:57` | `.env:100` sets `0.35` |
| `OFI_THRESHOLD` | `0.15` | `SETTINGS.min_poly_ofi_threshold` | `core/config.py:87`; used by `core/decision_engine.py:322-325` | none found |
| `SNIPER_EDGE_THRESHOLD` | `80 bps` = `0.008` | `SETTINGS.min_sniper_edge_bps` | `core/config.py:56`; used by `core/decision_engine.py:359` | `.env.example:74` shows older `1200` |
| `VPN_LATENCY_BLOCK_MS` | `600.0` | `SETTINGS.max_vpn_latency_ms` | `core/config.py:69`; enforced by `core/latency_monitor.py:70-79` | `.env.local:16` sets `950` |
| `WS_STALE_THRESHOLD_S` | `1.5` | `SETTINGS.vpn_max_ws_age_sec` | `core/config.py:66`; enforced by `core/runner.py:2863-2865` | none found |
| `MAX_POSITION_USD` | no single setting | split across `max_order_usd`, `live_order_hard_cap_usd`, `max_exposure_usd` | `core/config.py:46`, `49`, `218`; applied at `core/runner.py:5369-5422`, `5814-5828`, and `core/exchange.py:881-935` | `.env:10` `MAX_ORDER_USD=1.0`, `.env:18` `LIVE_ORDER_HARD_CAP_USD=5.0`, `.env:20` `MAX_EXPOSURE_USD=9999.0`, `.env.local:11` `MAX_ORDER_USD=0.5` |
| `HEDGE_FRACTION` | `0.5` | `SETTINGS.hedge_ratio` | `core/config.py:207`; used by `core/runner.py:6080-6083` | no env override found |

## Position and Hedge Sizing Knobs

| Setting | Runtime value | Source | Note |
| --- | --- | --- | --- |
| `SETTINGS.max_order_usd` | `1.0` | `core/config.py:46` | Baseline entry size before Kelly or caps |
| `SETTINGS.max_exposure_usd` | `1.0` | `core/config.py:49` | Aggregate exposure gate in `core/risk.py:48-49` |
| `SETTINGS.live_order_hard_cap_usd` | `3.0` | `core/config.py:218` | Live per-order hard cap |
| `SETTINGS.max_bet_cap_usd` | `3.0` | `core/config.py:229` | Kelly sizing cap |
| `SETTINGS.min_live_order_shares` | `5.0` | `core/config.py:47` | Venue minimum used by `plan_live_order()` |
| `SETTINGS.min_live_order_usd` | `1.0` | `core/config.py:48` | Venue min notional used by `plan_live_order()` |
| `SETTINGS.hedge_ratio` | `0.5` | `core/config.py:207` | Structured hedge size = `order_usd * hedge_ratio` |
| `SETTINGS.hedge_exit_enabled` | `False` | `core/config.py:206` | Exit-side hedge path disabled, but entry-side structured hedge still runs in `runner.py` |

## Model-Probability Weights

| Setting | Runtime value | Source | Use |
| --- | --- | --- | --- |
| `SETTINGS.scoreboard_aux_weight` | `0.3` | `core/config.py:246`; applied at `core/runner.py:2621-2624` | Blends model probability with scoreboard win rate |
| `SETTINGS.heuristic_probability_weight` | `0.5` | `core/config.py:208`; applied at `core/runner.py:2612-2620` | Down-weights heuristic probabilities toward 0.5 |
| `m1_weight` | `0.35` | `core/ensemble_models/ensemble.py:25` | Fair-value model weight |
| `m2_weight` | `0.65` | `core/ensemble_models/ensemble.py:26` | Microstructure modifier weight |
| `MicrostructureModel.max_skew_penalty` | `0.12` | `core/ensemble_models/microstructure.py:10-11` | Max OFI-driven probability shift |

## Fee-Related Constants

| Setting | Runtime value | Source | Note |
| --- | --- | --- | --- |
| `_CONSERVATIVE_FALLBACK_RATE` | `0.018` | `core/execution_engine.py:55` | Used if fee schedule fetch has never succeeded |
| `PolymarketFeeModel.exponent` | `1` | `core/execution_engine.py:64` | Matches current docs |
| `PolymarketFeeModel.taker_only` | `True` | `core/execution_engine.py:66` | Maker fee hard-coded to zero |
| `_REBATE_EXPIRY_EPOCH` | `1746057600` | `core/execution_engine.py:56` | Local expiry guard for rebate handling |
| `entry_fee_floor_buffer` | `1.0` | `core/config.py:185`; used by `core/runner.py:2572-2575` | Multiplier on coarse fee floor |
| `entry_require_maker_edge_buffer` | `0.01` | `core/config.py:194`; used by `core/runner.py:2576-2586` | Extra edge margin layered on fee floor |
| `latency_buffer_usd` | `0.02` | `core/config.py:72`; used by `core/execution_engine.py:191-193` | Execution-latency cost assumption |
| `report_assumed_taker_fee_rate` | missing from `Settings`; fallback `0.0156` used ad hoc | `core/exchange.py:315-318` | Price-insensitive assumed taker fee for `runner.py` |

## Mismatch Between Runtime and Env Files

The loaded runtime values do not match the operator-facing env files because env loading is currently inert:

| Key | Runtime | `.env` / `.env.local` |
| --- | --- | --- |
| `MAX_ORDER_USD` | `1.0` | `.env=1.0`, `.env.local=0.5` |
| `MAX_VPN_LATENCY_MS` | `600.0` | `.env.local=950` |
| `MIN_ENTRY_PRICE` | `0.05` | `.env=0.35` |
| `SCOREBOARD_AUX_WEIGHT` | `0.3` | `.env=0.05` |
| `MAX_EXPOSURE_USD` | `1.0` | `.env=9999.0` |
| `LIVE_ORDER_HARD_CAP_USD` | `3.0` | `.env=5.0` |

## Verification Notes

- The `ofi_below_threshold` log reason maps to `core/decision_engine.py:323-325`.
- The `VPN_LATENCY_BLOCK(...)` log reason maps to `core/runner.py:2857-2861`.
- The `VPN_WS_STALE_BLOCK(...)` log reason maps to `core/runner.py:2863-2865`.
- There is no first-class `MAX_POSITION_USD` or `HEDGE_FRACTION` env key in the repo; those operator terms map to multiple internal settings.
