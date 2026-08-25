import os

from yuki.cognition.assembly import CognitionAssembler
from yuki.cognition.model_registry import ModelRegistry
from yuki.config import Config
from yuki.functions.registry import FunctionRegistry
from yuki.health import HealthStatus
from yuki.logger import get_logger
from yuki.memory.manager import MemoryManager
from yuki.process import ProcessAgent

logger = get_logger("yuki.cognition.agent")


class CognitionAgent(ProcessAgent):
    name = "cognition"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 pipeline=None, l1=None, vlm=None, stt=None,
                 frame_client=None, speech_buffer=None,
                 memory: MemoryManager | None = None,
                 registry: FunctionRegistry | None = None,
                 model_registry: ModelRegistry | None = None) -> None:
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
        self._hub = None
        self._bridge = None
        self._context = None
        self._persona_store = None
        self._persona_refresh = None

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

    def teardown(self) -> None:
        if self._pipeline is not None and hasattr(self._pipeline, "close"):
            self._pipeline.close()
        if self._model_registry is not None:
            self._model_registry.shutdown()
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
        degraded = enabled and not (installed and configured and api_key_present)
        return HealthStatus(True, {
            "enabled": enabled,
            "degraded": degraded,
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

    def _health_models(self) -> HealthStatus:
        if self._model_registry is None:
            return HealthStatus(True, {"status": "not_configured", "models": {}})
        status = self._model_registry.get_overall_status()
        return HealthStatus(status["status"] == "healthy", status)
