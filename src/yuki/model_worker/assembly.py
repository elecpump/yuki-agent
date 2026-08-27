from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yuki.cognition.brain.local import LocalChatModel
from yuki.cognition.gpu_monitor import GpuMemoryMonitor
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config, ModelPolicyConfig
from yuki.interaction.tts import IndexTTSModel
from yuki.memory.embedding import default_embedding_registry
from yuki.model_cache import ModelCacheManager
from yuki.model_worker.controller import ManagedModelSpec
from yuki.model_worker.manager import ModelManager
from yuki.model_worker.operations import ModelOperationStore
from yuki.model_worker.scheduler import ModelInferenceScheduler
from yuki.model_worker.services import (
    TtsJobStore,
    operation_handler,
    register_inference_services,
    register_management_services,
)
from yuki.runtime_bus import RuntimeBusProtocol


_CATALOG_VRAM_MB = {
    "vlm": 5 * 1024,
    "stt": 1536,
    "local_chat": 2 * 1024,
    "tts": 2 * 1024,
    "embedding": 1024,
}


@dataclass
class ModelWorkerRuntime:
    manager: ModelManager
    scheduler: ModelInferenceScheduler
    operations: ModelOperationStore
    tts_jobs: TtsJobStore | None

    def close(self) -> None:
        if self.tts_jobs is not None:
            self.tts_jobs.close()
        self.operations.close()
        self.scheduler.close()
        self.manager.shutdown()


def assemble_model_worker(
    config: Config,
    bus: RuntimeBusProtocol,
) -> ModelWorkerRuntime:
    manager = ModelManager(
        gpu_monitor=GpuMemoryMonitor(),
        drain_timeout_s=config.models.drain_timeout_s,
        circuit_breaker_s=config.models.circuit_breaker_s,
        vram_safety_margin_mb=config.models.vram_safety_margin_mb,
        vram_hysteresis_mb=config.models.vram_hysteresis_mb,
    )
    cache_manager = ModelCacheManager(max_entries=256)

    vlm = VisualUnderstander(
        model_id=config.vlm.model,
        cache_dir=config.vlm.cache_dir,
        enabled=config.vlm.enabled,
        cache_manager=cache_manager,
    )
    _register_model(
        manager,
        "vlm",
        vlm,
        config.models.policies["vlm"],
        enabled=config.vlm.enabled,
    )

    stt = SpeechRecognizer(
        enabled=config.stt.enabled,
        model_id=config.stt.model,
        model_dir=config.stt.model_dir,
        device=config.stt.device,
        language=config.stt.language,
        use_itn=config.stt.use_itn,
        retry_window_s=config.stt.retry_window_s,
    )
    _register_model(
        manager,
        "stt",
        stt,
        config.models.policies["stt"],
        enabled=config.stt.enabled,
    )

    local_chat = LocalChatModel(
        model_id=config.local_brain.model_id,
        cache_dir=config.local_brain.cache_dir,
        device=config.local_brain.device,
        enabled=config.local_brain.enabled,
        fp8_dequantize=config.local_brain.fp8_dequantize,
        local_files_only=config.local_brain.local_files_only,
    )
    _register_model(
        manager,
        "local_chat",
        local_chat,
        config.models.policies["local_chat"],
        enabled=config.local_brain.enabled,
    )

    tts = IndexTTSModel(config.tts)
    _register_model(
        manager,
        "tts",
        tts,
        config.models.policies["tts"],
        enabled=config.tts.enabled,
    )

    if config.memory.vector_enabled:
        provider = default_embedding_registry.build(
            config.memory.embedding_provider,
            dimension=config.memory.embedding_dimension,
            model=config.memory.embedding_model,
            cache_dir=config.memory.embedding_cache_dir,
            device=config.memory.embedding_device,
        )
        _register_model(
            manager,
            "embedding",
            provider,
            config.models.policies["embedding"],
            enabled=True,
            lazy=True,
        )

    scheduler = ModelInferenceScheduler(
        concurrency=config.models.gpu_max_concurrency,
        interactive_queue_size=config.models.interactive_queue_size,
        background_queue_size=config.models.background_queue_size,
    )
    operations = ModelOperationStore(
        operation_handler(manager),
        ttl_s=config.models.operation_ttl_s,
    )
    register_management_services(
        bus,
        manager,
        operations,
        policies={
            name: policy.model_dump()
            for name, policy in config.models.policies.items()
        },
    )
    tts_jobs = register_inference_services(
        bus,
        manager,
        scheduler,
        oom_retry=config.models.oom_retry,
    )
    manager.start_maintenance()

    for name, policy in config.models.policies.items():
        if policy.warmup and name in manager.names():
            operations.submit(
                idempotency_key=f"startup-warmup:{name}",
                action="load",
                model=name,
                reason="startup_warmup",
            )
    return ModelWorkerRuntime(manager, scheduler, operations, tts_jobs)


def _register_model(
    manager: ModelManager,
    name: str,
    model: Any,
    policy: ModelPolicyConfig,
    *,
    enabled: bool,
    lazy: bool = False,
) -> None:
    load = getattr(model, "load", None)
    unload = getattr(model, "unload", None)
    health = getattr(model, "health", None)
    estimate = policy.estimated_vram_mb or _CATALOG_VRAM_MB[name]
    manager.register(
        ManagedModelSpec(
            name=name,
            loader=(lambda model=model: model)
            if lazy or not callable(load)
            else (lambda model=model: (model.load(), model)[1]),
            unloader=(lambda handle, model=model: model.unload())
            if callable(unload)
            else None,
            health_check=health if callable(health) else None,
            preflight_check=lambda name=name, model=model: _interface_preflight(
                name,
                model,
            ),
            enabled=enabled,
            manual_unload_allowed=True,
            priority=policy.priority,
            warmup=policy.warmup,
            evictable=policy.evictable,
            pinned=policy.pinned,
            idle_unload_s=policy.idle_unload_s,
            min_residency_s=policy.min_residency_s,
            estimated_vram_mb=estimate,
        )
    )
    attach = getattr(model, "set_model_registry", None)
    if callable(attach):
        attach(manager, name)


def _interface_preflight(name: str, model: Any) -> dict:
    required_method = {
        "vlm": "understand",
        "stt": "recognize",
        "local_chat": "generate",
        "tts": "synthesize_stream",
        "embedding": "embed",
    }[name]
    available = callable(getattr(model, required_method, None))
    return {
        "name": "model_interface",
        "ok": available,
        "severity": "error",
        "detail": {"required_method": required_method},
    }
