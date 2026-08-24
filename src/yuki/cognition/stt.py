import base64
import time
from collections.abc import Callable

import numpy as np

from yuki.cognition.load_gate import LoadGate
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.stt")


class SpeechRecognizer:
    """SenseVoice-Small 语音识别：中英混合，带情感/事件标注。"""

    def __init__(
        self,
        model=None,
        sample_rate: int = 16000,
        *,
        enabled: bool = True,
        retry_window_s: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._model = model
        self._sample_rate = sample_rate
        self._loaded = model is not None
        self._gate = LoadGate(
            enabled=enabled,
            retry_window_s=retry_window_s,
            clock=clock or time.monotonic,
        )

    def _load(self) -> None:
        if self._loaded:
            return
        error = self._gate.error_message()
        if error:
            raise RuntimeError(error)
        try:
            from funasr import AutoModel
            self._model = AutoModel(model="iic/SenseVoiceSmall")
            self._loaded = True
            self._gate.mark_success()
        except Exception:
            self._gate.mark_failure()
            raise

    def _infer(self, samples: np.ndarray, sample_rate: int) -> str:
        self._load()
        result = self._model(input=samples.astype(np.float32), fs=sample_rate)
        if isinstance(result, list) and result:
            return str(result[0].get("text", ""))
        return ""

    def recognize(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        if samples is None or len(samples) == 0:
            return ""
        if not self._loaded and self._gate.error_message() is not None:
            return ""
        try:
            return self._infer(samples, sample_rate)
        except Exception:
            logger.exception("stt inference failed")
            return ""

    def recognize_base64(self, pcm_b64: str, sample_rate: int = 16000) -> str:
        if not pcm_b64:
            return ""
        try:
            raw = base64.b64decode(pcm_b64)
            samples = np.frombuffer(raw, dtype=np.float32)
        except (ValueError, base64.binascii.Error):
            logger.warning("invalid pcm base64")
            return ""
        return self.recognize(samples, sample_rate)

    def health(self) -> dict:
        return {"loaded": self._loaded, **self._gate.health()}
