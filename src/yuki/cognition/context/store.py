from typing import Protocol

from yuki.memory.manager import MemoryManager


class TurnStore(Protocol):
    """会话轮次存储接口（未来 Redis 实现同协议即可替换）。"""

    def add(self, content: str, kind: str, ts: float) -> None: ...
    def items(self) -> list[dict]: ...
    def clear(self) -> None: ...


class ShortTermTurnStore:
    """默认实现：包装 MemoryManager.short_term（TTL 30min/容量 50）。"""

    def __init__(self, manager: MemoryManager) -> None:
        self._manager = manager

    def add(self, content: str, kind: str, ts: float) -> None:
        self._manager.short_term_add(content, kind=kind, at=ts)

    def items(self) -> list[dict]:
        return self._manager.short_term_items()

    def clear(self) -> None:
        self._manager.short_term_clear()
