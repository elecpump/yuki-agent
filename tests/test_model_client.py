import base64

import numpy as np
import pytest

from yuki.bus import BusTimeoutError
from yuki.model_client import (
    EmbeddingClient,
    LocalChatModelClient,
    RemoteModelRegistry,
    SttClient,
    TtsClient,
    VlmClient,
)


class FakeBus:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def request(self, service, payload, timeout_ms=2000):
        self.calls.append((service, payload, timeout_ms))
        response = self.responses.get(service, {})
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):
            return response.pop(0)
        return response


def test_vlm_client_encodes_png_and_degrades_on_timeout():
    bus = FakeBus({"model/vlm_understand": BusTimeoutError("late")})
    client = VlmClient(bus)

    result = client.understand(b"png", cache_key="frame")

    assert result["degraded"] is True
    assert bus.calls[0][1]["image_png_b64"] == base64.b64encode(b"png").decode()


def test_stt_client_uses_pcm_s16le_and_dynamic_timeout():
    bus = FakeBus({"model/stt_recognize": {"text": "hello"}})
    client = SttClient(bus)

    assert client.recognize(np.asarray([1.0, -1.0], dtype=np.float32), 2) == "hello"
    service, payload, timeout_ms = bus.calls[0]
    assert service == "model/stt_recognize"
    assert payload["encoding"] == "pcm_s16le"
    assert np.frombuffer(base64.b64decode(payload["samples_b64"]), dtype="<i2").tolist() == [32767, -32767]
    assert timeout_ms == 6000


def test_local_chat_and_embedding_clients_mirror_interfaces():
    bus = FakeBus(
        {
            "model/local_generate": {"text": "hi"},
            "model/embed": {"vectors": [[1, 2]]},
        }
    )
    assert LocalChatModelClient(bus).generate([{"role": "user", "content": "x"}]) == "hi"
    assert EmbeddingClient(bus).embed(["x"]) == [[1.0, 2.0]]


def test_tts_client_long_poll_and_cancel():
    bus = FakeBus(
        {
            "model/tts_synthesize": {"job_id": "ignored", "accepted": True},
            "model/tts_next": [
                {"ready": False, "done": False},
                {
                    "ready": True,
                    "seq": 1,
                    "pcm_b64": base64.b64encode(b"pcm").decode(),
                    "done": False,
                },
                {"ready": False, "done": True},
            ],
            "model/tts_cancel": {},
        }
    )
    client = TtsClient(bus, wait_ms=10)
    chunks = client.synthesize_stream("hello")
    assert next(chunks) == b"pcm"
    client.cancel()
    assert any(call[0] == "model/tts_cancel" for call in bus.calls)
    chunks.close()


def test_tts_client_rejects_non_contiguous_sequence():
    bus = FakeBus(
        {
            "model/tts_synthesize": {"accepted": True},
            "model/tts_next": {
                "ready": True,
                "seq": 2,
                "pcm_b64": base64.b64encode(b"pcm").decode(),
                "done": False,
            },
        }
    )
    chunks = TtsClient(bus, wait_ms=10).synthesize_stream("hello")

    with pytest.raises(RuntimeError, match="tts_sequence_gap"):
        next(chunks)


def test_remote_registry_uses_async_operation_services():
    bus = FakeBus(
        {
            "models/operations/submit": {"operation_id": "op", "accepted": True},
            "models/operations/status": {"state": "queued"},
        }
    )
    registry = RemoteModelRegistry(bus)
    submitted = registry.submit_operation("load", "vlm", idempotency_key="key")
    assert submitted["operation_id"] == "op"
    assert registry.operation_status("op")["state"] == "queued"
