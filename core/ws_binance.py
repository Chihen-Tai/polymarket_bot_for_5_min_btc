import json
import logging
import threading
import time
import websocket
from collections import deque

logger = logging.getLogger("ws_binance")

class BinanceWebSocket:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(BinanceWebSocket, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, symbol: str = "btcusdt"):
        if getattr(self, "_initialized", False):
            return
        self.symbol = symbol.lower()
        self.ws_url = f"wss://fstream.binance.com/stream?streams={self.symbol}@bookTicker/{self.symbol}@aggTrade/{self.symbol}@forceOrder"
        
        # Thread-safe states
        self.bba = {"b": 0.0, "B": 0.0, "a": 0.0, "A": 0.0, "ts": 0.0, "u": 0} 
        self.bba_history = deque(maxlen=2000)
        self.trades = deque(maxlen=5000)
        self.recent_prices = deque(maxlen=200)
        self.liquidations = deque(maxlen=200)
        self.last_event_latency_ms = 0.0
        self.last_bba_event_latency_ms = 0.0
        self.ws = None
        self.thread = None
        self.running = False
        self._initialized = True

    def _on_message(self, ws, message):
        try:
            payload = json.loads(message)
            stream = payload.get("stream", "")
            data = payload.get("data", {})
            recv_ms = time.time() * 1000.0
            event_ms = float(data.get("E") or data.get("T") or 0.0)
            if event_ms > 0.0:
                self.last_event_latency_ms = max(0.0, recv_ms - event_ms)
            
            if stream.endswith("@bookTicker"):
                if data.get("u", 0) > self.bba["u"]: # Ensure we don't process stale updates out of order
                    self.bba["b"] = float(data.get("b", 0))  # Best Bid Price
                    self.bba["B"] = float(data.get("B", 0))  # Best Bid Qty
                    self.bba["a"] = float(data.get("a", 0))  # Best Ask Price
                    self.bba["A"] = float(data.get("A", 0))  # Best Ask Qty
                    self.bba["u"] = data.get("u", 0)         # Orderbook update ID
                    self.bba["ts"] = time.time()
                    if event_ms > 0.0:
                        self.last_bba_event_latency_ms = max(0.0, recv_ms - event_ms)
                    self.bba_history.append(self.bba.copy())
                    
                    if self.bba["b"] > 0 and self.bba["a"] > 0:
                        self.recent_prices.append((self.bba["ts"], (self.bba["b"] + self.bba["a"]) / 2.0))
            
            elif stream.endswith("@aggTrade"):
                # p = price, q = qty, m = is_buyer_maker (True = market sell, False = market buy)
                self.trades.append({
                    "p": float(data.get("p", 0)),
                    "q": float(data.get("q", 0)),
                    "m": bool(data.get("m", False)),
                    "ts": time.time()
                })
                
            elif stream.endswith("@forceOrder"):
                # "S": "SELL" means a long position was liquidated (downward spike).
                # "S": "BUY" means a short position was liquidated (upward spike).
                o = data.get("o", {})
                if o:
                    price = float(o.get("p", 0.0))
                    qty = float(o.get("q", 0.0))
                    usd_size = price * qty
                    side = o.get("S", "")
                    if usd_size > 0:
                        self.liquidations.append({
                            "side": side,
                            "usd_size": usd_size,
                            "p": price,
                            "ts": time.time()
                        })
        except Exception as e:
            logger.debug(f"WS Msg Parse Error: {e}")

    def _on_error(self, ws, error):
        logger.error(f"Binance WS Error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning("Binance WS Closed.")

    def _on_open(self, ws):
        logger.info(f"Binance WS Connected to {self.symbol} streams.")

    def _connect(self):
        # We run the websocket blockingly in a safe loop
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WebSocket fatal error: {e}")
            
            if self.running:
                logger.warning("WS disconnected unexpectedly, retrying in 3s...")
                time.sleep(3)

    def start(self):
        if not self.running:
            self.running = True
            logger.info("Starting Binance WebSocket Daemon...")
            self.thread = threading.Thread(target=self._connect, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()

    def get_bba(self, lag_sec: float = 0.0):
        if lag_sec <= 0.0 or not self.bba_history:
            return self.bba.copy()

        cutoff = time.time() - lag_sec
        chosen = None
        for snap in reversed(self.bba_history):
            if snap.get("ts", 0.0) <= cutoff:
                chosen = snap
                break
        return (chosen or self.bba).copy()

    def get_recent_trades(self, seconds: float = 60.0, lag_sec: float = 0.0):
        upper = time.time() - max(0.0, lag_sec)
        cutoff = upper - seconds
        # Return a snap of recent trades within `seconds` timeout
        snapshot = list(self.trades)
        return [t for t in snapshot if cutoff <= t["ts"] <= upper]

    def get_price_velocity(self, seconds: float = 3.0, lag_sec: float = 0.0) -> float:
        """Returns the percentage change of the mid-price over the last X seconds.
        Returns 0.0 if the most recent tick is older than the requested window
        (stale / disconnected WebSocket), preventing misleading velocity signals.
        """
        if not self.recent_prices:
            return 0.0
        now = time.time() - max(0.0, lag_sec)
        snapshot = list(self.recent_prices)

        # Only consider samples that are not newer than the requested lag.
        eligible = [(ts, price) for ts, price in snapshot if ts <= now]
        if not eligible:
            return 0.0

        # Guard: if the newest tick is itself outside the window, data is stale
        if now - eligible[-1][0] > seconds:
            return 0.0

        # Find the OLDEST price still within the time window
        oldest_price = None
        for ts, price in eligible:  # oldest to newest
            if now - ts <= seconds:
                oldest_price = price
                break

        if not oldest_price:
            return 0.0

        current_price = eligible[-1][1]  # newest eligible price
        return (current_price - oldest_price) / oldest_price

    def get_recent_prices_window(self, seconds: float = 5.0, lag_sec: float = 0.0) -> tuple[float | None, float | None]:
        """Returns the oldest and newest mid-price in the requested time window up to strictly now."""
        if not self.recent_prices:
            return None, None
        now = time.time() - max(0.0, lag_sec)
        snapshot = list(self.recent_prices)
        eligible = [(ts, price) for ts, price in snapshot if ts <= now]
        if not eligible:
            return None, None

        oldest_price = None
        for ts, price in eligible:
            if now - ts <= seconds:
                oldest_price = price
                break

        if oldest_price is None:
            return None, None
            
        newest_price = eligible[-1][1]
        return oldest_price, newest_price

    def get_last_update_age(self) -> float:
        """Returns seconds since last WS price tick. Used to detect stale/disconnected state."""
        if not self.recent_prices:
            return float('inf')
        return time.time() - self.recent_prices[-1][0]

    def get_last_event_latency_ms(self) -> float:
        if self.last_bba_event_latency_ms > 0.0:
            return self.last_bba_event_latency_ms
        return self.last_event_latency_ms

    def get_bba_age_ms(self) -> float:
        ts = float(self.bba.get("ts", 0.0) or 0.0)
        if ts <= 0.0:
            return float("inf")
        return max(0.0, (time.time() - ts) * 1000.0)

    def get_recent_liquidations(self, seconds: float = 20.0) -> list[dict]:
        """Returns all liquidations that occurred within the last `seconds`."""
        if not self.liquidations:
            return []
        cutoff = time.time() - seconds
        snapshot = list(self.liquidations)
        return [lq for lq in snapshot if lq["ts"] >= cutoff]


# Global singleton instance
BINANCE_WS = BinanceWebSocket("btcusdt")
