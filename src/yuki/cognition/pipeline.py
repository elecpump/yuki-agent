import time

from yuki.bus import MessageBus
from yuki.cognition.frame_client import FrameClient
from yuki.cognition.l1 import L1Engine
from yuki.cognition.sensitive import SensitiveFilter
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.pipeline")


class PerceptionPipeline:
    """感知理解管线：前台变化→读屏情境、唤醒→L1 快答、音频→STT。"""

    def __init__(self, vlm, sensitive_filter, stt, l1, frame_client, bus=None) -> None:
        self._vlm = vlm
        self._sensitive = sensitive_filter
        self._stt = stt
        self._l1 = l1
        self._frame_client = frame_client
        self._bus = bus
        self._last_context: dict = {}
        self._listening = False

    def on_focus_changed(self, topic: str, payload: dict) -> None:
        frame = self._frame_client.get_latest()
        if not frame:
            return
        if frame.get("sensitive"):
            self._last_context = {"topic": "", "sensitive": True}
            return
        cache_key = f"{payload.get('title', '')}|{payload.get('url', '')}"
        context = self._vlm.understand(frame.get("png"), cache_key=cache_key)
        if self._sensitive.scan(context.get("summary", "") + context.get("topic", "")):
            self._last_context = {"topic": "", "sensitive": True}
            return
        self._last_context = context

    def on_awake(self, topic: str, payload: dict) -> None:
        self._listening = True
        reply = self._l1.reply("", context=self._last_context)
        self._bus_publish_reply(reply)

    def on_mic(self, topic: str, payload: dict) -> None:
        if not self._listening:
            return
        text = self._stt.recognize_base64(payload.get("pcm", ""), payload.get("sample_rate", 16000))
        if not text:
            return
        reply = self._l1.reply(text, context=self._last_context)
        self._bus_publish_reply(reply)

    def understand_screen(self) -> dict:
        frame = self._frame_client.get_latest()
        if not frame or frame.get("sensitive"):
            return {"topic": "", "sensitive": True}
        return self._vlm.understand(frame.get("png"))

    def _bus_publish_reply(self, text: str) -> None:
        self._bus.publish(Topics.REPLY, {"text": text, "ts": time.time()})


def build_pipeline(bus: MessageBus, *, vlm=None, sensitive_filter=None, stt=None, l1=None, frame_client=None) -> PerceptionPipeline:
    """组装感知理解管线并订阅事件。测试注入 fake，默认懒加载真实组件。"""
    pipeline = PerceptionPipeline(
        vlm=vlm or VisualUnderstander(),
        sensitive_filter=sensitive_filter or SensitiveFilter(),
        stt=stt or SpeechRecognizer(),
        l1=l1 or L1Engine(),
        frame_client=frame_client or FrameClient(bus),
        bus=bus,
    )
    bus.subscribe(Topics.FOCUS_CHANGED, pipeline.on_focus_changed)
    bus.subscribe(Topics.AWAKE, pipeline.on_awake)
    bus.subscribe(Topics.MIC, pipeline.on_mic)
    return pipeline
