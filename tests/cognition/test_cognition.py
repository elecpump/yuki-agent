from yuki.cognition.agent import CognitionAgent
from yuki.cognition.brain.classifier import Intent
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
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
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
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
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
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
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
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
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
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
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


def test_cognition_agent_builds_tuner(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._hub._tuner is not None
        assert agent._hub._policy is agent._hub._tuner._policy
    finally:
        agent.teardown()


def test_cognition_agent_builds_context_and_projector(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")},
               context={"snapshot_path": str(tmp_path / "snap.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._hub._context_wrapper is not None
        assert agent._hub._projector is not None
    finally:
        agent.teardown()


def test_cognition_agent_teardown_closes_context(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")},
               context={"snapshot_path": str(tmp_path / "snap.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    agent.teardown()
    # teardown 已调用 context.close()（写入快照到 data/context_snapshot.json 或按 config）
    # 本测试仅验证不抛异常


def test_cognition_agent_builds_sedimenter(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")},
               context={"snapshot_path": str(tmp_path / "snap.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._hub._sedimenter is not None
    finally:
        agent.teardown()


def test_cognition_agent_assembles_persona(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")},
               context={"snapshot_path": str(tmp_path / "ctx.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._persona_store is not None
        assert agent._hub._sedimenter._on_sedimented is not None
    finally:
        agent.teardown()


def test_agent_wires_refine_when_enabled(tmp_path, monkeypatch):
    class FakeCloudClient:
        def __init__(self, **kwargs):
            self.calls = []

        def chat(self, messages, tools=None, timeout_s=None):
            self.calls.append(messages)
            return {"choices": [{"message": {"content": "精修: " + messages[-1]["content"]}}]}

    fake = FakeCloudClient()
    monkeypatch.setattr("yuki.cognition.agent.CloudClient", lambda **kw: fake)

    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json"),
                        "enable_llm_refine": True},
               context={"snapshot_path": str(tmp_path / "ctx.json")},
               cloud={"enabled": True, "base_url": "http://x", "model": "m"}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._bridge is not None
        assert agent._persona_refresh is not None
        assert callable(agent._bridge.refine_persona)
        assert fake.calls  # setup 的 session-init refresh 已触发精修
    finally:
        agent.teardown()


def test_persona_loop_sedimentation_creates_version(tmp_path):
    config = Config(persona={"snapshots_path": str(tmp_path / "persona.json"),
                             "enable_llm_refine": False},
                    context={"snapshot_path": str(tmp_path / "ctx.json")},
                    cloud={"enabled": True, "base_url": "http://x", "model": "m"})
    bus = FakeBus()
    agent = CognitionAgent(
        config,
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._bridge is not None
        assert len(agent._persona_store.list_versions()) == 1  # session-init refresh 创建 v1
        sedimenter = agent._hub._sedimenter
        for _ in range(3):
            sedimenter.on_user_utterance("太吵了", Intent.UNKNOWN)
        assert len(agent._persona_store.list_versions()) == 2  # 沉淀回调 → 新版本
    finally:
        agent.teardown()
