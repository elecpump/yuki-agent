from pydantic import BaseModel

from yuki.cognition.brain.classifier import Emotion, Intent
from yuki.cognition.brain.hub import (
    COGNITION_AWAKE_SERVICE,
    CRISIS_FALLBACK_REPLY,
    L2_UNAVAILABLE_NOTICE,
    DecisionHub,
    build_brain,
)
from yuki.cognition.brain.local.router import LocalRoute, RouterDecision
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.context.store import ShortTermTurnStore
from yuki.cognition.context.working import WorkingContext
from yuki.cognition.l2.client import CloudError
from yuki.config import Config
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeBridge:
    def __init__(self, reply=None, error=None):
        self._reply = reply
        self._error = error
        self.calls = []

    def generate(self, utterance, snapshot=None, memory=None):
        self.calls.append((utterance, snapshot, memory))
        if self._error:
            raise self._error
        return self._reply


class FakeRouter:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def route(self, text, *, snapshot=None, situation=None):
        self.calls.append((text, snapshot, situation))
        return self.decision


class FakeComposer:
    def __init__(self, reply="local reply", error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def generate(self, utterance, snapshot=None, memory=None):
        self.calls.append((utterance, snapshot, memory))
        if self.error:
            raise self.error
        return self.reply


class FakeScreen:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def inspect(self, question):
        self.calls.append(question)
        return self.result


def _reply_text(bus) -> str | None:
    for topic, payload in reversed(bus.published):
        if topic == Topics.REPLY:
            return payload["text"]
    return None


def test_awake_is_silent(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(bus, memory=memory)
    result = hub.handle_awake_request({"source": "hotkey", "ts": 0.0})
    assert result["spoke"] is False
    assert result["text"] == ""
    assert _reply_text(bus) is None
    memory.close()


def test_local_disabled_utterance_goes_cloud_notice(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(bus, memory=memory, local_enabled=False)
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好"})
    assert _reply_text(bus) == L2_UNAVAILABLE_NOTICE
    memory.close()


def test_local_disabled_utterance_uses_cloud_when_available(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    bridge = FakeBridge(reply="cloud reply")
    hub = DecisionHub(bus, memory=memory, bridge=bridge, local_enabled=False)
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话"})
    assert _reply_text(bus) == "cloud reply"
    assert bridge.calls[0][0] == "讲个笑话"
    memory.close()


def test_crisis_never_calls_router_and_has_static_fallback(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    router = FakeRouter(RouterDecision(LocalRoute.CHAT_LOCAL, 1.0))
    hub = DecisionHub(bus, memory=memory, local_router=router, local_enabled=True)
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我不想活了"})
    assert router.calls == []
    assert _reply_text(bus) == CRISIS_FALLBACK_REPLY
    memory.close()


def test_crisis_can_use_cloud_before_static_fallback(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    bridge = FakeBridge(reply="crisis cloud")
    hub = DecisionHub(bus, memory=memory, bridge=bridge, local_enabled=True)
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我想死"})
    assert _reply_text(bus) == "crisis cloud"
    memory.close()


def test_chat_local_route_uses_composer(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    decision = RouterDecision(
        LocalRoute.CHAT_LOCAL,
        0.9,
        intent=Intent.CHIT_CHAT,
        emotion=Emotion.NEUTRAL,
        trusted_metadata=True,
    )
    composer = FakeComposer(reply="local hi")
    hub = DecisionHub(
        bus,
        memory=memory,
        local_router=FakeRouter(decision),
        local_composer=composer,
        local_enabled=True,
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好"})
    assert _reply_text(bus) == "local hi"
    assert composer.calls[0][0] == "你好"
    memory.close()


def test_chat_local_failure_falls_to_cloud_notice(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    decision = RouterDecision(LocalRoute.CHAT_LOCAL, 0.9, trusted_metadata=True)
    hub = DecisionHub(
        bus,
        memory=memory,
        local_router=FakeRouter(decision),
        local_composer=FakeComposer(reply=""),
        local_enabled=True,
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好"})
    assert _reply_text(bus) == L2_UNAVAILABLE_NOTICE
    memory.close()


class EchoParams(BaseModel):
    text: str


def test_tool_local_dispatches_allowlisted_tool(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    registry = FunctionRegistry()

    @registry.tool("local.echo", description="echo", params=EchoParams)
    def echo(params):
        return {"reply": params.text}

    decision = RouterDecision(
        LocalRoute.TOOL_LOCAL,
        0.9,
        intent=Intent.SYSTEM,
        emotion=Emotion.NEUTRAL,
        tool_call={"name": "local.echo", "arguments": {"text": "done"}},
        trusted_metadata=True,
    )
    hub = DecisionHub(
        bus,
        memory=memory,
        registry=registry,
        local_router=FakeRouter(decision),
        local_enabled=True,
        local_tool_allowlist=["local.echo"],
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "do it"})
    assert _reply_text(bus) == "done"
    memory.close()


def test_tool_local_invalid_falls_to_cloud(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    decision = RouterDecision(
        LocalRoute.TOOL_LOCAL,
        0.9,
        tool_call={"name": "local.echo", "arguments": {}},
        trusted_metadata=True,
    )
    hub = DecisionHub(
        bus,
        memory=memory,
        local_router=FakeRouter(decision),
        local_enabled=True,
        local_tool_allowlist=[],
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "do it"})
    assert _reply_text(bus) == L2_UNAVAILABLE_NOTICE
    memory.close()


def test_vision_can_answer_uses_local_composer_with_screen_context(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    decision = RouterDecision(LocalRoute.VISION, 0.9, trusted_metadata=True)
    composer = FakeComposer(reply="screen answer")
    screen = FakeScreen({"topic": "论文", "summary": "摘要", "key_points": [], "can_answer": True})
    hub = DecisionHub(
        bus,
        memory=memory,
        local_router=FakeRouter(decision),
        local_composer=composer,
        vision_screen=screen,
        local_enabled=True,
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "这页讲什么"})
    assert _reply_text(bus) == "screen answer"
    assert composer.calls[0][1].situation["topic"] == "论文"
    memory.close()


def test_vision_cannot_answer_falls_to_cloud(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    decision = RouterDecision(LocalRoute.VISION, 0.9, trusted_metadata=True)
    bridge = FakeBridge(reply="cloud vision")
    screen = FakeScreen({"topic": "论文", "summary": "摘要", "key_points": [], "can_answer": False})
    hub = DecisionHub(
        bus,
        memory=memory,
        bridge=bridge,
        local_router=FakeRouter(decision),
        vision_screen=screen,
        local_enabled=True,
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "作者最近论文是什么"})
    assert _reply_text(bus) == "cloud vision"
    assert bridge.calls[0][1].situation["topic"] == "论文"
    memory.close()


def test_decision_trace_includes_route(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    records = []
    hub = DecisionHub(bus, memory=memory, local_enabled=False)
    hub._trace_logger = type("L", (), {"info": lambda self, evt, **kw: records.append(kw)})()
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好"})
    assert records[0]["route"] == "cloud"
    assert "tier" not in records[0]
    memory.close()


class FakeContext:
    def __init__(self):
        self.users = []
        self.agents = []
        self.situations = []
        self.snap = ContextSnapshot()

    def add_user(self, text):
        self.users.append(text)

    def add_agent(self, text):
        self.agents.append(text)

    def update_situation(self, payload):
        self.situations.append(payload)


class FakeProjector:
    def build(self, working):
        return working.snap


def test_hub_writes_context_without_double_write(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    ctx = FakeContext()
    hub = DecisionHub(
        bus,
        memory=memory,
        context=ctx,
        projector=FakeProjector(),
        local_enabled=False,
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好"})
    assert ctx.users == ["你好"]
    assert ctx.agents == [L2_UNAVAILABLE_NOTICE]
    memory.close()


class FakeSedimenter:
    def __init__(self):
        self.utterances = []
        self.topics = []

    def on_user_utterance(self, text, intent):
        self.utterances.append((text, intent))

    def on_engagement(self, topic):
        self.topics.append(topic)


def test_sedimenter_only_receives_trusted_router_metadata(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    sed = FakeSedimenter()
    decision = RouterDecision(
        LocalRoute.CHAT_LOCAL,
        0.9,
        intent=Intent.SYSTEM,
        trusted_metadata=True,
    )
    hub = DecisionHub(
        bus,
        memory=memory,
        sedimenter=sed,
        local_router=FakeRouter(decision),
        local_composer=FakeComposer(),
        local_enabled=True,
    )
    hub._context = {"topic": "量子计算"}
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "其实我不喜欢主动聊天"})
    assert sed.utterances == [("其实我不喜欢主动聊天", Intent.SYSTEM)]
    assert sed.topics == ["量子计算"]
    memory.close()


def test_sedimenter_skips_router_failure_metadata(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    sed = FakeSedimenter()
    hub = DecisionHub(
        bus,
        memory=memory,
        sedimenter=sed,
        local_router=FakeRouter(RouterDecision.cloud("router_failed")),
        local_enabled=True,
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "其实我不喜欢主动聊天"})
    assert sed.utterances == []
    memory.close()


def test_situation_proactive_and_cooldown(tmp_path, monkeypatch):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(bus, policy=DecisionPolicy(120.0), memory=memory)
    monkeypatch.setattr("time.time", lambda: 0.0)
    hub.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "ts": 0.0})
    first = _reply_text(bus)
    monkeypatch.setattr("time.time", lambda: 60.0)
    before = len(bus.published)
    hub.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "ts": 60.0})
    assert "量子计算" in first
    assert len(bus.published) == before
    memory.close()


def test_select_situation_prefers_deep_for_same_source(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(bus, memory=memory)
    hub.on_situation_update(
        Topics.SITUATION_UPDATE,
        {"layer": "fast", "source_id": "article-1", "topic": "fast"},
    )
    hub.on_situation_update(
        Topics.SITUATION_UPDATE,
        {"layer": "deep", "source_id": "article-1", "topic": "deep"},
    )
    assert hub._context["topic"] == "deep"
    memory.close()


def test_single_turn_writer_no_double_write(tmp_path):
    bus = FakeBus()
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    working = WorkingContext(ShortTermTurnStore(manager), snapshot_path=None)
    hub = DecisionHub(bus, memory=manager, context=working, local_enabled=False)
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好"})
    assert working.turn_count() == 2
    contents = [it["content"] for it in working.items()]
    assert "你好" in contents
    assert L2_UNAVAILABLE_NOTICE in contents
    manager.close()


def test_build_brain_subscribes_and_configures(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    build_brain(
        bus,
        memory=memory,
        registry=FunctionRegistry(),
        config=Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
    )
    assert COGNITION_AWAKE_SERVICE in bus.services
    assert Topics.AWAKE not in bus.subscriptions
    assert Topics.USER_UTTERANCE in bus.subscriptions
    assert Topics.SITUATION_UPDATE in bus.subscriptions
    memory.close()


def test_cloud_failure_returns_notice(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(
        bus,
        memory=memory,
        bridge=FakeBridge(error=CloudError("boom")),
        local_enabled=False,
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话"})
    assert _reply_text(bus) == L2_UNAVAILABLE_NOTICE
    memory.close()
