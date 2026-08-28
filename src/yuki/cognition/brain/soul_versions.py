import copy
import json
from collections.abc import Callable
from pathlib import Path

from yuki.cognition.brain.soul_contract import SoulRestoreError
from yuki.logger import get_logger
from yuki.persistence import atomic_write_json

logger = get_logger("yuki.cognition.brain.soul_versions")


class SoulVersionStore:
    """Versioned Soul snapshots with coalescing and bounded retention."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_versions: int = 50,
        min_snapshot_interval_s: float = 60.0,
        clock: Callable[[], float],
    ) -> None:
        self._directory = Path(directory)
        self._max_versions = max(1, int(max_versions))
        self._min_snapshot_interval_s = max(0.0, float(min_snapshot_interval_s))
        self._clock = clock

    def ensure_baseline(self, soul: dict) -> None:
        path = self._path(int(soul.get("revision", 0)))
        if not path.exists():
            self._write(soul, self._clock())

    def stage(self, soul: dict) -> tuple[Path, float]:
        saved_at = self._clock()
        path = self._path(int(soul.get("revision", 0)))
        self._write(soul, saved_at)
        return path, saved_at

    def discard(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("staged soul snapshot cleanup failed", path=str(path))

    def finalize(self, revision: int, saved_at: float) -> None:
        current_path = self._path(revision)
        previous_paths = [path for path in self._paths() if path != current_path]
        if self._min_snapshot_interval_s > 0 and previous_paths:
            previous = previous_paths[-1]
            previous_revision, previous_saved_at = self._metadata(previous)
            if (
                previous_revision > 0
                and saved_at - previous_saved_at < self._min_snapshot_interval_s
            ):
                try:
                    previous.unlink()
                except OSError:
                    logger.warning("soul snapshot coalesce failed", path=str(previous))
        self._prune()

    def load(self, revision: int, *, current_revision: int) -> dict:
        if revision > current_revision:
            raise SoulRestoreError(f"uncommitted soul revision: {revision}")
        path = self._path(revision)
        if not path.exists():
            raise SoulRestoreError(f"unknown soul revision: {revision}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SoulRestoreError(f"invalid soul snapshot: {revision}") from exc
        restored = payload.get("soul") if isinstance(payload, dict) else None
        if not isinstance(restored, dict):
            raise SoulRestoreError(f"invalid soul snapshot: {revision}")
        return copy.deepcopy(restored)

    def list_revisions(self, *, current_revision: int) -> list[int]:
        revisions = []
        for path in self._paths():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                soul = payload.get("soul") if isinstance(payload, dict) else None
                if not isinstance(soul, dict):
                    continue
                revision = int(soul.get("revision"))
                path_revision = int(path.stem.removeprefix("soul_snapshot_r"))
            except (OSError, TypeError, ValueError):
                continue
            if revision != path_revision or not 0 <= revision <= current_revision:
                continue
            revisions.append(revision)
        return sorted(set(revisions))

    def _path(self, revision: int) -> Path:
        return self._directory / f"soul_snapshot_r{revision:06d}.json"

    def _paths(self) -> list[Path]:
        if not self._directory.exists():
            return []
        return sorted(self._directory.glob("soul_snapshot_r*.json"))

    def _write(self, soul: dict, saved_at: float) -> None:
        atomic_write_json(
            self._path(int(soul.get("revision", 0))),
            {"saved_at": saved_at, "soul": copy.deepcopy(soul)},
        )

    def _metadata(self, path: Path) -> tuple[int, float]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            revision = int((payload.get("soul") or {}).get("revision", 0))
            saved_at = float(payload.get("saved_at", 0.0))
            return revision, saved_at
        except (OSError, TypeError, ValueError):
            return 0, 0.0

    def _prune(self) -> None:
        paths = self._paths()
        while len(paths) > self._max_versions:
            victim = paths[0]
            try:
                victim.unlink()
            except OSError:
                logger.warning("soul snapshot prune failed", path=str(victim))
                return
            paths = self._paths()
