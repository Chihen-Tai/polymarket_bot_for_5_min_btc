from __future__ import annotations
from typing import List, Dict, Any

def get_vwap_from_ladder(ladder: List[Dict[str, Any]], size_usd: float) -> float:
    """
    模擬在訂單簿梯次中成交特定金額的 VWAP。
    如果深度不足以支撐該金額，回傳 999.0 (代表成本無窮大)。
    """
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

def calculate_committed_edge(
    fair_value: float, 
    ob_up: Dict[str, Any], 
    ob_down: Dict[str, Any], 
    order_size_usd: float, 
    side: str
) -> float:
    """
    計算「承諾邊際」。
    假設進場付 Taker 費（保守估計），出場持有到期（0 費用）。
    """
    taker_fee_rate = 0.0156
    safety_buffer = 0.01  # 1% 額外滑點/延遲緩衝
    
    if side == "UP":
        # 進場成本：從 UP 的 Ask 梯次計算
        entry_vwap = get_vwap_from_ladder(ob_up.get('asks', []), order_size_usd)
        if entry_vwap > 1.0: return -1.0
        
        # 預期價值：FV (機率 * $1.00)
        ev_expiry = fair_value
        edge = ev_expiry - entry_vwap - (entry_vwap * taker_fee_rate) - safety_buffer
    else:
        # 進場成本：從 DOWN 的 Ask 梯次計算
        entry_vwap = get_vwap_from_ladder(ob_down.get('asks', []), order_size_usd)
        if entry_vwap > 1.0: return -1.0
        
        # DOWN 的 FV 是 (1 - YES_FV)
        ev_expiry = 1.0 - fair_value
        edge = ev_expiry - entry_vwap - (entry_vwap * taker_fee_rate) - safety_buffer
        
    return float(edge)
