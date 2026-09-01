import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yuki.cognition.brain.hub import (
    COGNITION_AWAKE_SERVICE,
    COGNITION_CHAT_SERVICE,
    SOUL_GET_SERVICE,
    DecisionHub,
    build_brain,
)
from yuki.cognition.brain.cooldown import CooldownCalculator
from yuki.cognition.brain.local import (
    LocalComposer,
    LocalRouter,
    LocalViewBuilder,
)
from yuki.cognition.brain.persona import (
    generate as generate_persona,
)
from yuki.cognition.brain.snapshots import PersonaStore
from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.soul_reflector import SoulReflector
from yuki.cognition.brain.soul_scheduler import SoulReflectionScheduler
from yuki.cognition.context.consolidation import (
    CandidateResolver,
    ConsolidationStore,
    EvolutionPolicy,
)
from yuki.cognition.context.maintenance import SegmentSummarizer, ThreadMaintenanceScheduler
from yuki.cognition.context.sediment import Sedimenter
from yuki.cognition.context.snapshot import ContextProjector
from yuki.cognition.context.store import ThreadTurnStore
from yuki.cognition.context.working import WorkingContext
from yuki.cognition.l2.bridge import CloudBridge
from yuki.cognition.l2.client import CloudClient
from yuki.cognition.l2.proactive import ProactiveAgent
from yuki.cognition.l2.view import CloudViewBuilder
from yuki.cognition.pipeline import PerceptionPipeline, build_pipeline
from yuki.cognition.speech_buffer import SpeechBuffer
from yuki.cognition.vad import FsmnVadBackend
from yuki.config import Config
from yuki.functions.memory_tools import register_memory_functions
from yuki.functions.perception_tools import register_perception_tools
from yuki.functions.registry import FunctionRegistry
from yuki.functions.service import register_function_services
from yuki.functions.soul_tools import register_soul_functions
from yuki.functions.system import register_builtin_system
from yuki.logger import get_logger
from yuki.memory.embedding import (
    EmbeddingOutboxWorker,
    EmbeddingProvider,
    MemoryEmbeddingIndexer,
    build_embedding_indexer,
)
from yuki.memory.manager import MemoryManager
from yuki.memory.privacy import MemoryAccess
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryStore
from yuki.model_cache import ModelCacheManager
from yuki.model_client import LocalChatModelClient, RemoteModelRegistry
from yuki.runtime_bus import RuntimeBusProtocol
from yuki.topics import Topics

logger = get_logger("yuki.cognition.assembly")


@dataclass
class CognitionRuntime:
    pipeline: PerceptionPipeline
    memory: MemoryManager
    registry: FunctionRegistry
    model_registry: RemoteModelRegistry | None
    bridge: CloudBridge | None
    hub: DecisionHub
    context: WorkingContext
    soul_store: SoulStore
    persona_store: PersonaStore
    persona_refresh: Callable[..., None]
    soul_reflection_scheduler: SoulReflectionScheduler | None
    thread_maintenance_scheduler: ThreadMaintenanceScheduler | None
    cache_manager: ModelCacheManager

    def handle_awake_request(self, payload: dict) -> dict:
        payload = dict(payload or {})
        payload.setdefault("source", "request")
        payload.setdefault("ts", time.time())
        if hasattr(self.pipeline, "on_awake"):
            self.pipeline.on_awake(Topics.AWAKE, payload)
        return self.hub.handle_awake_request(payload)

    def handle_chat_request(self, payload: dict) -> dict:
        payload = dict(payload or {})
        payload.setdefault("task_id", "")
        return self.hub.handle_chat_request(payload)

    def handle_soul_get(self, payload: dict) -> dict:
        return {"soul": self.soul_store.load_or_default()}


class CognitionAssembler:
    """Builds cognition runtime adapters behind one lifecycle seam."""

    def __init__(
        self,
        config: Config,
        bus: RuntimeBusProtocol,
        *,
        pipeline=None,
        vlm=None,
        stt=None,
        frame_client=None,
        speech_buffer=None,
        memory: MemoryManager | None = None,
        registry: FunctionRegistry | None = None,
        model_registry: RemoteModelRegistry | None = None,
        cache_manager: ModelCacheManager | None = None,
        local_chat_model: LocalChatModelClient | None = None,
        embedding_provider: EmbeddingProvider | None = None,
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
        self.cache_manager = cache_manager
        self.local_chat_model = local_chat_model
        self.embedding_provider = embedding_provider

    def assemble(self) -> CognitionRuntime:
        cache_manager = self.cache_manager or ModelCacheManager(max_entries=256)
        if self.pipeline is None:
            speech_buffer = self.speech_buffer or self._build_speech_buffer()
            pipeline = build_pipeline(
                self.bus,
                vlm=self.vlm,
                stt=self.stt,
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
        pipeline.warmup_vlm()
        if self.config.stt.warmup and hasattr(pipeline, "warmup_stt"):
            pipeline.warmup_stt()

        memory = self.memory or self._build_memory(cache_manager=cache_manager)
        register_memory_services(self.bus, memory)

        registry = self.registry or FunctionRegistry()
        if self.registry is None:
            register_builtin_system(registry)
        register_memory_functions(registry, memory)
        self._register_perception_functions(registry, pipeline)
        register_function_services(self.bus, registry)

        cloud_client = self._build_cloud_client()
        bridge = self._build_bridge(registry, client=cloud_client)
        persona_store = PersonaStore(
            self.config.persona.snapshots_path,
            max_versions=self.config.persona.max_versions,
            persona_name=self.config.persona_name,
        )
        soul_store = SoulStore(
            self.config.soul.path,
            self.config.persona_name,
            default_description=self.config.persona.prompt.format(persona=self.config.persona_name),
            cooldown_state_path=self.config.soul.cooldown_state_path,
            legacy_tuner_state_path=self.config.soul.legacy_tuner_state_path,
            snapshots_dir=self.config.soul.snapshots_dir,
            max_versions=self.config.soul.max_versions,
            min_snapshot_interval_s=self.config.soul.min_snapshot_interval_s,
            max_description_chars=self.config.soul.max_description_chars,
        )
        soul_store.ensure()
        legacy_tuner_state_path = self.config.soul.legacy_tuner_state_path or Path(
            self.config.soul.cooldown_state_path
        ).with_name("tuner_state.json")
        cooldown = CooldownCalculator(
            self.config.brain.proactive_cooldown_s,
            path=self.config.soul.cooldown_state_path,
            legacy_path=legacy_tuner_state_path,
            persona_name=self.config.persona_name,
            max_cooldown_s=self.config.brain.max_cooldown_s,
        )
        proactive_agent = (
            ProactiveAgent(
                cloud_client,
                timeout_s=self.config.brain.proactive_timeout_s,
                max_chars=self.config.brain.proactive_max_chars,
                view_builder=CloudViewBuilder(
                    verbatim_turns=self.config.thread.verbatim_turns
                ),
            )
            if cloud_client is not None
            else None
        )
        thread_db_path = memory.db_path or Path(self.config.memory.db_path)
        thread_store = ThreadTurnStore(
            thread_db_path,
            segment_max_turns=self.config.thread.segment_max_turns,
            episode_idle_s=self.config.thread.episode_idle_s,
        )
        context = WorkingContext(thread_store)
        projector = ContextProjector(
            max_turns=self.config.thread.segment_verbatim_max,
            fallback_turns=self.config.thread.fallback_turns,
            max_summaries=self.config.thread.history_summary_max_segments,
            summary_max_tokens=self.config.thread.history_summary_max_tokens,
        )
        local_router, local_composer = self._build_local_brain()
        persona_refresh = self._build_persona_refresh(
            memory,
            bridge,
            persona_store,
            soul_store,
            local_composer,
        )
        soul_store.set_on_updated(lambda: persona_refresh(refine=False))
        register_soul_functions(registry, soul_store)
        soul_reflection_scheduler = None
        thread_maintenance_scheduler = None
        if cloud_client is not None:
            consolidation_store = None
            sedimenter = None
            if memory.db_path is not None:
                consolidation_store = ConsolidationStore(
                    thread_db_path,
                    policy=EvolutionPolicy(
                        promotion_min_episodes=self.config.sediment.promotion_min_episodes,
                        strengthen_min_episodes=self.config.sediment.strengthen_min_episodes,
                        tombstone_min_episodes=self.config.sediment.tombstone_min_episodes,
                        update_min_episodes=self.config.sediment.update_min_episodes,
                        explicit_activation_confidence=(
                            self.config.sediment.explicit_activation_confidence
                        ),
                    ),
                    resolver=CandidateResolver(
                        threshold=self.config.sediment.candidate_merge_similarity,
                    ),
                    related_provider=lambda turns, limit: self._related_memories(
                        memory,
                        turns,
                        limit,
                    ),
                )
                sedimenter = Sedimenter(
                    cloud_client,
                    model=self.config.cloud.model,
                    timeout_s=self.config.sediment.timeout_s,
                    domain_instructions=self.config.sediment.domain_instructions,
                )
            thread_maintenance_scheduler = ThreadMaintenanceScheduler(
                thread_store,
                SegmentSummarizer(
                    cloud_client,
                    model=self.config.cloud.model,
                    timeout_s=self.config.cloud.timeout_s,
                ),
                summary_failures_max=self.config.thread.summary_failures_max,
                tick_s=self.config.thread.maintenance_tick_s,
                consolidation_store=consolidation_store,
                sedimenter=sedimenter,
                retry_base_s=self.config.sediment.retry_base_s,
                retry_max_s=self.config.sediment.retry_max_s,
                outbox_worker=EmbeddingOutboxWorker(memory),
            )
            reflector = SoulReflector(
                cloud_client,
                soul_store,
                memory,
                timeout_s=self.config.cloud.timeout_s,
            )
            soul_reflection_scheduler = SoulReflectionScheduler(
                reflector,
                every_utterances=self.config.soul.reflect_every_utterances,
                interval_s=self.config.soul.reflect_interval_s,
            )
        hub = build_brain(
            self.bus,
            memory=memory,
            registry=registry,
            config=self.config,
            bridge=bridge,
            context=context,
            projector=projector,
            proactive_agent=proactive_agent,
            cooldown_calculator=cooldown,
            soul_store=soul_store,
            local_router=local_router,
            local_composer=local_composer,
            periodic=[persona_refresh],
            periodic_interval=self.config.persona.refresh_every_utterances,
            utterance_observers=(
                [soul_reflection_scheduler.on_utterance]
                if soul_reflection_scheduler is not None
                else []
            ),
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
            model_registry=self.model_registry,
            bridge=bridge,
            hub=hub,
            context=context,
            soul_store=soul_store,
            persona_store=persona_store,
            persona_refresh=persona_refresh,
            soul_reflection_scheduler=soul_reflection_scheduler,
            thread_maintenance_scheduler=thread_maintenance_scheduler,
            cache_manager=cache_manager,
        )
        self.bus.respond(COGNITION_AWAKE_SERVICE, runtime.handle_awake_request)
        self.bus.respond(COGNITION_CHAT_SERVICE, runtime.handle_chat_request)
        self.bus.respond(SOUL_GET_SERVICE, runtime.handle_soul_get)
        return runtime

    def _build_memory(self, *, cache_manager: ModelCacheManager | None = None) -> MemoryManager:
        store = MemoryStore(self.config.memory.db_path)
        embedding_indexer = None
        if self.config.memory.vector_enabled:
            if self.embedding_provider is not None:
                embedding_indexer = MemoryEmbeddingIndexer(
                    store,
                    self.embedding_provider,
                    cache_manager=cache_manager,
                )
            else:
                embedding_indexer = build_embedding_indexer(
                    store,
                    provider_name=self.config.memory.embedding_provider,
                    model=self.config.memory.embedding_model,
                    dimension=self.config.memory.embedding_dimension,
                    cache_dir=self.config.memory.embedding_cache_dir,
                    device=self.config.memory.embedding_device,
                    cache_manager=cache_manager,
                )
        return MemoryManager(
            store,
            decay_base=self.config.memory.decay_base,
            decay_lambda=self.config.memory.decay_lambda,
            decay_threshold=self.config.memory.decay_threshold,
            embedding_indexer=embedding_indexer,
            vector_enabled=self.config.memory.vector_enabled,
            vector_candidates=self.config.memory.vector_candidates,
            superseded_retention_days=self.config.memory.superseded_retention_days,
            tombstone_retention_days=self.config.memory.tombstone_retention_days,
        )

    @staticmethod
    def _related_memories(
        memory: MemoryManager,
        turns: list[dict],
        limit: int,
    ) -> list[dict]:
        related: list[dict] = []
        seen: set[int] = set()
        user_texts = [
            str(turn.get("content", ""))
            for turn in turns
            if turn.get("role", turn.get("kind")) == "user"
        ]
        for text in reversed(user_texts):
            for item in memory.query(text, top_k=limit, touch=False):
                memory_id = int(item["id"])
                if memory_id in seen:
                    continue
                seen.add(memory_id)
                related.append(item)
                if len(related) >= limit:
                    return related
        for item in memory.list():
            memory_id = int(item["id"])
            if memory_id in seen:
                continue
            seen.add(memory_id)
            related.append(item)
            if len(related) >= limit:
                break
        return related

    def _build_speech_buffer(self) -> SpeechBuffer:
        stt_cfg = self.config.stt
        vad_cfg = stt_cfg.vad
        return SpeechBuffer(
            vad=FsmnVadBackend(model=vad_cfg.model, device=stt_cfg.device, enabled=stt_cfg.enabled),
            vad_interval_ms=vad_cfg.vad_interval_ms,
            end_silence_ms=vad_cfg.end_silence_ms,
            max_utterance_s=vad_cfg.max_utterance_s,
        )

    def _build_cloud_client(self) -> CloudClient | None:
        if not self.config.cloud.enabled:
            return None
        api_key = os.environ.get(self.config.cloud.api_key_env)
        if not api_key:
            return None
        return CloudClient(
            base_url=self.config.cloud.base_url,
            model=self.config.cloud.model,
            api_key=api_key,
            timeout_s=self.config.cloud.timeout_s,
        )

    def _build_bridge(
        self,
        registry: FunctionRegistry,
        *,
        client: CloudClient | None = None,
    ) -> CloudBridge | None:
        client = client or self._build_cloud_client()
        if client is None:
            return None
        return CloudBridge(
            client,
            registry=registry,
            view_builder=CloudViewBuilder(
                verbatim_turns=self.config.thread.verbatim_turns
            ),
            max_turns=(
                self.config.agent_loop.max_steps
                if self.config.agent_loop.max_steps is not None
                else self.config.cloud.max_turns
            ),
            persona_name=self.config.persona_name,
            loop_kw={
                "max_duration_s": self.config.agent_loop.max_duration_s,
                "tool_result_max_chars": self.config.agent_loop.tool_result_max_chars,
                "compact_threshold_tokens": self.config.agent_loop.compact_threshold_tokens,
                "transition_fallback": self.config.agent_loop.transition_fallback,
            },
        )

    def _build_local_brain(
        self,
    ) -> tuple[LocalRouter | None, LocalComposer | None]:
        local_cfg = self.config.local_brain
        if not local_cfg.enabled:
            return None, None
        model = self.local_chat_model
        if model is None:
            raise ValueError(
                "local_chat_model client is required when local_brain is enabled"
            )
        router = LocalRouter(
            model,
            threshold=local_cfg.router_threshold,
            retry=local_cfg.retry,
            prompt_max_tokens=local_cfg.router_prompt_max_tokens,
            timeout_ms=local_cfg.router_timeout_ms,
        )
        composer = LocalComposer(
            model,
            persona_name=self.config.persona_name,
            view_builder=LocalViewBuilder(
                max_tokens=local_cfg.local_prompt_max_tokens,
                verbatim_turns=self.config.thread.verbatim_turns,
            ),
            reply_max_tokens=local_cfg.reply_max_tokens,
            timeout_ms=local_cfg.local_reply_timeout_ms,
        )
        router.warmup()
        return router, composer

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
        local_composer,
    ) -> Callable[[], None]:
        refresh_lock = threading.Lock()

        def persona_refresh(*, refine: bool = True) -> None:
            with refresh_lock:
                soul = soul_store.load_or_default()
                prefs = MemoryAccess(memory).personality_evidence()
                refine_fn = (
                    bridge.refine_persona
                    if refine and self.config.persona.enable_llm_refine and bridge
                    else None
                )
                prompt = generate_persona(
                    self.config.persona_name,
                    prefs,
                    {},
                    base_prompt=self.config.persona.prompt,
                    refine=refine_fn,
                    soul=soul,
                )
                snap = persona_store.save(prompt, {}, soul=soul_store.snapshot())
                effective_prompt = snap.persona_prompt if snap is not None else prompt
                if bridge is not None:
                    bridge.set_system_prompt(effective_prompt)
                if local_composer is not None:
                    local_composer.set_system_prompt(effective_prompt)

        return persona_refresh
