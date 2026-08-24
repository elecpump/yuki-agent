import base64
import binascii
import io
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from PIL import Image

from yuki.cognition.asr_session import AsrSession
from yuki.cognition.frame_client import FrameClient
from yuki.cognition.situation import (
    build_situation_update,
    cache_key_for,
    deep_cache_key_for,
    frame_id_for,
    scroll_band,
    source_id_for,
)
from yuki.cognition.speech_buffer import SpeechBuffer
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.text_client import TextClient
from yuki.cognition.vlm import VisualUnderstander
from yuki.logger import get_logger, get_situation_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.pipeline")


def decode_png_b64(png_b64: str) -> Image.Image | None:
    try:
        raw = base64.b64decode(png_b64)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except (ValueError, OSError):
        logger.warning("png decode failed")
        return None


class _LatestJobWorker:
    """Runs slow perception work outside bus handler threads, keeping only the latest pending job."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._condition = threading.Condition()
        self._pending: tuple[Callable[..., None], tuple[Any, ...], dict[str, Any]] | None = None
        self._closed = False
        self._thread: threading.Thread | None = None

    def submit(self, fn: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        with self._condition:
            if self._closed:
                return
            self._pending = (fn, args, kwargs)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name=self._name)
                self._thread.start()
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._pending is None and self._closed:
                    return
                job = self._pending
                self._pending = None
            fn, args, kwargs = job
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception("background perception job failed", worker=self._name)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


class DeepRateLimiter:
    """Single arbiter for expensive VLM deep understanding attempts."""

    def __init__(self, interval_s: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval_s = max(0.0, float(interval_s))
        self._clock = clock
        self._last_deep: float | None = None
        self._lock = threading.Lock()

    def allow(self, *, bypass: bool = False, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        with self._lock:
            if bypass or self._last_deep is None or now - self._last_deep >= self._interval_s:
                self._last_deep = now
                return True
            return False


class PerceptionPipeline:
    """纯感知管线：产出结构化理解事件，不产生任何回复。

    发布 event/perception/situation_update 与 event/perception/user_utterance，
    由 Brain（DecisionHub）作为 context 消费并决策是否主动评论。
    """

    def __init__(
        self,
        vlm: VisualUnderstander,
        stt: SpeechRecognizer,
        frame_client: FrameClient,
        bus,
        speech_buffer: SpeechBuffer | None = None,
        cache_scroll: bool = True,
        text_client: TextClient | None = None,
        text_summary_chars: int = 500,
        text_key_point_chars: int = 160,
        deep_interval_s: float = 300.0,
        user_bypass_rate_limit: bool = True,
        listen_timeout_s: float = 10.0,
        listen_window_s: float = 5.0,
        pre_roll_s: float = 1.2,
        audio_sample_rate: int = 16000,
        audio_frame_ms: int = 20,
        clock: Callable[[], float] = time.monotonic,
        start_deep_timer: bool = True,
        start_asr_watchdog: bool = True,
    ) -> None:
        self._vlm = vlm
        self._stt = stt
        self._frame_client = frame_client
        self._text_client = text_client or TextClient(bus)
        self._bus = bus
        self._text_summary_chars = text_summary_chars
        self._text_key_point_chars = text_key_point_chars
        self._user_bypass_rate_limit = user_bypass_rate_limit
        self._clock = clock
        self._deep_interval_s = max(0.0, float(deep_interval_s))
        self._deep_limiter = DeepRateLimiter(self._deep_interval_s, clock=clock)
        self._latest_deep_payload: dict | None = None
        self._deep_results: dict[str, dict] = {}
        self._deep_timer_stop = threading.Event()
        self._deep_timer_thread: threading.Thread | None = None
        self._asr = AsrSession(
            listen_timeout_s=listen_timeout_s,
            listen_window_s=listen_window_s,
            pre_roll_s=pre_roll_s,
            audio_frame_ms=audio_frame_ms,
            clock=clock,
            speech_buffer=speech_buffer,
            on_utterance=self._on_utterance,
        )
        self._listen_timeout_s = max(0.0, float(listen_timeout_s))
        self._listen_window_s = max(0.0, float(listen_window_s))
        self._asr_lock = threading.RLock()  # 兼容旧测试访问
        self._asr_watchdog_stop = threading.Event()
        self._asr_watchdog_thread: threading.Thread | None = None
        self._audio_sample_rate = int(audio_sample_rate)
        self._audio_frame_ms = int(audio_frame_ms)
        self._fast_worker = _LatestJobWorker("yuki-cognition-fast_worker")
        self._deep_worker = _LatestJobWorker("yuki-cognition-deep_worker")
        self._stt_worker = _LatestJobWorker("yuki-cognition-stt")
        if start_deep_timer:
            self._start_deep_timer()
        if start_asr_watchdog:
            self._start_asr_watchdog()

    def _frame_for_payload(self, payload: dict) -> dict:
        return self._frame_client.get_latest()

    def _frame_matches_observation(self, payload: dict, frame: dict | None) -> bool:
        payload_hwnd = self._int_or_none(payload.get("hwnd"))
        frame_hwnd = self._int_or_none(frame.get("hwnd")) if frame else None
        if payload_hwnd is not None and frame_hwnd is not None:
            return payload_hwnd == frame_hwnd
        return True

    @staticmethod
    def _int_or_none(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _on_utterance(self, samples) -> None:
        session_id = self._asr.session_id
        if not self._asr.consume_utterance(session_id):
            return
        self._stt_worker.submit(self._recognize_utterance, samples, session_id)

    def _recognize_utterance(self, samples, session_id: int | None = None) -> None:
        text = ""
        try:
            text = self._stt.recognize(samples, sample_rate=self._audio_sample_rate)
        except Exception:
            logger.exception("stt recognition failed")
        finally:
            should_publish = bool(text) and self._asr.is_current(session_id)
            try:
                if should_publish:
                    self._bus.publish(Topics.USER_UTTERANCE, {
                        "text": text,
                        "duration_s": round(len(samples) / self._audio_sample_rate, 2),
                        "ts": time.time(),
                    })
            finally:
                self._asr.finish(session_id)

    def _process_content_ready_fast(self, topic: str, payload: dict) -> None:
        frame = self._frame_for_payload(payload)
        if frame_id_for(payload, frame or {}) is None:
            return
        context = self._understand_observation_fast(payload, frame)
        if context is None:
            return
        self._publish_situation(
            build_situation_update(
                payload,
                frame or {},
                context,
                layer="fast",
                confidence=context.get("confidence", 0.6),
            )
        )

    def _process_content_ready_deep_if_needed(self, topic: str, payload: dict) -> None:
        frame = self._frame_for_payload(payload)
        if frame_id_for(payload, frame or {}) is None:
            return
        evidence = self._safe_text_evidence(payload, frame)
        if not self._needs_deep(evidence):
            return
        self._process_content_ready_deep(topic, payload, frame=frame, bypass=False)

    def _process_content_ready_deep(
        self,
        topic: str,
        payload: dict,
        *,
        frame: dict | None = None,
        bypass: bool = False,
    ) -> dict | None:
        if not self._deep_limiter.allow(bypass=bypass):
            logger.info(
                "deep understanding skipped by rate limit",
                source_id=source_id_for(payload),
            )
            return None
        frame = self._frame_for_payload(payload) if frame is None else frame
        if frame_id_for(payload, frame or {}) is None:
            return None
        if not self._frame_matches_observation(payload, frame):
            logger.info(
                "deep understanding skipped: latest frame window mismatch",
                source_id=source_id_for(payload),
            )
            return None
        context = self._understand_observation_deep(payload, frame)
        if context is None:
            context = {"topic": "", "summary": "", "content_type": "unknown",
                       "key_points": [], "degraded": True, "reason": "no_frame"}
        update = build_situation_update(
            payload,
            frame or {},
            context,
            layer="deep",
            confidence=0.0 if context.get("degraded") else 0.85,
            reason=context.get("reason", ""),
        )
        self._deep_results[source_id_for(payload)] = update
        self._publish_situation(update)
        return update

    def _safe_text_evidence(self, payload: dict, frame: dict | None) -> dict:
        try:
            return self._text_client.get_for_observation(payload, frame)
        except Exception:
            logger.exception("fast text evidence failed")
            return {"text": "", "degraded": True, "reason": "text_failed"}

    def _understand_observation_fast(self, payload: dict, frame: dict | None) -> dict | None:
        evidence = self._safe_text_evidence(payload, frame)
        text = str(evidence.get("text", "") or "")
        if text:
            return self._context_from_text(evidence, payload)

        return {
            "topic": "",
            "summary": "",
            "content_type": "unknown",
            "key_points": [],
            "degraded": True,
            "reason": evidence.get("reason", "no_text") or "no_text",
            "confidence": 0.0,
        }

    def _understand_observation_deep(self, payload: dict, frame: dict | None) -> dict | None:
        if not frame or not frame.get("png"):
            return None
        image = decode_png_b64(frame["png"])
        if image is None:
            return {"topic": "", "degraded": True, "reason": "decode_failed"}
        cache_key = deep_cache_key_for(payload)
        return self._vlm.understand(image, cache_key=cache_key)

    def _understand_observation(self, payload: dict, frame: dict | None) -> dict | None:
        context = self._understand_observation_fast(payload, frame)
        if context is not None and not context.get("degraded"):
            return context
        deep_context = self._understand_observation_deep(payload, frame)
        return deep_context if deep_context is not None else context

    def _context_from_text(self, evidence: dict, payload: dict) -> dict:
        text = self._normalize_text(str(evidence.get("text", "")))
        title = str(evidence.get("title") or payload.get("title") or "").strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        topic = self._topic_from_text(title, lines)
        summary = text[: self._text_summary_chars]
        points = [
            line[: self._text_key_point_chars]
            for line in lines
            if self._looks_like_key_point(line)
        ][:5]
        if not points:
            points = [line[: self._text_key_point_chars] for line in lines[:5]]
        return {
            "topic": topic,
            "summary": summary,
            "content_type": f"text/{evidence.get('source', 'unknown')}",
            "key_points": points,
            "confidence": 0.55 if evidence.get("source") == "ocr" else 0.6,
            "degraded": False,
            "reason": f"text_{evidence.get('source', 'unknown')}",
        }

    def _normalize_text(self, text: str) -> str:
        text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _topic_from_text(self, title: str, lines: list[str]) -> str:
        if title:
            return title[:80]
        for line in lines:
            if 4 <= len(line) <= 80:
                return line[:80]
        return (lines[0] if lines else "")[:80]

    def _looks_like_key_point(self, line: str) -> bool:
        stripped = line.lstrip()
        if stripped.startswith(("#", "-", "*", "•")):
            return True
        return bool(re.match(r"^\d+[.)]", stripped))

    def on_content_ready(self, topic: str, payload: dict) -> None:
        payload = dict(payload)
        self._latest_deep_payload = payload
        self._fast_worker.submit(self._process_content_ready_fast, topic, payload)
        self._deep_worker.submit(self._process_content_ready_deep_if_needed, topic, payload)

    def on_focus_changed(self, topic: str, payload: dict) -> None:
        if payload.get("content_ready_deferred"):
            return
        self.on_content_ready(topic, payload)

    def _publish_situation(self, data: dict) -> None:
        data.setdefault("source_id", "unknown")
        data.setdefault("scroll_band", "unknown")
        data.setdefault("key_points", [])
        data.setdefault("layer", "fast")
        data.setdefault("confidence", 0.0)
        data.setdefault("ts", time.time())
        self._bus.publish(Topics.SITUATION_UPDATE, data)
        get_situation_logger().info(
            "situation",
            **{
                key: data[key]
                for key in ("source_id", "topic", "summary", "layer", "degraded")
                if key in data
            },
        )

    def on_awake(self, topic: str, payload: dict) -> None:
        pre_roll = self._asr.begin()
        for samples in pre_roll:
            self._asr.add_frame(samples)

    def on_mic(self, topic: str, payload: dict) -> None:
        pcm_b64 = payload.get("pcm", "")
        if not pcm_b64:
            return
        import numpy as np
        try:
            raw = base64.b64decode(pcm_b64)
        except (TypeError, ValueError, binascii.Error):
            logger.warning("mic frame decode failed")
            return
        samples = np.frombuffer(raw, dtype=np.float32)
        self._asr.feed(samples)

    def check_asr_due(self) -> bool:
        return self._asr.check_due()

    def understand_screen(self) -> dict:
        frame = self._frame_client.get_latest()
        if not frame or not frame.get("png"):
            return {"topic": "", "degraded": True, "reason": "no_frame"}
        payload = {
            "reason": "awake",
            "frame_id": frame.get("frame_id") if frame else None,
            "hwnd": frame.get("hwnd") if frame else None,
        }
        context = self._understand_observation(payload, frame)
        if context is None:
            return {"topic": "", "degraded": True, "reason": "no_frame"}
        return context

    def latest_frame(self) -> dict:
        return self._frame_client.get_latest()

    @property
    def frame_client(self) -> FrameClient:
        return self._frame_client

    @property
    def vlm(self) -> VisualUnderstander:
        return self._vlm

    def current_text(self) -> dict:
        frame = self.latest_frame()
        payload = {
            "reason": "tool_call",
            "frame_id": frame.get("frame_id") if frame else None,
            "hwnd": frame.get("hwnd") if frame else None,
        }
        return self._safe_text_evidence(payload, frame)

    def understand_screen_deep(self, *, bypass_rate_limit: bool | None = None) -> dict:
        frame = self._frame_client.get_latest()
        payload = {
            "reason": "user_request",
            "frame_id": frame.get("frame_id") if frame else None,
            "hwnd": frame.get("hwnd") if frame else None,
        }
        if frame_id_for(payload, frame or {}) is None:
            return {"topic": "", "degraded": True, "reason": "no_frame"}
        bypass = self._user_bypass_rate_limit if bypass_rate_limit is None else bypass_rate_limit
        if not self._deep_limiter.allow(bypass=bypass):
            return {"topic": "", "degraded": True, "reason": "rate_limited"}
        context = self._understand_observation_deep(payload, frame)
        if context is None:
            return {"topic": "", "degraded": True, "reason": "no_frame"}
        return context

    def check_deep_due(self) -> None:
        payload = self._latest_deep_payload
        if payload is None:
            return
        source_id = source_id_for(payload)
        if source_id in self._deep_results:
            return
        self._deep_worker.submit(self._process_content_ready_deep, Topics.CONTENT_READY, dict(payload))

    def warmup_vlm(self) -> None:
        self._vlm.warmup()

    def close(self) -> None:
        self._asr_watchdog_stop.set()
        if self._asr_watchdog_thread is not None and self._asr_watchdog_thread.is_alive():
            self._asr_watchdog_thread.join(timeout=2.0)
        self._deep_timer_stop.set()
        if self._deep_timer_thread is not None and self._deep_timer_thread.is_alive():
            self._deep_timer_thread.join(timeout=2.0)
        self._fast_worker.close()
        self._deep_worker.close()
        self._stt_worker.close()

    def _needs_deep(self, evidence: dict) -> bool:
        text = str(evidence.get("text", "") or "").strip()
        if not text:
            return True
        if evidence.get("degraded"):
            return True
        confidence = evidence.get("confidence")
        try:
            return float(confidence) < 0.6
        except (TypeError, ValueError):
            return False

    def _start_deep_timer(self) -> None:
        if self._deep_interval_s <= 0:
            return

        def run() -> None:
            while not self._deep_timer_stop.wait(self._deep_interval_s):
                self.check_deep_due()

        self._deep_timer_thread = threading.Thread(
            target=run,
            daemon=True,
            name="yuki-cognition-deep-timer",
        )
        self._deep_timer_thread.start()

    def _start_asr_watchdog(self) -> None:
        if self._listen_timeout_s <= 0 and self._listen_window_s <= 0:
            return

        def run() -> None:
            while not self._asr_watchdog_stop.wait(0.1):
                self.check_asr_due()

        self._asr_watchdog_thread = threading.Thread(
            target=run,
            daemon=True,
            name="yuki-cognition-asr-watchdog",
        )
        self._asr_watchdog_thread.start()


def build_pipeline(bus, *, vlm=None, stt=None,
                   frame_client=None, speech_buffer=None, text_client=None,
                   text_summary_chars: int = 500, text_key_point_chars: int = 160,
                   deep_interval_s: float = 300.0,
                   user_bypass_rate_limit: bool = True,
                   listen_timeout_s: float = 10.0,
                   listen_window_s: float = 5.0,
                   pre_roll_s: float = 1.2) -> PerceptionPipeline:
    pipeline = PerceptionPipeline(
        vlm=vlm or VisualUnderstander(),
        stt=stt or SpeechRecognizer(),
        frame_client=frame_client or FrameClient(bus),
        bus=bus,
        speech_buffer=speech_buffer,
        text_client=text_client,
        text_summary_chars=text_summary_chars,
        text_key_point_chars=text_key_point_chars,
        deep_interval_s=deep_interval_s,
        user_bypass_rate_limit=user_bypass_rate_limit,
        listen_timeout_s=listen_timeout_s,
        listen_window_s=listen_window_s,
        pre_roll_s=pre_roll_s,
    )
    bus.subscribe(Topics.CONTENT_READY, pipeline.on_content_ready)
    bus.subscribe(Topics.FOCUS_CHANGED, pipeline.on_focus_changed)
    bus.subscribe(Topics.AWAKE, pipeline.on_awake)
    bus.subscribe(Topics.MIC, pipeline.on_mic)
    return pipeline
