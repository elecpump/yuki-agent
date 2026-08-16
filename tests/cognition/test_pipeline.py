import base64
import io

import numpy as np
from PIL import Image

from yuki.cognition.pipeline import PerceptionPipeline, build_pipeline, scroll_band
from yuki.cognition.sensitive import SensitiveFilter
from yuki.topics import Topics

from tests.fakes import FakeBus


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
    def recognize(self, samples, sample_rate=16000):
        return "你好"


class FakeFrameClient:
    def __init__(self, png=None, frames=None):
        self.latest = {
            "frame_id": 1,
            "png": _png_b64() if png is None else png,
            "width": 4,
            "height": 4,
            "ts": 1.0,
            "sensitive": False,
        }
        self.frames = frames or {}
        self.latest_requests = 0
        self.requested_ids = []

    def get_latest(self):
        self.latest_requests += 1
        return dict(self.latest)

    def get_by_id(self, frame_id):
        self.requested_ids.append(frame_id)
        return dict(self.frames.get(frame_id, {}))


class FakeSpeechBuffer:
    def __init__(self):
        self.reset_calls = 0
        self.frames = []

    def reset(self):
        self.reset_calls += 1
        self.frames = []

    def add_frame(self, samples):
        self.frames.append(samples)


def _make_pipeline(bus=None, vlm=None, sensitive=None, stt=None, frame_client=None, speech_buffer=None):
    return build_pipeline(
        bus or FakeBus(),
        vlm=vlm or FakeVLM(),
        sensitive_filter=sensitive or FakeSensitive(),
        stt=stt or FakeSTT(),
        frame_client=frame_client or FakeFrameClient(),
        speech_buffer=speech_buffer,
    )


def test_pipeline_focus_publishes_situation_update():
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.FOCUS_CHANGED][0]("event/focus_changed",
        {"app": "chrome", "url": "https://x.com/a", "title": "A"})
    events = [t for t, _ in bus.published if t == Topics.SITUATION_UPDATE]
    assert len(events) == 1
    payload = [p for t, p in bus.published if t == Topics.SITUATION_UPDATE][0]
    assert payload["topic"] == "climate"
    assert payload["source_id"] == "https://x.com/a"
    assert "scroll_band" in payload
    assert payload["degraded"] is False
    # 管线不直接回复
    assert not any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_content_ready_publishes_situation_update():
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.CONTENT_READY][0](
        "event/perception/content_ready",
        {"app": "chrome", "url": "https://x.com/a", "title": "A", "reason": "scroll_idle"},
    )
    payload = [p for t, p in bus.published if t == Topics.SITUATION_UPDATE][0]
    assert payload["topic"] == "climate"
    assert payload["source_id"] == "https://x.com/a"
    assert payload["situation_id"] == "frame:1"
    assert payload["frame_id"] == 1
    assert payload["frame_ts"] == 1.0
    assert payload["frame_width"] == 4
    assert payload["frame_height"] == 4
    assert payload["observation_reason"] == "scroll_idle"
    assert payload["cache_key"] == "https://x.com/a"


def test_pipeline_content_ready_reads_bound_frame_id():
    bus = FakeBus()
    frame_client = FakeFrameClient(
        png="",
        frames={
            42: {
                "frame_id": 42,
                "png": _png_b64(),
                "width": 4,
                "height": 4,
                "ts": 1.0,
                "sensitive": False,
            }
        },
    )
    build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=frame_client)

    bus.subscriptions[Topics.CONTENT_READY][0](
        "event/perception/content_ready",
        {
            "app": "chrome",
            "url": "https://x.com/a",
            "title": "A",
            "reason": "scroll_idle",
            "frame_id": 42,
        },
    )

    assert frame_client.latest_requests == 0
    assert frame_client.requested_ids == [42]
    payload = [p for t, p in bus.published if t == Topics.SITUATION_UPDATE][0]
    assert payload["situation_id"] == "frame:42"
    assert payload["frame_id"] == 42


def test_pipeline_ignores_deferred_focus_changed():
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.FOCUS_CHANGED][0](
        "event/focus_changed",
        {
            "app": "chrome",
            "url": "https://x.com/a",
            "title": "A",
            "content_ready_deferred": True,
        },
    )
    assert not any(t == Topics.SITUATION_UPDATE for t, _ in bus.published)


def test_pipeline_awake_no_direct_reply():
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert not any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_understand_screen_returns_vlm_context():
    bus = FakeBus()
    pipeline = build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                              stt=FakeSTT(), frame_client=FakeFrameClient())
    context = pipeline.understand_screen()
    assert context["topic"] == "climate"
    assert context["content_type"] == "article"
    assert not any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_stt_on_mic_publishes_utterance():
    sb = FakeSpeechBuffer()
    bus = FakeBus()
    pipeline = build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                              stt=FakeSTT(), frame_client=FakeFrameClient(), speech_buffer=sb)
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    bus.published = []
    pcm = base64.b64encode(np.zeros(320, dtype=np.float32).tobytes()).decode("ascii")
    bus.subscriptions[Topics.MIC][0]("audio/mic", {"pcm": pcm, "sample_rate": 16000, "ts": 0.0})
    assert len(sb.frames) == 1
    sb.on_utterance = pipeline._on_utterance
    sb.on_utterance(np.zeros(320, dtype=np.float32))
    events = [t for t, _ in bus.published if t == Topics.USER_UTTERANCE]
    assert len(events) == 1
    payload = [p for t, p in bus.published if t == Topics.USER_UTTERANCE][0]
    assert payload["text"] == "你好"


def test_pipeline_mic_before_awake_is_blocked():
    sb = FakeSpeechBuffer()
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=FakeFrameClient(), speech_buffer=sb)
    pcm = base64.b64encode(np.zeros(320, dtype=np.float32).tobytes()).decode("ascii")
    bus.subscriptions[Topics.MIC][0]("audio/mic", {"pcm": pcm, "sample_rate": 16000, "ts": 0.0})
    assert sb.frames == []
    assert bus.published == []


def test_pipeline_awake_resets_speech_buffer():
    sb = FakeSpeechBuffer()
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=FakeFrameClient(), speech_buffer=sb)
    sb.frames = [np.zeros(320, dtype=np.float32)]
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert sb.reset_calls == 1
    assert sb.frames == []


def test_pipeline_focus_passes_decoded_pil_image_to_vlm():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm)
    pipeline.on_focus_changed("event/focus", {"title": "t", "url": "u"})
    image = vlm.understand_calls[0][0]
    assert isinstance(image, Image.Image)
    assert not isinstance(image, str)


def test_pipeline_focus_cache_key_uses_source_id_scroll_band():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm)
    pipeline.on_focus_changed(
        "event/focus_changed", {"title": "T", "url": "https://x.com/a", "scroll_percent": 30}
    )
    assert vlm.understand_calls[0][1] == "https://x.com/a|25-50"


def test_scroll_band_clamps_to_valid_range():
    assert scroll_band(100) == "75-100"
    assert scroll_band(130) == "75-100"
    assert scroll_band(-10) == "0-25"
    assert scroll_band("30") == "25-50"


def test_pipeline_focus_skips_placeholder_frame():
    vlm = FakeVLM()
    bus = FakeBus()
    build_pipeline(bus, vlm=vlm, sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=FakeFrameClient(png=""))
    bus.subscriptions[Topics.FOCUS_CHANGED][0]("event/focus_changed", {"title": "t", "url": "u"})
    assert vlm.understand_calls == []
    assert bus.published == []


def test_pipeline_understand_screen_skips_placeholder_frame():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm, frame_client=FakeFrameClient(png=""))
    assert pipeline.understand_screen() == {"topic": "", "sensitive": True, "degraded": True, "reason": "no_frame"}
    assert vlm.understand_calls == []


def test_pipeline_understand_screen_blocks_sensitive_key_points():
    class SensitiveKeyPointsVLM(FakeVLM):
        def understand(self, image, cache_key=None):
            self.understand_calls.append((image, cache_key))
            return {
                "topic": "理财",
                "summary": "推荐方案",
                "content_type": "web",
                "key_points": ["联系方式 13812345678"],
            }

    pipeline = _make_pipeline(vlm=SensitiveKeyPointsVLM(), sensitive=SensitiveFilter())
    assert pipeline.understand_screen() == {"topic": "", "sensitive": True, "degraded": True, "reason": "sensitive"}


def test_pipeline_focus_blocks_sensitive_key_points():
    class SensitiveKeyPointsVLM(FakeVLM):
        def understand(self, image, cache_key=None):
            self.understand_calls.append((image, cache_key))
            return {
                "topic": "理财",
                "summary": "推荐方案",
                "content_type": "web",
                "key_points": ["联系方式 13812345678"],
            }

    bus = FakeBus()
    build_pipeline(bus, vlm=SensitiveKeyPointsVLM(), sensitive_filter=SensitiveFilter(),
                   stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.FOCUS_CHANGED][0]("event/focus_changed", {"title": "t", "url": "u"})
    events = [p for t, p in bus.published if t == Topics.SITUATION_UPDATE]
    assert len(events) == 1
    assert events[0]["sensitive"] is True
    assert events[0]["degraded"] is True
    assert events[0]["reason"] == "sensitive"


def test_pipeline_focus_publishes_degraded_on_vlm_failure():
    class BoomVLM(FakeVLM):
        def understand(self, image, cache_key=None):
            return {"topic": "", "summary": "", "content_type": "unknown",
                    "key_points": [], "degraded": True, "reason": "inference_failed"}

    bus = FakeBus()
    build_pipeline(bus, vlm=BoomVLM(), sensitive_filter=FakeSensitive(),
                   stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.FOCUS_CHANGED][0]("event/focus_changed", {"title": "t", "url": "u"})
    events = [p for t, p in bus.published if t == Topics.SITUATION_UPDATE]
    assert len(events) == 1
    assert events[0]["degraded"] is True
    assert events[0]["reason"] == "inference_failed"
