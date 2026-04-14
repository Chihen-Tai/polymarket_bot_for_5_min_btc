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
        and blends it with the M2 Microstructure (OFI).
        
        Prioritizes Microstructure (OFI) for the 15m horizon.
        """
        if base_probability is None:
            return 0.5
            
        m1_weight = 0.35  # Theoretical Weight
        m2_weight = 0.65  # Microstructure (OFI) Weight
        
        # M2: Microstructure Order Flow Imbalance
        if ws_bba:
            # We treat the OFI modifier as a directional probability shift relative to equilibrium (0.5)
            # and then blend it with the BS probability.
            skew_modifier = M2_MICROSTRUCTURE.calculate_skew_modifier(ws_bba)
            # Equivalent OFI Probability
            ofi_prob = 0.5 + skew_modifier 
            
            # Weighted Blend
            blended_prob = (base_probability * m1_weight) + (ofi_prob * m2_weight)
            return max(0.01, min(0.99, blended_prob))
            
        # Fallback to M1 only
        return max(0.01, min(0.99, base_probability))

ENSEMBLE = EnsembleAggregator()
