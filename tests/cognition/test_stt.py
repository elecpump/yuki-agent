import base64

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
