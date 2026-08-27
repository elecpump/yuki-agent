import base64
import io

import numpy as np
from PIL import Image

from yuki.model_worker.controller import ManagedModelSpec
from yuki.model_worker.manager import ModelManager
from yuki.model_worker.operations import ModelOperationStore
from yuki.model_worker.scheduler import ModelInferenceScheduler
from yuki.model_worker.services import (
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
