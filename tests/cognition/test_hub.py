import math
import sqlite3
import threading
import time

import pytest

from yuki.cognition.brain.hub import (
    COGNITION_AWAKE_SERVICE,
    CRISIS_FALLBACK_REPLY,
    L2_UNAVAILABLE_NOTICE,
    DecisionHub,
    build_brain,
)
from yuki.cognition.brain.local.router import GateRoute, RouterDecision
from yuki.cognition.context.snapshot import ContextProjector, ContextSnapshot
from yuki.cognition.context.store import ShortTermTurnStore, ThreadTurnStore
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


class FakeLoop:
    def __init__(self, result=None, transition=None, check_interrupt=False):
        self.calls = []
        self.result = result or {
            "text": "cloud reply",
            "steps": 1,
            "interrupted": False,
            "failed": False,
        }
        self.transition = transition
        self.check_interrupt = check_interrupt

    def run(
        self,
        utterance,
        context=None,
        memory=None,
        *,
        crisis=False,
        on_transition=None,
        interrupt_check=None,
    ):
        self.calls.append({
            "utterance": utterance,
            "crisis": crisis,
            "on_transition": on_transition,
            "interrupt_check": interrupt_check,
        })
        if self.check_interrupt and interrupt_check is not None and interrupt_check():
            return {"text": "", "steps": 0, "interrupted": True, "failed": False}
        if self.transition is not None and on_transition is not None:
            on_transition(self.transition)
        return dict(self.result)


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
    reply = next(payload for topic, payload in bus.published if topic == Topics.REPLY)
    assert reply["emotion"] == "neutral"
    memory.close()


def test_chat_request_does_not_publish_reply(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(bus, memory=memory, local_enabled=False)

    result = hub.handle_chat_request({"text": "你好", "session_id": "ui"})

    assert result["text"] == L2_UNAVAILABLE_NOTICE
    assert result["spoke"] is True
    assert result["emotion"] == "neutral"
    assert _reply_text(bus) is None
    memory.close()


def test_chat_request_is_not_interrupted_by_voice_probe(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    loop = FakeLoop(check_interrupt=True)
    hub = DecisionHub(bus, memory=memory, loop=loop, local_enabled=False)
    hub.on_user_utterance_probe(
        Topics.USER_UTTERANCE,
        {"text": "语音输入", "ts": time.time() + 1.0},
    )

    result = hub.handle_chat_request({"text": "桌面聊天请求"})

    assert loop.calls[0]["interrupt_check"] is None
    assert result["text"] == "cloud reply"
    assert _reply_text(bus) is None
    memory.close()


def test_non_finite_voice_probe_is_ignored_instead_of_poisoning_future_loops(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    loop = FakeLoop(check_interrupt=True)
    hub = DecisionHub(bus, memory=memory, loop=loop, local_enabled=False)
    hub.on_user_utterance_probe(
        Topics.USER_UTTERANCE,
        {"text": "invalid timestamp", "ts": float("inf")},
    )

    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "ts": time.time()})

    assert _reply_text(bus) == "cloud reply"
    assert math.isfinite(hub._pending_input_ts)
    memory.close()


def test_interrupted_voice_loop_cancels_published_transition(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    loop = FakeLoop(
        transition="让我看看",
        result={"text": "", "steps": 1, "interrupted": True, "failed": False},
    )
    hub = DecisionHub(bus, memory=memory, loop=loop, local_enabled=False)

    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "查一下", "ts": 1.0})

    replies = [payload for topic, payload in bus.published if topic == Topics.REPLY]
    assert [payload["kind"] for payload in replies] == ["transition", "cancel"]
    assert replies[0]["reply_id"] == replies[1]["reply_id"]
    memory.close()


def test_crisis_loop_uses_crisis_mode_and_interrupt_falls_back(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    loop = FakeLoop(
        result={"text": "", "steps": 1, "interrupted": True, "failed": False},
    )
    hub = DecisionHub(bus, memory=memory, loop=loop, local_enabled=False)

    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我想死", "ts": 1.0})

    assert loop.calls[0]["crisis"] is True
    replies = [payload for topic, payload in bus.published if topic == Topics.REPLY]
    assert [payload["kind"] for payload in replies] == ["final"]
    assert replies[0]["text"] == CRISIS_FALLBACK_REPLY
    assert replies[0]["emotion"] == "sadness"
    memory.close()


def test_periodic_callback_does_not_drop_trigger_while_running(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    fired: list[int] = []
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    def callback():
        fired.append(1)
        if len(fired) == 1:
            first_started.set()
            assert release_first.wait(1.0)
        if len(fired) == 2:
            second_finished.set()

    hub = DecisionHub(
        bus,
        memory=memory,
        loop=FakeLoop(),
        local_enabled=False,
        periodic=[callback],
        periodic_interval=2,
    )
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "一", "ts": 1.0})
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "二", "ts": 2.0})
    assert first_started.wait(1.0)
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "三", "ts": 3.0})
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "四", "ts": 4.0})
    release_first.set()

    assert second_finished.wait(1.0)
    assert len(fired) == 2
    memory.close()


def test_utterance_observer_runs_after_context_is_updated(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    observed = []
    context = WorkingContext(ShortTermTurnStore(memory))
    hub = DecisionHub(
        bus,
        memory=memory,
        loop=FakeLoop(),
        local_enabled=False,
        context=context,
        projector=ContextProjector(),
        utterance_observers=[lambda text: observed.append((text, context.turn_count()))],
    )

    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "ts": 1.0})

    assert observed == [("你好", 2)]
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


def test_successful_cloud_reply_uses_input_emotion(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(
        bus,
        memory=memory,
        bridge=FakeBridge(reply="我听到了"),
        local_enabled=False,
    )

    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "气死我了"})

    reply = next(payload for topic, payload in bus.published if topic == Topics.REPLY)
    assert reply["emotion"] == "anger"
    memory.close()


def test_cloud_failure_notice_is_always_neutral(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(
        bus,
        memory=memory,
        bridge=FakeBridge(error=CloudError("boom")),
        local_enabled=False,
    )

    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "气死我了"})

    reply = next(payload for topic, payload in bus.published if topic == Topics.REPLY)
    assert reply["text"] == L2_UNAVAILABLE_NOTICE
    assert reply["emotion"] == "neutral"
    memory.close()


def test_crisis_never_calls_router_and_has_static_fallback(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    router = FakeRouter(RouterDecision(GateRoute.LOCAL, 1.0))
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
    decision = RouterDecision(GateRoute.LOCAL, 0.9)
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
    decision = RouterDecision(GateRoute.LOCAL, 0.9)
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


def test_decision_trace_includes_route(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    records = []
    hub = DecisionHub(bus, memory=memory, local_enabled=False)
    hub._trace_logger = type("L", (), {"info": lambda self, evt, **kw: records.append(kw)})()
    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好"})
    assert records[0]["route"] == "cloud"
    assert records[0]["reply_id"]
    assert "tier" not in records[0]
    assert "intent" not in records[0]
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


def test_hub_persists_user_before_generation_and_links_reply(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    turn_store = ThreadTurnStore(tmp_path / "thread.db")
    context = WorkingContext(turn_store)

    class InspectingBridge:
        def generate(self, utterance, snapshot=None, memory=None):
            turns = turn_store.items()
            assert [(turn["role"], turn["response_state"]) for turn in turns] == [
                ("user", "pending")
            ]
            assert snapshot.recent_turns == ()
            return "已经记下了"

    hub = DecisionHub(
        bus,
        memory=memory,
        bridge=InspectingBridge(),
        context=context,
        projector=ContextProjector(),
        local_enabled=False,
    )

    hub.handle_chat_request({"text": "别忘了这句话"})

    turns = turn_store.items()
    assert [(turn["role"], turn["content"]) for turn in turns] == [
        ("agent", "已经记下了"),
        ("user", "别忘了这句话"),
    ]
    assert turns[0]["reply_to_turn_id"] == turns[1]["id"]
    assert turns[1]["response_state"] == "completed"
    turn_store.close()
    memory.close()


def test_hub_marks_user_turn_interrupted_when_no_reply_is_committed(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    turn_store = ThreadTurnStore(tmp_path / "thread.db")
    context = WorkingContext(turn_store)
    loop = FakeLoop(
        result={"text": "", "steps": 1, "interrupted": True, "failed": False},
    )
    hub = DecisionHub(
        bus,
        memory=memory,
        loop=loop,
        context=context,
        projector=ContextProjector(),
        local_enabled=False,
    )

    result = hub.handle_chat_request({"text": "先停一下"})

    assert result["spoke"] is False
    assert turn_store.items()[0]["response_state"] == "interrupted"
    turn_store.close()
    memory.close()


def test_hub_marks_failed_and_does_not_publish_when_reply_persistence_fails(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    thread_path = tmp_path / "thread.db"
    turn_store = ThreadTurnStore(thread_path)
    context = WorkingContext(turn_store)
    with sqlite3.connect(thread_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_agent_turn
            BEFORE INSERT ON thread_turns
            WHEN NEW.role = 'agent'
            BEGIN
                SELECT RAISE(ABORT, 'agent persistence failed');
            END
            """
        )
    hub = DecisionHub(
        bus,
        memory=memory,
        bridge=FakeBridge(reply="不会成功提交"),
        context=context,
        projector=ContextProjector(),
        local_enabled=False,
    )

    try:
        with pytest.raises(sqlite3.IntegrityError, match="agent persistence failed"):
            hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "请回答"})

        assert _reply_text(bus) is None
        assert turn_store.items()[0]["response_state"] == "failed"
    finally:
        turn_store.close()
        memory.close()


def test_hub_marks_user_turn_failed_when_generation_raises(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    turn_store = ThreadTurnStore(tmp_path / "thread.db")
    context = WorkingContext(turn_store)

    class ExplodingRouter:
        def route(self, text, *, snapshot=None, situation=None):
            raise RuntimeError("generation exploded")

    hub = DecisionHub(
        bus,
        memory=memory,
        local_router=ExplodingRouter(),
        context=context,
        projector=ContextProjector(),
        local_enabled=True,
    )

    try:
        with pytest.raises(RuntimeError, match="generation exploded"):
            hub.handle_chat_request({"text": "触发异常"})

        assert turn_store.items()[0]["response_state"] == "failed"
    finally:
        turn_store.close()
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
    working = WorkingContext(ShortTermTurnStore(manager))
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
    assert len(bus.subscriptions[Topics.USER_UTTERANCE]) == 2
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
