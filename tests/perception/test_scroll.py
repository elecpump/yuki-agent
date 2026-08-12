import time

import pytest

from yuki.perception.scroll import ScrollHook, ScrollIdleDetector


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


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
