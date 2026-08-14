import json
import time
from pathlib import Path

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.soul")


class SoulStore:
    """soul 状态：版本化 json 参数记录（环1 调参落点，环3 人格快照前身）。

    只存参数（proactive_cooldown_s 等），不存 persona 提示词。
    文件缺失/损坏/名字或版本不符 → load 返回 None，调用方回默认。
    """

    def __init__(self, path: str | Path, persona_name: str, persona_version: int = 1) -> None:
        self._path = Path(path)
        self._persona_name = persona_name
        self._persona_version = persona_version

    def load(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("soul read failed", error=str(exc))
            return None
        if not isinstance(data, dict):
            return None
        if data.get("persona_name") != self._persona_name or data.get("persona_version") != self._persona_version:
            return None
        params = data.get("params")
        if not isinstance(params, dict):
            return None
        return params

    def save(self, params: dict) -> None:
        payload = {
            "persona_name": self._persona_name,
            "persona_version": self._persona_version,
            "params": params,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("soul write failed", error=str(exc))

    def reset(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("soul reset failed", error=str(exc))
