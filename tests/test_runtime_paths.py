import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.runner as runner_mod
from core.runner import (
    OpenPos,
    PendingOrder,
    clear_expired_market_state,
    idle_sleep_seconds,
    next_cycle_interval_seconds,
    open_position_poll_interval_seconds,
    pending_order_poll_interval_seconds,
    perform_startup_sanity_check,
    refresh_daily_pnl_window,
    reversed_signal_origin,
    same_direction_entry_cooldown_age_sec,
    should_reset_clean_start_loss_streak,
    sync_open_positions,
)
from core.risk import RiskState
from core.runtime_paths import mode_label, run_journal_path, runtime_state_path, trade_journal_path


def main():
    cases = [
        ("dryrun_mode_label", mode_label(dry_run=True) == "dryrun"),
        ("live_mode_label", mode_label(dry_run=False) == "live"),
        ("dryrun_trade_journal_is_separate", trade_journal_path(dry_run=True).name == "trade_journal-dryrun.jsonl"),
        ("live_trade_journal_is_separate", trade_journal_path(dry_run=False).name == "trade_journal-live.jsonl"),
        ("dryrun_run_journal_is_separate", run_journal_path(dry_run=True).name == "run_journal-dryrun.jsonl"),
        ("live_run_journal_is_separate", run_journal_path(dry_run=False).name == "run_journal-live.jsonl"),
        ("dryrun_state_is_separate", runtime_state_path(dry_run=True).name == ".runtime_state-dryrun.json"),
        ("live_state_is_separate", runtime_state_path(dry_run=False).name == ".runtime_state-live.json"),
    ]

    canceled: list[str] = []
    kept_positions, kept_pending, cleanup_notes, cleanup_events = clear_expired_market_state(
        "btc-updown-5m-current",
        [
            OpenPos(
                slug="btc-updown-5m-old",
                side="UP",
                token_id="oldtok",
                shares=5.0,
                cost_usd=2.0,
                opened_ts=1.0,
            ),
            OpenPos(
                slug="btc-updown-5m-current",
                side="DOWN",
                token_id="curtok",
                shares=5.0,
                cost_usd=2.0,
                opened_ts=1.0,
            ),
        ],
        [
            PendingOrder(
                order_id="old-order",
                slug="btc-updown-5m-old",
                side="UP",
                token_id="oldtok",
                placed_ts=1.0,
                order_usd=1.0,
            ),
            PendingOrder(
                order_id="cur-order",
                slug="btc-updown-5m-current",
                side="DOWN",
                token_id="curtok",
                placed_ts=1.0,
                order_usd=1.0,
            ),
        ],
        cancel_order=lambda order_id: canceled.append(order_id),
    )
    cases.extend([
        ("expired_market_keeps_only_current_position", len(kept_positions) == 1 and kept_positions[0].slug == "btc-updown-5m-current"),
        ("expired_market_keeps_only_current_pending_order", len(kept_pending) == 1 and kept_pending[0].slug == "btc-updown-5m-current"),
        ("expired_market_cancels_old_pending_order", canceled == ["old-order"]),
        ("expired_market_logs_cleanup", any("clear expired live runtime position" in note for note in cleanup_notes)),
        ("expired_market_normal_cleanup_has_no_unresolved_event", cleanup_events == []),
    ])

    _, _, unresolved_notes, unresolved_events = clear_expired_market_state(
        "btc-updown-5m-current",
        [
            OpenPos(
                slug="btc-updown-5m-old",
                side="DOWN",
                token_id="residual-oldtok",
                shares=0.35,
                cost_usd=0.21,
                opened_ts=1.0,
                force_close_only=True,
                has_scaled_out_loss=True,
                position_id="pos-old-residual",
            ),
        ],
        [],
    )
    cases.extend([
        ("expired_market_unresolved_logs_special_note", any("expired unresolved live runtime position" in note for note in unresolved_notes)),
        (
            "expired_market_unresolved_emits_event",
            len(unresolved_events) == 1
            and unresolved_events[0].get("status") == "expired-unresolved-position"
            and unresolved_events[0].get("token_id") == "residual-oldtok"
        ),
    ])

    class DummyExchange:
        def __init__(self):
            self.calls = 0

        def get_positions(self):
            self.calls += 1
            return []

    dummy = DummyExchange()
    synced_positions, synced_notes = sync_open_positions(dummy, [])

    class DummyLivePos:
        def __init__(self, token_id: str):
            self.token_id = token_id
            self.size = 1.0
            self.initial_value = 1.0
            self.current_value = 1.0
            self.cash_pnl = 0.0

    class MismatchExchange:
        def __init__(self):
            self.calls = 0

        def get_positions(self):
            self.calls += 1
            return [DummyLivePos("other-token")]

    mismatch = MismatchExchange()
    held_positions, held_notes = sync_open_positions(
        mismatch,
        [
            OpenPos(
                slug="btc-updown-5m-current",
                side="UP",
                token_id="pending-token",
                shares=1.0,
                cost_usd=1.0,
                opened_ts=time.time() - 48.0,
                pending_confirmation=True,
                live_miss_count=5,
            ),
        ],
    )
    protected_positions, protected_notes = sync_open_positions(
        mismatch,
        [
            OpenPos(
                slug="btc-updown-5m-current",
                side="DOWN",
                token_id="residual-token",
                shares=0.3,
                cost_usd=0.2,
                opened_ts=time.time() - 120.0,
                has_scaled_out_loss=True,
                live_miss_count=9,
            ),
        ],
    )
    class MatchingExchange:
        def __init__(self):
            self.calls = 0

        def get_positions(self):
            self.calls += 1
            return [DummyLivePos("lottery-token")]

    matching = MatchingExchange()
    synced_lottery_positions, _ = sync_open_positions(
        matching,
        [
            OpenPos(
                slug="btc-updown-5m-current",
                side="UP",
                token_id="lottery-token",
                shares=1.0,
                cost_usd=1.0,
                opened_ts=time.time() - 12.0,
                lottery_activated=True,
                lottery_activated_ts=123.0,
            ),
        ],
    )
    same_market_cooldown_age = same_direction_entry_cooldown_age_sec(
        [
            OpenPos(
                slug="btc-updown-5m-current",
                side="UP",
                token_id="same-market-token",
                shares=1.0,
                cost_usd=1.0,
                opened_ts=100.0,
            ),
            OpenPos(
                slug="other-market",
                side="UP",
                token_id="other-market-token",
                shares=1.0,
                cost_usd=1.0,
                opened_ts=190.0,
            ),
        ],
        signal_side="UP",
        market_slug="btc-updown-5m-current",
        now_ts=200.0,
    )
    cross_market_cooldown_age = same_direction_entry_cooldown_age_sec(
        [
            OpenPos(
                slug="other-market",
                side="UP",
                token_id="other-market-token",
                shares=1.0,
                cost_usd=1.0,
                opened_ts=190.0,
            ),
        ],
        signal_side="UP",
        market_slug="btc-updown-5m-current",
        now_ts=200.0,
    )

    reversed_loss_origin = reversed_signal_origin(
        "model-ws_order_flow_down",
        "UP",
        reason="loss-reversal",
    )

    original_reset_sec = runner_mod.SETTINGS.clean_start_loss_streak_reset_sec
    runner_mod.SETTINGS.clean_start_loss_streak_reset_sec = 300.0
    try:
        stale_loss_reset, stale_loss_age = should_reset_clean_start_loss_streak(
            open_positions=[],
            pending_orders=[],
            last_trade_ts=100.0,
            risk_consec_losses=3,
            live_consec_losses=3,
            last_loss_side="DOWN",
            now_ts=450.0,
        )
        recent_loss_reset, recent_loss_age = should_reset_clean_start_loss_streak(
            open_positions=[],
            pending_orders=[],
            last_trade_ts=200.0,
            risk_consec_losses=3,
            live_consec_losses=3,
            last_loss_side="DOWN",
            now_ts=450.0,
        )
        active_position_reset, _ = should_reset_clean_start_loss_streak(
            open_positions=[
                OpenPos(
                    slug="btc-updown-5m-current",
                    side="UP",
                    token_id="active-token",
                    shares=1.0,
                    cost_usd=1.0,
                    opened_ts=1.0,
                ),
            ],
            pending_orders=[],
            last_trade_ts=100.0,
            risk_consec_losses=3,
            live_consec_losses=3,
            last_loss_side="DOWN",
            now_ts=450.0,
        )
    finally:
        runner_mod.SETTINGS.clean_start_loss_streak_reset_sec = original_reset_sec

    now_dt = runner_mod.datetime(2026, 4, 3, 10, 48, 0)
    prior_day_ts = runner_mod.datetime(2026, 4, 2, 12, 0, 0).timestamp()
    same_day_ts = runner_mod.datetime(2026, 4, 3, 8, 30, 0).timestamp()
    stored_date_risk = RiskState(daily_pnl=-3.5, daily_pnl_date="2026-04-01")
    stored_date_reset, stored_date_note = refresh_daily_pnl_window(
        stored_date_risk,
        last_trade_ts=same_day_ts,
        now_dt=now_dt,
    )
    inferred_date_risk = RiskState(daily_pnl=-2.25, daily_pnl_date="")
    inferred_date_reset, inferred_date_note = refresh_daily_pnl_window(
        inferred_date_risk,
        last_trade_ts=prior_day_ts,
        now_dt=now_dt,
    )
    same_day_risk = RiskState(daily_pnl=-1.25, daily_pnl_date="2026-04-03")
    same_day_reset, same_day_note = refresh_daily_pnl_window(
        same_day_risk,
        last_trade_ts=same_day_ts,
        now_dt=now_dt,
    )
    empty_date_risk = RiskState(daily_pnl=0.0, daily_pnl_date="")
    empty_date_reset, empty_date_note = refresh_daily_pnl_window(
        empty_date_risk,
        last_trade_ts=None,
        now_dt=now_dt,
    )

    startup_logged_messages: list[str] = []
    startup_events: list[dict] = []

    class StartupExchange:
        def get_positions(self):
            return []

        def reconcile_dry_run_positions(self, _positions):
            return False

    original_read_events = runner_mod.read_events
    original_replay_open_positions = runner_mod.replay_open_positions
    original_append_event = runner_mod.append_event
    original_log = runner_mod.log
    original_dry_run = runner_mod.SETTINGS.dry_run
    try:
        runner_mod.read_events = lambda limit=1000: []
        runner_mod.replay_open_positions = lambda _events: (
            {},
            [{"note": "exit without matching open entry in local journal", "token_id": "tok-startup"}],
        )
        runner_mod.append_event = lambda payload: startup_events.append(payload)
        runner_mod.log = lambda message: startup_logged_messages.append(message)
        runner_mod.SETTINGS.dry_run = False
        startup_positions, startup_notes, startup_recovery_restart, startup_runtime_state_changed = perform_startup_sanity_check(
            StartupExchange(),
            {"open_positions": []},
        )
    finally:
        runner_mod.read_events = original_read_events
        runner_mod.replay_open_positions = original_replay_open_positions
        runner_mod.append_event = original_append_event
        runner_mod.log = original_log
        runner_mod.SETTINGS.dry_run = original_dry_run

    cases.extend([
        ("sync_open_positions_short_circuits_when_empty", synced_positions == [] and synced_notes == [] and dummy.calls == 0),
        ("pending_confirmation_holds_until_grace_expires", len(held_positions) == 1 and any("sync_hold token=pending-token" in note for note in held_notes)),
        (
            "loss_residual_missing_live_is_force_close_protected",
            len(protected_positions) == 1
            and protected_positions[0].force_close_only is True
            and any("sync_protect token=residual-token" in note for note in protected_notes)
        ),
        (
            "sync_open_positions_preserves_lottery_activation_state",
            len(synced_lottery_positions) == 1
            and synced_lottery_positions[0].lottery_activated is True
            and abs(synced_lottery_positions[0].lottery_activated_ts - 123.0) < 1e-9
        ),
        ("same_direction_cooldown_scopes_to_current_market", same_market_cooldown_age is not None and abs(same_market_cooldown_age - 100.0) < 1e-9),
        ("same_direction_cooldown_ignores_other_markets", cross_market_cooldown_age is None),
        ("loss_reversal_origin_flips_strategy_side", reversed_loss_origin == "model-ws_order_flow_up+loss-reversal"),
        ("clean_start_resets_stale_loss_streak", stale_loss_reset is True and abs(stale_loss_age - 350.0) < 1e-9),
        ("clean_start_keeps_recent_loss_streak", recent_loss_reset is False and abs(recent_loss_age - 250.0) < 1e-9),
        ("clean_start_keeps_loss_streak_when_positions_are_active", active_position_reset is False),
        (
            "daily_pnl_reset_uses_stored_date",
            stored_date_reset is True
            and abs(stored_date_risk.daily_pnl) < 1e-9
            and stored_date_risk.daily_pnl_date == "2026-04-03"
            and "stored_date=2026-04-01" in stored_date_note
        ),
        (
            "daily_pnl_reset_can_infer_old_state_from_last_trade",
            inferred_date_reset is True
            and abs(inferred_date_risk.daily_pnl) < 1e-9
            and inferred_date_risk.daily_pnl_date == "2026-04-03"
            and "inferred_last_trade_date=2026-04-02" in inferred_date_note
        ),
        (
            "daily_pnl_window_keeps_same_day_state",
            same_day_reset is False
            and same_day_note == ""
            and abs(same_day_risk.daily_pnl + 1.25) < 1e-9
            and same_day_risk.daily_pnl_date == "2026-04-03"
        ),
        (
            "daily_pnl_window_initializes_empty_date_silently",
            empty_date_reset is True
            and empty_date_note == ""
            and abs(empty_date_risk.daily_pnl) < 1e-9
            and empty_date_risk.daily_pnl_date == "2026-04-03"
        ),
        (
            "startup_journal_reconcile_note_logged_once",
            startup_positions == []
            and startup_notes == ["journal reconcile note | exit without matching open entry in local journal | token=tok-startup"]
            and startup_recovery_restart is True
            and startup_runtime_state_changed is False
            and startup_logged_messages == ["startup sanity | journal reconcile note | exit without matching open entry in local journal | token=tok-startup"]
            and len(startup_events) == 1
            and startup_events[0].get("kind") == "startup_sanity"
        ),
        ("pending_orders_poll_every_second", abs(pending_order_poll_interval_seconds() - 1.0) < 1e-9),
        ("open_positions_poll_every_second", abs(open_position_poll_interval_seconds() - 1.0) < 1e-9),
        ("next_cycle_interval_uses_fast_pending_poll", abs(next_cycle_interval_seconds(has_pending_orders=True, has_open_positions=False) - 1.0) < 1e-9),
        ("next_cycle_interval_uses_fast_open_position_poll", abs(next_cycle_interval_seconds(has_pending_orders=False, has_open_positions=True) - 1.0) < 1e-9),
        ("next_cycle_interval_uses_two_second_market_poll_floor", abs(next_cycle_interval_seconds(has_pending_orders=False, has_open_positions=False) - 2.0) < 1e-9),
        ("idle_sleep_prefers_fast_pending_poll", abs(idle_sleep_seconds(has_open_positions=False, has_pending_orders=True) - 1.0) < 1e-9),
        ("idle_sleep_prefers_fast_open_position_poll", abs(idle_sleep_seconds(has_open_positions=True, has_pending_orders=False) - 1.0) < 1e-9),
    ])

    failed = [name for name, ok in cases if not ok]
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("OK")


def test_main():
    main()


if __name__ == "__main__":
    main()
