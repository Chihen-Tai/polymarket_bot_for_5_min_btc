import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.runner import (
    OpenPos,
    PendingOrder,
    clear_expired_market_state,
    idle_sleep_seconds,
    next_cycle_interval_seconds,
    open_position_poll_interval_seconds,
    pending_order_poll_interval_seconds,
    same_direction_entry_cooldown_age_sec,
    sync_open_positions,
)
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
