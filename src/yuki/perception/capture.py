import base64
import io
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
        self._window_hwnd = window_hwnd
        self._min_update_interval = min_update_interval
        self._capture = None

    def start(self) -> None:
        import windows_capture

        def on_frame(native_frame, buf_len, width, height, stop_list, timespan):
            if self.on_frame is None:
                return
            try:
                bgr = native_frame.convert_to_bgr()
                image = Image.fromarray(bgr)
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                self.on_frame(
                    buf.getvalue(),
                    {"width": width, "height": height, "ts": time.time()},
                )
            except Exception:
                logger.exception("wgc frame callback failed")

        self._capture = windows_capture.WindowsCapture(
            window_hwnd=self._window_hwnd,
            minimum_update_interval=self._min_update_interval,
        )
        self._capture.on_frame_arrived = on_frame
        self._capture.start_free_threaded()

    def stop(self) -> None:
        if self._capture is not None:
            try:
                self._capture.close()
            except Exception:
                pass
            self._capture = None


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


def make_frame_service(
    bus,
    capture: FrameCapture,
    strategy: FrameStrategy,
    window_info: Callable[[], tuple[str, str] | None] | None = None,
) -> None:
    """注册 frame REQ/REP 服务：返回最新帧（PNG base64 + 元数据）。

    应用 FrameStrategy 门控：敏感窗口发布占位黑帧；滚动中暂停截屏不更新 latest。
    """
    if window_info is None:
        window_info = _foreground_window_info
    latest: dict = {"png": "", "width": 0, "height": 0, "ts": 0.0, "sensitive": False}

    def on_frame(png: bytes, meta: dict) -> None:
        info = window_info()
        class_name, title = info if info is not None else (None, None)
        capture_ok, is_sensitive = strategy.should_capture(class_name, title)
        if not capture_ok and is_sensitive:
            latest["png"] = base64.b64encode(strategy.black_frame()).decode("ascii")
            latest["width"] = meta["width"]
            latest["height"] = meta["height"]
            latest["ts"] = meta["ts"]
            latest["sensitive"] = True
            return
        if not capture_ok:
            return
        latest["png"] = base64.b64encode(png).decode("ascii")
        latest["width"] = meta["width"]
        latest["height"] = meta["height"]
        latest["ts"] = meta["ts"]
        latest["sensitive"] = False

    capture.on_frame = on_frame

    def handler(payload: dict) -> dict:
        return dict(latest)

    bus.respond("frame", handler)
