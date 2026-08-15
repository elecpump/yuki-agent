import difflib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.snapshots")


@dataclass(frozen=True)
class PersonaSnapshot:
    version: int
    persona_prompt: str
    params: dict
    created_at: float
    locked: bool = False


class PersonaStore:
    """人格快照版本历史：生成即版本、跳过相同、cap 清理（锁定豁免、v1 保留）、回滚/重置/导出。"""

    def __init__(self, path: str | Path, *, max_versions: int = 50,
                 persona_name: str = "yuki") -> None:
        self._path = Path(path)
        self._max_versions = max_versions
        self._persona_name = persona_name
        self._versions: dict[int, dict] = {}
        self._active: int | None = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("persona store load failed")
            return
        for v in data.get("versions") or []:
            if isinstance(v, dict) and isinstance(v.get("version"), int):
                self._versions[v["version"]] = v
        self._active = data.get("active")

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "persona_name": self._persona_name,
                "active": self._active,
                "versions": [self._versions[k] for k in sorted(self._versions)],
            }
            self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("persona store save failed", error=str(exc))

    def _as_snapshot(self, v: dict) -> PersonaSnapshot:
        return PersonaSnapshot(
            version=v["version"],
            persona_prompt=v["persona_prompt"],
            params=v.get("params", {}),
            created_at=v.get("created_at", 0.0),
            locked=bool(v.get("locked", False)),
        )

    def active(self) -> PersonaSnapshot | None:
        if self._active is None:
            return None
        v = self._versions.get(self._active)
        return self._as_snapshot(v) if v else None

    def list_versions(self) -> list[PersonaSnapshot]:
        return [self._as_snapshot(self._versions[k]) for k in sorted(self._versions)]

    def save(self, persona_prompt: str, params: dict) -> PersonaSnapshot | None:
        current = self.active()
        if current is not None and current.persona_prompt == persona_prompt and current.params == params:
            return None  # 跳过相同
        version = (max(self._versions) if self._versions else 0) + 1
        self._versions[version] = {
            "version": version, "persona_prompt": persona_prompt, "params": params,
            "created_at": time.time(), "locked": False,
        }
        self._active = version
        self._prune()
        self._persist()
        return self._as_snapshot(self._versions[version])

    def _prune(self) -> None:
        removable = [v for v in sorted(self._versions)
                     if v != 1 and not self._versions[v].get("locked")]
        while len(self._versions) > self._max_versions and removable:
            oldest = removable.pop(0)
            del self._versions[oldest]
            if self._active == oldest:
                self._active = min(self._versions) if self._versions else None

    def rollback(self, version: int) -> None:
        if version not in self._versions:
            raise ValueError(f"unknown version: {version}")
        self._active = version
        self._persist()

    def lock(self, version: int) -> None:
        if version not in self._versions:
            raise ValueError(f"unknown version: {version}")
        self._versions[version]["locked"] = True
        self._persist()

    def reset(self) -> None:
        keep = {1: self._versions[1]} if 1 in self._versions else {}
        self._versions = keep
        self._active = 1 if 1 in self._versions else None
        self._persist()

    def diff(self, v1: int, v2: int) -> str:
        a = self._versions[v1]["persona_prompt"].splitlines()
        b = self._versions[v2]["persona_prompt"].splitlines()
        return "\n".join(difflib.unified_diff(a, b, fromfile=f"v{v1}", tofile=f"v{v2}"))

    def export(self, version: int) -> dict:
        if version not in self._versions:
            raise ValueError(f"unknown version: {version}")
        return dict(self._versions[version])

    def import_snapshot(self, data: dict) -> None:
        if not isinstance(data.get("persona_prompt"), str) or not isinstance(data.get("version"), int):
            raise ValueError("invalid snapshot")
        version = data["version"]
        self._versions[version] = {
            "version": version,
            "persona_prompt": data["persona_prompt"],
            "params": data.get("params", {}),
            "created_at": data.get("created_at", time.time()),
            "locked": bool(data.get("locked", False)),
        }
        self._persist()
