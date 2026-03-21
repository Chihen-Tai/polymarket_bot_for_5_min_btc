import json
from dataclasses import asdict
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent / ".runtime_state.json"


def load_state() -> dict:
    try:
        if not STATE_PATH.exists():
            return {}
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_PATH)


def serialize_positions(positions) -> list[dict]:
    return [asdict(p) for p in positions]
