import threading
import time
from collections import deque
from typing import Callable

from yuki.topics import Topics


class StableContentObservation:
    """Turns raw focus/scroll activity into content-ready events after dwell and a stored frame."""

    def __init__(
        self,
        bus,
        clock: Callable[[], float] = time.time,
        *,
        dwell_s: float = 2.0,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._dwell_s = max(0.0, float(dwell_s))
        self._lock = threading.Lock()
        self._last_focus: dict = {}
        self._pending: deque[dict] = deque()
        self._timer: threading.Timer | None = None
        self._timer_generation = 0

    def on_focus_changed(self, payload: dict) -> None:
        focus = dict(payload)
        with self._lock:
            self._cancel_timer_locked()
            self._last_focus = focus
            self._pending.clear()
            self._pending.append({
                "reason": "focus_changed",
                "focus": focus,
                "since": self._clock(),
            })

    def on_scroll_activity(self) -> None:
        with self._lock:
            if self._dwell_s > 0:
                self._cancel_timer_locked()
                self._pending.clear()
                self._pending.append({
                    "reason": "scroll_idle",
                    "focus": dict(self._last_focus),
                    "since": self._clock(),
                })
                return
            if self._pending and self._pending[-1]["reason"] == "scroll_idle":
                self._pending[-1]["since"] = self._clock()
                return
            self._pending.append({
                "reason": "scroll_idle",
                "focus": dict(self._last_focus),
                "since": self._clock(),
            })

    def on_frame_stored(self, frame: dict) -> None:
        frame_id = frame.get("frame_id")
        if frame_id is None:
            return
        with self._lock:
            if not self._pending:
                return
            self._pending[0]["frame"] = dict(frame)
            event = self._pop_ready_locked()
            if event is None and self._dwell_s > 0:
                pending = self._pending[0]
                elapsed = self._clock() - float(pending.get("since", 0.0))
                self._schedule_release_locked(self._dwell_s - elapsed)
                return

        self._publish_content_ready(*event)

    def close(self) -> None:
        with self._lock:
            self._cancel_timer_locked()
            self._pending.clear()

    def _pop_ready_locked(self) -> tuple[dict, dict] | None:
        if not self._pending:
            return None
        pending = self._pending[0]
        frame = pending.get("frame")
        if not frame:
            return None
        if self._clock() - float(pending.get("since", 0.0)) < self._dwell_s:
            return None
        pending = self._pending.popleft()
        self._cancel_timer_locked()
        return pending, frame

    def _schedule_release_locked(self, delay_s: float) -> None:
        self._cancel_timer_locked()
        self._timer_generation += 1
        generation = self._timer_generation
        timer = threading.Timer(
            max(0.0, delay_s),
            self._release_due,
            args=(generation,),
        )
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._timer_generation += 1

    def _release_due(self, generation: int) -> None:
        event = None
        with self._lock:
            if generation != self._timer_generation:
                return
            self._timer = None
            event = self._pop_ready_locked()
            if event is None and self._pending and self._pending[0].get("frame"):
                pending = self._pending[0]
                elapsed = self._clock() - float(pending.get("since", 0.0))
                self._schedule_release_locked(self._dwell_s - elapsed)
        if event is not None:
            self._publish_content_ready(*event)

    def _publish_content_ready(self, pending: dict, frame: dict) -> None:
        frame_id = frame.get("frame_id")
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
        if "hwnd" in frame:
            payload["hwnd"] = frame["hwnd"]
        self._bus.publish(Topics.CONTENT_READY, payload)
