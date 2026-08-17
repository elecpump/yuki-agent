import pytest

from yuki.cognition.brain.actions import DEFAULT_JOKES
from yuki.cognition.brain.hub import COGNITION_AWAKE_SERVICE, DecisionHub, build_brain
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.brain.policy import DecisionPolicy
from yuki.cognition.context.store import ShortTermTurnStore
from yuki.cognition.context.working import WorkingContext
from yuki.cognition.l2.client import CloudError
from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.store import MemoryStore
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def reply(self, text, context=None):
        return "我在，你说。" if not text else f"l1:{text}"


@pytest.fixture()
def hub(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = DecisionHub(
        bus,
        policy=DecisionPolicy(proactive_cooldown_s=120.0),
        memory=memory,
        l1=FakeL1(),
    )
    yield hub, bus, memory
    memory.close()


def _reply_text(bus) -> str | None:
    for topic, payload in reversed(bus.published):
        if topic == Topics.REPLY:
            return payload["text"]
    return None


def test_awake_replies_l1_greeting(hub):
    h, bus, _ = hub
    result = h.handle_awake_request({"source": "hotkey", "ts": 0.0})
    assert result["text"] == "我在，你说。"
    assert _reply_text(bus) is None


def test_chit_chat_utterance_replies(hub):
    h, bus, _ = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert _reply_text(bus) == "l1:你好"


def test_emotional_utterance_empathizes_and_writes_memory(hub):
    h, bus, memory = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我今天升职了", "duration_s": 1.0, "ts": 0.0})
    text = _reply_text(bus)
    assert "开心" in text or "替你开心" in text
    assert memory.query("升职")


def test_unknown_utterance_clarifies(hub):
    h, bus, _ = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "qwerty乱码", "duration_s": 1.0, "ts": 0.0})
    assert "听懂" in _reply_text(bus)


def test_safety_utterance_escalates(hub):
    h, bus, memory = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我不想活了", "duration_s": 1.0, "ts": 0.0})
    text = _reply_text(bus)
    assert "求助" in text
    assert memory.list() == []  # safety 不写记忆


def test_situation_within_cooldown_stays_silent(hub):
    h, bus, _ = hub
    h.handle_awake_request({"source": "hotkey", "ts": 0.0})  # 记录 last_open
    before = len(bus.published)
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    assert len(bus.published) == before  # 无新 REPLY


def test_situation_proactive_after_cooldown(hub, monkeypatch):
    h, bus, _ = hub
    import time
    monkeypatch.setattr("time.time", lambda: 0.0)
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    monkeypatch.setattr("time.time", lambda: 200.0)
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    text = _reply_text(bus)
    assert "量子计算" in text


def test_build_brain_subscribes_and_configures(tmp_path):
    bus = FakeBus()
    memory = MemoryManager(MemoryStore(tmp_path / "m.db"))
    hub = build_brain(bus, memory=memory, registry=FunctionRegistry())
    assert COGNITION_AWAKE_SERVICE in bus.services
    assert Topics.AWAKE not in bus.subscriptions
    assert Topics.USER_UTTERANCE in bus.subscriptions
    assert Topics.SITUATION_UPDATE in bus.subscriptions
    memory.close()


def test_decision_trace_logged(hub):
    h, bus, _ = hub
    records = []
    h._trace_logger = type("L", (), {"info": lambda self, evt, **kw: records.append(kw)})()
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert records and records[0]["trigger"] == "utterance"
    assert records[0]["intent"] == "chit_chat"


class FakeBridge:
    def __init__(self, reply=None, error=None):
        self._reply = reply
        self._error = error
        self.calls = []

    def generate(self, utterance, situation=None, memory=None):
        self.calls.append(utterance)
        if self._error:
            raise self._error
        return self._reply


class FakeSensitive:
    def __init__(self, flag: bool = False):
        self.flag = flag
        self.scanned = []

    def is_sensitive(self, text: str) -> bool:
        return self.flag

    def scan(self, text: str) -> list[str]:
        self.scanned.append(text)
        return ["test_rule"] if self.flag else []


def test_l2_blocked_for_sensitive_utterance(hub):
    h, bus, _ = hub
    bridge = FakeBridge(reply="不应被调用")
    h._bridge = bridge
    h._sensitive_filter = FakeSensitive(True)
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "我的密码是123456", "duration_s": 1.0, "ts": 0.0})
    assert bridge.calls == []
    assert _reply_text(bus)  # 本地 L1 兜底，仍然有回应

def test_l2_intent_routes_to_bridge(hub):
    h, bus, _ = hub
    h._bridge = FakeBridge(reply="云端深度回答")
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert _reply_text(bus) == "云端深度回答"


def test_l2_failure_falls_back_to_l1(hub):
    h, bus, _ = hub
    h._bridge = FakeBridge(error=CloudError("boom"))
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    text = _reply_text(bus)
    assert text.startswith(DEFAULT_JOKES[0])  # L1 动作链兜底
    assert "云端暂时不可用" in text


def test_l2_empty_reply_falls_back_to_l1(hub):
    h, bus, _ = hub
    bridge = FakeBridge(reply="   ")
    h._bridge = bridge
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert _reply_text(bus)  # 空回复 → L1 兜底
    assert bridge.calls == ["讲个笑话"]


def test_l2_intent_without_bridge_uses_l1(hub):
    h, bus, _ = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert _reply_text(bus)


def test_l2_intent_without_bridge_notifies_offline(hub):
    h, bus, _ = hub
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    text = _reply_text(bus)
    assert "云端暂时不可用" in text  # §8.2：未配置云端也要明确告知降级


def test_sensitive_l2_block_is_audited(hub):
    h, bus, _ = hub
    records = []
    h._audit_logger = type("L", (), {"info": lambda self, evt, **kw: records.append(kw)})()
    h._bridge = FakeBridge(reply="不应被调用")
    h._sensitive_filter = FakeSensitive(True)
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert records and records[0]["action"] == "block_l2_route"
    assert records[0]["categories"] == ["test_rule"]
    assert "ts" in records[0]  # §9.3：审计含时间/规则编号/命中类别，不存原文


def test_l1_intent_never_calls_bridge(hub):
    h, bus, _ = hub
    bridge = FakeBridge(reply="不应被调用")
    h._bridge = bridge
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert bridge.calls == []


def test_decision_trace_includes_tier(hub):
    h, bus, _ = hub
    records = []
    h._trace_logger = type("L", (), {"info": lambda self, evt, **kw: records.append(kw)})()
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "讲个笑话", "duration_s": 1.0, "ts": 0.0})
    assert records[0]["tier"] == "l2"


def test_decision_trace_includes_situation_provenance(hub):
    h, bus, _ = hub
    h._context = {
        "situation_id": "frame:42",
        "frame_id": 42,
        "source_id": "https://x.com/a",
        "scroll_band": "25-50",
        "observation_reason": "scroll_idle",
        "frame_ts": 9.5,
        "topic": "climate",
        "summary": "long text that should stay out of trace",
    }
    records = []
    h._trace_logger = type("L", (), {"info": lambda self, evt, **kw: records.append(kw)})()

    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "浣犲ソ", "duration_s": 1.0, "ts": 0.0})

    assert records[0]["situation_provenance"] == {
        "situation_id": "frame:42",
        "frame_id": 42,
        "source_id": "https://x.com/a",
        "scroll_band": "25-50",
        "observation_reason": "scroll_idle",
        "frame_ts": 9.5,
    }


class FakeTuner:
    def __init__(self):
        self.opens = 0
        self.utterances = []

    def on_proactive_open(self):
        self.opens += 1

    def on_user_utterance(self, text):
        self.utterances.append(text)


def test_hub_notifies_tuner_on_proactive_open(hub, monkeypatch):
    h, bus, _ = hub
    tuner = FakeTuner()
    h._tuner = tuner
    monkeypatch.setattr("time.time", lambda: 0.0)
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    assert tuner.opens == 1


def test_hub_does_not_notify_tuner_on_silent_situation(hub):
    h, bus, _ = hub
    tuner = FakeTuner()
    h._tuner = tuner
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "x", "sensitive": True, "ts": 0.0})
    assert tuner.opens == 0


def test_hub_feeds_utterance_to_tuner(hub):
    h, bus, _ = hub
    tuner = FakeTuner()
    h._tuner = tuner
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert tuner.utterances == ["你好"]


class FakeContext:
    def __init__(self):
        self.users = []
        self.agents = []
        self.situations = []
        self.snap = None

    def set_snapshot(self, snap):
        self.snap = snap

    def add_user(self, text):
        self.users.append(text)

    def add_agent(self, text):
        self.agents.append(text)

    def update_situation(self, payload):
        self.situations.append(payload)


class FakeProjector:
    def __init__(self):
        self.last = None

    def build(self, working):
        self.last = working
        return working.snap


def test_hub_writes_context_and_uses_projection(hub):
    h, bus, _ = hub
    ctx = FakeContext()
    ctx.set_snapshot(ContextSnapshot(situation={"topic": "量子计算", "sensitive": False}))
    proj = FakeProjector()
    h._context_wrapper = ctx
    h._projector = proj
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert ctx.users == ["你好"]
    assert proj.last is ctx
    assert ctx.agents == ["l1:你好"]


def test_single_turn_writer_no_double_write(tmp_path):
    bus = FakeBus()
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    working = WorkingContext(ShortTermTurnStore(manager), snapshot_path=None)
    h = DecisionHub(
        bus,
        policy=DecisionPolicy(proactive_cooldown_s=120.0),
        memory=manager,
        l1=FakeL1(),
        context=working,
    )
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert working.turn_count() == 2
    contents = [it["content"] for it in working.items()]
    assert "你好" in contents
    assert "l1:你好" in contents
    manager.close()


def test_hub_context_situation_used_for_situation_update(hub):
    h, bus, _ = hub
    ctx = FakeContext()
    ctx.set_snapshot(ContextSnapshot())
    h._context_wrapper = ctx
    h._projector = FakeProjector()
    h.on_situation_update(Topics.SITUATION_UPDATE, {"topic": "量子计算", "sensitive": False, "ts": 0.0})
    assert ctx.situations == [{"topic": "量子计算", "sensitive": False, "ts": 0.0}]


class FakeSedimenter:
    def __init__(self):
        self.utterances = []
        self.topics = []

    def on_user_utterance(self, text, intent):
        self.utterances.append((text, intent))

    def on_engagement(self, topic):
        self.topics.append(topic)


def test_hub_feeds_sedimenter(hub):
    h, bus, _ = hub
    sed = FakeSedimenter()
    h._sedimenter = sed
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "太吵了", "duration_s": 1.0, "ts": 0.0})
    assert sed.utterances and sed.utterances[0][0] == "太吵了"


def test_hub_feeds_engagement_topic(hub):
    h, bus, _ = hub
    sed = FakeSedimenter()
    h._sedimenter = sed
    h._context = {"topic": "量子计算", "sensitive": False}
    h.on_user_utterance(Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    assert "量子计算" in sed.topics
