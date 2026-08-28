import base64
import sys
import threading
import types

import numpy as np

from yuki.cognition.stt import SpeechRecognizer

from tests.fakes import RecordingCallTracker


def _install_funasr(monkeypatch, auto_model, postprocess=None):
    fake_funasr = types.ModuleType("funasr")
    fake_funasr.AutoModel = auto_model
    fake_utils = types.ModuleType("funasr.utils")
    fake_utils.__path__ = []
    fake_postprocess = types.ModuleType("funasr.utils.postprocess_utils")
    fake_postprocess.rich_transcription_postprocess = postprocess or (lambda text: text)
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)
    monkeypatch.setitem(sys.modules, "funasr.utils", fake_utils)
    monkeypatch.setitem(sys.modules, "funasr.utils.postprocess_utils", fake_postprocess)


def test_recognize_empty_returns_empty():
    stt = SpeechRecognizer(model=object())
    assert stt.recognize(np.array([], dtype=np.float32)) == ""


def test_recognize_base64_passes_kwargs_to_generate():
    calls = []

    class FakeModel:
        def generate(self, **kwargs):
            calls.append(kwargs)
            return [{"text": "你好"}]

    stt = SpeechRecognizer(model=FakeModel(), language="zn", use_itn=False)
    pcm = np.zeros(16000, dtype=np.float32).tobytes()
    text = stt.recognize_base64(base64.b64encode(pcm).decode("ascii"), sample_rate=16000)
    assert text == "你好"
    assert len(calls) == 1
    assert calls[0]["fs"] == 16000
    assert calls[0]["input"].dtype == np.float32
    assert calls[0]["cache"] == {}
    assert calls[0]["language"] == "zn"
    assert calls[0]["use_itn"] is False


def test_recognize_handles_empty_text_result():
    class FakeModel:
        def generate(self, **kwargs):
            return [{"text": ""}]

    stt = SpeechRecognizer(model=FakeModel())
    pcm = np.zeros(320, dtype=np.float32).tobytes()
    assert stt.recognize_base64(base64.b64encode(pcm).decode("ascii")) == ""


def test_recognize_records_call_tracker_metrics():
    tracker = RecordingCallTracker()

    class FakeModel:
        def generate(self, **kwargs):
            return [{"text": "ok"}]

    stt = SpeechRecognizer(model=FakeModel(), model_registry=tracker)

    assert stt.recognize(np.zeros(320, dtype=np.float32)) == "ok"

    assert tracker.success == 1
    assert tracker.failure == 0


def test_recognize_records_call_tracker_failures():
    tracker = RecordingCallTracker()

    class FakeModel:
        def generate(self, **kwargs):
            raise RuntimeError("timeout")

    stt = SpeechRecognizer(model=FakeModel(), model_registry=tracker)

    assert stt.recognize(np.zeros(320, dtype=np.float32)) == ""

    assert tracker.success == 0
    assert tracker.failure == 1


def test_load_uses_configured_model_and_device(monkeypatch):
    calls = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def generate(self, **kwargs):
            return [{"text": "ok"}]

    _install_funasr(monkeypatch, FakeAutoModel)
    stt = SpeechRecognizer(model_id="hub-model", model_dir="D:/models/sense", device="cuda:0")

    assert stt.recognize(np.zeros(320, dtype=np.float32)) == "ok"
    assert calls == [{
        "model": "D:/models/sense",
        "device": "cuda:0",
        "disable_update": True,
        "trust_remote_code": True,
    }]


def test_auto_device_prefers_cuda_when_available(monkeypatch):
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: True)
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    stt = SpeechRecognizer(device="auto")

    assert stt._resolve_device() == "cuda:0"


def test_rich_transcription_postprocess_is_called(monkeypatch):
    calls = []

    class FakeModel:
        def generate(self, **kwargs):
            return [{"text": "<|zh|>你好<|end|>"}]

    def postprocess(text):
        calls.append(text)
        return "你好"

    _install_funasr(monkeypatch, object, postprocess=postprocess)
    stt = SpeechRecognizer(model=FakeModel())

    assert stt.recognize(np.zeros(320, dtype=np.float32)) == "你好"
    assert calls == ["<|zh|>你好<|end|>"]


def test_warmup_loads_in_background(monkeypatch):
    loaded = threading.Event()

    class FakeAutoModel:
        def __init__(self, **kwargs):
            loaded.set()

    _install_funasr(monkeypatch, FakeAutoModel)
    stt = SpeechRecognizer(device="cpu")

    stt.warmup()

    assert loaded.wait(timeout=1.0)
    assert stt.health()["loaded"] is True


def test_warmup_failure_is_degraded(monkeypatch):
    loaded = threading.Event()

    class FakeAutoModel:
        def __init__(self, **kwargs):
            loaded.set()
            raise RuntimeError("missing model")

    _install_funasr(monkeypatch, FakeAutoModel)
    stt = SpeechRecognizer(device="cpu")

    stt.warmup()

    assert loaded.wait(timeout=1.0)
    assert stt.health()["loaded"] is False
    assert stt.health()["degraded"] is True


def test_load_failure_is_remembered(monkeypatch):
    calls = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("missing model")

    _install_funasr(monkeypatch, FakeAutoModel)

    stt = SpeechRecognizer()
    samples = np.zeros(320, dtype=np.float32)

    assert stt.recognize(samples) == ""
    assert stt.recognize(samples) == ""
    assert len(calls) == 1


def test_recognize_recovers_after_retry_window(monkeypatch):
    now = [0.0]
    calls = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("missing model")

    _install_funasr(monkeypatch, FakeAutoModel)

    stt = SpeechRecognizer(retry_window_s=10.0, clock=lambda: now[0])
    samples = np.zeros(320, dtype=np.float32)
    assert stt.recognize(samples) == ""
    assert stt.recognize(samples) == ""
    assert len(calls) == 1
    now[0] = 10.0
    assert stt._gate.can_load() is True


def test_health_reports_degraded_when_failed(monkeypatch):
    class FakeAutoModel:
        def __init__(self, **kwargs):
            raise RuntimeError("missing model")

    _install_funasr(monkeypatch, FakeAutoModel)

    stt = SpeechRecognizer()
    stt.recognize(np.zeros(320, dtype=np.float32))
    health = stt.health()
    assert health["degraded"] is True
    assert health["loaded"] is False
    assert health["model"] == "iic/SenseVoiceSmall"
