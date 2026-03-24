import logging
import os
import ssl
import sys
from datetime import datetime
from pathlib import Path

try:
    _create_unverified_https_context = getattr(ssl, '_create_unverified_context')
    ssl._create_default_https_context = _create_unverified_https_context
except AttributeError:
    pass


class _Tee:
    """Mirrors writes to both the original stream and a log file."""
    def __init__(self, original, log_path: Path):
        self._orig = original
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(log_path, "a", encoding="utf-8", buffering=1)

    def write(self, data):
        self._orig.write(data)
        self._file.write(data)

    def flush(self):
        self._orig.flush()
        self._file.flush()

    def close(self):
        self._file.close()

    # Proxy other attributes (e.g. .fileno) to the original stream
    def __getattr__(self, name):
        return getattr(self._orig, name)


if __name__ == '__main__':
    from core.config import SETTINGS
    data_dir = Path(__file__).resolve().parent / "data"
    mode_tag = "dryrun" if SETTINGS.dry_run else "live"
    _ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = data_dir / f"log-{mode_tag}-{_ts}.txt"

    tee = _Tee(sys.stdout, log_path)
    sys.stdout = tee  # type: ignore[assignment]

    print(f"[log] Output mirrored to: {log_path}", flush=True)

    try:
        from core.runner import main
        main()
    finally:
        sys.stdout = tee._orig
        tee.close()
