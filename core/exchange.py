from dataclasses import dataclass
from random import uniform
from typing import Any
import time
import os
import json
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


class PolymarketExchange:
    """
    dry-run: 本地模擬
    real mode: 使用 py-clob-client
    """

    def __init__(self, dry_run: bool = True):
        self._cash: float = 100.0
        self._equity: float = 100.0
        self.dry_run = dry_run
        
        self.paper_balance_file = os.path.join(SETTINGS.data_dir, "paper_balance.json")
        self._load_paper_balance()
        
        self._open_exposure = 0.0
        self._last_price = 73933.39

        self.client = None
        self._funder = SETTINGS.funder_address

        self._init_real_client()

    def _load_paper_balance(self):
        self._cash = 100.0
        self._equity = 100.0
        if not self.dry_run:
            return
        if os.path.exists(self.paper_balance_file):
            try:
                with open(self.paper_balance_file, "r") as f:
                    data = json.load(f)
                    self._cash = data.get("cash", 100.0)
                    self._equity = self._cash
            except Exception:
                pass

    def _save_paper_balance(self):
        if not self.dry_run:
            return
        try:
            os.makedirs(os.path.dirname(self.paper_balance_file), exist_ok=True)
            with open(self.paper_balance_file, "w") as f:
                json.dump({"cash": self._cash}, f)
        except Exception:
            pass

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


    def get_account(self) -> Account:
        if self.dry_run:
            self._load_paper_balance()
            return Account(
                equity=self._cash,
                cash=self._cash,
                open_exposure=self._open_exposure,
            )

        cash = self._get_cash_balance()
        positions_value = self._get_positions_value()
        equity = cash + positions_value

        # open exposure 先用保守值 0（後續可擴充為掃 open orders）
        return Account(equity=equity, cash=cash, open_exposure=0.0)

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

    def place_order(self, side: str, amount_usd: float, token_id_override: str | None = None, simulated_price: float | None = None) -> dict:
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
                "response": mock_resp,
            }

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # Use simulated_price (which refers to the real observed price passed from runner)
        price = simulated_price if simulated_price and simulated_price > 0 else 0.5
        price_rounded = round(price, 3) 
        size_rounded = round(amount_usd / price_rounded, 2)

        if force_taker:
            from py_clob_client.clob_types import MarketOrderArgs
            order = self.client.create_market_order(
                MarketOrderArgs(
                    token_id=token_id,
                    amount=float(size_rounded),
                    side=BUY,
                    order_type=OrderType.FAK,
                )
            )
            resp = self.client.post_order(order, OrderType.FAK)
        else:
            order = self.client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=float(price_rounded),
                    size=float(size_rounded),
                    side=BUY,
                )
            )
            resp = self.client.post_order(order, OrderType.POST_ONLY)

        return {
            "ok": True,
            "mode": "live",
            "side": side,
            "amount_usd": amount_usd,
            "response": resp,
        }

    def get_full_orderbook(self, token_id: str) -> dict:
        """獲取完整的 orderbook 來計算 Imbalance"""
        if not self.client:
            return {"bids_volume": 1000, "asks_volume": 1000, "best_bid": 0.5, "best_ask": 0.51}
        try:
            book = self.client.get_order_book(token_id)
            if not isinstance(book, dict):
                return {}
            
            bids_vol, asks_vol = 0.0, 0.0
            best_bid, best_ask = 0.0, 1.0

            bids = book.get("bids", [])
            for lv in (bids if isinstance(bids, list) else []):
                price = _to_float(lv.get("price") if isinstance(lv, dict) else lv[0], 0.0)
                sz = _to_float(lv.get("size", lv.get("amount", 0)) if isinstance(lv, dict) else lv[1], 0.0)
                bids_vol += sz
                if price > best_bid:
                    best_bid = price

            asks = book.get("asks", [])
            for lv in (asks if isinstance(asks, list) else []):
                price = _to_float(lv.get("price") if isinstance(lv, dict) else lv[0], 1.0)
                sz = _to_float(lv.get("size", lv.get("amount", 0)) if isinstance(lv, dict) else lv[1], 0.0)
                asks_vol += sz
                if price < best_ask:
                    best_ask = price

            return {"bids_volume": bids_vol, "asks_volume": asks_vol, "best_bid": best_bid, "best_ask": best_ask}
        except Exception:
            return {}

    def has_exit_liquidity(self, token_id: str, shares: float) -> bool:
        if not self.client:
            return True
        try:
            book = self.client.get_order_book(token_id)
            bids = book.get("bids") if isinstance(book, dict) else None
            if not isinstance(bids, list):
                return True
            total = 0.0
            for lv in bids:
                if isinstance(lv, dict):
                    sz = _to_float(lv.get("size", lv.get("amount", 0)), 0.0)
                elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
                    sz = _to_float(lv[1], 0.0)
                else:
                    sz = 0.0
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

    def close_position(self, token_id: str, shares: float, simulated_price: float | None = None, force_taker: bool = False) -> dict:
        if self.dry_run:
            if simulated_price and simulated_price > 0:
                best_bid = simulated_price
            else:
                book = self.get_full_orderbook(token_id)
                if not book or book.get("best_bid", 0.0) == 0.0:
                    book = {"best_bid": 0.5}
                best_bid = book.get("best_bid", 0.5)

            value_received = shares * best_bid
            
            self._cash += value_received
            self._open_exposure = max(0.0, self._open_exposure - value_received)
            self._save_paper_balance()

            return {
                "ok": True, 
                "mode": "dry-run", 
                "closed_shares": shares,
                "actual_exit_value_usd": value_received,
                "actual_exit_value_source": "paper_trade_simulation",
                "close_response_value": value_received,
                "remaining_shares": 0.0
            }

        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL
        import time

        cash_before = self._get_cash_balance()
        remaining = float(shares)
        attempts = 0
        last_resp = None
        last_error = None
        sold_total = 0.0
        usdc_received_total: float | None = None  # Strictly USDC received (takingAmount sum)
        usdc_received_source = "close_response_unavailable"
        shares_sold_per_attempt: list[float] = []  # Shares filled per attempt (for debug)
        
        while remaining > 0.0001 and attempts < 8:
            attempts += 1
            # progressively smaller chunks on retries
            if attempts == 1:
                chunk = remaining
            elif attempts == 2:
                chunk = max(remaining * 0.85, 0.01)
            elif attempts == 3:
                chunk = max(remaining * 0.7, 0.01)
            elif attempts == 4:
                chunk = max(remaining * 0.5, 0.01)
            else:
                chunk = max(remaining * 0.35, 0.01)

            try:
                self.cancel_all_orders() # Cancel open orders to free token balance
                
                # Maker Exits for first 5 attempts, Taker Fallback for remaining
                if not force_taker and attempts <= 5:
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
                    last_resp = self.client.post_order(order, OrderType.POST_ONLY)
                    
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
                    else:
                        filled_shares = 0.0
                        usdc_this = 0.0
                        usdc_src = "maker-no-fill"
                        
                else:
                    # Taker Fallback (Attempts 6-8)
                    order = self.client.create_market_order(
                        MarketOrderArgs(
                            token_id=token_id,
                            amount=float(chunk),
                            side=SELL,
                            order_type=OrderType.FAK,
                        )
                    )
                    last_resp = self.client.post_order(order, OrderType.FAK)
                    usdc_this, usdc_src = self._extract_close_usdc_received(last_resp)
                    filled_shares, _ = self._extract_close_shares_sold(last_resp)

                if usdc_this is not None and usdc_this > 0:
                    usdc_received_total = (usdc_received_total or 0.0) + usdc_this
                    usdc_received_source = usdc_src

                effective_filled = min(chunk, max(0.0, float(filled_shares or 0.0)))
                if effective_filled <= 0:
                    time.sleep(2)
                    continue

                shares_sold_per_attempt.append(effective_filled)
                remaining -= effective_filled
                sold_total += effective_filled
            except Exception as e:
                last_error = str(e)
                # small delay then retry
                time.sleep(2)
                continue

            time.sleep(2)

        cash_after = None
        cash_delta = None
        cash_delta_source = "cash_balance_unavailable"
        if sold_total > 0:
            for _ in range(3):
                cash_after = self._get_cash_balance()
                if cash_after > 0 or cash_before > 0:
                    cash_delta = cash_after - cash_before
                    if cash_delta > 0:
                        cash_delta_source = "cash_balance_delta"
                        break
                    cash_delta_source = "cash_balance_non_positive"
                time.sleep(1)

        ok = sold_total > 0 and (last_resp is not None)
        # Prefer USDC received from response (most accurate); fallback to cash delta
        best_exit_value = usdc_received_total if (usdc_received_total and usdc_received_total > 0) else cash_delta
        best_exit_source = usdc_received_source if (usdc_received_total and usdc_received_total > 0) else cash_delta_source
        return {
            "ok": ok,
            "mode": "live",
            "requested_shares": shares,
            "closed_shares": sold_total,
            "remaining_shares": max(0.0, float(shares) - sold_total),
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
        }

    def settle_mock(self, pnl: float):
        if not self.dry_run:
            return
        self._open_exposure = max(0.0, self._open_exposure - 1.0)
        self._cash += (1.0 + pnl)
        self._equity += pnl
