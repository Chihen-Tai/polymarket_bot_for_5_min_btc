import asynchat, asyncio
from core.exchange import CLOB_API
import json

async def main():
    try:
        from core.market_resolver import fetch_market_info
        market = fetch_market_info("btc-updown-5m-1775354400")
        print(json.dumps(market, indent=2))
        
        # Test my extraction function
        from core.decision_engine import _extract_strike_price
        strike = _extract_strike_price(market.get("question", ""))
        print(f"STRIKE PRICE: {strike}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == '__main__':
    asyncio.run(main())
