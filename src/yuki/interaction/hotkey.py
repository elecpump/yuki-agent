import time
from typing import Callable


class HotkeyManager:
    """全局热键管理器。Phase 4 接入真实 Windows 全局热键。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[], None]] = {}

    def register(self, name: str, handler: Callable[[], None]) -> None:
        self._handlers[name] = handler

    def trigger(self, name: str) -> None:
        if name in self._handlers:
            self._handlers[name]()

    def run(self) -> None:
        while True:
            time.sleep(1)
