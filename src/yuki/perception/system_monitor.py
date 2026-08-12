import threading
import time
from typing import Callable

from yuki.logger import get_logger

logger = get_logger("yuki.perception.system_monitor")

import win32gui  # noqa: E402
import win32process  # noqa: E402


def _default_process_name(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return "unknown"


class ForegroundProbe:
    """探测当前前台窗口（薄适配器，win32/UIA，可注入便于测试）。"""

    def __init__(
        self,
        get_foreground=win32gui.GetForegroundWindow,
        get_text=win32gui.GetWindowText,
        get_class=win32gui.GetClassName,
        get_pid=win32process.GetWindowThreadProcessId,
        process_name=_default_process_name,
    ) -> None:
        self._get_foreground = get_foreground
        self._get_text = get_text
        self._get_class = get_class
        self._get_pid = get_pid
        self._process_name = process_name

    def probe(self) -> dict | None:
        try:
            hwnd = self._get_foreground()
            if not hwnd:
                return None
            title = self._get_text(hwnd)
            app = self._app_name(hwnd)
            url = self._url_from_title(app, title)
            return {"app": app, "url": url, "title": title}
        except Exception:
            logger.exception("foreground probe failed")
            return None

    def _app_name(self, hwnd: int) -> str:
        try:
            result = self._get_pid(hwnd)
            pid = result[1] if isinstance(result, tuple) else result
            name = self._process_name(pid) or ""
            return name.rsplit(".", 1)[0].lower()  # chrome.exe -> chrome
        except Exception:
            return ""

    def _url_from_title(self, app: str, title: str) -> str:
        # 浏览器标题格式 "标题 - 站点"；仅提取站点部分做弱信号。Phase 2b 不做深解析。
        if app in ("chrome", "msedge", "firefox"):
            if " - " in title:
                site = title.rsplit(" - ", 1)[-1].strip()
                if "." in site:  # 站点需像域名，普通词（如 "Article"）不算 URL
                    return site
        return ""


class SystemMonitor:
    """前台窗口监控：变化才发事件（事件驱动而非轮询）。"""

    def __init__(
        self,
        probe: ForegroundProbe,
        on_change: Callable[[dict], None],
        poll_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._clock = clock
        self._last: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        current = self._probe.probe()
        if current is None:
            return
        if current != self._last:
            self._last = current
            self._on_change(current)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("system monitor tick failed")
            self._stop.wait(timeout=self._poll_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def make_monitor(bus, probe=None) -> SystemMonitor:
    """绑定总线：前台窗口变化 → publish event/focus_changed。"""
    from yuki.topics import Topics

    probe = probe or ForegroundProbe()

    def on_change(payload: dict) -> None:
        bus.publish(Topics.FOCUS_CHANGED, payload)

    return SystemMonitor(probe, on_change=on_change)
