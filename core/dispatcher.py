import time
import logging
from concurrent.futures import ThreadPoolExecutor
from core.latency_monitor import LATENCY_MONITOR
from core.config import SETTINGS

log = logging.getLogger("dispatcher")

class TradeDispatcher:
    def __init__(self, max_workers=3):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.active_trades = 0

    def dispatch(self, func, *args, **kwargs):
        """
        Dispatches a trade function asynchronously and tracks latency.
        """
        if self.active_trades >= getattr(SETTINGS, "max_concurrent_trades", 1):
            log.warning("Max concurrent trades reached. Skipping dispatch.")
            return

        self.active_trades += 1
        
        def wrapper():
            start_ts = time.time()
            try:
                result = func(*args, **kwargs)
                rtt = (time.time() - start_ts) * 1000
                LATENCY_MONITOR.add_rtt(rtt)
                log.info(f"Async trade completed. RTT: {rtt:.2f}ms")
                return result
            except Exception as e:
                log.error(f"Async trade failed: {e}")
            finally:
                self.active_trades -= 1

        self.executor.submit(wrapper)

DISPATCHER = TradeDispatcher()
