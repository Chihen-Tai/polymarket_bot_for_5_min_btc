from __future__ import annotations

import atexit
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

RUN_JOURNAL_PATH = Path(__file__).resolve().parent.parent / "data" / "run_journal.jsonl"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RunJournal:
    def __init__(self, notes: list[str] | None = None, recovery_restart: bool = False):
        self.run_id = f"run_{int(time.time())}_{os.getpid()}_{uuid4().hex[:6]}"
        self.pid = os.getpid()
        self.started_at = _now_iso()
        self._finalized = False
        self._pending_signal: str | None = None
        self._start_notes = list(notes or [])
        self._record({
            "kind": "run_started",
            "run_id": self.run_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "status": "started",
            "reason": "recovery-restart" if recovery_restart else "normal-start",
            "notes": self._start_notes,
        })
        atexit.register(self._atexit_finalize)

    def _record(self, row: dict[str, Any]) -> None:
        with RUN_JOURNAL_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def mark_signal(self, sig: int) -> None:
        try:
            self._pending_signal = signal.Signals(sig).name
        except Exception:
            self._pending_signal = str(sig)

    def finalize(self, *, status: str, reason: str, notes: list[str] | None = None) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._record({
            "kind": "run_stopped",
            "run_id": self.run_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "stop_at": _now_iso(),
            "status": status,
            "reason": reason,
            "signal": self._pending_signal,
            "notes": list(notes or []),
        })

    def _atexit_finalize(self) -> None:
        if self._finalized:
            return
        reason = "clean-exit"
        status = "stopped"
        if self._pending_signal == "SIGINT":
            reason = "manual-stop"
            status = "terminated"
        elif self._pending_signal == "SIGTERM":
            reason = os.getenv("BOT_STOP_REASON", "timeout-or-sigterm")
            status = "terminated"
        self.finalize(status=status, reason=reason, notes=["finalized via atexit"])