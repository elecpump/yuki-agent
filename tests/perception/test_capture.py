import base64
import io
import sys
import types

import numpy as np
from PIL import Image

from yuki.perception.capture import (
    FrameStrategy,
    WgcCapture,
    make_frame_service,
)
from yuki.perception.scroll import ScrollIdleDetector

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
    def __init__(self, results):
        self.results = results if isinstance(results, list) else [results]
        self.calls = []

    def should_capture(self):
        result = self.results[min(len(self.results) - 1, len(self.calls))]
        self.calls.append(result)
        return result


def _png_bytes(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def test_strategy_allows_normal_window():
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(idle=idle)
    assert strategy.should_capture() is True


def test_strategy_requires_idle_when_requested():
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(idle=idle, require_idle=True)
    idle.on_scroll_activity()
    assert strategy.should_capture() is False


def test_make_frame_service_registers_frame_and_returns_latest():
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(idle=idle)
    capture = FakeCapture()
    bus = FakeBus()

    make_frame_service(bus, capture, strategy)

    handler = bus.services.get("frame")
    assert handler is not None
    assert handler({}) == {
        "png": b"",
        "width": 0,
        "height": 0,
        "ts": 0.0,
    }

    png = _png_bytes()
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.5})

    result = handler({})
    assert result["png"] == png
    assert result["width"] == 64
    assert result["height"] == 48
    assert result["ts"] == 1.5


def test_make_frame_service_stores_real_frame_when_normal():
    capture = FakeCapture()
    bus = FakeBus()
    strategy = FakeStrategy(results=[True])

    make_frame_service(bus, capture, strategy)

    png = _png_bytes()
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.5})

    result = bus.services["frame"]({})
    assert result["png"] == png


def test_make_frame_service_notifies_when_frame_is_stored():
    capture = FakeCapture()
    bus = FakeBus()
    strategy = FakeStrategy(results=[True])
    stored = []
    make_frame_service(
        bus,
        capture,
        strategy,
        on_frame_stored=stored.append,
    )

    png = _png_bytes()
    capture.on_frame(png, {"width": 64, "height": 48, "ts": 1.5})

    assert len(stored) == 1
    assert stored[0]["frame_id"] == 1
    assert stored[0]["png"] == png
    assert stored[0]["width"] == 64
    assert stored[0]["height"] == 48
    assert stored[0]["ts"] == 1.5


def test_make_frame_service_keeps_latest_when_suppressed():
    capture = FakeCapture()
    bus = FakeBus()
    strategy = FakeStrategy(results=[True, False])

    make_frame_service(bus, capture, strategy)

    first = _png_bytes(color=(1, 2, 3))
    capture.on_frame(first, {"width": 64, "height": 48, "ts": 1.0})
    assert bus.services["frame"]({})["png"] == first

    second = _png_bytes(color=(9, 9, 9))
    capture.on_frame(second, {"width": 64, "height": 48, "ts": 2.0})

    result = bus.services["frame"]({})
    assert result["png"] == first
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
