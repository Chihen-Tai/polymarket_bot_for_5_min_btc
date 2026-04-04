import os
import sys
from collections import deque
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import SETTINGS
from core.decision_engine import explain_choose_side
from core.journal import replay_open_positions
from core.learning import StrategyScoreboard
from core.market_resolver import _extract_token_pair
from core.risk import can_place_order
from core.runner import (
    RuntimeFlags,
    assess_entry_liquidity,
    apply_scoreboard_aux_probability,
    collect_ranked_entry_candidates,
    decide_pending_order_action,
    entry_velocity_gate_rejects,
    entry_response_has_actionable_state,
    normalize_execution_style as normalize_runner_execution_style,
    refresh_runtime_flags,
    observe_api_latency,
    paper_settlement_from_last_mark,
    price_aware_kelly_fraction,
    required_trade_edge,
    score_entry_candidate,
    select_ranked_entry_candidate,
    select_ranked_entry_candidate_for_side,
    stabilize_entry_win_rate,
    strategy_name_for_side,
    summarize_entry_edge,
    update_network_guard,
)
from core.trade_manager import decide_exit, maybe_reverse_entry, can_reenter_same_market, should_block_same_market_reentry
import core.learning as learning_mod
import scripts.journal_analysis as journal_analysis_mod
from scripts.journal_analysis import (
    build_exit_accounting_rows,
    build_trade_pairs,
    classify_actual_source_tier,
    load_trade_events,
    normalize_execution_style as normalize_report_execution_style,
    summarize_trade_pairs,
)


def evaluate_with_settings(temp_settings, predicate):
    originals = {key: getattr(SETTINGS, key) for key in temp_settings}
    try:
        for key, value in temp_settings.items():
            setattr(SETTINGS, key, value)
        return bool(predicate())
    finally:
        for key, value in originals.items():
            setattr(SETTINGS, key, value)


def main():
    # Keep these tests independent from the repository .env so strategy-tuning
    # changes do not silently invalidate the expected baseline behaviors here.
    SETTINGS.smart_stop_loss_enabled = True
    SETTINGS.stop_loss_partial_pct = 0.05
    SETTINGS.stop_loss_warn_pct = 0.07
    SETTINGS.stop_loss_pct = 0.10
    SETTINGS.max_hold_seconds = 90
    SETTINGS.min_entry_price = 0.35
    SETTINGS.max_entry_price = 0.75
    SETTINGS.entry_max_spread = 0.03
    SETTINGS.entry_min_best_ask_multiple = 2.0
    SETTINGS.entry_min_total_ask_multiple = 6.0
    SETTINGS.entry_window_min_sec = 120
    SETTINGS.entry_window_max_sec = 999999.0
    SETTINGS.exit_deadline_sec = 20
    SETTINGS.exit_deadline_profit_sec = 5
    SETTINGS.exit_ghost_town_sec = 0
    SETTINGS.exit_deadline_flat_pnl_pct = 0.0
    SETTINGS.take_profit_soft_pct = 0.18
    SETTINGS.take_profit_bid_discount_buffer = 0.08
    SETTINGS.take_profit_partial_fraction = 0.40
    SETTINGS.take_profit_hard_pct = 0.30
    SETTINGS.take_profit_runner_fraction = 0.10
    SETTINGS.edge_threshold = 0.02
    SETTINGS.entry_neutral_band_half_width = 0.03
    SETTINGS.entry_neutral_edge_penalty = 0.02
    SETTINGS.entry_micro_band_half_width = 0.01
    SETTINGS.entry_micro_edge_penalty = 0.02
    SETTINGS.entry_side_conflict_enabled = True
    SETTINGS.entry_side_conflict_min_edge_gap = 0.025
    SETTINGS.entry_side_conflict_min_prob_gap = 0.03
    SETTINGS.report_assumed_taker_fee_rate = 0.0156
    SETTINGS.report_scratch_pnl_pct = 0.03
    SETTINGS.late_entry_edge_penalty = 0.015
    SETTINGS.rich_price_edge_penalty = 0.015
    SETTINGS.scoreboard_aux_weight = 0.10
    SETTINGS.binary_kelly_divisor = 4.0
    SETTINGS.force_full_exit_on_take_profit = False
    SETTINGS.force_full_exit_on_stop_loss_scaleout = False
    SETTINGS.failed_follow_through_window_sec = 45
    SETTINGS.failed_follow_through_loss_pct = 0.03
    SETTINGS.failed_follow_through_max_mfe_pct = 0.02
    SETTINGS.failed_follow_through_min_secs_left = 90
    SETTINGS.stalled_exit_window_sec = 35
    SETTINGS.stalled_exit_min_loss_pct = 0.01
    SETTINGS.stalled_exit_max_abs_pnl_pct = 0.02
    SETTINGS.stalled_exit_max_mfe_pct = 0.02
    SETTINGS.stalled_exit_min_secs_left = 45
    SETTINGS.post_scaleout_loss_exit_delay_sec = 20
    SETTINGS.post_scaleout_loss_exit_pct = 0.10
    SETTINGS.moonbag_drawdown_pct = 0.35
    SETTINGS.moonbag_drawdown_window_sec = 45
    SETTINGS.moonbag_min_peak_value_usd = 0.10
    SETTINGS.same_market_reentry_min_secs_left = 45
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
    zero_actual_accounting_rows = build_exit_accounting_rows([
        {
            "kind": "exit",
            "ts": "2026-03-19T10:00:30",
            "event_id": "exit_zero",
            "position_id": "pos_zero",
            "slug": "m_zero",
            "side": "UP",
            "reason": "dry-run-market-expired-binary-lose",
            "closed_shares": 4,
            "realized_cost_usd": 1.0,
            "actual_exit_value_usd": 0.0,
            "actual_exit_value_source": "paper_trade_simulation",
            "observed_exit_value_usd": 0.0,
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
    zero_actual_pair_rows = build_trade_pairs([
        {
            "kind": "entry",
            "ts": "2026-03-19T10:03:00",
            "event_id": "entry_zero",
            "position_id": "pos_zero",
            "slug": "m_zero",
            "side": "UP",
            "token_id": "tok_zero",
            "shares": 10,
            "cost_usd": 1.0,
            "execution_style": "unknown",
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:05:00",
            "event_id": "exit_zero",
            "position_id": "pos_zero",
            "slug": "m_zero",
            "side": "UP",
            "token_id": "tok_zero",
            "closed_shares": 10,
            "remaining_shares": 0,
            "realized_cost_usd": 1.0,
            "actual_exit_value_usd": 0.0,
            "actual_exit_value_source": "paper_trade_simulation",
            "observed_exit_value_usd": 0.0,
            "reason": "dry-run-market-expired-binary-lose",
            "exit_execution_style": "expiry-settlement",
        },
    ])
    zero_actual_summary = summarize_trade_pairs(zero_actual_pair_rows)
    orphan_residual_rows = build_trade_pairs([
        {
            "kind": "exit",
            "ts": "2026-03-19T10:05:30",
            "event_id": "exit_orphan",
            "position_id": "pos_orphan",
            "slug": "m_orphan",
            "side": "DOWN",
            "token_id": "tok_orphan",
            "closed_shares": 2.0,
            "remaining_shares": 0.0,
            "realized_cost_usd": 1.0,
            "actual_exit_value_usd": 1.2,
            "actual_exit_value_source": "close_response_takingAmount",
            "observed_exit_value_usd": 1.1,
            "reason": "residual-force-close",
            "exit_execution_style": "taker",
        },
    ])
    orphan_residual_summary = summarize_trade_pairs(orphan_residual_rows)
    original_funder_address = SETTINGS.funder_address
    original_fetch_activity = journal_analysis_mod._fetch_account_trade_activity
    SETTINGS.funder_address = "0xtest-user"
    journal_analysis_mod._fetch_account_trade_activity = lambda **kwargs: [
        {
            "type": "TRADE",
            "timestamp": 1773886440,
            "asset": "tok_orphan",
            "slug": "m_orphan",
            "side": "BUY",
            "size": 2.0,
            "usdcSize": 1.0,
            "transactionHash": "0xbuy",
        },
        {
            "type": "TRADE",
            "timestamp": 1773886530,
            "asset": "tok_orphan",
            "slug": "m_orphan",
            "side": "SELL",
            "size": 2.0,
            "usdcSize": 1.2,
            "transactionHash": "0xsell",
        },
    ]
    try:
        reconciled_orphan_rows = build_trade_pairs([
            {
                "kind": "exit",
                "ts": "2026-03-19T10:15:30",
                "event_id": "exit_orphan_reconciled",
                "position_id": "pos_orphan_reconciled",
                "slug": "m_orphan",
                "side": "DOWN",
                "token_id": "tok_orphan",
                "closed_shares": 2.0,
                "remaining_shares": 0.0,
                "realized_cost_usd": 1.0,
                "actual_exit_value_usd": 1.2,
                "actual_exit_value_source": "cash_balance_delta",
                "observed_exit_value_usd": 1.1,
                "reason": "residual-force-close",
                "exit_execution_style": "taker",
            },
        ])
    finally:
        SETTINGS.funder_address = original_funder_address
        journal_analysis_mod._fetch_account_trade_activity = original_fetch_activity
    reconciled_orphan_summary = summarize_trade_pairs(reconciled_orphan_rows)
    collapsed_principal_rows = build_trade_pairs([
        {
            "kind": "entry",
            "ts": "2026-03-19T10:20:00",
            "event_id": "entry_principal",
            "position_id": "pos_principal",
            "slug": "m_principal",
            "side": "UP",
            "token_id": "tok_principal",
            "shares": 2.0,
            "cost_usd": 1.0,
            "execution_style": "taker",
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:21:00",
            "event_id": "exit_partial_principal",
            "position_id": "pos_principal",
            "slug": "m_principal",
            "side": "UP",
            "token_id": "tok_principal",
            "closed_shares": 1.0,
            "remaining_shares": 1.0,
            "realized_cost_usd": 0.5,
            "actual_exit_value_usd": 0.6,
            "actual_exit_value_source": "close_response_takingAmount",
            "observed_exit_value_usd": 0.6,
            "reason": "take-profit-partial",
            "exit_execution_style": "taker",
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:22:00",
            "event_id": "exit_principal_overflow",
            "position_id": "pos_principal",
            "slug": "m_principal",
            "side": "UP",
            "token_id": "tok_principal",
            "closed_shares": 2.0,
            "remaining_shares": 0.0,
            "realized_cost_usd": 0.5,
            "actual_exit_value_usd": 0.6,
            "actual_exit_value_source": "close_response_takingAmount",
            "observed_exit_value_usd": 0.6,
            "reason": "take-profit-principal",
            "exit_execution_style": "taker",
        },
    ])
    collapsed_principal_summary = summarize_trade_pairs(collapsed_principal_rows)
    mixed_actual_fallback_rows = build_trade_pairs([
        {
            "kind": "entry",
            "ts": "2026-03-19T10:25:00",
            "event_id": "entry_mixed_actual",
            "position_id": "pos_mixed_actual",
            "slug": "m_mixed_actual",
            "side": "DOWN",
            "token_id": "tok_mixed_actual",
            "shares": 2.0,
            "cost_usd": 1.0,
            "execution_style": "taker",
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:26:00",
            "event_id": "exit_mixed_actual_partial",
            "position_id": "pos_mixed_actual",
            "slug": "m_mixed_actual",
            "side": "DOWN",
            "token_id": "tok_mixed_actual",
            "closed_shares": 1.0,
            "remaining_shares": 1.0,
            "realized_cost_usd": 0.5,
            "actual_exit_value_usd": 0.6,
            "actual_exit_value_source": "cash_balance_delta",
            "observed_exit_value_usd": 0.6,
            "reason": "take-profit-partial",
            "exit_execution_style": "taker",
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:27:00",
            "event_id": "exit_mixed_actual_principal",
            "position_id": "pos_mixed_actual",
            "slug": "m_mixed_actual",
            "side": "DOWN",
            "token_id": "tok_mixed_actual",
            "closed_shares": 1.0,
            "remaining_shares": 0.0,
            "realized_cost_usd": 0.5,
            "actual_exit_value_usd": None,
            "actual_exit_value_source": "unavailable",
            "observed_exit_value_usd": 0.9,
            "reason": "take-profit-principal",
            "exit_execution_style": "taker",
        },
    ])
    mixed_actual_fallback_summary = summarize_trade_pairs(mixed_actual_fallback_rows)
    original_fetch_market_settlement = journal_analysis_mod._fetch_market_settlement
    journal_analysis_mod._fetch_market_settlement = lambda slug, side: (0.0, "market-expired-binary-loss")
    try:
        settled_unmatched_rows = build_trade_pairs([
            {
                "kind": "entry",
                "ts": "2026-03-19T10:06:00",
                "event_id": "entry_unresolved",
                "position_id": "pos_unresolved",
                "slug": "btc-updown-5m-1",
                "side": "DOWN",
                "token_id": "tok_unresolved",
                "shares": 2.5,
                "cost_usd": 1.0,
                "execution_style": "unknown",
            },
        ])
    finally:
        journal_analysis_mod._fetch_market_settlement = original_fetch_market_settlement
    settled_unmatched_summary = summarize_trade_pairs(settled_unmatched_rows)
    original_funder_address = SETTINGS.funder_address
    original_fetch_activity = journal_analysis_mod._fetch_account_trade_activity
    original_fetch_market_settlement = journal_analysis_mod._fetch_market_settlement
    SETTINGS.funder_address = "0xtest-user"
    journal_analysis_mod._fetch_account_trade_activity = lambda **kwargs: [
        {
            "type": "TRADE",
            "timestamp": 1773885960,
            "asset": "tok_unresolved_activity",
            "slug": "btc-updown-5m-1773885840",
            "side": "BUY",
            "size": 2.5,
            "usdcSize": 1.0,
            "transactionHash": "0xbuy-expiry",
        },
        {
            "type": "TRADE",
            "timestamp": 1773886140,
            "asset": "tok_unresolved_activity",
            "slug": "btc-updown-5m-1773885840",
            "side": "SELL",
            "size": 2.5,
            "usdcSize": 0.1,
            "transactionHash": "0xsell-expiry",
        },
    ]
    journal_analysis_mod._fetch_market_settlement = lambda slug, side: (0.5, "market-expired-settlement")
    try:
        settled_with_activity_rows = build_trade_pairs([
            {
                "kind": "entry",
                "ts": "2026-03-19T10:06:00+00:00",
                "event_id": "entry_unresolved_activity",
                "position_id": "pos_unresolved_activity",
                "slug": "btc-updown-5m-1773885840",
                "side": "DOWN",
                "token_id": "tok_unresolved_activity",
                "shares": 2.5,
                "cost_usd": 1.0,
                "execution_style": "unknown",
            },
        ])
    finally:
        SETTINGS.funder_address = original_funder_address
        journal_analysis_mod._fetch_account_trade_activity = original_fetch_activity
        journal_analysis_mod._fetch_market_settlement = original_fetch_market_settlement
    settled_with_activity_summary = summarize_trade_pairs(settled_with_activity_rows)
    fee_summary_rows = build_trade_pairs([
        {
            "kind": "entry",
            "ts": "2026-03-19T10:00:00",
            "event_id": "entry_fee_active",
            "position_id": "pos_fee_active",
            "slug": "m_fee_active",
            "side": "UP",
            "token_id": "tok_fee_active",
            "shares": 10,
            "cost_usd": 1.0,
            "execution_style": "taker",
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:01:00",
            "event_id": "exit_fee_active",
            "position_id": "pos_fee_active",
            "slug": "m_fee_active",
            "side": "UP",
            "token_id": "tok_fee_active",
            "closed_shares": 10,
            "remaining_shares": 0,
            "realized_cost_usd": 1.0,
            "actual_exit_value_usd": 1.2,
            "observed_exit_value_usd": 1.2,
            "reason": "take-profit-hard",
            "exit_execution_style": "taker",
        },
        {
            "kind": "entry",
            "ts": "2026-03-19T10:02:00",
            "event_id": "entry_fee_expiry",
            "position_id": "pos_fee_expiry",
            "slug": "m_fee_expiry",
            "side": "DOWN",
            "token_id": "tok_fee_expiry",
            "shares": 10,
            "cost_usd": 1.0,
            "execution_style": "taker",
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:05:00",
            "event_id": "exit_fee_expiry",
            "position_id": "pos_fee_expiry",
            "slug": "m_fee_expiry",
            "side": "DOWN",
            "token_id": "tok_fee_expiry",
            "closed_shares": 10,
            "remaining_shares": 0,
            "realized_cost_usd": 1.0,
            "actual_exit_value_usd": 1.8,
            "observed_exit_value_usd": 1.8,
            "reason": "dry-run-market-expired-binary-win",
            "exit_execution_style": "expiry-settlement",
        },
    ])
    fee_summary = summarize_trade_pairs(fee_summary_rows)
    scratch_summary_rows = build_trade_pairs([
        {
            "kind": "entry",
            "ts": "2026-03-19T10:10:00",
            "event_id": "entry_scratch",
            "position_id": "pos_scratch",
            "slug": "m_scratch",
            "side": "UP",
            "token_id": "tok_scratch",
            "shares": 10,
            "cost_usd": 1.0,
            "execution_style": "unknown",
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:10:40",
            "event_id": "exit_scratch",
            "position_id": "pos_scratch",
            "slug": "m_scratch",
            "side": "UP",
            "token_id": "tok_scratch",
            "closed_shares": 10,
            "remaining_shares": 0,
            "realized_cost_usd": 1.0,
            "actual_exit_value_usd": 1.0,
            "observed_exit_value_usd": 1.0,
            "reason": "stalled-trade",
            "exit_execution_style": "unknown",
        },
    ])
    scratch_summary = summarize_trade_pairs(scratch_summary_rows)
    book_gate_ok = assess_entry_liquidity(
        book={"best_bid": 0.49, "best_ask": 0.51, "best_ask_size": 5.0, "asks_volume": 20.0},
        est_shares=2.0,
        max_spread=0.03,
        min_best_ask_multiple=2.0,
        min_total_ask_multiple=6.0,
    )
    book_gate_wide = assess_entry_liquidity(
        book={"best_bid": 0.45, "best_ask": 0.51, "best_ask_size": 5.0, "asks_volume": 20.0},
        est_shares=2.0,
        max_spread=0.03,
        min_best_ask_multiple=2.0,
        min_total_ask_multiple=6.0,
    )
    book_gate_thin = assess_entry_liquidity(
        book={"best_bid": 0.49, "best_ask": 0.51, "best_ask_size": 2.0, "asks_volume": 8.0},
        est_shares=2.0,
        max_spread=0.03,
        min_best_ask_multiple=2.0,
        min_total_ask_multiple=6.0,
    )
    reversed_token_pair = _extract_token_pair(
        {
            "outcomes": '["Down", "Up"]',
            "clobTokenIds": '["tok_down", "tok_up"]',
        }
    )
    original_min_sec = SETTINGS.entry_window_min_sec
    original_max_sec = SETTINGS.entry_window_max_sec
    SETTINGS.entry_window_min_sec = 15
    SETTINGS.entry_window_max_sec = 180
    natural_window_edge = required_trade_edge(0.70, 150, history_count=30)
    natural_window_center_edge = required_trade_edge(0.50, 150, history_count=30)
    SETTINGS.entry_window_min_sec = original_min_sec
    SETTINGS.entry_window_max_sec = original_max_sec

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
        poly_ob_up={"bids_volume": 9.0, "asks_volume": 1.0},
        poly_ob_down={"bids_volume": 5.0, "asks_volume": 5.0},
    )
    dual_signal_decision = explain_choose_side(
        market={
            "outcomes": ["up", "down"],
            "outcomePrices": [0.64, 0.36],
            "endDate": future_end,
        },
        yes_window=deque([0.41, 0.42, 0.43, 0.44, 0.45, 0.46, 0.47, 0.48, 0.49, 0.50], maxlen=20),
        up_window=deque([0.62, 0.63, 0.64], maxlen=5),
        down_window=deque([0.33, 0.34, 0.36], maxlen=5),
        observed_up=0.64,
        observed_down=0.36,
        ws_trades=[
            {"p": 100000.0, "q": 1.5, "m": False},
            {"p": 100001.0, "q": 1.2, "m": False},
            {"p": 100002.0, "q": 1.0, "m": False},
            {"p": 100003.0, "q": 0.1, "m": True},
        ],
    )
    class StubScoreboard:
        def __init__(self, scores: dict[str, float], decisive: int = 20, trades: int = 40):
            self.scores = scores
            self.decisive = decisive
            self.trades = trades

        def get_strategy_score(self, name: str) -> float:
            return float(self.scores.get(name, 0.5))

        def get_strategy_trade_count(self, name: str) -> int:
            return self.trades

        def get_strategy_decisive_trade_count(self, name: str) -> int:
            return self.decisive

    candidate_pick, candidate_rejections = select_ranked_entry_candidate(
        {
            "ok": True,
            "ranked_candidates": [
                {
                    "side": "DOWN",
                    "strategy_name": "model-ws_order_flow_down",
                    "entry_price": 0.45,
                    "model_probability": 0.62,
                },
                {
                    "side": "UP",
                    "strategy_name": "model-ws_flash_snipe_up",
                    "entry_price": 0.45,
                    "model_probability": 0.58,
                },
            ],
        },
        ws_velocity=0.0005,
        current_ws_velocity=0.0004,
        secs_left=200,
        scoreboard=StubScoreboard(
            {
                "model-ws_order_flow_down": 0.55,
                "model-ws_flash_snipe_up": 0.56,
            }
        ),
    )
    sparse_candidate = score_entry_candidate(
        {
            "side": "UP",
            "strategy_name": "model-ws_flash_snipe_up",
            "entry_price": 0.45,
            "model_probability": 0.52,
        },
        secs_left=200,
        scoreboard=StubScoreboard({"model-ws_flash_snipe_up": 0.18}, decisive=1, trades=2),
    )
    side_conflict_candidate, side_conflict_rejections = select_ranked_entry_candidate(
        {
            "ok": True,
            "ranked_candidates": [
                {
                    "side": "UP",
                    "strategy_name": "model-ws_order_flow_up",
                    "entry_price": 0.46,
                    "model_probability": 0.61,
                },
                {
                    "side": "DOWN",
                    "strategy_name": "model-poly_ob_imbalance_down",
                    "entry_price": 0.47,
                    "model_probability": 0.60,
                },
            ],
        },
        ws_velocity=0.0,
        current_ws_velocity=0.0,
        secs_left=220,
        scoreboard=StubScoreboard(
            {
                "model-ws_order_flow_up": 0.55,
                "model-poly_ob_imbalance_down": 0.56,
            }
        ),
    )
    low_aux_wr_candidate, low_aux_wr_rejections = select_ranked_entry_candidate(
        {
            "ok": True,
            "ranked_candidates": [
                {
                    "side": "DOWN",
                    "strategy_name": "model-ws_order_flow_down",
                    "entry_price": 0.45,
                    "model_probability": 0.72,
                },
            ],
        },
        ws_velocity=-0.0005,
        current_ws_velocity=-0.0004,
        secs_left=200,
        scoreboard=StubScoreboard(
            {
                "model-ws_order_flow_down": 0.20,
            },
            decisive=20,
            trades=40,
        ),
    )
    reversal_side_candidate, reversal_side_rejections = select_ranked_entry_candidate_for_side(
        {
            "ok": True,
            "ranked_candidates": [
                {
                    "side": "DOWN",
                    "strategy_name": "model-ws_order_flow_down",
                    "entry_price": 0.45,
                    "model_probability": 0.62,
                },
                {
                    "side": "UP",
                    "strategy_name": "model-ws_flash_snipe_up",
                    "entry_price": 0.45,
                    "model_probability": 0.58,
                },
            ],
        },
        side="UP",
        ws_velocity=0.0005,
        current_ws_velocity=0.0004,
        secs_left=200,
        scoreboard=StubScoreboard(
            {
                "model-ws_order_flow_down": 0.55,
                "model-ws_flash_snipe_up": 0.56,
            }
        ),
    )
    missing_reversal_candidate, missing_reversal_rejections = select_ranked_entry_candidate_for_side(
        {
            "ok": True,
            "ranked_candidates": [
                {
                    "side": "DOWN",
                    "strategy_name": "model-ws_order_flow_down",
                    "entry_price": 0.45,
                    "model_probability": 0.62,
                },
            ],
        },
        side="UP",
        ws_velocity=-0.0005,
        current_ws_velocity=-0.0004,
        secs_left=200,
        scoreboard=StubScoreboard(
            {
                "model-ws_order_flow_down": 0.55,
            }
        ),
    )
    eligible_candidates, eligible_rejections = collect_ranked_entry_candidates(
        {
            "ok": True,
            "ranked_candidates": [
                {
                    "side": "DOWN",
                    "strategy_name": "model-ws_order_flow_down",
                    "entry_price": 0.45,
                    "model_probability": 0.62,
                },
                {
                    "side": "UP",
                    "strategy_name": "model-ws_flash_snipe_up",
                    "entry_price": 0.45,
                    "model_probability": 0.58,
                },
            ],
        },
        ws_velocity=0.0,
        current_ws_velocity=0.0,
        secs_left=200,
        scoreboard=StubScoreboard(
            {
                "model-ws_order_flow_down": 0.55,
                "model-ws_flash_snipe_up": 0.56,
            }
        ),
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

    original_score_file = learning_mod.SCORE_FILE
    temp_score_file = os.path.join(os.path.dirname(__file__), "tmp_strategy_scores.json")
    learning_mod.SCORE_FILE = temp_score_file
    try:
        if os.path.exists(temp_score_file):
            os.remove(temp_score_file)
        scratch_scoreboard = StrategyScoreboard()
        scratch_scoreboard.record_outcome("model-ws_order_flow_up", 0.0, 1.0)
        scratch_score = scratch_scoreboard.get_strategy_score("model-ws_order_flow_up")
        scratch_total = scratch_scoreboard.get_strategy_trade_count("model-ws_order_flow_up")
        scratch_decisive = scratch_scoreboard.get_strategy_decisive_trade_count("model-ws_order_flow_up")
    finally:
        learning_mod.SCORE_FILE = original_score_file
        if os.path.exists(temp_score_file):
            os.remove(temp_score_file)

    original_read_events = journal_analysis_mod.read_events
    journal_analysis_mod.read_events = lambda limit=0: [
        {
            "kind": "entry",
            "ts": "2026-03-19T09:59:00",
            "event_id": "entry_pre",
            "position_id": "pos_run_x",
            "token_id": "tok_run_x",
            "slug": "m1",
            "side": "UP",
            "shares": 2.0,
            "cost_usd": 1.0,
        },
        {
            "kind": "exit",
            "ts": "2026-03-19T10:01:00",
            "event_id": "exit_run_x",
            "run_id": "run_x",
            "position_id": "pos_run_x",
            "token_id": "tok_run_x",
            "slug": "m1",
            "side": "UP",
            "closed_shares": 2.0,
            "remaining_shares": 0.0,
            "realized_cost_usd": 1.0,
            "actual_exit_value_usd": 1.2,
            "observed_exit_value_usd": 1.2,
        },
        {
            "kind": "entry",
            "ts": "2026-03-19T10:02:00",
            "event_id": "entry_other",
            "run_id": "run_other",
            "position_id": "pos_other",
            "token_id": "tok_other",
            "slug": "m2",
            "side": "DOWN",
            "shares": 2.0,
            "cost_usd": 1.0,
        },
    ]
    try:
        filtered_run_events = load_trade_events(run_id="run_x")
    finally:
        journal_analysis_mod.read_events = original_read_events

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
        ("stalled_trade_exit", decide_exit(pnl_pct=-0.01, hold_sec=40, secs_left=55, mfe_pnl_pct=0.01).reason == "stalled-trade"),
        ("stalled_trade_skips_exact_flat", decide_exit(pnl_pct=0.0, hold_sec=40, secs_left=55, mfe_pnl_pct=0.01).reason != "stalled-trade"),
        ("stalled_trade_skips_if_trade_showed_life", decide_exit(pnl_pct=0.0, hold_sec=40, secs_left=55, mfe_pnl_pct=0.05).reason != "stalled-trade"),
        ("stalled_trade_skips_if_reentry_window_too_short", decide_exit(pnl_pct=0.0, hold_sec=40, secs_left=40, mfe_pnl_pct=0.01).reason != "stalled-trade"),
        ("post_scaleout_loss_promotes_to_full_exit", decide_exit(pnl_pct=-0.12, hold_sec=30, secs_left=120, has_scaled_out_loss=True).reason == "post-scaleout-stop-loss"),
        ("deadline_exit_flat_without_principal", decide_exit(pnl_pct=0.0, hold_sec=50, secs_left=10).reason == "deadline-exit-flat"),
        ("deadline_exit_allows_moonbag_hold", decide_exit(pnl_pct=0.0, hold_sec=50, secs_left=10, has_extracted_principal=True).reason != "deadline-exit-flat"),
        ("deadline_exit_weak_win_without_principal", decide_exit(pnl_pct=0.12, hold_sec=50, secs_left=10).reason == "deadline-exit-weak-win"),
        ("deadline_exit_weak_win_skips_risk_free_moonbag", decide_exit(pnl_pct=0.22, hold_sec=50, secs_left=10, has_extracted_principal=True).reason != "deadline-exit-weak-win"),
        ("deadline_exit_loss_skips_risk_free_moonbag", decide_exit(pnl_pct=-0.40, hold_sec=50, secs_left=10, has_extracted_principal=True).should_close is False),
        (
            "profit_between_45_and_30s_forces_full_exit",
            evaluate_with_settings(
                {"exit_ghost_town_sec": 30, "exit_deadline_profit_sec": 45},
                lambda: decide_exit(pnl_pct=0.12, hold_sec=50, secs_left=30.4).reason == "deadline-take-profit-full",
            ),
        ),
        (
            "profit_before_last_30s_still_uses_profit_deadline",
            evaluate_with_settings(
                {"exit_ghost_town_sec": 30, "exit_deadline_profit_sec": 45},
                lambda: decide_exit(pnl_pct=0.12, hold_sec=50, secs_left=40).reason == "deadline-take-profit-full",
            ),
        ),
        (
            "last_30s_loss_still_lets_ride",
            evaluate_with_settings(
                {"exit_ghost_town_sec": 30},
                lambda: (
                    decide_exit(pnl_pct=-0.12, hold_sec=50, secs_left=29.9).reason == "ghost-town-let-ride"
                    and decide_exit(pnl_pct=-0.12, hold_sec=50, secs_left=29.9).should_close is False
                ),
            ),
        ),
        (
            "thirtieth_second_profit_now_lets_ride",
            evaluate_with_settings(
                {"exit_ghost_town_sec": 30, "exit_deadline_profit_sec": 45},
                lambda: (
                    decide_exit(pnl_pct=0.12, hold_sec=50, secs_left=30.0).reason == "ghost-town-let-ride"
                    and decide_exit(pnl_pct=0.12, hold_sec=50, secs_left=30.0).should_close is False
                ),
            ),
        ),
        (
            "inside_last_30s_profit_now_lets_ride",
            evaluate_with_settings(
                {"exit_ghost_town_sec": 30, "exit_deadline_profit_sec": 45},
                lambda: (
                    decide_exit(pnl_pct=0.12, hold_sec=50, secs_left=29.9).reason == "ghost-town-let-ride"
                    and decide_exit(pnl_pct=0.12, hold_sec=50, secs_left=29.9).should_close is False
                ),
            ),
        ),
        (
            "last_30s_flat_now_lets_ride",
            evaluate_with_settings(
                {"exit_ghost_town_sec": 30},
                lambda: (
                    decide_exit(pnl_pct=0.0, hold_sec=50, secs_left=10).reason == "ghost-town-let-ride"
                    and decide_exit(pnl_pct=0.0, hold_sec=50, secs_left=10).should_close is False
                ),
            ),
        ),
        ("max_hold_loss_skips_risk_free_moonbag", decide_exit(pnl_pct=-0.05, hold_sec=500, has_extracted_principal=True).should_close is False),
        (
            "moonbag_drawdown_triggers_fast_protection",
            decide_exit(
                pnl_pct=0.05,
                hold_sec=55,
                has_extracted_principal=True,
                runner_drawdown_pct=-0.35,
                runner_peak_age_sec=10.0,
                runner_peak_value_usd=0.25,
            ).reason == "moonbag-drawdown-stop",
        ),
            (
                "moonbag_drawdown_skips_if_peak_is_stale",
                decide_exit(
                    pnl_pct=0.05,
                    hold_sec=55,
                    has_extracted_principal=True,
                    runner_drawdown_pct=-0.35,
                    runner_peak_age_sec=46.0,
                    runner_peak_value_usd=0.25,
                ).should_close is False,
            ),
        (
            "moonbag_drawdown_skips_if_runner_is_tiny",
            decide_exit(
                pnl_pct=0.05,
                hold_sec=55,
                has_extracted_principal=True,
                runner_drawdown_pct=-0.35,
                runner_peak_age_sec=10.0,
                runner_peak_value_usd=0.05,
            ).should_close is False,
        ),
        ("smart_stop_loss_after_scale_out", decide_exit(pnl_pct=-0.08, hold_sec=5, recovery_chance_low=True, has_scaled_out_loss=True).reason == "smart-stop-loss"),
        ("smart_stop_loss_at_threshold", decide_exit(pnl_pct=-0.08, hold_sec=5, recovery_chance_low=True).reason == "smart-stop-loss"),
        ("hard_stop_loss", decide_exit(pnl_pct=-0.55, hold_sec=5).reason == "hard-stop-loss"),
        ("take_profit_uses_profit_reference_even_if_hard_stop_is_negative", decide_exit(pnl_pct=-0.05, profit_pnl_pct=0.60, hold_sec=5).reason == "take-profit-principal"),
        ("partial_take_profit_uses_profit_reference_even_if_hard_stop_is_negative", decide_exit(pnl_pct=-0.05, profit_pnl_pct=0.20, hold_sec=5).reason == "take-profit-partial"),
        ("take_profit_does_not_trigger_without_executable_profit_signal", decide_exit(pnl_pct=0.60, profit_pnl_pct=None, hold_sec=5).should_close is False),
        ("mark_fallback_requires_executable_profit_floor", decide_exit(pnl_pct=0.30, profit_pnl_pct=0.10, hold_sec=5).should_close is False),
        ("mark_fallback_allows_small_bid_discount", decide_exit(pnl_pct=0.28, profit_pnl_pct=0.11, hold_sec=5).reason == "take-profit-partial"),
            (
                "force_full_exit_on_take_profit",
                (setattr(SETTINGS, "force_full_exit_on_take_profit", True) or True)
                and decide_exit(pnl_pct=0.36, profit_pnl_pct=0.36, hold_sec=5).reason == "take-profit-full"
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
        (
            "reversal_target_uses_actual_live_candidate",
            reversal_side_candidate is not None
            and reversal_side_candidate.get("side") == "UP"
            and reversal_side_candidate.get("strategy_name") == "model-ws_flash_snipe_up",
        ),
        (
            "reversal_target_missing_side_returns_none",
            missing_reversal_candidate is None
            and missing_reversal_rejections == [],
        ),
        (
            "eligible_candidates_keep_both_sides_for_reversal_override",
            len(eligible_candidates) == 2
            and eligible_rejections == []
            and {candidate.get("side") for candidate in eligible_candidates} == {"UP", "DOWN"},
        ),
        ("reenter_gate", can_reenter_same_market(has_current_market_pos=False, closed_any=True, secs_left=50, current_market_slug="m1", blocked_market_slug="") is True),
        ("reenter_gate_respects_min_secs_left", can_reenter_same_market(has_current_market_pos=False, closed_any=True, secs_left=40, current_market_slug="m1", blocked_market_slug="") is False),
        ("reenter_block", can_reenter_same_market(has_current_market_pos=True, closed_any=True, secs_left=80, current_market_slug="m1", blocked_market_slug="") is False),
        ("reenter_same_market_slug_now_blocks", can_reenter_same_market(has_current_market_pos=False, closed_any=True, secs_left=80, current_market_slug="m1", blocked_market_slug="m1") is False),
        ("profitable_stop_loss_does_not_block_same_market_reentry", should_block_same_market_reentry("stop-loss", remaining_shares=0.02, realized_pnl_usd=0.05) is False),
        ("losing_residual_force_close_does_not_set_extra_market_block", should_block_same_market_reentry("residual-force-close", remaining_shares=0.25, realized_pnl_usd=-0.01) is False),
        ("scale_out_alone_does_not_block_same_market_reentry", should_block_same_market_reentry("stop-loss-scale-out", remaining_shares=0.25) is False),
        ("fully_closed_scale_out_now_blocks_same_market_reentry", should_block_same_market_reentry("stop-loss-scale-out", remaining_shares=0.0, realized_pnl_usd=-0.22) is True),
        ("deadline_exit_now_sets_market_block", should_block_same_market_reentry("deadline-exit-flat", remaining_shares=0.0) is True and should_block_same_market_reentry("deadline-exit-loss", remaining_shares=0.0) is True),
        ("binance_profit_protect_exit_blocks_same_market_reentry", should_block_same_market_reentry("binance-profit-protect-exit", remaining_shares=0.0, realized_pnl_usd=0.10) is True),
        ("lottery_plateau_stop_blocks_same_market_reentry", should_block_same_market_reentry("lottery-plateau-stop", remaining_shares=0.0, realized_pnl_usd=0.10) is True),
        ("terminal_take_profit_now_sets_market_block", should_block_same_market_reentry("take-profit-partial", remaining_shares=0.0, realized_pnl_usd=0.0) is True),
        ("non_terminal_exit_does_not_block_same_market_reentry", should_block_same_market_reentry("take-profit-partial", remaining_shares=0.5) is False),
        ("principal_exit_blocks_same_market_reentry", should_block_same_market_reentry("take-profit-principal", remaining_shares=0.0, realized_pnl_usd=0.12) is True),
        (
            "max_orders_per_market_is_hard_cap",
            can_place_order(
                equity=10.0,
                open_exposure=0.0,
                min_equity=1.0,
                order_usd=1.0,
                max_exposure_usd=10.0,
                max_orders_per_5min=3,
                max_consec_loss=10,
                daily_max_loss=10.0,
                consec_losses=0,
                daily_pnl=0.0,
                orders_this_window=3,
                current_ofi=0.99,
                ofi_bypass_threshold=0.1,
            )[0]
            is False
        ),
        ("journal_partial_close_shares", abs(lots["tok1"]["shares"] - 6.0) < 1e-9),
        ("journal_partial_close_cost", abs(lots["tok1"]["cost_usd"] - 0.6) < 1e-9),
        ("journal_partial_close_notes", len(notes) == 0),
        ("exit_accounting_diff", len(accounting_rows) == 1 and abs((accounting_rows[0].difference_usd or 0.0) - 0.05) < 1e-9),
        ("exit_accounting_zero_actual_is_available", len(zero_actual_accounting_rows) == 1 and zero_actual_accounting_rows[0].actual_exit_value_usd == 0.0 and zero_actual_accounting_rows[0].actual_status == "estimated"),
        ("trade_pair_closed", len(pair_rows) == 1 and pair_rows[0].status == "closed"),
        ("trade_pair_actual_pnl", len(pair_rows) == 1 and abs((pair_rows[0].actual_pnl_usd or 0.0) - 0.2) < 1e-9),
        ("trade_pair_zero_actual_loss_is_preserved", len(zero_actual_pair_rows) == 1 and abs((zero_actual_pair_rows[0].actual_pnl_usd or 0.0) + 1.0) < 1e-9),
        ("summary_counts_zero_actual_as_available", abs((zero_actual_summary["actual_available_ratio"] or 0.0) - 1.0) < 1e-9 and abs((zero_actual_summary["actual_pnl"]["sum"] or 0.0) + 1.0) < 1e-9),
        (
            "orphan_residual_rows_do_not_claim_trade_pnl",
            len(orphan_residual_rows) == 1
            and orphan_residual_rows[0].status == "residual"
            and orphan_residual_rows[0].actual_pnl_usd is None
            and orphan_residual_rows[0].observed_pnl_usd is None
            and "orphan-residual" in orphan_residual_rows[0].flags
            and "no-entry-match" in orphan_residual_rows[0].flags
        ),
        (
            "orphan_residual_rows_do_not_count_as_actual_performance",
            (orphan_residual_summary["actual_pnl"]["count"] or 0) == 0
            and abs((orphan_residual_summary["actual_available_ratio"] or 0.0) - 0.0) < 1e-9
            and orphan_residual_summary["status_counts"].get("residual") == 1
        ),
        (
            "account_activity_reconciles_orphan_residual_cost_basis",
            len(reconciled_orphan_rows) == 1
            and abs(reconciled_orphan_rows[0].entry_cost_usd - 1.0) < 1e-9
            and abs((reconciled_orphan_rows[0].actual_pnl_usd or 0.0) - 0.2) < 1e-9
            and "account-activity-reconciled-leg" in reconciled_orphan_rows[0].flags
        ),
        (
            "account_activity_reconciled_residual_counts_as_actual",
            abs((reconciled_orphan_summary["actual_available_ratio"] or 0.0) - 1.0) < 1e-9
            and abs((reconciled_orphan_summary["actual_pnl"]["sum"] or 0.0) - 0.2) < 1e-9
        ),
        (
            "principal_overflow_residuals_collapse_back_into_the_main_trade",
            len(collapsed_principal_rows) == 1
            and collapsed_principal_rows[0].status == "closed"
            and abs(collapsed_principal_rows[0].matched_exit_shares - 2.0) < 1e-9
            and abs((collapsed_principal_rows[0].actual_pnl_usd or 0.0) - 0.2) < 1e-9
            and "collapsed-overflow-residual" in collapsed_principal_rows[0].flags
            and abs((collapsed_principal_summary["actual_pnl"]["sum"] or 0.0) - 0.2) < 1e-9
        ),
        (
            "mixed_actual_rows_fill_missing_late_exit_legs_from_observed_value",
            len(mixed_actual_fallback_rows) == 1
            and mixed_actual_fallback_rows[0].status == "closed"
            and abs((mixed_actual_fallback_rows[0].exit_recovered_actual_usd or 0.0) - 1.5) < 1e-9
            and abs((mixed_actual_fallback_rows[0].actual_pnl_usd or 0.0) - 0.5) < 1e-9
            and mixed_actual_fallback_rows[0].actual_source == "mixed-actual-observed-fallback"
            and mixed_actual_fallback_rows[0].actual_source_tier == "medium"
            and "actual-partial-observed-fallback" in mixed_actual_fallback_rows[0].flags
            and abs((mixed_actual_fallback_summary["actual_pnl"]["sum"] or 0.0) - 0.5) < 1e-9
        ),
        (
            "expired_unmatched_position_is_settled_in_report",
            len(settled_unmatched_rows) == 1
            and settled_unmatched_rows[0].status == "closed"
            and settled_unmatched_rows[0].close_reason == "market-expired-binary-loss"
            and abs((settled_unmatched_rows[0].actual_pnl_usd or 0.0) + 1.0) < 1e-9
            and "market-settlement-imputed" in settled_unmatched_rows[0].flags
        ),
        (
            "expired_unmatched_settlement_counts_as_actual",
            abs((settled_unmatched_summary["actual_available_ratio"] or 0.0) - 1.0) < 1e-9
            and abs((settled_unmatched_summary["actual_pnl"]["sum"] or 0.0) + 1.0) < 1e-9
        ),
        (
            "expired_settlement_prefers_account_activity_when_available",
            len(settled_with_activity_rows) == 1
            and settled_with_activity_rows[0].actual_source == "account-activity-reconcile"
            and abs((settled_with_activity_rows[0].actual_pnl_usd or 0.0) + 0.9) < 1e-9
            and "account-activity-reconciled-leg" in settled_with_activity_rows[0].flags
            and "market-settlement-imputed" not in settled_with_activity_rows[0].flags
        ),
        (
            "expired_activity_reconcile_counts_as_high_confidence_actual",
            abs((settled_with_activity_summary["actual_available_ratio"] or 0.0) - 1.0) < 1e-9
            and abs((settled_with_activity_summary["actual_pnl"]["sum"] or 0.0) + 0.9) < 1e-9
            and settled_with_activity_summary["actual_source_tier_counts"].get("high") == 1
        ),
        ("trade_pair_fee_adjusted_defaults_unknown_to_zero", len(pair_rows) == 1 and abs((pair_rows[0].fee_adjusted_actual_pnl_usd or 0.0) - 0.2) < 1e-9),
        ("trade_pair_mae_mfe", len(pair_rows) == 1 and pair_rows[0].mae_pnl_usd == -0.1 and pair_rows[0].mfe_pnl_usd == 0.2),
        ("decision_engine_uses_observed_prices", observed_price_decision.get("ok") and observed_price_decision.get("side") == "UP" and abs((observed_price_decision.get("entry_price") or 0.0) - 0.45) < 1e-9),
        ("decision_engine_returns_model_probability", observed_price_decision.get("ok") and (observed_price_decision.get("model_probability") or 0.0) > (observed_price_decision.get("entry_price") or 1.0)),
        ("decision_engine_prefers_better_priced_side_edge", dual_signal_decision.get("ok") and dual_signal_decision.get("side") == "DOWN" and (dual_signal_decision.get("model_edge") or 0.0) > 0.0),
        ("decision_engine_exposes_ranked_candidates", isinstance(dual_signal_decision.get("ranked_candidates"), list) and len(dual_signal_decision.get("ranked_candidates") or []) >= 1),
        ("paper_settlement_win", paper_settlement_from_last_mark(0.72) == (1.0, "binary-win")),
        ("paper_settlement_loss", paper_settlement_from_last_mark(0.28) == (0.0, "binary-lose")),
        ("paper_settlement_neutral", paper_settlement_from_last_mark(0.50) == (0.5, "binary-neutral")),
        ("price_aware_kelly_fraction", abs(price_aware_kelly_fraction(0.60, 0.45) - (((0.60 - 0.45) / (1.0 - 0.45)) / 4.0)) < 1e-9),
        ("apply_scoreboard_aux_probability_blends_model_and_aux_wr", abs(apply_scoreboard_aux_probability(0.62, 0.20) - 0.578) < 1e-9),
        ("required_trade_edge_respects_fee_floor_even_for_fresh_strategy", abs(required_trade_edge(0.45, 250, history_count=0) - 0.03744) < 1e-9),
        ("required_trade_edge_penalizes_late_rich_price_under_wide_window", abs(required_trade_edge(0.70, 150, history_count=30) - 0.065) < 1e-9),
        ("required_trade_edge_skips_late_penalty_at_150_under_natural_window", abs(natural_window_edge - 0.05) < 1e-9),
        ("required_trade_edge_penalizes_center_prices", abs(required_trade_edge(0.50, 250, history_count=30) - 0.06) < 1e-9),
        ("required_trade_edge_penalizes_center_prices_under_natural_window", abs(natural_window_center_edge - 0.06) < 1e-9),
        ("summarize_entry_edge_blocks_weak_late_trade", summarize_entry_edge(win_rate=0.56, entry_price=0.55, secs_left=140, history_count=30)["ok"] is False),
        ("summarize_entry_edge_allows_fresh_discounted_trade", summarize_entry_edge(win_rate=0.50, entry_price=0.45, secs_left=250, history_count=0)["ok"] is True),
        ("summarize_entry_edge_blocks_fresh_neutral_band_trade", summarize_entry_edge(win_rate=0.50, entry_price=0.48, secs_left=250, history_count=0)["ok"] is False),
        ("stabilize_entry_win_rate_softens_sparse_history", abs(stabilize_entry_win_rate(0.18, 1) - 0.436) < 1e-9),
        ("candidate_fallback_selects_second_ranked_signal", candidate_pick is not None and candidate_pick.get("side") == "UP" and len(candidate_rejections) == 1),
        ("candidate_side_conflict_now_skips_coinflip_direction", side_conflict_candidate is None and any("rejected=side-conflict" in note for note in side_conflict_rejections)),
        ("candidate_low_aux_wr_hard_block_now_applies_in_ranked_path", low_aux_wr_candidate is None and any("rejected=low-auxWR-hard-block" in note for note in low_aux_wr_rejections)),
        ("sparse_history_candidate_still_penalized_below_neutral", abs(float(sparse_candidate.get("strategy_win_rate") or 0.0) - 0.436) < 1e-9),
        ("entry_velocity_gate_blocks_when_current_velocity_reverses", entry_velocity_gate_rejects("UP", "model-ws_order_flow_up", 0.0003, current_ws_velocity=-0.0002, require_dual_confirmation=True) is True),
        ("entry_velocity_gate_allows_when_lag_and_current_align", entry_velocity_gate_rejects("UP", "model-ws_order_flow_up", 0.0003, current_ws_velocity=0.0002, require_dual_confirmation=True) is False),
        ("entry_response_actionable_on_fill", entry_response_has_actionable_state({"response": {"takingAmount": "1.25"}}) is True),
        ("entry_response_actionable_on_order_id", entry_response_has_actionable_state({"response": {"orderID": "abc123"}}) is True),
        ("entry_response_not_actionable_when_empty", entry_response_has_actionable_state({"response": {}}) is False),
        ("runner_normalizes_timeout_fallback_as_taker", normalize_runner_execution_style("maker-timeout-fallback") == "taker"),
        ("report_normalizes_timeout_fallback_as_taker", normalize_report_execution_style("maker-timeout-fallback") == "taker"),
        ("report_normalizes_taker_simulated_as_taker", normalize_report_execution_style("taker-simulated") == "taker"),
        ("market_resolver_maps_tokens_by_outcome_label", reversed_token_pair == ("tok_up", "tok_down")),
        ("entry_book_gate_passes_normal_book", book_gate_ok["ok"] is True and abs(float(book_gate_ok["spread"] or 0.0) - 0.02) < 1e-9),
        ("entry_book_gate_blocks_wide_spread", book_gate_wide["ok"] is False and book_gate_wide["reason"] == "spread-too-wide"),
        ("entry_book_gate_blocks_thin_depth", book_gate_thin["ok"] is False and book_gate_thin["reason"] == "best-ask-too-thin"),
        (
            "pending_order_timeout_prefers_taker_fallback",
            decide_pending_order_action(
                order_still_open=True,
                age_sec=20.0,
                side="UP",
                ws_vel=0.0,
                cancel_velocity=0.003,
                timeout_sec=15.0,
                has_live_position=False,
                fallback_enabled=True,
                fallback_attempted=False,
            ) == "fallback-taker",
        ),
        (
            "pending_order_reversal_cancels",
            decide_pending_order_action(
                order_still_open=True,
                age_sec=5.0,
                side="DOWN",
                ws_vel=0.01,
                cancel_velocity=0.003,
                timeout_sec=15.0,
                has_live_position=False,
                fallback_enabled=True,
                fallback_attempted=False,
            ) == "cancel-reversal",
        ),
        (
            "pending_order_gone_without_live_position",
            decide_pending_order_action(
                order_still_open=False,
                age_sec=5.0,
                side="UP",
                ws_vel=0.0,
                cancel_velocity=0.003,
                timeout_sec=15.0,
                has_live_position=False,
                fallback_enabled=True,
                fallback_attempted=False,
            ) == "gone",
        ),
        ("observe_api_latency_detects_slow_call", slow_detected is True and abs(health_flags.last_api_latency_ms - 1600.0) < 1e-9),
        ("network_fail_safe_activates_on_slow_api_streak", activated_after_slow is True and any("ACTIVATED" in note for note in slow_notes_3)),
        ("network_fail_safe_clears_after_recovery", cleared_after_recovery is False and any("CLEARED" in note for note in clear_notes_2)),
        ("network_fail_safe_activates_on_ws_stale_streak", stale_flags.network_fail_safe_mode is True and any("ACTIVATED" in note for note in stale_notes_2) and any("ws stale detected" in note for note in stale_notes_1)),
        ("scoreboard_zero_pnl_is_neutral", abs(scratch_score - 0.5) < 1e-9 and scratch_total == 1 and scratch_decisive == 0),
        ("load_trade_events_filters_to_run_and_keeps_matching_entry", [ev["event_id"] for ev in filtered_run_events] == ["entry_pre", "exit_run_x"]),
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
        (
            "summary_tracks_fee_adjusted_and_buckets",
            fee_summary["close_bucket_counts"] == {"active-close": 1, "expiry-binary-win": 1}
            and abs((fee_summary["fee_adjusted_actual_pnl"]["sum"] or 0.0) - (0.16568 + 0.7844)) < 1e-6
            and abs((fee_summary["close_bucket_pnl"]["active-close"]["fee_adjusted_actual_pnl"]["sum"] or 0.0) - 0.16568) < 1e-6
            and abs((fee_summary["close_bucket_pnl"]["expiry-binary-win"]["fee_adjusted_actual_pnl"]["sum"] or 0.0) - 0.7844) < 1e-6
        ),
        (
            "summary_tracks_scratch_trades",
            scratch_summary["scratch_trades"]["count"] == 1
            and abs((scratch_summary["scratch_trades"]["ratio"] or 0.0) - 1.0) < 1e-9
            and scratch_summary["scratch_trades"]["close_reason_counts"] == {"stalled-trade": 1}
        ),
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
