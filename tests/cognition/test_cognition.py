from yuki.cognition.agent import CognitionAgent
from yuki.cognition.brain.hub import (
    COGNITION_AWAKE_SERVICE,
    COGNITION_CHAT_SERVICE,
    SOUL_GET_SERVICE,
)
from yuki.cognition.vlm import VisualUnderstander
from yuki.config import Config
from yuki.functions.service import FUNCTIONS_CALL_SERVICE
from yuki.memory.manager import MemoryManager
from yuki.memory.provenance import AUTOMATIC_STRENGTHENER
from yuki.memory.service import MEMORY_SERVICES
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def reply(self, text, context=None):
        return f"reply:{text}"


class FakePipeline:
    def __init__(self):
        self.awake_payloads = []

    def warmup_vlm(self):
        pass

    def on_awake(self, topic, payload):
        self.awake_payloads.append((topic, payload))


class FakeVlm:
    def __init__(self, loaded=False):
        self._loaded = loaded


def test_cognition_agent_wires_pipeline_responder_and_memory(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    assert Topics.SITUATION_UPDATE in bus.subscriptions
    assert Topics.USER_UTTERANCE in bus.subscriptions
    assert all(service in bus.services for service in MEMORY_SERVICES)
    assert FUNCTIONS_CALL_SERVICE in bus.services
    assert bus.request(
        FUNCTIONS_CALL_SERVICE, {"name": "system.ping", "arguments": "{}"}
    )["ok"] is True
    assert COGNITION_AWAKE_SERVICE in bus.services
    assert COGNITION_CHAT_SERVICE in bus.services
    assert SOUL_GET_SERVICE in bus.services
    agent.teardown()


class RecordingPauseBus(FakeBus):
    def __init__(self):
        super().__init__()
        self.pause_calls = 0

    def pause_subscriptions(self):
        self.pause_calls += 1


def test_agent_teardown_pauses_subscriptions_before_flush(tmp_path):
    bus = RecordingPauseBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    assert bus.pause_calls == 0
    agent.teardown()
    # spec §8：先停止接收新请求，再 scheduler bounded flush。
    assert bus.pause_calls == 1
    assert agent._thread_maintenance_scheduler is None


def test_cognition_agent_awake_service_coordinates_pipeline_and_brain(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=pipeline,
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        result = bus.request(COGNITION_AWAKE_SERVICE, {"source": "hotkey", "ts": 0.0})
        assert result["text"] == ""
        assert result["spoke"] is False
        assert pipeline.awake_payloads == [
            (Topics.AWAKE, {"source": "hotkey", "ts": 0.0})
        ]
        assert not any(topic == Topics.REPLY for topic, _ in bus.published)
    finally:
        agent.teardown()


def test_cognition_agent_chat_service_returns_without_publishing_reply(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        result = bus.request(
            COGNITION_CHAT_SERVICE,
            {"text": "你好", "session_id": "ui", "task_id": "t1"},
        )
        assert "text" in result
        assert "spoke" in result
        assert not any(topic == Topics.REPLY for topic, _ in bus.published)
    finally:
        agent.teardown()


def test_cognition_agent_soul_get_service_returns_soul(tmp_path):
    bus = FakeBus()
    soul_path = tmp_path / "soul.json"
    agent = CognitionAgent(
        Config(
            soul={"path": str(soul_path), "tuner_state_path": str(tmp_path / "tuner.json")},
            persona={"snapshots_path": str(tmp_path / "persona.json")},
        ),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        result = bus.request(SOUL_GET_SERVICE, {})
        assert result["soul"]["persona_name"] == "yuki"
        assert "core_values" in result["soul"]
    finally:
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


def test_cognition_agent_health_includes_model_registry(tmp_path):
    bus = FakeBus()

    class StubRemoteRegistry:
        def get_overall_status(self):
            return {
                "status": "degraded",
                "healthy": True,
                "models": {"vlm": {}, "stt": {}},
            }

    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
        model_registry=StubRemoteRegistry(),
    )
    agent.setup()
    try:
        status = agent.health_components()["models"]()
        assert status.ok is True
        assert status.detail["status"] == "degraded"
        assert status.detail["healthy"] is True
        assert set(status.detail["models"]) == {"vlm", "stt"}
    finally:
        agent.teardown()


def test_cognition_agent_teardown_shuts_down_model_registry(tmp_path):
    bus = FakeBus()
    calls = []

    class StubRemoteRegistry:
        def shutdown(self):
            calls.append("shutdown")

    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
        model_registry=StubRemoteRegistry(),
    )
    agent.setup()

    agent.teardown()

    assert calls == ["shutdown"]
    assert agent._model_registry is None


def test_cognition_agent_teardown_continues_after_model_shutdown_error(tmp_path):
    bus = FakeBus()

    class BoomRegistry:
        def shutdown(self):
            raise RuntimeError("boom")

    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
        model_registry=BoomRegistry(),
    )
    agent.setup()

    agent.teardown()

    assert agent._model_registry is None
    assert agent._context is None
    assert agent._memory is None


def test_cognition_agent_builds_cooldown_calculator(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._hub._cooldown is not None
    finally:
        agent.teardown()


def test_cognition_agent_builds_context_and_projector(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
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
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    agent.teardown()
    # teardown 已调用 context.close()（写入快照到 data/context_snapshot.json 或按 config）
    # 本测试仅验证不抛异常


def test_cognition_agent_assembles_persona(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert agent._persona_store is not None
        assert agent._hub._periodic == [agent._persona_refresh]
        assert agent._soul_reflection_scheduler is None
    finally:
        agent.teardown()


def test_cognition_agent_registers_soul_update_and_refreshes_persona(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(
            persona={"snapshots_path": str(tmp_path / "persona.json")},
            soul={
                "path": str(tmp_path / "soul.json"),
                "snapshots_dir": str(tmp_path / "soul_snapshots"),
            },
        ),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        assert "soul.update" in agent._registry.names()
        result = agent._registry.dispatch({
            "name": "soul.update",
            "arguments": {
                "traits": {"warmth": 0.9},
                "description": "下一次请求使用的新描述",
            },
        })
        assert result["ok"] is True
        active = agent._persona_store.active()
        assert active is not None
        assert active.persona_prompt.startswith("下一次请求使用的新描述")
        assert "表达温暖" in active.persona_prompt
    finally:
        agent.teardown()


def test_cognition_agent_starts_and_stops_soul_reflection_scheduler(
    tmp_path,
    monkeypatch,
):
    class FakeCloudClient:
        def __init__(self, **kwargs):
            pass

        def chat(self, messages, tools=None, timeout_s=None):
            return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr("yuki.cognition.assembly.CloudClient", FakeCloudClient)
    monkeypatch.setenv("YUKI_CLOUD_API_KEY", "test-key")
    agent = CognitionAgent(
        Config(
            cloud={"enabled": True, "base_url": "http://x", "model": "m"},
            persona={"snapshots_path": str(tmp_path / "persona.json")},
            soul={
                "path": str(tmp_path / "soul.json"),
                "snapshots_dir": str(tmp_path / "soul_snapshots"),
            },
        ),
        bus=FakeBus(),
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )

    agent.setup()
    scheduler = agent._soul_reflection_scheduler
    assert scheduler is not None
    assert scheduler._timer_thread.is_alive()

    agent.teardown()
    assert agent._soul_reflection_scheduler is None
    assert scheduler._timer_thread.is_alive() is False


def test_cognition_agent_starts_and_stops_thread_maintenance_scheduler(
    tmp_path,
    monkeypatch,
):
    class FakeCloudClient:
        def __init__(self, **kwargs):
            pass

        def chat(self, messages, tools=None, timeout_s=None, **kwargs):
            return {"choices": [{"message": {"content": "摘要"}}]}

    monkeypatch.setattr("yuki.cognition.assembly.CloudClient", FakeCloudClient)
    monkeypatch.setenv("YUKI_CLOUD_API_KEY", "test-key")
    agent = CognitionAgent(
        Config(
            cloud={"enabled": True, "base_url": "http://x", "model": "m"},
            memory={"db_path": str(tmp_path / "mem.db")},
            persona={"snapshots_path": str(tmp_path / "persona.json")},
            soul={
                "path": str(tmp_path / "soul.json"),
                "snapshots_dir": str(tmp_path / "soul_snapshots"),
            },
            thread={"maintenance_tick_s": 1.0},
        ),
        bus=FakeBus(),
        pipeline=FakePipeline(),
    )

    agent.setup()
    scheduler = agent._thread_maintenance_scheduler
    assert scheduler is not None
    assert scheduler._thread.is_alive()

    agent.teardown()
    assert agent._thread_maintenance_scheduler is None
    assert scheduler._thread.is_alive() is False


def test_agent_wires_refine_when_enabled(tmp_path, monkeypatch):
    class FakeCloudClient:
        def __init__(self, **kwargs):
            self.calls = []

        def chat(self, messages, tools=None, timeout_s=None):
            self.calls.append(messages)
            return {"choices": [{"message": {"content": "精修: " + messages[-1]["content"]}}]}

    fake = FakeCloudClient()
    monkeypatch.setattr("yuki.cognition.assembly.CloudClient", lambda **kw: fake)
    monkeypatch.setenv("YUKI_CLOUD_API_KEY", "test-key")

    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json"),
                        "enable_llm_refine": True},
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


def test_cognition_agent_vlm_health_degrades_while_loading(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()
    pipeline._vlm = FakeVlm(loaded=False)
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=pipeline,
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )

    status = agent.health_components()["vlm"]()

    assert status.ok is True
    assert status.detail == {"loaded": False, "degraded": True, "reason": "loading"}


def test_cognition_agent_vlm_health_degrades_when_unavailable(tmp_path):
    bus = FakeBus()
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )

    status = agent.health_components()["vlm"]()

    assert status.ok is True
    assert status.detail == {"loaded": False, "degraded": True, "reason": "no_vlm"}


def test_cognition_agent_vlm_health_degraded_via_gate(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()
    pipeline._vlm = VisualUnderstander(enabled=False)
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=pipeline,
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )

    status = agent.health_components()["vlm"]()

    assert status.ok is True
    assert status.detail["degraded"] is True
    assert status.detail["enabled"] is False


def test_persona_refresh_only_sees_automatic_stable_preferences(tmp_path, monkeypatch):
    class FakeCloudClient:
        def __init__(self, **kwargs):
            self.calls = []

        def chat(self, messages, tools=None, timeout_s=None):
            self.calls.append(messages)
            return {"choices": [{"message": {"content": messages[-1]["content"]}}]}

    fake = FakeCloudClient()
    monkeypatch.setattr("yuki.cognition.assembly.CloudClient", lambda **kw: fake)
    monkeypatch.setenv("YUKI_CLOUD_API_KEY", "test-key")

    memory = MemoryManager(MemoryStore(tmp_path / "mem.db"))
    memory.write("preference", "普通公开偏好", sensitivity=0)
    manual_id = memory.write("preference", "人工强化偏好", sensitivity=0)
    memory.strengthen(manual_id)
    stable_id = memory.write(
        "preference",
        "自动成熟偏好",
        sensitivity=0,
        metadata={"strengthened_by": AUTOMATIC_STRENGTHENER},
    )
    memory.strengthen(stable_id)
    memory.write("preference", "私密偏好", sensitivity=1)
    memory.write("preference", "高敏偏好", sensitivity=2)

    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json"),
                        "enable_llm_refine": True},
               cloud={"enabled": True, "base_url": "http://x", "model": "m"}),
        bus=FakeBus(),
        pipeline=FakePipeline(),
        memory=memory,
    )
    agent.setup()
    try:
        refine_prompt = fake.calls[0][-1]["content"]
        assert "自动成熟偏好" in refine_prompt
        assert "普通公开偏好" not in refine_prompt
        assert "人工强化偏好" not in refine_prompt
        assert "私密偏好" not in refine_prompt
        assert "高敏偏好" not in refine_prompt
    finally:
        agent.teardown()


def test_persona_refresh_does_not_include_direct_user_preference(tmp_path, monkeypatch):
    class FakeCloudClient:
        def __init__(self, **kwargs):
            self.calls = []

        def chat(self, messages, tools=None, timeout_s=None):
            self.calls.append(messages)
            return {"choices": [{"message": {"content": messages[-1]["content"]}}]}

    fake = FakeCloudClient()
    monkeypatch.setattr("yuki.cognition.assembly.CloudClient", lambda **kw: fake)
    monkeypatch.setenv("YUKI_CLOUD_API_KEY", "test-key")

    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json"),
                        "enable_llm_refine": True},
               cloud={"enabled": True, "base_url": "http://x", "model": "m"}),
        bus=FakeBus(),
        pipeline=FakePipeline(),
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    agent.setup()
    try:
        agent._memory.write(
            "preference",
            "请回复简短一些",
            source="user",
            sensitivity=0,
        )
        agent._persona_refresh()
        refine_prompt = fake.calls[-1][-1]["content"]
        assert "请回复简短一些" not in refine_prompt
    finally:
        agent.teardown()
