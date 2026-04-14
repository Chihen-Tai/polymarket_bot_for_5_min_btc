from __future__ import annotations
from typing import List, Dict, Any
from core.config import SETTINGS

def get_vwap_from_ladder(ladder: List[Any], size_usd: float) -> float:
    if not ladder:
        return 999.0
        
    cumulative_usd = 0.0
    cumulative_shares = 0.0
    
    for level in ladder:
        try:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price = float(level[0])
                shares = float(level[1])
            elif isinstance(level, dict):
                price = float(level.get('price', 999.0))
                shares = float(level.get('size', 0.0))
            else:
                price = float(getattr(level, 'price', 999.0))
                shares = float(getattr(level, 'size', 0.0))
        except Exception:
            continue
            
        level_usd = price * shares
        
        if cumulative_usd + level_usd >= size_usd:
            needed_usd = size_usd - cumulative_usd
            if price > 0:
                cumulative_shares += (needed_usd / price)
                return size_usd / max(cumulative_shares, 1e-9)
            return 999.0
            
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
    def __init__(self, taker_rate: float = 0.0156, maker_rate: float = 0.0):
        self.taker_rate = taker_rate
        self.maker_rate = maker_rate

    def calculate_taker_fee(self, price: float, size_usd: float) -> float:
        """
        Calculates flat taker fee for CLOB markets.
        """
        if price <= 0 or price >= 1.0:
            return 0.0
        return float(size_usd * self.taker_rate)

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
    assume_maker: bool = True,
    secs_left: float | None = None
) -> float:
    """
    Calculates execution edge.
    Edge = EV - EntryPrice - Fees - (LatencyBuffer + SlippageBuffer)
    """
    # 1. Determine Entry Price (Maker Best Ask or Taker VWAP)
    if side == "UP":
        asks = ob_up.get('ask_levels', ob_up.get('asks', []))
        if not asks:
            return -1.0
        
        if isinstance(asks[0], (tuple, list)) and len(asks[0]) >= 2:
            top_ask = float(asks[0][0])
        elif isinstance(asks[0], dict):
            top_ask = float(asks[0].get('price', 999.0))
        else:
            top_ask = float(getattr(asks[0], 'price', 999.0))
            
        entry_price = top_ask if assume_maker else get_vwap_from_ladder(asks, order_size_usd)
        if entry_price >= 1.0: return -1.0
        
        # 2. Calculate Fees
        fee_func = FEE_MODEL.calculate_maker_fee if assume_maker else FEE_MODEL.calculate_taker_fee
        entry_fee_rate = fee_func(entry_price, order_size_usd) / max(order_size_usd, 1e-9)
        
        # 3. Apply Buffers for VPN Latency & Micro-Slippage
        # latency_buffer_usd is a fixed cost assumption for stale signals
        latency_cost = float(getattr(SETTINGS, "latency_buffer_usd", 0.01) or 0.01)
        if secs_left is not None and secs_left < 180.0 and entry_price > 0.85:
            latency_cost = 0.0  # Late certainty override
            
        # slippage_buffer handles taker-sniping fills being worse than quoted
        slippage_cost = 0.005 if not assume_maker else 0.0
        
        ev_expiry = fair_value
        edge = ev_expiry - entry_price - entry_fee_rate - latency_cost - slippage_cost
    else:
        asks = ob_down.get('ask_levels', ob_down.get('asks', []))
        if not asks:
            return -1.0
            
        if isinstance(asks[0], (tuple, list)) and len(asks[0]) >= 2:
            top_ask = float(asks[0][0])
        elif isinstance(asks[0], dict):
            top_ask = float(asks[0].get('price', 999.0))
        else:
            top_ask = float(getattr(asks[0], 'price', 999.0))
            
        entry_price = top_ask if assume_maker else get_vwap_from_ladder(asks, order_size_usd)
        if entry_price >= 1.0: return -1.0
        
        fee_func = FEE_MODEL.calculate_maker_fee if assume_maker else FEE_MODEL.calculate_taker_fee
        entry_fee_rate = fee_func(entry_price, order_size_usd) / max(order_size_usd, 1e-9)
        
        latency_cost = float(getattr(SETTINGS, "latency_buffer_usd", 0.01) or 0.01)
        if secs_left is not None and secs_left < 180.0 and entry_price > 0.85:
            latency_cost = 0.0  # Late certainty override
            
        slippage_cost = 0.005 if not assume_maker else 0.0
        
        ev_expiry = 1.0 - fair_value
        edge = ev_expiry - entry_price - entry_fee_rate - latency_cost - slippage_cost
        
    return float(edge)
