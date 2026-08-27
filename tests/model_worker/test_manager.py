import pytest

from yuki.model_worker.controller import ManagedModelSpec, ModelUnavailableError
from yuki.model_worker.manager import ModelManager


class FakeGpuMonitor:
    def __init__(self, free_mb):
        self.free_mb = free_mb
        self.cleared = False

    def snapshot(self):
        return {"available": True, "free_mb": self.free_mb, "low_memory": False}

    def empty_cache(self):
        self.cleared = True
        return True


def test_manager_loads_dependencies_and_unloads_dependents_first():
    events = []
    manager = ModelManager(drain_timeout_s=0.1, vram_safety_margin_mb=0)
    manager.register(
        ManagedModelSpec(
            name="base",
            loader=lambda: events.append("load:base") or object(),
            unloader=lambda handle: events.append("unload:base"),
            min_residency_s=0,
        )
    )
    manager.register(
        ManagedModelSpec(
            name="child",
            dependencies=("base",),
            loader=lambda: events.append("load:child") or object(),
            unloader=lambda handle: events.append("unload:child"),
            min_residency_s=0,
        )
    )

    manager.load("child")
    manager.unload("base")

    assert events == ["load:base", "load:child", "unload:child", "unload:base"]


def test_memory_admission_evicts_low_priority_lru_candidate():
    gpu = FakeGpuMonitor(free_mb=1000)
    events = []
    manager = ModelManager(
        gpu_monitor=gpu,
        drain_timeout_s=0.1,
        vram_safety_margin_mb=0,
        vram_hysteresis_mb=0,
    )

    def spec(name, priority, estimate):
        def unload(handle):
            del handle
            events.append(f"unload:{name}")
            gpu.free_mb += estimate

        return ManagedModelSpec(
            name=name,
            loader=lambda: object(),
            unloader=unload,
            priority=priority,
            estimated_vram_mb=estimate,
            min_residency_s=0,
        )

    manager.register(spec("background", 10, 300))
    manager.load("background")
    gpu.free_mb = 100
    manager.register(spec("interactive", 90, 250))
    manager.load("interactive")

    assert events == ["unload:background"]
    assert manager.get_model_health("background")["runtime_state"] == "unloaded"
    assert manager.get_model_health("interactive")["runtime_state"] == "ready"


def test_pinned_model_is_not_evicted():
    gpu = FakeGpuMonitor(free_mb=1000)
    manager = ModelManager(
        gpu_monitor=gpu,
        vram_safety_margin_mb=0,
        vram_hysteresis_mb=0,
    )
    manager.register(
        ManagedModelSpec(
            name="pinned",
            loader=object,
            pinned=True,
            estimated_vram_mb=100,
            min_residency_s=0,
        )
    )
    manager.load("pinned")
    gpu.free_mb = 0
    manager.register(
        ManagedModelSpec(
            name="new",
            loader=object,
            estimated_vram_mb=100,
            min_residency_s=0,
        )
    )

    with pytest.raises(RuntimeError, match="insufficient_vram"):
        manager.load("new")


def test_health_preserves_legacy_state_and_adds_runtime_state():
    manager = ModelManager(vram_safety_margin_mb=0)
    manager.register(ManagedModelSpec(name="model", loader=object))
    manager.load("model")

    health = manager.get_model_health("model")

    assert health["state"] == "loaded"
    assert health["runtime_state"] == "ready"
    assert health["healthy"] if "healthy" in health else True


def test_generic_inference_failure_degrades_without_opening_circuit():
    manager = ModelManager(circuit_breaker_s=30.0, vram_safety_margin_mb=0)
    manager.register(ManagedModelSpec(name="model", loader=object))

    with pytest.raises(ValueError, match="bad input"):
        manager.run_inference(
            "model",
            lambda handle: (_ for _ in ()).throw(ValueError("bad input")),
        )

    assert manager.get_model_health("model")["runtime_state"] == "degraded"
    assert manager.get_model_health("model")["retry_after"] is None
    assert manager.run_inference("model", lambda handle: "recovered") == "recovered"
    assert manager.get_model_health("model")["runtime_state"] == "ready"


def test_cuda_oom_retries_then_opens_circuit_after_final_failure():
    gpu = FakeGpuMonitor(free_mb=4096)
    clock = {"now": 10.0}
    manager = ModelManager(
        gpu_monitor=gpu,
        circuit_breaker_s=5.0,
        vram_safety_margin_mb=0,
        clock=lambda: clock["now"],
    )
    manager.register(ManagedModelSpec(name="model", loader=object))
    attempts = {"count": 0}

    def fail_oom(handle):
        del handle
        attempts["count"] += 1
        raise RuntimeError("CUDA out of memory")

    with pytest.raises(RuntimeError, match="out of memory"):
        manager.run_inference("model", fail_oom, oom_retry=1)

    assert attempts["count"] == 2
    assert gpu.cleared is True
    assert manager.get_model_health("model")["last_error_code"] == "cuda_oom"
    with pytest.raises(ModelUnavailableError, match="circuit open"):
        manager.run_inference("model", lambda handle: None)


def test_cuda_context_failure_marks_worker_fatal():
    manager = ModelManager(vram_safety_margin_mb=0)
    manager.register(ManagedModelSpec(name="model", loader=object))

    with pytest.raises(RuntimeError, match="illegal memory access"):
        manager.run_inference(
            "model",
            lambda handle: (_ for _ in ()).throw(
                RuntimeError("CUDA illegal memory access")
            ),
        )

    status = manager.get_overall_status()
    assert status["healthy"] is False
    assert status["worker_fatal"] is True


def test_idle_reaper_unloads_eligible_model():
    clock = {"now": 0.0}
    unloaded = []
    manager = ModelManager(
        vram_safety_margin_mb=0,
        clock=lambda: clock["now"],
    )
    manager.register(
        ManagedModelSpec(
            name="idle",
            loader=object,
            unloader=lambda handle: unloaded.append(handle),
            idle_unload_s=10.0,
            min_residency_s=0.0,
        )
    )
    manager.load("idle")
    clock["now"] = 11.0

    assert manager.run_maintenance_once() == ["idle"]
    assert len(unloaded) == 1
    assert manager.get_model_health("idle")["runtime_state"] == "unloaded"


def test_health_reads_cached_gpu_and_model_snapshots_only():
    class CountingGpu(FakeGpuMonitor):
        def __init__(self):
            super().__init__(free_mb=2048)
            self.samples = 0

        def snapshot(self):
            self.samples += 1
            return super().snapshot()

    gpu = CountingGpu()
    health_calls = {"count": 0}

    def model_health():
        health_calls["count"] += 1
        return {"loaded": True}

    manager = ModelManager(gpu_monitor=gpu, vram_safety_margin_mb=0)
    manager.register(
        ManagedModelSpec(name="model", loader=object, health_check=model_health)
    )
    manager.run_maintenance_once()
    sampled = gpu.samples
    checked = health_calls["count"]

    first = manager.get_overall_status()
    second = manager.get_overall_status()

    assert first["gpu"] == second["gpu"]
    assert gpu.samples == sampled
    assert health_calls["count"] == checked


def test_preflight_runs_registered_custom_check():
    manager = ModelManager(vram_safety_margin_mb=0)
    manager.register(
        ManagedModelSpec(
            name="model",
            loader=object,
            preflight_check=lambda: {"name": "custom", "ok": False},
        )
    )

    result = manager.preflight("model")

    assert result["ok"] is False
    assert result["models"]["model"]["checks"][0]["name"] == "custom"
