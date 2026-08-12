import numpy as np

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.speech_buffer")


class SpeechBuffer:
    """音频帧累积器：VAD 判定语音/静音，静音超时或最大时长触发整段 utterance。

    vad 可注入 fake（纯逻辑可测）；默认懒加载 webrtcvad。
    """

    def __init__(
        self,
        frame_ms: int = 20,
        sample_rate: int = 16000,
        vad=None,
        on_utterance=None,
        silent_frames: int = 15,
        max_utterance_s: float = 10.0,
    ) -> None:
        self._frame_len = int(sample_rate * frame_ms / 1000)
        self._vad = vad
        self._sample_rate = sample_rate
        self._silent_frames = silent_frames
        self._max_frames = int(max_utterance_s * 1000 / frame_ms)
        self.on_utterance = on_utterance
        self._speech: list[np.ndarray] = []
        self._silence_count = 0

    def _get_vad(self):
        if self._vad is None:
            import webrtcvad
            self._vad = webrtcvad.Vad(0)
        return self._vad

    def add_frame(self, samples: np.ndarray) -> None:
        vad = self._get_vad()
        frame = np.asarray(samples, dtype=np.float32)
        # 20ms@16k = 320 采样；webrtcvad 需要 int16 + 精确长度
        pcm = (frame * 32767).astype(np.int16).tobytes()
        try:
            is_speech = bool(vad.is_speech(pcm, self._sample_rate))
        except Exception:
            logger.warning("vad frame skipped", exc_info=True)
            return
        if is_speech:
            self._speech.append(frame)
            self._silence_count = 0
        else:
            if self._speech:
                self._silence_count += 1
                if self._silence_count >= self._silent_frames:
                    self._flush()
            # 静音帧本身不累积
        if len(self._speech) >= self._max_frames:
            self._flush()

    def _flush(self) -> None:
        if self._speech and self.on_utterance is not None:
            utterance = np.concatenate(self._speech) if len(self._speech) > 1 else self._speech[0]
            try:
                self.on_utterance(utterance)
            except Exception:
                logger.exception("utterance callback failed")
        self._speech = []
        self._silence_count = 0

    def reset(self) -> None:
        self._speech = []
        self._silence_count = 0
