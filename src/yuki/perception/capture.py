import base64
import io
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable

from PIL import Image

from yuki.logger import get_logger
from yuki.perception.scroll import ScrollIdleDetector

logger = get_logger("yuki.perception.capture")


class FrameCapture(ABC):
    """帧捕获抽象：真实实现为 WGC，测试用 fake。"""

    on_frame: Callable[[bytes, dict], None] | None = None

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class WgcCapture(FrameCapture):
    """Windows Graphics Capture 适配器（薄壳，真实桌面会话）。"""

    def __init__(self, window_hwnd: int, min_update_interval: int = 100) -> None:
        self.window_hwnd = window_hwnd
        self._min_update_interval = min_update_interval
        self._capture = None
        self._running = False
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            self._running = True
            if not self.window_hwnd:
                return
            self._start_locked()

    def _start_locked(self) -> None:
        if self._capture is not None:
            return
        import windows_capture

        self._capture = windows_capture.WindowsCapture(
            window_hwnd=self.window_hwnd,
            minimum_update_interval=self._min_update_interval,
        )
        self._capture.frame_handler = self._handle_frame
        self._capture.closed_handler = self._handle_closed
        self._capture.start_free_threaded()

    def update_window(self, window_hwnd: int) -> None:
        with self._lock:
            window_hwnd = int(window_hwnd or 0)
            if window_hwnd == self.window_hwnd:
                return
            was_running = self._running
            self._close_locked()
            self.window_hwnd = window_hwnd
            if was_running and self.window_hwnd:
                self._start_locked()

    def _handle_closed(self) -> None:
        logger.debug("wgc capture closed")

    def _handle_frame(self, frame, control) -> None:
        if self.on_frame is None:
            return
        try:
            png = self._frame_to_png(frame)
            self.on_frame(
                png,
                {
                    "width": frame.width,
                    "height": frame.height,
                    "ts": time.time(),
                    "hwnd": self.window_hwnd,
                },
            )
        except Exception:
            logger.exception("wgc frame callback failed")

    def _frame_to_png(self, frame) -> bytes:
        bgr = frame.convert_to_bgr()
        image = Image.fromarray(bgr.frame_buffer)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._close_locked()

    def _close_locked(self) -> None:
        if self._capture is None:
            return
        try:
            self._capture.close()
        except Exception:
            pass
        self._capture = None


class NullCapture(FrameCapture):
    """无捕获降级：start/stop 空操作，从不触发 on_frame（frame 服务仍返回空负载）。"""

    def start(self) -> None:
        return

    def stop(self) -> None:
        return


class FrameStrategy:
    """帧策略：滚动中暂停截屏（纯逻辑）。"""

    def __init__(
        self,
        idle: ScrollIdleDetector,
        require_idle: bool = False,
    ) -> None:
        self._idle = idle
        self._require_idle = require_idle

    def should_capture(self) -> bool:
        if self._require_idle and not self._idle.is_idle():
            return False
        return True


class FrameStore:
    """Keeps the most recently captured frame; frame_id stays monotonic for event identity."""

    def __init__(self) -> None:
        self._latest: dict = {
            "png": "",
            "width": 0,
            "height": 0,
            "ts": 0.0,
        }
        self._next_frame_id = 0
        self._lock = threading.Lock()

    def store(
        self,
        *,
        png_b64: str,
        width: int,
        height: int,
        ts: float,
        hwnd: int | None = None,
    ) -> dict:
        with self._lock:
            self._next_frame_id += 1
            snapshot = {
                "frame_id": self._next_frame_id,
                "png": png_b64,
                "width": width,
                "height": height,
                "ts": ts,
            }
            if hwnd is not None:
                snapshot["hwnd"] = int(hwnd)
            self._latest = dict(snapshot)
            return dict(snapshot)

    def latest(self) -> dict:
        with self._lock:
            return dict(self._latest)


def make_frame_service(
    bus,
    capture: FrameCapture,
    strategy: FrameStrategy,
    *,
    hwnd: int | None = None,
    on_frame_stored: Callable[[dict], None] | None = None,
) -> FrameStore:
    """注册 frame REQ/REP 服务：返回最新帧（PNG base64 + 元数据）。

    应用 FrameStrategy 门控：滚动中暂停截屏不更新 latest。
    """
    store = FrameStore()

    def notify_stored(snapshot: dict) -> None:
        if on_frame_stored is None:
            return
        try:
            on_frame_stored(snapshot)
        except Exception:
            logger.exception("frame stored callback failed")

    def on_frame(png: bytes, meta: dict) -> None:
        if not strategy.should_capture():
            return
        frame_hwnd = meta.get("hwnd", hwnd or getattr(capture, "window_hwnd", None))
        snapshot = store.store(
            png_b64=base64.b64encode(png).decode("ascii"),
            width=meta["width"],
            height=meta["height"],
            ts=meta["ts"],
            hwnd=frame_hwnd,
        )
        notify_stored(snapshot)

    capture.on_frame = on_frame

    def handler(payload: dict) -> dict:
        return store.latest()

    bus.respond("frame", handler)
    return store
