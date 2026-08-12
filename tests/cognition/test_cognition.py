from yuki.cognition.l1_responder import build_l1_responder
from yuki.cognition.main import build_cognition
from yuki.topics import Topics


class FakeL1:
    def reply(self, text, context=None):
        return f"reply:{text}"


class FakeBus:
    def __init__(self):
        self.published = []
        self.subscriptions = {}

    def subscribe(self, prefix, handler):
        self.subscriptions[prefix] = handler

    def publish(self, topic, payload):
        self.published.append((topic, payload))


def test_build_cognition_wires_awake_to_reply():
    bus = FakeBus()
    build_cognition(bus)
    assert Topics.AWAKE in bus.subscriptions
    bus.subscriptions[Topics.AWAKE](Topics.AWAKE, {"source": "hotkey", "ts": 0.0})
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.REPLY
    assert payload["text"] == "我在，你说。"


def test_l1_responder_wires_awake_to_reply():
    bus = FakeBus()
    build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[Topics.AWAKE](Topics.AWAKE, {"source": "hotkey", "ts": 0.0})
    replies = [p for t, p in bus.published if t == Topics.REPLY]
    assert replies and replies[0]["text"] == "reply:"


def test_build_cognition_with_pipeline_skips_legacy_handler():
    bus = FakeBus()
    build_cognition(bus, pipeline=object())
    assert Topics.AWAKE not in bus.subscriptions
    assert bus.published == []
