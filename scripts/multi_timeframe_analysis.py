
import math
import statistics
import random
from dataclasses import dataclass
from typing import List, Dict, Optional

# --- CORE LOGIC ---

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calculate_binary_probability(S, K, T_sec, sigma):
    if T_sec <= 0: return 1.0 if S > K else 0.0
    T = T_sec / (365 * 24 * 3600)
    try:
        d2 = (math.log(S / K) - (sigma**2 / 2) * T) / (sigma * math.sqrt(T))
        return norm_cdf(d2)
    except: return 0.5

def get_polymarket_fee(p: float) -> float:
    return p * (1.0 - p) * 0.0156

# --- SIMULATION ENGINE ---

@dataclass
class MarketProfile:
    name: str
    duration_min: int
    base_spread: float
    depth_factor: float
    noise_ratio: float 

def run_backtest(profile: MarketProfile, iterations=10000, stress_factor=1.0):
    results = []
    
    # Global Parameters with Stress
    MIN_EDGE = 0.08
    LATENCY_BUFFER = 0.02 * stress_factor 
    SLIPPAGE_BUFFER = 0.01 * stress_factor
    BASE_FEE_MULT = 1.0 * stress_factor
    SPREAD_MULT = 1.0 * stress_factor
    
    for _ in range(iterations):
        S_start = 60000.0
        K = S_start
        sigma = 0.70
        
        # Entry allowed between 150s and 300s left
        secs_left = random.uniform(150, 300)
        dt = secs_left / (365 * 24 * 3600)
        
        # Simulate price move until entry
        S_current = S_start * math.exp((0.0 - 0.5 * sigma**2) * dt + sigma * math.sqrt(dt) * random.gauss(0, 1))
        
        # Market price noise (implied prob deviation)
        if random.random() < profile.noise_ratio:
            market_p_noise = random.uniform(-0.15, 0.15)
        else:
            market_p_noise = random.uniform(-0.02, 0.02)
            
        fv = calculate_binary_probability(S_current, K, secs_left, sigma)
        mid_p = max(0.01, min(0.99, fv + market_p_noise))
        current_spread = profile.base_spread * SPREAD_MULT
        ask_p = min(0.99, mid_p + (current_spread / 2))
        bid_p = max(0.01, mid_p - (current_spread / 2))
        
        side = None
        entry_p = 0.0
        edge = 0.0
        fee_at_mid = get_polymarket_fee(mid_p) * BASE_FEE_MULT
        
        if ask_p < 0.25:
            side = "UP"
            entry_p = ask_p
            edge = fv - entry_p - fee_at_mid - SLIPPAGE_BUFFER - LATENCY_BUFFER
        elif (1.0 - bid_p) < 0.25:
            side = "DOWN"
            entry_p = 1.0 - bid_p
            edge = (1.0 - fv) - entry_p - fee_at_mid - SLIPPAGE_BUFFER - LATENCY_BUFFER
            
        if side and edge >= MIN_EDGE:
            # Simulate price move from entry to expiry
            S_final = S_current * math.exp((0.0 - 0.5 * sigma**2) * dt + sigma * math.sqrt(dt) * random.gauss(0, 1))
            win = 0
            if side == "UP": win = 1 if S_final >= K else 0
            else: win = 1 if S_final < K else 0
            pnl = win - entry_p - fee_at_mid
            results.append({"pnl": pnl, "win": win})
            
    if not results: return {"name": profile.name, "count": 0, "win_rate": 0, "avg_pnl": 0, "robustness": 0}
    avg_pnl = statistics.mean(r['pnl'] for r in results)
    return {"name": profile.name, "count": len(results), "win_rate": sum(r['win'] for r in results) / len(results), "avg_pnl": avg_pnl, "robustness": avg_pnl / (abs(min([r['pnl'] for r in results])) + 1e-9)}

def main():
    profiles = [
        MarketProfile("15m", 15, 0.03, 1.0, 0.15),
        MarketProfile("30m", 30, 0.02, 1.5, 0.10),
        MarketProfile("1H",  60, 0.015, 2.0, 0.05),
    ]
    
    print("### BASELINE (10,000 iterations)")
    print("| Timeframe | Trades | Win Rate | Avg PnL | Robustness |")
    print("|-----------|--------|----------|---------|------------|")
    for p in profiles:
        res = run_backtest(p)
        print(f"| {res['name']:<9} | {res['count']:<6} | {res['win_rate']:>8.2%} | {res['avg_pnl']:>7.4f} | {res['robustness']:>10.2f} |")

    print("\n### STRESS TEST (+50% Spread/Latency/Fees)")
    print("| Timeframe | Trades | Win Rate | Avg PnL | Robustness |")
    print("|-----------|--------|----------|---------|------------|")
    for p in profiles:
        res = run_backtest(p, stress_factor=1.5)
        print(f"| {res['name']:<9} | {res['count']:<6} | {res['win_rate']:>8.2%} | {res['avg_pnl']:>7.4f} | {res['robustness']:>10.2f} |")

if __name__ == "__main__":
    main()
