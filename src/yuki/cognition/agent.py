import os

from yuki.cognition.assembly import CognitionAssembler
from yuki.config import Config
from yuki.functions.registry import FunctionRegistry
from yuki.health import HealthStatus
from yuki.logger import get_logger
from yuki.memory.manager import MemoryManager
from yuki.memory.embedding import EmbeddingProvider
from yuki.model_client import LocalChatModelClient, RemoteModelRegistry
from yuki.process import ProcessAgent

logger = get_logger("yuki.cognition.agent")
SOUL_REFLECTION_CLOSE_TIMEOUT_S = 1.0


class CognitionAgent(ProcessAgent):
    name = "cognition"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 pipeline=None, l1=None, vlm=None, stt=None,
                 frame_client=None, speech_buffer=None,
                 memory: MemoryManager | None = None,
                 registry: FunctionRegistry | None = None,
                 model_registry: RemoteModelRegistry | None = None,
                 local_chat_model: LocalChatModelClient | None = None,
                 embedding_provider: EmbeddingProvider | None = None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._pipeline = pipeline
        self._l1 = l1
        self._vlm = vlm
        self._stt = stt
        self._frame_client = frame_client
        self._speech_buffer = speech_buffer
        self._memory = memory
        self._registry = registry
        self._model_registry = model_registry
        self._local_chat_model = local_chat_model
        self._embedding_provider = embedding_provider
        self._hub = None
        self._bridge = None
        self._context = None
        self._persona_store = None
        self._persona_refresh = None
        self._soul_reflection_scheduler = None
        self._thread_maintenance_scheduler = None

    def setup(self) -> None:
        runtime = CognitionAssembler(
            self.config,
            self.bus,
            pipeline=self._pipeline,
            vlm=self._vlm,
            stt=self._stt,
            frame_client=self._frame_client,
            speech_buffer=self._speech_buffer,
            memory=self._memory,
            registry=self._registry,
            model_registry=self._model_registry,
            local_chat_model=self._local_chat_model,
            embedding_provider=self._embedding_provider,
        )
        assembled = runtime.assemble()
        self._pipeline = assembled.pipeline
        self._memory = assembled.memory
        self._registry = assembled.registry
        self._model_registry = assembled.model_registry
        self._bridge = assembled.bridge
        self._hub = assembled.hub
        self._context = assembled.context
        self._persona_store = assembled.persona_store
        self._persona_refresh = assembled.persona_refresh
        self._soul_reflection_scheduler = assembled.soul_reflection_scheduler
        self._thread_maintenance_scheduler = assembled.thread_maintenance_scheduler
        self._hub.start()
        if self._thread_maintenance_scheduler is not None:
            self._thread_maintenance_scheduler.start()
        if self._soul_reflection_scheduler is not None:
            self._soul_reflection_scheduler.start()

    def teardown(self) -> None:
        # spec §8 teardown 顺序：停止接收新请求 → scheduler bounded flush →
        # 关闭 hub/model client → 关闭 ThreadStore/MemoryStore。
        self.bus.pause_subscriptions()
        if self._thread_maintenance_scheduler is not None:
            self._thread_maintenance_scheduler.close(
                timeout_s=self.config.thread.shutdown_timeout_s
            )
            self._thread_maintenance_scheduler = None
        if self._hub is not None:
            self._hub.close(timeout_s=SOUL_REFLECTION_CLOSE_TIMEOUT_S)
            self._hub = None
        if self._soul_reflection_scheduler is not None:
            self._soul_reflection_scheduler.close(
                timeout_s=SOUL_REFLECTION_CLOSE_TIMEOUT_S
            )
            self._soul_reflection_scheduler = None
        if self._pipeline is not None and hasattr(self._pipeline, "close"):
            self._pipeline.close()
        if self._model_registry is not None:
            try:
                shutdown = getattr(self._model_registry, "shutdown", None)
                if callable(shutdown):
                    shutdown()
            except Exception:
                logger.warning("model registry shutdown failed", exc_info=True)
            self._model_registry = None
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._memory is not None:
            self._memory.close()
            self._memory = None

    def loop(self) -> None:
        # 定期执行衰减清理；进程存活期间不会无限积累已过期记忆。
        while not self.shutdown.shutdown_requested:
            self.shutdown.wait(timeout=self.config.memory.cleanup_interval_s)
            if self._memory is not None:
                try:
                    deleted = self._memory.cleanup()
                    if deleted:
                        logger.info("memory cleanup finished", deleted=deleted)
                except Exception:
                    logger.warning("memory cleanup failed", exc_info=True)

    def health_components(self):
        return {
            "vlm": self._health_vlm,
            "stt": self._health_stt,
            "brain": self._health_brain,
            "l2": self._health_l2,
            "pipeline": self._health_pipeline,
            "memory": self._health_memory,
            "thread_maintenance": self._health_thread_maintenance,
            "models": self._health_models,
        }

    def _health_vlm(self) -> HealthStatus:
        vlm = getattr(self._pipeline, "_vlm", None) if self._pipeline else None
        if vlm is None:
            return HealthStatus(True, {"loaded": False, "degraded": True, "reason": "no_vlm"})
        health_fn = getattr(vlm, "health", None)
        if callable(health_fn):
            detail = health_fn()
            detail["degraded"] = bool(detail.get("degraded", False))
            return HealthStatus(True, detail)
        loaded = bool(getattr(vlm, "_loaded", False))
        detail = {"loaded": loaded, "degraded": not loaded}
        if not loaded:
            detail["reason"] = "unavailable" if getattr(vlm, "_load_failed", False) else "loading"
        return HealthStatus(True, detail)

    def _health_stt(self) -> HealthStatus:
        stt = getattr(self._pipeline, "_stt", None) if self._pipeline else None
        if stt is None:
            return HealthStatus(True, {"installed": False, "degraded": True, "reason": "no_stt"})
        health_fn = getattr(stt, "health", None)
        if callable(health_fn):
            return HealthStatus(True, {"installed": True, **health_fn()})
        return HealthStatus(True, {"installed": True})

    def _health_brain(self) -> HealthStatus:
        return HealthStatus(self._hub is not None, {"installed": self._hub is not None})

    def _health_l2(self) -> HealthStatus:
        enabled = self.config.cloud.enabled
        configured = bool(self.config.cloud.base_url and self.config.cloud.model)
        api_key_present = bool(os.environ.get(self.config.cloud.api_key_env))
        installed = self._bridge is not None
        # §8.2：云端不可用是设计内的降级路径（L1 兜底），不是进程故障，不触发重启。
        degraded_reason: str | bool = False
        if enabled and not api_key_present:
            degraded_reason = "missing_api_key"
        elif enabled and not configured:
            degraded_reason = "missing_cloud_config"
        elif enabled and not installed:
            degraded_reason = "cloud_unavailable"
        return HealthStatus(True, {
            "enabled": enabled,
            "degraded": degraded_reason,
            "installed": installed,
            "configured": configured,
            "api_key_present": api_key_present,
        })

    def _health_pipeline(self) -> HealthStatus:
        frame_client = getattr(self._pipeline, "_frame_client", None) if self._pipeline else None
        ok = frame_client is not None and hasattr(frame_client, "get_latest")
        return HealthStatus(ok, {"frame_client_available": ok})

    def _health_memory(self) -> HealthStatus:
        ok = self._memory is not None and self._memory.ping()
        return HealthStatus(ok, {"db": self.config.memory.db_path})

    def _health_thread_maintenance(self) -> HealthStatus:
        scheduler = self._thread_maintenance_scheduler
        if scheduler is None:
            enabled = self.config.cloud.enabled
            api_key_present = bool(os.environ.get(self.config.cloud.api_key_env))
            reason = "cloud_disabled"
            if enabled and not api_key_present:
                reason = "missing_api_key"
            elif enabled:
                reason = "cloud_unavailable"
            return HealthStatus(
                True,
                {
                    "enabled": False,
                    "degraded": reason if enabled else False,
                    "reason": reason,
                },
            )
        detail = scheduler.health()
        ok = bool(detail["worker_alive"]) and not bool(detail["closed"])
        return HealthStatus(ok, {"enabled": True, **detail})

    def _health_models(self) -> HealthStatus:
        if self._model_registry is None:
            return HealthStatus(True, {"status": "not_configured", "models": {}})
        status = self._model_registry.get_overall_status()
        return HealthStatus(bool(status["healthy"]), status)
