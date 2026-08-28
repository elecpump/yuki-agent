from collections.abc import Iterator
from typing import Protocol


class CallTracker(Protocol):
    """Minimal call-metrics surface model objects attach to.

    The model worker attaches its ``ModelManager`` (which implements
    ``track_call``) so inference success/failure and latency are recorded
    once per worker-side call; in-process consumers leave it unset and the
    model's own tracking becomes a no-op.
    """

    def track_call(self, model: str) -> Iterator[None]: ...
