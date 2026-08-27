import base64
import binascii
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from yuki.config import WakeWordConfig
from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.perception.wake_word")


class WakeWordFrameAdapter:
    """Converts audio/mic float32 frames into int16 chunks for wake-word inference."""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        chunk_ms: int = 80,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.chunk_ms = chunk_ms
        self._chunk_len = int(sample_rate * chunk_ms / 1000)
        self._pending = np.array([], dtype=np.float32)

    def add_payload(self, payload: dict) -> list[np.ndarray]:
        if int(payload.get("sample_rate") or self.sample_rate) != self.sample_rate:
            logger.warning(
                "wake word frame skipped: sample rate mismatch",
                sample_rate=payload.get("sample_rate"),
            )
            return []
        native = payload.get("samples")
        if native is not None:
            frame = np.asarray(native, dtype=np.float32).reshape(-1)
        else:
            pcm_b64 = payload.get("pcm", "")
            if not pcm_b64:
                return []
            try:
                raw = base64.b64decode(pcm_b64)
            except (TypeError, ValueError, binascii.Error):
                logger.warning("wake word frame decode failed")
                return []
            frame = np.frombuffer(raw, dtype=np.float32)
        if frame.size == 0:
            return []
        self._pending = np.concatenate([self._pending, frame])
        chunks: list[np.ndarray] = []
        while self._pending.size >= self._chunk_len:
            chunk = self._pending[: self._chunk_len]
            self._pending = self._pending[self._chunk_len :]
            chunks.append((np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16))
        return chunks

    def reset(self) -> None:
        self._pending = np.array([], dtype=np.float32)


class OpenWakeWordBackend:
    """Lazy wrapper around openWakeWord so tests can inject a tiny fake backend."""

    def __init__(self, *, model_path: str = "") -> None:
        self._model_path = model_path
        self._model = None

    def _load(self):
        if self._model is None:
            from openwakeword.model import Model

            kwargs: dict[str, Any] = {}
            if self._model_path:
                kwargs["wakeword_models"] = [self._model_path]
            self._model = Model(**kwargs)
        return self._model

    def predict(self, pcm: np.ndarray) -> dict[str, float]:
        return dict(self._load().predict(pcm))


class WakeWordDetector:
    """Subscribes to audio/mic and publishes event/awake when a wake model fires."""

    def __init__(
        self,
        bus,
        config: WakeWordConfig,
        *,
        backend=None,
        adapter: WakeWordFrameAdapter | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._bus = bus
        self._config = config
        self._backend = backend
        self._adapter = adapter or WakeWordFrameAdapter(chunk_ms=config.chunk_ms)
        self._clock = clock
        self._wall_clock = wall_clock
        self._started = False
        self._last_wake_monotonic: float | None = None
        self._last_score = 0.0
        self._backend_failed = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._bus.subscribe(Topics.MIC, self.on_mic)

    def stop(self) -> None:
        self._started = False
        self._adapter.reset()

    def health(self) -> dict:
        return {
            "enabled": self._config.enabled,
            "started": self._started,
            "last_score": self._last_score,
            "model_path": self._config.model_path,
            "failed": self._backend_failed,
        }

    def on_mic(self, topic: str, payload: dict) -> None:
        if not self._started or not self._config.enabled:
            return
        if self._backend_failed:
            return
        for chunk in self._adapter.add_payload(payload):
            try:
                self._handle_scores(self._predict(chunk))
            except Exception:
                logger.exception("wake word inference failed, disabling detector")
                self._backend_failed = True
                return

    def _predict(self, pcm: np.ndarray) -> dict[str, float]:
        backend = self._backend
        if backend is None:
            backend = self._backend = OpenWakeWordBackend(model_path=self._config.model_path)
        scores = backend.predict(pcm)
        if isinstance(scores, (int, float)):
            return {"wake": float(scores)}
        return {str(key): float(value) for key, value in dict(scores).items()}

    def _handle_scores(self, scores: dict[str, float]) -> None:
        if not scores:
            return
        model, score = max(scores.items(), key=lambda item: item[1])
        self._last_score = score
        if score < self._config.threshold:
            return
        now = self._clock()
        if (
            self._last_wake_monotonic is not None
            and now - self._last_wake_monotonic < self._config.refractory_s
        ):
            return
        self._last_wake_monotonic = now
        self._bus.publish(
            Topics.AWAKE,
            {
                "source": "wake_word",
                "ts": self._wall_clock(),
                "score": score,
                "model": model,
            },
        )
