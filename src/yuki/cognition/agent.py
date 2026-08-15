import os
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.agent")

from yuki.cognition.l2.bridge import CloudBridge
from yuki.cognition.l2.client import CloudClient
from yuki.cognition.brain.hub import build_brain
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.tuner import FeedbackTuner
from yuki.cognition.l1 import L1Engine
from yuki.cognition.pipeline import build_pipeline
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.functions.memory_tools import register_memory_functions
from yuki.functions.registry import FunctionRegistry
from yuki.health import HealthStatus
from yuki.memory.manager import MemoryManager
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryStore
from yuki.process import ProcessAgent
from yuki.functions.system import register_builtin_system


class CognitionAgent(ProcessAgent):
    name = "cognition"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 pipeline=None, l1=None, vlm=None, stt=None,
                 frame_client=None, sensitive_filter=None, speech_buffer=None,
                 memory: MemoryManager | None = None,
                 registry: FunctionRegistry | None = None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._pipeline = pipeline
        self._l1 = l1
        self._vlm = vlm
        self._stt = stt
        self._frame_client = frame_client
        self._sensitive_filter = sensitive_filter
        self._speech_buffer = speech_buffer
        self._memory = memory
        self._registry = registry
        self._hub = None
        self._bridge = None

    def setup(self) -> None:
        if self._pipeline is None:
            self._pipeline = build_pipeline(
                self.bus,
                vlm=self._vlm,
                sensitive_filter=self._sensitive_filter,
                stt=self._stt,
                frame_client=self._frame_client,
                speech_buffer=self._speech_buffer,
            )
        self._pipeline.warmup_vlm()
        if self._memory is None:
            self._memory = MemoryManager(
                MemoryStore(self.config.memory.db_path),
                decay_base=self.config.memory.decay_base,
                decay_lambda=self.config.memory.decay_lambda,
                decay_threshold=self.config.memory.decay_threshold,
                short_term_ttl_s=self.config.memory.short_term_ttl_s,
                short_term_capacity=self.config.memory.short_term_capacity,
            )
        register_memory_services(self.bus, self._memory)
        if self._registry is None:
            self._registry = FunctionRegistry()
            register_builtin_system(self._registry)
        register_memory_functions(self._registry, self._memory)
        bridge = None
        if self.config.cloud.enabled:
            bridge = CloudBridge(
                CloudClient(
                    base_url=self.config.cloud.base_url,
                    model=self.config.cloud.model,
                    api_key=os.environ.get(self.config.cloud.api_key_env),
                    timeout_s=self.config.cloud.timeout_s,
                ),
                registry=self._registry,
                max_turns=self.config.cloud.max_turns,
                persona_name=self.config.persona_name,
            )
        policy = DecisionPolicy(
            proactive_cooldown_s=self.config.brain.proactive_cooldown_s,
            proactive_enabled=self.config.brain.proactive_enabled,
        )
        soul = SoulStore(self.config.soul.path, self.config.persona_name)
        tuner = FeedbackTuner(policy, soul)
        tuner.load_soul()
        self._bridge = bridge
        self._hub = build_brain(
            self.bus,
            memory=self._memory,
            registry=self._registry,
            config=self.config,
            policy=policy,
            bridge=bridge,
            tuner=tuner,
        )

    def teardown(self) -> None:
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
        }

    def _health_vlm(self) -> HealthStatus:
        vlm = getattr(self._pipeline, "_vlm", None) if self._pipeline else None
        if vlm is None:
            return HealthStatus(False, {"reason": "no_vlm"})
        return HealthStatus(vlm._loaded, {"loaded": vlm._loaded})

    def _health_stt(self) -> HealthStatus:
        stt = getattr(self._pipeline, "_stt", None) if self._pipeline else None
        return HealthStatus(stt is not None, {"installed": stt is not None})

    def _health_brain(self) -> HealthStatus:
        return HealthStatus(self._hub is not None, {"installed": self._hub is not None})

    def _health_l2(self) -> HealthStatus:
        enabled = self.config.cloud.enabled
        configured = bool(self.config.cloud.base_url and self.config.cloud.model)
        api_key_present = bool(os.environ.get(self.config.cloud.api_key_env))
        ok = (not enabled) or (
            self._bridge is not None and configured and api_key_present
        )
        return HealthStatus(ok, {
            "enabled": enabled,
            "installed": self._bridge is not None,
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
