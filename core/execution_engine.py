from __future__ import annotations
import logging
import time
from typing import List, Dict, Any
from core.config import SETTINGS

log = logging.getLogger(__name__)


def get_vwap_from_ladder(ladder: List[Any], size_usd: float) -> float:
    if not ladder:
        return 999.0

    cumulative_usd = 0.0
    cumulative_shares = 0.0

    for level in ladder:
        try:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price = float(level[0])
                shares = float(level[1])
            elif isinstance(level, dict):
                price = float(level.get('price', 999.0))
                shares = float(level.get('size', 0.0))
            else:
                price = float(getattr(level, 'price', 999.0))
                shares = float(getattr(level, 'size', 0.0))
        except Exception:
            continue

        level_usd = price * shares

        if cumulative_usd + level_usd >= size_usd:
            needed_usd = size_usd - cumulative_usd
            if price > 0:
                cumulative_shares += (needed_usd / price)
                return size_usd / max(cumulative_shares, 1e-9)
            return 999.0

        cumulative_usd += level_usd
        cumulative_shares += shares

    return 999.0


# ---------------------------------------------------------------------------
# Polymarket Fee Model (crypto_fees_v2)
#
# Correct formula:  fee = rate * p^exponent * (1-p)^exponent * shares
# For btc-updown-15m markets:  rate=0.072, exponent=1, takerOnly=True
# With 20% rebate (until 2026-04-30):  effective = rate * (1 - rebateRate)
# Maker fee is always 0 (post-only, confirmed by takerOnly=True).
# ---------------------------------------------------------------------------

_CONSERVATIVE_FALLBACK_RATE = 0.018  # 1.80% at p=0.50 with no rebate
_REBATE_EXPIRY_EPOCH = 1746057600    # 2026-05-01 00:00:00 UTC


class PolymarketFeeModel:
    """Fetches fee schedule from gamma-api, caches for up to 5 minutes."""

    def __init__(self) -> None:
        self.rate: float = _CONSERVATIVE_FALLBACK_RATE
        self.exponent: int = 1
        self.rebate_rate: float = 0.0
        self.taker_only: bool = True
        self._last_fetch: float = 0.0
        self._cache_ttl: float = 300.0  # 5 minutes
        self._fetch_attempted: bool = False

    def refresh(self, market_slug: str | None = None) -> None:
        """Fetch fee schedule from gamma-api for the given market slug."""
        now = time.time()
        if now - self._last_fetch < self._cache_ttl:
            return

        slug = market_slug
        if not slug:
            duration = int(SETTINGS.market_duration_sec)
            base = (int(now) // duration) * duration
            slug = f"{SETTINGS.market_slug_prefix}{base}"

        try:
            from core.http import request_json
            data = request_json(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug},
                timeout=10,
                retries=2,
            )
            if data and isinstance(data, list) and len(data) > 0:
                market = data[0]
                schedule = market.get("feeSchedule") or {}
                if isinstance(schedule, dict) and "rate" in schedule:
                    self.rate = float(schedule["rate"])
                    self.exponent = int(schedule.get("exponent", 1))
                    self.rebate_rate = float(schedule.get("rebateRate", 0.0))
                    self.taker_only = bool(schedule.get("takerOnly", True))
                    self._last_fetch = now
                    self._fetch_attempted = True
                    log.info(
                        "fee schedule refreshed: rate=%.4f exponent=%d rebate=%.2f slug=%s",
                        self.rate, self.exponent, self.rebate_rate, slug,
                    )
                    self._check_rebate_expiry()
                    return
            log.warning("fee fetch returned no data for slug=%s, using fallback", slug)
        except Exception as exc:
            log.warning("fee fetch failed: %s, using fallback rate=%.4f", exc, self.rate)

        if not self._fetch_attempted:
            self.rate = _CONSERVATIVE_FALLBACK_RATE
            self.rebate_rate = 0.0
        self._last_fetch = now

    def _check_rebate_expiry(self) -> None:
        if time.time() >= _REBATE_EXPIRY_EPOCH and self.rebate_rate > 0:
            log.warning(
                "REBATE WINDOW EXPIRED: rebateRate=%.2f still set in API but "
                "the 50%% rebate program ended 2026-04-30. Verify current rebate "
                "status. Setting rebate to 0 as conservative measure.",
                self.rebate_rate,
            )
            self.rebate_rate = 0.0

    @property
    def effective_taker_rate_after_rebate(self) -> float:
        return self.rate * (1.0 - self.rebate_rate)

    def calculate_taker_fee(self, price: float, size_usd: float, secs_left: float = 600.0) -> float:
        """Fee = effective_rate * p^exp * (1-p)^exp * shares."""
        if price <= 0 or price >= 1.0:
            return 0.0
        shares = size_usd / price
        fee = (
            self.effective_taker_rate_after_rebate
            * (price ** self.exponent)
            * ((1.0 - price) ** self.exponent)
            * shares
        )
        return float(fee)

    def calculate_maker_fee(self, price: float, size_usd: float) -> float:
        """Maker fee is 0 for crypto_fees_v2 (takerOnly=True)."""
        return 0.0


# Backward-compatible alias
PolymarketDynamicFeeModel = PolymarketFeeModel

FEE_MODEL = PolymarketFeeModel()

def calculate_committed_edge(
    fair_value: float, 
    ob_up: Dict[str, Any], 
    ob_down: Dict[str, Any], 
    order_size_usd: float, 
    side: str,
    assume_maker: bool = True,
    secs_left: float | None = None
) -> float:
    """
    Calculates execution edge.
    Edge = EV - EntryPrice - Fees - (LatencyBuffer + SlippageBuffer)
    """
    # 1. Determine Entry Price (Maker Best Ask or Taker VWAP)
    if side == "UP":
        asks = ob_up.get('ask_levels', ob_up.get('asks', []))
        if not asks:
            return -1.0
        
        if isinstance(asks[0], (tuple, list)) and len(asks[0]) >= 2:
            top_ask = float(asks[0][0])
        elif isinstance(asks[0], dict):
            top_ask = float(asks[0].get('price', 999.0))
        else:
            top_ask = float(getattr(asks[0], 'price', 999.0))
            
        entry_price = top_ask if assume_maker else get_vwap_from_ladder(asks, order_size_usd)
        if entry_price >= 1.0: return -1.0
        
        # 2. Calculate Fees (dynamic: taker fee escalates near expiry)
        _secs = float(secs_left) if secs_left is not None else 600.0
        if assume_maker:
            entry_fee_rate = FEE_MODEL.calculate_maker_fee(entry_price, order_size_usd) / max(order_size_usd, 1e-9)
        else:
            entry_fee_rate = FEE_MODEL.calculate_taker_fee(entry_price, order_size_usd, _secs) / max(order_size_usd, 1e-9)

        # 3. Apply Buffers for VPN Latency & Micro-Slippage
        # latency_buffer_usd is a fixed cost assumption for stale signals
        latency_cost = float(getattr(SETTINGS, "latency_buffer_usd", 0.01) or 0.01)
        if secs_left is not None and secs_left < 180.0 and entry_price > 0.85:
            latency_cost = 0.0  # Late certainty override

        # slippage_buffer handles taker-sniping fills being worse than quoted
        slippage_cost = 0.005 if not assume_maker else 0.0

        ev_expiry = fair_value
        edge = ev_expiry - entry_price - entry_fee_rate - latency_cost - slippage_cost
    else:
        asks = ob_down.get('ask_levels', ob_down.get('asks', []))
        if not asks:
            return -1.0

        if isinstance(asks[0], (tuple, list)) and len(asks[0]) >= 2:
            top_ask = float(asks[0][0])
        elif isinstance(asks[0], dict):
            top_ask = float(asks[0].get('price', 999.0))
        else:
            top_ask = float(getattr(asks[0], 'price', 999.0))

        entry_price = top_ask if assume_maker else get_vwap_from_ladder(asks, order_size_usd)
        if entry_price >= 1.0: return -1.0

        _secs = float(secs_left) if secs_left is not None else 600.0
        if assume_maker:
            entry_fee_rate = FEE_MODEL.calculate_maker_fee(entry_price, order_size_usd) / max(order_size_usd, 1e-9)
        else:
            entry_fee_rate = FEE_MODEL.calculate_taker_fee(entry_price, order_size_usd, _secs) / max(order_size_usd, 1e-9)

        latency_cost = float(getattr(SETTINGS, "latency_buffer_usd", 0.01) or 0.01)
        if secs_left is not None and secs_left < 180.0 and entry_price > 0.85:
            latency_cost = 0.0  # Late certainty override

        slippage_cost = 0.005 if not assume_maker else 0.0

        ev_expiry = 1.0 - fair_value
        edge = ev_expiry - entry_price - entry_fee_rate - latency_cost - slippage_cost
        
    return float(edge)
