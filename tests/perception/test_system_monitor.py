import time

import pytest

from yuki.perception.system_monitor import ForegroundProbe, SystemMonitor


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_probe_extracts_window_info(monkeypatch):
    calls = {}

    def fake_get_foreground():
        return 1234

    def fake_get_text(hwnd):
        return "My Browser - Article"

    def fake_get_class(hwnd):
        return "Chrome_WidgetWin_1"

    def fake_get_pid(hwnd):
        return 42

    def fake_process_name(pid):
        return "chrome.exe"

    probe = ForegroundProbe(
        get_foreground=fake_get_foreground,
        get_text=fake_get_text,
        get_class=fake_get_class,
        get_pid=fake_get_pid,
        process_name=fake_process_name,
    )
    result = probe.probe()
    assert result == {"app": "chrome", "url": "", "title": "My Browser - Article", "hwnd": 1234}


def test_monitor_emits_on_change():
    events = []
    clock = FakeClock()

    class FakeProbe:
        def __init__(self):
            self.value = None

        def probe(self):
            return self.value

    probe = FakeProbe()
    monitor = SystemMonitor(probe, on_change=events.append, poll_interval=0.0, clock=clock)
    probe.value = {"app": "chrome", "url": "", "title": "A"}
    monitor.tick()
    assert len(events) == 1
    assert events[0]["title"] == "A"


def test_monitor_does_not_reemit_same_window():
    events = []
    clock = FakeClock()

    class FakeProbe:
        def __init__(self):
            self.value = None

        def probe(self):
            return self.value

    probe = FakeProbe()
    monitor = SystemMonitor(probe, on_change=events.append, poll_interval=0.0, clock=clock)
    probe.value = {"app": "chrome", "url": "", "title": "A"}
    monitor.tick()
    monitor.tick()  # 窗口未变
    assert len(events) == 1


def test_monitor_emits_when_window_changes_back():
    events = []
    clock = FakeClock()

    class FakeProbe:
        def __init__(self):
            self.value = None

        def probe(self):
            return self.value

    probe = FakeProbe()
    monitor = SystemMonitor(probe, on_change=events.append, poll_interval=0.0, clock=clock)
    probe.value = {"app": "a", "url": "", "title": "A"}
    monitor.tick()
    probe.value = {"app": "b", "url": "", "title": "B"}
    monitor.tick()
    probe.value = {"app": "a", "url": "", "title": "A"}
    monitor.tick()
    assert len(events) == 3


def test_monitor_probe_none_skips():
    events = []
    clock = FakeClock()

    class FakeProbe:
        def probe(self):
            return None

    monitor = SystemMonitor(FakeProbe(), on_change=events.append, poll_interval=0.0, clock=clock)
    monitor.tick()
    assert events == []
