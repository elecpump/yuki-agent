import base64
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext

import numpy as np

from yuki.cognition.load_gate import LoadGate
from yuki.cognition.model_registry import ModelRegistry
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
        model_id: str = "iic/SenseVoiceSmall",
        model_dir: str = "",
        device: str = "auto",
        language: str = "auto",
        use_itn: bool = True,
        retry_window_s: float = 60.0,
        clock: Callable[[], float] | None = None,
        model_registry: ModelRegistry | None = None,
        model_name: str = "stt",
    ) -> None:
        self._model = model
        self._sample_rate = sample_rate
        self._model_id = model_id
        self._model_dir = model_dir
        self._device = device
        self._resolved_device: str | None = None
        self._language = language
        self._use_itn = use_itn
        self._loaded = model is not None
        self._gate = LoadGate(
            enabled=enabled,
            retry_window_s=retry_window_s,
            clock=clock or time.monotonic,
        )
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._model_registry = model_registry
        self._model_name = model_name

    def warmup(self) -> None:
        if self._loaded or not self._gate.can_load():
            return

        def _load_thread() -> None:
            try:
                self._load()
            except Exception:
                logger.warning("stt warmup failed", exc_info=True)

        threading.Thread(target=_load_thread, daemon=True, name="yuki-stt-warmup").start()

    def load(self) -> None:
        self._load()

    def unload(self) -> None:
        with self._infer_lock:
            with self._load_lock:
                self._model = None
                self._loaded = False
                self._resolved_device = None
                self._gate.reset()
        self._empty_torch_cache()

    def reload(self) -> None:
        self.unload()
        self.load()

    def set_model_registry(self, registry: ModelRegistry | None, model_name: str = "stt") -> None:
        self._model_registry = registry
        self._model_name = model_name

    def _resolve_device(self) -> str:
        if self._resolved_device is not None:
            return self._resolved_device
        if self._device != "auto":
            self._resolved_device = self._device
            return self._resolved_device
        try:
            import torch

            self._resolved_device = "cuda:0" if torch.cuda.is_available() else "cpu"
        except Exception:
            self._resolved_device = "cpu"
        return self._resolved_device

    def _load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            error = self._gate.error_message()
            if error:
                raise RuntimeError(error)
            try:
                from funasr import AutoModel

                self._model = AutoModel(
                    model=self._model_dir or self._model_id,
                    device=self._resolve_device(),
                    disable_update=True,
                    trust_remote_code=True,
                )
                self._loaded = True
                self._gate.mark_success()
            except Exception:
                self._gate.mark_failure()
                raise

    def _infer(self, samples: np.ndarray, sample_rate: int) -> str:
        self._load()
        try:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
        except Exception:
            rich_transcription_postprocess = str
        result = self._model.generate(
            input=samples.astype(np.float32),
            fs=sample_rate,
            cache={},
            language=self._language,
            use_itn=self._use_itn,
        )
        if isinstance(result, list) and result:
            return rich_transcription_postprocess(str(result[0].get("text", "")))
        return ""

    def recognize(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        if samples is None or len(samples) == 0:
            return ""
        if not self._loaded and self._gate.error_message() is not None:
            return ""
        try:
            with self._model_call_tracker():
                with self._infer_lock:
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
        return {
            "loaded": self._loaded,
            "device": self._resolved_device or self._device,
            "model": self._model_id,
            "model_dir": self._model_dir,
            **self._gate.health(),
        }

    def _empty_torch_cache(self) -> None:
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            logger.debug("torch cuda cache cleanup skipped", exc_info=True)

    def _model_call_tracker(self):
        if self._model_registry is None:
            return nullcontext()
        return self._model_registry.track_call(self._model_name)
