from core.trade_manager import decide_exit, maybe_reverse_entry, can_reenter_same_market
from core.journal import replay_open_positions
from scripts.journal_analysis import build_exit_accounting_rows, build_trade_pairs


def main():
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

    cases = [
        # stop-loss-scale-out triggers at -5% (stop_loss_partial_pct=0.05), before hard-stop at -10%
        ("stop_loss_scale_out", decide_exit(pnl_pct=-0.07, hold_sec=5).reason == "stop-loss-scale-out"),
        # after scale-out loss, smart-stop fires at -8% (< -7% warn, > -10% hard-stop) when recovery_chance_low
        ("smart_stop_loss_after_scale_out", decide_exit(pnl_pct=-0.08, hold_sec=5, recovery_chance_low=True, has_scaled_out_loss=True).reason == "smart-stop-loss"),
        # at -8% with recovery_chance_low, smart-stop fires (checked before scale-out in priority order)
        ("smart_stop_loss_at_threshold", decide_exit(pnl_pct=-0.08, hold_sec=5, recovery_chance_low=True).reason == "smart-stop-loss"),
        # hard-stop fires at -10% (stop_loss_pct=0.10)
        ("hard_stop_loss", decide_exit(pnl_pct=-0.55, hold_sec=5).reason == "hard-stop-loss"),
        # max-hold-extended fires at 2x max_hold_seconds (90*2=180) when smart_stop is enabled
        ("max_hold_extended", decide_exit(pnl_pct=-0.01, hold_sec=190).reason == "max-hold-loss-extended"),
        # max-hold-loss fires at 1x max_hold_seconds when recovery_chance_low=True
        ("max_hold_loss_low_recovery", decide_exit(pnl_pct=-0.01, hold_sec=95, recovery_chance_low=True).reason == "max-hold-loss"),
        (
            "loss_reversal_only_down",
            maybe_reverse_entry(signal_side="DOWN", live_consec_losses=2, last_loss_side="DOWN").side == "UP"
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
    ]

    failed = [name for name, ok in cases if not ok]
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("OK")


if __name__ == "__main__":
    main()
