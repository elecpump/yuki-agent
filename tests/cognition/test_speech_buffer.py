import numpy as np

from yuki.cognition.speech_buffer import SpeechBuffer


class FakeVad:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def segments(self, samples):
        self.calls.append(len(samples))
        duration_ms = int(len(samples) / 16000 * 1000)
        return self.callback(duration_ms)


def _frame(value=0.0):
    return np.full(320, value, dtype=np.float32)


def test_silence_only_no_utterance():
    utterances = []
    vad = FakeVad(lambda duration_ms: [])
    buf = SpeechBuffer(vad=vad, on_utterance=utterances.append, vad_interval_ms=400)

    for _ in range(50):
        buf.add_frame(_frame())

    assert utterances == []
    assert buf.has_speech() is False
    assert vad.calls


def test_speech_then_tail_silence_triggers_utterance():
    utterances = []

    def segments(duration_ms):
        if duration_ms < 400:
            return []
        return [[0, 400]]

    buf = SpeechBuffer(
        vad=FakeVad(segments),
        on_utterance=utterances.append,
        vad_interval_ms=400,
        end_silence_ms=800,
    )

    for _ in range(60):
        buf.add_frame(_frame(1.0))

    assert len(utterances) == 1
    assert utterances[0].shape[0] == int(0.4 * 16000)
    assert buf.has_speech() is False


def test_max_duration_flushes_detected_speech():
    utterances = []
    buf = SpeechBuffer(
        vad=FakeVad(lambda duration_ms: [[0, duration_ms]]),
        on_utterance=utterances.append,
        vad_interval_ms=400,
        end_silence_ms=800,
        max_utterance_s=1.0,
    )

    for _ in range(50):
        buf.add_frame(_frame(1.0))

    assert len(utterances) == 1
    assert utterances[0].shape[0] == 16000


def test_reset_clears_accumulation():
    utterances = []
    buf = SpeechBuffer(
        vad=FakeVad(lambda duration_ms: [[0, duration_ms]]),
        on_utterance=utterances.append,
        vad_interval_ms=400,
    )

    for _ in range(20):
        buf.add_frame(_frame(1.0))
    assert buf.has_speech() is True

    buf.reset()
    assert buf.has_speech() is False

    for _ in range(20):
        buf.add_frame(_frame())
    assert utterances == []


def test_vad_exception_skips_frame_without_crashing():
    utterances = []

    class BoomVad:
        def segments(self, samples):
            raise RuntimeError("vad failed")

    buf = SpeechBuffer(vad=BoomVad(), on_utterance=utterances.append, vad_interval_ms=20)

    buf.add_frame(_frame())

    assert utterances == []
    assert buf.has_speech() is False
