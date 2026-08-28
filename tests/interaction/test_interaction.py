import time

from yuki.bus import BusTimeoutError
from yuki.cognition.brain.hub import COGNITION_AWAKE_SERVICE, DecisionHub
from yuki.config import Config
from yuki.interaction.agent import InteractionAgent, VolumeController
from yuki.interaction.hotkey import HotkeyManager
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeHotkeys:
    def __init__(self):
        self.handler = None

    def register(self, name, handler):
        self.handler = handler

    def trigger(self, name):
        self.handler()


class FakeTTS:
    def __init__(self):
        self.said = []
        self.emotions = []
        self.kinds = []
        self.reply_ids = []
        self.cancelled = []

    def speak(self, text, emotion="neutral", *, kind="final", reply_id=None):
        self.said.append(text)
        self.emotions.append(emotion)
        self.kinds.append(kind)
        self.reply_ids.append(reply_id)

    def cancel(self, reply_id):
        self.cancelled.append(reply_id)


def test_hotkey_manager_register_trigger():
    calls = []
    hk = HotkeyManager()
    hk.register("trigger", lambda: calls.append("x"))
    hk.trigger("trigger")
    assert calls == ["x"]


def test_interaction_agent_requests_awake_on_trigger_and_speaks_reply():
    bus = FakeBus()
    calls = []

    def on_awake(payload):
        calls.append(payload)
        return {"text": "ready", "ts": 1.0}

    bus.respond(COGNITION_AWAKE_SERVICE, on_awake)
    tts = FakeTTS()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    agent.setup()
    agent._hotkeys.trigger("trigger")
    assert calls and calls[0]["source"] == "hotkey"
    assert tts.said == ["ready"]
    assert tts.emotions == ["neutral"]
    assert not any(topic == Topics.AWAKE for topic, _ in bus.published)
    agent.teardown()


def test_interaction_agent_reports_awake_timeout():
    class TimeoutBus(FakeBus):
        def request(self, service, payload, timeout_ms=2000):
            raise BusTimeoutError("cognition offline")

    bus = TimeoutBus()
    tts = FakeTTS()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    agent.setup()
    agent._hotkeys.trigger("trigger")
    assert tts.said == ["我现在连接不上 cognition。"]
    agent.teardown()


def test_volume_controller_restores_level_after_restart(tmp_path):
    path = tmp_path / "tier.json"
    first = VolumeController(path)
    first.set_level("active")
    assert first.level() == "active"

    restarted = VolumeController(path)
    assert restarted.level() == "active"  # §8.1：重启后恢复档位
    restarted.set_level("normal")
    assert restarted.level() == "normal"


def test_volume_controller_defaults_to_normal(tmp_path):
    controller = VolumeController(tmp_path / "missing.json")
    assert controller.level() == "normal"


def test_interaction_agent_reply_feeds_tts():
    bus = FakeBus()
    tts = FakeTTS()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    agent.setup()
    bus.subscriptions[Topics.REPLY][0](Topics.REPLY, {"text": "你好", "ts": 0.0})
    assert tts.said == ["你好"]
    assert tts.emotions == ["neutral"]
    agent.teardown()


def test_interaction_agent_forwards_reply_emotion():
    bus = FakeBus()
    tts = FakeTTS()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    agent.setup()
    bus.subscriptions[Topics.REPLY][0](
        Topics.REPLY,
        {"text": "太好了", "emotion": "joy", "ts": 0.0},
    )
    assert tts.said == ["太好了"]
    assert tts.emotions == ["joy"]
    agent.teardown()


def test_interaction_agent_forwards_reply_kind_and_id_and_handles_cancel():
    bus = FakeBus()
    tts = FakeTTS()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    agent.setup()
    on_reply = bus.subscriptions[Topics.REPLY][0]

    on_reply(
        Topics.REPLY,
        {
            "text": "我看看",
            "emotion": "neutral",
            "kind": "transition",
            "reply_id": "reply-1",
            "ts": 0.0,
        },
    )
    on_reply(
        Topics.REPLY,
        {"text": "", "kind": "cancel", "reply_id": "reply-1", "ts": 0.1},
    )

    assert tts.said == ["我看看"]
    assert tts.kinds == ["transition"]
    assert tts.reply_ids == ["reply-1"]
    assert tts.cancelled == ["reply-1"]
    agent.teardown()


def test_hub_transition_cancel_flow_reaches_interaction_controller():
    class InterruptingLoop:
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
            on_transition("我看看")
            return {"text": "", "steps": 1, "interrupted": True, "failed": False}

    bus = FakeBus()
    tts = FakeTTS()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    agent.setup()
    hub = DecisionHub(bus, loop=InterruptingLoop(), local_enabled=False)

    hub.on_user_utterance(Topics.USER_UTTERANCE, {"text": "查一下", "ts": time.time()})
    on_reply = bus.subscriptions[Topics.REPLY][0]
    for topic, payload in list(bus.published):
        if topic == Topics.REPLY:
            on_reply(topic, payload)

    assert tts.said == ["我看看"]
    assert tts.kinds == ["transition"]
    assert tts.cancelled == tts.reply_ids
    agent.teardown()


def test_unavailable_tts_falls_back_to_console(capsys):
    class UnavailableTts:
        def synthesize_stream(self, text, emotion_vector=None, ref_audio=None, lang=None):
            raise RuntimeError("model_worker_unavailable")

        def health(self):
            return {"degraded": True}

    bus = FakeBus()
    agent = InteractionAgent(
        Config(),
        bus=bus,
        hotkeys=FakeHotkeys(),
        tts_model=UnavailableTts(),
    )
    agent.setup()
    bus.subscriptions[Topics.REPLY][0](
        Topics.REPLY,
        {"text": "console fallback", "emotion": "joy", "ts": 0.0},
    )
    deadline = time.monotonic() + 1.0
    output = ""
    while "console fallback" not in output and time.monotonic() < deadline:
        time.sleep(0.01)
        output += capsys.readouterr().out
    assert "[yuki] console fallback" in output
    assert agent._tts_health().ok is True
    assert agent._tts_health().detail["degraded"] is True
    agent.teardown()
