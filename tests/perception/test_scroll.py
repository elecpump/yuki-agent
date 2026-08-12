import threading
import time

import pytest

from yuki.perception import scroll as scroll_mod
from yuki.perception.scroll import ScrollHook, ScrollIdleDetector


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _reset_scroll_state():
    scroll_mod._state.on_scroll = None
    scroll_mod._state.mouse_hook = None
    scroll_mod._state.keyboard_hook = None
    scroll_mod._state.thread = None
    scroll_mod._state.thread_id = None
    yield
    scroll_mod._state.on_scroll = None
    scroll_mod._state.mouse_hook = None
    scroll_mod._state.keyboard_hook = None
    scroll_mod._state.thread = None
    scroll_mod._state.thread_id = None


def test_idle_detector_no_activity_is_idle():
    det = ScrollIdleDetector(idle_ms=300, clock=FakeClock())
    assert det.is_idle() is True


def test_idle_detector_activity_resets_timer():
    clock = FakeClock()
    det = ScrollIdleDetector(idle_ms=300, clock=clock)
    clock.now = 100.0
    det.on_scroll_activity()
    clock.now = 100.2  # 200ms 后仍非静止
    assert det.is_idle() is False
    clock.now = 100.4  # 400ms 后静止
    assert det.is_idle() is True


def test_idle_detector_repeated_activity_extends():
    clock = FakeClock()
    det = ScrollIdleDetector(idle_ms=300, clock=clock)
    clock.now = 0.0
    det.on_scroll_activity()
    clock.now = 0.25
    det.on_scroll_activity()  # 重置
    clock.now = 0.4
    assert det.is_idle() is False  # 距上次 150ms


def test_idle_detector_records_last_activity():
    clock = FakeClock()
    det = ScrollIdleDetector(idle_ms=300, clock=clock)
    clock.now = 42.0
    det.on_scroll_activity()
    assert det.last_activity == 42.0


def test_scroll_hook_constructible():
    # 适配器薄壳：验证构造签名（真实钩子需交互会话，不在此启动）
    hook = ScrollHook(on_scroll=lambda: None)
    assert hook is not None


def test_idle_detector_exact_idle_ms_boundary_is_idle():
    # elapsed == idle_ms exactly -> idle (locks the >= semantics).
    # 0.0 baseline keeps now - last_activity exact in binary floats (0.3 - 0.0).
    clock = FakeClock()
    det = ScrollIdleDetector(idle_ms=300, clock=clock)
    clock.now = 0.0
    det.on_scroll_activity()
    clock.now = 0.3
    assert det.is_idle() is True


class _ThreadSpy:
    instances = []

    def __init__(self, *args, **kwargs):
        _ThreadSpy.instances.append(self)

    def start(self):
        pass


def test_start_is_idempotent_single_thread(monkeypatch):
    _ThreadSpy.instances.clear()
    monkeypatch.setattr(scroll_mod.threading, "Thread", _ThreadSpy)
    hook = ScrollHook(on_scroll=lambda: None)
    hook.start()
    hook.start()
    assert len(_ThreadSpy.instances) == 1


def test_stop_clears_callback_and_second_start_reregisters(monkeypatch):
    monkeypatch.setattr(ScrollHook, "_run_hooks", lambda self: None)
    cb = lambda: None
    hook = ScrollHook(on_scroll=cb)
    hook.start()
    assert scroll_mod._state.on_scroll is cb
    hook.stop()
    assert scroll_mod._state.on_scroll is None
    hook.start()
    assert scroll_mod._state.on_scroll is cb
    hook.stop()


def test_stop_posts_wm_quit_to_hook_thread(monkeypatch):
    posted = []

    def fake_post(thread_id, msg, w_param, l_param):
        posted.append((thread_id, msg, w_param, l_param))
        return 1

    ready = threading.Event()

    def fake_run(self):
        scroll_mod._state.thread_id = 999
        ready.set()

    monkeypatch.setattr(scroll_mod._user32, "PostThreadMessageW", fake_post)
    monkeypatch.setattr(ScrollHook, "_run_hooks", fake_run)
    hook = ScrollHook(on_scroll=lambda: None)
    hook.start()
    assert ready.wait(timeout=2.0)
    hook.stop()
    assert posted == [(999, 0x0012, 0, 0)]  # WM_QUIT


def test_stop_twice_does_not_raise(monkeypatch):
    monkeypatch.setattr(ScrollHook, "_run_hooks", lambda self: None)
    hook = ScrollHook(on_scroll=lambda: None)
    hook.start()
    hook.stop()
    hook.stop()
