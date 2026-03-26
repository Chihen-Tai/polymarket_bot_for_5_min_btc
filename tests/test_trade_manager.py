import os
import sys
from collections import deque
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import SETTINGS
from core.decision_engine import explain_choose_side
from core.journal import replay_open_positions
from core.runner import (
    RuntimeFlags,
    refresh_runtime_flags,
    observe_api_latency,
    paper_settlement_from_last_mark,
    price_aware_kelly_fraction,
    required_trade_edge,
    strategy_name_for_side,
    summarize_entry_edge,
    update_network_guard,
)
from core.trade_manager import decide_exit, maybe_reverse_entry, can_reenter_same_market
from scripts.journal_analysis import build_exit_accounting_rows, build_trade_pairs, classify_actual_source_tier


def main():
    SETTINGS.stop_loss_partial_pct = 0.05
    SETTINGS.stop_loss_warn_pct = 0.07
    SETTINGS.stop_loss_pct = 0.10
    SETTINGS.max_hold_seconds = 90
    SETTINGS.min_entry_price = 0.35
    SETTINGS.max_entry_price = 0.75
    SETTINGS.entry_window_min_sec = 120
    SETTINGS.edge_threshold = 0.02
    SETTINGS.late_entry_edge_penalty = 0.015
    SETTINGS.rich_price_edge_penalty = 0.015
    SETTINGS.binary_kelly_divisor = 4.0
    SETTINGS.force_full_exit_on_take_profit = False
    SETTINGS.force_full_exit_on_stop_loss_scaleout = False
    SETTINGS.failed_follow_through_window_sec = 45
    SETTINGS.failed_follow_through_loss_pct = 0.03
    SETTINGS.failed_follow_through_max_mfe_pct = 0.02
    SETTINGS.failed_follow_through_min_secs_left = 90
    SETTINGS.ws_stale_max_age_sec = 5.0
    SETTINGS.ws_stale_fail_safe_streak = 2
    SETTINGS.api_slow_threshold_ms = 1500.0
    SETTINGS.api_fail_safe_streak = 3
    SETTINGS.network_recovery_streak = 2

    lots, notes = replay_open_positions([
        {
            "kind": "entry",
            "event_id": "entry_1",
            "slug": "m1",
            "side": "UP",
            "token_id": "tok1",
            "shares": 10,
            "cost_usd": 1.0,
            "opened_ts": 100,
        },
        {
            "kind": "exit",
            "event_id": "exit_1",
            "slug": "m1",
            "side": "UP",
            "token_id": "tok1",
            "closed_shares": 4,
            "remaining_shares": 6,
        },
    ])

    accounting_rows = build_exit_accounting_rows([
        {
            "kind": "exit",
            "ts": "2026-03-19T10:00:00",
            "event_id": "exit_a",
            "position_id": "pos1",
            "slug": "m1",
            "side": "UP",
            "reason": "take-profit-hard",
            "closed_shares": 4,
            "realized_cost_usd": 0.4,
            "actual_exit_value_usd": 0.55,
            "actual_exit_value_source": "cash_balance_delta",
            "observed_exit_value_usd": 0.50,
        }
    ])
    pair_rows = build_trade_pairs([
        {
            "kind": "entry",
            "ts": "2026-03-19T10:00:00",
            "event_id": "entry_a",
            "position_id": "pos1",
            "slug": "m1",
            "side": "UP",
            "token_id": "tok1",
            "shares": 10,
            "cost_usd": 1.0,
            "mfe_pnl_usd": 0.2,
            "mae_pnl_usd": -0.1,
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:01:00",
            "event_id": "exit_a",
            "position_id": "pos1",
            "slug": "m1",
            "side": "UP",
            "token_id": "tok1",
            "closed_shares": 10,
            "remaining_shares": 0,
            "realized_cost_usd": 1.0,
            "actual_exit_value_usd": 1.2,
            "observed_exit_value_usd": 1.1,
            "reason": "take-profit-hard",
            "mfe_pnl_usd": 0.2,
            "mae_pnl_usd": -0.1,
        },
    ])

    future_end = (datetime.now(timezone.utc) + timedelta(seconds=180)).isoformat()
    observed_price_decision = explain_choose_side(
        market={
            "outcomes": ["up", "down"],
            "outcomePrices": [0.95, 0.05],
            "endDate": future_end,
        },
        yes_window=deque([0.45] * 10, maxlen=20),
        up_window=deque([0.43, 0.44, 0.45], maxlen=5),
        down_window=deque([0.57, 0.56, 0.55], maxlen=5),
        observed_up=0.45,
        observed_down=0.55,
        ws_trades=[
            {"p": 100000.0, "q": 1.0, "m": False},
            {"p": 100001.0, "q": 1.0, "m": False},
            {"p": 100002.0, "q": 1.0, "m": False},
            {"p": 100003.0, "q": 0.2, "m": True},
        ],
    )

    health_flags = RuntimeFlags(0, "", 0, False)
    slow_detected = observe_api_latency(health_flags, "test_call", 1600.0)
    slow_notes_1 = update_network_guard(health_flags, ws_age=1.0, cycle_had_slow_api=True, cycle_api_error=False)
    slow_notes_2 = update_network_guard(health_flags, ws_age=1.0, cycle_had_slow_api=True, cycle_api_error=False)
    slow_notes_3 = update_network_guard(health_flags, ws_age=1.0, cycle_had_slow_api=True, cycle_api_error=False)
    activated_after_slow = health_flags.network_fail_safe_mode
    clear_notes_1 = update_network_guard(health_flags, ws_age=1.0, cycle_had_slow_api=False, cycle_api_error=False)
    clear_notes_2 = update_network_guard(health_flags, ws_age=1.0, cycle_had_slow_api=False, cycle_api_error=False)
    cleared_after_recovery = health_flags.network_fail_safe_mode

    stale_flags = RuntimeFlags(0, "", 0, False)
    stale_notes_1 = update_network_guard(stale_flags, ws_age=7.0, cycle_had_slow_api=False, cycle_api_error=False)
    stale_notes_2 = update_network_guard(stale_flags, ws_age=7.0, cycle_had_slow_api=False, cycle_api_error=False)
    reloaded_flags = refresh_runtime_flags(
        RuntimeFlags(
            live_consec_losses=1,
            last_loss_side="UP",
            close_fail_streak=2,
            panic_exit_mode=True,
            network_fail_safe_mode=True,
            api_fail_streak=4,
            slow_api_streak=3,
            ws_stale_streak=2,
            network_recovery_streak=1,
            last_api_latency_ms=2222.0,
        ),
        [],
        "",
    )

    cases = [
        ("stop_loss_scale_out", decide_exit(pnl_pct=-0.07, hold_sec=5).reason == "stop-loss-scale-out"),
        (
            "force_full_exit_on_stop_scaleout",
            (setattr(SETTINGS, "force_full_exit_on_stop_loss_scaleout", True) or True)
            and decide_exit(pnl_pct=-0.07, hold_sec=5).reason == "stop-loss-full"
            and (setattr(SETTINGS, "force_full_exit_on_stop_loss_scaleout", False) or True),
        ),
        ("failed_follow_through", decide_exit(pnl_pct=-0.04, hold_sec=50, secs_left=200, mfe_pnl_pct=0.01).reason == "failed-follow-through"),
        ("failed_follow_through_skips_if_signal_showed_life", decide_exit(pnl_pct=-0.04, hold_sec=50, secs_left=200, mfe_pnl_pct=0.05).reason != "failed-follow-through"),
        ("smart_stop_loss_after_scale_out", decide_exit(pnl_pct=-0.08, hold_sec=5, recovery_chance_low=True, has_scaled_out_loss=True).reason == "smart-stop-loss"),
        ("smart_stop_loss_at_threshold", decide_exit(pnl_pct=-0.08, hold_sec=5, recovery_chance_low=True).reason == "smart-stop-loss"),
        ("hard_stop_loss", decide_exit(pnl_pct=-0.55, hold_sec=5).reason == "hard-stop-loss"),
        (
            "force_full_exit_on_take_profit",
            (setattr(SETTINGS, "force_full_exit_on_take_profit", True) or True)
            and decide_exit(pnl_pct=0.31, hold_sec=5).reason == "take-profit-full"
            and (setattr(SETTINGS, "force_full_exit_on_take_profit", False) or True),
        ),
        ("max_hold_extended", decide_exit(pnl_pct=-0.01, hold_sec=190).reason == "max-hold-loss-extended"),
        ("max_hold_loss_low_recovery", decide_exit(pnl_pct=-0.01, hold_sec=95, recovery_chance_low=True).reason == "max-hold-loss"),
        (
            "loss_reversal_symmetric",
            maybe_reverse_entry(signal_side="DOWN", live_consec_losses=2, last_loss_side="DOWN").side == "UP"
            and maybe_reverse_entry(signal_side="UP", live_consec_losses=2, last_loss_side="UP").side == "DOWN"
            and maybe_reverse_entry(signal_side="UP", live_consec_losses=2, last_loss_side="DOWN").side == "UP",
        ),
        ("reenter_gate", can_reenter_same_market(has_current_market_pos=False, closed_any=True, secs_left=80) is True),
        ("reenter_block", can_reenter_same_market(has_current_market_pos=True, closed_any=True, secs_left=80) is False),
        ("journal_partial_close_shares", abs(lots["tok1"]["shares"] - 6.0) < 1e-9),
        ("journal_partial_close_cost", abs(lots["tok1"]["cost_usd"] - 0.6) < 1e-9),
        ("journal_partial_close_notes", len(notes) == 0),
        ("exit_accounting_diff", len(accounting_rows) == 1 and abs((accounting_rows[0].difference_usd or 0.0) - 0.05) < 1e-9),
        ("trade_pair_closed", len(pair_rows) == 1 and pair_rows[0].status == "closed"),
        ("trade_pair_actual_pnl", len(pair_rows) == 1 and abs((pair_rows[0].actual_pnl_usd or 0.0) - 0.2) < 1e-9),
        ("trade_pair_mae_mfe", len(pair_rows) == 1 and pair_rows[0].mae_pnl_usd == -0.1 and pair_rows[0].mfe_pnl_usd == 0.2),
        ("decision_engine_uses_observed_prices", observed_price_decision.get("ok") and observed_price_decision.get("side") == "UP" and abs((observed_price_decision.get("entry_price") or 0.0) - 0.45) < 1e-9),
        ("paper_settlement_win", paper_settlement_from_last_mark(0.72) == (1.0, "binary-win")),
        ("paper_settlement_loss", paper_settlement_from_last_mark(0.28) == (0.0, "binary-lose")),
        ("paper_settlement_neutral", paper_settlement_from_last_mark(0.50) == (0.5, "binary-neutral")),
        ("price_aware_kelly_fraction", abs(price_aware_kelly_fraction(0.60, 0.45) - (((0.60 - 0.45) / (1.0 - 0.45)) / 4.0)) < 1e-9),
        ("required_trade_edge_relaxes_for_fresh_strategy", abs(required_trade_edge(0.45, 250, history_count=0) - 0.005) < 1e-9),
        ("required_trade_edge_penalizes_late_rich_price", abs(required_trade_edge(0.70, 150, history_count=30) - 0.065) < 1e-9),
        ("summarize_entry_edge_blocks_weak_late_trade", summarize_entry_edge(win_rate=0.56, entry_price=0.55, secs_left=140, history_count=30)["ok"] is False),
        ("summarize_entry_edge_allows_fresh_discounted_trade", summarize_entry_edge(win_rate=0.50, entry_price=0.48, secs_left=250, history_count=0)["ok"] is True),
        ("observe_api_latency_detects_slow_call", slow_detected is True and abs(health_flags.last_api_latency_ms - 1600.0) < 1e-9),
        ("network_fail_safe_activates_on_slow_api_streak", activated_after_slow is True and any("ACTIVATED" in note for note in slow_notes_3)),
        ("network_fail_safe_clears_after_recovery", cleared_after_recovery is False and any("CLEARED" in note for note in clear_notes_2)),
        ("network_fail_safe_activates_on_ws_stale_streak", stale_flags.network_fail_safe_mode is True and any("ACTIVATED" in note for note in stale_notes_2) and any("ws stale detected" in note for note in stale_notes_1)),
        (
            "refresh_runtime_flags_preserves_network_state",
            reloaded_flags.close_fail_streak == 0
            and reloaded_flags.panic_exit_mode is False
            and reloaded_flags.network_fail_safe_mode is True
            and reloaded_flags.api_fail_streak == 4
            and reloaded_flags.slow_api_streak == 3
            and reloaded_flags.ws_stale_streak == 2
            and reloaded_flags.network_recovery_streak == 1
            and abs(reloaded_flags.last_api_latency_ms - 2222.0) < 1e-9
        ),
        ("actual_source_tier_maker_balance_delta", classify_actual_source_tier("maker-balance-delta", 1.0) == "high"),
        ("strategy_name_for_reversed_side", strategy_name_for_side("model-ws_order_flow_down", "UP") == "model-ws_order_flow_up"),
        ("hard_stop_shield_opt_in_default", SETTINGS.enable_hard_stop_shield is False),
    ]

    failed = [name for name, ok in cases if not ok]
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("OK")


def test_main():
    main()


if __name__ == "__main__":
    main()
