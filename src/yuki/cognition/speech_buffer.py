import numpy as np

from yuki.cognition.vad import FsmnVadBackend
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.speech_buffer")


class SpeechBuffer:
    """Accumulates audio frames and flushes voiced utterances detected by a VAD backend."""

    def __init__(
        self,
        frame_ms: int = 20,
        sample_rate: int = 16000,
        vad=None,
        on_utterance=None,
        vad_interval_ms: int = 400,
        end_silence_ms: int | None = None,
        max_utterance_s: float = 10.0,
        silent_frames: int | None = None,
    ) -> None:
        self._sample_rate = int(sample_rate)
        self._frame_ms = int(frame_ms)
        self._vad = vad
        self._vad_interval_samples = max(
            1,
            int(self._sample_rate * max(1, int(vad_interval_ms)) / 1000),
        )
        if end_silence_ms is None:
            end_silence_ms = int(silent_frames * self._frame_ms) if silent_frames else 800
        self._end_silence_ms = max(0, int(end_silence_ms))
        self._max_samples = max(1, int(float(max_utterance_s) * self._sample_rate))
        self.on_utterance = on_utterance
        self._audio: list[np.ndarray] = []
        self._speech: list[np.ndarray] = []
        self._segments: list[list[int]] = []
        self._audio_samples = 0
        self._last_vad_samples = 0

    def _get_vad(self):
        if self._vad is None:
            self._vad = FsmnVadBackend(sample_rate=self._sample_rate)
        return self._vad

    def add_frame(self, samples: np.ndarray) -> None:
        frame = np.asarray(samples, dtype=np.float32)
        if len(frame) == 0:
            return
        self._audio.append(frame)
        self._audio_samples += len(frame)
        samples_since_vad = self._audio_samples - self._last_vad_samples
        if (
            samples_since_vad < self._vad_interval_samples
            and self._audio_samples < self._max_samples
        ):
            return
        self._run_vad()
        if self._audio_samples >= self._max_samples:
            self._flush()

    def _run_vad(self) -> None:
        audio = self._joined_audio()
        if len(audio) == 0:
            return
        self._last_vad_samples = self._audio_samples
        try:
            segments = self._get_vad().segments(audio)
        except Exception:
            logger.warning("vad frame skipped", exc_info=True)
            return
        self._segments = self._normalize_segments(segments, len(audio))
        self._speech = self._speech_from_segments(audio, self._segments)
        if not self._speech:
            return
        total_ms = int(round(len(audio) / max(1, self._sample_rate) * 1000))
        last_end_ms = self._segments[-1][1] if self._segments else 0
        if total_ms - last_end_ms >= self._end_silence_ms:
            self._flush()

    def _normalize_segments(self, segments, sample_count: int) -> list[list[int]]:
        duration_ms = int(round(sample_count / max(1, self._sample_rate) * 1000))
        normalized: list[list[int]] = []
        for item in segments or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                start_ms = int(item[0])
                end_ms = int(item[1])
            except (TypeError, ValueError):
                continue
            if start_ms < 0 or end_ms <= start_ms:
                continue
            normalized.append([start_ms, min(end_ms, duration_ms)])
        return normalized

    def _speech_from_segments(
        self, audio: np.ndarray, segments: list[list[int]]
    ) -> list[np.ndarray]:
        speech: list[np.ndarray] = []
        for start_ms, end_ms in segments:
            start = int(start_ms * self._sample_rate / 1000)
            end = int(end_ms * self._sample_rate / 1000)
            if end > start:
                speech.append(audio[start:end])
        return speech

    def _joined_audio(self) -> np.ndarray:
        if not self._audio:
            return np.array([], dtype=np.float32)
        if len(self._audio) == 1:
            return self._audio[0]
        return np.concatenate(self._audio)

    def has_speech(self) -> bool:
        return bool(self._speech)

    def _flush(self) -> None:
        if self._speech and self.on_utterance is not None:
            utterance = np.concatenate(self._speech) if len(self._speech) > 1 else self._speech[0]
            try:
                self.on_utterance(utterance)
            except Exception:
                logger.exception("utterance callback failed")
        self.reset()

    def reset(self) -> None:
        self._audio = []
        self._speech = []
        self._segments = []
        self._audio_samples = 0
        self._last_vad_samples = 0
