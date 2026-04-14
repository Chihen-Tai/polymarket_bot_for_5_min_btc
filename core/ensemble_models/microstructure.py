from typing import Dict, Any, List

class MicrostructureModel:
    """
    M2 Model: Evaluates Binance order flow imbalance (OFI) 
    and short-horizon tick dynamics.
    Outputs a probability modifier (-0.1 to +0.1) based on book skew.
    """
    
    def __init__(self, max_skew_penalty: float = 0.05):
        self.max_skew_penalty = max_skew_penalty

    def calculate_skew_modifier(self, bba: Dict[str, Any], depth: int = 3) -> float:
        """
        Calculates a probability modifier based on L2 Order Flow Imbalance.
        A positive modifier means UP probability should increase (bid wall).
        A negative modifier means UP probability should decrease (ask wall).
        """
        if not bba:
            return 0.0
            
        bids = bba.get('b', [])
        asks = bba.get('a', [])
        
        # Format might be list of [price, qty] or dicts depending on ws payload
        # Standardizing payload parser:
        def parse_levels(levels):
            vol = 0.0
            for i, level in enumerate(levels):
                if i >= depth: break
                if isinstance(level, dict):
                    vol += float(level.get('size', level.get('q', 0)))
                elif isinstance(level, list) or isinstance(level, tuple):
                    vol += float(level[1])
            return vol

        if 'B' in bba and 'A' in bba:
            # BBA format from Binance WS stream
            bid_vol = float(bba['B'])
            ask_vol = float(bba['A'])
        else:
            bid_vol = parse_levels(bba.get('b', []))
            ask_vol = parse_levels(bba.get('a', []))
        
        total_vol = bid_vol + ask_vol
        if total_vol < 1e-9:
            return 0.0
            
        imbalance = (bid_vol - ask_vol) / total_vol
        
        # Scale to penalty:
        # e.g. if imbalance is -1.0 (all asks), penalty is -max_skew_penalty
        return float(imbalance * self.max_skew_penalty)

# Singleton instance
M2_MICROSTRUCTURE = MicrostructureModel()
