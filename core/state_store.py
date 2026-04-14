import json
from dataclasses import asdict

from core.runtime_paths import runtime_state_path


def _state_path():
    return runtime_state_path()


def load_state() -> dict:
    path = _state_path()
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(path)


def serialize_positions(positions) -> list[dict]:
    return [asdict(p) for p in positions]
