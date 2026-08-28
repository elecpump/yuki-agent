import json
import time
from pathlib import Path

from yuki.logger import get_logger
from yuki.persistence import atomic_write_json

logger = get_logger("yuki.cognition.brain.tuner_state")

COOLDOWN_KEY = "proactive_cooldown_s"
FLOOR_KEY = "cooldown_floor_s"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class TunerStateStore:
    """Runtime tuning state, intentionally separate from the persona kernel."""

    def __init__(self, path: str | Path, persona_name: str = "yuki") -> None:
        self._path = Path(path)
        self._persona_name = persona_name

    def load(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("tuner state read failed", error=str(exc))
            return None
        if not isinstance(data, dict):
            return None
        if data.get("persona_name", self._persona_name) != self._persona_name:
            return None
        return data

    def save(self, params: dict) -> None:
        current = self.load() or {}
        payload = {
            "persona_name": self._persona_name,
            **{
                key: value
                for key, value in current.items()
                if key not in ("persona_name", "updated_at")
            },
            **params,
            "updated_at": _now_iso(),
        }
        try:
            atomic_write_json(self._path, payload)
        except OSError as exc:
            logger.warning("tuner state write failed", error=str(exc))

    def reset(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("tuner state reset failed", error=str(exc))
