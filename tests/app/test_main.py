from yuki.app.main import YukiApp
from yuki.config import Config
from yuki.health import HealthStatus
from yuki.runtime_bus import LocalRuntimeBus
from yuki.shutdown import ShutdownManager


class FakeHub:
    def __init__(self):
        self.closed = False

    def health_snapshot(self):
        return {"healthy": True}

    def close(self):
        self.closed = True


class FakeRemoteBus:
    def __init__(self):
        self.services = {}
        self.closed = False
        self.error_count = 0
        self.dropped_count = 0

    def respond(self, service, handler, *, lane="work"):
        self.services[service] = handler

    def publish(self, topic, payload, *, trace_id=None):
        pass

    def bus_health(self):
        return {"healthy": True}

    def close(self):
        self.closed = True


class FakeAgent:
    def __init__(self, name, shutdown, events):
        self.name = name
        self.shutdown = shutdown
        self.events = events

    def setup(self):
        self.events.append(f"setup:{self.name}")

    def loop(self):
        self.events.append(f"loop:{self.name}")
        self.shutdown.wait(1.0)

    def teardown(self):
        self.events.append(f"teardown:{self.name}")

    def health_components(self):
        return {"component": lambda: HealthStatus(True)}


def test_app_coordinates_shared_agents_and_reverse_teardown():
    events = []
    shutdown = ShutdownManager()
    local = LocalRuntimeBus()
    remote = FakeRemoteBus()
    agents = [FakeAgent(name, shutdown, events) for name in ("a", "b", "c")]
    app = YukiApp(
        Config(),
        hub=FakeHub(),
        remote_bus=remote,
        local_bus=local,
        shutdown=shutdown,
        agents=agents,
    )

    app.setup()
    app.close()

    assert events[:3] == ["setup:a", "setup:b", "setup:c"]
    assert events[-3:] == ["teardown:c", "teardown:b", "teardown:a"]
    assert "health/yuki" in remote.services
    assert remote.closed is True


def test_app_injects_cognition_local_model_control_into_gateway(monkeypatch):
    events = []
    shutdown = ShutdownManager()
    local = LocalRuntimeBus()
    remote = FakeRemoteBus()
    cognition = FakeAgent("cognition", shutdown, events)
    cognition.local_model_control = object()
    created = []

    class FakeGateway:
        def __init__(self, config, **kwargs):
            del config
            created.append(kwargs)

        def start(self):
            events.append("gateway:start")

        def stop(self):
            events.append("gateway:stop")

    monkeypatch.setattr("yuki.app.main.GatewayServer", FakeGateway)
    app = YukiApp(
        Config(gateway={"enabled": True}),
        hub=FakeHub(),
        remote_bus=remote,
        local_bus=local,
        shutdown=shutdown,
        agents=[cognition],
    )

    app.setup()
    app.close()

    assert created[0]["local_model_control"] is cognition.local_model_control
