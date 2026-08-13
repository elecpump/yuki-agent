import pytest

from yuki.cognition.l1_responder import L1Responder, build_l1_responder
from yuki.topics import Topics


class FakeL1:
    def reply(self, text, context=None):
        return f"reply:{text}"


class FakeBus:
    def __init__(self):
        self.published = []
        self.subscriptions = {}

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def subscribe(self, prefix, handler):
        self.subscriptions[prefix] = handler


def test_awake_triggers_l1_reply():
    bus = FakeBus()
    responder = build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[Topics.AWAKE]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert any(t == Topics.REPLY for t, _ in bus.published)


def test_utterance_triggers_l1_reply_with_text():
    bus = FakeBus()
    responder = build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[Topics.USER_UTTERANCE](
        Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    replies = [p for t, p in bus.published if t == Topics.REPLY]
    assert replies and replies[0]["text"] == "reply:你好"
