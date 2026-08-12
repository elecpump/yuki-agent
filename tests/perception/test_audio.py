import numpy as np

from yuki.perception.audio import AudioCapture, AudioFrameSplitter


def test_splitter_frames_at_20ms():
    splitter = AudioFrameSplitter(sample_rate=16000, frame_ms=20)
    assert splitter.frame_len == 320
    samples = np.zeros(16000)  # 1 秒 = 50 帧
    frames = splitter.split(samples)
    assert len(frames) == 50
    assert all(f.shape == (320,) for f in frames)


def test_splitter_drops_tail():
    splitter = AudioFrameSplitter(sample_rate=16000, frame_ms=20)
    samples = np.zeros(321)  # 余 1 采样，丢弃
    frames = splitter.split(samples)
    assert len(frames) == 1
    assert frames[0].shape == (320,)


def test_splitter_empty():
    splitter = AudioFrameSplitter(sample_rate=16000, frame_ms=20)
    assert splitter.split(np.array([])).shape == (0,)


def test_capture_uses_fake_stream():
    published = []
    splitter = AudioFrameSplitter(sample_rate=16000, frame_ms=20)

    class FakeBus:
        def publish(self, topic, payload):
            published.append((topic, payload))

    class FakeStream:
        def __init__(self, callback):
            self.callback = callback

        def start(self):
            # 注入一帧 320 采样
            self.callback(np.zeros(320), 0, None, None)

    def fake_stream_factory(callback, **kwargs):
        return FakeStream(callback)

    cap = AudioCapture(
        FakeBus(),
        stream_factory=fake_stream_factory,
        splitter=splitter,
    )
    cap.start()
    assert len(published) == 1
    topic, payload = published[0]
    assert topic == "audio/mic"
    assert payload["sample_rate"] == 16000
    assert payload["pcm"] != ""
