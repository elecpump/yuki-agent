import threading
import time

from yuki.cognition.brain.soul_scheduler import SoulReflectionScheduler


class RecordingReflector:
    def __init__(self):
        self.calls = 0
        self.called = threading.Event()

    def reflect(self, *, cancelled=None):
        if cancelled is not None and cancelled():
            return False
        self.calls += 1
        self.called.set()
        return False


def test_scheduler_triggers_on_utterance_threshold():
    reflector = RecordingReflector()
    scheduler = SoulReflectionScheduler(
        reflector,
        every_utterances=2,
        interval_s=10.0,
    )
    scheduler.start()
    scheduler.on_utterance("one")
    assert reflector.calls == 0
    scheduler.on_utterance("two")

    assert reflector.called.wait(1.0)
    assert reflector.calls == 1
    scheduler.close(timeout_s=1.0)


def test_scheduler_ignores_utterances_before_start():
    reflector = RecordingReflector()
    scheduler = SoulReflectionScheduler(
        reflector,
        every_utterances=1,
        interval_s=10.0,
    )

    scheduler.on_utterance("before start")
    assert reflector.calls == 0

    scheduler.start()
    scheduler.on_utterance("after start")
    assert reflector.called.wait(1.0)
    assert reflector.calls == 1
    scheduler.close(timeout_s=1.0)


def test_scheduler_triggers_on_wall_clock_and_stops_cleanly():
    reflector = RecordingReflector()
    scheduler = SoulReflectionScheduler(
        reflector,
        every_utterances=100,
        interval_s=0.03,
    )
    scheduler.start()

    assert reflector.called.wait(1.0)
    scheduler.close(timeout_s=1.0)
    calls_after_close = reflector.calls
    time.sleep(0.06)
    assert reflector.calls == calls_after_close


def test_scheduler_coalesces_pending_triggers_and_never_overlaps_workers():
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    class BlockingReflector:
        def __init__(self):
            self.calls = 0
            self.concurrent = 0
            self.max_concurrent = 0
            self.lock = threading.Lock()

        def reflect(self, *, cancelled=None):
            with self.lock:
                self.calls += 1
                call = self.calls
                self.concurrent += 1
                self.max_concurrent = max(self.max_concurrent, self.concurrent)
            if call == 1:
                first_started.set()
                release_first.wait(1.0)
            with self.lock:
                self.concurrent -= 1
            if call == 2:
                second_finished.set()
            return False

    reflector = BlockingReflector()
    scheduler = SoulReflectionScheduler(
        reflector,
        every_utterances=1,
        interval_s=10.0,
    )
    scheduler.start()
    scheduler.on_utterance("one")
    assert first_started.wait(1.0)
    scheduler.on_utterance("two")
    scheduler.on_utterance("three")
    release_first.set()

    assert second_finished.wait(1.0)
    assert reflector.calls == 2
    assert reflector.max_concurrent == 1
    scheduler.close(timeout_s=1.0)


def test_scheduler_close_cancels_inflight_commit():
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class CancellableReflector:
        def __init__(self):
            self.committed = False

        def reflect(self, *, cancelled=None):
            started.set()
            release.wait(1.0)
            self.committed = not cancelled()
            return self.committed

    reflector = CancellableReflector()
    scheduler = SoulReflectionScheduler(
        reflector,
        every_utterances=1,
        interval_s=10.0,
    )
    scheduler.start()
    scheduler.on_utterance("one")
    assert started.wait(1.0)

    def close_scheduler():
        scheduler.close()
        closed.set()

    closer = threading.Thread(target=close_scheduler)
    closer.start()
    release.set()

    assert closed.wait(1.0)
    closer.join(timeout=1.0)
    assert reflector.committed is False


def test_scheduler_close_timeout_is_bounded_while_cloud_work_is_stuck():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class StuckReflector:
        def __init__(self):
            self.committed = False

        def reflect(self, *, cancelled=None):
            started.set()
            release.wait(1.0)
            self.committed = not cancelled()
            finished.set()
            return self.committed

    reflector = StuckReflector()
    scheduler = SoulReflectionScheduler(
        reflector,
        every_utterances=1,
        interval_s=10.0,
    )
    scheduler.start()
    scheduler.on_utterance("one")
    assert started.wait(1.0)

    before = time.monotonic()
    scheduler.close(timeout_s=0.02)
    elapsed = time.monotonic() - before

    assert elapsed < 0.2
    release.set()
    assert finished.wait(1.0)
    assert reflector.committed is False
