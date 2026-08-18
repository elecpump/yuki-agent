from yuki.cognition.assembly import CognitionAssembler
from yuki.cognition.brain.hub import COGNITION_AWAKE_SERVICE
from yuki.config import Config
from yuki.functions.service import FUNCTIONS_CALL_SERVICE
from yuki.memory.manager import MemoryManager
from yuki.memory.service import MEMORY_SERVICES
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakePipeline:
    def __init__(self):
        self.warmups = 0

    def warmup_vlm(self):
        self.warmups += 1


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
        assert runtime.hub is not None
        assert runtime.context is not None
        assert runtime.persona_store is not None
        assert runtime.persona_refresh is not None
        assert pipeline.warmups == 1
        assert all(service in bus.services for service in MEMORY_SERVICES)
        assert FUNCTIONS_CALL_SERVICE in bus.services
        assert COGNITION_AWAKE_SERVICE in bus.services
        assert Topics.USER_UTTERANCE in bus.subscriptions
        assert Topics.SITUATION_UPDATE in bus.subscriptions
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
