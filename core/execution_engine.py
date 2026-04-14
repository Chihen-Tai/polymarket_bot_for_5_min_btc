from __future__ import annotations
from typing import List, Dict, Any
from core.config import SETTINGS

def get_vwap_from_ladder(ladder: List[Dict[str, Any]], size_usd: float) -> float:
    if not ladder:
        return 999.0
        
    cumulative_usd = 0.0
    cumulative_shares = 0.0
    
    for level in ladder:
        price = float(level['price'])
        shares = float(level['size'])
        level_usd = price * shares
        
        if cumulative_usd + level_usd >= size_usd:
            needed_usd = size_usd - cumulative_usd
            cumulative_shares += (needed_usd / price)
            return size_usd / cumulative_shares
            
        cumulative_usd += level_usd
        cumulative_shares += shares
        
    return 999.0

class PolymarketDynamicFeeModel:
    """
    Polymarket dynamic fee abstraction for crypto markets.
    Formula: TotalFee = AmountUSD * 0.02 * Price * (1.0 - Price)
    
    References:
    - Maker: Typically 0% if post-only.
    - Taker: Dynamic based on price proximity to 0.50.
    """
    def __init__(self, taker_rate: float = 0.02, maker_rate: float = 0.0):
        self.taker_rate = taker_rate
        self.maker_rate = maker_rate

    def calculate_taker_fee(self, price: float, size_usd: float) -> float:
        """
        Calculates taker fee based on theoretical p * (1-p) scaling.
        Max fee is at p=0.50 (0.25 * 0.02 = 0.5% of notional).
        """
        if price <= 0 or price >= 1.0:
            return 0.0
        return float(size_usd * self.taker_rate * price * (1.0 - price))

    def calculate_maker_fee(self, price: float, size_usd: float) -> float:
        """
        Calculates maker fee (or rebate if rate is negative).
        Currently assuming 0% as default for maker post-only.
        """
        if price <= 0 or price >= 1.0:
            return 0.0
        return float(size_usd * self.maker_rate * price * (1.0 - price))

# Global injected fee model for current epoch
FEE_MODEL = PolymarketDynamicFeeModel()

def calculate_committed_edge(
    fair_value: float, 
    ob_up: Dict[str, Any], 
    ob_down: Dict[str, Any], 
    order_size_usd: float, 
    side: str,
    assume_maker: bool = True
) -> float:
    """
    Calculates execution edge.
    Edge = EV - EntryPrice - Fees - (LatencyBuffer + SlippageBuffer)
    """
    # 1. Determine Entry Price (Maker Best Ask or Taker VWAP)
    if side == "UP":
        top_ask = float(ob_up.get('asks', [{'price': 999.0}])[0]['price'])
        entry_price = top_ask if assume_maker else get_vwap_from_ladder(ob_up.get('asks', []), order_size_usd)
        if entry_price > 1.0: return -1.0
        
        # 2. Calculate Fees
        fee_func = FEE_MODEL.calculate_maker_fee if assume_maker else FEE_MODEL.calculate_taker_fee
        entry_fee_rate = fee_func(entry_price, order_size_usd) / max(order_size_usd, 1e-9)
        
        # 3. Apply Buffers for VPN Latency & Micro-Slippage
        # latency_buffer_usd is a fixed cost assumption for stale signals
        latency_cost = float(getattr(SETTINGS, "latency_buffer_usd", 0.01) or 0.01)
        # slippage_buffer handles taker-sniping fills being worse than quoted
        slippage_cost = 0.005 if not assume_maker else 0.0
        
        ev_expiry = fair_value
        edge = ev_expiry - entry_price - entry_fee_rate - latency_cost - slippage_cost
    else:
        top_ask = float(ob_down.get('asks', [{'price': 999.0}])[0]['price'])
        entry_price = top_ask if assume_maker else get_vwap_from_ladder(ob_down.get('asks', []), order_size_usd)
        if entry_price > 1.0: return -1.0
        
        fee_func = FEE_MODEL.calculate_maker_fee if assume_maker else FEE_MODEL.calculate_taker_fee
        entry_fee_rate = fee_func(entry_price, order_size_usd) / max(order_size_usd, 1e-9)
        
        latency_cost = float(getattr(SETTINGS, "latency_buffer_usd", 0.01) or 0.01)
        slippage_cost = 0.005 if not assume_maker else 0.0
        
        ev_expiry = 1.0 - fair_value
        edge = ev_expiry - entry_price - entry_fee_rate - latency_cost - slippage_cost
        
    return float(edge)
