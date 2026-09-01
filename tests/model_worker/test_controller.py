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


def test_controller_can_disable_and_reenable_model():
    unloaded = []
    handle = object()
    controller = ModelController(
        ManagedModelSpec(
            name="model",
            loader=lambda: handle,
            unloader=lambda value: unloaded.append(value),
        )
    )
    controller.load()

    controller.disable(timeout_s=0.1)

    assert unloaded == [handle]
    assert controller.snapshot()["runtime_state"] == "disabled"

    controller.enable()

    assert controller.snapshot()["runtime_state"] == "unloaded"


def test_enable_recovers_failed_model_without_a_handle_for_retry():
    attempts = {"count": 0}

    def load():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("boom")
        return object()

    controller = ModelController(ManagedModelSpec(name="model", loader=load))
    with pytest.raises(RuntimeError, match="boom"):
        controller.load()
    assert controller.snapshot()["runtime_state"] == "failed"

    controller.enable()

    assert controller.snapshot()["runtime_state"] == "unloaded"
    assert controller.load() is not None
    assert controller.snapshot()["runtime_state"] == "ready"


def test_disable_converges_failed_model_without_unloading_missing_handle():
    unloaded = []
    controller = ModelController(
        ManagedModelSpec(
            name="model",
            loader=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            unloader=lambda handle: unloaded.append(handle),
        )
    )
    with pytest.raises(RuntimeError, match="boom"):
        controller.load()

    controller.disable(timeout_s=0.1)

    assert unloaded == []
    assert controller.snapshot()["runtime_state"] == "disabled"


def test_failed_model_with_retained_handle_cannot_be_loaded_over():
    loads = []
    handle = object()
    controller = ModelController(
        ManagedModelSpec(
            name="model",
            loader=lambda: loads.append("load") or handle,
            unloader=lambda value: (_ for _ in ()).throw(RuntimeError("busy")),
        )
    )
    controller.load()
    with pytest.raises(RuntimeError, match="busy"):
        controller.unload(timeout_s=0.1)

    controller.enable()

    with pytest.raises(ModelUnavailableError, match="failed"):
        controller.load()
    assert loads == ["load"]


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
