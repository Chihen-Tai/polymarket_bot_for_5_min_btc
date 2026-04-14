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

class FeeModel:
    """
    Polymarket dynamic fee abstraction.
    Maker orders often have 0% fee or a rebate.
    Taker orders incur the dynamic fee p * (1-p) * TakerRate.
    """
    def __init__(self, taker_rate: float = 0.02, maker_rate: float = 0.0):
        self.taker_rate = taker_rate
        self.maker_rate = maker_rate

    def calculate_taker_fee(self, price: float, size_usd: float) -> float:
        """Calculates taker fee based on theoretical p * (1-p) scaling."""
        return float(size_usd * self.taker_rate * price * (1.0 - price))

    def calculate_maker_fee(self, price: float, size_usd: float) -> float:
        """Calculates maker fee (or rebate if rate is negative)."""
        return float(size_usd * self.maker_rate * price * (1.0 - price))

# Global injected fee model for current epoch
FEE_MODEL = FeeModel()

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
    Edge = EV - EntryPrice - Fees - EmpiricalSlippage
    """
    if side == "UP":
        top_ask = float(ob_up.get('asks', [{'price': 999.0}])[0]['price'])
        entry_price = top_ask if assume_maker else get_vwap_from_ladder(ob_up.get('asks', []), order_size_usd)
        if entry_price > 1.0: return -1.0
        
        fee_func = FEE_MODEL.calculate_maker_fee if assume_maker else FEE_MODEL.calculate_taker_fee
        entry_fee = fee_func(entry_price, order_size_usd) / order_size_usd
        
        ev_expiry = fair_value
        edge = ev_expiry - entry_price - entry_fee - SETTINGS.latency_buffer_usd
    else:
        top_ask = float(ob_down.get('asks', [{'price': 999.0}])[0]['price'])
        entry_price = top_ask if assume_maker else get_vwap_from_ladder(ob_down.get('asks', []), order_size_usd)
        if entry_price > 1.0: return -1.0
        
        fee_func = FEE_MODEL.calculate_maker_fee if assume_maker else FEE_MODEL.calculate_taker_fee
        entry_fee = fee_func(entry_price, order_size_usd) / order_size_usd
        
        ev_expiry = 1.0 - fair_value
        edge = ev_expiry - entry_price - entry_fee - SETTINGS.latency_buffer_usd
        
    return float(edge)
