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
            return {"p50": 0.0, "p95": 0.0, "jitter": 0.0}
        data = sorted(list(window))
        n = len(data)
        p50 = statistics.median(data)
        p95 = data[int(n * 0.95)] if n > 0 else p50
        jitter = p95 - p50
        return {"p50": p50, "p95": p95, "jitter": jitter}

    def get_e2e_stats(self) -> Dict[str, float]:
        """Returns consolidated E2E stats based on decision_to_order."""
        return self._get_stats(self.decision_to_order)

    def get_median_rtt(self) -> float:
        if not self.rtts:
            return 0.0
        return statistics.median(self.rtts)

    def get_network_mode(self) -> str:
        """
        Determines the trading mode based on network health.
        Modes: normal, maker_only, close_only
        """
        if not SETTINGS.vpn_safe_mode:
            return "normal"
            
        median_rtt = self.get_median_rtt()
        # Fallback to general stats if e2e is empty
        stats = self.get_e2e_stats()
        
        # Use simple jitter calculation from generic RTTs if e2e is not yet populated
        rtt_list = list(self.rtts)
        jitter = 0.0
        if len(rtt_list) >= 5:
            jitter = statistics.stdev(rtt_list)

        # 1. Close-only mode (Hard stop for entries)
        if median_rtt > SETTINGS.NETWORK_CLOSE_ONLY_LATENCY_MS:
            return "close_only"
        if jitter > SETTINGS.NETWORK_CLOSE_ONLY_JITTER_MS:
            return "close_only"
            
        # 2. Maker-only mode (Degraded performance)
        if median_rtt > SETTINGS.NETWORK_MAKER_ONLY_LATENCY_MS:
            return "maker_only"
        if jitter > SETTINGS.NETWORK_MAKER_ONLY_JITTER_MS:
            return "maker_only"
            
        return "normal"

    def get_edge_penalty(self) -> float:
        median_rtt = self.get_median_rtt()
        # In VPN safe mode, we penalize more aggressively
        base_threshold = 100.0
        if SETTINGS.vpn_safe_mode:
            stats = self.get_e2e_stats()
            # If E2E is slow, add extra penalty
            e2e_penalty = 0.0
            if stats["p50"] > SETTINGS.vpn_e2e_p50_block_ms:
                e2e_penalty = SETTINGS.latency_edge_buffer * 2.0
            return max(e2e_penalty, (median_rtt - base_threshold) / 100.0 * SETTINGS.latency_edge_buffer)
        
        return max(0.0, (median_rtt - base_threshold) / 100.0 * SETTINGS.latency_edge_buffer)

    def is_blocked(self) -> tuple[bool, str]:
        mode = self.get_network_mode()
        if mode == "close_only":
            return True, f"NetworkMode=close_only (rtt={self.get_median_rtt():.0f}ms)"
        return False, ""

LATENCY_MONITOR = LatencyMonitor()
