from __future__ import annotations
import os
import warnings
from dataclasses import dataclass
from pathlib import Path

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
    sniper_extreme_upper: float = 0.75  # Only fade extreme FOMO
    sniper_extreme_lower: float = 0.25  # Only fade extreme panic
    min_sniper_edge_bps: float = _f("MIN_SNIPER_EDGE_BPS", 800)  # 800 bps = 8%

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
    max_consec_loss: int = 2

    # --- Windows ---
    vpn_entry_min_secs_left: float = _f("VPN_ENTRY_MIN_SECS_LEFT", 120.0)
    vpn_entry_max_secs_left: float = _f("VPN_ENTRY_MAX_SECS_LEFT", 880.0)
    
    # --- Selective Entry Gates ---
    vpn_neutral_zone_width: float = _f("VPN_NEUTRAL_ZONE_WIDTH", 0.05)
    min_volatility_gate_bps: float = _f("MIN_VOLATILITY_GATE_BPS", 15.0)

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
    clob_api_key: str = os.getenv("CLOB_API_KEY", "")
    clob_api_secret: str = os.getenv("CLOB_API_SECRET", "")
    clob_api_passphrase: str = os.getenv("CLOB_API_PASSPHRASE", "")

    # Shadow Journaling (Keep for analytics)
    enable_shadow_journal: bool = _b("ENABLE_SHADOW_JOURNAL", True)

SETTINGS = Settings()
