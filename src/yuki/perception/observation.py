import threading
import time
from collections import deque
from typing import Callable

from yuki.topics import Topics


class StableContentObservation:
    """Turns raw focus/scroll activity into content-ready events after a frame is stored."""

    def __init__(self, bus, clock: Callable[[], float] = time.time) -> None:
        self._bus = bus
        self._clock = clock
        self._lock = threading.Lock()
        self._last_focus: dict = {}
        self._pending: deque[dict] = deque()

    def on_focus_changed(self, payload: dict) -> None:
        focus = dict(payload)
        with self._lock:
            self._last_focus = focus
            self._pending.clear()
            self._pending.append({"reason": "focus_changed", "focus": focus})

    def on_scroll_activity(self) -> None:
        with self._lock:
            if self._pending and self._pending[-1]["reason"] == "scroll_idle":
                return
            self._pending.append({"reason": "scroll_idle", "focus": dict(self._last_focus)})

    def on_frame_stored(self, frame: dict) -> None:
        frame_id = frame.get("frame_id")
        if frame_id is None:
            return
        with self._lock:
            if not self._pending:
                return
            pending = self._pending.popleft()
            reason = pending["reason"]
            payload = dict(pending.get("focus", self._last_focus))

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
