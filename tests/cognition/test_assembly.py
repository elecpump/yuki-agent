from yuki.cognition.assembly import (
    COGNITION_VOICE_CANCEL_SERVICE,
    COGNITION_VOICE_START_SERVICE,
    COGNITION_VOICE_STATUS_SERVICE,
    CognitionAssembler,
    CognitionRuntime,
)
from yuki.cognition.brain.hub import COGNITION_AWAKE_SERVICE
from yuki.cognition.context.store import ThreadTurnStore
from yuki.config import Config
from yuki.functions.service import FUNCTIONS_CALL_SERVICE
from yuki.memory.manager import MemoryManager
from yuki.memory.service import MEMORY_SERVICES
from yuki.memory.store import MemoryStore
from yuki.model_client import RemoteModelRegistry
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakePipeline:
    def __init__(self):
        self.warmups = 0
        self.stt_warmups = 0
        self.voice = {"state": "idle", "session_id": None, "active": False}
        self.awake_payloads = []

    @property
    def frame_client(self):
        return getattr(self, "_frame_client", None)

    @property
    def vlm(self):
        return getattr(self, "_vlm", None)

    @property
    def stt(self):
        return getattr(self, "_stt", None)

    @property
    def speech_buffer(self):
        return getattr(self, "_speech_buffer", None)

    def warmup_vlm(self):
        self.warmups += 1

    def warmup_stt(self):
        self.stt_warmups += 1

    def voice_status(self):
        return dict(self.voice)

    def on_awake(self, topic, payload):
        self.awake_payloads.append((topic, dict(payload)))
        self.voice = {"state": "listening", "session_id": 1, "active": True}

    def cancel_voice(self):
        self.voice = {"state": "idle", "session_id": None, "active": False}
        return dict(self.voice)


def test_cognition_runtime_does_not_invent_chat_session_id():
    class Hub:
        def handle_chat_request(self, payload):
            return payload

    runtime = object.__new__(CognitionRuntime)
    runtime.hub = Hub()

    assert runtime.handle_chat_request({"text": "你好"}) == {
        "text": "你好",
        "task_id": "",
    }


def test_cognition_runtime_starts_and_cancels_frontend_voice_session():
    pipeline = FakePipeline()
    runtime = object.__new__(CognitionRuntime)
    runtime.pipeline = pipeline
    runtime.voice_available = True

    started = runtime.handle_voice_start({"source": "untrusted"})
    assert started == {"available": True, "state": "listening", "session_id": 1, "active": True}
    assert pipeline.awake_payloads[0][0] == Topics.AWAKE
    assert pipeline.awake_payloads[0][1]["source"] == "frontend"

    assert runtime.handle_voice_cancel({}) == {
        "available": True,
        "state": "idle",
        "session_id": None,
        "active": False,
    }


def test_cognition_runtime_reports_voice_unavailable_when_stt_is_disabled():
    pipeline = FakePipeline()
    runtime = object.__new__(CognitionRuntime)
    runtime.pipeline = pipeline
    runtime.voice_available = False

    assert runtime.handle_voice_start({}) == {
        "available": False,
        "state": "idle",
        "session_id": None,
        "active": False,
    }
    assert pipeline.awake_payloads == []


def test_cognition_assembler_builds_runtime_and_registers_services(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()
    memory = MemoryManager(MemoryStore(tmp_path / "mem.db"))

    runtime = CognitionAssembler(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus,
        pipeline=pipeline,
        memory=memory,
        model_registry=RemoteModelRegistry(bus),
    ).assemble()

    try:
        assert runtime.pipeline is pipeline
        assert runtime.memory is memory
        assert runtime.model_registry is not None
        assert runtime.local_model_control is not None
        assert runtime.hub is not None
        assert runtime.context is not None
        assert runtime.persona_store is not None
        assert runtime.persona_refresh is not None
        assert pipeline.warmups == 1
        assert pipeline.stt_warmups == 1
        assert all(service in bus.services for service in MEMORY_SERVICES)
        assert FUNCTIONS_CALL_SERVICE in bus.services
        assert COGNITION_AWAKE_SERVICE in bus.services
        assert COGNITION_VOICE_START_SERVICE in bus.services
        assert COGNITION_VOICE_CANCEL_SERVICE in bus.services
        assert COGNITION_VOICE_STATUS_SERVICE in bus.services
        assert Topics.USER_UTTERANCE in bus.subscriptions
        assert Topics.SITUATION_UPDATE in bus.subscriptions
        names = runtime.registry.names()
        assert "window.info" in names
        assert "screen.capture" in names
        assert "text.extract" in names
        assert "vision.understand" in names
        assert "perception.deep_understand_screen" not in names
    finally:
        runtime.local_model_control.close()
        runtime.context.close()
        memory.close()


def test_cloud_missing_api_key_skips_cloud_components(tmp_path, monkeypatch):
    monkeypatch.delenv("YUKI_CLOUD_API_KEY", raising=False)
    bus = FakeBus()
    pipeline = FakePipeline()
    runtime = CognitionAssembler(
        Config(
            cloud={"enabled": True},
            memory={"db_path": str(tmp_path / "mem.db")},
            persona={"snapshots_path": str(tmp_path / "persona.json")},
        ),
        bus,
        pipeline=pipeline,
    ).assemble()
    try:
        assert runtime.bridge is None
        assert runtime.thread_maintenance_scheduler is None
    finally:
        runtime.context.close()
        runtime.memory.close()


def test_cognition_assembler_uses_persistent_thread_store(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()
    db_path = tmp_path / "mem.db"
    config = Config(
        memory={"db_path": str(db_path)},
        thread={"segment_max_turns": 3, "episode_idle_s": 10},
        persona={"snapshots_path": str(tmp_path / "persona.json")},
    )
    runtime = CognitionAssembler(config, bus, pipeline=pipeline).assemble()

    try:
        assert isinstance(runtime.context._store, ThreadTurnStore)
        runtime.handle_chat_request({"text": "需要持久化"})
    finally:
        runtime.context.close()
        runtime.memory.close()

    reopened = ThreadTurnStore(db_path)
    try:
        assert any(turn["content"] == "需要持久化" for turn in reopened.items())
    finally:
        reopened.close()


def test_cognition_assembler_wires_vector_memory_from_config(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()

    runtime = CognitionAssembler(
        Config(
            memory={
                "db_path": str(tmp_path / "mem.db"),
                "vector_enabled": True,
                "embedding_dimension": 64,
            },
            persona={"snapshots_path": str(tmp_path / "persona.json")},
        ),
        bus,
        pipeline=pipeline,
    ).assemble()

    try:
        assert runtime.memory._vector_enabled is True
        assert runtime.memory._embedding_indexer._cache_manager is runtime.cache_manager
        memory_id = runtime.memory.write("preference", "assembler vector memory")
        assert runtime.memory._store.embeddings_count() == 1
        assert runtime.memory.query("asembler", top_k=1)[0]["id"] == memory_id
    finally:
        runtime.context.close()
        runtime.memory.close()


def test_cognition_assembler_builds_speech_buffer_from_config(tmp_path):
    bus = FakeBus()
    assembler = CognitionAssembler(
        Config(
            stt={
                "enabled": False,
                "device": "cuda:0",
                "vad": {
                    "model": "fsmn-local",
                    "vad_interval_ms": 200,
                    "end_silence_ms": 600,
                    "max_utterance_s": 3.0,
                },
            },
            persona={"snapshots_path": str(tmp_path / "persona.json")},
        ),
        bus,
    )

    speech_buffer = assembler._build_speech_buffer()

    assert speech_buffer._vad._model_id == "fsmn-local"
    assert speech_buffer._vad._device == "cuda:0"
    assert speech_buffer._vad._gate.disabled() is True
    assert speech_buffer._vad_interval_samples == 3200
    assert speech_buffer._end_silence_ms == 600
    assert speech_buffer._max_samples == 48000
