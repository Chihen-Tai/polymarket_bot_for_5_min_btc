import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.exchange import (
    PolymarketExchange,
    _limit_order_type,
    estimate_book_exit_value,
    minimum_order_usd,
    order_below_minimum_shares,
    parse_balance_allowance_available_shares,
    plan_live_order,
    select_live_close_exit_value,
)
from core.runner import (
    ExitDecision as RunnerExitDecision,
    OpenPos,
    entry_slippage_breach,
    entry_velocity_gate_rejects,
    effective_stop_loss_partial_fraction,
    extract_entry_implied_avg_price,
    observed_exit_value_from_mark,
    extract_entry_cost_usd,
    is_loss_exit_reason,
    principal_extraction_sell_fraction,
    principal_extraction_complete,
    realistic_exit_value,
    sanitize_live_actual_exit_value,
    should_delay_soft_stop_scaleout,
    should_force_taker_profit_protection,
    should_trigger_profit_reversal_exit,
    should_force_full_loss_exit,
    should_force_taker_take_profit,
    should_force_taker_exit,
)


def make_paper_exchange() -> PolymarketExchange:
    original = PolymarketExchange._init_real_client
    PolymarketExchange._init_real_client = lambda self: setattr(self, "client", None)
    try:
        ex = PolymarketExchange(dry_run=True)
    finally:
        PolymarketExchange._init_real_client = original
    ex._cash = 100.0
    ex._equity = 100.0
    ex._open_exposure = 0.0
    ex._position_cost = {}
    ex._position_shares = {}
    ex.paper_balance_file = os.path.join(os.path.dirname(__file__), "tmp_paper_balance.json")
    if os.path.exists(ex.paper_balance_file):
        os.remove(ex.paper_balance_file)
    return ex


def main():
    ex = make_paper_exchange()

    class LegacyOrderType:
        POST_ONLY = "POST_ONLY"
        GTC = "GTC"

    class ModernOrderType:
        GTC = "GTC"

    value, source = ex._extract_close_usdc_received({"takingAmount": 0.9823, "makingAmount": 2.094658})
    filled, filled_source = ex._extract_close_shares_sold({"takingAmount": 0.9823, "makingAmount": 2.094658})
    entry_cost, entry_cost_source = ex._extract_entry_cost_usd({"takingAmount": 6.46, "makingAmount": 1.938})
    runner_entry_cost = extract_entry_cost_usd(
        {
            "amount_usd": 3.0039,
            "actual_entry_cost_usd": 1.938,
            "response": {"takingAmount": "6.46", "makingAmount": "1.938", "status": "matched"},
        },
        3.0039,
    )
    implied_entry_avg_price = extract_entry_implied_avg_price(
        {
            "actual_entry_cost_usd": 1.0,
            "response": {"takingAmount": "1.25", "makingAmount": "1.0"},
        },
        1.0,
    )
    slippage_breach, slippage_premium = entry_slippage_breach(
        expected_entry_price=0.475,
        actual_avg_price=0.8,
        dry_run=False,
    )
    slippage_ok, slippage_ok_premium = entry_slippage_breach(
        expected_entry_price=0.475,
        actual_avg_price=0.52,
        dry_run=False,
    )
    depth_book = {
        "best_bid": 0.475,
        "best_bid_size": 1.0,
        "bid_levels": [(0.475, 1.0), (0.12, 1.0)],
    }
    depth_value, depth_fill_ratio = estimate_book_exit_value(depth_book, 2.0)
    thin_value, thin_fill_ratio = estimate_book_exit_value({"bid_levels": [(0.2, 1.0)]}, 2.0)
    depth_pos = OpenPos(slug="m", side="UP", token_id="tok2", shares=2.0, cost_usd=1.0, opened_ts=0.0)
    realistic_value = realistic_exit_value(depth_pos, 0.52, 0.48, depth_book, None)
    observed_partial_value = observed_exit_value_from_mark(sold_shares=1.61, mark=0.365)
    sane_actual_value, sane_actual_source = sanitize_live_actual_exit_value(
        actual_exit_value_usd=1.3846,
        actual_exit_value_source="close_response_takingAmount",
        sold_shares=1.61,
        mark=0.365,
        dry_run=False,
    )
    accepted_actual_value, accepted_actual_source = sanitize_live_actual_exit_value(
        actual_exit_value_usd=0.5877,
        actual_exit_value_source="close_response_takingAmount",
        sold_shares=1.61,
        mark=0.365,
        dry_run=False,
    )
    principal_recovery_from_rejected_actual = sane_actual_value if sane_actual_value is not None else observed_partial_value
    live_close_value, live_close_source = select_live_close_exit_value(
        usdc_received_total=1.3846,
        usdc_received_source="close_response_takingAmount",
        cash_delta=0.5877,
        cash_delta_source="cash_balance_delta",
    )
    parsed_balance_shares = parse_balance_allowance_available_shares(
        "PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance: "
        "the balance is not enough -> balance: 1198827, order amount: 1200000'}]"
    )
    live_acct_ex = make_paper_exchange()
    live_acct_ex.dry_run = False
    live_acct_ex._funder = "0xabc"
    cash_calls = {"count": 0}
    value_calls = {"count": 0}

    def fake_cash_balance():
        cash_calls["count"] += 1
        return 7.0

    def fake_positions_value():
        value_calls["count"] += 1
        return 3.0

    live_acct_ex._get_cash_balance = fake_cash_balance
    live_acct_ex._get_positions_value = fake_positions_value
    acct_first = live_acct_ex.get_account()
    acct_second = live_acct_ex.get_account()
    live_acct_ex.invalidate_live_account_cache()
    acct_third = live_acct_ex.get_account()

    entry = ex.place_order("UP", 1.0, token_id_override="tok1", simulated_price=0.5)
    partial = ex.close_position("tok1", 1.0, simulated_price=0.6)
    cost_after_partial = ex._position_cost.get("tok1", 0.0)
    exposure_after_partial = ex._open_exposure
    settle = ex.close_position("tok1", 1.0, simulated_price=0.0)
    ex._cash = 100.0
    ex._position_cost = {"ghost": 4.0}
    ex._position_shares = {"ghost": 8.0}
    ex._open_exposure = 4.0
    reconciled = ex.reconcile_dry_run_positions([])
    acct = ex.get_account()

    cases = [
        ("close_response_value_prefers_taking_amount", abs((value or 0.0) - 0.9823) < 1e-9),
        ("close_response_value_source", source == "close_response_takingAmount"),
        ("close_response_filled_shares_from_making_amount", abs((filled or 0.0) - 2.094658) < 1e-9),
        ("close_response_filled_shares_source", filled_source == "close_response_makingAmount"),
        ("entry_response_cost_from_making_amount", abs((entry_cost or 0.0) - 1.938) < 1e-9),
        ("entry_response_cost_source", entry_cost_source == "entry_response_makingAmount"),
        ("runner_prefers_actual_entry_cost", abs(runner_entry_cost - 1.938) < 1e-9),
        ("entry_implied_avg_price_uses_cost_divided_by_shares", abs((implied_entry_avg_price or 0.0) - 0.8) < 1e-9),
        ("entry_slippage_breach_detects_extreme_live_fill", slippage_breach is True and abs(slippage_premium - ((0.8 / 0.475) - 1.0)) < 1e-9),
        ("entry_slippage_breach_allows_normal_fill", slippage_ok is False and slippage_ok_premium < 0.18),
        ("principal_extraction_rejects_tiny_partial_fill", principal_extraction_complete(0.0286, 1.0) is False),
        ("principal_extraction_accepts_near_full_recovery", principal_extraction_complete(0.97, 1.0) is True),
        ("principal_extraction_sell_fraction_uses_total_position_value", abs(principal_extraction_sell_fraction(1.6, 1.0) - 0.625) < 1e-9),
        ("ws_order_flow_down_blocked_on_rising_velocity", entry_velocity_gate_rejects("DOWN", "model-ws_order_flow_down", 0.0001) is True),
        ("ws_order_flow_up_blocked_on_falling_velocity", entry_velocity_gate_rejects("UP", "model-ws_order_flow_up", -0.0001) is True),
        ("ws_order_flow_down_allows_flat_or_down_velocity", entry_velocity_gate_rejects("DOWN", "model-ws_order_flow_down", 0.0) is False and entry_velocity_gate_rejects("DOWN", "model-ws_order_flow_down", -0.0001) is False),
        ("loss_exit_reason_detects_stop_loss", is_loss_exit_reason("stop-loss") is True),
        ("loss_exit_reason_rejects_take_profit", is_loss_exit_reason("take-profit-principal") is False),
        ("live_force_full_loss_exit_skips_stop_loss_scaleout", should_force_full_loss_exit(reason="stop-loss-scale-out", dry_run=False) is False),
        ("live_force_full_loss_exit_on_deadline_loss", should_force_full_loss_exit(reason="deadline-exit-loss", dry_run=False) is True),
        ("dry_run_does_not_force_full_loss_exit", should_force_full_loss_exit(reason="stop-loss-scale-out", dry_run=True) is False),
        ("live_force_taker_on_deadline_loss", should_force_taker_exit(reason="deadline-exit-loss", dry_run=False) is True),
        ("live_take_profit_prefers_taker", should_force_taker_take_profit(dry_run=False) is True),
        ("dry_run_take_profit_does_not_force_taker", should_force_taker_take_profit(dry_run=True) is False),
        ("profit_reversal_exit_triggers_after_big_profit_drawdown", should_trigger_profit_reversal_exit(has_extracted_principal=False, side="UP", profit_pnl_pct=0.18, mfe_pnl_pct=0.60, current_value_usd=1.18, peak_value_usd=1.50, ws_velocity=-0.0010, secs_left=120.0) is True),
        ("profit_reversal_exit_skips_if_peak_profit_too_small", should_trigger_profit_reversal_exit(has_extracted_principal=False, side="UP", profit_pnl_pct=0.18, mfe_pnl_pct=0.30, current_value_usd=1.18, peak_value_usd=1.50, ws_velocity=-0.0010, secs_left=120.0) is False),
        ("profit_reversal_exit_skips_if_velocity_not_adverse", should_trigger_profit_reversal_exit(has_extracted_principal=False, side="UP", profit_pnl_pct=0.18, mfe_pnl_pct=0.60, current_value_usd=1.18, peak_value_usd=1.50, ws_velocity=0.0001, secs_left=120.0) is False),
        ("profit_reversal_exit_skips_risk_free_runner", should_trigger_profit_reversal_exit(has_extracted_principal=True, side="UP", profit_pnl_pct=0.18, mfe_pnl_pct=0.60, current_value_usd=1.18, peak_value_usd=1.50, ws_velocity=-0.0010, secs_left=120.0) is False),
        ("profit_reversal_full_exit_prefers_taker_live", should_force_taker_profit_protection(reason="profit-reversal-stop", dry_run=False) is True),
        ("panic_dump_always_forces_taker", should_force_taker_exit(reason="", dry_run=True, has_panic_dumped=True) is True),
        ("soft_stop_scaleout_waits_for_confirmation_when_shallow_and_flat", should_delay_soft_stop_scaleout(reason="stop-loss-scale-out", side="UP", pnl_pct=-0.10, breach_age_sec=2.0, secs_left=120.0, ws_velocity=0.0) is True),
        ("soft_stop_scaleout_does_not_wait_when_move_is_still_adverse", should_delay_soft_stop_scaleout(reason="stop-loss-scale-out", side="UP", pnl_pct=-0.10, breach_age_sec=2.0, secs_left=120.0, ws_velocity=-0.0010) is False),
        ("soft_stop_scaleout_does_not_wait_after_confirmation_window", should_delay_soft_stop_scaleout(reason="stop-loss-scale-out", side="UP", pnl_pct=-0.10, breach_age_sec=8.0, secs_left=120.0, ws_velocity=0.0) is False),
        ("soft_stop_scaleout_does_not_wait_when_loss_is_too_deep", should_delay_soft_stop_scaleout(reason="stop-loss-scale-out", side="UP", pnl_pct=-0.20, breach_age_sec=2.0, secs_left=120.0, ws_velocity=0.0) is False),
        ("dry_run_stop_loss_partial_fraction_unchanged", abs(effective_stop_loss_partial_fraction(dry_run=True) - 0.50) < 1e-9),
        ("live_stop_loss_partial_fraction_is_heavy", abs(effective_stop_loss_partial_fraction(dry_run=False) - 0.80) < 1e-9),
        ("runner_exports_exit_decision_for_force_close_branch", RunnerExitDecision(True, "residual-force-close").reason == "residual-force-close"),
        ("limit_order_type_prefers_post_only_when_available", _limit_order_type(LegacyOrderType) == "POST_ONLY"),
        ("limit_order_type_falls_back_to_gtc", _limit_order_type(ModernOrderType) == "GTC"),
        ("minimum_order_usd_for_five_shares", abs(minimum_order_usd(0.535, 5.0) - 2.675) < 1e-9),
        ("order_below_minimum_detects_small_live_order", order_below_minimum_shares(1.0, 0.535, 5.0) == (True, 1.87, 2.675)),
        ("order_below_minimum_allows_exact_five_shares", order_below_minimum_shares(1.0, 0.2, 5.0) == (False, 5.0, 1.0)),
        ("plan_live_order_rounds_up_small_notional_gap", plan_live_order(1.0, 0.4945, 0.0, 1.0) == (2.03, 1.0038)),
        ("plan_live_order_respects_five_share_minimum", plan_live_order(1.0, 0.535, 5.0, 1.0) == (5.0, 2.675)),
        ("plan_live_order_keeps_one_dollar_when_already_valid", plan_live_order(1.0, 0.2, 0.0, 1.0) == (5.0, 1.0)),
        ("estimate_book_exit_value_sweeps_bid_depth", abs((depth_value or 0.0) - 0.595) < 1e-9 and abs(depth_fill_ratio - 1.0) < 1e-9),
        ("estimate_book_exit_value_is_conservative_when_depth_is_thin", abs((thin_value or 0.0) - 0.2) < 1e-9 and abs(thin_fill_ratio - 0.5) < 1e-9),
        ("realistic_exit_value_uses_depth_aware_bids", abs((realistic_value or 0.0) - 0.595) < 1e-9),
        ("observed_exit_value_uses_sold_shares_times_mark", abs(observed_partial_value - 0.58765) < 1e-9),
        ("sanitize_live_actual_exit_value_rejects_improbable_fill", sane_actual_value is None and sane_actual_source.startswith("sanity-rejected-")),
        ("sanitize_live_actual_exit_value_accepts_close_to_mark_fill", abs((accepted_actual_value or 0.0) - 0.5877) < 1e-9 and accepted_actual_source == "close_response_takingAmount"),
        ("rejected_actual_fill_falls_back_to_observed_mark_value", abs(principal_recovery_from_rejected_actual - observed_partial_value) < 1e-9),
        ("live_close_exit_value_prefers_cash_delta", abs((live_close_value or 0.0) - 0.5877) < 1e-9 and live_close_source == "cash_balance_delta"),
        ("parse_balance_allowance_available_shares_handles_live_error", abs((parsed_balance_shares or 0.0) - 1.198827) < 1e-9),
        ("live_account_cache_reuses_recent_snapshot", cash_calls["count"] == 2 and value_calls["count"] == 2 and acct_first.cash == acct_second.cash == acct_third.cash == 7.0 and acct_first.equity == acct_second.equity == acct_third.equity == 10.0),
        ("paper_entry_is_taker_simulated", entry.get("execution_style") == "taker-simulated"),
        ("paper_partial_close_value", abs(float(partial["actual_exit_value_usd"]) - 0.6) < 1e-9),
        ("paper_partial_close_remaining_shares", abs(float(partial["remaining_shares"]) - 1.0) < 1e-9),
        ("paper_partial_close_preserves_cost", abs(cost_after_partial - 0.5) < 1e-9),
        ("paper_partial_close_preserves_exposure", abs(exposure_after_partial - 0.5) < 1e-9),
        ("paper_partial_close_is_taker_simulated", partial.get("execution_style") == "taker-simulated"),
        ("paper_zero_settlement_allowed", abs(float(settle["actual_exit_value_usd"]) - 0.0) < 1e-9),
        ("paper_zero_settlement_clears_position", "tok1" not in ex._position_cost and "tok1" not in ex._position_shares),
        ("paper_zero_settlement_source", settle.get("close_response_value_source") == "paper_trade_simulation"),
        ("reconcile_dry_run_positions_clears_ghost_exposure", reconciled is True and acct.cash == 100.0 and acct.equity == 100.0 and acct.open_exposure == 0.0),
    ]

    failed = [name for name, ok in cases if not ok]
    if os.path.exists(ex.paper_balance_file):
        os.remove(ex.paper_balance_file)
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("OK")


def test_main():
    main()


if __name__ == "__main__":
    main()
