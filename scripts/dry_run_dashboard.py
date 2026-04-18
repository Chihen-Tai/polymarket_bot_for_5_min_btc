"""Dry-run decision logger.

Appends one row per poll cycle to ``data/decisions.csv`` so that every
decision the bot considers is recorded for later analysis (Phase 2).

Usage — call ``log_decision(...)`` from the runner's poll loop.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

_CSV_PATH: Path | None = None
_HEADER = [
    "ts", "slug", "secs_left", "up", "down",
    "fv_yes", "edge_up", "edge_down", "fee_bps",
    "rtt_ms", "blocked_reasons",
]


def _ensure_csv(path: Path) -> None:
    global _CSV_PATH
    _CSV_PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(_HEADER)


def log_decision(
    *,
    slug: str,
    secs_left: float | None,
    up: float,
    down: float,
    fv_yes: float | None,
    edge_up: float,
    edge_down: float,
    fee_bps: float,
    rtt_ms: float | None = None,
    blocked_reasons: str = "",
    data_dir: str | None = None,
) -> None:
    """Append one decision row to ``data/decisions.csv``."""
    if data_dir is None:
        data_dir = os.getenv(
            "DATA_DIR",
            str(Path(__file__).resolve().parent.parent / "data"),
        )
    path = Path(data_dir) / "decisions.csv"
    _ensure_csv(path)

    row = [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        slug or "",
        f"{secs_left:.1f}" if secs_left is not None else "",
        f"{up:.4f}",
        f"{down:.4f}",
        f"{fv_yes:.4f}" if fv_yes is not None else "",
        f"{edge_up:.4f}",
        f"{edge_down:.4f}",
        f"{fee_bps:.1f}",
        f"{rtt_ms:.0f}" if rtt_ms is not None else "",
        blocked_reasons,
    ]
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow(row)
