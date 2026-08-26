from yuki.interaction.audio_output import AudioPlayer


class FakeStream:
    def __init__(self, events):
        self.events = events
        self.active = True
        self.closed = False

    def is_active(self):
        return self.active

    def write(self, chunk):
        self.events.append(("write", chunk))

    def stop_stream(self):
        self.events.append(("stop", None))
        self.active = False

    def start_stream(self):
        self.events.append(("start", None))
        self.active = True

    def close(self):
        self.events.append(("close", None))
        self.closed = True


def test_first_chunk_callback_precedes_write_and_stream_restarts():
    events = []
    stream = FakeStream(events)
    player = AudioPlayer(stream_factory=lambda: stream)

    assert player.play_stream([b"one"], lambda: events.append(("speaking", None))) is True
    assert events[:2] == [("speaking", None), ("write", b"one")]

    player.stop()
    assert player.play_stream([b"two"]) is True
    assert events[-2:] == [("start", None), ("write", b"two")]
    player.close()


def test_empty_stream_does_not_call_first_chunk_callback():
    events = []
    player = AudioPlayer(stream_factory=lambda: FakeStream(events))
    assert player.play_stream([], lambda: events.append(("speaking", None))) is True
    assert events == []
    player.close()


def test_large_segment_is_split_into_configured_sample_chunks():
    events = []
    player = AudioPlayer(chunk_size=2, stream_factory=lambda: FakeStream(events))
    assert player.play_stream([b"abcdefghij"]) is True
    assert events == [
        ("write", b"abcd"),
        ("write", b"efgh"),
        ("write", b"ij"),
    ]
    player.close()


def test_pyaudio_import_is_lazy():
    imports = []
    player = AudioPlayer(module_loader=lambda name: imports.append(name))
    assert imports == []
    player.stop()
    player.close()
    assert imports == []
