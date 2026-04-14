import collections
import statistics
import time
from typing import Dict, Deque, Optional
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

    def get_last_rtt(self) -> Optional[float]:
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

    def get_network_quality_tier(self) -> tuple[str, str]:
        """
        Calculates the network quality tier for Japan-VPN routing.
        Returns: (Tier, ReasonString)
        """
        if not SETTINGS.vpn_safe_mode:
            return "NORMAL", ""
            
        median_rtt = self.get_median_rtt()
        e2e = self.get_e2e_stats()
        jitter_pct = e2e["jitter_percentile"]
        
        max_rtt = getattr(SETTINGS, "max_vpn_latency_ms", 600.0)
        p50_block = getattr(SETTINGS, "vpn_e2e_p50_block_ms", 250.0)
        jitter_block = getattr(SETTINGS, "vpn_e2e_jitter_block_ms", 150.0)

        if median_rtt > max_rtt:
            return "BLOCKED", f"rtt={median_rtt:.0f}ms > {max_rtt}"
        if e2e["p50"] > p50_block:
            return "BLOCKED", f"e2e_p50={e2e['p50']:.0f}ms > {p50_block}"
        if jitter_pct > jitter_block:
            return "BLOCKED", f"jitter={jitter_pct:.0f}ms > {jitter_block}"
            
        if median_rtt > max_rtt * 0.75 or e2e["p50"] > p50_block * 0.8:
            return "CLOSE_ONLY", "sub-optimal latency bounds"
            
        return "NORMAL", ""

    def get_edge_penalty(self) -> float:
        tier, _ = self.get_network_quality_tier()
        if tier == "DEGRADED":
            return getattr(SETTINGS, "latency_buffer_usd", 0.02) * 2.0
        if tier in {"CLOSE_ONLY", "BLOCKED"}:
            return 9.99
        return 0.0

    def is_blocked(self) -> tuple[bool, str]:
        tier, reason = self.get_network_quality_tier()
        if tier == "BLOCKED":
            return True, f"NetworkTier=BLOCKED ({reason})"
        return False, ""

LATENCY_MONITOR = LatencyMonitor()
