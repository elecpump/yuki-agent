from yuki.cognition.agent import CognitionAgent
from yuki.config import Config
from yuki.memory.manager import MemoryManager
from yuki.memory.service import MEMORY_SERVICES
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def reply(self, text, context=None):
        return f"reply:{text}"


class FakePipeline:
    def warmup_vlm(self):
        pass


def test_cognition_agent_wires_pipeline_responder_and_memory(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    assert Topics.AWAKE in bus.subscriptions
    assert Topics.SITUATION_UPDATE in bus.subscriptions
    assert Topics.USER_UTTERANCE in bus.subscriptions
    assert all(service in bus.services for service in MEMORY_SERVICES)
    agent.teardown()


def test_cognition_agent_health_includes_memory(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        l1=FakeL1(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    components = agent.health_components()
    assert "memory" in components
    status = components["memory"]()
    assert status.ok is True


def test_cognition_agent_memory_health_unhealthy_after_teardown(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        l1=FakeL1(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    agent.teardown()
    status = agent.health_components()["memory"]()
    assert status.ok is False


def test_cognition_agent_health_includes_brain(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        l1=FakeL1(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()  # hub 在 setup 中构建，必须先 setup 再查健康
    try:
        components = agent.health_components()
        assert "brain" in components
        assert components["brain"]().ok is True
    finally:
        agent.teardown()


def test_cognition_agent_registers_memory_functions_and_l2_health(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert "memory.query" in agent._registry.names()
        assert agent._bridge is None  # cloud 默认未启用
        components = agent.health_components()
        assert "l2" in components
        assert components["l2"]().ok is True  # 未启用视为正常
        assert components["l2"]().detail["installed"] is False
    finally:
        agent.teardown()
