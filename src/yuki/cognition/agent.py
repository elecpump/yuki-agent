import os

from yuki.cognition.l2.bridge import CloudBridge
from yuki.cognition.l2.client import CloudClient
from yuki.cognition.brain.persona import generate as generate_persona
from yuki.cognition.brain.hub import build_brain
from yuki.cognition.brain.snapshots import PersonaStore
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.context.snapshot import ContextProjector
from yuki.cognition.context.store import ShortTermTurnStore
from yuki.cognition.context.working import WorkingContext
from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.tuner import FeedbackTuner
from yuki.cognition.brain.sedimenter import PreferenceSedimenter
from yuki.cognition.l1 import L1Engine
from yuki.cognition.pipeline import build_pipeline
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.functions.memory_tools import register_memory_functions
from yuki.functions.registry import FunctionRegistry
from yuki.functions.system import register_builtin_system
from yuki.health import HealthStatus
from yuki.logger import get_logger
from yuki.memory.manager import MemoryManager
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryStore
from yuki.process import ProcessAgent

logger = get_logger("yuki.cognition.agent")


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
        self._context = None
        self._persona_store = None
        self._persona_refresh = None

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
        self._persona_store = PersonaStore(
            self.config.persona.snapshots_path,
            max_versions=self.config.persona.max_versions,
            persona_name=self.config.persona_name,
        )

        def persona_refresh() -> None:
            prefs = [m for m in self._memory.list(memory_type="preference")
                     if m.get("sensitivity", 0) != 2]
            prompt = generate_persona(
                self.config.persona_name, prefs, {},
                base_prompt=self.config.persona.prompt,
            )
            snap = self._persona_store.save(prompt, {})
            if snap is not None and bridge is not None:
                bridge.set_system_prompt(snap.persona_prompt)
        self._persona_refresh = persona_refresh

        policy = DecisionPolicy(
            proactive_cooldown_s=self.config.brain.proactive_cooldown_s,
            proactive_enabled=self.config.brain.proactive_enabled,
        )
        soul = SoulStore(self.config.soul.path, self.config.persona_name)
        tuner = FeedbackTuner(policy, soul)
        tuner.load_soul()
        context = WorkingContext(
            ShortTermTurnStore(self._memory),
            snapshot_path=self.config.context.snapshot_path or None,
        )
        context.restore()
        projector = ContextProjector(max_turns=self.config.context.max_turns)
        sedimenter = PreferenceSedimenter(
            self._memory,
            tuner=tuner,
            min_signals=self.config.sedimenter.min_signals,
            confidence_threshold=self.config.sedimenter.confidence_threshold,
            topic_engagement_threshold=self.config.sedimenter.topic_engagement_threshold,
            on_sedimented=persona_refresh,
        )
        self._context = context
        self._bridge = bridge
        self._hub = build_brain(
            self.bus,
            memory=self._memory,
            registry=self._registry,
            config=self.config,
            policy=policy,
            bridge=bridge,
            tuner=tuner,
            context=context,
            projector=projector,
            sedimenter=sedimenter,
        )
        active = self._persona_store.active()
        if bridge is not None:
            bridge.set_system_prompt(active.persona_prompt if active
                                     else self.config.persona.prompt.format(persona=self.config.persona_name))
        persona_refresh()

    def teardown(self) -> None:
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
