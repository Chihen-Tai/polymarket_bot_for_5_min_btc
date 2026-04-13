from __future__ import annotations
import time
from core.config import SETTINGS

class RiskManager:
    def __init__(self):
        self.consecutive_losses = 0
        self.last_trade_time = 0
        self.cooldown_until = 0
        
    def update_outcome(self, pnl_usd: float):
        """更新交易結果，若虧損則增加連敗計數"""
        if pnl_usd < 0:
            self.consecutive_losses += 1
            # 每多一筆連敗，冷卻時間增加
            cooldown_sec = SETTINGS.COOLDOWN_AFTER_LOSS * self.consecutive_losses
            self.cooldown_until = time.time() + cooldown_sec
        else:
            self.consecutive_losses = 0
            self.cooldown_until = 0

    def can_trade(self, current_equity: float, current_exposure: float) -> tuple[bool, str]:
        """檢查是否允許交易"""
        now = time.time()
        
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
