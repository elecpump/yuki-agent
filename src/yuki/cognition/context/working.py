import threading
import time

from yuki.cognition.context.store import ResponseState, TurnStore


class WorkingContext:
    """写入侧：追加持久化会话轮次并维护当前情境。

    决策用 ContextProjector 投影只读快照，不直接读本对象。
    """

    def __init__(self, store: TurnStore) -> None:
        self._store = store
        self._situation: dict | None = None
        self._closed = False
        self._lock = threading.RLock()

    def add_user(self, text: str) -> int | None:
        return self._add("user", text)

    def add_agent(self, text: str, *, reply_to_turn_id: int | None = None) -> int | None:
        return self._add("agent", text, reply_to_turn_id=reply_to_turn_id)

    def _add(
        self,
        kind: str,
        text: str,
        *,
        reply_to_turn_id: int | None = None,
    ) -> int | None:
        with self._lock:
            now = time.time()
            if kind == "user":
                turn_id = self._store.add_user(text, at=now)
            else:
                turn_id = self._store.add_agent(
                    text,
                    at=now,
                    reply_to_turn_id=reply_to_turn_id,
                )
            return turn_id

    def mark_response(self, user_turn_id: int, state: ResponseState) -> None:
        with self._lock:
            self._store.mark_response(user_turn_id, state)

    def update_situation(self, payload: dict) -> None:
        with self._lock:
            self._situation = payload

    def situation(self) -> dict | None:
        with self._lock:
            return self._situation

    def turn_count(self) -> int:
        with self._lock:
            return len(self._store.items())

    def items(self) -> list[dict]:
        with self._lock:
            return self._store.items()

    def projection_items(self) -> tuple[list[dict], list[dict]]:
        with self._lock:
            return self._store.projection_items()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._store.close()
            self._closed = True
