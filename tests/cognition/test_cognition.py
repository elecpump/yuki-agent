from yuki.cognition.main import build_cognition
from yuki.topics import Topics


class FakeBus:
    def __init__(self):
        self.handler = None
        self.published = []

    def subscribe(self, prefix, handler):
        self.handler = handler

    def publish(self, topic, payload):
        self.published.append((topic, payload))


def test_build_cognition_wires_awake_to_reply():
    bus = FakeBus()
    build_cognition(bus)
    assert bus.handler is not None
    bus.handler(Topics.AWAKE, {"source": "hotkey", "ts": 0.0})
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.REPLY
    assert payload["text"] == "我在，你说。"


def test_build_cognition_still_replies_on_awake():
    bus = FakeBus()
    build_cognition(bus)
    assert bus.handler is not None
    bus.handler(Topics.AWAKE, {"source": "hotkey", "ts": 0.0})
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.REPLY
    assert payload["text"] == "我在，你说。"


def test_build_cognition_with_pipeline_skips_legacy_handler():
    bus = FakeBus()
    build_cognition(bus, pipeline=object())
    assert bus.handler is None
    assert bus.published == []
