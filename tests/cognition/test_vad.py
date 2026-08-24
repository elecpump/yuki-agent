import sys
import types

import numpy as np

from yuki.cognition.vad import FsmnVadBackend


def _install_funasr(monkeypatch, auto_model):
    fake_funasr = types.ModuleType("funasr")
    fake_funasr.AutoModel = auto_model
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)


def test_segments_loads_model_and_normalizes_values(monkeypatch):
    calls = []
    generate_calls = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def generate(self, **kwargs):
            generate_calls.append(kwargs)
            return [{"value": [[0, 400], [500, -1], ["bad", 900], [900, 1500]]}]

    _install_funasr(monkeypatch, FakeAutoModel)
    vad = FsmnVadBackend(model="fsmn-local", device="cpu")

    segments = vad.segments(np.zeros(16000, dtype=np.float32))

    assert calls == [{
        "model": "fsmn-local",
        "device": "cpu",
        "disable_update": True,
        "trust_remote_code": True,
    }]
    assert generate_calls[0]["input"].dtype == np.float32
    assert segments == [[0, 400], [900, 1000]]


def test_segments_returns_empty_on_failure(monkeypatch):
    class FakeAutoModel:
        def __init__(self, **kwargs):
            raise RuntimeError("missing model")

    _install_funasr(monkeypatch, FakeAutoModel)
    vad = FsmnVadBackend(device="cpu")

    assert vad.segments(np.zeros(320, dtype=np.float32)) == []
    assert vad.health()["degraded"] is True


def test_auto_device_prefers_cpu_when_torch_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    vad = FsmnVadBackend(device="auto")

    assert vad._resolve_device() == "cpu"
