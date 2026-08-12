import pytest

from yuki.cognition.pipeline import PerceptionPipeline, build_pipeline
from yuki.topics import Topics


class FakeVLM:
    def __init__(self):
        self.understand_calls = []

    def understand(self, image, cache_key=None):
        self.understand_calls.append(cache_key)
        return {"topic": "climate", "summary": "s", "content_type": "article", "key_points": []}


class FakeSensitive:
    def scan(self, text):
        return []


class FakeSTT:
    def recognize_base64(self, pcm, sample_rate=16000):
        return "你好"


class FakeL1:
    def reply(self, text, context=None):
        return "你好呀，我在呢。"


class FakeFrameClient:
    def __init__(self):
        self.latest = {"png": "AAA", "width": 10, "height": 10, "ts": 1.0, "sensitive": False}

    def get_latest(self):
        return dict(self.latest)


class FakeBus:
    def __init__(self):
        self.published = []
        self.subscriptions = {}

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def subscribe(self, prefix, handler):
        self.subscriptions[prefix] = handler

    def respond(self, service, handler):
        pass


def test_pipeline_on_awake_replies():
    bus = FakeBus()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        sensitive_filter=FakeSensitive(),
        stt=FakeSTT(),
        l1=FakeL1(),
        frame_client=FakeFrameClient(),
    )
    bus.subscriptions[Topics.AWAKE]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_understand_screen_uses_vlm():
    bus = FakeBus()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        sensitive_filter=FakeSensitive(),
        stt=FakeSTT(),
        l1=FakeL1(),
        frame_client=FakeFrameClient(),
    )
    context = pipeline.understand_screen()
    assert context["topic"] == "climate"


def test_pipeline_stt_on_mic():
    bus = FakeBus()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        sensitive_filter=FakeSensitive(),
        stt=FakeSTT(),
        l1=FakeL1(),
        frame_client=FakeFrameClient(),
    )
    import base64
    bus.subscriptions[Topics.AWAKE]("event/awake", {"source": "hotkey", "ts": 0.0})
    bus.published = []
    pcm = base64.b64encode(b"\x00\x00\x00\x00").decode("ascii")
    bus.subscriptions[Topics.MIC]("audio/mic", {"pcm": pcm, "sample_rate": 16000, "ts": 0.0})
    replies = [payload for topic, payload in bus.published if topic == Topics.REPLY]
    assert len(replies) == 1 and replies[0]["text"] == "你好呀，我在呢。"


def test_pipeline_mic_before_awake_is_blocked():
    bus = FakeBus()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        sensitive_filter=FakeSensitive(),
        stt=FakeSTT(),
        l1=FakeL1(),
        frame_client=FakeFrameClient(),
    )
    import base64
    pcm = base64.b64encode(b"\x00\x00\x00\x00").decode("ascii")
    bus.subscriptions[Topics.MIC]("audio/mic", {"pcm": pcm, "sample_rate": 16000, "ts": 0.0})
    assert bus.published == []
