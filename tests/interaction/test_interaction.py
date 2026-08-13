from yuki.config import Config
from yuki.interaction.agent import InteractionAgent
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

    def speak(self, text):
        self.said.append(text)


def test_hotkey_manager_register_trigger():
    calls = []
    hk = HotkeyManager()
    hk.register("trigger", lambda: calls.append("x"))
    hk.trigger("trigger")
    assert calls == ["x"]


def test_interaction_agent_publishes_awake_on_trigger():
    bus = FakeBus()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=FakeTTS())
    agent.setup()
    agent._hotkeys.trigger("trigger")
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.AWAKE
    assert payload["source"] == "hotkey"
    agent.teardown()


def test_interaction_agent_reply_feeds_tts():
    bus = FakeBus()
    tts = FakeTTS()
    agent = InteractionAgent(Config(), bus=bus, hotkeys=FakeHotkeys(), tts=tts)
    agent.setup()
    bus.subscriptions[Topics.REPLY][0](Topics.REPLY, {"text": "你好", "ts": 0.0})
    assert tts.said == ["你好"]
    agent.teardown()
