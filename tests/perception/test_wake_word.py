import base64

import numpy as np

from yuki.config import WakeWordConfig
from yuki.perception.wake_word import WakeWordDetector, WakeWordFrameAdapter
from yuki.topics import Topics

from tests.fakes import FakeBus


def _payload(samples, *, sample_rate=16000):
    pcm = base64.b64encode(np.asarray(samples, dtype=np.float32).tobytes()).decode("ascii")
    return {"pcm": pcm, "sample_rate": sample_rate, "ts": 1.0}


class FakeBackend:
    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = []

    def predict(self, pcm):
        self.calls.append(pcm)
        return self.scores.pop(0) if self.scores else {"yuki": 0.0}


def test_wake_word_adapter_aggregates_20ms_frames_into_80ms_int16_chunk():
    adapter = WakeWordFrameAdapter(chunk_ms=80)
    chunks = []
    for value in (0.25, -0.25, 2.0, -2.0):
        chunks.extend(adapter.add_payload(_payload(np.ones(320, dtype=np.float32) * value)))

    assert len(chunks) == 1
    assert chunks[0].dtype == np.int16
    assert chunks[0].shape == (1280,)
    assert chunks[0][0] == int(0.25 * 32767)
    assert chunks[0][640] == 32767
    assert chunks[0][960] == -32767


def test_wake_word_detector_publishes_awake_above_threshold():
    bus = FakeBus()
    backend = FakeBackend([{"yuki": 0.8}])
    detector = WakeWordDetector(
        bus,
        WakeWordConfig(enabled=True, threshold=0.5),
        backend=backend,
        clock=lambda: 10.0,
        wall_clock=lambda: 123.0,
    )
    detector.start()

    for _ in range(4):
        bus.subscriptions[Topics.MIC][0]("audio/mic", _payload(np.zeros(320)))

    assert bus.published == [
        (
            Topics.AWAKE,
            {"source": "wake_word", "ts": 123.0, "score": 0.8, "model": "yuki"},
        )
    ]
    assert backend.calls[0].dtype == np.int16


def test_wake_word_detector_honors_threshold_and_refractory():
    bus = FakeBus()
    now = [10.0]
    backend = FakeBackend([
        {"yuki": 0.4},
        {"yuki": 0.9},
        {"yuki": 0.95},
        {"yuki": 0.96},
    ])
    detector = WakeWordDetector(
        bus,
        WakeWordConfig(enabled=True, threshold=0.5, refractory_s=2.0),
        backend=backend,
        clock=lambda: now[0],
        wall_clock=lambda: now[0],
    )
    detector.start()

    for _ in range(4):
        bus.subscriptions[Topics.MIC][0]("audio/mic", _payload(np.zeros(320)))
    assert bus.published == []

    for _ in range(4):
        bus.subscriptions[Topics.MIC][0]("audio/mic", _payload(np.zeros(320)))
    assert len(bus.published) == 1

    now[0] += 1.0
    for _ in range(4):
        bus.subscriptions[Topics.MIC][0]("audio/mic", _payload(np.zeros(320)))
    assert len(bus.published) == 1

    now[0] += 1.1
    for _ in range(4):
        bus.subscriptions[Topics.MIC][0]("audio/mic", _payload(np.zeros(320)))
    assert len(bus.published) == 2


def test_wake_word_detector_disabled_ignores_audio():
    bus = FakeBus()
    backend = FakeBackend([{"yuki": 1.0}])
    detector = WakeWordDetector(
        bus,
        WakeWordConfig(enabled=False),
        backend=backend,
    )
    detector.start()

    for _ in range(4):
        bus.subscriptions[Topics.MIC][0]("audio/mic", _payload(np.zeros(320)))

    assert bus.published == []
    assert backend.calls == []


def test_wake_word_detector_tolerates_backend_failure():
    class BoomBackend:
        def __init__(self):
            self.calls = 0

        def predict(self, pcm):
            self.calls += 1
            raise RuntimeError("model load failed")

    bus = FakeBus()
    backend = BoomBackend()
    detector = WakeWordDetector(
        bus,
        WakeWordConfig(enabled=True, threshold=0.5),
        backend=backend,
        clock=lambda: 10.0,
    )
    detector.start()

    for _ in range(8):
        bus.subscriptions[Topics.MIC][0]("audio/mic", _payload(np.zeros(320)))

    assert bus.published == []
    assert backend.calls == 1
    assert detector.health()["failed"] is True
