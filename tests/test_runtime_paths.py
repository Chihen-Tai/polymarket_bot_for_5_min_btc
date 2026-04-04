import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.runner as runner_mod
from core.runner import (
    OpenPos,
    PendingOrder,
    clear_expired_market_state,
    dedupe_open_positions_by_token,
    existing_token_entry_conflict,
    idle_sleep_seconds,
    maybe_apply_stale_loss_streak_reset,
    next_cycle_interval_seconds,
    open_position_poll_interval_seconds,
    pending_order_poll_interval_seconds,
    perform_startup_sanity_check,
    refresh_daily_pnl_window,
    risk_block_sleep_seconds,
    reversed_signal_origin,
    same_direction_entry_cooldown_age_sec,
    should_reset_clean_start_loss_streak,
    sync_open_positions,
    validate_live_startup_requirements,
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
        def __init__(
            self,
            token_id: str,
            *,
            size: float = 1.0,
            initial_value: float = 1.0,
            current_value: float = 1.0,
            cash_pnl: float = 0.0,
        ):
            self.token_id = token_id
            self.size = size
            self.initial_value = initial_value
            self.current_value = current_value
            self.cash_pnl = cash_pnl

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
    partial_protected_positions, partial_protected_notes = sync_open_positions(
        mismatch,
        [
            OpenPos(
                slug="btc-updown-5m-current",
                side="UP",
                token_id="partial-token",
                shares=0.45,
                cost_usd=0.18,
                opened_ts=time.time() - 180.0,
                has_taken_partial=True,
                live_miss_count=12,
                live_sync_protect_until_ts=time.time() + 75.0,
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
    deduped_positions, dedupe_notes = dedupe_open_positions_by_token(
        [
            OpenPos(
                slug="btc-updown-5m-current",
                side="UP",
                token_id="dup-token",
                shares=1.0,
                cost_usd=0.60,
                opened_ts=100.0,
                position_id="dup-old",
                has_taken_partial=True,
                live_miss_count=9,
            ),
            OpenPos(
                slug="btc-updown-5m-current",
                side="UP",
                token_id="dup-token",
                shares=0.5,
                cost_usd=0.40,
                opened_ts=150.0,
                position_id="dup-new",
                pending_confirmation=True,
                live_miss_count=1,
            ),
        ],
        live_positions=[
            DummyLivePos(
                "dup-token",
                size=1.25,
                initial_value=0.95,
                current_value=1.10,
                cash_pnl=0.15,
            ),
        ],
        source="runtime-test",
    )
    token_conflict_open = existing_token_entry_conflict(
        [
            OpenPos(
                slug="btc-updown-5m-current",
                side="UP",
                token_id="dup-token",
                shares=1.0,
                cost_usd=1.0,
                opened_ts=1.0,
            ),
        ],
        [],
        token_id="dup-token",
    )
    token_conflict_pending = existing_token_entry_conflict(
        [],
        [
            PendingOrder(
                order_id="po-1",
                slug="btc-updown-5m-current",
                side="UP",
                token_id="dup-token",
                placed_ts=1.0,
                order_usd=1.0,
            ),
        ],
        token_id="dup-token",
    )
    daily_loss_pause_sleep = risk_block_sleep_seconds(
        reason="daily max loss reached",
        has_open_positions=False,
        has_pending_orders=False,
        secs_left=42.0,
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

    runtime_sync_reset_positions: list[OpenPos] = []
    runtime_sync_reset_notes: list[str] = []
    runtime_sync_reset_note = ""
    runtime_sync_reset_risk = RiskState(consec_losses=2)
    runtime_sync_reset_flags = runner_mod.RuntimeFlags(
        live_consec_losses=2,
        last_loss_side="UP",
        close_fail_streak=0,
        panic_exit_mode=False,
    )

    class SyncResetExchange:
        def get_positions(self):
            return [DummyLivePos("other-token")]

    original_time_fn = runner_mod.time.time
    try:
        runner_mod.time.time = lambda: 450.0
        runtime_sync_reset_positions, runtime_sync_reset_notes = sync_open_positions(
            SyncResetExchange(),
            [
                OpenPos(
                    slug="btc-updown-5m-current",
                    side="UP",
                    token_id="startup-stale-token",
                    shares=1.0,
                    cost_usd=1.0,
                    opened_ts=100.0,
                ),
            ],
        )
        runtime_sync_reset_note = maybe_apply_stale_loss_streak_reset(
            runtime_sync_reset_risk,
            runtime_sync_reset_flags,
            open_positions=runtime_sync_reset_positions,
            pending_orders=[],
            last_trade_ts=100.0,
            note_prefix="reset stale loss streak after runtime sync",
            now_ts=450.0,
        )
    finally:
        runner_mod.time.time = original_time_fn

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

    original_private_key = runner_mod.SETTINGS.private_key
    original_funder_address = runner_mod.SETTINGS.funder_address
    original_clob_api_key = runner_mod.SETTINGS.clob_api_key
    original_clob_api_secret = runner_mod.SETTINGS.clob_api_secret
    original_clob_api_passphrase = runner_mod.SETTINGS.clob_api_passphrase
    try:
        runner_mod.SETTINGS.dry_run = False
        runner_mod.SETTINGS.private_key = ""
        runner_mod.SETTINGS.funder_address = ""
        runner_mod.SETTINGS.clob_api_key = ""
        runner_mod.SETTINGS.clob_api_secret = ""
        runner_mod.SETTINGS.clob_api_passphrase = ""
        live_preflight_ok_missing, live_preflight_notes_missing = validate_live_startup_requirements()

        runner_mod.SETTINGS.private_key = "pk"
        runner_mod.SETTINGS.funder_address = "0xfunder"
        runner_mod.SETTINGS.clob_api_key = ""
        runner_mod.SETTINGS.clob_api_secret = ""
        runner_mod.SETTINGS.clob_api_passphrase = ""
        live_preflight_ok_derivation, live_preflight_notes_derivation = validate_live_startup_requirements()

        runner_mod.SETTINGS.clob_api_key = "api-key"
        runner_mod.SETTINGS.clob_api_secret = ""
        runner_mod.SETTINGS.clob_api_passphrase = "api-pass"
        live_preflight_ok_partial, live_preflight_notes_partial = validate_live_startup_requirements()
    finally:
        runner_mod.SETTINGS.dry_run = original_dry_run
        runner_mod.SETTINGS.private_key = original_private_key
        runner_mod.SETTINGS.funder_address = original_funder_address
        runner_mod.SETTINGS.clob_api_key = original_clob_api_key
        runner_mod.SETTINGS.clob_api_secret = original_clob_api_secret
        runner_mod.SETTINGS.clob_api_passphrase = original_clob_api_passphrase

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
            "partial_profit_residual_honors_explicit_sync_protection",
            len(partial_protected_positions) == 1
            and partial_protected_positions[0].force_close_only is False
            and any(
                "sync_hold token=partial-token" in note and "protect_sec_left=" in note
                for note in partial_protected_notes
            )
        ),
        (
            "sync_open_positions_preserves_lottery_activation_state",
            len(synced_lottery_positions) == 1
            and synced_lottery_positions[0].lottery_activated is True
            and abs(synced_lottery_positions[0].lottery_activated_ts - 123.0) < 1e-9
        ),
        (
            "dedupe_open_positions_uses_live_position_as_authoritative_total",
            len(deduped_positions) == 1
            and abs(deduped_positions[0].shares - 1.25) < 1e-9
            and abs(deduped_positions[0].cost_usd - 0.95) < 1e-9
            and deduped_positions[0].pending_confirmation is True
            and deduped_positions[0].has_taken_partial is True
            and deduped_positions[0].live_miss_count == 1
            and any("sanitize_merge[runtime-test] token=dup-token" in note for note in dedupe_notes)
        ),
        (
            "existing_token_entry_conflict_blocks_duplicate_open_positions",
            token_conflict_open == (True, "open-position", 1, 1.0)
        ),
        (
            "existing_token_entry_conflict_blocks_duplicate_pending_orders",
            token_conflict_pending == (True, "pending-order", 1, 0.0)
        ),
        ("same_direction_cooldown_scopes_to_current_market", same_market_cooldown_age is not None and abs(same_market_cooldown_age - 100.0) < 1e-9),
        ("same_direction_cooldown_ignores_other_markets", cross_market_cooldown_age is None),
        ("loss_reversal_origin_flips_strategy_side", reversed_loss_origin == "model-ws_order_flow_up+loss-reversal"),
        ("clean_start_resets_stale_loss_streak", stale_loss_reset is True and abs(stale_loss_age - 350.0) < 1e-9),
        ("clean_start_keeps_recent_loss_streak", recent_loss_reset is False and abs(recent_loss_age - 250.0) < 1e-9),
        ("clean_start_keeps_loss_streak_when_positions_are_active", active_position_reset is False),
        (
            "runtime_sync_drop_can_clear_stale_loss_breaker",
            runtime_sync_reset_positions == []
            and any("sync_drop token=startup-stale-token" in note for note in runtime_sync_reset_notes)
            and runtime_sync_reset_note.startswith("reset stale loss streak after runtime sync |")
            and runtime_sync_reset_risk.consec_losses == 0
            and runtime_sync_reset_flags.live_consec_losses == 0
            and runtime_sync_reset_flags.last_loss_side == ""
        ),
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
        (
            "live_preflight_blocks_missing_required_wallet_settings",
            live_preflight_ok_missing is False
            and live_preflight_notes_missing[0] == "live startup preflight failed | missing required settings: PRIVATE_KEY, FUNDER_ADDRESS"
            and ".env.local or .env.secrets" in live_preflight_notes_missing[1]
        ),
        (
            "live_preflight_allows_wallet_based_clob_derivation",
            live_preflight_ok_derivation is True
            and live_preflight_notes_derivation == [
                "live startup preflight | CLOB_API_* not set; client will derive API creds from wallet"
            ]
        ),
        (
            "live_preflight_warns_on_partial_clob_creds",
            live_preflight_ok_partial is True
            and len(live_preflight_notes_partial) == 1
            and "partial CLOB_API_* detected" in live_preflight_notes_partial[0]
        ),
        ("pending_orders_poll_every_second", abs(pending_order_poll_interval_seconds() - 1.0) < 1e-9),
        ("open_positions_poll_every_second", abs(open_position_poll_interval_seconds() - 1.0) < 1e-9),
        ("next_cycle_interval_uses_fast_pending_poll", abs(next_cycle_interval_seconds(has_pending_orders=True, has_open_positions=False) - 1.0) < 1e-9),
        ("next_cycle_interval_uses_fast_open_position_poll", abs(next_cycle_interval_seconds(has_pending_orders=False, has_open_positions=True) - 1.0) < 1e-9),
        ("next_cycle_interval_uses_two_second_market_poll_floor", abs(next_cycle_interval_seconds(has_pending_orders=False, has_open_positions=False) - 2.0) < 1e-9),
        ("idle_sleep_prefers_fast_pending_poll", abs(idle_sleep_seconds(has_open_positions=False, has_pending_orders=True) - 1.0) < 1e-9),
        ("idle_sleep_prefers_fast_open_position_poll", abs(idle_sleep_seconds(has_open_positions=True, has_pending_orders=False) - 1.0) < 1e-9),
        ("daily_loss_pause_sleep_backs_off_until_market_end", abs(daily_loss_pause_sleep - 44.0) < 1e-9),
    ])

    failed = [name for name, ok in cases if not ok]
    if failed:
        raise SystemExit(f"FAILED: {', '.join(failed)}")
    print("OK")


def test_main():
    main()


if __name__ == "__main__":
    main()
