from yuki.config import Config
from yuki.health import HealthStatus
from yuki.perception.audio import AudioCapture
from yuki.perception.capture import FrameStrategy, NullCapture, WgcCapture, make_frame_service
from yuki.perception.scroll import ScrollHook, ScrollIdleDetector
from yuki.perception.sensitive import SensitiveDetector
from yuki.perception.system_monitor import ForegroundProbe, SystemMonitor, make_monitor
from yuki.process import ProcessAgent


class PerceptionAgent(ProcessAgent):
    name = "perception"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 capture=None, monitor=None, audio=None, scroll_hook=None,
                 strategy=None, foreground_hwnd: int | None = None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._capture = capture
        self._monitor = monitor
        self._audio = audio
        self._scroll_hook = scroll_hook
        self._strategy = strategy
        self._foreground_hwnd = foreground_hwnd
        self._components: dict = {}

    def setup(self) -> None:
        detector = SensitiveDetector()
        idle = ScrollIdleDetector(idle_ms=300)
        strategy = self._strategy or FrameStrategy(sensitive=detector, idle=idle, require_idle=True)

        gate_hwnd = 0
        capture = self._capture
        if capture is None:
            hwnd = self._foreground_hwnd
            if hwnd is None:
                try:
                    import win32gui
                    hwnd = win32gui.GetForegroundWindow()
                except Exception:
                    hwnd = 0
            gate_hwnd = hwnd
            capture = WgcCapture(hwnd) if hwnd else NullCapture()
        elif isinstance(capture, WgcCapture):
            gate_hwnd = capture.window_hwnd

        monitor = self._monitor or make_monitor(self.bus, probe=ForegroundProbe())
        audio = self._audio or AudioCapture(self.bus)
        scroll_hook = self._scroll_hook or ScrollHook(on_scroll=idle.on_scroll_activity)

        make_frame_service(self.bus, capture, strategy, hwnd=gate_hwnd)

        self._components = {
            "capture": capture,
            "monitor": monitor,
            "audio": audio,
            "scroll_hook": scroll_hook,
        }
        monitor.start()
        audio.start()
        capture.start()
        scroll_hook.start()

    def teardown(self) -> None:
        for key in ("scroll_hook", "capture", "monitor", "audio"):
            comp = self._components.get(key)
            if comp is not None:
                try:
                    comp.stop()
                except Exception:
                    pass

    def health_components(self):
        return {
            "audio": self._health_audio,
            "capture": self._health_capture,
            "monitor": self._health_monitor,
            "scroll_hook": self._health_scroll,
        }

    def _health_audio(self) -> HealthStatus:
        stream = getattr(self._components.get("audio"), "_stream", None)
        return HealthStatus(stream is not None, {"stream_active": stream is not None})

    def _health_capture(self) -> HealthStatus:
        capture = self._components.get("capture")
        ok = capture is not None and capture.on_frame is not None
        return HealthStatus(ok, {"frame_registered": ok})

    def _health_monitor(self) -> HealthStatus:
        monitor = self._components.get("monitor")
        thread = getattr(monitor, "_thread", None)
        alive = thread is not None and thread.is_alive()
        return HealthStatus(alive, {"thread_alive": alive})

    def _health_scroll(self) -> HealthStatus:
        scroll = self._components.get("scroll_hook")
        return HealthStatus(scroll is not None, {"installed": scroll is not None})
