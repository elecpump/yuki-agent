import pytest

from yuki.config import Config
from yuki.perception.capture import FrameStrategy, black_frame_png
from yuki.perception.main import build_perception
from yuki.perception.sensitive import SensitiveDetector
from yuki.perception.scroll import ScrollIdleDetector


class FakeBus:
    def __init__(self):
        self.published = []
        self.services = {}

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def respond(self, service, handler):
        self.services[service] = handler

    def request(self, service, payload, timeout_ms=2000):
        return self.services[service](payload)


class FakeCapture:
    on_frame = None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeMonitor:
    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeAudio:
    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeScrollHook:
    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_build_perception_wires_components():
    bus = FakeBus()
    config = Config(bus_role="node")
    strategy = FrameStrategy(sensitive=SensitiveDetector(), idle=ScrollIdleDetector())
    capture = FakeCapture()
    monitor = FakeMonitor()
    audio = FakeAudio()
    scroll_hook = FakeScrollHook()
    build_perception(
        bus,
        config,
        capture=capture,
        monitor=monitor,
        audio=audio,
        scroll_hook=scroll_hook,
        strategy=strategy,
    )
    assert "frame" in bus.services
    assert bus.services["frame"]({}) == {
        "png": "",
        "width": 0,
        "height": 0,
        "ts": 0.0,
        "sensitive": False,
    }
    assert capture.started
    assert monitor.started
    assert audio.started
    assert scroll_hook.started


def test_build_perception_default_constructs(monkeypatch):
    # 默认路径：注入 fake，验证组装与 frame 服务注册（不启动真实硬件）
    import yuki.perception.main as pm

    monkeypatch.setattr(pm, "_perception_state", {})
    bus = FakeBus()
    config = Config(bus_role="node")
    build_perception(
        bus,
        config,
        capture=FakeCapture(),
        monitor=FakeMonitor(),
        audio=FakeAudio(),
        scroll_hook=FakeScrollHook(),
    )
    assert "frame" in bus.services


def test_build_perception_default_strategy_gates_on_scroll(monkeypatch):
    import win32gui as _wg
    import yuki.perception.main as pm

    monkeypatch.setattr(_wg, "GetForegroundWindow", lambda: 0)
    monkeypatch.setattr(pm, "_perception_state", {})
    recorded = {}

    class RecordingScrollHook:
        def __init__(self, on_scroll):
            recorded["on_scroll"] = on_scroll

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(pm, "ScrollHook", RecordingScrollHook)
    bus = FakeBus()
    config = Config(bus_role="node")
    capture = FakeCapture()
    build_perception(bus, config, capture=capture, monitor=FakeMonitor(), audio=FakeAudio())
    assert "frame" in bus.services
    recorded["on_scroll"]()
    png = black_frame_png(width=64, height=48, color=(1, 2, 3))
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.0})
    assert bus.services["frame"]({})["png"] == ""


def test_build_perception_registers_frame_service_when_no_capture(monkeypatch):
    import yuki.perception.main as pm

    monkeypatch.setattr(pm, "_perception_state", {})
    bus = FakeBus()
    config = Config(bus_role="node")
    build_perception(
        bus,
        config,
        monitor=FakeMonitor(),
        audio=FakeAudio(),
        scroll_hook=FakeScrollHook(),
        foreground_hwnd=0,
    )
    assert "frame" in bus.services
    assert bus.services["frame"]({}) == {
        "png": "",
        "width": 0,
        "height": 0,
        "ts": 0.0,
        "sensitive": False,
    }
