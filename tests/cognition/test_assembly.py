from yuki.cognition.assembly import CognitionAssembler
from yuki.cognition.brain.hub import COGNITION_AWAKE_SERVICE
from yuki.config import Config
from yuki.cognition.model_service import MODEL_SERVICES
from yuki.functions.service import FUNCTIONS_CALL_SERVICE
from yuki.memory.manager import MemoryManager
from yuki.memory.service import MEMORY_SERVICES
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakePipeline:
    def __init__(self):
        self.warmups = 0
        self.stt_warmups = 0

    def warmup_vlm(self):
        self.warmups += 1

    def warmup_stt(self):
        self.stt_warmups += 1


class FakeModelAdapter:
    def __init__(self):
        self.loaded = False
        self.unloaded = 0

    def load(self):
        self.loaded = True

    def unload(self):
        self.loaded = False
        self.unloaded += 1

    def health(self):
        return {"loaded": self.loaded, "degraded": False}


def test_cognition_assembler_builds_runtime_and_registers_services(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()
    memory = MemoryManager(MemoryStore(tmp_path / "mem.db"))

    runtime = CognitionAssembler(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus,
        pipeline=pipeline,
        memory=memory,
    ).assemble()

    try:
        assert runtime.pipeline is pipeline
        assert runtime.memory is memory
        assert runtime.model_registry is not None
        assert runtime.hub is not None
        assert runtime.context is not None
        assert runtime.persona_store is not None
        assert runtime.persona_refresh is not None
        assert pipeline.warmups == 1
        assert pipeline.stt_warmups == 1
        assert all(service in bus.services for service in MEMORY_SERVICES)
        assert all(service in bus.services for service in MODEL_SERVICES)
        assert FUNCTIONS_CALL_SERVICE in bus.services
        assert COGNITION_AWAKE_SERVICE in bus.services
        assert Topics.USER_UTTERANCE in bus.subscriptions
        assert Topics.SITUATION_UPDATE in bus.subscriptions
        names = runtime.registry.names()
        assert "window.info" in names
        assert "screen.capture" in names
        assert "text.extract" in names
        assert "vision.understand" in names
        assert "perception.deep_understand_screen" not in names
    finally:
        runtime.context.close()
        memory.close()


def test_cognition_assembler_registers_pipeline_models(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()
    pipeline._vlm = FakeModelAdapter()
    pipeline._stt = FakeModelAdapter()
    memory = MemoryManager(MemoryStore(tmp_path / "mem.db"))

    runtime = CognitionAssembler(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus,
        pipeline=pipeline,
        memory=memory,
    ).assemble()

    try:
        health = runtime.model_registry.get_all_models_health()
        assert set(health) == {"vlm", "stt"}
        assert health["vlm"]["loaded"] is False
    finally:
        runtime.context.close()
        memory.close()


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
        memory_id = runtime.memory.write("preference", "assembler vector memory")
        assert runtime.memory._store.embeddings_count() == 1
        assert runtime.memory.query("asembler", top_k=1)[0]["id"] == memory_id
    finally:
        runtime.context.close()
        runtime.memory.close()


def test_cognition_assembler_builds_vlm_from_config(tmp_path):
    bus = FakeBus()
    assembler = CognitionAssembler(
        Config(
            vlm={"model": "Qwen/Qwen3-VL-8B-Instruct", "cache_dir": "D:/hf"},
            persona={"snapshots_path": str(tmp_path / "persona.json")},
        ),
        bus,
    )
    vlm = assembler._build_vlm()
    assert vlm._model_id == "Qwen/Qwen3-VL-8B-Instruct"
    assert vlm._cache_dir == "D:/hf"


def test_cognition_assembler_vlm_disabled_skips_load(tmp_path):
    bus = FakeBus()
    assembler = CognitionAssembler(
        Config(
            vlm={"enabled": False},
            persona={"snapshots_path": str(tmp_path / "persona.json")},
        ),
        bus,
    )
    vlm = assembler._build_vlm()
    assert vlm._gate.disabled() is True
    assert vlm._gate.can_load() is False


def test_cognition_assembler_builds_stt_from_config(tmp_path):
    bus = FakeBus()
    assembler = CognitionAssembler(
        Config(
            stt={
                "enabled": False,
                "model": "hub/sense",
                "model_dir": "D:/models/sense",
                "device": "cuda:0",
                "language": "zn",
                "use_itn": False,
                "retry_window_s": 12.0,
            },
            persona={"snapshots_path": str(tmp_path / "persona.json")},
        ),
        bus,
    )

    stt = assembler._build_stt()

    assert stt._model_id == "hub/sense"
    assert stt._model_dir == "D:/models/sense"
    assert stt._device == "cuda:0"
    assert stt._language == "zn"
    assert stt._use_itn is False
    assert stt._gate.disabled() is True


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
