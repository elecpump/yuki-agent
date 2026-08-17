import base64
import pytest
import io
import sys
import types

import numpy as np
from PIL import Image

from yuki.perception.capture import (
    FrameStrategy,
    WgcCapture,
    _window_info_from_hwnd,
    black_frame_png,
    make_frame_service,
)
from yuki.perception.scroll import ScrollIdleDetector
from yuki.perception.sensitive import SensitiveDetector

from tests.fakes import FakeBus


class FakeCapture:
    def __init__(self):
        self.on_frame = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeStrategy:
    def __init__(self, results, black=b""):
        self.results = results if isinstance(results, list) else [results]
        self.black = black
        self.calls = []

    def should_capture(self, class_name, title):
        result = self.results[min(len(self.results) - 1, len(self.calls))]
        self.calls.append((class_name, title))
        return result

    def black_frame(self):
        return self.black


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
    window_info = lambda: ("Chrome_WidgetWin_1", "如何写代码")

    make_frame_service(bus, capture, strategy, window_info=window_info)

    handler = bus.services.get("frame")
    assert handler is not None
    assert handler({}) == {
        "png": "",
        "width": 0,
        "height": 0,
        "ts": 0.0,
        "sensitive": False,
    }

    png = black_frame_png(width=64, height=48)
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.5})

    result = handler({})
    assert result["png"] == base64.b64encode(png).decode("ascii")
    assert result["width"] == 64
    assert result["height"] == 48
    assert result["ts"] == 1.5
    assert result["sensitive"] is False


def test_make_frame_service_publishes_black_frame_when_sensitive():
    capture = FakeCapture()
    bus = FakeBus()
    black = black_frame_png(width=64, height=48)
    strategy = FakeStrategy(results=[(False, True)], black=black)
    window_info = lambda: ("Chrome_WidgetWin_1", "网上银行登录")

    make_frame_service(bus, capture, strategy, window_info=window_info)

    real = black_frame_png(width=64, height=48, color=(10, 20, 30))
    capture.on_frame(real, {"width": 64, "height": 48, "ts": 1.5})

    result = bus.services["frame"]({})
    assert result["png"] == base64.b64encode(black).decode("ascii")
    assert result["png"] != base64.b64encode(real).decode("ascii")
    assert result["width"] == 64
    assert result["height"] == 48
    assert result["ts"] == 1.5
    assert result["sensitive"] is True
    assert strategy.calls == [("Chrome_WidgetWin_1", "网上银行登录")]


def test_make_frame_service_stores_real_frame_when_normal():
    capture = FakeCapture()
    bus = FakeBus()
    strategy = FakeStrategy(results=[(True, False)])
    window_info = lambda: ("Chrome_WidgetWin_1", "如何写代码")

    make_frame_service(bus, capture, strategy, window_info=window_info)

    png = black_frame_png(width=64, height=48, color=(10, 20, 30))
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.5})

    result = bus.services["frame"]({})
    assert result["png"] == base64.b64encode(png).decode("ascii")
    assert result["sensitive"] is False


def test_make_frame_service_notifies_when_frame_is_stored():
    capture = FakeCapture()
    bus = FakeBus()
    strategy = FakeStrategy(results=[(True, False)])
    stored = []
    make_frame_service(
        bus,
        capture,
        strategy,
        window_info=lambda: ("Chrome_WidgetWin_1", "Article"),
        on_frame_stored=stored.append,
    )

    png = black_frame_png(width=64, height=48, color=(10, 20, 30))
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.5})

    assert len(stored) == 1
    assert stored[0]["frame_id"] == 1
    assert stored[0]["png"] == base64.b64encode(png).decode("ascii")
    assert stored[0]["width"] == 64
    assert stored[0]["height"] == 48
    assert stored[0]["ts"] == 1.5
    assert stored[0]["sensitive"] is False


def test_make_frame_service_returns_frame_by_id():
    capture = FakeCapture()
    bus = FakeBus()
    strategy = FakeStrategy(results=[(True, False), (True, False)])
    make_frame_service(
        bus,
        capture,
        strategy,
        window_info=lambda: ("Chrome_WidgetWin_1", "Article"),
    )

    first_png = black_frame_png(width=64, height=48, color=(1, 2, 3))
    second_png = black_frame_png(width=64, height=48, color=(9, 9, 9))
    capture.on_frame(first_png, {"width": 64, "height": 48, "ts": 1.0})
    first = bus.services["frame"]({})
    capture.on_frame(second_png, {"width": 64, "height": 48, "ts": 2.0})
    second = bus.services["frame"]({})

    assert first["frame_id"] == 1
    assert second["frame_id"] == 2
    assert bus.services["frame"]({"frame_id": 1})["ts"] == 1.0
    assert bus.services["frame"]({"frame_id": 2})["ts"] == 2.0
    assert bus.services["frame"]({"frame_id": 999}) == {}


def test_make_frame_service_keeps_latest_when_suppressed():
    capture = FakeCapture()
    bus = FakeBus()
    strategy = FakeStrategy(results=[(True, False), (False, False)])
    window_info = lambda: ("Chrome_WidgetWin_1", "文章")

    make_frame_service(bus, capture, strategy, window_info=window_info)

    first = black_frame_png(width=64, height=48, color=(1, 2, 3))
    capture.on_frame(first, {"width": 64, "height": 48, "ts": 1.0})
    assert bus.services["frame"]({})["png"] == base64.b64encode(first).decode("ascii")

    second = black_frame_png(width=64, height=48, color=(9, 9, 9))
    capture.on_frame(second, {"width": 64, "height": 48, "ts": 2.0})

    result = bus.services["frame"]({})
    assert result["png"] == base64.b64encode(first).decode("ascii")
    assert result["ts"] == 1.0


class FakeNativeFrame:
    def __init__(self, frame_buffer):
        self.frame_buffer = frame_buffer
        self.width = frame_buffer.shape[1]
        self.height = frame_buffer.shape[0]
        self.timespan = 0

    def convert_to_bgr(self):
        return FakeNativeFrame(self.frame_buffer[:, :, :3])


def test_wgc_frame_to_png_from_frame_buffer():
    frame_buffer = np.zeros((48, 64, 3), dtype=np.uint8)
    frame_buffer[:, :, 0] = 255
    capture = WgcCapture(window_hwnd=1234)
    png = capture._frame_to_png(FakeNativeFrame(frame_buffer))
    img = Image.open(io.BytesIO(png))
    assert img.size == (64, 48)


def test_wgc_start_wires_frame_and_closed_handlers(monkeypatch):
    class FakeWindowsCapture:
        def __init__(self, **kwargs):
            self.frame_handler = None
            self.closed_handler = None
            self.kwargs = kwargs
            self.started_free_threaded = False

        def start_free_threaded(self):
            self.started_free_threaded = True

    monkeypatch.setitem(
        sys.modules, "windows_capture", types.SimpleNamespace(WindowsCapture=FakeWindowsCapture)
    )

    capture = WgcCapture(window_hwnd=1234)
    stored = []
    capture.on_frame = lambda png, meta: stored.append((png, meta))

    capture.start()

    native = capture._capture
    assert isinstance(native, FakeWindowsCapture)
    assert native.started_free_threaded is True
    assert callable(native.frame_handler)
    assert callable(native.closed_handler)

    frame_buffer = np.zeros((48, 64, 4), dtype=np.uint8)
    native.frame_handler(FakeNativeFrame(frame_buffer), object())

    assert len(stored) == 1
    png, meta = stored[0]
    img = Image.open(io.BytesIO(png))
    assert img.size == (64, 48)
    assert meta["width"] == 64
    assert meta["height"] == 48
    assert "ts" in meta


def test_wgc_update_window_restarts_running_capture(monkeypatch):
    created = []

    class FakeWindowsCapture:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started_free_threaded = False
            self.closed = False
            created.append(self)

        def start_free_threaded(self):
            self.started_free_threaded = True

        def close(self):
            self.closed = True

    monkeypatch.setitem(
        sys.modules, "windows_capture", types.SimpleNamespace(WindowsCapture=FakeWindowsCapture)
    )

    capture = WgcCapture(window_hwnd=1001)
    capture.start()
    capture.update_window(2002)

    assert [native.kwargs["window_hwnd"] for native in created] == [1001, 2002]
    assert created[0].closed is True
    assert created[1].started_free_threaded is True
    assert capture.window_hwnd == 2002


def test_wgc_start_without_hwnd_waits_for_update(monkeypatch):
    created = []

    class FakeWindowsCapture:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

        def start_free_threaded(self):
            pass

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules, "windows_capture", types.SimpleNamespace(WindowsCapture=FakeWindowsCapture)
    )

    capture = WgcCapture(window_hwnd=0)
    capture.start()
    capture.update_window(3003)

    assert [native.kwargs["window_hwnd"] for native in created] == [3003]


def test_wgc_on_frame_callback_stores_real_png():
    capture = WgcCapture(window_hwnd=1234)
    stored = []
    capture.on_frame = lambda png, meta: stored.append((png, meta))
    frame_buffer = np.zeros((48, 64, 4), dtype=np.uint8)
    capture._handle_frame(FakeNativeFrame(frame_buffer), object())
    assert len(stored) == 1
    png, meta = stored[0]
    img = Image.open(io.BytesIO(png))
    assert img.size == (64, 48)
    assert meta["width"] == 64
    assert meta["height"] == 48
    assert "ts" in meta


def test_window_info_resolves_from_given_hwnd(monkeypatch):
    pytest.importorskip("win32gui")
    import win32gui as _wg

    sensitive_hwnd, normal_hwnd = 1001, 2002
    monkeypatch.setattr(_wg, "GetForegroundWindow", lambda: normal_hwnd)
    monkeypatch.setattr(
        _wg,
        "GetClassName",
        lambda h: "KeePassMainWindow" if h == sensitive_hwnd else "Chrome_WidgetWin_1",
    )
    monkeypatch.setattr(
        _wg,
        "GetWindowText",
        lambda h: "KeePass - vault" if h == sensitive_hwnd else "Article",
    )

    info = _window_info_from_hwnd(sensitive_hwnd)()
    assert info == ("KeePassMainWindow", "KeePass - vault")


def test_frame_gating_uses_captured_hwnd_not_foreground(monkeypatch):
    pytest.importorskip("win32gui")
    import win32gui as _wg

    sensitive_hwnd, normal_hwnd = 1001, 2002
    monkeypatch.setattr(_wg, "GetForegroundWindow", lambda: normal_hwnd)
    monkeypatch.setattr(
        _wg,
        "GetClassName",
        lambda h: "KeePassMainWindow" if h == sensitive_hwnd else "Chrome_WidgetWin_1",
    )
    monkeypatch.setattr(
        _wg,
        "GetWindowText",
        lambda h: "KeePass - vault" if h == sensitive_hwnd else "Article",
    )

    capture = FakeCapture()
    bus = FakeBus()
    black = black_frame_png(width=64, height=48)
    strategy = FakeStrategy(results=[(False, True)], black=black)
    make_frame_service(bus, capture, strategy, hwnd=sensitive_hwnd)

    real = black_frame_png(width=64, height=48, color=(10, 20, 30))
    capture.on_frame(real, {"width": 64, "height": 48, "ts": 1.5})

    result = bus.services["frame"]({})
    assert result["png"] == base64.b64encode(black).decode("ascii")
    assert result["png"] != base64.b64encode(real).decode("ascii")
    assert result["sensitive"] is True
