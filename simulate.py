import os
import sys
import json
import time
from collections import deque
import requests
from config import SETTINGS
from exchange import PolymarketExchange
from decision_engine import explain_choose_side, check_arbitrage

def main():
    slug = "btc-updown-5m-1773985500"
    print(f"=== SIMULATING MARKET: {slug} ===")
    
    # 1. Fetch market info from Gamma API
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}")
        data = r.json()
        if not data:
            print("ERROR: Market event not found or has expired entirely.")
            return
            
        event = data[0]
        markets = event.get("markets", [])
        if not markets:
            print("ERROR: No markets in this event.")
            return
            
        market = markets[0]
        print(f"Market parsed: {market.get('question')} | Active: {market.get('active')} | Closed: {market.get('closed')}")
        
    except Exception as e:
        print(f"API Error: {e}")
        return

    # 2. Extract Token IDs
    tokens = market.get("clobTokenIds", [])
    if len(tokens) < 2:
        print("ERROR: Not enough token IDs.")
        return
        
    token_up = str(tokens[0])
    token_down = str(tokens[1])
    
    market["token_up"] = token_up
    market["token_down"] = token_down

    # Initialize exchange
    ex = PolymarketExchange(dry_run=True)
    
    # 3. Fetch Binance 1m candle (Oracle)
    print("\n--- Fetching Oracle (Binance) ---")
    binance_1m = ex.get_binance_1m_candle()
    print(f"Binance 1m Candle: {binance_1m}")

    # 4. Fetch Orderbook Imbalance
    print("\n--- Fetching Full Orderbooks ---")
    ob_up = ex.get_full_orderbook(token_up)
    ob_down = ex.get_full_orderbook(token_down)
    print(f"UP Book: {ob_up}")
    print(f"DOWN Book: {ob_down}")
    
    # Fake some history for mean reversion
    from decision_engine import get_outcome_prices
    prices = get_outcome_prices(market)
    up_price = prices.get("up") or 0.5
    down_price = prices.get("down") or 0.5
    print(f"\nCurrent Market Price -> UP: {up_price}, DOWN: {down_price}")
    
    yes_window = deque([up_price]*10, maxlen=10)
    up_window = deque([up_price]*10, maxlen=10)
    down_window = deque([down_price]*10, maxlen=10)

    # 5. Check Arbitrage
    print("\n--- Checking Arbitrage ---")
    has_arb = check_arbitrage(up_price, down_price)
    print(f"Arbitrage Triggered? {has_arb}")

    # 6. Run Decision Engine
    print("\n--- Running Decision Engine ---")
    decision = explain_choose_side(
        market=market,
        yes_window=yes_window,
        up_window=up_window,
        down_window=down_window,
        binance_1m=binance_1m,
        ob_up=ob_up,
        ob_down=ob_down
    )
    
    print(json.dumps(decision, indent=2))
    
    if decision.get("ok"):
        print(f"\n✅ BOT WOULD BUY: {decision.get('side')} at {decision.get('entry_price')}")
        print(f"Reason: {decision.get('reason')}")
    else:
        print(f"\n❌ BOT WOULD SKIP. Reason: {decision.get('reason')}")

if __name__ == "__main__":
    main()
