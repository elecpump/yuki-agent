import threading

from yuki.config import Config
from yuki.health import HealthStatus
from yuki.process import ProcessAgent
from yuki.shutdown import ShutdownManager

from tests.fakes import FakeBus


class FakeAgent(ProcessAgent):
    name = "fake"

    def __init__(self, config, bus=None):
        super().__init__(config, bus=bus)
        self.events = []
        self.components = {"comp": lambda: HealthStatus(True)}

    def setup(self):
        self.events.append("setup")

    def teardown(self):
        self.events.append("teardown")

    def health_components(self):
        return self.components


def test_agent_run_orders_lifecycle_and_closes_bus():
    bus = FakeBus()
    shutdown = ShutdownManager()
    agent = FakeAgent(Config())
    agent.bus = bus
    agent.shutdown = shutdown
    threading.Timer(0.05, shutdown.request_shutdown).start()
    agent.run(register_signals=False)
    assert agent.events == ["setup", "teardown"]
    assert bus.closed is True


def test_agent_teardown_runs_even_when_setup_raises():
    bus = FakeBus()
    agent = FakeAgent(Config())
    agent.bus = bus
    agent.events = []

    def boom():
        raise RuntimeError("setup failed")

    agent.setup = boom
    try:
        agent.run(register_signals=False)
    except RuntimeError:
        pass
    assert "teardown" in agent.events
    assert bus.closed is True


def test_agent_run_runs_cleanups():
    bus = FakeBus()
    shutdown = ShutdownManager()
    order = []
    shutdown.register_cleanup("x", lambda: order.append("cleanup"), priority=0)
    agent = FakeAgent(Config())
    agent.bus = bus
    agent.shutdown = shutdown
    threading.Timer(0.05, shutdown.request_shutdown).start()
    agent.run(register_signals=False)
    assert order == ["cleanup"]


def test_agent_health_started_only_when_register_health():
    class NoHealthAgent(FakeAgent):
        register_health = False

    bus = FakeBus()
    shutdown = ShutdownManager()
    agent = NoHealthAgent(Config())
    agent.bus = bus
    agent.shutdown = shutdown
    threading.Timer(0.05, shutdown.request_shutdown).start()
    agent.run(register_signals=False)
    assert "health/fake" not in bus.services


def test_agent_teardown_raising_still_stops_health_and_closes_bus():
    import pytest

    bus = FakeBus()
    shutdown = ShutdownManager()
    agent = FakeAgent(Config(), bus=bus)
    agent.shutdown = shutdown
    agent.health.start()
    assert "health/fake" in bus.services

    def boom():
        raise RuntimeError("teardown failed")

    agent.teardown = boom
    threading.Timer(0.05, shutdown.request_shutdown).start()
    with pytest.raises(RuntimeError):
        agent.run(register_signals=False)
    assert bus.closed is True
