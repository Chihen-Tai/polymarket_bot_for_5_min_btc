from __future__ import annotations
import math
from typing import Optional

def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function approximation."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calculate_binary_probability(
    current_price: float,
    strike_price: float,
    time_to_expiry_sec: float,
    volatility_annual: float = 0.60, # Default 60% annual vol
) -> float:
    """
    Calculates the theoretical probability that current_price > strike_price at expiry
    using a simplified Black-Scholes model for a binary option (delta).
    
    P = Phi(d2)
    d2 = [ln(S/K) - (sigma^2 / 2) * T] / (sigma * sqrt(T))
    """
    if time_to_expiry_sec <= 0:
        return 1.0 if current_price > strike_price else 0.0
    
    if current_price <= 0 or strike_price <= 0:
        return 0.5
    
    # Convert seconds to years
    T = time_to_expiry_sec / (365 * 24 * 3600)
    S = current_price
    K = strike_price
    sigma = volatility_annual
    
    try:
        d2 = (math.log(S / K) - (sigma**2 / 2) * T) / (sigma * math.sqrt(T))
        return float(norm_cdf(d2))
    except Exception:
        return 0.5

import numpy as np

def calculate_realized_vol(price_history: List[float], window: int = 20) -> float:
    """
    從價格歷史計算年化實現波動率。
    假設輸入是 1 分鐘級別的價格。
    """
    if len(price_history) < window:
        return 0.70  # 樣本不足時回傳預設值
    
    returns = np.diff(np.log(price_history))
    # 年化係數: sqrt(一年分鐘數)
    vol = np.std(returns) * math.sqrt(365 * 24 * 60)
    return float(np.clip(vol, 0.30, 1.50)) # 限制在 30%-150% 之間

def get_fair_value(
    btc_price: float,
    strike_price: float,
    secs_left: float,
    implied_vol: Optional[float] = None,
    price_history: Optional[List[float]] = None
) -> float:
    """
    Returns the unified fair value (0.0 to 1.0) for a YES token.
    """
    # 優先使用傳入的波動率，其次計算實現波動率，最後使用預設 70%
    if implied_vol is not None:
        vol = implied_vol
    elif price_history:
        vol = calculate_realized_vol(price_history)
    else:
        vol = 0.70
    
    prob = calculate_binary_probability(
        current_price=btc_price,
        strike_price=strike_price,
        time_to_expiry_sec=secs_left,
        volatility_annual=vol
    )
    
    return prob
