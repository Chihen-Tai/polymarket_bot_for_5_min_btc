from __future__ import annotations
import numpy as np
from typing import Dict, List, Any

def calculate_ofi(order_book: Dict[str, List[Dict[str, Any]]]) -> float:
    """
    計算訂單流不平衡 (Order Flow Imbalance, OFI)。
    正值代表買盤壓力大，負值代表賣盤壓力大。
    """
    bids = order_book.get('bids', [])[:3]  # 只看前三檔
    asks = order_book.get('asks', [])[:3]
    
    if not bids or not asks:
        return 0.0
    
    # 簡單成交量加權不平衡
    bid_vol = sum(float(b['size']) for b in bids)
    ask_vol = sum(float(a['size']) for a in asks)
    
    total_vol = bid_vol + ask_vol
    if total_vol < 1e-9:
        return 0.0
        
    imbalance = (bid_vol - ask_vol) / total_vol
    return float(np.clip(imbalance, -1.0, 1.0))

def get_book_skew(order_book: Dict[str, List[Dict[str, Any]]]) -> float:
    """計算掛單價格偏斜度"""
    bids = order_book.get('bids', [])
    asks = order_book.get('asks', [])
    if not bids or not asks:
        return 0.5
    
    best_bid = float(bids[0]['price'])
    best_ask = float(asks[0]['price'])
    mid = (best_bid + best_ask) / 2
    return mid
