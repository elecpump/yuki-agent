import threading
import time

from yuki.cognition.brain.cooldown import CooldownCalculator
from yuki.cognition.brain.hub import DecisionHub
from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.proactive import ProactiveDecision
from yuki.topics import Topics


class FakeBus:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))


class FakeTraceLogger:
    def __init__(self):
        self.records = []

    def info(self, event, **fields):
        self.records.append((event, fields))


class FakeContext:
    def __init__(self):
        self.current = None
        self.agents = []

    def update_situation(self, payload):
        self.current = payload

    def add_agent(self, text):
        self.agents.append(text)

    def situation(self):
        return self.current


class FakeProjector:
    def build(self, context):
        return ContextSnapshot(situation=context.current)


class FakeAgent:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = 0

    def decide(self, snapshot, soul):
        self.calls += 1
        return self.decisions.pop(0)


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached")


def test_situation_speak_is_async_published_and_written_to_context():
    bus = FakeBus()
    context = FakeContext()
    trace_logger = FakeTraceLogger()
    agent = FakeAgent([ProactiveDecision("speak", "要聊聊这个吗？", "opening", "raw")])
    hub = DecisionHub(
        bus,
        proactive_agent=agent,
        cooldown_calculator=CooldownCalculator(),
        context=context,
        projector=FakeProjector(),
        trace_logger=trace_logger,
        proactive_tick_s=0,
    )
    hub.start()
    hub.on_situation_update(
        Topics.SITUATION_UPDATE,
        {"source_id": "page-1", "topic": "文章"},
    )
    wait_until(lambda: bool(bus.published))
    hub.close()
    assert bus.published[0][0] == Topics.REPLY
    assert bus.published[0][1]["kind"] == "final"
    assert context.agents == ["要聊聊这个吗？"]
    assert trace_logger.records[-1][1]["llm_reason"] == "opening"


def test_lifecycle_and_hard_gates_skip_cloud_calls():
    now = [100.0]
    bus = FakeBus()
    agent = FakeAgent([ProactiveDecision("silent", "", "quiet", "raw")])
    hub = DecisionHub(
        bus,
        proactive_agent=agent,
        proactive_tick_s=0,
        dedup_min_interval_s=30,
        clock=lambda: now[0],
    )
    situation = {"source_id": "page-1", "topic": "文章"}
    hub.on_situation_update(Topics.SITUATION_UPDATE, situation)
    assert agent.calls == 0
    hub.start()
    hub.on_user_utterance_probe(Topics.USER_UTTERANCE, {"ts": 99.0})
    hub.on_situation_update(Topics.SITUATION_UPDATE, situation)
    assert agent.calls == 0
    now[0] = 140.0
    hub.on_situation_update(Topics.SITUATION_UPDATE, situation)
    wait_until(lambda: agent.calls == 1)
    now[0] = 150.0
    hub.on_situation_update(Topics.SITUATION_UPDATE, situation)
    assert agent.calls == 1
    now[0] = 180.0
    hub.on_situation_update(Topics.SITUATION_UPDATE, situation)
    assert agent.calls == 1  # silent hold remains after the shorter dedup gate expires
    hub.close()


def test_new_input_during_decision_drops_reply_without_silent_signal():
    entered = threading.Event()
    release = threading.Event()

    class BlockingAgent:
        def decide(self, snapshot, soul):
            entered.set()
            release.wait(1.0)
            return ProactiveDecision("speak", "过期回复", "opening", "raw")

    now = [10.0]
    bus = FakeBus()
    cooldown = CooldownCalculator()
    hub = DecisionHub(
        bus,
        proactive_agent=BlockingAgent(),
        cooldown_calculator=cooldown,
        proactive_tick_s=0,
        clock=lambda: now[0],
    )
    hub.start()
    hub.trigger_proactive_tick()
    assert entered.wait(1.0)
    hub.on_user_utterance_probe(Topics.USER_UTTERANCE, {"ts": 11.0})
    now[0] = 11.0
    release.set()
    wait_until(lambda: cooldown.next_available_ts > 11.0)
    hub.close()
    assert bus.published == []
    assert cooldown.silent_streak == 0


def test_disabled_and_cooldown_gates_are_free():
    situation = {"source_id": "page-1", "topic": "文章"}
    agent = FakeAgent([ProactiveDecision("speak", "hi", "opening", "raw")])
    disabled = DecisionHub(FakeBus(), proactive_agent=agent, proactive_enabled=False)
    disabled.start()
    disabled.on_situation_update(Topics.SITUATION_UPDATE, situation)
    assert agent.calls == 0
    disabled.close()

    now = [10.0]
    cooldown = CooldownCalculator()
    cooldown.on_decision("speak", now[0])
    hub = DecisionHub(
        FakeBus(),
        proactive_agent=agent,
        cooldown_calculator=cooldown,
        proactive_tick_s=0,
        dedup_min_interval_s=0,
        silent_hold_s=0,
        clock=lambda: now[0],
    )
    hub.start()
    hub.on_situation_update(Topics.SITUATION_UPDATE, situation)
    assert agent.calls == 0
    hub.close()


def test_three_failures_open_circuit_and_later_success_resets_it():
    now = [0.0]
    failures = [ProactiveDecision("silent", "", "cloud_error", None) for _ in range(3)]
    agent = FakeAgent([*failures, ProactiveDecision("silent", "", "quiet", "raw")])
    hub = DecisionHub(
        FakeBus(),
        proactive_agent=agent,
        proactive_tick_s=0,
        dedup_min_interval_s=0,
        silent_hold_s=0,
        clock=lambda: now[0],
    )
    hub.start()
    for index in range(3):
        hub.trigger_proactive_tick()
        wait_until(lambda: agent.calls == index + 1)
        now[0] = hub._cooldown.next_available_ts
    assert hub._proactive._failure_streak == 3
    hub.trigger_proactive_tick()
    assert agent.calls == 3
    now[0] = max(now[0], hub._proactive._disabled_until) + 0.1
    hub.trigger_proactive_tick()
    wait_until(lambda: agent.calls == 4)
    assert hub._proactive._failure_streak == 0
    hub.close()


def test_tick_uses_same_worker_path():
    bus = FakeBus()
    context = FakeContext()
    context.current = {"source_id": "idle", "topic": "桌面"}
    agent = FakeAgent([ProactiveDecision("speak", "破冰", "opening", "raw")])
    hub = DecisionHub(
        bus,
        proactive_agent=agent,
        context=context,
        projector=FakeProjector(),
        proactive_tick_s=0.01,
    )
    hub.start()
    wait_until(lambda: bool(bus.published))
    hub.close()
    assert bus.published[0][1]["text"] == "破冰"
