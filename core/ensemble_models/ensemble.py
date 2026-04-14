from typing import Dict, Any, Optional, List
from core.ensemble_models.microstructure import M2_MICROSTRUCTURE

class EnsembleAggregator:
    """
    Central Orchestrator for the Multi-Model Probability Ensemble.
    Combines Model 1 (M1 - Black Scholes Theoretical Base) 
    and Model 2 (M2 - Order Flow Imbalance Modifier).
    """

    @staticmethod
    def get_calibrated_fair_value(
        base_probability: float,
        ws_bba: Optional[Dict[str, Any]] = None
    ) -> float:
        """
        Takes the uncalibrated M1 base probability (theoretical BS) 
        and applies dynamic empirical modifiers.
        """
        if base_probability is None:
            return 0.5
            
        prob = base_probability
        
        # M2: Microstructure Order Flow Imbalance
        if ws_bba:
            skew_modifier = M2_MICROSTRUCTURE.calculate_skew_modifier(ws_bba)
            prob += skew_modifier
            
        # Clamp to bounds
        return max(0.01, min(0.99, prob))

ENSEMBLE = EnsembleAggregator()
