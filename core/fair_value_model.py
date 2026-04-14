from __future__ import annotations
import math
from typing import Optional

def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function approximation."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calculate_binary_probability(
    current_price: float,
    strike_price: float | None,
    time_to_expiry_sec: float,
    volatility_annual: float = 0.60, # Default 60% annual vol
) -> float | None:
    """
    Calculates the theoretical probability that current_price > strike_price at expiry
    using a simplified Black-Scholes model for a binary option (delta).
    
    P = Phi(d2)
    d2 = [ln(S/K) - (sigma^2 / 2) * T] / (sigma * sqrt(T))
    """
    if strike_price is None:
        return None

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

import statistics

def calculate_realized_vol(price_history: list[float], window: int = 20) -> float:
    """
    從價格歷史計算年化實現波動率。
    假設輸入是 1 分鐘級別的價格。
    """
    if len(price_history) < window:
        return 0.70  # 樣本不足時回傳預設值
    
    # Calculate log returns
    returns = []
    for i in range(1, len(price_history)):
        returns.append(math.log(price_history[i] / price_history[i-1]))
    
    if len(returns) < 2:
        return 0.70

    # 年化係數: sqrt(一年分鐘數)
    stdev = statistics.stdev(returns)
    vol = stdev * math.sqrt(365 * 24 * 60)
    return float(max(0.30, min(vol, 1.50))) # 限制在 30%-150% 之間

def get_fair_value(
    btc_price: float,
    strike_price: float | None,
    secs_left: float,
    implied_vol: Optional[float] = None,
    price_history: Optional[list[float]] = None,
    ws_bba: Optional[dict] = None
) -> float:
    """
    Returns the unified fair value (0.0 to 1.0) for a YES token 
    by orchestrating the M1 (Black-Scholes) and M2 (Microstructure) Ensemble.
    """
    if implied_vol is not None:
        vol = implied_vol
    elif price_history:
        vol = calculate_realized_vol(price_history)
    else:
        vol = 0.70
    
    # M1 Base Probability
    base_prob = calculate_binary_probability(
        current_price=btc_price,
        strike_price=strike_price,
        time_to_expiry_sec=secs_left,
        volatility_annual=vol
    )
    
    # Send through ensemble aggregator
    from core.ensemble_models.ensemble import ENSEMBLE
    prob = ENSEMBLE.get_calibrated_fair_value(base_prob, ws_bba)
    
    return float(prob)
