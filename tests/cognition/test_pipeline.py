import base64
import io

import pytest
from PIL import Image

from yuki.cognition.pipeline import PerceptionPipeline, build_pipeline
from yuki.cognition.sensitive import SensitiveFilter
from yuki.topics import Topics


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class FakeVLM:
    def __init__(self):
        self.understand_calls = []

    def understand(self, image, cache_key=None):
        self.understand_calls.append((image, cache_key))
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


class RecordingL1(FakeL1):
    def __init__(self):
        self.contexts = []

    def reply(self, text, context=None):
        self.contexts.append(context)
        return "你好呀，我在呢。"


class FakeFrameClient:
    def __init__(self, png=None):
        self.latest = {
            "png": _png_b64() if png is None else png,
            "width": 4,
            "height": 4,
            "ts": 1.0,
            "sensitive": False,
        }

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


def _make_pipeline(vlm=None, sensitive=None, stt=None, l1=None, frame_client=None):
    return build_pipeline(
        FakeBus(),
        vlm=vlm or FakeVLM(),
        sensitive_filter=sensitive or FakeSensitive(),
        stt=stt or FakeSTT(),
        l1=l1 or FakeL1(),
        frame_client=frame_client or FakeFrameClient(),
    )


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


def test_pipeline_focus_passes_decoded_pil_image_to_vlm():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm)
    pipeline.on_focus_changed("event/focus", {"title": "t", "url": "u"})
    image = vlm.understand_calls[0][0]
    assert isinstance(image, Image.Image)
    assert not isinstance(image, str)


def test_pipeline_focus_skips_placeholder_frame():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm, frame_client=FakeFrameClient(png=""))
    pipeline.on_focus_changed("event/focus", {"title": "t", "url": "u"})
    assert vlm.understand_calls == []


def test_pipeline_understand_screen_skips_placeholder_frame():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm, frame_client=FakeFrameClient(png=""))
    assert pipeline.understand_screen() == {"topic": "", "sensitive": True}
    assert vlm.understand_calls == []


def test_pipeline_focus_blocks_sensitive_key_points():
    l1 = RecordingL1()

    class SensitiveKeyPointsVLM(FakeVLM):
        def understand(self, image, cache_key=None):
            self.understand_calls.append((image, cache_key))
            return {
                "topic": "理财",
                "summary": "推荐方案",
                "content_type": "web",
                "key_points": ["联系方式 13812345678"],
            }

    pipeline = _make_pipeline(vlm=SensitiveKeyPointsVLM(), sensitive=SensitiveFilter(), l1=l1)
    pipeline.on_focus_changed("event/focus", {"title": "t", "url": "u"})
    pipeline.on_awake("event/awake", {"source": "hotkey", "ts": 0.0})
    assert l1.contexts[-1] == {"topic": "", "sensitive": True}


def test_pipeline_understand_screen_blocks_sensitive_summary():
    class SensitiveSummaryVLM(FakeVLM):
        def understand(self, image, cache_key=None):
            self.understand_calls.append((image, cache_key))
            return {
                "topic": "web",
                "summary": "客服电话 13812345678",
                "content_type": "web",
                "key_points": [],
            }

    pipeline = _make_pipeline(vlm=SensitiveSummaryVLM(), sensitive=SensitiveFilter())
    assert pipeline.understand_screen() == {"topic": "", "sensitive": True}
