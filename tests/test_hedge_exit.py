import pytest
from core.exchange import PolymarketExchange, estimate_book_exit_value, estimate_hedge_exit_value

def test_estimate_hedge_exit_value():
    """Test the math logic for estimating a hedge exit via opposite token ASK."""
    # Opposite token orderbook (want to buy, so we hit ASKs)
    book_opposite = {
        "ask_levels": [
            [0.60, 50.0],
            [0.65, 100.0],
        ]
    }
    
    # We want to hedge 60 shares
    # We will buy 50 shares at 0.60 (Cost = 30)
    # We will buy 10 shares at 0.65 (Cost = 6.5)
    # Total shares filled = 60
    # Total cost = 36.5
    # Equivalent exit yield = shares - total_cost = 60 - 36.5 = 23.5
    yield_val, fill_ratio = estimate_hedge_exit_value(book_opposite, 60.0)
    assert fill_ratio == 1.0
    assert abs(yield_val - 23.5) < 1e-6

    # Test partial fill
    # We want to buy 200 shares
    # Buy 50 at 0.60 (Cost = 30)
    # Buy 100 at 0.65 (Cost = 65)
    # Total cost = 95
    # Filled = 150
    # Equivalent exit yield = 150 - 95 = 55
    # Fill ratio = 150 / 200 = 0.75
    yield_val_partial, fill_ratio_partial = estimate_hedge_exit_value(book_opposite, 200.0)
    assert abs(fill_ratio_partial - 0.75) < 1e-6
    assert abs(yield_val_partial - 55.0) < 1e-6

def test_dry_run_hedge_mode():
    """Test dry-run close_position uses hedge_mode properly to lock in value."""
    ex = PolymarketExchange(dry_run=True)
    # Give some initial balance
    ex._cash = 1000.0
    ex._open_exposure = 100.0
    
    token_id = "token_yes_123"
    opp_token = "token_no_456"
    
    # We hold 100 shares of token_yes_123, cost was $90
    ex._position_shares[token_id] = 100.0
    ex._position_cost[token_id] = 90.0
    
    # Mocking orderbook internally via patch or injecting into a mock book dictionary if supported
    # In PolymarketExchange dry_run, if simulated_price is None, it uses get_full_orderbook
    # Let's mock get_full_orderbook
    
    class MockExchange(PolymarketExchange):
        def get_full_orderbook(self, token):
            if token == opp_token:
                return {
                    "best_ask": 0.60  # Hedge cost
                }
            return {"best_bid": 0.20} # Sell value is terrible
            
    ex = MockExchange(dry_run=True)
    ex._cash = 1000.0
    ex._open_exposure = 90.0
    ex._position_shares[token_id] = 100.0
    ex._position_cost[token_id] = 90.0
    
    # Hedge mode
    resp = ex.close_position(
        token_id=token_id, 
        shares=100.0, 
        hedge_mode=True, 
        opposite_token_id=opp_token
    )
    
    assert resp["ok"] is True
    # We hedged 100 shares. Best ask of opposite is 0.60.
    # Cost to hedge = 100 * 0.60 = 60.0
    # Net exit yield = 100 - 60 = 40.0
    assert resp["actual_exit_value_usd"] == 40.0
    
    # Original balance 1000. Spent 60 to hedge. Balance = 940
    assert ex._get_cash_balance() == 940.0
    
    # Position conceptually gone
    assert ex.get_position(token_id) is None
