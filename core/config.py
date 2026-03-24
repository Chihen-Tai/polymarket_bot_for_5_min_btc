import os
from dataclasses import dataclass
from dotenv import load_dotenv

from pathlib import Path
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


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
    max_exposure_usd: float = _f("MAX_EXPOSURE_USD", 1.0)
    max_orders_per_5min: int = _i("MAX_ORDERS_PER_5MIN", 2)
    max_consec_loss: int = _i("MAX_CONSEC_LOSS", 3)
    daily_max_loss: float = _f("DAILY_MAX_LOSS", 3.0)
    ofi_bypass_threshold: float = _f("OFI_BYPASS_THRESHOLD", 0.65)

    poll_seconds: int = _i("POLL_SECONDS", 15)

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
    zscore_window: int = _i("ZSCORE_WINDOW", 20)
    zscore_threshold: float = _f("ZSCORE_THRESHOLD", 2.0)
    entry_window_min_sec: float = _f("ENTRY_WINDOW_MIN_SEC", 120.0)
    min_entry_price: float = _f("MIN_ENTRY_PRICE", 0.35)
    max_entry_price: float = _f("MAX_ENTRY_PRICE", 0.75)

    # Cadence guard: avoid long no-trade stretches
    max_idle_minutes: int = _i("MAX_IDLE_MINUTES", 120)
    live_position_grace_sec: int = _i("LIVE_POSITION_GRACE_SEC", 90)
    live_position_miss_limit: int = _i("LIVE_POSITION_MISS_LIMIT", 3)

    # Dump+hedge integration
    dump_move_threshold: float = _f("DUMP_MOVE_THRESHOLD", 0.25)
    hedge_sum_target: float = _f("HEDGE_SUM_TARGET", 0.95)
    hedge_ratio: float = _f("HEDGE_RATIO", 0.0)
    ws_flash_snipe_threshold: float = _f("WS_FLASH_SNIPE_THRESHOLD", 0.003)
    hedge_max_wait_sec: int = _i("HEDGE_MAX_WAIT_SEC", 90)
    stop_loss_pct: float = _f("STOP_LOSS_PCT", 0.05)
    smart_stop_loss_enabled: bool = _b("SMART_STOP_LOSS_ENABLED", True)
    stop_loss_partial_pct: float = _f("STOP_LOSS_PARTIAL_PCT", 0.03)
    stop_loss_partial_fraction: float = _f("STOP_LOSS_PARTIAL_FRACTION", 0.50)
    max_hold_seconds: int = _i("MAX_HOLD_SECONDS", 180)
    take_profit_scaleout_pct: float = _f("TAKE_PROFIT_SCALEOUT_PCT", 0.03)
    take_profit_soft_pct: float = _f("TAKE_PROFIT_SOFT_PCT", 0.05)
    take_profit_hard_pct: float = _f("TAKE_PROFIT_HARD_PCT", 0.08)
    momentum_ticks: int = _i("MOMENTUM_TICKS", 3)
    momentum_min_move: float = _f("MOMENTUM_MIN_MOVE", 0.005)
    exit_deadline_sec: int = _i("EXIT_DEADLINE_SEC", 20)
    stop_loss_warn_pct: float = _f("STOP_LOSS_WARN_PCT", 0.03)

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
    cancel_on_reversal_velocity: float = _f("CANCEL_ON_REVERSAL_VELOCITY", 0.003)

    # Phase 2: Advanced Loophole Exploitation
    taker_snipe_velocity: float = _f("TAKER_SNIPE_VELOCITY", 0.0008)
    panic_dump_velocity: float = _f("PANIC_DUMP_VELOCITY", 0.0010)
    tp_hold_velocity: float = _f("TP_HOLD_VELOCITY", 0.0004)

    # Phase 3: Entry & Exit Quality Guards
    # hard_stop_shield_velocity: if Binance velocity is same-direction as position,
    # skip hard stop for this cycle. Set 0.0 to disable.
    hard_stop_shield_velocity: float = _f("HARD_STOP_SHIELD_VELOCITY", 0.0004)
    # entry_velocity_min: block entry if Binance velocity is strongly opposing signal.
    # Only blocks adverse moves; flat/zero velocity still allows entry. Set 0.0 to disable.
    entry_velocity_min: float = _f("ENTRY_VELOCITY_MIN", 0.0002)


SETTINGS = Settings()
