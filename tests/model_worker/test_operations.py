import threading
import time

import pytest

from yuki.model_worker.operations import ModelOperationFailure, ModelOperationStore


def _wait_for_state(store, operation_id, expected, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = store.status(operation_id)["state"]
        if state == expected:
            return store.status(operation_id)
        time.sleep(0.01)
    raise AssertionError(f"operation did not reach {expected}")


def test_submit_is_idempotent_and_operation_completes():
    calls = []
    store = ModelOperationStore(
        lambda action, model: calls.append((action, model)) or {"ok": True}
    )
    try:
        first = store.submit(idempotency_key="same", action="load", model="vlm")
        second = store.submit(idempotency_key="same", action="load", model="vlm")
        assert first == second
        status = _wait_for_state(store, first["operation_id"], "succeeded")
        assert status["result"] == {"ok": True}
        assert calls == [("load", "vlm")]
    finally:
        store.close()


def test_queued_operation_can_be_cancelled():
    release = threading.Event()

    def wait_for_release(action, model):
        del action, model
        release.wait(1.0)
        return {}

    store = ModelOperationStore(wait_for_release)
    try:
        first = store.submit(idempotency_key="first", action="load", model="a")
        _wait_for_state(store, first["operation_id"], "running")
        second = store.submit(idempotency_key="second", action="load", model="b")
        cancelled = store.cancel(second["operation_id"])
        assert cancelled == {"cancel_requested": True, "state": "cancelled"}
        assert store.status(second["operation_id"])["state"] == "cancelled"
    finally:
        release.set()
        store.close()


def test_unknown_operation_is_explicit():
    store = ModelOperationStore(lambda action, model: {})
    try:
        with pytest.raises(KeyError, match="operation_not_found"):
            store.status("missing")
    finally:
        store.close()


def test_operation_failure_preserves_structured_error_code():
    def fail(action, model):
        del action, model
        raise ModelOperationFailure("load_failed")

    store = ModelOperationStore(fail)
    try:
        submitted = store.submit(
            idempotency_key="failure",
            action="load",
            model="local_chat",
        )

        status = _wait_for_state(store, submitted["operation_id"], "failed")

        assert status["error_code"] == "load_failed"
    finally:
        store.close()


def test_close_cancels_queued_operations_and_rejects_new_submissions():
    release = threading.Event()

    def wait_for_release(action, model):
        del action, model
        release.wait(1.0)
        return {}

    store = ModelOperationStore(wait_for_release)
    first = store.submit(idempotency_key="running", action="load", model="a")
    _wait_for_state(store, first["operation_id"], "running")
    queued = store.submit(idempotency_key="queued", action="load", model="b")

    store.close()

    assert store.status(queued["operation_id"])["state"] == "cancelled"
    with pytest.raises(RuntimeError, match="operation_store_stopped"):
        store.submit(idempotency_key="new", action="load", model="c")
    release.set()
