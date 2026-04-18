import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
import numpy as np

from core.config import SETTINGS

log = logging.getLogger("learning")

SCORE_FILE = os.path.join(SETTINGS.data_dir, "strategy_scores.json")

# Prior for expectancy: 1 small win, 1 small loss to prevent early wild swings
PRIOR_TRADES = 2.0
PRIOR_EXPECTANCY = 0.0
MAX_HISTORY = 200

@dataclass
class TradeOutcome:
    fee_adjusted_pnl_pct: float
    timestamp: float
    execution_style: str = "unknown"
    price_bucket: str = "unknown"
    secs_left_bucket: str = "unknown"

class StrategyScoreboard:
    def __init__(self):
        self.history: Dict[str, List[TradeOutcome]] = defaultdict(list)
        self.load()

    def _normalized_name(self, strategy_name: str) -> str:
        return strategy_name.replace("model-", "").split("+")[0]

    def _neutral_band(self) -> float:
        return max(0.0, float(getattr(SETTINGS, "scoreboard_neutral_pnl_pct", 0.001)))

    def load(self):
        if not os.path.exists(SCORE_FILE):
            return
        try:
            with open(SCORE_FILE, "r") as f:
                data = json.load(f)
                for k, v in data.items():
                    # Handle migration from old format
                    parsed = []
                    for item in v:
                        if "pnl_pct" in item and "fee_adjusted_pnl_pct" not in item:
                            item["fee_adjusted_pnl_pct"] = item.pop("pnl_pct")
                        parsed.append(TradeOutcome(**item))
                    self.history[k] = parsed
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

    def record_outcome(
        self, 
        strategy_name: str, 
        fee_adjusted_pnl_pct: float, 
        timestamp: float,
        execution_style: str = "unknown",
        price_bucket: str = "unknown",
        secs_left_bucket: str = "unknown"
    ):
        strategy_name = self._normalized_name(strategy_name)
        
        outcome = TradeOutcome(
            fee_adjusted_pnl_pct=fee_adjusted_pnl_pct, 
            timestamp=timestamp,
            execution_style=execution_style,
            price_bucket=price_bucket,
            secs_left_bucket=secs_left_bucket
        )
        self.history[strategy_name].append(outcome)
        
        if len(self.history[strategy_name]) > MAX_HISTORY:
            self.history[strategy_name] = self.history[strategy_name][-MAX_HISTORY:]
            
        self.save()

    def get_strategy_expectancy(self, strategy_name: str) -> float:
        """
        Calculate the Bayesian smoothed fee-adjusted expectancy for a strategy.
        Returns the expected fee-adjusted PnL percentage per trade.
        """
        strategy_name = self._normalized_name(strategy_name)
        trades = self.history.get(strategy_name, [])

        decay = float(getattr(SETTINGS, "scoreboard_decay_factor", 0.95))
        
        total_weight = PRIOR_TRADES
        weighted_sum = PRIOR_EXPECTANCY * PRIOR_TRADES
        
        decay_power = 0
        for t in reversed(trades):
            decay_mult = (decay ** decay_power)
            weight = 1.0 * decay_mult
            
            weighted_sum += t.fee_adjusted_pnl_pct * weight
            total_weight += weight
                
            decay_power += 1

        return weighted_sum / total_weight

    def get_strategy_score(self, strategy_name: str) -> float:
        """
        For backward compatibility, returns a normalized score [0.01, 0.99] based on expectancy.
        0 expectancy -> 0.5 score.
        +5% expectancy -> ~0.9 score.
        -5% expectancy -> ~0.1 score.
        """
        expectancy = self.get_strategy_expectancy(strategy_name)
        # Sigmoid-like normalization
        score = 1.0 / (1.0 + np.exp(-expectancy * 40.0)) # scale factor
        return min(0.99, max(0.01, float(score)))

    def get_strategy_stats(self, strategy_name: str) -> dict:
        strategy_name = self._normalized_name(strategy_name)
        trades = self.history.get(strategy_name, [])
        if not trades:
            return {"expectancy": 0.0, "win_rate": 0.5, "count": 0, "profit_factor": 1.0, "avg_pnl": 0.0}
        
        pnls = [t.fee_adjusted_pnl_pct for t in trades]
        wins = [p for p in pnls if p > self._neutral_band()]
        losses = [p for p in pnls if p < -self._neutral_band()]
        
        win_rate = len(wins) / len(pnls) if pnls else 0.5
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 1.0)
        
        return {
            "expectancy": self.get_strategy_expectancy(strategy_name),
            "win_rate": win_rate,
            "count": len(pnls),
            "profit_factor": profit_factor,
            "avg_pnl": sum(pnls) / len(pnls)
        }

    def get_bayesian_win_rate(self, strategy_name: str) -> tuple[float, float]:
        """
        Bayesian posterior win rate using Beta(alpha, beta) conjugate prior.
        Prior: Beta(1, 1) = uniform.
        Returns (posterior_mean, credible_interval_lower_bound) where
        lower bound is the 5th percentile of the posterior.
        """
        from scipy.stats import beta as beta_dist

        strategy_name = self._normalized_name(strategy_name)
        trades = self.history.get(strategy_name, [])
        neutral_band = self._neutral_band()

        # Count wins and losses (ignoring neutral trades)
        wins = sum(1 for t in trades if t.fee_adjusted_pnl_pct > neutral_band)
        losses = sum(1 for t in trades if t.fee_adjusted_pnl_pct < -neutral_band)

        # Beta posterior: prior Beta(1,1) + data
        alpha = 1.0 + wins
        beta_param = 1.0 + losses

        posterior_mean = alpha / (alpha + beta_param)
        lower_bound = float(beta_dist.ppf(0.05, alpha, beta_param))

        return posterior_mean, lower_bound

    def get_strategy_trade_count(self, strategy_name: str) -> int:
        strategy_name = self._normalized_name(strategy_name)
        return len(self.history.get(strategy_name, []))

    def get_strategy_decisive_trade_count(self, strategy_name: str) -> int:
        strategy_name = self._normalized_name(strategy_name)
        neutral_band = self._neutral_band()
        return sum(1 for trade in self.history.get(strategy_name, []) if abs(trade.fee_adjusted_pnl_pct) > neutral_band)


SCOREBOARD = StrategyScoreboard()
