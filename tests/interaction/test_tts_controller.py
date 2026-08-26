import threading
import time

from yuki.interaction.tts_controller import EmotionMapper, TtsController
from yuki.topics import Topics

from tests.fakes import FakeBus


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


class FakeModel:
    def __init__(self, chunks=(b"pcm",)):
        self.chunks = chunks
        self.calls = []

    def synthesize_stream(self, text, emotion_vector=None):
        self.calls.append((text, emotion_vector))
        return iter(self.chunks)

    def health(self):
        return {"loaded": True, "degraded": False}


class FakePlayer:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.stop_calls = 0
        self.closed = False

    def play_stream(self, chunks, on_first_chunk=None):
        for chunk in chunks:
            if on_first_chunk is not None:
                on_first_chunk()
                on_first_chunk = None
            self.events.append(("write", chunk))
        return True

    def stop(self):
        self.stop_calls += 1
        self.events.append(("stop", None))

    def close(self):
        self.closed = True
        self.events.append(("close", None))


def test_controller_publishes_speaking_before_write_and_finished():
    events = []

    class EventBus(FakeBus):
        def publish(self, topic, payload):
            events.append((topic, payload["text"]))
            super().publish(topic, payload)

    player = FakePlayer(events)
    bus = EventBus()
    controller = TtsController(FakeModel(), player, bus)
    controller.speak("hello", emotion="joy")
    _wait_until(lambda: any(topic == Topics.TTS_FINISHED for topic, _ in bus.published))

    assert events[:3] == [
        (Topics.TTS_SPEAKING, "hello"),
        ("write", b"pcm"),
        (Topics.TTS_FINISHED, "hello"),
    ]
    lifecycle = [
        payload
        for topic, payload in bus.published
        if topic in {Topics.TTS_SPEAKING, Topics.TTS_FINISHED}
    ]
    assert all(payload["kind"] == "final" for payload in lifecycle)
    assert all(payload["reply_id"] is None for payload in lifecycle)
    controller.shutdown()


def test_controller_empty_or_failed_output_falls_back_without_events(capsys):
    bus = FakeBus()
    controller = TtsController(FakeModel(chunks=()), FakePlayer(), bus)
    controller.speak("fallback")
    _wait_until(lambda: "fallback" in capsys.readouterr().out)
    assert not any(
        topic in {Topics.TTS_SPEAKING, Topics.TTS_FINISHED}
        for topic, _ in bus.published
    )
    controller.shutdown()


def test_latest_job_replaces_pending_and_stale_output_is_dropped():
    release = threading.Event()

    class BlockingModel(FakeModel):
        def synthesize_stream(self, text, emotion_vector=None):
            self.calls.append((text, emotion_vector))
            if text == "A":
                release.wait(timeout=1.0)
            return iter([text.encode()])

    model = BlockingModel()
    player = FakePlayer()
    bus = FakeBus()
    controller = TtsController(model, player, bus)
    controller.speak("A")
    _wait_until(lambda: bool(model.calls))
    controller.speak("B")
    controller.speak("C")
    release.set()
    _wait_until(lambda: any(event == ("write", b"C") for event in player.events))

    assert ("write", b"A") not in player.events
    assert ("write", b"B") not in player.events
    assert [text for text, _ in model.calls] == ["A", "C"]
    controller.shutdown()


def test_new_reply_stops_active_playback_before_next_job():
    started = threading.Event()
    released = threading.Event()

    class BlockingPlayer(FakePlayer):
        def play_stream(self, chunks, on_first_chunk=None):
            for chunk in chunks:
                on_first_chunk()
                started.set()
                released.wait(timeout=1.0)
                if self.stop_calls:
                    return False
                self.events.append(("write", chunk))
            return True

        def stop(self):
            super().stop()
            released.set()

    player = BlockingPlayer()
    bus = FakeBus()
    controller = TtsController(FakeModel(), player, bus)
    controller.speak("old")
    assert started.wait(timeout=1.0)

    controller.speak("new")
    assert player.stop_calls == 1
    _wait_until(
        lambda: any(
            topic == Topics.TTS_FINISHED and payload["text"] == "old"
            for topic, payload in bus.published
        )
    )
    controller.shutdown()


def test_shutdown_stops_before_finished_and_closes_worker():
    started = threading.Event()
    released = threading.Event()
    events = []

    class EventBus(FakeBus):
        def publish(self, topic, payload):
            events.append((topic, payload["text"]))
            super().publish(topic, payload)

    class BlockingPlayer(FakePlayer):
        def play_stream(self, chunks, on_first_chunk=None):
            for chunk in chunks:
                on_first_chunk()
                started.set()
                released.wait(timeout=1.0)
                return False
            return True

        def stop(self):
            events.append(("stop", None))
            released.set()

        def close(self):
            events.append(("close", None))

    controller = TtsController(FakeModel(), BlockingPlayer(), EventBus())
    assert controller._thread.daemon is True
    controller.speak("active")
    assert started.wait(timeout=1.0)
    controller.shutdown()

    stop_index = events.index(("stop", None))
    finish_index = events.index((Topics.TTS_FINISHED, "active"))
    close_index = events.index(("close", None))
    assert stop_index < finish_index < close_index
    assert not controller._thread.is_alive()


def test_emotion_mapper_tolerates_missing_and_invalid_values():
    mapper = EmotionMapper()
    assert mapper.map(None) is None
    assert mapper.map("neutral") is None
    assert mapper.map("invalid") is None
    assert mapper.map("joy") == [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_final_replaces_pending_transition_without_synthesizing_it():
    model = FakeModel()
    player = FakePlayer()
    controller = TtsController(model, player, FakeBus())

    with controller._condition:
        controller.speak("transition", kind="transition", reply_id="r1")
        controller.speak("final", kind="final", reply_id="r1")

    _wait_until(lambda: ("write", b"pcm") in player.events)
    assert [text for text, _ in model.calls] == ["final"]
    controller.shutdown()


def test_final_invalidates_transition_still_synthesizing_without_stopping_player():
    synth_started = threading.Event()
    release_synth = threading.Event()

    class BlockingModel(FakeModel):
        def synthesize_stream(self, text, emotion_vector=None):
            self.calls.append((text, emotion_vector))
            if text == "transition":
                synth_started.set()
                assert release_synth.wait(1.0)
            return iter([text.encode()])

    model = BlockingModel()
    player = FakePlayer()
    controller = TtsController(model, player, FakeBus(), transition_grace_s=0.5)
    controller.speak("transition", kind="transition", reply_id="r1")
    assert synth_started.wait(1.0)

    controller.speak("final", kind="final", reply_id="r1")

    assert player.stop_calls == 0
    release_synth.set()
    _wait_until(lambda: ("write", b"final") in player.events)
    assert ("write", b"transition") not in player.events
    controller.shutdown()


def test_final_waits_for_matching_speaking_transition_to_finish_naturally():
    transition_started = threading.Event()
    release_transition = threading.Event()

    class TextModel(FakeModel):
        def synthesize_stream(self, text, emotion_vector=None):
            self.calls.append((text, emotion_vector))
            return iter([text.encode()])

    class BlockingPlayer(FakePlayer):
        def play_stream(self, chunks, on_first_chunk=None):
            for chunk in chunks:
                if on_first_chunk is not None:
                    on_first_chunk()
                    on_first_chunk = None
                if chunk == b"transition":
                    transition_started.set()
                    assert release_transition.wait(1.0)
                self.events.append(("write", chunk))
            return True

    player = BlockingPlayer()
    controller = TtsController(TextModel(), player, FakeBus(), transition_grace_s=0.5)
    controller.speak("transition", kind="transition", reply_id="r1")
    assert transition_started.wait(1.0)

    controller.speak("final", kind="final", reply_id="r1")
    assert player.stop_calls == 0
    release_transition.set()

    _wait_until(lambda: ("write", b"final") in player.events)
    assert player.events[:2] == [("write", b"transition"), ("write", b"final")]
    assert player.stop_calls == 0
    controller.shutdown()


def test_final_stops_matching_transition_after_grace_expires():
    transition_started = threading.Event()
    release_transition = threading.Event()

    class TextModel(FakeModel):
        def synthesize_stream(self, text, emotion_vector=None):
            self.calls.append((text, emotion_vector))
            return iter([text.encode()])

    class BlockingPlayer(FakePlayer):
        def play_stream(self, chunks, on_first_chunk=None):
            for chunk in chunks:
                if on_first_chunk is not None:
                    on_first_chunk()
                    on_first_chunk = None
                if chunk == b"transition":
                    transition_started.set()
                    assert release_transition.wait(1.0)
                    if self.stop_calls:
                        return False
                self.events.append(("write", chunk))
            return True

        def stop(self):
            super().stop()
            release_transition.set()

    player = BlockingPlayer()
    controller = TtsController(TextModel(), player, FakeBus(), transition_grace_s=0.01)
    controller.speak("transition", kind="transition", reply_id="r1")
    assert transition_started.wait(1.0)

    controller.speak("final", kind="final", reply_id="r1")

    _wait_until(lambda: ("write", b"final") in player.events)
    assert player.stop_calls == 1
    assert ("write", b"transition") not in player.events
    controller.shutdown()


def test_transition_is_dropped_while_final_is_active_without_stopping_it():
    final_started = threading.Event()
    release_final = threading.Event()

    class TextModel(FakeModel):
        def synthesize_stream(self, text, emotion_vector=None):
            self.calls.append((text, emotion_vector))
            return iter([text.encode()])

    class BlockingPlayer(FakePlayer):
        def play_stream(self, chunks, on_first_chunk=None):
            for chunk in chunks:
                on_first_chunk()
                final_started.set()
                assert release_final.wait(1.0)
                self.events.append(("write", chunk))
            return True

    model = TextModel()
    player = BlockingPlayer()
    controller = TtsController(model, player, FakeBus())
    controller.speak("final", kind="final", reply_id="r1")
    assert final_started.wait(1.0)

    controller.speak("transition", kind="transition", reply_id="r2")

    assert player.stop_calls == 0
    release_final.set()
    _wait_until(lambda: ("write", b"final") in player.events)
    assert [text for text, _ in model.calls] == ["final"]
    controller.shutdown()


def test_cancel_only_stops_matching_identified_transition():
    transition_started = threading.Event()
    release_transition = threading.Event()

    class BlockingPlayer(FakePlayer):
        def play_stream(self, chunks, on_first_chunk=None):
            for chunk in chunks:
                on_first_chunk()
                transition_started.set()
                assert release_transition.wait(1.0)
                return False
            return True

        def stop(self):
            super().stop()
            release_transition.set()

    player = BlockingPlayer()
    controller = TtsController(FakeModel(), player, FakeBus())
    controller.speak("transition", kind="transition", reply_id="r1")
    assert transition_started.wait(1.0)

    controller.cancel(None)
    controller.cancel("other")
    assert player.stop_calls == 0
    controller.cancel("r1")

    assert player.stop_calls == 1
    controller.shutdown()


def test_cancel_never_stops_a_final_with_the_same_reply_id():
    final_started = threading.Event()
    release_final = threading.Event()

    class BlockingPlayer(FakePlayer):
        def play_stream(self, chunks, on_first_chunk=None):
            for chunk in chunks:
                on_first_chunk()
                final_started.set()
                assert release_final.wait(1.0)
                self.events.append(("write", chunk))
            return True

    player = BlockingPlayer()
    controller = TtsController(FakeModel(), player, FakeBus())
    controller.speak("final", kind="final", reply_id="r1")
    assert final_started.wait(1.0)

    controller.cancel("r1")

    assert player.stop_calls == 0
    release_final.set()
    _wait_until(lambda: ("write", b"pcm") in player.events)
    controller.shutdown()
