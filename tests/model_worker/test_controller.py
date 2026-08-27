import threading
import time

import pytest

from yuki.model_worker.controller import (
    ManagedModelSpec,
    ModelController,
    ModelDrainTimeoutError,
    ModelReadinessState,
    ModelUnavailableError,
)


def test_controller_load_lease_and_unload_lifecycle():
    unloaded = []
    handle = object()
    controller = ModelController(
        ManagedModelSpec(
            name="model",
            loader=lambda: handle,
            unloader=lambda value: unloaded.append(value),
            min_residency_s=0,
        )
    )

    assert controller.snapshot()["runtime_state"] == "unloaded"
    assert controller.load() is handle
    with controller.lease() as leased:
        assert leased is handle
        assert controller.snapshot()["active_calls"] == 1
    assert controller.snapshot()["active_calls"] == 0
    assert controller.unload(timeout_s=0.1) is True
    assert unloaded == [handle]
    assert controller.snapshot()["runtime_state"] == "unloaded"


def test_draining_rejects_new_lease_and_times_out_without_unloading():
    controller = ModelController(
        ManagedModelSpec(name="model", loader=object, unloader=lambda handle: None)
    )
    controller.load()
    entered = threading.Event()
    release = threading.Event()

    def active_call():
        with controller.lease():
            entered.set()
            release.wait(1.0)

    thread = threading.Thread(target=active_call)
    thread.start()
    assert entered.wait(1.0)
    with pytest.raises(ModelDrainTimeoutError):
        controller.unload(timeout_s=0.01)
    assert controller.snapshot()["runtime_state"] == "draining"
    with pytest.raises(ModelUnavailableError):
        with controller.lease():
            pass
    release.set()
    thread.join(timeout=1.0)
    assert controller.snapshot()["active_calls"] == 0


def test_disabled_model_cannot_load():
    controller = ModelController(ManagedModelSpec(name="off", loader=object, enabled=False))
    assert controller.snapshot()["runtime_state"] == ModelReadinessState.DISABLED.value
    with pytest.raises(ModelUnavailableError, match="disabled"):
        controller.load()


def test_drain_timeout_requests_cooperative_cancel_and_stays_draining():
    class Handle:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    handle = Handle()
    controller = ModelController(ManagedModelSpec(name="model", loader=lambda: handle))
    controller.load()
    entered = threading.Event()
    release = threading.Event()

    def active_call():
        with controller.lease():
            entered.set()
            release.wait(1.0)

    thread = threading.Thread(target=active_call)
    thread.start()
    assert entered.wait(1.0)
    with pytest.raises(ModelDrainTimeoutError):
        controller.unload(timeout_s=0.01)

    assert handle.cancelled is True
    assert controller.snapshot()["runtime_state"] == "draining"
    release.set()
    thread.join(timeout=1.0)
