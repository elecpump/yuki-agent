import pytest

from yuki.cognition.model_registry import ModelRegistry, ModelSpec


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
