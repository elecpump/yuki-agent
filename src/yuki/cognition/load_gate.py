import time
from collections.abc import Callable


class LoadGate:
    """Three-state model load gate: disabled, retryable failure, or ready."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        retry_window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._retry_window_s = max(0.0, float(retry_window_s))
        self._clock = clock
        self._failed_until: float | None = None

    def disabled(self) -> bool:
        return not self._enabled

    def can_load(self) -> bool:
        if not self._enabled:
            return False
        if self._failed_until is None:
            return True
        return self._clock() >= self._failed_until

    def mark_failure(self) -> None:
        self._failed_until = self._clock() + self._retry_window_s

    def mark_success(self) -> None:
        self._failed_until = None

    def error_message(self) -> str | None:
        if not self._enabled:
            return "model disabled"
        if not self.can_load():
            return "model load previously failed"
        return None

    def health(self) -> dict:
        retry_after = 0.0
        if self._failed_until is not None:
            retry_after = max(0.0, self._failed_until - self._clock())
        return {
            "enabled": self._enabled,
            "degraded": not self.can_load(),
            "retry_after_s": retry_after,
        }
