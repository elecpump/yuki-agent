from __future__ import annotations

import math
import time
from collections import deque

from yuki.memory.store import MemoryStore


class Reflector:
    """反思生成接口。无 LLM，本次不可用；LLM 接入后实现 generate 并落库为 reflection。"""

    def generate(self, scenario_ids: list[int], context: dict | None = None) -> list[str]:
        raise NotImplementedError("reflection generation requires an LLM (future)")


class ShortTermMemory:
    """短期（工作）记忆：进程内 TTL 队列，不落盘。"""

    def __init__(self, ttl_s: float = 1800, capacity: int = 50) -> None:
        self._ttl = ttl_s
        self._cap = capacity
        self._items: deque[dict] = deque()

    def add(self, content: str, *, kind: str = "event", at: float | None = None) -> None:
        self._items.append({"content": content, "kind": kind, "ts": time.time() if at is None else at})
        while len(self._items) > self._cap:
            self._items.popleft()

    def items(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        fresh = [it for it in self._items if now - it["ts"] <= self._ttl]
        self._items = deque(fresh, maxlen=self._cap)
        return list(reversed(fresh))

    def recent(self, n: int = 5, now: float | None = None) -> list[dict]:
        return self.items(now)[:n]

    def clear(self) -> None:
        self._items.clear()


class MemoryManager:
    """记忆门面：衰减加权检索、清理策略、短期工作记忆。"""

    def __init__(
        self,
        store: MemoryStore,
        *,
        decay_base: float = 1.0,
        decay_lambda: float = 0.1,
        decay_threshold: float = 0.02,
        short_term_ttl_s: float = 1800,
        short_term_capacity: int = 50,
        short_term: ShortTermMemory | None = None,
    ) -> None:
        self._store = store
        self._base = decay_base
        self._lam = decay_lambda
        self._threshold = decay_threshold
        self._short_term = short_term or ShortTermMemory(
            ttl_s=short_term_ttl_s, capacity=short_term_capacity,
        )

    def write(
        self,
        memory_type: str,
        content: str,
        *,
        confidence: float = 0.5,
        sensitivity: int = 0,
        source: str = "cli",
        metadata: dict | None = None,
    ) -> int:
        return self._store.create(
            memory_type, content,
            confidence=confidence, sensitivity=sensitivity,
            source=source, metadata=metadata,
        )

    def get(self, memory_id: int) -> dict | None:
        mem = self._store.get(memory_id)
        if mem is not None:
            self._store.touch(memory_id)
        return mem

    def touch(self, memory_id: int) -> None:
        self._store.touch(memory_id)

    def delete(self, memory_id: int) -> bool:
        return self._store.delete(memory_id)

    def list(self, *, memory_type: str | None = None, min_sensitivity: int = 0) -> list[dict]:
        return self._store.list(memory_type=memory_type, min_sensitivity=min_sensitivity)

    def query(
        self,
        text: str,
        *,
        memory_type: str | None = None,
        top_k: int = 5,
        min_sensitivity: int = 0,
        touch: bool = True,
    ) -> list[dict]:
        now = time.time()
        hits = self._store.search(
            text, memory_type=memory_type, top_k=top_k * 3, min_sensitivity=min_sensitivity,
        )
        scored: list[dict] = []
        for mem, rank in hits:
            mem["score"] = rank * self.decay_weight(mem, now)
            scored.append(mem)
        scored.sort(key=lambda m: m["score"], reverse=True)
        returned = scored[:top_k]
        if touch:
            for mem in returned:
                self._store.touch(mem["id"])
        return returned

    def decay_weight(self, memory: dict, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if memory["strengthened"]:
            return 1.0
        days = max(0.0, (now - memory["last_access"]) / 86400.0)
        return self._base * math.exp(-self._lam * days)

    def strengthen(self, memory_id: int) -> bool:
        return self._store.strengthen(memory_id)

    def cleanup(self) -> int:
        now = time.time()
        deleted = 0
        for mem in self._store.all():
            if mem["memory_type"] == "personal" or mem["strengthened"]:
                continue
            if self.decay_weight(mem, now) < self._threshold:
                if self._store.delete(mem["id"]):
                    deleted += 1
        return deleted

    def wipe(self) -> int:
        return self._store.wipe()

    def ping(self) -> bool:
        return self._store.ping()

    def short_term_add(self, content: str, *, kind: str = "event", at: float | None = None) -> None:
        self._short_term.add(content, kind=kind, at=at)

    def short_term_items(self) -> list[dict]:
        return self._short_term.items()

    def short_term_clear(self) -> None:
        self._short_term.clear()

    def close(self) -> None:
        self._store.close()
