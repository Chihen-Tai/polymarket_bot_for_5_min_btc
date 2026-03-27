import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.exchange import (
    PolymarketExchange,
    _limit_order_type,
    minimum_order_usd,
    order_below_minimum_shares,
    plan_live_order,
)
from core.runner import extract_entry_cost_usd, principal_extraction_complete


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
        ("limit_order_type_prefers_post_only_when_available", _limit_order_type(LegacyOrderType) == "POST_ONLY"),
        ("limit_order_type_falls_back_to_gtc", _limit_order_type(ModernOrderType) == "GTC"),
        ("minimum_order_usd_for_five_shares", abs(minimum_order_usd(0.535, 5.0) - 2.675) < 1e-9),
        ("order_below_minimum_detects_small_live_order", order_below_minimum_shares(1.0, 0.535, 5.0) == (True, 1.87, 2.675)),
        ("order_below_minimum_allows_exact_five_shares", order_below_minimum_shares(1.0, 0.2, 5.0) == (False, 5.0, 1.0)),
        ("plan_live_order_rounds_up_small_notional_gap", plan_live_order(1.0, 0.4945, 0.0, 1.0) == (2.03, 1.0038)),
        ("plan_live_order_respects_five_share_minimum", plan_live_order(1.0, 0.535, 5.0, 1.0) == (5.0, 2.675)),
        ("plan_live_order_keeps_one_dollar_when_already_valid", plan_live_order(1.0, 0.2, 0.0, 1.0) == (5.0, 1.0)),
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
