import json
import time
import requests
from typing import Dict, Any, List, Optional
from core.config import SETTINGS

class AIAdvisor:
    def __init__(self):
        self.api_key = getattr(SETTINGS, "ai_api_key", "")
        self.model = getattr(SETTINGS, "ai_advisor_model", "gemini-1.5-flash")
        self.endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        self._last_call_ts = 0
        self._cache = {}

    def get_advisory(self, 
                    market_slug: str,
                    secs_left: float,
                    price_up: float,
                    price_down: float,
                    velocity: float,
                    recent_stats: Optional[Dict] = None) -> Dict[str, Any]:
        
        if not SETTINGS.ai_advisor_enabled or not self.api_key:
            return self._fail_open("AI_DISABLED")

        # Throttle AI calls (e.g., once every 30s per market)
        now = time.time()
        if now - self._last_call_ts < 30:
            return self._cache.get(market_slug, self._fail_open("THROTTLED"))

        prompt = self._build_prompt(market_slug, secs_left, price_up, price_down, velocity, recent_stats)
        
        try:
            resp = requests.post(
                self.endpoint,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"response_mime_type": "application/json"} if SETTINGS.ai_advisor_json_strict else {}
                },
                timeout=SETTINGS.ai_advisor_timeout_sec
            )
            resp.raise_for_status()
            raw_data = resp.json()
            
            # Extract JSON from response
            text_content = raw_data['candidates'][0]['content']['parts'][0]['text']
            advice = json.loads(text_content)
            
            # Validate schema
            required = ["regime", "allow_strategies", "no_trade_bias", "confidence_modifier"]
            if all(k in advice for k in required):
                self._last_call_ts = now
                self._cache[market_slug] = advice
                return advice
            else:
                return self._fail_open("INVALID_SCHEMA")

        except Exception as e:
            return self._fail_open(f"AI_ERROR: {str(e)}")

    def _build_prompt(self, slug, secs_left, p_up, p_down, vel, stats) -> str:
        return f"""
Analyze this Polymarket 15m BTC market.
Market: {slug}
Seconds Left: {secs_left:.1f}
Prices: UP={p_up}, DOWN={p_down}
Velocity: {vel:.6f}
Recent Stats: {json.dumps(stats or {{}})}

Output strict JSON only:
{{
  "regime": "trend|chop|panic|exhaustion|unclear",
  "allow_strategies": ["value_entry", "hybrid_maker", "extreme_fade"],
  "no_trade_bias": boolean,
  "confidence_modifier": float (e.g. -0.02 to 0.02),
  "comment": "brief reason"
}}
"""

    def _fail_open(self, reason: str) -> Dict[str, Any]:
        return {
            "regime": "unclear",
            "allow_strategies": ["value_entry", "hybrid_maker", "extreme_fade"],
            "no_trade_bias": False,
            "confidence_modifier": 0.0,
            "comment": reason
        }

AI_ADVISOR = AIAdvisor()
