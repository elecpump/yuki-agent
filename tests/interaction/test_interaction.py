from yuki.interaction.hotkey import HotkeyManager
from yuki.interaction.main import build_interaction
from yuki.topics import Topics


class FakeBus:
    def __init__(self):
        self.handler = None
        self.published = []

    def subscribe(self, prefix, handler):
        self.handler = handler

    def publish(self, topic, payload):
        self.published.append((topic, payload))


class FakeHotkeys:
    def __init__(self):
        self.handler = None

    def register(self, name, handler):
        self.handler = handler

    def trigger(self, name):
        self.handler()


def test_hotkey_manager_register_trigger():
    calls = []
    hk = HotkeyManager()
    hk.register("trigger", lambda: calls.append("x"))
    hk.trigger("trigger")
    assert calls == ["x"]


def test_build_interaction_publishes_awake_on_trigger():
    bus = FakeBus()
    hotkeys = FakeHotkeys()
    build_interaction(bus, hotkeys)
    hotkeys.trigger("trigger")
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.AWAKE
    assert payload["source"] == "hotkey"


def test_build_interaction_subscribes_to_reply():
    bus = FakeBus()
    build_interaction(bus, FakeHotkeys())
    assert bus.handler is not None
