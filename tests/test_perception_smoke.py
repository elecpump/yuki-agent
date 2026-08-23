import io

from PIL import Image

from yuki.config import Config
from yuki.perception.agent import PerceptionAgent
from yuki.perception.capture import FrameStrategy
from yuki.perception.scroll import ScrollIdleDetector
from yuki.topics import Topics

from tests.fakes import FakeBus


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


class FakeWakeWord:
    def __init__(self, log=None):
        self.log = log
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        if self.log is not None:
            self.log.append("wake_word_start")

    def stop(self):
        self.stopped = True
        if self.log is not None:
            self.log.append("wake_word")

    def health(self):
        return {"started": self.started}


class TrackingCapture(FakeCapture):
    def __init__(self, log=None):
        super().__init__(log)
        self.window_hwnd = 1001
        self.updates = []

    def update_window(self, hwnd):
        self.window_hwnd = int(hwnd)
        self.updates.append(self.window_hwnd)

    def window_info(self):
        return ("Chrome_WidgetWin_1", f"window-{self.window_hwnd}")


class RecordingMonitor:
    instances = []

    def __init__(self, probe, on_change):
        self.probe = probe
        self.on_change = on_change
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_perception_agent_setup_wires_components():
    bus = FakeBus()
    strategy = FrameStrategy(idle=ScrollIdleDetector())
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


def test_perception_agent_wires_wake_word_component():
    bus = FakeBus()
    log = []
    wake_word = FakeWakeWord(log)
    agent = PerceptionAgent(
        Config(wake_word={"enabled": True}),
        bus=bus,
        capture=FakeCapture(log),
        monitor=FakeMonitor(log),
        audio=FakeAudio(log),
        scroll_hook=FakeScrollHook(log),
        wake_word=wake_word,
    )

    agent.setup()
    assert wake_word.started
    assert agent._health_wake_word().detail["started"] is True
    agent.teardown()

    assert wake_word.stopped
    assert "wake_word" in log


def test_perception_agent_default_strategy_gates_on_scroll():
    recorded = {}
    idle = ScrollIdleDetector()
    strategy = FrameStrategy(
        idle=idle,
        require_idle=True,
    )

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
    png = io.BytesIO()
    Image.new("RGB", (64, 48), color=(1, 2, 3)).save(png, format="PNG")
    capture.on_frame(png.getvalue(), {"width": 64, "height": 48, "ts": 1.0})
    assert bus.services["frame"]({})["png"] == ""
    agent.teardown()


def test_perception_agent_retargets_capture_on_focus_change(monkeypatch):
    RecordingMonitor.instances = []
    monkeypatch.setattr("yuki.perception.agent.SystemMonitor", RecordingMonitor)

    bus = FakeBus()
    capture = TrackingCapture()
    agent = PerceptionAgent(
        Config(),
        bus=bus,
        capture=capture,
        audio=FakeAudio(),
        scroll_hook=FakeScrollHook(),
    )
    agent.setup()

    RecordingMonitor.instances[0].on_change(
        {"app": "chrome", "url": "", "title": "Next", "hwnd": 2002}
    )

    assert capture.updates == [2002]
    assert bus.published[-1] == (
        Topics.FOCUS_CHANGED,
        {
            "app": "chrome",
            "url": "",
            "title": "Next",
            "hwnd": 2002,
            "content_ready_deferred": True,
        },
    )
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
    }
    agent.teardown()
