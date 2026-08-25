from yuki.cognition.model_registry import ModelRegistry, ModelSpec
from yuki.cognition.model_service import MODEL_SERVICES, register_model_services

from tests.fakes import FakeBus


def test_model_services_expose_registry_health_and_lifecycle():
    calls = []
    bus = FakeBus()
    registry = ModelRegistry()
    registry.register(
        ModelSpec(
            name="local_chat",
            loader=lambda: "handle",
            unloader=lambda handle: calls.append(f"unload:{handle}"),
        )
    )
    register_model_services(bus, registry)

    assert all(service in bus.services for service in MODEL_SERVICES)
    assert bus.request("models/list", {}) == {"models": []}

    registry.load("local_chat")
    assert bus.request("models/list", {}) == {"models": ["local_chat"]}
    assert bus.request("models/health", {"model": "local_chat"})["model"]["loaded"] is True
    assert bus.request("models/health", {})["status"] == "healthy"

    assert bus.request("models/unload", {"model": "local_chat"}) == {"ok": True}
    assert calls == ["unload:handle"]


def test_model_services_reload_and_relieve_memory_pressure():
    class FakeGpuMonitor:
        def snapshot(self):
            return {"available": True, "low_memory": False}

        def empty_cache(self):
            return True

    bus = FakeBus()
    registry = ModelRegistry(gpu_monitor=FakeGpuMonitor())
    registry.register(ModelSpec(name="stt", loader=lambda: "stt"))
    register_model_services(bus, registry)

    assert bus.request("models/reload", {"model": "stt"}) == {"ok": True}
    result = bus.request("models/relieve_memory_pressure", {})
    assert result["action"] == "none"
    assert result["reason"] == "memory_ok"


def test_model_services_expose_preflight():
    bus = FakeBus()
    registry = ModelRegistry()
    registry.register(
        ModelSpec(
            name="vlm",
            loader=lambda: object(),
            preflight_check=lambda: {"name": "cache", "ok": True},
        )
    )
    register_model_services(bus, registry)

    result = bus.request("models/preflight", {"model": "vlm"})

    assert result["ok"] is True
    assert result["models"]["vlm"]["checks"][-1]["name"] == "cache"
