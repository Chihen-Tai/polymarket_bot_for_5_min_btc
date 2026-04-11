import os
from dataclasses import dataclass
import warnings

try:
    from dotenv import load_dotenv
except Exception:

    def load_dotenv(*args, **kwargs):
        return False


from pathlib import Path


def load_repo_env(repo_root: Path) -> None:
    load_dotenv(repo_root / ".env")
    # Local overrides are loaded after the tracked .env so pulls do not wipe secrets.
    for name in (".env.local", ".env.secrets"):
        path = repo_root / name
        if path.exists():
            load_dotenv(path, override=True)


load_repo_env(Path(__file__).resolve().parent.parent)


def _f(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _i(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _b(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    dry_run: bool = _b("DRY_RUN", True)
    data_dir: str = str(Path(__file__).resolve().parent.parent / "data")

    min_equity: float = _f("MIN_EQUITY", 1.0)
    max_order_usd: float = _f("MAX_ORDER_USD", 1.0)
    min_live_order_shares: float = _f("MIN_LIVE_ORDER_SHARES", 5.0)
    min_live_order_usd: float = _f("MIN_LIVE_ORDER_USD", 1.0)
    live_order_hard_cap_usd: float = _f("LIVE_ORDER_HARD_CAP_USD", 3.0)
    live_entry_use_market_orders: bool = _b("LIVE_ENTRY_USE_MARKET_ORDERS", True)
    max_exposure_usd: float = _f("MAX_EXPOSURE_USD", 1.0)
    max_orders_per_5min: int = _i("MAX_ORDERS_PER_5MIN", 3)
    max_consec_loss: int = _i("MAX_CONSEC_LOSS", 3)
    clean_start_loss_streak_reset_sec: float = _f(
        "CLEAN_START_LOSS_STREAK_RESET_SEC", 14400.0
    )
    daily_max_loss: float = _f("DAILY_MAX_LOSS", 3.0)
    manual_reset_daily_max_loss_on_start: bool = _b(
        "MANUAL_RESET_DAILY_MAX_LOSS_ON_START", False
    )
    conservative_mode_enabled: bool = _b("CONSERVATIVE_MODE_ENABLED", False)
    profitability_conservative_mode_enabled: bool = _b(
        "PROFITABILITY_CONSERVATIVE_MODE_ENABLED", True
    )
    recent_active_close_summary: dict | None = None
    conservative_extra_edge: float = _f("CONSERVATIVE_EXTRA_EDGE", 0.015)
    conservative_max_open_positions: int = _i("CONSERVATIVE_MAX_OPEN_POSITIONS", 1)
    conservative_max_orders_per_5min: int = _i("CONSERVATIVE_MAX_ORDERS_PER_5MIN", 2)
    conservative_sync_miss_limit: int = _i("CONSERVATIVE_SYNC_MISS_LIMIT", 1)
    conservative_block_pending_orders: bool = _b(
        "CONSERVATIVE_BLOCK_PENDING_ORDERS", True
    )
    conservative_block_pending_confirmation: bool = _b(
        "CONSERVATIVE_BLOCK_PENDING_CONFIRMATION", True
    )
    conservative_block_live_sync_protect: bool = _b(
        "CONSERVATIVE_BLOCK_LIVE_SYNC_PROTECT", True
    )
    ofi_bypass_threshold: float = _f("OFI_BYPASS_THRESHOLD", 0.65)

    poll_seconds: int = _i("POLL_SECONDS", 15)
    live_account_cache_ttl_sec: float = _f("LIVE_ACCOUNT_CACHE_TTL_SEC", 10.0)
    pending_order_poll_seconds: float = _f("PENDING_ORDER_POLL_SECONDS", 1.0)
    open_position_poll_seconds: float = _f("OPEN_POSITION_POLL_SECONDS", 1.0)
    near_stop_poll_seconds: float = _f("NEAR_STOP_POLL_SECONDS", 0.5)
    near_stop_poll_hold_sec: float = _f("NEAR_STOP_POLL_HOLD_SEC", 15.0)
    position_watch_debug_enabled: bool = _b("POSITION_WATCH_DEBUG_ENABLED", True)
    position_watch_log_interval_sec: float = _f("POSITION_WATCH_LOG_INTERVAL_SEC", 5.0)

    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # CLOB runtime settings
    clob_host: str = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
    data_api_host: str = os.getenv("DATA_API_HOST", "https://data-api.polymarket.com")
    chain_id: int = _i("CHAIN_ID", 137)
    signature_type: int = _i("SIGNATURE_TYPE", 1)

    # Trading identity
    private_key: str = os.getenv("PRIVATE_KEY", "")
    funder_address: str = os.getenv("FUNDER_ADDRESS", "")

    # Optional fixed CLOB API creds
    clob_api_key: str = os.getenv("CLOB_API_KEY", "")
    clob_api_secret: str = os.getenv("CLOB_API_SECRET", "")
    clob_api_passphrase: str = os.getenv("CLOB_API_PASSPHRASE", "")

    # Target market token ids (optional static)
    token_id_up: str = os.getenv("TOKEN_ID_UP", "")
    token_id_down: str = os.getenv("TOKEN_ID_DOWN", "")

    # Auto market selection
    auto_market_selection: bool = _b("AUTO_MARKET_SELECTION", True)
    market_slug_prefix: str = os.getenv("MARKET_SLUG_PREFIX", "btc-updown-5m-")

    # Integrated decision rules (from prior paper simulations)
    edge_threshold: float = _f("EDGE_THRESHOLD", 0.02)
    fee_buffer: float = _f("FEE_BUFFER", 0.01)
    report_assumed_taker_fee_rate: float = _f("REPORT_ASSUMED_TAKER_FEE_RATE", 0.0156)
    scoreboard_neutral_pnl_pct: float = _f("SCOREBOARD_NEUTRAL_PNL_PCT", 0.001)
    scoreboard_entry_gate_min_decisive_trades: int = _i(
        "SCOREBOARD_ENTRY_GATE_MIN_DECISIVE_TRADES", 5
    )
    scoreboard_aux_weight: float = _f("SCOREBOARD_AUX_WEIGHT", 0.10)
    scoreboard_min_win_rate: float = _f(
        "SCOREBOARD_MIN_WIN_RATE", 0.40
    )  # hard-block gate: block entry if auxWR < this and enough history
    scoreboard_decay_factor: float = _f("SCOREBOARD_DECAY_FACTOR", 0.95)
    entry_fee_floor_buffer: float = _f(
        "ENTRY_FEE_FLOOR_BUFFER", 1.0
    )  # multiplier on 2x taker fee floor for required_edge
    entry_neutral_hard_block_half_width: float = _f(
        "ENTRY_NEUTRAL_HARD_BLOCK_HALF_WIDTH", 0.02
    )
    entry_execution_cost_buffer: float = _f("ENTRY_EXECUTION_COST_BUFFER", 0.015)
    entry_require_maker_edge_buffer: float = _f("ENTRY_REQUIRE_MAKER_EDGE_BUFFER", 0.01)
    conservative_active_close_loss_streak: int = _i(
        "CONSERVATIVE_ACTIVE_CLOSE_LOSS_STREAK", 3
    )
    conservative_active_close_fee_pnl_floor: float = _f(
        "CONSERVATIVE_ACTIVE_CLOSE_FEE_PNL_FLOOR", -0.05
    )
    conservative_skip_windows: int = _i("CONSERVATIVE_SKIP_WINDOWS", 2)
    zscore_window: int = _i("ZSCORE_WINDOW", 20)
    zscore_threshold: float = _f("ZSCORE_THRESHOLD", 2.0)
    entry_window_min_sec: float = _f("ENTRY_WINDOW_MIN_SEC", 120.0)
    entry_window_max_sec: float = _f("ENTRY_WINDOW_MAX_SEC", 999999.0)
    min_entry_price: float = _f("MIN_ENTRY_PRICE", 0.35)
    snipe_min_entry_price: float = _f("SNIPE_MIN_ENTRY_PRICE", 0.05)
    snipe_max_entry_price: float = _f("SNIPE_MAX_ENTRY_PRICE", 0.96)
    max_entry_price: float = _f("MAX_ENTRY_PRICE", 0.75)
    entry_slippage_guard_enabled: bool = _b("ENTRY_SLIPPAGE_GUARD_ENABLED", True)
    entry_max_actual_slippage_pct: float = _f("ENTRY_MAX_ACTUAL_SLIPPAGE_PCT", 0.18)
    entry_neutral_band_half_width: float = _f("ENTRY_NEUTRAL_BAND_HALF_WIDTH", 0.03)
    entry_neutral_edge_penalty: float = _f("ENTRY_NEUTRAL_EDGE_PENALTY", 0.02)
    entry_micro_band_half_width: float = _f("ENTRY_MICRO_BAND_HALF_WIDTH", 0.01)
    entry_micro_edge_penalty: float = _f("ENTRY_MICRO_EDGE_PENALTY", 0.02)
    entry_side_conflict_enabled: bool = _b("ENTRY_SIDE_CONFLICT_ENABLED", True)
    entry_side_conflict_min_edge_gap: float = _f(
        "ENTRY_SIDE_CONFLICT_MIN_EDGE_GAP", 0.025
    )
    entry_side_conflict_min_prob_gap: float = _f(
        "ENTRY_SIDE_CONFLICT_MIN_PROB_GAP", 0.03
    )
    entry_max_spread: float = _f("ENTRY_MAX_SPREAD", 0.03)
    entry_min_best_ask_multiple: float = _f("ENTRY_MIN_BEST_ASK_MULTIPLE", 2.0)
    entry_min_total_ask_multiple: float = _f("ENTRY_MIN_TOTAL_ASK_MULTIPLE", 6.0)
    report_scratch_pnl_pct: float = _f("REPORT_SCRATCH_PNL_PCT", 0.03)

    # Cadence guard: avoid long no-trade stretches
    max_idle_minutes: int = _i("MAX_IDLE_MINUTES", 120)
    live_position_grace_sec: int = _i("LIVE_POSITION_GRACE_SEC", 90)
    live_position_miss_limit: int = _i("LIVE_POSITION_MISS_LIMIT", 3)

    # Dump+hedge integration
    enable_dump_trigger: bool = _b("ENABLE_DUMP_TRIGGER", False)
    dump_move_threshold: float = _f("DUMP_MOVE_THRESHOLD", 0.25)
    hedge_sum_target: float = _f("HEDGE_SUM_TARGET", 0.95)
    hedge_ratio: float = _f("HEDGE_RATIO", 0.0)
    ws_flash_snipe_threshold: float = _f("WS_FLASH_SNIPE_THRESHOLD", 0.003)
    liquidation_fade_min_usd: float = _f("LIQUIDATION_FADE_MIN_USD", 500000.0)
    liquidation_fade_window_sec: float = _f("LIQUIDATION_FADE_WINDOW_SEC", 20.0)
    early_underdog_max_price: float = _f("EARLY_UNDERDOG_MAX_PRICE", 0.35)
    early_underdog_min_time: float = _f("EARLY_UNDERDOG_MIN_TIME", 220.0)
    early_underdog_exit_lock_time: float = _f("EARLY_UNDERDOG_EXIT_LOCK_TIME", 150.0)
    early_underdog_let_ride_loss_pct: float = _f(
        "EARLY_UNDERDOG_LET_RIDE_LOSS_PCT", 0.35
    )
    early_underdog_take_profit_pct: float = _f("EARLY_UNDERDOG_TAKE_PROFIT_PCT", 1.50)
    # 樂透爆發模式：在此時間窗口內出現此漲幅才觸發樂透鎖定，否則視為一般單
    lottery_burst_window_sec: float = _f("LOTTERY_BURST_WINDOW_SEC", 60.0)
    lottery_burst_min_pct: float = _f("LOTTERY_BURST_MIN_PCT", 0.50)
    # 樂透高原停利：在 +plateau_min_pct ~ +hard_pct 區間，若停止創高超過 stall_sec 且 Binance 速度弱，停利
    lottery_plateau_min_pct: float = _f("LOTTERY_PLATEAU_MIN_PCT", 0.75)
    lottery_plateau_stall_sec: float = _f("LOTTERY_PLATEAU_STALL_SEC", 15.0)
    lottery_plateau_velocity_threshold: float = _f(
        "LOTTERY_PLATEAU_VELOCITY_THRESHOLD", 0.0003
    )
    hedge_max_wait_sec: int = _i("HEDGE_MAX_WAIT_SEC", 90)
    stop_loss_min_hold_sec: float = _f("STOP_LOSS_MIN_HOLD_SEC", 30.0)
    stop_loss_pct: float = _f("STOP_LOSS_PCT", 0.18)
    smart_stop_loss_enabled: bool = _b("SMART_STOP_LOSS_ENABLED", True)
    stop_loss_partial_pct: float = _f("STOP_LOSS_PARTIAL_PCT", 0.08)
    stop_loss_partial_fraction: float = _f("STOP_LOSS_PARTIAL_FRACTION", 0.50)
    live_stop_loss_partial_fraction: float = _f("LIVE_STOP_LOSS_PARTIAL_FRACTION", 0.80)
    max_hold_seconds: int = _i("MAX_HOLD_SECONDS", 180)
    take_profit_scaleout_pct: float = _f("TAKE_PROFIT_SCALEOUT_PCT", 0.03)
    take_profit_soft_pct: float = _f(
        "TAKE_PROFIT_SOFT_PCT", 0.18
    )  # Stage 1: start locking profit at +18%
    # bid 可成交報酬因流動性打折時，mark 報酬需超過 soft_pct + 此緩衝才觸發 fallback 停利
    take_profit_bid_discount_buffer: float = _f("TAKE_PROFIT_BID_DISCOUNT_BUFFER", 0.08)
    take_profit_partial_fraction: float = _f("TAKE_PROFIT_PARTIAL_FRACTION", 0.40)
    take_profit_hard_pct: float = _f(
        "TAKE_PROFIT_HARD_PCT", 0.26
    )  # Stage 2: extract principal at +26%
    take_profit_principal_after_partial_enabled: bool = _b(
        "TAKE_PROFIT_PRINCIPAL_AFTER_PARTIAL_ENABLED", True
    )
    take_profit_principal_after_partial_min_mfe_pct: float = _f(
        "TAKE_PROFIT_PRINCIPAL_AFTER_PARTIAL_MIN_MFE_PCT", 0.24
    )
    take_profit_principal_after_partial_drawdown_pct: float = _f(
        "TAKE_PROFIT_PRINCIPAL_AFTER_PARTIAL_DRAWDOWN_PCT", 0.08
    )
    take_profit_principal_after_partial_min_current_pct: float = _f(
        "TAKE_PROFIT_PRINCIPAL_AFTER_PARTIAL_MIN_CURRENT_PCT", 0.14
    )
    take_profit_runner_fraction: float = _f("TAKE_PROFIT_RUNNER_FRACTION", 0.10)
    moonbag_drawdown_pct: float = _f("MOONBAG_DRAWDOWN_PCT", 0.35)
    moonbag_drawdown_window_sec: int = _i("MOONBAG_DRAWDOWN_WINDOW_SEC", 45)
    moonbag_min_peak_value_usd: float = _f("MOONBAG_MIN_PEAK_VALUE_USD", 0.10)
    momentum_ticks: int = _i("MOMENTUM_TICKS", 3)
    momentum_min_move: float = _f("MOMENTUM_MIN_MOVE", 0.005)
    exit_deadline_sec: float = _f("EXIT_DEADLINE_SEC", 20.0)
    exit_deadline_profit_sec: float = _f("EXIT_DEADLINE_PROFIT_SEC", 45.0)
    exit_ghost_town_sec: float = _f("EXIT_GHOST_TOWN_SEC", 30.0)
    exit_deadline_flat_pnl_pct: float = _f("EXIT_DEADLINE_FLAT_PNL_PCT", 0.0)
    loss_exit_retry_delay_sec: float = _f("LOSS_EXIT_RETRY_DELAY_SEC", 0.25)
    loss_exit_max_attempts: int = _i("LOSS_EXIT_MAX_ATTEMPTS", 4)
    emergency_exit_retry_delay_sec: float = _f("EMERGENCY_EXIT_RETRY_DELAY_SEC", 1.0)
    emergency_exit_max_attempts: int = _i("EMERGENCY_EXIT_MAX_ATTEMPTS", 8)
    stop_loss_scaleout_emergency_fill_ratio: float = _f(
        "STOP_LOSS_SCALEOUT_EMERGENCY_FILL_RATIO", 0.60
    )
    stop_loss_scaleout_emergency_remaining_cost_pct: float = _f(
        "STOP_LOSS_SCALEOUT_EMERGENCY_REMAINING_COST_PCT", 0.45
    )
    stop_loss_warn_pct: float = _f("STOP_LOSS_WARN_PCT", 0.08)

    hedge_exit_enabled: bool = _b("HEDGE_EXIT_ENABLED", True)
    hedge_exit_advantage_threshold: float = _f("HEDGE_EXIT_ADVANTAGE_THRESHOLD", 0.005)

    # Strategy 1: Binance Oracle Front-running
    use_cex_oracle: bool = _b("USE_CEX_ORACLE", True)
    cex_frontrun_threshold: float = _f("CEX_FRONTRUN_THRESHOLD", 60.0)

    # Strategy 2: Arbitrage (disabled to enforce strict single-order 1 USD rule)
    enable_arbitrage: bool = _b("ENABLE_ARBITRAGE", False)
    arbitrage_max_cost: float = _f("ARBITRAGE_MAX_COST", 0.98)

    # Strategy 3: Dynamic Volatility
    use_dynamic_thresholds: bool = _b("USE_DYNAMIC_THRESHOLDS", True)

    # Advanced Risk
    use_kelly_sizing: bool = _b("USE_KELLY_SIZING", True)
    max_bet_cap_usd: float = _f("MAX_BET_CAP_USD", 50.0)

    # Market Maker Settings
    mm_spread: float = _f("MM_SPREAD", 0.05)
    mm_order_size: float = _f("MM_ORDER_SIZE", 1.0)
    mm_safety_halt: float = _f("MM_SAFETY_HALT", 30.0)

    # Maker/Limit Latency Execution Settings
    maker_order_timeout_sec: int = _i("MAKER_ORDER_TIMEOUT_SEC", 15)
    maker_timeout_fallback_taker: bool = _b("MAKER_TIMEOUT_FALLBACK_TAKER", True)
    cancel_on_reversal_velocity: float = _f("CANCEL_ON_REVERSAL_VELOCITY", 0.003)
    entry_retry_attempts: int = _i("ENTRY_RETRY_ATTEMPTS", 3)
    entry_retry_backoff_sec: float = _f("ENTRY_RETRY_BACKOFF_SEC", 2.0)

    # Phase 2: Advanced Loophole Exploitation
    taker_snipe_velocity: float = _f("TAKER_SNIPE_VELOCITY", 0.0008)
    panic_dump_velocity: float = _f("PANIC_DUMP_VELOCITY", 0.0010)
    tp_hold_velocity: float = _f("TP_HOLD_VELOCITY", 0.00045)

    # Phase 3: Entry & Exit Quality Guards
    # Hard-stop shield is explicit opt-in only. Historical runs showed it could delay
    # stop-loss execution too much in fast 5m resolution markets.
    enable_hard_stop_shield: bool = _b("ENABLE_HARD_STOP_SHIELD", False)
    hard_stop_shield_velocity: float = _f("HARD_STOP_SHIELD_VELOCITY", 0.0)
    late_entry_edge_penalty: float = _f("LATE_ENTRY_EDGE_PENALTY", 0.015)
    rich_price_edge_penalty: float = _f("RICH_PRICE_EDGE_PENALTY", 0.015)
    binary_kelly_divisor: float = _f("BINARY_KELLY_DIVISOR", 4.0)
    force_full_exit_on_take_profit: bool = _b("FORCE_FULL_EXIT_ON_TAKE_PROFIT", False)
    live_take_profit_force_taker: bool = _b("LIVE_TAKE_PROFIT_FORCE_TAKER", False)
    live_take_profit_fee_rate: float = _f("LIVE_TAKE_PROFIT_FEE_RATE", 0.0)
    leave_moonbag_pct: float = _f("LEAVE_MOONBAG_PCT", 0.05)
    leave_loss_tail_pct: float = _f("LEAVE_LOSS_TAIL_PCT", 0.0)
    force_full_exit_on_stop_loss_scaleout: bool = _b(
        "FORCE_FULL_EXIT_ON_STOP_LOSS_SCALEOUT", False
    )
    live_force_full_loss_exit: bool = _b("LIVE_FORCE_FULL_LOSS_EXIT", True)
    live_loss_exit_force_taker: bool = _b("LIVE_LOSS_EXIT_FORCE_TAKER", False)
    breakeven_giveback_enabled: bool = _b("BREAKEVEN_GIVEBACK_ENABLED", True)
    breakeven_giveback_min_mfe_pct: float = _f("BREAKEVEN_GIVEBACK_MIN_MFE_PCT", 0.10)
    breakeven_giveback_floor_pct: float = _f("BREAKEVEN_GIVEBACK_FLOOR_PCT", 0.03)
    breakeven_giveback_min_hold_sec: float = _f("BREAKEVEN_GIVEBACK_MIN_HOLD_SEC", 12.0)
    breakeven_giveback_min_secs_left: float = _f(
        "BREAKEVEN_GIVEBACK_MIN_SECS_LEFT", 45.0
    )
    profit_reversal_enabled: bool = _b("PROFIT_REVERSAL_ENABLED", True)
    profit_reversal_min_mfe_pct: float = _f("PROFIT_REVERSAL_MIN_MFE_PCT", 0.50)
    profit_reversal_min_current_profit_pct: float = _f(
        "PROFIT_REVERSAL_MIN_CURRENT_PROFIT_PCT", 0.12
    )
    profit_reversal_drawdown_pct: float = _f("PROFIT_REVERSAL_DRAWDOWN_PCT", 0.18)
    profit_reversal_adverse_velocity: float = _f(
        "PROFIT_REVERSAL_ADVERSE_VELOCITY", 0.0003
    )
    binance_adverse_exit_enabled: bool = _b("BINANCE_ADVERSE_EXIT_ENABLED", True)
    binance_adverse_exit_confirm_sec: float = _f(
        "BINANCE_ADVERSE_EXIT_CONFIRM_SEC", 3.0
    )
    binance_adverse_exit_velocity: float = _f("BINANCE_ADVERSE_EXIT_VELOCITY", 0.00035)
    binance_adverse_exit_max_profit_pct: float = _f(
        "BINANCE_ADVERSE_EXIT_MAX_PROFIT_PCT", 0.08
    )
    binance_adverse_exit_min_hold_sec: float = _f(
        "BINANCE_ADVERSE_EXIT_MIN_HOLD_SEC", 4.0
    )
    binance_adverse_exit_require_current_confirm: bool = _b(
        "BINANCE_ADVERSE_EXIT_REQUIRE_CURRENT_CONFIRM", True
    )
    binance_profit_protect_enabled: bool = _b("BINANCE_PROFIT_PROTECT_ENABLED", True)
    binance_profit_protect_min_profit_pct: float = _f(
        "BINANCE_PROFIT_PROTECT_MIN_PROFIT_PCT", 0.08
    )
    binance_profit_protect_max_profit_pct: float = _f(
        "BINANCE_PROFIT_PROTECT_MAX_PROFIT_PCT", 0.17
    )
    binance_profit_protect_stall_sec: float = _f(
        "BINANCE_PROFIT_PROTECT_STALL_SEC", 30.0
    )
    binance_profit_protect_confirm_sec: float = _f(
        "BINANCE_PROFIT_PROTECT_CONFIRM_SEC", 1.0
    )
    binance_profit_protect_velocity: float = _f(
        "BINANCE_PROFIT_PROTECT_VELOCITY", 0.00012
    )
    binance_profit_protect_min_hold_sec: float = _f(
        "BINANCE_PROFIT_PROTECT_MIN_HOLD_SEC", 10.0
    )
    binance_profit_protect_require_current_confirm: bool = _b(
        "BINANCE_PROFIT_PROTECT_REQUIRE_CURRENT_CONFIRM", False
    )
    soft_stop_confirm_sec: float = _f("SOFT_STOP_CONFIRM_SEC", 2.5)
    soft_stop_confirm_buffer_pct: float = _f("SOFT_STOP_CONFIRM_BUFFER_PCT", 0.015)
    soft_stop_adverse_velocity: float = _f("SOFT_STOP_ADVERSE_VELOCITY", 0.00018)
    failed_follow_through_window_sec: int = _i("FAILED_FOLLOW_THROUGH_WINDOW_SEC", 25)
    failed_follow_through_loss_pct: float = _f("FAILED_FOLLOW_THROUGH_LOSS_PCT", 0.02)
    failed_follow_through_max_mfe_pct: float = _f(
        "FAILED_FOLLOW_THROUGH_MAX_MFE_PCT", 0.015
    )
    failed_follow_through_min_secs_left: int = _i(
        "FAILED_FOLLOW_THROUGH_MIN_SECS_LEFT", 120
    )
    stalled_exit_window_sec: int = _i("STALLED_EXIT_WINDOW_SEC", 35)
    stalled_exit_min_loss_pct: float = _f("STALLED_EXIT_MIN_LOSS_PCT", 0.01)
    stalled_exit_max_abs_pnl_pct: float = _f("STALLED_EXIT_MAX_ABS_PNL_PCT", 0.02)
    stalled_exit_max_mfe_pct: float = _f("STALLED_EXIT_MAX_MFE_PCT", 0.02)
    stalled_exit_min_secs_left: int = _i("STALLED_EXIT_MIN_SECS_LEFT", 45)
    post_scaleout_loss_exit_delay_sec: int = _i("POST_SCALEOUT_LOSS_EXIT_DELAY_SEC", 15)
    post_scaleout_loss_exit_pct: float = _f("POST_SCALEOUT_LOSS_EXIT_PCT", 0.16)
    same_market_reentry_min_secs_left: int = _i("SAME_MARKET_REENTRY_MIN_SECS_LEFT", 60)
    binance_signal_lag_sec: float = _f("BINANCE_SIGNAL_LAG_SEC", 0.5)
    entry_dual_velocity_confirm: bool = _b("ENTRY_DUAL_VELOCITY_CONFIRM", True)
    ws_stale_max_age_sec: float = _f("WS_STALE_MAX_AGE_SEC", 5.0)

    # Advanced Options Strategies (Theta Bleed & Strike Cross Front-run)
    theta_bleed_enabled: bool = _b("THETA_BLEED_ENABLED", True)
    theta_bleed_min_sec: float = _f("THETA_BLEED_MIN_SEC", 60.0)
    theta_bleed_distance: float = _f("THETA_BLEED_DISTANCE", 120.0)
    strike_cross_snipe_enabled: bool = _b("STRIKE_CROSS_SNIPE_ENABLED", True)
    strike_cross_gap: float = _f("STRIKE_CROSS_GAP", 20.0)

    ws_stale_fail_safe_streak: int = _i("WS_STALE_FAIL_SAFE_STREAK", 2)
    api_slow_threshold_ms: float = _f("API_SLOW_THRESHOLD_MS", 1500.0)
    api_fail_safe_streak: int = _i("API_FAIL_SAFE_STREAK", 3)
    network_recovery_streak: int = _i("NETWORK_RECOVERY_STREAK", 2)
    # entry_velocity_min: block entry if Binance velocity is strongly opposing signal.
    # Only blocks adverse moves; flat/zero velocity still allows entry. Set 0.0 to disable.
    entry_velocity_min: float = _f("ENTRY_VELOCITY_MIN", 0.0002)
    # 同方向進場冷卻：已有同方向倉位時，N 秒內不重複進場。設 0 停用。
    same_direction_entry_cooldown_sec: float = _f(
        "SAME_DIRECTION_ENTRY_COOLDOWN_SEC", 60.0
    )

    def __post_init__(self) -> None:
        if self.take_profit_hard_pct <= self.take_profit_soft_pct:
            normalized_soft = min(
                self.take_profit_soft_pct,
                max(0.05, self.take_profit_hard_pct - 0.20),
            )
            if normalized_soft >= self.take_profit_hard_pct:
                normalized_soft = max(0.05, self.take_profit_hard_pct * 0.6)
            warnings.warn(
                "TAKE_PROFIT_HARD_PCT must be greater than TAKE_PROFIT_SOFT_PCT; "
                f"normalizing soft threshold from {self.take_profit_soft_pct:.2f} "
                f"to {normalized_soft:.2f} while keeping hard threshold at "
                f"{self.take_profit_hard_pct:.2f}.",
                RuntimeWarning,
                stacklevel=2,
            )
            self.take_profit_soft_pct = normalized_soft
        normalized_partial_fraction = min(
            0.95, max(0.05, float(self.take_profit_partial_fraction or 0.30))
        )
        if (
            abs(
                normalized_partial_fraction
                - float(self.take_profit_partial_fraction or 0.30)
            )
            > 1e-9
        ):
            warnings.warn(
                "TAKE_PROFIT_PARTIAL_FRACTION must be between 0.05 and 0.95; "
                f"normalizing from {self.take_profit_partial_fraction:.2f} "
                f"to {normalized_partial_fraction:.2f}.",
                RuntimeWarning,
                stacklevel=2,
            )
            self.take_profit_partial_fraction = normalized_partial_fraction
        normalized_runner_fraction = min(
            0.95, max(0.0, float(self.take_profit_runner_fraction or 0.10))
        )
        if (
            abs(
                normalized_runner_fraction
                - float(self.take_profit_runner_fraction or 0.10)
            )
            > 1e-9
        ):
            warnings.warn(
                "TAKE_PROFIT_RUNNER_FRACTION must be between 0.00 and 0.95; "
                f"normalizing from {self.take_profit_runner_fraction:.2f} "
                f"to {normalized_runner_fraction:.2f}.",
                RuntimeWarning,
                stacklevel=2,
            )
            self.take_profit_runner_fraction = normalized_runner_fraction


SETTINGS = Settings()
