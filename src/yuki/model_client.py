from __future__ import annotations

import base64
import io
import threading
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import numpy as np

from yuki.bus import BusError
from yuki.runtime_bus import RuntimeBusProtocol


def _image_png_b64(image: Any) -> str:
    if isinstance(image, str):
        return image
    if isinstance(image, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(image)).decode("ascii")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


class _ModelClient:
    def __init__(
        self,
        bus: RuntimeBusProtocol,
        *,
        model_name: str,
        timeout_ms: int = 2000,
    ) -> None:
        self._bus = bus
        self._model_name = model_name
        self._timeout_ms = timeout_ms

    def warmup(self) -> None:
        try:
            self._bus.request(
                "models/operations/submit",
                {
                    "idempotency_key": f"warmup:{self._model_name}",
                    "action": "load",
                    "model": self._model_name,
                    "reason": "client_warmup",
                },
                timeout_ms=self._timeout_ms,
            )
        except BusError:
            return

    def health(self) -> dict:
        try:
            result = self._bus.request(
                "models/health",
                {"model": self._model_name},
                timeout_ms=self._timeout_ms,
            )
            return dict(result.get("model") or {})
        except BusError:
            return {
                "loaded": False,
                "degraded": True,
                "reason": "model_worker_unavailable",
            }


class VlmClient(_ModelClient):
    def __init__(self, bus: RuntimeBusProtocol, *, timeout_ms: int = 15000) -> None:
        super().__init__(bus, model_name="vlm", timeout_ms=timeout_ms)

    def understand(self, image: Any, cache_key: str | None = None) -> dict:
        return self._understand(
            "model/vlm_understand",
            image,
            cache_key=cache_key,
        )

    def understand_for_question(
        self,
        image: Any,
        question: str,
        cache_key: str | None = None,
    ) -> dict:
        return self._understand(
            "model/vlm_understand_question",
            image,
            cache_key=cache_key,
            question=question,
        )

    def load(self) -> None:
        self.warmup()

    def unload(self) -> None:
        RemoteModelRegistry(self._bus).submit_operation("unload", "vlm")

    def _understand(self, service: str, image: Any, **extra: Any) -> dict:
        payload = {"image_png_b64": _image_png_b64(image), **extra}
        try:
            result = self._bus.request(service, payload, timeout_ms=self._timeout_ms)
            return dict(result.get("context") or {})
        except BusError:
            return {
                "topic": "",
                "summary": "",
                "content_type": "unknown",
                "key_points": [],
                "degraded": True,
                "reason": "model_worker_unavailable",
            }


class SttClient(_ModelClient):
    def __init__(
        self,
        bus: RuntimeBusProtocol,
        *,
        timeout_padding_ms: int = 5000,
    ) -> None:
        super().__init__(bus, model_name="stt")
        self._timeout_padding_ms = timeout_padding_ms

    def recognize(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        duration_ms = int(len(values) / max(1, sample_rate) * 1000)
        result = self._bus.request(
            "model/stt_recognize",
            {
                "samples_b64": base64.b64encode(pcm).decode("ascii"),
                "sample_rate": sample_rate,
                "encoding": "pcm_s16le",
            },
            timeout_ms=duration_ms + self._timeout_padding_ms,
        )
        return str(result.get("text", ""))


class LocalChatModelClient(_ModelClient):
    def __init__(
        self,
        bus: RuntimeBusProtocol,
        *,
        timeout_padding_ms: int = 1000,
    ) -> None:
        super().__init__(bus, model_name="local_chat")
        self._timeout_padding_ms = timeout_padding_ms

    def generate(
        self,
        messages: Sequence[dict],
        *,
        max_new_tokens: int = 256,
        timeout_ms: int | None = None,
    ) -> str:
        model_timeout = timeout_ms or self._timeout_ms
        result = self._bus.request(
            "model/local_generate",
            {
                "messages": list(messages),
                "max_new_tokens": max_new_tokens,
                "timeout_ms": timeout_ms,
            },
            timeout_ms=model_timeout + self._timeout_padding_ms,
        )
        return str(result.get("text", ""))


class TtsClient(_ModelClient):
    def __init__(self, bus: RuntimeBusProtocol, *, wait_ms: int = 2000) -> None:
        super().__init__(bus, model_name="tts")
        self._wait_ms = wait_ms
        self._lock = threading.Lock()
        self._active_job_id: str | None = None

    def synthesize_stream(
        self,
        text: str,
        emotion_vector: list[float] | None = None,
        ref_audio: str | None = None,
        lang: str | None = None,
    ) -> Iterator[bytes]:
        job_id = uuid.uuid4().hex
        self._bus.request(
            "model/tts_synthesize",
            {
                "job_id": job_id,
                "text": text,
                "emotion_vector": emotion_vector,
                "ref_audio": ref_audio,
                "lang": lang,
            },
            timeout_ms=self._timeout_ms,
        )
        with self._lock:
            self._active_job_id = job_id

        def generate() -> Iterator[bytes]:
            after_seq = 0
            try:
                while True:
                    result = self._bus.request(
                        "model/tts_next",
                        {
                            "job_id": job_id,
                            "after_seq": after_seq,
                            "wait_ms": self._wait_ms,
                        },
                        timeout_ms=self._wait_ms + 1000,
                    )
                    if result.get("error"):
                        raise RuntimeError(str(result["error"]))
                    if result.get("ready"):
                        sequence = int(result["seq"])
                        if sequence != after_seq + 1:
                            raise RuntimeError(
                                f"tts_sequence_gap: expected {after_seq + 1}, got {sequence}"
                            )
                        after_seq = sequence
                        yield base64.b64decode(result["pcm_b64"])
                    if result.get("done"):
                        return
            finally:
                with self._lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None

        return generate()

    def cancel(self) -> None:
        with self._lock:
            job_id = self._active_job_id
        if not job_id:
            return
        try:
            self._bus.request(
                "model/tts_cancel",
                {"job_id": job_id},
                timeout_ms=self._timeout_ms,
            )
        except BusError:
            return


class EmbeddingClient(_ModelClient):
    def __init__(
        self,
        bus: RuntimeBusProtocol,
        *,
        model: str = "remote",
        dimension: int = 0,
        timeout_ms: int = 15000,
    ) -> None:
        super().__init__(bus, model_name="embedding", timeout_ms=timeout_ms)
        self.name = "remote"
        self.model = model
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = self._bus.request(
            "model/embed",
            {"texts": list(texts)},
            timeout_ms=self._timeout_ms,
        )
        return [list(map(float, row)) for row in result.get("vectors", [])]


class RemoteModelRegistry:
    def __init__(self, bus: RuntimeBusProtocol, *, timeout_ms: int = 2000) -> None:
        self._bus = bus
        self._timeout_ms = timeout_ms

    def get_overall_status(self) -> dict:
        return self._bus.request("models/health", {}, timeout_ms=self._timeout_ms)

    def get_model_health(self, model: str) -> dict:
        result = self._bus.request(
            "models/health",
            {"model": model},
            timeout_ms=self._timeout_ms,
        )
        return dict(result["model"])

    def get_loaded_models(self) -> list[str]:
        result = self._bus.request("models/list", {}, timeout_ms=self._timeout_ms)
        return list(result.get("models", []))

    def preflight(self, model: str | None = None) -> dict:
        return self._bus.request(
            "models/preflight",
            {"model": model} if model else {},
            timeout_ms=self._timeout_ms,
        )

    def submit_operation(
        self,
        action: str,
        model: str | None = None,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        return self._bus.request(
            "models/operations/submit",
            {
                "idempotency_key": idempotency_key or uuid.uuid4().hex,
                "action": action,
                "model": model,
                "reason": reason,
            },
            timeout_ms=self._timeout_ms,
        )

    def set_local_chat_enabled(
        self,
        enabled: bool,
        *,
        idempotency_key: str,
        reason: str | None = None,
    ) -> dict:
        return self._bus.request(
            "models/local-chat/control",
            {
                "enabled": enabled,
                "idempotency_key": idempotency_key,
                "reason": reason,
            },
            timeout_ms=self._timeout_ms,
        )

    def operation_status(self, operation_id: str) -> dict:
        return self._bus.request(
            "models/operations/status",
            {"operation_id": operation_id},
            timeout_ms=self._timeout_ms,
        )

    def cancel_operation(self, operation_id: str) -> dict:
        return self._bus.request(
            "models/operations/cancel",
            {"operation_id": operation_id},
            timeout_ms=self._timeout_ms,
        )

    def shutdown(self) -> None:
        return
