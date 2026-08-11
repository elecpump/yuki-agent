import signal
import threading


class ShutdownManager:
    """注册 SIGINT/SIGTERM/SIGBREAK，提供优雅关闭事件。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def register_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass

    def _handle(self, signum, frame) -> None:
        self._event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def request_shutdown(self) -> None:
        self._event.set()
