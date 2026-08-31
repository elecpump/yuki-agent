import signal
import threading
from typing import Callable
from yuki.logger import get_logger

logger = get_logger("yuki.shutdown")


class ShutdownManager:
    """注册 SIGINT/SIGTERM/SIGBREAK，提供优雅关闭事件与优先级清理。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._cleanups: list[tuple[int, str, Callable[[], None]]] = []

    def register_cleanup(self, name: str, fn: Callable[[], None], priority: int = 0) -> None:
        self._cleanups.append((priority, name, fn))

    def run_cleanups(self) -> None:
        for _, name, fn in sorted(self._cleanups, key=lambda item: item[0], reverse=True):
            try:
                fn()
            except Exception:
                logger.warning("cleanup failed", name=name, exc_info=True)
                pass

    def register_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass

    def _handle(self, _signum, _frame) -> None:
        self._event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def request_shutdown(self) -> None:
        self._event.set()
