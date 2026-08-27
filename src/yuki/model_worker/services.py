from __future__ import annotations

import base64
import io
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image

from yuki.model_worker.manager import ModelManager
from yuki.model_worker.operations import ModelOperationStore
from yuki.model_worker.scheduler import ModelInferenceScheduler
from yuki.runtime_bus import RuntimeBusProtocol


ALLOWED_ACTIONS = {"load", "unload", "reload", "preflight", "relieve_memory_pressure"}


def operation_handler(
    manager: ModelManager,
) -> Callable[[str, str | None], dict]:
    def handle(action: str, model: str | None) -> dict:
        if action == "load":
            manager.load(_required_model(model))
            return {"ok": True}
        if action == "unload":
            manager.unload(_required_model(model))
            return {"ok": True}
        if action == "reload":
            manager.reload(_required_model(model))
            return {"ok": True}
        if action == "preflight":
            return manager.preflight(model)
        if action == "relieve_memory_pressure":
            return manager.relieve_memory_pressure()
        raise ValueError("invalid_action")

    return handle


def register_management_services(
    bus: RuntimeBusProtocol,
    manager: ModelManager,
    operations: ModelOperationStore,
    *,
    policies: dict | None = None,
    legacy_wait_s: float = 2.0,
) -> None:
    def health(payload: dict) -> dict:
        model = (payload or {}).get("model")
        return (
            {"model": manager.get_model_health(str(model))}
            if model
            else manager.get_overall_status()
        )

    def submit(payload: dict) -> dict:
        payload = dict(payload or {})
        action = str(payload.get("action") or "")
        model = payload.get("model")
        if action not in ALLOWED_ACTIONS:
            raise ValueError("invalid_action")
        if action in {"load", "unload", "reload"} and not model:
            raise ValueError("model_required")
        if action == "relieve_memory_pressure" and model:
            raise ValueError("model_not_allowed")
        if model and str(model) not in manager.names():
            raise ValueError("unknown_model")
        return operations.submit(
            idempotency_key=str(payload.get("idempotency_key") or ""),
            action=action,
            model=str(model) if model else None,
            reason=str(payload.get("reason") or "") or None,
        )

    def status(payload: dict) -> dict:
        try:
            return operations.status(str((payload or {}).get("operation_id") or ""))
        except KeyError:
            return {"ok": False, "error_code": "operation_not_found"}

    def cancel(payload: dict) -> dict:
        try:
            return operations.cancel(str((payload or {}).get("operation_id") or ""))
        except KeyError:
            return {
                "ok": False,
                "error_code": "operation_not_found",
                "cancel_requested": False,
            }

    def legacy(action: str, payload: dict) -> dict:
        model = (payload or {}).get("model")
        accepted = operations.submit(
            idempotency_key=f"legacy:{action}:{model or '*'}:{time.time_ns()}",
            action=action,
            model=str(model) if model else None,
            reason="legacy_wrapper",
        )
        operation_id = accepted["operation_id"]
        deadline = time.monotonic() + legacy_wait_s
        while time.monotonic() < deadline:
            result = operations.status(operation_id)
            if result["state"] == "succeeded":
                return dict(result.get("result") or {"ok": True})
            if result["state"] in {"failed", "cancelled"}:
                return {
                    "ok": False,
                    "operation_id": operation_id,
                    "reason": result.get("error_code") or result["state"],
                }
            time.sleep(0.01)
        return {
            "ok": False,
            "operation_id": operation_id,
            "reason": "operation_pending",
        }

    bus.respond("models/health", health, lane="control")
    bus.respond(
        "models/list",
        lambda payload: {"models": manager.get_loaded_models()},
        lane="control",
    )
    bus.respond(
        "models/policy",
        lambda payload: {"policies": policies or {}},
        lane="control",
    )
    bus.respond("models/operations/submit", submit, lane="control")
    bus.respond("models/operations/status", status, lane="control")
    bus.respond("models/operations/cancel", cancel, lane="control")
    bus.respond("models/unload", lambda payload: legacy("unload", payload))
    bus.respond("models/reload", lambda payload: legacy("reload", payload))
    bus.respond("models/preflight", lambda payload: legacy("preflight", payload))
    bus.respond(
        "models/relieve_memory_pressure",
        lambda payload: legacy("relieve_memory_pressure", payload),
    )


@dataclass
class _TtsJob:
    job_id: str
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock())
    )
    chunks: dict[int, bytes] = field(default_factory=dict)
    next_seq: int = 1
    acknowledged_seq: int = 0
    done: bool = False
    error: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    last_access: float = field(default_factory=time.monotonic)


class TtsJobStore:
    def __init__(
        self,
        manager: ModelManager,
        scheduler: ModelInferenceScheduler,
        *,
        max_chunks: int = 32,
        ttl_s: float = 60.0,
        oom_retry: int = 0,
    ) -> None:
        self._manager = manager
        self._scheduler = scheduler
        self._max_chunks = max(1, max_chunks)
        self._ttl_s = ttl_s
        self._oom_retry = max(0, oom_retry)
        self._jobs: dict[str, _TtsJob] = {}
        self._lock = threading.Lock()

    def start(self, payload: dict) -> dict:
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            raise ValueError("job_id_required")
        self._cleanup()
        with self._lock:
            if job_id in self._jobs:
                raise ValueError("job_id_exists")
            job = _TtsJob(job_id)
            self._jobs[job_id] = job

        def produce() -> None:
            invocation = 0

            def synthesize(model: Any) -> None:
                nonlocal invocation
                invocation += 1
                if invocation > 1:
                    with job.condition:
                        if job.acknowledged_seq:
                            raise RuntimeError("tts_retry_after_delivery")
                        job.chunks.clear()
                        job.next_seq = 1
                chunks = model.synthesize_stream(
                    str(payload.get("text") or ""),
                    emotion_vector=payload.get("emotion_vector"),
                    ref_audio=payload.get("ref_audio"),
                    lang=payload.get("lang"),
                )
                for chunk in chunks:
                    if job.cancelled.is_set():
                        break
                    with job.condition:
                        if len(job.chunks) >= self._max_chunks:
                            job.error = "client_slow"
                            break
                        job.chunks[job.next_seq] = bytes(chunk)
                        job.next_seq += 1
                        job.condition.notify_all()

            try:
                self._manager.run_inference(
                    "tts",
                    synthesize,
                    oom_retry=self._oom_retry,
                )
            except Exception:
                with job.condition:
                    job.error = "synthesis_failed"
            finally:
                with job.condition:
                    job.done = True
                    job.condition.notify_all()

        try:
            self._scheduler.submit(produce, lane="interactive", priority=80)
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise
        return {"job_id": job_id, "accepted": True}

    def next(self, payload: dict) -> dict:
        job = self._job(str(payload.get("job_id") or ""))
        after_seq = int(payload.get("after_seq") or 0)
        wait_s = min(max(float(payload.get("wait_ms") or 0) / 1000.0, 0.0), 5.0)
        deadline = time.monotonic() + wait_s
        with job.condition:
            job.last_access = time.monotonic()
            job.acknowledged_seq = max(job.acknowledged_seq, after_seq)
            for sequence in [seq for seq in job.chunks if seq <= after_seq]:
                job.chunks.pop(sequence, None)
            while True:
                available = [seq for seq in job.chunks if seq > after_seq]
                if available:
                    sequence = min(available)
                    return {
                        "ready": True,
                        "seq": sequence,
                        "pcm_b64": base64.b64encode(job.chunks[sequence]).decode("ascii"),
                        "done": False,
                    }
                if job.done:
                    return {
                        "ready": False,
                        "done": True,
                        "error": job.error,
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"ready": False, "done": False}
                job.condition.wait(timeout=remaining)

    def cancel(self, payload: dict) -> dict:
        job = self._job(str(payload.get("job_id") or ""))
        job.cancelled.set()
        with job.condition:
            job.done = True
            job.condition.notify_all()
        return {}

    def close(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancelled.set()
            with job.condition:
                job.done = True
                job.condition.notify_all()

    def _job(self, job_id: str) -> _TtsJob:
        self._cleanup()
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError("tts_job_not_found")
        return job

    def _cleanup(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [
                job_id
                for job_id, job in self._jobs.items()
                if job.done and now - job.last_access >= self._ttl_s
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)


def register_inference_services(
    bus: RuntimeBusProtocol,
    manager: ModelManager,
    scheduler: ModelInferenceScheduler,
    *,
    oom_retry: int = 0,
) -> TtsJobStore | None:
    def run(
        model: str,
        callback: Callable[[Any], Any],
        *,
        lane: str,
        priority: int,
    ) -> Any:
        def execute() -> Any:
            return manager.run_inference(
                model,
                callback,
                oom_retry=oom_retry,
            )

        return scheduler.submit(execute, lane=lane, priority=priority).result()

    def vlm(payload: dict, *, question: bool = False) -> dict:
        raw = base64.b64decode(str(payload.get("image_png_b64") or ""))
        image = Image.open(io.BytesIO(raw)).convert("RGB")

        def infer(model: Any) -> dict:
            if question:
                return model.understand_for_question(
                    image,
                    str(payload.get("question") or ""),
                    cache_key=payload.get("cache_key"),
                )
            return model.understand(image, cache_key=payload.get("cache_key"))

        return {"context": run("vlm", infer, lane="background", priority=30)}

    def stt(payload: dict) -> dict:
        if payload.get("encoding") != "pcm_s16le":
            raise ValueError("unsupported_audio_encoding")
        pcm = np.frombuffer(
            base64.b64decode(str(payload.get("samples_b64") or "")),
            dtype="<i2",
        )
        samples = pcm.astype(np.float32) / 32767.0
        text = run(
            "stt",
            lambda model: model.recognize(samples, int(payload.get("sample_rate") or 16000)),
            lane="interactive",
            priority=90,
        )
        return {"text": text}

    def local_generate(payload: dict) -> dict:
        text = run(
            "local_chat",
            lambda model: model.generate(
                list(payload.get("messages") or []),
                max_new_tokens=int(payload.get("max_new_tokens") or 256),
                timeout_ms=payload.get("timeout_ms"),
            ),
            lane="interactive",
            priority=100,
        )
        return {"text": text}

    def embed(payload: dict) -> dict:
        vectors = run(
            "embedding",
            lambda model: model.embed(list(payload.get("texts") or [])),
            lane="background",
            priority=10,
        )
        return {"vectors": vectors}

    bus.respond("model/vlm_understand", lambda payload: vlm(payload))
    bus.respond(
        "model/vlm_understand_question",
        lambda payload: vlm(payload, question=True),
    )
    bus.respond("model/stt_recognize", stt)
    bus.respond("model/local_generate", local_generate)
    if "embedding" in manager.names():
        bus.respond("model/embed", embed)

    tts_jobs = None
    if "tts" in manager.names():
        tts_jobs = TtsJobStore(manager, scheduler, oom_retry=oom_retry)
        bus.respond("model/tts_synthesize", tts_jobs.start)
        bus.respond("model/tts_next", tts_jobs.next, lane="stream")
        bus.respond("model/tts_cancel", tts_jobs.cancel, lane="control")
    return tts_jobs


def _required_model(model: str | None) -> str:
    if not model:
        raise ValueError("model_required")
    return model
