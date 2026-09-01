from __future__ import annotations

import time
import threading

import pytest

from yuki.bus import BusError, BusTimeoutError
from yuki.cognition.local_model_control import LocalChatControl, LocalModelControlError


class FakeHub:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.transitions: list[bool] = []

    def set_local_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.transitions.append(enabled)

    def local_enabled(self) -> bool:
        return self.enabled


class FakeRegistry:
    def __init__(self, hub: FakeHub | None = None) -> None:
        self.hub = hub
        self.health = {
            "runtime_state": "ready",
            "runtime_enabled": True,
            "callable": True,
            "loaded": True,
            "active_calls": 0,
        }
        self.submissions: list[tuple[bool, str, bool | None]] = []
        self.operation_state = "succeeded"
        self.operation_error: str | None = None

    def get_model_health(self, model: str) -> dict:
        assert model == "local_chat"
        return dict(self.health)

    def set_local_chat_enabled(self, enabled: bool, *, idempotency_key: str) -> dict:
        self.submissions.append(
            (enabled, idempotency_key, self.hub.local_enabled() if self.hub else None)
        )
        if not enabled:
            self.health.update(
                runtime_state="disabled",
                runtime_enabled=False,
                callable=False,
                loaded=False,
            )
        return {"operation_id": f"worker-{len(self.submissions)}", "accepted": True}

    def operation_status(self, operation_id: str) -> dict:
        return {
            "operation_id": operation_id,
            "state": self.operation_state,
            "error_code": self.operation_error,
        }


def eventually(predicate, timeout_s: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def settle_disabled_bootstrap(control: LocalChatControl, registry: FakeRegistry) -> None:
    registry.health.update(
        runtime_state="disabled",
        runtime_enabled=False,
        callable=False,
        loaded=False,
    )
    eventually(lambda: control.status()["state"] == "disabled")


def test_config_disabled_is_unavailable_and_rejects_changes() -> None:
    control = LocalChatControl(FakeRegistry(), FakeHub(), available=False)
    try:
        assert control.status() == {
            "available": False,
            "enabled": False,
            "target_enabled": False,
            "state": "unavailable",
            "runtime_state": "disabled",
            "loaded": False,
            "active_calls": 0,
            "operation": None,
            "last_error": "",
        }
        with pytest.raises(LocalModelControlError, match="local_model_config_disabled") as exc:
            control.set_enabled(True, "enable-1")
        assert exc.value.code == "local_model_config_disabled"
    finally:
        control.close()


def test_available_control_bootstraps_through_callable_health_before_opening_route() -> None:
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    control = LocalChatControl(registry, hub, available=True)
    try:
        assert hub.transitions[0] is False
        eventually(lambda: control.status()["state"] == "enabled")
        assert hub.local_enabled() is True
        assert registry.submissions == []
    finally:
        control.close()


def test_disable_closes_routing_before_worker_and_finishes_without_client_polling() -> None:
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    control = LocalChatControl(registry, hub, available=True)
    try:
        accepted = control.set_enabled(False, "disable-1")
        assert accepted["accepted"] is True
        assert accepted["target_enabled"] is False

        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        assert registry.submissions == [(False, "disable-1", False)]
        assert control.status()["state"] == "disabled"
        assert hub.local_enabled() is False
    finally:
        control.close()


def test_enable_opens_routing_only_after_worker_reports_callable() -> None:
    hub = FakeHub(enabled=False)
    registry = FakeRegistry(hub)
    registry.health.update(
        runtime_state="disabled",
        runtime_enabled=False,
        callable=False,
        loaded=False,
    )
    control = LocalChatControl(registry, hub, available=True, initially_enabled=False)
    try:
        settle_disabled_bootstrap(control, registry)
        registry.health.update(runtime_state="loading", runtime_enabled=True)
        accepted = control.set_enabled(True, "enable-1")
        eventually(lambda: len(registry.submissions) == 1)
        time.sleep(0.06)
        assert control.operation_status(accepted["operation_id"])["state"] == "running"
        assert hub.local_enabled() is False

        registry.health.update(runtime_state="ready", callable=True, loaded=True)
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        assert hub.local_enabled() is True
        assert control.status()["state"] == "enabled"
    finally:
        control.close()


def test_enable_request_closes_stale_route_until_callable_is_confirmed() -> None:
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    registry.operation_state = "running"
    control = LocalChatControl(registry, hub, available=True)
    try:
        control.set_enabled(True, "reconfirm-enable")
        assert hub.local_enabled() is False
    finally:
        control.close()


def test_stale_health_result_cannot_reopen_route_after_disable_request() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingHealthRegistry(FakeRegistry):
        def get_model_health(self, model: str) -> dict:
            started.set()
            assert release.wait(1.0)
            return super().get_model_health(model)

    hub = FakeHub(enabled=True)
    registry = BlockingHealthRegistry(hub)
    control = LocalChatControl(registry, hub, available=True)
    try:
        assert started.wait(1.0)
        control.set_enabled(False, "disable-during-health-check")
        transition_count = len(hub.transitions)
        release.set()
        time.sleep(0.05)
        assert True not in hub.transitions[transition_count:]
    finally:
        release.set()
        control.close()


def test_idempotency_key_is_bound_to_its_original_target() -> None:
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    registry.operation_state = "running"
    control = LocalChatControl(registry, hub, available=True)
    try:
        first = control.set_enabled(False, "switch-1")
        duplicate = control.set_enabled(False, "switch-1")
        assert duplicate == first

        with pytest.raises(LocalModelControlError) as exc:
            control.set_enabled(True, "switch-1")
        assert exc.value.code == "idempotency_key_conflict"
    finally:
        control.close()


def test_active_operation_is_shared_for_same_target_and_rejects_reversal() -> None:
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    registry.operation_state = "running"
    control = LocalChatControl(registry, hub, available=True)
    try:
        first = control.set_enabled(False, "disable-1")
        same_target = control.set_enabled(False, "disable-2")
        assert same_target["operation_id"] == first["operation_id"]

        with pytest.raises(LocalModelControlError) as exc:
            control.set_enabled(True, "enable-while-disabling")
        assert exc.value.code == "local_model_operation_in_progress"
    finally:
        control.close()


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (BusTimeoutError("late"), "model_worker_timeout"),
        (BusError("gone"), "model_worker_unavailable"),
    ],
)
def test_transient_submit_failure_recovers_in_background(failure, error_code: str) -> None:
    class FlakyRegistry(FakeRegistry):
        def __init__(self, hub: FakeHub) -> None:
            super().__init__(hub)
            self.failure = failure

        def set_local_chat_enabled(self, enabled: bool, *, idempotency_key: str) -> dict:
            if self.failure is not None:
                current, self.failure = self.failure, None
                raise current
            return super().set_local_chat_enabled(enabled, idempotency_key=idempotency_key)

    hub = FakeHub(enabled=True)
    registry = FlakyRegistry(hub)
    control = LocalChatControl(registry, hub, available=True, retry_delays=(0.2,))
    try:
        accepted = control.set_enabled(False, "disable-flaky")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "recovering"
        )
        assert control.operation_status(accepted["operation_id"])["error_code"] == error_code
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded",
            timeout_s=1.0,
        )
        assert hub.local_enabled() is False
    finally:
        control.close()


@pytest.mark.parametrize(
    "lost_status",
    [
        {"state": "cancelled", "error_code": None},
        {"ok": False, "error_code": "operation_not_found"},
    ],
)
def test_lost_worker_operation_is_resubmitted(lost_status: dict) -> None:
    class RestartedRegistry(FakeRegistry):
        def __init__(self, hub: FakeHub) -> None:
            super().__init__(hub)
            self.lost_status = lost_status

        def operation_status(self, operation_id: str) -> dict:
            if self.lost_status is not None:
                current, self.lost_status = self.lost_status, None
                return dict(current)
            return super().operation_status(operation_id)

    hub = FakeHub(enabled=True)
    registry = RestartedRegistry(hub)
    control = LocalChatControl(registry, hub, available=True, retry_delays=(0.2,))
    try:
        accepted = control.set_enabled(False, "disable-restarted")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "recovering"
        )
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        assert len(registry.submissions) == 2
    finally:
        control.close()


def test_deterministic_worker_failure_is_terminal_until_new_user_operation() -> None:
    hub = FakeHub(enabled=False)
    registry = FakeRegistry(hub)
    registry.health.update(runtime_state="disabled", runtime_enabled=False, callable=False)
    control = LocalChatControl(registry, hub, available=True, initially_enabled=False)
    try:
        settle_disabled_bootstrap(control, registry)
        registry.operation_state = "failed"
        registry.operation_error = "insufficient_vram"
        accepted = control.set_enabled(True, "enable-too-large")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"] == "failed"
        )
        time.sleep(0.1)
        assert len(registry.submissions) == 1
        assert hub.local_enabled() is False
        assert control.status()["last_error"] == "insufficient_vram"
    finally:
        control.close()


def test_worker_restart_reapplies_disabled_target_without_frontend_polling() -> None:
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    control = LocalChatControl(registry, hub, available=True, retry_delays=(0.02,))
    try:
        accepted = control.set_enabled(False, "disable-before-restart")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )

        registry.health.update(
            runtime_state="ready",
            runtime_enabled=True,
            callable=True,
            loaded=True,
        )
        eventually(lambda: len(registry.submissions) == 2)
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        assert hub.local_enabled() is False
        assert control.status()["state"] == "disabled"
    finally:
        control.close()


def test_worker_restart_closes_route_until_enabled_target_is_callable_again() -> None:
    hub = FakeHub(enabled=False)
    registry = FakeRegistry(hub)
    registry.health.update(runtime_state="disabled", runtime_enabled=False, callable=False)
    control = LocalChatControl(
        registry,
        hub,
        available=True,
        initially_enabled=False,
        retry_delays=(0.2,),
    )
    try:
        settle_disabled_bootstrap(control, registry)
        registry.health.update(
            runtime_state="ready",
            runtime_enabled=True,
            callable=True,
            loaded=True,
        )
        accepted = control.set_enabled(True, "enable-before-restart")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        assert hub.local_enabled() is True

        registry.health.update(runtime_state="loading", callable=False, loaded=False)
        eventually(lambda: control.status()["state"] == "recovering")
        assert hub.local_enabled() is False

        registry.health.update(runtime_state="ready", callable=True, loaded=True)
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        assert hub.local_enabled() is True
    finally:
        control.close()


def test_health_transport_loss_reopens_completed_operation_as_recovering() -> None:
    class HealthFlakyRegistry(FakeRegistry):
        health_failure: Exception | None = None

        def get_model_health(self, model: str) -> dict:
            if self.health_failure is not None:
                raise self.health_failure
            return super().get_model_health(model)

    hub = FakeHub(enabled=False)
    registry = HealthFlakyRegistry(hub)
    registry.health.update(runtime_state="disabled", runtime_enabled=False, callable=False)
    control = LocalChatControl(
        registry,
        hub,
        available=True,
        initially_enabled=False,
        retry_delays=(0.05,),
    )
    try:
        settle_disabled_bootstrap(control, registry)
        registry.health.update(
            runtime_state="ready",
            runtime_enabled=True,
            callable=True,
            loaded=True,
        )
        accepted = control.set_enabled(True, "enable-health-flaky")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )

        registry.health_failure = BusError("worker restarted")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "recovering"
        )
        assert hub.local_enabled() is False

        registry.health_failure = None
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        assert hub.local_enabled() is True
    finally:
        control.close()


def test_close_stops_background_reconciliation() -> None:
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    control = LocalChatControl(registry, hub, available=True, retry_delays=(0.01,))
    accepted = control.set_enabled(False, "disable-before-close")
    eventually(
        lambda: control.operation_status(accepted["operation_id"])["state"] == "succeeded"
    )

    control.close()
    registry.health.update(
        runtime_state="ready",
        runtime_enabled=True,
        callable=True,
        loaded=True,
    )
    time.sleep(0.05)
    assert len(registry.submissions) == 1


def test_close_reports_when_an_inflight_request_has_not_stopped() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingRegistry(FakeRegistry):
        def get_model_health(self, model: str) -> dict:
            started.set()
            assert release.wait(1.0)
            return super().get_model_health(model)

    control = LocalChatControl(BlockingRegistry(), FakeHub(), available=True)
    assert started.wait(1.0)
    assert control.close(timeout_s=0.01) is False
    release.set()
    assert control.close(timeout_s=1.0) is True


def test_completed_operation_expires_using_injected_clock() -> None:
    now = [10.0]
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    control = LocalChatControl(
        registry,
        hub,
        available=True,
        operation_ttl_s=5.0,
        clock=lambda: now[0],
    )
    try:
        accepted = control.set_enabled(False, "expiring-operation")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        now[0] += 6.0
        with pytest.raises(LocalModelControlError) as exc:
            control.operation_status(accepted["operation_id"])
        assert exc.value.code == "local_model_operation_not_found"
    finally:
        control.close()


def test_operation_expiry_does_not_stop_worker_restart_reconciliation() -> None:
    now = [10.0]
    hub = FakeHub(enabled=True)
    registry = FakeRegistry(hub)
    control = LocalChatControl(
        registry,
        hub,
        available=True,
        operation_ttl_s=5.0,
        retry_delays=(0.02,),
        clock=lambda: now[0],
    )
    try:
        accepted = control.set_enabled(False, "disable-before-expiry")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"]
            == "succeeded"
        )
        now[0] += 6.0
        with pytest.raises(LocalModelControlError):
            control.operation_status(accepted["operation_id"])
        time.sleep(0.06)
        assert len(registry.submissions) == 1

        registry.health.update(
            runtime_state="ready",
            runtime_enabled=True,
            callable=True,
            loaded=True,
        )
        eventually(lambda: len(registry.submissions) == 2)
        assert hub.local_enabled() is False
    finally:
        control.close()


def test_unexpected_worker_failure_becomes_terminal_operation_failed() -> None:
    class BrokenRegistry(FakeRegistry):
        def set_local_chat_enabled(self, enabled: bool, *, idempotency_key: str) -> dict:
            raise RuntimeError("implementation detail must not escape")

    hub = FakeHub(enabled=True)
    control = LocalChatControl(BrokenRegistry(hub), hub, available=True)
    try:
        accepted = control.set_enabled(False, "disable-broken")
        eventually(
            lambda: control.operation_status(accepted["operation_id"])["state"] == "failed"
        )
        assert control.operation_status(accepted["operation_id"])["error_code"] == (
            "operation_failed"
        )
    finally:
        control.close()
