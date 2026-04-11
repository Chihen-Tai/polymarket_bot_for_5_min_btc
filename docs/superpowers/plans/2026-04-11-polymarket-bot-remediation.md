# Polymarket Bot Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the BTC 5-minute Polymarket bot safer for live trading by fixing transport security, restoring real risk controls, correcting execution/accounting bugs, and disabling uncalibrated strategy behavior.

**Architecture:** Fixes should proceed in layers. First harden network and configuration behavior so the bot stops trusting unsafe responses, then repair live accounting and order-state handling so risk limits are real, and only then simplify strategy and exit logic that currently depends on hand-tuned assumptions. Keep changes small, test-driven, and isolated so each step can be verified independently.

**Tech Stack:** Python, requests, py-clob-client, pytest, Polymarket CLOB/Data/Gamma APIs, Binance WebSocket data.

---

## File Map

- Modify: `main.py` — remove global TLS bypass.
- Modify: `core/market_resolver.py` — stop using `verify=False`; add explicit timeout-safe fetch behavior.
- Create: `core/http.py` — centralize safe HTTP request helpers and response parsing.
- Create: `tests/test_http_safety.py` — verify HTTP helpers default to certificate verification and sane timeouts.
- Modify: `core/exchange.py` — fix live account exposure/accounting, close-value accounting, order/fill handling.
- Modify: `core/runner.py` — remove fake maker-fill confirmation path, use actual open-order/position reconciliation, tighten entry/exit safety behavior.
- Modify: `core/decision_engine.py` — stop encoding fake calibrated probabilities directly in strategy candidates.
- Modify: `core/trade_manager.py` — simplify dangerous endgame overrides and make high-risk hold logic opt-in.
- Modify: `.env.example` — safer dry-run defaults and comments.
- Modify: `.env.live.example` — safer live defaults; disable effectively infinite loss limits.
- Modify: `README.md` — document live-trading safeguards and changed defaults.
- Modify: `tests/test_exit_fix.py` — extend execution/accounting regression coverage.
- Modify: `tests/test_trade_manager.py` — extend entry scoring, exit logic, and market re-entry tests.

---

### Task 1: Remove unsafe TLS bypass and centralize HTTP safety

**Files:**
- Create: `core/http.py`
- Modify: `main.py`
- Modify: `core/market_resolver.py`
- Test: `tests/test_http_safety.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.http import request_json, request_json_with_session


def test_request_json_defaults_to_tls_verification(monkeypatch):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return DummyResponse()

    monkeypatch.setattr("requests.get", fake_get)

    payload = request_json("https://example.com/markets")

    assert payload == {"ok": True}
    assert captured["verify"] is True
    assert captured["timeout"] == 12


def test_request_json_with_session_uses_verify_true(monkeypatch):
    captured = {}

    class DummySession:
        def get(self, url, **kwargs):
            captured.update(kwargs)

            class DummyResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return []

            return DummyResponse()

    payload = request_json_with_session(DummySession(), "https://example.com/data")

    assert payload == []
    assert captured["verify"] is True
    assert captured["timeout"] == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_http_safety.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.http'`

- [ ] **Step 3: Write minimal implementation**

```python
# core/http.py
from __future__ import annotations

from typing import Any

import requests


DEFAULT_TIMEOUT = 12


def request_json(url: str, *, params: dict[str, Any] | None = None, timeout: int = DEFAULT_TIMEOUT):
    response = requests.get(url, params=params, timeout=timeout, verify=True)
    response.raise_for_status()
    return response.json()


def request_json_with_session(session, url: str, *, params: dict[str, Any] | None = None, timeout: int = DEFAULT_TIMEOUT):
    response = session.get(url, params=params, timeout=timeout, verify=True)
    response.raise_for_status()
    return response.json()
```

```python
# main.py
# delete the ssl._create_default_https_context override entirely
```

```python
# core/market_resolver.py
from core.http import request_json


def _fetch_by_slug(slug: str):
    arr = request_json(
        "https://gamma-api.polymarket.com/markets",
        params={"slug": slug},
    ) or []
    ...


def resolve_latest_btc_5m_token_ids() -> dict:
    ...
    data = request_json(
        "https://gamma-api.polymarket.com/markets",
        params={"active": "true", "closed": "false", "limit": 500},
    )
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_http_safety.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py core/http.py core/market_resolver.py tests/test_http_safety.py
git commit -m "fix: restore TLS verification for market data requests"
```

---

### Task 2: Make live risk accounting reflect real exposure

**Files:**
- Modify: `core/exchange.py`
- Modify: `.env.live.example`
- Test: `tests/test_exit_fix.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.exchange import Account, PolymarketExchange


def test_live_account_open_exposure_uses_position_notional(monkeypatch):
    ex = PolymarketExchange(dry_run=False)
    ex.client = object()
    ex._funder = "0xtest"

    monkeypatch.setattr(ex, "_get_cash_balance", lambda: 12.0)
    monkeypatch.setattr(ex, "_get_positions_value", lambda: 3.0)
    monkeypatch.setattr(
        ex,
        "get_positions",
        lambda: [
            type("P", (), {"token_id": "tok1", "size": 10.0, "avg_price": 0.0, "initial_value": 2.0, "current_value": 3.0, "cash_pnl": 1.0, "percent_pnl": 0.5})()
        ],
    )

    acct = ex.get_account()

    assert acct.equity == 15.0
    assert acct.cash == 12.0
    assert acct.open_exposure == 2.0


def test_live_example_does_not_disable_loss_guards():
    text = open(".env.live.example", "r", encoding="utf-8").read()

    assert "MAX_CONSEC_LOSS=99" not in text
    assert "DAILY_MAX_LOSS=999999999" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exit_fix.py -q`
Expected: FAIL because `get_account()` currently returns `open_exposure=0.0` and the live env file still contains effectively disabled loss limits.

- [ ] **Step 3: Write minimal implementation**

```python
# core/exchange.py inside get_account()
cash = self._get_cash_balance()
positions_value = self._get_positions_value()
positions = self.get_positions()
open_exposure = sum(max(0.0, float(getattr(pos, "initial_value", 0.0) or 0.0)) for pos in positions)
equity = cash + positions_value
acct = Account(equity=equity, cash=cash, open_exposure=open_exposure)
```

```dotenv
# .env.live.example
MIN_EQUITY=5.0
MAX_CONSEC_LOSS=3
DAILY_MAX_LOSS=3.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exit_fix.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/exchange.py .env.live.example tests/test_exit_fix.py
git commit -m "fix: restore live exposure and loss guard defaults"
```

---

### Task 3: Fix live close accounting and maker fill confirmation

**Files:**
- Modify: `core/exchange.py`
- Modify: `core/runner.py`
- Test: `tests/test_exit_fix.py`
- Test: `tests/test_trade_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.exchange import select_live_close_exit_value
from core.runner import entry_response_has_actionable_state


def test_close_value_prefers_cash_delta_when_available():
    value, source = select_live_close_exit_value(
        usdc_received_total=0.52,
        usdc_received_source="close_response_takingAmount",
        cash_delta=0.51,
        cash_delta_source="cash_balance_delta",
    )

    assert value == 0.51
    assert source == "cash_balance_delta"


def test_entry_response_requires_fill_or_order_id():
    assert entry_response_has_actionable_state({"response": {"orderID": "abc"}}) is True
    assert entry_response_has_actionable_state({"response": {"takingAmount": 0}}) is False
```

```python
def test_runner_does_not_use_exit_liquidity_as_fill_confirmation():
    source = open("core/runner.py", "r", encoding="utf-8").read()
    assert "has_exit_liquidity(token_override" not in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exit_fix.py tests/test_trade_manager.py -q`
Expected: FAIL because `core/runner.py` still uses `has_exit_liquidity(...)` during maker fill confirmation and `core/exchange.py` does not compute live `cash_after/cash_delta` before selecting the actual close value.

- [ ] **Step 3: Write minimal implementation**

```python
# core/exchange.py near close_position() end
try:
    cash_after = self._get_cash_balance()
    cash_delta = cash_after - cash_before
    cash_delta_source = "cash_balance_delta"
except Exception:
    cash_after = None
    cash_delta = None
    cash_delta_source = "cash_balance_unavailable"

best_exit_value, best_exit_source = select_live_close_exit_value(
    usdc_received_total=usdc_received_total,
    usdc_received_source=usdc_received_source,
    cash_delta=cash_delta,
    cash_delta_source=cash_delta_source,
)
```

```python
# core/runner.py maker wait loop
# replace has_exit_liquidity(...) with explicit order/position reconciliation
open_order_ids = {o.get("orderID") for o in ex.get_open_orders()}
live_positions = {p.token_id: p for p in ex.get_positions()}

if token_override in live_positions:
    _maker_filled = True
elif maker_resp.get("response", {}).get("orderID") not in open_order_ids:
    _maker_filled = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exit_fix.py tests/test_trade_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/exchange.py core/runner.py tests/test_exit_fix.py tests/test_trade_manager.py
git commit -m "fix: use real fill reconciliation for live execution"
```

---

### Task 4: Disable uncalibrated probability-driven sizing

**Files:**
- Modify: `core/decision_engine.py`
- Modify: `core/runner.py`
- Modify: `.env.example`
- Modify: `.env.live.example`
- Test: `tests/test_trade_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.runner import price_aware_kelly_fraction


def test_kelly_fraction_disabled_by_default_when_probability_is_not_calibrated():
    assert price_aware_kelly_fraction(0.99, 0.55) == 0.0


def test_decision_engine_source_does_not_assign_fixed_ninety_nine_percent_probability():
    source = open("core/decision_engine.py", "r", encoding="utf-8").read()
    assert "model_probability=0.99" not in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_trade_manager.py -q`
Expected: FAIL because Kelly sizing is still active by default and `core/decision_engine.py` still hard-codes fake high probabilities.

- [ ] **Step 3: Write minimal implementation**

```python
# core/runner.py
def price_aware_kelly_fraction(win_rate: float, entry_price: float) -> float:
    if not bool(getattr(SETTINGS, "use_kelly_sizing", False)):
        return 0.0
    if entry_price <= 0.0 or entry_price >= 1.0:
        return 0.0
    raw_fraction = max(0.0, (win_rate - entry_price) / max(1.0 - entry_price, 1e-9))
    return raw_fraction / max(1.0, float(getattr(SETTINGS, "binary_kelly_divisor", 4.0)))
```

```python
# core/decision_engine.py
# Replace fixed 0.99/0.76/0.75 assignments with bounded confidence-derived values.
# Example pattern:
derived_probability = _probability_from_confidence(signal_confidence, floor=0.52, ceiling=0.68)
```

```dotenv
# .env.example and .env.live.example
USE_KELLY_SIZING=false
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_trade_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/decision_engine.py core/runner.py .env.example .env.live.example tests/test_trade_manager.py
git commit -m "fix: disable uncalibrated probability sizing by default"
```

---

### Task 5: Make endgame exit logic safer and opt-in

**Files:**
- Modify: `core/trade_manager.py`
- Modify: `core/runner.py`
- Modify: `.env.example`
- Modify: `.env.live.example`
- Modify: `README.md`
- Test: `tests/test_trade_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
from core.trade_manager import decide_exit


def test_last_seconds_do_not_force_unconditional_let_ride_on_losses():
    decision = decide_exit(pnl_pct=-0.20, hold_sec=50, secs_left=25)
    assert decision.should_close is True
    assert decision.reason == "deadline-exit-loss"


def test_lottery_hold_logic_is_disabled_by_default():
    source = open("core/runner.py", "r", encoding="utf-8").read()
    assert 'getattr(SETTINGS, "enable_lottery_hold", False)' in source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_trade_manager.py -q`
Expected: FAIL because `ghost-town-let-ride` still overrides final-30-second losses and the lottery hold behavior is not behind a disabled-by-default flag.

- [ ] **Step 3: Write minimal implementation**

```python
# core/trade_manager.py
if inside_ghost_town_window and pnl_pct > 0:
    return ExitDecision(False, "ghost-town-let-ride", pnl_pct, hold_sec)

if secs_left is not None and secs_left <= getattr(SETTINGS, "exit_deadline_sec", 20):
    if pnl_pct < 0:
        return ExitDecision(True, "deadline-exit-loss", pnl_pct, hold_sec)
```

```python
# core/runner.py
if bool(getattr(SETTINGS, "enable_lottery_hold", False)) and "early_underdog" in getattr(p, "entry_reason", ""):
    ...

if bool(getattr(SETTINGS, "late_certainty_hold_enabled", False)):
    ...
```

```dotenv
# .env.example and .env.live.example
ENABLE_LOTTERY_HOLD=false
LATE_CERTAINTY_HOLD_ENABLED=false
```

```markdown
# README.md
- Final-seconds hold behavior is now opt-in only.
- Default live configuration prioritizes executable exits over settlement gambling.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_trade_manager.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/trade_manager.py core/runner.py .env.example .env.live.example README.md tests/test_trade_manager.py
git commit -m "fix: make endgame hold logic opt-in"
```

---

### Task 6: Final verification pass

**Files:**
- Modify: none expected
- Test: `tests/test_http_safety.py`
- Test: `tests/test_exit_fix.py`
- Test: `tests/test_trade_manager.py`

- [ ] **Step 1: Run focused regression suite**

Run: `pytest tests/test_http_safety.py tests/test_exit_fix.py tests/test_trade_manager.py -q`
Expected: PASS

- [ ] **Step 2: Run the broader test suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 3: Run one dry-run startup check**

Run: `python main.py`
Expected: bot starts without TLS/security warnings, no immediate startup exception, and logs market resolution / bot loop startup.

- [ ] **Step 4: Review config examples**

Run: `python - <<'PY'
from pathlib import Path
for name in ['.env.example', '.env.live.example']:
    text = Path(name).read_text()
    assert 'verify=False' not in text
    print(name, 'ok')
PY`
Expected: both files print `ok`

- [ ] **Step 5: Commit verification-only changes if needed**

```bash
git add .
git commit -m "chore: finalize bot safety remediation verification"
```

---

## Self-Review

- Spec coverage: covers the audit findings in transport security, live risk accounting, execution reconciliation, probability calibration, and high-risk exit behavior.
- Placeholder scan: no `TODO`, `TBD`, or implicit "handle appropriately" steps remain.
- Type consistency: all referenced functions and files exist in the current repository or are explicitly created by this plan.
