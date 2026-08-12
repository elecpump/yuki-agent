import base64
import io

from PIL import Image

from yuki.perception.capture import FrameStrategy, black_frame_png, make_frame_service
from yuki.perception.scroll import ScrollIdleDetector
from yuki.perception.sensitive import SensitiveDetector


class FakeCapture:
    def __init__(self):
        self.on_frame = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeBus:
    def __init__(self):
        self.services = {}

    def respond(self, service, handler):
        self.services[service] = handler


def test_black_frame_is_pure_black_png():
    png = black_frame_png(width=64, height=48)
    img = Image.open(io.BytesIO(png))
    assert img.size == (64, 48)
    assert img.getpixel((32, 24)) == (0, 0, 0)


def test_strategy_allows_normal_window():
    det = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(sensitive=det, idle=idle)
    capture, sensitive = strategy.should_capture("Chrome_WidgetWin_1", "如何写代码")
    assert capture is True
    assert sensitive is False


def test_strategy_blocks_sensitive_window():
    det = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(sensitive=det, idle=idle)
    capture, sensitive = strategy.should_capture("Chrome_WidgetWin_1", "网上银行登录")
    assert capture is False
    assert sensitive is True


def test_strategy_requires_idle_when_requested():
    det = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(sensitive=det, idle=idle, require_idle=True)
    idle.on_scroll_activity()
    capture, sensitive = strategy.should_capture("Chrome_WidgetWin_1", "文章")
    assert capture is False
    assert sensitive is False


def test_make_frame_service_registers_frame_and_returns_latest():
    det = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(sensitive=det, idle=idle)
    capture = FakeCapture()
    bus = FakeBus()

    make_frame_service(bus, capture, strategy)

    handler = bus.services.get("frame")
    assert handler is not None
    assert handler({}) == {"png": "", "width": 0, "height": 0, "ts": 0.0}

    png = black_frame_png(width=64, height=48)
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.5})

    result = handler({})
    assert result["png"] == base64.b64encode(png).decode("ascii")
    assert result["width"] == 64
    assert result["height"] == 48
    assert result["ts"] == 1.5
