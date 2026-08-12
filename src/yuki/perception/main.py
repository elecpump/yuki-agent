from yuki.bus import MessageBus
from yuki.config import Config
from yuki.health import register_health_service
from yuki.logger import get_logger
from yuki.perception.audio import AudioCapture
from yuki.perception.capture import FrameStrategy, NullCapture, WgcCapture, make_frame_service
from yuki.perception.scroll import ScrollHook, ScrollIdleDetector
from yuki.perception.sensitive import SensitiveDetector
from yuki.perception.system_monitor import ForegroundProbe, SystemMonitor, make_monitor
from yuki.shutdown import ShutdownManager

logger = get_logger("yuki.perception")

_perception_state: dict = {}


def build_perception(
    bus: MessageBus,
    config: Config,
    *,
    capture=None,
    monitor=None,
    audio=None,
    scroll_hook=None,
    strategy=None,
    foreground_hwnd: int | None = None,
) -> None:
    """组装采集层四组件。测试注入 fake；默认用真实适配器。"""

    detector = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = strategy or FrameStrategy(sensitive=detector, idle=idle, require_idle=True)

    gate_hwnd = 0
    if capture is None:
        # WGC 需前台窗口句柄；取不到时降级为 NullCapture（FrameStrategy 仍发空负载黑帧）
        hwnd = foreground_hwnd
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

    if monitor is None:
        monitor = make_monitor(bus, probe=ForegroundProbe())

    if audio is None:
        audio = AudioCapture(bus)

    if scroll_hook is None:
        scroll_hook = ScrollHook(on_scroll=idle.on_scroll_activity)

    make_frame_service(bus, capture, strategy, hwnd=gate_hwnd)

    _perception_state["capture"] = capture
    _perception_state["monitor"] = monitor
    _perception_state["audio"] = audio
    _perception_state["scroll_hook"] = scroll_hook
    monitor.start()
    audio.start()
    capture.start()
    scroll_hook.start()


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role=config.bus_role, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    build_perception(bus, config)
    register_health_service(bus, "perception")
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        for key in ("scroll_hook", "capture", "monitor", "audio"):
            comp = _perception_state.get(key)
            if comp is not None:
                try:
                    comp.stop()
                except Exception:
                    pass
        bus.close()


if __name__ == "__main__":
    main()
