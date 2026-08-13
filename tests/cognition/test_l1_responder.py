import pytest

from yuki.cognition.l1_responder import L1Responder, build_l1_responder
from yuki.topics import Topics

from tests.fakes import FakeBus


class FakeL1:
    def __init__(self):
        self.seen_contexts = []

    def reply(self, text, context=None):
        self.seen_contexts.append(context)
        return f"reply:{text}"


def test_awake_triggers_l1_reply():
    bus = FakeBus()
    responder = build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert any(t == Topics.REPLY for t, _ in bus.published)


def test_utterance_triggers_l1_reply_with_text():
    bus = FakeBus()
    responder = build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[Topics.USER_UTTERANCE][0](
        Topics.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    replies = [p for t, p in bus.published if t == Topics.REPLY]
    assert replies and replies[0]["text"] == "reply:你好"


def test_situation_update_stores_context_and_feeds_reply():
    bus = FakeBus()
    l1 = FakeL1()
    responder = build_l1_responder(bus, l1=l1)
    situation = {"source_id": "https://example.com", "scroll_band": "0-25",
                 "topic": "量子计算", "summary": "介绍", "content_type": "web",
                 "key_points": ["a"], "sensitive": False, "degraded": False,
                 "reason": "", "ts": 0.0}
    bus.subscriptions[Topics.SITUATION_UPDATE][0](Topics.SITUATION_UPDATE, situation)
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert l1.seen_contexts and l1.seen_contexts[0] == situation
