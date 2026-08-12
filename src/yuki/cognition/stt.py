import base64

import numpy as np

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.stt")


class SpeechRecognizer:
    """SenseVoice-Small 语音识别：中英混合，带情感/事件标注。"""

    def __init__(self, model=None, sample_rate: int = 16000) -> None:
        self._model = model
        self._sample_rate = sample_rate
        self._loaded = model is not None

    def _load(self) -> None:
        if self._loaded:
            return
        from funasr import AutoModel
        self._model = AutoModel(model="iic/SenseVoiceSmall")
        self._loaded = True

    def _infer(self, samples: np.ndarray, sample_rate: int) -> str:
        self._load()
        result = self._model(samples.astype(np.float32), sample_rate)
        if isinstance(result, list) and result:
            return str(result[0].get("text", ""))
        return ""

    def recognize(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        if samples is None or len(samples) == 0:
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
