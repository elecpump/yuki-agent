import threading
import time
from collections.abc import Callable
from contextlib import nullcontext

import numpy as np

from yuki.cognition.call_tracker import CallTracker
from yuki.cognition.load_gate import LoadGate
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.vad")


class FsmnVadBackend:
    """funasr fsmn-vad backend using non-streaming segmentation."""

    def __init__(
        self,
        model_instance=None,
        *,
        model: str = "fsmn-vad",
        device: str = "auto",
        sample_rate: int = 16000,
        enabled: bool = True,
        retry_window_s: float = 60.0,
        clock: Callable[[], float] | None = None,
        model_registry: CallTracker | None = None,
        model_name: str = "vad",
    ) -> None:
        self._model = model_instance
        self._model_id = model
        self._device = device
        self._resolved_device: str | None = None
        self._sample_rate = sample_rate
        self._loaded = model_instance is not None
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
                logger.warning("vad warmup failed", exc_info=True)

        threading.Thread(target=_load_thread, daemon=True, name="yuki-vad-warmup").start()

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

    def set_model_registry(self, registry: CallTracker | None, model_name: str = "vad") -> None:
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
                    model=self._model_id,
                    device=self._resolve_device(),
                    disable_update=True,
                    trust_remote_code=True,
                )
                self._loaded = True
                self._gate.mark_success()
            except Exception:
                self._gate.mark_failure()
                raise

    def segments(self, samples: np.ndarray) -> list[list[int]]:
        if samples is None or len(samples) == 0:
            return []
        try:
            with self._model_call_tracker():
                with self._infer_lock:
                    self._load()
                    result = self._model.generate(input=np.asarray(samples, dtype=np.float32))
                    value = result[0].get("value", []) if isinstance(result, list) and result else []
                    return self._normalize_segments(value, len(samples))
        except Exception:
            logger.warning("vad segmentation failed", exc_info=True)
            return []

    def _normalize_segments(self, value, sample_count: int) -> list[list[int]]:
        duration_ms = int(round(sample_count / max(1, self._sample_rate) * 1000))
        segments: list[list[int]] = []
        if not isinstance(value, list):
            return segments
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                start_ms = int(item[0])
                end_ms = int(item[1])
            except (TypeError, ValueError):
                continue
            if start_ms < 0 or end_ms <= start_ms:
                continue
            segments.append([start_ms, min(end_ms, duration_ms)])
        return segments

    def health(self) -> dict:
        return {
            "loaded": self._loaded,
            "device": self._resolved_device or self._device,
            "model": self._model_id,
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
