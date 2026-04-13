from __future__ import annotations
import time
from core.config import SETTINGS

import statistics
from core.resolution_source import check_resolution_divergence

class RiskManager:
    def __init__(self):
        self.consecutive_losses = 0
        self.last_trade_time = 0
        self.cooldown_until = 0
        self.rtt_history = [] # 儲存最近的 ping 延遲
        
    def add_latency_sample(self, ms: float):
        """記錄延遲樣本以計算抖動"""
        self.rtt_history.append(ms)
        if len(self.rtt_history) > 20:
            self.rtt_history.pop(0)

    def get_jitter(self) -> float:
        """計算延遲抖動 (標準差)"""
        if len(self.rtt_history) < 5:
            return 0.0
        return float(statistics.stdev(self.rtt_history))

    def update_outcome(self, pnl_usd: float):
        """更新交易結果，若虧損則增加連敗計數"""
        if pnl_usd < 0:
            self.consecutive_losses += 1
            cooldown_sec = SETTINGS.COOLDOWN_AFTER_LOSS * self.consecutive_losses
            self.cooldown_until = time.time() + cooldown_sec
        else:
            self.consecutive_losses = 0
            self.cooldown_until = 0

    def can_trade(self, current_equity: float, current_exposure: float, 
                  binance_p: float = 0, chainlink_p: float = 0,
                  network_mode: str = "normal") -> tuple[bool, str]:
        """檢查是否允許交易"""
        now = time.time()
        
        # 1. 網路品質檢查 (Graded Degradation)
        if network_mode == "close_only":
            return False, "network_close_only"
            
        # 2. 決議源分歧檢查
        if chainlink_p > 0:
            is_consistent, div = check_resolution_divergence(chainlink_p, binance_p)
            if not is_consistent:
                return False, f"resolution_divergence_{div:.4%}"

        # 3. 基礎風控
        if now < self.cooldown_until:
            return False, f"cooldown_active_{int(self.cooldown_until - now)}s"
            
        if current_equity < SETTINGS.min_equity:
            return False, "insufficient_equity"
            
        if current_exposure >= SETTINGS.max_exposure_usd:
            return False, "max_exposure_reached"
            
        if self.consecutive_losses >= SETTINGS.max_consec_loss:
            return False, "max_consec_loss_reached"
            
        return True, "ok"

# 全域單例
RISK_MANAGER = RiskManager()
