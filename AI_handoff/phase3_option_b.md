# Phase 3 Option B Handoff

Generated: 2026-04-19

## Scope Completed In Repo

Option B required two repo-local fixes before any VPN retune could matter:

1. `.env.local` had to become effective even when `python-dotenv` is missing.
2. Shadow data collection flags had to be wired to the env values the operator was already using.

Those are now in place.

## Repo Changes Applied

### Config loading

- `core/config.py` now has a simple `.env` fallback parser.
- Result: `.env`, `.env.local`, and `.env.secrets` now load even without `python-dotenv`.

### Shadow data collection

- `ENABLE_SHADOW_JOURNAL=true` now drives `SETTINGS.enable_shadow_journal`.
- `SHADOW_MODE=true` is now set in `.env.local`.

### VPN latency retune

`.env.local` now sets:

- `MAX_VPN_LATENCY_MS=900`
- `VPN_AUTO_CALIBRATE_LATENCY=true`
- `VPN_LATENCY_MULTIPLIER=1.2`
- `VPN_LATENCY_FLOOR_MS=900`

### Latency gate behavior

`core/latency_monitor.py` now computes:

- `effective_max_vpn_latency_ms = max(MAX_VPN_LATENCY_MS, current_p95_rtt * VPN_LATENCY_MULTIPLIER, VPN_LATENCY_FLOOR_MS)`

With the sample set `[780, 800, 812, 830, 850]`, verification produced:

- `effective_max_vpn_latency_ms = 1020.0`
- `blocked = False`

## Manual Operator Step Still Required

I cannot switch the host VPN exit from inside the repo.

To complete Option B operationally:

1. Change the VPN exit from Japan to the closest Seoul or Singapore exit with the best Europe peering available from the provider.
2. Keep `DRY_RUN=true`.
3. Keep `SHADOW_MODE=true`.
4. Run for at least 1 hour and inspect:
   - `cycle metrics` log lines
   - `data/shadow_journal.csv`

## What To Verify After The VPN Exit Change

Target checks for Option B:

- `VPN_LATENCY_BLOCK` count drops materially versus the Japan exit path
- the strategy layer sees a large increase in eligible decisions
- `data/shadow_journal.csv` grows continuously during the run
- no live orders are placed because `DRY_RUN=true`

Stretch target from the brief:

- `>= 100` entry-layer decisions per hour reach the strategy instead of dying at the gate

## Current Limitation

I did not run a full one-hour dry-run from this shell because the local runtime environment is incomplete for a full bot session. The repo-side retune and shadow-mode collection path were verified with focused tests and smoke checks, but the operational verification still depends on the manual VPN exit switch and an actual dry-run session on the operator machine.
