# Phase 1.1 Codemap

Generated: 2026-04-19
Scope: read-only audit of `core/`

## Entry Path

`main.py` calls `core.runner.main()`.

The `model-fade_retail_fomo` path is:

1. `core/runner.py:4152-4287` resolves the active market, fetches both order books, and pulls Binance data.
2. `core/runner.py:4375-4388` calls `core.decision_engine.explain_choose_side()`.
3. `core/decision_engine.py:258-264` computes fair value via `core.fair_value_model.get_fair_value()`, which blends Black-Scholes and microstructure in `core/ensemble_models/ensemble.py:12-41`.
4. `core/decision_engine.py:364-381` creates the `fade_retail_fomo` sniper candidate when edge clears the sniper threshold.
5. `core/decision_engine.py:125-156` turns that into `strategy_name="model-fade_retail_fomo"`.
6. `core/runner.py:4406-4413` calls `select_ranked_entry_candidate()`.
7. `core/runner.py:2857-2865` applies the hard VPN entry gates first: `VPN_LATENCY_BLOCK` and `VPN_WS_STALE_BLOCK`.
8. `core/runner.py:2811-2846` and `2679-2784` score the candidate with scoreboard-adjusted probability and the fee floor.
9. `core/runner.py:5920-6047` sends the maker-first entry through `place_entry_order_with_retry()`.
10. `core/exchange.py:823-999` places the actual Polymarket order.

Verification target from the operator log: the logged strategy name `model-fade_retail_fomo` maps to `core/decision_engine.py:365` and `core/decision_engine.py:375`, then to `core/runner.py:4440-4451`.

## Capability Map

| Capability | Primary implementation |
| --- | --- |
| Market discovery | `core/market_resolver.py:86-130` `resolve_latest_btc_token_ids()` |
| WS subscription | `core/ws_binance.py:10-212` `BinanceWebSocket` |
| OFI / signal computation | `core/indicators.py:98-105` `compute_buy_sell_pressure()`, `core/strategies/ws_order_flow.py:27-106`, `core/ensemble_models/microstructure.py:13-53`, `core/decision_engine.py:301-325` |
| `model-fade_retail_fomo` strategy | `core/decision_engine.py:364-381` |
| Entry decision and ranking | `core/decision_engine.py:222-425`, `core/runner.py:2530-2895`, `core/runner.py:4375-4478` |
| Order placement | `core/runner.py:2187-2265`, `core/runner.py:5877-6065`, `core/exchange.py:823-999` |
| Hedge logic | `core/runner.py:6079-6125` for structured entry hedge; `core/hedge_logic.py` only provides helpers |
| Exit / settlement | `core/trade_manager.py:23-86`, `core/runner.py:4527-4872`, `core/exchange.py:1088-1225`, `core/runner.py:2407-2415` |
| Journaling | `core/journal.py:39-156`, `core/run_journal.py:23-85`, `core/state_store.py:11-30`, `core/runtime_paths.py:13-27` |
| Latency gate | `core/latency_monitor.py:58-98`, `core/runner.py:2467-2527`, `core/runner.py:2857-2865` |

## Module Inventory

- `core/__init__.py`: no public symbols; package marker.
- `core/ai_advisor.py`: `AIAdvisor`; optional external AI advisory surface.
- `core/calibration.py`: `FillProbabilityCalibrator`; logistic-regression fill calibration for maker orders.
- `core/config.py`: `Settings`, `load_repo_env`; runtime configuration loader and env-backed settings object.
- `core/decision_engine.py`: `get_outcome_prices`, `seconds_to_market_end`, `check_arbitrage`, `explain_choose_side`, `choose_side`; core signal generation and candidate building.
- `core/dispatcher.py`: `TradeDispatcher`; dispatch wrapper, not on the hot path shown in `runner.py`.
- `core/ensemble_models/__init__.py`: no public symbols; package marker.
- `core/ensemble_models/ensemble.py`: `EnsembleAggregator`; blends theoretical and microstructure probabilities.
- `core/ensemble_models/microstructure.py`: `MicrostructureModel`; turns Binance BBA skew into a probability modifier.
- `core/exchange.py`: `Account`, `Position`, `PolymarketExchange`, `estimate_order_shares`, `minimum_order_usd`, `order_below_minimum_shares`, `plan_live_order`, `taker_sell_worst_price`, `parse_balance_allowance_available_shares`, `select_live_close_exit_value`, `estimate_entry_avg_price_from_asks`, `estimate_exit_value_from_bids`, `estimate_book_exit_value`, `market_is_fee_enabled`, `get_fee_rate_bps`, `estimate_book_exit_floor_price`, `estimate_hedge_exit_value`; Polymarket API, orderbook, and execution adapter.
- `core/execution_engine.py`: `PolymarketFeeModel`, `get_vwap_from_ladder`, `calculate_committed_edge`; fee curve and committed-edge calculator used by the sniper logic.
- `core/executor.py`: `AsyncExecutor`; async helper surface, not used by the main loop.
- `core/fair_value_model.py`: `norm_cdf`, `calculate_binary_probability`, `calculate_realized_vol`, `get_fair_value`; fair-value engine for the binary market.
- `core/hedge_logic.py`: `HedgeState`, `opposite`, `should_trigger_dump`; helper logic for direction inversion and dump-trigger detection.
- `core/http.py`: `request_json`, `request_json_with_session`; retrying HTTP helpers.
- `core/indicators.py`: `lsma`, `calc_zlsma`, `calc_atr`, `calc_chandelier_exit`, `compute_cvd`, `compute_buy_sell_pressure`, `calc_rsi`, `calc_ema`, `calc_macd`; technical and order-flow indicators.
- `core/journal.py`: `new_event_id`, `set_journal_context`, `clear_journal_context`, `append_event`, `append_shadow_event`, `read_events`, `format_entry_summary`, `format_exit_summary`, `replay_open_positions`, `summarize_reconciliation`; trade journal storage and reconstruction.
- `core/latency_monitor.py`: `LatencyMonitor`; RTT and E2E latency tracking plus VPN-quality classification.
- `core/learning.py`: `TradeOutcome`, `StrategyScoreboard`; expectancy-based strategy scoring and Bayesian win-rate smoothing.
- `core/market_resolver.py`: `MarketResolutionError`, `resolve_latest_btc_token_ids`; current BTC 15m market lookup.
- `core/microstructure.py`: `calculate_ofi`, `get_book_skew`; orderbook imbalance helpers.
- `core/notifier.py`: `notify_discord`; Discord notification sender.
- `core/resolution_source.py`: `get_chainlink_btc_price`, `check_resolution_divergence`; Chainlink resolution abstraction and divergence guard.
- `core/risk.py`: `RiskState`, `current_5min_key`, `update_window`, `can_place_order`; stateless risk gating helpers.
- `core/risk_manager.py`: `RiskManager`; stateful latency, cooldown, and exposure guard.
- `core/run_journal.py`: `RunJournal`; run-level lifecycle journal.
- `core/runner.py`: `GracefulStop`, `OpenPos`, `PendingOrder`, `RuntimeFlags`, plus the orchestration functions listed below; primary trading loop and execution state machine.
- `core/runtime_paths.py`: `mode_label`, `trade_journal_path`, `run_journal_path`, `runtime_state_path`; runtime file-path conventions.
- `core/state_store.py`: `load_state`, `save_state`, `serialize_positions`; persistent runtime state storage.
- `core/strategies/__init__.py`: no public symbols; package marker.
- `core/strategies/base.py`: `StrategyResult`; shared strategy result dataclass.
- `core/strategies/mean_reversion.py`: `run`; legacy mean-reversion strategy entry point.
- `core/strategies/ws_flash_snipe.py`: `get_flash_snipe_signal`; velocity-based legacy snipe strategy.
- `core/strategies/ws_order_flow.py`: `get_ofi_signal`; legacy OFI-confirmed strategy.
- `core/strategy.py`: `Signal`, `simple_5min_momentum`; older toy momentum strategy.
- `core/trade_manager.py`: `ExitDecision`, `EntryDecision`, `decide_exit`, `maybe_reverse_entry`, `should_block_same_market_reentry`, `can_reenter_same_market`; exit decisions and re-entry policy.
- `core/ws_binance.py`: `BinanceWebSocket`; Binance combined stream client for BBA, agg trades, and liquidations.

## Runner Hotspots

The public functions in `core/runner.py` that materially affect the audited live behavior are:

- `place_entry_order_with_retry()`
- `update_network_guard()`
- `required_trade_edge()`
- `apply_scoreboard_aux_probability()`
- `summarize_entry_edge()`
- `score_entry_candidate()`
- `collect_ranked_entry_candidates()`
- `select_ranked_entry_candidate()`
- `select_ranked_entry_candidate_for_side()`
- `main()`

## Audit Notes

- The structured hedge is not implemented in `core/hedge_logic.py`; it is inlined inside `core/runner.py:6079-6125`.
- The Chainlink resolution adapter is still a stub in `core/resolution_source.py:5-12`.
- The live entry path is maker-first, but candidate selection is blocked before strategy ranking whenever `LATENCY_MONITOR.is_blocked()` trips.
