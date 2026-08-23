import base64
import io
import threading
import time

import numpy as np
from PIL import Image

from yuki.cognition.pipeline import DeepRateLimiter, PerceptionPipeline, build_pipeline, scroll_band
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


class FakeSTT:
    def recognize(self, samples, sample_rate=16000):
        return "你好"


class FakeFrameClient:
    def __init__(self, png=None):
        self.latest = {
            "frame_id": 1,
            "png": _png_b64() if png is None else png,
            "width": 4,
            "height": 4,
            "ts": 1.0,
        }
        self.latest_requests = 0

    def get_latest(self):
        self.latest_requests += 1
        return dict(self.latest)


class FakeSpeechBuffer:
    def __init__(self):
        self.reset_calls = 0
        self.frames = []

    def reset(self):
        self.reset_calls += 1
        self.frames = []

    def add_frame(self, samples):
        self.frames.append(samples)


class FakeTextClient:
    def __init__(self, evidence=None):
        self.evidence = evidence or {}
        self.calls = []

    def get_for_observation(self, observation, frame=None):
        self.calls.append((dict(observation), dict(frame or {})))
        return dict(self.evidence)


def _make_pipeline(
    bus=None,
    vlm=None,
    stt=None,
    frame_client=None,
    speech_buffer=None,
    text_client=None,
    **kwargs,
):
    return build_pipeline(
        bus or FakeBus(),
        vlm=vlm or FakeVLM(),
        stt=stt or FakeSTT(),
        frame_client=frame_client or FakeFrameClient(),
        speech_buffer=speech_buffer,
        text_client=text_client,
        **kwargs,
    )


def _wait_for_topic(bus, topic, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = [p for t, p in bus.published if t == topic]
        if events:
            return events[0]
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {topic}")


def _wait_for_situation_layer(bus, layer, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = [p for t, p in bus.published if t == Topics.SITUATION_UPDATE and p.get("layer") == layer]
        if events:
            return events[0]
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for situation layer {layer}")


def test_pipeline_focus_publishes_situation_update():
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(),                    stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.FOCUS_CHANGED][0]("event/focus_changed",
        {"app": "chrome", "url": "https://x.com/a", "title": "A"})
    payload = _wait_for_situation_layer(bus, "deep")
    assert payload["topic"] == "climate"
    assert payload["source_id"] == "https://x.com/a"
    assert payload["layer"] == "deep"
    assert payload["confidence"] == 0.85
    assert "scroll_band" in payload
    assert payload["degraded"] is False
    # 管线不直接回复
    assert not any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_content_ready_publishes_situation_update():
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(),                    stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.CONTENT_READY][0](
        "event/perception/content_ready",
        {"app": "chrome", "url": "https://x.com/a", "title": "A", "reason": "scroll_idle"},
    )
    payload = _wait_for_situation_layer(bus, "deep")
    assert payload["topic"] == "climate"
    assert payload["source_id"] == "https://x.com/a"
    assert payload["situation_id"] == "frame:1"
    assert payload["frame_id"] == 1
    assert payload["frame_ts"] == 1.0
    assert payload["frame_width"] == 4
    assert payload["frame_height"] == 4
    assert payload["observation_reason"] == "scroll_idle"
    assert payload["cache_key"] == "https://x.com/a"


def test_pipeline_content_ready_uses_latest_frame_not_payload_id():
    bus = FakeBus()
    frame_client = FakeFrameClient()
    build_pipeline(bus, vlm=FakeVLM(),                    stt=FakeSTT(), frame_client=frame_client)

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

    payload = _wait_for_situation_layer(bus, "deep")
    assert frame_client.latest_requests > 0
    assert payload["situation_id"] == "frame:1"
    assert payload["frame_id"] == 1


def test_pipeline_content_ready_uses_text_evidence_before_vlm():
    bus = FakeBus()
    vlm = FakeVLM()
    text_client = FakeTextClient({
        "source": "dom",
        "text": "Document heading\n- first point\n- second point",
        "title": "Document title",
        "url": "https://x.com/doc",
        "confidence": 0.95,
        "degraded": False,
        "reason": "native_dom",
    })
    build_pipeline(
        bus,
        vlm=vlm,
        stt=FakeSTT(),
        frame_client=FakeFrameClient(),
        text_client=text_client,
    )

    bus.subscriptions[Topics.CONTENT_READY][0](
        "event/perception/content_ready",
        {
            "app": "chrome",
            "url": "https://x.com/doc",
            "title": "Document title",
            "reason": "focus_changed",
            "frame_id": 1,
        },
    )

    payload = _wait_for_topic(bus, Topics.SITUATION_UPDATE)
    assert vlm.understand_calls == []
    assert payload["topic"] == "Document title"
    assert payload["layer"] == "fast"
    assert payload["confidence"] == 0.6
    assert payload["content_type"] == "text/dom"
    assert payload["reason"] == "text_dom"
    assert payload["key_points"] == ["- first point", "- second point"]


def test_pipeline_skips_unidentified_frames():
    class UnidentifiedFrameClient:
        def get_latest(self):
            return {
                "png": _png_b64(),
                "width": 4,
                "height": 4,
                "ts": 1.0,
            }

    vlm = FakeVLM()
    bus = FakeBus()
    build_pipeline(
        bus,
        vlm=vlm,
        stt=FakeSTT(),
        frame_client=UnidentifiedFrameClient(),
    )

    bus.subscriptions[Topics.CONTENT_READY][0](
        "event/perception/content_ready",
        {"app": "chrome", "url": "https://x.com/a", "title": "A"},
    )

    assert vlm.understand_calls == []
    assert not any(t == Topics.SITUATION_UPDATE for t, _ in bus.published)


def test_pipeline_ignores_deferred_focus_changed():
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(),                    stt=FakeSTT(), frame_client=FakeFrameClient())
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
    build_pipeline(bus, vlm=FakeVLM(),                    stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert not any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_understand_screen_returns_vlm_context():
    bus = FakeBus()
    pipeline = build_pipeline(bus, vlm=FakeVLM(),                               stt=FakeSTT(), frame_client=FakeFrameClient())
    context = pipeline.understand_screen()
    assert context["topic"] == "climate"
    assert context["content_type"] == "article"
    assert not any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_understand_screen_uses_text_evidence_before_vlm():
    vlm = FakeVLM()
    pipeline = _make_pipeline(
        vlm=vlm,
        text_client=FakeTextClient({
            "source": "uia",
            "text": "Awake document\n1. immediate point",
            "title": "Awake title",
            "url": "",
            "confidence": 0.8,
            "degraded": False,
            "reason": "",
        }),
    )

    context = pipeline.understand_screen()

    assert vlm.understand_calls == []
    assert context["topic"] == "Awake title"
    assert context["content_type"] == "text/uia"
    assert context["key_points"] == ["1. immediate point"]


def test_pipeline_latest_frame_and_current_text_use_clients():
    frame_client = FakeFrameClient()
    frame_client.latest["hwnd"] = 123
    text_client = FakeTextClient({"source": "dom", "text": "current"})
    pipeline = _make_pipeline(frame_client=frame_client, text_client=text_client)

    assert pipeline.latest_frame()["frame_id"] == 1
    evidence = pipeline.current_text()

    assert evidence["text"] == "current"
    observation, frame = text_client.calls[-1]
    assert observation["reason"] == "tool_call"
    assert observation["frame_id"] == 1
    assert observation["hwnd"] == 123
    assert frame["frame_id"] == 1


def test_pipeline_stt_on_mic_publishes_utterance():
    sb = FakeSpeechBuffer()
    bus = FakeBus()
    pipeline = build_pipeline(bus, vlm=FakeVLM(),                               stt=FakeSTT(), frame_client=FakeFrameClient(), speech_buffer=sb)
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    bus.published = []
    pcm = base64.b64encode(np.zeros(320, dtype=np.float32).tobytes()).decode("ascii")
    bus.subscriptions[Topics.MIC][0]("audio/mic", {"pcm": pcm, "sample_rate": 16000, "ts": 0.0})
    assert len(sb.frames) == 1
    sb.on_utterance = pipeline._on_utterance
    sb.on_utterance(np.zeros(320, dtype=np.float32))
    payload = _wait_for_topic(bus, Topics.USER_UTTERANCE)
    assert payload["text"] == "你好"


def test_pipeline_mic_before_awake_is_blocked():
    sb = FakeSpeechBuffer()
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(),                    stt=FakeSTT(), frame_client=FakeFrameClient(), speech_buffer=sb)
    pcm = base64.b64encode(np.zeros(320, dtype=np.float32).tobytes()).decode("ascii")
    bus.subscriptions[Topics.MIC][0]("audio/mic", {"pcm": pcm, "sample_rate": 16000, "ts": 0.0})
    assert sb.frames == []
    assert bus.published == []


def test_pipeline_mic_before_awake_is_kept_as_pre_roll():
    sb = FakeSpeechBuffer()
    bus = FakeBus()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        stt=FakeSTT(),
        frame_client=FakeFrameClient(),
        speech_buffer=sb,
        pre_roll_s=0.04,
    )
    first = np.ones(320, dtype=np.float32)
    second = np.ones(320, dtype=np.float32) * 2
    for samples in (first, second):
        pcm = base64.b64encode(samples.tobytes()).decode("ascii")
        bus.subscriptions[Topics.MIC][0]("audio/mic", {"pcm": pcm, "sample_rate": 16000})

    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})

    assert len(sb.frames) == 2
    np.testing.assert_array_equal(sb.frames[0], first)
    np.testing.assert_array_equal(sb.frames[1], second)
    pipeline.close()


def test_pipeline_awake_resets_speech_buffer():
    sb = FakeSpeechBuffer()
    bus = FakeBus()
    build_pipeline(bus, vlm=FakeVLM(),                    stt=FakeSTT(), frame_client=FakeFrameClient(), speech_buffer=sb)
    sb.frames = [np.zeros(320, dtype=np.float32)]
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert sb.reset_calls == 1
    assert sb.frames == []


def test_pipeline_awake_timeout_returns_to_idle():
    now = [10.0]
    sb = FakeSpeechBuffer()
    pipeline = PerceptionPipeline(
        vlm=FakeVLM(),
        stt=FakeSTT(),
        frame_client=FakeFrameClient(),
        bus=FakeBus(),
        speech_buffer=sb,
        listen_timeout_s=1.0,
        clock=lambda: now[0],
        start_deep_timer=False,
        start_asr_watchdog=False,
    )

    pipeline.on_awake("event/awake", {"source": "hotkey"})
    now[0] += 0.9
    assert pipeline.check_asr_due() is False
    now[0] += 0.2
    assert pipeline.check_asr_due() is True
    assert pipeline._asr_state == "idle"
    assert sb.reset_calls == 2


def test_pipeline_listen_window_timeout_after_empty_stt():
    now = [10.0]

    class EmptySTT(FakeSTT):
        def recognize(self, samples, sample_rate=16000):
            return ""

    pipeline = PerceptionPipeline(
        vlm=FakeVLM(),
        stt=EmptySTT(),
        frame_client=FakeFrameClient(),
        bus=FakeBus(),
        speech_buffer=FakeSpeechBuffer(),
        listen_window_s=0.5,
        clock=lambda: now[0],
        start_deep_timer=False,
        start_asr_watchdog=False,
    )

    pipeline.on_awake("event/awake", {"source": "hotkey"})
    session_id = pipeline._session_id
    pipeline._recognize_utterance(np.zeros(320, dtype=np.float32), session_id)
    assert pipeline._asr_state == "listening"
    now[0] += 0.6
    assert pipeline.check_asr_due() is True
    assert pipeline._asr_state == "idle"


def test_pipeline_discards_stale_stt_result():
    bus = FakeBus()
    pipeline = PerceptionPipeline(
        vlm=FakeVLM(),
        stt=FakeSTT(),
        frame_client=FakeFrameClient(),
        bus=bus,
        speech_buffer=FakeSpeechBuffer(),
        start_deep_timer=False,
        start_asr_watchdog=False,
    )

    pipeline.on_awake("event/awake", {"source": "hotkey"})
    stale_session = pipeline._session_id
    with pipeline._asr_lock:
        pipeline._return_to_idle_locked()
    pipeline._recognize_utterance(np.zeros(320, dtype=np.float32), stale_session)

    assert not any(t == Topics.USER_UTTERANCE for t, _ in bus.published)


def test_pipeline_focus_passes_decoded_pil_image_to_vlm():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm)
    pipeline.on_focus_changed("event/focus", {"title": "t", "url": "u"})
    deadline = time.monotonic() + 1.0
    while not vlm.understand_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    image = vlm.understand_calls[0][0]
    assert isinstance(image, Image.Image)
    assert not isinstance(image, str)


def test_pipeline_focus_cache_key_uses_source_id_scroll_band():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm)
    pipeline.on_focus_changed(
        "event/focus_changed", {"title": "T", "url": "https://x.com/a", "scroll_percent": 30}
    )
    deadline = time.monotonic() + 1.0
    while not vlm.understand_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert vlm.understand_calls[0][1] == "https://x.com/a"


def test_scroll_band_clamps_to_valid_range():
    assert scroll_band(100) == "75-100"
    assert scroll_band(130) == "75-100"
    assert scroll_band(-10) == "0-25"
    assert scroll_band("30") == "25-50"


def test_pipeline_focus_skips_placeholder_frame():
    vlm = FakeVLM()
    bus = FakeBus()
    build_pipeline(bus, vlm=vlm,                    stt=FakeSTT(), frame_client=FakeFrameClient(png=""))
    bus.subscriptions[Topics.FOCUS_CHANGED][0]("event/focus_changed", {"title": "t", "url": "u"})
    fast = _wait_for_situation_layer(bus, "fast")
    assert vlm.understand_calls == []
    assert fast["degraded"] is True
    assert fast["reason"] == "no_text"


def test_pipeline_understand_screen_skips_placeholder_frame():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm, frame_client=FakeFrameClient(png=""))
    assert pipeline.understand_screen() == {"topic": "", "degraded": True, "reason": "no_frame"}
    assert vlm.understand_calls == []


def test_pipeline_focus_publishes_degraded_on_vlm_failure():
    class BoomVLM(FakeVLM):
        def understand(self, image, cache_key=None):
            return {"topic": "", "summary": "", "content_type": "unknown",
                    "key_points": [], "degraded": True, "reason": "inference_failed"}

    bus = FakeBus()
    build_pipeline(bus, vlm=BoomVLM(), stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.FOCUS_CHANGED][0]("event/focus_changed", {"title": "t", "url": "u"})
    event = _wait_for_situation_layer(bus, "deep")
    assert event["degraded"] is True
    assert event["reason"] == "inference_failed"


def test_pipeline_content_ready_does_not_block_on_slow_vlm():
    class BlockingVLM(FakeVLM):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def understand(self, image, cache_key=None):
            self.started.set()
            self.release.wait(timeout=2.0)
            return super().understand(image, cache_key=cache_key)

    bus = FakeBus()
    vlm = BlockingVLM()
    pipeline = build_pipeline(
        bus,
        vlm=vlm,
        stt=FakeSTT(),
        frame_client=FakeFrameClient(),
    )

    started = time.monotonic()
    pipeline.on_content_ready("event/perception/content_ready", {"title": "t", "url": "u"})
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert vlm.started.wait(timeout=1.0)
    fast = _wait_for_situation_layer(bus, "fast")
    assert fast["degraded"] is True
    assert fast["reason"] == "no_text"

    vlm.release.set()
    _wait_for_topic(bus, Topics.SITUATION_UPDATE)


def test_fast_and_deep_workers_do_not_block_each_other():
    class BlockingVLM(FakeVLM):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def understand(self, image, cache_key=None):
            self.started.set()
            self.release.wait(timeout=2.0)
            return super().understand(image, cache_key=cache_key)

    bus = FakeBus()
    vlm = BlockingVLM()
    pipeline = _make_pipeline(bus=bus, vlm=vlm)
    pipeline.on_content_ready("event/perception/content_ready", {"title": "t", "url": "u"})

    fast = _wait_for_situation_layer(bus, "fast")
    assert fast["reason"] == "no_text"
    assert vlm.started.wait(timeout=1.0)
    assert not any(
        t == Topics.SITUATION_UPDATE and p.get("layer") == "deep"
        for t, p in bus.published
    )
    vlm.release.set()
    deep = _wait_for_situation_layer(bus, "deep")
    assert deep["topic"] == "climate"


def test_deep_rate_limiter_blocks_until_interval():
    limiter = DeepRateLimiter(300.0, clock=lambda: 0.0)
    assert limiter.allow(now=1000.0) is True
    assert limiter.allow(now=1299.0) is False
    assert limiter.allow(now=1300.0) is True


def test_content_ready_deep_is_rate_limited():
    bus = FakeBus()
    vlm = FakeVLM()
    pipeline = _make_pipeline(bus=bus, vlm=vlm)

    pipeline.on_content_ready("event/perception/content_ready", {"title": "t", "url": "u"})
    _wait_for_situation_layer(bus, "deep")
    bus.published = []

    pipeline.on_content_ready("event/perception/content_ready", {"title": "t", "url": "u"})
    _wait_for_situation_layer(bus, "fast")
    time.sleep(0.05)

    assert len(vlm.understand_calls) == 1
    assert not any(
        t == Topics.SITUATION_UPDATE and p.get("layer") == "deep"
        for t, p in bus.published
    )


def test_user_requested_deep_bypasses_rate_limit():
    vlm = FakeVLM()
    pipeline = _make_pipeline(vlm=vlm)

    first = pipeline.understand_screen_deep()
    second = pipeline.understand_screen_deep()

    assert first["topic"] == "climate"
    assert second["topic"] == "climate"
    assert len(vlm.understand_calls) == 2


def test_user_requested_deep_can_respect_rate_limit_when_disabled():
    vlm = FakeVLM()
    pipeline = PerceptionPipeline(
        vlm=vlm,
        stt=FakeSTT(),
        frame_client=FakeFrameClient(),
        bus=FakeBus(),
        user_bypass_rate_limit=False,
        start_deep_timer=False,
    )

    first = pipeline.understand_screen_deep()
    second = pipeline.understand_screen_deep()

    assert first["topic"] == "climate"
    assert second["reason"] == "rate_limited"
    assert len(vlm.understand_calls) == 1


def test_periodic_check_fills_missing_deep_result():
    bus = FakeBus()
    vlm = FakeVLM()
    pipeline = _make_pipeline(
        bus=bus,
        vlm=vlm,
        text_client=FakeTextClient({
            "source": "dom",
            "text": "Document heading",
            "title": "Document title",
            "confidence": 0.95,
            "degraded": False,
        }),
    )
    pipeline.on_content_ready(
        "event/perception/content_ready",
        {"title": "Document title", "url": "https://x.com/doc"},
    )
    _wait_for_situation_layer(bus, "fast")
    assert vlm.understand_calls == []

    pipeline.check_deep_due()
    deep = _wait_for_situation_layer(bus, "deep")

    assert deep["topic"] == "climate"
    assert vlm.understand_calls


def test_deep_skips_frame_with_mismatched_window():
    bus = FakeBus()
    vlm = FakeVLM()
    pipeline = _make_pipeline(bus=bus, vlm=vlm)
    frame = {
        "frame_id": 5,
        "png": _png_b64(),
        "width": 4,
        "height": 4,
        "ts": 2.0,
        "hwnd": 999,
    }
    result = pipeline._process_content_ready_deep(
        "event/perception/content_ready",
        {"app": "chrome", "url": "https://x.com/a", "title": "A", "hwnd": 42},
        frame=frame,
        bypass=True,
    )
    assert result is None
    assert vlm.understand_calls == []
    assert not any(t == Topics.SITUATION_UPDATE for t, _ in bus.published)


def test_deep_runs_when_frame_window_matches():
    bus = FakeBus()
    vlm = FakeVLM()
    pipeline = _make_pipeline(bus=bus, vlm=vlm)
    frame = {
        "frame_id": 5,
        "png": _png_b64(),
        "width": 4,
        "height": 4,
        "ts": 2.0,
        "hwnd": 42,
    }
    update = pipeline._process_content_ready_deep(
        "event/perception/content_ready",
        {"app": "chrome", "url": "https://x.com/a", "title": "A", "hwnd": 42},
        frame=frame,
        bypass=True,
    )
    assert update is not None
    assert update["situation_id"] == "frame:5"
    assert vlm.understand_calls


def test_pipeline_utterance_callback_does_not_block_on_slow_stt():
    class BlockingSTT(FakeSTT):
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def recognize(self, samples, sample_rate=16000):
            self.started.set()
            self.release.wait(timeout=2.0)
            return "hello"

    bus = FakeBus()
    stt = BlockingSTT()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        stt=stt,
        frame_client=FakeFrameClient(),
    )
    bus.subscriptions[Topics.AWAKE][0]("event/awake", {"source": "hotkey", "ts": 0.0})

    started = time.monotonic()
    pipeline._on_utterance(np.zeros(320, dtype=np.float32))
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert stt.started.wait(timeout=1.0)
    assert not any(t == Topics.USER_UTTERANCE for t, _ in bus.published)

    stt.release.set()
    _wait_for_topic(bus, Topics.USER_UTTERANCE)
