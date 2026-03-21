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
        self.ws_url = f"wss://fstream.binance.com/stream?streams={self.symbol}@bookTicker/{self.symbol}@aggTrade"
        
        # Thread-safe states
        self.bba = {"b": 0.0, "B": 0.0, "a": 0.0, "A": 0.0, "ts": 0.0, "u": 0} 
        self.trades = deque(maxlen=5000)
        self.recent_prices = deque(maxlen=200)
        self.ws = None
        self.thread = None
        self.running = False
        self._initialized = True

    def _on_message(self, ws, message):
        try:
            payload = json.loads(message)
            stream = payload.get("stream", "")
            data = payload.get("data", {})
            
            if stream.endswith("@bookTicker"):
                if data.get("u", 0) > self.bba["u"]: # Ensure we don't process stale updates out of order
                    self.bba["b"] = float(data.get("b", 0))  # Best Bid Price
                    self.bba["B"] = float(data.get("B", 0))  # Best Bid Qty
                    self.bba["a"] = float(data.get("a", 0))  # Best Ask Price
                    self.bba["A"] = float(data.get("A", 0))  # Best Ask Qty
                    self.bba["u"] = data.get("u", 0)         # Orderbook update ID
                    self.bba["ts"] = time.time()
                    
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

    def get_bba(self):
        return self.bba.copy()

    def get_recent_trades(self, seconds: float = 60.0):
        cutoff = time.time() - seconds
        # Return a snap of recent trades within `seconds` timeout
        snapshot = list(self.trades)
        return [t for t in snapshot if t["ts"] >= cutoff]

    def get_price_velocity(self, seconds: float = 3.0) -> float:
        """Returns the percentage change of the mid-price over the last X seconds."""
        if not self.recent_prices:
            return 0.0
        now = time.time()
        oldest_price = None
        for ts, price in self.recent_prices:
            if now - ts <= seconds:
                oldest_price = price
                break
        
        if not oldest_price:
            return 0.0
        
        current_price = self.recent_prices[-1][1]
        return (current_price - oldest_price) / oldest_price


# Global singleton instance
BINANCE_WS = BinanceWebSocket("btcusdt")
