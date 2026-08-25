import os
import time
from dataclasses import dataclass
from typing import Callable

from yuki.cognition.brain.hub import (
    COGNITION_AWAKE_SERVICE,
    COGNITION_CHAT_SERVICE,
    DecisionHub,
    SOUL_GET_SERVICE,
    build_brain,
)
from yuki.cognition.brain.local import (
    LocalChatModel,
    LocalComposer,
    LocalRouter,
    LocalViewBuilder,
    VisionScreenAdapter,
)
from yuki.cognition.brain.persona import (
    compose_personality_description,
    generate as generate_persona,
)
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.brain.sedimenter import PreferenceSedimenter
from yuki.cognition.brain.snapshots import PersonaStore
from yuki.cognition.brain.soul import PREFS_PER_PERSONA_REGEN, SoulStore, TunerStateStore
from yuki.cognition.brain.tuner import FeedbackTuner
from yuki.cognition.context.snapshot import ContextProjector
from yuki.cognition.context.store import ShortTermTurnStore
from yuki.cognition.context.working import WorkingContext
from yuki.cognition.gpu_monitor import GpuMemoryMonitor
from yuki.cognition.l2.bridge import CloudBridge
from yuki.cognition.l2.client import CloudClient
from yuki.cognition.model_registry import ModelRegistry, ModelSpec
from yuki.cognition.pipeline import PerceptionPipeline, build_pipeline
from yuki.cognition.speech_buffer import SpeechBuffer
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vad import FsmnVadBackend
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.functions.memory_tools import register_memory_functions
from yuki.functions.perception_tools import register_perception_tools
from yuki.functions.registry import FunctionRegistry
from yuki.functions.service import register_function_services
from yuki.functions.system import register_builtin_system
from yuki.logger import get_logger
from yuki.memory.embedding import build_embedding_indexer
from yuki.memory.manager import MemoryManager
from yuki.memory.privacy import MemoryAccess, MemoryPurpose
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

logger = get_logger("yuki.cognition.assembly")


@dataclass
class CognitionRuntime:
    pipeline: PerceptionPipeline
    memory: MemoryManager
    registry: FunctionRegistry
    model_registry: ModelRegistry
    bridge: CloudBridge | None
    hub: DecisionHub
    context: WorkingContext
    soul_store: SoulStore
    persona_store: PersonaStore
    persona_refresh: Callable[..., None]

    def handle_awake_request(self, payload: dict) -> dict:
        payload = dict(payload or {})
        payload.setdefault("source", "request")
        payload.setdefault("ts", time.time())
        if hasattr(self.pipeline, "on_awake"):
            self.pipeline.on_awake(Topics.AWAKE, payload)
        return self.hub.handle_awake_request(payload)

    def handle_chat_request(self, payload: dict) -> dict:
        payload = dict(payload or {})
        payload.setdefault("session_id", "default")
        payload.setdefault("task_id", "")
        return self.hub.handle_chat_request(payload)

    def handle_soul_get(self, payload: dict) -> dict:
        return {"soul": self.soul_store.load_or_default()}


class CognitionAssembler:
    """Builds cognition runtime adapters behind one lifecycle seam."""

    def __init__(
        self,
        config: Config,
        bus,
        *,
        pipeline=None,
        vlm=None,
        stt=None,
        frame_client=None,
        speech_buffer=None,
        memory: MemoryManager | None = None,
        registry: FunctionRegistry | None = None,
        model_registry: ModelRegistry | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.pipeline = pipeline
        self.vlm = vlm
        self.stt = stt
        self.frame_client = frame_client
        self.speech_buffer = speech_buffer
        self.memory = memory
        self.registry = registry
        self.model_registry = model_registry

    def assemble(self) -> CognitionRuntime:
        model_registry = self.model_registry or ModelRegistry(gpu_monitor=GpuMemoryMonitor())
        if self.pipeline is None:
            vlm = self.vlm or self._build_vlm()
            stt = self.stt or self._build_stt()
            speech_buffer = self.speech_buffer or self._build_speech_buffer()
            pipeline = build_pipeline(
                self.bus,
                vlm=vlm,
                stt=stt,
                frame_client=self.frame_client,
                speech_buffer=speech_buffer,
                text_summary_chars=self.config.text.summary_chars,
                text_key_point_chars=self.config.text.key_point_chars,
                deep_interval_s=self.config.vlm.deep_interval_s,
                user_bypass_rate_limit=self.config.vlm.user_bypass_rate_limit,
                listen_timeout_s=self.config.wake_word.listen_timeout_s,
                listen_window_s=self.config.wake_word.listen_window_s,
                pre_roll_s=self.config.wake_word.pre_roll_s,
            )
        else:
            pipeline = self.pipeline
            vlm = getattr(pipeline, "vlm", getattr(pipeline, "_vlm", None))
            stt = getattr(pipeline, "stt", getattr(pipeline, "_stt", None))
            speech_buffer = getattr(pipeline, "speech_buffer", self.speech_buffer)
        self._register_runtime_models(model_registry, vlm, stt, speech_buffer)
        pipeline.warmup_vlm()
        if self.config.stt.warmup and hasattr(pipeline, "warmup_stt"):
            pipeline.warmup_stt()

        memory = self.memory or self._build_memory()
        register_memory_services(self.bus, memory)

        registry = self.registry or FunctionRegistry()
        if self.registry is None:
            register_builtin_system(registry)
        register_memory_functions(registry, memory)
        self._register_perception_functions(registry, pipeline)
        register_function_services(self.bus, registry)

        bridge = self._build_bridge(registry)
        persona_store = PersonaStore(
            self.config.persona.snapshots_path,
            max_versions=self.config.persona.max_versions,
            persona_name=self.config.persona_name,
        )
        soul_store = SoulStore(
            self.config.soul.path,
            self.config.persona_name,
            default_description=self.config.persona.prompt.format(persona=self.config.persona_name),
            tuner_state_path=self.config.soul.tuner_state_path,
        )
        soul_store.ensure()
        persona_refresh = self._build_persona_refresh(memory, bridge, persona_store, soul_store)

        policy = DecisionPolicy(
            proactive_cooldown_s=self.config.brain.proactive_cooldown_s,
            proactive_enabled=self.config.brain.proactive_enabled,
            binding_core_values=soul_store.binding_core_values(),
        )
        tuner = FeedbackTuner(
            policy,
            TunerStateStore(self.config.soul.tuner_state_path, self.config.persona_name),
            soul=soul_store,
        )
        tuner.load_soul()
        context = WorkingContext(
            ShortTermTurnStore(memory),
            snapshot_path=self.config.context.snapshot_path or None,
        )
        context.restore()
        projector = ContextProjector(max_turns=self.config.context.max_turns)
        sedimenter = PreferenceSedimenter(
            memory,
            tuner=tuner,
            min_signals=self.config.sedimenter.min_signals,
            confidence_threshold=self.config.sedimenter.confidence_threshold,
            topic_engagement_threshold=self.config.sedimenter.topic_engagement_threshold,
            soul=soul_store,
            on_sedimented=persona_refresh,
        )
        local_router, local_composer, vision_screen = self._build_local_brain(
            registry,
            pipeline,
            model_registry,
        )
        hub = build_brain(
            self.bus,
            memory=memory,
            registry=registry,
            config=self.config,
            policy=policy,
            bridge=bridge,
            tuner=tuner,
            context=context,
            projector=projector,
            sedimenter=sedimenter,
            local_router=local_router,
            local_composer=local_composer,
            vision_screen=vision_screen,
            register_awake_service=False,
        )

        active = persona_store.active()
        if bridge is not None:
            bridge.set_system_prompt(self._active_persona_prompt(active, soul_store))
        if local_composer is not None:
            local_composer.set_system_prompt(
                self._active_persona_prompt(active, soul_store)
            )
        persona_refresh()

        runtime = CognitionRuntime(
            pipeline=pipeline,
            memory=memory,
            registry=registry,
            model_registry=model_registry,
            bridge=bridge,
            hub=hub,
            context=context,
            soul_store=soul_store,
            persona_store=persona_store,
            persona_refresh=persona_refresh,
        )
        self.bus.respond(COGNITION_AWAKE_SERVICE, runtime.handle_awake_request)
        self.bus.respond(COGNITION_CHAT_SERVICE, runtime.handle_chat_request)
        self.bus.respond(SOUL_GET_SERVICE, runtime.handle_soul_get)
        return runtime

    def _build_memory(self) -> MemoryManager:
        store = MemoryStore(self.config.memory.db_path)
        embedding_indexer = None
        if self.config.memory.vector_enabled:
            embedding_indexer = build_embedding_indexer(
                store,
                provider_name=self.config.memory.embedding_provider,
                model=self.config.memory.embedding_model,
                dimension=self.config.memory.embedding_dimension,
            )
        return MemoryManager(
            store,
            decay_base=self.config.memory.decay_base,
            decay_lambda=self.config.memory.decay_lambda,
            decay_threshold=self.config.memory.decay_threshold,
            short_term_ttl_s=self.config.memory.short_term_ttl_s,
            short_term_capacity=self.config.memory.short_term_capacity,
            embedding_indexer=embedding_indexer,
            vector_enabled=self.config.memory.vector_enabled,
            vector_candidates=self.config.memory.vector_candidates,
            lexical_weight=self.config.memory.lexical_weight,
            vector_weight=self.config.memory.vector_weight,
            confidence_weight=self.config.memory.confidence_weight,
        )

    def _build_vlm(self) -> VisualUnderstander:
        vlm_cfg = self.config.vlm
        return VisualUnderstander(
            model_id=vlm_cfg.model,
            cache_dir=vlm_cfg.cache_dir,
            enabled=vlm_cfg.enabled,
        )

    def _build_stt(self) -> SpeechRecognizer:
        stt_cfg = self.config.stt
        return SpeechRecognizer(
            enabled=stt_cfg.enabled,
            model_id=stt_cfg.model,
            model_dir=stt_cfg.model_dir,
            device=stt_cfg.device,
            language=stt_cfg.language,
            use_itn=stt_cfg.use_itn,
            retry_window_s=stt_cfg.retry_window_s,
        )

    def _build_speech_buffer(self) -> SpeechBuffer:
        stt_cfg = self.config.stt
        vad_cfg = stt_cfg.vad
        return SpeechBuffer(
            vad=FsmnVadBackend(model=vad_cfg.model, device=stt_cfg.device, enabled=stt_cfg.enabled),
            vad_interval_ms=vad_cfg.vad_interval_ms,
            end_silence_ms=vad_cfg.end_silence_ms,
            max_utterance_s=vad_cfg.max_utterance_s,
        )

    def _build_bridge(self, registry: FunctionRegistry) -> CloudBridge | None:
        if not self.config.cloud.enabled:
            return None
        return CloudBridge(
            CloudClient(
                base_url=self.config.cloud.base_url,
                model=self.config.cloud.model,
                api_key=os.environ.get(self.config.cloud.api_key_env),
                timeout_s=self.config.cloud.timeout_s,
            ),
            registry=registry,
            max_turns=self.config.cloud.max_turns,
            persona_name=self.config.persona_name,
        )

    def _build_local_brain(
        self,
        registry: FunctionRegistry,
        pipeline: PerceptionPipeline,
        model_registry: ModelRegistry,
    ) -> tuple[LocalRouter | None, LocalComposer | None, VisionScreenAdapter | None]:
        local_cfg = self.config.local_brain
        if not local_cfg.enabled:
            return None, None, None
        model = LocalChatModel(
            model_id=local_cfg.model_id,
            cache_dir=local_cfg.cache_dir,
            device=local_cfg.device,
            enabled=local_cfg.enabled,
            fp8_dequantize=local_cfg.fp8_dequantize,
            local_files_only=local_cfg.local_files_only,
        )
        self._register_model_object(
            model_registry,
            "local_chat",
            model,
            priority=1,
            critical=False,
        )
        router = LocalRouter(
            model,
            registry=registry,
            threshold=local_cfg.router_threshold,
            retry=local_cfg.retry,
            prompt_max_tokens=local_cfg.router_prompt_max_tokens,
            timeout_ms=local_cfg.router_timeout_ms,
            local_tool_allowlist=local_cfg.local_tool_allowlist,
            model_registry=model_registry,
        )
        composer = LocalComposer(
            model,
            persona_name=self.config.persona_name,
            view_builder=LocalViewBuilder(max_tokens=local_cfg.local_prompt_max_tokens),
            reply_max_tokens=local_cfg.reply_max_tokens,
            timeout_ms=local_cfg.local_reply_timeout_ms,
            model_registry=model_registry,
        )
        frame_client = pipeline.frame_client
        vlm = pipeline.vlm
        screen = (
            VisionScreenAdapter(frame_client, vlm, timeout_ms=local_cfg.vision_timeout_ms)
            if frame_client is not None and vlm is not None
            else None
        )
        router.warmup()
        return router, composer, screen

    def _register_runtime_models(
        self,
        model_registry: ModelRegistry,
        vlm,
        stt,
        speech_buffer,
    ) -> None:
        if vlm is not None:
            self._register_model_object(model_registry, "vlm", vlm, priority=2, critical=False)
        if stt is not None:
            self._register_model_object(model_registry, "stt", stt, priority=1, critical=False)
        vad = getattr(speech_buffer, "_vad", None)
        if vad is not None:
            self._register_model_object(model_registry, "vad", vad, priority=1, critical=False)

    def _register_model_object(
        self,
        model_registry: ModelRegistry,
        name: str,
        model,
        *,
        priority: int,
        critical: bool,
    ) -> None:
        load = getattr(model, "load", None)
        unload = getattr(model, "unload", None)
        health = getattr(model, "health", None)
        if not callable(load) or not callable(health):
            return
        try:
            model_registry.register(
                ModelSpec(
                    name=name,
                    loader=lambda model=model: (model.load(), model)[1],
                    unloader=(lambda handle, model=model: model.unload())
                    if callable(unload)
                    else None,
                    health_check=health,
                    priority=priority,
                    critical=critical,
                )
            )
        except ValueError:
            logger.debug("model already registered", model=name)

    def _active_persona_prompt(self, active, soul_store: SoulStore) -> str:
        return (
            active.persona_prompt
            if active
            else generate_persona(
                self.config.persona_name,
                [],
                {},
                base_prompt=self.config.persona.prompt,
                soul=soul_store.load_or_default(),
            )
        )

    def _register_perception_functions(
        self,
        registry: FunctionRegistry,
        pipeline: PerceptionPipeline,
    ) -> None:
        register_perception_tools(registry, pipeline)

    def _build_persona_refresh(
        self,
        memory: MemoryManager,
        bridge: CloudBridge | None,
        persona_store: PersonaStore,
        soul_store: SoulStore,
    ) -> Callable[[], None]:
        def persona_refresh(
            *,
            label: str = "",
            confidence: float = 0.0,
            content: str = "",
            is_new: bool = True,
        ) -> None:
            if label:
                soul = soul_store.on_preference_sedimented(
                    label,
                    confidence,
                    is_new=is_new,
                )
            else:
                soul = soul_store.load_or_default()
            prefs = MemoryAccess(memory).list(
                purpose=MemoryPurpose.PERSONA_REFINE_CLOUD,
                memory_type="preference",
            )
            refine = bridge.refine_persona if self.config.persona.enable_llm_refine and bridge else None
            should_regenerate_description = (
                int(soul.get("prefs_since_regen", 0)) >= PREFS_PER_PERSONA_REGEN
            )
            if should_regenerate_description:
                description = compose_personality_description(
                    soul,
                    base_description=self.config.persona.prompt.format(
                        persona=self.config.persona_name
                    ),
                    refine=refine,
                )
                if soul_store.set_personality_description(description):
                    soul = soul_store.load_or_default()
            prompt = generate_persona(
                self.config.persona_name,
                prefs,
                {},
                base_prompt=self.config.persona.prompt,
                refine=refine,
                soul=soul,
            )
            snap = persona_store.save(prompt, {}, soul=soul_store.snapshot())
            if snap is not None and bridge is not None:
                bridge.set_system_prompt(snap.persona_prompt)
            if should_regenerate_description:
                soul_store.reset_prefs_since_regen()

        return persona_refresh
