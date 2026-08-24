import threading
import time
from collections import deque
from collections.abc import Callable

from yuki.cognition.speech_buffer import SpeechBuffer
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.asr_session")


class AsrSession:
    """ASR 状态机：idle→listening→speaking/processing→idle，含 pre-roll 与超时回退。

    从 PerceptionPipeline 拆出；不依赖 bus/topic，纯逻辑可测。
    """

    def __init__(
        self,
        *,
        listen_timeout_s: float = 10.0,
        listen_window_s: float = 5.0,
        pre_roll_s: float = 1.2,
        audio_frame_ms: int = 20,
        clock: Callable[[], float] = time.monotonic,
        speech_buffer: SpeechBuffer | None = None,
        on_utterance=None,
    ) -> None:
        self._listen_timeout_s = max(0.0, float(listen_timeout_s))
        self._listen_window_s = max(0.0, float(listen_window_s))
        self._clock = clock
        self._lock = threading.RLock()
        self._speech_buffer = speech_buffer or SpeechBuffer(on_utterance=on_utterance)
        if speech_buffer is not None and on_utterance is not None:
            try:
                self._speech_buffer.on_utterance = on_utterance
            except Exception:
                logger.warning("speech buffer utterance rewire failed", exc_info=True)
        pre_roll_frames = int(max(0.0, float(pre_roll_s)) * 1000 / max(1, int(audio_frame_ms)))
        self._pre_roll: deque = deque(maxlen=pre_roll_frames)
        self._state = "idle"
        self._listening = False
        self._generation = 0
        self._session_id: int | None = None
        self._last_activity = self._clock()
        self._current_timeout_s = self._listen_timeout_s

    @property
    def state(self) -> str:
        return self._state

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def speech_buffer(self) -> SpeechBuffer:
        return self._speech_buffer

    def begin(self) -> list:
        """on_awake：idle→listening，返回需回灌的 pre-roll 帧。"""
        with self._lock:
            if self._state != "idle":
                return []
            self._generation += 1
            self._session_id = self._generation
            self._state = "listening"
            self._listening = True
            self._last_activity = self._clock()
            self._current_timeout_s = self._listen_timeout_s
            pre_roll = list(self._pre_roll)
            self._speech_buffer.reset()
            return pre_roll

    def feed(self, samples) -> bool:
        """on_mic：累积 pre-roll；listening 时同时入 speech buffer，返回是否 listening。"""
        with self._lock:
            self._pre_roll.append(samples)
            listening = self._listening
        if listening:
            self.add_frame(samples)
        return listening

    def add_frame(self, samples) -> None:
        self._speech_buffer.add_frame(samples)
        with self._lock:
            if not self._listening:
                return
            if self.has_speech():
                self._state = "speaking"
                self._last_activity = self._clock()

    def has_speech(self) -> bool:
        has_speech = getattr(self._speech_buffer, "has_speech", None)
        if callable(has_speech):
            return bool(has_speech())
        speech = getattr(self._speech_buffer, "_speech", None)
        return bool(speech)

    def consume_utterance(self, session_id: int | None) -> bool:
        """_on_utterance 前段：确认当前会话后置为 processing。"""
        with self._lock:
            if not self._listening or self._session_id != session_id:
                return False
            self._state = "processing"
            self._last_activity = self._clock()
            return True

    def is_current(self, session_id: int | None) -> bool:
        with self._lock:
            if session_id is None:
                return False
            return self._listening and self._session_id == session_id

    def finish(self, session_id: int | None) -> None:
        """_finish_stt_session：识别完成后回 listening 或保持 speaking。"""
        with self._lock:
            if session_id is not None and self._session_id != session_id:
                return
            if not self._listening:
                return
            if self.has_speech():
                self._state = "speaking"
                return
            self._state = "listening"
            self._last_activity = self._clock()
            self._current_timeout_s = self._listen_window_s

    def check_due(self) -> bool:
        with self._lock:
            if self._state != "listening":
                return False
            if self._current_timeout_s <= 0:
                return False
            if self._clock() - self._last_activity < self._current_timeout_s:
                return False
            self.return_to_idle()
            return True

    def return_to_idle(self) -> None:
        with self._lock:
            self._generation += 1
            self._session_id = None
            self._state = "idle"
            self._listening = False
            self._current_timeout_s = self._listen_timeout_s
            self._speech_buffer.reset()

    def reset(self) -> None:
        with self._lock:
            self._pre_roll.clear()
            self._speech_buffer.reset()
