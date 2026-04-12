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

def get_fair_value(
    btc_price: float,
    strike_price: float,
    secs_left: float,
    implied_vol: Optional[float] = None
) -> float:
    """
    Returns the unified fair value (0.0 to 1.0) for a YES token.
    """
    # Use 70% annual vol as a conservative default for short-term BTC
    vol = implied_vol if implied_vol is not None else 0.70
    
    prob = calculate_binary_probability(
        current_price=btc_price,
        strike_price=strike_price,
        time_to_expiry_sec=secs_left,
        volatility_annual=vol
    )
    
    return prob
