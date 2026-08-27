import time

import numpy as np

from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.perception.audio")


class AudioFrameSplitter:
    """把输入采样切成固定帧长（纯逻辑）。"""

    def __init__(self, sample_rate: int = 16000, frame_ms: int = 20, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.channels = channels
        self.frame_len = int(sample_rate * frame_ms / 1000) * channels

    def split(self, samples: np.ndarray) -> np.ndarray:
        usable = len(samples) - (len(samples) % self.frame_len)
        if usable == 0:
            return samples[:0]
        return samples[:usable].reshape(-1, self.frame_len)


class AudioCapture:
    """麦克风采集：WASAPI（sounddevice），帧切分后发布 audio/mic。

    本阶段仅采集发布；唤醒词/STT 在 Phase 4 消费。stream_factory 可注入 fake。
    """

    def __init__(
        self,
        bus,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_ms: int = 20,
        splitter: AudioFrameSplitter | None = None,
        stream_factory=None,
    ) -> None:
        self._bus = bus
        self._splitter = splitter or AudioFrameSplitter(sample_rate, frame_ms, channels)
        self._stream_factory = stream_factory
        self._stream = None

    def _default_stream(self, callback):
        import sounddevice as sd

        return sd.InputStream(
            samplerate=self._splitter.sample_rate,
            channels=self._splitter.channels,
            dtype="float32",
            callback=callback,
        )

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            logger.warning("audio status: %s", status)
        samples = np.asarray(indata)[:, 0] if indata.ndim > 1 else np.asarray(indata)
        for frame in self._splitter.split(samples):
            owned = np.asarray(frame, dtype=np.float32).copy()
            owned.setflags(write=False)
            self._bus.publish(
                Topics.MIC,
                {
                    "samples": owned,
                    "sample_rate": self._splitter.sample_rate,
                    "ts": time.time(),
                },
            )

    def start(self) -> None:
        if self._stream is not None:
            return
        factory = self._stream_factory or self._default_stream
        self._stream = factory(self._on_audio)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
