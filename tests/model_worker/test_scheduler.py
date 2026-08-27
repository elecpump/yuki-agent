import threading

import pytest

from yuki.model_worker.scheduler import ModelInferenceScheduler, SchedulerBusyError


def test_scheduler_uses_bounded_queues():
    release = threading.Event()
    started = threading.Event()
    scheduler = ModelInferenceScheduler(concurrency=1, interactive_queue_size=1)

    def block():
        started.set()
        release.wait(1.0)

    first = scheduler.submit(block)
    assert started.wait(1.0)
    second = scheduler.submit(lambda: 2)
    try:
        with pytest.raises(SchedulerBusyError, match="interactive_queue_full"):
            scheduler.submit(lambda: 3)
    finally:
        release.set()
        first.result(timeout=1.0)
        assert second.result(timeout=1.0) == 2
        scheduler.close()


def test_interactive_work_runs_before_queued_background_work():
    release = threading.Event()
    started = threading.Event()
    order = []
    scheduler = ModelInferenceScheduler(concurrency=1)

    def blocker():
        started.set()
        release.wait(1.0)

    active = scheduler.submit(blocker)
    assert started.wait(1.0)
    background = scheduler.submit(lambda: order.append("background"), lane="background")
    interactive = scheduler.submit(lambda: order.append("interactive"), lane="interactive")
    release.set()
    active.result(timeout=1.0)
    interactive.result(timeout=1.0)
    background.result(timeout=1.0)
    scheduler.close()

    assert order == ["interactive", "background"]
