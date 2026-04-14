import collections
import statistics
import time
from typing import Dict, Deque
from core.config import SETTINGS

class LatencyMonitor:
    def __init__(self, history_size=30):
        # Generic RTTs (API latency)
        self.rtts: Deque[float] = collections.deque(maxlen=history_size)
        
        # E2E Metrics
        # decision_to_order: decision start -> order accepted by CLOB
        self.decision_to_order: Deque[float] = collections.deque(maxlen=history_size)
        # order_to_actionable: order accepted -> first actionable state (filled or placed)
        self.order_to_actionable: Deque[float] = collections.deque(maxlen=history_size)
        # trigger_to_close: exit trigger -> close response received
        self.trigger_to_close: Deque[float] = collections.deque(maxlen=history_size)

    def add_rtt(self, rtt_ms: float):
        self.rtts.append(rtt_ms)

    def get_last_rtt(self) -> float | None:
        if not self.rtts:
            return None
        return self.rtts[-1]

    def record_decision_to_order(self, ms: float):
        self.decision_to_order.append(ms)

    def record_order_to_actionable(self, ms: float):
        self.order_to_actionable.append(ms)

    def record_trigger_to_close(self, ms: float):
        self.trigger_to_close.append(ms)

    def _get_stats(self, window: Deque[float]) -> Dict[str, float]:
        if not window:
            return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "jitter_percentile": 0.0}
        data = sorted(list(window))
        n = len(data)
        p50 = statistics.median(data)
        p90 = data[int(n * 0.90)] if n > 0 else p50
        p95 = data[int(n * 0.95)] if n > 0 else p50
        # jitter_percentile captures the spread between typical and spike latency
        jitter_pct = p90 - p50
        return {"p50": p50, "p90": p90, "p95": p95, "jitter_percentile": jitter_pct}

    def get_e2e_stats(self) -> Dict[str, float]:
        """Returns consolidated E2E stats based on decision_to_order."""
        return self._get_stats(self.decision_to_order)

    def get_median_rtt(self) -> float:
        if not self.rtts:
            return 0.0
        return statistics.median(self.rtts)

    def get_network_quality_tier(self) -> str:
        """
        Calculates the network quality tier for Japan-VPN routing.
        Returns: NORMAL, DEGRADED, CLOSE_ONLY, BLOCKED
        """
        if not SETTINGS.vpn_safe_mode:
            return "NORMAL"
            
        median_rtt = self.get_median_rtt()
        e2e = self.get_e2e_stats()
        jitter_pct = e2e["jitter_percentile"]
        
        # 4. BLOCKED: Literal blackout or extreme stale
        if median_rtt > 800 or e2e["p50"] > 600 or jitter_pct > 400:
            return "BLOCKED"
            
        # 3. CLOSE_ONLY: High risk of toxic fills, but can manage existing
        if median_rtt > 600 or e2e["p50"] > 400 or jitter_pct > 250:
            return "CLOSE_ONLY"
            
        # 2. DEGRADED: Noticeable lag, requires higher edge thresholds
        if median_rtt > 400 or e2e["p50"] > 250 or jitter_pct > 150:
            return "DEGRADED"
            
        # 1. NORMAL: Within safety bounds for maker-first
        return "NORMAL"

    def get_edge_penalty(self) -> float:
        tier = self.get_network_quality_tier()
        if tier == "DEGRADED":
            return SETTINGS.latency_buffer_usd * 2.0
        if tier in {"CLOSE_ONLY", "BLOCKED"}:
            return 9.99 # Prohibitive
        return 0.0

    def is_blocked(self) -> tuple[bool, str]:
        tier = self.get_network_quality_tier()
        if tier == "BLOCKED":
            return True, f"NetworkTier=BLOCKED (rtt={self.get_median_rtt():.0f}ms)"
        return False, ""

LATENCY_MONITOR = LatencyMonitor()
