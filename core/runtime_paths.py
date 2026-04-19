from __future__ import annotations

from pathlib import Path

from core.config import SETTINGS


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
CORE_DIR = Path(__file__).resolve().parent


def mode_label(*, dry_run: bool | None = None) -> str:
    active = SETTINGS.dry_run if dry_run is None else dry_run
    return "dryrun" if active else "live"


def trade_journal_path(*, dry_run: bool | None = None) -> Path:
    return DATA_DIR / f"trade_journal-{mode_label(dry_run=dry_run)}.jsonl"


def shadow_journal_csv_path() -> Path:
    return DATA_DIR / "shadow_journal.csv"


def run_journal_path(*, dry_run: bool | None = None) -> Path:
    return DATA_DIR / f"run_journal-{mode_label(dry_run=dry_run)}.jsonl"


def runtime_state_path(*, dry_run: bool | None = None) -> Path:
    return CORE_DIR / f".runtime_state-{mode_label(dry_run=dry_run)}.json"
