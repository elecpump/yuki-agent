import threading
import time
from typing import Callable

from yuki.topics import Topics


class StableContentObservation:
    """Turns raw focus/scroll activity into content-ready events after a frame is stored."""

    def __init__(self, bus, clock: Callable[[], float] = time.time) -> None:
        self._bus = bus
        self._clock = clock
        self._lock = threading.Lock()
        self._last_focus: dict = {}
        self._pending_reason: str | None = None

    def on_focus_changed(self, payload: dict) -> None:
        with self._lock:
            self._last_focus = dict(payload)
            self._pending_reason = "focus_changed"

    def on_scroll_activity(self) -> None:
        with self._lock:
            self._pending_reason = "scroll_idle"

    def on_frame_stored(self, frame: dict) -> None:
        frame_id = frame.get("frame_id")
        if frame_id is None:
            return
        with self._lock:
            reason = self._pending_reason
            if reason is None:
                return
            self._pending_reason = None
            payload = dict(self._last_focus)

        payload.setdefault("app", "")
        payload.setdefault("url", "")
        payload.setdefault("title", "")
        payload.update({
            "reason": reason,
            "frame_id": frame_id,
            "ts": self._clock(),
            "frame_ts": frame.get("ts", 0.0),
            "frame_width": frame.get("width", 0),
            "frame_height": frame.get("height", 0),
            "sensitive": bool(frame.get("sensitive", False)),
        })
        self._bus.publish(Topics.CONTENT_READY, payload)
