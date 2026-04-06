from __future__ import annotations

from dataclasses import dataclass
import math
from random import uniform
from typing import Any
import time
import os
import json
import re
import requests

from core.config import SETTINGS


@dataclass
class Account:
    equity: float
    cash: float
    open_exposure: float


@dataclass
class Position:
    token_id: str
    size: float
    avg_price: float
    initial_value: float
    current_value: float
    cash_pnl: float
    percent_pnl: float


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _limit_order_type(order_type_cls: Any):
    """Support both old POST_ONLY enums and current py-clob-client GTC limit orders."""
    return getattr(order_type_cls, "POST_ONLY", getattr(order_type_cls, "GTC", None))


def estimate_order_shares(amount_usd: float, price: float) -> float:
    if price <= 0:
        return 0.0
    return round(float(amount_usd) / float(price), 2)


def _round_up_to_step(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return math.ceil((float(value) - 1e-12) / float(step)) * float(step)


def minimum_order_usd(price: float, min_shares: float) -> float:
    if price <= 0 or min_shares <= 0:
        return 0.0
    return round(float(price) * float(min_shares), 4)


def order_below_minimum_shares(amount_usd: float, price: float, min_shares: float) -> tuple[bool, float, float]:
    est_shares = estimate_order_shares(amount_usd, price)
    required_usd = minimum_order_usd(price, min_shares)
    return est_shares + 1e-9 < float(min_shares), est_shares, required_usd


def plan_live_order(amount_usd: float, price: float, min_shares: float, min_order_usd: float) -> tuple[float, float]:
    if price <= 0:
        return 0.0, 0.0
    required_shares = max(
        float(amount_usd) / float(price),
        float(min_shares) if min_shares > 0 else 0.0,
        float(min_order_usd) / float(price) if min_order_usd > 0 else 0.0,
    )
    rounded_shares = round(_round_up_to_step(required_shares, 0.01), 2)
    actual_usd = round(rounded_shares * float(price), 4)
    return rounded_shares, actual_usd


_BALANCE_ALLOWANCE_RE = re.compile(
    r"balance:\s*(?P<balance>\d+)\s*,\s*order amount:\s*(?P<amount>\d+)",
    re.IGNORECASE,
)
MIN_LIVE_CLOSE_SHARES = 0.01


def parse_balance_allowance_available_shares(error_text: str) -> float | None:
    text = str(error_text or "")
    match = _BALANCE_ALLOWANCE_RE.search(text)
    if not match:
        return None
    balance_units = _to_float(match.group("balance"), 0.0)
    if balance_units <= 0.0:
        return 0.0
    # Polymarket errors report share balances in 1e6 precision units.
    return balance_units / 1_000_000.0


def select_live_close_exit_value(
    *,
    usdc_received_total: float | None,
    usdc_received_source: str,
    cash_delta: float | None,
    cash_delta_source: str,
) -> tuple[float | None, str]:
    cash_value = float(cash_delta) if cash_delta is not None else 0.0
    response_value = float(usdc_received_total) if usdc_received_total is not None else 0.0
    if cash_value > 0.0 and response_value > 0.0:
        # Balance refresh can lag on live partial exits; when the wallet delta and
        # matched-order proceeds disagree materially, trust the order response.
        agreement_tolerance = max(0.05, response_value * 0.15)
        if abs(cash_value - response_value) <= agreement_tolerance:
            return cash_value, (cash_delta_source or "cash_balance_delta")
        return response_value, (usdc_received_source or "close_response_takingAmount")
    if cash_value > 0.0:
        return cash_value, (cash_delta_source or "cash_balance_delta")
    if response_value > 0.0:
        return response_value, (usdc_received_source or "close_response_takingAmount")
    return (cash_delta if cash_delta is not None else usdc_received_total), (
        cash_delta_source if cash_delta is not None else (usdc_received_source or "close_response_unavailable")
    )


def _normalize_book_levels(raw_levels: Any, *, reverse: bool) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    for lv in (raw_levels if isinstance(raw_levels, list) else []):
        if isinstance(lv, dict):
            price = _to_float(lv.get("price"), 0.0)
            size = _to_float(lv.get("size", lv.get("amount", 0.0)), 0.0)
        elif hasattr(lv, "price") or hasattr(lv, "size") or hasattr(lv, "amount"):
            price = _to_float(getattr(lv, "price", 0.0), 0.0)
            size = _to_float(getattr(lv, "size", getattr(lv, "amount", 0.0)), 0.0)
        elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
            price = _to_float(lv[0], 0.0)
            size = _to_float(lv[1], 0.0)
        else:
            price, size = 0.0, 0.0
        if price > 0.0 and size > 0.0:
            levels.append((price, size))
    levels.sort(key=lambda item: item[0], reverse=reverse)
    return levels


def _normalize_orderbook_summary(raw_book: Any) -> dict:
    if isinstance(raw_book, dict):
        return raw_book
    if raw_book is None:
        return {}

    for dumper in ("model_dump", "dict"):
        fn = getattr(raw_book, dumper, None)
        if callable(fn):
            try:
                dumped = fn()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                return dumped

    fields = (
        "market",
        "asset_id",
        "timestamp",
        "bids",
        "asks",
        "min_order_size",
        "neg_risk",
        "tick_size",
        "last_trade_price",
        "hash",
    )
    normalized = {field: getattr(raw_book, field) for field in fields if hasattr(raw_book, field)}
    if normalized:
        return normalized

    try:
        raw_dict = vars(raw_book)
    except Exception:
        raw_dict = None
    if isinstance(raw_dict, dict):
        return {str(key): value for key, value in raw_dict.items() if not str(key).startswith("_")}
    return {}


def estimate_book_exit_value(book: dict | None, shares: float) -> tuple[float | None, float]:
    target_shares = max(0.0, float(shares or 0.0))
    if target_shares <= 0.0:
        return 0.0, 1.0
    if not isinstance(book, dict):
        return None, 0.0

    bid_levels = _normalize_book_levels(book.get("bid_levels"), reverse=True)
    if bid_levels:
        remaining = target_shares
        realized_value = 0.0
        filled_shares = 0.0
        for price, size in bid_levels:
            take = min(remaining, size)
            if take <= 0.0:
                continue
            realized_value += take * price
            filled_shares += take
            remaining -= take
            if remaining <= 1e-9:
                break
        fill_ratio = min(1.0, filled_shares / target_shares)
        return realized_value, fill_ratio

    best_bid = _to_float(book.get("best_bid"), 0.0)
    if best_bid <= 0.0:
        return 0.0, 0.0
    best_bid_size = _to_float(book.get("best_bid_size"), 0.0)
    if best_bid_size > 0.0:
        filled_shares = min(target_shares, best_bid_size)
        fill_ratio = min(1.0, filled_shares / target_shares)
        return filled_shares * best_bid, fill_ratio
    return target_shares * best_bid, 1.0


def estimate_book_exit_floor_price(book: dict | None, shares: float) -> float | None:
    target_shares = max(0.0, float(shares or 0.0))
    if target_shares <= 0.0:
        return 0.0
    if not isinstance(book, dict):
        return None

    bid_levels = _normalize_book_levels(book.get("bid_levels"), reverse=True)
    if bid_levels:
        remaining = target_shares
        floor_price = None
        filled_shares = 0.0
        for price, size in bid_levels:
            take = min(remaining, size)
            if take <= 0.0:
                continue
            floor_price = price
            filled_shares += take
            remaining -= take
            if remaining <= 1e-9:
                break
        fill_ratio = min(1.0, filled_shares / target_shares)
        if floor_price is not None and fill_ratio >= 0.999:
            return float(floor_price)
        return None

    best_bid = _to_float(book.get("best_bid"), 0.0)
    if best_bid <= 0.0:
        return None
    best_bid_size = _to_float(book.get("best_bid_size"), 0.0)
    if best_bid_size > 0.0 and best_bid_size + 1e-9 < target_shares:
        return None
    return float(best_bid)


def estimate_hedge_exit_value(book_opposite: dict | None, shares: float) -> tuple[float | None, float]:
    """Estimate the equivalent exit value by taking the ASK liquidity of the opposite token."""
    target_shares = max(0.0, float(shares or 0.0))
    if target_shares <= 0.0:
        return 0.0, 1.0
    if not isinstance(book_opposite, dict):
        return None, 0.0

    ask_levels = _normalize_book_levels(book_opposite.get("ask_levels"), reverse=False)
    if ask_levels:
        remaining = target_shares
        total_cost = 0.0
        filled_shares = 0.0
        for price, size in ask_levels:
            take = min(remaining, size)
            if take <= 0.0:
                continue
            total_cost += take * price
            filled_shares += take
            remaining -= take
            if remaining <= 1e-9:
                break
        fill_ratio = min(1.0, filled_shares / target_shares)
        if filled_shares <= 0.0:
            return 0.0, 0.0
        # For a full fill, effective value is (shares - cost).
        effective_value = filled_shares - total_cost
        return effective_value, fill_ratio

    best_ask = _to_float(book_opposite.get("best_ask"), 0.0)
    if best_ask <= 0.0:
        return 0.0, 0.0
    best_ask_size = _to_float(book_opposite.get("best_ask_size"), 0.0)
    if best_ask_size > 0.0:
        filled_shares = min(target_shares, best_ask_size)
        fill_ratio = min(1.0, filled_shares / target_shares)
        if filled_shares <= 0.0:
            return 0.0, 0.0
        effective_value = filled_shares - (filled_shares * best_ask)
        return effective_value, fill_ratio
    return target_shares - (target_shares * best_ask), 1.0


class PolymarketExchange:
    """
    dry-run: 本地模擬
    real mode: 使用 py-clob-client
    """

    def __init__(self, dry_run: bool = True):
        self._cash: float = 100.0
        self._equity: float = 100.0
        self.dry_run = dry_run

        self._open_exposure = 0.0
        self._last_price = 73933.39
        self._position_cost: dict[str, float] = {}  # token_id -> cost_usd (for exposure accounting)
        self._position_shares: dict[str, float] = {}  # token_id -> shares (for dry-run cost-basis accounting)
        self._live_account_cache: Account | None = None
        self._live_account_cache_ts: float = 0.0

        self.paper_balance_file = os.path.join(SETTINGS.data_dir, "paper_balance.json")
        self._load_paper_balance()

        self.client = None
        self._funder = SETTINGS.funder_address

        self._init_real_client()

    def invalidate_live_account_cache(self) -> None:
        self._live_account_cache = None
        self._live_account_cache_ts = 0.0

    def _load_paper_balance(self):
        self._cash = 100.0
        self._equity = 100.0
        self._open_exposure = 0.0
        self._position_cost = {}
        self._position_shares = {}
        if not self.dry_run:
            return
        if os.path.exists(self.paper_balance_file):
            try:
                with open(self.paper_balance_file, "r") as f:
                    data = json.load(f)
                    self._cash = data.get("cash", 100.0)
                    self._equity = self._cash
                    self._position_cost = {
                        str(token_id): _to_float(cost, 0.0)
                        for token_id, cost in (data.get("position_cost", {}) or {}).items()
                    }
                    self._position_shares = {
                        str(token_id): _to_float(shares, 0.0)
                        for token_id, shares in (data.get("position_shares", {}) or {}).items()
                    }
                    self._open_exposure = sum(max(0.0, cost) for cost in self._position_cost.values())
            except Exception:
                pass

    def _save_paper_balance(self):
        if not self.dry_run:
            return
        try:
            os.makedirs(os.path.dirname(self.paper_balance_file), exist_ok=True)
            with open(self.paper_balance_file, "w") as f:
                json.dump(
                    {
                        "cash": self._cash,
                        "position_cost": self._position_cost,
                        "position_shares": self._position_shares,
                    },
                    f,
                )
        except Exception:
            pass

    def reconcile_dry_run_positions(self, positions: list[Any]) -> bool:
        if not self.dry_run:
            return False

        expected_cost: dict[str, float] = {}
        expected_shares: dict[str, float] = {}
        for pos in positions or []:
            token_id = str(getattr(pos, "token_id", "") or "")
            if not token_id:
                continue
            expected_cost[token_id] = expected_cost.get(token_id, 0.0) + max(0.0, _to_float(getattr(pos, "cost_usd", 0.0), 0.0))
            expected_shares[token_id] = expected_shares.get(token_id, 0.0) + max(0.0, _to_float(getattr(pos, "shares", 0.0), 0.0))

        expected_exposure = sum(expected_cost.values())
        changed = (
            self._position_cost != expected_cost
            or self._position_shares != expected_shares
            or abs(self._open_exposure - expected_exposure) > 1e-9
        )
        if not changed:
            return False

        self._position_cost = expected_cost
        self._position_shares = expected_shares
        self._open_exposure = expected_exposure
        self._save_paper_balance()
        return True

    def _init_real_client(self):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if self.dry_run:
            # Unauthenticated client just for reading public orderbooks during Paper Trading
            self.client = ClobClient(SETTINGS.clob_host, chain_id=SETTINGS.chain_id)
            return

        if not SETTINGS.private_key:
            raise ValueError("PRIVATE_KEY is required when DRY_RUN=false")

        if not self._funder:
            raise ValueError("FUNDER_ADDRESS is required when DRY_RUN=false")

        if SETTINGS.clob_api_key and SETTINGS.clob_api_secret and SETTINGS.clob_api_passphrase:
            creds = ApiCreds(
                api_key=SETTINGS.clob_api_key,
                api_secret=SETTINGS.clob_api_secret,
                api_passphrase=SETTINGS.clob_api_passphrase,
            )
            self.client = ClobClient(
                SETTINGS.clob_host,
                key=SETTINGS.private_key,
                chain_id=SETTINGS.chain_id,
                creds=creds,
                signature_type=SETTINGS.signature_type,
                funder=self._funder,
            )
            return

        temp_client = ClobClient(
            SETTINGS.clob_host,
            key=SETTINGS.private_key,
            chain_id=SETTINGS.chain_id,
            signature_type=SETTINGS.signature_type,
            funder=self._funder,
        )
        creds = temp_client.create_or_derive_api_creds()

        self.client = ClobClient(
            SETTINGS.clob_host,
            key=SETTINGS.private_key,
            chain_id=SETTINGS.chain_id,
            creds=creds,
            signature_type=SETTINGS.signature_type,
            funder=self._funder,
        )

    def _get_positions_value(self) -> float:
        # Data API: https://data-api.polymarket.com/value?user=0x...
        if not self._funder:
            return 0.0
        try:
            r = requests.get(
                f"{SETTINGS.data_api_host}/value",
                params={"user": self._funder},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                # 通常為 [{"user":..., "value": ...}]
                return _to_float(data[0].get("value", 0.0), 0.0)
            if isinstance(data, dict):
                return _to_float(data.get("value", 0.0), 0.0)
            return 0.0
        except Exception:
            return 0.0

    def _get_cash_balance(self) -> float:
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            # 先用設定值，再 fallback 掃 0/1/2（解決代理錢包 signature type 不一致）
            sig_candidates = [SETTINGS.signature_type, 0, 1, 2]
            best = 0.0
            for sig in dict.fromkeys(sig_candidates):
                try:
                    resp = self.client.get_balance_allowance(
                        BalanceAllowanceParams(
                            asset_type=AssetType.COLLATERAL,
                            token_id="",
                            signature_type=sig,
                        )
                    )
                    for k in ("balance", "available", "available_balance"):
                        if isinstance(resp, dict) and k in resp:
                            v = _to_float(resp.get(k), 0.0)
                            # USDC 常見為 6 decimals（回傳最小單位）
                            if v > 100000:
                                v = v / 1_000_000
                            if v > best:
                                best = v
                except Exception:
                    continue
            return best
        except Exception:
            return 0.0

    def _extract_close_usdc_received(self, payload: Any) -> tuple[float | None, str]:
        """Extract USDC received (cash in) from a close order response. Only takingAmount is USDC."""
        if not isinstance(payload, dict):
            return None, "close_response_unavailable"
        taking = _to_float(payload.get("takingAmount"), -1.0)
        if taking > 0:
            return taking, "close_response_takingAmount"
        return None, "close_response_missing_takingAmount"

    def _extract_close_shares_sold(self, payload: Any) -> tuple[float | None, str]:
        """Extract shares actually sold (filled) from a close order response."""
        if not isinstance(payload, dict):
            return None, "close_response_unavailable"
        # makingAmount = shares sold in a SELL order, fallback to size/filledSize
        for key in ("makingAmount", "size", "filledSize", "filled_size", "matchedSize", "matched_size"):
            value = _to_float(payload.get(key), -1.0)
            if value > 0:
                return value, f"close_response_{key}"
        return None, "close_response_missing_filled_shares"

    def _extract_entry_cost_usd(self, payload: Any) -> tuple[float | None, str]:
        """Extract actual USDC spent for a BUY fill."""
        if not isinstance(payload, dict):
            return None, "entry_response_unavailable"
        making = _to_float(payload.get("makingAmount"), -1.0)
        if making > 0:
            return making, "entry_response_makingAmount"
        return None, "entry_response_missing_makingAmount"


    def get_account(self) -> Account:
        if self.dry_run:
            self._load_paper_balance()
            # Include mark-to-market value of open positions so equity reflects unrealized P&L
            positions_value = sum(self._position_cost.values())  # conservative: use cost basis (no live price here)
            return Account(
                equity=self._cash + positions_value,
                cash=self._cash,
                open_exposure=self._open_exposure,
            )

        cache_ttl_sec = max(0.0, float(getattr(SETTINGS, "live_account_cache_ttl_sec", 0.0) or 0.0))
        now_ts = time.time()
        if (
            cache_ttl_sec > 0.0
            and self._live_account_cache is not None
            and (now_ts - self._live_account_cache_ts) <= cache_ttl_sec
        ):
            cached = self._live_account_cache
            return Account(
                equity=float(cached.equity),
                cash=float(cached.cash),
                open_exposure=float(cached.open_exposure),
            )

        cash = self._get_cash_balance()
        positions_value = self._get_positions_value()
        equity = cash + positions_value

        # open exposure 先用保守值 0（後續可擴充為掃 open orders）
        acct = Account(equity=equity, cash=cash, open_exposure=0.0)
        if cache_ttl_sec > 0.0:
            self._live_account_cache = acct
            self._live_account_cache_ts = now_ts
        return Account(equity=acct.equity, cash=acct.cash, open_exposure=acct.open_exposure)

    def get_positions(self) -> list[Position]:
        if self.dry_run or not self._funder:
            return []
        try:
            r = requests.get(
                f"{SETTINGS.data_api_host}/positions",
                params={"user": self._funder},
                timeout=10,
            )
            r.raise_for_status()
            arr = r.json() or []
            out: list[Position] = []
            for row in arr:
                size = _to_float(row.get("size", 0.0), 0.0)
                if size <= 0:
                    continue
                out.append(Position(
                    token_id=str(row.get("asset") or ""),
                    size=size,
                    avg_price=_to_float(row.get("avgPrice", 0.0), 0.0),
                    initial_value=_to_float(row.get("initialValue", 0.0), 0.0),
                    current_value=_to_float(row.get("currentValue", 0.0), 0.0),
                    cash_pnl=_to_float(row.get("cashPnl", 0.0), 0.0),
                    percent_pnl=_to_float(row.get("percentPnl", 0.0), 0.0),
                ))
            return out
        except Exception:
            return []

    def get_position(self, token_id: str) -> Position | None:
        for pos in self.get_positions():
            if pos.token_id == token_id:
                return pos
        return None

    def get_btc_price(self) -> float:
        if self.dry_run:
            self._last_price += uniform(-40, 40)
            return max(1000, self._last_price)

        # 改用 Binance API 獲取即刻 CEX 價格（比 Coingecko 反應快且適合做 Oracle front-running）
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
            p = _to_float(data.get("price", 0), 0)
            if p > 0:
                self._last_price = p
                return p
        except Exception:
            pass
        return self._last_price

    def get_binance_1m_candle(self) -> dict:
        """獲取幣安最近 1 分鐘的 K 線，用來計算突發動能與趨勢"""
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": 2},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
            if len(data) >= 1:
                # [Open time, Open, High, Low, Close, Volume, Close time, ...]
                current = data[-1]
                prev = data[-2] if len(data) >= 2 else current
                return {
                    "open": _to_float(current[1]),
                    "high": _to_float(current[2]),
                    "low": _to_float(current[3]),
                    "close": _to_float(current[4]),
                    "volume": _to_float(current[5]),
                    "prev_close": _to_float(prev[4]),
                    "change": _to_float(current[4]) - _to_float(prev[4])
                }
        except Exception:
            return {}
        return {}

    def get_binance_5m_klines(self, limit: int = 100) -> list[dict]:
        """獲取幣安 5 分鐘 K 線，用於計算 ZLSMA 與 ATR"""
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "5m", "limit": limit},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
            results = []
            for i, candle in enumerate(data):
                prev_close = _to_float(data[i-1][4]) if i > 0 else _to_float(candle[1])
                results.append({
                    "open": _to_float(candle[1]),
                    "high": _to_float(candle[2]),
                    "low": _to_float(candle[3]),
                    "close": _to_float(candle[4]),
                    "volume": _to_float(candle[5]),
                    "prev_close": prev_close
                })
            return results
        except Exception:
            return []

    def place_order(self, side: str, amount_usd: float, token_id_override: str | None = None, simulated_price: float | None = None, force_taker: bool = False) -> dict:
        token_id = token_id_override or (SETTINGS.token_id_up if side == "UP" else SETTINGS.token_id_down)
        if not token_id:
            raise ValueError("TOKEN_ID_UP / TOKEN_ID_DOWN is required")

        if self.dry_run:
            if simulated_price and simulated_price > 0:
                best_ask = simulated_price
            else:
                book = self.get_full_orderbook(token_id)
                if not book or book.get("best_ask", 0.0) == 0.0:
                    book = {"best_ask": 0.5}
                best_ask = book.get("best_ask", 0.5)
                
            filled_shares = amount_usd / best_ask
            
            self._cash -= amount_usd
            self._open_exposure += amount_usd
            self._position_cost[token_id] = self._position_cost.get(token_id, 0.0) + amount_usd
            self._position_shares[token_id] = self._position_shares.get(token_id, 0.0) + filled_shares
            self._save_paper_balance()
            
            mock_resp = {
                "orderID": "paper-order-" + str(int(time.time())),
                "originalQuantity": str(filled_shares),
                "fillAmount": str(filled_shares),
                "takingAmount": str(filled_shares), # 讓 runner 能計算到 shares
                "makingAmount": str(amount_usd),    # 模擬買單支出
                "status": "MATCHED",
            }
            return {
                "ok": True,
                "mode": "dry-run",
                "side": side,
                "amount_usd": amount_usd,
                "execution_style": "taker-simulated",
                "response": mock_resp,
            }

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # Use simulated_price (which refers to the real observed price passed from runner)
        price = simulated_price if simulated_price and simulated_price > 0 else 0.5
        price_rounded = round(price, 3) 
        min_live_order_shares = float(getattr(SETTINGS, "min_live_order_shares", 5.0) or 0.0)
        min_live_order_usd = float(getattr(SETTINGS, "min_live_order_usd", 1.0) or 0.0)
        live_order_hard_cap_usd = float(getattr(SETTINGS, "live_order_hard_cap_usd", 0.0) or 0.0)
        use_market_entry = force_taker or bool(getattr(SETTINGS, "live_entry_use_market_orders", True))

        if use_market_entry:
            price_rounded = 0.99
            size_rounded, actual_order_usd = plan_live_order(
                amount_usd,
                price_rounded,
                min_live_order_shares,
                min_live_order_usd,
            )
            if live_order_hard_cap_usd > 0.0 and actual_order_usd > live_order_hard_cap_usd + 1e-9:
                raise ValueError(
                    f"order notional exceeds live cap: requested=${amount_usd:.2f} "
                    f"actual=${actual_order_usd:.4f} cap=${live_order_hard_cap_usd:.2f}"
                )
            order = self.client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=float(price_rounded),
                    size=float(size_rounded),
                    side=BUY,
                )
            )
            from py_clob_client.clob_types import OrderType
            resp = self.client.post_order(order, OrderType.FAK)
        else:
            size_rounded, actual_order_usd = plan_live_order(
                amount_usd,
                price_rounded,
                min_live_order_shares,
                min_live_order_usd,
            )
            if live_order_hard_cap_usd > 0.0 and actual_order_usd > live_order_hard_cap_usd + 1e-9:
                raise ValueError(
                    f"order notional exceeds live cap: requested=${amount_usd:.2f} "
                    f"actual=${actual_order_usd:.4f} cap=${live_order_hard_cap_usd:.2f}"
                )
            book = self.get_full_orderbook(token_id)
            if book:
                best_bid = float(book.get("best_bid", 0.01))
                best_ask = float(book.get("best_ask", 0.99))
                # Never cross the spread for POST_ONLY
                safe_price = min(price_rounded, best_ask - 0.001)
                safe_price = max(safe_price, best_bid + 0.001)
                price_rounded = round(safe_price, 3)
                # recalculate size if price changed
                size_rounded, actual_order_usd = plan_live_order(
                    amount_usd,
                    price_rounded,
                    min_live_order_shares,
                    min_live_order_usd,
                )
                if live_order_hard_cap_usd > 0.0 and actual_order_usd > live_order_hard_cap_usd + 1e-9:
                    raise ValueError(
                        f"order notional exceeds live cap: requested=${amount_usd:.2f} "
                        f"actual=${actual_order_usd:.4f} cap=${live_order_hard_cap_usd:.2f}"
                    )

            order = self.client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=float(price_rounded),
                    size=float(size_rounded),
                    side=BUY,
                )
            )
            limit_order_type = _limit_order_type(OrderType)
            if limit_order_type is None:
                raise AttributeError("py_clob_client OrderType missing both POST_ONLY and GTC")
            resp = self.client.post_order(order, limit_order_type)

        filled_cost_usd, filled_cost_source = self._extract_entry_cost_usd(resp)
        effective_amount_usd = (
            float(filled_cost_usd)
            if filled_cost_usd is not None and filled_cost_usd > 0
            else actual_order_usd
        )
        self.invalidate_live_account_cache()

        return {
            "ok": True,
            "mode": "live",
            "side": side,
            "requested_amount_usd": amount_usd,
            "amount_usd": effective_amount_usd,
            "planned_amount_usd": actual_order_usd,
            "actual_entry_cost_usd": filled_cost_usd,
            "actual_entry_cost_source": filled_cost_source,
            "execution_style": "taker" if use_market_entry else "maker",
            "response": resp,
        }

    def get_full_orderbook(self, token_id: str) -> dict:
        """獲取完整的 orderbook 來計算 Imbalance"""
        if not self.client:
            return {
                "bids_volume": 1000,
                "asks_volume": 1000,
                "best_bid": 0.5,
                "best_ask": 0.51,
                "best_bid_size": 1000,
                "best_ask_size": 1000,
                "bid_levels": [(0.5, 1000.0)],
                "ask_levels": [(0.51, 1000.0)],
            }
        try:
            raw_book = self.client.get_order_book(token_id)
            book = _normalize_orderbook_summary(raw_book)
            if not isinstance(book, dict) or not book:
                return {}

            bid_levels = _normalize_book_levels(book.get("bids"), reverse=True)
            ask_levels = _normalize_book_levels(book.get("asks"), reverse=False)
            bids_vol = sum(size for _price, size in bid_levels)
            asks_vol = sum(size for _price, size in ask_levels)
            best_bid, best_bid_size = bid_levels[0] if bid_levels else (0.0, 0.0)
            best_ask, best_ask_size = ask_levels[0] if ask_levels else (0.0, 0.0)

            return {
                "bids_volume": bids_vol,
                "asks_volume": asks_vol,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "best_bid_size": best_bid_size,
                "best_ask_size": best_ask_size,
                "bid_levels": bid_levels,
                "ask_levels": ask_levels,
            }
        except Exception:
            return {}

    def has_exit_liquidity(self, token_id: str, shares: float) -> bool:
        if not self.client:
            return True
        try:
            raw_book = self.client.get_order_book(token_id)
            book = _normalize_orderbook_summary(raw_book)
            bid_levels = _normalize_book_levels(book.get("bids"), reverse=True)
            if not bid_levels:
                return True
            total = 0.0
            for _price, sz in bid_levels:
                total += max(0.0, sz)
                if total >= shares * 0.8:
                    return True
            return False
        except Exception:
            return True

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run or not self.client:
            return True
        try:
            if hasattr(self.client, "cancel_order"):
                self.client.cancel_order(order_id)
            elif hasattr(self.client, "cancel"):
                self.client.cancel(order_id)
            return True
        except Exception as e:
            return False

    def get_open_orders(self, token_id: str | None = None) -> list[dict]:
        if self.dry_run or not self.client:
            return []
        try:
            from py_clob_client.clob_types import OpenOrderParams
            params = {"token_id": token_id} if token_id else {}
            resp = getattr(self.client, "get_orders", lambda x: [])(OpenOrderParams(**params))
            if isinstance(resp, list):
                return resp
            return []
        except Exception:
            return []

    def close_position(
        self,
        token_id: str,
        shares: float,
        simulated_price: float | None = None,
        force_taker: bool = False,
        max_attempts: int | None = None,
        retry_delay_sec: float | None = None,
        is_hard_stop: bool = False,
        hedge_mode: bool = False,
        opposite_token_id: str | None = None,
    ) -> dict:
        if self.dry_run:
            if simulated_price is not None and simulated_price >= 0:
                best_bid = simulated_price
            else:
                book = self.get_full_orderbook(token_id)
                if not book or book.get("best_bid", 0.0) == 0.0:
                    book = {"best_bid": 0.5}
                best_bid = book.get("best_bid", 0.5)

            current_shares = max(0.0, _to_float(self._position_shares.get(token_id, 0.0), 0.0))
            current_cost = max(0.0, _to_float(self._position_cost.get(token_id, 0.0), 0.0))
            closed_shares = min(max(0.0, shares), current_shares if current_shares > 0 else max(0.0, shares))
            avg_cost = (current_cost / current_shares) if current_shares > 0 else 0.0
            realized_cost = min(current_cost, avg_cost * closed_shares)
            remaining_shares = max(0.0, current_shares - closed_shares)
            remaining_cost = max(0.0, current_cost - realized_cost)
            value_received = closed_shares * best_bid

            if hedge_mode and opposite_token_id:
                # In hedge mode, we simulate buying the opposite token instead of selling the current one.
                book_opp = self.get_full_orderbook(opposite_token_id)
                if not book_opp or book_opp.get("best_ask", 0.0) == 0.0:
                    book_opp = {"best_ask": 0.5}
                best_ask = book_opp.get("best_ask", 0.5)
                # cost to hedge
                hedge_cost = closed_shares * best_ask
                self._cash -= hedge_cost
                # Conceptually, the position is now fully risk-neutral, so we just remove it from open exposure.
                self._open_exposure = max(0.0, self._open_exposure - realized_cost)
                if remaining_shares > 0 and remaining_cost > 0:
                    self._position_cost[token_id] = remaining_cost
                    self._position_shares[token_id] = remaining_shares
                else:
                    self._position_cost.pop(token_id, None)
                    self._position_shares.pop(token_id, None)
                self._save_paper_balance()
                
                return {
                    "ok": True, 
                    "mode": "dry-run", 
                    "closed_shares": closed_shares,
                    "actual_exit_value_usd": closed_shares - hedge_cost,
                    "actual_exit_value_source": "paper_trade_simulation_hedge",
                    "close_response_value": closed_shares - hedge_cost,
                    "close_response_value_source": "paper_trade_simulation_hedge",
                    "remaining_shares": remaining_shares,
                    "execution_style": "taker-simulated-hedge",
                }

            self._cash += value_received
            self._open_exposure = max(0.0, self._open_exposure - realized_cost)
            if remaining_shares > 0 and remaining_cost > 0:
                self._position_cost[token_id] = remaining_cost
                self._position_shares[token_id] = remaining_shares
            else:
                self._position_cost.pop(token_id, None)
                self._position_shares.pop(token_id, None)
            self._save_paper_balance()

            return {
                "ok": True, 
                "mode": "dry-run", 
                "closed_shares": closed_shares,
                "actual_exit_value_usd": value_received,
                "actual_exit_value_source": "paper_trade_simulation",
                "close_response_value": value_received,
                "close_response_value_source": "paper_trade_simulation",
                "remaining_shares": remaining_shares,
                "execution_style": "taker-simulated",
            }

        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL, BUY
        import time

        cash_before = self._get_cash_balance()
        remaining = float(shares)
        attempts = 0
        attempt_limit = max(1, int(max_attempts or 8))
        retry_sleep = max(0.0, float(2.0 if retry_delay_sec is None else retry_delay_sec))
        last_resp = None
        last_error = None
        sold_total = 0.0
        usdc_received_total: float | None = None  # Strictly USDC received (takingAmount sum)
        usdc_received_source = "close_response_unavailable"
        shares_sold_per_attempt: list[float] = []  # Shares filled per attempt (for debug)
        maker_filled = False
        taker_filled = False
        
        while remaining > 0.0001 and attempts < attempt_limit:
            if remaining + 1e-9 < MIN_LIVE_CLOSE_SHARES:
                break
            attempts += 1
            # For aggressive taker exits, keep sweeping the full residual size.
            # Shrinking chunks creates dust-like leftovers that are hard to clear.
            if force_taker or is_hard_stop:
                chunk = remaining
            else:
                # progressively smaller chunks on retries
                if attempts == 1:
                    chunk = remaining
                elif attempts == 2:
                    chunk = max(remaining * 0.85, MIN_LIVE_CLOSE_SHARES)
                elif attempts == 3:
                    chunk = max(remaining * 0.7, MIN_LIVE_CLOSE_SHARES)
                elif attempts == 4:
                    chunk = max(remaining * 0.5, MIN_LIVE_CLOSE_SHARES)
                else:
                    chunk = max(remaining * 0.35, MIN_LIVE_CLOSE_SHARES)

            try:
                should_clear_open_orders = attempts == 1 or not (force_taker or is_hard_stop)
                if should_clear_open_orders:
                    open_ords = self.get_open_orders(token_id)
                    for o in open_ords:
                        oid = o.get("id") or o.get("orderID")
                        if oid:
                            self.cancel_order(oid)
                
                # Maker Exits for first 5 attempts, Taker Fallback for remaining (except Hedge Mode always uses Taker)
                if not force_taker and attempts <= 5 and not (hedge_mode and opposite_token_id):
                    book = self.get_full_orderbook(token_id)
                    best_bid = float(book.get("best_bid", 0.01)) if book else 0.01
                    best_ask = float(book.get("best_ask", 1.00)) if book else 1.00
                    
                    target_price = max(best_bid + 0.001, best_ask - (attempts - 1) * 0.005)
                    target_price = round(target_price, 3)
                    
                    order = self.client.create_order(
                        OrderArgs(
                            token_id=token_id,
                            price=float(target_price),
                            size=float(chunk),
                            side=SELL,
                        )
                    )
                    limit_order_type = _limit_order_type(OrderType)
                    if limit_order_type is None:
                        raise AttributeError("py_clob_client OrderType missing both POST_ONLY and GTC")
                    last_resp = self.client.post_order(order, limit_order_type)
                    
                    # If this is a limit order, it won't fill immediately. Sleep 3 seconds to expose liquidity to the market.
                    time.sleep(3.0)
                    
                    # Check USDC Delta to see if the market bought our Limit Ask!
                    current_cash = self._get_cash_balance()
                    usdc_gained = current_cash - cash_before
                    # Previous loop iterations might have gained USDC, so we subtract what we already tracked
                    new_usdc_this_round = usdc_gained - (usdc_received_total or 0.0)
                    
                    if new_usdc_this_round > 0.05:
                        # We got a fill! Estimate shares based on target price
                        filled_shares = min(remaining, new_usdc_this_round / target_price)
                        usdc_this = new_usdc_this_round
                        usdc_src = "maker-balance-delta"
                        maker_filled = True
                    else:
                        filled_shares = 0.0
                        usdc_this = 0.0
                        usdc_src = "maker-no-fill"
                        
                else:
                    # Taker Fallback (Attempts 6-8) or Hedge Mode
                    if hedge_mode and opposite_token_id:
                        target_token = opposite_token_id
                        target_side = BUY
                        worst_price = 0.99  # Cross the whole spread for hedge BUY
                    else:
                        target_token = token_id
                        target_side = SELL
                        worst_price = 0.01
                    order = self.client.create_order(
                        OrderArgs(
                            token_id=target_token,
                            price=float(worst_price),
                            size=float(chunk),
                            side=target_side,
                        )
                    )
                    from py_clob_client.clob_types import OrderType
                    last_resp = self.client.post_order(order, OrderType.FAK)
                    
                    if hedge_mode and opposite_token_id:
                        # It was a BUY, so we look at makingAmount for cost
                        filled_cost, _ = self._extract_entry_cost_usd(last_resp)
                        filled_shares, _ = self._extract_close_shares_sold(last_resp)
                        if filled_shares and filled_shares > 0:
                            taker_filled = True
                        if filled_cost is not None and filled_cost > 0:
                            # Equivalent exit value is the shares secured minus the cost we paid
                            usdc_this = (filled_shares or 0.0) - filled_cost
                            usdc_src = "taker-buy-hedge"
                        else:
                            usdc_this = None
                            usdc_src = "taker-buy-hedge-failed"
                    else:
                        usdc_this, usdc_src = self._extract_close_usdc_received(last_resp)
                        filled_shares, _ = self._extract_close_shares_sold(last_resp)
                        if filled_shares and filled_shares > 0:
                            taker_filled = True

                if usdc_this is not None and usdc_this > 0 or (hedge_mode and usdc_this is not None):
                    usdc_received_total = (usdc_received_total or 0.0) + usdc_this
                    usdc_received_source = usdc_src

                effective_filled = min(chunk, max(0.0, float(filled_shares or 0.0)))
                if effective_filled <= 0:
                    if remaining > 0.0001 and attempts < attempt_limit and retry_sleep > 0:
                        time.sleep(retry_sleep)
                    continue

                shares_sold_per_attempt.append(effective_filled)
                remaining -= effective_filled
                sold_total += effective_filled
            except Exception as e:
                last_error = str(e)
                adjusted_remaining = parse_balance_allowance_available_shares(last_error)
                if adjusted_remaining is not None:
                    adjusted_remaining = max(0.0, round(adjusted_remaining - 0.0005, 6))
                    if 0.0 < adjusted_remaining + 1e-9 < remaining:
                        remaining = adjusted_remaining
                        if remaining + 1e-9 < MIN_LIVE_CLOSE_SHARES:
                            break
                        time.sleep(min(1.0, retry_sleep) if retry_sleep > 0 else 0.0)
                        continue
                # small delay then retry
                if remaining > 0.0001 and attempts < attempt_limit and retry_sleep > 0:
                    time.sleep(retry_sleep)
                continue

            if remaining > 0.0001 and attempts < attempt_limit and retry_sleep > 0:
                time.sleep(retry_sleep)

        cash_after = None
        cash_delta = None
        cash_delta_source = "cash_balance_unavailable"

        ok = sold_total > 0 and (last_resp is not None)
        best_exit_value, best_exit_source = select_live_close_exit_value(
            usdc_received_total=usdc_received_total,
            usdc_received_source=usdc_received_source,
            cash_delta=cash_delta,
            cash_delta_source=cash_delta_source,
        )
        if maker_filled and taker_filled:
            execution_style = "mixed"
        elif taker_filled or force_taker:
            execution_style = "taker"
        elif maker_filled:
            execution_style = "maker"
        else:
            execution_style = "unknown"
        if sold_total > 0:
            self.invalidate_live_account_cache()
        return {
            "ok": ok,
            "mode": "live",
            "requested_shares": shares,
            "closed_shares": sold_total,
            "remaining_shares": max(0.0, float(remaining)),
            "attempts": attempts,
            "response": last_resp,
            "error": None if ok else (last_error or "no fill"),
            "cash_before": cash_before,
            "cash_after": cash_after,
            "actual_exit_value_usd": best_exit_value,
            "actual_exit_value_source": best_exit_source,
            "close_response_value": usdc_received_total,
            "close_response_value_source": usdc_received_source,
            "close_response_amount_fields": {"shares_sold_per_attempt": str(shares_sold_per_attempt)},
            "execution_style": execution_style,
        }

    def settle_mock(self, pnl: float):
        if not self.dry_run:
            return
        self._open_exposure = max(0.0, self._open_exposure - 1.0)
        self._cash += (1.0 + pnl)
        self._equity += pnl
