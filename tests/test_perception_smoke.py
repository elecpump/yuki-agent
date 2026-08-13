from yuki.config import Config
from yuki.perception.agent import PerceptionAgent
from yuki.perception.capture import FrameStrategy, black_frame_png
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

    def __init__(self, log=None):
        self.log = log

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        if self.log is not None:
            self.log.append("capture")


class FakeMonitor:
    def __init__(self, log=None):
        self.log = log

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        if self.log is not None:
            self.log.append("monitor")


class FakeAudio:
    def __init__(self, log=None):
        self.log = log

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        if self.log is not None:
            self.log.append("audio")


class FakeScrollHook:
    def __init__(self, log=None):
        self.log = log

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        if self.log is not None:
            self.log.append("scroll_hook")


def test_perception_agent_setup_wires_components():
    bus = FakeBus()
    strategy = FrameStrategy(sensitive=SensitiveDetector(), idle=ScrollIdleDetector())
    log = []
    capture = FakeCapture(log)
    monitor = FakeMonitor(log)
    audio = FakeAudio(log)
    scroll_hook = FakeScrollHook(log)
    agent = PerceptionAgent(
        Config(),
        bus=bus,
        capture=capture,
        monitor=monitor,
        audio=audio,
        scroll_hook=scroll_hook,
        strategy=strategy,
    )
    agent.setup()
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
    agent.teardown()
    assert capture.stopped
    assert monitor.stopped
    assert audio.stopped
    assert scroll_hook.stopped
    assert log == ["scroll_hook", "capture", "monitor", "audio"]


def test_perception_agent_default_constructs():
    # 默认路径：注入 fake，验证组装与 frame 服务注册（不启动真实硬件）
    bus = FakeBus()
    agent = PerceptionAgent(
        Config(),
        bus=bus,
        capture=FakeCapture(),
        monitor=FakeMonitor(),
        audio=FakeAudio(),
        scroll_hook=FakeScrollHook(),
    )
    agent.setup()
    assert "frame" in bus.services
    agent.teardown()


def test_perception_agent_default_strategy_gates_on_scroll():
    recorded = {}
    idle = ScrollIdleDetector()
    strategy = FrameStrategy(sensitive=SensitiveDetector(), idle=idle, require_idle=True)

    class RecordingScrollHook:
        def __init__(self, on_scroll):
            recorded["on_scroll"] = on_scroll

        def start(self):
            pass

        def stop(self):
            pass

    bus = FakeBus()
    capture = FakeCapture()
    agent = PerceptionAgent(
        Config(),
        bus=bus,
        capture=capture,
        monitor=FakeMonitor(),
        audio=FakeAudio(),
        strategy=strategy,
        scroll_hook=RecordingScrollHook(idle.on_scroll_activity),
    )
    agent.setup()
    assert "frame" in bus.services
    recorded["on_scroll"]()
    png = black_frame_png(width=64, height=48, color=(1, 2, 3))
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.0})
    assert bus.services["frame"]({})["png"] == ""
    agent.teardown()


def test_perception_agent_registers_frame_service_when_no_capture():
    bus = FakeBus()
    agent = PerceptionAgent(
        Config(),
        bus=bus,
        monitor=FakeMonitor(),
        audio=FakeAudio(),
        scroll_hook=FakeScrollHook(),
        foreground_hwnd=0,
    )
    agent.setup()
    assert "frame" in bus.services
    assert bus.services["frame"]({}) == {
        "png": "",
        "width": 0,
        "height": 0,
        "ts": 0.0,
        "sensitive": False,
    }
    agent.teardown()
