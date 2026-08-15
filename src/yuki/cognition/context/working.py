import json
import time
from pathlib import Path

from yuki.cognition.context.store import TurnStore
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.context.working")


class WorkingContext:
    """写入侧：追加会话轮次/情境 + 尽力持久化。

    决策用 ContextProjector 投影只读快照，不直接读本对象。
    """

    def __init__(self, store: TurnStore, *, snapshot_path: str | Path | None = None,
                 snapshot_interval: int = 5, ttl_s: float = 1800.0) -> None:
        self._store = store
        self._snapshot_path = Path(snapshot_path) if snapshot_path else None
        self._snapshot_interval = snapshot_interval
        self._ttl_s = ttl_s
        self._situation: dict | None = None
        self._add_count = 0

    def add_user(self, text: str) -> None:
        self._add("user", text)

    def add_agent(self, text: str) -> None:
        self._add("agent", text)

    def _add(self, kind: str, text: str) -> None:
        self._store.add(text, kind, time.time())
        self._add_count += 1
        if self._snapshot_path is not None and self._add_count % self._snapshot_interval == 0:
            self.snapshot()

    def update_situation(self, payload: dict) -> None:
        self._situation = payload

    def situation(self) -> dict | None:
        return self._situation

    def turn_count(self) -> int:
        return len(self._store.items())

    def items(self) -> list[dict]:
        return self._store.items()

    def snapshot(self) -> None:
        if self._snapshot_path is None:
            return
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "turns": [{"content": t["content"], "kind": t["kind"], "ts": t["ts"]}
                          for t in reversed(self._store.items())],
                "situation": self._situation,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._snapshot_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("context snapshot failed", error=str(exc))

    def restore(self) -> None:
        if self._snapshot_path is None or not self._snapshot_path.exists():
            return
        try:
            data = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("context restore failed", error=str(exc))
            return
        now = time.time()
        for turn in data.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            ts = turn.get("ts", now)
            if isinstance(ts, (int, float)) and now - ts <= self._ttl_s:
                self._store.add(turn.get("content", ""), turn.get("kind", "turn"), ts)
        situation = data.get("situation")
        if isinstance(situation, dict):
            self._situation = situation

    def close(self) -> None:
        self.snapshot()
