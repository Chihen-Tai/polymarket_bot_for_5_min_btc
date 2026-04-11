# BTC 5-Minute Profitability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce fee-driven and slippage-driven losses in the BTC 5-minute bot by making entries more selective, preserving maker-first execution discipline, and adding adaptive conservative behavior based on recent active-close quality.

**Architecture:** The implementation stays inside the current bot architecture instead of rewriting it into a full market maker. The main changes live in configuration and runner-side gating logic: tighter neutral-zone / fee-aware entry rules, explicit execution-cost filtering before maker→taker fallback, earlier weak-trade recycling, and a conservative mode triggered by recent active-close outcomes.

**Tech Stack:** Python, existing bot runtime in `core/runner.py`, configuration via `core/config.py`, execution logic in `core/exchange.py`, regression tests in `tests/test_trade_manager.py` and `tests/test_exit_fix.py`, report verification via `scripts/trade_pair_ledger.py` and generated run reports.

---

## File Structure

### Modify
- `core/config.py`
  - Add new environment-backed knobs for neutral-zone hard block, execution-cost buffers, and active-close conservative mode triggers.
- `core/runner.py`
  - Extend entry gating helpers (`required_trade_edge`, `summarize_entry_edge`, `score_entry_candidate`, `collect_ranked_entry_candidates`) to incorporate neutral-zone hard blocks and explicit execution-cost awareness.
  - Add conservative-mode decision helpers based on recent active-close trade quality.
  - Apply those helpers where ranked candidates are selected and where runtime behavior already uses conservative settings.
- `core/trade_manager.py`
  - Add or tighten earlier weak-trade cleanup rules so low-quality pre-principal positions recycle before expensive deadline exits.
- `tests/test_trade_manager.py`
  - Add regression coverage for the new entry gating, conservative mode triggers, and earlier weak-trade exit behavior.
- `tests/test_exit_fix.py`
  - Add execution-discipline coverage proving normal maker→taker fallback is skipped when projected post-cost edge is too thin.

### Verify / Read During Implementation
- `scripts/trade_pair_ledger.py`
  - Use for post-change verification; no code changes required unless the existing output proves insufficient.
- `data/latest_run_report.txt`
  - Compare pre/post behavior after implementation.

---

### Task 1: Add profitability-hardening configuration knobs

**Files:**
- Modify: `core/config.py`
- Test: `tests/test_trade_manager.py`

- [ ] **Step 1: Write the failing config-facing tests**

Add these assertions near the existing settings-driven checks in `tests/test_trade_manager.py`:

```python
    SETTINGS.entry_neutral_hard_block_half_width = 0.02
    SETTINGS.entry_execution_cost_buffer = 0.015
    SETTINGS.conservative_active_close_loss_streak = 3
    SETTINGS.conservative_active_close_fee_pnl_floor = -0.05

    checks.extend([
        (
            "required_trade_edge_respects_execution_cost_buffer",
            abs(required_trade_edge(0.55, 180, history_count=20) - 0.0556) < 1e-9,
        ),
        (
            "required_trade_edge_hard_blocks_neutral_center_prices",
            summarize_entry_edge(
                win_rate=0.58,
                entry_price=0.50,
                secs_left=180,
                history_count=20,
            )["blocked_reason"] == "neutral-no-trade-zone",
        ),
    ])
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
./.venv/bin/python3.14 tests/test_trade_manager.py
```

Expected: failure because `Settings` does not yet expose the new knobs and `summarize_entry_edge()` does not yet emit `blocked_reason` for a hard neutral block.

- [ ] **Step 3: Add the new settings in `core/config.py`**

Insert the new configuration fields alongside the existing entry/conservative settings:

```python
    entry_neutral_hard_block_half_width: float = _f("ENTRY_NEUTRAL_HARD_BLOCK_HALF_WIDTH", 0.02)
    entry_execution_cost_buffer: float = _f("ENTRY_EXECUTION_COST_BUFFER", 0.015)
    entry_require_maker_edge_buffer: float = _f("ENTRY_REQUIRE_MAKER_EDGE_BUFFER", 0.01)
    conservative_active_close_loss_streak: int = _i("CONSERVATIVE_ACTIVE_CLOSE_LOSS_STREAK", 3)
    conservative_active_close_fee_pnl_floor: float = _f("CONSERVATIVE_ACTIVE_CLOSE_FEE_PNL_FLOOR", -0.05)
    conservative_skip_windows: int = _i("CONSERVATIVE_SKIP_WINDOWS", 2)
```

- [ ] **Step 4: Run the test again to confirm the failure narrows**

Run:

```bash
./.venv/bin/python3.14 tests/test_trade_manager.py
```

Expected: the config attribute errors are gone, but the new assertions still fail because runner logic has not been updated yet.

- [ ] **Step 5: Commit the config-only groundwork**

```bash
git add core/config.py tests/test_trade_manager.py
git commit -m "plan: add profitability hardening config knobs"
```

### Task 2: Harden entry gating around neutral prices and execution cost

**Files:**
- Modify: `core/runner.py:2265-2448`
- Test: `tests/test_trade_manager.py`

- [ ] **Step 1: Extend the failing tests to cover the new gating output**

Add these checks after the existing edge/candidate assertions:

```python
    neutral_summary = summarize_entry_edge(
        win_rate=0.58,
        entry_price=0.50,
        secs_left=180,
        history_count=20,
    )
    off_center_summary = summarize_entry_edge(
        win_rate=0.62,
        entry_price=0.42,
        secs_left=180,
        history_count=20,
    )

    checks.extend([
        (
            "neutral_summary_reports_block_reason",
            neutral_summary["ok"] is False
            and neutral_summary["blocked_reason"] == "neutral-no-trade-zone",
        ),
        (
            "off_center_summary_keeps_reason_empty_when_allowed",
            off_center_summary["blocked_reason"] == "",
        ),
    ])
```

- [ ] **Step 2: Run the test to verify it fails for the expected reason**

Run:

```bash
./.venv/bin/python3.14 tests/test_trade_manager.py
```

Expected: failures because `summarize_entry_edge()` currently returns only `ok`, `raw_edge`, and `required_edge`, without a neutral hard-block reason.

- [ ] **Step 3: Implement the minimal runner-side gating changes**

Update `required_trade_edge()` and `summarize_entry_edge()` so they explicitly model neutral hard blocks and execution-cost buffers.

Target shape:

```python
def required_trade_edge(entry_price: float, secs_left: float | None, history_count: int = 0) -> float:
    required = max(0.0, float(getattr(SETTINGS, "edge_threshold", 0.0)))
    # existing history and time penalties...
    fee_floor = float(getattr(SETTINGS, "report_assumed_taker_fee_rate", 0.0156)) * 2.0
    required = max(required, fee_floor * float(getattr(SETTINGS, "entry_fee_floor_buffer", 1.0)))
    required += float(getattr(SETTINGS, "entry_execution_cost_buffer", 0.015) or 0.015)
    return required


def summarize_entry_edge(*, win_rate: float, entry_price: float, secs_left: float | None, history_count: int = 0) -> dict:
    raw_edge = win_rate - entry_price
    neutral_hard_block = abs(float(entry_price) - 0.5) <= float(
        getattr(SETTINGS, "entry_neutral_hard_block_half_width", 0.02) or 0.02
    )
    required = required_trade_edge(entry_price, secs_left, history_count=history_count)
    blocked_reason = "neutral-no-trade-zone" if neutral_hard_block else ""
    return {
        "win_rate": win_rate,
        "entry_price": entry_price,
        "raw_edge": raw_edge,
        "required_edge": required,
        "ok": (raw_edge >= required) and not neutral_hard_block,
        "blocked_reason": blocked_reason,
        "history_count": history_count,
    }
```

- [ ] **Step 4: Propagate the new blocking reason into candidate rejection notes**

Update the `collect_ranked_entry_candidates()` rejection path to surface the blocking reason rather than reporting only a numeric edge miss:

```python
        if not scored.get("ok"):
            rejection_notes.append(
                f"rank={idx} strategy={scored.get('strategy_name') or 'unknown'} "
                f"rejected={scored['entry_edge'].get('blocked_reason') or 'edge'} "
                f"raw={float(scored['entry_edge']['raw_edge']):.3f} "
                f"required={float(scored['entry_edge']['required_edge']):.3f}"
            )
            continue
```

- [ ] **Step 5: Run the tests to verify the gating behavior passes**

Run:

```bash
./.venv/bin/python3.14 tests/test_trade_manager.py
```

Expected: the new neutral-zone and execution-cost gating assertions pass, while any unrelated pre-existing failures remain isolated.

- [ ] **Step 6: Commit the entry-gating change**

```bash
git add core/runner.py tests/test_trade_manager.py core/config.py
git commit -m "plan: harden btc 5m entry gating"
```

### Task 3: Add adaptive conservative mode from recent active-close quality

**Files:**
- Modify: `core/runner.py`
- Test: `tests/test_trade_manager.py`

- [ ] **Step 1: Write the failing test for active-close-triggered conservative mode**

Add a minimal helper-level regression to `tests/test_trade_manager.py`:

```python
    negative_active_close_summary = {
        "close_bucket_pnl": {
            "active-close": {
                "count": 3,
                "fee_adjusted_actual_pnl": {"count": 3, "sum": -0.21, "average": -0.07},
            }
        }
    }

    checks.extend([
        (
            "conservative_mode_triggers_on_negative_active_close_streak",
            should_enable_profitability_conservative_mode(negative_active_close_summary) is True,
        ),
    ])
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
./.venv/bin/python3.14 tests/test_trade_manager.py
```

Expected: failure because `should_enable_profitability_conservative_mode()` does not exist.

- [ ] **Step 3: Implement the minimal helper in `core/runner.py`**

Add a small pure helper near the other decision utilities:

```python
def should_enable_profitability_conservative_mode(summary: dict | None) -> bool:
    if not bool(getattr(SETTINGS, "conservative_mode_enabled", False)):
        return False
    if not isinstance(summary, dict):
        return False
    active_close = ((summary.get("close_bucket_pnl") or {}).get("active-close") or {})
    count = int(active_close.get("count") or 0)
    fee_stats = active_close.get("fee_adjusted_actual_pnl") or {}
    average = fee_stats.get("average")
    return (
        count >= int(getattr(SETTINGS, "conservative_active_close_loss_streak", 3) or 3)
        and average is not None
        and float(average) <= float(getattr(SETTINGS, "conservative_active_close_fee_pnl_floor", -0.05) or -0.05)
    )
```

- [ ] **Step 4: Wire the helper into runtime conservative-mode selection**

Where the runner currently decides whether conservative settings should apply, add the profitability-based trigger to the existing checks. Keep this change minimal by extending the current conservative-mode decision instead of creating a second separate regime system.

- [ ] **Step 5: Run the test to verify the helper and wiring pass**

Run:

```bash
./.venv/bin/python3.14 tests/test_trade_manager.py
```

Expected: the new conservative-mode assertion passes.

- [ ] **Step 6: Commit the conservative-mode change**

```bash
git add core/runner.py tests/test_trade_manager.py core/config.py
git commit -m "plan: add profitability conservative mode"
```

### Task 4: Make normal taker fallback conditional on post-cost edge

**Files:**
- Modify: `core/runner.py`, `core/exchange.py`
- Test: `tests/test_exit_fix.py`

- [ ] **Step 1: Write the failing fallback-discipline test**

Add a case to `tests/test_exit_fix.py` that proves a weak edge should not escalate into routine taker fallback:

```python
    weak_edge_allowance = should_allow_normal_taker_fallback(
        raw_edge=0.03,
        required_edge=0.05,
        emergency=False,
    )
    strong_edge_allowance = should_allow_normal_taker_fallback(
        raw_edge=0.09,
        required_edge=0.05,
        emergency=False,
    )

    checks.extend([
        ("normal_taker_fallback_blocks_weak_post_cost_edge", weak_edge_allowance is False),
        ("normal_taker_fallback_allows_strong_post_cost_edge", strong_edge_allowance is True),
    ])
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
./.venv/bin/python3.14 tests/test_exit_fix.py
```

Expected: failure because the helper does not exist yet.

- [ ] **Step 3: Implement the minimal helper and use it in the entry path**

Add a pure helper in `core/runner.py`:

```python
def should_allow_normal_taker_fallback(*, raw_edge: float, required_edge: float, emergency: bool) -> bool:
    if emergency:
        return True
    maker_edge_buffer = float(getattr(SETTINGS, "entry_require_maker_edge_buffer", 0.01) or 0.01)
    return float(raw_edge) >= float(required_edge) + maker_edge_buffer
```

Then, in the code that chooses between maker-only behavior and maker→taker fallback for entries, use this helper so routine entries with marginal edge do not degrade into taker flow.

- [ ] **Step 4: Run the test to verify the helper passes**

Run:

```bash
./.venv/bin/python3.14 tests/test_exit_fix.py
```

Expected: the new fallback-discipline assertions pass and the existing exit-fix checks remain green.

- [ ] **Step 5: Commit the taker-discipline change**

```bash
git add core/runner.py core/exchange.py tests/test_exit_fix.py core/config.py
git commit -m "plan: restrict weak-edge taker fallback"
```

### Task 5: Recycle weak trades earlier instead of paying deadline cleanup costs

**Files:**
- Modify: `core/trade_manager.py`
- Test: `tests/test_trade_manager.py`

- [ ] **Step 1: Write the failing early-recycle exit tests**

Add two minimal `decide_exit()` checks to `tests/test_trade_manager.py`:

```python
    weak_trade_recycle = decide_exit(
        pnl_pct=-0.012,
        profit_pnl_pct=-0.012,
        hold_sec=40,
        secs_left=80,
        mfe_pnl_pct=0.01,
        has_taken_partial=False,
        has_extracted_principal=False,
    )
    healthy_trade_no_recycle = decide_exit(
        pnl_pct=0.015,
        profit_pnl_pct=0.015,
        hold_sec=40,
        secs_left=80,
        mfe_pnl_pct=0.05,
        has_taken_partial=False,
        has_extracted_principal=False,
    )

    checks.extend([
        ("weak_trade_recycles_before_deadline_zone", weak_trade_recycle.reason == "stalled-trade"),
        ("healthy_trade_is_not_force_recycled", healthy_trade_no_recycle.should_close is False),
    ])
```

- [ ] **Step 2: Run the test to verify it fails if behavior is not yet strict enough**

Run:

```bash
./.venv/bin/python3.14 tests/test_trade_manager.py
```

Expected: failure if current weak-trade recycling is too loose to catch the intended case.

- [ ] **Step 3: Implement the minimal exit-policy adjustment**

Tighten the existing stalled/failed-follow-through conditions instead of inventing a new exit regime. The change should stay inside `decide_exit()` and only make weak, low-MFE, low-conviction trades exit earlier when enough time remains.

Target shape:

```python
    if (
        hold_sec >= getattr(SETTINGS, "stalled_exit_window_sec", 35)
        and secs_left is not None
        and secs_left >= getattr(SETTINGS, "stalled_exit_min_secs_left", 45)
        and not has_extracted_principal
        and pnl_pct <= -getattr(SETTINGS, "stalled_exit_min_loss_pct", 0.01)
        and pnl_pct >= -getattr(SETTINGS, "stalled_exit_max_abs_pnl_pct", 0.02)
        and mfe_pnl_pct <= getattr(SETTINGS, "stalled_exit_max_mfe_pct", 0.02)
    ):
        return ExitDecision(True, "stalled-trade", pnl_pct, hold_sec)
```

If the current code already has this shape, only tune the relevant thresholds in `core/config.py` and update the test baselines to match the new intended stricter behavior.

- [ ] **Step 4: Run the tests to verify the earlier weak-trade recycle behavior passes**

Run:

```bash
./.venv/bin/python3.14 tests/test_trade_manager.py
```

Expected: the new stalled-trade behavior passes without breaking principal-extraction or stop-loss paths.

- [ ] **Step 5: Commit the exit-policy adjustment**

```bash
git add core/trade_manager.py tests/test_trade_manager.py core/config.py
git commit -m "plan: recycle weak btc 5m trades earlier"
```

### Task 6: Verification using both tests and trading-quality outputs

**Files:**
- Verify: `tests/test_exit_fix.py`
- Verify: `tests/test_trade_manager.py`
- Verify: `scripts/trade_pair_ledger.py`
- Verify: `data/latest_run_report.txt`

- [ ] **Step 1: Run the scoped regression suites**

Run:

```bash
./.venv/bin/python3.14 tests/test_exit_fix.py
./.venv/bin/python3.14 tests/test_trade_manager.py
./.venv/bin/pytest -q tests/test_exit_fix.py tests/test_hedge_exit.py
```

Expected:
- `tests/test_exit_fix.py` prints `OK`
- `tests/test_trade_manager.py` passes for the updated entry/exit policy baseline
- pytest reports the scoped suites green

- [ ] **Step 2: Run compile verification on the touched files**

Run:

```bash
./.venv/bin/python3.14 -m py_compile core/config.py core/runner.py core/trade_manager.py tests/test_trade_manager.py tests/test_exit_fix.py tests/test_hedge_exit.py
```

Expected: no output, exit code 0

- [ ] **Step 3: Run ledger/report verification commands**

Run:

```bash
./.venv/bin/python3.14 scripts/trade_pair_ledger.py --limit 30 --summary
```

Then inspect:

```bash
git diff -- data/latest_run_report.txt
```

Expected evaluation points:
- no increase in taker-heavy active-close leakage,
- fewer `deadline-*` active-close outcomes if new run data exists,
- improved or at least less-negative `active-close` fee-adjusted pnl,
- lower evidence of neutral-price churn.

- [ ] **Step 4: If report output is missing or too sparse, document that limitation explicitly**

Record in the implementation notes whether the repository has enough new trade rows to evaluate trading-quality acceptance. Do **not** claim performance success from only unit tests.

- [ ] **Step 5: Commit the verified hardening pass**

```bash
git add core/config.py core/runner.py core/trade_manager.py tests/test_trade_manager.py tests/test_exit_fix.py
git commit -m "feat: harden btc 5m profitability filters"
```

---

## Self-Review

### Spec coverage

- **Stricter neutral-zone / fee-aware entry gating** → Task 1, Task 2
- **Stricter maker-first execution discipline** → Task 4
- **Adaptive conservative mode from active-close quality** → Task 3
- **Earlier weak-trade recycling** → Task 5
- **Tests and ledger/report verification** → Task 6

No spec sections are currently uncovered.

### Placeholder scan

No `TBD`, `TODO`, or deferred “implement later” placeholders remain in the plan.

### Type / naming consistency

Planned helper names are consistent across tasks:
- `should_enable_profitability_conservative_mode`
- `should_allow_normal_taker_fallback`
- `build_take_profit_principal_exit_event` remains the existing helper name already present in the codebase

---

Plan complete and saved to `docs/superpowers/plans/2026-04-11-btc-5m-profitability-hardening.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
