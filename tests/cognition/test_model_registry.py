import pytest

from yuki.cognition.model_registry import ModelRegistry, ModelSpec


class FakeGpuMonitor:
    def __init__(self, *, low_memory=True):
        self.low_memory = low_memory
        self.cache_cleared = 0

    def snapshot(self):
        return {"available": True, "low_memory": self.low_memory, "reason": ""}

    def empty_cache(self):
        self.cache_cleared += 1
        return True


def test_model_registry_loads_dependencies_before_model_and_unloads_dependents_first():
    calls = []
    registry = ModelRegistry()

    registry.register(
        ModelSpec(
            name="base",
            loader=lambda: calls.append("load:base") or "base",
            unloader=lambda handle: calls.append(f"unload:{handle}"),
        )
    )
    registry.register(
        ModelSpec(
            name="child",
            loader=lambda: calls.append("load:child") or "child",
            unloader=lambda handle: calls.append(f"unload:{handle}"),
            dependencies=["base"],
        )
    )

    registry.load("child")
    registry.unload("base")

    assert calls == ["load:base", "load:child", "unload:child", "unload:base"]
    assert registry.get_loaded_models() == []


def test_model_registry_health_reports_degraded_overall_status():
    registry = ModelRegistry()
    registry.register(
        ModelSpec(
            name="vlm",
            loader=lambda: object(),
            health_check=lambda: {"loaded": False, "degraded": True, "reason": "disabled"},
        )
    )

    status = registry.get_overall_status()

    assert status["status"] == "degraded"
    assert status["healthy"] is True
    assert status["models"]["vlm"]["degraded"] is True


def test_model_registry_loaded_models_reflects_external_health():
    registry = ModelRegistry()
    registry.register(
        ModelSpec(
            name="stt",
            loader=lambda: object(),
            health_check=lambda: {"loaded": True, "degraded": False},
        )
    )

    assert registry.get_loaded_models() == ["stt"]


def test_model_registry_shutdown_unloads_in_lowest_priority_first_order():
    calls = []
    registry = ModelRegistry()
    registry.register(
        ModelSpec(
            name="important",
            loader=lambda: "important",
            unloader=lambda handle: calls.append(handle),
            priority=1,
        )
    )
    registry.register(
        ModelSpec(
            name="optional",
            loader=lambda: "optional",
            unloader=lambda handle: calls.append(handle),
            priority=5,
        )
    )

    registry.load("important")
    registry.load("optional")
    registry.shutdown()

    assert calls == ["optional", "important"]


def test_model_registry_records_load_errors():
    registry = ModelRegistry()
    registry.register(ModelSpec(name="broken", loader=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))

    with pytest.raises(RuntimeError, match="boom"):
        registry.load("broken")

    health = registry.get_model_health("broken")
    assert health["state"] == "error"
    assert health["degraded"] is True
    assert health["last_error"] == "boom"
    assert health["failure_count"] == 1

    status = registry.get_overall_status()
    assert status["recent_incidents"][0]["model"] == "broken"
    assert status["recent_incidents"][0]["kind"] == "model_error"


def test_model_registry_records_model_call_metrics():
    registry = ModelRegistry()
    registry.register(ModelSpec(name="local_chat", loader=lambda: object()))

    registry.record_success("local_chat", latency_ms=10.0)
    registry.record_success("local_chat", latency_ms=20.0)
    registry.record_failure("local_chat", "timeout", latency_ms=30.0)

    health = registry.get_model_health("local_chat")
    assert health["success_count"] == 2
    assert health["failure_count"] == 1
    assert health["last_error"] == "timeout"
    assert health["latency_p50_ms"] == 20.0
    assert health["latency_p95_ms"] == 30.0


def test_model_registry_track_call_records_success_and_failure():
    registry = ModelRegistry()
    registry.register(ModelSpec(name="vlm", loader=lambda: object()))

    with registry.track_call("vlm"):
        pass

    with pytest.raises(RuntimeError, match="oom"):
        with registry.track_call("vlm"):
            raise RuntimeError("oom")

    health = registry.get_model_health("vlm")
    assert health["success_count"] == 1
    assert health["failure_count"] == 1
    assert health["last_error"] == "oom"
    assert health["latency_p50_ms"] >= 0.0
    assert health["latency_p95_ms"] >= 0.0
    assert registry.get_overall_status()["recent_incidents"][0]["message"] == "oom"


def test_model_registry_relieves_memory_pressure_by_unloading_lowest_priority_model():
    calls = []
    optional = {"loaded": True}
    important = {"loaded": True}
    registry = ModelRegistry(gpu_monitor=FakeGpuMonitor(low_memory=True))
    registry.register(
        ModelSpec(
            name="important",
            loader=lambda: important,
            unloader=lambda handle: calls.append("important"),
            health_check=lambda: {"loaded": important["loaded"], "degraded": False},
            priority=1,
        )
    )
    registry.register(
        ModelSpec(
            name="optional",
            loader=lambda: optional,
            unloader=lambda handle: calls.append("optional"),
            health_check=lambda: {"loaded": optional["loaded"], "degraded": False},
            priority=5,
        )
    )

    result = registry.relieve_memory_pressure()

    assert result["action"] == "unloaded"
    assert result["model"] == "optional"
    assert result["cache_cleared"] is True
    assert calls == ["optional"]


def test_model_registry_memory_pressure_respects_allow_unload():
    monitor = FakeGpuMonitor(low_memory=True)
    registry = ModelRegistry(gpu_monitor=monitor)
    registry.register(
        ModelSpec(
            name="pinned",
            loader=lambda: object(),
            health_check=lambda: {"loaded": True, "degraded": False},
            allow_unload=False,
        )
    )

    result = registry.relieve_memory_pressure()

    assert result["action"] == "none"
    assert result["reason"] == "no_unload_candidate"
    assert result["cache_cleared"] is True
    assert monitor.cache_cleared == 1
