from __future__ import annotations
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

def load_repo_env(repo_root: Path) -> None:
    load_dotenv(repo_root / ".env")
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
    # --- System ---
    dry_run: bool = _b("DRY_RUN", True)
    data_dir: str = str(Path(__file__).resolve().parent.parent / "data")
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # --- Profile Enforcement ---
    market_profile: str = "btc_15m_vpn_safe"
    market_duration_sec: float = 900.0
    market_slug_prefix: str = "btc-updown-15m-"

    # --- Capital & Risk Management ---
    min_equity: float = _f("MIN_EQUITY", 1.0)
    max_order_usd: float = _f("MAX_ORDER_USD", 1.0)
    min_live_order_shares: float = _f("MIN_LIVE_ORDER_SHARES", 5.0)
    min_live_order_usd: float = _f("MIN_LIVE_ORDER_USD", 1.0)
    max_exposure_usd: float = _f("MAX_EXPOSURE_USD", 1.0)
    daily_max_loss: float = _f("DAILY_MAX_LOSS", 3.0)
    
    # --- Extreme Value Gating (15M Sniper) ---
    enable_sniper_mode: bool = True
    sniper_extreme_upper: float = _f("SNIPER_EXTREME_UPPER", 0.95)  # Was 0.75 — dead zone removed
    sniper_extreme_lower: float = _f("SNIPER_EXTREME_LOWER", 0.05)  # Was 0.25 — dead zone removed
    min_sniper_edge_bps: float = _f("MIN_SNIPER_EDGE_BPS", 80)  # Was 150 bps — lowered for maker-only
    min_entry_price: float = _f("MIN_ENTRY_PRICE", 0.05)
    max_entry_price: float = _f("MAX_ENTRY_PRICE", 0.95)

    # --- Strict VPN Execution Gates ---
    vpn_safe_mode: bool = True
    vpn_maker_only: bool = True
    vpn_disable_taker_fallback: bool = True  # NEVER fallback to taker locally if timed out
    vpn_min_executable_edge: float = _f("VPN_MIN_EXECUTABLE_EDGE", 0.06)
    vpn_maker_timeout_sec: float = _f("VPN_MAKER_TIMEOUT_SEC", 15.0)
    vpn_max_ws_age_sec: float = _f("VPN_MAX_WS_AGE_SEC", 1.5)
    
    # Latency Blockers
    max_vpn_latency_ms: float = _f("MAX_VPN_LATENCY_MS", 600.0)
    vpn_e2e_p50_block_ms: float = _f("VPN_E2E_P50_BLOCK_MS", 250.0)
    vpn_e2e_jitter_block_ms: float = _f("VPN_E2E_JITTER_BLOCK_MS", 150.0)
    latency_buffer_usd: float = 0.02 # Cost assumption buffer
    
    # --- Frequency Restraints ---
    max_concurrent_trades: int = 1
    cooldown_after_trade_sec: int = 900  # Lock out for 15m after trade
    cooldown_after_loss_sec: int = 1800  # Lock out for 30m after loss
    max_consec_loss: int = int(os.environ.get("MAX_CONSEC_LOSS", "10"))  # Was 2 — too tight for maker-only

    # --- Windows ---
    vpn_entry_min_secs_left: float = _f("VPN_ENTRY_MIN_SECS_LEFT", 120.0)
    vpn_entry_max_secs_left: float = _f("VPN_ENTRY_MAX_SECS_LEFT", 880.0)
    
    # --- Selective Entry Gates ---
    vpn_neutral_zone_width: float = _f("VPN_NEUTRAL_ZONE_WIDTH", 0.05)
    min_volatility_gate_bps: float = _f("MIN_VOLATILITY_GATE_BPS", 8.0)
    min_poly_ofi_threshold: float = _f("MIN_POLY_OFI_THRESHOLD", 0.15)
    macro_trend_filter_enabled: bool = _b("MACRO_TREND_FILTER_ENABLED", True)
    golden_entry_window_enabled: bool = _b("GOLDEN_ENTRY_WINDOW_ENABLED", False)
    golden_entry_window_min_sec: float = _f("GOLDEN_ENTRY_WINDOW_MIN_SEC", 300.0)
    golden_entry_window_max_sec: float = _f("GOLDEN_ENTRY_WINDOW_MAX_SEC", 480.0)

    # --- Legacy Signal Compatibility ---
    zscore_window: int = _i("ZSCORE_WINDOW", 10)
    zscore_threshold: float = _f("ZSCORE_THRESHOLD", 2.0)
    momentum_ticks: int = _i("MOMENTUM_TICKS", 5)
    momentum_min_move: float = _f("MOMENTUM_MIN_MOVE", 0.02)
    
    # --- Integration & Legacy Orchestration ---
    auto_market_selection: bool = _b("AUTO_MARKET_SELECTION", True)
    poll_seconds: float = _f("POLL_SECONDS", 3.0)
    dump_move_threshold: float = 0.05
    enable_shadow_journal: bool = False
    hybrid_maker_mode_enabled: bool = False
    maker_max_reprice_attempts: int = 3
    maker_reprice_enabled: bool = False
    maker_reprice_ticks: int = 1
    ofi_bypass_threshold: float = 0.05
    recent_active_close_summary: Any = field(default=None)
    same_direction_entry_cooldown_sec: int = 300
    stop_loss_pct: float = 0.10
    taker_snipe_velocity: float = 0.001
    use_cex_oracle: bool = _b("USE_CEX_ORACLE", True)
    use_dynamic_thresholds: bool = False

    # --- Exits ---
    # We strictly hold to expiry if EV is high, or emergency close only
    expiry_first_certainty_hold_enabled: bool = True
    catastrophic_stop_loss_pct: float = 0.30
    
    report_scratch_pnl_pct: float = _f("REPORT_SCRATCH_PNL_PCT", 0.03)

    # --- CLOB runtime settings ---
    clob_host: str = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
    data_api_host: str = os.getenv("DATA_API_HOST", "https://data-api.polymarket.com")
    chain_id: int = _i("CHAIN_ID", 137)
    signature_type: int = _i("SIGNATURE_TYPE", 1)

    private_key: str = os.getenv("PRIVATE_KEY", "")
    funder_address: str = os.getenv("FUNDER_ADDRESS", "")
    token_id_up: str = os.getenv("TOKEN_ID_UP", "")
    token_id_down: str = os.getenv("TOKEN_ID_DOWN", "")
    clob_api_key: str = os.getenv("CLOB_API_KEY", "")
    clob_api_secret: str = os.getenv("CLOB_API_SECRET", "")
    clob_api_passphrase: str = os.getenv("CLOB_API_PASSPHRASE", "")
    enable_arbitrage: bool = _b("ENABLE_ARBITRAGE", False)
    arbitrage_max_cost: float = _f("ARBITRAGE_MAX_COST", 0.98)
    regime_opening_end_sec: float = _f("REGIME_OPENING_END_SEC", 300.0)
    regime_mid_end_sec: float = _f("REGIME_MID_END_SEC", 600.0)
    ai_advisor_enabled: bool = _b("AI_ADVISOR_ENABLED", False)
    ai_advisor_model: str = os.getenv("AI_ADVISOR_MODEL", "gemini-1.5-flash")
    ai_advisor_json_strict: bool = _b("AI_ADVISOR_JSON_STRICT", True)
    ai_advisor_timeout_sec: float = _f("AI_ADVISOR_TIMEOUT_SEC", 10.0)
    ai_api_key: str = os.getenv("AI_API_KEY", "")

    # --- Massive Legacy Compat ---
    api_fail_safe_streak: int = _i("API_FAIL_SAFE_STREAK", 3)
    api_slow_threshold_ms: float = _f("API_SLOW_THRESHOLD_MS", 1500.0)
    binance_adverse_exit_confirm_sec: float = _f("BINANCE_ADVERSE_EXIT_CONFIRM_SEC", 3.0)
    binance_adverse_exit_enabled: bool = _b("BINANCE_ADVERSE_EXIT_ENABLED", True)
    binance_adverse_exit_max_profit_pct: float = _f("BINANCE_ADVERSE_EXIT_MAX_PROFIT_PCT", 0.08)
    binance_adverse_exit_min_hold_sec: float = _f("BINANCE_ADVERSE_EXIT_MIN_HOLD_SEC", 4.0)
    binance_adverse_exit_require_current_confirm: bool = _b("BINANCE_ADVERSE_EXIT_REQUIRE_CURRENT_CONFIRM", True)
    binance_adverse_exit_velocity: float = _f("BINANCE_ADVERSE_EXIT_VELOCITY", 0.00035)
    binance_profit_protect_confirm_sec: float = _f("BINANCE_PROFIT_PROTECT_CONFIRM_SEC", 1.0)
    binance_profit_protect_enabled: bool = _b("BINANCE_PROFIT_PROTECT_ENABLED", True)
    binance_profit_protect_max_profit_pct: float = _f("BINANCE_PROFIT_PROTECT_MAX_PROFIT_PCT", 0.17)
    binance_profit_protect_min_hold_sec: float = _f("BINANCE_PROFIT_PROTECT_MIN_HOLD_SEC", 10.0)
    binance_profit_protect_min_profit_pct: float = _f("BINANCE_PROFIT_PROTECT_MIN_PROFIT_PCT", 0.08)
    binance_profit_protect_require_current_confirm: bool = _b("BINANCE_PROFIT_PROTECT_REQUIRE_CURRENT_CONFIRM", False)
    binance_profit_protect_stall_sec: float = _f("BINANCE_PROFIT_PROTECT_STALL_SEC", 6.0)
    binance_profit_protect_velocity: float = _f("BINANCE_PROFIT_PROTECT_VELOCITY", 0.00012)
    binance_signal_lag_sec: float = 2.0
    binary_kelly_divisor: float = _f("BINARY_KELLY_DIVISOR", 4.0)
    cancel_on_reversal_velocity: float = 0.0002
    clean_start_loss_streak_reset_sec: float = _f("CLEAN_START_LOSS_STREAK_RESET_SEC", 300.0)
    conservative_active_close_fee_pnl_floor: float = -0.05
    conservative_active_close_loss_streak: int = 3
    conservative_block_live_sync_protect: bool = _b("CONSERVATIVE_BLOCK_LIVE_SYNC_PROTECT", True)
    conservative_block_pending_confirmation: bool = _b("CONSERVATIVE_BLOCK_PENDING_CONFIRMATION", True)
    conservative_block_pending_orders: bool = _b("CONSERVATIVE_BLOCK_PENDING_ORDERS", True)
    conservative_extra_edge: float = 0.015
    conservative_max_open_positions: int = 1
    conservative_max_orders_per_5min: int = 1
    conservative_mode_enabled: bool = _b("CONSERVATIVE_MODE_ENABLED", False)
    conservative_skip_windows: int = 0
    conservative_sync_miss_limit: int = 1
    edge_threshold: float = _f("EDGE_THRESHOLD", 0.02)
    emergency_exit_max_attempts: int = _i("EMERGENCY_EXIT_MAX_ATTEMPTS", 8)
    emergency_exit_retry_delay_sec: float = _f("EMERGENCY_EXIT_RETRY_DELAY_SEC", 1.0)
    enable_dump_trigger: bool = _b("ENABLE_DUMP_TRIGGER", False)
    entry_blocked_utc_hours: str = os.getenv("ENTRY_BLOCKED_UTC_HOURS", "")
    entry_dual_velocity_confirm: bool = _b("ENTRY_DUAL_VELOCITY_CONFIRM", True)
    entry_execution_cost_buffer: float = 0.01
    entry_fee_floor_buffer: float = _f("ENTRY_FEE_FLOOR_BUFFER", 1.0)
    entry_max_actual_slippage_pct: float = _f("ENTRY_MAX_ACTUAL_SLIPPAGE_PCT", 0.18)
    entry_max_spread: float = _f("ENTRY_MAX_SPREAD", 0.10)
    entry_micro_band_half_width: float = 0.01
    entry_micro_edge_penalty: float = 0.01
    entry_min_best_ask_multiple: float = 1.0
    entry_min_total_ask_multiple: float = 1.0
    entry_neutral_band_half_width: float = 0.02
    entry_neutral_edge_penalty: float = 0.01
    entry_require_maker_edge_buffer: float = _f("ENTRY_REQUIRE_MAKER_EDGE_BUFFER", 0.01)
    entry_retry_attempts: int = 1
    entry_retry_backoff_sec: float = 1.0
    entry_side_conflict_enabled: bool = _b("ENTRY_SIDE_CONFLICT_ENABLED", True)
    entry_side_conflict_min_edge_gap: float = _f("ENTRY_SIDE_CONFLICT_MIN_EDGE_GAP", 0.025)
    entry_side_conflict_min_prob_gap: float = _f("ENTRY_SIDE_CONFLICT_MIN_PROB_GAP", 0.03)
    entry_slippage_guard_enabled: bool = _b("ENTRY_SLIPPAGE_GUARD_ENABLED", True)
    entry_velocity_min: float = _f("ENTRY_VELOCITY_MIN", 0.0002)
    entry_window_max_sec: float = _f("ENTRY_WINDOW_MAX_SEC", 95.0)
    entry_window_min_sec: float = _f("ENTRY_WINDOW_MIN_SEC", 30.0)
    exit_deadline_sec: float = _f("EXIT_DEADLINE_SEC", 35.0)
    hedge_exit_advantage_threshold: float = 0.05
    hedge_exit_enabled: bool = False
    hedge_ratio: float = 0.5
    heuristic_probability_weight: float = 0.5
    high_confidence_edge_extra: float = 0.015
    high_confidence_taker_fallback_enabled: bool = False
    late_certainty_hold_enabled: bool = True
    late_certainty_hold_min_mark: float = 0.90
    late_entry_edge_penalty: float = _f("LATE_ENTRY_EDGE_PENALTY", 0.015)
    leave_loss_tail_pct: float = 0.10
    live_entry_use_market_orders: bool = _b("LIVE_ENTRY_USE_MARKET_ORDERS", True)
    live_force_full_loss_exit: bool = _b("LIVE_FORCE_FULL_LOSS_EXIT", True)
    live_loss_exit_force_taker: bool = _b("LIVE_LOSS_EXIT_FORCE_TAKER", True)
    live_order_hard_cap_usd: float = _f("LIVE_ORDER_HARD_CAP_USD", 3.0)
    live_position_grace_sec: float = 5.0
    live_position_miss_limit: int = 3
    live_take_profit_force_taker: bool = _b("LIVE_TAKE_PROFIT_FORCE_TAKER", True)
    loss_exit_max_attempts: int = 1
    loss_exit_retry_delay_sec: float = 1.0
    maker_entry_enabled: bool = True
    maker_fallback_extra_edge_buffer: float = 0.01
    maker_order_timeout_sec: float = 15.0
    maker_timeout_fallback_taker: bool = True
    manual_reset_daily_max_loss_on_start: bool = False
    max_bet_cap_usd: float = 3.0
    max_open_positions: int = _i("MAX_OPEN_POSITIONS", 2)
    max_orders_per_5min: int = _i("MAX_ORDERS_PER_5MIN", 3)
    near_stop_poll_hold_sec: float = _f("NEAR_STOP_POLL_HOLD_SEC", 15.0)
    near_stop_poll_seconds: float = 0.5
    network_recovery_streak: int = _i("NETWORK_RECOVERY_STREAK", 2)
    open_position_poll_seconds: float = _f("OPEN_POSITION_POLL_SECONDS", 1.0)
    pending_order_poll_seconds: float = _f("PENDING_ORDER_POLL_SECONDS", 1.0)
    position_watch_debug_enabled: bool = _b("POSITION_WATCH_DEBUG_ENABLED", True)
    position_watch_log_interval_sec: float = _f("POSITION_WATCH_LOG_INTERVAL_SEC", 5.0)
    profit_reversal_adverse_velocity: float = _f("PROFIT_REVERSAL_ADVERSE_VELOCITY", 0.0003)
    profit_reversal_drawdown_pct: float = _f("PROFIT_REVERSAL_DRAWDOWN_PCT", 0.18)
    profit_reversal_enabled: bool = _b("PROFIT_REVERSAL_ENABLED", True)
    profit_reversal_min_current_profit_pct: float = _f("PROFIT_REVERSAL_MIN_CURRENT_PROFIT_PCT", 0.12)
    profit_reversal_min_mfe_pct: float = _f("PROFIT_REVERSAL_MIN_MFE_PCT", 0.50)
    profitability_conservative_mode_enabled: bool = True
    rich_price_edge_penalty: float = _f("RICH_PRICE_EDGE_PENALTY", 0.015)
    scoreboard_aux_weight: float = _f("SCOREBOARD_AUX_WEIGHT", 0.3)  # Was 0.2 — increased per Phase 3
    scoreboard_entry_gate_min_decisive_trades: int = _i("SCOREBOARD_ENTRY_GATE_MIN_DECISIVE_TRADES", 5)
    scoreboard_min_win_rate: float = 0.55
    smart_stop_loss_enabled: bool = False
    soft_stop_adverse_velocity: float = _f("SOFT_STOP_ADVERSE_VELOCITY", 0.00018)
    soft_stop_confirm_buffer_pct: float = _f("SOFT_STOP_CONFIRM_BUFFER_PCT", 0.015)
    soft_stop_confirm_sec: float = _f("SOFT_STOP_CONFIRM_SEC", 2.5)
    stop_loss_partial_fraction: float = _f("LIVE_STOP_LOSS_PARTIAL_FRACTION", 0.80)
    stop_loss_partial_pct: float = 0.05
    stop_loss_scaleout_emergency_fill_ratio: float = 0.0
    take_profit_partial_fraction: float = _f("TAKE_PROFIT_PARTIAL_FRACTION", 0.40)
    take_profit_runner_fraction: float = _f("TAKE_PROFIT_RUNNER_FRACTION", 0.10)
    take_profit_soft_pct: float = _f("TAKE_PROFIT_SOFT_PCT", 0.18)
    use_kelly_sizing: bool = False
    volatility_gate_enabled: bool = True
    volatility_gate_min_range_usd: float = 25.0
    ws_stale_fail_safe_streak: int = _i("WS_STALE_FAIL_SAFE_STREAK", 2)
    ws_stale_max_age_sec: float = _f("WS_STALE_MAX_AGE_SEC", 5.0)

    # Legacy strategies (OFI, flash snipe) — disabled until backtested on 15m
    enable_legacy_strategies: bool = _b("ENABLE_LEGACY_STRATEGIES", False)

    # Shadow Journaling (Keep for analytics)
    enable_shadow_journal_legacy: bool = _b("ENABLE_SHADOW_JOURNAL", True)

SETTINGS = Settings()
