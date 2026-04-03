import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import SETTINGS
from core.exchange import (
    PolymarketExchange,
    _limit_order_type,
    estimate_book_exit_floor_price,
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
    conservative_exit_decision_value,
    emergency_exit_retry_kwargs,
    estimate_book_entry_fill,
    executable_take_profit_value,
    entry_slippage_breach,
    entry_velocity_gate_rejects,
    effective_stop_loss_partial_fraction,
    extract_entry_implied_avg_price,
    observed_exit_value_from_mark,
    extract_entry_cost_usd,
    is_loss_exit_reason,
    realized_exit_pnl,
    principal_extraction_sell_fraction,
    principal_extraction_complete,
    realistic_exit_value,
    sanitize_live_actual_exit_value,
    should_count_entry_toward_market_limit,
    should_block_live_entry_for_unavailable_book,
    should_arm_residual_force_close_after_stop_loss_scaleout,
    should_delay_soft_stop_scaleout,
    should_trigger_binance_adverse_exit,
    should_trigger_binance_profit_protect_exit,
    should_force_taker_profit_protection,
    should_trigger_profit_reversal_exit,
    should_force_full_loss_exit,
    should_force_taker_take_profit,
    should_force_taker_exit,
    resolve_close_remaining_shares,
    resolve_effective_closed_shares,
    preserve_partial_close_residual,
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
    # Keep these checks independent from any prior test file mutating SETTINGS.
    SETTINGS.stop_loss_partial_pct = 0.10
    SETTINGS.soft_stop_confirm_sec = 2.5
    SETTINGS.soft_stop_confirm_buffer_pct = 0.015
    SETTINGS.soft_stop_adverse_velocity = 0.00018
    SETTINGS.stop_loss_partial_fraction = 0.50
    SETTINGS.live_stop_loss_partial_fraction = 0.80
    SETTINGS.binance_adverse_exit_enabled = True
    SETTINGS.binance_adverse_exit_confirm_sec = 3.0
    SETTINGS.binance_adverse_exit_velocity = 0.00035
    SETTINGS.binance_adverse_exit_max_profit_pct = 0.08
    SETTINGS.binance_adverse_exit_min_hold_sec = 4.0
    SETTINGS.binance_adverse_exit_require_current_confirm = True
    SETTINGS.binance_profit_protect_enabled = True
    SETTINGS.binance_profit_protect_min_profit_pct = 0.08
    SETTINGS.binance_profit_protect_max_profit_pct = 0.17
    SETTINGS.binance_profit_protect_stall_sec = 6.0
    SETTINGS.binance_profit_protect_confirm_sec = 1.0
    SETTINGS.binance_profit_protect_velocity = 0.00012
    SETTINGS.binance_profit_protect_min_hold_sec = 10.0
    SETTINGS.binance_profit_protect_require_current_confirm = False
    SETTINGS.take_profit_soft_pct = 0.18
    SETTINGS.take_profit_partial_fraction = 0.40
    SETTINGS.take_profit_hard_pct = 0.30
    SETTINGS.take_profit_runner_fraction = 0.10

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
    estimated_entry_avg, estimated_entry_shares, estimated_entry_fill_ratio = estimate_book_entry_fill(
        book={"ask_levels": [(0.85, 2.0)]},
        amount_usd=1.0,
    )
    prechecked_slippage_breach, prechecked_slippage_premium = entry_slippage_breach(
        expected_entry_price=0.525,
        actual_avg_price=estimated_entry_avg,
        dry_run=False,
    )
    postcheck_with_estimated_expected, postcheck_with_estimated_premium = entry_slippage_breach(
        expected_entry_price=estimated_entry_avg,
        actual_avg_price=0.85,
        dry_run=False,
    )
    depth_book = {
        "best_bid": 0.475,
        "best_bid_size": 1.0,
        "bid_levels": [(0.475, 1.0), (0.12, 1.0)],
    }
    depth_value, depth_fill_ratio = estimate_book_exit_value(depth_book, 2.0)
    depth_floor_price = estimate_book_exit_floor_price(depth_book, 2.0)
    thin_value, thin_fill_ratio = estimate_book_exit_value({"bid_levels": [(0.2, 1.0)]}, 2.0)
    thin_floor_price = estimate_book_exit_floor_price({"bid_levels": [(0.2, 1.0)]}, 2.0)
    depth_pos = OpenPos(slug="m", side="UP", token_id="tok2", shares=2.0, cost_usd=1.0, opened_ts=0.0)
    realistic_value = realistic_exit_value(depth_pos, 0.52, 0.48, depth_book, None)
    executable_profit_value = executable_take_profit_value(depth_pos, depth_book, None)
    executable_profit_without_book = executable_take_profit_value(depth_pos, None, None)
    conservative_profitless_value = conservative_exit_decision_value(
        OpenPos(slug="m2", side="DOWN", token_id="tok3", shares=20.0, cost_usd=1.0, opened_ts=0.0),
        executable_exit_value=None,
        mark_value=7.9,
    )
    conservative_loss_value = conservative_exit_decision_value(
        OpenPos(slug="m3", side="UP", token_id="tok4", shares=2.0, cost_usd=1.0, opened_ts=0.0),
        executable_exit_value=None,
        mark_value=0.62,
    )
    counted_normal_entry = should_count_entry_toward_market_limit(
        slippage_breach=False,
        shares=2.5,
        order_id="",
    )
    counted_pending_entry = should_count_entry_toward_market_limit(
        slippage_breach=False,
        shares=0.0,
        order_id="ord_123",
    )
    counted_slippage_breach = should_count_entry_toward_market_limit(
        slippage_breach=True,
        shares=2.5,
        order_id="ord_456",
    )
    block_missing_live_book, missing_live_book_reason = should_block_live_entry_for_unavailable_book(
        dry_run=False,
        entry_book_quality={"ok": True, "available": False, "reason": "book-unavailable"},
    )
    allow_available_live_book, allow_available_live_book_reason = should_block_live_entry_for_unavailable_book(
        dry_run=False,
        entry_book_quality={"ok": True, "available": True, "reason": "ok"},
    )
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
    live_close_value_close_match, live_close_source_close_match = select_live_close_exit_value(
        usdc_received_total=0.5920,
        usdc_received_source="close_response_takingAmount",
        cash_delta=0.5877,
        cash_delta_source="cash_balance_delta",
    )
    live_close_value_mismatched, live_close_source_mismatched = select_live_close_exit_value(
        usdc_received_total=1.3846,
        usdc_received_source="close_response_takingAmount",
        cash_delta=0.0589,
        cash_delta_source="cash_balance_delta",
    )
    residual_force_close_armed = should_arm_residual_force_close_after_stop_loss_scaleout(
        dry_run=False,
        remaining_shares=0.42,
        remaining_cost_usd=0.18,
    )
    residual_force_close_not_armed_dry_run = should_arm_residual_force_close_after_stop_loss_scaleout(
        dry_run=True,
        remaining_shares=0.42,
        remaining_cost_usd=0.18,
    )
    residual_force_close_not_armed_for_dust = should_arm_residual_force_close_after_stop_loss_scaleout(
        dry_run=False,
        remaining_shares=0.0,
        remaining_cost_usd=0.0,
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

    object_book_ex = make_paper_exchange()
    object_book_ex.dry_run = False

    class ObjectLevel:
        def __init__(self, price: str, size: str):
            self.price = price
            self.size = size

    class ObjectBook:
        def __init__(self):
            self.bids = [ObjectLevel("0.48", "12.5"), ObjectLevel("0.47", "9.0")]
            self.asks = [ObjectLevel("0.49", "8.0"), ObjectLevel("0.50", "10.0")]

    class ObjectBookClient:
        def get_order_book(self, token_id):
            return ObjectBook()

    object_book_ex.client = ObjectBookClient()
    object_book = object_book_ex.get_full_orderbook("tok-object-book")
    object_book_liquidity = object_book_ex.has_exit_liquidity("tok-object-book", 10.0)
    resolved_close_remaining_dust = resolve_close_remaining_shares(
        requested_shares=1.550382,
        sold_shares=0.91,
        remaining_hint=0.0,
    )
    resolved_close_remaining_partial_clip = resolve_close_remaining_shares(
        requested_shares=1.550382,
        sold_shares=0.91,
        remaining_hint=0.0,
        close_request_shares=0.91,
    )
    resolved_close_remaining_live_hint = resolve_close_remaining_shares(
        requested_shares=1.758613,
        sold_shares=1.26,
        remaining_hint=0.498613,
    )
    effective_closed_from_zero_remaining_hint = resolve_effective_closed_shares(
        starting_shares=1.587300,
        sold_shares=1.269840,
        remaining_shares=0.0,
    )
    effective_closed_with_live_residual_hint = resolve_effective_closed_shares(
        starting_shares=1.724136,
        sold_shares=1.3793088,
        remaining_shares=0.3448272,
    )
    preserved_partial_residual = preserve_partial_close_residual(
        starting_shares=1.960783,
        requested_close_shares=1.274509,
        sold_shares=1.274509,
        remaining_shares=0.0,
    )
    preserved_full_close_residual = preserve_partial_close_residual(
        starting_shares=1.960783,
        requested_close_shares=1.960783,
        sold_shares=1.960783,
        remaining_shares=0.0,
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
        ("entry_implied_avg_price_uses_cost_divided_by_shares", abs((implied_entry_avg_price or 0.0) - 0.8) < 1e-9),
        ("entry_slippage_breach_detects_extreme_live_fill", slippage_breach is True and abs(slippage_premium - ((0.8 / 0.475) - 1.0)) < 1e-9),
        ("entry_slippage_breach_allows_normal_fill", slippage_ok is False and slippage_ok_premium < 0.18),
        ("estimate_book_entry_fill_uses_ask_depth", abs((estimated_entry_avg or 0.0) - 0.85) < 1e-9 and abs(estimated_entry_shares - (1.0 / 0.85)) < 1e-9 and abs(estimated_entry_fill_ratio - 1.0) < 1e-9),
        ("entry_slippage_precheck_blocks_too_expensive_market_fill", prechecked_slippage_breach is True and prechecked_slippage_premium > 0.18),
        ("entry_slippage_postcheck_accepts_fill_near_prechecked_avg", postcheck_with_estimated_expected is False and abs(postcheck_with_estimated_premium) < 1e-9),
        ("principal_extraction_rejects_tiny_partial_fill", principal_extraction_complete(0.0286, 1.0) is False),
        ("principal_extraction_accepts_near_full_recovery", principal_extraction_complete(0.97, 1.0) is True),
        ("principal_extraction_sell_fraction_uses_total_position_value", abs(principal_extraction_sell_fraction(1.6, 1.0) - 0.625) < 1e-9),
        (
            "principal_extraction_sell_fraction_respects_final_runner_target",
            abs(
                principal_extraction_sell_fraction(
                    0.8,
                    0.4,
                    current_shares=4.0,
                    target_remaining_shares=1.0,
                ) - 0.75
            ) < 1e-9,
        ),
        ("realized_exit_pnl_falls_back_to_observed_when_actual_is_none", abs(realized_exit_pnl(None, 0.18, 0.20) + 0.02) < 1e-9),
        ("realized_exit_pnl_prefers_actual_when_present", abs(realized_exit_pnl(0.22, 0.18, 0.20) - 0.02) < 1e-9),
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
        ("binance_adverse_exit_prefers_taker_live", should_force_taker_profit_protection(reason="binance-adverse-exit", dry_run=False) is True),
        ("deadline_weak_win_prefers_taker_live", should_force_taker_profit_protection(reason="deadline-exit-weak-win", dry_run=False) is True),
        ("deadline_flat_prefers_taker_live", should_force_taker_profit_protection(reason="deadline-exit-flat", dry_run=False) is True),
        (
            "deadline_exit_emergency_retry_uses_one_second_loop",
            emergency_exit_retry_kwargs(reason="deadline-exit-weak-win", secs_left=18.0, dry_run=False) == {
                "retry_delay_sec": 1.0,
                "max_attempts": 8,
            },
        ),
        ("non_deadline_exit_has_no_emergency_retry", emergency_exit_retry_kwargs(reason="take-profit-principal", secs_left=18.0, dry_run=False) == {}),
        ("panic_dump_always_forces_taker", should_force_taker_exit(reason="", dry_run=True, has_panic_dumped=True) is True),
        ("soft_stop_scaleout_waits_for_confirmation_when_shallow_and_flat", should_delay_soft_stop_scaleout(reason="stop-loss-scale-out", side="UP", pnl_pct=-0.08, breach_age_sec=1.0, secs_left=120.0, ws_velocity=0.0) is True),
        ("soft_stop_scaleout_does_not_wait_when_move_is_still_adverse", should_delay_soft_stop_scaleout(reason="stop-loss-scale-out", side="UP", pnl_pct=-0.08, breach_age_sec=1.0, secs_left=120.0, ws_velocity=-0.0010) is False),
        ("soft_stop_scaleout_does_not_wait_after_confirmation_window", should_delay_soft_stop_scaleout(reason="stop-loss-scale-out", side="UP", pnl_pct=-0.08, breach_age_sec=3.0, secs_left=120.0, ws_velocity=0.0) is False),
        ("soft_stop_scaleout_does_not_wait_when_loss_is_too_deep", should_delay_soft_stop_scaleout(reason="stop-loss-scale-out", side="UP", pnl_pct=-0.12, breach_age_sec=1.0, secs_left=120.0, ws_velocity=0.0) is False),
        ("binance_adverse_exit_waits_for_confirmation_window", should_trigger_binance_adverse_exit(has_extracted_principal=False, side="UP", pnl_pct=-0.03, profit_pnl_pct=None, hold_sec=10.0, breach_age_sec=2.0, secs_left=120.0, ws_velocity=-0.0010, current_ws_velocity=-0.0011) is False),
        ("binance_adverse_exit_triggers_after_confirmed_dual_adverse_velocity", should_trigger_binance_adverse_exit(has_extracted_principal=False, side="UP", pnl_pct=-0.03, profit_pnl_pct=None, hold_sec=10.0, breach_age_sec=3.1, secs_left=120.0, ws_velocity=-0.0010, current_ws_velocity=-0.0011) is True),
        ("binance_adverse_exit_skips_safe_executable_profit", should_trigger_binance_adverse_exit(has_extracted_principal=False, side="UP", pnl_pct=0.02, profit_pnl_pct=0.12, hold_sec=10.0, breach_age_sec=3.1, secs_left=120.0, ws_velocity=-0.0010, current_ws_velocity=-0.0011) is False),
        ("binance_adverse_exit_requires_current_confirmation_when_enabled", should_trigger_binance_adverse_exit(has_extracted_principal=False, side="DOWN", pnl_pct=-0.02, profit_pnl_pct=None, hold_sec=10.0, breach_age_sec=3.1, secs_left=120.0, ws_velocity=0.0010, current_ws_velocity=0.0) is False),
        ("binance_profit_protect_exit_waits_for_stall_window", should_trigger_binance_profit_protect_exit(has_extracted_principal=False, side="UP", profit_pnl_pct=0.10, take_profit_soft_pct=0.18, hold_sec=12.0, peak_age_sec=5.0, breach_age_sec=1.1, secs_left=120.0, ws_velocity=-0.0010, current_ws_velocity=-0.0011) is False),
        ("binance_profit_protect_exit_triggers_for_stalled_small_profit", should_trigger_binance_profit_protect_exit(has_extracted_principal=False, side="UP", profit_pnl_pct=0.10, take_profit_soft_pct=0.18, hold_sec=12.0, peak_age_sec=7.0, breach_age_sec=1.1, secs_left=120.0, ws_velocity=-0.0010, current_ws_velocity=-0.0011) is True),
        ("binance_profit_protect_exit_skips_big_profit_reserved_for_take_profit", should_trigger_binance_profit_protect_exit(has_extracted_principal=False, side="UP", profit_pnl_pct=0.19, take_profit_soft_pct=0.18, hold_sec=12.0, peak_age_sec=7.0, breach_age_sec=1.1, secs_left=120.0, ws_velocity=-0.0010, current_ws_velocity=-0.0011) is False),
        ("binance_profit_protect_exit_can_fire_without_current_confirmation", should_trigger_binance_profit_protect_exit(has_extracted_principal=False, side="DOWN", profit_pnl_pct=0.10, take_profit_soft_pct=0.18, hold_sec=12.0, peak_age_sec=7.0, breach_age_sec=1.1, secs_left=120.0, ws_velocity=0.0010, current_ws_velocity=0.0) is True),
        ("binance_profit_protect_prefers_taker_live", should_force_taker_profit_protection(reason="binance-profit-protect-exit", dry_run=False) is True),
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
        ("estimate_book_exit_floor_price_uses_lowest_bid_needed_for_full_fill", abs((depth_floor_price or 0.0) - 0.12) < 1e-9),
        ("estimate_book_exit_value_is_conservative_when_depth_is_thin", abs((thin_value or 0.0) - 0.2) < 1e-9 and abs(thin_fill_ratio - 0.5) < 1e-9),
        ("estimate_book_exit_floor_price_requires_full_depth", thin_floor_price is None),
        ("realistic_exit_value_uses_depth_aware_bids", abs((realistic_value or 0.0) - 0.595) < 1e-9),
        ("executable_take_profit_value_uses_orderbook_only", abs((executable_profit_value or 0.0) - 0.595) < 1e-9 and executable_profit_without_book is None),
        ("conservative_exit_decision_value_caps_unbacked_profit_to_cost", abs(conservative_profitless_value - 1.0) < 1e-9),
        ("conservative_exit_decision_value_keeps_mark_for_losses", abs(conservative_loss_value - 0.62) < 1e-9),
        ("market_limit_counts_normal_and_pending_entries", counted_normal_entry is True and counted_pending_entry is True),
        ("market_limit_skips_entry_slippage_guard_retries", counted_slippage_breach is False),
        ("live_entry_blocks_when_orderbook_is_unavailable", block_missing_live_book is True and missing_live_book_reason == "book-unavailable"),
        ("live_entry_allows_usable_orderbook", allow_available_live_book is False and allow_available_live_book_reason == ""),
        ("observed_exit_value_uses_sold_shares_times_mark", abs(observed_partial_value - 0.58765) < 1e-9),
        ("sanitize_live_actual_exit_value_rejects_improbable_fill", sane_actual_value is None and sane_actual_source.startswith("sanity-rejected-")),
        ("sanitize_live_actual_exit_value_accepts_close_to_mark_fill", abs((accepted_actual_value or 0.0) - 0.5877) < 1e-9 and accepted_actual_source == "close_response_takingAmount"),
        ("rejected_actual_fill_falls_back_to_observed_mark_value", abs(principal_recovery_from_rejected_actual - observed_partial_value) < 1e-9),
        ("live_close_exit_value_keeps_cash_delta_when_it_agrees", abs((live_close_value_close_match or 0.0) - 0.5877) < 1e-9 and live_close_source_close_match == "cash_balance_delta"),
        ("live_close_exit_value_prefers_response_when_cash_delta_lags", abs((live_close_value_mismatched or 0.0) - 1.3846) < 1e-9 and live_close_source_mismatched == "close_response_takingAmount"),
        ("parse_balance_allowance_available_shares_handles_live_error", abs((parsed_balance_shares or 0.0) - 1.198827) < 1e-9),
        ("stop_loss_scaleout_arms_live_tail_cleanup", residual_force_close_armed is True),
        ("stop_loss_scaleout_tail_cleanup_skips_dry_run", residual_force_close_not_armed_dry_run is False),
        ("stop_loss_scaleout_tail_cleanup_skips_dust", residual_force_close_not_armed_for_dust is False),
        ("live_account_cache_reuses_recent_snapshot", cash_calls["count"] == 2 and value_calls["count"] == 2 and acct_first.cash == acct_second.cash == acct_third.cash == 7.0 and acct_first.equity == acct_second.equity == acct_third.equity == 10.0),
        ("get_full_orderbook_accepts_object_style_orderbook", object_book.get("best_bid") == 0.48 and object_book.get("best_ask") == 0.49 and object_book.get("bids_volume") == 21.5 and object_book.get("asks_volume") == 18.0),
        ("has_exit_liquidity_accepts_object_style_levels", object_book_liquidity is True),
        ("close_remaining_shares_trusts_exchange_dust_hint", abs(resolved_close_remaining_dust - 0.0) < 1e-9),
        ("close_remaining_shares_ignores_zero_hint_for_partial_clip", abs(resolved_close_remaining_partial_clip - 0.640382) < 1e-9),
        ("close_remaining_shares_preserves_non_dust_hint", abs(resolved_close_remaining_live_hint - 0.498613) < 1e-9),
        ("effective_closed_shares_uses_zero_remaining_hint_as_full_close", abs(effective_closed_from_zero_remaining_hint - 1.587300) < 1e-9),
        ("effective_closed_shares_preserves_partial_when_residual_remains", abs(effective_closed_with_live_residual_hint - 1.3793088) < 1e-9),
        ("preserve_partial_close_residual_recovers_expected_runner", abs(preserved_partial_residual - (1.960783 - 1.274509)) < 1e-9),
        ("preserve_partial_close_residual_keeps_true_full_close_at_zero", abs(preserved_full_close_residual - 0.0) < 1e-9),
        ("take_profit_soft_pct_uses_eighteen_percent_default", abs(float(getattr(SETTINGS, "take_profit_soft_pct", 0.0)) - 0.18) < 1e-9),
        ("take_profit_partial_fraction_uses_forty_percent_default", abs(float(getattr(SETTINGS, "take_profit_partial_fraction", 0.0)) - 0.40) < 1e-9),
        ("take_profit_hard_pct_uses_thirty_percent_default", abs(float(getattr(SETTINGS, "take_profit_hard_pct", 0.0)) - 0.30) < 1e-9),
        ("take_profit_runner_fraction_uses_ten_percent_default", abs(float(getattr(SETTINGS, "take_profit_runner_fraction", 0.0)) - 0.10) < 1e-9),
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
