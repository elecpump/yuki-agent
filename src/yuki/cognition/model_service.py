from yuki.cognition.model_registry import ModelRegistry

MODEL_SERVICES = (
    "models/list",
    "models/health",
    "models/unload",
    "models/reload",
    "models/relieve_memory_pressure",
)


def register_model_services(bus, registry: ModelRegistry) -> None:
    """Expose ModelRegistry lifecycle and health operations on the process bus."""

    def on_health(payload: dict) -> dict:
        model = (payload or {}).get("model")
        if model:
            return {"model": registry.get_model_health(model)}
        return registry.get_overall_status()

    def on_unload(payload: dict) -> dict:
        registry.unload(payload["model"])
        return {"ok": True}

    def on_reload(payload: dict) -> dict:
        registry.reload(payload["model"])
        return {"ok": True}

    bus.respond("models/list", lambda payload: {"models": registry.get_loaded_models()})
    bus.respond("models/health", on_health)
    bus.respond("models/unload", on_unload)
    bus.respond("models/reload", on_reload)
    bus.respond("models/relieve_memory_pressure", lambda payload: registry.relieve_memory_pressure())
