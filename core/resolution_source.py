from __future__ import annotations
import time
from typing import Optional


def get_chainlink_btc_price() -> Optional[float]:
    """
    獲取 Chainlink BTC/USD 聚合餵價。
    Polymarket 決議的核心依據。
    """
    # TODO: 實作真正的 Chainlink Oracle 抓取 (例如透過 RPC 或特定 API)
    # 目前暫時使用占位邏輯，實務上需與 Binance 進行分歧檢查
    return None


def get_chainlink_oracle_age_s() -> float | None:
    # Chainlink snapshot ingestion is not implemented yet, so freshness is unknown.
    return None

def check_resolution_divergence(primary_price: float, secondary_price: float) -> tuple[bool, float]:
    """
    檢查主決議源 (Chainlink) 與參考源 (Binance) 的分歧。
    如果分歧超過 5 bps (0.05%)，代表數據不可信。
    """
    if primary_price <= 0 or secondary_price <= 0:
        return False, 0.0
        
    divergence = abs(primary_price - secondary_price) / primary_price
    is_safe = divergence <= 0.0005 # 5 bps
    return is_safe, divergence
