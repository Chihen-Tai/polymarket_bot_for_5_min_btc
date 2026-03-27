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
)
from core.runner import (
    ExitDecision as RunnerExitDecision,
    OpenPos,
    entry_velocity_gate_rejects,
    effective_stop_loss_partial_fraction,
    extract_entry_cost_usd,
    is_loss_exit_reason,
    principal_extraction_sell_fraction,
    principal_extraction_complete,
    realistic_exit_value,
    should_force_full_loss_exit,
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
    depth_book = {
        "best_bid": 0.475,
        "best_bid_size": 1.0,
        "bid_levels": [(0.475, 1.0), (0.12, 1.0)],
    }
    depth_value, depth_fill_ratio = estimate_book_exit_value(depth_book, 2.0)
    thin_value, thin_fill_ratio = estimate_book_exit_value({"bid_levels": [(0.2, 1.0)]}, 2.0)
    depth_pos = OpenPos(slug="m", side="UP", token_id="tok2", shares=2.0, cost_usd=1.0, opened_ts=0.0)
    realistic_value = realistic_exit_value(depth_pos, 0.52, 0.48, depth_book, None)
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
        ("panic_dump_always_forces_taker", should_force_taker_exit(reason="", dry_run=True, has_panic_dumped=True) is True),
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
