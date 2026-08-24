import base64
import types

import numpy as np
import pytest

from yuki.cognition.stt import SpeechRecognizer


def test_recognize_empty_returns_empty():
    stt = SpeechRecognizer(model=object())
    assert stt.recognize(np.array([], dtype=np.float32)) == ""


def test_recognize_base64_passes_kwargs_to_model():
    calls = []

    class FakeModel:
        def __call__(self, **kwargs):
            calls.append(kwargs)
            return [{"text": "你好"}]

    stt = SpeechRecognizer(model=FakeModel())
    pcm = np.zeros(16000, dtype=np.float32).tobytes()
    text = stt.recognize_base64(base64.b64encode(pcm).decode("ascii"), sample_rate=16000)
    assert text == "你好"
    assert len(calls) == 1
    assert calls[0]["fs"] == 16000
    assert calls[0]["input"].dtype == np.float32


def test_recognize_handles_empty_text_result():
    class FakeModel:
        def __call__(self, input, fs):
            return [{"text": ""}]

    stt = SpeechRecognizer(model=FakeModel())
    pcm = np.zeros(320, dtype=np.float32).tobytes()
    assert stt.recognize_base64(base64.b64encode(pcm).decode("ascii")) == ""


def test_load_failure_is_remembered(monkeypatch):
    import sys

    calls = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("missing model")

    fake_funasr = types.SimpleNamespace(AutoModel=FakeAutoModel)
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    stt = SpeechRecognizer()
    samples = np.zeros(320, dtype=np.float32)

    assert stt.recognize(samples) == ""
    assert stt.recognize(samples) == ""
    assert len(calls) == 1


def test_recognize_recovers_after_retry_window(monkeypatch):
    import sys

    now = [0.0]
    calls = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("missing model")

    fake_funasr = types.SimpleNamespace(AutoModel=FakeAutoModel)
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    stt = SpeechRecognizer(retry_window_s=10.0, clock=lambda: now[0])
    samples = np.zeros(320, dtype=np.float32)
    assert stt.recognize(samples) == ""
    assert stt.recognize(samples) == ""
    assert len(calls) == 1
    now[0] = 10.0
    assert stt._gate.can_load() is True


def test_health_reports_degraded_when_failed(monkeypatch):
    import sys

    class FakeAutoModel:
        def __init__(self, **kwargs):
            raise RuntimeError("missing model")

    fake_funasr = types.SimpleNamespace(AutoModel=FakeAutoModel)
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    stt = SpeechRecognizer()
    stt.recognize(np.zeros(320, dtype=np.float32))
    health = stt.health()
    assert health["degraded"] is True
    assert health["loaded"] is False
