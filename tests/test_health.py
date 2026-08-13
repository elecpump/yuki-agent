import threading
import time

import pytest

from yuki.health import HealthReporter, HealthStatus
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeTickingBus(FakeBus):
    """心跳线程使用 .wait()，用带 timeout 的 Event 语义的 stop flag。"""

    def __init__(self, heartbeat_interval=0.05):
        super().__init__()
        self._heartbeat_interval = heartbeat_interval


def test_collect_reports_process_and_uptime():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="cognition")
    data = reporter.collect()
    assert data["process"] == "cognition"
    assert data["pid"] > 0
    assert data["uptime_s"] >= 0
    assert data["error_count"] == 0
    assert data["healthy"] is True
    assert data["components"] == {}


def test_collect_aggregates_component_health():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="cognition")
    reporter.register_component("vlm", lambda: HealthStatus(True, {"loaded": True}))
    reporter.register_component("stt", lambda: HealthStatus(False, {"reason": "not loaded"}))
    data = reporter.collect()
    assert data["components"]["vlm"] == {"ok": True, "detail": {"loaded": True}}
    assert data["components"]["stt"] == {"ok": False, "detail": {"reason": "not loaded"}}
    assert data["healthy"] is False


def test_collect_marks_unhealthy_when_check_raises():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="cognition")

    def boom():
        raise RuntimeError("check failed")

    reporter.register_component("broken", boom)
    data = reporter.collect()
    assert data["healthy"] is False
    assert data["components"]["broken"]["ok"] is False


def test_start_registers_health_service_and_publishes_heartbeat():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="perception", heartbeat_interval=0.05)
    reporter.start()
    assert "health/perception" in bus.services
    deadline = time.time() + 1.5
    while time.time() < deadline:
        heartbeats = [p for t, p in bus.published if t == Topics.HEARTBEAT]
        if heartbeats:
            break
        time.sleep(0.02)
    assert heartbeats, "expected at least one heartbeat"
    assert heartbeats[0]["process"] == "perception"
    assert heartbeats[0]["healthy"] is True
    reporter.stop()


def test_health_service_returns_collect_result():
    bus = FakeBus()
    reporter = HealthReporter(bus, process="interaction")
    reporter.register_component("tts", lambda: HealthStatus(True))
    reporter.start()
    try:
        result = bus.request("health/interaction", {}, timeout_ms=1000)
        assert result["process"] == "interaction"
        assert result["components"]["tts"]["ok"] is True
    finally:
        reporter.stop()


def test_health_service_over_real_bus():
    from yuki.bus import BusHub, BusNode

    port = 6250
    hub = BusHub(base_port=port, hwm=10)
    node = BusNode(base_port=port, hwm=10)
    try:
        reporter = HealthReporter(node, process="cognition")
        reporter.register_component("vlm", lambda: HealthStatus(True, {"loaded": True}))
        reporter.start()
        time.sleep(0.1)
        result = node.request("health/cognition", {}, timeout_ms=1000)
        assert result["process"] == "cognition"
        assert result["components"]["vlm"] == {"ok": True, "detail": {"loaded": True}}
        assert result["healthy"] is True
    finally:
        reporter.stop()
        node.close()
        hub.close()
