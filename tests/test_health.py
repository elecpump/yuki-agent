import time

import pytest

from yuki.bus import MessageBus
from yuki.health import register_health_service


@pytest.fixture()
def make_bus():
    buses = []

    def _make(port, role="hub"):
        bus = MessageBus(base_port=port, role=role, hwm=10)
        buses.append(bus)
        return bus

    yield _make
    for bus in buses:
        bus.close()


def test_register_health_service_responds(make_bus):
    bus = make_bus(6200)
    register_health_service(bus, "cognition")
    time.sleep(0.1)
    result = bus.request("health/cognition", {}, timeout_ms=1000)
    assert result["process"] == "cognition"
    assert result["pid"] > 0
    assert "uptime_s" in result
    assert "error_count" in result
