import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from core.config import SETTINGS

log = logging.getLogger("learning")

SCORE_FILE = os.path.join(SETTINGS.data_dir, "strategy_scores.json")

# A simple Bayesian-like tracking of wins and losses per strategy
# Prior: We assume every strategy starts with 1 win and 1 loss (50% win rate) to prevent wild early swings.
PRIOR_WINS = 1.0
PRIOR_LOSSES = 1.0
MAX_HISTORY = 100

@dataclass
class TradeOutcome:
    pnl_pct: float
    timestamp: float

class StrategyScoreboard:
    def __init__(self):
        self.history: Dict[str, List[TradeOutcome]] = defaultdict(list)
        self.load()

    def load(self):
        if not os.path.exists(SCORE_FILE):
            return
        try:
            with open(SCORE_FILE, "r") as f:
                data = json.load(f)
                for k, v in data.items():
                    self.history[k] = [TradeOutcome(**item) for item in v]
        except Exception as e:
            log.error(f"Failed to load strategy scores: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(SCORE_FILE), exist_ok=True)
            with open(SCORE_FILE, "w") as f:
                data = {k: [asdict(item) for item in v] for k, v in self.history.items()}
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save strategy scores: {e}")

    def record_outcome(self, strategy_name: str, pnl_pct: float, timestamp: float):
        """Record the outcome of a trade to update the strategy's historical win rate."""
        # Standardize strategy name (remove 'model-' prefix if present)
        strategy_name = strategy_name.replace("model-", "").split("+")[0]
        
        outcome = TradeOutcome(pnl_pct=pnl_pct, timestamp=timestamp)
        self.history[strategy_name].append(outcome)
        
        # Keep only the last MAX_HISTORY trades to auto-adapt to changing market regimes
        if len(self.history[strategy_name]) > MAX_HISTORY:
            self.history[strategy_name] = self.history[strategy_name][-MAX_HISTORY:]
            
        self.save()
        log.info(f"Learning step: {strategy_name} outcome {pnl_pct:.2%} recorded.")

    def get_strategy_score(self, strategy_name: str) -> float:
        """
        Calculate the Bayesian/smoothed win rate for a strategy.
        Win condition: pnl_pct > 0. (Can be adjusted to account for slippage buffering).
        """
        strategy_name = strategy_name.replace("model-", "").split("+")[0]
        trades = self.history.get(strategy_name, [])
        
        wins = PRIOR_WINS
        losses = PRIOR_LOSSES

        # Weight by PnL magnitude: a +50% win counts more than a +1% win.
        # Scale factor 5.0 keeps the prior (1 win + 1 loss) meaningful.
        _SCALE = 5.0
        for t in trades:
            weight = 1.0 + abs(t.pnl_pct) * _SCALE
            if t.pnl_pct > 0.0:
                wins += weight
            else:
                losses += weight

        total = wins + losses
        win_rate = wins / total
        return win_rate

    def get_best_strategy(self, available_strategies: Dict[str, dict]) -> Optional[dict]:
        """
        Given a dictionary of proposed signals {strategy_name: decision_dict},
        evaluate their historical scores and return the highest-scoring decision.
        """
        if not available_strategies:
            return None
            
        best_strategy = None
        best_score = -1.0
        
        for strat_name, decision in available_strategies.items():
            if not decision.get("ok"):
                continue
                
            score = self.get_strategy_score(strat_name)
            log.info(f"Evaluating strategy candidate: {strat_name} | Expected Win-Rate: {score:.1%}")
            
            if score > best_score:
                best_score = score
                best_strategy = strat_name
                
        if best_strategy:
            log.info(f"Adaptive Learning Selected: [{best_strategy}] with score {best_score:.1%}")
            return available_strategies[best_strategy]
            
        return None

# Global instance
SCOREBOARD = StrategyScoreboard()
