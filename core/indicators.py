from __future__ import annotations

def lsma(prices: list[float]) -> float:
    """Least Squares Moving Average (Linear Regression at the last point)"""
    n = len(prices)
    if n == 0:
        return 0.0
    sum_x = n * (n - 1) / 2
    sum_x2 = (n - 1) * n * (2 * n - 1) / 6
    sum_y = sum(prices)
    sum_xy = sum(i * p for i, p in enumerate(prices))
    
    denominator = (n * sum_x2 - sum_x * sum_x)
    if denominator == 0:
        return prices[-1]
        
    m = (n * sum_xy - sum_x * sum_y) / denominator
    b = (sum_y - m * sum_x) / n
    return m * (n - 1) + b

def calc_zlsma(prices: list[float], length: int = 50) -> float | None:
    """Zero Lag LSMA implemented from scratch for 50 periods"""
    required_len = length * 2 - 1
    if len(prices) < required_len:
        return None
        
    # Standard LSMA array over the last `length` periods
    lsma_vals = []
    for i in range(len(prices) - length + 1):
        window = prices[i : i + length]
        lsma_vals.append(lsma(window))
        
    lsma1 = lsma_vals[-1]
    lsma2 = lsma(lsma_vals[-length:])
    
    return lsma1 + (lsma1 - lsma2)

def calc_atr(high: float, low: float, prev_close: float) -> float:
    """True Range calculation"""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))

def calc_chandelier_exit(klines: list[dict], atr_period: int = 1, mult: float = 2.0) -> int:
    """
    Simulates Chandelier Exit with ATR(1).
    Returns 1 if the current state is LONG, -1 if SHORT.
    """
    if len(klines) < 2:
        return 1
        
    direction = 1
    long_stop = klines[0]['low']
    short_stop = klines[0]['high']
    
    for i in range(1, len(klines)):
        curr = klines[i]
        prev = klines[i-1]
        
        atr = calc_atr(curr['high'], curr['low'], prev.get('close', curr['open']))
        hh = curr['high']
        ll = curr['low']
        
        new_long_stop = hh - mult * atr
        new_short_stop = ll + mult * atr
        
        if prev.get('close', 0) > long_stop:
            long_stop = max(long_stop, new_long_stop)
        else:
            long_stop = new_long_stop
            
        if prev.get('close', 0) < short_stop:
            short_stop = min(short_stop, new_short_stop)
        else:
            short_stop = new_short_stop
            
            
        if direction == 1 and curr['close'] < long_stop:
            direction = -1
        elif direction == -1 and curr['close'] > short_stop:
            direction = 1
            
    return direction

def compute_cvd(trades: list[dict]) -> float:
    """
    Computes Cumulative Volume Delta (CVD) from a list of aggTrades.
    Trade format: {"p": price, "q": qty, "m": is_buyer_maker, "ts": timestamp}
    If `m` is True, it's a market SELL hitting a maker bid.
    """
    cvd = 0.0
    for t in trades:
        vol = t['p'] * t['q']
        if t['m']:
            cvd -= vol  # Market sell
        else:
            cvd += vol  # Market buy
    return cvd

def compute_buy_sell_pressure(trades: list[dict]) -> tuple[float, float]:
    """
    Computes total buy volume vs total sell volume (in USD equivalent).
    Returns (buy_vol, sell_vol).
    """
    buy_vol = sum(t['p'] * t['q'] for t in trades if not t['m'])
    sell_vol = sum(t['p'] * t['q'] for t in trades if t['m'])
    return buy_vol, sell_vol

def calc_rsi(prices: list[float], period: int = 14) -> float | None:
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calc_ema(prices: list[float], period: int) -> list[float]:
    if not prices:
        return []
    k = 2 / (period + 1)
    ema = [prices[0]]
    for p in prices[1:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

def calc_macd(prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    if len(prices) < slow + signal:
        return None
    ema_fast = calc_ema(prices, fast)
    ema_slow = calc_ema(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = calc_ema(macd_line, signal)
    if not macd_line or not signal_line:
        return None
    histogram = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], histogram
