import base64
import io
import threading
import time

import numpy as np
import pytest
from PIL import Image

from yuki.model_worker.controller import ManagedModelSpec, ModelUnavailableError
from yuki.model_worker.manager import ModelManager
from yuki.model_worker.operations import ModelOperationFailure, ModelOperationStore
from yuki.model_worker.scheduler import ModelInferenceScheduler
from yuki.model_worker.services import (
    TtsJobStore,
    operation_handler,
    register_inference_services,
    register_management_services,
)


class FakeBus:
    def __init__(self):
        self.services = {}
        self.lanes = {}

    def respond(self, service, handler, *, lane="work"):
        self.services[service] = handler
        self.lanes[service] = lane


class FakeVlm:
    def understand(self, image, cache_key=None):
        return {"topic": image.mode, "summary": cache_key or ""}


class FakeStt:
    def recognize(self, samples, sample_rate):
        return f"{sample_rate}:{round(float(samples[0]), 2)}"


class FakeTts:
    def synthesize_stream(self, text, **kwargs):
        del kwargs
        yield text.encode()


def _png_b64():
    output = io.BytesIO()
    Image.new("RGB", (1, 1)).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


def _register(manager, name, model, priority=50):
    manager.register(
        ManagedModelSpec(
            name=name,
            loader=lambda: model,
            priority=priority,
            min_residency_s=0,
        )
    )


def test_management_services_use_control_lane_and_async_operations():
    bus = FakeBus()
    manager = ModelManager(vram_safety_margin_mb=0)
    _register(manager, "vlm", FakeVlm())
    operations = ModelOperationStore(operation_handler(manager))
    try:
        register_management_services(bus, manager, operations)
        result = bus.services["models/operations/submit"](
            {"idempotency_key": "load", "action": "load", "model": "vlm"}
        )
        assert result["accepted"] is True
        assert bus.lanes["models/operations/submit"] == "control"
        assert bus.lanes["models/health"] == "control"
    finally:
        operations.close()


def test_model_load_failure_has_stable_operation_error_code():
    bus = FakeBus()
    manager = ModelManager(vram_safety_margin_mb=0)
    manager.register(
        ManagedModelSpec(
            name="local_chat",
            loader=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    )
    operations = ModelOperationStore(operation_handler(manager))
    try:
        register_management_services(bus, manager, operations)
        submitted = bus.services["models/operations/submit"](
            {
                "idempotency_key": "load-failure",
                "action": "load",
                "model": "local_chat",
            }
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = bus.services["models/operations/status"](
                {"operation_id": submitted["operation_id"]}
            )
            if status["state"] == "failed":
                break
            time.sleep(0.01)

        assert status["error_code"] == "load_failed"
    finally:
        operations.close()


def test_local_chat_control_is_dedicated_and_generic_submit_rejects_enable():
    bus = FakeBus()
    manager = ModelManager(vram_safety_margin_mb=0)
    manager.register(
        ManagedModelSpec(name="local_chat", loader=object, enabled=False)
    )
    operations = ModelOperationStore(operation_handler(manager))
    try:
        register_management_services(bus, manager, operations)
        with pytest.raises(ValueError, match="invalid_action"):
            bus.services["models/operations/submit"](
                {
                    "idempotency_key": "generic-enable",
                    "action": "enable",
                    "model": "local_chat",
                }
            )

        submitted = bus.services["models/local-chat/control"](
            {"idempotency_key": "dedicated-enable", "enabled": True}
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = bus.services["models/operations/status"](
                {"operation_id": submitted["operation_id"]}
            )
            if status["state"] == "succeeded":
                break
            time.sleep(0.01)

        assert status["state"] == "succeeded"
        assert manager.get_model_health("local_chat")["callable"] is True
        assert bus.lanes["models/local-chat/control"] == "control"
    finally:
        operations.close()


def test_vram_admission_failure_has_stable_operation_error_code():
    bus = FakeBus()

    class LowMemoryGpu:
        def snapshot(self):
            return {"available": True, "free_mb": 0, "low_memory": True}

        def empty_cache(self):
            return False

    manager = ModelManager(
        gpu_monitor=LowMemoryGpu(),
        vram_safety_margin_mb=0,
        vram_hysteresis_mb=0,
    )
    manager.register(
        ManagedModelSpec(
            name="local_chat",
            loader=object,
            estimated_vram_mb=100,
        )
    )
    operations = ModelOperationStore(operation_handler(manager))
    try:
        register_management_services(bus, manager, operations)
        submitted = bus.services["models/operations/submit"](
            {
                "idempotency_key": "low-vram",
                "action": "load",
                "model": "local_chat",
            }
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = bus.services["models/operations/status"](
                {"operation_id": submitted["operation_id"]}
            )
            if status["state"] == "failed":
                break
            time.sleep(0.01)

        assert status["error_code"] == "insufficient_vram"
    finally:
        operations.close()


def test_enable_preserves_failed_controller_error_code() -> None:
    manager = ModelManager(vram_safety_margin_mb=0)
    manager.register(
        ManagedModelSpec(
            name="local_chat",
            loader=object,
            unloader=lambda handle: (_ for _ in ()).throw(RuntimeError("cannot unload")),
        )
    )
    manager.load("local_chat")
    with pytest.raises(RuntimeError, match="cannot unload"):
        manager.unload("local_chat")

    with pytest.raises(ModelOperationFailure) as exc:
        operation_handler(manager)("enable", "local_chat")

    assert exc.value.error_code == "unload_failed"


def test_unclassified_model_unavailability_uses_existing_operation_error_code() -> None:
    class UnavailableManager:
        def load(self, model: str) -> None:
            del model
            raise ModelUnavailableError("not ready")

    with pytest.raises(ModelOperationFailure) as exc:
        operation_handler(UnavailableManager())("load", "local_chat")  # type: ignore[arg-type]

    assert exc.value.error_code == "operation_failed"


def test_inference_services_decode_payloads_and_tts_replays_unacked_chunk():
    bus = FakeBus()
    manager = ModelManager(vram_safety_margin_mb=0)
    _register(manager, "vlm", FakeVlm(), 30)
    _register(manager, "stt", FakeStt(), 90)
    _register(manager, "tts", FakeTts(), 80)
    scheduler = ModelInferenceScheduler(concurrency=1)
    jobs = register_inference_services(bus, manager, scheduler)
    try:
        context = bus.services["model/vlm_understand"](
            {"image_png_b64": _png_b64(), "cache_key": "frame"}
        )
        assert context["context"] == {"topic": "RGB", "summary": "frame"}

        pcm = np.asarray([32767], dtype="<i2").tobytes()
        recognized = bus.services["model/stt_recognize"](
            {
                "samples_b64": base64.b64encode(pcm).decode(),
                "sample_rate": 16000,
                "encoding": "pcm_s16le",
            }
        )
        assert recognized["text"] == "16000:1.0"

        bus.services["model/tts_synthesize"]({"job_id": "job", "text": "pcm"})
        first = bus.services["model/tts_next"](
            {"job_id": "job", "after_seq": 0, "wait_ms": 1000}
        )
        replay = bus.services["model/tts_next"](
            {"job_id": "job", "after_seq": 0, "wait_ms": 1000}
        )
        assert first == replay
        assert base64.b64decode(first["pcm_b64"]) == b"pcm"
    finally:
        if jobs is not None:
            jobs.close()
        scheduler.close()


def test_tts_jobs_are_cancelled_and_reaped_after_inactivity():
    release = threading.Event()

    class BlockingTts:
        def synthesize_stream(self, text, **kwargs):
            del text, kwargs
            release.wait(1.0)
            yield b"late"

    manager = ModelManager(vram_safety_margin_mb=0)
    _register(manager, "tts", BlockingTts(), 80)
    scheduler = ModelInferenceScheduler(concurrency=1)
    jobs = TtsJobStore(manager, scheduler, ttl_s=0.05)
    try:
        jobs.start({"job_id": "orphan", "text": "pcm"})
        orphan = jobs._jobs["orphan"]
        assert orphan.cancelled.wait(1.0), "background cleanup did not cancel the job"
        with pytest.raises(KeyError, match="tts_job_not_found"):
            jobs.next({"job_id": "orphan", "after_seq": 0, "wait_ms": 0})
    finally:
        release.set()
        jobs.close()
        scheduler.close()


def test_closed_tts_job_store_rejects_new_jobs():
    manager = ModelManager(vram_safety_margin_mb=0)
    _register(manager, "tts", FakeTts(), 80)
    scheduler = ModelInferenceScheduler(concurrency=1)
    jobs = TtsJobStore(manager, scheduler)
    jobs.close()
    try:
        with pytest.raises(RuntimeError, match="tts_job_store_stopped"):
            jobs.start({"job_id": "late", "text": "pcm"})
    finally:
        scheduler.close()
