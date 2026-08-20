import base64
import io
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable

from PIL import Image

from yuki.logger import get_logger
from yuki.perception.scroll import ScrollIdleDetector
from yuki.perception.sensitive import SensitiveDetector

logger = get_logger("yuki.perception.capture")


class FrameCapture(ABC):
    """帧捕获抽象：真实实现为 WGC，测试用 fake。"""

    on_frame: Callable[[bytes, dict], None] | None = None

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


def black_frame_png(width: int = 1920, height: int = 1080, color=(0, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


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

    def window_info(self) -> tuple[str, str] | None:
        return _window_info_from_hwnd(self.window_hwnd)()

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
    """帧策略：敏感窗口发黑帧、滚动中暂停截屏（纯逻辑）。"""

    def __init__(
        self,
        sensitive: SensitiveDetector,
        idle: ScrollIdleDetector,
        require_idle: bool = False,
        black: bytes | None = None,
    ) -> None:
        self._sensitive = sensitive
        self._idle = idle
        self._require_idle = require_idle
        self._black = black

    def should_capture(self, class_name: str, title: str) -> tuple[bool, bool]:
        if self._sensitive.is_sensitive(class_name, title):
            return False, True
        if self._require_idle and not self._idle.is_idle():
            return False, False
        return True, False

    def black_frame(self) -> bytes:
        return self._black if self._black is not None else black_frame_png()


def _foreground_window_info() -> tuple[str, str] | None:
    try:
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        return win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)
    except Exception:
        return None


def _window_info_from_hwnd(hwnd: int) -> Callable[[], tuple[str, str] | None]:
    """从捕获目标 hwnd 解析窗口身份（不依赖实时前台窗口）。"""

    def info() -> tuple[str, str] | None:
        if not hwnd:
            return None
        try:
            import win32gui

            return win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)
        except Exception:
            return None

    return info


class FrameStore:
    """Keeps the most recently captured frame; frame_id stays monotonic for event identity."""

    def __init__(self) -> None:
        self._latest: dict = {
            "png": "",
            "width": 0,
            "height": 0,
            "ts": 0.0,
            "sensitive": False,
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
        sensitive: bool,
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
                "sensitive": sensitive,
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
    window_info: Callable[[], tuple[str, str] | None] | None = None,
    *,
    hwnd: int | None = None,
    on_frame_stored: Callable[[dict], None] | None = None,
) -> FrameStore:
    """注册 frame REQ/REP 服务：返回最新帧（PNG base64 + 元数据）。

    应用 FrameStrategy 门控：敏感窗口发布占位黑帧；滚动中暂停截屏不更新 latest。
    门控基于捕获目标窗口（hwnd）的身份，而非实时前台窗口。
    """
    if window_info is None:
        window_info = _window_info_from_hwnd(hwnd) if hwnd else _foreground_window_info
    store = FrameStore()

    def notify_stored(snapshot: dict) -> None:
        if on_frame_stored is None:
            return
        try:
            on_frame_stored(snapshot)
        except Exception:
            logger.exception("frame stored callback failed")

    def on_frame(png: bytes, meta: dict) -> None:
        info = window_info()
        class_name, title = info if info is not None else (None, None)
        capture_ok, is_sensitive = strategy.should_capture(class_name, title)
        frame_hwnd = meta.get("hwnd", hwnd or getattr(capture, "window_hwnd", None))
        if not capture_ok and is_sensitive:
            snapshot = store.store(
                png_b64=base64.b64encode(strategy.black_frame()).decode("ascii"),
                width=meta["width"],
                height=meta["height"],
                ts=meta["ts"],
                sensitive=True,
                hwnd=frame_hwnd,
            )
            notify_stored(snapshot)
            return
        if not capture_ok:
            return
        snapshot = store.store(
            png_b64=base64.b64encode(png).decode("ascii"),
            width=meta["width"],
            height=meta["height"],
            ts=meta["ts"],
            sensitive=False,
            hwnd=frame_hwnd,
        )
        notify_stored(snapshot)

    capture.on_frame = on_frame

    def handler(payload: dict) -> dict:
        return store.latest()

    bus.respond("frame", handler)
    return store
