import base64
import io
import time
from typing import Callable

from PIL import Image

from yuki.cognition.frame_client import FrameClient
from yuki.cognition.sensitive import SensitiveFilter
from yuki.cognition.speech_buffer import SpeechBuffer
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.pipeline")


def decode_png_b64(png_b64: str) -> Image.Image | None:
    try:
        raw = base64.b64decode(png_b64)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except (ValueError, OSError):
        logger.warning("png decode failed")
        return None


def scroll_band(scroll_percent: float | None) -> str:
    if scroll_percent is None:
        return "unknown"
    percent = min(max(float(scroll_percent), 0.0), 100.0)
    idx = min(int(percent // 25), 3)
    return f"{idx * 25}-{idx * 25 + 25}"


class PerceptionPipeline:
    """纯感知管线：产出结构化理解事件，不产生任何回复。

    发布 event/perception/situation_update 与 event/perception/user_utterance，
    由 Brain（DecisionHub）作为 context 消费并决策是否主动评论。
    """

    def __init__(
        self,
        vlm: VisualUnderstander,
        sensitive_filter: SensitiveFilter,
        stt: SpeechRecognizer,
        frame_client: FrameClient,
        bus,
        speech_buffer: SpeechBuffer | None = None,
        cache_scroll: bool = True,
    ) -> None:
        self._vlm = vlm
        self._sensitive = sensitive_filter
        self._stt = stt
        self._frame_client = frame_client
        self._bus = bus
        self._listening = False
        self._speech_buffer = speech_buffer or SpeechBuffer(
            on_utterance=self._on_utterance
        )

    def _frame_for_payload(self, payload: dict) -> dict:
        frame_id = payload.get("frame_id")
        if frame_id is not None:
            return self._frame_client.get_by_id(frame_id)
        return self._frame_client.get_latest()

    def _on_utterance(self, samples) -> None:
        text = self._stt.recognize(samples, sample_rate=16000)
        if not text:
            return
        self._bus.publish(Topics.USER_UTTERANCE, {
            "text": text, "duration_s": round(len(samples) / 16000, 2), "ts": time.time(),
        })

    def on_content_ready(self, topic: str, payload: dict) -> None:
        frame = self._frame_for_payload(payload)
        if not frame or not frame.get("png") or frame.get("sensitive"):
            return
        image = decode_png_b64(frame["png"])
        if image is None:
            return
        source_id = payload.get("url") or payload.get("title") or "unknown"
        scroll_percent = payload.get("scroll_percent")
        cache_key = (
            f"{source_id}|{scroll_band(scroll_percent)}"
            if scroll_percent is not None
            else source_id
        )
        context = self._vlm.understand(image, cache_key=cache_key)
        text = " ".join([
            context.get("topic", ""),
            context.get("summary", ""),
            " ".join(context.get("key_points", []) or []),
        ])
        if self._sensitive.scan(text):
            self._publish_situation({"topic": "", "sensitive": True, "degraded": True,
                                     "reason": "sensitive"})
            return
        self._publish_situation({
            "source_id": source_id,
            "scroll_band": scroll_band(scroll_percent),
            "topic": context.get("topic", ""),
            "summary": context.get("summary", ""),
            "content_type": context.get("content_type", "unknown"),
            "key_points": context.get("key_points", []),
            "sensitive": False,
            "degraded": context.get("degraded", False),
            "reason": context.get("reason", ""),
        })

    def on_focus_changed(self, topic: str, payload: dict) -> None:
        if payload.get("content_ready_deferred"):
            return
        self.on_content_ready(topic, payload)

    def _publish_situation(self, data: dict) -> None:
        data.setdefault("source_id", "unknown")
        data.setdefault("scroll_band", "unknown")
        data.setdefault("key_points", [])
        data.setdefault("ts", time.time())
        self._bus.publish(Topics.SITUATION_UPDATE, data)

    def on_awake(self, topic: str, payload: dict) -> None:
        self._listening = True
        self._speech_buffer.reset()

    def on_mic(self, topic: str, payload: dict) -> None:
        if not self._listening:
            return
        pcm_b64 = payload.get("pcm", "")
        if not pcm_b64:
            return
        import numpy as np
        raw = base64.b64decode(pcm_b64)
        samples = np.frombuffer(raw, dtype=np.float32)
        self._speech_buffer.add_frame(samples)

    def understand_screen(self) -> dict:
        frame = self._frame_client.get_latest()
        if not frame or not frame.get("png") or frame.get("sensitive"):
            return {"topic": "", "sensitive": True, "degraded": True, "reason": "no_frame"}
        image = decode_png_b64(frame["png"])
        if image is None:
            return {"topic": "", "degraded": True, "reason": "decode_failed"}
        context = self._vlm.understand(image)
        text = " ".join([
            context.get("topic", ""),
            context.get("summary", ""),
            " ".join(context.get("key_points", []) or []),
        ])
        if self._sensitive.scan(text):
            return {"topic": "", "sensitive": True, "degraded": True, "reason": "sensitive"}
        return context

    def warmup_vlm(self) -> None:
        self._vlm.warmup()


def build_pipeline(bus, *, vlm=None, sensitive_filter=None, stt=None,
                   frame_client=None, speech_buffer=None) -> PerceptionPipeline:
    pipeline = PerceptionPipeline(
        vlm=vlm or VisualUnderstander(),
        sensitive_filter=sensitive_filter or SensitiveFilter(),
        stt=stt or SpeechRecognizer(),
        frame_client=frame_client or FrameClient(bus),
        bus=bus,
        speech_buffer=speech_buffer,
    )
    bus.subscribe(Topics.CONTENT_READY, pipeline.on_content_ready)
    bus.subscribe(Topics.FOCUS_CHANGED, pipeline.on_focus_changed)
    bus.subscribe(Topics.AWAKE, pipeline.on_awake)
    bus.subscribe(Topics.MIC, pipeline.on_mic)
    return pipeline
